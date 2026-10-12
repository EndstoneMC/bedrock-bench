# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import math
import random
from collections.abc import Callable

import pytest

from bedrock_bench.metrics import tps
from bedrock_bench.metrics.stats import Censored
from bedrock_bench.model import ProbeSample

NS = 1_000_000_000


def ticks(rate: Callable[[float], float], t: float, step: float = 0.001) -> int:
    """Ticks executed from 0 to ``t`` under a piecewise-constant rate (integrated numerically on a 1 ms grid)."""
    n = round(t / step)
    return math.floor(sum(rate(k * step) for k in range(n)) * step + 1e-6)


def probe(
    rate: Callable[[float], float],
    start: float = 0.0,
    end: float = 200.0,
    every: float = 5.0,
    latency: float = 0.0,
    jitter: Callable[[int], float] = lambda i: 0.0,
) -> list[ProbeSample]:
    out = []
    t, i = start, 0
    while t <= end + 1e-9:
        rx = t + latency + jitter(i)
        out.append(ProbeSample(round(t * NS), round(rx * NS), ticks(rate, rx)))
        i += 1
        t = start + i * every
    return out


def constant(value: float) -> Callable[[float], float]:
    return lambda t: value


def step_dip(t: float) -> float:
    return 10.0 if 50.0 <= t < 70.0 else 20.0


class TestConstant20:
    samples = probe(constant(20.0))

    def test_window_and_stats(self) -> None:
        assert tps.window(self.samples, 100, 190) == pytest.approx(20.0)
        stats = tps.stats(self.samples, 0, 200)
        assert stats is not None
        assert stats["min"] == pytest.approx(20.0) and stats["max"] == pytest.approx(20.0)
        assert stats["n"] == 40
        assert stats["p5"] == pytest.approx(20.0)

    def test_headroom_metrics_are_zero(self) -> None:
        assert tps.lowest(self.samples, 0, 200, 4.5) == pytest.approx(20.0)
        assert tps.ticks_lost(self.samples, 0, 200) == pytest.approx(0.0)
        assert tps.time_below(self.samples, 0, 200) == 0
        assert tps.recovery_s(self.samples, 10, 200) == 0.0

    def test_series_is_stamped_at_the_later_reply(self) -> None:
        series = tps.series(self.samples)
        assert series[0] == (5.0, pytest.approx(20.0))
        assert len(series) == len(self.samples) - 1


class TestStepDip:
    samples = probe(step_dip)

    def test_lowest_5_and_15_seconds(self) -> None:
        assert tps.lowest(self.samples, 20, 150, 4.5) == pytest.approx(10.0)
        assert tps.lowest(self.samples, 20, 150, 14.5) == pytest.approx(10.0)
        # a 25 s stretch cannot fit inside the 20 s dip
        assert tps.lowest(self.samples, 20, 150, 24.5) == pytest.approx((5 * 20 + 20 * 10) / 25)

    def test_ticks_lost_is_the_dip_area(self) -> None:
        assert tps.ticks_lost(self.samples, 20, 150) == pytest.approx(200.0)

    def test_time_below(self) -> None:
        assert tps.time_below(self.samples, 20, 150) == pytest.approx(20.0)

    def test_recovery_after_the_dip(self) -> None:
        # first drop sample ends at 55; the first 30 s window that is healthy throughout starts at the reply at 70
        assert tps.recovery_s(self.samples, 30, 200) == pytest.approx(40.0)

    def test_window_tps_over_the_dip(self) -> None:
        assert tps.window(self.samples, 45, 75) == pytest.approx((5 * 20 + 20 * 10 + 5 * 20) / 30)


def test_recovery_is_censored_when_the_rate_never_comes_back() -> None:
    samples = probe(lambda t: 20.0 if t < 50 else 12.0)
    result = tps.recovery_s(samples, 30, 200)
    assert result == Censored(200 - 30 - 30)


def test_recovery_needs_every_sample_above_the_floor() -> None:
    # a single 5 s sample at 18.9 inside an otherwise healthy 30 s window blocks recovery at that window
    def rate(t: float) -> float:
        if 50 <= t < 55:
            return 10.0
        if 65 <= t < 70:
            return 18.9
        return 20.0

    samples = probe(rate)
    assert tps.recovery_s(samples, 30, 200) == pytest.approx(70.0 - 30.0)


def test_reply_jitter_never_exceeds_20_5_over_five_seconds() -> None:
    rng = random.Random(20261010)
    jitter = [rng.uniform(-0.05, 0.05) for _ in range(100)]
    samples = probe(constant(20.0), latency=0.1, jitter=lambda i: jitter[i])
    series = [rate for _, rate in tps.series(samples)]
    assert max(series) > 20.0  # jitter shows in single samples and is kept
    assert max(series) <= 20.5
    assert tps.lowest(samples, 0, 200, 4.5) >= 19.5
    assert tps.window(samples, 10, 190) == pytest.approx(20.0, abs=0.02)


def test_sparse_replies_make_the_window_unavailable() -> None:
    samples = [s for s in probe(constant(20.0)) if s.rx_ns / NS <= 160]
    assert tps.window(samples, 100, 190) is None  # covers 60 of 90 s
    assert tps.window(samples, 100, 190, min_coverage=0.5) == pytest.approx(20.0)
    # coverage depends on the end replies only; gaps inside are the probe_coverage gate's business
    gappy = [s for s in probe(constant(20.0)) if not 105 <= s.rx_ns / NS <= 185]
    assert tps.window(gappy, 100, 190) == pytest.approx(20.0)


def test_window_needs_two_replies() -> None:
    samples = probe(constant(20.0))
    assert tps.window(samples, 101, 104) is None
    assert tps.window(samples, 10, 10) is None
    assert tps.stats(samples, 101, 104) is None
    assert tps.ticks_lost(samples, 101, 104) is None
    assert tps.time_below(samples, 101, 104) is None


def test_samples_need_both_replies_in_the_window() -> None:
    samples = probe(constant(20.0))
    inside = tps.samples_in(samples, 12, 31)
    assert [(a, b) for a, b, _ in inside] == [(15.0, 20.0), (20.0, 25.0), (25.0, 30.0)]


def test_latency_gap_and_counter_checks() -> None:
    samples = probe(constant(20.0), latency=0.25)
    assert tps.latency_ms(samples, 0, 20) == pytest.approx([250.0] * 4)
    assert tps.max_gap(samples, 0, 20) == pytest.approx(5.0)
    gappy = [s for s in samples if not 40 <= s.rx_ns / NS <= 60]
    assert tps.max_gap(gappy, 30, 80) == pytest.approx(60.25 - 35.25)
    assert tps.max_gap([], 0, 10) == 10
    assert tps.decreases(samples) == 0
    assert tps.advanced(samples, 0, 20) == 300

    broken = [*samples[:3], ProbeSample(samples[3].send_ns, samples[3].rx_ns, 0), *samples[4:]]
    assert tps.decreases(broken) == 1


def test_replies_are_sorted_by_receive_time() -> None:
    a = ProbeSample(10 * NS, 11 * NS, 220)
    b = ProbeSample(0, 1 * NS, 20)
    assert [r.tick for r in tps.replies([a, b])] == [20, 220]
    assert tps.window([a, b], 0, 12, min_coverage=0.5) == pytest.approx(20.0)
