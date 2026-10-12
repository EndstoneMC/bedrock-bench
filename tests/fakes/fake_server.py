#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A scripted stand-in for a Minecraft Bedrock server, for tests only.

The fake prints the console output of one server family (BDS, Endstone, LeviLamina, PocketMine-MP, Nukkit,
PowerNukkitX or Dragonfly): that family's line prefix and level names, ready line, version banner and player lines.
It answers the console commands the harness sends (tick probe, positions, effects, teleports, kick, kill, stop) and
accepts fake bots over a JSON-lines TCP protocol on the configured port. A JSON behaviour script sets
the tick rate, delays or drops replies, rejects players and crashes on cue.

Line shapes follow the real servers; the texts around them are synthetic. The module uses the standard library only,
so it runs under any Python 3.11+ without the package installed, and it can be imported for its family table.

    python fake_server.py --family bds --version 1.26.52.3 --dir <server dir> [--script behaviour.json]

Families. Each family class shows its line prefix; the commands are the harness's tick probe and positions query::

    family        ready line             tick probe            positions
    bds           Server started.        time query gametime   querytarget @a
    endstone      Server started.        time query gametime   querytarget @a
    levilamina    Server started in (    time query gametime   querytarget @a
    pocketmine    Done (...s)!           gametime (helper)     positions (helper)
    nukkit        Done (...s)!           gametime (helper)     bot-reported
    powernukkitx  Done (...s)!           time query gametime   bot-reported
    dragonfly     Server started.        gametime (helper)     positions (helper)

Endstone runs the server as the child of a launcher, and Nukkit reads ``eng`` on stdin before its banner. BDS and
LeviLamina print the transport banner at error level after the ready line, so their dialects treat it as benign.
``test_fakes.py`` checks that the real bds, endstone and levilamina adapters read the fake output of their families.

Behaviour scripts. A fake adapter copies ``scripts/<variant>.json`` to ``bench-script.json`` in the server directory.
:data:`DEFAULT_SCRIPT` lists the keys and their defaults; unknown keys are errors. An event fires ``after_s`` seconds
after its ``anchor`` (default ``ready``): ``start``, ``ready``, ``first_spawn``, ``all_spawned``, or
``command:<regex>``, the first console command matching the regex. The actions:

- ``tps`` (``value``)
- ``crash`` (``exit_code``, ``reason``): writes ``crash_reports/crash-*.txt``, then exits
- ``hang``: stops ticking and stops answering the console
- ``probe_gap`` (``duration_s``): no replies to the tick probe for that long
- ``drop_player`` (``player``)
- ``decode_error`` (``player``): sends the bot a packet it cannot decode
- ``kick`` (``player``, ``reason``)
- ``error_line`` (``text``)

Wire protocol. Bots connect to ``127.0.0.1:<server-port>`` over TCP and exchange one JSON object per line, named by
its ``op`` key:

- bot to server: ``login`` (``name``, ``i``, ``n``, ``xuid``, ``uuid``, ``radius``, ``csg``, ``session``), ``move``
  (``x``, ``y``, ``z``), ``respawn``, ``radius`` (ignored), ``quit``
- server to bot: ``spawn`` (``x``, ``y``, ``z``, ``dim``, ``radius``, ``csg``, ``rates``), ``reject`` (``reason``),
  ``tp``, ``kick`` (``reason``), ``die``, ``respawned``, ``garbage``; the bots also accept ``dim``

The server clamps the requested radius to its view distance.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

#: Exit codes besides the scripted ones.
EXIT_USAGE = 64
EXIT_BIND_FAILED = 1
EXIT_LIFETIME = 70

#: Where a fresh player appears before the harness teleports it.
WORLD_SPAWN = (0.5, 70.0, 0.5)

#: Server config defaults; an adapter records every value it changes from these as an override.
CONFIG_FILE = "server.properties"
CONFIG_DEFAULTS: Mapping[str, str] = {
    "server-name": "Fake Server",
    "server-port": "19132",
    "server-portv6": "19133",
    "online-mode": "true",
    "level-name": "Bedrock level",
    "level-seed": "",
    "view-distance": "32",
    "tick-distance": "4",
    "max-players": "10",
    "difficulty": "easy",
    "gamemode": "survival",
    "client-side-chunk-generation-enabled": "true",
}

#: The behaviour script file an adapter copies next to the config.
SCRIPT_FILE = "bench-script.json"


# --------------------------------------------------------------------------------------------------------------------
# Server families: how each one formats its console output
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One console line before formatting. ``level`` is a canonical level, or None for a line without a prefix."""

    text: str
    level: str | None = "info"
    logger: str | None = None
    raw_level: str | None = None  # printed instead of the family's name for ``level``


def raw(text: str) -> Line:
    return Line(text, level=None)


@dataclass
class Player:
    name: str
    xuid: str
    uuid: str
    index: int
    entity_id: int
    port: int = 0
    peer: Peer | None = None
    x: float = WORLD_SPAWN[0]
    y: float = WORLD_SPAWN[1]
    z: float = WORLD_SPAWN[2]
    dim: int = 0
    spawned: bool = False


class Family:
    """Console format of one server family. Subclasses override what their server prints differently.

    The ``*_re`` attributes are the patterns an adapter needs to read this output; ``fake_adapter.py`` compiles them
    into a ``Dialect``. They live here, next to the code that prints the lines, so the two cannot drift apart.
    """

    name: ClassVar[str]
    levels: ClassVar[Mapping[str, str]] = {
        "debug": "DEBUG",
        "info": "INFO",
        "warn": "WARN",
        "error": "ERROR",
        "fatal": "FATAL",
    }
    extra_levels: ClassVar[Mapping[str, str]] = {}  # other raw levels this server prints -> canonical level
    logger: ClassVar[str | None] = None

    prefix_re: ClassVar[str]
    ready_re: ClassVar[str] = r"^Server started\."
    version_re: ClassVar[str]
    player_re: ClassVar[str] = r"^Player Spawned: (?P<player>\S+) xuid:"
    tick_re: ClassVar[str] = r"^Gametime is (?P<tick>\d+)$"
    benign_re: ClassVar[tuple[str, ...]] = ()

    tick_cmd: ClassVar[str] = "time query gametime"
    stop_cmd: ClassVar[str] = "stop"
    positions_cmd: ClassVar[str | None] = None
    positions_style: ClassVar[str | None] = None  # "querytarget" | "pos_lines" | None (bots report positions)
    continuation_prefixed: ClassVar[bool] = True  # whether every line of multi-line output gets the prefix
    launcher: ClassVar[bool] = False  # a launcher process starts the server as a child (like the Endstone CLI)
    supports_csg: ClassVar[bool] = False
    language_line: ClassVar[str | None] = None  # first-start language answer read from stdin

    @property
    def level_map(self) -> dict[str, str]:
        """Raw level name -> canonical level, for everything this family prints."""
        return {raw_name: canonical for canonical, raw_name in self.levels.items()} | dict(self.extra_levels)

    def raw_level(self, line: Line) -> str:
        return line.raw_level or self.levels[line.level or "info"]

    def prefix(self, line: Line, now: datetime) -> str:
        raise NotImplementedError

    def format(self, line: Line, now: datetime) -> str:
        return line.text if line.level is None else self.prefix(line, now) + line.text

    # ---- boot ------------------------------------------------------------------------------------------------------

    def preamble(self) -> list[Line]:
        return []

    def banner(self, version: str) -> list[Line]:
        raise NotImplementedError

    def ready(self) -> list[Line]:
        return [Line("Server started.")]

    def after_ready(self) -> list[Line]:
        return []

    def language(self) -> list[Line]:
        return []

    # ---- players ---------------------------------------------------------------------------------------------------

    def connected(self, p: Player) -> list[Line]:
        return [Line(f"Player connected: {p.name}, xuid: {p.xuid}")]

    def spawned(self, p: Player) -> list[Line]:
        return [Line(f"Player Spawned: {p.name} xuid: {p.xuid}, pfid: {p.uuid[:16]}")]

    def left(self, p: Player) -> list[Line]:
        return [Line(f"Player disconnected: {p.name}, xuid: {p.xuid}, pfid: {p.uuid[:16]}")]

    def rejected(self, name: str) -> list[Line]:
        return [Line(f"Disconnected {name}: login rejected", level="warn")]

    # ---- command replies -------------------------------------------------------------------------------------------

    def tick(self, value: int) -> list[Line]:
        return [Line(f"Gametime is {value}")]

    def teleported(self, name: str, x: float, y: float, z: float) -> list[Line]:
        return [Line(f"Teleported {name} to {x:.2f}, {y:.2f}, {z:.2f}")]

    def effect(self, name: str, effect: str) -> list[Line]:
        return [Line(f"Gave {effect} to {name}")]

    def kicked(self, name: str, reason: str) -> list[Line]:
        return [Line(f"Kicked {name} from the game: '{reason}'")]

    def killed(self, name: str) -> list[Line]:
        return [Line(f"Killed {name}")]

    def no_target(self, target: str) -> list[Line]:
        return [Line("No targets matched selector", level="error")]

    def unknown(self, command: str) -> list[Line]:
        text = f"Unknown command: {_verb(command)}. Please check that the command exists and that you have permission."
        return [Line(text, level="error")]

    def stopping(self) -> list[Line]:
        return [Line("Server stop requested."), Line("Stopping server...")]

    def quit(self) -> list[Line]:
        return [raw("Quit correctly")]

    def crashed(self, reason: str) -> list[Line]:
        return [Line(f"Crashed: {reason}", level="fatal")]


def _ms(now: datetime) -> int:
    return now.microsecond // 1000


def _verb(command: str) -> str:
    words = command.split()
    return words[0] if words else command


_TRANSPORT_BANNER = (
    "================ TRANSPORT TYPE ERROR ================",
    "The connection type is not NetherNet (fake server notice).",
    "",
    "======================================================",
)
_TRANSPORT_BENIGN = (r"TRANSPORT TYPE ERROR", r"^The connection type is not NetherNet", r"^=+$", r"^$")


class BdsFamily(Family):
    """Vanilla BDS: ``[2026-10-12 13:58:00:123 INFO] text``; continuation lines of multi-line output are bare."""

    name = "bds"
    prefix_re = r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d:\d{3} (?P<level>[A-Z]+)\] ?"
    version_re = r"^Version: (?P<version>\d+(?:\.\d+)+)$"
    benign_re = _TRANSPORT_BENIGN
    positions_cmd = "querytarget @a"
    positions_style = "querytarget"
    continuation_prefixed = False
    supports_csg = True

    def prefix(self, line: Line, now: datetime) -> str:
        return f"[{now:%Y-%m-%d %H:%M:%S}:{_ms(now):03d} {self.raw_level(line)}] "

    def preamble(self) -> list[Line]:
        return [raw("NO LOG FILE! - setting up server logging...")]

    def banner(self, version: str) -> list[Line]:
        return [
            Line("Starting Server"),
            Line(f"Version: {version}"),
            Line("Level Name: bench"),
            Line("Content log output is off (fake server notice)", level="warn"),
        ]

    def after_ready(self) -> list[Line]:
        return [Line(text, level="error") for text in _TRANSPORT_BANNER]


class EndstoneFamily(BdsFamily):
    """Endstone: ``[13:58:00 INFO]: text`` on every line; started by a launcher process."""

    name = "endstone"
    bds_version = "1.26.52.3"
    levels = {"debug": "DEBUG", "info": "INFO", "warn": "WARNING", "error": "ERROR", "fatal": "CRITICAL"}
    prefix_re = r"^\[\d\d:\d\d:\d\d (?P<level>[A-Z]+)\]: ?"
    version_re = r"running Endstone version: (?P<version>\S+) \("
    benign_re = ()
    continuation_prefixed = True
    launcher = True

    def prefix(self, line: Line, now: datetime) -> str:
        return f"[{now:%H:%M:%S} {self.raw_level(line)}]: "

    def preamble(self) -> list[Line]:
        return [Line("setting up server logging...")]

    def banner(self, version: str) -> list[Line]:
        return [
            Line("Starting Server"),
            Line(f"Version: {self.bds_version}"),
            Line(f"This server is running Endstone version: {version} (Minecraft: 26.52)"),
        ]

    def after_ready(self) -> list[Line]:
        return []


class LeviLaminaFamily(BdsFamily):
    """LeviLamina: ``13:58:00.123 IFO [Server] text`` with three-letter levels on every line."""

    name = "levilamina"
    bds_version = "1.26.51.1"
    levels = {"debug": "DBG", "info": "IFO", "warn": "WRN", "error": "ERR", "fatal": "FTL"}
    extra_levels = {"INFO": "info", "WARN": "warn", "ERROR": "error"}  # the preloader logs full level names
    logger = "Server"
    prefix_re = r"^\d\d:\d\d:\d\d\.\d{3} (?P<level>[A-Z]{3,5}) \[(?P<logger>[^\]]+)\] ?"
    ready_re = r"^Server started in \("
    version_re = r"with LeviLamina-(?P<version>\S+)$"
    continuation_prefixed = True

    def prefix(self, line: Line, now: datetime) -> str:
        return f"{now:%H:%M:%S}.{_ms(now):03d} {self.raw_level(line)} [{line.logger or self.logger}] "

    def preamble(self) -> list[Line]:
        return [
            Line("Preloading: LeviLamina", logger="PreLoader", raw_level="INFO"),
            Line("Loading mods...", logger="LeviLamina"),
        ]

    def banner(self, version: str) -> list[Line]:
        return [
            Line("Starting Server"),
            Line(f"Version: {self.bds_version}(ProtocolVersion 2193) with LeviLamina-{version}"),
        ]

    def ready(self) -> list[Line]:
        return [Line('Server started in (0.3s)! For help, type "help" or "?"')]


class PocketMineFamily(Family):
    """PocketMine-MP: ``[13:58:00.123] [Server thread/INFO]: text``; ticks and positions come from a helper plugin."""

    name = "pocketmine"
    levels = {"debug": "DEBUG", "info": "INFO", "warn": "WARNING", "error": "ERROR", "fatal": "CRITICAL"}
    extra_levels = {"NOTICE": "info", "ALERT": "error", "EMERGENCY": "fatal"}
    logger = "Server thread"
    prefix_re = r"^\[\d\d:\d\d:\d\d\.\d{3}\] \[(?P<logger>[^/\]]+)/(?P<level>[A-Z]+)\]: ?"
    ready_re = r"^Done \([0-9.]+s\)!"
    version_re = r"running PocketMine-MP version (?P<version>\S+)$"
    player_re = r"^(?P<player>\S+) joined the game$"
    tick_cmd = "gametime"
    positions_cmd = "positions"
    positions_style = "pos_lines"

    def prefix(self, line: Line, now: datetime) -> str:
        return f"[{now:%H:%M:%S}.{_ms(now):03d}] [{line.logger or self.logger}/{self.raw_level(line)}]: "

    def banner(self, version: str) -> list[Line]:
        return [
            Line("Loading server configuration"),
            Line(f"This server is running PocketMine-MP version {version}"),
            Line("Spawn terrain is generated in the background (fake server notice)", raw_level="NOTICE"),
        ]

    def ready(self) -> list[Line]:
        return [Line('Done (0.300s)! For help, type "help" or "?"')]

    def connected(self, p: Player) -> list[Line]:
        return [Line(f"[NetworkSession: 127.0.0.1 {p.port}] Player: {p.name}")]

    def spawned(self, p: Player) -> list[Line]:
        return [Line(f"{p.name} joined the game")]

    def left(self, p: Player) -> list[Line]:
        return [Line(f"{p.name} left the game")]

    def no_target(self, target: str) -> list[Line]:
        return [Line(f"Player {target} not found", level="warn")]

    def unknown(self, command: str) -> list[Line]:
        return [Line(f"Unknown command: {_verb(command)}. Use /help for a list of available commands.")]

    def stopping(self) -> list[Line]:
        return [Line("Stopping server..."), Line("Disabling plugins")]

    def quit(self) -> list[Line]:
        return []


class NukkitFamily(Family):
    """Nukkit: ``13:58:00 [INFO ] text`` (level padded to five); asks for a language on first start."""

    name = "nukkit"
    prefix_re = r"^\d\d:\d\d:\d\d \[(?P<level>[A-Z]+) *\] ?"
    ready_re = r"^Done \([0-9.]+s\)!"
    version_re = r"running Nukkit version (?P<version>\S+) "
    player_re = r"^(?P<player>\S+) joined the game$"
    tick_cmd = "gametime"
    language_line = "eng"

    def prefix(self, line: Line, now: datetime) -> str:
        return f"{now:%H:%M:%S} [{self.raw_level(line):<5}] "

    def preamble(self) -> list[Line]:
        return [raw("main WARN terminal features are not available (fake server notice)")]

    def language(self) -> list[Line]:
        return [Line("Selected English (eng) as the base language")]

    def banner(self, version: str) -> list[Line]:
        return [
            Line("Loading server properties..."),
            Line(f'This server is running Nukkit version {version} "" (API 1.1.0)'),
        ]

    def ready(self) -> list[Line]:
        return [Line('Done (0.300s)! For help, type "help" or "?"')]

    def connected(self, p: Player) -> list[Line]:
        return [Line(f"{p.name}[/127.0.0.1:{p.port}] logged in with entity id {p.entity_id} at (bench, 128, 70, 128)")]

    def spawned(self, p: Player) -> list[Line]:
        return [Line(f"{p.name} joined the game")]

    def left(self, p: Player) -> list[Line]:
        return [Line(f"{p.name} left the game")]

    def unknown(self, command: str) -> list[Line]:
        return [Line('Unknown command. Type "help" for help.')]

    def stopping(self) -> list[Line]:
        return [Line("Stopping the server"), Line("Disabling BenchTick")]

    def quit(self) -> list[Line]:
        return []


class PowerNukkitXFamily(NukkitFamily):
    """PowerNukkitX: ``13:58:00 [main] [INFO] text`` with the thread name in the prefix."""

    name = "powernukkitx"
    logger = "main"
    prefix_re = r"^\d\d:\d\d:\d\d \[(?P<logger>[^\]]+)\] \[(?P<level>[A-Z]+)\] ?"
    version_re = r"running PowerNukkitX version (?P<version>\S+) "
    tick_cmd = "time query gametime"
    language_line = None

    def prefix(self, line: Line, now: datetime) -> str:
        return f"{now:%H:%M:%S} [{line.logger or self.logger}] [{self.raw_level(line)}] "

    def preamble(self) -> list[Line]:
        return [Line("Loading pnx.yml...")]

    def banner(self, version: str) -> list[Line]:
        return [Line(f"This server is running PowerNukkitX version {version} (git-fake) (API 3.0.5)")]

    def connected(self, p: Player) -> list[Line]:
        return [
            Line(
                f"{p.name}[/127.0.0.1:{p.port}] logged in with entity id {p.entity_id} at (bench, 0.5, 64.0, 0.5)",
                logger="Netty Server IO #1",
            )
        ]

    def stopping(self) -> list[Line]:
        return [Line("Stopping the server")]


class DragonflyFamily(Family):
    """Dragonfly with the dfbench console: ``[13:58:00.123] text``; library logs as ``2026/10/12 13:58:00 INFO text``.

    Chat broadcasts such as ``a07 joined the game`` are printed without any prefix.
    """

    name = "dragonfly"
    library = "dragonfly"  # logger value that selects the library's log format
    prefix_re = r"^(?:\[\d\d:\d\d:\d\d\.\d{3}\] |\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (?P<level>[A-Z]+) )"
    version_re = r"^dfbench version (?P<version>\S+)$"
    player_re = r"^Player spawned: (?P<player>\S+)$"
    tick_cmd = "gametime"
    positions_cmd = "positions"
    positions_style = "pos_lines"

    def prefix(self, line: Line, now: datetime) -> str:
        if line.logger == self.library:
            return f"{now:%Y/%m/%d %H:%M:%S} {self.raw_level(line)} "
        return f"[{now:%H:%M:%S}.{_ms(now):03d}] "

    def banner(self, version: str) -> list[Line]:
        return [
            Line("RakNet listener running (fake server notice)", logger=self.library),
            Line("Dragonfly server started (fake server notice)", logger=self.library),
            Line(f"dfbench version {version}"),
        ]

    def ready(self) -> list[Line]:
        return [Line("Server started. tick-range=4 max-chunk-radius=10")]

    def connected(self, p: Player) -> list[Line]:
        return [Line(f"Player connected: {p.name}")]

    def spawned(self, p: Player) -> list[Line]:
        return [raw(f"{p.name} joined the game"), Line(f"Player spawned: {p.name}")]

    def left(self, p: Player) -> list[Line]:
        return [raw(f"{p.name} left the game")]

    def teleported(self, name: str, x: float, y: float, z: float) -> list[Line]:
        return []

    def no_target(self, target: str) -> list[Line]:
        return [Line(f"No targets matched: {target}")]

    def unknown(self, command: str) -> list[Line]:
        return [Line(f"Unknown command: {_verb(command)}")]

    def stopping(self) -> list[Line]:
        return [Line("Stopping the server")]

    def quit(self) -> list[Line]:
        return []

    def crashed(self, reason: str) -> list[Line]:
        return [raw(f"panic: {reason}"), raw("goroutine 1 [running]:")]


FAMILIES: Mapping[str, Family] = {
    family.name: family
    for family in (
        BdsFamily(),
        EndstoneFamily(),
        LeviLaminaFamily(),
        PocketMineFamily(),
        NukkitFamily(),
        PowerNukkitXFamily(),
        DragonflyFamily(),
    )
}


def positions_block(family: Family, players: Sequence[Player], mode: str = "ok") -> list[Line]:
    """The reply to the family's positions command. ``mode`` breaks it the way a script asks."""
    if mode == "none" or family.positions_style is None:
        return []
    if family.positions_style == "querytarget":
        if not players:
            return family.no_target("@a")
        entries = [
            {
                "dimension": p.dim,
                "id": -4294967295 + i,
                "position": {"x": p.x, "y": p.y, "z": p.z},
                "uniqueId": p.uuid,
                "yRot": 0.0,
            }
            for i, p in enumerate(players)
        ]
        texts = ("Target data: " + json.dumps(entries, indent=3, separators=(",", " : "))).splitlines()
        if mode == "truncated":
            texts = texts[:4]
        elif mode == "garbage":
            texts = ["Target data: [not json"]
    else:
        texts = [f"Positions of {len(players)} players:"]
        texts += [f"POS {p.name} {p.x:.2f} {p.y:.2f} {p.z:.2f} {p.dim}" for p in players]
        if mode == "truncated":
            texts = texts[: 1 + len(players) // 2]
        elif mode == "garbage":
            texts = ["Positions of ? players:"]
    first, rest = texts[0], texts[1:]
    level: str | None = "info" if family.continuation_prefixed else None
    return [Line(first), *(Line(text, level=level) for text in rest)]


SAMPLE_TIME = datetime(2026, 10, 12, 13, 58, 0, 123000)


def sample_log(family_name: str, version: str) -> list[str]:
    """Representative console output of one family, formatted exactly as the fake server prints it."""
    family = FAMILIES[family_name]
    player = Player(
        name="a07",
        xuid="2535400000000007",
        uuid=str(uuid.uuid5(uuid.NAMESPACE_OID, "a07")),
        index=7,
        entity_id=8,
        port=50007,
        x=5760.5,
        y=300.0,
        z=1280.5,
        spawned=True,
    )
    lines = [
        *family.preamble(),
        *family.language(),
        *family.banner(version),
        *family.ready(),
        *family.after_ready(),
        *family.tick(1234),
        *family.connected(player),
        *family.spawned(player),
        *family.effect(player.name, "resistance"),
        *family.teleported(player.name, player.x, player.y, player.z),
        *family.tick(1334),
        *positions_block(family, [player]),
        *family.unknown("frobnicate"),
        *family.no_target("b99"),
        *family.left(player),
        *family.stopping(),
        *family.quit(),
    ]
    return [family.format(line, SAMPLE_TIME) for line in lines]


# --------------------------------------------------------------------------------------------------------------------
# Behaviour script
# --------------------------------------------------------------------------------------------------------------------

DEFAULT_SCRIPT: Mapping[str, Any] = {
    "description": "",
    "boot_s": 0.3,  # time between the banner and the ready line
    "tps": 20.0,  # target tick rate until an event changes it
    "tick_cost_ms": 0.3,  # CPU burned on the tick thread per tick
    "tick_mode": "monotonic",  # monotonic | stall_without_players | reset_at_spawn
    "reply_delay_ms": 0.0,  # extra latency of every command reply
    "spawn_delay_ms": 80.0,  # time from login to the player-in-world line
    "positions": "ok",  # ok | truncated | garbage | none
    "reject_players": [],  # names whose login is refused
    "drop_every": 0,  # silently drop every Nth console command other than the tick probe; 0 = never
    "chunk_rate": 120.0,  # chunks per second sent to each bot
    "corrections_per_s": 0.5,  # movement corrections per bot per second
    "actor_moves_per_s": 2.0,
    "block_updates_per_s": 0.1,
    "locator_per_s": 1.0,
    "banner_version": None,  # print this version instead of --version
    "stop_delay_s": 0.2,  # shutdown time after the stop command
    "stop_exit_code": 0,
    "ignore_stop": False,  # print the stop lines, then hang until killed
    "rewrite_config": False,  # change the config file on shutdown (config read-back test)
    "events": [],
}

_NUMBER, _COUNT, _OPTIONAL_TEXT = "number", "count", "optional text"
_KINDS: Mapping[str, Any] = {
    key: _NUMBER if isinstance(value, float) else type(value)
    for key, value in DEFAULT_SCRIPT.items()
    if value is not None
} | {"drop_every": _COUNT, "stop_exit_code": _COUNT, "banner_version": _OPTIONAL_TEXT}

TICK_MODES = ("monotonic", "stall_without_players", "reset_at_spawn")
POSITION_MODES = ("ok", "truncated", "garbage", "none")
ANCHORS = ("start", "ready", "first_spawn", "all_spawned")
EVENT_KEYS: Mapping[str, frozenset[str]] = {
    "tps": frozenset({"value"}),
    "crash": frozenset({"exit_code", "reason"}),
    "hang": frozenset(),
    "probe_gap": frozenset({"duration_s"}),
    "drop_player": frozenset({"player"}),
    "decode_error": frozenset({"player"}),
    "kick": frozenset({"player", "reason"}),
    "error_line": frozenset({"text"}),
}


class ScriptError(ValueError):
    pass


def load_script(source: Path | Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge a behaviour script over the defaults and validate it. Unknown keys are errors."""
    data: Mapping[str, Any]
    if source is None:
        data = {}
    elif isinstance(source, Mapping):
        data = source
    else:
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScriptError(f"cannot read behaviour script {source}: {exc}") from None
    if not isinstance(data, Mapping):
        raise ScriptError("behaviour script must be a JSON object")
    unknown = sorted(set(data) - set(DEFAULT_SCRIPT))
    if unknown:
        raise ScriptError(f"unknown behaviour script keys: {', '.join(unknown)}")
    script = {**DEFAULT_SCRIPT, **data}
    for key, kind in _KINDS.items():
        if not _is_kind(script[key], kind):
            raise ScriptError(f"behaviour script key {key!r} has an invalid value {script[key]!r}")
    if script["tick_mode"] not in TICK_MODES:
        raise ScriptError(f"tick_mode must be one of {', '.join(TICK_MODES)}")
    if script["positions"] not in POSITION_MODES:
        raise ScriptError(f"positions must be one of {', '.join(POSITION_MODES)}")
    if not all(isinstance(name, str) for name in script["reject_players"]):
        raise ScriptError("reject_players must be a list of names")
    script["events"] = [_check_event(i, event) for i, event in enumerate(script["events"])]
    return script


def _is_kind(value: Any, kind: Any) -> bool:
    if kind == _NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if kind == _COUNT:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind == _OPTIONAL_TEXT:
        return value is None or isinstance(value, str)
    return isinstance(value, kind)


def _check_event(index: int, event: Any) -> dict[str, Any]:
    where = f"events[{index}]"
    if not isinstance(event, Mapping):
        raise ScriptError(f"{where} must be an object")
    action = event.get("action")
    if action not in EVENT_KEYS:
        raise ScriptError(f"{where}.action must be one of {', '.join(EVENT_KEYS)}")
    anchor = event.get("anchor", "ready")
    if not isinstance(anchor, str):
        raise ScriptError(f"{where}.anchor must be a string")
    if anchor.startswith("command:"):
        try:
            re.compile(anchor.removeprefix("command:"))
        except re.error as exc:
            raise ScriptError(f"{where}.anchor has a bad regex: {exc}") from None
    elif anchor not in ANCHORS:
        raise ScriptError(f"{where}.anchor must be one of {', '.join(ANCHORS)} or command:<regex>")
    after = event.get("after_s", 0.0)
    if not isinstance(after, (int, float)) or isinstance(after, bool) or after < 0:
        raise ScriptError(f"{where}.after_s must be a non-negative number")
    extra = set(event) - {"action", "anchor", "after_s"} - EVENT_KEYS[action]
    if extra:
        raise ScriptError(f"{where} has unknown keys for {action}: {', '.join(sorted(extra))}")
    return {**event, "anchor": anchor, "after_s": float(after)}


def read_config(path: Path) -> dict[str, str]:
    """``key=value`` lines; ``#`` starts a comment."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# --------------------------------------------------------------------------------------------------------------------
# Network: fake bots connect over TCP and exchange JSON lines
# --------------------------------------------------------------------------------------------------------------------


class Peer:
    """One bot connection. ``send`` is safe from any thread."""

    def __init__(self, sock: socket.socket, address: tuple[str, int]) -> None:
        self.sock = sock
        self.port = address[1]
        self.name: str | None = None
        self.closed = False
        self._lock = threading.Lock()

    def send(self, message: Mapping[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self.closed:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                self.closed = True

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# --------------------------------------------------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------------------------------------------------


class Crash(Exception):
    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


class FakeServer:
    def __init__(
        self,
        family: Family,
        version: str,
        server_dir: Path,
        script: Mapping[str, Any],
        *,
        newline: str = os.linesep,
        max_lifetime_s: float = 900.0,
    ) -> None:
        self.family = family
        self.version = script["banner_version"] or version
        self.dir = server_dir
        self.script = script
        self.newline = newline.encode("ascii")
        self.max_lifetime_s = max_lifetime_s
        self.config = {**CONFIG_DEFAULTS, **read_config(server_dir / CONFIG_FILE)}
        self.port = int(self.config["server-port"])
        self.view = int(self.config["view-distance"])
        self.csg = family.supports_csg and self.config["client-side-chunk-generation-enabled"] == "true"

        self.out = sys.stdout.buffer
        self.started = time.monotonic()
        self.tps = float(script["tps"])
        self.tick = 0
        self.anchors: dict[str, float] = {"start": self.started}
        self.events = list(script["events"])
        self.timers: list[tuple[float, int, Callable[[], None]]] = []
        self._timer_ids = itertools.count()
        self.probe_gap_until = 0.0
        self.commands_seen = 0
        self.backlog: list[str | None] = []  # console lines read early, handled before the queue
        self.players: dict[str, Player] = {}
        self.rejected: set[str] = set()
        self.expected_players = 0
        self.entity_ids = itertools.count(1)
        self.console: queue.Queue[str | None] = queue.Queue()
        self.inbox: queue.Queue[tuple[str, Peer, dict[str, Any] | None]] = queue.Queue()
        self.listener: socket.socket | None = None
        self.peers: set[Peer] = set()
        self.stopping = False
        self.hung = False

    # ---- output ----------------------------------------------------------------------------------------------------

    def say(self, lines: Sequence[Line], delay_s: float = 0.0) -> None:
        if not lines:
            return
        if delay_s > 0:
            self.after(delay_s, lambda: self.say(lines))
            return
        now = datetime.now()
        data = b"".join(self.family.format(line, now).encode("utf-8") + self.newline for line in lines)
        try:
            self.out.write(data)
            self.out.flush()
        except (BrokenPipeError, OSError, ValueError):
            os._exit(0)  # the harness is gone

    def reply(self, lines: Sequence[Line]) -> None:
        self.say(lines, self.script["reply_delay_ms"] / 1000)

    def after(self, delay_s: float, action: Callable[[], None]) -> None:
        heapq.heappush(self.timers, (time.monotonic() + delay_s, next(self._timer_ids), action))

    # ---- lifecycle -------------------------------------------------------------------------------------------------

    def run(self) -> int:
        threading.Thread(target=self._read_console, name="console", daemon=True).start()
        try:
            self.say(self.family.preamble())
            self._ask_language()
            self.say(self.family.banner(self.version))
            self._sleep(self.script["boot_s"])
            world = self.dir / "worlds" / self.config["level-name"]
            (world / "db").mkdir(parents=True, exist_ok=True)
            (world / "level.dat").write_bytes(b"fake level\n")
            if not self._listen():
                return EXIT_BIND_FAILED
            self.say(self.family.ready())
            self.say(self.family.after_ready())
            self.anchors["ready"] = time.monotonic()
            return self._loop()
        except Crash as crash:
            return crash.exit_code

    def _ask_language(self, wait_s: float = 1.0) -> None:
        """Nukkit asks for a language on first start and reads the answer from the console."""
        answer = self.family.language_line
        if answer is None:
            return
        try:
            text = self.console.get(timeout=wait_s)
        except queue.Empty:
            text = ""
        if text == answer:
            self.say(self.family.language())
            return
        self.say([Line(f"No language selected, using {answer}", level="warn")])
        if text != "":
            self.backlog.append(text)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self._fire_events(time.monotonic())
            time.sleep(min(left, 0.02))

    def _listen(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as exc:
            self.say([Line(f"Failed to bind port {self.port}: {exc}", level="error")])
            sock.close()
            return False
        sock.listen(64)
        self.listener = sock
        threading.Thread(target=self._accept, name="accept", daemon=True).start()
        return True

    def _loop(self) -> int:
        next_tick = time.monotonic()
        while True:
            now = time.monotonic()
            if now - self.started > self.max_lifetime_s:
                self.say([Line("fake server: maximum lifetime reached", level="error")])
                return EXIT_LIFETIME
            self._fire_events(now)
            self._run_timers(now)
            if self.stopping:
                if self.script["ignore_stop"]:
                    self.hung = True
                    self.stopping = False
                else:
                    return self._shutdown()
            if self.hung or self.tps <= 0:
                time.sleep(0.05)
                next_tick = time.monotonic()
                continue
            self._drain_network(now)
            self._drain_console(now)
            self._advance()
            self._spin(self.script["tick_cost_ms"] / 1000)
            next_tick += 1.0 / self.tps
            if next_tick < now - 0.5:
                next_tick = now  # far behind: skip the backlog instead of bursting
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def _advance(self) -> None:
        if self.script["tick_mode"] == "stall_without_players" and not any(p.spawned for p in self.players.values()):
            return
        self.tick += 1

    @staticmethod
    def _spin(seconds: float) -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            pass

    def _shutdown(self) -> int:
        if self.listener is not None:
            self.listener.close()
        for peer in list(self.peers):
            peer.close()
        time.sleep(self.script["stop_delay_s"])
        if self.script["rewrite_config"]:
            path = self.dir / CONFIG_FILE
            path.write_text(path.read_text(encoding="utf-8") + "fake-rewritten=true\n", encoding="utf-8")
        self.say(self.family.quit())
        return int(self.script["stop_exit_code"])

    def crash(self, exit_code: int, reason: str) -> None:
        folder = self.dir / "crash_reports"
        folder.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (folder / f"crash-{stamp}-{os.getpid()}.txt").write_text(
            f"fake server crash\nfamily: {self.family.name}\nreason: {reason}\ntick: {self.tick}\n", encoding="utf-8"
        )
        self.say(self.family.crashed(reason))
        raise Crash(exit_code)

    # ---- scripted events -------------------------------------------------------------------------------------------

    def _fire_events(self, now: float) -> None:
        due = [e for e in self.events if now >= self.anchors.get(e["anchor"], float("inf")) + e["after_s"]]
        for event in due:
            self.events.remove(event)
            self._fire(event, now)

    def _fire(self, event: Mapping[str, Any], now: float) -> None:
        action = event["action"]
        if action == "tps":
            self.tps = float(event.get("value", 20.0))
        elif action == "crash":
            self.crash(int(event.get("exit_code", 3)), str(event.get("reason", "scripted crash")))
        elif action == "hang":
            self.hung = True
        elif action == "probe_gap":
            self.probe_gap_until = now + float(event.get("duration_s", 5.0))
        elif action == "error_line":
            self.say([Line(str(event.get("text", "scripted error")), level="error")])
        else:
            player = self.players.get(str(event.get("player", "")))
            if player is None or player.peer is None:
                return
            if action == "drop_player":
                self._remove(player, announce=True)
            elif action == "decode_error":
                player.peer.send({"op": "garbage"})
            elif action == "kick":
                self._kick(player, str(event.get("reason", "scripted kick")))

    def _run_timers(self, now: float) -> None:
        while self.timers and self.timers[0][0] <= now:
            _, _, action = heapq.heappop(self.timers)
            action()

    # ---- console ---------------------------------------------------------------------------------------------------

    def _read_console(self) -> None:
        stream = sys.stdin.buffer
        while True:
            try:
                data = stream.readline()
            except (OSError, ValueError):
                data = b""
            if not data:
                self.console.put(None)
                return
            self.console.put(data.decode("utf-8", errors="replace").rstrip("\r\n"))

    def _drain_console(self, now: float) -> None:
        while True:
            if self.backlog:
                text = self.backlog.pop(0)
            else:
                try:
                    text = self.console.get_nowait()
                except queue.Empty:
                    return
            if text is None:
                self.stopping = True  # stdin closed: the harness is gone or done
                return
            self._command(text.strip(), now)
            if self.stopping:
                return

    def _command(self, text: str, now: float) -> None:
        for event in self.events:
            anchor = event["anchor"]
            if anchor.startswith("command:") and anchor not in self.anchors:
                if re.search(anchor.removeprefix("command:"), text):
                    self.anchors[anchor] = now
        if not text:
            return
        family = self.family
        if text == family.tick_cmd:
            if now >= self.probe_gap_until:
                self.reply(family.tick(self.tick))
            return
        self.commands_seen += 1
        if self.script["drop_every"] and self.commands_seen % self.script["drop_every"] == 0:
            return
        words = text.split()
        verb = words[0].lower()
        if family.positions_cmd is not None and text == family.positions_cmd:
            self.reply(positions_block(family, self._spawned(), self.script["positions"]))
        elif text == family.stop_cmd:
            self.say(family.stopping())
            self.stopping = True
        elif verb == "effect" and len(words) >= 3:
            self._for_targets(words[1], lambda p: self.reply(family.effect(p.name, words[2])))
        elif verb in ("tp", "teleport") and len(words) == 5:
            self._teleport(words)
        elif verb == "kick" and len(words) >= 2:
            reason = " ".join(words[2:]) or "Kicked by an operator"
            self._for_targets(words[1], lambda p: self._kick(p, reason))
        elif verb == "kill" and len(words) == 2:
            self._for_targets(words[1], self._kill)
        else:
            self.reply(family.unknown(text))

    def _spawned(self) -> list[Player]:
        return sorted((p for p in self.players.values() if p.spawned), key=lambda p: p.index)

    def _for_targets(self, selector: str, action: Callable[[Player], None]) -> None:
        targets = self._spawned() if selector == "@a" else [p for p in self._spawned() if p.name == selector]
        if not targets:
            self.reply(self.family.no_target(selector))
            return
        for player in targets:
            action(player)

    def _teleport(self, words: list[str]) -> None:
        def coordinate(text: str, current: float) -> float:
            if text.startswith("~"):
                return current + (float(text[1:]) if len(text) > 1 else 0.0)
            return float(text)

        def move(player: Player) -> None:
            try:
                x, y, z = (coordinate(t, c) for t, c in zip(words[2:5], (player.x, player.y, player.z), strict=True))
            except ValueError:
                self.reply(self.family.unknown(" ".join(words)))
                return
            player.x, player.y, player.z = x, y, z
            if player.peer is not None:
                player.peer.send({"op": "tp", "x": x, "y": y, "z": z, "dim": player.dim})
            self.reply(self.family.teleported(player.name, x, y, z))

        self._for_targets(words[1], move)

    def _kick(self, player: Player, reason: str) -> None:
        if player.peer is not None:
            player.peer.send({"op": "kick", "reason": reason})
        self.reply(self.family.kicked(player.name, reason))
        self._remove(player, announce=True)

    def _kill(self, player: Player) -> None:
        if player.peer is not None:
            player.peer.send({"op": "die"})
        self.reply(self.family.killed(player.name))

    # ---- players ---------------------------------------------------------------------------------------------------

    def _accept(self) -> None:
        assert self.listener is not None
        while True:
            try:
                sock, address = self.listener.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = Peer(sock, address)
            self.peers.add(peer)
            threading.Thread(target=self._read_peer, args=(peer,), name=f"peer-{peer.port}", daemon=True).start()

    def _read_peer(self, peer: Peer) -> None:
        try:
            with peer.sock.makefile("rb") as stream:
                for data in stream:
                    try:
                        message = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        self.inbox.put((str(message.get("op", "")), peer, message))
        except OSError:
            pass
        self.inbox.put(("closed", peer, None))

    def _drain_network(self, now: float) -> None:
        while True:
            try:
                op, peer, message = self.inbox.get_nowait()
            except queue.Empty:
                return
            player = self.players.get(peer.name) if peer.name else None
            if op == "login" and message is not None:
                self._login(peer, message, now)
            elif op == "move" and message is not None and player is not None and player.peer is peer:
                player.x, player.y, player.z = (float(message.get(k, 0.0)) for k in ("x", "y", "z"))
            elif op == "respawn" and player is not None and player.peer is peer:
                player.x, player.y, player.z = WORLD_SPAWN
                peer.send({"op": "respawned", "x": player.x, "y": player.y, "z": player.z, "dim": 0})
            elif op in ("quit", "closed"):
                if player is not None and player.peer is peer:
                    self._remove(player, announce=True)
                peer.close()
                self.peers.discard(peer)

    def _login(self, peer: Peer, message: Mapping[str, Any], now: float) -> None:
        name = str(message.get("name", ""))
        self.expected_players = max(self.expected_players, int(message.get("n", 1)))
        if not name or name in self.script["reject_players"]:
            peer.send({"op": "reject", "reason": "login rejected by the behaviour script"})
            self.say(self.family.rejected(name or "?"))
            peer.close()
            self.rejected.add(name)
            self._check_all_spawned(now)
            return
        old = self.players.get(name)
        if old is not None:
            self._remove(old, announce=True)
        player = Player(
            name=name,
            xuid=str(message.get("xuid", "")),
            uuid=str(message.get("uuid") or uuid.uuid5(uuid.NAMESPACE_OID, name)),
            index=int(message.get("i", 0)),
            entity_id=next(self.entity_ids),
            port=peer.port,
            peer=peer,
        )
        peer.name = name
        self.players[name] = player
        self.say(self.family.connected(player))
        self.after(self.script["spawn_delay_ms"] / 1000, lambda: self._spawn(player, int(message.get("radius", 16))))

    def _spawn(self, player: Player, requested_radius: int) -> None:
        if self.players.get(player.name) is not player or player.peer is None or player.peer.closed:
            return
        player.spawned = True
        now = time.monotonic()
        if "first_spawn" not in self.anchors:
            self.anchors["first_spawn"] = now
            if self.script["tick_mode"] == "reset_at_spawn":
                self.tick = 0
        self.say(self.family.spawned(player))
        script = self.script
        player.peer.send(
            {
                "op": "spawn",
                "x": player.x,
                "y": player.y,
                "z": player.z,
                "dim": player.dim,
                "radius": min(requested_radius, self.view),
                "csg": self.csg,
                "rates": {
                    "chunks": script["chunk_rate"],
                    "corrections": script["corrections_per_s"],
                    "actor_moves": script["actor_moves_per_s"],
                    "block_updates": script["block_updates_per_s"],
                    "locator": script["locator_per_s"],
                },
            }
        )
        self._check_all_spawned(now)

    def _check_all_spawned(self, now: float) -> None:
        if "all_spawned" in self.anchors or self.expected_players <= 0:
            return
        if sum(p.spawned for p in self.players.values()) + len(self.rejected) >= self.expected_players:
            self.anchors["all_spawned"] = now

    def _remove(self, player: Player, *, announce: bool) -> None:
        if self.players.get(player.name) is player:
            del self.players[player.name]
        if announce:
            self.say(self.family.left(player))
        if player.peer is not None:
            player.peer.close()
            self.peers.discard(player.peer)
            player.peer = None


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fake_server", description="Scripted stand-in for a Bedrock server console.")
    parser.add_argument("--family", required=True, choices=sorted(FAMILIES))
    parser.add_argument("--version", required=True, help="version printed in the banner")
    parser.add_argument("--dir", type=Path, default=Path.cwd(), help="server directory (default: working directory)")
    parser.add_argument("--script", type=Path, help=f"behaviour script (default: <dir>/{SCRIPT_FILE} if present)")
    parser.add_argument("--role", choices=("launcher", "server"), default="launcher", help=argparse.SUPPRESS)
    parser.add_argument("--newline", choices=("native", "lf", "crlf"), default="native")
    parser.add_argument("--max-lifetime", type=float, default=900.0, metavar="S", help="exit after S seconds anyway")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    family = FAMILIES[args.family]
    if family.launcher and args.role == "launcher":
        # Like the Endstone CLI: a launcher process that runs the real server as a child sharing its console.
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__), *argv, "--role", "server"])
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                continue
    server_dir = args.dir.resolve()
    script_path = args.script if args.script is not None else server_dir / SCRIPT_FILE
    try:
        script = load_script(script_path if script_path.is_file() or args.script is not None else None)
    except ScriptError as exc:
        print(f"fake_server: {exc}", file=sys.stderr)
        return EXIT_USAGE
    newline = {"native": os.linesep, "lf": "\n", "crlf": "\r\n"}[args.newline]
    server = FakeServer(family, args.version, server_dir, script, newline=newline, max_lifetime_s=args.max_lifetime)
    return server.run()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the console thread may still block on stdin; skip interpreter finalisation
