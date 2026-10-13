# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, config, fetch
from bedrock_bench.adapters import chorus
from bedrock_bench.adapters.chorus import CHORUS_DIALECT, ChorusAdapter
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import Install, LogLine, Placement, RunContext
from workloads import WORKLOAD, with_overrides

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"
VERSION = "5c5ffd3"
COMMIT = "5c5ffd3962dcb1deedef3021b62aa2f3c33e9a6d"

#: 'chorusbench -defaults' of the pinned build.
DEFAULTS = """\
[server]
name = "Chorus"
description = "bedrock-crustaceans.org"
max_players = 20
authentication = true
threads = "auto"

[network]
ip = "0.0.0.0"
port = 19132
transport = "RakNet"
max_batch_size = 16777216
compression = "zlib"

[network.raknet]
encryption = true

[network.nethernet]
signaler = "both"

[network.zlib]
threshold = 1
level = 6

[network.snappy]
threshold = 1

[network.chunks]
subchunks = false
cache = false

[level]
name = "world"
seed = 0
chunk_saving = "modified"
max_view_distance = 32
autosave = "1m"

[level.database]
compression = 6
auto_compaction = "5m"
shutdown_compaction = false

[packs]
resource_directory = "resource_packs"
behavior_directory = "behavior_packs"
force_accept = false
force_disable_vibrant_visuals = false

[log]
level = "info"
to_file = true
directory = "logs"

[permissions]
default = "operator"

[permissions.players]
"""

#: 'chorusbench -version' of the pinned build.
BUILD_INFO = {
    "chorus": VERSION,
    "chorus_commit": COMMIT,
    "chorus_version": "0.0.1",
    "minecraft": "1.26.50",
    "module": "chorusbench",
    "protocol": 2193,
    "rustc": "rustc 1.95.0 (59807616e 2026-04-14)",
    "target": "x86_64-pc-windows-msvc",
    "version": "0.1.0",
}


def line(raw: str) -> LogLine:
    level, logger, text = CHORUS_DIALECT.split(raw)
    return LogLine(t_ns=0, epoch_ms=0, level=level, logger=logger, text=text, raw=raw)


def session() -> list[LogLine]:
    return [line(raw) for raw in (LOGS / "chorus-session.log").read_text(encoding="utf-8").splitlines()]


def make_adapter(artifact: Path | None = None) -> ChorusAdapter:
    return ChorusAdapter(config.arm_from_ref(f"chorus@{VERSION}", artifact=artifact), config.default_machine())


def make_ctx(tmp_path: Path, **world: Any) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-chorus-test-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=config.arm_from_ref(f"chorus@{VERSION}"),
        workload=with_overrides(WORKLOAD, {"world": world}),
        machine=config.default_machine(),
        placement=Placement((0, 1, 2, 3), (4, 5), (4, 5), "normal", "physical"),
    )


def make_install(tmp_path: Path) -> Install:
    """An install as fetch leaves it: the binary, its defaults and its build information."""
    root = tmp_path / "install"
    root.mkdir()
    (root / chorus.EXE).write_bytes(b"chorusbench binary")
    (root / chorus.DEFAULTS).write_text(DEFAULTS, encoding="utf-8")
    (root / chorus.BUILD_INFO).write_text(json.dumps(BUILD_INFO), encoding="utf-8")
    digest = hashlib.sha256(b"chorusbench binary").hexdigest()
    extra = {"exe": chorus.EXE, "exe_sha256": digest, "chorus_commit": COMMIT, "rustc": BUILD_INFO["rustc"]}
    return Install(root=root, tree_sha256="0" * 64, artifacts=(), extra=extra)


# --------------------------------------------------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------------------------------------------------


class TestDialect:
    def test_registered(self) -> None:
        assert adapters.get("chorus") is ChorusAdapter
        assert ChorusAdapter.class_problems() == []
        assert not ChorusAdapter.supports_csg
        assert ChorusAdapter.bot_compat_flags == ()
        assert ChorusAdapter.tick_thread_rule == "first_created"

    def test_pins(self) -> None:
        pin = chorus.SourcePin.load(VERSION)
        assert (pin.commit, pin.protocol, pin.minecraft, pin.rust) == (COMMIT, 2193, "1.26.50", "1.95.0")
        assert config.load_artifacts("chorus").get(VERSION, "windows").url.endswith(f"/archive/{COMMIT}.tar.gz")

    def test_session_log(self) -> None:
        lines = session()
        texts = [entry.text for entry in lines]
        assert [t for t in texts if CHORUS_DIALECT.is_ready(t)] == ["Started on 127.0.0.1:19930."]
        assert [v for v in map(CHORUS_DIALECT.parse_version, texts) if v] == [VERSION]
        assert [p for p in map(CHORUS_DIALECT.player_entered, texts) if p] == ["a00", "a01", "a02", "a03"]
        assert [t for t in map(CHORUS_DIALECT.parse_tick, texts) if t is not None] == [23, 286, 486, 1046]
        errors = [entry.text for entry in lines if entry.level == "error"]
        assert errors == ['No player named "a 01" is online.', 'No player named "a09" is online.']

    def test_upstream_banner_is_not_the_version(self) -> None:
        assert CHORUS_DIALECT.parse_version("Chorus v0.0.1 1.26.50 (2193)") is None
        assert CHORUS_DIALECT.split("Chorus v0.0.1 1.26.50 (2193)")[0] is None

    def test_replies_have_no_level(self) -> None:
        for raw in ("Gametime is 23", "positions: 0", "Unknown command: effect. Type /help for a list of commands."):
            assert CHORUS_DIALECT.split(raw) == (None, None, raw)

    def test_span_names_stay_in_the_text(self) -> None:
        level, _, text = CHORUS_DIALECT.split("18:58:21 INFO network Listening for RakNet connections on 127.0.0.1:1")
        assert level == "info" and text.startswith("network Listening")
        assert CHORUS_DIALECT.player_entered("network a00 joined the game") is None


class TestPositions:
    def test_replies_from_the_session(self) -> None:
        lines = session()
        headers = [i for i, entry in enumerate(lines) if entry.text.startswith("positions: ")]
        assert make_adapter().parse_positions(lines[headers[0] :]) == []
        positions = make_adapter().parse_positions(lines[headers[2] :])
        assert positions is not None
        assert [(p.name, p.x, p.y, p.z, p.dim) for p in positions] == [
            ("a00", 1264.13, 300.0, 1286.63, 0),
            ("a01", 1925.69, 300.0, 1265.04, 0),
            ("a02", 2565.88, 300.0, 1293.58, 0),
            ("a03", 3187.4, 300.0, 1274.95, 0),
        ]

    def test_incomplete_reply_is_unknown(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_positions() == "positions"
        assert adapter.parse_positions([line("positions: 2"), line("position: a00 1 2 3 0")]) is None


class TestCommands:
    def test_placement(self) -> None:
        adapter = make_adapter()
        assert adapter.cmd_effects("a07") == [] and adapter.cmd_effects("@a") == []
        assert adapter.cmd_tp("a07", 5760.5, 300, 1280) == "tpp a07 5760.5 300 1280"
        assert adapter.cmd_tp("a b", 1, 2, 3) == 'tpp "a b" 1 2 3'
        assert CHORUS_DIALECT.tick_cmd == "gametime" and CHORUS_DIALECT.stop_cmd == "stop"


# --------------------------------------------------------------------------------------------------------------------
# chorus.toml
# --------------------------------------------------------------------------------------------------------------------


class TestTemplate:
    def test_every_key_has_a_reason(self) -> None:
        lines = (config.server_dir("chorus") / chorus.CONFIG).read_text(encoding="utf-8").splitlines()
        for index, entry in enumerate(lines):
            if entry and not entry.startswith(("#", "[")):
                assert lines[index - 1].startswith("# "), entry


class TestPrepare:
    def test_fresh_server_directory(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        server = layout.server_dir
        written = tomllib.loads((server / chorus.CONFIG).read_text(encoding="utf-8"))
        assert written == {
            "network": {"ip": "127.0.0.1", "port": 19672, "transport": "RakNet"},
            "server": {"authentication": False, "max_players": 200, "threads": 4},
            "level": {
                "name": "bench",
                "seed": 20261010,
                "max_view_distance": 10,
                "autosave": False,
                "chunk_saving": "modified",
            },
        }
        assert layout.world.fresh and layout.world.generator == "custom"
        assert layout.world.path == server / "worlds" / "bench" and not layout.world.path.exists()
        assert [c.path for c in layout.config_files] == [chorus.CONFIG]
        assert adapter.launch(layout).argv == (str(server / chorus.EXE),)
        assert adapter.executable(layout).read_bytes() == b"chorusbench binary"

        (written_file,) = layout.config_files
        assert (written_file.template, written_file.created) == (chorus.CONFIG, True)
        assert all(k.before is None for k in written_file.keys), "chorusbench starts without a chorus.toml"
        assert {k.key for k in written_file.keys} == {
            f"{table}.{key}" for table, values in written.items() for key in values
        }

    def test_benchmark_workload(self, tmp_path: Path) -> None:
        ctx = dataclasses.replace(make_ctx(tmp_path), workload=WORKLOAD)
        layout = make_adapter().prepare(make_install(tmp_path), ctx)
        written = (layout.server_dir / chorus.CONFIG).read_text(encoding="utf-8")
        assert written.startswith("[server]\n")
        assert "autosave = false\n" in written and "threads = 4\n" in written

    def test_readback(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        assert adapter.readback(layout) == []
        path = layout.server_dir / chorus.CONFIG
        path.write_text(path.read_text(encoding="utf-8").replace("max_view_distance = 10", "max_view_distance = 32"))
        assert adapter.readback(layout) == [
            "chorus.toml: level.max_view_distance was written as 10 and read back as 32"
        ]
        path.unlink()
        assert adapter.readback(layout) == ["chorus.toml is missing after the run"]

    def test_tampered_install(self, tmp_path: Path) -> None:
        install = make_install(tmp_path)
        (install.root / chorus.EXE).write_bytes(b"changed")
        with pytest.raises(fetch.FetchError, match="does not match"):
            make_adapter().prepare(install, make_ctx(tmp_path))

    def test_facts(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        layout = adapter.prepare(make_install(tmp_path), make_ctx(tmp_path))
        runtime = {"build": BUILD_INFO, "threads": 4, "compute_threads": 1, "io_threads": 0, "chunk_saving": "modified"}
        (layout.server_dir / chorus.FACTS).write_text(json.dumps(runtime), encoding="utf-8")
        facts = adapter.facts(layout, None)  # type: ignore[arg-type]
        assert facts["chorus_commit"] == COMMIT and facts["threads"] == 4 and facts["io_threads"] == 0
        assert facts["difficulty"].startswith("peaceful") and facts["tick_range"] == 0
        (layout.server_dir / chorus.FACTS).unlink()
        assert adapter.facts(layout, None)["chorus_commit"] == COMMIT  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------------------------------------------------


class TestSource:
    def test_the_checked_in_source_matches_the_pin(self) -> None:
        src = config.server_dir("chorus")
        chorus.check_source(src, chorus.SourcePin.load(VERSION))
        assert chorus.locked_commit((src / "Cargo.lock").read_text(encoding="utf-8"), "chorus") == COMMIT

    def test_lock_hash_ignores_line_endings(self) -> None:
        assert chorus.lock_sha256(b"a\r\nb\r\n") == chorus.lock_sha256(b"a\nb\n")

    @pytest.mark.parametrize(
        ("edit", "message"),
        [
            (lambda m, lock: (m.replace(COMMIT, "0" * 40), lock), "names Chorus"),
            (lambda m, lock: (m + '\n[patch."https://github.com/x"]\n', lock), "must not patch"),
            (lambda m, lock: (m, lock + "\n"), "Cargo.lock has sha256"),
        ],
    )
    def test_source_problems(self, tmp_path: Path, edit: Any, message: str) -> None:
        src = tmp_path / "chorus"
        shutil.copytree(config.server_dir("chorus"), src, ignore=shutil.ignore_patterns("target"))
        manifest, lock = edit((src / "Cargo.toml").read_text(), (src / "Cargo.lock").read_text())
        (src / "Cargo.toml").write_text(manifest, newline="\n")
        (src / "Cargo.lock").write_text(lock, newline="\n")
        with pytest.raises(ConfigError, match=message):
            chorus.check_source(src, chorus.SourcePin.load(VERSION))

    def test_cargo_env(self, tmp_path: Path) -> None:
        base = {"PATH": "x", "RUSTFLAGS": "-C target-cpu=native", "CARGO_PROFILE_RELEASE_LTO": "fat", "CARGO_HOME": "h"}
        env = chorus.cargo_env("1.95.0", tmp_path, base)
        assert env == {"PATH": "x", "CARGO_HOME": "h", "RUSTUP_TOOLCHAIN": "1.95.0", "CARGO_TARGET_DIR": str(tmp_path)}


#: The real subprocess.run, for commands the fake does not handle.
REAL_RUN = subprocess.run


class FakeCargo:
    """Stands in for cargo, rustc and the built binary; records every command it handles."""

    def __init__(self) -> None:
        self.rustc = "rustc 1.95.0 (59807616e 2026-04-14)"
        self.info = dict(BUILD_INFO)
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def builds(self) -> list[tuple[list[str], dict[str, str] | None]]:
        return [(argv, env) for argv, env in self.calls if argv[1:2] == ["build"]]

    def __call__(self, argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(argv, str) or not argv or Path(argv[0]).stem not in ("cargo", "rustc", "chorusbench"):
            return REAL_RUN(argv, **kwargs)
        argv, env = list(argv), kwargs.get("env")
        self.calls.append((argv, env))
        out = ""
        if Path(argv[0]).stem == "rustc":
            out = self.rustc + "\n"
        elif argv[1:2] == ["build"]:
            assert env is not None
            target = Path(env["CARGO_TARGET_DIR"]) / "release" / chorus.EXE
            target.parent.mkdir(parents=True)
            target.write_bytes(b"chorusbench")
        elif argv[-1] == "-version":
            out = json.dumps(self.info)
        elif argv[-1] == "-defaults":
            out = DEFAULTS
        else:
            raise AssertionError(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def cargo(monkeypatch: pytest.MonkeyPatch) -> FakeCargo:
    fake = FakeCargo()
    monkeypatch.setattr(chorus.shutil, "which", lambda name: name if name in ("cargo", "rustc") else None)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


class TestFetch:
    def test_builds_once_and_records_the_build(self, cargo: FakeCargo, tmp_path: Path) -> None:
        cache = Cache(tmp_path / "cache")
        install = make_adapter().fetch(cache)
        assert (install.root / chorus.EXE).read_bytes() == b"chorusbench"
        assert (install.root / chorus.DEFAULTS).read_text(encoding="utf-8") == DEFAULTS
        lock, built = install.artifacts
        pin = chorus.SourcePin.load(VERSION)
        assert (lock.name, lock.sha256, lock.source) == ("Cargo.lock", pin.cargo_lock, "build")
        assert (built.name, built.source) == (chorus.EXE, "build") and install.extra["exe_sha256"] == built.sha256
        assert install.extra["chorus_commit"] == COMMIT
        [(argv, env)] = cargo.builds()
        assert argv[1:4] == ["build", "--release", "--locked"] and env is not None
        assert env["RUSTUP_TOOLCHAIN"] == "1.95.0"
        assert not Path(env["CARGO_TARGET_DIR"]).exists(), "the scratch target directory is removed"

        again = make_adapter().fetch(cache)
        assert again.tree_sha256 == install.tree_sha256
        assert len(cargo.builds()) == 1, "a cached build is reused"

    def test_other_rust_version(self, cargo: FakeCargo, tmp_path: Path) -> None:
        cargo.rustc = "rustc 1.96.0 (aaaaaaaaa 2026-06-01)"
        with pytest.raises(fetch.FetchError, match="rustup toolchain install 1.95.0"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_wrong_chorus_in_the_binary(self, cargo: FakeCargo, tmp_path: Path) -> None:
        cargo.info["chorus_commit"] = "6c82c07bcf7ff5afb54c5364bef49d20bdf0b116"
        with pytest.raises(fetch.FetchError, match="links Chorus 6c82c07"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_no_rust(self, cargo: FakeCargo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chorus.shutil, "which", lambda name: None)
        with pytest.raises(fetch.FetchError, match="Rust 1.95.0"):
            make_adapter().fetch(Cache(tmp_path / "cache"))

    def test_unpinned_version(self, tmp_path: Path) -> None:
        adapter = ChorusAdapter(config.arm_from_ref("chorus@abcdef0"), config.default_machine())
        with pytest.raises(ConfigError, match="abcdef0"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_adopts_a_prebuilt_binary(self, cargo: FakeCargo, tmp_path: Path) -> None:
        prebuilt = tmp_path / "chorusbench-local"
        prebuilt.write_bytes(b"prebuilt")
        install = make_adapter(artifact=prebuilt).fetch(Cache(tmp_path / "cache"))
        [built] = install.artifacts
        assert built.source == "local" and cargo.builds() == []

    def test_real_binary(self) -> None:
        """With BBENCH_CHORUSBENCH set to a chorusbench of the pinned build, its -defaults equal DEFAULTS."""
        exe = os.environ.get("BBENCH_CHORUSBENCH")
        if not exe:
            pytest.skip("set BBENCH_CHORUSBENCH to a chorusbench binary to check it")
        assert chorus.run_command([exe, "-defaults"], what="chorusbench -defaults") == DEFAULTS
        assert json.loads(chorus.run_command([exe, "-version"], what="chorusbench -version"))["chorus_commit"] == COMMIT
