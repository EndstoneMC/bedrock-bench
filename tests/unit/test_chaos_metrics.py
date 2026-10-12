# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

import pytest

from bedrock_bench.metrics import chaos
from bedrock_bench.metrics.load import BotEvent
from bedrock_bench.model import LogLine, ProbeSample

NS = 1_000_000_000


def probe(until: int = 100) -> list[ProbeSample]:
    """Replies every 5 s; 20 TPS, except 10 TPS from 30 s to 60 s."""
    out, tick = [], 0
    for t in range(0, until + 1, 5):
        out.append(ProbeSample(t * NS, t * NS, tick))
        tick += 50 if 30 <= t < 60 else 100
    return out


class TestRegimes:
    def test_samples_are_labelled_by_the_applied_regime_at_their_end(self) -> None:
        changes = [(30.0, 1000), (60.0, 1)]
        split = chaos.regimes(probe(), changes, 0, 100)
        # a change exactly on a reply time falls between samples, not inside one
        assert split.mixed == 0
        assert split.per_regime[1000] == pytest.approx((10.0,) * 6)
        assert len(split.per_regime[1]) == 14
        assert split.seconds == {1: pytest.approx(70.0), 1000: pytest.approx(30.0)}
        assert split.applied == 2
        assert chaos.median(split.group(chaos.HIGH_REGIMES)) == pytest.approx(10.0)

    def test_straddling_samples_are_mixed_and_excluded(self) -> None:
        changes = [(32.5, 100), (61.0, 3)]
        split = chaos.regimes(probe(), changes, 0, 100)
        assert split.mixed == 2
        assert len(split.per_regime[100]) == 5  # 35..60
        assert split.seconds[100] == pytest.approx(61.0 - 32.5)
        assert split.seconds[3] == pytest.approx(100.0 - 61.0)

    def test_start_regime_and_changes_before_the_window(self) -> None:
        split = chaos.regimes(probe(), [(-5.0, 0)], 0, 100)
        assert set(split.per_regime) == {0}
        assert split.applied == 0
        assert split.seconds == {0: pytest.approx(100.0)}

    def test_table(self) -> None:
        split = chaos.regimes(probe(), [(30.0, 1000), (60.0, 1)], 0, 100)
        assert chaos.regime_table(split)[1] == [1000, pytest.approx(30.0), 6, pytest.approx(10.0)]


def rec(
    action: str,
    status: str = "sent",
    scheduled: float | None = 0.0,
    sent: float | None = None,
    applied: float | None = None,
    **detail: Any,
) -> dict[str, Any]:
    def ns(v: float | None) -> int | None:
        return None if v is None else round(v * NS)

    return {
        "action": action,
        "status": status,
        "scheduled_ns": ns(scheduled),
        "sent_ns": ns(sent),
        "applied_ns": ns(applied),
        "applied": detail or None,
    }


def test_records_are_rebased_and_regime_changes_come_from_applied_lines() -> None:
    origin = 50 * NS
    raw = [
        rec("tickspeed", scheduled=60.0, sent=60.5, applied=61.0, tickspeed=100),
        rec("tickspeed", scheduled=90.0, sent=90.2, applied=None),  # never confirmed: no regime change
        rec("weather", scheduled=70.0, sent=70.1),
        {"note": "ignored without an action"},
    ]
    recs = chaos.records(raw, origin_mono_ns=origin)
    assert len(recs) == 3
    assert recs[0].scheduled == pytest.approx(10.0)
    assert recs[0].lag == pytest.approx(0.5)
    assert chaos.regime_changes(recs) == [(pytest.approx(11.0), 100)]
    assert chaos.tickspeed_queued(recs, 0, 100) == 2


def test_action_counts() -> None:
    recs = chaos.records(
        [
            rec("kick", scheduled=10.0, sent=10.5, applied=11.0),
            rec("kick", scheduled=20.0, sent=27.0),  # late
            rec("summon", status="unsupported", scheduled=30.0),
            rec("kick", status="skip_offline", scheduled=40.0),
            rec("weather", status="overrun", scheduled=50.0),
        ]
    )
    totals, per_action = chaos.actions(recs)
    assert (totals.planned, totals.sent, totals.applied, totals.late) == (5, 2, 1, 1)
    assert (totals.unsupported, totals.skip_offline, totals.overrun) == (1, 1, 1)
    assert list(per_action) == ["kick", "summon", "weather"]
    table = chaos.action_table(per_action)
    assert table[0] == ["kick", 3, 2, 1, 1, 0, 1, pytest.approx(7.0), pytest.approx(7.0)]


def test_not_respawned() -> None:
    def ev(t: float, kind: str, bot: str) -> BotEvent:
        return BotEvent(t, kind, {"ev": kind, "bot": bot})

    bots = [
        ev(5.0, "died", "a00"),  # before chaos
        ev(20.0, "died", "a01"),
        ev(21.0, "respawned", "a01"),
        ev(30.0, "died", "a02"),
        ev(40.0, "died", "a03"),
        ev(45.0, "rejoin", "a03"),
    ]
    assert chaos.not_respawned(bots, 10.0) == 1


def test_error_categories() -> None:
    def line(text: str) -> LogLine:
        return LogLine(0, 0, "error", None, text, text)

    lines = [line("Failed to send packet 1234 to 'a01'"), line("Failed to send packet 99 to 'a02'"), line("other")]
    assert chaos.category('Packet 1234 from "b07" dropped') == "Packet # from '…' dropped"
    assert chaos.error_table(lines) == [["Failed to send packet # to '…'", 2], ["other", 1]]
    assert len(chaos.category("x" * 500)) == chaos.CATEGORY_LENGTH
