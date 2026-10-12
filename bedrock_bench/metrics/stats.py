# SPDX-License-Identifier: Apache-2.0
"""Order statistics shared by every metric.

Percentiles are nearest rank, ``sorted[round(q * (n - 1))]`` with halves rounded up, so a reported percentile is
always a value that was observed. Medians are the conventional ones (the mean of the two middle values for even
``n``). Nothing here interpolates, resamples or drops outliers.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Censored:
    """A right-censored observation: the true value is larger than ``bound``."""

    bound: float


def rank(q: float, n: int) -> int:
    """Index of the nearest-rank ``q`` quantile in a sorted list of ``n`` values."""
    if n <= 0:
        raise ValueError("rank of an empty sample")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile {q} is outside [0, 1]")
    # The epsilon keeps q * (n - 1) == k + 0.5 from landing just below the half because of binary rounding.
    return min(n - 1, math.floor(q * (n - 1) + 0.5 + 1e-9))


def pct(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile; None for an empty sample."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[rank(q, len(ordered))]


def median(values: Iterable[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return statistics.median(ordered)


def mean(values: Iterable[float]) -> float | None:
    data = list(values)
    if not data:
        return None
    return math.fsum(data) / len(data)


def censored_median(values: Iterable[float | Censored]) -> float | None:
    """Median of a sample with right-censored values.

    Censored values sort after every observed one. The median is known only when the middle value (both middle values
    for even ``n``) is observed; otherwise the result is None. For odd ``n`` that means more than half are censored.
    """
    data = list(values)
    if not data:
        return None
    ordered = sorted(data, key=lambda v: (1, v.bound) if isinstance(v, Censored) else (0, v))
    n = len(ordered)
    middle = [ordered[n // 2]] if n % 2 else [ordered[n // 2 - 1], ordered[n // 2]]
    observed = [v for v in middle if not isinstance(v, Censored)]
    if len(observed) < len(middle):
        return None
    return math.fsum(observed) / len(observed)


def stat(values: Sequence[float], name: str) -> float | None:
    """One named statistic: ``min``, ``max``, ``median``, ``mean`` or ``p<NN>`` (nearest rank)."""
    if name == "min":
        return min(values, default=None)
    if name == "max":
        return max(values, default=None)
    if name == "median":
        return median(values)
    if name == "mean":
        return mean(values)
    if name.startswith("p") and name[1:].isdigit():
        return pct(values, int(name[1:]) / 100)
    raise ValueError(f"unknown statistic {name!r}")


def distribution(values: Iterable[float], names: Sequence[str]) -> dict[str, float | int] | None:
    """``{name: value, ..., "n": n}`` for the named statistics; None for an empty sample."""
    data = sorted(values)
    if not data:
        return None
    out: dict[str, float | int] = {}
    for name in names:
        value = stat(data, name)
        if value is not None:
            out[name] = value
    out["n"] = len(data)
    return out
