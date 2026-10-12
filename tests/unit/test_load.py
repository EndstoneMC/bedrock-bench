# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

import pytest

from bedrock_bench.metrics import load
from bedrock_bench.metrics.load import BotEvent
from bedrock_bench.metrics.stats import Censored

NS = 1_000_000_000


def ev(t: float, kind: str, **data: Any) -> BotEvent:
    return BotEvent(t, kind, {"ev": kind, **data})


class TestTimedEvents:
    def test_minimum_delay_fit_per_bot_process(self) -> None:
        origin = 100 * NS
        raw = [
            {"ev": "hello", "t": 0, "mono_ms": 0, "rx_ns": origin + 10 * NS + 5_000_000},
            {"ev": "dial", "t": 0, "mono_ms": 1000, "rx_ns": origin + 11 * NS + 1_000_000},
            {"ev": "dial", "t": 0, "mono_ms": 2000, "rx_ns": origin + 12 * NS + 9_000_000},
        ]
        events = load.timed_events(raw, origin_mono_ns=origin)
        # offset = min(rx - mono) = 10.001 s
        assert [e.t for e in events] == pytest.approx([10.001, 11.001, 12.001])

    def test_epoch_fallback_is_independent_of_time_of_day(self) -> None:
        midnight = 1_791_849_600_000
        raw = [
            {"ev": "hello", "t": midnight - 500, "mono_ms": 0},
            {"ev": "dial", "t": midnight + 500, "mono_ms": 1000},
        ]
        events = load.timed_events(raw, origin_epoch_ms=midnight - 10_000)
        assert [e.t for e in events] == pytest.approx([9.5, 10.5])

    def test_each_hello_starts_a_new_clock(self) -> None:
        raw = [
            {"ev": "hello", "t": 0, "mono_ms": 0, "rx_ns": 10 * NS},
            {"ev": "done", "t": 0, "mono_ms": 5000, "rx_ns": 15 * NS},
            {"ev": "hello", "t": 0, "mono_ms": 0, "rx_ns": 20 * NS},
            {"ev": "dial", "t": 0, "mono_ms": 1000, "rx_ns": 21 * NS},
        ]
        events = load.timed_events(raw)
        assert [(e.ev, e.t) for e in events] == [("hello", 10.0), ("done", 15.0), ("hello", 20.0), ("dial", 21.0)]

    def test_events_are_sorted_and_unstamped_ones_dropped(self) -> None:
        raw = [
            {"ev": "hello", "t": 2000},
            {"ev": "dial", "t": 1000},
            {"ev": "stat"},
        ]
        events = load.timed_events(raw)
        assert [e.ev for e in events] == ["dial", "hello"]


def test_disc() -> None:
    assert load.disc(0) == 1
    assert load.disc(1) == 5
    assert load.disc(10) == 317


def test_join() -> None:
    bots = [
        ev(1.0, "dial", bot="a00", mono_ms=0),
        ev(1.8, "spawned", bot="a00", session=1, join_ms=800, radius=10),
        ev(2.0, "dial", bot="a01", mono_ms=1000),
        ev(3.2, "spawned", bot="a01", session=1, mono_ms=2200, radius=8),
        ev(4.0, "join_failed", bot="a02", stage="spawn"),
    ]
    stats = load.join(bots)
    assert stats.join_ms == (800.0, 1200.0)
    assert stats.joined == 2
    assert stats.failed == 1
    assert load.granted_radius(bots) == 9.0


def progress(t: float, *rows: tuple[str, int, int, float | None]) -> BotEvent:
    return ev(t, "progress", rows=[[b, uniq, view, 0.0, y, 0.0, 0, 0, 1] for b, uniq, view, y in rows])


class TestView:
    def bots(self) -> list[BotEvent]:
        return [
            ev(10.0, "tp", bot="a00", pos=[0, 300, 0], dim=0, n=1),
            ev(10.0, "tp", bot="a01", pos=[0, 300, 0], dim=0, n=1),
            ev(11.0, "first_chunk", bot="a00", dt_ms=1000),
            ev(12.0, "first_chunk", bot="a01", dt_ms=2000),
            progress(15.0, ("a00", 60, 50, 300.0), ("a01", 20, 10, 300.0)),
            # uniq counts chunks seen anywhere (old view, other dimensions); U_b is in_view only
            progress(20.0, ("a00", 400, 100, 70.0), ("a01", 90, 40, 300.0)),
            progress(25.0, ("a00", 410, 100, 70.0), ("a01", 95, 50, 300.0)),
            ev(
                40.0,
                "bot_summary",
                bot="a00",
                uniq=410,
                in_view=100,
                chunks=420,
                corrections=0,
                first_seen_ms=[1000] + [100] * 99,
            ),
        ]

    def test_u_b_is_the_in_view_count_at_settled(self) -> None:
        assert load.view_at(self.bots(), 22.0) == {"a00": 100, "a01": 40}
        assert load.view_at(self.bots(), 5.0) == {}

    def test_t90_from_arrival_times_and_progress_fallback(self) -> None:
        view = load.view(self.bots(), settled=22.0, n_target=80)
        # a00: ceil(0.9 * 100) = 90th chunk at 1000 + 89 * 100 ms; a01 has no summary: first snapshot with 36 chunks
        assert view.t90_own == pytest.approx({"a00": 9.9, "a01": 10.0})
        assert view.t90_fallback == 1

    def test_t_n_censors_bots_that_never_get_there(self) -> None:
        view = load.view(self.bots(), settled=22.0, n_target=80)
        assert view.t_n[0] == pytest.approx(1.0 + 79 * 0.1)
        assert view.t_n[1] == Censored(15.0)  # last progress at 25 s, first tp at 10 s

    def test_summary_shorter_than_n_is_censored_at_the_summary(self) -> None:
        view = load.view(self.bots(), settled=22.0, n_target=150)
        assert view.t_n[0] == Censored(30.0)

    def test_delivery_curve(self) -> None:
        shares = load.delivery(self.bots(), t_from=10.0, settled=22.0, fractions=(0.4, 0.5, 1.0))
        # totals: 60 at 15 s, 140 at 20 s; 40% of 140 = 56 -> 15 s; 50% = 70 -> 20 s
        assert shares == {0.4: 5.0, 0.5: 10.0, 1.0: 10.0}
        assert load.delivery([], 0.0, 22.0) == {0.5: None, 0.9: None}

    def test_first_tp_and_first_chunk(self) -> None:
        assert load.first_tp(self.bots()) == 10.0
        assert sorted(load.first_chunk_ms(self.bots())) == [1000.0, 2000.0]


def test_resend_ratio_and_csg() -> None:
    bots = [
        ev(10.0, "stat", level_chunks=1050, uniq=1000),
        ev(30.0, "stat", level_chunks=5000, uniq=1000),
    ]
    assert load.resend_ratio(bots, 20.0) == pytest.approx(1.05)
    assert load.resend_ratio(bots, 5.0) is None
    assert load.csg_effective({"a": 70, "b": 80, "c": 90}, 10) is True
    assert load.csg_effective({"a": 300, "b": 310}, 10) is False
    assert load.csg_effective({}, 10) is None
