# SPDX-License-Identifier: Apache-2.0
"""PowerNukkitX, a Nukkit fork with vanilla-like gameplay, run from a release or nightly jar on the pinned JDK.

Fetch: ``powernukkitx.jar`` from ``servers/powernukkitx/server.toml`` and the JDK that version names from
``servers/runtimes.toml``. No helper plugin: ``time query gametime`` prints the tick counter, and positions
come from the bots, because PowerNukkitX has no ``querytarget``. Nightly builds are pinned as ``nightly.<commit>``.

Prepare renders ``pnx.yml``, which PowerNukkitX completes and rewrites on start, and ``config.json`` as
``worlds/<level>/config.json``, the only place PowerNukkitX reads the seed from. Launch:
``java -Xms<heap> -Xmx<heap> -jar powernukkitx.jar``. Like Nukkit, it derives a player's UUID from the XUID, so the
bots get ``-fake-xuid``. See servers/powernukkitx/README.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench.adapters.base import Dialect
from bedrock_bench.adapters.standalone import JavaServer, fetch_release, fetch_runtime, runtime_platform, yaml_scalars
from bedrock_bench.model import ConfigFile, Install, LaunchSpec, Layout, Override, RunContext, WorldInfo

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

JAR = "powernukkitx.jar"
WORLD_CONFIG = "config.json"
NIGHTLY = "nightly"
#: pnx.yml values left at their defaults and recorded as facts, by fact name. Keys a version lacks are skipped.
RECORDED_SETTINGS: Mapping[str, str] = {
    "upstream_bandwidth_mbps": "network-settings.upstream-bandwidth",
    "generation_queue_size": "chunk-settings.generationQueueSize",
    "leveldb_backend": "level-settings.leveldb-backend",
    "raknet_global_packet_limit_per_10ms": "network-settings.raknet.globalPacketLimit",
}

#: ``13:58:00 [main] [INFO] text`` with the thread name; log4j's own start-up warning is
#: ``2026-10-10T22:29:18.175Z main WARN text``.
PNX_PREFIX = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\S+Z \S+ |\d{2}:\d{2}:\d{2} \[(?P<logger>[^\]]+)\] \[)(?P<level>[A-Z]+)(?:\] | )"
)
PNX_LEVELS: Mapping[str, str] = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "ERROR": "error",
    "FATAL": "fatal",
}
_RELEASE = re.compile(r"\d+(?:\.\d+)+")


class PnxDialect(Dialect):
    """Release jars report ``3.0.5-SNAPSHOT (git-9813b75)``, read as ``3.0.5``. Nightly jars report
    ``nightly-SNAPSHOT (git-86f70bc)``, read as ``nightly.86f70bc``.
    """

    def parse_version(self, text: str) -> str | None:
        match = self.version.search(text)
        if match is None:
            return None
        version = match["version"]
        return version if _RELEASE.fullmatch(version) else f"{version}.{match['commit']}"


PNX_DIALECT = PnxDialect(
    ready=re.compile(r"^Done \(\d+(?:\.\d+)?s\)!"),
    prefix=PNX_PREFIX,
    level_map=PNX_LEVELS,
    benign=(),
    version=re.compile(
        r"This server is running PowerNukkitX version (?P<version>\S+?)(?:-SNAPSHOT)?"
        r" \(git-(?P<commit>[0-9a-f]{7,40})\)"
    ),
    player_in_world=re.compile(r"^(?P<player>\S+) joined the game$"),
    tick_cmd="time query gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)"),
)


class PowerNukkitXAdapter(JavaServer):
    name: ClassVar[str] = "powernukkitx"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    bot_compat_flags: ClassVar[tuple[str, ...]] = ("-fake-xuid",)
    dialect: ClassVar[Dialect] = PNX_DIALECT
    server_file: ClassVar[str] = JAR
    config_templates: ClassVar[tuple[str, ...]] = ("pnx.yml",)

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        jar, jar_ref, entry = fetch_release(cache, self.name, self.arm.version, artifact=self.arm.artifact)
        meta = dict(entry.meta) if entry is not None else {}
        runtime = fetch_runtime(cache, self.pinned_runtime(entry), runtime_platform())
        return self.build_install(
            cache,
            release=jar,
            release_ref=jar_ref,
            runtime=runtime,
            build=lambda tmp: None,
            extra={"commit": meta.get("commit"), "protocol": meta.get("protocol"), "minecraft": meta.get("minecraft")},
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def world_data(self, level_dir: Path) -> Path:
        """The level's database: prepare writes ``config.json`` into the level folder before the first start."""
        return level_dir / "db"

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext, world: WorldInfo
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        target = f"worlds/{ctx.workload.world.level_name}/{WORLD_CONFIG}"
        return [], [self.write_config(server_dir, WORLD_CONFIG, ctx, target=target)], {}

    def launch(self, layout: Layout) -> LaunchSpec:
        return LaunchSpec(argv=self.java_launch(layout), cwd=layout.server_dir)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = self.jvm_facts(layout)
        facts.update(
            {
                "usage_statistics": "off",
                "inbound_rate_limit": "on as shipped: 1500 packets/s and 500 per tick per session",
                "level_worker_threads": "-1 (one per available processor)",
            }
        )
        try:
            settings = yaml_scalars((layout.server_dir / "pnx.yml").read_text(encoding="utf-8"))
        except OSError:
            settings = {}
        facts.update({fact: settings[key] for fact, key in RECORDED_SETTINGS.items() if key in settings})
        return facts
