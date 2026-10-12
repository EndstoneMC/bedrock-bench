# SPDX-License-Identifier: Apache-2.0
"""Memory of the server process, from the 1 Hz process samples.

``resident_mib`` is the working set on Windows and VmRSS on Linux; ``private_mib`` is PrivateUsage on Windows and
RssAnon + VmSwap on Linux. The two operating systems measure different things, so values are never compared across
them, and JVM servers run with a fixed heap that dominates both.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from bedrock_bench.metrics.stats import slope
from bedrock_bench.model import ProcSample

NS_PER_S = 1e9
#: A memory slope over a shorter window is labelled weak.
SLOPE_MIN_S = 1800.0


@dataclass(frozen=True)
class Mem:
    samples: int
    private_mean: float
    private_max: float
    resident_mean: float
    resident_max: float
    threads_max: int
    handles_max: int | None  # Windows only


def _inside(process: Sequence[ProcSample], t0: float, t1: float) -> list[ProcSample]:
    return sorted((s for s in process if t0 <= s.t_ns / NS_PER_S <= t1), key=lambda s: s.t_ns)


def window(process: Sequence[ProcSample], t0: float, t1: float) -> Mem | None:
    """Mean and maximum over the samples in the window; None without samples."""
    inside = _inside(process, t0, t1)
    if not inside:
        return None
    handles = [s.handles for s in inside if s.handles is not None]
    return Mem(
        samples=len(inside),
        private_mean=math.fsum(s.private_mib for s in inside) / len(inside),
        private_max=max(s.private_mib for s in inside),
        resident_mean=math.fsum(s.resident_mib for s in inside) / len(inside),
        resident_max=max(s.resident_mib for s in inside),
        threads_max=max(s.threads for s in inside),
        handles_max=max(handles) if handles else None,
    )


def slope_mib_per_h(process: Sequence[ProcSample], t0: float, t1: float) -> float | None:
    """Least-squares slope of ``private_mib`` over the window, in MiB per hour."""
    points = [(s.t_ns / NS_PER_S / 3600.0, s.private_mib) for s in _inside(process, t0, t1)]
    return slope(points)


def start_end(process: Sequence[ProcSample], t0: float, t1: float) -> tuple[ProcSample, ProcSample] | None:
    """First and last sample in the window."""
    inside = _inside(process, t0, t1)
    if not inside:
        return None
    return inside[0], inside[-1]
