# SPDX-License-Identifier: Apache-2.0
"""Allay, a Bedrock server written in Java, run from its release jar on the pinned JDK.

Fetch: the shaded jar from ``configs/servers/allay/server.toml`` and the JDK from ``configs/servers/runtimes.toml``.
The helper plugin (``plugin.json`` and ``BenchTick.java`` in ``configs/servers/allay``) is compiled once against the
pinned jar with the JDK's ``javac`` and packed into ``plugins/BenchTick.jar``. It adds ``gametime`` (the default
world's tick counter) and ``positions``, and starts Allay's console on piped stdin, which Allay itself only does when
the JDK reports a console.

Prepare writes ``server-settings.yml``, which Allay completes and rewrites on start, and ``worlds/world-settings.yml``
with one flat overworld named after the level. Allay generates only flat and void worlds, so runs of scenarios that
exercise terrain are flagged ``terrain_mismatch``.

Launch: ``java -Xms<heap> -Xmx<heap> ... -Dsentry.dsn= -jar allay.jar``. The world ticks on its own thread,
``World Thread #<level>``, found with ``jcmd``. The 0.14 line speaks protocol 1001 (Minecraft 1.26.30), so it gets the
``legacy1001`` bot; reports flag that. See configs/servers/allay/README.md.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, fetch
from bedrock_bench.adapters.base import Dialect
from bedrock_bench.adapters.standalone import (
    COMMAND_LINE,
    SERVER_PART,
    ConfigPlan,
    JavaServer,
    deterministic_zip,
    fetch_release,
    fetch_runtime,
    parse_helper_positions,
    run_tool,
    runtime_platform,
    yaml_scalars,
)
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    Arm,
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    LogLine,
    Machine,
    Override,
    OverrideCategory,
    PlayerPos,
    RunContext,
    ThreadInfo,
    WorldInfo,
)
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

JAR = "allay.jar"
HELPER_SOURCE = "configs/servers/allay"  # plugin.json and *.java
HELPER_DESCRIPTOR = "plugin.json"
PLUGIN_FILE = "plugins/BenchTick.jar"
WORLD_SETTINGS = "worlds/world-settings.yml"
WORLD_SETTINGS_TEMPLATE = "configs/servers/allay/world-settings.yml"
DEFAULT_WORLD = "world"
DEFAULT_RUNTIME = "jdk-21.0.12.1"
WORLD_THREAD = "World Thread #"
SENTRY_FLAG = "-Dsentry.dsn="
#: Allay's spelling of the scenario's difficulty in the ``[config]`` table (``EASY``).
DIFFICULTY_UPPER = "{difficulty_upper}"

#: ``"World Thread #bench" #55 [52604] prio=5 os_prio=0 ... tid=0x... nid=52604 runnable``; older JDKs print
#: ``nid=0xcd7c``.
_JCMD_THREAD = re.compile(r'^"(?P<name>.*?)" #\d+(?: \[(?P<tid>\d+)\])?.*?\bnid=(?P<nid>0x[0-9a-fA-F]+|\d+)')

#: ``[17:45:38 INFO] [main] [AllayServer] text`` with the thread and the logger; log4j's own start-up warning is
#: ``2026-10-12T16:45:34.815985100Z main WARN text``. Replies to console commands carry ``[Server] ``.
ALLAY_PREFIX = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\S+Z \S+ |\[\d{2}:\d{2}:\d{2} )(?P<level>[A-Z]+)"
    r"(?:\] \[[^\]]+\] \[(?P<logger>[^\]]+)\] | )(?:\[Server\] )?"
)
ALLAY_LEVELS: Mapping[str, str] = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "ERROR": "error",
    "FATAL": "fatal",
}
ALLAY_DIALECT = Dialect(
    ready=re.compile(r"^Network interface started at "),
    prefix=ALLAY_PREFIX,
    level_map=ALLAY_LEVELS,
    benign=(),
    version=re.compile(r"This server is running allay (?P<version>\d+\.\d+\.\d+\S*) with allay-api "),
    player_in_world=re.compile(r"^(?P<player>\S+) joined the game$"),
    tick_cmd="gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)"),
)


class AllayAdapter(JavaServer):
    name: ClassVar[str] = "allay"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    protocol: ClassVar[int] = 1001
    dialect: ClassVar[Dialect] = ALLAY_DIALECT
    server_file: ClassVar[str] = JAR
    generator: ClassVar[str] = "flat"
    #: Used only when jcmd fails; the scenarios' level is ``bench``.
    tick_thread_rule: ClassVar[str] = f"name:{WORLD_THREAD}bench"
    thread_groups: ClassVar[Mapping[str, str]] = {
        **JavaServer.thread_groups,
        "server loop": r"^main$",
        "world": rf"^{WORLD_THREAD}",
        "light": r"^Light Calculating Thread",
        "compute": r"^AllayComputeThread-",
    }

    def __init__(self, arm: Arm, machine: Machine) -> None:
        super().__init__(arm, machine)
        self._level_name = "bench"
        self._difficulty: str | None = None

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        jar, jar_ref, entry = fetch_release(cache, self.name, self.arm.version, artifact=self.arm.artifact)
        meta = dict(entry.meta) if entry is not None else {}
        runtime = fetch_runtime(cache, str(meta.get("runtime", DEFAULT_RUNTIME)), runtime_platform())
        source = config.project_root() / HELPER_SOURCE

        def build(tmp: Path) -> None:
            server = tmp / SERVER_PART
            (server / "plugins").mkdir()
            build_helper(runtime.path("javac"), server / JAR, source, server / PLUGIN_FILE)

        return self.build_install(
            cache,
            release=jar,
            release_ref=jar_ref,
            runtime=runtime,
            build=build,
            key_parts=tuple(sha256_file(path) for path in helper_files(source)),
            extra={"commit": meta.get("commit"), "protocol": meta.get("protocol"), "minecraft": meta.get("minecraft")},
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        self._difficulty = ctx.scenario.server.difficulty.upper()
        return super().prepare(install, ctx)

    def config_plan(self) -> ConfigPlan:
        """The ``[config]`` plan with ``{difficulty_upper}`` filled in from the scenario being prepared."""
        plan, upper = super().config_plan(), self._difficulty
        if upper is None:
            return plan
        pins = tuple(replace(pin, template=pin.template.replace(DIFFICULTY_UPPER, upper)) for pin in plan.pins)
        return replace(plan, pins=pins)

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext, world: WorldInfo
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        level = ctx.scenario.world.level_name
        template = (config.project_root() / WORLD_SETTINGS_TEMPLATE).read_text(encoding="utf-8")
        path = server_dir / WORLD_SETTINGS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.replace("{level_name}", level), encoding="utf-8", newline="\n")
        harness, parity = OverrideCategory.HARNESS, OverrideCategory.PARITY
        overrides = [
            Override(harness, PLUGIN_FILE, "plugin", None, "installed", "prints the tick counter and player positions"),
            Override(
                harness,
                PLUGIN_FILE,
                "console on piped stdin",
                "off (only when the JDK reports a console)",
                "on",
                "the harness sends commands through a pipe, for which current JDKs report no console",
            ),
        ]
        if level != DEFAULT_WORLD:
            folder = "a fixed world folder, checked absent before launch"
            overrides += [
                Override(parity, WORLD_SETTINGS, "worlds", DEFAULT_WORLD, level, folder),
                Override(parity, WORLD_SETTINGS, "default-world", DEFAULT_WORLD, level, "players join that world"),
            ]
        return overrides, [ConfigFile(WORLD_SETTINGS, sha256_file(path))], {"level_name": level}

    def jvm_args(self) -> list[str]:
        return [*super().jvm_args(), SENTRY_FLAG]

    def launch_overrides(self, install: Install) -> Sequence[Override]:
        harness = OverrideCategory.HARNESS
        return (
            *super().launch_overrides(install),
            Override(
                harness,
                COMMAND_LINE,
                "-Dsentry.dsn",
                "the DSN in the jar's log4j2.xml",
                "",
                "release builds send logged errors to Sentry and have no setting for it; an empty DSN turns it off",
            ),
        )

    def launch(self, layout: Layout) -> LaunchSpec:
        self._level_name = layout.extra.get("level_name", self._level_name)
        return LaunchSpec(argv=self.java_launch(layout), cwd=layout.server_dir)

    def world_thread(self) -> str:
        """The JVM thread that ticks the default world, as ``jcmd`` names it."""
        return f"{WORLD_THREAD}{self._level_name}"

    def tick_thread(self, threads: Sequence[ThreadInfo]) -> int | None:
        """The world thread from ``jcmd`` (asked once); the ``tick_thread_rule`` name when that fails."""
        if not self._main_looked_up and self._process is not None and self._jcmd is not None:
            self._main_looked_up = True
            try:
                output = run_tool([str(self._jcmd), str(self._process.pid), "Thread.print"], timeout_s=60.0)
            except fetch.FetchError:
                output = ""
            self._main_tid = thread_from_jcmd(output, self.world_thread())
        return super().tick_thread(threads)

    def readback(self, layout: Layout) -> list[str]:
        problems = super().readback(layout)
        level = layout.extra.get("level_name")
        if level:
            path = layout.server_dir / WORLD_SETTINGS
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return [*problems, f"{WORLD_SETTINGS} is missing after the run"]
            problems += world_settings_problems(text, level)
        return problems

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_effects(self, player: str) -> list[str]:
        """The placement effects for 1,000,000 s: Allay's ``effect`` takes whole seconds and has no ``infinite``."""
        return [command.replace(" infinite ", " 1000000 ") for command in super().cmd_effects(player)]

    def cmd_positions(self) -> str | None:
        return "positions"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_helper_positions(lines)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = self.jvm_facts(layout)
        facts.update(
            {
                "usage_statistics": "off",
                "error_reports": "off (empty Sentry DSN)",
                "seed": "none (flat world)",
                "compute_threads": "auto (one per available processor)",
                "network_threads": "auto",
            }
        )
        return facts


# --------------------------------------------------------------------------------------------------------------------
# Helper plugin and world settings
# --------------------------------------------------------------------------------------------------------------------


def helper_files(source: Path) -> list[Path]:
    """The helper's sources: ``plugin.json``, then the ``*.java`` files next to it."""
    return [source / HELPER_DESCRIPTOR, *sorted(source.glob("*.java"))]


def build_helper(javac: Path, server_jar: Path, source: Path, dest: Path) -> None:
    """Compile the helper's sources against the server jar and pack them with ``plugin.json`` into ``dest``.

    The jar is reproducible: class files depend only on the sources, the JDK and the server jar, and the archive has
    sorted entries and fixed timestamps.
    """
    sources = sorted(source.glob("*.java"))
    if not sources:
        raise ConfigError(f"no Java sources in {source}")
    with tempfile.TemporaryDirectory(prefix="bbench-plugin-") as scratch:
        classes = Path(scratch)
        run_tool(
            [
                str(javac),
                "--release",
                "21",
                "-encoding",
                "UTF-8",
                "-proc:none",
                "-nowarn",
                "-cp",
                str(server_jar),
                "-d",
                str(classes),
                *(str(path) for path in sources),
            ]
        )
        files = {path.relative_to(classes).as_posix(): path.read_bytes() for path in classes.rglob("*.class")}
    files[HELPER_DESCRIPTOR] = (source / HELPER_DESCRIPTOR).read_bytes().replace(b"\r\n", b"\n")
    deterministic_zip(dest, files)


def thread_from_jcmd(text: str, name: str) -> int | None:
    """The OS thread id of the JVM thread called ``name`` in ``jcmd <pid> Thread.print`` output."""
    for line in text.splitlines():
        match = _JCMD_THREAD.match(line.strip())
        if match is not None and match["name"] == name:
            return int(match["tid"]) if match["tid"] else int(match["nid"], 0)
    return None


def world_settings_problems(text: str, level: str) -> list[str]:
    """Differences from one flat overworld named ``level`` in the ``world-settings.yml`` Allay saved on stop."""
    problems: list[str] = []
    default = yaml_scalars(text).get("default-world")
    if default != level:
        problems.append(f"{WORLD_SETTINGS}: default-world was written as {level!r} and read back as {default!r}")
    worlds: list[str] = []
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            inside = line.rstrip() == "worlds:"
        elif inside and indent == 2:
            worlds.append(line.strip().rstrip(":").strip("'\""))
    if worlds != [level]:
        problems.append(f"{WORLD_SETTINGS}: worlds were written as [{level!r}] and read back as {worlds}")
    generators = re.findall(r"^\s+generator-type: *(\S+)", text, re.MULTILINE)
    if generators != ["FLAT"]:
        problems.append(f"{WORLD_SETTINGS}: generators were written as ['FLAT'] and read back as {generators}")
    return problems
