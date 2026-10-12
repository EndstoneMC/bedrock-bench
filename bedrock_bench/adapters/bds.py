# SPDX-License-Identifier: Apache-2.0
"""Vanilla Bedrock Dedicated Server, run straight from Mojang's zip. The baseline every BDS-hosted server is
compared with.

Fetch: the pinned zip for this platform from ``artifacts/bds.toml`` (Minecraft EULA), unpacked with permission bits
kept. Launch: ``bedrock_server.exe`` on Windows, ``./bedrock_server`` with ``LD_LIBRARY_PATH=.`` on Linux, as
Mojang's instructions say. See docs/servers/bds.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from bedrock_bench.adapters.base import Dialect
from bedrock_bench.adapters.bdsfamily import BDS_DIALECT, BdsFamily, fetch_bds
from bedrock_bench.model import Install, LaunchSpec, Layout

if TYPE_CHECKING:
    from bedrock_bench.cache import Cache


class BdsAdapter(BdsFamily):
    name: ClassVar[str] = "bds"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    dialect: ClassVar[Dialect] = BDS_DIALECT

    def fetch(self, cache: Cache) -> Install:
        return fetch_bds(cache, self.arm.version, self.platform, artifact=self.arm.artifact)

    def launch(self, layout: Layout) -> LaunchSpec:
        self._server_exe = self.executable(layout)
        if self.platform == "windows":
            return LaunchSpec(argv=(str(self._server_exe),), cwd=layout.server_dir)
        return LaunchSpec(
            argv=(f"./{layout.extra['exe']}",),
            cwd=layout.server_dir,
            env_set={"LD_LIBRARY_PATH": "."},
            env_remove=("LD_PRELOAD",),
        )
