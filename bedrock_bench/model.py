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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bedrock_bench.workload import Workload

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
    """Process exit codes of ``bbench``. A session exits with the worst code of its runs."""

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
    NOT_APPLICABLE = "not_applicable"  # the server or workload has no such quantity (tick thread of a Go server)
    UNAVAILABLE = "unavailable"  # should exist but could not be computed; ``reason`` says why


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
    BOT_PROTOCOL_DIFFERS = "bot_protocol_differs"
    DIFFERENT_BDS_BUILD = "different_bds_build"
    KNOWN_ISSUE = "known_issue"
    UNPUBLISHED_ARTIFACT = "unpublished_artifact"


# --------------------------------------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------------------------------------

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
GENERATORS = ("vanilla", "custom", "flat")
ARTIFACT_SOURCES = ("mojang", "pypi", "github", "maven", "web", "build", "local")

# --------------------------------------------------------------------------------------------------------------------
# Servers under test
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    """One server build under test. ``bds`` and ``python`` apply to BDS-hosted servers only."""

    id: str  # the tag, ``<server>@<version>``
    server: str
    version: str
    bds: str | None = None
    python: str | None = None
    artifact: Path | None = None  # local artifact override; results are marked ``unpublished_artifact``

    @property
    def ref(self) -> str:
        """``server@version``, the form the CLI takes."""
        return f"{self.server}@{self.version}"


@dataclass(frozen=True)
class Benchmark:
    """The ``[benchmark]`` table of a server file: the build ``bbench run`` measures when no tag is given."""

    server: str
    version: str
    bds: str | None = None
    python: str | None = None
    include: bool = True
    reason: str | None = None  # why the server is left out; set exactly when include is false

    @property
    def arm(self) -> Arm:
        return Arm(f"{self.server}@{self.version}", self.server, self.version, bds=self.bds, python=self.python)


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
# Artifacts
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
class KeyChange:
    key: str  # property name, or dotted path for YAML, TOML and JSON
    before: Any  # None when the key or the file was absent
    after: Any


@dataclass(frozen=True)
class ConfigFile:
    """A config file written into the server directory, rendered from a template in ``servers/<server>/``."""

    path: str  # relative to the server directory
    written_sha256: str
    template: str | None = None  # file name in the server's folder
    created: bool = False  # the file did not exist before
    keys: tuple[KeyChange, ...] = ()


@dataclass(frozen=True)
class WorldInfo:
    fresh: bool
    generator: str  # one of GENERATORS
    path: Path | None = None  # world directory, checked absent before launch when fresh


@dataclass(frozen=True)
class Layout:
    """What ``prepare`` produced: a fresh server copy with rendered configs."""

    server_dir: Path
    config_files: tuple[ConfigFile, ...]
    world: WorldInfo
    extra: Mapping[str, Any] = field(default_factory=dict)  # adapter-private data ``launch`` needs
    overrides: tuple[Override, ...] = ()


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
    workload: Workload
    machine: Machine
    placement: Placement
    repeat: int = 1


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
