# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from bedrock_bench.clock import NS_PER_MS, NS_PER_S, OffsetMap, Origin, Stamp, mono_ns, utc_compact, utc_iso


def test_stamp_now_reads_both_clocks() -> None:
    before_wall = time.time_ns() // NS_PER_MS
    first = Stamp.now()
    second = Stamp.now()
    after_wall = time.time_ns() // NS_PER_MS
    assert first.mono_ns <= second.mono_ns <= mono_ns()
    assert before_wall - 5 <= first.epoch_ms <= after_wall + 5


def test_origin_capture_is_now() -> None:
    origin = Origin.capture()
    assert abs(origin.seconds(mono_ns())) < 5


class TestOrigin:
    origin = Origin(mono_ns=50 * NS_PER_S, epoch_ms=1_791_813_480_000)

    def test_seconds_and_back(self) -> None:
        assert self.origin.seconds(self.origin.mono_ns) == 0
        assert self.origin.seconds(self.origin.mono_ns + 2_500_000_000) == 2.5
        assert self.origin.mono_at(2.5) == self.origin.mono_ns + 2_500_000_000
        assert self.origin.seconds(self.origin.mono_at(-1.25)) == -1.25

    def test_epoch_mapping_round_trip(self) -> None:
        mono = self.origin.epoch_to_mono_ns(self.origin.epoch_ms + 1500)
        assert mono == self.origin.mono_ns + 1500 * NS_PER_MS
        assert self.origin.mono_to_epoch_ms(mono) == self.origin.epoch_ms + 1500

    def test_mapping_is_independent_of_midnight(self) -> None:
        midnight = int(datetime(2026, 10, 12, tzinfo=UTC).timestamp() * 1000)
        origin = Origin(mono_ns=10 * NS_PER_S, epoch_ms=midnight - 2000)
        before, after = origin.epoch_to_mono_ns(midnight - 1000), origin.epoch_to_mono_ns(midnight + 1000)
        assert after - before == 2 * NS_PER_S

    def test_drift(self) -> None:
        steady = Stamp(self.origin.mono_ns + 60 * NS_PER_S, self.origin.epoch_ms + 60_000)
        stepped = Stamp(self.origin.mono_ns + 60 * NS_PER_S, self.origin.epoch_ms + 61_500)
        assert self.origin.drift_ms(steady) == 0
        assert self.origin.drift_ms(stepped) == 1500


class TestOffsetMap:
    def test_minimum_delay_fit(self) -> None:
        # true offset 1000 ns; delays 5, 1 and 30 ns
        pairs = [(0, 1005), (100, 1101), (200, 1230)]
        fitted = OffsetMap.fit(pairs)
        assert fitted.offset_ns == 1001
        assert fitted(500) == 1501

    def test_fit_accepts_iterators(self) -> None:
        assert OffsetMap.fit(iter([(10, 15)])).offset_ns == 5

    def test_fit_needs_data(self) -> None:
        with pytest.raises(ValueError):
            OffsetMap.fit([])

    def test_from_origin_matches_origin(self) -> None:
        origin = Origin(mono_ns=7 * NS_PER_S, epoch_ms=1_791_813_480_000)
        epoch_map = OffsetMap.from_origin(origin)
        epoch_ms = origin.epoch_ms + 12_345
        assert epoch_map(epoch_ms * NS_PER_MS) == origin.epoch_to_mono_ns(epoch_ms)


def test_utc_formats() -> None:
    moment = datetime(2026, 10, 12, 13, 58, 0, tzinfo=UTC)
    epoch_ms = int(moment.timestamp()) * 1000 + 123
    assert utc_compact(epoch_ms) == "20261012T135800Z"
    assert utc_iso(epoch_ms) == "2026-10-12T13:58:00.123Z"
    assert utc_compact(0) == "19700101T000000Z"
    assert utc_iso(999) == "1970-01-01T00:00:00.999Z"
