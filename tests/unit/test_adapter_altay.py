# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import struct
import tomllib
import zlib
from pathlib import Path

import pytest

from bedrock_bench import adapters, bots, config, templates
from bedrock_bench.adapters import pocketmine
from bedrock_bench.adapters.altay import ALTAY_DIALECT, AltayAdapter
from bedrock_bench.adapters.pocketmine import PMMP_DIALECT, PocketMineAdapter, written_values
from bedrock_bench.adapters.standalone import Runtime, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.model import ArtifactRef, Install, LogLine, Placement, RunContext
from bedrock_bench.provenance import sha256_file
from workloads import WORKLOAD

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"
SERVER_DIR = config.server_dir("altay")

#: Lines of the pocketmine.yml that Altay 5.44.7 ships, cut down to the keys the harness sets.
SHIPPED_YML = """# Main configuration file for Altay
settings:
  async-workers: auto
memory:
  main-hard-limit: 1024
network:
  # Network transport
  # Options: raknet or nethernet
  transport: raknet
player:
  save-player-data: true
chunk-ticking:
  tick-radius: 3
ticks-per:
  autosave: 6000
auto-report:
  enabled: true
  host: crash.getaltay.org
anonymous-statistics:
  enabled: true
auto-updater:
  enabled: true
  host: update.getaltay.org
"""


def make_phar(path: Path, files: dict[str, bytes]) -> Path:
    """A minimal phar with gzip-compressed entries, as Altay ships them; no signature."""
    records = b""
    contents = b""
    for name, data in files.items():
        deflate = zlib.compressobj(wbits=-15)
        stored = deflate.compress(data) + deflate.flush()
        encoded = name.encode()
        records += struct.pack("<I", len(encoded)) + encoded
        records += struct.pack("<IIIIII", len(data), 0, len(stored), zlib.crc32(data), 0x1000 | 0o644, 0)
        contents += stored
    body = struct.pack("<I", len(files)) + struct.pack(">H", 0x1100) + struct.pack("<I", 0x10000)
    body += struct.pack("<I", 0) + struct.pack("<I", 0) + records
    path.write_bytes(b"<?php echo 'stub'; __HALT_COMPILER(); ?>\r\n" + struct.pack("<I", len(body)) + body + contents)
    return path


def make_adapter() -> AltayAdapter:
    return AltayAdapter(config.arm_from_ref("altay@5.44.7"), config.default_machine())


def make_ctx(tmp_path: Path, adapter: AltayAdapter) -> RunContext:
    workload = WORKLOAD
    return RunContext(
        run_id="20260101T120000Z-altay-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        workload=workload,
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
    )


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    phar = make_phar(
        tmp_path / "Altay.phar", {"src/Server.php": b"<?php", "resources/pocketmine.yml": SHIPPED_YML.encode()}
    )
    ref = ArtifactRef("Altay.phar", None, sha256_file(phar), phar.stat().st_size, "local")
    entry = config.load_artifacts("altay").versions["5.44.7"]
    runtime = Runtime(
        "php-test", tmp_path / "php", "0" * 64, ArtifactRef("php.zip", None, "1" * 64, 1, "github"), {"php": "php.exe"}
    )
    # The inherited fetch looks these up in the pocketmine module.
    monkeypatch.setattr(pocketmine, "fetch_release", lambda cache, server, version, artifact=None: (phar, ref, entry))
    monkeypatch.setattr(pocketmine, "fetch_runtime", lambda cache, key, platform: runtime)
    return make_adapter().fetch(Cache(tmp_path / "cache"))


def line(raw: str) -> LogLine:
    level, logger, text = ALTAY_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("altay") is AltayAdapter
        assert AltayAdapter.class_problems() == []
        assert not AltayAdapter.supports_csg and AltayAdapter.bot_compat_flags == ()
        assert AltayAdapter.tick_thread_rule == "first_created"
        own, base = (config.server_dir(name) / pocketmine.HELPER_SOURCE for name in ("altay", "pocketmine"))
        assert own.read_bytes() != base.read_bytes(), "Altay's transport needs its own helper"

    def test_templates_are_pocketmines_plus_the_transport(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        variables = adapter.template_variables(make_ctx(tmp_path, adapter))
        for name, extra in (("server.properties", {}), ("pocketmine.yml", {"network.transport": "raknet"})):
            fmt = templates.file_format(name)
            own = templates.values(fmt, templates.render(SERVER_DIR, name, variables))
            base = templates.values(fmt, templates.render(config.server_dir("pocketmine"), name, variables))
            assert own == base | extra, name

    def test_pins(self) -> None:
        version = config.load_artifacts("altay").versions["5.44.7"]
        assert bots.module_for_protocol(version.meta["protocol"]) == "loadbot"
        assert version.meta["minecraft"] == "1.26.50"
        assert len(version.meta["commit"]) == 40
        assert version.meta["runtime"] in config.load_artifacts(ROOT / config.RUNTIMES_FILE).versions
        spec = version.platforms["any"]
        assert spec.url.startswith("https://github.com/altayofficial/Altay/releases/download/5.44.7/")
        assert spec.sha256 is not None and spec.size

    def test_dialect_samples(self) -> None:
        samples = tomllib.loads((LOGS / "altay.toml").read_text(encoding="utf-8"))["sample"]
        for sample in samples:
            level, _, text = ALTAY_DIALECT.split(sample["line"])
            role, value = sample["role"], sample.get("value")
            if role == "ready":
                assert ALTAY_DIALECT.is_ready(text), sample
            elif role == "version":
                assert ALTAY_DIALECT.parse_version(text) == value, sample
            elif role == "player_in_world":
                assert ALTAY_DIALECT.player_entered(text) == value, sample
            elif role == "tick":
                assert ALTAY_DIALECT.parse_tick(text) == value, sample
            elif role == "level":
                assert level == value, sample
            elif role == "strip":
                assert text == value, sample
            elif role == "not_benign":
                assert level in ("error", "fatal") and not ALTAY_DIALECT.is_benign(text), sample
            else:
                raise AssertionError(f"unknown role {role}")

    def test_banner_names_the_fork(self) -> None:
        assert ALTAY_DIALECT.parse_version("This server is running PocketMine-MP version 5.44.3") is None
        assert PMMP_DIALECT.parse_version("This server is running Altay version 5.44.7") is None
        assert ALTAY_DIALECT.parse_version("This server is running Altay") is None, "the version command's reply"

    def test_session_log(self) -> None:
        lines = [line(raw) for raw in (LOGS / "altay-session.log").read_text(encoding="utf-8").splitlines()]
        assert sum(ALTAY_DIALECT.is_ready(entry.text) for entry in lines) == 1
        assert [v for v in (ALTAY_DIALECT.parse_version(e.text) for e in lines) if v] == ["5.44.7"]
        assert [t for t in (ALTAY_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [77, 191, 520, 634]
        assert [p for p in (ALTAY_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00", "a01", "a02", "a03"]
        adapter = make_adapter()
        placed = adapter.parse_positions(lines)
        assert placed is not None and [(p.name, p.y) for p in placed] == [
            ("a00", 300.0),
            ("a01", 300.0),
            ("a02", 300.0),
            ("a03", 300.0),
        ]
        kicked = next(i for i, e in enumerate(lines) if e.text == "a03 left the game")
        after = adapter.parse_positions(lines[kicked:])
        assert after is not None and [(p.name, p.y) for p in after] == [("a00", 300.0), ("a01", 300.0), ("a02", 75.0)]
        assert adapter.parse_positions([]) is None


class TestFetchAndPrepare:
    def test_install_layout(self, install: Install) -> None:
        server = install.root / "server"
        assert (server / "Altay.phar").is_file()
        assert (server / "plugins" / "BenchHelper.php").read_bytes() == (SERVER_DIR / "BenchHelper.php").read_bytes()
        assert (install.root / "defaults" / "pocketmine.yml").read_text(encoding="utf-8") == SHIPPED_YML
        assert [a.name for a in install.artifacts] == ["Altay.phar", "php.zip"]
        assert install.extra["protocol"] == 2193 and install.extra["minecraft"] == "1.26.50"

    def test_prepare_renders_the_templates(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        yml = yaml_scalars((layout.server_dir / "pocketmine.yml").read_text(encoding="utf-8"))
        assert yml["network.transport"] == "raknet"
        assert yml["anonymous-statistics.enabled"] == "false" and yml["memory.main-hard-limit"] == "0"
        assert yml["auto-report.host"] == "crash.getaltay.org", "shipped values stay"

        files = {c.path: c for c in layout.config_files}
        assert [c.template for c in files.values()] == ["server.properties", "pocketmine.yml"]
        changes = {c.key: (c.before, c.after) for c in files["pocketmine.yml"].keys}
        assert changes["network.transport"] == ("raknet", "raknet")
        assert changes["anonymous-statistics.enabled"] == ("true", "false")
        assert changes["memory.main-hard-limit"] == ("1024", "0")
        assert written_values(layout, "server.properties")["xbox-auth"] == "off"

        assert adapter.executable(layout) == layout.server_dir / "Altay.phar"
        spec = adapter.launch(layout)
        assert spec.argv[1:] == ("Altay.phar", "--no-wizard", "--disable-ansi") and spec.env_remove == ("PHPRC",)
        assert adapter.readback(layout) == []
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert facts["transport"] == "raknet" and facts["usage_statistics"] == "off"
        assert facts["crash_upload"] == "off" and facts["auto_updater"] == "off"


class TestCommands:
    def test_same_commands_as_pocketmine(self) -> None:
        adapter = make_adapter()
        reference = PocketMineAdapter(config.arm_from_ref("pocketmine@5.44.3"), config.default_machine())
        assert adapter.cmd_effects("@a") == []
        assert adapter.cmd_effects("a07") == reference.cmd_effects("a07")
        assert adapter.cmd_tp("a07", 1280.5, 300, 1280) == "tp a07 1280.5 300 1280"
        assert adapter.cmd_positions() == "positions" and ALTAY_DIALECT.tick_cmd == "gametime"
