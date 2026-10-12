# SPDX-License-Identifier: Apache-2.0
"""Shared types: the contract between the harness packages.

Everything here is plain data. Changes are additive only: a new field gets a default, nothing is renamed or removed.
Times named ``*_ns`` are harness monotonic nanoseconds (``clock.mono_ns``); ``*_ms`` and ``epoch_ms`` are wall-clock
UTC milliseconds since the Unix epoch. Paths inside these types are local and are scrubbed before anything is written
to a result file.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path, PurePath
from typing import Any

# --------------------------------------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------------------------------------


class Status(StrEnum):
    """Outcome of one run. ``invalid`` is a harness or environment fault; ``server_failed`` is a result."""

    OK = "ok"
    SERVER_FAILED = "server_failed"
    INVALID = "invalid"

    @property
    def exit_code(self) -> ExitCode:
        return _STATUS_EXIT[self]

    @classmethod
    def worst(cls, statuses: Iterable[Status | str]) -> Status:
        """``invalid`` beats ``server_failed`` beats ``ok``. An empty iterable is ``ok``."""
        found = {cls(s) for s in statuses}
        for status in (cls.INVALID, cls.SERVER_FAILED):
            if status in found:
                return status
        return cls.OK


class ExitCode(IntEnum):
    """Process exit codes of ``bbench``. A matrix exits with the worst code of its runs."""

    OK = 0
    USAGE = 2
    ENV_GATE = 3
    SERVER_FAILED = 4
    INVALID = 5
    INTERRUPTED = 130

    @classmethod
    def for_statuses(cls, statuses: Iterable[Status | str]) -> ExitCode:
        """Exit code derived from recorded run statuses, so a server death can never exit 0."""
        return Status.worst(statuses).exit_code


_STATUS_EXIT = {
    Status.OK: ExitCode.OK,
    Status.SERVER_FAILED: ExitCode.SERVER_FAILED,
    Status.INVALID: ExitCode.INVALID,
}


class OverrideCategory(StrEnum):
    """Why a config value differs from the server's default (rule R6)."""

    HARNESS = "harness"  # needed to drive the server at all: offline mode, ports, console, helper plugin
    PARITY = "parity"  # aligns the workload across servers: view distance, seed, heap
    RESCUE = "rescue"  # the default config fails the workload; always footnoted
    NONVANILLA = "nonvanilla"  # changes gameplay to save work; always footnoted

    @property
    def footnoted(self) -> bool:
        return self in (OverrideCategory.RESCUE, OverrideCategory.NONVANILLA)


class Availability(StrEnum):
    """Whether a metric value exists for a run."""

    MEASURED = "measured"
    NOT_APPLICABLE = "not_applicable"  # the server or scenario has no such quantity (tick thread of a Go server)
    UNAVAILABLE = "unavailable"  # should exist but could not be computed; ``reason`` says why


class FeatureClass(StrEnum):
    """Declared parity of one feature against vanilla BDS, from a server file's ``[features]`` table."""

    VANILLA = "vanilla"
    PARTIAL = "partial"
    ALTERED = "altered"  # deliberate deviation to save work
    ABSENT = "absent"
    UNKNOWN = "unknown"  # not allowed in a published table for a key the scenario exercises


class Fidelity(StrEnum):
    """Optimisation layer of a server build: vanilla code paths, equivalent behaviour, or changed gameplay."""

    V0 = "V0"
    V1 = "V1"
    V2 = "V2"


class Gate(StrEnum):
    """Validity gates (docs/methodology.md#statistics-and-run-status).

    ``status`` is the outcome a failed gate forces, or None for a warning.
    """

    HARNESS_OK = "harness_ok"
    PROBE_MONOTONIC = "probe_monotonic"
    PROBE_COVERAGE = "probe_coverage"
    PROBE_LAGGING = "probe_lagging"
    FOREIGN_LOAD = "foreign_load"
    PLACEMENT_MATCH = "placement_match"
    DECODE_ERRORS = "decode_errors"
    BOT_SATURATED = "bot_saturated"
    SPREAD = "spread"
    CSG_MATCHES = "csg_matches"
    VERSION_MATCH = "version_match"
    WORLD_FRESH = "world_fresh"
    CONFIG_READBACK = "config_readback"
    INTERVENTIONS_IN_MEASURED = "interventions_in_measured"
    SERVER_ALIVE = "server_alive"
    JOINED_ALL = "joined_all"
    DROPPED = "dropped"

    @property
    def status(self) -> Status | None:
        if self in (Gate.SERVER_ALIVE, Gate.JOINED_ALL):
            return Status.SERVER_FAILED
        if self is Gate.DROPPED:
            return None
        return Status.INVALID


class Flag(StrEnum):
    """Run flags: facts a reader must see next to the numbers. They never change the status."""

    SETTLE_CAPPED = "settle_capped"
    VIEWS_INCOMPLETE = "views_incomplete"
    PARITY_MISMATCH = "parity_mismatch"
    BOT_PROTOCOL_DIFFERS = "bot_protocol_differs"
    TERRAIN_MISMATCH = "terrain_mismatch"
    DIFFERENT_BDS_BUILD = "different_bds_build"
    KNOWN_ISSUE = "known_issue"
    UNPUBLISHED_ARTIFACT = "unpublished_artifact"


# --------------------------------------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------------------------------------

#: Phases in execution order. ``boot``, ``join``, ``settle``, ``spread`` and ``teardown`` always run.
PHASES = ("boot", "idle", "join", "settle", "spread", "steady", "teardown")

#: Phase marks recorded in run.json, in seconds since the run's time origin.
MARKS = (
    "ready",
    "idle0",
    "idle1",
    "bots_start",
    "alljoined",
    "settled",
    "steady0",
    "steady1",
    "stop",
)

#: Measurement windows metrics are computed over. ``load`` is ``[bots_start, settled]``.
WINDOWS = ("idle", "join", "settle", "load", "steady")

#: Normalised log levels; adapters map their raw levels onto these.
LOG_LEVELS = ("debug", "info", "warn", "error", "fatal")

PRIORITIES = ("normal", "above_normal")
PLATFORMS = ("windows", "linux")
PLACEMENT_KINDS = ("physical", "vcpu")
WORLD_SOURCES = ("fresh", "template")
GENERATORS = ("vanilla", "custom", "flat")
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
GAMEMODES = ("survival", "creative", "adventure")
ARTIFACT_SOURCES = ("mojang", "pypi", "github", "maven", "web", "build", "local")

#: Feature keys of the parity matrix (docs/methodology.md#fairness-and-feature-parity); add new keys in both places.
FEATURE_KEYS = frozenset(
    {
        "terrain.vanilla_generator",
        "terrain.any_generator",
        "terrain.structures",
        "chunks.client_side_generation",
        "chunks.subchunk_requests",
        "entity.mob_spawning",
        "entity.mob_ai",
        "entity.collision_push",
        "entity.item_entities",
        "player.server_authoritative_movement",
        "player.physics",
        "player.hunger_damage",
        "player.locator_bar",
        "block.redstone",
        "block.random_ticks",
        "block.fluids",
        "block.falling",
        "world.lighting",
        "world.dimensions",
        "world.autosave",
        "commands.vanilla_set",
        "net.encryption",
    }
)

# --------------------------------------------------------------------------------------------------------------------
# Matrix and arms
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    """One server build under test. ``bds`` and ``python`` apply to BDS-hosted servers only."""

    id: str
    server: str
    version: str
    variant: str | None = None
    bds: str | None = None
    python: str | None = None
    artifact: Path | None = None  # local artifact override; results are marked ``unpublished_artifact``
    scenarios: tuple[str, ...] = ()  # subset of the matrix scenarios this arm runs; empty = all

    @property
    def ref(self) -> str:
        """``server@version``, the form the CLI takes."""
        return f"{self.server}@{self.version}"


@dataclass(frozen=True)
class Matrix:
    """``configs/matrices/<name>.toml``: which arms run which scenarios, how often and in which order."""

    name: str
    path: Path
    sha256: str
    scenarios: tuple[str, ...]
    arms: tuple[Arm, ...]
    repeats: int = 1
    order: str = "rotate"  # rotate | sequential | seeded:<int>
    cooldown_s: float = 60.0
    rerun_invalid: int = 1  # server_failed is never rerun
    priority_override: str | None = None

    def arm(self, arm_id: str) -> Arm:
        for arm in self.arms:
            if arm.id == arm_id:
                return arm
        raise KeyError(f"matrix {self.name!r} has no arm {arm_id!r}")

    def scenarios_for(self, arm: Arm) -> tuple[str, ...]:
        return arm.scenarios or self.scenarios


# --------------------------------------------------------------------------------------------------------------------
# Scenario
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BotParams:
    """Load bot settings. Every arm of a matrix gets the same values (rule R2)."""

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
    pkt_interval_s: float = 60.0
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
    source: str = "fresh"  # fresh | template
    template_radius: int = 12  # view distance of the pre-generation tour (template scenarios)
    min_coverage: float = 0.99  # per-cell coverage a template must reach


@dataclass(frozen=True)
class ServerParams:
    """Gameplay settings every adapter maps onto its own config (docs/methodology.md#fairness-and-feature-parity)."""

    csg: bool  # client-side chunk generation on servers that support it; bots get -csg when True
    view_distance: int = 10
    tick_distance: int = 4
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
    """One phase. ``delay_s`` is measured from the previous phase's end mark; ``options`` are fully resolved."""

    name: str
    delay_s: float = 0.0
    duration_s: float | None = None  # fixed-length phases (idle, steady)
    cap_s: float | None = None  # hard cap for condition-ended phases (boot, join, settle)
    options: Mapping[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.options[key]


@dataclass(frozen=True)
class Scenario:
    """``configs/scenarios/<name>.toml`` after defaults and ``extends`` are resolved.

    ``sha256`` is the hash of ``resolved`` in canonical JSON, so two files with the same effective content hash
    the same.
    """

    name: str
    sha256: str
    bots: BotParams
    grid: Grid
    world: WorldParams
    server: ServerParams
    phases: tuple[PhaseSpec, ...]
    probe: ProbeParams
    exercises: tuple[str, ...] = ()  # feature keys whose parity rows the report must show
    requires: tuple[str, ...] = ()  # feature keys that must match for efficiency metrics to be comparable
    description: str = ""
    resolved: Mapping[str, Any] = field(default_factory=dict)

    def phase(self, name: str) -> PhaseSpec | None:
        for spec in self.phases:
            if spec.name == name:
                return spec
        return None

    def has_phase(self, name: str) -> bool:
        return self.phase(name) is not None


# --------------------------------------------------------------------------------------------------------------------
# Machine and placement
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gates:
    """Interference limits from the machine profile; a run that breaks one is invalid."""

    idle_check_s: float = 15.0
    foreign_cores_mean: float = 0.25
    foreign_cores_peak_10s: float = 1.5
    foreign_server_set_cores_mean: float = 0.25
    bot_cpu_frac_max: float = 0.85


@dataclass(frozen=True)
class Machine:
    """A machine profile. CPU sets are ``auto``, ``bot`` (harness only) or a list such as ``0-7,16``."""

    server_cpus: str
    bot_cpus: str
    harness_cpus: str
    priority: str
    port: int
    jvm_heap: str
    gates: Gates
    profile_sha256: str
    label: str = ""


@dataclass(frozen=True)
class Placement:
    """CPU placement resolved on the host by ``host.py``."""

    server_cpus: tuple[int, ...]
    bot_cpus: tuple[int, ...]
    harness_cpus: tuple[int, ...]
    priority: str
    kind: str  # physical | vcpu


# --------------------------------------------------------------------------------------------------------------------
# Artifacts, features and claims files
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactSpec:
    """One pinned download from a server file's ``[artifacts]`` table. ``extra`` holds adapter-specific keys."""

    name: str
    url: str
    sha256: str | None  # None until pinned; fetch refuses an unpinned artifact
    size: int | None = None
    executables: tuple[str, ...] = ()  # paths inside the archive that get the exec bit
    eula: str | None = None  # e.g. "minecraft": fetch needs --accept-eula
    source: str = "web"  # one of ARTIFACT_SOURCES, recorded in run.json
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactVersion:
    version: str
    platforms: Mapping[str, ArtifactSpec]  # platform key ("windows", "linux", "any", ...) -> download
    meta: Mapping[str, Any] = field(default_factory=dict)  # non-table keys of the version table


@dataclass(frozen=True)
class ArtifactCatalog:
    server: str
    path: Path
    sha256: str
    versions: Mapping[str, ArtifactVersion]

    def get(self, version: str, platform: str) -> ArtifactSpec:
        try:
            entry = self.versions[version]
        except KeyError:
            raise KeyError(f"{self.server}: no pinned version {version!r}") from None
        for key in (platform, "any"):
            if key in entry.platforms:
                return entry.platforms[key]
        raise KeyError(f"{self.server} {version}: no artifact for platform {platform!r}")


@dataclass(frozen=True)
class FeatureCell:
    key: str
    cls: FeatureClass
    evidence: str | None = None  # link
    observed_by: str | None = None  # metric id that observes the feature
    note: str | None = None


@dataclass(frozen=True)
class FeatureRange:
    prefix: str  # version prefix ("5.44" matches "5.44" and "5.44.3"); "*" matches every version
    fidelity: Fidelity
    cells: Mapping[str, FeatureCell]


@dataclass(frozen=True)
class FeatureFile:
    server: str
    path: Path
    sha256: str
    ranges: tuple[FeatureRange, ...]

    def row(self, version: str) -> FeatureRange | None:
        """The range with the longest prefix that matches ``version``."""
        matches = [
            rng
            for rng in self.ranges
            if rng.prefix == "*" or version == rng.prefix or version.startswith(rng.prefix + ".")
        ]
        return max(matches, key=lambda rng: (rng.prefix != "*", len(rng.prefix)), default=None)


@dataclass(frozen=True)
class Claim:
    """A relational statement checked against a report (``bbench report --claims <file>``)."""

    id: str
    text: str
    expr: str  # evaluated by report/claims.py with a whitelisted AST
    inconclusive_if: str | None = None
    scenario: str | None = None
    machine: str | None = None  # machine label the claim is restricted to
    source: str | None = None  # where the claimed figure was published


@dataclass(frozen=True)
class ClaimSet:
    matrix: str
    path: Path
    sha256: str
    claims: tuple[Claim, ...]


# --------------------------------------------------------------------------------------------------------------------
# Adapter inputs and outputs
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRef:
    """Provenance of one verified artifact behind an install."""

    name: str
    url: str | None
    sha256: str
    size: int | None
    source: str  # one of ARTIFACT_SOURCES


@dataclass(frozen=True)
class Install:
    """A verified, immutable install in the cache. Never modified after ``fetch``."""

    root: Path
    tree_sha256: str
    artifacts: tuple[ArtifactRef, ...]
    extra: Mapping[str, Any] = field(default_factory=dict)  # adapter-private paths (venv, BDS root, ...)


@dataclass(frozen=True)
class Override:
    """One config value that differs from the server's default (rule R6)."""

    category: OverrideCategory
    file: str  # path relative to the server directory
    key: str
    default: str | None
    value: str
    reason: str


@dataclass(frozen=True)
class ConfigFile:
    path: str  # relative to the server directory
    written_sha256: str


@dataclass(frozen=True)
class WorldInfo:
    fresh: bool
    generator: str  # one of GENERATORS
    template_sha256: str | None = None
    path: Path | None = None  # world directory, checked absent before launch when fresh


@dataclass(frozen=True)
class Layout:
    """What ``prepare`` produced: a fresh server copy with rendered configs."""

    server_dir: Path
    overrides: tuple[Override, ...]
    config_files: tuple[ConfigFile, ...]
    world: WorldInfo
    extra: Mapping[str, Any] = field(default_factory=dict)  # adapter-private data ``launch`` needs


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: Path
    env_set: Mapping[str, str] = field(default_factory=dict)
    env_remove: tuple[str, ...] = ()
    stdin_lines: tuple[str, ...] = ()  # written once right after launch, before any other console input


@dataclass(frozen=True)
class RunContext:
    """Everything ``prepare`` needs about the run it prepares for."""

    run_id: str
    run_dir: Path  # results directory; nothing of the server lives here
    work_dir: Path  # short, empty directory for the fresh server copy (Cache.work_dir)
    arm: Arm
    scenario: Scenario
    machine: Machine
    placement: Placement
    repeat: int = 1
    template: Path | None = None  # validated world template for template scenarios


@dataclass(frozen=True)
class ThreadInfo:
    tid: int
    name: str
    start_module: str | None = None
    created_ns: int | None = None  # harness monotonic ns


@dataclass(frozen=True)
class PlayerPos:
    name: str
    x: float
    y: float
    z: float
    dim: int = 0


# --------------------------------------------------------------------------------------------------------------------
# Raw run data (what metrics read)
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeSample:
    send_ns: int
    rx_ns: int
    tick: int


@dataclass(frozen=True)
class CpuSample:
    t_ns: int
    cpu_ns: int
    cycles: int | None = None


@dataclass(frozen=True)
class ThreadSample:
    t_ns: int
    tid: int
    name: str
    cpu_ns: int
    cycles: int | None = None


@dataclass
class ThreadSeries:
    """Per-thread CPU samples in time order. Threads that exited stay in the series."""

    samples: list[ThreadSample] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.samples)

    def by_tid(self) -> dict[int, list[ThreadSample]]:
        out: dict[int, list[ThreadSample]] = {}
        for sample in self.samples:
            out.setdefault(sample.tid, []).append(sample)
        return out


@dataclass(frozen=True)
class ProcSample:
    t_ns: int
    cpu_user_s: float
    cpu_sys_s: float
    resident_mib: float  # Windows working set, Linux VmRSS
    private_mib: float  # Windows PrivateUsage, Linux RssAnon + VmSwap
    threads: int
    handles: int | None = None  # Windows only
    tree_cpu_s: float | None = None  # launcher tree (user + sys) excluding the server process


@dataclass(frozen=True)
class MachineSample:
    t_ns: int
    per_cpu_busy: tuple[float, ...]  # busy fraction per logical CPU
    foreign_cores: float
    foreign_server_set_cores: float


@dataclass(frozen=True)
class LogLine:
    t_ns: int
    epoch_ms: int
    level: str | None  # one of LOG_LEVELS after the adapter's level map
    logger: str | None
    text: str  # prefix stripped
    raw: str


@dataclass
class RunData:
    """A run directory loaded for analysis (results.py builds it, metrics read it)."""

    run: dict[str, Any]
    probe: list[ProbeSample] = field(default_factory=list)
    threads: ThreadSeries = field(default_factory=ThreadSeries)
    tick: list[CpuSample] = field(default_factory=list)
    process: list[ProcSample] = field(default_factory=list)
    machine: list[MachineSample] = field(default_factory=list)
    bots: list[dict[str, Any]] = field(default_factory=list)
    log: list[LogLine] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------------------------------------------------


def to_json(obj: Any) -> Any:
    """Convert dataclasses, enums, paths, patterns and containers into plain JSON values.

    Sets become sorted lists so the output is deterministic. Paths become POSIX strings and are not scrubbed here.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return obj.as_posix()
    if isinstance(obj, re.Pattern):
        return obj.pattern
    if isinstance(obj, Mapping):
        return {str(k): to_json(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted((to_json(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError(f"cannot convert {type(obj).__name__} to JSON")


def canonical_json(obj: Any) -> str:
    """Compact JSON with sorted keys; the input to every content hash."""
    return json.dumps(to_json(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
