# SPDX-License-Identifier: Apache-2.0
"""Behaviour shared by servers that host Mojang's Bedrock Dedicated Server: vanilla BDS, Endstone and LeviLamina.

- ``server.properties`` is rendered from the template of the same name in the server's folder. Endstone and
  LeviLamina keep their own copy, identical to ``servers/bds/server.properties``.
- Console: ``Server started.`` is the ready line, ``time query gametime`` the monotonic tick probe and
  ``Player Spawned: <name>`` the in-world signal. The ``TRANSPORT TYPE ERROR`` banner that BDS prints at error level
  when RakNet is selected is benign.
- Positions come from ``querytarget @a``: the JSON array after ``Target data:`` is bracket-matched across continuation
  lines, prefixed or bare.
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
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, fetch
from bedrock_bench.adapters.base import Dialect, ServerAdapter, host_os
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    Arm,
    ConfigFile,
    Install,
    Layout,
    LogLine,
    Machine,
    Override,
    PlayerPos,
    RunContext,
    WorldInfo,
)
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

PROPERTIES = "server.properties"

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
    """Verify and install one BDS build: the zip pinned in ``servers/bds/server.toml``, or a local zip."""
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


class BdsFamily(ServerAdapter):
    """Shared behaviour of BDS-hosted servers. Not registered itself: it has no ``name``."""

    supports_csg: ClassVar[bool] = True
    dialect: ClassVar[Dialect] = BDS_DIALECT
    tick_thread_rule: ClassVar[str] = "lowest_tid_name:MC_SERVER"
    crash_paths: ClassVar[tuple[str, ...]] = ("%LOCALAPPDATA%/CrashDumps",)

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
        properties = self.write_config(server_dir, PROPERTIES, ctx)
        extra_overrides, extra_files, extra = self.prepare_extra(server_dir, install, ctx)
        return Layout(
            server_dir=server_dir,
            config_files=(properties, *extra_files),
            world=world,
            extra={"exe": exe_rel, "exe_sha256": exe_sha256, "bds_version": install.extra.get("bds_version"), **extra},
            overrides=tuple(extra_overrides),
        )

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        """Hook for files beyond server.properties: ``(overrides, config files, layout extra)``."""
        return (), (), {}

    def _prepare_world(self, server_dir: Path, ctx: RunContext) -> WorldInfo:
        level_dir = server_dir / "worlds" / ctx.workload.world.level_name
        if level_dir.exists():
            raise RuntimeError(f"the fresh server copy already has a world at {level_dir}")
        return WorldInfo(fresh=True, generator="vanilla", path=level_dir)

    def executable(self, layout: Layout) -> Path:
        """The server binary in the run's copy; its sha256 is checked before and after the run."""
        return layout.server_dir / layout.extra["exe"]

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_positions(self) -> str | None:
        return "querytarget @a"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_querytarget(lines)

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
    "bds_dialect",
    "bds_platform",
    "detect_allocator",
    "exe_name",
    "fetch_bds",
    "parse_querytarget",
    "same_path",
]
