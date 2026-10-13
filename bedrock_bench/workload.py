# SPDX-License-Identifier: Apache-2.0
"""The workload a run executes, loaded from a config in configs/ (docs/methodology.md#workload).

The values live in the config files; :func:`bedrock_bench.config.load_workload` reads and validates them. run.json
records the config's name, its base chain, the resolved values and their hash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from bedrock_bench.model import sha256_json, to_json

#: Phases in execution order; a config sets every one of them.
PHASES = ("boot", "idle", "join", "settle", "spread", "steady", "teardown")
#: Phase settings that are timings rather than options.
PHASE_TIMINGS = ("delay_s", "duration_s", "cap_s")
#: The settings of each phase but settle, whose settings are the fields of ``settle.SettleRules``.
PHASE_KEYS: Mapping[str, Mapping[str, type]] = {
    "boot": {"cap_s": float},
    "idle": {"delay_s": float, "duration_s": float},
    "join": {"cap_s": float, "pre_bots_s": float},
    "spread": {"query_timeout_s": float},
    "steady": {"delay_s": float, "duration_s": float},
    "teardown": {"bot_quit_timeout_s": float, "stop_timeout_s": float, "crash_log_lines": int},
}
DIFFICULTY_IDS: Mapping[str, int] = {"peaceful": 0, "easy": 1, "normal": 2, "hard": 3}
GAMEMODE_IDS: Mapping[str, int] = {"survival": 0, "creative": 1, "adventure": 2}


@dataclass(frozen=True)
class BotParams:
    n: int
    prefix: str
    stagger_ms: int
    radius: int
    loadscreen: bool
    walk: str
    spawn_timeout_s: float
    join_retries: int
    identity_seed: str
    progress_interval_s: float
    stat_interval_s: float
    pkt_interval_s: float
    probe_bots: int
    decode_errors: str


@dataclass(frozen=True)
class Grid:
    """Spread grid: ``cell(i) = (origin + (i % columns) * spacing, origin + (i // columns) * spacing)``."""

    columns: int
    spacing: int
    origin: int
    y: float
    warn_offset_blocks: float


@dataclass(frozen=True)
class WorldParams:
    seed: int
    level_name: str


@dataclass(frozen=True)
class ServerParams:
    """Gameplay settings every adapter maps onto its own config."""

    csg: bool
    view_distance: int
    tick_distance: int
    difficulty: str
    gamemode: str
    max_players: int
    autosave: bool
    save_player_data: bool

    @property
    def difficulty_id(self) -> int:
        """``difficulty`` as a number: peaceful 0, easy 1, normal 2, hard 3."""
        return DIFFICULTY_IDS[self.difficulty]

    @property
    def gamemode_id(self) -> int:
        """``gamemode`` as a number: survival 0, creative 1, adventure 2."""
        return GAMEMODE_IDS[self.gamemode]


@dataclass(frozen=True)
class ProbeParams:
    interval_s: float
    max_gap_s: float
    lagging_p99_ms: float


@dataclass(frozen=True)
class RunParams:
    """How ``bbench run`` runs a session."""

    repeats: int
    order: str
    cooldown_s: float
    rerun_invalid: int


@dataclass(frozen=True)
class PhaseSpec:
    """One phase. ``delay_s`` is measured from the previous phase's end mark; a timing a phase lacks is None."""

    name: str
    delay_s: float | None = None
    duration_s: float | None = None
    cap_s: float | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.options[key]

    def to_json(self) -> dict[str, Any]:
        """The phase's table as a config writes it."""
        timings = {key: getattr(self, key) for key in PHASE_TIMINGS if getattr(self, key) is not None}
        return {**timings, **to_json(self.options)}


@dataclass(frozen=True)
class Workload:
    name: str  # the config file name without .toml
    base: tuple[str, ...]  # the configs it inherits from, nearest first
    bots: BotParams
    grid: Grid
    world: WorldParams
    server: ServerParams
    probe: ProbeParams
    phases: tuple[PhaseSpec, ...]
    run: RunParams

    def phase(self, name: str) -> PhaseSpec:
        for spec in self.phases:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def has_phase(self, name: str) -> bool:
        return any(spec.name == name for spec in self.phases)

    @property
    def params(self) -> dict[str, Any]:
        """The resolved values, shaped like a config file without ``base``: ``workload.params`` in run.json."""
        values = {key: to_json(getattr(self, key)) for key in ("bots", "grid", "world", "server", "probe", "run")}
        values["phases"] = {spec.name: spec.to_json() for spec in self.phases}
        return values

    @property
    def sha256(self) -> str:
        return sha256_json(self.params)

    def record(self) -> dict[str, Any]:
        """The ``workload`` block of run.json."""
        return {"name": self.name, "base": list(self.base), "sha256": self.sha256, "params": self.params}


def from_record(run: Mapping[str, Any]) -> Workload:
    """The workload a run used, rebuilt and validated from the ``workload`` block of its run.json."""
    from bedrock_bench.config import ConfigError, parse_workload

    info = run.get("workload")
    if not isinstance(info, Mapping) or not isinstance(info.get("params"), Mapping):
        raise ConfigError("run.json has no workload")
    return parse_workload(info["params"], name=str(info.get("name")), base=tuple(info.get("base") or ()))


__all__ = [
    "PHASES",
    "PHASE_KEYS",
    "PHASE_TIMINGS",
    "BotParams",
    "Grid",
    "PhaseSpec",
    "ProbeParams",
    "RunParams",
    "ServerParams",
    "Workload",
    "WorldParams",
    "from_record",
]
