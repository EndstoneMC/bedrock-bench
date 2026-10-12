# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, bots, config, fetch
from bedrock_bench.adapters import dragonfly
from bedrock_bench.adapters.dragonfly import DRAGONFLY_DIALECT, DragonflyAdapter
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import Arm, Install, LogLine, Machine, OverrideCategory, Placement, RunContext
from bedrock_bench.workload import WORKLOAD, with_overrides

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"
VERSION = "1793369"
MODULE = "github.com/df-mc/dragonfly"
MODULE_VERSION = "v0.11.6-0.20260920163020-1793369709ce"

#: 'dfbench -defaults' of the pinned build.
DEFAULTS = """\
# Defaults of dfbench 0.1.0 on Dragonfly 1793369: a missing key in config.toml means this value.

[Network]
  Address = ":19132"
  Transport = ["raknet"]

  [Network.NetherNet]
    Address = ""
    Domain = "self"
    KeyFile = "keys/server_identity_key.pem"
    UDPPorts = "19133"

[Players]
  Folder = "players"
  MaxCount = 0
  MaximumChunkRadius = 32
  SaveData = true

[Resources]
  AutoBuildPack = true
  Folder = "resources"
  Required = false

[Server]
  AuthEnabled = true
  DisableJoinQuitMessages = false
  MuteEmoteChat = false
  Name = "Dragonfly Server"

[World]
  Folder = "world"
  SaveData = true


[Bench]
  Difficulty = "normal"
  GameMode = "creative"
  IdleTicking = false
  LogLevel = "debug"
  SaveIntervalSeconds = 600
  TickRange = 6
"""

BUILD_INFO = {
    "module": "dfbench",
    "version": "0.1.0",
    "go": "go1.26.3",
    "dragonfly": VERSION,
    "dragonfly_module": MODULE_VERSION,
    "gophertunnel": "v1.62.0",
    "protocol": 2193,
    "minecraft": "1.26.50",
}


def line(text: str, t_ns: int = 0) -> LogLine:
    return LogLine(t_ns=t_ns, epoch_ms=0, level="info", logger=None, text=text, raw=text)


def make_adapter(machine: Machine | None = None, artifact: Path | None = None) -> DragonflyAdapter:
    arm = config.arm_from_ref(f"dragonfly@{VERSION}", artifact=artifact)
    return DragonflyAdapter(arm, machine or config.default_machine())


def make_ctx(tmp_path: Path, **world: Any) -> RunContext:
    workload = with_overrides(WORKLOAD, {"world": world})
    return RunContext(
        run_id="20260101T120000Z-dragonfly-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=Arm(id=f"dragonfly@{VERSION}", server="dragonfly", version=VERSION),
        workload=workload,
        machine=config.default_machine(),
        placement=Placement((0, 1), (2, 3), (2, 3), "normal", "physical"),
    )


def make_install(tmp_path: Path, defaults: str = DEFAULTS) -> Install:
    """An install as fetch leaves it: the binary, its defaults and its build information."""
    root = tmp_path / "install"
    root.mkdir()
    exe = root / dragonfly.exe_name(bots.Target.host())
    exe.write_bytes(b"dfbench binary")
    (root / dragonfly.DEFAULTS).write_text(defaults, encoding="utf-8")
    (root / dragonfly.BUILD_INFO).write_text(json.dumps(BUILD_INFO), encoding="utf-8")
    digest = hashlib.sha256(b"dfbench binary").hexdigest()
    return Install(root=root, tree_sha256="0" * 64, artifacts=(), extra={"exe": exe.name, "exe_sha256": digest})


# --------------------------------------------------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------------------------------------------------


class TestDialect:
    def test_registered(self) -> None:
        assert adapters.get("dragonfly") is DragonflyAdapter
        assert DragonflyAdapter.class_problems() == []
        assert DragonflyAdapter.tick_thread_rule == "none"
        assert DragonflyAdapter.bot_compat_flags == ()
        assert not DragonflyAdapter.supports_csg

    def test_session_log(self) -> None:
        texts = [DRAGONFLY_DIALECT.strip(raw) for raw in (LOGS / "dragonfly-session.log").read_text().splitlines()]
        assert [t for t in texts if DRAGONFLY_DIALECT.is_ready(t)] == ["Server started."]
        assert [v for v in map(DRAGONFLY_DIALECT.parse_version, texts) if v] == [VERSION]
        assert [p for p in map(DRAGONFLY_DIALECT.player_entered, texts) if p] == ["a00", "a01"]
        ticks = [t for t in map(DRAGONFLY_DIALECT.parse_tick, texts) if t is not None]
        assert ticks == sorted(ticks) and len(ticks) == 4

    def test_library_banner_is_not_the_ready_line(self) -> None:
        level, _, text = DRAGONFLY_DIALECT.split("2026/01/01 12:00:00.105033 INFO Dragonfly server started. commit=")
        assert level == "info"
        assert not DRAGONFLY_DIALECT.is_ready(text)
        assert DRAGONFLY_DIALECT.parse_version(text) is None

    def test_unprefixed_lines_have_no_level(self) -> None:
        for raw in ("a00 joined the game", "panic: runtime error: index out of range [16] with length 16"):
            assert DRAGONFLY_DIALECT.split(raw) == (None, None, raw)


class TestPositions:
    def test_complete_reply(self) -> None:
        lines = [
            line("Positions: 2"),
            line("Gametime is 1698"),  # another reply in between
            line("Position: 0 1283.06 300.00 1281.38 a00"),
            line("Position: 1 160.00 100.00 160.00 A Player"),
        ]
        positions = dragonfly.parse_positions(lines)
        assert positions is not None
        assert [(p.name, p.x, p.y, p.z, p.dim) for p in positions] == [
            ("a00", 1283.06, 300.0, 1281.38, 0),
            ("A Player", 160.0, 100.0, 160.0, 1),
        ]

    def test_incomplete_or_missing_reply_is_unknown(self) -> None:
        assert dragonfly.parse_positions([]) is None
        assert dragonfly.parse_positions([line("Gametime is 5")]) is None
        assert dragonfly.parse_positions([line("Positions: 2"), line("Position: 0 1 2 3 a00")]) is None
        assert dragonfly.parse_positions([line("Positions: 1"), line("Position: 0 x 2 3 a00")]) is None

    def test_no_players(self) -> None:
        assert dragonfly.parse_positions([line("Positions: 0")]) == []

    def test_adapter_uses_the_helper_command(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_positions() == "positions"
        assert adapter.parse_positions([line("Positions: 0")]) == []


class TestCommands:
    def test_placement_uses_vanilla_syntax(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_effects("a07")[0] == "effect a07 resistance infinite 255 true"
        assert adapter.cmd_effects("@a")[3] == "effect @a saturation infinite 255 true"
        assert adapter.cmd_tp("a07", 5760.5, 300, 1280) == "tp a07 5760.5 300 1280"
        assert DRAGONFLY_DIALECT.tick_cmd == "gametime"
        assert DRAGONFLY_DIALECT.stop_cmd == "stop"


# --------------------------------------------------------------------------------------------------------------------
# config.toml
# --------------------------------------------------------------------------------------------------------------------


class TestSettings:
    def test_overrides_and_types(self, tmp_path: Path) -> None:
        settings = dragonfly.load_settings(ROOT / dragonfly.SETTINGS_FILE)
        values, overrides = dragonfly.render_settings(
            settings, tomllib.loads(DEFAULTS), dragonfly.settings_context(make_ctx(tmp_path))
        )
        assert values["Network.Address"] == "127.0.0.1:19672"
        assert values["Server.AuthEnabled"] is False
        assert values["Players.MaximumChunkRadius"] == 10
        assert values["Players.MaxCount"] == 200
        assert values["Bench.TickRange"] == 4
        assert values["Bench.Difficulty"] == "easy"
        assert values["Bench.GameMode"] == "survival"
        assert values["Bench.SaveIntervalSeconds"] == 300
        assert values["Bench.IdleTicking"] is True
        assert values["World.Folder"] == "bench"
        by_key = {o.key: o for o in overrides}
        harness = {k for k, o in by_key.items() if o.category is OverrideCategory.HARNESS}
        assert harness == {
            "Network.Address",
            "Server.Name",
            "Server.AuthEnabled",
            "Bench.LogLevel",
            "Bench.IdleTicking",
        }
        assert "Players.SaveData" not in by_key and "World.SaveData" not in by_key  # equal to the defaults
        assert by_key["Bench.GameMode"].default == "creative"
        assert by_key["Server.AuthEnabled"].default == "true" and by_key["Server.AuthEnabled"].value == "false"
        assert by_key["Players.MaxCount"].category is OverrideCategory.PARITY
        assert not any(o.category.footnoted for o in overrides)

    def test_unknown_key(self) -> None:
        setting = dragonfly.Setting("Players.MaxCont", 1, OverrideCategory.PARITY, "typo")
        with pytest.raises(ConfigError, match="not a setting"):
            dragonfly.render_settings([setting], tomllib.loads(DEFAULTS), {})

    def test_bad_placeholder_and_type(self) -> None:
        defaults = tomllib.loads(DEFAULTS)
        with pytest.raises(ConfigError, match="placeholder"):
            dragonfly.render_settings(
                [dragonfly.Setting("Players.MaxCount", "{nope}", OverrideCategory.PARITY, "x")], defaults, {}
            )
        for key, value in (("Players.MaxCount", "many"), ("Server.AuthEnabled", "maybe"), ("Server.Name", 3)):
            with pytest.raises(ConfigError, match="like its default"):
                dragonfly.render_settings([dragonfly.Setting(key, value, OverrideCategory.HARNESS, "x")], defaults, {})

    @pytest.mark.parametrize(
        ("schema", "fmt", "body"),
        [
            (2, "toml", '[config.file."config.toml".harness]\n"Server.Name" = { value = "x", reason = "y" }\n'),
            (1, "toml", '[config.file."config.toml".tuning]\n"Server.Name" = { value = "x", reason = "y" }\n'),
            (1, "toml", '[config.file."config.toml".harness]\n"Server.Name" = { value = "x" }\n'),
            (1, "toml", '[config.file."config.toml".harness]\nName = { value = "x", reason = "y" }\n'),
            (1, "yaml", '[config.file."config.toml".harness]\n"Server.Name" = { value = "x", reason = "y" }\n'),
            (
                1,
                "toml",
                '[config.file."config.toml".harness]\n"Server.Name" = { value = "x", reason = "y" }\n'
                '[config.file."config.toml".parity]\n"Server.Name" = { value = "z", reason = "y" }\n',
            ),
        ],
    )
    def test_settings_file_is_checked(self, write_file: Any, schema: int, fmt: str, body: str) -> None:
        text = f'schema = {schema}\n[config.file."config.toml"]\nformat = "{fmt}"\n{body}'
        with pytest.raises(ConfigError):
            dragonfly.load_settings(write_file("config.toml", text))

    def test_settings_file_needs_a_config_table(self, write_file: Any) -> None:
        with pytest.raises(ConfigError):
            dragonfly.load_settings(write_file("config.toml", "schema = 1\n[artifacts]\n"))

    def test_dump_toml_round_trips(self) -> None:
        values = {
            "Network.Address": "127.0.0.1:1",
            "Players.MaxCount": 200,
            "Bench.IdleTicking": True,
            "Network.Transport": ["raknet"],
            "Server.Name": 'quote " and \\',
        }
        assert tomllib.loads(dragonfly.dump_toml(values)) == {
            "Network": {"Address": "127.0.0.1:1", "Transport": ["raknet"]},
            "Players": {"MaxCount": 200},
            "Bench": {"IdleTicking": True},
            "Server": {"Name": 'quote " and \\'},
        }


# --------------------------------------------------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------------------------------------------------


#: The real subprocess.run, for commands the fake does not handle (the platform module runs some on Windows).
REAL_RUN = subprocess.run


class FakeGo:
    """Stands in for the Go toolchain and the built binary; records every command it handles."""

    def __init__(self, zip_path: Path, go_sum: str, info: dict[str, Any] | None = None) -> None:
        self.zip_path = zip_path
        self.go_sum = go_sum
        self.info = dict(BUILD_INFO if info is None else info)
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def builds(self) -> list[tuple[list[str], dict[str, str] | None]]:
        return [(argv, env) for argv, env in self.calls if argv[1:2] == ["build"]]

    def __call__(self, argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(argv, str) or not argv or Path(argv[0]).stem not in ("go", "dfbench"):
            return REAL_RUN(argv, **kwargs)
        argv, env = list(argv), kwargs.get("env")
        self.calls.append((argv, env))
        out = ""
        if argv[1:3] == ["env", "GOVERSION"]:
            out = "go1.26.3\n"
        elif argv[1:4] == ["mod", "download", "-json"]:
            out = json.dumps({"Path": MODULE, "Sum": self.go_sum, "Zip": str(self.zip_path)})
        elif argv[1:2] == ["build"]:
            target = Path(argv[argv.index("-o") + 1])
            target.write_bytes(f"dfbench for {env['GOOS']}/{env['GOARCH']}".encode())
        elif argv[-1] == "-version":
            out = json.dumps(self.info)
        elif argv[-1] == "-defaults":
            out = DEFAULTS
        else:
            raise AssertionError(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A project root with configs/dragonfly, whose server.toml pins a fake module zip."""
    root = tmp_path / "project"
    src = root / dragonfly.SOURCE_DIR
    src.mkdir(parents=True)
    zip_path = tmp_path / "module.zip"
    zip_path.write_bytes(b"module zip")
    go_sum = "h1:fakeFAKEfake="
    (src / "go.mod").write_text(f"module example/dfbench\n\ngo 1.26.0\n\nrequire (\n\t{MODULE} {MODULE_VERSION}\n)\n")
    (src / "go.sum").write_text(f"{MODULE} {MODULE_VERSION} {go_sum}\n{MODULE} {MODULE_VERSION}/go.mod h1:x=\n")
    (src / "main.go").write_text("package main\n\nfunc main() {}\n")
    settings = (ROOT / dragonfly.SETTINGS_FILE).read_text(encoding="utf-8")
    (root / dragonfly.SETTINGS_FILE).write_text(
        f'schema = 1\nserver = "dragonfly"\n\n[artifacts]\nsource = "web"\n\n[artifacts.version."{VERSION}".any]\n'
        f'url = "https://proxy.golang.org/{MODULE}/@v/{MODULE_VERSION}.zip"\n'
        f'sha256 = "{hashlib.sha256(b"module zip").hexdigest()}"\nsize = {len(b"module zip")}\n'
        f'module = "{MODULE}"\nmodule_version = "{MODULE_VERSION}"\ngo_sum = "{go_sum}"\n\n'
        + settings[settings.index('\n[config.file."config.toml"]\n') :]
    )
    monkeypatch.setenv("BBENCH_ROOT", str(root))
    fake = FakeGo(zip_path, go_sum)
    monkeypatch.setattr(dragonfly.shutil, "which", lambda name: "go" if name == "go" else None)
    monkeypatch.setattr(dragonfly.subprocess, "run", fake)
    return {"root": root, "src": src, "go": fake, "zip": zip_path}


class TestFetch:
    def test_builds_once_and_records_the_build(self, project: dict[str, Any], tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        install = make_adapter().fetch(cache)
        exe = install.root / dragonfly.exe_name(bots.Target.host())
        assert exe.is_file()
        assert tomllib.loads((install.root / dragonfly.DEFAULTS).read_text())["Bench"]["TickRange"] == 6
        module, built = install.artifacts
        assert (module.url, module.source) == (f"https://proxy.golang.org/{MODULE}/@v/{MODULE_VERSION}.zip", "web")
        assert (built.name, built.source, built.url) == (exe.name, "build", None)
        assert install.extra["exe_sha256"] == built.sha256
        assert install.extra["dragonfly_module"] == MODULE_VERSION
        [(argv, env)] = project["go"].builds()
        assert env is not None and env["CGO_ENABLED"] == "0" and env["GOWORK"] == "off"
        assert env["GOFLAGS"] == "-mod=readonly"
        assert "-trimpath" in argv and any("-X main.version=" in a for a in argv)
        if env["GOARCH"] == "amd64":
            assert env["GOAMD64"] == "v1"

        again = make_adapter().fetch(cache)
        assert again.tree_sha256 == install.tree_sha256
        assert len(project["go"].builds()) == 1, "a cached build is reused"

    def test_checksum_mismatch(self, project: dict[str, Any], tmp_path: Path) -> None:
        project["go"].go_sum = "h1:other="
        with pytest.raises(fetch.FetchError, match="checksum"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_zip_mismatch(self, project: dict[str, Any], tmp_path: Path) -> None:
        project["zip"].write_bytes(b"another zip")
        with pytest.raises(fetch.FetchError, match="proxy.golang.org"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_wrong_dragonfly_in_the_binary(self, project: dict[str, Any], tmp_path: Path) -> None:
        project["go"].info["dragonfly"] = "devel"
        with pytest.raises(fetch.FetchError, match="links Dragonfly devel"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_go_mod_must_require_the_pinned_version(self, project: dict[str, Any], tmp_path: Path) -> None:
        go_mod = project["src"] / "go.mod"
        go_mod.write_text(go_mod.read_text().replace(MODULE_VERSION, "v0.11.5"))
        with pytest.raises(ConfigError, match="requires"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_go_mod_must_not_replace(self, project: dict[str, Any], tmp_path: Path) -> None:
        go_mod = project["src"] / "go.mod"
        go_mod.write_text(go_mod.read_text() + f"\nreplace {MODULE} => ../dragonfly\n")
        with pytest.raises(ConfigError, match="replace"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_go_sum_must_record_the_pin(self, project: dict[str, Any], tmp_path: Path) -> None:
        (project["src"] / "go.sum").write_text("")
        with pytest.raises(ConfigError, match="go.sum"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_unpinned_version(self, project: dict[str, Any], tmp_path: Path) -> None:
        adapter = DragonflyAdapter(config.arm_from_ref("dragonfly@abcdef0"), config.default_machine())
        with pytest.raises(ConfigError, match="abcdef0"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_no_go_toolchain(self, project: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dragonfly.shutil, "which", lambda name: None)
        with pytest.raises(fetch.FetchError, match="Go toolchain"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_failed_build_reports_go_output(
        self, project: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = project["go"]

        def failing(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if not isinstance(argv, str) and list(argv)[1:2] == ["build"]:
                return subprocess.CompletedProcess(list(argv), 1, "", "main.go:3: undefined: nope\n")
            return fake(argv, **kwargs)

        monkeypatch.setattr(dragonfly.subprocess, "run", failing)
        with pytest.raises(fetch.FetchError, match="undefined: nope"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_prebuilt_artifact(self, project: dict[str, Any], tmp_path: Path) -> None:
        prebuilt = tmp_path / "my-dfbench"
        prebuilt.write_bytes(b"my build")
        install = make_adapter(artifact=prebuilt).fetch(Cache(tmp_path / "cache"))
        [ref] = install.artifacts
        assert ref.source == "local" and ref.sha256 == hashlib.sha256(b"my build").hexdigest()
        assert (install.root / dragonfly.DEFAULTS).is_file()
        assert project["go"].builds() == []


# --------------------------------------------------------------------------------------------------------------------
# Prepare, launch and facts
# --------------------------------------------------------------------------------------------------------------------


class TestPrepare:
    def test_fresh_server_directory(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        written = tomllib.loads((layout.server_dir / dragonfly.CONFIG).read_text(encoding="utf-8"))
        assert written["Players"] == {"MaximumChunkRadius": 10, "MaxCount": 200, "SaveData": True}
        assert written["Bench"]["GameMode"] == "survival"
        assert "NetherNet" not in written.get("Network", {}), "unlisted keys keep their defaults"
        assert [c.path for c in layout.config_files] == ["config.toml"]
        assert layout.world.fresh and layout.world.generator == "flat"
        assert layout.world.path == layout.server_dir / "bench"
        assert adapter.readback(layout) == []
        assert adapter.executable(layout).read_bytes() == b"dfbench binary"

    def test_benchmark_config_is_what_dfbench_tests_read(self, tmp_path: Path) -> None:
        """configs/dragonfly/testdata/config.toml is the benchmark config; dfbench's Go tests parse it."""
        ctx = dataclasses.replace(make_ctx(tmp_path), workload=WORKLOAD)
        layout = make_adapter().prepare(make_install(tmp_path), ctx)
        written = (layout.server_dir / dragonfly.CONFIG).read_bytes()
        assert written == (ROOT / dragonfly.SOURCE_DIR / "testdata" / "config.toml").read_bytes()

    def test_readback_finds_changes(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        path = layout.server_dir / dragonfly.CONFIG
        path.write_text(path.read_text().replace("MaxCount = 200", "MaxCount = 0"))
        assert adapter.readback(layout) == ["config.toml: Players.MaxCount was written as 200 and read back as 0"]
        path.unlink()
        assert adapter.readback(layout)[0].startswith("config.toml cannot be read")

    def test_tampered_install(self, tmp_path: Path) -> None:
        install = make_install(tmp_path)
        (install.root / install.extra["exe"]).write_bytes(b"changed")
        with pytest.raises(fetch.FetchError, match="does not match"):
            make_adapter().prepare(install, make_ctx(tmp_path))


class TestLaunchAndFacts:
    def test_launch(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        spec = adapter.launch(layout)
        assert spec.argv == (str(layout.server_dir / layout.extra["exe"]), "-config", "config.toml")
        assert spec.cwd == layout.server_dir
        assert {"GOMAXPROCS", "GOGC", "GOMEMLIMIT", "GODEBUG"} <= set(spec.env_remove)

    def test_facts(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        before = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert before["allocator"] == "go" and before["csg_setting"] is False and before["gomaxprocs"] is None
        runtime = {**BUILD_INFO, "gomaxprocs": 8, "num_cpu": 16, "difficulty": "easy", "tick_range": 4}
        (layout.server_dir / dragonfly.FACTS).write_text(json.dumps(runtime), encoding="utf-8")
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert (facts["gomaxprocs"], facts["num_cpu"], facts["difficulty"], facts["tick_range"]) == (8, 16, "easy", 4)
        assert facts["dragonfly_module"] == MODULE_VERSION


# --------------------------------------------------------------------------------------------------------------------
# Checked-in data
# --------------------------------------------------------------------------------------------------------------------


class TestDataFiles:
    def test_artifacts_pin_matches_go_mod_and_go_sum(self) -> None:
        pin = dragonfly.ModulePin.load(VERSION)
        assert pin.version.endswith("-" + "1793369709ce")
        dragonfly.check_source(ROOT / dragonfly.SOURCE_DIR, pin)

    def test_required_version(self) -> None:
        go_mod = f"module x\n\nrequire {MODULE} v1.2.3\n"
        assert dragonfly.required_version(go_mod, MODULE) == "v1.2.3"
        assert dragonfly.required_version(f"require (\n\t{MODULE} {MODULE_VERSION}\n)\n", MODULE) == MODULE_VERSION
        assert dragonfly.required_version("module x\n", MODULE) is None

    def test_source_hash_ignores_tests_and_line_endings(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_bytes(b"module x\n")
        (tmp_path / "main.go").write_bytes(b"package main\n")
        first = dragonfly.source_sha256(tmp_path)
        (tmp_path / "main_test.go").write_bytes(b"package main\n")
        (tmp_path / "main.go").write_bytes(b"package main\r\n")
        (tmp_path / "README.md").write_bytes(b"docs")
        assert dragonfly.source_sha256(tmp_path) == first
        (tmp_path / "console.go").write_bytes(b"package main\n")
        assert dragonfly.source_sha256(tmp_path) != first

    def test_exercised_features_are_declared_with_evidence(self) -> None:
        features = config.load_features("dragonfly")
        row = features.row(VERSION)
        assert row is not None
        for key in WORKLOAD.exercises:
            cell = row.cells[key]
            assert cell.cls.value != "unknown", key
            assert cell.evidence or cell.observed_by, key

    def test_settings_match_a_real_build(self) -> None:
        """With BBENCH_DFBENCH pointing at a built dfbench, every setting must be a key of its defaults."""
        exe = os.environ.get("BBENCH_DFBENCH")
        if not exe:
            pytest.skip("set BBENCH_DFBENCH to a dfbench binary to check the settings against it")
        defaults = tomllib.loads(subprocess.run([exe, "-defaults"], capture_output=True, text=True, check=True).stdout)
        for setting in dragonfly.load_settings(ROOT / dragonfly.SETTINGS_FILE):
            assert dragonfly.lookup(defaults, setting.key)[0], setting.key
        assert defaults == tomllib.loads(DEFAULTS), "DEFAULTS in this test is out of date"
