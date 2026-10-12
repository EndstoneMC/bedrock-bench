# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from bedrock_bench import adapters, config, pregen
from bedrock_bench.adapters import powernukkitx
from bedrock_bench.adapters.base import UnsupportedAction
from bedrock_bench.adapters.powernukkitx import PNX_DIALECT, PowerNukkitXAdapter
from bedrock_bench.adapters.standalone import Runtime, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import ArtifactRef, Install, LogLine, OverrideCategory, Placement, RunContext
from bedrock_bench.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

#: pnx.yml as PowerNukkitX rewrites it on start: every key, comments, unquoted strings (shortened, synthetic).
REWRITTEN_PNX_YML = """# Basic settings for your PowerNukkitX server
settings:
  # Server's port
  port: 19672
  maxPlayers: 200
  defaultLevelName: bench
  autoSave: true
  autosaveDelay: 6000
  xboxAuth: false
player-settings:
  savePlayerData: true
gameplay-settings:
  viewDistance: 10
  gamemode: 0
  difficulty: 1
  experiments:
  - data_driven_vanilla_blocks_and_items
misc-settings:
  enableMetrics: false
level-settings:
  autoTickRate: false
  loadAllLevels: false
chunk-settings:
  tickRadius: 4
network-settings:
  transport: raknet
  raknet:
    packetLimit: 1000000
    autoFlush: true
config:
  version: 3.0.1
"""


def make_adapter(runtime: str = "native") -> PowerNukkitXAdapter:
    return PowerNukkitXAdapter(
        config.arm_from_ref("powernukkitx@3.0.5"), config.parse_machine({"runtime": {"kind": runtime}})
    )


def make_ctx(tmp_path: Path, adapter: PowerNukkitXAdapter, template: Path | None = None, **world: object) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-powernukkitx-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        scenario=config.parse_scenario({"server": {"csg": False}, "world": world}, name="test"),
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
        template=template,
    )


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    jar = tmp_path / "powernukkitx.jar"
    jar.write_bytes(b"PK jar")
    ref = ArtifactRef("powernukkitx.jar", None, sha256_file(jar), jar.stat().st_size, "local")
    entry = config.load_artifacts("powernukkitx").versions["3.0.5"]
    paths = {"home": "jdk", "java": "jdk/bin/java", "javac": "jdk/bin/javac", "jcmd": "jdk/bin/jcmd"}
    runtime = Runtime(
        "jdk-test", tmp_path / "jdk-runtime", "0" * 64, ArtifactRef("jdk.tgz", None, "1" * 64, 1, "github"), paths
    )
    monkeypatch.setattr(powernukkitx, "fetch_release", lambda cache, server, version, artifact=None: (jar, ref, entry))
    monkeypatch.setattr(powernukkitx, "fetch_runtime", lambda cache, key, platform: runtime)
    return make_adapter().fetch(Cache(tmp_path / "cache"))


def line(raw: str) -> LogLine:
    level, logger, text = PNX_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("powernukkitx") is PowerNukkitXAdapter
        assert PowerNukkitXAdapter.class_problems() == []
        assert PowerNukkitXAdapter.protocol == 2193 and not PowerNukkitXAdapter.supports_csg
        assert PowerNukkitXAdapter.bot_compat_flags == ("-fake-xuid",)
        assert PowerNukkitXAdapter.tick_thread_rule == "java_main"
        assert PowerNukkitXAdapter.variants == {}

    def test_pins(self) -> None:
        catalog = config.load_artifacts("powernukkitx")
        assert catalog.get("3.0.5", "windows").url.endswith("/3.0.5/powernukkitx.jar")
        assert catalog.versions["3.0.5"].meta["commit"].startswith("9813b75")
        assert config.load_features("powernukkitx").row("3.0.5") is not None

    def test_dialect_samples(self) -> None:
        with open(LOGS / "powernukkitx.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        for sample in samples:
            level, _, text = PNX_DIALECT.split(sample["line"])
            role, value = sample["role"], sample.get("value")
            if role == "ready":
                assert PNX_DIALECT.is_ready(text), sample
            elif role == "version":
                assert PNX_DIALECT.parse_version(text) == value, sample
            elif role == "player_in_world":
                assert PNX_DIALECT.player_entered(text) == value, sample
            elif role == "tick":
                assert PNX_DIALECT.parse_tick(text) == value, sample
            elif role == "level":
                assert level == value, sample
            elif role == "strip":
                assert text == value, sample
            elif role == "not_benign":
                assert level == "error" and not PNX_DIALECT.is_benign(text), sample
            else:
                raise AssertionError(f"unknown role {role}")

    @pytest.mark.parametrize(
        ("banner", "version"),
        [
            ("This server is running PowerNukkitX version 3.0.5-SNAPSHOT (git-9813b75) (API 3.0.5)", "3.0.5"),
            ("This server is running PowerNukkitX version 3.0.6 (git-abcdef0) (API 3.0.6)", "3.0.6"),
        ],
    )
    def test_banner_version(self, banner: str, version: str) -> None:
        assert PNX_DIALECT.parse_version(banner) == version

    def test_session_log(self) -> None:
        lines = [line(raw) for raw in (LOGS / "powernukkitx-session.log").read_text(encoding="utf-8").splitlines()]
        assert [t for t in (PNX_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [2, 203]
        assert [p for p in (PNX_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00"]
        applied = [make_adapter().applied("randomtickspeed", e) for e in lines]
        assert [a for a in applied if a] == [{"randomtickspeed": 3}]
        assert {e.logger for e in lines if e.logger} >= {"main", "Netty Server IO #1"}


class TestPrepare:
    def test_fresh_world(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        server = layout.server_dir
        yml = yaml_scalars((server / "pnx.yml").read_text(encoding="utf-8"))
        assert yml["settings.port"] == "19672" and yml["settings.defaultLevelName"] == "bench"
        assert yml["gameplay-settings.viewDistance"] == "10"
        assert yml["network-settings.raknet.packetLimit"] == "1000000"
        assert yml["level-settings.loadAllLevels"] == "false" and yml["level-settings.autoTickRate"] == "false"
        world_config = json.loads((server / "worlds" / "bench" / "config.json").read_text(encoding="utf-8"))
        assert {g["seed"] for g in world_config["generators"].values()} == {20261010}
        assert layout.world.fresh and layout.world.path == server / "worlds" / "bench" / "db"
        assert not layout.world.path.exists(), "the level database is created by the server"

        by_key = {(o.file, o.key): o for o in layout.overrides}
        assert by_key[("pnx.yml", "gameplay-settings.viewDistance")].default == "8"
        assert by_key[("pnx.yml", "misc-settings.enableMetrics")].category is OverrideCategory.HARNESS
        assert by_key[("worlds/bench/config.json", "generators.*.seed")].value == "20261010"
        assert ("pnx.yml", "gameplay-settings.difficulty") not in by_key, "easy is PowerNukkitX's default"
        assert ("pnx.yml", "network-settings.transport") not in by_key
        assert [c.path for c in layout.config_files] == ["pnx.yml", "worlds/bench/config.json"]

    def test_read_back_after_the_server_rewrote_its_config(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        (layout.server_dir / "pnx.yml").write_text(REWRITTEN_PNX_YML, encoding="utf-8")
        assert adapter.readback(layout) == []
        config_json = layout.server_dir / "worlds" / "bench" / "config.json"
        data = json.loads(config_json.read_text(encoding="utf-8"))
        data["generators"]["0"]["seed"] = 1
        config_json.write_text(json.dumps(data), encoding="utf-8")
        assert any("seeds read back" in p for p in adapter.readback(layout))
        (layout.server_dir / "pnx.yml").write_text(REWRITTEN_PNX_YML.replace("viewDistance: 10", "viewDistance: 8"))
        assert any("viewDistance" in p for p in adapter.readback(layout))

    def test_template_world(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        ctx = make_ctx(tmp_path, adapter, source="template")
        world = tmp_path / "built" / "bench"
        (world / "db").mkdir(parents=True)
        (world / "db" / "000001.ldb").write_bytes(b"chunks")
        (world / "config.json").write_text('{"generators": {}}', encoding="utf-8")
        report = pregen.TemplateReport(
            server="powernukkitx",
            version="3.0.5",
            scenario="test",
            pipeline=pregen.PIPELINE_TOUR,
            world=pregen.world_key(ctx.scenario),
            min_coverage=0.99,
            cells=tuple(pregen.CellCoverage(i, 0, 0, 441, 441) for i in range(ctx.scenario.bots.n)),
        )
        template = Cache(tmp_path / "cache").template_dir("powernukkitx", "3.0.5", "test")
        stored = pregen.store_template(Cache(tmp_path / "cache"), template, world, report)
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter, template=template, source="template"))
        assert not layout.world.fresh and layout.world.template_sha256 == stored.tree_sha256
        assert (layout.server_dir / "worlds" / "bench" / "db" / "000001.ldb").read_bytes() == b"chunks"
        assert (layout.server_dir / "worlds" / "bench" / "config.json").read_text() == '{"generators": {}}'
        assert not [o for o in layout.overrides if o.file.endswith("config.json")]
        assert adapter.readback(layout) == []

    def test_template_scenario_needs_a_template(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        with pytest.raises(ConfigError, match="prepare-world"):
            adapter.prepare(install, make_ctx(tmp_path, adapter, source="template"))

    def test_launch(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        spec = adapter.launch(layout)
        assert spec.argv[1:] == (
            "-Xms8G",
            "-Xmx8G",
            "-Dstdout.encoding=UTF-8",
            "-Dstderr.encoding=UTF-8",
            "-jar",
            "powernukkitx.jar",
        )
        assert Path(spec.argv[0]) == tmp_path / "jdk-runtime" / "jdk" / "bin" / "java"


class TestCommands:
    def test_positions_come_from_the_bots(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_positions() is None and adapter.parse_positions([]) is None
        assert PNX_DIALECT.tick_cmd == "time query gametime"
        assert adapter.cmd_effects("@a")[0] == "effect @a resistance infinite 255 true"

    def test_chaos_dialect(self) -> None:
        dialect = make_adapter().chaos_dialect()
        assert dialect.render("tp_dimension", player="a07", dimension="nether", x=160, y=100, z=160) == [
            "execute in nether run tp a07 160 100 160"
        ]
        assert dialect.render("randomtickspeed", value=3) == ["gamerule randomtickspeed 3"]
        assert dialect.render("tickingarea_add", name="bbench0", x=20000, z=-30000, y=64, radius=2) == [
            "tickingarea add circle 20000 64 -30000 2 bbench0"
        ]
        for action in ("loot", "playerwaypoints", "cleanup"):
            with pytest.raises(UnsupportedAction):
                dialect.render(action)
            assert dialect.unsupported[action]

    def test_every_catalogue_command_is_rendered_or_unsupported(self) -> None:
        catalogue = ROOT / "scenarios" / "chaos-actions.toml"
        if not catalogue.is_file():
            pytest.skip("no chaos catalogue")
        commands = tomllib.loads(catalogue.read_text(encoding="utf-8")).get("command", {})
        dialect = make_adapter().chaos_dialect()
        for name, entry in commands.items():
            assert dialect.supports(name) or name in dialect.unsupported, name
            if dialect.supports(name):
                assert dialect.render(name, **entry.get("example", {}))
