# SPDX-License-Identifier: Apache-2.0
"""PocketMine-MP, a Bedrock server written in PHP, run from its release phar on PocketMine's own PHP build.

Fetch: ``PocketMine-MP.phar`` from ``servers/pocketmine/server.toml`` and the PHP binaries from
``servers/runtimes.toml``. The shipped ``pocketmine.yml`` (what PocketMine-MP copies out of the phar on its
first start) is extracted from the phar, so its defaults are always those of the pinned release.
``servers/pocketmine/BenchHelper.php`` goes into ``plugins/``: it adds ``gametime`` (the server's tick counter)
and ``positions``, and lifts RakLib's per-IP packet limit, which the bots, all on 127.0.0.1, would trip.

Launch: ``php PocketMine-MP.phar --no-wizard --disable-ansi`` in the run's server directory, like the official start
scripts. On Linux the bundled extension directory is passed with ``-d extension_dir``, as PocketMine-MP's installer
writes it, so opcache loads.

The 5.44 line speaks protocol 1001 (Minecraft 1.26.30), so it gets the ``legacy1001`` bot; reports flag that.
See servers/pocketmine/README.md.
"""

from __future__ import annotations

import re
import shutil
import struct
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import fetch
from bedrock_bench.adapters.base import Dialect
from bedrock_bench.adapters.standalone import (
    COMMAND_LINE,
    DEFAULTS_PART,
    ENVIRONMENT,
    SERVER_PART,
    StandaloneServer,
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

PHAR = "PocketMine-MP.phar"
SHIPPED_CONFIG = "resources/pocketmine.yml"
HELPER_SOURCE = "BenchHelper.php"
HELPER_FILE = "plugins/BenchHelper.php"

#: ``[13:58:00.123] [Server thread/INFO]: text``. Replies to console commands are printed as ``Command output | text``.
PMMP_PREFIX = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[(?P<logger>[^/\]]+)/(?P<level>[A-Z]+)\]: ?|Command output \| ?)"
)
PMMP_LEVELS: Mapping[str, str] = {
    "DEBUG": "debug",
    "INFO": "info",
    "NOTICE": "info",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "fatal",
    "ALERT": "fatal",
    "EMERGENCY": "fatal",
}
PMMP_DIALECT = Dialect(
    ready=re.compile(r"^Done \(\d+(?:\.\d+)?s\)!"),
    prefix=PMMP_PREFIX,
    level_map=PMMP_LEVELS,
    benign=(),
    version=re.compile(r"This server is running PocketMine-MP version (?P<version>\d+(?:\.\d+)+\S*)\s*$"),
    player_in_world=re.compile(r"^(?P<player>\S+) joined the game$"),
    tick_cmd="gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)"),
)


class PocketMineAdapter(StandaloneServer):
    name: ClassVar[str] = "pocketmine"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    dialect: ClassVar[Dialect] = PMMP_DIALECT
    tick_thread_rule: ClassVar[str] = "first_created"
    crash_paths: ClassVar[tuple[str, ...]] = ("crashdumps",)
    server_file: ClassVar[str] = PHAR
    #: The network layer whose per-IP packet limit the helper lifts, and the limit it sets.
    packet_limiter: ClassVar[str] = "RakLib"
    packet_limit: ClassVar[str] = "unlimited"

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        phar, phar_ref, entry = fetch_release(cache, self.name, self.arm.version, artifact=self.arm.artifact)
        meta = dict(entry.meta) if entry is not None else {}
        runtime = fetch_runtime(cache, self.pinned_runtime(entry), runtime_platform())
        helper = self.folder() / HELPER_SOURCE

        def build(tmp: Path) -> None:
            (tmp / DEFAULTS_PART / "pocketmine.yml").write_bytes(read_phar_member(phar, SHIPPED_CONFIG))
            (tmp / SERVER_PART / "plugins").mkdir()
            shutil.copyfile(helper, tmp / SERVER_PART / HELPER_FILE)

        return self.build_install(
            cache,
            release=phar,
            release_ref=phar_ref,
            runtime=runtime,
            build=build,
            key_parts=(sha256_file(helper),),
            extra={"protocol": meta.get("protocol"), "minecraft": meta.get("minecraft")},
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext, world: WorldInfo
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        harness = OverrideCategory.HARNESS
        limiter = self.packet_limiter
        overrides = [
            Override(harness, HELPER_FILE, "plugin", None, "installed", "prints the tick counter and player positions"),
            Override(
                harness,
                HELPER_FILE,
                f"{limiter} per-IP packet limit",
                f"200 packets per {limiter} tick",
                self.packet_limit,
                f"every bot connects from 127.0.0.1; at the default limit {limiter} blocks the address for 300 s",
            ),
        ]
        return overrides, [], {}

    def launch_overrides(self, install: Install) -> Sequence[Override]:
        harness = OverrideCategory.HARNESS
        overrides = [
            Override(harness, COMMAND_LINE, "--no-wizard", None, "set", "skips the interactive setup wizard"),
            Override(harness, COMMAND_LINE, "--disable-ansi", None, "set", "plain console output, no title updates"),
            Override(harness, ENVIRONMENT, "PHPRC", None, "(unset)", "PHP reads the php.ini of its own build"),
        ]
        if "extension_dir" in install.extra.get("runtime_paths", {}):
            overrides.append(
                Override(
                    harness,
                    COMMAND_LINE,
                    "-d extension_dir",
                    "PHP's build-time path",
                    f"<runtime>/{install.extra['runtime_paths']['extension_dir']}",
                    "the bundled extensions (opcache) load, as PocketMine-MP's installer configures it",
                )
            )
        return overrides

    def launch(self, layout: Layout) -> LaunchSpec:
        args = (self.server_file, "--no-wizard", "--disable-ansi")
        php = str(self.runtime_path(layout, "php"))
        options: tuple[str, ...] = ()
        if "extension_dir" in layout.extra.get("runtime_paths", {}):
            options = ("-d", f"extension_dir={self.runtime_path(layout, 'extension_dir')}")
        return LaunchSpec(argv=(php, *options, *args), cwd=layout.server_dir, env_remove=("PHPRC",))

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_effects(self, player: str) -> list[str]:
        """The placement effects; PocketMine-MP has no target selectors, so ``@a`` renders nothing."""
        return [] if player.startswith("@") else super().cmd_effects(player)

    def cmd_positions(self) -> str | None:
        return "positions"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_helper_positions(lines)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        written = layout.extra.get("written", {}).get("pocketmine.yml", {})
        return {
            "allocator": "default",
            "csg_setting": False,
            "php_runtime": layout.extra.get("runtime"),
            "async_workers": written.get("settings.async-workers"),
            "main_thread_hard_limit_mb": written.get("memory.main-hard-limit", "1024 (shipped)"),
            "crash_upload": "off",
            "auto_updater": "off",
        }


# --------------------------------------------------------------------------------------------------------------------
# Phar archives
# --------------------------------------------------------------------------------------------------------------------

_HALT = b"__HALT_COMPILER();"
_PHAR_GZ = 0x1000
_PHAR_BZ2 = 0x2000


def read_phar_member(path: Path, name: str) -> bytes:
    """One file from a PHP phar archive (uncompressed or gzip-compressed entries), checked against its CRC32.

    Layout: a PHP stub ending in ``__HALT_COMPILER(); ?>``, a manifest (length, file count, API version, flags, alias,
    metadata, then one record per file), the file contents in manifest order, and a signature.
    """
    data = path.read_bytes()
    halt = data.find(_HALT)
    if halt < 0:
        raise fetch.FetchError(f"{path.name} is not a phar archive")
    pos = halt + len(_HALT)
    for end in (b" ?>\r\n", b" ?>\n", b"?>\r\n", b"?>\n"):
        if data.startswith(end, pos):
            pos += len(end)
            break
    try:
        manifest_len, count = struct.unpack_from("<II", data, pos)
        cursor = pos + 8 + 2 + 4  # API version (2 bytes) and global flags (4 bytes)
        (alias_len,) = struct.unpack_from("<I", data, cursor)
        cursor += 4 + alias_len
        (meta_len,) = struct.unpack_from("<I", data, cursor)
        cursor += 4 + meta_len
        offset = pos + 4 + manifest_len
        for _ in range(count):
            (name_len,) = struct.unpack_from("<I", data, cursor)
            entry_name = data[cursor + 4 : cursor + 4 + name_len].decode("utf-8", errors="replace")
            cursor += 4 + name_len
            size, _, stored, crc, flags, meta = struct.unpack_from("<IIIIII", data, cursor)
            cursor += 24 + meta
            if entry_name == name:
                raw = data[offset : offset + stored]
                if flags & _PHAR_BZ2:
                    raise fetch.FetchError(f"{path.name}: {name} is bzip2-compressed, which is not supported")
                content = zlib.decompress(raw, -15) if flags & _PHAR_GZ else raw
                if len(content) != size or zlib.crc32(content) != crc:
                    raise fetch.FetchError(f"{path.name}: {name} fails its size or CRC check")
                return content
            offset += stored
    except (struct.error, zlib.error) as exc:
        raise fetch.FetchError(f"{path.name}: unreadable phar manifest: {exc}") from exc
    raise fetch.FetchError(f"{path.name} has no {name}")
