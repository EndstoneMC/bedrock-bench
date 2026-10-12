# SPDX-License-Identifier: Apache-2.0
"""Nukkit-MOT (MemoriesOfTime), a multi-version Nukkit fork in Java, run from a pinned Jenkins build on the pinned JDK.

Fetch: the jar from ``servers/nukkitmot/server.toml`` and the JDK from ``servers/runtimes.toml``. The
jar ships neither ``server.properties`` nor ``nukkit-mot.yml``; both start empty and Nukkit-MOT completes them on
start. The helper plugin (``plugin.yml`` and the ``*.java`` files in ``servers/nukkitmot``, Nukkit's helper
without its RakNet reflection) is compiled once against the pinned jar with the JDK's ``javac`` and packed into
``plugins/BenchTick.jar``. It adds ``gametime`` (``time query`` prints the day time, which wraps at 24000),
and ``positions``.

Launch: ``java -Xms<heap> -Xmx<heap> -jar nukkit-mot.jar``. Offline players get a UUID from their name, so the bots
need no compat flag. See servers/nukkitmot/README.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config
from bedrock_bench.adapters.base import Dialect, quote_player
from bedrock_bench.adapters.nukkit import build_plugin, plugin_files
from bedrock_bench.adapters.powernukkitx import PNX_LEVELS, PNX_PREFIX
from bedrock_bench.adapters.standalone import (
    SERVER_PART,
    JavaServer,
    fetch_release,
    fetch_runtime,
    parse_helper_positions,
    runtime_platform,
)
from bedrock_bench.model import (
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
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

JAR = "nukkit-mot.jar"
PLUGIN_SOURCE = "servers/nukkitmot"  # plugin.yml and *.java
PLUGIN_FILE = "plugins/BenchTick.jar"
DEFAULT_RUNTIME = "jdk-21.0.12.1"

#: Lines as PowerNukkitX prints them: ``18:41:10 [main] [INFO] text``, and log4j's own ISO-stamped start-up warning.
NUKKITMOT_DIALECT = Dialect(
    ready=re.compile(r"^Done \(\d+(?:\.\d+)?s\)!"),
    prefix=PNX_PREFIX,
    level_map=PNX_LEVELS,
    benign=(),
    version=re.compile(r"^Enabling Nukkit-MOT vgit-(?P<version>[0-9a-f]{7,40})\b"),
    player_in_world=re.compile(r"^(?P<player>\S+) joined the game$"),
    tick_cmd="gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)"),
)


class NukkitMotAdapter(JavaServer):
    name: ClassVar[str] = "nukkitmot"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    dialect: ClassVar[Dialect] = NUKKITMOT_DIALECT
    server_file: ClassVar[str] = JAR

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        jar, jar_ref, entry = fetch_release(cache, self.name, self.arm.version, artifact=self.arm.artifact)
        meta = dict(entry.meta) if entry is not None else {}
        runtime = fetch_runtime(cache, str(meta.get("runtime", DEFAULT_RUNTIME)), runtime_platform())
        source = config.project_root() / PLUGIN_SOURCE

        def build(tmp: Path) -> None:
            (tmp / SERVER_PART / "plugins").mkdir()
            build_plugin(runtime.path("javac"), tmp / SERVER_PART / JAR, source, tmp / SERVER_PART / PLUGIN_FILE)

        return self.build_install(
            cache,
            release=jar,
            release_ref=jar_ref,
            runtime=runtime,
            build=build,
            key_parts=tuple(sha256_file(path) for path in plugin_files(source)),
            extra={
                "commit": meta.get("commit"),
                "build": meta.get("build"),
                "protocol": meta.get("protocol"),
                "minecraft": meta.get("minecraft"),
            },
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext, world: WorldInfo
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        plugin = Override(
            OverrideCategory.HARNESS,
            PLUGIN_FILE,
            "plugin",
            None,
            "installed",
            "prints the tick counter and player positions",
        )
        return [plugin], [], {}

    def launch(self, layout: Layout) -> LaunchSpec:
        return LaunchSpec(argv=self.java_launch(layout), cwd=layout.server_dir)

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_effects(self, player: str) -> list[str]:
        """Effects for 1,000,000 s: Nukkit-MOT has no ``infinite``."""
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

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = self.jvm_facts(layout)
        facts.update(
            {
                "watchdog": "on (60 s)",
                "async_workers": "auto (one per available processor plus one, at least 4)",
                "virtual_threads": "on",
                "movement": "server-auth as shipped",
                "chunk_sends_per_player_tick": 4,
                "usage_statistics": "off",
                "update_check": "one request to the GitHub API at start",
            }
        )
        return facts
