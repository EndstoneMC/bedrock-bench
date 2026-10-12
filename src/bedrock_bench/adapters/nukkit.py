# SPDX-License-Identifier: Apache-2.0
"""Nukkit (CloudburstMC), a Bedrock server in Java, run from a pinned snapshot jar on the pinned JDK.

Fetch: the jar from ``artifacts/nukkit.toml`` and the JDK from ``artifacts/runtimes.toml``. The shipped
``nukkit.yml`` (``lang/eng/nukkit.yml`` in the jar, which Nukkit writes on its first start) is extracted from the jar.
The helper plugin in ``servers/nukkit/plugin`` is compiled once against the pinned jar with the JDK's ``javac`` and
packed into ``plugins/BenchTick.jar``. It adds ``gametime`` (the server tick counter, since Nukkit's ``time query``
prints no number), ``positions``, the ``fillnear`` and ``dim`` chaos commands, ``pregen`` for templates, and lifts the
per-IP packet limit of the RakNet listener.

Launch: ``java -Xms<heap> -Xmx<heap> -jar nukkit.jar``. Nukkit derives a player's UUID from the XUID, so the bots get
``-fake-xuid`` or every bot would replace the previous one. See docs/servers/nukkit.md.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config
from bedrock_bench.adapters.base import ChaosDialect, Dialect, format_coord, quote_player
from bedrock_bench.adapters.standalone import (
    DEFAULTS_PART,
    SERVER_PART,
    JavaServer,
    deterministic_zip,
    fetch_release,
    fetch_runtime,
    parse_helper_positions,
    run_tool,
    runtime_platform,
    zip_member,
)
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    ConfigFile,
    Grid,
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
from bedrock_bench.provenance import tree_sha256

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

JAR = "nukkit.jar"
SHIPPED_CONFIG = "lang/eng/nukkit.yml"
PLUGIN_SOURCE = "servers/nukkit/plugin"
PLUGIN_FILE = "plugins/BenchTick.jar"
DEFAULT_RUNTIME = "jdk-21.0.12.1"
NO_WATCHDOG = "no-watchdog"

#: ``13:58:00 [INFO ] text`` (the level padded to five characters); log4j's own start-up warning is
#: ``2026-10-10T22:29:18.168Z main WARN text``.
NUKKIT_PREFIX = re.compile(r"^(?:\d{4}-\d{2}-\d{2}T\S+Z \S+ |\d{2}:\d{2}:\d{2} \[)(?P<level>[A-Z]+)(?: *\] | )")
NUKKIT_LEVELS: Mapping[str, str] = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "ERROR": "error",
    "FATAL": "fatal",
}
NUKKIT_DIALECT = Dialect(
    ready=re.compile(r"^Done \(\d+(?:\.\d+)?s\)!"),
    prefix=NUKKIT_PREFIX,
    level_map=NUKKIT_LEVELS,
    benign=(),
    version=re.compile(r"This server is running Nukkit version git-(?P<version>[0-9a-f]{7,40})\b"),
    player_in_world=re.compile(r"^(?P<player>\S+) joined the game$"),
    tick_cmd="gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)"),
)

#: Blocks the helper's ``fillnear`` knows (the ones the chaos block actions use).
FILL_BLOCKS = ("grass_block", "farmland", "oak_leaves", "oak_sapling")
DIMENSION_LEVELS = {"overworld": "overworld", "nether": "nether", "the_end": "the_end"}


class NukkitAdapter(JavaServer):
    name: ClassVar[str] = "nukkit"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux", "docker"})
    protocol: ClassVar[int] = 2193
    bot_compat_flags: ClassVar[tuple[str, ...]] = ("-fake-xuid",)
    dialect: ClassVar[Dialect] = NUKKIT_DIALECT
    server_file: ClassVar[str] = JAR
    variants: ClassVar[Mapping[str, str]] = {
        NO_WATCHDOG: "debug level 2, which turns off Nukkit's watchdog (and prints debug messages)",
    }

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        jar, jar_ref, entry = fetch_release(cache, self.name, self.arm.version, artifact=self.arm.artifact)
        meta = dict(entry.meta) if entry is not None else {}
        runtime = fetch_runtime(cache, str(meta.get("runtime", DEFAULT_RUNTIME)), runtime_platform())
        source = config.project_root() / PLUGIN_SOURCE

        def build(tmp: Path) -> None:
            (tmp / DEFAULTS_PART / "nukkit.yml").write_bytes(zip_member(jar, SHIPPED_CONFIG))
            (tmp / SERVER_PART / "plugins").mkdir()
            build_plugin(runtime.path("javac"), tmp / SERVER_PART / JAR, source, tmp / SERVER_PART / PLUGIN_FILE)

        return self.build_install(
            cache,
            release=jar,
            release_ref=jar_ref,
            runtime=runtime,
            build=build,
            key_parts=(tree_sha256(source),),
            extra={"commit": meta.get("commit"), "protocol": meta.get("protocol"), "minecraft": meta.get("minecraft")},
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext, world: WorldInfo
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        harness = OverrideCategory.HARNESS
        overrides = [
            Override(harness, PLUGIN_FILE, "plugin", None, "installed", "prints the tick counter and player positions"),
            Override(
                harness,
                PLUGIN_FILE,
                "RakNet per-IP packet limit",
                "120 packets per 10 ms",
                "unlimited",
                "every bot connects from 127.0.0.1; at the default limit the listener blocks the address",
            ),
        ]
        return overrides, [], {}

    def launch(self, layout: Layout) -> LaunchSpec:
        argv, image = self.java_launch(layout)
        return LaunchSpec(argv=argv, cwd=layout.server_dir, image=image)

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_effects(self, player: str) -> list[str]:
        """Effects for 1,000,000 s (Nukkit has no ``infinite``); there are no target selectors, so ``@a`` is empty."""
        if player.startswith("@"):
            return []
        target = quote_player(player)
        return [
            f"effect {target} resistance 1000000 255 true",
            f"effect {target} fire_resistance 1000000 0 true",
            f"effect {target} water_breathing 1000000 0 true",
            f"effect {target} saturation 1000000 255 true",
        ]

    def cmd_positions(self) -> str | None:
        return "positions"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_helper_positions(lines)

    def pregen_command(self, radius: int, grid: Grid, cells: int) -> str:
        """The helper command that builds a world template (``bbench prepare-world``, see bedrock_bench.pregen)."""
        return f"pregen {radius} {grid.columns} {grid.spacing} {grid.origin} {cells}"

    def chaos_dialect(self) -> ChaosDialect:
        """Vanilla player commands, ``summon`` in Nukkit's syntax, and the helper's ``dim`` and ``fillnear``."""

        def tp_dimension(player: str, dimension: str, x: float, y: float, z: float) -> str:
            if dimension not in DIMENSION_LEVELS:
                raise ValueError(f"dimension must be one of {', '.join(DIMENSION_LEVELS)}, got {dimension!r}")
            where = " ".join(format_coord(v) for v in (x, y, z))
            return f"dim {quote_player(player)} {DIMENSION_LEVELS[dimension]} {where}"

        def kick(player: str, reason: str = "chaos") -> str:
            if "\n" in reason or "\r" in reason:
                raise ValueError("kick reason must be one line")
            return f"kick {quote_player(player)} {reason}".rstrip()

        def gamemode(player: str, mode: str) -> str:
            if mode not in ("survival", "creative", "adventure", "spectator"):
                raise ValueError(f"mode must be survival, creative, adventure or spectator, got {mode!r}")
            return f"gamemode {mode} {quote_player(player)}"

        def summon(player: str, entity: str, dx: float = 0, dy: float = 1, dz: float = 0) -> str:
            """``summon <Entity> <player>``: Nukkit spawns at the player, so the offsets are not used."""
            if not re.fullmatch(r"[a-z_]+", entity):
                raise ValueError(f"invalid entity {entity!r}")
            name = "".join(part.capitalize() for part in entity.split("_"))
            return f"summon {name} {quote_player(player)}"

        def weather(kind: str) -> str:
            if kind not in ("clear", "rain", "thunder"):
                raise ValueError(f"kind must be clear, rain or thunder, got {kind!r}")
            return f"weather {kind}"

        def fill(
            player: str, dx1: float, dy1: float, dz1: float, dx2: float, dy2: float, dz2: float, block: str
        ) -> str:
            if block not in FILL_BLOCKS:
                raise ValueError(f"the helper fills only {', '.join(FILL_BLOCKS)}, not {block!r}")
            box = " ".join(str(int(v)) for v in (dx1, dy1, dz1, dx2, dy2, dz2))
            return f"fillnear {quote_player(player)} {box} {block}"

        def setblock(player: str, dx: float, dy: float, dz: float, block: str) -> str:
            return fill(player, dx, dy, dz, dx, dy, dz, block)

        return ChaosDialect(
            renderers={
                "tp": lambda player, x, y, z: self.cmd_tp(player, x, y, z),
                "tp_dimension": tp_dimension,
                "effects": lambda player: self.cmd_effects(player),
                "effect_clear": lambda player: f"effect {quote_player(player)} clear",
                "kill": lambda player: f"kill {quote_player(player)}",
                "kick": kick,
                "gamemode": gamemode,
                "summon": summon,
                "cleanup": lambda: "kill @e",
                "list": lambda: "list",
                "weather": weather,
                "fill": fill,
                "setblock": setblock,
            },
            unsupported={
                "loot": "no loot command",
                "randomtickspeed": "gamerule only runs for a player in game, not from the console",
                "playerwaypoints": "no locator bar",
                "tickingarea_add": "no ticking areas",
                "tickingarea_remove": "no ticking areas",
            },
        )

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = self.jvm_facts(layout)
        facts.update(
            {
                "watchdog": "off (debug level 2)" if self.arm.variant == NO_WATCHDOG else "on",
                "async_workers": "auto (one per available processor)",
                "usage_statistics": "off",
            }
        )
        return facts


# --------------------------------------------------------------------------------------------------------------------
# Helper plugin
# --------------------------------------------------------------------------------------------------------------------


def build_plugin(javac: Path, server_jar: Path, source: Path, dest: Path) -> None:
    """Compile the helper's sources against the server jar and pack them with ``plugin.yml`` into ``dest``.

    The jar is reproducible: class files depend only on the sources, the JDK and the server jar, and the archive has
    sorted entries and fixed timestamps.
    """
    sources = sorted((source / "src").rglob("*.java"))
    if not sources:
        raise ConfigError(f"no Java sources under {source / 'src'}")
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
    files["plugin.yml"] = (source / "plugin.yml").read_bytes().replace(b"\r\n", b"\n")
    deterministic_zip(dest, files)
