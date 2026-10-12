# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import shutil
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, config, fetch, pregen
from bedrock_bench.adapters import allay
from bedrock_bench.adapters.allay import ALLAY_DIALECT, AllayAdapter, thread_from_jcmd, world_settings_problems
from bedrock_bench.adapters.standalone import Runtime, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.model import ArtifactRef, Install, LogLine, OverrideCategory, Placement, RunContext, ThreadInfo
from bedrock_bench.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

#: server-settings.yml as Allay 0.14.0 rewrote it after a boot with the spread100 settings (shortened, no comments).
REWRITTEN_SERVER_SETTINGS = """generic-settings:
  motd: An allay-powered server
  max-player-count: 200
  default-game-mode: SURVIVAL
  default-difficulty: EASY
  enable-gui: false
network-settings:
  ip: 0.0.0.0
  port: 19672
  enablev6: false
  ipv6: '::'
  xbox-auth: false
  raknet-packet-limit: 2147483647
world-settings:
  tick-radius: 4
  view-distance: 10
storage-settings:
  save-player-data: true
  chunk-auto-save-cycle: 6000
bstats-settings:
  enable: false
  server-uuid: 00000000-0000-0000-0000-000000000000
"""

#: worlds/world-settings.yml as Allay 0.14.0 rewrote it on stop (verbatim).
REWRITTEN_WORLD_SETTINGS = """# Height settings shared by the built-in dimensions in all worlds
built-in-dimensions:
  minecraft:overworld:
    min-height: -64
    max-height: 319
  minecraft:nether:
    min-height: 0
    max-height: 127
  minecraft:the_end:
    min-height: 0
    max-height: 255
worlds:
  bench:
    storage-type: LEVELDB
    use-virtual-thread: false
    dimensions:
      minecraft:overworld:
        generator-type: FLAT
        generator-preset: ''
        enable-light-calculation: true
    overworld: null
    nether: null
    the-end: null
# The default world is the world that newly joined players will be in
default-world: bench
"""

#: ``jcmd <pid> Thread.print`` lines from a real Allay 0.14.0 boot.
JCMD_OUTPUT = (
    '"main" #1 [28840] prio=5 os_prio=0 cpu=3625.00ms elapsed=11.43s tid=0x000001b479aef740 nid=28840 waiting on '
    "condition  [0x000000a0962ff000]\n"
    '"World Thread #bench" #55 [52604] prio=5 os_prio=0 cpu=31.25ms elapsed=7.33s tid=0x000001b460b6d400 nid=52604 '
    "waiting on condition  [0x000000a0995ff000]\n"
    '"GC Thread#0" os_prio=2 cpu=31.25ms elapsed=11.43s tid=0x000001b479b44250 nid=22420 runnable\n'
)


def make_adapter() -> AllayAdapter:
    return AllayAdapter(config.arm_from_ref("allay@0.14.0"), config.default_machine())


def make_ctx(
    tmp_path: Path, adapter: AllayAdapter, template: Path | None = None, difficulty: str = "easy", **world: object
) -> RunContext:
    server = {"csg": False, "difficulty": difficulty}
    return RunContext(
        run_id="20260101T120000Z-allay-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        scenario=config.parse_scenario({"server": server, "world": world}, name="test"),
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
        template=template,
    )


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    jar = tmp_path / "allay.jar"
    jar.write_bytes(b"PK jar")
    ref = ArtifactRef("allay.jar", None, sha256_file(jar), jar.stat().st_size, "local")
    entry = config.load_artifacts("allay").versions["0.14.0"]
    paths = {"home": "jdk", "java": "jdk/bin/java", "javac": "jdk/bin/javac", "jcmd": "jdk/bin/jcmd"}
    (tmp_path / "jdk-runtime" / "jdk").mkdir(parents=True)
    (tmp_path / "jdk-runtime" / "jdk" / "release").write_text('JAVA_RUNTIME_VERSION="21.0.12.1+1-LTS"\n')
    runtime = Runtime(
        "jdk-test", tmp_path / "jdk-runtime", "0" * 64, ArtifactRef("jdk.zip", None, "1" * 64, 1, "github"), paths
    )
    built: list[Path] = []

    def fake_build(javac: Path, server_jar: Path, source: Path, dest: Path) -> None:
        built.append(source)
        dest.write_bytes(b"plugin jar")

    monkeypatch.setattr(allay, "fetch_release", lambda cache, server, version, artifact=None: (jar, ref, entry))
    monkeypatch.setattr(allay, "fetch_runtime", lambda cache, key, platform: runtime)
    monkeypatch.setattr(allay, "build_helper", fake_build)
    result = make_adapter().fetch(Cache(tmp_path / "cache"))
    assert built == [ROOT / "configs" / "allay"]
    return result


def line(raw: str) -> LogLine:
    level, logger, text = ALLAY_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("allay") is AllayAdapter
        assert AllayAdapter.class_problems() == []
        assert AllayAdapter.protocol == 1001 and not AllayAdapter.supports_csg
        assert AllayAdapter.bot_compat_flags == ()
        assert AllayAdapter.generator == "flat"
        assert AllayAdapter.tick_thread_rule == "name:World Thread #bench"
        assert AllayAdapter.variants == {}

    def test_pins(self) -> None:
        catalog = config.load_artifacts("allay")
        spec = catalog.get("0.14.0", "linux")
        assert spec.url.endswith("/0.14.0/allay-server-0.14.0-058fef833-shaded.jar")
        assert spec.sha256 == "fc944c56be559a1a9df3097b7f7974ea7d644677d7a6c14b2b86d25c070f9c03"
        meta = catalog.versions["0.14.0"].meta
        assert meta["commit"].startswith("058fef833") and meta["protocol"] == 1001
        row = config.load_features("allay").row("0.14.0")
        assert row is not None and row.cells["terrain.vanilla_generator"].cls.value == "absent"

    def test_dialect_samples(self) -> None:
        with open(LOGS / "allay.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        for sample in samples:
            level, _, text = ALLAY_DIALECT.split(sample["line"])
            role, value = sample["role"], sample.get("value")
            if role == "ready":
                assert ALLAY_DIALECT.is_ready(text), sample
            elif role == "version":
                assert ALLAY_DIALECT.parse_version(text) == value, sample
            elif role == "player_in_world":
                assert ALLAY_DIALECT.player_entered(text) == value, sample
            elif role == "tick":
                assert ALLAY_DIALECT.parse_tick(text) == value, sample
            elif role == "level":
                assert level == value, sample
            elif role == "strip":
                assert text == value, sample
            elif role == "not_benign":
                assert level == "error" and not ALLAY_DIALECT.is_benign(text), sample
            else:
                raise AssertionError(f"unknown role {role}")

    def test_session_log(self) -> None:
        lines = [line(raw) for raw in (LOGS / "allay-session.log").read_text(encoding="utf-8").splitlines()]
        assert [v for v in (ALLAY_DIALECT.parse_version(e.text) for e in lines) if v] == ["0.14.0", "0.14.0"]
        assert [t for t in (ALLAY_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [41, 271]
        assert [p for p in (ALLAY_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00"]
        assert make_adapter().parse_positions(lines) == []
        assert {e.logger for e in lines if e.logger} >= {"Allay", "AllayServer", "AllayWorld"}


class TestPrepare:
    def test_install_layout(self, install: Install) -> None:
        assert (install.root / "server" / "allay.jar").is_file()
        assert (install.root / "server" / "plugins" / "BenchTick.jar").read_bytes() == b"plugin jar"
        assert not any((install.root / "defaults").iterdir()), "Allay writes its defaults itself"
        assert install.extra["commit"].startswith("058fef833")

    def test_configs_and_overrides(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        server = layout.server_dir
        settings = yaml_scalars((server / "server-settings.yml").read_text(encoding="utf-8"))
        assert settings["network-settings.port"] == "19672"
        assert settings["network-settings.enablev6"] == "false" and settings["network-settings.xbox-auth"] == "false"
        assert settings["network-settings.raknet-packet-limit"] == "2147483647"
        assert settings["generic-settings.default-game-mode"] == "SURVIVAL"
        assert settings["generic-settings.default-difficulty"] == "EASY"
        assert settings["world-settings.view-distance"] == "10"
        worlds = (server / "worlds" / "world-settings.yml").read_text(encoding="utf-8")
        assert "\n  bench:\n" in worlds and worlds.endswith("default-world: bench\n") and "{level_name}" not in worlds
        assert world_settings_problems(worlds, "bench") == []
        assert layout.world.fresh and layout.world.generator == "flat"
        assert layout.world.path == server / "worlds" / "bench" and not layout.world.path.exists()

        by_key = {(o.file, o.key): o for o in layout.overrides}
        assert by_key[("server-settings.yml", "world-settings.view-distance")].default == "8"
        assert by_key[("server-settings.yml", "generic-settings.default-game-mode")].default == "CREATIVE"
        assert by_key[("server-settings.yml", "bstats-settings.enable")].category is OverrideCategory.HARNESS
        assert by_key[("worlds/world-settings.yml", "worlds")].value == "bench"
        assert by_key[("plugins/BenchTick.jar", "console on piped stdin")].value == "on"
        assert by_key[("<command line>", "-Dsentry.dsn")].value == ""
        assert ("server-settings.yml", "world-settings.tick-radius") not in by_key, "Allay's default is already 4"
        assert not [o for o in layout.overrides if o.category.footnoted]
        assert [c.path for c in layout.config_files] == ["server-settings.yml", "worlds/world-settings.yml"]
        assert adapter.readback(layout) == []

    def test_read_back_after_the_server_rewrote_its_configs(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        settings = layout.server_dir / "server-settings.yml"
        worlds = layout.server_dir / "worlds" / "world-settings.yml"
        settings.write_text(REWRITTEN_SERVER_SETTINGS, encoding="utf-8")
        worlds.write_text(REWRITTEN_WORLD_SETTINGS, encoding="utf-8")
        assert adapter.readback(layout) == []
        worlds.write_text(REWRITTEN_WORLD_SETTINGS.replace("generator-type: FLAT", "generator-type: VOID"))
        assert adapter.readback(layout) == [
            "worlds/world-settings.yml: generators were written as ['FLAT'] and read back as ['VOID']"
        ]
        worlds.write_text(REWRITTEN_WORLD_SETTINGS.replace("bench", "world"))
        problems = adapter.readback(layout)
        assert any("default-world" in p for p in problems) and any("worlds were written" in p for p in problems)
        worlds.unlink()
        assert adapter.readback(layout) == ["worlds/world-settings.yml is missing after the run"]
        worlds.write_text(REWRITTEN_WORLD_SETTINGS, encoding="utf-8")
        settings.write_text(REWRITTEN_SERVER_SETTINGS.replace("view-distance: 10", "view-distance: 8"))
        assert any("view-distance" in p for p in adapter.readback(layout))

    def test_template_world(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        ctx = make_ctx(tmp_path, adapter, source="template")
        world = tmp_path / "built" / "bench"
        (world / "db").mkdir(parents=True)
        (world / "db" / "000001.ldb").write_bytes(b"chunks")
        report = pregen.TemplateReport(
            server="allay",
            version="0.14.0",
            scenario="test",
            pipeline=pregen.PIPELINE_TOUR,
            world=pregen.world_key(ctx.scenario),
            min_coverage=0.99,
            cells=tuple(pregen.CellCoverage(i, 0, 0, 441, 441) for i in range(ctx.scenario.bots.n)),
        )
        cache = Cache(tmp_path / "cache")
        template = cache.template_dir("allay", "0.14.0", "test")
        stored = pregen.store_template(cache, template, world, report)
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter, template=template, source="template"))
        assert not layout.world.fresh and layout.world.template_sha256 == stored.tree_sha256
        assert (layout.server_dir / "worlds" / "bench" / "db" / "000001.ldb").read_bytes() == b"chunks"
        assert (layout.server_dir / "worlds" / "world-settings.yml").is_file()
        assert adapter.readback(layout) == []

    def test_launch(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        spec = adapter.launch(layout)
        assert Path(spec.argv[0]) == tmp_path / "jdk-runtime" / "jdk" / "bin" / "java"
        assert spec.argv[1:] == (
            "-Xms8G",
            "-Xmx8G",
            "-Dstdout.encoding=UTF-8",
            "-Dstderr.encoding=UTF-8",
            "-Dsentry.dsn=",
            "-jar",
            "allay.jar",
        )
        assert spec.stdin_lines == () and spec.cwd == layout.server_dir
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert facts["jvm"] == "21.0.12.1+1-LTS" and facts["jvm_flags"][-1] == "-Dsentry.dsn="
        assert facts["seed"] == "none (flat world)"

    def test_tick_thread_from_jcmd(self, install: Install, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = make_adapter()
        adapter.launch(adapter.prepare(install, make_ctx(tmp_path, adapter, level_name="arena")))
        assert adapter.world_thread() == "World Thread #arena"
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: Any) -> str:
            calls.append(command)
            return JCMD_OUTPUT.replace("#bench", "#arena")

        monkeypatch.setattr(allay, "run_tool", fake_run)
        adapter.server_process(type("Proc", (), {"pid": 77})())  # type: ignore[arg-type]
        threads = [ThreadInfo(28840, "main"), ThreadInfo(52604, "World Thread #arena")]
        assert adapter.tick_thread(threads) == 52604
        assert adapter.tick_thread(threads) == 52604
        assert len(calls) == 1 and calls[0][1:] == ["77", "Thread.print"]

    def test_difficulty_in_allays_spelling(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        assert any("{difficulty_upper}" in pin.template for pin in adapter.config_plan().pins)
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter, difficulty="hard"))
        settings = yaml_scalars((layout.server_dir / "server-settings.yml").read_text(encoding="utf-8"))
        assert settings["generic-settings.default-difficulty"] == "HARD"
        assert adapter.readback(layout) == []

    def test_tick_thread_when_jcmd_fails(
        self, install: Install, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter()
        adapter.launch(adapter.prepare(install, make_ctx(tmp_path, adapter)))

        def failing(command: list[str], **_: Any) -> str:
            raise fetch.FetchError("jcmd failed")

        monkeypatch.setattr(allay, "run_tool", failing)
        adapter.server_process(type("Proc", (), {"pid": 1})())  # type: ignore[arg-type]
        threads = [ThreadInfo(3, "main"), ThreadInfo(9, "World Thread #bench")]
        assert adapter.tick_thread(threads) == 9, "falls back to the OS thread name"
        assert adapter.tick_thread([ThreadInfo(3, "main")]) is None


class TestCommands:
    def test_effects_and_helper_commands(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_effects("a07") == [
            "effect a07 resistance 1000000 255 true",
            "effect a07 fire_resistance 1000000 0 true",
            "effect a07 water_breathing 1000000 0 true",
            "effect a07 saturation 1000000 255 true",
        ]
        assert adapter.cmd_effects("@a")[0] == "effect @a resistance 1000000 255 true"
        assert adapter.cmd_tp("a07", 5760.5, 300, 1280.5) == "tp a07 5760.5 300 1280.5"
        assert ALLAY_DIALECT.tick_cmd == "gametime" and adapter.cmd_positions() == "positions"
        prefix = "[19:01:02 INFO] [Console Thread] [AllayServer] "
        reply = [line(prefix + "positions: 1"), line(prefix + "position: a00 1280.50 300.00 1280.50 0")]
        assert [(p.name, p.x, p.y, p.dim) for p in adapter.parse_positions(reply) or []] == [("a00", 1280.5, 300, 0)]

# --------------------------------------------------------------------------------------------------------------------
# Helper plugin and world settings
# --------------------------------------------------------------------------------------------------------------------


def test_helper_sources_are_committed() -> None:
    folder = ROOT / "configs" / "allay"
    assert [p.name for p in allay.helper_files(folder)] == ["plugin.json", "BenchTick.java"]
    manifest = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "BenchTick" and manifest["entrance"] == "bench.BenchTick"
    source = (folder / "BenchTick.java").read_text(encoding="utf-8")
    for text in ('"gametime"', '"positions"', '"Gametime is "', '"positions: "', "AllayTerminalConsole"):
        assert text in source
    template = (folder / "world-settings.yml").read_text(encoding="utf-8")
    assert template.count("{level_name}") == 3 and "generator-type: FLAT" in template


def test_thread_from_jcmd() -> None:
    assert thread_from_jcmd(JCMD_OUTPUT, "World Thread #bench") == 52604
    assert thread_from_jcmd(JCMD_OUTPUT, "main") == 28840
    assert thread_from_jcmd(JCMD_OUTPUT, "World Thread #world") is None
    assert thread_from_jcmd(JCMD_OUTPUT, "GC Thread#0") is None, "VM threads have no Java thread number"
    old_jdk = '"World Thread #bench" #55 prio=5 os_prio=0 tid=0x1 nid=0xcd7c runnable'
    assert thread_from_jcmd(old_jdk, "World Thread #bench") == 0xCD7C
    assert thread_from_jcmd("", "main") is None


def test_world_settings_problems() -> None:
    assert world_settings_problems(REWRITTEN_WORLD_SETTINGS, "bench") == []
    two_dimensions = REWRITTEN_WORLD_SETTINGS.replace(
        "    overworld: null", "      minecraft:nether:\n        generator-type: VOID\n    overworld: null"
    )
    assert world_settings_problems(two_dimensions, "bench") == [
        "worlds/world-settings.yml: generators were written as ['FLAT'] and read back as ['FLAT', 'VOID']"
    ]


def find_javac() -> Path | None:
    found = shutil.which("javac")
    return Path(found) if found else None


@pytest.mark.skipif(find_javac() is None, reason="needs a JDK (javac) on PATH")
def test_build_helper_packs_plugin_json(tmp_path: Path) -> None:
    source = tmp_path / "helper"
    source.mkdir()
    (source / "Demo.java").write_text("package demo;\npublic final class Demo {}\n", encoding="utf-8")
    (source / "plugin.json").write_bytes(b'{\r\n  "name": "Demo"\r\n}\r\n')
    server_jar = tmp_path / "server.jar"
    with zipfile.ZipFile(server_jar, "w") as zf:
        zf.writestr("placeholder.txt", "")
    javac = find_javac()
    assert javac is not None
    allay.build_helper(javac, server_jar, source, tmp_path / "one.jar")
    allay.build_helper(javac, server_jar, source, tmp_path / "two.jar")
    assert (tmp_path / "one.jar").read_bytes() == (tmp_path / "two.jar").read_bytes()
    with zipfile.ZipFile(tmp_path / "one.jar") as zf:
        assert zf.namelist() == ["demo/Demo.class", "plugin.json"]
        assert zf.read("plugin.json") == b'{\n  "name": "Demo"\n}\n'
