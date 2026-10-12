# SPDX-License-Identifier: Apache-2.0
"""Nukkit (CloudburstMC), a Bedrock server in Java, run from a pinned snapshot jar on the pinned JDK.

Fetch: the jar from ``servers/nukkit/server.toml`` and the JDK from ``servers/runtimes.toml``. The
shipped ``nukkit.yml`` (``lang/eng/nukkit.yml`` in the jar, which Nukkit writes on its first start) is extracted from
the jar. The helper plugin (``plugin.yml`` and the ``*.java`` files in ``servers/nukkit``) is compiled once
against the pinned jar with the JDK's ``javac`` and packed into ``plugins/BenchTick.jar``. It adds ``gametime`` (the
server tick counter, since Nukkit's ``time query`` prints no number) and ``positions``, and lifts the per-IP packet
limit of the RakNet listener.

Launch: ``java -Xms<heap> -Xmx<heap> -jar nukkit.jar``. Nukkit derives a player's UUID from the XUID, so the bots get
``-fake-xuid`` or every bot would replace the previous one. See servers/nukkit/README.md.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config
from bedrock_bench.adapters.base import Dialect, quote_player
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

JAR = "nukkit.jar"
SHIPPED_CONFIG = "lang/eng/nukkit.yml"
PLUGIN_SOURCE = "servers/nukkit"  # plugin.yml and *.java
PLUGIN_FILE = "plugins/BenchTick.jar"
DEFAULT_RUNTIME = "jdk-21.0.12.1"

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


class NukkitAdapter(JavaServer):
    name: ClassVar[str] = "nukkit"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    protocol: ClassVar[int] = 2193
    bot_compat_flags: ClassVar[tuple[str, ...]] = ("-fake-xuid",)
    dialect: ClassVar[Dialect] = NUKKIT_DIALECT
    server_file: ClassVar[str] = JAR

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
            key_parts=tuple(sha256_file(path) for path in plugin_files(source)),
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
        return LaunchSpec(argv=self.java_launch(layout), cwd=layout.server_dir)

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

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = self.jvm_facts(layout)
        facts.update(
            {
                "watchdog": "on",
                "async_workers": "auto (one per available processor)",
                "usage_statistics": "off",
            }
        )
        return facts


# --------------------------------------------------------------------------------------------------------------------
# Helper plugin
# --------------------------------------------------------------------------------------------------------------------


def plugin_files(source: Path) -> list[Path]:
    """The helper's sources: ``plugin.yml``, then the ``*.java`` files next to it."""
    return [source / "plugin.yml", *sorted(source.glob("*.java"))]


def build_plugin(javac: Path, server_jar: Path, source: Path, dest: Path) -> None:
    """Compile the helper's sources against the server jar and pack them with ``plugin.yml`` into ``dest``.

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
    files["plugin.yml"] = (source / "plugin.yml").read_bytes().replace(b"\r\n", b"\n")
    deterministic_zip(dest, files)
