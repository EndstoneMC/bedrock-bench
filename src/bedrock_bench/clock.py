# SPDX-License-Identifier: Apache-2.0
"""One timebase per run.

The harness stamps everything it records with the monotonic clock in nanoseconds. Wall-clock time enters a run once,
as the origin pair taken at start, and through sources that only carry their own stamps (bot events carry epoch ``t``
and bot-monotonic ``mono_ms``). Mapping those onto the harness timebase goes through this module, so no metric ever
mixes clocks or depends on the time of day.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000


def mono_ns() -> int:
    """Harness monotonic time in nanoseconds."""
    return time.monotonic_ns()


@dataclass(frozen=True, order=True)
class Stamp:
    """A monotonic reading and the wall-clock time it corresponds to."""

    mono_ns: int
    epoch_ms: int

    @classmethod
    def now(cls) -> Stamp:
        """Read both clocks; the monotonic value is the midpoint of two reads around the wall-clock read."""
        before = time.monotonic_ns()
        wall_ns = time.time_ns()
        after = time.monotonic_ns()
        return cls((before + after) // 2, wall_ns // NS_PER_MS)


@dataclass(frozen=True)
class Origin:
    """The run's origin pair. Phase marks are seconds since ``mono_ns``."""

    mono_ns: int
    epoch_ms: int

    @classmethod
    def capture(cls) -> Origin:
        stamp = Stamp.now()
        return cls(stamp.mono_ns, stamp.epoch_ms)

    def seconds(self, mono_ns: int) -> float:
        """Seconds since the origin."""
        return (mono_ns - self.mono_ns) / NS_PER_S

    def mono_at(self, seconds: float) -> int:
        """Monotonic ns of a time given in seconds since the origin."""
        return self.mono_ns + round(seconds * NS_PER_S)

    def epoch_to_mono_ns(self, epoch_ms: float) -> int:
        """Map a wall-clock stamp onto the monotonic timebase, assuming no clock step since the origin."""
        return self.mono_ns + round((epoch_ms - self.epoch_ms) * NS_PER_MS)

    def mono_to_epoch_ms(self, mono_ns: int) -> float:
        return self.epoch_ms + (mono_ns - self.mono_ns) / NS_PER_MS

    def drift_ms(self, end: Stamp) -> float:
        """How far the wall clock moved against the monotonic clock between the origin and ``end``.

        A large value means the wall clock was stepped (NTP, manual change) and epoch-mapped data is suspect.
        """
        return (end.epoch_ms - self.epoch_ms) - (end.mono_ns - self.mono_ns) / NS_PER_MS


@dataclass(frozen=True)
class OffsetMap:
    """Maps another clock onto harness monotonic time: ``harness_ns = source_ns + offset_ns``."""

    offset_ns: int

    def __call__(self, source_ns: int) -> int:
        return source_ns + self.offset_ns

    @classmethod
    def from_origin(cls, origin: Origin) -> OffsetMap:
        """Map epoch nanoseconds through the run's origin pair."""
        return cls(origin.mono_ns - origin.epoch_ms * NS_PER_MS)

    @classmethod
    def fit(cls, pairs: Iterable[tuple[int, int]]) -> OffsetMap:
        """Minimum-delay fit from ``(source_ns, received_ns)`` pairs.

        A message is always received after it was stamped, so ``received - source`` is the true offset plus a
        non-negative delay. The smallest difference is the tightest bound on the offset. Use it for sources whose
        clock is monotonic too (bot ``mono_ms``), where a constant offset is exact.
        """
        best: int | None = None
        for source_ns, received_ns in pairs:
            diff = received_ns - source_ns
            if best is None or diff < best:
                best = diff
        if best is None:
            raise ValueError("OffsetMap.fit needs at least one pair")
        return cls(best)


def utc_compact(epoch_ms: int) -> str:
    """``20261012T135800Z``: the timestamp form used in run ids."""
    return datetime.fromtimestamp(epoch_ms // 1000, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def utc_iso(epoch_ms: int) -> str:
    """``2026-10-12T13:58:00.123Z``."""
    moment = datetime.fromtimestamp(epoch_ms // 1000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"
