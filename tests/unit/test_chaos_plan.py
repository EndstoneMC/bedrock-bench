# SPDX-License-Identifier: Apache-2.0
"""The chaos catalogue, the plan generator and the adapter checks in ``bedrock_bench.chaos``."""

from __future__ import annotations

import copy
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, chaos, config
from bedrock_bench.adapters.base import ChaosDialect
from bedrock_bench.adapters.bds import BdsAdapter
from bedrock_bench.config import ConfigError
from bedrock_bench.grid import cell
from bedrock_bench.model import Scenario

ROOT = config.project_root()
CATALOGUE_FILE = ROOT / "configs" / "scenarios" / "chaos-actions.toml"

#: Action intervals the catalogue must keep.
INTERVALS = {
    "churn": 20,
    "stack": 25,
    "waypoints": 60,
    "gamemode": 15,
    "kill": 30,
    "summon": 30,
    "cleanup": 90,
    "kick": 40,
    "dimension": 20,
    "list": 10,
    "far_tp": 15,
    "view_disconnect": 30,
    "tickspeed": 30,
    "weather": 45,
    "blocks": 20,
    "tickingarea": 40,
}

#: Plan hash of the full catalogue for 100 bots, 900 s and seed 20261010 (see ``golden_scenario``). It must be the same
#: on every platform and Python version. A deliberate change to the planner or the catalogue changes it: review the new
#: plan, then update this value.
GOLDEN_SHA256 = "c6a891954ef84cc7a22a7a548982c44af24cc1bb45e3084712936f622a4033a7"


def raw_catalogue() -> dict[str, Any]:
    return tomllib.loads(CATALOGUE_FILE.read_text(encoding="utf-8"))


def parse(data: dict[str, Any]) -> chaos.Catalogue:
    return chaos.parse_catalogue(data, name="test")


def scenario(
    actions: list[str],
    *,
    n: int = 100,
    duration: float = 900.0,
    seed: int = 20261010,
    cycle: tuple[int, ...] = (),
    name: str = "chaos-test",
) -> Scenario:
    data: dict[str, Any] = {
        "server": {"csg": False},
        "bots": {"n": n, "prefix": "a"},
        "phases": {"chaos": {"seed": seed, "duration_s": duration, "actions": actions, "tickspeed_cycle": list(cycle)}},
    }
    return config.parse_scenario(data, name=name)


def golden_scenario(**changes: Any) -> Scenario:
    options: dict[str, Any] = {"cycle": (0, 1, 3, 100, 1000, 1)} | changes
    return scenario(list(INTERVALS), **options)


@pytest.fixture(scope="module")
def catalogue() -> chaos.Catalogue:
    return chaos.load_catalogue()


@pytest.fixture(scope="module")
def plan(catalogue: chaos.Catalogue) -> chaos.Plan:
    return chaos.build_plan(golden_scenario(), catalogue)


def bds_adapter(server: str = "bds") -> Any:
    version = {"bds": "1.26.52.3", "endstone": "0.11.14"}[server]
    return adapters.create(config.arm_from_ref(f"{server}@{version}"), config.default_machine())


def key(step: chaos.Step) -> tuple[Any, ...]:
    """A step without its id and parent link, for comparing plans built from different action lists."""
    return (step.t_s, step.action, step.step, step.commands, step.ops, step.confirm, step.expect)


# --------------------------------------------------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------------------------------------------------


class TestCatalogue:
    def test_shipped_catalogue_declares_every_action(self, catalogue: chaos.Catalogue) -> None:
        assert set(catalogue.actions) == set(chaos.ACTIONS) == set(INTERVALS)
        assert catalogue.far_cells > 0 and len(catalogue.sha256) == 64

    def test_intervals_match_the_spec(self, catalogue: chaos.Catalogue) -> None:
        assert {name: action.interval_s for name, action in catalogue.actions.items()} == INTERVALS

    def test_examples_name_every_parameter(self, catalogue: chaos.Catalogue) -> None:
        for spec in catalogue.commands.values():
            assert set(spec.example) == set(spec.params), spec.name

    def test_bot_side_actions_need_no_command(self, catalogue: chaos.Catalogue) -> None:
        assert catalogue.actions["churn"].requires == ()

    def test_confirmed_actions(self, catalogue: chaos.Catalogue) -> None:
        confirmed = {name: a.confirm for name, a in catalogue.actions.items() if a.confirm}
        assert confirmed == {"tickspeed": "randomtickspeed", "waypoints": "playerwaypoints"}

    def test_parse_matches_load(self, catalogue: chaos.Catalogue) -> None:
        parsed = parse(raw_catalogue())
        assert parsed.commands == catalogue.commands and parsed.actions == catalogue.actions

    @pytest.mark.parametrize(
        ("where", "message"),
        [
            ((), "unknown key"),
            (("command", "tp"), "unknown key"),
            (("action", "kick"), "unknown key"),
        ],
    )
    def test_unknown_keys_are_rejected(self, where: tuple[str, ...], message: str) -> None:
        data = raw_catalogue()
        table = data
        for part in where:
            table = table[part]
        table["typo"] = 1
        with pytest.raises(ConfigError, match=message):
            parse(data)

    def test_schema_version(self) -> None:
        data = raw_catalogue() | {"schema": 2}
        with pytest.raises(ConfigError, match="schema"):
            parse(data)

    def test_example_must_match_params(self) -> None:
        data = raw_catalogue()
        del data["command"]["tp"]["example"]["z"]
        with pytest.raises(ConfigError, match=r"command\.tp\.example"):
            parse(data)

    def test_unknown_action(self) -> None:
        data = raw_catalogue()
        data["action"]["explode"] = copy.deepcopy(data["action"]["list"])
        with pytest.raises(ConfigError, match=r"action\.explode.*no such chaos action"):
            parse(data)

    def test_required_command_must_be_declared(self) -> None:
        data = raw_catalogue()
        data["action"]["kick"]["requires"] = ["kick", "ban"]
        with pytest.raises(ConfigError, match=r"action\.kick\.requires.*ban"):
            parse(data)

    def test_confirm_must_be_required(self) -> None:
        data = raw_catalogue()
        data["action"]["kick"]["confirm"] = "list"
        with pytest.raises(ConfigError, match=r"action\.kick\.confirm"):
            parse(data)

    def test_optional_must_not_repeat_required(self) -> None:
        data = raw_catalogue()
        data["action"]["kill"]["optional"] = ["kill"]
        with pytest.raises(ConfigError, match=r"action\.kill\.optional"):
            parse(data)

    @pytest.mark.parametrize(
        ("action", "param", "value"),
        [
            ("stack", "hold_s", [30, 15]),
            ("stack", "hold_s", [0, 5]),
            ("gamemode", "bots", [3, 1]),
            ("summon", "reconnect_p", 1.5),
            ("weather", "cycle", []),
            ("kick", "bots", "two"),
        ],
    )
    def test_bad_parameters(self, action: str, param: str, value: Any) -> None:
        data = raw_catalogue()
        data["action"][action][param] = value
        with pytest.raises(ConfigError, match=rf"action\.{action}\.{param}"):
            parse(data)

    def test_missing_parameter(self) -> None:
        data = raw_catalogue()
        del data["action"]["stack"]["hold_s"]
        with pytest.raises(ConfigError, match=r"action\.stack\.hold_s: is required"):
            parse(data)

    def test_edits_must_match_their_command(self) -> None:
        data = raw_catalogue()
        data["action"]["blocks"]["edits"][0] = {"command": "fill", "dx1": 0, "block": "stone"}
        with pytest.raises(ConfigError, match=r"edits\[0\]"):
            parse(data)
        data["action"]["blocks"]["edits"][0] = {"command": "kill"}
        with pytest.raises(ConfigError, match=r"edits\[0\]\.command"):
            parse(data)


# --------------------------------------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------------------------------------


class TestPlan:
    def test_same_seed_same_plan(self, catalogue: chaos.Catalogue, plan: chaos.Plan) -> None:
        again = chaos.build_plan(golden_scenario(), catalogue)
        assert again.sha256 == plan.sha256 and again.lines() == plan.lines()

    def test_golden_hash(self, plan: chaos.Plan) -> None:
        assert plan.sha256 == GOLDEN_SHA256, "the plan changed; review it, then update GOLDEN_SHA256"

    def test_other_seed_other_plan(self, catalogue: chaos.Catalogue, plan: chaos.Plan) -> None:
        assert chaos.build_plan(golden_scenario(seed=1), catalogue).sha256 != plan.sha256

    def test_header_records_the_inputs(self, catalogue: chaos.Catalogue, plan: chaos.Plan) -> None:
        header = plan.header
        assert header["plan"] == chaos.PLAN_FORMAT and header["sha256"] == chaos.plan_sha256(plan.steps)
        assert header["seed"] == 20261010 and header["n"] == 100 and header["duration_s"] == 900.0
        assert header["actions"] == list(INTERVALS) and header["steps"] == len(plan.steps)
        assert header["catalogue_sha256"] == catalogue.sha256
        assert header["scenario"] == "chaos-test" and len(header["scenario_sha256"]) == 64

    def test_actions_do_not_disturb_each_other(self, catalogue: chaos.Catalogue, plan: chaos.Plan) -> None:
        alone = chaos.build_plan(scenario(["kick", "far_tp"]), catalogue)
        assert [key(s) for s in alone.steps] == [key(s) for s in plan.steps if s.action in ("kick", "far_tp")]

    def test_retuning_an_action_keeps_its_schedule(self, catalogue: chaos.Catalogue) -> None:
        data = raw_catalogue()
        data["action"]["far_tp"]["bots"] = [1, 1]
        retuned = chaos.build_plan(scenario(["far_tp"]), parse(data))
        original = chaos.build_plan(scenario(["far_tp"]), catalogue)
        assert [s.t_s for s in retuned.steps] == [s.t_s for s in original.steps]
        assert all(len(s.commands) == 1 for s in retuned.steps)

    def test_ids_follow_time(self, plan: chaos.Plan) -> None:
        assert [s.id for s in plan.steps] == list(range(len(plan.steps)))
        assert [s.t_s for s in plan.steps] == sorted(s.t_s for s in plan.steps)
        assert all(0 < s.t_s <= 900.0 for s in plan.steps)

    def test_firing_times(self, catalogue: chaos.Catalogue, plan: chaos.Plan) -> None:
        firings: dict[str, list[float]] = defaultdict(list)
        for step in plan.steps:
            if step.parent is None:
                firings[step.action].append(step.t_s)
        assert set(firings) == set(INTERVALS)
        for action, times in firings.items():
            interval = catalogue.actions[action].interval_s
            assert 3.0 - 1e-3 <= times[0] <= min(interval, 60.0) + 1e-3, action
            for earlier, later in zip(times, times[1:], strict=False):
                assert 0.6 * interval - 2e-3 <= later - earlier <= 1.4 * interval + 2e-3, action
            assert times[-1] >= 900.0 - 1.4 * interval - 2e-3, f"{action} stops early"

    def test_follow_ups(self, plan: chaos.Plan) -> None:
        followed = 0
        for step in plan.steps:
            if step.parent is None:
                continue
            parent = plan.steps[step.parent]
            assert parent.action == step.action and parent.parent is None
            assert parent.t_s < step.t_s <= 900.0
            assert set(step.targets) <= set(parent.targets)
            followed += 1
        assert followed > 100

    def test_steps_use_declared_commands(self, catalogue: chaos.Catalogue, plan: chaos.Plan) -> None:
        for step in plan.steps:
            action = catalogue.actions[step.action]
            assert step.commands or step.ops, step
            for command in step.commands:
                spec = catalogue.commands[command.command]
                assert command.command in action.commands, (step.action, command.command)
                assert set(command.params) == set(spec.params) - {"player"}, command
                assert (command.bot is not None) == spec.takes_player, command
                assert command.bot is None or 0 <= command.bot < 100
            for op in step.ops:
                assert op.op == "reconnect" and 0 <= op.bot < 100 and op.delay_ms is not None
            if step.confirm is not None:
                assert step.confirm == action.confirm
                assert any(c.command == step.confirm for c in step.commands)

    def test_bot_counts(self, plan: chaos.Plan) -> None:
        counts: dict[str, set[int]] = defaultdict(set)
        for step in plan.steps:
            if step.parent is None:
                counts[step.action].add(len(step.targets))
        assert counts["churn"] == {10} and counts["kill"] == {2} and counts["view_disconnect"] == {1}
        assert counts["stack"] <= set(range(5, 11)) and counts["gamemode"] <= {1, 2, 3}
        assert counts["kick"] <= {1, 2} and counts["far_tp"] <= {3, 4, 5} and counts["summon"] <= {1, 2, 3}
        assert counts["list"] == counts["weather"] == counts["tickspeed"] == {0}

    def test_far_teleports_leave_the_bot_cells(self, plan: chaos.Plan) -> None:
        homes = {cell(i) for i in range(100)}
        far = {cell(i) for i in range(100, 500)}
        for step in plan.steps:
            if (step.action, step.step) in (
                ("far_tp", "teleport"),
                ("view_disconnect", "teleport"),
                ("stack", "stack"),
            ):
                for command in step.commands:
                    spot = (int(command.params["x"]), int(command.params["z"]))
                    assert spot in far and spot not in homes, command

    def test_undo_steps_go_home(self, plan: chaos.Plan) -> None:
        for step in plan.steps:
            if (step.action, step.step) in (("stack", "home"), ("dimension", "return")):
                for command in step.commands:
                    x, z = cell(command.bot)
                    assert (command.params["x"], command.params["z"]) == (x, z)
                    assert command.params.get("dimension", "overworld") == "overworld"
            if (step.action, step.step) == ("gamemode", "restore"):
                assert {c.params["mode"] for c in step.commands} == {"survival"}
                assert step.restores == chaos.GAMEMODE and plan.steps[step.parent].displaces == chaos.GAMEMODE

    def test_bots_are_not_sent_away_twice(self, plan: chaos.Plan) -> None:
        away_until: dict[int, float] = {}
        returns = {s.parent: s.t_s for s in plan.steps if s.action == "dimension" and s.parent is not None}
        for step in plan.steps:
            if step.action == "dimension" and step.parent is None:
                for bot in step.targets:
                    assert away_until.get(bot, -1.0) <= step.t_s, f"bot {bot} sent away while away"
                    away_until[bot] = returns[step.id]

    def test_cycles(self, plan: chaos.Plan) -> None:
        speeds = [s for s in plan.steps if s.action == "tickspeed"]
        assert [s.expect for s in speeds[:7]] == [0, 1, 3, 100, 1000, 1, 0]
        assert all(s.confirm == "randomtickspeed" and s.commands[0].params == {"value": s.expect} for s in speeds)
        weather = [s.commands[0].params["kind"] for s in plan.steps if s.action == "weather"]
        assert weather[:5] == ["thunder", "clear", "rain", "clear", "thunder"]

    def test_ticking_areas_pair_up(self, plan: chaos.Plan) -> None:
        adds = {s.id: s.commands[0].params["name"] for s in plan.steps if s.step == "add"}
        removes = {s.parent: s.commands[0].params["name"] for s in plan.steps if s.step == "remove"}
        assert adds == removes and len(set(adds.values())) == len(adds)

    def test_follow_ups_past_the_end_happen_at_the_end(self, catalogue: chaos.Catalogue) -> None:
        short = chaos.build_plan(scenario(["stack"], duration=20.0), catalogue)
        assert [s.step for s in short.steps] == ["stack", "home"] and short.steps[-1].t_s == 20.0

    def test_a_firing_without_bots_plans_nothing(self) -> None:
        data = raw_catalogue()
        data["action"]["stack"]["bots_pct"] = [0, 0]
        data["action"]["stack"]["min_bots"] = 0
        assert chaos.build_plan(scenario(["stack", "list"]), parse(data)).actions == ("stack", "list")
        assert {s.action for s in chaos.build_plan(scenario(["stack", "list"]), parse(data)).steps} == {"list"}

    def test_few_bots(self, catalogue: chaos.Catalogue) -> None:
        small = chaos.build_plan(golden_scenario(n=1, duration=120.0), catalogue)
        assert small.steps and all(set(s.targets) <= {0} for s in small.steps)

    def test_round_trip(self, plan: chaos.Plan, tmp_path: Path) -> None:
        path = tmp_path / "chaos-plan.jsonl"
        plan.write(path)
        loaded = chaos.Plan.read(path)
        assert loaded.steps == plan.steps and loaded.sha256 == plan.sha256
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"t_s":', '"t_s":1', 1)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="hash"):
            chaos.Plan.read(path)

    @pytest.mark.parametrize(
        ("actions", "cycle", "message"),
        [
            ([], (), "at least one"),
            (["kick", "kick"], (), "twice"),
            (["kick", "explode"], (), "unknown chaos actions"),
            (["tickspeed"], (), "needs a non-empty cycle"),
            (["kick"], (1, 3), "is not in"),
        ],
    )
    def test_bad_chaos_settings(
        self, catalogue: chaos.Catalogue, actions: list[str], cycle: tuple[int, ...], message: str
    ) -> None:
        with pytest.raises(ConfigError, match=message):
            chaos.build_plan(scenario(actions, cycle=cycle), catalogue)

    def test_no_chaos_phase(self) -> None:
        plain = config.parse_scenario({"server": {"csg": False}}, name="plain")
        with pytest.raises(ConfigError, match="no chaos phase"):
            chaos.build_plan(plain)
        chaos.check_scenario(plain)


# --------------------------------------------------------------------------------------------------------------------
# Shipped scenarios
# --------------------------------------------------------------------------------------------------------------------


class TestShippedScenarios:
    def test_they_plan(self) -> None:
        for name in ("chaos", "chaos-portable"):
            chaos.check_scenario(config.load_scenario(name))

    def test_full_and_portable(self) -> None:
        full, portable = config.load_scenario("chaos"), config.load_scenario("chaos-portable")
        spread = config.load_scenario("spread100")
        full_spec, portable_spec = full.phase("chaos"), portable.phase("chaos")
        assert full_spec is not None and portable_spec is not None
        assert set(full_spec["actions"]) == set(INTERVALS)
        assert set(portable_spec["actions"]) == {
            "churn",
            "stack",
            "gamemode",
            "kill",
            "kick",
            "far_tp",
            "view_disconnect",
            "weather",
        }
        assert full_spec["tickspeed_cycle"] == (0, 1, 3, 100, 1000, 1) and portable_spec["tickspeed_cycle"] == ()
        for s in (full, portable):
            spec = s.phase("chaos")
            assert spec is not None and spec.duration_s == 900.0 and spec["seed"] == 20261010
            assert s.bots.rejoin_on_drop and s.bots.radius_jitter_s == 20.0
            assert s.has_phase("steady") and s.bots.n == spread.bots.n and s.grid == spread.grid
            assert set(spread.exercises) <= set(s.exercises)

    def test_portable_shares_the_full_plan(self) -> None:
        full = chaos.build_plan(config.load_scenario("chaos"))
        portable = chaos.build_plan(config.load_scenario("chaos-portable"))
        actions = set(portable.actions)
        assert [key(s) for s in portable.steps] == [key(s) for s in full.steps if s.action in actions]


# --------------------------------------------------------------------------------------------------------------------
# Adapter checks
# --------------------------------------------------------------------------------------------------------------------


def limited(dialect: ChaosDialect, keep: set[str]) -> ChaosDialect:
    return ChaosDialect(
        renderers={k: v for k, v in dialect.renderers.items() if k in keep},
        unsupported={k: "not in this test" for k in dialect.renderers if k not in keep},
    )


class WeatherAndKickOnly(BdsAdapter):
    def chaos_dialect(self) -> ChaosDialect:
        return limited(super().chaos_dialect(), {"weather", "kick"})


class TestAdapterChecks:
    @pytest.mark.parametrize("server", ["bds", "endstone"])
    def test_bds_family_renders_every_command(self, server: str, catalogue: chaos.Catalogue) -> None:
        dialect = bds_adapter(server).chaos_dialect()
        assert chaos.check_dialect(dialect, catalogue) == []
        assert all(not missing for missing in chaos.supported_actions(dialect, catalogue).values())

    def test_every_step_renders_for_bds(self, plan: chaos.Plan) -> None:
        dialect = bds_adapter().chaos_dialect()
        names = [chaos.bot_name("a", i) for i in range(100)]
        for step in plan.steps:
            for command in step.commands:
                for text in chaos.render(dialect, command, names):
                    assert text == text.strip() and "\n" not in text and text, (step, text)

    def test_check_dialect_reports_problems(self, catalogue: chaos.Catalogue) -> None:
        def broken(player: str) -> str:
            raise ValueError("nope")

        dialect = ChaosDialect(
            renderers={"kill": broken, "list": lambda: "list\nlist", "weather": lambda kind: f"weather {kind}"},
            unsupported={
                "kick": "",
                **{c: "no" for c in catalogue.commands if c not in ("kill", "list", "weather", "tp", "kick")},
            },
        )
        problems = chaos.check_dialect(dialect, catalogue)
        assert any(p.startswith("tp: neither rendered nor marked unsupported") for p in problems)
        assert any(p.startswith("kick: marked unsupported without a reason") for p in problems)
        assert any(p.startswith("kill: the renderer rejects the catalogue example") for p in problems)
        assert any(p.startswith("list: rendered") for p in problems)
        assert not any(p.startswith("weather") for p in problems)

    def test_supported_actions(self, catalogue: chaos.Catalogue) -> None:
        dialect = limited(bds_adapter().chaos_dialect(), {"tp", "kick"})
        missing = chaos.supported_actions(dialect, catalogue)
        assert missing["churn"] == [] and missing["far_tp"] == [] and missing["kick"] == []
        assert missing["tickingarea"] == ["tickingarea_add", "tickingarea_remove"]
        assert missing["kill"] == ["kill"]

    def test_smoke_commands(self, catalogue: chaos.Catalogue) -> None:
        commands = chaos.smoke_commands(bds_adapter())
        assert commands[0] == "list" and commands[-1] == "kick a00 chaos"
        assert len(commands) == len(catalogue.commands)
        assert "a07" not in " ".join(commands) and "tp a00 5760.5 300 1280.5" in commands
        assert commands.index("tickingarea add circle 20000 64 -30000 2 bbench0") < commands.index(
            "tickingarea remove bbench0"
        )

    def test_smoke_commands_skip_what_the_adapter_lacks(self) -> None:
        adapter = WeatherAndKickOnly(config.arm_from_ref("bds@1.26.52.3"), config.default_machine())
        assert chaos.smoke_commands(adapter) == ["weather clear", "kick a00 chaos"]

    def test_bot_name(self) -> None:
        assert chaos.bot_name("a", 7) == "a07" and chaos.bot_name("b", 123) == "b123"
