# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

import pytest

from bedrock_bench.metrics import workload
from bedrock_bench.metrics.load import BotEvent


def ev(t: float, kind: str, **data: Any) -> BotEvent:
    return BotEvent(t, kind, {"ev": kind, **data})


def stat(t: float, **counters: int) -> BotEvent:
    base = {"online": 4, "corrections": 0, "rx_pkts": 0, "tx_pkts": 0, "rx_bytes": 0}
    return ev(t, "stat", **(base | counters))


def test_stat_rates_per_bot_per_second() -> None:
    bots = [
        stat(100.0, corrections=1000, rx_pkts=10_000, tx_pkts=500, rx_bytes=1_024_000),
        stat(110.0, corrections=1400, rx_pkts=30_000, tx_pkts=900, rx_bytes=2_048_000),
        stat(120.0, corrections=1800, rx_pkts=50_000, tx_pkts=1300, rx_bytes=3_072_000),
    ]
    rates = workload.stat_rates(bots, 95, 125, n=4)
    assert rates["corrections"] == pytest.approx(800 / 4 / 20)
    assert rates["rx_pkts"] == pytest.approx(40_000 / 4 / 20)
    assert rates["tx_pkts"] == pytest.approx(800 / 4 / 20)
    assert rates["rx_kib"] == pytest.approx(2000 / 4 / 20)
    assert workload.stat_rates(bots, 95, 105, n=4)["corrections"] is None


def test_packet_mix_uses_only_intervals_inside_the_window() -> None:
    bots = [
        ev(0.0, "hello"),
        ev(30.0, "pkt", rx={"MoveActorDelta": 999}),
        ev(60.0, "pkt", rx={"MoveActorDelta": 1200, "MoveActorAbsolute": 0, "PlayerLocation": 600}),
        ev(90.0, "pkt", rx={"MoveActorDelta": 1200, "UpdateBlock": 30, "BlockEvent": 6, "PlayerLocation": 600}),
        ev(120.0, "pkt", rx={"MoveActorDelta": 999}),
    ]
    mix = workload.packet_mix(bots, 30.0, 100.0)
    assert mix.intervals == 2
    assert mix.span_s == pytest.approx(60.0)
    assert mix.counts["MoveActorDelta"] == 2400
    rates = workload.counters(bots, 30.0, 100.0, n=4)
    assert rates["actor_moves"] == pytest.approx(2400 / 4 / 60)
    assert rates["block_updates"] == pytest.approx(36 / 4 / 60)
    assert rates["locator_pkts"] == pytest.approx(1200 / 4 / 60)
    table = workload.mix_table(mix, 4)
    assert table[0] == ["MoveActorDelta", pytest.approx(10.0)]
    assert workload.counters(bots, 35.0, 80.0, n=4)["actor_moves"] is None


def test_heights_airborne_and_bins() -> None:
    def progress(t: float, ys: list[float | None]) -> BotEvent:
        return ev(t, "progress", rows=[[f"a{i}", 1, 1, 0.0, y, 0.0, 0, 0, 1] for i, y in enumerate(ys)])

    bots = [progress(100.0, [301.62, 301.62, 301.62]), progress(150.0, [301.62, 71.0, 64.0, None])]
    ys = workload.heights(bots, 120.0, 200.0)
    assert ys == [301.62, 71.0, 64.0]
    assert workload.airborne_frac(ys, 300.0) == pytest.approx(1 / 3)
    assert workload.airborne_frac([], 300.0) is None
    assert workload.y_table(ys) == [[64, 2], [288, 1]]
    assert workload.heights(bots, 200.0, 300.0) == []


def test_online() -> None:
    bots = [stat(10.0, online=3), stat(20.0, online=4), stat(40.0, online=2)]
    assert workload.online(bots, 5.0, 25.0) == [3, 4]
