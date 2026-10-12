# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from bedrock_bench.adapters.bdsfamily import BDS_DIALECT, parse_querytarget
from bedrock_bench.adapters.endstone import ENDSTONE_DIALECT
from bedrock_bench.clock import Stamp, mono_ns
from bedrock_bench.console import Console, clean, parse_line, read_journal, read_log

LOGS = Path(__file__).resolve().parents[1] / "fixtures" / "logs"


def session(name: str) -> bytes:
    return (LOGS / name).read_bytes()


class FakeClock:
    """Monotonic stamps that advance 1 ms per read, with a fixed epoch."""

    def __init__(self) -> None:
        self.mono = 1_000_000_000
        self.lock = threading.Lock()

    def __call__(self) -> Stamp:
        with self.lock:
            self.mono += 1_000_000
            return Stamp(self.mono, 1_767_268_800_000 + (self.mono - 1_000_000_000) // 1_000_000)


class RecordingStdin:
    """Collects writes; can hold the first write until ``gate`` is set, or fail like a closed pipe."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.writes: list[tuple[int, bytes]] = []
        self.gate = gate
        self.broken = False
        self.first_write = threading.Event()

    def write(self, data: bytes) -> int:
        self.first_write.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.broken:
            raise BrokenPipeError("pipe closed")
        self.writes.append((mono_ns(), data))
        return len(data)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("pipe closed")

    @property
    def texts(self) -> list[bytes]:
        return [data for _, data in self.writes]


@pytest.fixture
def pipe() -> Iterator[tuple[io.BufferedReader, io.BufferedWriter]]:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer = os.fdopen(write_fd, "wb")
    yield reader, writer
    for fh in (writer, reader):
        try:
            fh.close()
        except OSError:
            pass


def drained(console: Console, timeout: float = 5.0) -> Console:
    assert console.eof.wait(timeout), "reader did not reach end of file"
    return console


# --------------------------------------------------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------------------------------------------------


class TestLines:
    def test_clean(self) -> None:
        assert clean(b"\x1b[33m\x1b[1m[12:00:00 WARNING]: careful\x1b[0m\r\n") == "[12:00:00 WARNING]: careful"
        assert clean(b"bad \xff byte\n") == "bad � byte"
        assert clean("\x1b]0;title\x07text") == "text"

    def test_bds_prefix(self) -> None:
        line = parse_line("[2026-01-01 12:00:00:003 WARN] Content logging", 1, 2, BDS_DIALECT)
        assert (line.level, line.logger, line.text) == ("warn", None, "Content logging")
        first = parse_line("NO LOG FILE! - setting up server logging...", 1, 2, BDS_DIALECT)
        assert (first.level, first.text) == (None, "setting up server logging...")
        continuation = parse_line('      "dimension" : 0,', 1, 2, BDS_DIALECT)
        assert continuation.level is None and continuation.text == '      "dimension" : 0,'

    def test_endstone_prefix_and_logger(self) -> None:
        line = parse_line("[12:00:01 INFO]: [ExamplePlugin] Enabling v1", 1, 2, ENDSTONE_DIALECT)
        assert (line.level, line.logger, line.text) == ("info", "ExamplePlugin", "Enabling v1")
        crit = parse_line("[12:00:26 CRITICAL]: Something went badly wrong", 1, 2, ENDSTONE_DIALECT)
        assert crit.level == "fatal"
        json_line = parse_line("[12:00:20 INFO]:       },", 1, 2, ENDSTONE_DIALECT)
        assert json_line.logger is None and json_line.text == "      },"
        empty = parse_line("[12:00:20 INFO]:", 1, 2, ENDSTONE_DIALECT)
        assert empty.text == "" and empty.level == "info"

    def test_format_codes_only_leave_text(self) -> None:
        line = parse_line("[12:00:00 INFO]: §aGreen§r text", 1, 2, ENDSTONE_DIALECT)
        assert line.text == "Green text"
        assert "§a" in line.raw

    def test_without_dialect(self) -> None:
        line = parse_line("[12:00:00 INFO]: x", 5, 6)
        assert (line.t_ns, line.epoch_ms, line.level, line.text) == (5, 6, None, "[12:00:00 INFO]: x")


# --------------------------------------------------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------------------------------------------------


class TestReader:
    def test_bds_session(self, tmp_path: Path) -> None:
        log_path = tmp_path / "server.log.gz"
        console = Console(
            io.BytesIO(session("bds-session.log")), None, BDS_DIALECT, log_path=log_path, clock=FakeClock()
        )
        drained(console.start())
        console.close()
        lines = console.lines()
        assert len(lines) == session("bds-session.log").count(b"\n")
        assert console.ready.is_set() and console.ready_line is not None
        assert console.ready_line.text == "Server started."
        assert console.banner_version == "1.26.52.3"
        assert set(console.in_world) == {"a00", "a01"}
        assert [line.t_ns for line in lines] == sorted(line.t_ns for line in lines)
        assert len({line.t_ns for line in lines}) == len(lines)

        reloaded = read_log(log_path, BDS_DIALECT)
        assert reloaded == lines
        assert [line.raw for line in read_log(log_path)] == [line.raw for line in lines]

    def test_endstone_session(self) -> None:
        console = Console(io.BytesIO(session("endstone-session.log")), None, ENDSTONE_DIALECT, clock=FakeClock())
        drained(console.start())
        assert console.banner_version == "0.11.14"
        assert console.ready.is_set()
        assert set(console.in_world) == {"a00", "a01"}
        levels = {line.level for line in console.lines()}
        assert {"info", "warn", "error", "fatal"} <= levels

    def test_querytarget_across_prefixed_lines(self) -> None:
        for name, dialect in (("bds-session.log", BDS_DIALECT), ("endstone-session.log", ENDSTONE_DIALECT)):
            console = Console(io.BytesIO(session(name)), None, dialect, clock=FakeClock())
            drained(console.start())
            positions = parse_querytarget(console.lines())
            assert positions is not None and len(positions) == 2, name
            assert positions[0].x == 1280.5 and positions[0].z == 1280.5

    def test_crlf_and_ansi(self) -> None:
        raw = b"\x1b[32m[12:00:00 INFO]: Server started.\x1b[0m\r\n[12:00:01 INFO]: Gametime is 7\r\n"
        console = Console(io.BytesIO(raw), None, ENDSTONE_DIALECT, clock=FakeClock())
        drained(console.start())
        assert [line.text for line in console.lines()] == ["Server started.", "Gametime is 7"]
        assert console.ready.is_set()

    def test_wait_for(self, pipe: tuple[io.BufferedReader, io.BufferedWriter]) -> None:
        reader, writer = pipe
        console = Console(reader, None, BDS_DIALECT).start()
        assert console.wait_for("Gametime", timeout=0.05) is None
        mark = console.mark()

        def later() -> None:
            time.sleep(0.05)
            writer.write(b"[2026-01-01 12:00:00:000 INFO] Gametime is 5\n")
            writer.flush()

        threading.Thread(target=later).start()
        line = console.wait_for(BDS_DIALECT.tick_re, after=mark, timeout=5)
        assert line is not None and BDS_DIALECT.parse_tick(line.text) == 5
        writer.close()
        assert console.eof.wait(5)
        assert console.wait_for("never", timeout=5) is None, "end of file ends the wait"
        console.close()

    def test_wait_ready(self) -> None:
        console = Console(io.BytesIO(session("bds-session.log")), None, BDS_DIALECT).start()
        assert console.wait_ready(timeout=5)
        late = Console(io.BytesIO(b"[2026-01-01 12:00:00:000 INFO] Starting Server\n"), None, BDS_DIALECT).start()
        assert not late.wait_ready(timeout=5)

    def test_subscriber_errors_do_not_stop_the_reader(self) -> None:
        seen: list[str] = []
        console = Console(io.BytesIO(session("bds-session.log")), None, BDS_DIALECT)

        def broken(line: object) -> None:
            raise RuntimeError("subscriber bug")

        console.subscribe(broken)
        unsubscribe = console.subscribe(lambda line: seen.append(line.text))
        drained(console.start())
        assert len(seen) == len(console.lines())
        unsubscribe()

    def test_truncated_log_is_readable(self, tmp_path: Path) -> None:
        log_path = tmp_path / "server.log.gz"
        console = Console(io.BytesIO(session("bds-session.log")), None, BDS_DIALECT, log_path=log_path)
        drained(console.start())
        console.close()
        data = log_path.read_bytes()
        cut = tmp_path / "cut.log.gz"
        cut.write_bytes(data[: len(data) - 12])
        assert len(read_log(cut)) <= len(console.lines())


# --------------------------------------------------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------------------------------------------------


class TestWriter:
    def test_lf_only_utf8(self, tmp_path: Path) -> None:
        stdin = RecordingStdin()
        console = Console(io.BytesIO(b""), stdin, BDS_DIALECT, pace_s=0, journal_path=tmp_path / "c.jsonl.gz").start()
        command = console.send("say héllo")
        assert command.wait(5)
        console.close()
        assert stdin.texts == ["say héllo\n".encode()]
        assert b"\r" not in stdin.texts[0]

    def test_priority_lane_goes_first(self, tmp_path: Path) -> None:
        gate = threading.Event()
        stdin = RecordingStdin(gate)
        journal = tmp_path / "commands.jsonl.gz"
        console = Console(io.BytesIO(b""), stdin, BDS_DIALECT, pace_s=0, journal_path=journal).start()
        console.send("first")
        assert stdin.first_write.wait(5)
        n1 = console.send("normal 1", origin="chaos")
        n2 = console.send("normal 2", origin="chaos")
        p1 = console.send("time query gametime", lane="priority", origin="probe")
        assert console.backlog() == 3
        gate.set()
        assert n2.wait(5) and n1.sent and p1.sent
        console.close()
        assert stdin.texts == [b"first\n", b"time query gametime\n", b"normal 1\n", b"normal 2\n"]
        records = read_journal(journal)
        assert [r["text"] for r in records] == ["first", "time query gametime", "normal 1", "normal 2"]
        assert [r["lane"] for r in records] == ["normal", "priority", "normal", "normal"]
        assert [r["origin"] for r in records] == ["phase", "probe", "chaos", "chaos"]
        assert all(r["send_ns"] >= r["enqueue_ns"] for r in records)

    def test_on_sent_runs_before_the_write(self) -> None:
        events: list[tuple[str, int]] = []

        class OrderedStdin(RecordingStdin):
            def write(self, data: bytes) -> int:
                events.append(("write", len(events)))
                return super().write(data)

        console = Console(io.BytesIO(b""), OrderedStdin(), BDS_DIALECT, pace_s=0).start()
        command = console.send(
            "time query gametime", lane="priority", origin="probe", on_sent=lambda ns: events.append(("sent", ns))
        )
        assert command.wait(5)
        console.close()
        assert [kind for kind, _ in events] == ["sent", "write"]
        assert events[0][1] == command.send_ns

    def test_pacing(self) -> None:
        stdin = RecordingStdin()
        console = Console(io.BytesIO(b""), stdin, BDS_DIALECT, pace_s=0.02).start()
        commands = [console.send(f"c{i}") for i in range(5)]
        assert commands[-1].wait(5)
        console.close()
        times = [t for t, _ in stdin.writes]
        gaps = [(b - a) / 1e9 for a, b in zip(times, times[1:], strict=False)]
        assert min(gaps) >= 0.018, gaps

    def test_rate_source_stretches_the_gap(self) -> None:
        stdin = RecordingStdin()
        console = Console(io.BytesIO(b""), stdin, BDS_DIALECT, pace_s=0, rate=lambda: 25.0).start()
        commands = [console.send(f"c{i}") for i in range(3)]
        assert commands[-1].wait(5)
        console.close()
        times = [t for t, _ in stdin.writes]
        assert min((b - a) / 1e9 for a, b in zip(times, times[1:], strict=False)) >= 0.035

    def test_rejects_bad_commands(self) -> None:
        console = Console(io.BytesIO(b""), RecordingStdin(), BDS_DIALECT)
        with pytest.raises(ValueError):
            console.send("two\nlines")
        with pytest.raises(ValueError):
            console.send("x", lane="express")
        with pytest.raises(ValueError):
            console.send("x", origin="someone")

    def test_dead_stdin_never_raises(self, tmp_path: Path) -> None:
        stdin = RecordingStdin()
        stdin.broken = True
        journal = tmp_path / "commands.jsonl.gz"
        console = Console(io.BytesIO(b""), stdin, BDS_DIALECT, pace_s=0, journal_path=journal).start()
        first = console.send("stop")
        assert not first.wait(5)
        assert first.error and "closed" in first.error and first.send_ns is None
        second = console.send("list")
        assert second.done and not second.sent
        assert not console.writable
        console.close()
        assert [r["error"] is not None for r in read_journal(journal)] == [True, True]

    def test_no_stdin(self) -> None:
        console = Console(io.BytesIO(b""), None, BDS_DIALECT).start()
        assert not console.send("list").sent
        console.close()

    def test_close_fails_pending(self) -> None:
        gate = threading.Event()
        stdin = RecordingStdin(gate)
        console = Console(io.BytesIO(b""), stdin, BDS_DIALECT, pace_s=0).start()
        console.send("blocks")
        assert stdin.first_write.wait(5)
        pending = console.send("never sent")
        assert console.writer_lag_s() >= 0
        closer = threading.Thread(target=console.close)
        closer.start()
        assert pending.wait(5) is False
        gate.set()
        closer.join(5)
        assert pending.error and "closed" in pending.error
        assert console.send("after close").error == "console closed"


class EchoServer:
    """Stands in for a server's stdin: answers every ``time query gametime`` with a gametime line on its stdout."""

    def __init__(self, stdout: io.BufferedWriter) -> None:
        self.stdout = stdout
        self.tick = 100

    def write(self, data: bytes) -> int:
        if data.strip() == b"time query gametime":
            self.tick += 100
            self.stdout.write(f"[2026-01-01 12:00:00:000 INFO] Gametime is {self.tick}\n".encode())
            self.stdout.flush()
        return len(data)

    def flush(self) -> None:
        pass


class TestRequest:
    def test_request_reply(self, pipe: tuple[io.BufferedReader, io.BufferedWriter]) -> None:
        reader, writer = pipe
        console = Console(reader, EchoServer(writer), BDS_DIALECT, pace_s=0).start()
        writer.write(b"[2026-01-01 12:00:00:000 INFO] Gametime is 1\n")
        writer.flush()
        reply = console.request("time query gametime", BDS_DIALECT.tick_re, timeout=5, lane="priority", origin="probe")
        assert reply.ok and reply.match is not None and int(reply.match["tick"]) == 200
        assert reply.line is not None and reply.command.send_ns is not None
        assert reply.line.t_ns >= reply.command.send_ns
        silent = console.request("list", "never printed", timeout=0.1)
        assert not silent.ok and silent.command.sent
        writer.close()
        console.close()
