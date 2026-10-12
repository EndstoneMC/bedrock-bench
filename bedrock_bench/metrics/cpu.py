# SPDX-License-Identifier: Apache-2.0
"""CPU metrics: tick thread, process, and the machine-wide interference counters (docs/metrics.md).

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
from collections.abc import Sequence
from dataclasses import dataclass

from bedrock_bench.metrics import tps
from bedrock_bench.metrics.stats import mean
from bedrock_bench.model import CpuSample, MachineSample, ProbeSample, ProcSample

NS_PER_S = 1e9
#: Length of the rolling window for peak foreign load, in machine samples (1 Hz).
PEAK_SAMPLES = 10

_DIGITS = re.compile(r"\d+")


def normalize_name(name: str) -> str:
    """Thread name with every digit run replaced by ``#``, so pool members group together."""
    return _DIGITS.sub("#", name) if name else "(unnamed)"


# --------------------------------------------------------------------------------------------------------------------
# Tick thread
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TickCpu:
    ms_per_tick: float | None  # 1000 * dCPU / dG over the probe replies bounding the window
    ticks: int | None  # dG
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


def tick(tick_samples: Sequence[CpuSample], probe: Sequence[ProbeSample], t0: float, t1: float) -> TickCpu | None:
    """Tick-thread CPU per executed tick over the window; None without two 10 Hz samples in the window."""
    inside = sorted((s for s in tick_samples if t0 <= s.t_ns / NS_PER_S <= t1), key=lambda s: s.t_ns)
    if len(inside) < 2 or inside[-1].t_ns <= inside[0].t_ns:
        return None

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
    return TickCpu(ms_per_tick=ms_per_tick, ticks=ticks, samples=len(inside))


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
