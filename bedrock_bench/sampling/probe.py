# SPDX-License-Identifier: Apache-2.0
"""The tick probe: the adapter's tick command on the console priority lane every 5 s, into ``series/probe.csv``.

TPS comes only from this probe: ``(tick_b - tick_a) / (rx_b - rx_a)`` over replies of a counter the server advances
once per executed tick (rule R8). Each row pairs one command with its reply: ``send_ns`` when the command was
written, ``rx_ns`` when the console read the reply line, ``tick`` the counter.

The probe does not own the console. It is wired to it with two calls:

- ``send(command, on_sent)``: queue ``command`` on the priority lane. The console calls ``on_sent(send_ns)`` with the
  monotonic time it takes just before writing the bytes, so the stamp always precedes the reply.
- :meth:`TickProbe.feed` ``(rx_ns, text)``: the console passes every prefix-stripped output line.

Replies are paired with sends first in, first out, which matches a console that answers in order. A send that gets
no reply within ``timeout_s`` is counted in :attr:`TickProbe.lost` and dropped, so one swallowed command cannot shift
every later pairing. The ``probe_coverage`` gate sees the gap.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bedrock_bench.clock import mono_ns
from bedrock_bench.model import ProbeSample
from bedrock_bench.sampling.series import PROBE_COLUMNS, SeriesWriter

log = logging.getLogger(__name__)

Sender = Callable[[str, Callable[[int], None]], None]


@dataclass
class _Pending:
    queued_ns: int
    send_ns: int | None = None


class TickProbe:
    """Sends ``command`` every ``interval_s`` and records ``(send_ns, rx_ns, tick)`` for each reply."""

    def __init__(
        self,
        command: str,
        parse: Callable[[str], int | None],
        send: Sender,
        path: Path | None,
        *,
        interval_s: float = 5.0,
        timeout_s: float = 15.0,
        clock: Callable[[], int] = mono_ns,
    ) -> None:
        self.command = command
        self.parse = parse
        self.interval_s = interval_s
        self.timeout_ns = int(timeout_s * 1e9)
        self.samples: list[ProbeSample] = []
        self.sent = 0
        self.lost = 0  # sends that got no reply within timeout_s
        self.unmatched = 0  # replies with no send waiting
        self.unstamped = 0  # replies that arrived before the console reported the send time
        self.error: BaseException | None = None
        self._send = send
        self._clock = clock
        self._pending: deque[_Pending] = deque()
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._writer = SeriesWriter(path, PROBE_COLUMNS, flush_s=0.0) if path is not None else None
        self._thread = threading.Thread(target=self._run, name="bbench-probe", daemon=True)

    def start(self) -> TickProbe:
        self._thread.start()
        return self

    def stop(self, drain_s: float = 2.0) -> None:
        """Stop sending, give outstanding replies ``drain_s`` seconds to arrive, then close the file."""
        self._halt.set()
        if self._thread.is_alive():
            self._thread.join()
        deadline = time.monotonic() + drain_s
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    break
            time.sleep(0.05)
        with self._lock:
            self.lost += len(self._pending)
            self._pending.clear()
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    def probe_once(self) -> None:
        """Queue one probe command now."""
        entry = _Pending(self._clock())
        with self._lock:
            self._expire(entry.queued_ns)
            self._pending.append(entry)
            self.sent += 1

        def on_sent(send_ns: int, entry: _Pending = entry) -> None:
            entry.send_ns = send_ns

        self._send(self.command, on_sent)

    def feed(self, rx_ns: int, text: str) -> bool:
        """Offer one console line. Returns True if it was a tick reply."""
        tick = self.parse(text)
        if tick is None:
            return False
        with self._lock:
            self._expire(rx_ns)
            if not self._pending:
                self.unmatched += 1
                return True
            entry = self._pending.popleft()
            if entry.send_ns is None:
                self.unstamped += 1
            sample = ProbeSample(send_ns=entry.send_ns or entry.queued_ns, rx_ns=rx_ns, tick=tick)
            self.samples.append(sample)
            if self._writer is not None:
                self._writer.write([sample.send_ns, sample.rx_ns, sample.tick])
        return True

    def rate(self) -> float | None:
        """Ticks per second between the last two replies, or None before there are two (for console pacing)."""
        with self._lock:
            if len(self.samples) < 2:
                return None
            a, b = self.samples[-2], self.samples[-1]
        span = (b.rx_ns - a.rx_ns) / 1e9
        return (b.tick - a.tick) / span if span > 0 else None

    def _expire(self, now_ns: int) -> None:
        while self._pending and now_ns - (self._pending[0].send_ns or self._pending[0].queued_ns) > self.timeout_ns:
            self._pending.popleft()
            self.lost += 1

    def _run(self) -> None:
        deadline = time.perf_counter()
        try:
            while not self._halt.is_set():
                self.probe_once()
                deadline += self.interval_s
                now = time.perf_counter()
                if deadline < now:  # the sender blocked; keep the 5 s spacing from here
                    deadline = now + self.interval_s
                self._halt.wait(deadline - now)
        except (OSError, ValueError) as exc:  # console closed: the server is gone
            self.error = exc
            log.info("tick probe stopped: %s", exc)
