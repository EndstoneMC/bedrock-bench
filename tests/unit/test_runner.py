# SPDX-License-Identifier: Apache-2.0
"""The runner and the phase helpers that need no live server: session servers and run order, pre-flight checks,
the bot command, placement and the smoke checks.

End-to-end runs against the fake server are in ``tests/integration``.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import runner
from bedrock_bench.adapters.base import host_os
from bedrock_bench.config import ConfigError, load_machine
from bedrock_bench.grid import cell
from bedrock_bench.model import Arm, LogLine
from bedrock_bench.phases import ENV_BOT_CMD, Placer, bot_command
from bedrock_bench.workload import SMOKE, WORKLOAD
from fakes.fake_adapter import FakeBds
from fixtures.runs import synthetic


def arms(*tags: str) -> list[Arm]:
    return [Arm(tag, *tag.split("@")) for tag in tags]


def ids(plan: list[runner.PlannedRun]) -> list[tuple[str, int]]:
    return [(run.arm.id, run.repeat) for run in plan]


# --------------------------------------------------------------------------------------------------------------------
# Session servers and run order
# --------------------------------------------------------------------------------------------------------------------


class TestPlanOrder:
    def test_rotate_shifts_each_block(self) -> None:
        plan = runner.plan_order(arms("a@1", "b@1"), repeats=2, order="rotate")
        assert ids(plan) == [("a@1", 1), ("b@1", 1), ("b@1", 2), ("a@1", 2)]
        assert [run.order_index for run in plan] == [0, 1, 2, 3]

    def test_rotate_three_servers(self) -> None:
        plan = runner.plan_order(arms("a@1", "b@1", "c@1"), repeats=3, order="rotate")
        blocks = [[run.arm.id[0] for run in plan if run.repeat == k] for k in (1, 2, 3)]
        assert blocks == [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"]]

    def test_sequential(self) -> None:
        plan = runner.plan_order(arms("a@1", "b@1"), repeats=2, order="sequential")
        assert ids(plan) == [("a@1", 1), ("b@1", 1), ("a@1", 2), ("b@1", 2)]

    def test_seeded_is_deterministic(self) -> None:
        servers = arms(*(f"s{i}@1" for i in range(6)))
        first = runner.plan_order(servers, repeats=3, order="seeded:7")
        assert ids(first) == ids(runner.plan_order(servers, repeats=3, order="seeded:7"))
        assert ids(first) != ids(runner.plan_order(servers, repeats=3, order="seeded:8"))
        for k in (1, 2, 3):
            assert sorted(run.arm.id for run in first if run.repeat == k) == sorted(a.id for a in servers)

    def test_bad_order(self) -> None:
        with pytest.raises(ValueError, match="order"):
            runner.plan_order(arms("a@1"), repeats=1, order="random")

    def test_dry_run_lines_name_one_server_each(self) -> None:
        plan = runner.plan_order(arms("a@1", "b@1"), repeats=1, order="rotate")
        assert "a@1" not in runner.PlannedRun.HEADER
        assert [line.split()[2] for line in map(runner.PlannedRun.describe, plan)] == ["a@1", "b@1"]


class TestSessionServers:
    def test_tags(self) -> None:
        found, skipped = runner.session_arms(["bds@1.26.52.3", "pocketmine@5.44.3"])
        assert [arm.id for arm in found] == ["bds@1.26.52.3", "pocketmine@5.44.3"] and skipped == []

    def test_a_default_tag_brings_its_bds_and_python(self) -> None:
        assert runner.tag_arm("endstone@0.11.14") == Arm("endstone@0.11.14", "endstone", "0.11.14", "1.26.52.3", "3.13")
        assert runner.tag_arm("endstone@0.11.13") == Arm("endstone@0.11.13", "endstone", "0.11.13")

    def test_a_tag_twice(self) -> None:
        with pytest.raises(ConfigError, match="twice"):
            runner.session_arms(["bds@1.26.52.3", "bds@1.26.52.3"])

    def test_defaults_skip_excluded_and_foreign_platform_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner, "host_os", lambda: "linux")
        found, skipped = runner.session_arms([])
        tags = [arm.id for arm in found]
        assert "bds@1.26.52.3" in tags and "endstone@0.11.14" in tags
        assert not any(tag.startswith(("chorus@", "levilamina@")) for tag in tags)
        assert skipped == [
            "chorus@95ca835: its player list gives the bots decode errors",
            "levilamina@26.51.6: runs on windows only",
        ]
        monkeypatch.setattr(runner, "host_os", lambda: "windows")
        assert "levilamina@26.51.6" in [arm.id for arm in runner.session_arms([])[0]]


def run_args(**values: Any) -> argparse.Namespace:
    defaults = {
        "tags": ["bds@1.26.52.3"],
        "repeats": 3,
        "order": "rotate",
        "cooldown": 60.0,
        "rerun_invalid": 1,
        "machine": None,
        "out": None,
        "dry_run": True,
        "verbose": 0,
    }
    return argparse.Namespace(**{**defaults, **values})


class TestRunSession:
    def test_dry_run(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        args = run_args(tags=["bds@1.26.52.3", "pocketmine@5.44.3"], repeats=2, out=tmp_path)
        assert runner.run_session(args) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("session ") and "4 runs, order rotate" in lines[0]
        assert [line.split()[2] for line in lines[2:]] == [
            "bds@1.26.52.3",
            "pocketmine@5.44.3",
            "pocketmine@5.44.3",
            "bds@1.26.52.3",
        ]
        assert not any(tmp_path.iterdir()), "a dry run writes nothing"

    @pytest.mark.parametrize(
        ("change", "message"),
        [({"repeats": 0}, "--repeats"), ({"cooldown": -1.0}, "--cooldown"), ({"rerun_invalid": -1}, "--rerun-invalid")],
    )
    def test_bad_options(self, change: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            runner.run_session(run_args(**change))

    def test_child_command(self, tmp_path: Path) -> None:
        run = runner.PlannedRun(3, 2, runner.tag_arm("endstone@0.11.14"))
        command = runner._child_command(run_args(order="seeded:4"), "S1", run, tmp_path)
        assert command[command.index("run-one") :] == [
            "run-one",
            "--server",
            "endstone@0.11.14",
            "--session",
            "S1",
            "--repeat",
            "2",
            "--order-index",
            "3",
            "--order",
            "seeded:4",
            "--out",
            str(tmp_path),
            "--bds",
            "1.26.52.3",
            "--python",
            "3.13",
        ]


# --------------------------------------------------------------------------------------------------------------------
# Requests and pre-flight checks
# --------------------------------------------------------------------------------------------------------------------


def namespace(**values: Any) -> argparse.Namespace:
    defaults = {
        "server": None,
        "bds": None,
        "python": None,
        "machine": None,
        "session": None,
        "repeat": 1,
        "order_index": None,
        "order": None,
        "artifact": None,
        "out": None,
    }
    return argparse.Namespace(**{**defaults, **values})


class TestRequest:
    def test_server_tag(self) -> None:
        request = runner._request(namespace(server="bds@1.26.52.3"))
        assert request.arm.id == "bds@1.26.52.3"
        assert request.order is None and request.order_index is None
        assert request.session_dir == runner.DEFAULT_OUT / request.session
        assert request.workload == WORKLOAD

    def test_session_run(self, tmp_path: Path) -> None:
        args = namespace(
            server="endstone@0.11.14",
            bds="1.26.52.3",
            python="3.13",
            session="S1",
            repeat=2,
            order_index=5,
            order="rotate",
            out=tmp_path,
        )
        request = runner._request(args)
        assert (request.arm.bds, request.arm.python) == ("1.26.52.3", "3.13")
        assert (request.session, request.repeat, request.order_index, request.order) == ("S1", 2, 5, "rotate")
        assert request.session_dir == tmp_path / "S1"

    def test_bad_session_id(self) -> None:
        with pytest.raises(ConfigError, match="session"):
            runner._request(namespace(server="bds@1.26.52.3", session="../x"))


class TestPreflight:
    def test_platform(self) -> None:
        machine = load_machine(None)
        adapter = FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), machine)
        runner.check_platform(adapter)

        class Elsewhere(FakeBds):
            platforms = frozenset({"windows", "linux"} - {host_os()}) or frozenset({"windows"})

        with pytest.raises(ConfigError, match="does not run on"):
            runner.check_platform(Elsewhere(adapter.arm, machine))

    def test_a_taken_port_is_refused(self) -> None:
        from bedrock_bench import host

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", 0))
            port = sock.getsockname()[1]
            machine = dataclasses.replace(load_machine(None), port=port)
            topology = host.detect_topology()
            placement = host.plan_placement(machine, topology)
            problems = runner.preflight(machine, placement, topology)
        assert any(str(port) in problem for problem in problems)


# --------------------------------------------------------------------------------------------------------------------
# Phase helpers
# --------------------------------------------------------------------------------------------------------------------


class TestBotCommand:
    def test_override_keeps_the_workload_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BOT_CMD, json.dumps(["python", "fake_bots.py"]))
        machine = load_machine(None)
        adapter = FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), machine)
        command = bot_command(adapter, SMOKE, machine)
        assert command.override
        assert command.argv[:2] == ("python", "fake_bots.py")
        assert command.argv[2:4] == ("-addr", f"127.0.0.1:{machine.port}")
        assert command.provenance["module"] == "loadbot"
        assert command.provenance["argv"] == ["loadbot", *command.argv[2:]]
        assert "-n" in command.argv and command.argv[command.argv.index("-n") + 1] == "4"

    @pytest.mark.parametrize("value", ["[]", "python", '{"a": 1}', "[1]"])
    def test_bad_override(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(ENV_BOT_CMD, value)
        machine = load_machine(None)
        adapter = FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), machine)
        with pytest.raises(ConfigError, match=ENV_BOT_CMD):
            bot_command(adapter, SMOKE, machine)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, **_: Any) -> None:
        self.sent.append(text)


class TestPlacer:
    def placer(self) -> tuple[Placer, Recorder]:
        adapter = FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), load_machine(None))
        console = Recorder()
        return Placer(adapter, SMOKE, console), console  # type: ignore[arg-type]

    @staticmethod
    def in_world(name: str) -> LogLine:
        text = f"Player Spawned: {name} xuid: 123, pfid: 0"
        return LogLine(0, 0, "info", None, text, text)

    def test_spawned_then_in_world(self) -> None:
        placer, console = self.placer()
        placer.on_bot_event({"ev": "spawned", "bot": "a03", "i": 3, "session": 1})
        assert console.sent == []
        placer.on_line(self.in_world("a03"))
        x, z = cell(3)
        assert console.sent[-1] == f"tp a03 {x} 300 {z}"
        assert len(console.sent) == 5 and all(c.startswith("effect a03 ") for c in console.sent[:4])
        assert [(p.bot, p.index) for p in placer.snapshot()] == [("a03", 3)]

    def test_in_world_then_spawned(self) -> None:
        placer, console = self.placer()
        placer.on_line(self.in_world("a00"))
        placer.on_bot_event({"ev": "spawned", "bot": "a00", "i": 0})
        assert console.sent[-1] == "tp a00 1280 300 1280"

    def test_a_new_dial_forgets_the_old_session(self) -> None:
        placer, console = self.placer()
        placer.on_line(self.in_world("a00"))
        placer.on_bot_event({"ev": "dial", "bot": "a00", "i": 0})
        placer.on_bot_event({"ev": "spawned", "bot": "a00", "i": 0})
        assert console.sent == []
        placer.on_line(self.in_world("a00"))
        assert len(console.sent) == 5

    def test_disabled_placer_only_places_on_request(self) -> None:
        placer, console = self.placer()
        placer.enabled = False
        placer.on_bot_event({"ev": "spawned", "bot": "a00", "i": 0})
        placer.on_line(self.in_world("a00"))
        assert console.sent == []
        placer.place("a00", 0)
        assert len(console.sent) == 5

    def test_other_lines_and_events_are_ignored(self) -> None:
        placer, console = self.placer()
        placer.on_bot_event({"ev": "stat", "online": 1})
        placer.on_bot_event({"ev": "spawned", "bot": "a00"})  # no index
        placer.on_line(LogLine(0, 0, "info", None, "Gametime is 5", "Gametime is 5"))
        assert console.sent == []


# --------------------------------------------------------------------------------------------------------------------
# Smoke checks
# --------------------------------------------------------------------------------------------------------------------


class TestSmokeProblems:
    def run_dir(self, tmp_path: Path, *, movement: str | None = None, corrections: float | None = 2.0) -> Path:
        path = tmp_path / "run"
        shutil.copytree(synthetic.HERE / "synthetic-steady", path)
        run = json.loads((path / "run.json").read_text(encoding="utf-8"))
        if movement is not None:
            run["parity"]["row"]["player.server_authoritative_movement"] = movement
        (path / "run.json").write_text(json.dumps(run), encoding="utf-8")
        metrics = json.loads((path / "metrics.json").read_text(encoding="utf-8"))
        metrics["metrics"]["corrections_per_bot_s"]["value"] = corrections
        (path / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        return path

    @staticmethod
    def adapter() -> FakeBds:
        return FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), load_machine(None))

    def test_a_clean_run(self, tmp_path: Path) -> None:
        assert runner.smoke_problems(self.run_dir(tmp_path, movement="vanilla"), self.adapter()) == []

    def test_declared_movement_validation_needs_corrections(self, tmp_path: Path) -> None:
        problems = runner.smoke_problems(self.run_dir(tmp_path, movement="vanilla", corrections=0.0), self.adapter())
        assert problems and "movement corrections" in problems[0]
        quiet = runner.smoke_problems(self.run_dir(tmp_path / "b", movement="absent", corrections=0.0), self.adapter())
        assert quiet == []

    def test_a_command_answered_with_an_error(self, tmp_path: Path) -> None:
        path = self.run_dir(tmp_path)
        run = json.loads((path / "run.json").read_text(encoding="utf-8"))
        run["arm"]["server"] = "bds"  # read the log with the BDS dialect
        (path / "run.json").write_text(json.dumps(run), encoding="utf-8")
        with gzip.open(path / "commands.jsonl.gz", "rt", encoding="utf-8") as fh:
            command = next(c for c in map(json.loads, fh) if c["origin"] == "phase")
        line = "[2026-10-12 13:58:00:123 ERROR] Syntax error: Unexpected token"
        with gzip.open(path / "server.log.gz", "at", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{command['send_ns'] + 1000}\t0\t{line}\n")
        problems = runner.smoke_problems(path, self.adapter())
        assert problems == [f"command {command['text']!r} was answered with: Syntax error: Unexpected token"]


def test_backend_names_the_platform() -> None:
    assert runner.backend() == f"native-{host_os()}"
