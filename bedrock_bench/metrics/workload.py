# SPDX-License-Identifier: Apache-2.0
"""Work counters: what the server actually did for the bots.

Rates are per bot per second over a window. Cumulative counters come from the bots' ``stat`` events (first and last
inside the window); packet counts come from ``pkt`` events, each holding the packets received since the previous one.
Only ``pkt`` intervals that lie wholly inside the window are used, so traffic from the load phase never leaks into
steady numbers.

Movement corrections are a cost the server pays because the bots do not simulate physics; real clients cause far
fewer. Bots that stay near the teleport height had no gravity applied: the server does not simulate player physics.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bedrock_bench.metrics.load import BotEvent, progress

#: Packet names summed into each work counter (gophertunnel names).
PACKET_GROUPS: Mapping[str, tuple[str, ...]] = {
    "actor_moves": ("MoveActorDelta", "MoveActorAbsolute"),
    "block_updates": ("UpdateBlock", "UpdateSubChunkBlocks", "BlockEvent"),
    "locator_pkts": ("PlayerLocation",),
}

#: Cumulative ``stat`` counters reported per bot per second, with their divisor (bytes -> KiB).
STAT_COUNTERS: Mapping[str, tuple[str, float]] = {
    "corrections": ("corrections", 1.0),
    "rx_pkts": ("rx_pkts", 1.0),
    "tx_pkts": ("tx_pkts", 1.0),
    "rx_kib": ("rx_bytes", 1024.0),
}

#: A bot this close below the teleport height or above it was not pulled down by gravity.
AIRBORNE_MARGIN = 10.0
Y_BIN = 32


def stat_rates(bots: Sequence[BotEvent], t0: float, t1: float, n: int) -> dict[str, float | None]:
    """Per bot per second rates of the cumulative ``stat`` counters over the window."""
    stats = [e for e in bots if e.ev == "stat" and t0 <= e.t <= t1]
    out: dict[str, float | None] = dict.fromkeys(STAT_COUNTERS)
    if len(stats) < 2 or n <= 0 or stats[-1].t <= stats[0].t:
        return out
    first, last = stats[0], stats[-1]
    span = last.t - first.t
    for name, (key, divisor) in STAT_COUNTERS.items():
        if key in first.data and key in last.data:
            out[name] = (last.data[key] - first.data[key]) / divisor / n / span
    return out


@dataclass(frozen=True)
class PacketMix:
    counts: Mapping[str, int]  # packets received over the covered intervals, by name
    span_s: float  # total length of the intervals used
    intervals: int


def packet_mix(bots: Sequence[BotEvent], t0: float, t1: float) -> PacketMix:
    """Packets from the ``pkt`` intervals that lie wholly inside the window.

    An interval runs from the previous ``pkt`` event (or the bot process's ``hello``) to this one.
    """
    counts: dict[str, int] = {}
    span = 0.0
    intervals = 0
    start: float | None = None
    for event in bots:
        if event.ev == "hello":
            start = event.t
        elif event.ev == "pkt":
            if start is not None and start >= t0 and event.t <= t1 and event.t > start:
                for name, value in event.data.get("rx", {}).items():
                    counts[name] = counts.get(name, 0) + int(value)
                span += event.t - start
                intervals += 1
            start = event.t
    return PacketMix(counts=dict(sorted(counts.items())), span_s=span, intervals=intervals)


def counters(bots: Sequence[BotEvent], t0: float, t1: float, n: int) -> dict[str, float | None]:
    """Every work counter of the window, per bot per second (None when not measurable)."""
    out = stat_rates(bots, t0, t1, n)
    mix = packet_mix(bots, t0, t1)
    for name, packets in PACKET_GROUPS.items():
        if mix.intervals and n > 0:
            out[name] = sum(mix.counts.get(p, 0) for p in packets) / n / mix.span_s
        else:
            out[name] = None
    return out


def mix_table(mix: PacketMix, n: int) -> list[list[object]]:
    """Rows ``[packet, per_bot_s]``, highest rate first."""
    if not mix.intervals or n <= 0:
        return []
    rates = sorted(((count / n / mix.span_s, name) for name, count in mix.counts.items()), key=lambda r: (-r[0], r[1]))
    return [[name, rate] for rate, name in rates]


def heights(bots: Sequence[BotEvent], t0: float, t1: float) -> list[float]:
    """Bot heights from the last progress snapshot inside the window (bots the server has not placed are skipped)."""
    snapshot = None
    for t, rows in progress(bots):
        if t0 <= t <= t1:
            snapshot = rows
    if snapshot is None:
        return []
    return [row.y for row in snapshot.values() if row.y is not None]


def airborne_frac(ys: Sequence[float], teleport_y: float) -> float | None:
    """Share of bots at least as high as ``teleport_y - 10``."""
    if not ys:
        return None
    return sum(1 for y in ys if y >= teleport_y - AIRBORNE_MARGIN) / len(ys)


def y_table(ys: Sequence[float]) -> list[list[object]]:
    """Rows ``[y_from, bots]`` in 32-block bins."""
    bins: dict[int, int] = {}
    for y in ys:
        key = math.floor(y / Y_BIN) * Y_BIN
        bins[key] = bins.get(key, 0) + 1
    return [[k, v] for k, v in sorted(bins.items())]


def online(bots: Sequence[BotEvent], t0: float, t1: float) -> list[int]:
    """``online`` of each ``stat`` event inside the window."""
    return [int(e.data["online"]) for e in bots if e.ev == "stat" and t0 <= e.t <= t1 and "online" in e.data]
