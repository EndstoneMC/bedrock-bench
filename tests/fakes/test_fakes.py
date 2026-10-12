# SPDX-License-Identifier: Apache-2.0
"""Self-tests of the fakes: the fake server in every family, the fake bots, the fake adapters and their registration.

These run on every test invocation and need nothing beyond the package itself; the end-to-end runs that drive the
harness through the fakes live in ``tests/integration``.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
import pytest

from bedrock_bench import adapters, bots, config
from bedrock_bench.cache import Cache
from bedrock_bench.model import LaunchSpec, Layout, LogLine, OverrideCategory, Placement, RunContext
from bedrock_bench.workload import WORKLOAD, with_overrides
from fakes import fake_bots, fake_server
from fakes.bench import ENV_BOT_CMD, FakeBench, fake_workload, free_port, register_entry_point
from fakes.fake_adapter import (
    BOTS_SCRIPT,
    ENV_BEHAVIOUR,
    FAKE_ADAPTERS,
    FakeAdapter,
    FakeBds,
    FakeEndstone,
    FakeNukkit,
    interpreter,
    parse_pos_lines,
    parse_querytarget,
)

BOT_EVENTS = (
    "hello dial spawned join_failed tp first_chunk progress stat pkt rtt died respawned dim left dropped rejoin "
    "kicked decode_error alljoined bot_summary done"
).split()


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def make_adapter(cls: type[FakeAdapter], port: int, behaviour: str | None = None) -> FakeAdapter:
    machine = config.parse_machine({"runtime": {"port": port}}, label="fakes")
    return cls(config.arm_from_ref(f"{cls.name}@1.0.0"), machine, behaviour)


def make_context(adapter: FakeAdapter, tmp_path: Path) -> RunContext:
    return RunContext(
        run_id="fake-run",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        workload=fake_workload(),
        machine=adapter.machine,
        placement=Placement((0,), (1,), (1,), "normal", "physical"),
    )


def prepared(
    cls: type[FakeAdapter], tmp_path: Path, behaviour: str | None = None
) -> tuple[FakeAdapter, Layout, LaunchSpec]:
    adapter = make_adapter(cls, free_port(), behaviour)
    install = adapter.fetch(Cache(tmp_path / "cache"))
    layout = adapter.prepare(install, make_context(adapter, tmp_path))
    return adapter, layout, adapter.launch(layout)


class Console:
    """A launched fake server: stamped output lines, commands in, waits on the adapter's dialect."""

    def __init__(self, adapter: FakeAdapter, spec: LaunchSpec) -> None:
        self.dialect = adapter.dialect
        env = {k: v for k, v in os.environ.items() if k not in spec.env_remove} | dict(spec.env_set)
        self.proc = subprocess.Popen(
            spec.argv, cwd=spec.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        for line in spec.stdin_lines:
            self.send(line)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for data in self.proc.stdout:
            self.lines.put(data.decode("utf-8", errors="replace").rstrip("\r\n"))

    def send(self, command: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((command + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait_for(self, predicate: Callable[[str], Any], timeout: float = 5.0) -> Any:
        """The first truthy ``predicate(text)`` over prefix-stripped lines that arrive within ``timeout``."""
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                break
            self.seen.append(line)
            result = predicate(self.dialect.strip(line))
            if result:
                return result
        raise AssertionError("expected console line never came; output so far:\n" + "\n".join(self.seen[-40:]))

    def probe(self) -> int:
        self.send(self.dialect.tick_cmd)

        def tick(text: str) -> tuple[int] | None:
            value = self.dialect.parse_tick(text)
            return None if value is None else (value,)

        return self.wait_for(tick)[0]

    def close(self, timeout: float = 5.0) -> int:
        if self.proc.poll() is None:
            self.send(self.dialect.stop_cmd)
        try:
            return self.proc.wait(timeout)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


@contextmanager
def running(adapter: FakeAdapter, spec: LaunchSpec) -> Iterator[Console]:
    console = Console(adapter, spec)
    try:
        yield console
    finally:
        if console.proc.poll() is None:
            for child in _children(console.proc.pid):
                child.kill()
            console.proc.kill()
            console.proc.wait(5)


def _children(pid: int) -> list[psutil.Process]:
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


class RawBot:
    """One connection speaking the fake server's protocol directly."""

    def __init__(self, port: int, name: str, n: int = 1) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        self.stream = self.sock.makefile("rb")
        self.send({"op": "login", "name": name, "i": int(name[1:]), "n": n, "radius": 16, "xuid": "", "uuid": ""})

    def send(self, message: dict[str, Any]) -> None:
        self.sock.sendall((json.dumps(message) + "\n").encode("utf-8"))

    def recv(self, op: str) -> dict[str, Any]:
        while True:
            data = self.stream.readline()
            assert data, f"connection closed while waiting for {op!r}"
            message = json.loads(data)
            if message["op"] == op:
                return message

    def at_eof(self) -> bool:
        return self.stream.readline() == b""

    def close(self) -> None:
        self.sock.close()


def run_bots(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    """Run the fake bots to completion with stdin held open, since end of input means quit."""
    proc = subprocess.Popen(
        [interpreter(), "-I", "-u", str(BOTS_SCRIPT), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    output: dict[str, str] = {}

    def read(name: str, stream: Any) -> None:
        output[name] = stream.read()

    readers = [threading.Thread(target=read, args=(name, getattr(proc, name))) for name in ("stdout", "stderr")]
    for reader in readers:
        reader.start()
    try:
        code = proc.wait(timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
        assert proc.stdin is not None
        proc.stdin.close()
    for reader in readers:
        reader.join(5)
    return subprocess.CompletedProcess(proc.args, code, output.get("stdout", ""), output.get("stderr", ""))


def events_of(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------------------------------------------------
# Fake server
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", FAKE_ADAPTERS, ids=lambda cls: cls.name)
def test_fake_server_session(cls: type[FakeAdapter], tmp_path: Path) -> None:
    adapter, layout, spec = prepared(cls, tmp_path)
    port = adapter.machine.port
    with running(adapter, spec) as console:
        dialect = adapter.dialect
        version = console.wait_for(dialect.parse_version)
        assert version == "1.0.0"
        console.wait_for(dialect.is_ready)
        assert (layout.server_dir / "worlds" / "bench").is_dir()

        first = console.probe()
        time.sleep(0.4)
        assert 2 <= console.probe() - first <= 20, "the tick counter should advance at about 20 TPS"

        bot = RawBot(port, "a00")
        assert console.wait_for(dialect.player_entered) == "a00"
        assert bot.recv("spawn")["radius"] == 4

        console.send(adapter.cmd_tp("a00", 1280.5, 300, 1920.5))
        tp = bot.recv("tp")
        assert (tp["x"], tp["y"], tp["z"]) == (1280.5, 300.0, 1920.5)

        command = adapter.cmd_positions()
        if command is not None:
            console.send(command)
            collected: list[LogLine] = []

            def positions(text: str) -> Any:
                collected.append(LogLine(0, 0, None, None, text, text))
                return adapter.parse_positions(collected)

            (pos,) = console.wait_for(positions)
            assert (pos.x, pos.y, pos.z) == (1280.5, 300.0, 1920.5)

        if cls.family == "endstone":
            server = adapter.server_process(psutil.Process(console.proc.pid))
            assert server.pid != console.proc.pid
            assert "server" in server.cmdline()

        assert console.close() == 0
        assert bot.at_eof()
        bot.close()


def test_nukkit_reads_the_language_line(tmp_path: Path) -> None:
    adapter, _, spec = prepared(FakeNukkit, tmp_path)
    assert spec.stdin_lines == ("eng",)
    with running(adapter, spec) as console:
        console.wait_for(lambda text: text == "Selected English (eng) as the base language")
        assert console.close() == 0


def test_crash_writes_a_report_the_adapter_collects(tmp_path: Path) -> None:
    started = int(time.time() * 1000) - 1000
    adapter, layout, spec = prepared(FakeBds, tmp_path, behaviour="crash-at-boot")
    with running(adapter, spec) as console:
        assert console.proc.wait(10) == 3
    (report,) = adapter.crash_artifacts(layout, started)
    assert report.parent.name == "crash_reports"
    assert "scripted crash during boot" in report.read_text(encoding="utf-8")


def test_tick_counter_can_stall_without_players(tmp_path: Path) -> None:
    adapter, _, spec = prepared(FakeBds, tmp_path, behaviour="tick-stalls-idle")
    with running(adapter, spec) as console:
        console.wait_for(adapter.dialect.is_ready)
        first = console.probe()
        time.sleep(0.3)
        assert console.probe() == first
        assert console.close() == 0


def test_launcher_family_runs_the_server_as_a_child(tmp_path: Path) -> None:
    adapter, _, spec = prepared(FakeEndstone, tmp_path)
    with running(adapter, spec) as console:
        console.wait_for(adapter.dialect.is_ready)
        launched = psutil.Process(console.proc.pid)
        assert adapter.server_process(launched) in launched.children(recursive=True)
        assert console.close() == 0


def test_shipped_behaviour_scripts_are_valid() -> None:
    for path in sorted((Path(fake_server.__file__).parent / "scripts").glob("*.json")):
        script = fake_server.load_script(path)
        assert script["description"], f"{path.name} needs a description"


@pytest.mark.parametrize(
    "script",
    [
        {"bogus": 1},
        {"tps": "fast"},
        {"positions": "sometimes"},
        {"events": [{"action": "explode"}]},
        {"events": [{"action": "crash", "anchor": "command:("}]},
        {"events": [{"action": "crash", "anchor": "noon"}]},
        {"events": [{"action": "tps", "value": 5, "color": "red"}]},
    ],
)
def test_bad_behaviour_scripts_are_rejected(script: dict[str, Any]) -> None:
    with pytest.raises(fake_server.ScriptError):
        fake_server.load_script(script)


# --------------------------------------------------------------------------------------------------------------------
# Fake bots
# --------------------------------------------------------------------------------------------------------------------


def test_bot_selftest_output_matches_the_schema(schema_validator: Callable[[str], Any]) -> None:
    result = run_bots("-selftest-output")
    assert result.returncode == 0, result.stderr
    events = events_of(result.stdout)
    validator = schema_validator("bot-event")
    for event in events:
        validator.validate(event)
    assert sorted({e["ev"] for e in events}) == sorted(BOT_EVENTS)


def test_bot_version_is_json() -> None:
    result = run_bots("-version")
    assert result.returncode == 0
    build = json.loads(result.stdout)
    assert build["module"] == "loadbot"
    assert build["protocol"] == 2193


@pytest.mark.parametrize("args", [["-addr", "127.0.0.1:1", "-bogus"], ["-n", "2"], ["-addr", "x:1", "-n", "0"]])
def test_bot_usage_errors_exit_1(args: list[str]) -> None:
    assert run_bots(*args).returncode == 1


@pytest.mark.parametrize(
    ("text", "seconds"), [("300ms", 0.3), ("1m30s", 90.0), ("0", 0.0), ("1.5s", 1.5), ("2h", 7200.0)]
)
def test_go_durations(text: str, seconds: float) -> None:
    assert fake_bots.go_duration(text) == pytest.approx(seconds)


def test_bots_exit_2_when_every_join_fails(schema_validator: Callable[[str], Any]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def hang_up() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.close()

        threading.Thread(target=hang_up, daemon=True).start()
        port = listener.getsockname()[1]
        result = run_bots("-addr", f"127.0.0.1:{port}", "-n", "2", "-stagger", "10ms", "-spawn-timeout", "2s")
    assert result.returncode == 2, result.stderr
    events = events_of(result.stdout)
    assert [e["stage"] for e in events if e["ev"] == "join_failed"] == ["spawn", "spawn"]
    assert [e for e in events if e["ev"] == "alljoined"][0]["joined"] == 0
    assert events[-1]["ev"] == "done"
    for event in events:
        schema_validator("bot-event").validate(event)


def _bots(port: int, *extra: str) -> subprocess.Popen[str]:
    args = ["-addr", f"127.0.0.1:{port}", "-prefix", "a", "-stagger", "20ms", "-walk", "none"]
    args += ["-progress-interval", "200ms", "-stat-interval", "200ms", "-pkt-interval", "500ms", "-spawn-timeout=5s"]
    return subprocess.Popen(
        [interpreter(), "-I", "-u", str(BOTS_SCRIPT), *args, *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


class EventStream:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.all: list[dict[str, Any]] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            event = json.loads(line)
            self.all.append(event)
            self.events.put(event)

    def until(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                event = self.events.get(timeout=left)
            except queue.Empty:
                break
            if predicate(event):
                return event
        raise AssertionError(f"event never came; got {[e['ev'] for e in self.all]}")


def test_bots_against_the_fake_server(tmp_path: Path, schema_validator: Callable[[str], Any]) -> None:
    adapter, _, spec = prepared(FakeBds, tmp_path)
    with running(adapter, spec) as console:
        console.wait_for(adapter.dialect.is_ready)
        bots = _bots(adapter.machine.port, "-n", "3", "-fake-xuid")
        stream = EventStream(bots)
        try:
            for _ in range(3):
                name = console.wait_for(adapter.dialect.player_entered)
                console.send(adapter.cmd_tp(name, 1280 + 640 * int(name[1:]), 300, 1280))
            stream.until(lambda e: e["ev"] == "alljoined")
            stream.until(lambda _: sum(e["ev"] == "first_chunk" for e in stream.all) == 3)
            console.send("kill a01")
            stream.until(lambda e: e["ev"] == "respawned" and e["bot"] == "a01")
            console.send("kick a02 testing")
            assert stream.until(lambda e: e["ev"] == "kicked")["reason"] == "testing"
            stream.until(lambda e: e["ev"] == "stat" and e["online"] == 2)
            assert bots.stdin is not None
            bots.stdin.write("quit\n")
            bots.stdin.flush()
            assert bots.wait(10) == 0
        finally:
            if bots.poll() is None:
                bots.kill()
        time.sleep(0.2)
        events = stream.all
        assert console.close() == 0

    validator = schema_validator("bot-event")
    for event in events:
        validator.validate(event)
    assert events[0]["ev"] == "hello" and events[-1]["ev"] == "done"
    assert [e["bot"] for e in events if e["ev"] == "tp"] == ["a00", "a01", "a02"]
    assert sorted(e["bot"] for e in events if e["ev"] == "bot_summary") == ["a00", "a01", "a02"]
    final = [e for e in events if e["ev"] == "stat"][-1]
    assert (final["joined"], final["failed"], final["deaths"], final["decode_errors"]) == (3, 0, 1, 0)
    assert final["in_view"] > 0
    summary = next(e for e in events if e["ev"] == "bot_summary" and e["bot"] == "a00")
    assert summary["in_view"] == 49, "view distance 4 is a 49-chunk disc"
    assert len(summary["first_seen_ms"]) == 49
    assert any("xuid: 2535400000000001" in line for line in console.seen)


def test_rejected_login_is_a_join_failure(tmp_path: Path) -> None:
    adapter, _, spec = prepared(FakeBds, tmp_path, behaviour="reject-join")
    with running(adapter, spec) as console:
        console.wait_for(adapter.dialect.is_ready)
        bots = _bots(adapter.machine.port, "-n", "3")
        stream = EventStream(bots)
        try:
            alljoined = stream.until(lambda e: e["ev"] == "alljoined")
            assert (alljoined["joined"], alljoined["failed"]) == (2, 1)
            failure = next(e for e in stream.all if e["ev"] == "join_failed")
            assert (failure["bot"], failure["stage"]) == ("a02", "login")
        finally:
            bots.kill()
        assert console.close() == 0


def test_decode_error_fails_the_bots_on_request(tmp_path: Path) -> None:
    adapter, _, spec = prepared(FakeBds, tmp_path, behaviour="decode-error")
    with running(adapter, spec) as console:
        console.wait_for(adapter.dialect.is_ready)
        bots = _bots(adapter.machine.port, "-n", "2", "-decode-errors", "fail")
        stream = EventStream(bots)
        try:
            assert bots.wait(10) == fake_bots.EXIT_DECODE
            time.sleep(0.2)
        finally:
            if bots.poll() is None:
                bots.kill()
        error = next(e for e in stream.all if e["ev"] == "decode_error")
        assert error["bot"] == "a01"
        assert stream.all[-1]["ev"] == "done"
        assert console.close() == 0


# --------------------------------------------------------------------------------------------------------------------
# Fake adapters
# --------------------------------------------------------------------------------------------------------------------


def test_fetch_is_idempotent(tmp_path: Path) -> None:
    adapter = make_adapter(FakeBds, 19672)
    cache = Cache(tmp_path / "cache")
    first, second = adapter.fetch(cache), adapter.fetch(cache)
    assert first == second
    assert (first.root / "fake_server.py").is_file()
    assert first.artifacts[0].source == "build"


def test_prepare_records_categorised_overrides(tmp_path: Path) -> None:
    adapter, layout, spec = prepared(FakeBds, tmp_path, behaviour="exit-nonzero")
    overrides = {o.key: o for o in layout.overrides}
    assert overrides["online-mode"].category is OverrideCategory.HARNESS
    assert overrides["view-distance"].category is OverrideCategory.PARITY
    assert (overrides["view-distance"].default, overrides["view-distance"].value) == ("32", "4")
    assert "tick-distance" not in overrides, "a value equal to the default is not an override"
    config_file = layout.server_dir / "server.properties"
    assert layout.config_files[0].written_sha256 == hashlib.sha256(config_file.read_bytes()).hexdigest()
    assert json.loads((layout.server_dir / "bench-script.json").read_text())["stop_exit_code"] == 1
    assert layout.world.fresh and layout.world.path is not None and not layout.world.path.exists()
    assert spec.cwd == layout.server_dir
    assert "--family" in spec.argv and spec.argv[spec.argv.index("--family") + 1] == "bds"
    assert "PYTHONPATH" in spec.env_remove


def test_prepare_refuses_a_used_work_directory(tmp_path: Path) -> None:
    adapter = make_adapter(FakeBds, 19672)
    install = adapter.fetch(Cache(tmp_path / "cache"))
    context = make_context(adapter, tmp_path)
    context.work_dir.mkdir()
    (context.work_dir / "leftover").write_text("x")
    with pytest.raises(RuntimeError, match="not empty"):
        adapter.prepare(install, context)


def test_unknown_behaviour_is_a_config_error() -> None:
    with pytest.raises(config.ConfigError):
        make_adapter(FakeBds, 19672, behaviour="no-such-behaviour")


def test_behaviour_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BEHAVIOUR, "crash-at-boot,fake_nukkit=dip-on-join")
    assert make_adapter(FakeBds, 19672).behaviour == "crash-at-boot"
    assert make_adapter(FakeNukkit, 19672).behaviour == "dip-on-join"
    monkeypatch.delenv(ENV_BEHAVIOUR)
    assert make_adapter(FakeBds, 19672).behaviour is None


def test_position_parsers_never_turn_garbage_into_zero_players() -> None:
    assert parse_querytarget([]) is None
    assert parse_querytarget(["Target data: [", '   {"position" : {']) is None
    assert parse_querytarget(["Target data: [not json"]) is None
    assert parse_pos_lines(["Positions of 2 players:", "POS a00 1.00 2.00 3.00 0"]) is None
    assert parse_pos_lines([]) is None
    assert parse_pos_lines(["Positions of 0 players:"]) == []


def test_entry_point_registers_every_fake_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(register_entry_point(tmp_path)))
    adapters.refresh()
    try:
        assert {cls.name for cls in FAKE_ADAPTERS} <= set(adapters.names())
        assert adapters.get("fake_bds") is FakeBds
    finally:
        monkeypatch.undo()
        adapters.refresh()
    assert "fake_bds" not in adapters.names()


# --------------------------------------------------------------------------------------------------------------------
# Fidelity against the real adapters, and the integration workspace
# --------------------------------------------------------------------------------------------------------------------

#: Families whose console output is the server's own, not a bench helper's, so the real adapter must read the fake.
VANILLA_OUTPUT = ("bds", "endstone", "levilamina")


@pytest.mark.parametrize("family", VANILLA_OUTPUT)
def test_real_adapter_reads_the_fake_family(family: str) -> None:
    real = adapters.get(family)
    dialect = real.dialect
    lines = fake_server.sample_log(family, "1.2.3")
    texts = [dialect.split(line)[2] for line in lines]
    assert any(dialect.is_ready(text) for text in texts)
    assert [v for v in map(dialect.parse_version, texts) if v] == ["1.2.3"]
    assert [t for t in map(dialect.parse_tick, texts) if t is not None] == [1234, 1334]
    assert [p for p in map(dialect.player_entered, texts) if p] == ["a07"]
    errors = [text for line, text in zip(lines, texts, strict=True) if dialect.split(line)[0] == "error"]
    banner = [text for text in errors if "TRANSPORT" in text or "NetherNet" in text]
    assert all(dialect.is_benign(text) for text in banner), "the transport banner is benign"
    adapter = real(config.arm_from_ref(f"{family}@1.2.3"), config.default_machine())
    if adapter.cmd_positions() is not None:
        stamped = [LogLine(0, 0, *dialect.split(line), line) for line in lines]
        positions = adapter.parse_positions(stamped)
        assert positions is not None and [(p.x, p.y, p.z) for p in positions] == [(5760.5, 300.0, 1280.5)]


def test_fake_bots_accept_the_harness_bot_arguments() -> None:
    args = bots.bot_args(fake_workload().bots, addr="127.0.0.1:19672", csg=True, compat_flags=["-fake-xuid"])
    result = run_bots(*args, "-version")
    assert result.returncode == 0, result.stderr


def test_bench_workspace_resolves_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = FakeBench.create(tmp_path / "bench")
    monkeypatch.setenv("BBENCH_ROOT", bench.env["BBENCH_ROOT"])
    assert fake_workload().bots.n == 4
    extended = json.loads(bench.workload("short-stop", phases={"teardown": {"stop_timeout_s": 2.0}}).read_text())
    assert with_overrides(WORKLOAD, extended).phase("teardown")["stop_timeout_s"] == 2.0
    assert with_overrides(WORKLOAD, extended).phase("teardown")["bot_quit_timeout_s"] == 3.0
    machine = config.load_machine(bench.machine_path())
    assert machine.port == bench.port
    assert set(config.parse_cpu_list(machine.server_cpus)).isdisjoint(config.parse_cpu_list(machine.bot_cpus))
    assert [b.arm.id for b in config.benchmark_defaults()] == sorted(f"{cls.name}@1.0.0" for cls in FAKE_ADAPTERS)
    assert bench.session_args("fake_bds", repeats=2)[:4] == ["run", "fake_bds@1.0.0", "--repeats", "2"]


def test_bench_environment_reaches_bbench_subprocesses(tmp_path: Path) -> None:
    bench = FakeBench.create(tmp_path / "bench")
    probe = (
        "from bedrock_bench import adapters; from bedrock_bench.config import project_root; "
        "print(','.join(adapters.names())); print(project_root())"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], env=bench.env, capture_output=True, text=True, timeout=60, cwd=bench.root
    )
    assert result.returncode == 0, result.stderr
    names, root = result.stdout.splitlines()
    assert {cls.name for cls in FAKE_ADAPTERS} <= set(names.split(","))
    assert Path(root) == bench.root
    bot_cmd = json.loads(bench.env[ENV_BOT_CMD])
    version = subprocess.run([*bot_cmd, "-version"], capture_output=True, text=True, timeout=30)
    assert json.loads(version.stdout)["module"] == "loadbot"
