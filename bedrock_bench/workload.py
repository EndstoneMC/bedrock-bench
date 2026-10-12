# SPDX-License-Identifier: Apache-2.0
"""The workload: the same for every server and every run (docs/methodology.md#workload).

100 bots, one per grid cell, client-side generation off, so every server generates and sends the full view of every
player. ``SMOKE`` is the short workload of ``bbench smoke``. run.json records the parameters and their hash.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bedrock_bench.model import sha256_json, to_json

#: A JSON file of workload values that replace the built-in ones. Only the test suite sets it.
ENV_TEST_WORKLOAD = "BBENCH_TEST_WORKLOAD"

#: Phases in execution order. ``boot``, ``join``, ``settle``, ``spread`` and ``teardown`` always run.
PHASES = ("boot", "idle", "join", "settle", "spread", "steady", "teardown")


@dataclass(frozen=True)
class BotParams:
    n: int = 100
    prefix: str = "a"
    stagger_ms: int = 300
    radius: int = 16  # RequestChunkRadius sent after spawn; servers clamp it to their view distance
    loadscreen: bool = True
    walk: str = "circle"  # circle | none
    spawn_timeout_s: float = 180.0
    join_retries: int = 0
    identity_seed: str = "bedrock-bench"
    progress_interval_s: float = 5.0
    stat_interval_s: float = 10.0
    pkt_interval_s: float = 30.0
    probe_bots: int = 0
    decode_errors: str = "count"  # count | fail


@dataclass(frozen=True)
class Grid:
    """Spread grid: ``cell(i) = (origin + (i % columns) * spacing, origin + (i // columns) * spacing)``."""

    columns: int = 11
    spacing: int = 640
    origin: int = 1280
    y: float = 300.0  # teleport height
    warn_offset_blocks: float = 200.0


@dataclass(frozen=True)
class WorldParams:
    seed: int = 20261010
    level_name: str = "bench"


@dataclass(frozen=True)
class ServerParams:
    """Gameplay settings every adapter maps onto its own config."""

    csg: bool = False  # client-side chunk generation
    view_distance: int = 10
    tick_distance: int = 4  # simulation distance
    difficulty: str = "easy"
    gamemode: str = "survival"
    max_players: int = 200
    autosave_ticks: int = 6000
    save_player_data: bool = True


@dataclass(frozen=True)
class ProbeParams:
    interval_s: float = 5.0
    max_gap_s: float = 15.0  # probe_coverage gate
    lagging_p99_ms: float = 1000.0  # probe_lagging gate


@dataclass(frozen=True)
class PhaseSpec:
    """One phase. ``delay_s`` is measured from the previous phase's end mark."""

    name: str
    delay_s: float = 0.0
    duration_s: float | None = None  # fixed-length phases (idle, steady)
    cap_s: float | None = None  # hard cap for condition-ended phases (boot, join, settle)
    options: Mapping[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.options[key]


SETTLE_OPTIONS: Mapping[str, Any] = {
    "min_s": 300.0,
    "rate_per_bot": 0.1,
    "rate_window_s": 60.0,
    "eval_interval_s": 5.0,
    "max_data_age_s": 5.0,
    "min_view_frac": 0.0,
    "view_complete_frac": 0.95,
    "view_median_frac": 0.5,
    "retp_after_s": 20.0,
    "retp_max": 3,
    "nudge_after_s": 60.0,
    "nudge_min_y": 290.0,
    "nudge_max_uniq": 30,
    "nudge_max": 2,
    "nudge_step": 16.0,
}

TEARDOWN_OPTIONS: Mapping[str, Any] = {"bot_quit_timeout_s": 10.0, "stop_timeout_s": 180.0, "crash_log_lines": 200}


@dataclass(frozen=True)
class Workload:
    name: str
    bots: BotParams = BotParams()
    grid: Grid = Grid()
    world: WorldParams = WorldParams()
    server: ServerParams = ServerParams()
    probe: ProbeParams = ProbeParams()
    phases: tuple[PhaseSpec, ...] = ()

    def phase(self, name: str) -> PhaseSpec | None:
        for spec in self.phases:
            if spec.name == name:
                return spec
        return None

    def has_phase(self, name: str) -> bool:
        return self.phase(name) is not None

    @property
    def params(self) -> dict[str, Any]:
        """Everything but the name, as plain JSON: the ``workload.params`` block of run.json."""
        values = to_json(self)
        del values["name"]
        return values

    @property
    def sha256(self) -> str:
        return sha256_json(self.params)

    def record(self) -> dict[str, Any]:
        """The ``workload`` block of run.json."""
        return {"name": self.name, "sha256": self.sha256, "params": self.params}


WORKLOAD = Workload(
    name="benchmark",
    phases=(
        PhaseSpec("boot", cap_s=600.0),
        PhaseSpec("idle", delay_s=10.0, duration_s=20.0),
        PhaseSpec("join", cap_s=300.0, options={"pre_bots_s": 0.0}),
        PhaseSpec("settle", cap_s=900.0, options=SETTLE_OPTIONS),
        PhaseSpec("spread", options={"query_timeout_s": 10.0}),
        PhaseSpec("steady", delay_s=15.0, duration_s=90.0),
        PhaseSpec("teardown", options=TEARDOWN_OPTIONS),
    ),
)

SMOKE = Workload(
    name="smoke",
    bots=BotParams(n=4, spawn_timeout_s=60.0, pkt_interval_s=10.0),
    server=ServerParams(view_distance=4),
    phases=(
        PhaseSpec("boot", cap_s=600.0),
        PhaseSpec("idle", delay_s=5.0, duration_s=10.0),
        PhaseSpec("join", cap_s=120.0, options={"pre_bots_s": 0.0}),
        PhaseSpec(
            "settle",
            cap_s=60.0,
            options={**SETTLE_OPTIONS, "min_s": 30.0, "rate_window_s": 20.0, "nudge_after_s": 30.0},
        ),
        PhaseSpec("spread", options={"query_timeout_s": 10.0}),
        PhaseSpec("steady", delay_s=5.0, duration_s=20.0),
        PhaseSpec("teardown", options={**TEARDOWN_OPTIONS, "stop_timeout_s": 60.0}),
    ),
)


def _build(cls: type, data: Mapping[str, Any]) -> Any:
    return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in data.items()})


def from_params(name: str, params: Mapping[str, Any]) -> Workload:
    """A workload rebuilt from its ``params`` block."""
    sections = {"bots": BotParams, "grid": Grid, "world": WorldParams, "server": ServerParams, "probe": ProbeParams}
    return Workload(
        name=name,
        **{key: _build(cls, params.get(key) or {}) for key, cls in sections.items()},
        phases=tuple(_build(PhaseSpec, p) for p in params.get("phases") or ()),
    )


def from_record(run: Mapping[str, Any]) -> Workload:
    """The workload a run used, from run.json; ``WORKLOAD`` when the record has none."""
    info = run.get("workload")
    if not info:
        return WORKLOAD
    return from_params(str(info.get("name") or WORKLOAD.name), info.get("params") or {})


def with_overrides(base: Workload, data: Mapping[str, Any]) -> Workload:
    """``base`` with some values replaced: ``{"bots": {"n": 4}, "phases": {"settle": {"min_s": 1.0}}, ...}``."""
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key == "phases":
            values["phases"] = tuple(_phase_overrides(spec, value.get(spec.name) or {}) for spec in base.phases)
        else:
            values[key] = dataclasses.replace(getattr(base, key), **value)
    return dataclasses.replace(base, **values)


def _phase_overrides(spec: PhaseSpec, data: Mapping[str, Any]) -> PhaseSpec:
    timing = {k: v for k, v in data.items() if k in ("delay_s", "duration_s", "cap_s")}
    options = {k: v for k, v in data.items() if k not in timing}
    unknown = sorted(set(options) - set(spec.options))
    if unknown:
        raise ValueError(f"the {spec.name} phase has no {unknown[0]!r}")
    return dataclasses.replace(spec, options={**spec.options, **options}, **timing)


def for_run(base: Workload) -> Workload:
    """``base``, or ``base`` with the test suite's values from ``$BBENCH_TEST_WORKLOAD``."""
    path = os.environ.get(ENV_TEST_WORKLOAD)
    if not path:
        return base
    return with_overrides(base, json.loads(Path(path).read_text(encoding="utf-8")))


def is_builtin(workload: Workload) -> bool:
    return workload.sha256 in (WORKLOAD.sha256, SMOKE.sha256)


__all__ = [
    "ENV_TEST_WORKLOAD",
    "PHASES",
    "SMOKE",
    "WORKLOAD",
    "BotParams",
    "Grid",
    "PhaseSpec",
    "ProbeParams",
    "ServerParams",
    "Workload",
    "WorldParams",
    "for_run",
    "from_params",
    "from_record",
    "is_builtin",
    "with_overrides",
]
