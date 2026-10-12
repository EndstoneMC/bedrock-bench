# SPDX-License-Identifier: Apache-2.0
"""Memory of the server process, from the 1 Hz process samples.

``private_mib`` is PrivateUsage on Windows and RssAnon + VmSwap on Linux. The two operating systems measure different
things, so values are never compared across them, and JVM servers run with a fixed heap that dominates both.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from bedrock_bench.model import ProcSample

NS_PER_S = 1e9


@dataclass(frozen=True)
class Mem:
    samples: int
    private_mean: float


def window(process: Sequence[ProcSample], t0: float, t1: float) -> Mem | None:
    """Mean private memory over the samples in the window; None without samples."""
    inside = [s for s in process if t0 <= s.t_ns / NS_PER_S <= t1]
    if not inside:
        return None
    return Mem(samples=len(inside), private_mean=math.fsum(s.private_mib for s in inside) / len(inside))
