# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest

from bedrock_bench import adapters, config
from bedrock_bench.adapters import nukkitmot
from bedrock_bench.adapters.nukkit import plugin_files
from bedrock_bench.adapters.nukkitmot import NUKKITMOT_DIALECT, NukkitMotAdapter
from bedrock_bench.adapters.standalone import Runtime, main_thread_from_jcmd, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.model import ArtifactRef, Install, LogLine, OverrideCategory, Placement, RunContext
from bedrock_bench.provenance import sha256_file
from bedrock_bench.workload import WORKLOAD

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

#: nukkit-mot.yml as Nukkit-MOT rewrites it on start: every key with a comment above it (shortened).
REWRITTEN_YML = """########################################
# Nukkit-MOT Advanced Configuration
# Standard settings are in server.properties
########################################

# Performance and threading settings
performance-settings:
  # Number of async worker threads (auto = CPU cores + 1)
  async-workers: auto
  # Automatically adjust tick rate based on server load
  auto-tick-rate: false
  # Enable thread watchdog
  thread-watchdog: true
  # Ticks between auto-saves (6000 = 5 minutes)
  ticks-per-autosave: 6000

# Network and compression settings
network-settings:
  nethernet:
    # Enabled by default alongside RakNet, set false to disable
    enabled: false
    # Extra ICE STUN/TURN servers
    ice-servers: []
  # Max RakNet packets per 10ms per IP
  rak-packet-limit: 2000
  proxy-protocol-whitelist:
  - 127.0.0.1/32
  - ::1/128

# Chunk loading and ticking settings
chunk-settings:
  # Chunk ticking radius around players
  ticking-radius: 4

# Player data and interaction settings
player-settings:
  # Save player data to disk
  save-player-data: true

# Debug, logging and metrics settings
debug-settings:
  # Enable automatic bug reporting (Sentry)
  automatic-bug-report: false
  # Show update notifications
  update-notifications: false
  # Enable bStats metrics
  bstats-metrics: false

# Custom block settings
custom-block-settings:
  auto-download-vanilla-palette: false
"""

#: A ``jcmd <pid> Thread.print`` line from the pinned build on the pinned JDK.
JCMD_MAIN = (
    '"main" #1 [17380] prio=5 os_prio=0 cpu=2015.62ms elapsed=5.36s tid=0x00000215be868660 nid=17380 '
    "waiting on condition  [0x000000c4723fe000]"
)


def make_adapter() -> NukkitMotAdapter:
    return NukkitMotAdapter(config.arm_from_ref("nukkitmot@9cb0aba"), config.default_machine())


def make_ctx(tmp_path: Path, adapter: NukkitMotAdapter) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-nukkitmot-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=adapter.arm,
        workload=WORKLOAD,
        machine=adapter.machine,
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
    )


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    jar = tmp_path / "Nukkit-MOT-SNAPSHOT.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("cn/nukkit/Nukkit.class", b"\xca\xfe\xba\xbe")
    ref = ArtifactRef("Nukkit-MOT-SNAPSHOT.jar", None, sha256_file(jar), jar.stat().st_size, "local")
    entry = config.load_artifacts("nukkitmot").versions["9cb0aba"]
    home = "jdk"
    paths = {"home": home, "java": f"{home}/bin/java", "javac": f"{home}/bin/javac", "jcmd": f"{home}/bin/jcmd"}
    (tmp_path / "jdk-runtime" / home).mkdir(parents=True)
    (tmp_path / "jdk-runtime" / home / "release").write_text('JAVA_RUNTIME_VERSION="21.0.12.1+1"\n', encoding="utf-8")
    runtime = Runtime(
        "jdk-test", tmp_path / "jdk-runtime", "0" * 64, ArtifactRef("jdk.zip", None, "1" * 64, 1, "github"), paths
    )
    built: list[Path] = []

    def fake_build(javac: Path, server_jar: Path, source: Path, dest: Path) -> None:
        built.append(source)
        dest.write_bytes(b"plugin jar")

    monkeypatch.setattr(nukkitmot, "fetch_release", lambda cache, server, version, artifact=None: (jar, ref, entry))
    monkeypatch.setattr(nukkitmot, "fetch_runtime", lambda cache, key, platform: runtime)
    monkeypatch.setattr(nukkitmot, "build_plugin", fake_build)
    result = make_adapter().fetch(Cache(tmp_path / "cache"))
    assert built == [ROOT / "configs" / "nukkitmot"]
    return result


def line(raw: str) -> LogLine:
    level, logger, text = NUKKITMOT_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


def session() -> list[LogLine]:
    return [line(raw) for raw in (LOGS / "nukkitmot-session.log").read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("nukkitmot") is NukkitMotAdapter
        assert NukkitMotAdapter.class_problems() == []
        assert NukkitMotAdapter.protocol == 2193 and not NukkitMotAdapter.supports_csg
        assert NukkitMotAdapter.bot_compat_flags == (), "offline UUIDs come from the name"
        assert NukkitMotAdapter.tick_thread_rule == "java_main"

    def test_pins(self) -> None:
        catalog = config.load_artifacts("nukkitmot")
        assert catalog.get("9cb0aba", "linux").url.startswith("https://motci.cn/job/Nukkit-MOT/job/master/1592/")
        meta = catalog.versions["9cb0aba"].meta
        assert meta["commit"].startswith("9cb0aba") and meta["build"] == 1592 and meta["protocol"] == 2193
        assert config.load_features("nukkitmot").row("9cb0aba") is not None

    def test_dialect_samples(self) -> None:
        with open(LOGS / "nukkitmot.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        for sample in samples:
            level, _, text = NUKKITMOT_DIALECT.split(sample["line"])
            role, value = sample["role"], sample.get("value")
            if role == "ready":
                assert NUKKITMOT_DIALECT.is_ready(text), sample
            elif role == "version":
                assert NUKKITMOT_DIALECT.parse_version(text) == value, sample
            elif role == "player_in_world":
                assert NUKKITMOT_DIALECT.player_entered(text) == value, sample
            elif role == "tick":
                assert NUKKITMOT_DIALECT.parse_tick(text) == value, sample
            elif role == "level":
                assert level == value, sample
            elif role == "strip":
                assert text == value, sample
            elif role == "not_benign":
                assert level == "error" and not NUKKITMOT_DIALECT.is_benign(text), sample
            else:
                raise AssertionError(f"unknown role {role}")

    def test_session_log(self) -> None:
        lines = session()
        assert [v for v in (NUKKITMOT_DIALECT.parse_version(e.text) for e in lines) if v] == ["9cb0aba"]
        ticks = [t for t in (NUKKITMOT_DIALECT.parse_tick(e.text) for e in lines) if t is not None]
        assert ticks == [47, 338, 1295, 1351]
        players = [p for p in (NUKKITMOT_DIALECT.player_entered(e.text) for e in lines) if p]
        assert players == ["a00", "a01", "a02", "a03"], "the login line is not the in-world line"
        assert {e.logger for e in lines if e.logger} == {"main"}

    def test_positions_from_the_session(self) -> None:
        lines = session()
        start = next(i for i, e in enumerate(lines) if e.text == "positions: 4")
        positions = make_adapter().parse_positions(lines[start:])
        assert positions is not None and [p.name for p in positions] == ["a01", "a03", "a02", "a00"]
        assert positions[0].y == 300.0 and positions[0].dim == 0
        nether = next(i for i, e in enumerate(lines) if e.text == "Moved a02 to nether")
        moved = make_adapter().parse_positions(lines[nether:])
        assert moved is not None and [(p.name, p.dim) for p in moved if p.dim] == [("a02", 1)]
        assert make_adapter().parse_positions(lines[:start]) == [], "an empty server replies positions: 0"


class TestPrepare:
    def test_install_layout(self, install: Install) -> None:
        assert (install.root / "server" / "nukkit-mot.jar").is_file()
        assert (install.root / "server" / "plugins" / "BenchTick.jar").read_bytes() == b"plugin jar"
        assert not any((install.root / "defaults").iterdir()), "the jar ships no config file"
        assert install.extra["commit"].startswith("9cb0aba") and install.extra["build"] == 1592

    def test_configs_and_overrides(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        server = layout.server_dir
        props = (server / "server.properties").read_text(encoding="utf-8").splitlines()
        for expected in ("server-port=19672", "xbox-auth=off", "allow-flight=on", "rcon.password=",
                         "view-distance=10", "max-players=200", "difficulty=1", "level-name=bench"):  # fmt: skip
            assert expected in props
        yml = yaml_scalars((server / "nukkit-mot.yml").read_text(encoding="utf-8"))
        assert yml["network-settings.nethernet.enabled"] == "false"
        assert "network-settings.rak-packet-limit" not in yml
        assert yml["performance-settings.auto-tick-rate"] == "false"
        assert yml["chunk-settings.ticking-radius"] == "4"
        assert yml["custom-block-settings.auto-download-vanilla-palette"] == "false"
        assert "performance-settings.thread-watchdog" not in yml, "the watchdog stays on"
        by_key = {(o.file, o.key): o for o in layout.overrides}
        assert by_key[("server.properties", "view-distance")].default == "8"
        assert by_key[("nukkit-mot.yml", "network-settings.nethernet.enabled")].category is OverrideCategory.HARNESS
        assert by_key[("nukkit-mot.yml", "performance-settings.auto-tick-rate")].category is OverrideCategory.PARITY
        assert by_key[("plugins/BenchTick.jar", "plugin")].value == "installed"
        assert ("server.properties", "gamemode") not in by_key and ("server.properties", "auto-save") not in by_key
        assert ("nukkit-mot.yml", "player-settings.save-player-data") not in by_key
        assert not [o for o in layout.overrides if o.category is OverrideCategory.RESCUE]
        assert [c.path for c in layout.config_files] == ["server.properties", "nukkit-mot.yml"]
        assert adapter.readback(layout) == []

    def test_readback_after_the_server_rewrites_its_config(self, install: Install, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(install, make_ctx(tmp_path, adapter))
        (layout.server_dir / "nukkit-mot.yml").write_text(REWRITTEN_YML, encoding="utf-8")
        assert adapter.readback(layout) == []
        flipped = REWRITTEN_YML.replace("enabled: false", "enabled: true")
        (layout.server_dir / "nukkit-mot.yml").write_text(flipped, encoding="utf-8")
        assert any("nethernet.enabled" in p for p in adapter.readback(layout))

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
            "-jar",
            "nukkit-mot.jar",
        )
        assert spec.stdin_lines == ()
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert facts["jvm"] == "21.0.12.1+1" and facts["watchdog"].startswith("on") and facts["csg_setting"] is False

    def test_tick_thread_line_from_jcmd(self) -> None:
        assert main_thread_from_jcmd(JCMD_MAIN) == 17380


class TestCommands:
    def test_effects_and_helper_commands(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_effects("a07")[0] == "effect a07 resistance 1000000 255 true"
        assert adapter.cmd_effects("@a")[3] == "effect @a saturation 1000000 255 true", "selectors work"
        assert NUKKITMOT_DIALECT.tick_cmd == "gametime" and adapter.cmd_positions() == "positions"
        assert adapter.cmd_tp("a07", 1280.5, 300, 1280.5) == "tp a07 1280.5 300 1280.5"


def test_helper_sources_are_committed() -> None:
    plugin = ROOT / nukkitmot.PLUGIN_SOURCE
    assert [p.name for p in plugin_files(plugin)] == ["plugin.yml", "BenchTick.java"]
    manifest = (plugin / "plugin.yml").read_text(encoding="utf-8")
    for command in ("gametime", "positions"):
        assert f"\n  {command}:\n" in manifest
    assert "RakNetInterface" not in (plugin / "BenchTick.java").read_text(encoding="utf-8")
