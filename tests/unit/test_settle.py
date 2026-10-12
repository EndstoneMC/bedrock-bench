# SPDX-License-Identifier: Apache-2.0
"""The settle rule and its interventions, driven with synthetic times and progress rows."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from bedrock_bench.grid import cell
from bedrock_bench.settle import BotView, Nudge, Reteleport, Settle, SettleRules
from bedrock_bench.workload import Grid
from workloads import WORKLOAD

START = 100.0  # alljoined, seconds since the run origin
N = 4
GRID = Grid(columns=11, spacing=640, origin=1280, y=300.0, warn_offset_blocks=200.0)
RULES = SettleRules(
    min_s=300.0,
    cap_s=900.0,
    rate_per_bot=0.1,
    rate_window_s=60.0,
    eval_interval_s=5.0,
    max_data_age_s=5.0,
    min_view_frac=0.0,
    view_complete_frac=0.95,
    view_median_frac=0.5,
    retp_after_s=20.0,
    retp_max=3,
    nudge_after_s=60.0,
    nudge_min_y=290.0,
    nudge_max_uniq=30,
    nudge_max=2,
    nudge_step=16.0,
)


def row(i: int, uniq: int, in_view: int | None = None, y: float | None = 70.0) -> list[Any]:
    x, z = cell(i, GRID)
    position = [None, None, None, None] if y is None else [x + 0.5, y, z + 0.5, 0]
    return [f"a{i:02d}", uniq, uniq if in_view is None else in_view, *position, 0, 1]


def rows(uniq: int, *, y: float | None = 70.0, n: int = N) -> list[list[Any]]:
    return [row(i, uniq, y=y) for i in range(n)]


def settle(**rules: Any) -> Settle:
    return Settle(dataclasses.replace(RULES, **rules), n=N, start=START, grid=GRID)


def placed(s: Settle, t: float, n: int = N, *, teleported: bool = True) -> None:
    for i in range(n):
        s.placed(f"a{i:02d}", i, t)
        if teleported:
            s.teleported(f"a{i:02d}", t + 0.5)


# --------------------------------------------------------------------------------------------------------------------
# Rules and rows
# --------------------------------------------------------------------------------------------------------------------


class TestRules:
    def test_rules_come_from_the_settle_phase(self) -> None:
        spec = WORKLOAD.phase("settle")
        rules = SettleRules.from_phase(spec)
        assert rules.cap_s == spec.cap_s
        assert dataclasses.asdict(rules) == {"cap_s": spec.cap_s, **spec.options}


class TestBotView:
    def test_full_row(self) -> None:
        view = BotView.from_row(["a07", 120, 100, 1.5, 300.0, 2.5, 0, 3, 1])
        assert view == BotView("a07", 120, 100, 1.5, 300.0, 2.5, 0)
        assert view.placed

    def test_row_without_position(self) -> None:
        view = BotView.from_row(["a07", 0, 0, None, None, None, None, 0, 0])
        assert view == BotView("a07", 0, 0)
        assert not view.placed

    @pytest.mark.parametrize("bad", [[], ["a07"], [7, 1, 1], ["a07", "x", 1]])
    def test_malformed_rows_are_skipped(self, bad: list[Any]) -> None:
        assert BotView.from_row(bad) is None


# --------------------------------------------------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------------------------------------------------


class TestRate:
    def test_rate_over_the_window(self) -> None:
        s = settle()
        for k in range(30):
            s.progress(START + 5 * k, rows(10 * 5 * k // N))  # 10 chunks per second in total
        assert s.rate(START + 145) == pytest.approx(10.0, rel=0.05)

    def test_base_is_the_newest_snapshot_a_window_back(self) -> None:
        s = settle(rate_window_s=60.0)
        s.progress(0.0, rows(0))
        s.progress(30.0, rows(100))
        s.progress(60.0, rows(100))
        s.progress(90.0, rows(100))
        assert s.rate(90.0) == 0.0  # base at 30 s, no growth since
        assert s.rate(60.0) == pytest.approx(400 / 60)  # base at 0 s

    def test_needs_two_snapshots(self) -> None:
        s = settle()
        assert s.rate(START) is None
        s.progress(START, rows(10))
        assert s.rate(START) is None

    def test_out_of_order_snapshots_are_ignored(self) -> None:
        s = settle()
        s.progress(10.0, rows(5))
        s.progress(5.0, rows(50))
        assert s.latest() is not None and s.latest()[0] == 10.0


class TestDecision:
    def test_not_before_min_s_even_when_quiet(self) -> None:
        s = settle()
        placed(s, START)
        s.progress(START, rows(300))
        s.progress(START + 290, rows(300))
        decision = s.evaluate(START + 290)
        assert not decision.done
        assert decision.rate_per_bot == 0.0

    def test_done_at_min_s_once_the_rate_is_low(self) -> None:
        s = settle()
        placed(s, START)
        for k in range(61):
            s.progress(START + 5 * k, rows(min(300, 10 * k)))
        decision = s.evaluate(START + 300)
        assert decision.done and not decision.capped
        assert s.settled_at == START + 300
        assert s.settle_record() == {"settle_s": 300.0, "capped": False, "views_incomplete": False}

    def test_not_done_while_views_still_grow(self) -> None:
        s = settle()
        placed(s, START)
        for k in range(61):
            s.progress(START + 5 * k, rows(10 * k))  # 2 chunks per second per bot
        decision = s.evaluate(START + 300)
        assert not decision.done
        assert decision.rate_per_bot == pytest.approx(2.0)

    def test_capped(self) -> None:
        s = settle(cap_s=900.0)
        placed(s, START)
        for k in range(181):
            s.progress(START + 5 * k, rows(10 * k))
        decision = s.evaluate(START + 900)
        assert decision.done and decision.capped
        assert s.settle_record()["capped"] is True

    def test_stale_data_decides_nothing_but_the_cap(self) -> None:
        s = settle(max_data_age_s=5.0)
        placed(s, START)
        s.progress(START, rows(300))
        s.progress(START + 290, rows(300))
        stale = s.evaluate(START + 310)
        assert not stale.fresh and not stale.done
        assert s.evaluate(START + 900).capped

    def test_views_incomplete_at_settled(self) -> None:
        s = settle(min_s=0.0)
        placed(s, START)
        lopsided = [row(0, 300), row(1, 300), row(2, 300), row(3, 10)]
        s.progress(START, lopsided)
        s.progress(START + 60, lopsided)
        assert s.evaluate(START + 60).done
        assert s.views_incomplete

    def test_min_view_frac_holds_the_rule(self) -> None:
        s = settle(min_s=0.0, min_view_frac=1.0)
        placed(s, START)
        lopsided = [row(0, 300), row(1, 300), row(2, 300), row(3, 10)]
        s.progress(START, lopsided)
        s.progress(START + 60, lopsided)
        assert not s.evaluate(START + 60).done
        s.progress(START + 65, rows(300))
        s.progress(START + 130, rows(300))
        assert s.evaluate(START + 130).done

    def test_done_is_final(self) -> None:
        s = settle(min_s=0.0)
        placed(s, START)
        s.progress(START, rows(10))
        s.progress(START + 60, rows(10))
        first = s.evaluate(START + 60)
        s.progress(START + 65, rows(5000))
        again = s.evaluate(START + 65)
        assert first.done and again.done and not again.actions
        assert s.settled_at == START + 60

    def test_evaluations_are_spaced(self) -> None:
        s = settle(eval_interval_s=5.0)
        assert s.due(START)
        s.evaluate(START)
        assert not s.due(START + 4.9)
        assert s.due(START + 5.0)


# --------------------------------------------------------------------------------------------------------------------
# Interventions
# --------------------------------------------------------------------------------------------------------------------


class TestReteleport:
    def test_a_bot_without_teleport_gets_its_placement_again(self) -> None:
        s = settle(retp_after_s=20.0)
        placed(s, START, teleported=False)
        assert not s.evaluate(START + 19).actions
        actions = s.evaluate(START + 20).actions
        assert actions == tuple(Reteleport(f"a{i:02d}", i, 1) for i in range(N))

    def test_a_teleport_after_placement_clears_it(self) -> None:
        s = settle()
        s.placed("a00", 0, START)
        s.teleported("a00", START + 1)
        assert not s.evaluate(START + 30).actions

    def test_a_teleport_before_the_latest_placement_does_not_count(self) -> None:
        s = settle()
        s.placed("a00", 0, START)
        s.teleported("a00", START - 5)
        assert s.evaluate(START + 20).actions == (Reteleport("a00", 0, 1),)

    def test_bounded_per_bot(self) -> None:
        s = settle(retp_after_s=20.0, retp_max=3, eval_interval_s=0.0)
        s.placed("a00", 0, START)
        attempts = [a.attempt for t in range(20, 200, 20) for a in s.evaluate(START + t).actions]
        assert attempts == [1, 2, 3]
        assert s.interventions_record()["retp"] == 3
        assert s.interventions_record()["retp_bots"] == ["a00"]

    def test_offline_bots_are_left_alone(self) -> None:
        s = settle()
        s.placed("a00", 0, START)
        s.left("a00", START + 1)
        assert not s.evaluate(START + 30).actions


class TestNudge:
    def hovering(self, **rules: Any) -> Settle:
        s = settle(eval_interval_s=0.0, **rules)
        placed(s, START)
        return s

    def test_not_before_nudge_after_s(self) -> None:
        s = self.hovering(nudge_after_s=60.0)
        s.progress(START + 59, rows(5, y=300.0))
        assert not s.evaluate(START + 59).actions

    def test_hovering_bots_with_few_chunks_are_moved_alternately(self) -> None:
        s = self.hovering()
        s.progress(START + 60, rows(5, y=300.0))
        first = s.evaluate(START + 60).actions
        x, z = cell(0, GRID)
        assert first[0] == Nudge("a00", 0, x + 16.0, 300.0, z, 1)
        assert len(first) == N
        s.progress(START + 70, rows(5, y=300.0))
        assert not s.evaluate(START + 70).actions  # held off for retp_after_s after the last nudge
        s.progress(START + 80, rows(5, y=300.0))
        second = s.evaluate(START + 80).actions
        assert second[0] == Nudge("a00", 0, x, 300.0, z, 2)
        s.progress(START + 120, rows(5, y=300.0))
        assert not s.evaluate(START + 120).actions  # at most nudge_max per bot
        record = s.interventions_record()
        assert (record["nudges"], record["nudged"]) == (2 * N, [f"a{i:02d}" for i in range(N)])

    @pytest.mark.parametrize(("y", "uniq"), [(290.0, 5), (70.0, 5), (300.0, 30), (None, 5)])
    def test_landed_or_loaded_bots_stay(self, y: float | None, uniq: int) -> None:
        s = self.hovering()
        s.progress(START + 60, rows(uniq, y=y))
        assert not s.evaluate(START + 60).actions

    def test_stale_progress_nudges_nobody(self) -> None:
        s = self.hovering(max_data_age_s=5.0)
        s.progress(START + 60, rows(5, y=300.0))
        assert not s.evaluate(START + 66).actions

    def test_interventions_are_never_in_measured_windows(self) -> None:
        assert settle().interventions_record() == {
            "nudges": 0,
            "nudged": [],
            "retp": 0,
            "retp_bots": [],
            "in_measured": 0,
        }


def test_needs_a_bot() -> None:
    with pytest.raises(ValueError):
        Settle(RULES, n=0, start=0.0, grid=GRID)
