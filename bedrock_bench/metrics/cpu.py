# SPDX-License-Identifier: Apache-2.0
"""CPU metrics: tick thread, per-thread, process, and the machine-wide interference counters (docs/metrics.md).

Window bounds are seconds since the run origin; samples carry nanoseconds since the origin. Rates are computed over
the span the samples actually cover inside the window (first sample at or after ``t0`` to last at or before ``t1``),
which differs from ``t1 - t0`` by at most one sampling period.

Tick-thread CPU is CPU time, not wall time: it excludes time the thread spends blocked, so it is not MSPT
(docs/methodology.md#tick-thread-cpu-is-not-mspt).
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from bedrock_bench.metrics import tps
from bedrock_bench.metrics.stats import mean
from bedrock_bench.model import CpuSample, MachineSample, ProbeSample, ProcSample, ThreadSample, ThreadSeries

NS_PER_S = 1e9
#: A second counts as saturated when the tick thread was busy for at least this fraction of it.
SATURATED_UTIL = 0.95
#: Threads above this rate count as busy in the thread tables.
BUSY_MS_PER_S = 100.0
#: Length of the rolling window for peak foreign load, in machine samples (1 Hz).
PEAK_SAMPLES = 10

_DIGITS = re.compile(r"\d+")


def normalize_name(name: str) -> str:
    """Thread name with every digit run replaced by ``#``, so pool members group together."""
    return _DIGITS.sub("#", name) if name else "(unnamed)"


# --------------------------------------------------------------------------------------------------------------------
# Per-thread CPU
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadDelta:
    """CPU of one thread over a window. A reused tid shows up once per thread that carried it."""

    tid: int
    name: str
    ms_per_s: float
    born: bool  # first seen after the window's first sample: its CPU counts from 0
    exited: bool  # last seen before the window's last sample: its CPU counts up to its last sample


def _instances(samples: Sequence[ThreadSample]) -> list[list[ThreadSample]]:
    """Split the samples into one list per thread lifetime.

    A tid starts a new lifetime when its name changes, its CPU counter goes down, or it was missing from a sampler
    sweep in between (the operating system reused the tid).
    """
    sweeps = sorted({s.t_ns for s in samples})
    index = {t: i for i, t in enumerate(sweeps)}
    by_tid: dict[int, list[ThreadSample]] = {}
    for sample in sorted(samples, key=lambda s: (s.tid, s.t_ns)):
        by_tid.setdefault(sample.tid, []).append(sample)
    out: list[list[ThreadSample]] = []
    for tid in sorted(by_tid):
        current: list[ThreadSample] = []
        for sample in by_tid[tid]:
            if current:
                prev = current[-1]
                if sample.name != prev.name or sample.cpu_ns < prev.cpu_ns or index[sample.t_ns] - index[prev.t_ns] > 1:
                    out.append(current)
                    current = []
            current.append(sample)
        if current:
            out.append(current)
    return out


def thread_rows(threads: ThreadSeries, t0: float, t1: float) -> list[ThreadDelta]:
    """Per-thread CPU over the window.

    ``ms_per_s = 1000 * (end - start) / span`` where ``end`` is the CPU at the window's last sweep, or the thread's
    last sample if it exited, and ``start`` is the CPU at the window's first sweep, or 0 if the thread was created
    after it. Empty without two sweeps in the window.
    """
    lo, hi = round(t0 * NS_PER_S), round(t1 * NS_PER_S)
    sweeps = sorted({s.t_ns for s in threads.samples if lo <= s.t_ns <= hi})
    if len(sweeps) < 2:
        return []
    first, last = sweeps[0], sweeps[-1]
    span_s = (last - first) / NS_PER_S
    rows: list[ThreadDelta] = []
    for lifetime in _instances(threads.samples):
        inside = [s for s in lifetime if first <= s.t_ns <= last]
        if not inside:
            continue
        born = inside[0].t_ns > first
        start = 0 if born else inside[0].cpu_ns
        end = inside[-1]
        rows.append(
            ThreadDelta(
                tid=end.tid,
                name=end.name,
                ms_per_s=(end.cpu_ns - start) / 1e6 / span_s,
                born=born,
                exited=end.t_ns < last,
            )
        )
    return rows


def thread_deltas(threads: ThreadSeries, t0: float, t1: float) -> dict[int, float]:
    """``tid -> ms/s`` over the window; lifetimes that shared a tid are summed."""
    out: dict[int, float] = {}
    for row in thread_rows(threads, t0, t1):
        out[row.tid] = out.get(row.tid, 0.0) + row.ms_per_s
    return out


def top_threads(rows: Iterable[ThreadDelta], limit: int = 25) -> list[list[object]]:
    """Rows ``[name, threads, sum_ms_per_s, max_ms_per_s, busy]`` grouped by normalised name, busiest first."""
    groups: dict[str, list[float]] = {}
    for row in rows:
        groups.setdefault(normalize_name(row.name), []).append(row.ms_per_s)
    totals = sorted(((math.fsum(v), name) for name, v in groups.items()), key=lambda item: (-item[0], item[1]))
    return [
        [name, len(groups[name]), total, max(groups[name]), sum(1 for x in groups[name] if x > BUSY_MS_PER_S)]
        for total, name in totals[:limit]
    ]


def group_threads(rows: Iterable[ThreadDelta], groups: Mapping[str, str]) -> list[list[object]]:
    """Rows ``[group, threads, sum_ms_per_s, max_ms_per_s]`` for an adapter's diagnostic thread groups."""
    rows = list(rows)
    table = []
    for label in sorted(groups):
        pattern = re.compile(groups[label])
        values = [r.ms_per_s for r in rows if pattern.search(r.name)]
        table.append([label, len(values), math.fsum(values), max(values, default=0.0)])
    return table


def census(threads: ThreadSeries, at: float) -> dict[str, int]:
    """Threads alive at the last sweep at or before ``at``, counted by normalised name."""
    limit = round(at * NS_PER_S)
    times = [s.t_ns for s in threads.samples if s.t_ns <= limit]
    if not times:
        return {}
    sweep = max(times)
    counts: dict[str, int] = {}
    for sample in threads.samples:
        if sample.t_ns == sweep:
            key = normalize_name(sample.name)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------------------------------------------------
# Tick thread
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TickCpu:
    ms_per_s: float  # 1000 * dCPU / dt
    busy_frac: float  # ms_per_s / 1000
    ms_per_tick: float | None  # 1000 * dCPU / dG over the probe replies bounding the window
    ticks: int | None  # dG
    util: tuple[float, ...]  # per-second busy fraction
    saturated_frac: float | None  # share of seconds with util >= 0.95
    cycles_ms_per_s: float | None  # same as ms_per_s from cycle counts and the calibrated TSC (Windows)
    samples: int


class _Curve:
    """Piecewise-linear CPU-seconds curve through the tick samples."""

    def __init__(self, samples: Sequence[CpuSample]) -> None:
        points = sorted((s.t_ns / NS_PER_S, s.cpu_ns / NS_PER_S) for s in samples)
        self.t = [p[0] for p in points]
        self.cpu = [p[1] for p in points]

    def at(self, t: float) -> float | None:
        """CPU seconds at ``t``; None outside the sampled range."""
        if not self.t or t < self.t[0] or t > self.t[-1]:
            return None
        i = bisect.bisect_left(self.t, t)
        if self.t[i] == t:
            return self.cpu[i]
        t_a, t_b = self.t[i - 1], self.t[i]
        c_a, c_b = self.cpu[i - 1], self.cpu[i]
        return c_a + (c_b - c_a) * (t - t_a) / (t_b - t_a)


def tick(
    tick_samples: Sequence[CpuSample],
    probe: Sequence[ProbeSample],
    t0: float,
    t1: float,
    tsc_hz: float | None = None,
) -> TickCpu | None:
    """Tick-thread CPU over the window from the 10 Hz samples; None without two samples in the window."""
    inside = sorted((s for s in tick_samples if t0 <= s.t_ns / NS_PER_S <= t1), key=lambda s: s.t_ns)
    if len(inside) < 2:
        return None
    first, last = inside[0], inside[-1]
    span = (last.t_ns - first.t_ns) / NS_PER_S
    if span <= 0:
        return None
    ms_per_s = 1000.0 * (last.cpu_ns - first.cpu_ns) / NS_PER_S / span

    curve = _Curve(tick_samples)
    ms_per_tick: float | None = None
    ticks: int | None = None
    points = tps.replies(probe)
    span_ab = tps.bounds(points, t0, t1)
    if span_ab is not None:
        a, b = points[span_ab[0]], points[span_ab[1]]
        cpu_a, cpu_b = curve.at(a.t), curve.at(b.t)
        ticks = b.tick - a.tick
        if cpu_a is not None and cpu_b is not None and ticks > 0:
            ms_per_tick = 1000.0 * (cpu_b - cpu_a) / ticks

    start = first.t_ns / NS_PER_S
    util = []
    for k in range(int(span)):
        lo, hi = curve.at(start + k), curve.at(start + k + 1)
        if lo is not None and hi is not None:
            util.append(hi - lo)
    saturated = sum(1 for u in util if u >= SATURATED_UTIL) / len(util) if util else None

    cycles_ms_per_s = None
    if tsc_hz and first.cycles is not None and last.cycles is not None:
        cycles_ms_per_s = 1000.0 * (last.cycles - first.cycles) / tsc_hz / span

    return TickCpu(
        ms_per_s=ms_per_s,
        busy_frac=ms_per_s / 1000.0,
        ms_per_tick=ms_per_tick,
        ticks=ticks,
        util=tuple(util),
        saturated_frac=saturated,
        cycles_ms_per_s=cycles_ms_per_s,
        samples=len(inside),
    )


# --------------------------------------------------------------------------------------------------------------------
# Process
# --------------------------------------------------------------------------------------------------------------------


def _ends(process: Sequence[ProcSample], t0: float, t1: float) -> tuple[ProcSample, ProcSample] | None:
    inside = sorted((s for s in process if t0 <= s.t_ns / NS_PER_S <= t1), key=lambda s: s.t_ns)
    if len(inside) < 2 or inside[-1].t_ns <= inside[0].t_ns:
        return None
    return inside[0], inside[-1]


def process_cores(process: Sequence[ProcSample], t0: float, t1: float) -> float | None:
    """Server process user + system CPU seconds per second (includes threads that exited)."""
    ends = _ends(process, t0, t1)
    if ends is None:
        return None
    a, b = ends
    return ((b.cpu_user_s + b.cpu_sys_s) - (a.cpu_user_s + a.cpu_sys_s)) / ((b.t_ns - a.t_ns) / NS_PER_S)


def process_cpu_s(process: Sequence[ProcSample], t0: float, t1: float) -> float | None:
    """Server process CPU seconds spent over the window."""
    ends = _ends(process, t0, t1)
    if ends is None:
        return None
    a, b = ends
    return (b.cpu_user_s + b.cpu_sys_s) - (a.cpu_user_s + a.cpu_sys_s)


def aux_cores(process: Sequence[ProcSample], t0: float, t1: float) -> float | None:
    """Launcher tree CPU per second, excluding the server process; None when no launcher tree was sampled."""
    ends = _ends(process, t0, t1)
    if ends is None:
        return None
    a, b = ends
    if a.tree_cpu_s is None or b.tree_cpu_s is None:
        return None
    return (b.tree_cpu_s - a.tree_cpu_s) / ((b.t_ns - a.t_ns) / NS_PER_S)


# --------------------------------------------------------------------------------------------------------------------
# Machine
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Foreign:
    cores: float  # mean busy cores outside the server, bot and harness trees
    peak_10s: float  # highest mean over 10 consecutive samples
    server_set_cores: float  # mean foreign busy cores on the server's CPUs
    samples: int


def _machine_in(machine: Sequence[MachineSample], t0: float, t1: float) -> list[MachineSample]:
    return sorted((s for s in machine if t0 <= s.t_ns / NS_PER_S <= t1), key=lambda s: s.t_ns)


def foreign(machine: Sequence[MachineSample], t0: float, t1: float) -> Foreign | None:
    """Interference over the window; None without machine samples."""
    inside = _machine_in(machine, t0, t1)
    if not inside:
        return None
    values = [s.foreign_cores for s in inside]
    k = min(PEAK_SAMPLES, len(values))
    peak = max(math.fsum(values[i : i + k]) / k for i in range(len(values) - k + 1))
    return Foreign(
        cores=math.fsum(values) / len(values),
        peak_10s=peak,
        server_set_cores=math.fsum(s.foreign_server_set_cores for s in inside) / len(inside),
        samples=len(inside),
    )


def cpu_set_busy(machine: Sequence[MachineSample], cpus: Sequence[int], t0: float, t1: float) -> float | None:
    """Mean busy cores on ``cpus`` over the window (everything running there, from any process)."""
    if not cpus:
        return None
    inside = _machine_in(machine, t0, t1)
    per_sample = [
        math.fsum(s.per_cpu_busy[c] for c in cpus if c < len(s.per_cpu_busy)) for s in inside if s.per_cpu_busy
    ]
    return mean(per_sample)
