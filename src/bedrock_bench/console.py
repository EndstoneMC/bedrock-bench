# SPDX-License-Identifier: Apache-2.0
"""The server console: one reader, one writer, everything stamped.

Reader. A thread reads the server's stdout as bytes and stamps each line with harness monotonic and wall-clock time
the moment it is read. The line is decoded as UTF-8 (invalid bytes replaced), ANSI escape sequences are removed, and
the adapter's :class:`~bedrock_bench.adapters.base.Dialect` splits off the log prefix and level. Lines are appended to
``server.log.gz`` as ``<mono_ns>\\t<epoch_ms>\\t<line>`` and handed to subscribers.

Writer. Commands queue in two lanes. The priority lane (the tick probe) always goes first; the normal lane keeps its
order. Writes are UTF-8, end in the dialect's newline (LF unless an adapter says otherwise) and are spaced at least
``pace_s`` apart, stretched to one server tick at the current tick rate when a rate source is given, because servers
can drop console input that arrives faster than they tick. Every command is journaled to ``commands.jsonl.gz`` with
its enqueue and send times, lane and origin.

A dead server never raises into the caller: once stdin is closed, commands are journaled as failed and the reader
ends at end of file.
"""

from __future__ import annotations

import collections
import gzip
import json
import logging
import re
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from bedrock_bench.clock import NS_PER_S, Stamp, mono_ns
from bedrock_bench.model import LogLine

if TYPE_CHECKING:
    from bedrock_bench.adapters.base import Dialect

log = logging.getLogger(__name__)

LANES = ("priority", "normal")
ORIGINS = ("phase", "probe", "chaos")

#: CSI sequences (colours, cursor moves), OSC sequences (window titles) and two-byte escapes.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
#: Minecraft formatting codes; removed from ``LogLine.text`` but kept in ``LogLine.raw``.
FORMAT_CODE_RE = re.compile("§[0-9A-Za-z]")


# --------------------------------------------------------------------------------------------------------------------
# Line handling
# --------------------------------------------------------------------------------------------------------------------


def clean(raw: bytes | str) -> str:
    """One output line as text: line ending removed, UTF-8 decoded with replacement, ANSI escapes stripped."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.rstrip("\r\n")
    return ANSI_RE.sub("", text)


def parse_line(raw: str, t_ns: int, epoch_ms: int, dialect: Dialect | None = None) -> LogLine:
    """A :class:`LogLine` from a cleaned line. Without a dialect the text is the raw line and the level unknown."""
    level = logger = None
    text = raw
    if dialect is not None:
        level, logger, text = dialect.split(raw)
    return LogLine(t_ns=t_ns, epoch_ms=epoch_ms, level=level, logger=logger, text=FORMAT_CODE_RE.sub("", text), raw=raw)


def format_record(line: LogLine) -> str:
    """The ``server.log.gz`` record of one line."""
    return f"{line.t_ns}\t{line.epoch_ms}\t{line.raw}\n"


def read_log(path: Path | str, dialect: Dialect | None = None) -> list[LogLine]:
    """Load ``server.log.gz`` (or a plain-text copy). A truncated gzip stream yields the lines read before the cut."""
    lines: list[LogLine] = []
    for record in _read_text_lines(Path(path)):
        t_text, _, rest = record.partition("\t")
        epoch_text, _, raw = rest.partition("\t")
        try:
            lines.append(parse_line(raw, int(t_text), int(epoch_text), dialect))
        except ValueError:
            continue
    return lines


def read_journal(path: Path | str) -> list[dict[str, Any]]:
    """Load ``commands.jsonl.gz``. A truncated stream yields the records read before the cut."""
    records = []
    for line in _read_text_lines(Path(path)):
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def _read_text_lines(path: Path) -> Iterable[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace", newline="\n") as fh:
            for line in fh:
                yield line.rstrip("\n")
    except (EOFError, gzip.BadGzipFile, zlib.error):
        return


class _GzipLines:
    """Thread-safe gzip text writer that flushes at least every ``flush_s`` so a crash loses little."""

    def __init__(self, path: Path, flush_s: float = 1.0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] | None = gzip.open(path, "wt", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._flush_s = flush_s
        self._last_flush = time.monotonic()

    def write(self, text: str) -> None:
        with self._lock:
            if self._fh is None:
                return
            self._fh.write(text)
            now = time.monotonic()
            if now - self._last_flush >= self._flush_s:
                self._fh.flush()
                self._last_flush = now

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


# --------------------------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class Command:
    """One console write. ``send_ns`` is stamped right before the bytes go out; None until then, or on failure."""

    seq: int
    text: str
    lane: str
    origin: str
    enqueue_ns: int
    send_ns: int | None = None
    error: str | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _on_sent: Callable[[int], None] | None = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def sent(self) -> bool:
        return self._done.is_set() and self.error is None

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until the command was written or failed. True when it was written."""
        self._done.wait(timeout)
        return self.sent

    def record(self) -> dict[str, Any]:
        """The ``commands.jsonl.gz`` record."""
        return {
            "seq": self.seq,
            "lane": self.lane,
            "origin": self.origin,
            "text": self.text,
            "enqueue_ns": self.enqueue_ns,
            "send_ns": self.send_ns,
            "error": self.error,
        }


@dataclass(frozen=True)
class Reply:
    """Result of :meth:`Console.request`: the command, and the first matching line after it was sent (or None)."""

    command: Command
    line: LogLine | None
    match: re.Match[str] | None

    @property
    def ok(self) -> bool:
        return self.line is not None


LinePredicate = Callable[[LogLine], Any]


# --------------------------------------------------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------------------------------------------------


class Console:
    """Reads and writes one server's console. Call :meth:`start` once the process runs and :meth:`close` at the end.

    ``stdout`` and ``stdin`` are the process's binary pipes. ``rate`` returns the current ticks per second (or None
    when unknown) and stretches the write spacing to one tick. ``clock`` is replaceable for tests.
    """

    def __init__(
        self,
        stdout: IO[bytes],
        stdin: IO[bytes] | None,
        dialect: Dialect,
        *,
        log_path: Path | None = None,
        journal_path: Path | None = None,
        pace_s: float = 0.05,
        rate: Callable[[], float | None] | None = None,
        clock: Callable[[], Stamp] = Stamp.now,
    ) -> None:
        if pace_s < 0:
            raise ValueError("pace_s must not be negative")
        self.dialect = dialect
        self._stdout = stdout
        self._stdin = stdin
        self._pace_ns = int(pace_s * NS_PER_S)
        self._rate = rate
        self._clock = clock
        self._log = _GzipLines(log_path) if log_path is not None else None
        self._journal = _GzipLines(journal_path) if journal_path is not None else None

        self._cond = threading.Condition()
        self._lines: list[LogLine] = []
        self._subscribers: list[Callable[[LogLine], None]] = []
        self._priority: collections.deque[Command] = collections.deque()
        self._normal: collections.deque[Command] = collections.deque()
        self._seq = 0
        self._closing = False
        self._broken = False
        self._eof = False
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None

        #: Set when the dialect's ready line was read.
        self.ready = threading.Event()
        #: Set when stdout reached end of file (the server exited or closed it).
        self.eof = threading.Event()
        self.ready_line: LogLine | None = None
        #: The version from the first banner line that matches ``dialect.version``.
        self.banner_version: str | None = None
        #: Player name -> monotonic ns of the first line saying the player is in the world.
        self.in_world: dict[str, int] = {}

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> Console:
        if self._reader is not None:
            raise RuntimeError("console already started")
        self._reader = threading.Thread(target=self._read_loop, name="console-reader", daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name="console-writer", daemon=True)
        self._reader.start()
        self._writer.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        """Stop writing (queued commands are journaled as failed), wait briefly for the reader, close the files."""
        with self._cond:
            self._closing = True
            pending = list(self._priority) + list(self._normal)
            self._priority.clear()
            self._normal.clear()
            self._cond.notify_all()
        for command in pending:
            self._finish(command, error="console closed before the command was sent")
        if self._writer is not None:
            self._writer.join(timeout)
        if self._reader is not None:
            self._reader.join(timeout)
        if self._log is not None:
            self._log.close()
        if self._journal is not None:
            self._journal.close()

    # ---- reading --------------------------------------------------------------------------------------------------

    def mark(self) -> int:
        """Index of the next line to arrive; pass it as ``after`` to only look at newer lines."""
        with self._cond:
            return len(self._lines)

    def lines(self, start: int = 0, end: int | None = None) -> list[LogLine]:
        with self._cond:
            return self._lines[start:end]

    def tail(self, n: int) -> list[LogLine]:
        with self._cond:
            return self._lines[-n:] if n > 0 else []

    def subscribe(self, callback: Callable[[LogLine], None]) -> Callable[[], None]:
        """Call ``callback`` on the reader thread for every new line. Returns a function that unsubscribes.

        Callbacks must be quick; an exception in one is logged and does not stop the reader.
        """
        with self._cond:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._cond:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def wait_for(
        self,
        what: re.Pattern[str] | str | LinePredicate,
        *,
        after: int | None = None,
        timeout: float | None = None,
    ) -> LogLine | None:
        """The first line at index ``after`` or later (default: from now on) that matches.

        ``what`` is a pattern searched in ``LogLine.text`` or a predicate. Returns None on timeout or end of file.
        """
        predicate = _predicate(what)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            index = len(self._lines) if after is None else after
            while True:
                while index < len(self._lines):
                    line = self._lines[index]
                    index += 1
                    if predicate(line):
                        return line
                if self._eof:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait for the ready line. False on timeout or when the server exited first."""
        return self.wait_for(self.dialect.ready, after=0, timeout=timeout) is not None

    # ---- writing --------------------------------------------------------------------------------------------------

    def send(
        self,
        text: str,
        *,
        lane: str = "normal",
        origin: str = "phase",
        on_sent: Callable[[int], None] | None = None,
    ) -> Command:
        """Queue one command. Never blocks and never raises because the server is gone; check ``Command.error``.

        ``on_sent(send_ns)`` runs on the writer thread with the send stamp just before the bytes are written, so it
        always runs before any reply can be read (the tick probe pairs replies this way).
        """
        if "\n" in text or "\r" in text:
            raise ValueError(f"a console command is one line: {text!r}")
        if lane not in LANES:
            raise ValueError(f"lane must be one of {LANES}, got {lane!r}")
        if origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}, got {origin!r}")
        with self._cond:
            self._seq += 1
            command = Command(self._seq, text, lane, origin, enqueue_ns=self._clock().mono_ns, _on_sent=on_sent)
            reason = None
            if self._closing:
                reason = "console closed"
            elif self._broken or self._stdin is None:
                reason = "server stdin is closed"
            else:
                (self._priority if lane == "priority" else self._normal).append(command)
                self._cond.notify_all()
        if reason is not None:
            self._finish(command, error=reason)
        return command

    def request(
        self,
        text: str,
        reply: re.Pattern[str] | str | LinePredicate,
        *,
        timeout: float,
        lane: str = "normal",
        origin: str = "phase",
    ) -> Reply:
        """Send a command and wait for the first matching line read after it was sent."""
        deadline = time.monotonic() + timeout
        start = self.mark()
        command = self.send(text, lane=lane, origin=origin)
        if not command.wait(max(0.0, deadline - time.monotonic())):
            return Reply(command, None, None)
        assert command.send_ns is not None
        sent_ns = command.send_ns
        predicate = _predicate(reply)
        line = self.wait_for(
            lambda candidate: candidate.t_ns >= sent_ns and predicate(candidate),
            after=start,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        match = None
        if line is not None and isinstance(reply, (str, re.Pattern)):
            match = re.search(reply, line.text)
        return Reply(command, line, match)

    def backlog(self) -> int:
        """Commands queued and not yet written."""
        with self._cond:
            return len(self._priority) + len(self._normal)

    def writer_lag_s(self) -> float:
        """Age of the oldest queued normal-lane command in seconds (0 when the queue is empty)."""
        with self._cond:
            if not self._normal:
                return 0.0
            return (self._clock().mono_ns - self._normal[0].enqueue_ns) / NS_PER_S

    @property
    def writable(self) -> bool:
        return not (self._closing or self._broken or self._stdin is None)

    # ---- threads --------------------------------------------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for raw in iter(self._stdout.readline, b""):
                stamp = self._clock()
                self._accept(parse_line(clean(raw), stamp.mono_ns, stamp.epoch_ms, self.dialect))
        except (OSError, ValueError) as exc:
            log.debug("console reader stopped: %s", exc)
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()
            self.eof.set()

    def _accept(self, line: LogLine) -> None:
        with self._cond:
            self._lines.append(line)
            if self.ready_line is None and self.dialect.is_ready(line.text):
                self.ready_line = line
                self.ready.set()
            if self.banner_version is None:
                self.banner_version = self.dialect.parse_version(line.text)
            player = self.dialect.player_entered(line.text)
            if player is not None:
                self.in_world.setdefault(player, line.t_ns)
            subscribers = list(self._subscribers)
            self._cond.notify_all()
        if self._log is not None:
            self._log.write(format_record(line))
        for callback in subscribers:
            try:
                callback(line)
            except Exception:
                log.exception("console subscriber %r failed", callback)

    def _write_loop(self) -> None:
        last_write_ns: int | None = None  # real monotonic time, so pacing holds with any injected clock
        while True:
            with self._cond:
                while True:
                    if self._closing:
                        return
                    if self._priority or self._normal:
                        wait_ns = 0 if last_write_ns is None else last_write_ns + self._gap_ns() - mono_ns()
                        if wait_ns <= 0:
                            break
                        self._cond.wait(wait_ns / NS_PER_S)
                    else:
                        self._cond.wait()
                command = (self._priority or self._normal).popleft()
            last_write_ns = mono_ns()
            self._write(command)

    def _gap_ns(self) -> int:
        gap = self._pace_ns
        if self._rate is not None:
            try:
                tps = self._rate()
            except Exception:
                tps = None
            if tps:
                gap = max(gap, int(NS_PER_S / max(float(tps), 1.0)))
        return gap

    def _write(self, command: Command) -> None:
        data = (command.text + self.dialect.newline).encode("utf-8")
        stdin = self._stdin
        if self._broken or stdin is None:
            self._finish(command, error="server stdin is closed")
            return
        send_ns = self._clock().mono_ns
        if command._on_sent is not None:
            try:
                command._on_sent(send_ns)
            except Exception:
                log.exception("on_sent callback of %r failed", command.text)
        try:
            stdin.write(data)
            stdin.flush()
        except (OSError, ValueError) as exc:
            with self._cond:
                self._broken = True
            self._finish(command, error=f"server stdin is closed ({type(exc).__name__})")
            return
        command.send_ns = send_ns
        self._finish(command)

    def _finish(self, command: Command, *, error: str | None = None) -> None:
        if error is not None:
            command.error = error
            command.send_ns = None
        command._done.set()
        if self._journal is not None:
            self._journal.write(json.dumps(command.record(), ensure_ascii=False, separators=(",", ":")) + "\n")


def _predicate(what: re.Pattern[str] | str | LinePredicate) -> LinePredicate:
    if isinstance(what, str):
        pattern = re.compile(what)
        return lambda line: pattern.search(line.text) is not None
    if isinstance(what, re.Pattern):
        return lambda line: what.search(line.text) is not None
    return what
