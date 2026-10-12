# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from bedrock_bench.metrics.stats import (
    Censored,
    censored_median,
    distribution,
    mean,
    median,
    pct,
    rank,
    separated,
    slope,
    stat,
)

TEN = [float(v) for v in range(1, 11)]


class TestNearestRank:
    def test_index_is_rounded_q_times_n_minus_one(self) -> None:
        assert pct(TEN, 0.0) == 1
        assert pct(TEN, 1.0) == 10
        assert pct(TEN, 0.5) == 6  # 0.5 * 9 = 4.5 rounds up to index 5
        assert pct(TEN, 0.9) == 9  # 8.1 -> 8
        assert pct(TEN, 0.05) == 1  # 0.45 -> 0

    def test_halves_round_up(self) -> None:
        assert rank(0.25, 3) == 1
        assert rank(0.5, 2) == 1
        assert rank(0.5, 6) == 3

    def test_order_of_input_does_not_matter(self) -> None:
        assert pct(reversed(TEN), 0.9) == 9

    def test_empty_and_bad_quantile(self) -> None:
        assert pct([], 0.5) is None
        with pytest.raises(ValueError):
            rank(1.5, 3)
        with pytest.raises(ValueError):
            rank(0.5, 0)


def test_median_and_mean() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 2.0, 3.0]) == 2.5
    assert median([]) is None
    assert mean([1.0, 2.0, 6.0]) == 3.0
    assert mean([]) is None


class TestDistribution:
    def test_p5_is_left_out_below_twenty_samples(self) -> None:
        small = distribution(TEN, ("min", "p5", "median", "max"))
        assert small == {"min": 1.0, "median": 5.5, "max": 10.0, "n": 10}

    def test_p5_with_twenty_samples(self) -> None:
        values = [float(v) for v in range(20)]
        result = distribution(values, ("min", "p5", "median", "max"))
        assert result is not None
        assert result["p5"] == 1.0  # 0.05 * 19 = 0.95 -> index 1
        assert result["n"] == 20

    def test_empty(self) -> None:
        assert distribution([], ("median",)) is None

    def test_unknown_statistic(self) -> None:
        with pytest.raises(ValueError):
            stat(TEN, "mode")


class TestCensoredMedian:
    def test_observed_middle(self) -> None:
        assert censored_median([1.0, 2.0, Censored(3.0)]) == 2.0
        assert censored_median([5.0, Censored(1.0), 2.0, 3.0, 4.0]) == 4.0

    def test_censored_values_sort_last_whatever_their_bound(self) -> None:
        # censoring happens at the end of observation, so a censored value sorts after every observed one
        assert censored_median([Censored(1.0), 2.0, 3.0]) == 3.0

    def test_more_than_half_censored_is_unknown(self) -> None:
        assert censored_median([1.0, Censored(2.0), Censored(3.0)]) is None

    def test_even_count_needs_both_middles(self) -> None:
        assert censored_median([1.0, 2.0, 3.0, Censored(4.0)]) == 2.5
        assert censored_median([1.0, 2.0, Censored(3.0), Censored(4.0)]) is None

    def test_empty(self) -> None:
        assert censored_median([]) is None


class TestSeparation:
    def test_complete_separation_only(self) -> None:
        assert separated([19.5, 19.6, 19.7], [12.0, 14.0, 19.0], higher_is_better=True)
        assert not separated([19.5, 19.6, 19.7], [12.0, 14.0, 19.6], higher_is_better=True)

    def test_lower_is_better(self) -> None:
        assert separated([40.0, 41.0, 42.0], [50.0, 55.0, 60.0], higher_is_better=False)
        assert not separated([40.0, 51.0], [50.0, 55.0], higher_is_better=False)

    def test_ties_and_empty_are_not_separated(self) -> None:
        assert not separated([20.0], [20.0], higher_is_better=True)
        assert not separated([], [1.0], higher_is_better=True)


def test_slope() -> None:
    assert slope([(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)]) == pytest.approx(2.0)
    assert slope([(1.0, 1.0)]) is None
    assert slope([(1.0, 1.0), (1.0, 2.0)]) is None
