# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, config, fetch
from bedrock_bench.adapters import nukkit
from bedrock_bench.adapters.base import UnsupportedAction
from bedrock_bench.adapters.nukkit import NUKKIT_DIALECT, NukkitAdapter, build_plugin
from bedrock_bench.adapters.standalone import Runtime, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.model import ArtifactRef, Grid, Install, LogLine, OverrideCategory, Placement, RunContext, ThreadInfo
from bedrock_bench.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

#: nukkit.yml in the shape Nukkit ships (one-space indentation), with the keys the harness sets (synthetic).
SHIPPED_YML = """# Advanced configuration file for Nukkit
settings:
 language: "eng"
 async-workers: auto
debug:
 #If > 1, it will show debug messages in the console
 level: 1
level-settings:
 default-format: leveldb
 auto-tick-rate: true
 auto-tick-rate-limit: 20
chunk-ticking:
 tick-radius: 3
ticks-per:
 autosave: 6000
player:
 save-player-data: true
worlds:
 world:
  generator: normal
 nether:
  generator: nether
"""

JCMD_OUTPUT = '"main" #1 [4242] prio=5 os_prio=0 cpu=5.00ms elapsed=9.00s tid=0x1 nid=4242 runnable\n'


def make_adapter(variant: str | None = None) -> NukkitAdapter:
    arm = config.arm_from_ref("nukkit@b3caad6", variant=variant)
    return NukkitAdapter(arm, config.default_machine())


def make_ctx(tmp_path: Path, adapter: NukkitAdapter) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-nukkit-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        scenario=config.parse_scenario({"server": {"csg": False}}, name="test"),
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
    )


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    jar = tmp_path / "nukkit.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("cn/nukkit/Server.class", b"\xca\xfe\xba\xbe")
        zf.writestr("lang/eng/nukkit.yml", SHIPPED_YML)
    ref = ArtifactRef("nukkit.jar", None, sha256_file(jar), jar.stat().st_size, "local")
    entry = config.load_artifacts("nukkit").versions["b3caad6"]
    home = "jdk"
    paths = {
        "home": home,
        "java": f"{home}/bin/java.exe",
        "javac": f"{home}/bin/javac.exe",
        "jcmd": f"{home}/bin/jcmd.exe",
    }
    (tmp_path / "jdk-runtime" / home).mkdir(parents=True)
    (tmp_path / "jdk-runtime" / home / "release").write_text('JAVA_RUNTIME_VERSION="21.0.12.1+1"\n', encoding="utf-8")
    runtime = Runtime(
        "jdk-test", tmp_path / "jdk-runtime", "0" * 64, ArtifactRef("jdk.zip", None, "1" * 64, 1, "github"), paths
    )
    built: list[Path] = []

    def fake_build(javac: Path, server_jar: Path, source: Path, dest: Path) -> None:
        built.append(javac)
        dest.write_bytes(b"plugin jar")

    monkeypatch.setattr(nukkit, "fetch_release", lambda cache, server, version, artifact=None: (jar, ref, entry))
    monkeypatch.setattr(nukkit, "fetch_runtime", lambda cache, key, platform: runtime)
    monkeypatch.setattr(nukkit, "build_plugin", fake_build)
    result = make_adapter().fetch(Cache(tmp_path / "cache"))
    assert built == [tmp_path / "jdk-runtime" / "jdk" / "bin" / "javac.exe"]
    return result


def line(raw: str) -> LogLine:
    level, logger, text = NUKKIT_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("nukkit") is NukkitAdapter
        assert NukkitAdapter.class_problems() == []
        assert NukkitAdapter.protocol == 2193 and not NukkitAdapter.supports_csg
        assert NukkitAdapter.bot_compat_flags == ("-fake-xuid",)
        assert NukkitAdapter.tick_thread_rule == "java_main"
        assert set(NukkitAdapter.variants) == {"no-watchdog"}

    def test_pins(self) -> None:
        catalog = config.load_artifacts("nukkit")
        assert catalog.get("b3caad6", "linux").url.startswith("https://repo.opencollab.dev/")
        assert catalog.versions["b3caad6"].meta["commit"].startswith("b3caad6")
        assert config.load_features("nukkit").row("b3caad6") is not None

    def test_dialect_samples(self) -> None:
        with open(LOGS / "nukkit.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        for sample in samples:
            level, _, text = NUKKIT_DIALECT.split(sample["line"])
            role, value = sample["role"], sample.get("value")
            if role == "ready":
                assert NUKKIT_DIALECT.is_ready(text), sample
            elif role == "version":
                assert NUKKIT_DIALECT.parse_version(text) == value, sample
            elif role == "player_in_world":
                assert NUKKIT_DIALECT.player_entered(text) == value, sample
            elif role == "tick":
                assert NUKKIT_DIALECT.parse_tick(text) == value, sample
            elif role == "level":
                assert level == value, sample
            elif role == "strip":
                assert text == value, sample
            elif role == "not_benign":
                assert level == "error" and not NUKKIT_DIALECT.is_benign(text), sample
            else:
                raise AssertionError(f"unknown role {role}")

    def test_session_log(self) -> None:
        lines = [line(raw) for raw in (LOGS / "nukkit-session.log").read_text(encoding="utf-8").splitlines()]
        assert [v for v in (NUKKIT_DIALECT.parse_version(e.text) for e in lines) if v] == ["b3caad6"]
        assert [t for t in (NUKKIT_DIALECT.parse_tick(e.text) for e in lines) if t is not None] == [3, 203]
        assert [p for p in (NUKKIT_DIALECT.player_entered(e.text) for e in lines) if p] == ["a00"]
        assert make_adapter().parse_positions(lines) is not None


class TestPrepare:
    def test_install_layout(self, install: Install) -> None:
        assert (install.root / "server" / "nukkit.jar").is_file()
        assert (install.root / "server" / "plugins" / "BenchTick.jar").read_bytes() == b"plugin jar"
        assert (install.root / "defaults" / "nukkit.yml").read_text(encoding="utf-8") == SHIPPED_YML
        assert install.extra["commit"].startswith("b3caad6")

    def test_configs_and_overrides(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        server = layout.server_dir
        props = (server / "server.properties").read_text(encoding="utf-8").splitlines()
        for expected in ("server-port=19672", "xbox-auth=off", "allow-flight=on", "rcon.password=",
                         "max-players=200", "difficulty=1", "gamemode=0", "level-name=bench"):  # fmt: skip
            assert expected in props
        yml_text = (server / "nukkit.yml").read_text(encoding="utf-8")
        yml = yaml_scalars(yml_text)
        assert yml["level-settings.auto-tick-rate"] == "false"
        assert yml["chunk-ticking.tick-radius"] == "4"
        assert yml["worlds"] == "{}" and "generator" not in yml_text
        assert yml["debug.level"] == "1", "the watchdog stays on without the variant"
        assert yaml_scalars((server / "plugins" / "bStats" / "config.yml").read_text(encoding="utf-8")) == {
            "enabled": "false"
        }
        by_key = {(o.file, o.key): o for o in layout.overrides}
        assert by_key[("nukkit.yml", "worlds")].default == "world, nether"
        assert by_key[("nukkit.yml", "level-settings.auto-tick-rate")].category is OverrideCategory.PARITY
        assert by_key[("server.properties", "rcon.password")].value == ""
        assert ("server.properties", "view-distance") not in by_key, "Nukkit's default is already 10"
        assert by_key[("<command line>", "-Xmx")].value == "8G"
        assert not [o for o in layout.overrides if o.category is OverrideCategory.RESCUE]
        assert [c.path for c in layout.config_files] == ["server.properties", "nukkit.yml", "plugins/bStats/config.yml"]
        assert adapter.readback(layout) == []

    def test_no_watchdog_variant(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter("no-watchdog")
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        assert yaml_scalars((layout.server_dir / "nukkit.yml").read_text(encoding="utf-8"))["debug.level"] == "2"
        rescue = [o for o in layout.overrides if o.category is OverrideCategory.RESCUE]
        assert [(o.key, o.default, o.value) for o in rescue] == [("debug.level", "1", "2")]
        assert adapter.facts(layout, None)["watchdog"].startswith("off")  # type: ignore[arg-type]

    def test_launch(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        spec = adapter.launch(layout)
        assert Path(spec.argv[0]) == tmp_path / "jdk-runtime" / "jdk" / "bin" / "java.exe"
        assert spec.argv[1:] == (
            "-Xms8G",
            "-Xmx8G",
            "-Dstdout.encoding=UTF-8",
            "-Dstderr.encoding=UTF-8",
            "-jar",
            "nukkit.jar",
        )
        assert spec.stdin_lines == (), "nukkit.yml exists, so Nukkit never asks for a language"
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert facts["jvm"] == "21.0.12.1+1" and facts["jvm_heap_mib"] == 8192 and facts["allocator"] == "jvm"

    def test_tick_thread_from_jcmd(self, install: Install, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        adapter.launch(layout)

        class Proc:
            pid = 77

        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: Any) -> str:
            calls.append(command)
            return JCMD_OUTPUT

        monkeypatch.setattr("bedrock_bench.adapters.standalone.run_tool", fake_run)
        adapter.server_process(Proc())  # type: ignore[arg-type]
        threads = [ThreadInfo(1, "java"), ThreadInfo(4242, "Thread-0")]
        assert adapter.tick_thread(threads) == 4242
        assert adapter.tick_thread(threads) == 4242
        assert len(calls) == 1 and calls[0][1:] == ["77", "Thread.print"]
        assert adapter.tick_thread([ThreadInfo(5, "main")]) == 5, "falls back to the thread named main"

    def test_tick_thread_when_jcmd_fails(
        self, install: Install, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = make_adapter()
        adapter.launch(adapter.prepare(install, make_ctx(tmp_path, adapter)))

        def failing(command: list[str], **_: Any) -> str:
            raise fetch.FetchError("jcmd failed")

        monkeypatch.setattr("bedrock_bench.adapters.standalone.run_tool", failing)
        adapter.server_process(type("Proc", (), {"pid": 1})())  # type: ignore[arg-type]
        assert adapter.tick_thread([ThreadInfo(3, "Thread-0")]) is None


class TestCommands:
    def test_effects_and_helper_commands(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_effects("@a") == []
        assert adapter.cmd_effects("a07")[0] == "effect a07 resistance 1000000 255 true"
        assert NUKKIT_DIALECT.tick_cmd == "gametime" and adapter.cmd_positions() == "positions"
        grid = Grid()
        assert adapter.pregen_command(12, grid, 100) == "pregen 12 11 640 1280 100"

    def test_chaos_dialect(self) -> None:
        dialect = make_adapter().chaos_dialect()
        assert dialect.render("summon", player="a07", entity="cow", dx=2, dy=1, dz=-3) == ["summon Cow a07"]
        assert dialect.render("summon", player="a07", entity="iron_golem") == ["summon IronGolem a07"]
        assert dialect.render("tp_dimension", player="a07", dimension="nether", x=160, y=100, z=160) == [
            "dim a07 nether 160 100 160"
        ]
        assert dialect.render("fill", player="a07", dx1=-2, dy1=-1, dz1=-2, dx2=2, dy2=-1, dz2=2, block="farmland") == [
            "fillnear a07 -2 -1 -2 2 -1 2 farmland"
        ]
        assert dialect.render("setblock", player="a07", dx=1, dy=-1, dz=1, block="grass_block") == [
            "fillnear a07 1 -1 1 1 -1 1 grass_block"
        ]
        assert dialect.render("cleanup") == ["kill @e"]
        with pytest.raises(ValueError):
            dialect.render("fill", player="a07", dx1=0, dy1=0, dz1=0, dx2=0, dy2=0, dz2=0, block="diamond_block")
        with pytest.raises(UnsupportedAction):
            dialect.render("randomtickspeed", value=3)
        assert "console" in dialect.unsupported["randomtickspeed"]

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


# --------------------------------------------------------------------------------------------------------------------
# The helper plugin build
# --------------------------------------------------------------------------------------------------------------------


def find_javac() -> Path | None:
    found = shutil.which("javac")
    return Path(found) if found else None


@pytest.mark.skipif(find_javac() is None, reason="needs a JDK (javac) on PATH")
def test_build_plugin_is_reproducible(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    (source / "src" / "demo").mkdir(parents=True)
    (source / "src" / "demo" / "Demo.java").write_text("package demo;\npublic final class Demo {}\n", encoding="utf-8")
    (source / "plugin.yml").write_bytes(b"name: Demo\r\nmain: demo.Demo\r\n")
    server_jar = tmp_path / "server.jar"
    with zipfile.ZipFile(server_jar, "w") as zf:
        zf.writestr("placeholder.txt", "")
    javac = find_javac()
    assert javac is not None
    build_plugin(javac, server_jar, source, tmp_path / "one.jar")
    build_plugin(javac, server_jar, source, tmp_path / "two.jar")
    assert (tmp_path / "one.jar").read_bytes() == (tmp_path / "two.jar").read_bytes()
    with zipfile.ZipFile(tmp_path / "one.jar") as zf:
        assert zf.namelist() == ["demo/Demo.class", "plugin.yml"]
        assert zf.read("plugin.yml") == b"name: Demo\nmain: demo.Demo\n"


def test_build_plugin_needs_sources(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(config.ConfigError, match="no Java sources"):
        build_plugin(Path("javac"), tmp_path / "server.jar", tmp_path, tmp_path / "out.jar")


def test_helper_sources_are_committed() -> None:
    plugin = ROOT / "servers" / "nukkit" / "plugin"
    assert sorted(p.name for p in (plugin / "src" / "bench").glob("*.java")) == ["BenchTick.java", "PreGen.java"]
    manifest = (plugin / "plugin.yml").read_text(encoding="utf-8")
    for command in ("gametime", "positions", "fillnear", "dim", "pregen"):
        assert f"\n  {command}:\n" in manifest
