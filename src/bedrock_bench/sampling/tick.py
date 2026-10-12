# SPDX-License-Identifier: Apache-2.0
"""CPU time of the tick thread ten times a second, into ``series/tick.csv.gz``.

The tick thread is chosen by the adapter's ``tick_thread_rule`` from :meth:`ThreadSampler.infos`. The 10 Hz rate gives
per-second utilisation quantiles (``tick_util_p50/p99``) and the saturated fraction. On Windows ``GetThreadTimes``
advances at the scheduler tick (about 15.6 ms), so single 100 ms deltas are coarse; the ``cycles`` column is the
fine-grained secondary source.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bedrock_bench import platform
from bedrock_bench.clock import mono_ns
from bedrock_bench.platform import ProcessGone
from bedrock_bench.sampling.base import PeriodicSampler
from bedrock_bench.sampling.series import CYCLES_COLUMN, TICK_COLUMNS, SeriesWriter


class TickSampler(PeriodicSampler):
    def __init__(
        self,
        pid: int,
        tid: int,
        path: Path,
        *,
        interval_s: float = 0.1,
        clock: Callable[[], int] = mono_ns,
        reader: object | None = None,
    ) -> None:
        super().__init__(f"bbench-tick-{tid}", interval_s, clock=clock)
        self.pid = pid
        self.tid = tid
        self.path = Path(path)
        self._reader = reader if reader is not None else platform.current().TickReader(pid, tid)
        self._cycles = bool(getattr(self._reader, "cycles_supported", False))
        self._writer: SeriesWriter | None = None

    def setup(self) -> None:
        self._writer = SeriesWriter(self.path, TICK_COLUMNS + ((CYCLES_COLUMN,) if self._cycles else ()))

    def sample(self) -> None:
        cpu_ns, cycles = self._reader.read()
        t_ns = self.clock()
        assert self._writer is not None
        row: list[object] = [t_ns, cpu_ns]
        if self._cycles:
            row.append("" if cycles is None else cycles)
        self._writer.write(row)
        if self._reader.exited():
            raise ProcessGone(self.tid)  # the row above holds the thread's final CPU time

    def teardown(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader.close()
