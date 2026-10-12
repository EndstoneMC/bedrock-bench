# SPDX-License-Identifier: Apache-2.0
"""Machine-wide CPU once a second and who else is using it: ``series/machine.csv.gz`` and ``series/foreign_top.jsonl``.

- *busy* is the busy fraction of each logical CPU between two samples, from the OS per-CPU counters.
- *own* is the CPU of the harness's own trees: ``server`` (the launched server and everything under it), ``bot``
  (the load bot) and ``harness`` (this process and its other children), assigned in that order.
- *foreign* is ``sum(busy) - own``: load from anything else on the machine.
- *foreign on the server set* is the busy time of the server's CPUs minus the server tree, which is pinned there
  while nothing else of ours is.

Each second only the per-CPU counters and the handful of own processes are read. The full process table is scanned
every 10 s, to find new members of the own trees and to name the processes behind foreign load. Values are not
clamped: counter granularity can push foreign slightly below zero, and clamping would bias its mean upwards.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from bedrock_bench import platform
from bedrock_bench.clock import mono_ns
from bedrock_bench.sampling.base import PeriodicSampler, ProcessGroup, children_map
from bedrock_bench.sampling.series import MACHINE_COLUMNS, MACHINE_GROUPS, JsonlWriter, SeriesWriter, fmt

GROUPS = MACHINE_GROUPS


def busy_total(times: Any) -> tuple[float, float]:
    """``(busy, total)`` seconds of one psutil per-CPU sample.

    Linux: busy is everything but ``idle`` and ``iowait`` (steal counts as busy: the CPU was not ours to use).
    Windows: ``system`` already contains interrupt and DPC time, so busy is ``user + system``.
    """
    if hasattr(times, "iowait"):
        total = sum(
            getattr(times, f, 0.0) for f in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
        )
        return total - times.idle - times.iowait, total
    return times.user + times.system, times.user + times.system + times.idle


class CpuMeter:
    """Busy fraction of every logical CPU since the previous :meth:`read`."""

    def __init__(self, cpu_times: Callable[..., Sequence[Any]] = psutil.cpu_times) -> None:
        self._cpu_times = cpu_times
        self._prev = [busy_total(t) for t in self._cpu_times(percpu=True)]

    @property
    def count(self) -> int:
        return len(self._prev)

    def read(self) -> tuple[float, ...]:
        current = [busy_total(t) for t in self._cpu_times(percpu=True)]
        out = []
        for (busy0, total0), (busy1, total1) in zip(self._prev, current, strict=False):
            span = total1 - total0
            out.append((busy1 - busy0) / span if span > 0 else 0.0)
        self._prev = current
        return tuple(out)


class OwnGroups:
    """CPU of the harness's own process trees, one :class:`ProcessGroup` per group in :data:`GROUPS`."""

    def __init__(self, harness_root: int | None = None) -> None:
        self.groups = {g: ProcessGroup() for g in GROUPS}
        self.groups["harness"].add_root(os.getpid() if harness_root is None else harness_root)

    def add_root(self, group: str, root: psutil.Process | int) -> None:
        self.groups[group].add_root(root)

    def refresh(self, children: Mapping[int, list[int]]) -> set[int]:
        """Assign every descendant to the first group (in :data:`GROUPS` order) that reaches it."""
        assigned: set[int] = set()
        roots = {pid for g in self.groups.values() for pid in g.root_pids}
        for name in GROUPS:
            group = self.groups[name]
            assigned |= group.refresh(children, exclude=assigned | (roots - group.root_pids))
        return assigned

    def cpu(self) -> dict[str, float]:
        """Cumulative CPU seconds per group."""
        return {name: sum(group.cpu()) for name, group in self.groups.items()}

    @property
    def pids(self) -> set[int]:
        return {pid for g in self.groups.values() for pid in g.pids}


_Foreign = dict[tuple[int, int], tuple[str, float]]  # (pid, started) -> (name, CPU seconds)


def _scan(own: OwnGroups) -> tuple[set[int], _Foreign]:
    """One read of the process table: refresh own membership, and CPU seconds of every other process."""
    table = platform.current().process_table()
    own_pids = own.refresh(children_map(table))
    foreign = {(e.pid, e.started): (e.name, e.cpu_s) for e in table if e.pid != 0 and e.pid not in own_pids}
    return own_pids, foreign


def _top(before: _Foreign, after: _Foreign, seconds: float, n: int) -> list[dict[str, Any]]:
    """Processes that used at least 0.01 cores between two scans, busiest first.

    A process missing from ``before`` started in between, so all of its CPU time counts. One that started and ended
    in between is not seen at all; the foreign column still has its load.
    """
    rows = []
    for key, (name, cpu) in after.items():
        prev = before.get(key)
        if seconds > 0:
            cores = (cpu - (prev[1] if prev is not None else 0.0)) / seconds
            if cores >= 0.01:
                rows.append((cores, key[0], name))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [{"pid": pid, "name": name, "cores": round(cores, 3)} for cores, pid, name in rows[:n]]


class MachineSampler(PeriodicSampler):
    def __init__(
        self,
        path: Path,
        top_path: Path | None,
        server_cpus: Iterable[int] = (),
        *,
        interval_s: float = 1.0,
        top_interval_s: float = 10.0,
        top_n: int = 10,
        harness_root: int | None = None,
        clock: Callable[[], int] = mono_ns,
    ) -> None:
        super().__init__("bbench-machine", interval_s, clock=clock)
        self.path = Path(path)
        self.top_path = Path(top_path) if top_path is not None else None
        self.server_cpus = tuple(sorted(set(server_cpus)))
        self.top_interval_s = top_interval_s
        self.top_n = top_n
        self._own = OwnGroups(harness_root)
        self._lock = threading.Lock()
        self._refresh = threading.Event()
        self._writer: SeriesWriter | None = None
        self._top_writer: JsonlWriter | None = None
        self._meter: CpuMeter | None = None
        self._foreign: _Foreign = {}
        self._prev_own: dict[str, float] = {}
        self._prev_t = 0
        self._scan_at = 0.0

    def add_root(self, group: str, root: psutil.Process | int) -> None:
        """Count ``root`` and its future children as ``group`` (``server``, ``bot`` or ``harness``)."""
        if group not in GROUPS:
            raise ValueError(f"unknown group {group!r}; use one of {', '.join(GROUPS)}")
        with self._lock:
            self._own.add_root(group, root)
        self._refresh.set()

    def refresh(self) -> None:
        """Rescan the process table at the next sample, e.g. after a launcher started the server."""
        self._refresh.set()

    def setup(self) -> None:
        self._meter = CpuMeter()
        columns = MACHINE_COLUMNS + tuple(f"cpu{i}" for i in range(self._meter.count))
        self._writer = SeriesWriter(self.path, columns)
        if self.top_path is not None:
            self._top_writer = JsonlWriter(self.top_path)
        with self._lock:
            _, self._foreign = _scan(self._own)
            self._prev_own = self._own.cpu()
        self._scan_at = time.monotonic()
        self._prev_t = self.clock()

    def sample(self) -> None:
        assert self._meter is not None
        busy = self._meter.read()
        with self._lock:
            own = self._own.cpu()
        t_ns = self.clock()
        span = (t_ns - self._prev_t) / 1e9
        cores = {g: (own[g] - self._prev_own[g]) / span if span > 0 else 0.0 for g in GROUPS}
        self._prev_own, self._prev_t = own, t_ns
        foreign = sum(busy) - sum(cores.values())
        server_set = sum(busy[c] for c in self.server_cpus if c < len(busy)) - cores["server"]
        assert self._writer is not None
        self._writer.write(
            [t_ns, fmt(foreign, 4), fmt(server_set, 4), *(fmt(cores[g], 4) for g in GROUPS), *(fmt(b, 4) for b in busy)]
        )
        now = time.monotonic()
        if self._refresh.is_set() or now - self._scan_at >= self.top_interval_s:
            self._refresh.clear()
            self._scan_foreign(now)

    def _scan_foreign(self, now: float) -> None:
        with self._lock:
            _, foreign = _scan(self._own)
        if self._top_writer is not None and now - self._scan_at >= self.top_interval_s / 2:
            seconds = now - self._scan_at
            top = _top(self._foreign, foreign, seconds, self.top_n)
            self._top_writer.write({"t_ns": self.clock(), "interval_s": round(seconds, 3), "top": top})
        self._foreign, self._scan_at = foreign, now

    def teardown(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._top_writer is not None:
            self._top_writer.close()


# --------------------------------------------------------------------------------------------------------------------
# Idle check
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ForeignLoad:
    """Foreign load measured over a quiet period (no server, no bots)."""

    interval_s: float
    foreign: tuple[float, ...]  # cores per interval
    server_set: tuple[float, ...]
    top: list[dict[str, Any]] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return sum(self.foreign) / len(self.foreign) if self.foreign else 0.0

    @property
    def server_set_mean(self) -> float:
        return sum(self.server_set) / len(self.server_set) if self.server_set else 0.0

    def peak(self, window_s: float = 10.0) -> float:
        """Highest mean over any ``window_s`` stretch (the whole period if it is shorter)."""
        n = max(1, round(window_s / self.interval_s))
        values = self.foreign
        if len(values) <= n:
            return self.mean
        return max(sum(values[i : i + n]) / n for i in range(len(values) - n + 1))


def measure_foreign(
    duration_s: float,
    server_cpus: Iterable[int] = (),
    *,
    interval_s: float = 1.0,
    top_n: int = 10,
    harness_root: int | None = None,
) -> ForeignLoad:
    """Measure foreign load for ``duration_s`` seconds; everything but this process tree counts as foreign."""
    server = tuple(sorted(set(server_cpus)))
    own = OwnGroups(harness_root)
    _, before = _scan(own)
    started = time.perf_counter()
    meter = CpuMeter()
    prev_own, prev_t = sum(own.cpu().values()), time.perf_counter()
    foreign: list[float] = []
    server_set: list[float] = []
    steps = max(1, round(duration_s / interval_s))
    for step in range(1, steps + 1):
        time.sleep(max(0.0, started + step * interval_s - time.perf_counter()))
        busy = meter.read()
        own_cpu, now = sum(own.cpu().values()), time.perf_counter()
        own_cores = (own_cpu - prev_own) / (now - prev_t)
        prev_own, prev_t = own_cpu, now
        foreign.append(sum(busy) - own_cores)
        server_set.append(sum(busy[c] for c in server if c < len(busy)))
    _, after = _scan(own)
    return ForeignLoad(
        interval_s, tuple(foreign), tuple(server_set), _top(before, after, time.perf_counter() - started, top_n)
    )
