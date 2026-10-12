# SPDX-License-Identifier: Apache-2.0
"""Tests for bedrock_bench.bots and the bot's event contract."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import bots
from bedrock_bench.bots import BotBinary, BotError, BotExit, BotFleet, Target, ToolchainError
from bedrock_bench.config import ConfigError, load_schema
from workloads import WORKLOAD

GOLDEN = Path("bots") / "core" / "testdata" / "golden.jsonl"


# --------------------------------------------------------------------------------------------------------------------
# Event contract
# --------------------------------------------------------------------------------------------------------------------


def _event_kinds() -> set[str]:
    return set(load_schema("bot-event")["properties"]["ev"]["enum"])


def test_golden_events_match_the_schema(project_root: Path, schema_validator: Any) -> None:
    """The Go selftest output (pinned by bots/core/golden_test.go) is valid and covers every event kind."""
    validator = schema_validator("bot-event")
    lines = (project_root / GOLDEN).read_text(encoding="utf-8").splitlines()
    seen = set()
    for line in lines:
        event = json.loads(line)
        errors = sorted(validator.iter_errors(event), key=str)
        assert not errors, f"{event['ev']}: {errors[0].message}"
        seen.add(event["ev"])
    assert seen == _event_kinds()
    assert json.loads(lines[0])["ev"] == "hello"
    assert json.loads(lines[-1])["ev"] == "done"


def _host_binary(project_root: Path) -> Path | None:
    override = os.environ.get("BBENCH_LOADBOT")
    if override:
        return Path(override)
    path = bots.binary_path("loadbot", root=project_root)
    return path if path.is_file() else None


def test_built_binary_selftest_matches_the_schema(project_root: Path, schema_validator: Any) -> None:
    binary = _host_binary(project_root)
    if binary is None:
        pytest.skip("no loadbot binary; run 'bbench bots build' or set BBENCH_LOADBOT")
    proc = subprocess.run([str(binary), "-selftest-output"], capture_output=True, timeout=30, check=True)
    validator = schema_validator("bot-event")
    seen = set()
    for line in proc.stdout.decode("utf-8").splitlines():
        event = json.loads(line)
        errors = sorted(validator.iter_errors(event), key=str)
        assert not errors, f"{event['ev']}: {errors[0].message}"
        seen.add(event["ev"])
    assert seen == _event_kinds()


def test_provenance_matches_the_run_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    run_schema = load_schema("run")
    bot_schema = {"$defs": run_schema["$defs"], **run_schema["properties"]["bot"]}
    info = {
        "module": "loadbot",
        "version": "0.1.0",
        "vcs": "abc",
        "go": "go1.26.3",
        "gophertunnel": "v1.62.0",
        "patches": [],
        "protocol": 2193,
    }
    binary = BotBinary("loadbot", Path("/somewhere/loadbot"), "a" * 64, Target("linux", "amd64"), info)
    record = binary.provenance(["-addr", "127.0.0.1:19672", "-n", "4"])
    jsonschema.validate(record, bot_schema)
    assert record["argv"][0] == "loadbot"
    assert "somewhere" not in json.dumps(record)


# --------------------------------------------------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------------------------------------------------


def test_bot_args_are_derived_from_the_workload_only() -> None:
    params = WORKLOAD.bots
    a = bots.bot_args(params, addr="127.0.0.1:19672", csg=True)
    b = bots.bot_args(params, addr="127.0.0.1:19672", csg=True)
    assert a == b
    flags = dict(zip(a[::2], a[1::2], strict=False))
    assert flags["-n"] == "100"
    assert flags["-prefix"] == "a"
    assert flags["-stagger"] == "300ms"
    assert flags["-spawn-timeout"] == "180000ms"
    assert flags["-progress-interval"] == "5000ms"
    assert flags["-identity-seed"] == "bedrock-bench"
    assert "-csg=true" in a
    assert "-loadscreen=true" in a


def test_bot_args_compat_flags_are_allowlisted() -> None:
    params = WORKLOAD.bots
    plain = bots.bot_args(params, addr="127.0.0.1:1", csg=False)
    xuid = bots.bot_args(params, addr="127.0.0.1:1", csg=False, compat_flags=["-fake-xuid"])
    assert xuid == [*plain, "-fake-xuid"]
    assert "-csg=false" in plain
    with pytest.raises(ConfigError, match="allowlist"):
        bots.bot_args(params, addr="127.0.0.1:1", csg=False, compat_flags=["-walk=none"])


# --------------------------------------------------------------------------------------------------------------------
# Targets, paths, release metadata
# --------------------------------------------------------------------------------------------------------------------


def test_targets_and_asset_names(tmp_path: Path) -> None:
    linux = Target.parse("linux/amd64")
    windows = Target.parse("windows-amd64")
    assert str(linux) == "linux/amd64"
    assert bots.asset_name("loadbot", linux) == "loadbot-linux-amd64"
    assert bots.asset_name("loadbot", windows) == "loadbot-windows-amd64.exe"
    assert bots.binary_path("loadbot", windows, tmp_path) == tmp_path / "bots/bin/windows-amd64/loadbot.exe"
    host = Target.host()
    assert host.goos in ("windows", "linux", "darwin")
    for bad in ("linux", "plan9/amd64", "linux/mips"):
        with pytest.raises(ConfigError):
            Target.parse(bad)
    with pytest.raises(ConfigError):
        bots.asset_name("nosuchbot", linux)


def test_module_for_protocol() -> None:
    assert bots.module_for_protocol(2193) == "loadbot"
    assert bots.module_for_protocol(1001) == "legacy1001"
    with pytest.raises(BotError):
        bots.module_for_protocol(1)


def test_parse_sha256sums() -> None:
    a, b = "a" * 64, "B" * 64
    sums = bots.parse_sha256sums(f"{a}  loadbot-linux-amd64\n{b} *loadbot-windows-amd64.exe\n\n")
    assert sums == {"loadbot-linux-amd64": a, "loadbot-windows-amd64.exe": b.lower()}
    for bad in ("nothex  file", f"{a}", f"{a[:-1]}  file"):
        with pytest.raises(BotError):
            bots.parse_sha256sums(bad)


def test_release_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bots.ENV_RELEASE_TAG, raising=False)
    monkeypatch.setattr(bots, "__version__", "0.1.0.dev0")
    with pytest.raises(BotError, match="not a release"):
        bots.release_tag()
    monkeypatch.setattr(bots, "__version__", "0.2.1")
    assert bots.release_tag() == "v0.2.1"
    monkeypatch.setenv(bots.ENV_RELEASE_TAG, "v9.9.9")
    assert bots.release_tag() == "v9.9.9"


# --------------------------------------------------------------------------------------------------------------------
# Build, fetch, locate
# --------------------------------------------------------------------------------------------------------------------


def _fake_checkout(tmp_path: Path) -> Path:
    (tmp_path / "bots" / "loadbot").mkdir(parents=True)
    (tmp_path / "bots" / "loadbot" / "go.mod").write_text("module example/loadbot\n", encoding="utf-8")
    return tmp_path


def test_build_runs_a_reproducible_go_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_checkout(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> None:
        calls.append((cmd, kwargs))
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"binary")

    info = {"module": "loadbot", "version": "0.1.0", "protocol": 2193, "patches": []}
    monkeypatch.setattr(bots, "version_info", lambda path: info)
    target = Target.host()
    binary = bots.build("loadbot", target, root=root, go="go", version="0.1.0", commit="abc", run=fake_run)

    cmd, kwargs = calls[0]
    assert cmd[:4] == ["go", "build", "-trimpath", "-buildvcs=false"]
    assert cmd[cmd.index("-ldflags") + 1] == "-s -w -buildid= -X main.version=0.1.0 -X main.commit=abc"
    assert kwargs["cwd"] == root / "bots" / "loadbot"
    env = kwargs["env"]
    assert (env["CGO_ENABLED"], env["GOOS"], env["GOARCH"], env["GOWORK"]) == ("0", target.goos, target.goarch, "off")
    assert binary.path == bots.binary_path("loadbot", target, root)
    assert binary.sha256 == bots.sha256_file(binary.path)
    assert binary.info == info

    monkeypatch.setattr(bots, "version_info", lambda path: pytest.fail("manifest should be used"))
    found = bots.locate("loadbot", target=target, root=root)
    assert (found.sha256, found.info["protocol"], found.source) == (binary.sha256, 2193, "build")


def test_locate_ignores_a_stale_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_checkout(tmp_path)
    cross = Target("linux", "arm64") if Target.host() != Target("linux", "arm64") else Target("windows", "amd64")

    def fake_run(cmd: list[str], **kwargs: Any) -> None:
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"v1")

    bots.build("loadbot", cross, root=root, go="go", commit="", run=fake_run)
    path = bots.binary_path("loadbot", cross, root)
    path.write_bytes(b"v2")  # replaced behind the manifest's back
    found = bots.locate("loadbot", target=cross, root=root)
    assert found.sha256 == bots.sha256_file(path)
    assert found.info == {}
    with pytest.raises(BotError, match="not found"):
        bots.locate("loadbot", target=Target("darwin", "arm64"), root=root)


def test_build_without_go_is_a_toolchain_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(bots.shutil, "which", lambda name: None)
    with pytest.raises(ToolchainError, match="Go toolchain"):
        bots.build("loadbot", root=root)


def test_build_failure_leaves_no_binary(tmp_path: Path) -> None:
    root = _fake_checkout(tmp_path)

    def failing_run(cmd: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    with pytest.raises(ToolchainError, match="exit code 1"):
        bots.build("loadbot", root=root, go="go", commit="", run=failing_run)
    assert not bots.binary_path("loadbot", root=root).exists()


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _fake_release(files: dict[str, bytes]) -> Any:
    def urlopen(url: str, timeout: float) -> _Response:
        name = url.rsplit("/", 1)[1]
        if name not in files:
            raise OSError(f"404 {url}")
        return _Response(files[name])

    return urlopen


def test_fetch_installs_a_verified_release_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    target = Target("linux", "amd64")
    payload = b"\x7fELF fake bot"
    sums = f"{hashlib.sha256(payload).hexdigest()}  loadbot-linux-amd64\n".encode()
    urlopen = _fake_release({"SHA256SUMS": sums, "loadbot-linux-amd64": payload})
    monkeypatch.setattr(bots, "version_info", lambda path: {"module": "loadbot", "protocol": 2193})
    binary = bots.fetch(
        "loadbot", target, root=tmp_path, tag="v0.1.0", base_url="https://example.invalid/dl", urlopen=urlopen
    )
    assert binary.path.read_bytes() == payload
    assert binary.source == "release"
    manifest = json.loads(binary.path.with_name("loadbot.json").read_text(encoding="utf-8"))
    assert manifest["url"] == "https://example.invalid/dl/v0.1.0/loadbot-linux-amd64"
    assert manifest["sha256"] == binary.sha256


def test_fetch_rejects_a_hash_mismatch(tmp_path: Path) -> None:
    target = Target("linux", "amd64")
    urlopen = _fake_release({"SHA256SUMS": f"{'0' * 64}  loadbot-linux-amd64\n".encode(), "loadbot-linux-amd64": b"x"})
    with pytest.raises(BotError, match="does not match"):
        bots.fetch("loadbot", target, root=tmp_path, tag="v0.1.0", base_url="https://example.invalid", urlopen=urlopen)
    assert not bots.binary_path("loadbot", target, tmp_path).exists()
    assert list(bots.binary_path("loadbot", target, tmp_path).parent.iterdir()) == []
    with pytest.raises(BotError, match="has no"):
        bots.fetch(
            "loadbot", Target("windows", "amd64"), root=tmp_path, tag="v0.1.0", base_url="https://x", urlopen=urlopen
        )


# --------------------------------------------------------------------------------------------------------------------
# BotFleet against a scripted fake bot
# --------------------------------------------------------------------------------------------------------------------

FAKE_BOT = textwrap.dedent(
    """
    import json, os, sys, time

    def emit(ev, **fields):
        sys.stdout.write(json.dumps({"v": 1, "ev": ev, "t": int(time.time() * 1000), "mono_ms": 0, **fields}) + "\\n")
        sys.stdout.flush()

    stubborn = "--stubborn" in sys.argv
    build = {"module": "loadbot", "version": "test", "protocol": 2193, "patches": []}
    intervals = {"progress": 5000, "stat": 10000, "pkt": 60000}
    emit("hello", build=build, flags={"gomaxprocs": os.environ.get("GOMAXPROCS")}, intervals_ms=intervals)
    sys.stdout.write("this is not an event\\n")
    sys.stdout.flush()
    emit("dial", bot="a00", i=0)
    emit("alljoined", joined=1, failed=0)
    for line in sys.stdin:
        line = line.strip()
        if line == "quit" and not stubborn:
            emit("bot_summary", bot="a00", uniq=1, in_view=1, chunks=1, corrections=0, first_seen_ms=[5])
            emit("done")
            sys.exit(0)
    time.sleep(60)
    """
)


@pytest.fixture
def fake_bot(tmp_path: Path) -> Path:
    path = tmp_path / "fake_bot.py"
    path.write_text(FAKE_BOT, encoding="utf-8")
    return path


def test_fleet_reads_events_and_stops_with_quit(fake_bot: Path, tmp_path: Path) -> None:
    received: list[dict[str, Any]] = []
    fleet = BotFleet(
        [sys.executable, "-u", str(fake_bot)], cpus=[0], on_event=received.append, stderr_path=tmp_path / "bots.stderr"
    )
    with fleet:
        assert fleet.wait_for("alljoined", timeout_s=20) is not None
        hello = fleet.hello
        assert hello is not None and hello["flags"]["gomaxprocs"] == "1"
        import psutil

        assert psutil.Process(fleet.pid).cpu_affinity() == [0]
    assert fleet.returncode == BotExit.OK
    assert fleet.finished
    kinds = [ev["ev"] for ev in fleet.events()]
    assert kinds == ["hello", "dial", "alljoined", "bot_summary", "done"]
    assert [ev["ev"] for ev in received] == kinds
    stamps = [ev["rx_ns"] for ev in fleet.events()]
    assert stamps == sorted(stamps) and all(isinstance(s, int) for s in stamps)
    assert fleet.junk_lines == ["this is not an event"]
    assert fleet.events("dial")[0]["bot"] == "a00"
    assert fleet.last("bot_summary")["first_seen_ms"] == [5]
    assert not fleet.send("quit"), "stdin must be closed after stop"


def test_fleet_escalates_when_the_bot_ignores_quit(fake_bot: Path) -> None:
    fleet = BotFleet([sys.executable, "-u", str(fake_bot), "--stubborn"])
    fleet.start()
    try:
        assert fleet.wait_for("alljoined", timeout_s=20) is not None
        started = time.monotonic()
        code = fleet.stop(quit_timeout_s=0.5, signal_timeout_s=2.0)
        assert code is not None and code != BotExit.OK
        assert not fleet.alive
        assert time.monotonic() - started < 15
        assert not fleet.finished
    finally:
        fleet.kill()


def test_fleet_wait_for_returns_none_at_end_of_output(tmp_path: Path) -> None:
    script = tmp_path / "short.py"
    script.write_text('print(\'{"v":1,"ev":"hello","t":0,"mono_ms":0}\')\n', encoding="utf-8")
    with BotFleet([sys.executable, str(script)]) as fleet:
        assert fleet.wait_for("hello", timeout_s=20) is not None
        assert fleet.wait_for("alljoined", timeout_s=20) is None
    assert fleet.returncode == 0


def test_fleet_start_failure_is_a_bot_error(tmp_path: Path) -> None:
    with pytest.raises(BotError, match="cannot start"):
        BotFleet([str(tmp_path / "no-such-binary")]).start()


def test_send_rejects_line_breaks(fake_bot: Path) -> None:
    fleet = BotFleet([sys.executable, str(fake_bot)])
    with pytest.raises(ValueError):
        fleet.send("quit\nquit")
