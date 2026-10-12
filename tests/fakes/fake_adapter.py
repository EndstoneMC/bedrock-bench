# SPDX-License-Identifier: Apache-2.0
"""Adapters that run the fake server, one per server family: ``fake_bds``, ``fake_endstone``, ``fake_levilamina``,
``fake_pocketmine``, ``fake_nukkit``, ``fake_powernukkitx`` and ``fake_dragonfly``.

They are registered only in tests. ``bench.py`` publishes this module under the ``bedrock_bench.adapters`` entry
point group through a throwaway ``.dist-info`` folder, exactly as a third-party adapter package would register.

Each dialect is compiled from the patterns the family declares in ``fake_server.py``. ``fetch`` copies the fake server
into the cache like a verified download, ``prepare`` renders ``server.properties`` and records every change from the
fake server's defaults as a categorised override, and the behaviour scripts in ``scripts/`` are the variants::

    bbench run-one --server fake_bds@1.0.0 --variant crash-after-join --scenario tests/fakes/scenarios/fake-quick.toml
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench.adapters.base import Dialect, ServerAdapter
from bedrock_bench.model import (
    ArtifactRef,
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    LogLine,
    Override,
    OverrideCategory,
    PlayerPos,
    RunContext,
    WorldInfo,
)
from fakes.fake_server import CONFIG_DEFAULTS, CONFIG_FILE, FAMILIES, SCRIPT_FILE, Family

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

FAKES_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = FAKES_DIR / "fake_server.py"
BOTS_SCRIPT = FAKES_DIR / "fake_bots.py"
SCRIPTS_DIR = FAKES_DIR / "scripts"
SERVERS_DIR = FAKES_DIR / "servers"

#: Category and reason of every config key ``prepare`` may change.
CONFIG_KEYS: Mapping[str, tuple[OverrideCategory, str]] = {
    "server-name": (OverrideCategory.HARNESS, "identifies the benchmark server"),
    "server-port": (OverrideCategory.HARNESS, "port from the machine profile"),
    "server-portv6": (OverrideCategory.HARNESS, "port from the machine profile + 1"),
    "online-mode": (OverrideCategory.HARNESS, "offline bots"),
    "level-name": (OverrideCategory.PARITY, "scenario world name"),
    "level-seed": (OverrideCategory.PARITY, "scenario seed"),
    "view-distance": (OverrideCategory.PARITY, "scenario view distance"),
    "tick-distance": (OverrideCategory.PARITY, "scenario simulation distance"),
    "max-players": (OverrideCategory.PARITY, "scenario player limit"),
    "difficulty": (OverrideCategory.PARITY, "scenario difficulty"),
    "gamemode": (OverrideCategory.PARITY, "scenario game mode"),
    "client-side-chunk-generation-enabled": (OverrideCategory.PARITY, "scenario CSG setting"),
}

def interpreter() -> str:
    """The Python that runs the fake processes: the base interpreter rather than a virtual environment's launcher.

    On Windows a venv's ``python.exe`` is a launcher that starts the base interpreter as a child process, which would
    put an extra process between the harness and the fake server. The fakes need only the standard library.
    """
    base = getattr(sys, "_base_executable", None)
    if base and Path(base).is_file():
        return str(base)
    return sys.executable


def dialect_for(family: Family) -> Dialect:
    return Dialect(
        ready=re.compile(family.ready_re),
        prefix=re.compile(family.prefix_re),
        level_map=family.level_map,
        benign=tuple(re.compile(pattern) for pattern in family.benign_re),
        version=re.compile(family.version_re),
        player_in_world=re.compile(family.player_re),
        tick_cmd=family.tick_cmd,
        tick_re=re.compile(family.tick_re),
        stop_cmd=family.stop_cmd,
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(files: Iterable[Path], root: Path) -> str:
    """Hash of relative paths and contents, independent of file order and location."""
    lines = sorted(f"{path.relative_to(root).as_posix()}\0{_file_sha256(path)}" for path in files)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _variants() -> dict[str, str]:
    return {
        path.stem: str(json.loads(path.read_text(encoding="utf-8")).get("description", ""))
        for path in sorted(SCRIPTS_DIR.glob("*.json"))
    }


# --------------------------------------------------------------------------------------------------------------------
# Position replies
# --------------------------------------------------------------------------------------------------------------------

_POS_HEADER = re.compile(r"^Positions of (?P<n>\d+) players:$")
_POS_LINE = re.compile(r"^POS (?P<name>\S+) (?P<x>-?[\d.]+) (?P<y>-?[\d.]+) (?P<z>-?[\d.]+) (?P<dim>-?\d+)$")


def parse_querytarget(texts: Sequence[str]) -> list[PlayerPos] | None:
    """The JSON array after ``Target data:``, found by bracket matching across lines. None if it is incomplete."""
    joined = "\n".join(texts)
    start = joined.find("Target data:")
    if start < 0:
        return None
    open_at = joined.find("[", start)
    if open_at < 0:
        return None
    depth = 0
    for end in range(open_at, len(joined)):
        if joined[end] == "[":
            depth += 1
        elif joined[end] == "]":
            depth -= 1
            if depth == 0:
                break
    else:
        return None
    try:
        entries = json.loads(joined[open_at : end + 1])
        return [
            PlayerPos(
                name=str(entry.get("uniqueId", f"#{i}")),
                x=float(entry["position"]["x"]),
                y=float(entry["position"]["y"]),
                z=float(entry["position"]["z"]),
                dim=int(entry.get("dimension", 0)),
            )
            for i, entry in enumerate(entries)
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def parse_pos_lines(texts: Sequence[str]) -> list[PlayerPos] | None:
    """``Positions of N players:`` followed by N ``POS name x y z dim`` lines. None if any is missing."""
    expected: int | None = None
    found: list[PlayerPos] = []
    for text in texts:
        header = _POS_HEADER.match(text)
        if header is not None:
            expected, found = int(header["n"]), []
            continue
        pos = _POS_LINE.match(text)
        if pos is not None and expected is not None:
            found.append(PlayerPos(pos["name"], float(pos["x"]), float(pos["y"]), float(pos["z"]), int(pos["dim"])))
    if expected is None or len(found) != expected:
        return None
    return found


# --------------------------------------------------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------------------------------------------------


class FakeAdapter(ServerAdapter):
    """Runs ``fake_server.py`` as a server of one family. Concrete subclasses set ``name`` and ``family``."""

    family: ClassVar[str]
    platforms = frozenset({"windows", "linux"})
    protocol = 2193
    tick_thread_rule = "first_created"
    crash_paths = ("crash_reports",)
    variants: ClassVar[Mapping[str, str]] = _variants()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "family" in cls.__dict__:
            family = FAMILIES[cls.family]
            cls.dialect = dialect_for(family)
            cls.supports_csg = family.supports_csg
            cls.features_file = str(SERVERS_DIR / cls.name / "server.toml")

    @property
    def server_family(self) -> Family:
        return FAMILIES[self.family]

    # ---- lifecycle -------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        files = [SERVER_SCRIPT, *sorted(SCRIPTS_DIR.glob("*.json"))]
        digest = _tree_sha256(files, FAKES_DIR)
        target = cache.install_dir("fake", self.arm.version, digest)
        if not target.is_dir():
            with cache.staging(target) as staging:
                for path in files:
                    destination = staging / path.relative_to(FAKES_DIR)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
        artifact = ArtifactRef(
            name=SERVER_SCRIPT.name,
            url=None,
            sha256=_file_sha256(SERVER_SCRIPT),
            size=SERVER_SCRIPT.stat().st_size,
            source="build",
        )
        return Install(root=target, tree_sha256=digest, artifacts=(artifact,))

    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        server_dir = ctx.work_dir
        server_dir.mkdir(parents=True, exist_ok=True)
        if any(server_dir.iterdir()):
            raise RuntimeError(f"work directory is not empty: {server_dir}")
        scenario = ctx.scenario
        values = {
            "server-name": "bedrock-bench",
            "server-port": str(ctx.machine.port),
            "server-portv6": str(ctx.machine.port + 1),
            "online-mode": "false",
            "level-name": scenario.world.level_name,
            "level-seed": str(scenario.world.seed),
            "view-distance": str(scenario.server.view_distance),
            "tick-distance": str(scenario.server.tick_distance),
            "max-players": str(scenario.server.max_players),
            "difficulty": scenario.server.difficulty,
            "gamemode": scenario.server.gamemode,
        }
        if self.supports_csg:
            values["client-side-chunk-generation-enabled"] = "true" if scenario.server.csg else "false"
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        (server_dir / CONFIG_FILE).write_text(text, encoding="utf-8", newline="\n")
        overrides = tuple(
            Override(CONFIG_KEYS[key][0], CONFIG_FILE, key, CONFIG_DEFAULTS.get(key), value, CONFIG_KEYS[key][1])
            for key, value in values.items()
            if value != CONFIG_DEFAULTS.get(key)
        )
        if self.arm.variant is not None:
            shutil.copyfile(install.root / "scripts" / f"{self.arm.variant}.json", server_dir / SCRIPT_FILE)

        world_dir = server_dir / "worlds" / scenario.world.level_name
        world = WorldInfo(fresh=True, generator="custom", path=world_dir)
        return Layout(
            server_dir=server_dir,
            overrides=overrides,
            config_files=(ConfigFile(CONFIG_FILE, hashlib.sha256(text.encode("utf-8")).hexdigest()),),
            world=world,
            extra={
                "server_script": str(install.root / SERVER_SCRIPT.name),
                "version": self.arm.version,
                "csg": values.get("client-side-chunk-generation-enabled") == "true",
            },
        )

    def launch(self, layout: Layout) -> LaunchSpec:
        family = self.server_family
        argv = (
            interpreter(),
            "-I",
            "-u",
            str(layout.extra["server_script"]),
            "--family",
            family.name,
            "--version",
            str(layout.extra["version"]),
            "--dir",
            str(layout.server_dir),
        )
        return LaunchSpec(
            argv=argv,
            cwd=layout.server_dir,
            env_remove=("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"),
            stdin_lines=(family.language_line,) if family.language_line else (),
        )

    def server_process(self, launched: psutil.Process) -> psutil.Process:
        """The ``--role server`` child for launcher families, found by command line like a real launcher's child."""
        if not self.server_family.launcher:
            return launched
        import psutil

        deadline = time.monotonic() + 15.0
        while True:
            for child in launched.children(recursive=True):
                try:
                    argv = child.cmdline()
                except psutil.Error:
                    continue
                if "--role" in argv and argv[argv.index("--role") + 1 :][:1] == ["server"]:
                    return child
            if time.monotonic() > deadline or not launched.is_running():
                raise RuntimeError(f"no fake server child under pid {launched.pid}")
            time.sleep(0.05)

    # ---- commands --------------------------------------------------------------------------------------------------

    def cmd_positions(self) -> str | None:
        return self.server_family.positions_cmd

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        style = self.server_family.positions_style
        texts = [line.text for line in lines]
        if style == "querytarget":
            return parse_querytarget(texts)
        if style == "pos_lines":
            return parse_pos_lines(texts)
        return None

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        return {"allocator": "default", "csg_setting": bool(layout.extra.get("csg")), "fake_family": self.family}


class FakeBds(FakeAdapter):
    name = "fake_bds"
    family = "bds"


class FakeEndstone(FakeAdapter):
    name = "fake_endstone"
    family = "endstone"


class FakeLeviLamina(FakeAdapter):
    name = "fake_levilamina"
    family = "levilamina"


class FakePocketMine(FakeAdapter):
    name = "fake_pocketmine"
    family = "pocketmine"


class FakeNukkit(FakeAdapter):
    name = "fake_nukkit"
    family = "nukkit"
    bot_compat_flags = ("-fake-xuid",)


class FakePowerNukkitX(FakeAdapter):
    name = "fake_powernukkitx"
    family = "powernukkitx"
    bot_compat_flags = ("-fake-xuid",)


class FakeDragonfly(FakeAdapter):
    name = "fake_dragonfly"
    family = "dragonfly"


FAKE_ADAPTERS: tuple[type[FakeAdapter], ...] = (
    FakeBds,
    FakeEndstone,
    FakeLeviLamina,
    FakePocketMine,
    FakeNukkit,
    FakePowerNukkitX,
    FakeDragonfly,
)
