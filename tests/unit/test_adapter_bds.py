# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import re
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bedrock_bench import adapters, config, fetch
from bedrock_bench.adapters import bdsfamily
from bedrock_bench.adapters.base import UnsupportedAction
from bedrock_bench.adapters.bds import BdsAdapter
from bedrock_bench.adapters.bdsfamily import BDS_DIALECT, Properties, load_property_pins, parse_querytarget
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    FEATURE_KEYS,
    Arm,
    LogLine,
    Machine,
    OverrideCategory,
    Placement,
    RunContext,
    ThreadInfo,
)

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

#: A shipped server.properties in BDS's shape, with the values the parity checks care about.
SHIPPED_PROPERTIES = """server-name=Dedicated Server
# Used as the server name
gamemode=survival
difficulty=easy
max-players=10
online-mode=true
allow-list=true
server-port=19132
server-portv6=19133
enable-lan-visibility=true
view-distance=32
tick-distance=4
level-name=Bedrock level
level-seed=
max-threads=8
client-side-chunk-generation-enabled=true
server-build-radius-ratio=Disabled
transport=nethernet
"""


def bds_zip(path: Path, properties: str = SHIPPED_PROPERTIES) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in ("bedrock_server.exe", "bedrock_server"):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100755 << 16
            zf.writestr(info, b"server binary")
        zf.writestr("server.properties", properties.replace("\n", "\r\n"))
        zf.writestr("behavior_packs/vanilla/manifest.json", "{}")
    return path


def make_ctx(
    tmp_path: Path, *, csg: bool = True, machine: Machine | None = None, template: Path | None = None, **world: Any
) -> RunContext:
    scenario = config.parse_scenario({"server": {"csg": csg}, "world": world}, name="test")
    machine = machine or config.default_machine()
    return RunContext(
        run_id="20260101T120000Z-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=Arm(id="bds-test", server="bds", version="1.26.52.3"),
        scenario=scenario,
        machine=machine,
        placement=Placement((0, 1), (2, 3), (2, 3), "normal", "physical"),
        template=template,
    )


def line(text: str, t_ns: int = 0) -> LogLine:
    return LogLine(t_ns=t_ns, epoch_ms=0, level="info", logger=None, text=text, raw=text)


@pytest.fixture
def adapter() -> BdsAdapter:
    return BdsAdapter(Arm(id="bds@1.26.52.3", server="bds", version="1.26.52.3"), config.default_machine())


@pytest.fixture
def local_adapter(tmp_path: Path) -> BdsAdapter:
    arm = Arm(id="bds-local", server="bds", version="1.26.52.3", artifact=bds_zip(tmp_path / "bedrock-server.zip"))
    return BdsAdapter(arm, config.default_machine())


def samples(name: str) -> list[dict[str, Any]]:
    with open(LOGS / f"{name}.toml", "rb") as fh:
        return tomllib.load(fh)["sample"]


def check_dialect_samples(dialect: Any, name: str) -> None:
    for sample in samples(name):
        level, _, text = dialect.split(sample["line"])
        role = sample["role"]
        if role == "ready":
            assert dialect.is_ready(text), sample
        elif role == "version":
            assert dialect.parse_version(text) == sample["value"], sample
        elif role == "player_in_world":
            assert dialect.player_entered(text) == sample["value"], sample
        elif role == "tick":
            assert dialect.parse_tick(text) == sample["value"], sample
        elif role == "level":
            assert level == sample["value"], sample
        elif role == "strip":
            assert text == sample["value"], sample
        elif role == "benign":
            assert level == "error" and dialect.is_benign(text), sample
        elif role == "not_benign":
            assert level == "error" and not dialect.is_benign(text), sample
        else:
            raise AssertionError(f"unknown role {role}")


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("bds") is BdsAdapter
        assert BdsAdapter.class_problems() == []
        assert BdsAdapter.features_file == "configs/servers/bds/server.toml"
        assert BdsAdapter.protocol == 2193 and BdsAdapter.supports_csg
        assert BdsAdapter.bot_compat_flags == ()

    def test_dialect_samples(self) -> None:
        check_dialect_samples(BDS_DIALECT, "bds")

    def test_session_levels_and_benign(self) -> None:
        errors = []
        for raw in (LOGS / "bds-session.log").read_text(encoding="utf-8").splitlines():
            level, _, text = BDS_DIALECT.split(raw)
            if level == "error" and not BDS_DIALECT.is_benign(text):
                errors.append(raw)
        assert errors == [], "the transport banner is the only error block and it is benign"

    def test_tick_thread_rule(self, adapter: BdsAdapter) -> None:
        threads = [ThreadInfo(900, "MC_SERVER"), ThreadInfo(120, "Main"), ThreadInfo(450, "MC_SERVER")]
        assert adapter.tick_thread(threads) == 450
        assert adapter.tick_thread([ThreadInfo(1, "Streaming Pool(0)")]) is None
        for pattern in BdsAdapter.thread_groups.values():
            re.compile(pattern)


class TestCommands:
    def test_placement_commands(self, adapter: BdsAdapter) -> None:
        assert adapter.cmd_effects("a00") == [
            "effect a00 resistance infinite 255 true",
            "effect a00 fire_resistance infinite 0 true",
            "effect a00 water_breathing infinite 0 true",
            "effect a00 saturation infinite 255 true",
        ]
        assert adapter.cmd_tp("a07", 1280.0, 300, 1920.5) == "tp a07 1280 300 1920.5"
        assert adapter.cmd_positions() == "querytarget @a"
        assert BDS_DIALECT.tick_cmd == "time query gametime" and BDS_DIALECT.stop_cmd == "stop"

    def test_chaos_dialect(self, adapter: BdsAdapter) -> None:
        chaos = adapter.chaos_dialect()
        rendered = {
            "tp": chaos.render("tp", player="a01", x=1920, y=300, z=1280),
            "tp_dimension": chaos.render("tp_dimension", player="a01", dimension="nether", x=240, y=100, z=160),
            "effects": chaos.render("effects", player="a01"),
            "effect_clear": chaos.render("effect_clear", player="a01"),
            "kill": chaos.render("kill", player="a01"),
            "kick": chaos.render("kick", player="a01"),
            "gamemode": chaos.render("gamemode", player="a01", mode="spectator"),
            "summon": chaos.render("summon", player="a01", entity="zombie", dx=3, dy=1, dz=-2),
            "loot": chaos.render("loot", player="a01", table="entities/zombie"),
            "cleanup": chaos.render("cleanup"),
            "list": chaos.render("list"),
            "randomtickspeed": chaos.render("randomtickspeed", value=1000),
            "playerwaypoints": chaos.render("playerwaypoints", mode="off"),
            "weather": chaos.render("weather", kind="thunder"),
            "fill": chaos.render(
                "fill", player="a01", dx1=-5, dy1=-1, dz1=-5, dx2=5, dy2=-1, dz2=5, block="grass_block"
            ),
            "setblock": chaos.render("setblock", player="a01", dx=1, dy=-1, dz=0, block="farmland"),
            "tickingarea_add": chaos.render("tickingarea_add", name="chaos1", x=-20000, z=45000),
            "tickingarea_remove": chaos.render("tickingarea_remove", name="chaos1"),
        }
        assert set(rendered) == set(chaos.actions)
        assert rendered["tp_dimension"] == ["execute in nether run tp a01 240 100 160"]
        assert rendered["kick"] == ["kick a01 chaos"]
        assert rendered["gamemode"] == ["gamemode spectator a01"]
        assert rendered["summon"] == ["execute at a01 run summon zombie ~3 ~1 ~-2"]
        assert rendered["loot"] == ['execute at a01 run loot spawn ~ ~2 ~ loot "entities/zombie"']
        assert rendered["cleanup"] == ["kill @e[type=!player]"]
        assert rendered["randomtickspeed"] == ["gamerule randomtickspeed 1000"]
        assert rendered["playerwaypoints"] == ["gamerule playerwaypoints off"]
        assert rendered["fill"] == ["execute at a01 run fill ~-5 ~-1 ~-5 ~5 ~-1 ~5 grass_block"]
        assert rendered["setblock"] == ["execute at a01 run setblock ~1 ~-1 ~ farmland"]
        assert rendered["tickingarea_add"] == ["tickingarea add circle -20000 64 45000 2 chaos1"]
        assert len(rendered["effects"]) == 4
        for commands in rendered.values():
            assert all("\n" not in c and c == c.strip() for c in commands)

    @pytest.mark.parametrize(
        ("action", "params"),
        [
            ("gamemode", {"player": "a01", "mode": "hardcore"}),
            ("weather", {"kind": "snow"}),
            ("playerwaypoints", {"mode": "false"}),
            ("tp_dimension", {"player": "a01", "dimension": "moon", "x": 0, "y": 0, "z": 0}),
            ("summon", {"player": "a01", "entity": "zombie; stop"}),
            ("randomtickspeed", {"value": -1}),
            ("tickingarea_remove", {"name": "a b"}),
        ],
    )
    def test_chaos_validation(self, adapter: BdsAdapter, action: str, params: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            adapter.chaos_dialect().render(action, **params)

    def test_unsupported_chaos_action(self, adapter: BdsAdapter) -> None:
        with pytest.raises(UnsupportedAction):
            adapter.chaos_dialect().render("status")

    def test_applied(self, adapter: BdsAdapter) -> None:
        tick = line("Game rule randomtickspeed has been updated to 100")
        assert adapter.applied("randomtickspeed", tick) == {"randomtickspeed": 100}
        assert adapter.applied("playerwaypoints", tick) is None
        waypoints = line("Game rule playerwaypoints has been updated to everyone")
        assert adapter.applied("playerwaypoints", waypoints) == {"playerwaypoints": "everyone"}
        assert adapter.applied("weather", tick) is None
        assert adapter.applied("randomtickspeed", line("Gametime is 5")) is None


class TestPositions:
    def test_vanilla_continuation_lines(self) -> None:
        raw = (LOGS / "bds-session.log").read_text(encoding="utf-8").splitlines()
        lines = [line(BDS_DIALECT.strip(r)) for r in raw]
        positions = parse_querytarget(lines)
        assert positions is not None
        assert [(p.x, p.z, p.dim) for p in positions] == [(1280.5, 1280.5, 0), (1922.25, 1279.75, 0)]
        assert positions[1].y == pytest.approx(301.6, abs=0.01)
        assert positions[0].name == "-4294967295"

    def test_no_reply_is_unknown(self) -> None:
        assert parse_querytarget([line("Gametime is 5")]) is None
        assert parse_querytarget([]) is None

    def test_no_players(self) -> None:
        assert parse_querytarget([line("No targets matched selector")]) == []

    def test_broken_json_is_unknown(self) -> None:
        assert parse_querytarget([line("Target data: ["), line('{"position": {"x": 1')]) is None
        assert parse_querytarget([line("Target data: [{}]")]) is None

    def test_interleaved_line_is_skipped(self) -> None:
        lines = [
            line("Target data: ["),
            line('   {"dimension": 0, "position": {"x": 1.5, "y": 2, "z": 3.5}, "uniqueId": "-1"},'),
            line("Gametime is 1234"),
            line('   {"dimension": 1, "position": {"x": 4, "y": 5, "z": 6}, "uniqueId": "-2"}'),
            line("]"),
        ]
        positions = parse_querytarget(lines)
        assert positions is not None and [p.name for p in positions] == ["-1", "-2"]

    def test_brackets_inside_strings(self) -> None:
        text = 'Target data: [{"uniqueId": "a]b[", "position": {"x": 1, "y": 2, "z": 3}}]'
        positions = parse_querytarget([line(text)])
        assert positions is not None and positions[0].name == "a]b["


class TestProperties:
    def test_parse_set_and_keep_layout(self) -> None:
        props = Properties.parse("a=1\r\n# comment\r\nb = two words \r\nc=\r\n")
        assert props.newline == "\r\n"
        assert (props.get("a"), props.get("b"), props.get("c"), props.get("d")) == ("1", "two words", "", None)
        props.set("a", "9")
        props.set("d", "new")
        assert props.text() == "a=9\r\n# comment\r\nb = two words \r\nc=\r\nd=new\r\n"
        with pytest.raises(ValueError):
            props.set("e", "x\ny")

    def test_template_loads(self) -> None:
        pins = load_property_pins(ROOT / "configs" / "servers" / "bds" / "server.toml")
        keys = {pin.key: pin for pin in pins}
        assert keys["online-mode"].category is OverrideCategory.HARNESS
        assert keys["view-distance"].category is OverrideCategory.PARITY
        assert "max-threads" not in keys, "max-threads stays at the shipped default"
        assert all(pin.reason for pin in pins)

    def test_template_validation(self, tmp_path: Path) -> None:
        bad = tmp_path / "p.toml"
        bad.write_text('schema = 1\n[config.tuning]\nx = { value = "1", reason = "r" }\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown table"):
            load_property_pins(bad)
        bad.write_text('schema = 1\n[config.harness]\nx = { value = "1" }\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            load_property_pins(bad)


class TestFetchAndPrepare:
    def test_fetch_local_zip(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        install = local_adapter.fetch(cache)
        assert install.artifacts[0].source == "local"
        assert install.extra["exe"] in ("bedrock_server.exe", "bedrock_server")
        assert local_adapter.fetch(cache) == install
        if os.name == "posix":
            assert os.stat(install.root / "bedrock_server").st_mode & 0o111

    def test_prepare_renders_properties(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        ctx = make_ctx(tmp_path)
        layout = local_adapter.prepare(install, ctx)
        props = Properties.load(layout.server_dir / "server.properties")
        assert props.newline == "\r\n", "the shipped file's line endings are kept"
        expected = {
            "online-mode": "false",
            "server-port": str(ctx.machine.port),
            "server-portv6": str(ctx.machine.port + 1),
            "view-distance": "10",
            "level-name": "bench",
            "level-seed": "20261010",
            "client-side-chunk-generation-enabled": "true",
            "transport": "raknet",
            "max-threads": "8",
        }
        assert {key: props.get(key) for key in expected} == expected

        by_key = {o.key: o for o in layout.overrides}
        assert by_key["online-mode"].category is OverrideCategory.HARNESS
        assert (by_key["online-mode"].default, by_key["online-mode"].value) == ("true", "false")
        assert by_key["view-distance"].category is OverrideCategory.PARITY
        assert by_key["level-seed"].default == ""
        assert by_key["emit-server-telemetry"].default is None
        assert by_key["allow-list"].category is OverrideCategory.HARNESS
        for unchanged in ("difficulty", "gamemode", "tick-distance", "max-threads", "server-build-radius-ratio"):
            assert unchanged not in by_key, f"{unchanged} equals the shipped value and is not an override"
        assert "client-side-chunk-generation-enabled" not in by_key
        assert all(o.file == "server.properties" for o in layout.overrides)

        assert [c.path for c in layout.config_files] == ["server.properties"]
        assert layout.world.fresh and layout.world.generator == "vanilla"
        assert layout.world.path is not None and not layout.world.path.exists()
        assert local_adapter.readback(layout) == []
        assert local_adapter.executable(layout).is_file()

    def test_csg_off_is_a_parity_override(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        layout = local_adapter.prepare(install, make_ctx(tmp_path, csg=False))
        csg = next(o for o in layout.overrides if o.key == "client-side-chunk-generation-enabled")
        assert (csg.category, csg.default, csg.value) == (OverrideCategory.PARITY, "true", "false")

    def test_readback_catches_changes(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        layout = local_adapter.prepare(install, make_ctx(tmp_path))
        path = layout.server_dir / "server.properties"
        props = Properties.load(path)
        props.set("view-distance", "32")
        props.dump(path)
        problems = local_adapter.readback(layout)
        assert len(problems) == 1 and "view-distance" in problems[0]
        path.unlink()
        assert local_adapter.readback(layout) == ["server.properties is missing after the run"]

    def test_install_untouched_by_runs(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        install = local_adapter.fetch(cache)
        before = (install.root / "server.properties").read_bytes()
        local_adapter.prepare(install, make_ctx(tmp_path))
        assert (install.root / "server.properties").read_bytes() == before
        assert local_adapter.fetch(cache).tree_sha256 == install.tree_sha256

    def test_work_dir_must_be_empty(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        ctx = make_ctx(tmp_path)
        ctx.work_dir.mkdir()
        (ctx.work_dir / "leftover").write_text("x", encoding="utf-8")
        with pytest.raises(FileExistsError):
            local_adapter.prepare(install, ctx)

    def test_template_world(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        template = tmp_path / "template"
        (template / "db").mkdir(parents=True)
        (template / "level.dat").write_bytes(b"level")
        ctx = make_ctx(tmp_path, source="template")
        with pytest.raises(ConfigError, match="prepare-world"):
            local_adapter.prepare(install, ctx)
        ctx = make_ctx(tmp_path / "second", source="template", template=template)
        layout = local_adapter.prepare(install, ctx)
        assert not layout.world.fresh and layout.world.template_sha256
        assert (layout.server_dir / "worlds" / "bench" / "level.dat").read_bytes() == b"level"


class TestLaunch:
    def test_windows(self, local_adapter: BdsAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bdsfamily, "host_os", lambda: "windows")
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        layout = local_adapter.prepare(install, make_ctx(tmp_path))
        spec = local_adapter.launch(layout)
        assert spec.argv == (str(layout.server_dir / "bedrock_server.exe"),)
        assert spec.cwd == layout.server_dir and dict(spec.env_set) == {}

    def test_linux(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bdsfamily, "host_os", lambda: "linux")
        arm = Arm(id="bds-local", server="bds", version="1.26.52.3", artifact=bds_zip(tmp_path / "bds.zip"))
        adapter = BdsAdapter(arm, config.default_machine())
        install = adapter.fetch(Cache(tmp_path / "cache"))
        layout = adapter.prepare(install, make_ctx(tmp_path))
        spec = adapter.launch(layout)
        assert spec.argv == ("./bedrock_server",)
        assert dict(spec.env_set) == {"LD_LIBRARY_PATH": "."}
        assert "LD_PRELOAD" in spec.env_remove


class TestFactsAndCrashes:
    def test_facts(self, local_adapter: BdsAdapter, tmp_path: Path) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        layout = local_adapter.prepare(install, make_ctx(tmp_path, csg=False))
        proc = SimpleNamespace(memory_maps=lambda grouped=True: [SimpleNamespace(path="/usr/lib/libc.so.6")])
        facts = local_adapter.facts(layout, proc)  # type: ignore[arg-type]
        assert facts["allocator"] == "default"
        assert facts["csg_setting"] is False
        assert facts["max_threads"] == "8"
        assert facts["transport"] == "raknet"

    def test_allocator_detection(self) -> None:
        mapped = SimpleNamespace(memory_maps=lambda grouped=True: [SimpleNamespace(path="/x/libmimalloc.so.2")])
        assert bdsfamily.detect_allocator(mapped) == "mimalloc"  # type: ignore[arg-type]

        def denied(grouped: bool = True) -> list[Any]:
            raise PermissionError

        assert bdsfamily.detect_allocator(SimpleNamespace(memory_maps=denied)) is None  # type: ignore[arg-type]

    def test_crash_artifacts(self, local_adapter: BdsAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install = local_adapter.fetch(Cache(tmp_path / "cache"))
        layout = local_adapter.prepare(install, make_ctx(tmp_path))
        dumps = tmp_path / "CrashDumps"
        dumps.mkdir()
        (dumps / "bedrock_server.exe.4242.dmp").write_bytes(b"dump")
        (dumps / "someothertool.exe.11.dmp").write_bytes(b"dump")
        monkeypatch.setattr(BdsAdapter, "crash_paths", (str(dumps),))
        names = [p.name for p in local_adapter.crash_artifacts(layout, since_epoch_ms=0)]
        assert names == ["bedrock_server.exe.4242.dmp"]


class TestDataFiles:
    def test_artifacts_pinned(self) -> None:
        catalog = config.load_artifacts("bds")
        assert {"1.26.52.3", "1.26.51.1"} <= set(catalog.versions)
        for version, entry in catalog.versions.items():
            assert set(entry.platforms) == {"windows", "linux"}, version
            for platform, spec in entry.platforms.items():
                assert spec.sha256 and spec.size and spec.eula == "minecraft" and spec.source == "mojang"
                assert spec.url.startswith("https://www.minecraft.net/bedrockdedicatedserver/")
                assert spec.name == f"bedrock-server-{version}.zip"
                assert spec.executables == (bdsfamily.exe_name(platform),)

    def test_features_file(self) -> None:
        features = config.load_features("bds")
        row = features.row("1.26.52.3")
        assert row is not None and row.fidelity.value == "V0"
        assert set(row.cells) == FEATURE_KEYS
        assert all(cell.cls.value == "vanilla" for cell in row.cells.values())

    def test_observed_counters_are_metrics(self) -> None:
        registry = pytest.importorskip("bedrock_bench.metrics.registry")
        known = set(registry.ids())
        for server in ("bds", "endstone"):
            for rng in config.load_features(server).ranges:
                for cell in rng.cells.values():
                    assert cell.observed_by is None or cell.observed_by in known, (server, cell.key)

    def test_methodology_lists_every_feature_key(self) -> None:
        text = (ROOT / "docs" / "methodology.md").read_text(encoding="utf-8")
        listed = set(re.findall(r"^\| `([a-z]+\.[a-z_]+)` \|", text, flags=re.MULTILINE))
        assert listed == FEATURE_KEYS

    def test_fetch_needs_eula_for_download(self, adapter: BdsAdapter, tmp_path: Path) -> None:
        with fetch.using(fetch.FetchOptions(report=lambda _: None)), pytest.raises(fetch.EulaNotAccepted):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_unpinned_version(self, tmp_path: Path) -> None:
        adapter = BdsAdapter(Arm(id="bds-x", server="bds", version="1.0.0.0"), config.default_machine())
        with pytest.raises(ConfigError, match="no pinned version"):
            adapter.fetch(Cache(tmp_path / "cache"))
