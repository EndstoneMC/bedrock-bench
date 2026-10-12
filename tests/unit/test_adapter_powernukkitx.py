# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from bedrock_bench import adapters, config
from bedrock_bench.adapters import powernukkitx
from bedrock_bench.adapters.powernukkitx import PNX_DIALECT, PowerNukkitXAdapter
from bedrock_bench.adapters.standalone import Runtime, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.model import ArtifactRef, Install, LogLine, OverrideCategory, Placement, RunContext
from bedrock_bench.provenance import sha256_file
from bedrock_bench.workload import WORKLOAD, with_overrides

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"
NIGHTLY = "nightly.86f70bc"

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


def make_adapter(version: str = "3.0.5") -> PowerNukkitXAdapter:
    return PowerNukkitXAdapter(config.arm_from_ref(f"powernukkitx@{version}"), config.default_machine())


def make_ctx(tmp_path: Path, adapter: PowerNukkitXAdapter, **world: object) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-powernukkitx-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        workload=with_overrides(WORKLOAD, {"world": world}),
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
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

    def test_pins(self) -> None:
        catalog = config.load_artifacts("powernukkitx")
        assert catalog.get("3.0.5", "windows").url.endswith("/3.0.5/powernukkitx.jar")
        assert catalog.versions["3.0.5"].meta["commit"].startswith("9813b75")
        assert config.load_features("powernukkitx").row("3.0.5") is not None

    def test_nightly_pin(self) -> None:
        catalog = config.load_artifacts("powernukkitx")
        entry = catalog.versions[NIGHTLY]
        assert entry.meta["commit"].startswith("86f70bc") and entry.meta["runtime"] == "jdk-25.0.4.1"
        spec = catalog.get(NIGHTLY, "linux")
        assert spec.url.endswith("/nightly/powernukkitx.jar")
        assert spec.name != catalog.get("3.0.5", "linux").name, "both jars must fit in one --artifact-dir"
        runtimes = config.load_artifacts(config.project_root() / config.RUNTIMES_FILE)
        for platform in ("windows", "linux"):
            jdk = runtimes.get(entry.meta["runtime"], platform)
            assert {"home", "java", "javac", "jcmd"} <= set(jdk.extra), platform

    def test_nightly_feature_range(self) -> None:
        features = config.load_features("powernukkitx")
        assert features.row(NIGHTLY).prefix == "nightly"
        assert features.row("nightly.0123abc").prefix == "nightly", "a later nightly keeps the range"
        assert features.row("3.0.5").prefix == "3.0"

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
            ("This server is running PowerNukkitX version nightly-SNAPSHOT (git-86f70bc) (API 3.0.5)", NIGHTLY),
            (
                "This server is running PowerNukkitX version nightly-SNAPSHOT (git-0123abc) (API 3.0.6)",
                "nightly.0123abc",
            ),
        ],
    )
    def test_banner_version(self, banner: str, version: str) -> None:
        assert PNX_DIALECT.parse_version(banner) == version

    def test_version_command_reply_is_not_the_banner(self) -> None:
        reply = (
            "This server is running PowerNukkitX nightly-SNAPSHOT (git-86f70bc) implementing API version 3.0.5 "
            "for Minecraft: BE 1.26.50 (protocol version 2193)"
        )
        assert PNX_DIALECT.parse_version(reply) is None

    def test_session_log(self) -> None:
        lines = [line(raw) for raw in (LOGS / "powernukkitx-session.log").read_text(encoding="utf-8").splitlines()]
        assert [t for t in (PNX_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [2, 203]
        assert [p for p in (PNX_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00"]
        assert {e.logger for e in lines if e.logger} >= {"main", "Netty Server IO #1"}

    def test_nightly_session_log(self) -> None:
        raw = (LOGS / "powernukkitx-nightly-session.log").read_text(encoding="utf-8").splitlines()
        lines = [line(r) for r in raw]
        assert [v for v in (PNX_DIALECT.parse_version(e.text) for e in lines) if v] == [NIGHTLY]
        assert sum(PNX_DIALECT.is_ready(e.text) for e in lines) == 1
        assert [t for t in (PNX_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [20, 610, 1070, 1830]
        assert [p for p in (PNX_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00", "a01", "a03", "a02"]
        warnings = [e for e in lines if e.raw.startswith("WARNING: ")]
        assert warnings and all(e.level is None and e.text == e.raw for e in warnings)
        assert {e.level for e in lines} == {None, "info", "warn"}


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

    def test_nightly_fetch_uses_the_pinned_jdk(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        jar = tmp_path / "powernukkitx.jar"
        jar.write_bytes(b"PK nightly jar")
        ref = ArtifactRef("powernukkitx.jar", None, sha256_file(jar), jar.stat().st_size, "local")
        entry = config.load_artifacts("powernukkitx").versions[NIGHTLY]
        asked: list[str] = []

        def runtime(cache: Cache, key: str, platform: str) -> Runtime:
            asked.append(key)
            return Runtime(key, tmp_path / "jdk", "0" * 64, ArtifactRef("jdk.zip", None, "1" * 64, 1, "github"), {})

        monkeypatch.setattr(
            powernukkitx, "fetch_release", lambda cache, server, version, artifact=None: (jar, ref, entry)
        )
        monkeypatch.setattr(powernukkitx, "fetch_runtime", runtime)
        install = make_adapter(NIGHTLY).fetch(Cache(tmp_path / "cache"))
        assert asked == ["jdk-25.0.4.1"]
        assert install.extra["runtime"] == "jdk-25.0.4.1" and install.extra["commit"].startswith("86f70bc")

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


class TestFacts:
    def test_recorded_settings_come_from_the_rewritten_config(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        rewritten = REWRITTEN_PNX_YML.replace(
            "  transport: raknet\n", "  transport: raknet\n  upstream-bandwidth: 1000\n"
        ).replace("  tickRadius: 4\n", "  tickRadius: 4\n  generationQueueSize: -1\n")
        (layout.server_dir / "pnx.yml").write_text(rewritten, encoding="utf-8")
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert facts["upstream_bandwidth_mbps"] == "1000" and facts["generation_queue_size"] == "-1"
        assert "leveldb_backend" not in facts, "a key the version lacks is not recorded"
        assert facts["usage_statistics"] == "off"
