# SPDX-License-Identifier: Apache-2.0
"""Tick rate from the probe.

The probe asks the server for a counter that advances once per executed tick. Each reply is
``(s_i, t_i, g_i)``: send time, receive time and counter. Times here are seconds since the run origin (the samples
carry nanoseconds since the origin, see ``metrics.compute``). A window ``[t0, t1]`` uses the replies
``a = min{i: t_i >= t0}`` to ``b = max{i: t_i <= t1}``.

Reply times jitter, so a single 5 s sample can read above 20. Values are never clamped; only windows of at least
4.5 s are published.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

from bedrock_bench.model import ProbeSample

NS_PER_S = 1e9


@dataclass(frozen=True)
class Reply:
    sent: float  # s since origin
    t: float  # receive time, s since origin
    tick: int


def replies(probe: Sequence[ProbeSample]) -> list[Reply]:
    """Probe replies in receive order."""
    out = [Reply(s.send_ns / NS_PER_S, s.rx_ns / NS_PER_S, s.tick) for s in probe]
    out.sort(key=lambda r: (r.t, r.sent, r.tick))
    return out


def bounds(points: list[Reply], t0: float, t1: float) -> tuple[int, int] | None:
    """``(a, b)``: indices of the first and last of ``points`` inside ``[t0, t1]``; None without two of them."""
    times = [p.t for p in points]
    a = bisect.bisect_left(times, t0)
    b = bisect.bisect_right(times, t1) - 1
    if a >= len(points) or b < 0 or b <= a:
        return None
    return a, b


def series(probe: Sequence[ProbeSample]) -> list[tuple[float, float]]:
    """``(t_{i+1}, (g_{i+1} - g_i) / (t_{i+1} - t_i))`` for consecutive replies, stamped at the later one."""
    points = replies(probe)
    return [(q.t, (q.tick - p.tick) / (q.t - p.t)) for p, q in zip(points, points[1:], strict=False) if q.t > p.t]


def window(probe: Sequence[ProbeSample], t0: float, t1: float, min_coverage: float = 0.9) -> float | None:
    """``(g_b - g_a) / (t_b - t_a)``; None when the replies cover less than ``min_coverage`` of the window."""
    if t1 <= t0:
        return None
    points = replies(probe)
    span = bounds(points, t0, t1)
    if span is None:
        return None
    a, b = points[span[0]], points[span[1]]
    if b.t - a.t < min_coverage * (t1 - t0):
        return None
    return (b.tick - a.tick) / (b.t - a.t)


def count(probe: Sequence[ProbeSample], t0: float, t1: float) -> int:
    """Replies received inside ``[t0, t1]``."""
    return sum(1 for s in probe if t0 <= s.rx_ns / NS_PER_S <= t1)


def lowest(probe: Sequence[ProbeSample], t0: float, t1: float, min_span_s: float) -> float | None:
    """Lowest rate over any stretch of at least ``min_span_s`` inside the window.

    For each reply ``i`` in the window, ``j`` is the first later reply in the window with ``t_j - t_i >= min_span_s``
    and the stretch rate is ``(g_j - g_i) / (t_j - t_i)``. ``min_span_s`` = 4.5 gives the "lowest 5 s" value (the
    half second absorbs reply jitter around a 5 s probe).
    """
    points = replies(probe)
    span = bounds(points, t0, t1)
    if span is None:
        return None
    a, b = span
    best: float | None = None
    j = a
    for i in range(a, b + 1):
        j = max(j, i + 1)
        while j <= b and points[j].t - points[i].t < min_span_s:
            j += 1
        if j > b:
            break
        rate = (points[j].tick - points[i].tick) / (points[j].t - points[i].t)
        if best is None or rate < best:
            best = rate
    return best


def latency_ms(probe: Sequence[ProbeSample], t0: float, t1: float) -> list[float]:
    """``t_i - s_i`` in ms for the replies received inside the window."""
    return [(r.t - r.sent) * 1000.0 for r in replies(probe) if t0 <= r.t <= t1]


def max_gap(probe: Sequence[ProbeSample], t0: float, t1: float) -> float:
    """Longest stretch of the window without a reply, counting from ``t0`` and up to ``t1``."""
    times = [t0, *(r.t for r in replies(probe) if t0 <= r.t <= t1), t1]
    return max(b - a for a, b in zip(times, times[1:], strict=False))


def decreases(probe: Sequence[ProbeSample]) -> int:
    """How often the counter went down between consecutive replies (0 for a monotonic counter)."""
    points = replies(probe)
    return sum(1 for p, q in zip(points, points[1:], strict=False) if q.tick < p.tick)


def advanced(probe: Sequence[ProbeSample], t0: float, t1: float) -> int | None:
    """``g_b - g_a``: ticks executed between the first and last reply in the window."""
    points = replies(probe)
    span = bounds(points, t0, t1)
    if span is None:
        return None
    return points[span[1]].tick - points[span[0]].tick
