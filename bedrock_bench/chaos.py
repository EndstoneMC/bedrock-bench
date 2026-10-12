# SPDX-License-Identifier: Apache-2.0
"""Seeded chaos: a plan made before the run and an executor that never skips for backlog (SPEC.md section 6.3).

The catalogue (``configs/scenarios/chaos-actions.toml``) declares two vocabularies:

- **commands**: the console commands a server's chaos dialect renders (:meth:`ServerAdapter.chaos_dialect`), with
  their parameter names and an example each;
- **actions**: what chaos does, built from those commands here: how often each action fires, which commands it
  requires and its parameters (how many bots, how long until it is undone, ...).

Adapters only render commands, so every server gets the same actions. An action whose required commands an adapter
cannot render is recorded as ``unsupported`` and never sent.

Plan. :func:`build_plan` turns the scenario's ``[phases.chaos]`` settings and the catalogue into timed steps that
address bots by index. Nothing measured goes in, so every arm of a matrix gets the same plan, and the same seed always
gives the same plan hash. Each action draws from its own random streams (one for its firing times, one for its
contents), so adding an action to a scenario or retuning one leaves every other action's steps unchanged. A step that
undoes another (teleport home, game mode back) follows it in the plan and only addresses bots the first one reached.

Executor. :func:`run_phase` saves the plan as ``chaos-plan.jsonl`` and sends every step at its time through the paced
console writer (bot-side steps go to the bot's control input). Each step gets one record in ``chaos-exec.jsonl`` with
its scheduled, sent and applied times and one of these statuses:

==================  ==================================================================================================
``sent``            dispatched; ``sent_ns`` is when the first command was written (None if none was)
``unsupported``     the adapter cannot render a required command
``skip_offline``    every bot the step addresses was offline
``overrun``         not sent: the console writer had fallen more than ``overrun_lag_s`` behind. From then on no new
                    step is sent; the run is flagged ``chaos_overrun``
==================  ==================================================================================================

Applied times come from the server's own log (:meth:`ServerAdapter.applied`), never from send times. The executor
also keeps the bots usable: a bot that respawns gets its effects and is teleported home, and a bot that rejoins while
still moved by an action (game mode, dimension) is moved back. Those restores are journaled as ``restore`` records,
which carry no ``action`` and so are not counted as actions. Bot radius jitter is switched on for the phase only.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import math
import random
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from bedrock_bench import config
from bedrock_bench.clock import NS_PER_S, mono_ns
from bedrock_bench.config import ConfigError
from bedrock_bench.grid import cell
from bedrock_bench.metrics.chaos import TICKSPEED_ACTION
from bedrock_bench.model import Grid, PhaseSpec, Scenario, canonical_json, sha256_json
from bedrock_bench.results import CHAOS_EXEC_FILE, CHAOS_PLAN_FILE

if TYPE_CHECKING:
    from bedrock_bench.adapters.base import ChaosDialect, ServerAdapter
    from bedrock_bench.model import LogLine

log = logging.getLogger(__name__)

#: Format id on the first line of ``chaos-plan.jsonl``.
PLAN_FORMAT = "bedrock-bench/chaos-plan/1"
#: Default catalogue (``configs/scenarios/chaos-actions.toml``).
CATALOGUE = "chaos-actions"
CATALOGUE_SCHEMA = 1

#: First firing of an action at U(FIRST_FIRE_S, min(interval, FIRST_FIRE_MAX_S)); later ones every interval × U(JITTER).
FIRST_FIRE_S = 3.0
FIRST_FIRE_MAX_S = 60.0
JITTER = (0.6, 1.4)

#: How often the executor looks at new bot events and log lines when no step is due sooner.
POLL_S = 0.2
#: Pause between a bot respawning (or rejoining and appearing in the world) and the commands that restore it.
RESTORE_DELAY_S = 2.0
#: How long after a confirmable step was written its applied line may still arrive.
CONFIRM_TIMEOUT_S = 30.0
#: How long the phase waits at its end for commands still queued in the console writer.
FINISH_GRACE_S = 10.0

#: Displacements a step can cause and a later step (or a restore) undoes.
GAMEMODE = "gamemode"
DIMENSION = "dimension"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MISSING: Any = object()


class ChaosError(RuntimeError):
    """The chaos phase cannot run as planned: a harness or adapter bug, not a server outcome."""


def bot_name(prefix: str, index: int) -> str:
    """The load bot's name for bot ``index``: the prefix and the index with at least two digits (``a07``)."""
    return f"{prefix}{index:02d}"


# --------------------------------------------------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """A console command chaos may send. Renderers take exactly ``params`` as keyword arguments."""

    name: str
    description: str
    params: tuple[str, ...]
    example: Mapping[str, Any]

    @property
    def takes_player(self) -> bool:
        return "player" in self.params


@dataclass(frozen=True)
class Edit:
    """One block edit of the ``blocks`` action: a command and its parameters other than ``player``."""

    command: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class ActionSpec:
    """A chaos action from the catalogue. ``params`` are the action's own settings, already validated."""

    name: str
    description: str
    interval_s: float
    requires: tuple[str, ...]
    optional: tuple[str, ...] = ()
    confirm: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def commands(self) -> tuple[str, ...]:
        return self.requires + self.optional


@dataclass(frozen=True)
class Catalogue:
    name: str
    path: Path | None
    sha256: str
    far_cells: int
    commands: Mapping[str, CommandSpec]
    actions: Mapping[str, ActionSpec]

    def missing(self, action: str, dialect: ChaosDialect) -> list[str]:
        """Required commands of ``action`` that ``dialect`` cannot render; empty when the action is supported."""
        return [name for name in self.actions[action].requires if not dialect.supports(name)]


def load_catalogue(ref: str | Path = CATALOGUE) -> Catalogue:
    """Load ``configs/scenarios/<ref>.toml`` (or a path)."""
    path = config.resolve("scenarios", ref)
    return parse_catalogue(config.load_toml(path), name=path.stem, file=path)


def parse_catalogue(data: Mapping[str, Any], *, name: str, file: Path | None = None) -> Catalogue:
    """Validate catalogue data. Unknown keys, unknown actions and dangling command names are errors."""
    top = _Reader(dict(data), file)
    schema = top.get("schema", _count)
    if schema != CATALOGUE_SCHEMA:
        raise top.error("schema", f"only schema = {CATALOGUE_SCHEMA} is supported")
    far_cells = top.get("far_cells", _positive_int)

    commands: dict[str, CommandSpec] = {}
    command_table = top.table("command")
    for command, raw in command_table.items():
        reader = command_table.sub(command, raw)
        if not _NAME_RE.match(command):
            raise reader.error(None, f"command names must match {_NAME_RE.pattern}")
        params = reader.get("params", _identifiers)
        example = reader.get("example", _table)
        if set(example) != set(params):
            raise reader.error("example", f"must give exactly the parameters {list(params)}")
        commands[command] = CommandSpec(command, reader.get("description", _text), params, example)
        reader.finish()

    actions: dict[str, ActionSpec] = {}
    action_table = top.table("action")
    for action, raw in action_table.items():
        reader = action_table.sub(action, raw)
        actions[action] = _read_action(action, reader, commands)
    if not actions:
        raise top.error("action", "the catalogue defines no action")
    top.finish()
    sha = config.file_sha256(file) if file is not None else sha256_json(data)
    return Catalogue(name, file, sha, far_cells, commands, actions)


def _read_action(name: str, reader: _Reader, commands: Mapping[str, CommandSpec]) -> ActionSpec:
    definition = _ACTIONS.get(name)
    if definition is None:
        raise reader.error(None, f"no such chaos action (this harness implements {', '.join(_ACTIONS)})")

    def command_names(key: str, default: Any = _MISSING) -> tuple[str, ...]:
        names = reader.get(key, _identifiers, default)
        unknown = [n for n in names if n not in commands]
        if unknown:
            raise reader.error(key, f"names commands the catalogue does not declare: {unknown}")
        return names

    requires = command_names("requires")
    optional = command_names("optional", ())
    if set(requires) & set(optional):
        raise reader.error("optional", "repeats a required command")
    confirm = reader.get("confirm", _text, None)
    if confirm is not None and confirm not in requires:
        raise reader.error("confirm", "must be one of the required commands")
    params: dict[str, Any] = {}
    for key, kind in definition.params.items():
        params[key] = reader.get(key, kind)
    if "edits" in params:
        params["edits"] = _check_edits(reader, params["edits"], commands, (*requires, *optional))
    spec = ActionSpec(
        name=name,
        description=reader.get("description", _text),
        interval_s=float(reader.get("interval_s", _positive)),
        requires=requires,
        optional=optional,
        confirm=confirm,
        params=params,
    )
    reader.finish()
    return spec


def _check_edits(
    reader: _Reader, edits: Sequence[Mapping[str, Any]], commands: Mapping[str, CommandSpec], allowed: Sequence[str]
) -> tuple[Edit, ...]:
    out = []
    for index, raw in enumerate(edits):
        values = dict(raw)
        command = values.pop("command", None)
        if command not in allowed:
            raise reader.error(f"edits[{index}].command", f"must be one of the action's commands {list(allowed)}")
        expected = [p for p in commands[command].params if p != "player"]
        if set(values) != set(expected):
            raise reader.error(f"edits[{index}]", f"must give exactly the {command} parameters {expected}")
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise reader.error(f"edits[{index}].{key}", "must be a number or a string")
        out.append(Edit(command, values))
    return tuple(out)


class _Bad(ValueError):
    pass


class _Reader:
    """Typed reads from one TOML table; ``finish`` rejects every key that was not read."""

    def __init__(self, data: Any, file: Path | None, where: str = "") -> None:
        if not isinstance(data, dict):
            raise ConfigError("must be a table", file=file, key=where or None)
        self.data: dict[str, Any] = data
        self.file = file
        self.where = where
        self._read: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self.where}.{name}" if self.where else name

    def error(self, name: str | None, message: str) -> ConfigError:
        return ConfigError(message, file=self.file, key=self.key(name) if name else (self.where or None))

    def get(self, name: str, convert: Callable[[Any], Any], default: Any = _MISSING) -> Any:
        self._read.add(name)
        if name not in self.data:
            if default is _MISSING:
                raise self.error(name, "is required")
            return default
        try:
            return convert(self.data[name])
        except _Bad as exc:
            raise self.error(name, str(exc)) from None

    def table(self, name: str) -> _Reader:
        self._read.add(name)
        return _Reader(self.data.get(name, {}), self.file, self.key(name))

    def items(self) -> list[tuple[str, Any]]:
        self._read.update(self.data)
        return list(self.data.items())

    def sub(self, name: str, value: Any) -> _Reader:
        return _Reader(value, self.file, self.key(name))

    def finish(self) -> None:
        for name in self.data:
            if name not in self._read:
                allowed = ", ".join(sorted(self._read)) or "none"
                raise self.error(name, f"unknown key (allowed here: {allowed})")


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Bad(f"expected a non-negative integer, got {value!r}")
    return value


def _positive_int(value: Any) -> int:
    if _count(value) < 1:
        raise _Bad(f"expected a positive integer, got {value!r}")
    return value


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _Bad(f"expected a number, got {value!r}")
    return value


def _positive(value: Any) -> int | float:
    if _number(value) <= 0:
        raise _Bad(f"must be > 0, got {value!r}")
    return value


def _probability(value: Any) -> float:
    if not 0 <= _number(value) <= 1:
        raise _Bad(f"must be between 0 and 1, got {value!r}")
    return float(value)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Bad(f"expected a non-empty string, got {value!r}")
    return value


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _Bad("expected a non-empty array of strings")
    return tuple(_text(item) for item in value)


def _identifiers(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _Bad("expected an array of names")
    names = tuple(_text(item) for item in value)
    bad = [n for n in names if not _NAME_RE.match(n)]
    if bad:
        raise _Bad(f"names must match {_NAME_RE.pattern}: {bad}")
    if len(set(names)) != len(names):
        raise _Bad("repeats a name")
    return names


def _table(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Bad("expected a table")
    return dict(value)


def _int_range(value: Any) -> tuple[int, int]:
    """``n`` or ``[low, high]`` of non-negative integers."""
    if isinstance(value, list):
        if len(value) != 2:
            raise _Bad("expected [low, high]")
        low, high = (_count(v) for v in value)
    else:
        low = high = _count(value)
    if low > high:
        raise _Bad(f"low is above high in {value!r}")
    return low, high


def _seconds(value: Any) -> tuple[float, float]:
    """``s`` or ``[low, high]`` of positive seconds."""
    if isinstance(value, list):
        if len(value) != 2:
            raise _Bad("expected [low, high]")
        low, high = (float(_positive(v)) for v in value)
    else:
        low = high = float(_positive(value))
    if low > high:
        raise _Bad(f"low is above high in {value!r}")
    return low, high


def _edit_list(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        raise _Bad("expected a non-empty array of tables")
    return tuple(value)


# --------------------------------------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StepCommand:
    """One console command of a step. ``bot`` is the bot index rendered as ``player``; None for global commands."""

    command: str
    params: Mapping[str, Any]
    bot: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"command": self.command, "params": dict(self.params), "bot": self.bot}


@dataclass(frozen=True)
class BotOp:
    """One control operation for the load bot (``bots/README.md``), e.g. ``reconnect``."""

    op: str
    bot: int
    delay_ms: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"op": self.op, "bot": self.bot, "delay_ms": self.delay_ms}


@dataclass(frozen=True)
class Step:
    """One planned step, ``t_s`` seconds after the chaos phase started.

    ``parent`` is the step this one follows up on; it addresses only bots the parent reached. ``confirm`` names the
    command whose applied line confirms the step, with the value ``expect``. ``displaces`` and ``restores`` mark steps
    that move bots away from the scenario's normal state and back.
    """

    id: int
    t_s: float
    action: str
    step: str
    commands: tuple[StepCommand, ...] = ()
    ops: tuple[BotOp, ...] = ()
    parent: int | None = None
    confirm: str | None = None
    expect: Any = None
    displaces: str | None = None
    restores: str | None = None

    @property
    def targets(self) -> tuple[int, ...]:
        bots = {c.bot for c in self.commands if c.bot is not None} | {o.bot for o in self.ops}
        return tuple(sorted(bots))

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "t_s": self.t_s,
            "action": self.action,
            "step": self.step,
            "commands": [c.to_json() for c in self.commands],
            "ops": [o.to_json() for o in self.ops],
            "parent": self.parent,
            "confirm": self.confirm,
            "expect": self.expect,
            "displaces": self.displaces,
            "restores": self.restores,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Step:
        return cls(
            id=int(data["id"]),
            t_s=float(data["t_s"]),
            action=str(data["action"]),
            step=str(data["step"]),
            commands=tuple(StepCommand(c["command"], dict(c["params"]), c["bot"]) for c in data["commands"]),
            ops=tuple(BotOp(o["op"], int(o["bot"]), o["delay_ms"]) for o in data["ops"]),
            parent=data["parent"],
            confirm=data["confirm"],
            expect=data["expect"],
            displaces=data["displaces"],
            restores=data["restores"],
        )


def plan_sha256(steps: Sequence[Step]) -> str:
    """The plan hash: sha256 of the steps in canonical JSON."""
    return sha256_json([step.to_json() for step in steps])


@dataclass(frozen=True)
class Plan:
    """The steps of one chaos phase and the inputs they came from (``header``)."""

    steps: tuple[Step, ...]
    header: Mapping[str, Any]

    @property
    def sha256(self) -> str:
        return str(self.header["sha256"])

    @property
    def duration_s(self) -> float:
        return float(self.header["duration_s"])

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(self.header["actions"])

    def lines(self) -> list[str]:
        return [canonical_json(self.header), *(canonical_json(step.to_json()) for step in self.steps)]

    def write(self, path: Path) -> None:
        Path(path).write_text("".join(line + "\n" for line in self.lines()), encoding="utf-8", newline="\n")

    @classmethod
    def read(cls, path: Path) -> Plan:
        """Load ``chaos-plan.jsonl``; raises ValueError when the steps do not match the recorded hash."""
        lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"{path}: empty chaos plan")
        header = json.loads(lines[0])
        if header.get("plan") != PLAN_FORMAT:
            raise ValueError(f"{path}: not a chaos plan ({PLAN_FORMAT})")
        steps = tuple(Step.from_json(json.loads(line)) for line in lines[1:])
        if plan_sha256(steps) != header.get("sha256"):
            raise ValueError(f"{path}: the steps do not match the plan hash")
        return cls(steps, header)


@dataclass(eq=False)
class _Draft:
    """A step before ids are assigned. ``follows`` links a follow-up to the draft it undoes or completes."""

    t_s: float
    step: str
    commands: list[StepCommand] = field(default_factory=list)
    ops: list[BotOp] = field(default_factory=list)
    follows: _Draft | None = None
    confirm: str | None = None
    expect: Any = None
    displaces: str | None = None
    restores: str | None = None
    action: str = ""
    order: tuple[int, ...] = ()


class _Rng:
    """Draws built only on ``random.Random.random``, the one output Python keeps stable across versions.

    Each stream is seeded from a hash of the run seed and its labels, so streams are independent and portable.
    """

    def __init__(self, seed: int, *labels: str) -> None:
        text = "/".join(("bedrock-bench", "chaos", "1", str(seed), *labels))
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        self.random = random.Random(int.from_bytes(digest[:8], "big")).random

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.random()

    def randint(self, low: int, high: int) -> int:
        """An integer in ``[low, high]``."""
        return low + min(int(self.random() * (high - low + 1)), high - low)

    def chance(self, p: float) -> bool:
        return self.random() < p

    def choice(self, items: Sequence[Any]) -> Any:
        return items[self.randint(0, len(items) - 1)]

    def sample(self, items: Sequence[Any], k: int) -> list[Any]:
        pool = list(items)
        return [pool.pop(self.randint(0, len(pool) - 1)) for _ in range(min(k, len(pool)))]


def undo_command(kind: str, bot: int, grid: Grid, gamemode: str) -> StepCommand:
    """The command that moves ``bot`` back from a displacement: its game mode, or its home cell in the overworld."""
    if kind == GAMEMODE:
        return StepCommand("gamemode", {"mode": gamemode}, bot)
    if kind == DIMENSION:
        x, z = cell(bot, grid)
        return StepCommand("tp_dimension", {"dimension": "overworld", "x": x, "y": grid.y, "z": z}, bot)
    raise ValueError(f"unknown displacement {kind!r}")


class _Planner:
    """What the action planners share: the scenario's bots and grid, the window, and per-action state."""

    def __init__(self, scenario: Scenario, catalogue: Catalogue, duration_s: float, cycle: tuple[int, ...]) -> None:
        self.n = scenario.bots.n
        self.grid = scenario.grid
        self.gamemode = scenario.server.gamemode
        self.duration = duration_s
        self.far_cells = catalogue.far_cells
        self.cycle = cycle
        self._state: dict[str, dict[Any, Any]] = {}

    def state(self, action: str) -> dict[Any, Any]:
        return self._state.setdefault(action, {})

    def count(self, rng: _Rng, params: Mapping[str, Any]) -> int:
        if "bots" in params:
            return rng.randint(*params["bots"])
        pct = rng.randint(*params["bots_pct"])
        return max(params["min_bots"], math.floor(pct * self.n / 100 + 0.5))

    def pick(self, rng: _Rng, action: ActionSpec, pool: Sequence[int] | None = None) -> list[int]:
        candidates = list(range(self.n)) if pool is None else list(pool)
        return sorted(rng.sample(candidates, self.count(rng, action.params)))

    def home(self, bot: int) -> tuple[int, int]:
        return cell(bot, self.grid)

    def far(self, rng: _Rng) -> tuple[int, int]:
        """A grid cell past the bots' cells: terrain no bot has loaded."""
        return cell(self.n + rng.randint(0, self.far_cells - 1), self.grid)

    def tp_home(self, bot: int) -> StepCommand:
        x, z = self.home(bot)
        return StepCommand("tp", {"x": x, "y": self.grid.y, "z": z}, bot)

    def undo(self, kind: str, bot: int) -> StepCommand:
        return undo_command(kind, bot, self.grid, self.gamemode)

    def later(self, rng: _Rng, t: float, span: tuple[float, float]) -> float:
        """A follow-up time ``span`` after ``t``; follow-ups past the window end happen at its end."""
        return min(t + rng.uniform(*span), self.duration)


Planned = list[_Draft]
PlanFn = Callable[[_Planner, ActionSpec, _Rng, int, float], Planned]


def _plan_churn(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    ops = [BotOp("reconnect", bot, rng.randint(*a.params["delay_ms"])) for bot in p.pick(rng, a)]
    return [_Draft(t, "reconnect", ops=ops)]


def _plan_stack(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    bots = p.pick(rng, a)
    x, z = p.far(rng)
    spot = {"x": x + 0.5, "y": p.grid.y, "z": z + 0.5}
    go = _Draft(t, "stack", [StepCommand("tp", spot, bot) for bot in bots])
    home = _Draft(p.later(rng, t, a.params["hold_s"]), "home", [p.tp_home(bot) for bot in bots], follows=go)
    return [go, home]


def _plan_waypoints(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    mode, restore = a.params["mode"], a.params["restore"]
    off = _Draft(t, "set", [StepCommand("playerwaypoints", {"mode": mode})], confirm=a.confirm, expect=mode)
    back_t = p.later(rng, t, a.params["hold_s"])
    command = StepCommand("playerwaypoints", {"mode": restore})
    on = _Draft(back_t, "restore", [command], follows=off, confirm=a.confirm, expect=restore)
    return [off, on]


def _plan_gamemode(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    bots = p.pick(rng, a)
    mode = rng.choice(a.params["modes"])
    go = _Draft(t, "set", [StepCommand("gamemode", {"mode": mode}, bot) for bot in bots], displaces=GAMEMODE)
    back_t = p.later(rng, t, a.params["hold_s"])
    back = _Draft(back_t, "restore", [p.undo(GAMEMODE, bot) for bot in bots], follows=go, restores=GAMEMODE)
    return [go, back]


def _plan_kill(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    commands = []
    for bot in p.pick(rng, a):
        commands += [StepCommand("effect_clear", {}, bot), StepCommand("kill", {}, bot)]
    return [_Draft(t, "kill", commands)]


def _plan_summon(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    params = a.params
    bots = p.pick(rng, a)
    if not bots:
        return []
    spread = params["spread"]
    commands = []
    for m in range(rng.randint(*params["mobs"])):
        entity = rng.choice(params["entities"])
        dx, dz = rng.randint(-spread, spread), rng.randint(-spread, spread)
        where = {"entity": entity, "dx": dx, "dy": params["dy"], "dz": dz}
        commands.append(StepCommand("summon", where, bots[m % len(bots)]))
    for bot in bots:
        for _ in range(rng.randint(*params["loot"])):
            commands.append(StepCommand("loot", {"table": rng.choice(params["loot_tables"])}, bot))
    go = _Draft(t, "summon", commands)
    drafts = [go]
    for bot in bots:
        if rng.chance(params["reconnect_p"]):
            when = p.later(rng, t, params["reconnect_after_s"])
            op = BotOp("reconnect", bot, rng.randint(*params["delay_ms"]))
            drafts.append(_Draft(when, "reconnect", ops=[op], follows=go))
    return drafts


def _plan_cleanup(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    return [_Draft(t, "cleanup", [StepCommand("cleanup", {})])]


def _plan_kick(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    reason = a.params["reason"]
    return [_Draft(t, "kick", [StepCommand("kick", {"reason": reason}, bot) for bot in p.pick(rng, a)])]


def _plan_dimension(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    params = a.params
    away = p.state(a.name)  # bot -> planned return time
    bots = p.pick(rng, a, [bot for bot in range(p.n) if away.get(bot, -1.0) <= t])
    if not bots:
        return []
    commands = []
    for bot in bots:
        x, z = p.home(bot)
        if rng.chance(params["nether_p"]):
            where = {"dimension": "nether", "x": x // 8, "y": params["nether_y"], "z": z // 8}
        else:
            r = params["end_range"]
            where = {"dimension": "the_end", "x": rng.randint(-r, r), "y": params["end_y"], "z": rng.randint(-r, r)}
        commands.append(StepCommand("tp_dimension", where, bot))
    back_t = p.later(rng, t, params["hold_s"])
    away.update(dict.fromkeys(bots, back_t))
    go = _Draft(t, "leave", commands, displaces=DIMENSION)
    back = _Draft(back_t, "return", [p.undo(DIMENSION, bot) for bot in bots], follows=go, restores=DIMENSION)
    return [go, back]


def _plan_list(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    return [_Draft(t, "list", [StepCommand("list", {})])]


def _plan_far_tp(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    commands = []
    for bot in p.pick(rng, a):
        x, z = p.far(rng)
        commands.append(StepCommand("tp", {"x": x, "y": p.grid.y, "z": z}, bot))
    return [_Draft(t, "teleport", commands)]


def _plan_view_disconnect(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    params = a.params
    bots = p.pick(rng, a)
    commands = []
    for bot in bots:
        x, z = p.far(rng)
        commands.append(StepCommand("tp", {"x": x, "y": p.grid.y, "z": z}, bot))
    go = _Draft(t, "teleport", commands)
    when = p.later(rng, t, params["reconnect_after_s"])
    ops = [BotOp("reconnect", bot, rng.randint(*params["delay_ms"])) for bot in bots]
    return [go, _Draft(when, "reconnect", ops=ops, follows=go)]


def _plan_tickspeed(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    value = p.cycle[k % len(p.cycle)]
    return [_Draft(t, "set", [StepCommand("randomtickspeed", {"value": value})], confirm=a.confirm, expect=value)]


def _plan_weather(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    cycle = a.params["cycle"]
    return [_Draft(t, "set", [StepCommand("weather", {"kind": cycle[k % len(cycle)]})])]


def _plan_blocks(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    commands = []
    for bot in p.pick(rng, a):
        for edit in rng.sample(a.params["edits"], a.params["edits_per_bot"]):
            commands.append(StepCommand(edit.command, edit.params, bot))
    return [_Draft(t, "edit", commands)]


def _plan_tickingarea(p: _Planner, a: ActionSpec, rng: _Rng, k: int, t: float) -> Planned:
    params = a.params
    name = f"{params['name_prefix']}{k}"
    low, high = params["distance"]
    x = rng.choice((-1, 1)) * rng.randint(low, high)
    z = rng.choice((-1, 1)) * rng.randint(low, high)
    area = {"name": name, "x": x, "z": z, "y": params["y"], "radius": params["radius"]}
    add = _Draft(t, "add", [StepCommand("tickingarea_add", area)])
    when = p.later(rng, t, params["hold_s"])
    return [add, _Draft(when, "remove", [StepCommand("tickingarea_remove", {"name": name})], follows=add)]


@dataclass(frozen=True)
class _ActionDef:
    plan: PlanFn
    params: Mapping[str, Callable[[Any], Any]]


_PICK_PCT = {"bots_pct": _int_range, "min_bots": _count}
_ACTIONS: Mapping[str, _ActionDef] = {
    "churn": _ActionDef(_plan_churn, {**_PICK_PCT, "delay_ms": _int_range}),
    "stack": _ActionDef(_plan_stack, {**_PICK_PCT, "hold_s": _seconds}),
    "waypoints": _ActionDef(_plan_waypoints, {"mode": _text, "restore": _text, "hold_s": _seconds}),
    "gamemode": _ActionDef(_plan_gamemode, {"bots": _int_range, "modes": _texts, "hold_s": _seconds}),
    "kill": _ActionDef(_plan_kill, dict(_PICK_PCT)),
    "summon": _ActionDef(
        _plan_summon,
        {
            "bots": _int_range,
            "mobs": _int_range,
            "entities": _texts,
            "spread": _count,
            "dy": _number,
            "loot": _int_range,
            "loot_tables": _texts,
            "reconnect_p": _probability,
            "reconnect_after_s": _seconds,
            "delay_ms": _int_range,
        },
    ),
    "cleanup": _ActionDef(_plan_cleanup, {}),
    "kick": _ActionDef(_plan_kick, {"bots": _int_range, "reason": _text}),
    "dimension": _ActionDef(
        _plan_dimension,
        {
            "bots": _int_range,
            "nether_p": _probability,
            "nether_y": _number,
            "end_range": _count,
            "end_y": _number,
            "hold_s": _seconds,
        },
    ),
    "list": _ActionDef(_plan_list, {}),
    "far_tp": _ActionDef(_plan_far_tp, {"bots": _int_range}),
    "view_disconnect": _ActionDef(
        _plan_view_disconnect, {"bots": _int_range, "reconnect_after_s": _seconds, "delay_ms": _int_range}
    ),
    TICKSPEED_ACTION: _ActionDef(_plan_tickspeed, {}),
    "weather": _ActionDef(_plan_weather, {"cycle": _texts}),
    "blocks": _ActionDef(_plan_blocks, {"bots": _int_range, "edits_per_bot": _positive_int, "edits": _edit_list}),
    "tickingarea": _ActionDef(
        _plan_tickingarea,
        {"distance": _int_range, "y": _number, "radius": _positive_int, "hold_s": _seconds, "name_prefix": _text},
    ),
}

#: Actions this harness implements; a catalogue may tune them but not add new ones.
ACTIONS: tuple[str, ...] = tuple(_ACTIONS)


def build_plan(scenario: Scenario, catalogue: Catalogue | None = None) -> Plan:
    """The chaos plan of a scenario. Raises ConfigError when the scenario's chaos settings do not fit the catalogue."""
    spec = scenario.phase("chaos")
    if spec is None:
        raise ConfigError(f"scenario {scenario.name} has no chaos phase")
    catalogue = catalogue if catalogue is not None else load_catalogue(spec["catalogue"])
    actions = tuple(spec["actions"])
    cycle = tuple(int(v) for v in spec["tickspeed_cycle"])
    _check_chaos_settings(actions, cycle, catalogue)
    seed = int(spec["seed"])
    duration = float(spec.duration_s or 0.0)
    planner = _Planner(scenario, catalogue, duration, cycle)

    drafts: list[_Draft] = []
    low, high = JITTER
    for order, name in enumerate(actions):
        action = catalogue.actions[name]
        times, draws = _Rng(seed, name, "time"), _Rng(seed, name, "draw")
        t = times.uniform(FIRST_FIRE_S, min(action.interval_s, FIRST_FIRE_MAX_S))
        firing = 0
        while t < duration:
            kept: list[_Draft] = []
            for draft in _ACTIONS[name].plan(planner, action, draws, firing, t):
                if (draft.commands or draft.ops) and (draft.follows is None or draft.follows in kept):
                    kept.append(draft)  # a firing that picked no bot plans nothing
            for index, draft in enumerate(kept):
                draft.action, draft.order = name, (order, firing, index)
            drafts += kept
            firing += 1
            t += action.interval_s * times.uniform(low, high)

    drafts.sort(key=lambda d: (round(d.t_s, 3), d.order))
    ids = {id(draft): number for number, draft in enumerate(drafts)}
    steps = tuple(
        Step(
            id=number,
            t_s=round(draft.t_s, 3),
            action=draft.action,
            step=draft.step,
            commands=tuple(draft.commands),
            ops=tuple(draft.ops),
            parent=ids[id(draft.follows)] if draft.follows is not None else None,
            confirm=draft.confirm,
            expect=draft.expect,
            displaces=draft.displaces,
            restores=draft.restores,
        )
        for number, draft in enumerate(drafts)
    )
    header = {
        "plan": PLAN_FORMAT,
        "sha256": plan_sha256(steps),
        "steps": len(steps),
        "seed": seed,
        "scenario": scenario.name,
        "scenario_sha256": scenario.sha256,
        "n": scenario.bots.n,
        "duration_s": duration,
        "actions": list(actions),
        "tickspeed_cycle": list(cycle),
        "catalogue": catalogue.name,
        "catalogue_sha256": catalogue.sha256,
    }
    return Plan(steps, header)


def _check_chaos_settings(actions: tuple[str, ...], cycle: tuple[int, ...], catalogue: Catalogue) -> None:
    key = "phases.chaos.actions"
    if not actions:
        raise ConfigError("must name at least one chaos action", key=key)
    if len(set(actions)) != len(actions):
        raise ConfigError("names an action twice", key=key)
    unknown = [a for a in actions if a not in catalogue.actions]
    if unknown:
        known = ", ".join(catalogue.actions)
        raise ConfigError(f"unknown chaos actions {unknown} (catalogue {catalogue.name}: {known})", key=key)
    if TICKSPEED_ACTION in actions and not cycle:
        raise ConfigError(f"the {TICKSPEED_ACTION} action needs a non-empty cycle", key="phases.chaos.tickspeed_cycle")
    if cycle and TICKSPEED_ACTION not in actions:
        raise ConfigError(f"is set, but {TICKSPEED_ACTION} is not in {key}", key="phases.chaos.tickspeed_cycle")


def check_scenario(scenario: Scenario) -> None:
    """Raise ConfigError when a chaos scenario cannot be planned; a scenario without chaos passes. For preflight."""
    if scenario.has_phase("chaos"):
        build_plan(scenario)


# --------------------------------------------------------------------------------------------------------------------
# Adapter checks
# --------------------------------------------------------------------------------------------------------------------


def render(dialect: ChaosDialect, command: StepCommand, names: Sequence[str]) -> list[str]:
    """Console text of one step command, with the bot index replaced by its name."""
    params = dict(command.params)
    if command.bot is not None:
        params["player"] = names[command.bot]
    return dialect.render(command.command, **params)


def check_dialect(dialect: ChaosDialect, catalogue: Catalogue | None = None) -> list[str]:
    """Problems of an adapter's chaos dialect against the catalogue; empty when it is well formed.

    Every catalogue command must be rendered or marked unsupported with a reason, and a renderer must turn the
    catalogue's example into non-empty single-line commands.
    """
    catalogue = catalogue if catalogue is not None else load_catalogue()
    problems = []
    for name, spec in catalogue.commands.items():
        if not dialect.supports(name):
            reason = dialect.unsupported.get(name)
            if reason is None:
                problems.append(f"{name}: neither rendered nor marked unsupported")
            elif not reason.strip():
                problems.append(f"{name}: marked unsupported without a reason")
            continue
        try:
            texts = dialect.render(name, **spec.example)
        except Exception as exc:  # any failure is a contract problem worth reporting
            problems.append(f"{name}: the renderer rejects the catalogue example: {type(exc).__name__}: {exc}")
            continue
        if not texts:
            problems.append(f"{name}: rendered no command")
        for text in texts:
            if not isinstance(text, str) or not text.strip() or text != text.strip() or "\n" in text or "\r" in text:
                problems.append(f"{name}: rendered {text!r}, which is not a single trimmed line")
    return problems


def supported_actions(dialect: ChaosDialect, catalogue: Catalogue | None = None) -> dict[str, list[str]]:
    """Every catalogue action and the required commands the dialect cannot render (empty list = supported)."""
    catalogue = catalogue if catalogue is not None else load_catalogue()
    return {name: catalogue.missing(name, dialect) for name in catalogue.actions}


def smoke_commands(
    adapter: ServerAdapter, *, scenario: Scenario | None = None, catalogue: Catalogue | None = None
) -> list[str]:
    """Every catalogue command the adapter renders, once, in catalogue order, aimed at the smoke scenario's first bot.

    ``bbench smoke`` sends these after the measured windows; none of them may be answered with an error.
    """
    catalogue = catalogue if catalogue is not None else load_catalogue()
    scenario = scenario if scenario is not None else config.load_scenario("smoke")
    player = bot_name(scenario.bots.prefix, 0)
    dialect = adapter.chaos_dialect()
    texts: list[str] = []
    for name, spec in catalogue.commands.items():
        if dialect.supports(name):
            params = dict(spec.example)
            if spec.takes_player:
                params["player"] = player
            texts += dialect.render(name, **params)
    return texts


# --------------------------------------------------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------------------------------------------------


class ChaosSession(Protocol):
    """What the chaos phase uses from :class:`bedrock_bench.phases.Session`.

    ``sleep_until`` raises when the server dies; the phase lets that propagate after closing its journal.
    """

    adapter: ServerAdapter
    scenario: Scenario
    console: Any  # bedrock_bench.console.Console
    fleet: Any  # bedrock_bench.bots.BotFleet
    run_dir: Any  # bedrock_bench.results.RunDir

    def now(self) -> float: ...

    def sleep_until(self, t: float) -> None: ...


def run_phase(session: ChaosSession, spec: PhaseSpec) -> None:
    """The chaos phase: plan, save the plan, execute it for the phase's duration (called by ``phases.Session``)."""
    catalogue = load_catalogue(spec["catalogue"])
    plan = build_plan(session.scenario, catalogue)
    plan.write(session.run_dir.file(CHAOS_PLAN_FILE))
    log.info("chaos plan %s: %d steps over %.0f s", plan.sha256[:12], len(plan.steps), plan.duration_s)
    executor = Executor(
        session,
        plan,
        catalogue,
        journal_path=session.run_dir.file(CHAOS_EXEC_FILE),
        overrun_lag_s=float(spec["overrun_lag_s"]),
    )
    executor.run()


@dataclass(eq=False)
class _Sending:
    """A dispatched step or restore; its record is written once its commands are done (and confirmed, if asked)."""

    record: dict[str, Any]
    commands: list[Any]  # bedrock_bench.console.Command
    op_ns: list[int] = field(default_factory=list)
    op_failed: int = 0
    confirm: str | None = None
    expect: Any = None
    confirm_commands: list[Any] = field(default_factory=list)
    applied_ns: int | None = None
    applied: dict[str, Any] | None = None
    done_at: float | None = None

    def done(self) -> bool:
        return all(command.done for command in self.commands)

    def confirm_sent_ns(self) -> int | None:
        stamps = [c.send_ns for c in self.confirm_commands if c.send_ns is not None]
        return min(stamps) if stamps else None

    def final(self) -> dict[str, Any]:
        record = dict(self.record)
        stamps = [c.send_ns for c in self.commands if c.done and c.sent and c.send_ns is not None] + self.op_ns
        record["sent_ns"] = min(stamps) if stamps else None
        record["failed"] = sum(1 for c in self.commands if c.done and not c.sent) + self.op_failed
        record["queued"] = sum(1 for c in self.commands if not c.done)
        if "applied_ns" in record:
            record["applied_ns"] = self.applied_ns
            record["applied"] = self.applied
        return record


class _Journal:
    """``chaos-exec.jsonl``: one JSON object per line, flushed after each record."""

    def __init__(self, path: Path | None) -> None:
        self._fh = open(path, "w", encoding="utf-8", newline="\n") if path is not None else None

    def write(self, record: dict[str, Any]) -> None:
        if self._fh is not None:
            self._fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class Executor:
    """Runs one plan against a live session. Use :meth:`run`; everything else is internal state.

    ``clock`` gives harness monotonic nanoseconds (the console and the bot events use the same clock).
    """

    def __init__(
        self,
        session: ChaosSession,
        plan: Plan,
        catalogue: Catalogue,
        *,
        journal_path: Path | None = None,
        overrun_lag_s: float = 60.0,
        clock: Callable[[], int] = mono_ns,
    ) -> None:
        self.session = session
        self.plan = plan
        self.catalogue = catalogue
        self.overrun_lag_s = overrun_lag_s
        self.clock = clock
        self.adapter = session.adapter
        self.dialect = session.adapter.chaos_dialect()
        scenario = session.scenario
        self.grid = scenario.grid
        self.gamemode = scenario.server.gamemode
        self.jitter = scenario.bots.radius_jitter_s > 0
        self.names = [bot_name(scenario.bots.prefix, i) for i in range(scenario.bots.n)]
        self.index = {name: i for i, name in enumerate(self.names)}
        self.missing = {action: catalogue.missing(action, self.dialect) for action in plan.actions}
        self._texts: dict[int, list[tuple[StepCommand, list[str]]]] = {}
        self._prerender()
        self.journal = _Journal(journal_path)

        self.online: set[int] = set()
        self.displaced: dict[int, set[str]] = {}
        self.reached: dict[int, frozenset[int]] = {}
        self.statuses: Counter[str] = Counter()
        self.restores = 0
        self.overrun_ns: int | None = None
        self._sending: list[_Sending] = []
        self._due_restores: list[tuple[float, int, int, str]] = []
        self._restore_seq = 0
        self._spawned: set[int] = set()  # rejoined bots waiting for their in-world line
        self._in_world: set[int] = set()  # bots the server reported in the world, waiting for their spawn event
        self._next = 0
        self._event_cursor = 0
        self._line_cursor = 0
        self.start = 0.0
        self.base_ns = 0

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    def run(self) -> None:
        """Execute the plan for its duration. Lets the session's server-death exception through, journal closed."""
        try:
            self._begin()
            self._loop()
            self._drain()
        finally:
            self._end()

    def _begin(self) -> None:
        console, fleet = self.session.console, self.session.fleet
        self.start = self.session.now()
        self.base_ns = self.clock()
        history = fleet.events()
        for event in history:
            self._bot_event(event, replay=True)
        self._event_cursor = len(history)
        self._line_cursor = console.mark()
        if self.jitter:
            fleet.control("jitter", on=True)
        unsupported = {action: missing for action, missing in self.missing.items() if missing}
        for action, missing in unsupported.items():
            log.info("chaos action %s is unsupported by %s (missing %s)", action, self.adapter.name, ", ".join(missing))
        self.journal.write(
            {
                "kind": "start",
                "plan_sha256": self.plan.sha256,
                "t0_ns": self.base_ns,
                "adapter": self.adapter.name,
                "jitter": self.jitter,
                "online": len(self.online),
                "unsupported": unsupported,
            }
        )

    def _prerender(self) -> None:
        """Render every step the adapter supports before anything is sent, so a renderer bug stops the phase early."""
        for step in self.plan.steps:
            if self.missing[step.action]:
                continue
            rendered = []
            for command in step.commands:
                if not self.dialect.supports(command.command):
                    continue  # an optional command the adapter does not have
                try:
                    rendered.append((command, render(self.dialect, command, self.names)))
                except Exception as exc:
                    raise ChaosError(
                        f"{self.adapter.name} cannot render step {step.id} ({step.action}/{step.step}) "
                        f"command {command.command} {dict(command.params)}: {type(exc).__name__}: {exc}"
                    ) from exc
            self._texts[step.id] = rendered

    def _loop(self) -> None:
        steps = self.plan.steps
        end = self.start + self.plan.duration_s
        while True:
            self._poll()
            now = self.session.now()
            self._dispatch_restores(now)
            while self._next < len(steps) and self.start + steps[self._next].t_s <= now:
                self._dispatch(steps[self._next], now)
                self._next += 1
            if now >= end:
                return
            wake = min(end, now + POLL_S)
            if self._next < len(steps):
                wake = min(wake, self.start + steps[self._next].t_s)
            if self._due_restores:
                wake = min(wake, self._due_restores[0][0])
            self.session.sleep_until(wake)

    def _drain(self) -> None:
        """Give commands still queued in the console writer a short grace to be written before records close."""
        deadline = self.session.now() + FINISH_GRACE_S
        while any(not s.done() for s in self._sending) and self.session.now() < deadline:
            self.session.sleep_until(min(deadline, self.session.now() + POLL_S))
            self._poll()

    def _end(self) -> None:
        """Close out the phase; never raises, so a server death propagates unchanged."""
        if self.jitter:
            try:
                self.session.fleet.control("jitter", on=False)
            except Exception:
                log.exception("cannot switch bot radius jitter off")
        try:
            self._poll(finalize=False)
        except Exception:
            log.exception("cannot read the last bot events and log lines")
        for sending in list(self._sending):
            self._write(sending)
        self.journal.write(
            {
                "kind": "end",
                "t_ns": self.clock(),
                "steps": len(self.plan.steps),
                "dispatched": self._next,
                "statuses": dict(sorted(self.statuses.items())),
                "restores": self.restores,
                "overrun_ns": self.overrun_ns,
            }
        )
        self.journal.close()
        log.info(
            "chaos: %d of %d steps dispatched (%s), %d restores%s",
            self._next,
            len(self.plan.steps),
            ", ".join(f"{k} {v}" for k, v in sorted(self.statuses.items())) or "none",
            self.restores,
            ", overrun" if self.overrun_ns is not None else "",
        )

    # ---- input --------------------------------------------------------------------------------------------------

    def _poll(self, *, finalize: bool = True) -> None:
        """Process new bot events and log lines in time order, then write the records that are complete."""
        events = self.session.fleet.events(since=self._event_cursor)
        self._event_cursor += len(events)
        lines = self.session.console.lines(self._line_cursor)
        self._line_cursor += len(lines)
        merged: list[tuple[int, int, int, Any]] = [(int(e.get("rx_ns", 0)), 0, i, e) for i, e in enumerate(events)]
        merged += [(line.t_ns, 1, i, line) for i, line in enumerate(lines)]
        for _, kind, _, item in sorted(merged, key=lambda entry: entry[:3]):
            if kind == 0:
                self._bot_event(item)
            else:
                self._line(item)
        if finalize:
            self._finalize()

    def _bot_index(self, event: Mapping[str, Any]) -> int | None:
        index = event.get("i")
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(self.names):
            return index
        name = event.get("bot")
        return self.index.get(name) if isinstance(name, str) else None

    def _bot_event(self, event: Mapping[str, Any], *, replay: bool = False) -> None:
        kind = event.get("ev")
        bot = self._bot_index(event)
        if bot is None:
            return
        if kind in ("dial", "rejoin"):
            self._spawned.discard(bot)
            self._in_world.discard(bot)
        elif kind == "spawned":
            self.online.add(bot)
            if not replay:
                self._spawned.add(bot)
                self._check_rejoined(bot)
        elif kind in ("left", "kicked", "dropped", "join_failed"):
            self.online.discard(bot)
        elif kind == "respawned" and not replay:
            self.displaced.get(bot, set()).discard(DIMENSION)  # a respawn lands in the overworld
            self._schedule_restore(bot, "respawned")

    def _line(self, line: LogLine) -> None:
        self._confirm(line)
        name = self.adapter.dialect.player_entered(line.text)
        bot = self.index.get(name) if name is not None else None
        if bot is not None:
            self._in_world.add(bot)
            self._check_rejoined(bot)

    def _check_rejoined(self, bot: int) -> None:
        """A bot back in the world after rejoining: once both signals are in, restore it if an action moved it."""
        if bot in self._spawned and bot in self._in_world:
            self._spawned.discard(bot)
            self._in_world.discard(bot)
            if self.displaced.get(bot):
                self._schedule_restore(bot, "rejoined")

    def _confirm(self, line: LogLine) -> None:
        for sending in self._sending:
            if sending.confirm is None or sending.applied_ns is not None:
                continue
            sent_ns = sending.confirm_sent_ns()
            if sent_ns is None or line.t_ns < sent_ns:
                continue
            payload = self.adapter.applied(sending.confirm, line)
            if not payload:
                continue
            value = _confirmed_value(payload, sending.confirm)
            if value is None or (sending.expect is not None and not _same_value(value, sending.expect)):
                continue
            sending.applied_ns = line.t_ns
            sending.applied = {sending.record["action"]: value}
            return

    # ---- output -------------------------------------------------------------------------------------------------

    def _ns(self, t_s: float) -> int:
        return self.base_ns + round(t_s * NS_PER_S)

    def _overrun(self, now: float) -> bool:
        """True once the console writer fell too far behind; from then on nothing new is sent."""
        if self.overrun_ns is None and self.session.console.writer_lag_s() > self.overrun_lag_s:
            self.overrun_ns = self.clock()
            log.warning(
                "chaos overrun at %.0f s: the console writer is more than %.0f s behind", now, self.overrun_lag_s
            )
        return self.overrun_ns is not None

    def _dispatch(self, step: Step, now: float) -> None:
        record: dict[str, Any] = {
            "kind": "step",
            "id": step.id,
            "action": step.action,
            "step": step.step,
            "t_s": step.t_s,
            "status": "sent",
            "scheduled_ns": self._ns(step.t_s),
            "sent_ns": None,
            "applied_ns": None,
            "applied": None,
        }
        if self._overrun(now):
            self._close(record, "overrun")
            return
        missing = self.missing[step.action]
        if missing:
            self._close(record, "unsupported", missing=missing)
            return

        targets = list(step.targets)
        if step.parent is not None:
            reached = self.reached.get(step.parent, frozenset())
            targets = [bot for bot in targets if bot in reached]
        offline = [bot for bot in targets if bot not in self.online]
        live = set(targets) - set(offline)
        record.update(targets=targets, offline=offline)
        commands = [(c, texts) for c, texts in self._texts[step.id] if c.bot is None or c.bot in live]
        ops = [op for op in step.ops if op.bot in live]
        self.reached[step.id] = frozenset(live)
        if not commands and not ops:
            self._close(record, "skip_offline")
            return

        console = self.session.console
        sending = _Sending(record, [], confirm=step.confirm, expect=step.expect)
        texts = []
        for command, rendered in commands:
            for text in rendered:
                handle = console.send(text, origin="chaos")
                sending.commands.append(handle)
                if command.command == step.confirm:
                    sending.confirm_commands.append(handle)
                texts.append(text)
        record["texts"] = texts
        if ops:
            record["ops"] = [self._send_op(op, sending) for op in ops]
        for bot in live:
            if step.displaces is not None:
                self.displaced.setdefault(bot, set()).add(step.displaces)
            if step.restores is not None:
                self.displaced.get(bot, set()).discard(step.restores)
        self.statuses["sent"] += 1
        self._sending.append(sending)

    def _send_op(self, op: BotOp, sending: _Sending) -> dict[str, Any]:
        name = self.names[op.bot]
        params: dict[str, Any] = {"bot": name}
        if op.delay_ms is not None:
            params["delay_ms"] = op.delay_ms
        t_ns = self.clock()
        if self.session.fleet.control(op.op, **params):
            sending.op_ns.append(t_ns)
        else:
            sending.op_failed += 1
        return {"op": op.op, **params}

    def _close(self, record: dict[str, Any], status: str, **extra: Any) -> None:
        record.update(status=status, **extra)
        self.statuses[status] += 1
        self.journal.write(record)

    def _schedule_restore(self, bot: int, cause: str) -> None:
        self._restore_seq += 1
        heapq.heappush(self._due_restores, (self.session.now() + RESTORE_DELAY_S, self._restore_seq, bot, cause))

    def _dispatch_restores(self, now: float) -> None:
        while self._due_restores and self._due_restores[0][0] <= now:
            due, _, bot, cause = heapq.heappop(self._due_restores)
            if bot not in self.online or self._overrun(now):
                continue  # gone again; a rejoin brings it back through the placer
            name = self.names[bot]
            if cause == "respawned":
                x, z = cell(bot, self.grid)
                texts = [*self.adapter.cmd_effects(name), self.adapter.cmd_tp(name, x, self.grid.y, z)]
                undone: list[str] = []
            else:
                undone = sorted(self.displaced.pop(bot, set()))
                undo = [undo_command(kind, bot, self.grid, self.gamemode) for kind in undone]
                texts = [text for command in undo for text in render(self.dialect, command, self.names)]
            if not texts:
                continue
            record = {
                "kind": "restore",
                "bot": name,
                "cause": cause,
                "undo": undone,
                "scheduled_ns": self._ns(due - self.start),
                "texts": texts,
            }
            handles = [self.session.console.send(text, origin="chaos") for text in texts]
            self.restores += 1
            self._sending.append(_Sending(record, handles))

    def _finalize(self) -> None:
        now = self.session.now()
        for sending in list(self._sending):
            if not sending.done():
                continue
            if sending.done_at is None:
                sending.done_at = now
            if (
                sending.confirm is not None
                and sending.applied_ns is None
                and sending.confirm_sent_ns() is not None
                and now < sending.done_at + CONFIRM_TIMEOUT_S
            ):
                continue
            self._write(sending)

    def _write(self, sending: _Sending) -> None:
        self._sending.remove(sending)
        self.journal.write(sending.final())


def _confirmed_value(payload: Mapping[str, Any], command: str) -> Any:
    """The confirmed value in an ``applied`` payload: its ``command`` key, or its only value."""
    if command in payload:
        return payload[command]
    if len(payload) == 1:
        return next(iter(payload.values()))
    return None


def _same_value(a: Any, b: Any) -> bool:
    numbers = (int, float)
    if isinstance(a, numbers) and isinstance(b, numbers) and not isinstance(a, bool) and not isinstance(b, bool):
        return float(a) == float(b)
    return str(a).strip().lower() == str(b).strip().lower()


__all__ = [
    "ACTIONS",
    "CATALOGUE",
    "PLAN_FORMAT",
    "ActionSpec",
    "BotOp",
    "Catalogue",
    "ChaosError",
    "ChaosSession",
    "CommandSpec",
    "Edit",
    "Executor",
    "Plan",
    "Step",
    "StepCommand",
    "bot_name",
    "build_plan",
    "check_dialect",
    "check_scenario",
    "load_catalogue",
    "parse_catalogue",
    "plan_sha256",
    "render",
    "run_phase",
    "smoke_commands",
    "supported_actions",
    "undo_command",
]
