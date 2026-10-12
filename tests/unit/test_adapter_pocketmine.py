# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import struct
import tomllib
import zlib
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, config, fetch
from bedrock_bench.adapters import pocketmine
from bedrock_bench.adapters.base import UnsupportedAction
from bedrock_bench.adapters.pocketmine import PMMP_DIALECT, PocketMineAdapter, read_phar_member
from bedrock_bench.adapters.standalone import Runtime, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.model import ArtifactRef, Install, LogLine, OverrideCategory, Placement, RunContext
from bedrock_bench.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

#: A pocketmine.yml in the shape PocketMine-MP ships, with the keys the harness sets (synthetic).
SHIPPED_YML = """# Main configuration file
settings:
  force-language: false
  #Number of AsyncTask workers.
  async-workers: auto
memory:
  global-limit: 0
  main-hard-limit: 1024
chunk-ticking:
  tick-radius: 3
ticks-per:
  autosave: 6000
player:
  save-player-data: true
auto-report:
  enabled: true
anonymous-statistics:
  enabled: false #disabled upstream
auto-updater:
  enabled: true
"""


def make_phar(path: Path, files: dict[str, bytes], *, compress: bool = True) -> Path:
    """A minimal phar archive: stub, manifest with one record per file, the contents, no signature."""
    records = b""
    contents = b""
    for name, data in files.items():
        if compress:
            deflate = zlib.compressobj(wbits=-15)
            stored = deflate.compress(data) + deflate.flush()
        else:
            stored = data
        flags = (0x1000 if compress else 0) | 0o644
        encoded = name.encode()
        records += struct.pack("<I", len(encoded)) + encoded
        records += struct.pack("<IIIIII", len(data), 0, len(stored), zlib.crc32(data), flags, 0)
        contents += stored
    body = struct.pack("<I", len(files)) + struct.pack(">H", 0x1100) + struct.pack("<I", 0x10000)
    body += struct.pack("<I", 0) + struct.pack("<I", 0) + records
    path.write_bytes(b"<?php echo 'stub'; __HALT_COMPILER(); ?>\r\n" + struct.pack("<I", len(body)) + body + contents)
    return path


def make_ctx(tmp_path: Path, adapter: PocketMineAdapter, **world: Any) -> RunContext:
    scenario = config.parse_scenario({"server": {"csg": False}, "world": world}, name="test")
    return RunContext(
        run_id="20260101T120000Z-pocketmine-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        scenario=scenario,
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
    )


def make_adapter(variant: str | None = None) -> PocketMineAdapter:
    arm = config.arm_from_ref("pocketmine@5.44.3", variant=variant)
    return PocketMineAdapter(arm, config.default_machine())


@pytest.fixture
def runtime_paths() -> dict[str, str]:
    return {"php": "bin/php/php.exe"}


@pytest.fixture
def fake_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_paths: dict[str, str]) -> Path:
    phar = make_phar(
        tmp_path / "PocketMine-MP.phar", {"src/Server.php": b"<?php", "resources/pocketmine.yml": SHIPPED_YML.encode()}
    )
    ref = ArtifactRef("PocketMine-MP.phar", None, sha256_file(phar), phar.stat().st_size, "local")
    entry = config.load_artifacts("pocketmine").versions["5.44.3"]
    runtime_root = tmp_path / "php"
    runtime = Runtime(
        "php-test", runtime_root, "0" * 64, ArtifactRef("php.zip", None, "1" * 64, 1, "github"), runtime_paths
    )
    monkeypatch.setattr(pocketmine, "fetch_release", lambda cache, server, version, artifact=None: (phar, ref, entry))
    monkeypatch.setattr(pocketmine, "fetch_runtime", lambda cache, key, platform: runtime)
    return tmp_path


@pytest.fixture
def install(fake_artifacts: Path) -> Install:
    return make_adapter().fetch(Cache(fake_artifacts / "cache"))


def line(raw: str) -> LogLine:
    level, logger, text = PMMP_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("pocketmine") is PocketMineAdapter
        assert PocketMineAdapter.class_problems() == []
        assert PocketMineAdapter.protocol == 1001 and not PocketMineAdapter.supports_csg
        assert PocketMineAdapter.bot_compat_flags == ()
        assert PocketMineAdapter.tick_thread_rule == "first_created"
        assert set(PocketMineAdapter.variants) == {"stock"}
        assert PocketMineAdapter.features_file == "features/pocketmine.toml"

    def test_pins(self) -> None:
        catalog = config.load_artifacts("pocketmine")
        assert catalog.get("5.44.3", "windows").sha256 == catalog.get("5.44.3", "linux").sha256
        runtime = catalog.versions["5.44.3"].meta["runtime"]
        assert runtime in config.load_artifacts("runtimes").versions
        assert config.load_features("pocketmine").row("5.44.3") is not None

    def test_dialect_samples(self) -> None:
        with open(LOGS / "pocketmine.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        for sample in samples:
            level, _, text = PMMP_DIALECT.split(sample["line"])
            role, value = sample["role"], sample.get("value")
            if role == "ready":
                assert PMMP_DIALECT.is_ready(text), sample
            elif role == "version":
                assert PMMP_DIALECT.parse_version(text) == value, sample
            elif role == "player_in_world":
                assert PMMP_DIALECT.player_entered(text) == value, sample
            elif role == "tick":
                assert PMMP_DIALECT.parse_tick(text) == value, sample
            elif role == "level":
                assert level == value, sample
            elif role == "strip":
                assert text == value, sample
            elif role == "not_benign":
                assert level == "error" and not PMMP_DIALECT.is_benign(text), sample
            else:
                raise AssertionError(f"unknown role {role}")

    def test_session_log(self) -> None:
        lines = [line(raw) for raw in (LOGS / "pocketmine-session.log").read_text(encoding="utf-8").splitlines()]
        assert sum(PMMP_DIALECT.is_ready(entry.text) for entry in lines) == 1
        assert [v for v in (PMMP_DIALECT.parse_version(e.text) for e in lines) if v] == ["5.44.3"]
        assert [t for t in (PMMP_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [3, 103, 203]
        assert [p for p in (PMMP_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00", "a01"]
        positions = make_adapter().parse_positions(lines)
        assert positions is not None and [(p.name, p.x, p.z) for p in positions] == [
            ("a00", 1280.5, 1280.5),
            ("a01", 1920.5, 1280.5),
        ]
        assert make_adapter().parse_positions([]) is None


class TestPhar:
    @pytest.mark.parametrize("compress", [True, False])
    def test_reads_a_member(self, tmp_path: Path, compress: bool) -> None:
        phar = make_phar(tmp_path / "a.phar", {"a.php": b"<?php", "resources/x.yml": b"key: 1\n"}, compress=compress)
        assert read_phar_member(phar, "resources/x.yml") == b"key: 1\n"
        assert read_phar_member(phar, "a.php") == b"<?php"

    def test_missing_member(self, tmp_path: Path) -> None:
        phar = make_phar(tmp_path / "a.phar", {"a.php": b"<?php"})
        with pytest.raises(fetch.FetchError, match="has no resources/pocketmine.yml"):
            read_phar_member(phar, "resources/pocketmine.yml")

    def test_corrupt_member(self, tmp_path: Path) -> None:
        phar = make_phar(tmp_path / "a.phar", {"a.php": b"<?php"}, compress=False)
        data = bytearray(phar.read_bytes())
        data[-1] ^= 0xFF
        phar.write_bytes(bytes(data))
        with pytest.raises(fetch.FetchError, match="CRC"):
            read_phar_member(phar, "a.php")

    def test_not_a_phar(self, tmp_path: Path) -> None:
        (tmp_path / "a.zip").write_bytes(b"PK\x03\x04")
        with pytest.raises(fetch.FetchError, match="not a phar"):
            read_phar_member(tmp_path / "a.zip", "a.php")


class TestFetchAndPrepare:
    def test_install_layout(self, install: Install) -> None:
        server = install.root / "server"
        assert (server / "PocketMine-MP.phar").is_file()
        helper = (ROOT / "servers" / "pocketmine" / "plugins" / "BenchHelper.php").read_bytes()
        assert (server / "plugins" / "BenchHelper.php").read_bytes() == helper
        assert (install.root / "defaults" / "pocketmine.yml").read_text(encoding="utf-8") == SHIPPED_YML
        assert [a.name for a in install.artifacts] == ["PocketMine-MP.phar", "php.zip"]
        assert install.extra["protocol"] == 1001 and install.extra["runtime"] == "php-test"

    def test_prepare_writes_configs_and_overrides(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        server = layout.server_dir
        props = (server / "server.properties").read_text(encoding="utf-8")
        for expected in ("server-port=19672", "enable-ipv6=off", "xbox-auth=off", "view-distance=10",
                         "level-name=bench", "level-seed=20261010", "difficulty=1", "gamemode=SURVIVAL"):  # fmt: skip
            assert expected in props.splitlines()
        yml = yaml_scalars((server / "pocketmine.yml").read_text(encoding="utf-8"))
        assert yml["settings.async-workers"] == "4"
        assert yml["memory.main-hard-limit"] == "0"
        assert yml["chunk-ticking.tick-radius"] == "4"
        assert yml["auto-report.enabled"] == "false" and yml["auto-updater.enabled"] == "false"
        assert yml["settings.force-language"] == "false", "shipped values stay"

        by_key = {(o.file, o.key): o for o in layout.overrides}
        rescue = by_key[("pocketmine.yml", "memory.main-hard-limit")]
        assert (rescue.category, rescue.default, rescue.value) == (OverrideCategory.RESCUE, "1024", "0")
        assert by_key[("pocketmine.yml", "settings.async-workers")].default == "auto"
        assert by_key[("server.properties", "view-distance")].default == "16"
        assert ("pocketmine.yml", "anonymous-statistics.enabled") not in by_key, "equal to the shipped value"
        assert ("pocketmine.yml", "ticks-per.autosave") not in by_key
        assert by_key[("<command line>", "--no-wizard")].category is OverrideCategory.HARNESS
        assert by_key[("plugins/BenchHelper.php", "RakLib per-IP packet limit")].value == "unlimited"

        assert [c.path for c in layout.config_files] == ["server.properties", "pocketmine.yml"]
        assert layout.world.fresh and layout.world.generator == "custom"
        assert layout.world.path == server / "worlds" / "bench" and not layout.world.path.exists()
        assert layout.extra["exe_sha256"] == install.extra["server_sha256"]
        assert adapter.executable(layout) == server / "PocketMine-MP.phar"
        assert adapter.readback(layout) == []
        (server / "pocketmine.yml").write_text("memory:\n  main-hard-limit: 1024\n", encoding="utf-8")
        problems = adapter.readback(layout)
        assert any("memory.main-hard-limit was written as '0' and read back as '1024'" in p for p in problems)

    def test_stock_variant_keeps_the_memory_limit(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter("stock")
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        assert not [o for o in layout.overrides if o.category is OverrideCategory.RESCUE]
        yml = yaml_scalars((layout.server_dir / "pocketmine.yml").read_text(encoding="utf-8"))
        assert yml["memory.main-hard-limit"] == "1024"
        assert adapter.facts(layout, None)["main_thread_hard_limit_mb"] == "1024 (shipped)"  # type: ignore[arg-type]

    def test_refuses_a_used_work_dir(self, install: Install, tmp_path: Path) -> None:
        (tmp_path / "work" / "worlds" / "bench").mkdir(parents=True)
        (tmp_path / "work" / "worlds" / "bench" / "level.dat").write_bytes(b"")
        adapter = make_adapter()
        with pytest.raises(FileExistsError):
            adapter.prepare(install, make_ctx(tmp_path, adapter))

    def test_launch_windows_layout(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        spec = adapter.launch(layout)
        assert Path(spec.argv[0]) == tmp_path / "php" / "bin" / "php" / "php.exe"
        assert spec.argv[1:] == ("PocketMine-MP.phar", "--no-wizard", "--disable-ansi")
        assert spec.cwd == layout.server_dir and spec.env_remove == ("PHPRC",)

    @pytest.mark.parametrize(
        "runtime_paths",
        [{"php": "bin/php7/bin/php", "extension_dir": "bin/php7/lib/php/extensions/no-debug-zts-20220829"}],
    )
    def test_launch_with_extension_dir(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        spec = adapter.launch(layout)
        assert spec.argv[1] == "-d"
        assert Path(spec.argv[2].removeprefix("extension_dir=")) == (
            tmp_path / "php" / "bin" / "php7" / "lib" / "php" / "extensions" / "no-debug-zts-20220829"
        )
        assert spec.argv[3] == "PocketMine-MP.phar"
        override = next(o for o in layout.overrides if o.key == "-d extension_dir")
        assert override.value.startswith("<runtime>/") and str(tmp_path) not in override.value


class TestCommands:
    def test_effects(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_effects("@a") == []
        effects = adapter.cmd_effects("a07")
        assert effects[0] == "effect a07 resistance infinite 255 true" and len(effects) == 4
        assert adapter.cmd_tp("a07", 1280.5, 300, 1280) == "tp a07 1280.5 300 1280"
        assert adapter.cmd_positions() == "positions"
        assert PMMP_DIALECT.tick_cmd == "gametime"

    def test_chaos_dialect(self) -> None:
        dialect = make_adapter().chaos_dialect()
        assert dialect.actions == {"tp", "effects", "effect_clear", "kill", "kick", "gamemode", "list"}
        assert dialect.render("kick", player="a07", reason="chaos") == ["kick a07 chaos"]
        assert dialect.render("gamemode", player="a07", mode="adventure") == ["gamemode adventure a07"]
        assert dialect.render("effect_clear", player="a07") == ["effect a07 clear"]
        with pytest.raises(UnsupportedAction):
            dialect.render("weather", kind="rain")
        with pytest.raises(ValueError):
            dialect.render("gamemode", player="a07", mode="hardcore")

    def test_every_catalogue_command_is_rendered_or_unsupported(self) -> None:
        catalogue = ROOT / "configs" / "scenarios" / "chaos-actions.toml"
        if not catalogue.is_file():
            pytest.skip("no chaos catalogue")
        commands = tomllib.loads(catalogue.read_text(encoding="utf-8")).get("command", {})
        dialect = make_adapter().chaos_dialect()
        for name, entry in commands.items():
            assert dialect.supports(name) or name in dialect.unsupported, name
            if dialect.supports(name):
                assert dialect.render(name, **entry.get("example", {}))
