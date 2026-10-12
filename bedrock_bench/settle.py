# SPDX-License-Identifier: Apache-2.0
"""When views have finished loading, and the interventions allowed until then (docs/methodology.md#run-phases).

:class:`Settle` is a pure state machine. The phase loop feeds it what happened, each with its time in seconds on the
run's timebase, and asks for a decision; it never sends anything itself:

- :meth:`Settle.placed`: placement commands (effects and a teleport) were sent for a bot;
- :meth:`Settle.teleported`: the bot reported a teleport (its ``tp`` event);
- :meth:`Settle.left`: the bot went offline;
- :meth:`Settle.progress`: a ``progress`` event with one row per bot;
- :meth:`Settle.evaluate`: a :class:`Decision`, with the interventions to send now.

The settings come from the config's ``[phases.settle]`` table.

**The rule.** ``rate`` is the growth of the bots' total unique chunks per second over the last ``rate_window_s``.
Settle ends once at least ``min_s`` have passed since ``alljoined`` and ``rate / n <= rate_per_bot`` (chunks per bot
per second), or at ``cap_s``, which is flagged ``settle_capped``. A decision is only taken on data at most
``max_data_age_s`` old. With ``min_view_frac`` above 0 the rule also waits until that share of the bots has at least
``view_median_frac`` of the median view.

**Interventions** (settle only; counted per bot and recorded in run.json):

- Re-teleport: a bot whose placement got no ``tp`` event within ``retp_after_s`` gets its placement commands again,
  at most ``retp_max`` times.
- Nudge: from ``nudge_after_s`` on, a bot still hovering above ``nudge_min_y`` with fewer than ``nudge_max_uniq``
  unique chunks is teleported to ``(x + nudge_step * (k % 2), y, z)`` of its cell on its ``k``-th nudge, at most
  ``nudge_max`` times. A bot is never nudged within ``retp_after_s`` of its last placement or nudge, so it has time to
  fall and load before it is judged again.

At ``settled`` the run is flagged ``views_incomplete`` when fewer than ``view_complete_frac`` of the bots have at least
``view_median_frac`` of the median view.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from bedrock_bench.grid import cell
from bedrock_bench.workload import Grid, PhaseSpec

#: Tolerance for comparing times that come from the same clock, against float rounding.
_EPS = 1e-9


@dataclass(frozen=True)
class SettleRules:
    """The settings of the settle phase: exactly the keys of a config's ``[phases.settle]``."""

    min_s: float
    cap_s: float
    rate_per_bot: float
    rate_window_s: float
    eval_interval_s: float
    max_data_age_s: float
    min_view_frac: float
    view_complete_frac: float
    view_median_frac: float
    retp_after_s: float
    retp_max: int
    nudge_after_s: float
    nudge_min_y: float
    nudge_max_uniq: int
    nudge_max: int
    nudge_step: float

    @classmethod
    def from_phase(cls, spec: PhaseSpec) -> SettleRules:
        """Rules from a loaded ``settle`` phase."""
        return cls(cap_s=spec.cap_s, **spec.options)


@dataclass(frozen=True)
class BotView:
    """One row of a ``progress`` event: ``[bot, uniq, in_view, x, y, z, dim, corrections, teleports]``."""

    name: str
    uniq: int
    in_view: int
    x: float | None = None
    y: float | None = None
    z: float | None = None
    dim: int | None = None

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> BotView | None:
        """The view of one row; None when the row is malformed."""
        if len(row) < 3 or not isinstance(row[0], str):
            return None
        try:
            position = [None if v is None else float(v) for v in row[3:6]] if len(row) >= 6 else [None, None, None]
            dim = int(row[6]) if len(row) >= 7 and row[6] is not None else None
            return cls(row[0], int(row[1] or 0), int(row[2] or 0), *position, dim)
        except (TypeError, ValueError):
            return None

    @property
    def placed(self) -> bool:
        return self.y is not None


@dataclass(frozen=True)
class Reteleport:
    """Send the bot's placement commands again (attempt ``attempt`` of ``retp_max``)."""

    bot: str
    index: int
    attempt: int


@dataclass(frozen=True)
class Nudge:
    """Teleport a hovering bot to ``(x, y, z)`` (nudge ``attempt`` of ``nudge_max``)."""

    bot: str
    index: int
    x: float
    y: float
    z: float
    attempt: int


Action = Reteleport | Nudge


@dataclass(frozen=True)
class Decision:
    """The outcome of one evaluation. ``actions`` is empty once ``done``."""

    t: float
    elapsed: float
    rate_per_bot: float | None  # None while the rate cannot be computed from fresh data
    fresh: bool
    done: bool
    capped: bool
    actions: tuple[Action, ...] = ()


@dataclass
class _Bot:
    index: int
    placed_at: float | None = None  # last placement commands (initial or re-teleport)
    last_tp: float | None = None
    last_action: float | None = None  # last placement or nudge
    online: bool = True
    retp: int = 0
    nudges: int = 0

    def awaiting_tp(self) -> bool:
        return self.placed_at is not None and (self.last_tp is None or self.last_tp + _EPS < self.placed_at)


class Settle:
    """The settle state machine for one run. ``start`` is the ``alljoined`` time; ``n`` the workload's bot count."""

    def __init__(self, rules: SettleRules, *, n: int, start: float, grid: Grid) -> None:
        if n < 1:
            raise ValueError("Settle needs at least one bot")
        self.rules = rules
        self.n = n
        self.start = start
        self.grid = grid
        self.settled_at: float | None = None
        self.capped = False
        self.views_incomplete = False
        self._bots: dict[str, _Bot] = {}
        self._totals: list[tuple[float, int]] = []  # (t, sum of uniq) per progress event
        self._latest: tuple[float, tuple[BotView, ...]] | None = None
        self._last_eval: float | None = None

    # ---- inputs -----------------------------------------------------------------------------------------------------

    def placed(self, bot: str, index: int, t: float) -> None:
        state = self._bot(bot, index)
        state.placed_at = t if state.placed_at is None else max(state.placed_at, t)
        state.last_action = t if state.last_action is None else max(state.last_action, t)
        state.online = True

    def teleported(self, bot: str, t: float) -> None:
        state = self._bots.get(bot)
        if state is None:
            return  # a teleport before any placement is not ours to count
        state.last_tp = t if state.last_tp is None else max(state.last_tp, t)

    def left(self, bot: str, t: float) -> None:
        state = self._bots.get(bot)
        if state is not None:
            state.online = False

    def progress(self, t: float, rows: Sequence[Sequence[Any]]) -> None:
        views = tuple(v for v in (BotView.from_row(row) for row in rows) if v is not None)
        if self._latest is not None and t < self._latest[0]:
            return  # out of order; keep the newest snapshot
        self._totals.append((t, sum(v.uniq for v in views)))
        self._latest = (t, views)

    # ---- queries ----------------------------------------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self.settled_at is not None

    def due(self, t: float) -> bool:
        """Whether an evaluation is due at ``t`` (at most one per ``eval_interval_s``)."""
        return self._last_eval is None or t - self._last_eval >= self.rules.eval_interval_s - _EPS

    def latest(self) -> tuple[float, tuple[BotView, ...]] | None:
        return self._latest

    def rate(self, t: float) -> float | None:
        """Growth of the total unique chunks per second over the window ending at the newest snapshot before ``t``.

        The base is the newest snapshot at least ``rate_window_s`` before ``t``, or the oldest one while there is not
        that much history yet.
        """
        totals = [s for s in self._totals if s[0] <= t + _EPS]
        if len(totals) < 2:
            return None
        last = totals[-1]
        older = [s for s in totals[:-1] if s[0] <= t - self.rules.rate_window_s + _EPS]
        base = older[-1] if older else totals[0]
        span = last[0] - base[0]
        if span <= 0:
            return None
        return (last[1] - base[1]) / span

    def fresh(self, t: float) -> bool:
        return self._latest is not None and t - self._latest[0] <= self.rules.max_data_age_s + _EPS

    def view_share(self) -> float | None:
        """Share of bots in the newest snapshot with at least ``view_median_frac`` of the median view."""
        if self._latest is None or not self._latest[1]:
            return None
        views = [v.in_view for v in self._latest[1]]
        threshold = self.rules.view_median_frac * statistics.median(views)
        return sum(1 for v in views if v >= threshold) / len(views)

    # ---- decision ---------------------------------------------------------------------------------------------------

    def evaluate(self, t: float) -> Decision:
        """Decide at ``t``. After the run has settled, every further call returns the same done decision."""
        rules = self.rules
        elapsed = t - self.start
        fresh = self.fresh(t)
        rate = self.rate(t) if fresh else None
        per_bot = None if rate is None else rate / self.n
        self._last_eval = t
        if self.done:
            return Decision(t, elapsed, per_bot, fresh, True, self.capped)

        capped = elapsed >= rules.cap_s - _EPS
        done = capped or (
            fresh
            and elapsed >= rules.min_s - _EPS
            and per_bot is not None
            and per_bot <= rules.rate_per_bot + _EPS
            and self._views_ready()
        )
        if done:
            self.settled_at = t
            self.capped = capped
            share = self.view_share()
            self.views_incomplete = share is None or share < rules.view_complete_frac - _EPS
            return Decision(t, elapsed, per_bot, fresh, True, capped)

        actions: list[Action] = list(self._reteleports(t))
        if fresh and elapsed >= rules.nudge_after_s - _EPS:
            actions.extend(self._nudges(t))
        return Decision(t, elapsed, per_bot, fresh, False, False, tuple(actions))

    def _views_ready(self) -> bool:
        if self.rules.min_view_frac <= 0:
            return True
        share = self.view_share()
        return share is not None and share >= self.rules.min_view_frac - _EPS

    def _reteleports(self, t: float) -> list[Reteleport]:
        out = []
        for name, state in sorted(self._bots.items(), key=lambda item: item[1].index):
            if not state.online or not state.awaiting_tp() or state.retp >= self.rules.retp_max:
                continue
            assert state.placed_at is not None
            if t - state.placed_at >= self.rules.retp_after_s - _EPS:
                state.retp += 1
                state.placed_at = state.last_action = t
                out.append(Reteleport(name, state.index, state.retp))
        return out

    def _nudges(self, t: float) -> list[Nudge]:
        assert self._latest is not None
        rules = self.rules
        out = []
        known = [(self._bots[v.name], v) for v in self._latest[1] if v.name in self._bots]
        for state, view in sorted(known, key=lambda pair: pair[0].index):
            if not state.online or state.nudges >= rules.nudge_max:
                continue
            if view.y is None or view.y <= rules.nudge_min_y or view.uniq >= rules.nudge_max_uniq:
                continue
            if state.last_action is not None and t - state.last_action < rules.retp_after_s - _EPS:
                continue
            state.nudges += 1
            state.last_action = t
            x, z = cell(state.index, self.grid)
            step = rules.nudge_step * (state.nudges % 2)
            out.append(Nudge(view.name, state.index, x + step, self.grid.y, z, state.nudges))
        return out

    # ---- results ----------------------------------------------------------------------------------------------------

    def settle_record(self) -> dict[str, Any]:
        """The ``settle`` block of run.json."""
        return {
            "settle_s": None if self.settled_at is None else round(self.settled_at - self.start, 3),
            "capped": self.capped,
            "views_incomplete": self.views_incomplete,
        }

    def interventions_record(self) -> dict[str, Any]:
        """The ``interventions`` block of run.json. Only settle intervenes, so ``in_measured`` is always 0."""
        return {
            "nudges": sum(s.nudges for s in self._bots.values()),
            "nudged": sorted(name for name, s in self._bots.items() if s.nudges),
            "retp": sum(s.retp for s in self._bots.values()),
            "retp_bots": sorted(name for name, s in self._bots.items() if s.retp),
            "in_measured": 0,
        }

    def _bot(self, name: str, index: int) -> _Bot:
        state = self._bots.get(name)
        if state is None:
            state = self._bots[name] = _Bot(index)
        return state


__all__ = ["Action", "BotView", "Decision", "Nudge", "Reteleport", "Settle", "SettleRules"]
