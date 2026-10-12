# SPDX-License-Identifier: Apache-2.0
"""Per-thread CPU of the server process, once a second, into ``series/threads.csv.gz``.

Every thread is written on every sample with its current name, so renamed threads and pools that grow are visible.
Threads that exit keep their rows; on Windows the exited thread gets one final row with its exact final CPU time.
:meth:`ThreadSampler.infos` gives the live threads for ``ServerAdapter.tick_thread`` and the idle thread census.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from bedrock_bench import platform
from bedrock_bench.clock import mono_ns
from bedrock_bench.model import ThreadInfo
from bedrock_bench.sampling.base import PeriodicSampler
from bedrock_bench.sampling.series import CYCLES_COLUMN, THREAD_COLUMNS, SeriesWriter


class ThreadSampler(PeriodicSampler):
    def __init__(
        self,
        pid: int,
        path: Path,
        *,
        interval_s: float = 1.0,
        clock: Callable[[], int] = mono_ns,
        reader: object | None = None,
    ) -> None:
        super().__init__(f"bbench-threads-{pid}", interval_s, clock=clock)
        self.pid = pid
        self.path = Path(path)
        self._reader = reader if reader is not None else platform.current().ThreadReader(pid, clock=clock)
        self._cycles = bool(getattr(self._reader, "cycles_supported", False))
        self._writer: SeriesWriter | None = None
        self._lock = threading.Lock()
        self._infos: dict[int, ThreadInfo] = {}

    def setup(self) -> None:
        columns = THREAD_COLUMNS + ((CYCLES_COLUMN,) if self._cycles else ())
        self._writer = SeriesWriter(self.path, columns)

    def sample(self) -> None:
        stats = self._reader.snapshot()
        assert self._writer is not None
        for stat in stats:
            row: list[object] = [stat.t_ns, stat.tid, stat.name, stat.cpu_ns]
            if self._cycles:
                row.append("" if stat.cycles is None else stat.cycles)
            self._writer.write(row)
        with self._lock:
            self._infos = {s.tid: platform.to_thread_info(s) for s in stats if s.alive}

    def teardown(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader.close()

    def infos(self) -> list[ThreadInfo]:
        """Live threads as of the latest sample, by tid."""
        with self._lock:
            return [self._infos[tid] for tid in sorted(self._infos)]
