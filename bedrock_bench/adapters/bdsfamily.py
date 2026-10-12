# SPDX-License-Identifier: Apache-2.0
"""Behaviour shared by servers that host Mojang's Bedrock Dedicated Server: vanilla BDS, Endstone and LeviLamina.

- ``server.properties`` is rendered from the ``[config]`` table of ``configs/servers/bds/server.toml``. Every value
  that differs from what the pinned BDS build ships is recorded as an :class:`~bedrock_bench.model.Override` in its
  category.
- Console: ``Server started.`` is the ready line, ``time query gametime`` the monotonic tick probe and
  ``Player Spawned: <name>`` the in-world signal. The ``TRANSPORT TYPE ERROR`` banner that BDS prints at error level
  when RakNet is selected is benign.
- Positions come from ``querytarget @a``: the JSON array after ``Target data:`` is bracket-matched across continuation
  lines, prefixed or bare.
- The full chaos command set (see :meth:`BdsFamily.chaos_dialect`).
- The tick thread is the lowest-tid thread named ``MC_SERVER``: there is one on Windows and several on Linux, where
  threads started from the tick thread inherit its name.

Concrete adapters provide ``fetch``, ``launch``, the console ``dialect`` (prefix and levels) and, when a launcher
starts the server as a child, ``server_process`` and ``facts``. Extra files go through :meth:`BdsFamily.prepare_extra`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, fetch
from bedrock_bench.adapters.base import ChaosDialect, Dialect, ServerAdapter, format_coord, host_os, quote_player
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    Arm,
    ConfigFile,
    Install,
    Layout,
    LogLine,
    Machine,
    Override,
    OverrideCategory,
    PlayerPos,
    RunContext,
    WorldInfo,
)
from bedrock_bench.provenance import sha256_file, tree_sha256

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

PROPERTIES = "server.properties"
PROPERTIES_TEMPLATE = "configs/servers/bds/server.toml"
SERVER_NAME = "bedrock-bench"

# --------------------------------------------------------------------------------------------------------------------
# Console dialect
# --------------------------------------------------------------------------------------------------------------------

#: ``[2026-10-11 12:54:05:233 INFO] text``; the first line of a session is ``NO LOG FILE! - setting up ...``.
#: Lines of multi-line output after the first carry no prefix.
BDS_PREFIX = re.compile(r"^\s*(?:NO LOG FILE! - |\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3} (?P<level>[A-Z]+)\] ?)")
BDS_LEVELS: Mapping[str, str] = {
    "VERBOSE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "ERROR": "error",
    "FATAL": "fatal",
}

#: BDS prints this block at error level whenever RakNet is selected; the bots need RakNet, so it is expected.
TRANSPORT_BANNER: tuple[re.Pattern[str], ...] = (
    re.compile(r"TRANSPORT TYPE ERROR"),
    re.compile(r"connection type is not (?:set to )?NetherNet"),
    re.compile(r"without NetherNet"),
    re.compile(r"set 'transport=nethernet'"),
    re.compile(r"bedrock_server_how_to\.html"),
    re.compile(r"^=+\s*$"),
    re.compile(r"^\s*$"),
)

BDS_DIALECT = Dialect(
    ready=re.compile(r"^Server started\."),
    prefix=BDS_PREFIX,
    level_map=BDS_LEVELS,
    benign=TRANSPORT_BANNER,
    version=re.compile(r"^Version: (?P<version>\d+(?:\.\d+)+)\s*$"),
    player_in_world=re.compile(r"^Player Spawned: (?P<player>.+?) xuid:"),
    tick_cmd="time query gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)", re.IGNORECASE),
)


def bds_dialect(**changes: Any) -> Dialect:
    """The BDS dialect with some fields replaced, for launchers that reformat the log (prefix, levels, banner)."""
    return dataclasses.replace(BDS_DIALECT, **changes)


# --------------------------------------------------------------------------------------------------------------------
# server.properties
# --------------------------------------------------------------------------------------------------------------------


class Properties:
    """A ``server.properties`` file: ``key=value`` lines and ``#`` comments. Order, comments and newlines are kept."""

    def __init__(self, lines: Sequence[str] = (), newline: str = "\n") -> None:
        self._lines = list(lines)
        self.newline = newline

    @classmethod
    def parse(cls, text: str) -> Properties:
        newline = "\r\n" if "\r\n" in text else "\n"
        body = text[1:] if text.startswith("﻿") else text
        return cls(body.splitlines(), newline)

    @classmethod
    def load(cls, path: Path) -> Properties:
        with open(path, encoding="utf-8", newline="") as fh:
            return cls.parse(fh.read())

    @staticmethod
    def _split(line: str) -> tuple[str, str] | None:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")) or "=" not in stripped:
            return None
        key, _, value = stripped.partition("=")
        return key.strip(), value.strip()

    def get(self, key: str) -> str | None:
        for line in self._lines:
            pair = self._split(line)
            if pair is not None and pair[0] == key:
                return pair[1]
        return None

    def set(self, key: str, value: str) -> None:
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: a property value is one line")
        for index, line in enumerate(self._lines):
            pair = self._split(line)
            if pair is not None and pair[0] == key:
                self._lines[index] = f"{key}={value}"
                return
        self._lines.append(f"{key}={value}")

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self._lines:
            pair = self._split(line)
            if pair is not None:
                out.setdefault(pair[0], pair[1])
        return out

    def text(self) -> str:
        return self.newline.join(self._lines) + self.newline

    def dump(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(self.text())


@dataclass(frozen=True)
class PropertyPin:
    """One value every BDS-hosted server gets, from the ``[config]`` table of ``configs/servers/bds/server.toml``."""

    key: str
    template: str  # may contain {placeholders} filled from the scenario and machine profile
    category: OverrideCategory
    reason: str

    def render(self, context: Mapping[str, str]) -> str:
        try:
            return self.template.format_map(context)
        except (KeyError, IndexError, ValueError) as exc:
            known = ", ".join(sorted(context))
            raise ConfigError(f"{self.key}: bad placeholder in {self.template!r} ({exc}); known: {known}") from None


def load_property_pins(path: Path) -> tuple[PropertyPin, ...]:
    """Read the ``[config]`` table of a server file: one table per override category, ``key = {value, reason}``."""
    _, data = config.load_server_table(path, "config")
    if data.get("file", PROPERTIES) != PROPERTIES:
        raise ConfigError(f"must be {PROPERTIES!r}", file=path, key="config.file")
    pins: list[PropertyPin] = []
    seen: set[str] = set()
    for section, table in data.items():
        if section == "file":
            continue
        try:
            category = OverrideCategory(section)
        except ValueError:
            known = ", ".join(c.value for c in OverrideCategory)
            raise ConfigError(f"unknown table; use one of {known}", file=path, key=f"config.{section}") from None
        if not isinstance(table, dict):
            raise ConfigError("must be a table", file=path, key=f"config.{section}")
        for key, entry in table.items():
            where = f"config.{section}.{key}"
            if not isinstance(entry, dict) or set(entry) != {"value", "reason"}:
                raise ConfigError('must be { value = "...", reason = "..." }', file=path, key=where)
            if not isinstance(entry["value"], str) or not isinstance(entry["reason"], str) or not entry["reason"]:
                raise ConfigError("value and reason must be strings, reason non-empty", file=path, key=where)
            if key in seen:
                raise ConfigError("key appears in two categories", file=path, key=where)
            seen.add(key)
            pins.append(PropertyPin(key, entry["value"], category, entry["reason"]))
    return tuple(pins)


def property_context(ctx: RunContext, *, csg: bool) -> dict[str, str]:
    """Placeholder values for the ``[config]`` table of ``configs/servers/bds/server.toml``."""
    server = ctx.scenario.server
    world = ctx.scenario.world
    return {
        "server_name": SERVER_NAME,
        "port": str(ctx.machine.port),
        "port_v6": str(ctx.machine.port + 1),
        "view_distance": str(server.view_distance),
        "tick_distance": str(server.tick_distance),
        "max_players": str(server.max_players),
        "difficulty": server.difficulty,
        "gamemode": server.gamemode,
        "level_name": world.level_name,
        "seed": str(world.seed),
        "csg": "true" if csg else "false",
    }


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("true", "on", "1", "yes"):
        return True
    if lowered in ("false", "off", "0", "no"):
        return False
    return None


# --------------------------------------------------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------------------------------------------------

TARGET_DATA = "Target data:"
NO_TARGETS = re.compile(r"No targets matched", re.IGNORECASE)
_JSON_CONTINUATION = re.compile(r"^\s*[\[\]{}\"]")


def json_array_at(text: str, start: int) -> str | None:
    """The JSON array starting at ``text[start]`` (which must be ``[``), matched with string literals skipped."""
    if start >= len(text) or text[start] != "[":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_querytarget(lines: Sequence[LogLine]) -> list[PlayerPos] | None:
    """Positions from ``querytarget @a`` output. None when the reply is missing or unreadable, [] for no players.

    querytarget prints no names, so ``PlayerPos.name`` is the entity's unique id. Continuation lines are joined as
    printed; if an unrelated line interrupts the array, only lines that look like JSON are tried a second time.
    """
    for index, line in enumerate(lines):
        position = line.text.find(TARGET_DATA)
        if position < 0:
            continue
        head = line.text[position + len(TARGET_DATA) :]
        rest = [later.text for later in lines[index + 1 :]]
        for body in (rest, [text for text in rest if _JSON_CONTINUATION.match(text)]):
            parsed = _positions_from("\n".join([head, *body]))
            if parsed is not None:
                return parsed
        return None
    if any(NO_TARGETS.search(line.text) for line in lines):
        return []
    return None


def _positions_from(text: str) -> list[PlayerPos] | None:
    array = json_array_at(text, text.find("["))
    if array is None:
        return None
    try:
        data = json.loads(array)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    positions = []
    for number, entry in enumerate(data):
        try:
            where = entry["position"]
            x, y, z = float(where["x"]), float(where["y"]), float(where["z"])
            dim = int(entry.get("dimension", 0))
        except (KeyError, TypeError, ValueError):
            return None
        name = entry.get("uniqueId", entry.get("id", number))
        positions.append(PlayerPos(name=str(name), x=x, y=y, z=z, dim=dim))
    return positions


# --------------------------------------------------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------------------------------------------------


def bds_platform() -> str:
    """``windows`` or ``linux``: which BDS build this machine runs."""
    system = host_os()
    if system not in ("windows", "linux"):
        raise ConfigError(f"BDS runs on Windows and Linux only, not on {system}")
    return system


def exe_name(platform: str) -> str:
    return "bedrock_server.exe" if platform == "windows" else "bedrock_server"


def fetch_bds(cache: Cache, version: str, platform: str, *, artifact: Path | None = None) -> Install:
    """Verify and install one BDS build: the zip pinned in ``configs/servers/bds/server.toml``, or a local zip."""
    executable = exe_name(platform)
    if artifact is not None:
        archive, ref = fetch.adopt_local(artifact, cache)
        executables: tuple[str, ...] = (executable,)
    else:
        catalog = config.load_artifacts("bds")
        try:
            spec = catalog.get(version, platform)
        except KeyError as exc:
            raise ConfigError(str(exc.args[0]), file=catalog.path) from None
        archive, ref = fetch.obtain(spec, cache)
        executables = spec.executables or (executable,)
    root = cache.install_dir("bds", version, platform, ref.sha256)
    tree = fetch.ensure_install(cache, root, lambda tmp: fetch.extract_zip(archive, tmp, executables=executables))
    exe = root / executable
    if not exe.is_file():
        raise fetch.FetchError(f"{ref.name} has no {executable} at its top level")
    return Install(
        root=root,
        tree_sha256=tree,
        artifacts=(ref,),
        extra={"bds_version": version, "platform": platform, "exe": executable, "exe_sha256": sha256_file(exe)},
    )


# --------------------------------------------------------------------------------------------------------------------
# Adapter base
# --------------------------------------------------------------------------------------------------------------------

GAMEMODES = ("survival", "creative", "adventure", "spectator")
WEATHER = ("clear", "rain", "thunder")
DIMENSIONS = ("overworld", "nether", "the_end")
PLAYER_WAYPOINTS = ("off", "everyone")
_IDENTIFIER = re.compile(r"^[a-z0-9_]+(?::[a-z0-9_]+)?$")
_LOOT_TABLE = re.compile(r"^[a-z0-9_./:-]+$")
_AREA_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
GAMERULE_APPLIED = re.compile(r"^Game rule (?P<rule>\w+) has been updated to (?P<value>\S+)")


def _choice(name: str, value: str, choices: Sequence[str]) -> str:
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _matches(name: str, value: str, pattern: re.Pattern[str]) -> str:
    if not pattern.match(value):
        raise ValueError(f"invalid {name} {value!r}")
    return value


def _relative(offset: float) -> str:
    return "~" if offset == 0 else f"~{format_coord(offset)}"


class BdsFamily(ServerAdapter):
    """Shared behaviour of BDS-hosted servers. Not registered itself: it has no ``name``."""

    protocol: ClassVar[int] = 2193
    supports_csg: ClassVar[bool] = True
    dialect: ClassVar[Dialect] = BDS_DIALECT
    tick_thread_rule: ClassVar[str] = "lowest_tid_name:MC_SERVER"
    thread_groups: ClassVar[Mapping[str, str]] = {
        "streaming_pool": r"^Streaming Pool",
        "leveldb": r"^LevelDB",
        "io": r"^IO Thread",
        "rest": r"^REST Thread",
    }
    crash_paths: ClassVar[tuple[str, ...]] = ("%LOCALAPPDATA%/CrashDumps",)
    #: Server file whose ``[config]`` table holds the server.properties values, relative to the project root.
    properties_template: ClassVar[str] = PROPERTIES_TEMPLATE

    def __init__(self, arm: Arm, machine: Machine) -> None:
        super().__init__(arm, machine)
        self._server_exe: Path | None = None

    @property
    def platform(self) -> str:
        return bds_platform()

    # ---- prepare --------------------------------------------------------------------------------------------------

    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        server_dir = ctx.work_dir
        bds_root = Path(install.extra.get("bds_root", install.root))
        fetch.copy_install(bds_root, server_dir)
        exe_rel = install.extra["exe"]
        exe_sha256 = sha256_file(server_dir / exe_rel)
        if exe_sha256 != install.extra["exe_sha256"]:
            raise fetch.FetchError(f"the fresh copy of {exe_rel} does not match the verified install")
        self._server_exe = server_dir / exe_rel

        world = self._prepare_world(server_dir, ctx)
        overrides, written, properties_sha = self._render_properties(server_dir, ctx)
        extra_overrides, extra_files, extra = self.prepare_extra(server_dir, install, ctx)
        return Layout(
            server_dir=server_dir,
            overrides=(*overrides, *extra_overrides),
            config_files=(ConfigFile(PROPERTIES, properties_sha), *extra_files),
            world=world,
            extra={
                "exe": exe_rel,
                "exe_sha256": exe_sha256,
                "bds_version": install.extra.get("bds_version"),
                "properties": written,
                **extra,
            },
        )

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        """Hook for files beyond server.properties: ``(overrides, config files, layout extra)``."""
        return (), (), {}

    def _prepare_world(self, server_dir: Path, ctx: RunContext) -> WorldInfo:
        level_dir = server_dir / "worlds" / ctx.scenario.world.level_name
        if ctx.scenario.world.source == "template":
            if ctx.template is None:
                raise ConfigError(
                    f"scenario {ctx.scenario.name} needs a world template; build one with bbench prepare-world"
                )
            fetch.copy_install(ctx.template, level_dir)
            return WorldInfo(
                fresh=False, generator="vanilla", template_sha256=tree_sha256(ctx.template), path=level_dir
            )
        if level_dir.exists():
            raise RuntimeError(f"the fresh server copy already has a world at {level_dir}")
        return WorldInfo(fresh=True, generator="vanilla", path=level_dir)

    def _render_properties(self, server_dir: Path, ctx: RunContext) -> tuple[list[Override], dict[str, str], str]:
        path = server_dir / PROPERTIES
        props = Properties.load(path) if path.is_file() else Properties()
        pins = load_property_pins(config.project_root() / self.properties_template)
        context = property_context(ctx, csg=ctx.scenario.server.csg and self.supports_csg)
        overrides: list[Override] = []
        written: dict[str, str] = {}
        for pin in pins:
            value = pin.render(context)
            default = props.get(pin.key)
            props.set(pin.key, value)
            written[pin.key] = value
            if default != value:
                overrides.append(Override(pin.category, PROPERTIES, pin.key, default, value, pin.reason))
        props.dump(path)
        return overrides, written, sha256_file(path)

    def readback(self, layout: Layout) -> list[str]:
        """Pinned server.properties values that changed while the server ran; empty when all read back."""
        path = layout.server_dir / PROPERTIES
        if not path.is_file():
            return [f"{PROPERTIES} is missing after the run"]
        props = Properties.load(path)
        problems = []
        for key, value in layout.extra.get("properties", {}).items():
            found = props.get(key)
            if found != value:
                problems.append(f"{PROPERTIES}: {key} was written as {value!r} and read back as {found!r}")
        return problems

    def executable(self, layout: Layout) -> Path:
        """The server binary in the run's copy; its sha256 is checked before and after the run."""
        return layout.server_dir / layout.extra["exe"]

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_positions(self) -> str | None:
        return "querytarget @a"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_querytarget(lines)

    def chaos_dialect(self) -> ChaosDialect:
        """Console commands for chaos actions. Parameters are keyword arguments from the chaos plan.

        ========================  =========================================================  =========================
        command                   parameters                                                 renders
        ========================  =========================================================  =========================
        ``tp``                    player, x, y, z                                            ``tp a00 1280 300 1280``
        ``tp_dimension``          player, dimension (overworld|nether|the_end), x, y, z      ``execute in nether ...``
        ``effects``               player                                                     the placement effects
        ``effect_clear``          player                                                     ``effect a00 clear``
        ``kill``                  player                                                     ``kill a00``
        ``kick``                  player, reason="chaos"                                     ``kick a00 chaos``
        ``gamemode``              player, mode (survival|creative|adventure|spectator)       ``gamemode creative a00``
        ``summon``                player, entity, dx, dy, dz (offsets from the player)       ``execute at a00 run ...``
        ``loot``                  player, table                                              ``... loot spawn ...``
        ``cleanup``               (none)                                                     ``kill @e[type=!player]``
        ``list``                  (none)                                                     ``list``
        ``randomtickspeed``       value                                                      ``gamerule ... 3``
        ``playerwaypoints``       mode (off|everyone)                                        ``gamerule ... off``
        ``weather``               kind (clear|rain|thunder)                                  ``weather rain``
        ``fill``                  player, dx1, dy1, dz1, dx2, dy2, dz2, block                ``execute at a00 run ...``
        ``setblock``              player, dx, dy, dz, block                                  ``execute at a00 run ...``
        ``tickingarea_add``       name, x, z, y=64, radius=2                                 ``tickingarea add ...``
        ``tickingarea_remove``    name                                                       ``tickingarea remove ...``
        ========================  =========================================================  =========================
        """

        def tp(player: str, x: float, y: float, z: float) -> str:
            return self.cmd_tp(player, x, y, z)

        def tp_dimension(player: str, dimension: str, x: float, y: float, z: float) -> str:
            _choice("dimension", dimension, DIMENSIONS)
            return f"execute in {dimension} run {self.cmd_tp(player, x, y, z)}"

        def effects(player: str) -> list[str]:
            return self.cmd_effects(player)

        def effect_clear(player: str) -> str:
            return f"effect {quote_player(player)} clear"

        def kill(player: str) -> str:
            return f"kill {quote_player(player)}"

        def kick(player: str, reason: str = "chaos") -> str:
            if "\n" in reason or "\r" in reason:
                raise ValueError("kick reason must be one line")
            return f"kick {quote_player(player)} {reason}".rstrip()

        def gamemode(player: str, mode: str) -> str:
            return f"gamemode {_choice('mode', mode, GAMEMODES)} {quote_player(player)}"

        def summon(player: str, entity: str, dx: float = 0, dy: float = 1, dz: float = 0) -> str:
            entity = _matches("entity", entity, _IDENTIFIER)
            where = f"{_relative(dx)} {_relative(dy)} {_relative(dz)}"
            return f"execute at {quote_player(player)} run summon {entity} {where}"

        def loot(player: str, table: str) -> str:
            table = _matches("loot table", table, _LOOT_TABLE)
            return f'execute at {quote_player(player)} run loot spawn ~ ~2 ~ loot "{table}"'

        def cleanup() -> str:
            return "kill @e[type=!player]"

        def list_players() -> str:
            return "list"

        def randomtickspeed(value: int) -> str:
            if int(value) != value or value < 0:
                raise ValueError(f"randomtickspeed must be a non-negative integer, got {value!r}")
            return f"gamerule randomtickspeed {int(value)}"

        def playerwaypoints(mode: str) -> str:
            return f"gamerule playerwaypoints {_choice('mode', mode, PLAYER_WAYPOINTS)}"

        def weather(kind: str) -> str:
            return f"weather {_choice('kind', kind, WEATHER)}"

        def fill(
            player: str, dx1: float, dy1: float, dz1: float, dx2: float, dy2: float, dz2: float, block: str
        ) -> str:
            block = _matches("block", block, _IDENTIFIER)
            corners = " ".join(_relative(v) for v in (dx1, dy1, dz1, dx2, dy2, dz2))
            return f"execute at {quote_player(player)} run fill {corners} {block}"

        def setblock(player: str, dx: float, dy: float, dz: float, block: str) -> str:
            block = _matches("block", block, _IDENTIFIER)
            where = " ".join(_relative(v) for v in (dx, dy, dz))
            return f"execute at {quote_player(player)} run setblock {where} {block}"

        def tickingarea_add(name: str, x: float, z: float, y: float = 64, radius: int = 2) -> str:
            name = _matches("ticking area name", name, _AREA_NAME)
            return f"tickingarea add circle {format_coord(x)} {format_coord(y)} {format_coord(z)} {int(radius)} {name}"

        def tickingarea_remove(name: str) -> str:
            return f"tickingarea remove {_matches('ticking area name', name, _AREA_NAME)}"

        return ChaosDialect(
            renderers={
                "tp": tp,
                "tp_dimension": tp_dimension,
                "effects": effects,
                "effect_clear": effect_clear,
                "kill": kill,
                "kick": kick,
                "gamemode": gamemode,
                "summon": summon,
                "loot": loot,
                "cleanup": cleanup,
                "list": list_players,
                "randomtickspeed": randomtickspeed,
                "playerwaypoints": playerwaypoints,
                "weather": weather,
                "fill": fill,
                "setblock": setblock,
                "tickingarea_add": tickingarea_add,
                "tickingarea_remove": tickingarea_remove,
            }
        )

    def applied(self, action: str, line: LogLine) -> dict[str, Any] | None:
        """Game rule changes confirmed by the server: ``randomtickspeed`` and ``playerwaypoints``."""
        if action not in ("randomtickspeed", "playerwaypoints"):
            return None
        match = GAMERULE_APPLIED.search(line.text)
        if match is None or match["rule"].lower() != action:
            return None
        value = match["value"].rstrip(".")
        if action == "randomtickspeed":
            try:
                return {action: int(value)}
            except ValueError:
                return None
        return {action: value}

    # ---- facts and crashes ----------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        props = Properties.load(layout.server_dir / PROPERTIES)
        return {
            "allocator": detect_allocator(proc),
            "csg_setting": parse_bool(props.get("client-side-chunk-generation-enabled")),
            "max_threads": props.get("max-threads"),
            "transport": props.get("transport"),
            "bds_version": layout.extra.get("bds_version"),
        }

    def crash_artifacts(self, layout: Layout, since_epoch_ms: int) -> list[Path]:
        """Files under the crash paths written since the run started; dumps of other programs are left out."""
        stem = Path(layout.extra.get("exe", "bedrock_server")).stem.lower()
        server_dir = layout.server_dir.resolve()
        kept = []
        for path in super().crash_artifacts(layout, since_epoch_ms):
            inside = path.resolve().is_relative_to(server_dir)
            if inside or path.name.lower().startswith(stem):
                kept.append(path)
        return kept


def detect_allocator(proc: psutil.Process) -> str | None:
    """``mimalloc``, ``jemalloc`` or ``tcmalloc`` when the process maps one, else ``default``; None if unreadable."""
    try:
        paths = [os.path.basename(m.path).lower() for m in proc.memory_maps(grouped=True) if m.path]
    except Exception:
        return None
    for allocator in ("mimalloc", "jemalloc", "tcmalloc"):
        if any(allocator in name for name in paths):
            return allocator
    return "default"


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Whether two paths name the same file, ignoring case on Windows."""

    def norm(p: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.realpath(os.fspath(p)))

    return norm(a) == norm(b)


__all__ = [
    "BDS_DIALECT",
    "BdsFamily",
    "Properties",
    "PropertyPin",
    "bds_dialect",
    "bds_platform",
    "detect_allocator",
    "exe_name",
    "fetch_bds",
    "load_property_pins",
    "parse_querytarget",
    "property_context",
    "same_path",
]
