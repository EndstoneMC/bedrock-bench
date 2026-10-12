# SPDX-License-Identifier: Apache-2.0
"""Altay, a PocketMine-MP 5 fork on the current Bedrock protocol, run from its release phar on PocketMine's PHP build.

Fetch, launch, the console dialect and the commands come from
:class:`~bedrock_bench.adapters.pocketmine.PocketMineAdapter`. The phar, the version banner and the helper differ:
``servers/altay/BenchHelper.php`` lifts the per-IP packet limit through Altay's transport interface instead of
RakLib's.

Altay 5.44.7 speaks protocol 2193 (Minecraft 1.26.50), so it gets the main load bot. Its version numbers follow
PocketMine-MP's. See servers/altay/README.md.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench.adapters.base import Dialect
from bedrock_bench.adapters.pocketmine import PMMP_DIALECT, PocketMineAdapter
from bedrock_bench.model import Layout

if TYPE_CHECKING:
    import psutil

#: PocketMine-MP's console format; only the banner names the fork.
ALTAY_DIALECT = dataclasses.replace(
    PMMP_DIALECT,
    version=re.compile(r"This server is running Altay version (?P<version>\d+(?:\.\d+)+\S*)\s*$"),
)


class AltayAdapter(PocketMineAdapter):
    name: ClassVar[str] = "altay"
    protocol: ClassVar[int] = 2193
    dialect: ClassVar[Dialect] = ALTAY_DIALECT
    server_file: ClassVar[str] = "Altay.phar"
    helper_source: ClassVar[str] = "servers/altay/BenchHelper.php"
    packet_limiter: ClassVar[str] = "RakNet"
    packet_limit: ClassVar[str] = "2147483647"

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        written = layout.extra.get("written", {}).get("pocketmine.yml", {})
        facts = super().facts(layout, proc)
        facts.update({"transport": written.get("network.transport"), "usage_statistics": "off"})
        return facts
