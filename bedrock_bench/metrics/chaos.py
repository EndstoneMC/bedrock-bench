# SPDX-License-Identifier: Apache-2.0
"""Chaos window metrics. Chaos results are shown, never ranked.

Input is the executor's journal (``chaos-exec.jsonl``), one record per planned action::

    {"action": "tickspeed", "status": "sent", "scheduled_ns": ..., "sent_ns": ..., "applied_ns": ...,
     "applied": {"tickspeed": 100}}

``status`` is ``sent``, ``unsupported`` (the adapter has no renderer; never sent), ``skip_offline`` (the target bot was
offline) or ``overrun`` (not sent because the console writer fell too far behind). ``*_ns`` are harness monotonic
nanoseconds; ``applied_ns`` and ``applied`` are set when the server's log confirmed the action. Unknown fields are
ignored.

The random tick speed regime comes only from applied changes, never from send times: a server can drop or delay
console commands.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bedrock_bench.metrics import tps
from bedrock_bench.metrics.load import BotEvent
from bedrock_bench.metrics.stats import median, pct
from bedrock_bench.model import LogLine, ProbeSample

NS_PER_S = 1e9
NORMAL_REGIMES = frozenset({0, 1, 3})
HIGH_REGIMES = frozenset({100, 1000})
#: The random tick speed a fresh world starts with.
START_REGIME = 1
#: An action is late when it was sent more than this long after its scheduled time.
LATE_S = 5.0
TICKSPEED_ACTION = "tickspeed"

_DIGITS = re.compile(r"\d+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
CATEGORY_LENGTH = 110


@dataclass(frozen=True)
class ActionRecord:
    action: str
    status: str
    scheduled: float | None  # seconds since the run origin
    sent: float | None
    applied: float | None
    detail: Mapping[str, Any] = field(default_factory=dict)  # the "applied" payload

    @property
    def lag(self) -> float | None:
        if self.sent is None or self.scheduled is None:
            return None
        return self.sent - self.scheduled


def records(raw: Iterable[Mapping[str, Any]], origin_mono_ns: int = 0) -> list[ActionRecord]:
    """Executor journal records on the run's timebase."""

    def seconds(value: Any) -> float | None:
        return None if value is None else (int(value) - origin_mono_ns) / NS_PER_S

    out = []
    for rec in raw:
        if "action" not in rec:
            continue
        out.append(
            ActionRecord(
                action=str(rec["action"]),
                status=str(rec.get("status", "sent")),
                scheduled=seconds(rec.get("scheduled_ns")),
                sent=seconds(rec.get("sent_ns")),
                applied=seconds(rec.get("applied_ns")),
                detail=dict(rec.get("applied") or {}),
            )
        )
    return out


def regime_changes(recs: Sequence[ActionRecord]) -> list[tuple[float, int]]:
    """``(applied time, tick speed)`` of every applied tick speed change, in time order."""
    changes = [
        (r.applied, int(r.detail[TICKSPEED_ACTION]))
        for r in recs
        if r.applied is not None and TICKSPEED_ACTION in r.detail
    ]
    return sorted(changes)


@dataclass(frozen=True)
class Regimes:
    per_regime: Mapping[int, tuple[float, ...]]  # regime -> TPS samples labelled with it
    seconds: Mapping[int, float]  # regime -> seconds spent in it inside the window
    mixed: int  # samples a change fell inside; excluded
    applied: int  # changes applied inside the window

    def group(self, regimes: Collection[int]) -> list[float]:
        return [v for k, values in self.per_regime.items() if k in regimes for v in values]


def regimes(
    probe: Sequence[ProbeSample],
    changes: Sequence[tuple[float, int]],
    t0: float,
    t1: float,
    start: int = START_REGIME,
) -> Regimes:
    """Split the window's TPS samples by the random tick speed in effect.

    A sample with a change strictly between its two replies ran under two regimes: it is ``mixed`` and left out.
    Every other sample ran under one regime and is labelled with it. ``start`` is the regime before the first change.
    """

    def regime_at(t: float) -> int:
        current = start
        for when, value in changes:
            if when <= t:
                current = value
            else:
                break
        return current

    per: dict[int, list[float]] = {}
    mixed = 0
    for ta, tb, rate in tps.samples_in(probe, t0, t1):
        if any(ta < when < tb for when, _ in changes):
            mixed += 1
            continue
        per.setdefault(regime_at(ta), []).append(rate)

    seconds: dict[int, float] = {}
    inside = [(when, value) for when, value in changes if t0 < when <= t1]
    bounds = [(t0, regime_at(t0)), *inside]
    for k, (when, value) in enumerate(bounds):
        end = bounds[k + 1][0] if k + 1 < len(bounds) else t1
        seconds[value] = seconds.get(value, 0.0) + (end - when)

    return Regimes(
        per_regime={k: tuple(v) for k, v in sorted(per.items())},
        seconds=dict(sorted(seconds.items())),
        mixed=mixed,
        applied=len(inside),
    )


def regime_table(result: Regimes) -> list[list[object]]:
    """Rows ``[regime, seconds, samples, median_tps]``."""
    keys = sorted(set(result.seconds) | set(result.per_regime))
    return [
        [k, result.seconds.get(k, 0.0), len(result.per_regime.get(k, ())), median(result.per_regime.get(k, ()))]
        for k in keys
    ]


@dataclass(frozen=True)
class ActionStats:
    planned: int
    sent: int
    applied: int
    late: int
    unsupported: int
    skip_offline: int
    overrun: int
    lags: tuple[float, ...]


def _stats(recs: Sequence[ActionRecord]) -> ActionStats:
    lags = tuple(r.lag for r in recs if r.lag is not None)
    return ActionStats(
        planned=len(recs),
        sent=sum(1 for r in recs if r.sent is not None),
        applied=sum(1 for r in recs if r.applied is not None),
        late=sum(1 for lag in lags if lag > LATE_S),
        unsupported=sum(1 for r in recs if r.status == "unsupported"),
        skip_offline=sum(1 for r in recs if r.status == "skip_offline"),
        overrun=sum(1 for r in recs if r.status == "overrun"),
        lags=lags,
    )


def actions(recs: Sequence[ActionRecord]) -> tuple[ActionStats, dict[str, ActionStats]]:
    """Totals and per-action counts."""
    by_action: dict[str, list[ActionRecord]] = {}
    for rec in recs:
        by_action.setdefault(rec.action, []).append(rec)
    return _stats(recs), {name: _stats(by_action[name]) for name in sorted(by_action)}


def action_table(per_action: Mapping[str, ActionStats]) -> list[list[object]]:
    """Rows ``[action, planned, sent, applied, late, unsupported, skip_offline, lag_p50_s, lag_p99_s]``."""
    return [
        [
            name,
            s.planned,
            s.sent,
            s.applied,
            s.late,
            s.unsupported,
            s.skip_offline,
            pct(s.lags, 0.5),
            pct(s.lags, 0.99),
        ]
        for name, s in per_action.items()
    ]


def tickspeed_queued(recs: Sequence[ActionRecord], t0: float, t1: float) -> int:
    """Tick speed commands sent inside the window."""
    return sum(1 for r in recs if r.action == TICKSPEED_ACTION and r.sent is not None and t0 <= r.sent <= t1)


def not_respawned(bots: Sequence[BotEvent], t0: float) -> int:
    """Bots whose last death after ``t0`` was not followed by a respawn, spawn or rejoin."""
    dead: dict[str, bool] = {}
    for event in bots:
        if event.t < t0 or event.bot is None:
            continue
        if event.ev == "died":
            dead[event.bot] = True
        elif event.ev in ("respawned", "spawned", "rejoin"):
            dead[event.bot] = False
    return sum(1 for value in dead.values() if value)


def category(text: str) -> str:
    """Error line with numbers and quoted strings replaced, so repeats group together."""
    key = _QUOTED.sub("'…'", _DIGITS.sub("#", text.strip()))
    return key[:CATEGORY_LENGTH]


def error_table(lines: Iterable[LogLine], limit: int = 30) -> list[list[object]]:
    """Rows ``[category, count]``, most frequent first."""
    counts: dict[str, int] = {}
    for line in lines:
        key = category(line.text)
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[k, v] for k, v in ranked[:limit]]
