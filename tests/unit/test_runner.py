# SPDX-License-Identifier: Apache-2.0
"""The runner and the phase helpers that need no live server: run order, matrix-directory refusal, pre-flight checks,
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import runner
from bedrock_bench.adapters.base import host_os
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError, load_machine, load_scenario, parse_matrix, parse_scenario
from bedrock_bench.grid import cell
from bedrock_bench.model import Arm, LogLine, Matrix
from bedrock_bench.phases import ENV_BOT_CMD, Placer, bot_command
from fakes.bench import FAKE_SCENARIOS
from fakes.fake_adapter import FakeBds
from fixtures.runs import synthetic


def matrix(arms: list[str], *, scenarios: tuple[str, ...] = ("s1",), **head: Any) -> Matrix:
    data = {"matrix": {"name": "m", "scenarios": list(scenarios), **head}, "arm": [{"server": a} for a in arms]}
    return parse_matrix(data)


def ids(plan: list[runner.PlannedRun]) -> list[tuple[str, int]]:
    return [(run.arm.id, run.repeat) for run in plan]


# --------------------------------------------------------------------------------------------------------------------
# Run order
# --------------------------------------------------------------------------------------------------------------------


class TestPlanOrder:
    def test_rotate_shifts_each_block(self) -> None:
        plan = runner.plan_order(matrix(["a@1", "b@1"]), repeats=2, order="rotate")
        assert ids(plan) == [("a@1", 1), ("b@1", 1), ("b@1", 2), ("a@1", 2)]
        assert [run.order_index for run in plan] == [0, 1, 2, 3]

    def test_rotate_three_arms(self) -> None:
        plan = runner.plan_order(matrix(["a@1", "b@1", "c@1"]), repeats=3, order="rotate")
        blocks = [[run.arm.id[0] for run in plan if run.repeat == k] for k in (1, 2, 3)]
        assert blocks == [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"]]

    def test_sequential(self) -> None:
        plan = runner.plan_order(matrix(["a@1", "b@1"]), repeats=2, order="sequential")
        assert ids(plan) == [("a@1", 1), ("b@1", 1), ("a@1", 2), ("b@1", 2)]

    def test_seeded_is_deterministic(self) -> None:
        m = matrix([f"s{i}@1" for i in range(6)])
        first = runner.plan_order(m, repeats=3, order="seeded:7")
        assert ids(first) == ids(runner.plan_order(m, repeats=3, order="seeded:7"))
        assert ids(first) != ids(runner.plan_order(m, repeats=3, order="seeded:8"))
        for k in (1, 2, 3):
            assert sorted(run.arm.id for run in first if run.repeat == k) == sorted(a.id for a in m.arms)

    def test_scenarios_and_arm_subsets(self) -> None:
        data = {
            "matrix": {"name": "m", "scenarios": ["s1", "s2"]},
            "arm": [{"server": "a@1"}, {"server": "b@1", "scenarios": ["s2"]}],
        }
        plan = runner.plan_order(parse_matrix(data), repeats=1, order="rotate")
        assert [(run.arm.id, run.scenario) for run in plan] == [("a@1", "s1"), ("a@1", "s2"), ("b@1", "s2")]

    def test_only(self) -> None:
        plan = runner.plan_order(matrix(["a@1", "b@1", "c@1"]), repeats=1, order="rotate", only=["c@1", "a@1"])
        assert [run.arm.id for run in plan] == ["a@1", "c@1"]
        with pytest.raises(ConfigError, match="no arm 'd@1'"):
            runner.plan_order(matrix(["a@1"]), repeats=1, order="rotate", only=["d@1"])

    def test_dry_run_lines_name_one_arm_each(self) -> None:
        plan = runner.plan_order(matrix(["a@1", "b@1"]), repeats=1, order="rotate")
        assert "a@1" not in runner.PlannedRun.HEADER
        assert [line.split()[2] for line in map(runner.PlannedRun.describe, plan)] == ["a@1", "b@1"]


class TestMatrixDirectory:
    MACHINE = load_machine(None)
    SCENARIO = parse_scenario({"server": {"csg": False}}, name="s1")

    def recorded(self, **machine: Any) -> dict[str, Any]:
        return {
            "machine": {"profile_sha256": self.MACHINE.profile_sha256, "fingerprint_id": "f1", **machine},
            "scenario": {"name": "s1", "sha256": self.SCENARIO.sha256},
        }

    def problems(self, record: dict[str, Any]) -> list[str]:
        return runner.mixing_problems([record], self.MACHINE, "f1", {"s1": self.SCENARIO})

    def test_same_profile_machine_and_scenario(self) -> None:
        assert self.problems(self.recorded()) == []

    def test_another_profile(self) -> None:
        assert "another machine profile" in self.problems(self.recorded(profile_sha256="0" * 64))[0]

    def test_another_machine(self) -> None:
        assert "another machine" in self.problems(self.recorded(fingerprint_id="f2"))[0]

    def test_another_scenario_version(self) -> None:
        record = {**self.recorded(), "scenario": {"name": "s1", "sha256": "0" * 64}}
        assert "earlier version of scenario s1" in self.problems(record)[0]

    def test_existing_records(self, tmp_path: Path) -> None:
        (tmp_path / "r1").mkdir()
        (tmp_path / "r1" / "run.json").write_text('{"run_id": "r1"}', encoding="utf-8")
        (tmp_path / "r2").mkdir()
        (tmp_path / "r2" / "run.partial.json").write_text('{"run_id": "r2"}', encoding="utf-8")
        (tmp_path / "r3").mkdir()
        (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
        assert [r["run_id"] for r in runner.existing_records(tmp_path)] == ["r1", "r2"]
        assert runner.existing_records(tmp_path / "missing") == []


# --------------------------------------------------------------------------------------------------------------------
# Requests and pre-flight checks
# --------------------------------------------------------------------------------------------------------------------


def namespace(**values: Any) -> argparse.Namespace:
    defaults = {
        "server": None,
        "matrix": None,
        "arm": None,
        "scenario": "smoke",
        "machine": None,
        "variant": None,
        "repeat": 1,
        "order_index": None,
        "artifact": None,
        "out": None,
    }
    return argparse.Namespace(**{**defaults, **values})


class TestRequest:
    def test_server_tag(self) -> None:
        request = runner._request(namespace(server="bds@1.26.52.3", variant="x"))
        assert request.arm.id == "bds@1.26.52.3+x"
        assert request.matrix is None and request.order is None
        assert request.matrix_dir == runner.DEFAULT_OUT / "smoke"

    def test_matrix_arm(
        self, write_file: Callable[[str, str], Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = write_file(
            "configs/matrices/m.toml",
            '[matrix]\nname = "m"\nscenarios = ["smoke"]\norder = "rotate"\npriority_override = "above_normal"\n'
            '[[arm]]\nserver = "bds@1.26.52.3"\n',
        )
        monkeypatch.setenv(runner.ENV_MATRIX_ORDER, "sequential")
        request = runner._request(namespace(matrix=str(path), arm="bds@1.26.52.3", repeat=2, out=tmp_path))
        assert request.machine.priority == "above_normal"
        assert request.order == "sequential"
        assert request.matrix_dir == tmp_path / "m"

    def test_matrix_arm_errors(self, write_file: Callable[[str, str], Path]) -> None:
        path = write_file(
            "configs/matrices/m.toml", '[matrix]\nscenarios = ["spread100"]\n[[arm]]\nserver = "bds@1.0"\n'
        )
        with pytest.raises(ConfigError, match="no arm"):
            runner._request(namespace(matrix=str(path), arm="bds@2.0", scenario="spread100"))
        with pytest.raises(ConfigError, match="does not run scenario smoke"):
            runner._request(namespace(matrix=str(path), arm="bds@1.0"))
        with pytest.raises(ConfigError, match="go with --server"):
            runner._request(namespace(matrix=str(path), arm="bds@1.0", scenario="spread100", variant="x"))


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

    def test_template(self, tmp_path: Path) -> None:
        cache = Cache(tmp_path)
        arm = Arm("x@1", "x", "1")
        assert runner.template_for(cache, arm, load_scenario("spread100")) is None
        pregen = load_scenario("spread100-pregen")
        with pytest.raises(ConfigError, match="bbench prepare-world x@1 --scenario spread100-pregen"):
            runner.template_for(cache, arm, pregen)
        expected = cache.template_dir("x", "1", "spread100-pregen")
        expected.mkdir(parents=True)
        assert runner.template_for(cache, arm, pregen) == expected


# --------------------------------------------------------------------------------------------------------------------
# Phase helpers
# --------------------------------------------------------------------------------------------------------------------


class TestBotCommand:
    def test_override_keeps_the_scenario_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BOT_CMD, json.dumps(["python", "fake_bots.py"]))
        machine = load_machine(None)
        adapter = FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), machine)
        scenario = load_scenario(FAKE_SCENARIOS / "fake-quick.toml")
        command = bot_command(adapter, scenario, machine)
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
            bot_command(adapter, load_scenario("smoke"), machine)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, **_: Any) -> None:
        self.sent.append(text)


class TestPlacer:
    def placer(self) -> tuple[Placer, Recorder]:
        adapter = FakeBds(Arm("fake_bds@1.0.0", "fake_bds", "1.0.0"), load_machine(None))
        console = Recorder()
        return Placer(adapter, load_scenario("smoke"), console), console  # type: ignore[arg-type]

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

    def test_chaos_commands_must_be_sent(self, tmp_path: Path) -> None:
        problems = runner.smoke_problems(self.run_dir(tmp_path), self.adapter(), ["weather clear"])
        assert problems == ["chaos command 'weather clear' was never sent"]


def test_backend_names_the_platform() -> None:
    assert runner.backend() == f"native-{host_os()}"
