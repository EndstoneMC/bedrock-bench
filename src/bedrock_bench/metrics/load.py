# SPDX-License-Identifier: Apache-2.0
"""Join and view-load latency, chunk work (SPEC.md section 7.4).

Bot events are first put on the run's timebase with :func:`timed_events`. A bot process stamps every line with its
own monotonic clock (``mono_ms``) and the harness adds its receive time (``rx_ns``); since both clocks are monotonic,
the smallest ``rx - mono`` difference over a bot process is the tightest bound on their offset. Lines without
``rx_ns`` fall back to the epoch stamp ``t`` mapped through the run's origin pair. Nothing depends on the time of day.

"In view" follows the bot: a chunk counts when it lies within the view radius + 1 of the bot's current chunk, so
chunks the server streamed for an earlier position do not count. ``U_b`` is a bot's in-view unique chunk count at
``settled``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import accumulate
from typing import Any

from bedrock_bench.clock import NS_PER_MS, OffsetMap
from bedrock_bench.metrics.stats import Censored, median

NS_PER_S = 1e9
#: Chunk count for the uniform view-load metric ``view_t_n_s``.
VIEW_N = 80


@dataclass(frozen=True)
class BotEvent:
    t: float  # seconds since the run origin
    ev: str
    data: Mapping[str, Any]

    @property
    def bot(self) -> str | None:
        return self.data.get("bot")


def timed_events(raw: Iterable[Mapping[str, Any]], origin_mono_ns: int = 0, origin_epoch_ms: int = 0) -> list[BotEvent]:
    """Bot events with their time in seconds since the run origin, in time order.

    A ``hello`` line starts a new bot process (with its own monotonic clock). Events with neither ``rx_ns`` nor ``t``
    are dropped.
    """
    segments: list[list[Mapping[str, Any]]] = []
    for event in raw:
        if event.get("ev") == "hello" or not segments:
            segments.append([])
        segments[-1].append(event)

    timed: list[tuple[float, int, BotEvent]] = []
    order = 0
    for segment in segments:
        pairs = [
            (round(e["mono_ms"] * NS_PER_MS), e["rx_ns"] - origin_mono_ns)
            for e in segment
            if e.get("mono_ms") is not None and e.get("rx_ns") is not None
        ]
        fit = OffsetMap.fit(pairs) if pairs else None
        for event in segment:
            if fit is not None and event.get("mono_ms") is not None:
                t = fit(round(event["mono_ms"] * NS_PER_MS)) / NS_PER_S
            elif event.get("t") is not None:
                t = (event["t"] - origin_epoch_ms) / 1000.0
            else:
                continue
            timed.append((t, order, BotEvent(t, str(event.get("ev")), event)))
            order += 1
    timed.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in timed]


def of_kind(bots: Sequence[BotEvent], *kinds: str) -> list[BotEvent]:
    return [e for e in bots if e.ev in kinds]


def first_per_bot(bots: Sequence[BotEvent], kind: str) -> dict[str, BotEvent]:
    out: dict[str, BotEvent] = {}
    for event in bots:
        if event.ev == kind and event.bot is not None:
            out.setdefault(event.bot, event)
    return out


def last_at(bots: Sequence[BotEvent], kind: str, at: float) -> BotEvent | None:
    """The last event of ``kind`` at or before ``at``."""
    found = None
    for event in bots:
        if event.t > at:
            break
        if event.ev == kind:
            found = event
    return found


def first_tp(bots: Sequence[BotEvent]) -> float | None:
    """Time of the first teleport of any bot."""
    return next((e.t for e in bots if e.ev == "tp"), None)


def disc(radius: int) -> int:
    """Chunk columns within ``radius`` of a centre chunk (``dx^2 + dz^2 <= r^2``); ``disc(10) = 317``."""
    span = range(-radius, radius + 1)
    return sum(1 for dx in span for dz in span if dx * dx + dz * dz <= radius * radius)


# --------------------------------------------------------------------------------------------------------------------
# Join
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinStats:
    join_ms: tuple[float, ...]  # bot clock, dial -> spawned, first session of each bot
    joined: int  # bots that spawned at least once
    failed: int  # bots that never spawned and reported join_failed
    rejoin_failed: int  # join_failed after the bot had spawned before


def join(bots: Sequence[BotEvent]) -> JoinStats:
    dials = first_per_bot(bots, "dial")
    spawned: dict[str, BotEvent] = {}
    failed: set[str] = set()
    rejoin_failed = 0
    for event in bots:
        name = event.bot
        if name is None:
            continue
        if event.ev == "spawned":
            spawned.setdefault(name, event)
        elif event.ev == "join_failed":
            if name in spawned:
                rejoin_failed += 1
            else:
                failed.add(name)
    times = []
    for name, event in spawned.items():
        if event.data.get("join_ms") is not None:
            times.append(float(event.data["join_ms"]))
        elif name in dials and event.data.get("mono_ms") is not None and dials[name].data.get("mono_ms") is not None:
            times.append(float(event.data["mono_ms"]) - float(dials[name].data["mono_ms"]))
    return JoinStats(
        join_ms=tuple(times),
        joined=len(spawned),
        failed=len(failed - set(spawned)),
        rejoin_failed=rejoin_failed,
    )


def first_chunk_ms(bots: Sequence[BotEvent]) -> list[float]:
    """Per bot, ms from its first teleport to its first in-view LevelChunk."""
    return [float(e.data["dt_ms"]) for e in first_per_bot(bots, "first_chunk").values() if "dt_ms" in e.data]


# --------------------------------------------------------------------------------------------------------------------
# View load
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressRow:
    bot: str
    uniq: int
    in_view: int
    x: float | None
    y: float | None
    z: float | None
    dim: int | None
    corrections: int
    teleports: int


def progress(bots: Sequence[BotEvent]) -> list[tuple[float, dict[str, ProgressRow]]]:
    """Progress snapshots in time order: ``(t, {bot: row})``."""
    out = []
    for event in bots:
        if event.ev != "progress":
            continue
        rows = {}
        for raw in event.data.get("rows", ()):
            row = ProgressRow(*raw[:9])
            rows[row.bot] = row
        out.append((event.t, rows))
    return out


def view_at(bots: Sequence[BotEvent], at: float) -> dict[str, int]:
    """``U_b``: each bot's in-view unique chunks in the latest progress snapshot at or before ``at``."""
    out: dict[str, int] = {}
    for t, rows in progress(bots):
        if t > at:
            break
        for name, row in rows.items():
            out[name] = row.in_view
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class ViewStats:
    u: Mapping[str, int]  # U_b per bot at settled
    t90_own: tuple[float, ...]  # s from first tp to ceil(0.9 U_b) in-view chunks
    t90_fallback: int  # how many t90 values came from 5 s progress snapshots instead of first-seen times
    t_n: tuple[float | Censored, ...]  # s from first tp to the n-th in-view chunk
    n_target: int


def _arrivals(summary: BotEvent | None) -> list[int] | None:
    """Cumulative ms since the bot's first tp at which each in-view chunk arrived."""
    if summary is None or "first_seen_ms" not in summary.data:
        return None
    return list(accumulate(int(d) for d in summary.data["first_seen_ms"]))


def view(bots: Sequence[BotEvent], settled: float, n_target: int = VIEW_N) -> ViewStats:
    """View-load latency per bot.

    ``t90_own``: time to ``ceil(0.9 U_b)`` in-view chunks, from the bot's chunk arrival times (``bot_summary``), or
    from the first 5 s progress snapshot that reaches the count when those are missing or too short. Relative to each
    bot's own final view, so a server that sends fewer chunks finishes sooner.

    ``t_n``: time to the ``n_target``-th in-view chunk, the same count for every server. A bot that never got there is
    censored at the last moment it was observed.
    """
    u = view_at(bots, settled)
    tp = first_per_bot(bots, "tp")
    summaries: dict[str, BotEvent] = {}
    for event in bots:
        if event.ev == "bot_summary" and event.bot is not None:
            summaries[event.bot] = event
    snapshots = progress(bots)

    def from_progress(name: str, count: int) -> float | None:
        for t, rows in snapshots:
            row = rows.get(name)
            if row is not None and t >= tp[name].t and row.in_view >= count:
                return t - tp[name].t
        return None

    def last_seen(name: str) -> float:
        if name in summaries:
            return summaries[name].t
        seen = [t for t, rows in snapshots if name in rows]
        return seen[-1] if seen else tp[name].t

    t90: list[float] = []
    fallback = 0
    t_n: list[float | Censored] = []
    for name, u_b in u.items():
        if name not in tp:
            continue
        arrivals = _arrivals(summaries.get(name))
        if u_b > 0:
            k = (9 * u_b + 9) // 10  # ceil(0.9 * U_b) in integers
            if arrivals is not None and len(arrivals) >= k:
                t90.append(arrivals[k - 1] / 1000.0)
            else:
                hit = from_progress(name, k)
                if hit is not None:
                    t90.append(hit)
                    fallback += 1
        if arrivals is not None and len(arrivals) >= n_target:
            t_n.append(arrivals[n_target - 1] / 1000.0)
        elif arrivals is None and (hit := from_progress(name, n_target)) is not None:
            t_n.append(hit)
        else:
            t_n.append(Censored(max(0.0, last_seen(name) - tp[name].t)))
    return ViewStats(u=u, t90_own=tuple(t90), t90_fallback=fallback, t_n=tuple(t_n), n_target=n_target)


def delivery(
    bots: Sequence[BotEvent], t_from: float, settled: float, fractions: Sequence[float] = (0.5, 0.9)
) -> dict[float, float | None]:
    """Seconds from ``t_from`` until the summed in-view chunks of all bots reach each fraction of their value at
    ``settled`` (5 s progress snapshots)."""
    totals = [(t, sum(row.in_view for row in rows.values())) for t, rows in progress(bots) if t <= settled]
    if not totals or totals[-1][1] <= 0:
        return {q: None for q in fractions}
    final = totals[-1][1]
    out: dict[float, float | None] = {}
    for q in fractions:
        hit = next((t for t, total in totals if total >= q * final - 1e-9), None)
        out[q] = hit - t_from if hit is not None else None
    return out


def resend_ratio(bots: Sequence[BotEvent], at: float) -> float | None:
    """LevelChunk packets received per unique chunk, from the last ``stat`` at or before ``at``."""
    stat = last_at(bots, "stat", at)
    if stat is None or not stat.data.get("uniq"):
        return None
    return stat.data["level_chunks"] / stat.data["uniq"]


def granted_radius(bots: Sequence[BotEvent]) -> float | None:
    """Median chunk radius the server granted at spawn."""
    return median(float(e.data["radius"]) for e in first_per_bot(bots, "spawned").values() if "radius" in e.data)


def csg_effective(u: Mapping[str, int], radius: int) -> bool | None:
    """True when the median bot holds fewer than half the chunks a full view of ``radius`` contains."""
    med = median(u.values())
    if med is None:
        return None
    return med < 0.5 * disc(radius)
