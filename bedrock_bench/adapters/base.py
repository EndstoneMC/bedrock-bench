# SPDX-License-Identifier: Apache-2.0
"""The server adapter contract (.github/CONTRIBUTING.md#adding-a-server).

Adapters are the only code that knows servers by name. Each one says how to fetch and verify a build, how to lay out a
fresh server directory with its configs rendered from the templates in ``servers/<server>/``, how to launch it, and
how to talk to its console: the ready line, log prefix and levels, the monotonic tick probe, and the commands for
effects, teleports and positions. Phases, samplers, metrics and reports only use what an adapter declares here.

A server is a black box: adapters read what the server prints and what the operating system reports about the
process, and never hook into it.

Commands default to vanilla Bedrock syntax; an adapter overrides only what its server does differently.
"""

from __future__ import annotations

import abc
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, templates
from bedrock_bench.model import (
    LOG_LEVELS,
    PLATFORMS,
    Arm,
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    LogLine,
    Machine,
    PlayerPos,
    RunContext,
    ThreadInfo,
)

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

#: Bot flags an adapter may add for its server (rule R2). Anything else would change the workload.
ALLOWED_COMPAT = frozenset({"-fake-xuid"})

#: Tick thread rules without an argument. ``name:<thread>`` and ``lowest_tid_name:<thread>`` take one.
TICK_THREAD_RULES = ("none", "java_main", "first_created")

ADAPTER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def host_os() -> str:
    """``windows`` or ``linux`` (other platforms report their ``sys.platform``)."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def format_coord(value: float) -> str:
    """A coordinate for a console command: ``1280`` for whole numbers, at most two decimals otherwise."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def quote_player(name: str) -> str:
    """A player name as a command argument; names with spaces are quoted."""
    return f'"{name}"' if " " in name else name


# --------------------------------------------------------------------------------------------------------------------
# Console dialect
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Dialect:
    """How a server's console reads and writes.

    ``prefix`` is matched at the start of every output line and removed before any other pattern is applied, including
    on lines of multi-line JSON output. It may define the named groups ``level`` and ``logger``; ``level`` goes through
    ``level_map``. The other patterns are searched in the stripped text and need these named groups: ``version`` in
    ``version``, ``player`` in ``player_in_world``, ``tick`` in ``tick_re`` and ``mspt`` in ``mspt_re``.
    """

    ready: re.Pattern[str]  # "Server started." etc.
    prefix: re.Pattern[str]
    level_map: Mapping[str, str]  # raw level -> one of LOG_LEVELS (e.g. "WRN" -> "warn")
    benign: tuple[re.Pattern[str], ...]  # error-level lines that are not errors
    version: re.Pattern[str]  # banner version; must match the arm version
    player_in_world: re.Pattern[str]  # the server says the player is in the world; gate for effects and teleport
    tick_cmd: str  # prints a counter that advances once per executed tick
    tick_re: re.Pattern[str]
    tick_monotonic: bool = True  # False -> TPS is unavailable for this server
    mspt_cmd: str | None = None  # the server's own MSPT readout (later)
    mspt_re: re.Pattern[str] | None = None
    stop_cmd: str = "stop"
    newline: str = "\n"

    def __post_init__(self) -> None:
        for attr, group in (("version", "version"), ("player_in_world", "player"), ("tick_re", "tick")):
            if group not in getattr(self, attr).groupindex:
                raise ValueError(f"Dialect.{attr} needs a named group {group!r}")
        if self.mspt_re is not None and "mspt" not in self.mspt_re.groupindex:
            raise ValueError("Dialect.mspt_re needs a named group 'mspt'")
        if (self.mspt_cmd is None) != (self.mspt_re is None):
            raise ValueError("Dialect.mspt_cmd and Dialect.mspt_re go together")
        unknown = sorted({level for level in self.level_map.values() if level not in LOG_LEVELS})
        if unknown:
            raise ValueError(f"Dialect.level_map maps to unknown levels {unknown}; use {', '.join(LOG_LEVELS)}")
        if self.newline not in ("\n", "\r\n"):
            raise ValueError("Dialect.newline must be '\\n' or '\\r\\n'")

    def split(self, line: str) -> tuple[str | None, str | None, str]:
        """``(level, logger, text)`` of one output line. Unknown or missing parts are None."""
        match = self.prefix.match(line)
        if match is None:
            return None, None, line
        groups = match.groupdict()
        raw_level = groups.get("level")
        level = self.level_map.get(raw_level) if raw_level is not None else None
        return level, groups.get("logger"), line[match.end() :]

    def strip(self, line: str) -> str:
        return self.split(line)[2]

    def is_ready(self, text: str) -> bool:
        return self.ready.search(text) is not None

    def is_benign(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.benign)

    def parse_tick(self, text: str) -> int | None:
        match = self.tick_re.search(text)
        return int(match["tick"]) if match else None

    def parse_version(self, text: str) -> str | None:
        match = self.version.search(text)
        return match["version"] if match else None

    def player_entered(self, text: str) -> str | None:
        """The player name if this line says a player is in the world."""
        match = self.player_in_world.search(text)
        return match["player"] if match else None


# --------------------------------------------------------------------------------------------------------------------
# Tick thread rules
# --------------------------------------------------------------------------------------------------------------------


def check_tick_rule(rule: str) -> None:
    kind, sep, arg = rule.partition(":")
    if sep:
        if kind not in ("name", "lowest_tid_name") or not arg:
            raise ValueError(f"bad tick thread rule {rule!r}")
    elif rule not in TICK_THREAD_RULES:
        raise ValueError(f"bad tick thread rule {rule!r}")


def apply_tick_rule(rule: str, threads: Sequence[ThreadInfo]) -> int | None:
    """The tick thread id under ``rule``, or None when the rule finds no single answer.

    - ``name:<n>``: the one thread named ``n``; None if there are none or several.
    - ``lowest_tid_name:<n>``: the lowest tid among threads named ``n``.
    - ``java_main``: the one thread named ``main``.
    - ``first_created``: the earliest created thread (lowest tid when creation times are unknown).
    - ``none``: no tick thread; only process CPU is reported.
    """
    check_tick_rule(rule)
    kind, _, arg = rule.partition(":")
    if kind == "name" or rule == "java_main":
        wanted = arg if kind == "name" else "main"
        matches = [t.tid for t in threads if t.name == wanted]
        return matches[0] if len(matches) == 1 else None
    if kind == "lowest_tid_name":
        return min((t.tid for t in threads if t.name == arg), default=None)
    if rule == "first_created":
        timed = [t for t in threads if t.created_ns is not None]
        if timed:
            return min(timed, key=lambda t: (t.created_ns, t.tid)).tid
        return min((t.tid for t in threads), default=None)
    return None


# --------------------------------------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------------------------------------


class ServerAdapter(abc.ABC):
    """Base class of every server adapter.

    A concrete subclass that declares ``name`` is registered automatically (see ``adapters/__init__.py``).
    """

    name: ClassVar[str]
    platforms: ClassVar[frozenset[str]]  # subset of {"windows", "linux"}
    protocol: ClassVar[int]  # network protocol; selects the bot build (2193 -> loadbot, 1001 -> legacy1001)
    bot_compat_flags: ClassVar[tuple[str, ...]] = ()  # subset of ALLOWED_COMPAT
    supports_csg: ClassVar[bool] = False
    dialect: ClassVar[Dialect]
    tick_thread_rule: ClassVar[str] = "none"
    crash_paths: ClassVar[tuple[str, ...]] = ()  # files or folders (relative to the server dir, env vars expanded)

    def __init__(self, arm: Arm, machine: Machine) -> None:
        self.arm = arm
        self.machine = machine

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.arm.id!r})"

    @classmethod
    def class_problems(cls) -> list[str]:
        """Contract violations in the class attributes; empty when the adapter is well formed."""
        problems: list[str] = []
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not ADAPTER_NAME_RE.match(name):
            problems.append(f"name must match {ADAPTER_NAME_RE.pattern}, got {name!r}")
        platforms = getattr(cls, "platforms", None)
        if not isinstance(platforms, frozenset) or not platforms or not platforms <= set(PLATFORMS):
            problems.append(f"platforms must be a non-empty frozenset of {PLATFORMS}, got {platforms!r}")
        protocol = getattr(cls, "protocol", None)
        if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol <= 0:
            problems.append(f"protocol must be a positive int, got {protocol!r}")
        extra_flags = set(cls.bot_compat_flags) - ALLOWED_COMPAT
        if extra_flags:
            problems.append(f"bot_compat_flags outside the allowlist {sorted(ALLOWED_COMPAT)}: {sorted(extra_flags)}")
        if not isinstance(getattr(cls, "dialect", None), Dialect):
            problems.append("dialect must be a Dialect")
        try:
            check_tick_rule(cls.tick_thread_rule)
        except ValueError as exc:
            problems.append(str(exc))
        return problems

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    @abc.abstractmethod
    def fetch(self, cache: Cache) -> Install:
        """Download, verify and install the arm's build into the cache. Must be idempotent."""

    @abc.abstractmethod
    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        """Make a fresh server copy under ``ctx.work_dir`` and render its configs with :meth:`write_config`."""

    @abc.abstractmethod
    def launch(self, layout: Layout) -> LaunchSpec:
        """The command line that starts the server. Pure: depends on the layout only."""

    def server_process(self, launched: psutil.Process) -> psutil.Process:
        """The process to measure. Override when a launcher starts the server as a child; match by exe path."""
        return launched

    # ---- config templates -----------------------------------------------------------------------------------------

    def folder(self) -> Path:
        """``servers/<name>/``: the server's server.toml, config templates and helper sources."""
        return config.server_dir(self.name)

    def template_variables(self, ctx: RunContext) -> dict[str, Any]:
        """What a template can use: ``workload``, and ``run`` with the ports and the number of server CPUs."""
        return {
            "workload": ctx.workload,
            "run": {
                "port": ctx.machine.port,
                "port_v6": ctx.machine.port + 1,
                "server_cpu_count": len(ctx.placement.server_cpus),
            },
        }

    def write_config(self, server_dir: Path, name: str, ctx: RunContext, *, target: str | None = None) -> ConfigFile:
        """Render the template ``name`` from :meth:`folder` onto ``server_dir/target`` (``name`` when not given)."""
        return templates.apply(self.folder(), name, server_dir, self.template_variables(ctx), target=target)

    def readback(self, layout: Layout) -> list[str]:
        """Rendered config values that changed while the server ran; empty when all read back."""
        return templates.readback(layout.server_dir, layout.config_files)

    def tick_thread(self, threads: Sequence[ThreadInfo]) -> int | None:
        return apply_tick_rule(self.tick_thread_rule, threads)

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_effects(self, player: str) -> list[str]:
        """Effects applied after spawn: no damage, no fire, no drowning, no hunger, particles hidden."""
        target = quote_player(player)
        return [
            f"effect {target} resistance infinite 255 true",
            f"effect {target} fire_resistance infinite 0 true",
            f"effect {target} water_breathing infinite 0 true",
            f"effect {target} saturation infinite 255 true",
        ]

    def cmd_tp(self, player: str, x: float, y: float, z: float) -> str:
        return f"tp {quote_player(player)} {format_coord(x)} {format_coord(y)} {format_coord(z)}"

    def cmd_positions(self) -> str | None:
        """A command that prints every player's position, or None to use bot-reported positions."""
        return None

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        """Positions from the reply to :meth:`cmd_positions`. None means unknown (the spread is then invalid)."""
        return None

    # ---- facts and crashes ----------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        """Observed facts for run.json: allocator, JVM flags, effective CSG setting and the like."""
        return {}

    def crash_artifacts(self, layout: Layout, since_epoch_ms: int) -> list[Path]:
        """Files under ``crash_paths`` modified since the run started."""
        cutoff = since_epoch_ms / 1000
        found: set[Path] = set()
        for entry in self.crash_paths:
            expanded = os.path.expandvars(entry)
            if "%" in expanded or "$" in expanded:
                continue  # variable not set on this host
            root = Path(expanded)
            if not root.is_absolute():
                root = layout.server_dir / root
            candidates = [root] if root.is_file() else root.rglob("*") if root.is_dir() else []
            for path in candidates:
                try:
                    if path.is_file() and path.stat().st_mtime >= cutoff:
                        found.add(path)
                except OSError:
                    continue
        return sorted(found)
