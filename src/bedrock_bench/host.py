# SPDX-License-Identifier: Apache-2.0
"""The host: CPU topology, placement planning and verification, the idle check, and ``bbench doctor``.

**Placement** (rule R3) splits the logical CPUs the harness may use into a server set and a bot set; the harness
runs on the bot set unless the profile says otherwise. ``auto`` resolves like this:

1. On a hybrid CPU (more than one efficiency class) the server gets every CPU of the fastest class and the bots get
   the rest, provided that leaves the bots at least :data:`MIN_BOT_CPUS` logical CPUs.
2. Otherwise whole physical cores go to the bots, slowest class and highest number first, until the bots have a
   quarter of the cores and at least :data:`MIN_BOT_CPUS` logical CPUs, but never more than half the cores. The
   server gets the rest. SMT siblings always stay together.

An explicit list (``"0-15"``) is used as is; ``auto`` next to an explicit list means "every other CPU". Fewer than
:data:`MIN_BOT_CPUS` bot CPUs is a failed doctor check, because a starved bot would show up as server slowness.

The placement kind is ``vcpu`` when the machine is virtualized (including WSL2), else ``physical``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import platform as _stdlib_platform
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import psutil

from bedrock_bench import __version__, lock, platform
from bedrock_bench.config import load_machine, parse_cpu_list
from bedrock_bench.model import ExitCode, Machine, Placement
from bedrock_bench.platform import CpuRecord

#: Fewest logical CPUs the bots may have before ``bbench doctor`` fails.
MIN_BOT_CPUS = 4

#: Oldest Go toolchain that builds the bots.
MIN_GO = (1, 25)

LEVELS = ("ok", "info", "warn", "fail")


def format_cpus(cpus: Iterable[int]) -> str:
    """``(0, 1, 2, 3, 8)`` -> ``"0-3,8"``."""
    values = sorted(set(cpus))
    if not values:
        return "none"
    ranges: list[tuple[int, int]] = []
    start = prev = values[0]
    for cpu in values[1:]:
        if cpu != prev + 1:
            ranges.append((start, prev))
            start = cpu
        prev = cpu
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


# --------------------------------------------------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Core:
    """One physical core: its logical CPUs and efficiency class."""

    cpus: tuple[int, ...]
    efficiency: int


@dataclass(frozen=True)
class Topology:
    """The logical CPUs available to the harness and how they map to cores and classes."""

    cpus: tuple[CpuRecord, ...]
    model: str = "unknown"
    virtualization: str | None = None
    machine_logical: int | None = None  # every logical CPU of the machine, available or not
    machine_physical: int | None = None

    @property
    def logical(self) -> tuple[int, ...]:
        return tuple(sorted(r.cpu for r in self.cpus))

    def cores(self) -> list[Core]:
        """Physical cores in order of their first logical CPU."""
        grouped: dict[int, list[CpuRecord]] = {}
        for record in self.cpus:
            grouped.setdefault(record.core, []).append(record)
        cores = [
            Core(tuple(sorted(r.cpu for r in records)), max(r.efficiency for r in records))
            for records in grouped.values()
        ]
        return sorted(cores, key=lambda c: c.cpus[0])

    @property
    def classes(self) -> dict[int, tuple[int, ...]]:
        """Efficiency class -> logical CPUs; one entry on a non-hybrid machine."""
        out: dict[int, list[int]] = {}
        for record in self.cpus:
            out.setdefault(record.efficiency, []).append(record.cpu)
        return {cls: tuple(sorted(cpus)) for cls, cpus in sorted(out.items())}

    @property
    def hybrid(self) -> bool:
        return len(self.classes) > 1

    @property
    def smt(self) -> bool:
        return any(len(core.cpus) > 1 for core in self.cores())

    @property
    def kind(self) -> str:
        return "vcpu" if self.virtualization else "physical"

    def core_classes(self) -> dict[str, list[int]]:
        """``{"performance": [...], "efficiency": [...]}`` as recorded in run.json."""
        classes = self.classes
        if not classes:
            return {"performance": [], "efficiency": []}
        top = max(classes)
        return {
            "performance": list(classes[top]),
            "efficiency": sorted(c for cls, cpus in classes.items() if cls != top for c in cpus),
        }

    def describe(self) -> str:
        cores = self.cores()
        text = f"{self.model}: {len(self.cpus)} logical CPUs on {len(cores)} cores"
        if self.machine_logical and self.machine_logical != len(self.cpus):
            text += f" (of {self.machine_logical} on the machine)"
        if self.smt:
            text += ", SMT"
        if self.hybrid:
            classes = self.core_classes()
            text += (
                f", hybrid: performance {format_cpus(classes['performance'])},"
                f" efficiency {format_cpus(classes['efficiency'])}"
            )
        return text

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "logical": len(self.cpus),
            "physical": len(self.cores()),
            "machine_logical": self.machine_logical,
            "machine_physical": self.machine_physical,
            "smt": self.smt,
            "hybrid": self.hybrid,
            "core_classes": self.core_classes(),
            "cores": [list(core.cpus) for core in self.cores()],
            "virtualization": self.virtualization,
            "kind": self.kind,
        }


def detect_topology() -> Topology:
    impl = platform.current()
    return Topology(
        cpus=tuple(impl.cpu_records()),
        model=impl.cpu_model(),
        virtualization=impl.virtualization(),
        machine_logical=psutil.cpu_count(logical=True),
        machine_physical=psutil.cpu_count(logical=False),
    )


# --------------------------------------------------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------------------------------------------------


class PlacementError(ValueError):
    """The machine profile cannot be placed on this host."""


def plan_placement(machine: Machine, topology: Topology) -> Placement:
    """Resolve the profile's CPU sets on this host (see the module docstring for the ``auto`` rule)."""
    available = set(topology.logical)
    if not available:
        raise PlacementError("no CPUs available to the harness")

    def explicit(spec: str, what: str) -> set[int]:
        try:
            cpus = set(parse_cpu_list(spec))
        except ValueError as exc:
            raise PlacementError(f"{what} {spec!r}: {exc}") from None
        missing = cpus - available
        if missing:
            raise PlacementError(
                f"{what} {spec!r} names CPUs {format_cpus(missing)} that the harness cannot use"
                f" (available: {format_cpus(available)})"
            )
        return cpus

    harness = None if machine.harness_cpus == "bot" else explicit(machine.harness_cpus, "harness_cpus")
    reserved = harness or set()
    if machine.server_cpus == "auto" and machine.bot_cpus == "auto":
        server, bots = _auto_split(topology, reserved)
    elif machine.server_cpus == "auto":
        bots = explicit(machine.bot_cpus, "bot_cpus")
        server = available - bots - reserved
    elif machine.bot_cpus == "auto":
        server = explicit(machine.server_cpus, "server_cpus")
        bots = available - server - reserved
    else:
        server = explicit(machine.server_cpus, "server_cpus")
        bots = explicit(machine.bot_cpus, "bot_cpus")

    if not server:
        raise PlacementError("no CPUs left for the server")
    if not bots:
        raise PlacementError("no CPUs left for the bots")
    if server & bots:
        raise PlacementError(f"server and bot CPU sets overlap on {format_cpus(server & bots)}")
    if harness is not None and harness & server:
        raise PlacementError(f"harness and server CPU sets overlap on {format_cpus(harness & server)}")
    return Placement(
        server_cpus=tuple(sorted(server)),
        bot_cpus=tuple(sorted(bots)),
        harness_cpus=tuple(sorted(bots if harness is None else harness)),
        priority=machine.priority,
        kind=topology.kind,
    )


def _auto_split(topology: Topology, reserved: set[int]) -> tuple[set[int], set[int]]:
    cores = [Core(tuple(c for c in core.cpus if c not in reserved), core.efficiency) for core in topology.cores()]
    cores = [core for core in cores if core.cpus]
    if len(cores) < 2:
        raise PlacementError("auto placement needs at least two physical cores; set server_cpus and bot_cpus")
    classes = sorted({core.efficiency for core in cores})
    if len(classes) > 1:
        server = {c for core in cores if core.efficiency == classes[-1] for c in core.cpus}
        bots = {c for core in cores if core.efficiency != classes[-1] for c in core.cpus}
        if len(bots) >= MIN_BOT_CPUS:
            return server, bots
    order = sorted(cores, key=lambda core: (core.efficiency, -core.cpus[0]))
    quota = math.ceil(len(cores) / 4)
    bot_cores: list[Core] = []
    for core in order:
        logical = sum(len(c.cpus) for c in bot_cores)
        if len(bot_cores) >= len(cores) // 2 or (len(bot_cores) >= quota and logical >= MIN_BOT_CPUS):
            break
        bot_cores.append(core)
    bots = {c for core in bot_cores for c in core.cpus}
    server = {c for core in cores for c in core.cpus} - bots
    return server, bots


@dataclass(frozen=True)
class Check:
    """One doctor finding. ``fail`` makes ``bbench doctor`` and the runner's environment gate refuse."""

    id: str
    level: str  # ok | info | warn | fail
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"bad check level {self.level!r}")


def placement_checks(placement: Placement, topology: Topology) -> list[Check]:
    """Findings about a resolved placement that do not make it unusable but may make it unfair or invalid."""
    checks: list[Check] = []
    bots = len(placement.bot_cpus)
    if bots < MIN_BOT_CPUS:
        checks.append(
            Check(
                "bot_cpus",
                "fail",
                f"the bots get {bots} logical CPUs; at least {MIN_BOT_CPUS} are needed so the bot never limits the"
                " server",
            )
        )
    core_of = {r.cpu: r.core for r in topology.cpus}
    server_cores = {core_of[c] for c in placement.server_cpus if c in core_of}
    shared = sorted(
        c for c in (*placement.bot_cpus, *placement.harness_cpus) if c in core_of and core_of[c] in server_cores
    )
    if shared:
        checks.append(
            Check(
                "smt_split",
                "warn",
                f"CPUs {format_cpus(shared)} are SMT siblings of server CPUs; bot load on them slows the server",
            )
        )
    efficiency = {r.cpu: r.efficiency for r in topology.cpus}
    if len({efficiency.get(c, 0) for c in placement.server_cpus}) > 1:
        checks.append(Check("server_classes", "warn", "the server CPU set mixes performance and efficiency cores"))
    if sys.platform == "win32" and max((*placement.server_cpus, *placement.bot_cpus, *placement.harness_cpus)) >= 64:
        checks.append(Check("processor_group", "fail", "affinity masks only reach the first 64 logical CPUs"))
    return checks


@dataclass(frozen=True)
class PlacementCheck:
    """A running process measured against its planned CPU set (the ``placement_match`` gate)."""

    ok: bool
    expected: tuple[int, ...]
    process_cpus: tuple[int, ...] | None
    outside: dict[int, tuple[int, ...]]  # tid -> CPUs, threads allowed outside the plan
    narrowed: dict[int, tuple[int, ...]]  # tid -> CPUs, threads the server restricted itself (not a violation)
    priority: str | None
    problems: tuple[str, ...]


def verify_placement(proc: psutil.Process, cpus: Sequence[int], priority: str | None = None) -> PlacementCheck:
    """Check that ``proc`` and every one of its threads stay inside ``cpus``, and its priority if given.

    A thread that a server pins to a subset of the plan is reported in ``narrowed`` but is not a violation.
    """
    impl = platform.current()
    expected = tuple(sorted(set(cpus)))
    allowed = set(expected)
    try:
        process_cpus = impl.process_affinity(proc.pid)
        threads = [t.id for t in proc.threads()]
        observed = impl.get_priority(proc.pid) if priority is not None else None
    except (platform.ProcessGone, psutil.NoSuchProcess):
        return PlacementCheck(False, expected, None, {}, {}, None, ("process exited",))
    problems: list[str] = []
    if process_cpus != expected:
        problems.append(f"process affinity {format_cpus(process_cpus)} != planned {format_cpus(expected)}")
    outside: dict[int, tuple[int, ...]] = {}
    narrowed: dict[int, tuple[int, ...]] = {}
    for tid in threads:
        affinity = impl.thread_affinity(proc.pid, tid)
        if affinity is None:
            continue
        if not set(affinity) <= allowed:
            outside[tid] = affinity
        elif set(affinity) != allowed:
            narrowed[tid] = affinity
    if outside:
        problems.append(f"{len(outside)} threads may run outside the planned CPUs")
    if priority is not None and observed != priority:
        problems.append(f"priority {observed} != planned {priority}")
    return PlacementCheck(not problems, expected, process_cpus, outside, narrowed, observed, tuple(problems))


# --------------------------------------------------------------------------------------------------------------------
# Idle check
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IdleCheck:
    """Foreign load on a quiet machine, measured before a run (and by ``bbench doctor``)."""

    seconds: float
    foreign_cores: float  # mean, floored at 0
    peak_10s: float
    server_set_cores: float
    top: list[dict[str, Any]]
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reasons


def idle_check(machine: Machine, placement: Placement | None, seconds: float | None = None) -> IdleCheck:
    """Measure foreign load for ``seconds`` (default the profile's ``idle_check_s``) against the profile's gates."""
    from bedrock_bench.sampling.machine import measure_foreign

    gates = machine.gates
    duration = gates.idle_check_s if seconds is None else seconds
    load = measure_foreign(duration, placement.server_cpus if placement else ())
    mean, peak, server_set = max(0.0, load.mean), max(0.0, load.peak(10.0)), max(0.0, load.server_set_mean)
    reasons = []
    if mean > gates.foreign_cores_mean:
        reasons.append(f"foreign load {mean:.2f} cores > {gates.foreign_cores_mean:g}")
    if peak > gates.foreign_cores_peak_10s:
        reasons.append(f"foreign 10 s peak {peak:.2f} cores > {gates.foreign_cores_peak_10s:g}")
    if placement is not None and server_set > gates.foreign_server_set_cores_mean:
        reasons.append(f"foreign load on server CPUs {server_set:.2f} cores > {gates.foreign_server_set_cores_mean:g}")
    return IdleCheck(duration, mean, peak, server_set, load.top, tuple(reasons))


# --------------------------------------------------------------------------------------------------------------------
# Machine facts
# --------------------------------------------------------------------------------------------------------------------


def os_description() -> tuple[str, str | None]:
    """``(os, kernel)``, e.g. ``("Ubuntu 24.04.1 LTS", "6.8.0-45-generic")``; the kernel is None on Windows."""
    if sys.platform == "win32":
        return f"Windows {_stdlib_platform.release()} ({_stdlib_platform.version()})", None
    try:
        info = _stdlib_platform.freedesktop_os_release()
        name = info.get("PRETTY_NAME") or info.get("NAME") or "Linux"
    except OSError:
        name = _stdlib_platform.system()
    return name, _stdlib_platform.release()


def power_description(settings: dict[str, Any]) -> str | None:
    if "scheme" in settings:
        return str(settings["scheme"])
    parts = []
    if settings.get("driver"):
        parts.append(str(settings["driver"]))
    if settings.get("governors"):
        parts.append("/".join(settings["governors"]))
    return " ".join(parts) or None


def machine_facts(topology: Topology | None = None) -> dict[str, Any]:
    """Host facts for the ``machine`` block of run.json. No hostname, user name or path.

    ``fingerprint_id``, ``label``, ``profile_sha256`` and ``tsc_hz_calibrated`` are added by the caller.
    """
    topology = topology or detect_topology()
    os_name, kernel = os_description()
    return {
        "os": os_name,
        "kernel": kernel,
        "cpu": topology.model,
        "logical": topology.machine_logical or len(topology.cpus),
        "physical": topology.machine_physical,
        "core_classes": topology.core_classes(),
        "ram_gib": round(psutil.virtual_memory().total / 2**30, 1),
        "virtualized": topology.virtualization is not None,
        "power": power_description(platform.current().power_settings()),
    }


def calibrate_tsc() -> float | None:
    """TSC ticks per second on Windows (for the ``cycles`` columns); None elsewhere."""
    impl = platform.current()
    return impl.calibrate_tsc() if hasattr(impl, "calibrate_tsc") else None


# --------------------------------------------------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------------------------------------------------


def _udp_free(port: int, family: socket.AddressFamily) -> bool | None:
    """Whether a UDP port can be bound on all addresses; None if the address family is unavailable."""
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM)
    except OSError:
        return None
    with sock:
        if family == socket.AF_INET6:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            sock.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", port))
        except OSError:
            return False
    return True


def check_ports(port: int) -> Check:
    v4 = _udp_free(port, socket.AF_INET)
    v6 = _udp_free(port + 1, socket.AF_INET6)
    busy = [f"{port}/udp (IPv4)"] if v4 is False else []
    busy += [f"{port + 1}/udp (IPv6)"] if v6 is False else []
    if busy:
        return Check("ports", "fail", f"in use: {', '.join(busy)}; stop what holds them or change runtime.port")
    note = "" if v6 is not None else " (no IPv6 stack)"
    return Check("ports", "ok", f"UDP {port} (IPv4) and {port + 1} (IPv6) are free{note}")


def _tool_version(argv: Sequence[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip() or None


def check_go() -> Check:
    text = _tool_version(["go", "version"])
    if text is None:
        return Check("go", "warn", "go not found: 'bbench bots build' needs Go >= 1.25 ('bbench bots fetch' does not)")
    match = re.search(r"go(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return Check("go", "warn", f"cannot read the Go version from {text!r}")
    version = (int(match.group(1)), int(match.group(2)))
    if version < MIN_GO:
        return Check(
            "go", "warn", f"{match.group(0)} is older than go{MIN_GO[0]}.{MIN_GO[1]}; 'bbench bots build' fails"
        )
    return Check("go", "ok", match.group(0))


def check_power() -> Check:
    settings = platform.current().power_settings()
    text = power_description(settings) or "unknown"
    governors = settings.get("governors") or []
    driver = str(settings.get("driver", ""))
    slow = settings.get("scheme") == "power_saver" or ("powersave" in governors and "pstate" not in driver)
    if slow:
        return Check("power", "warn", f"power setting {text} lowers clocks; use a performance plan", settings)
    return Check("power", "info", f"power setting {text}", settings)


def check_clock_source() -> Check:
    if sys.platform == "win32":
        return Check("cpu_time", "ok", "per-thread CPU from GetThreadTimes, cycles from QueryThreadCycleTime")
    impl = platform.current()
    if impl.schedstat_available():
        return Check("cpu_time", "ok", "per-thread CPU from schedstat (nanoseconds)")
    return Check(
        "cpu_time",
        "warn",
        f"schedstat is missing: per-thread CPU falls back to clock ticks of 1/{impl.clk_tck()} s",
    )


@dataclass
class DoctorReport:
    machine: Machine
    topology: Topology
    placement: Placement | None
    checks: list[Check]
    idle: IdleCheck | None = None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == "fail"]

    def to_json(self) -> dict[str, Any]:
        placement = None
        if self.placement is not None:
            placement = {
                "server_cpus": list(self.placement.server_cpus),
                "bot_cpus": list(self.placement.bot_cpus),
                "harness_cpus": list(self.placement.harness_cpus),
                "priority": self.placement.priority,
                "kind": self.placement.kind,
            }
        idle = None
        if self.idle is not None:
            idle = {
                "seconds": self.idle.seconds,
                "foreign_cores": round(self.idle.foreign_cores, 3),
                "peak_10s": round(self.idle.peak_10s, 3),
                "server_set_cores": round(self.idle.server_set_cores, 3),
                "top": self.idle.top,
            }
        return {
            "ok": not self.failed,
            "machine_profile": self.machine.label,
            "topology": self.topology.to_json(),
            "placement": placement,
            "idle": idle,
            "checks": [{"id": c.id, "level": c.level, "summary": c.summary, "detail": c.detail} for c in self.checks],
        }

    def render(self) -> str:
        width = max(len(c.id) for c in self.checks)
        lines = [f"bbench doctor (machine profile: {self.machine.label or 'default'})"]
        lines += [f"  {c.level:<4}  {c.id:<{width}}  {c.summary}" for c in self.checks]
        warnings = sum(c.level == "warn" for c in self.checks)
        if self.failed:
            lines.append(f"FAILED: {', '.join(c.id for c in self.failed)}")
        else:
            lines.append("OK" + (f" ({warnings} warning{'s' if warnings != 1 else ''})" if warnings else ""))
        return "\n".join(lines)


def run_doctor(machine: Machine, *, idle_s: float | None = None, progress: bool = False) -> DoctorReport:
    """Every doctor check. ``idle_s`` overrides the profile's idle measurement time (0 skips it)."""
    impl = platform.current()
    topology = detect_topology()
    os_name, kernel = os_description()
    checks = [
        Check(
            "platform",
            "info",
            f"{os_name}{f', kernel {kernel}' if kernel else ''}; Python {_stdlib_platform.python_version()},"
            f" psutil {psutil.__version__}, bbench {__version__}",
        ),
        Check("topology", "info", topology.describe(), topology.to_json()),
    ]
    if topology.virtualization:
        checks.append(
            Check(
                "virtualization",
                "warn",
                f"running under {topology.virtualization}: placement pins vCPUs only; publish numbers from bare metal",
            )
        )
    else:
        checks.append(Check("virtualization", "ok", "bare metal"))

    placement: Placement | None = None
    try:
        placement = plan_placement(machine, topology)
    except PlacementError as exc:
        checks.append(Check("placement", "fail", str(exc)))
    else:
        checks.append(
            Check(
                "placement",
                "ok",
                f"server {format_cpus(placement.server_cpus)}, bots {format_cpus(placement.bot_cpus)},"
                f" harness {format_cpus(placement.harness_cpus)}, priority {placement.priority}, {placement.kind}",
            )
        )
        checks.extend(placement_checks(placement, topology))

    if impl.can_set_priority(machine.priority):
        checks.append(Check("priority", "ok", f"{machine.priority} priority can be set"))
    else:
        checks.append(Check("priority", "fail", f"{machine.priority} needs root, CAP_SYS_NICE or a higher RLIMIT_NICE"))
    checks.append(check_power())
    checks.append(check_clock_source())

    holder = lock.holder()
    idle = None
    if holder is not None:
        checks.append(Check("lock", "warn", f"machine lock held by {holder.describe()}"))
        checks.append(Check("idle", "info", "idle load not measured while another benchmark holds the lock"))
    else:
        checks.append(Check("lock", "ok", f"machine lock {lock.default_path().name} is free"))
        seconds = machine.gates.idle_check_s if idle_s is None else idle_s
        if seconds > 0:
            if progress:
                print(f"measuring idle load for {seconds:g} s...", file=sys.stderr, flush=True)
            idle = idle_check(machine, placement, seconds)
            if idle.ok:
                summary = (
                    f"foreign load {idle.foreign_cores:.2f} cores (10 s peak {idle.peak_10s:.2f},"
                    f" server CPUs {idle.server_set_cores:.2f}) over {seconds:g} s"
                )
            else:
                summary = "; ".join(idle.reasons) + f" over {seconds:g} s"
            if idle.top:
                summary += "; top: " + ", ".join(f"{t['name']} {t['cores']:.2f}" for t in idle.top[:3])
            checks.append(Check("idle", "ok" if idle.ok else "fail", summary))

    checks.append(check_ports(machine.port))
    checks.append(check_go())
    return DoctorReport(machine, topology, placement, checks, idle)


def doctor(args: argparse.Namespace) -> int:
    """``bbench doctor``: print the host's topology, planned placement and environment checks."""
    machine = load_machine(args.machine)
    try:
        platform.name()
    except platform.PlatformUnsupported as exc:
        print(f"bbench doctor: {exc}", file=sys.stderr)
        return ExitCode.ENV_GATE
    report = run_doctor(machine, progress=not args.json)
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.render())
    return ExitCode.ENV_GATE if report.failed else ExitCode.OK


__all__ = [
    "MIN_BOT_CPUS",
    "Check",
    "Core",
    "DoctorReport",
    "IdleCheck",
    "PlacementCheck",
    "PlacementError",
    "Topology",
    "calibrate_tsc",
    "detect_topology",
    "doctor",
    "format_cpus",
    "idle_check",
    "machine_facts",
    "placement_checks",
    "plan_placement",
    "run_doctor",
    "verify_placement",
]
