# SPDX-License-Identifier: Apache-2.0
"""The chaos executor (``bedrock_bench.chaos.Executor`` and ``run_phase``) against a scripted session.

Session, console and bot fleet are in-process fakes on a simulated clock: time only moves while the executor sleeps,
scripted bot events and log lines fire at their times, and the console writes each command at once unless a test holds
it back. The adapter is the real BDS adapter, so commands and applied lines use BDS syntax.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, chaos, config
from bedrock_bench.adapters.base import ChaosDialect
from bedrock_bench.adapters.bds import BdsAdapter
from bedrock_bench.console import parse_line
from bedrock_bench.metrics import chaos as chaos_metrics
from bedrock_bench.model import ProbeSample, Scenario
from bedrock_bench.results import CHAOS_EXEC_FILE, CHAOS_PLAN_FILE

ORIGIN_NS = 7_000_000_000_000  # harness monotonic ns at the run origin (session time 0)
START = 100.0  # session time when the chaos phase starts
LOG_PREFIX = "[2026-10-11 12:00:00:000 INFO] "
CATALOGUE = chaos.load_catalogue()


# --------------------------------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = START

    def ns(self) -> int:
        return ORIGIN_NS + round(self.t * 1e9)


class FakeCommand:
    """The parts of ``console.Command`` the executor reads."""

    def __init__(self, text: str, origin: str) -> None:
        self.text = text
        self.origin = origin
        self.send_ns: int | None = None
        self.error: str | None = None
        self.done = False

    @property
    def sent(self) -> bool:
        return self.done and self.error is None

    def write(self, ns: int) -> None:
        self.send_ns, self.done = ns, True


class FakeConsole:
    def __init__(self, clock: Clock, adapter: BdsAdapter) -> None:
        self.clock = clock
        self.dialect = adapter.dialect
        self.commands: list[FakeCommand] = []
        self.lag = 0.0
        self.hold = False  # True: commands stay queued
        self._lines: list[Any] = []

    def send(self, text: str, *, lane: str = "normal", origin: str = "phase", on_sent: Any = None) -> FakeCommand:
        command = FakeCommand(text, origin)
        if not self.hold:
            command.write(self.clock.ns())
        self.commands.append(command)
        return command

    def writer_lag_s(self) -> float:
        return self.lag

    def mark(self) -> int:
        return len(self._lines)

    def lines(self, start: int = 0, end: int | None = None) -> list[Any]:
        return self._lines[start:end]

    def say(self, text: str) -> None:
        self._lines.append(parse_line(LOG_PREFIX + text, self.clock.ns(), 0, self.dialect))

    def texts(self) -> list[str]:
        return [command.text for command in self.commands]


class FakeFleet:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.controls: list[tuple[str, dict[str, Any]]] = []
        self._events: list[dict[str, Any]] = []

    def emit(self, ev: str, bot: int, **fields: Any) -> None:
        self._events.append({"v": 1, "ev": ev, "bot": chaos.bot_name("a", bot), **fields, "rx_ns": self.clock.ns()})

    def events(self, match: Any = None, since: int = 0) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events[since:]]

    def control(self, op: str, **params: Any) -> bool:
        self.controls.append((op, params))
        return True


class FakeRunDir:
    def __init__(self, path: Path) -> None:
        self.path = path

    def file(self, relative: str) -> Path:
        return self.path / relative


class Died(Exception):
    """Stands in for phases.ServerDied."""


class FakeSession:
    def __init__(self, tmp_path: Path, scenario: Scenario, adapter: BdsAdapter | None = None) -> None:
        self.clock = Clock()
        self.adapter = adapter or bds()
        self.scenario = scenario
        self.console = FakeConsole(self.clock, self.adapter)
        self.fleet = FakeFleet(self.clock)
        self.run_dir = FakeRunDir(tmp_path)
        self.death_at: float | None = None
        self._script: list[tuple[float, int, Callable[[], None]]] = []
        for i in range(scenario.bots.n):
            self.fleet.emit("spawned", i, i=i, session=1, join_ms=900, radius=10, dim=0)

    def at(self, t: float, action: Callable[[], None]) -> None:
        """Run ``action`` ``t`` seconds after the chaos phase starts."""
        heapq.heappush(self._script, (START + t, len(self._script), action))

    def now(self) -> float:
        return self.clock.t

    def sleep_until(self, t: float) -> None:
        while self._script and self._script[0][0] <= t:
            when, _, action = heapq.heappop(self._script)
            self.clock.t = max(self.clock.t, when)
            self._check()
            action()
        self.clock.t = max(self.clock.t, t)
        self._check()

    def _check(self) -> None:
        if self.death_at is not None and self.clock.t >= START + self.death_at:
            raise Died("the server exited")


def bds() -> BdsAdapter:
    adapter = adapters.create(config.arm_from_ref("bds@1.26.52.3"), config.default_machine())
    assert isinstance(adapter, BdsAdapter)
    return adapter


def without(*names: str) -> BdsAdapter:
    """The BDS adapter with some chaos commands marked unsupported."""

    class Limited(BdsAdapter):
        def chaos_dialect(self) -> ChaosDialect:
            full = super().chaos_dialect()
            return ChaosDialect(
                renderers={k: v for k, v in full.renderers.items() if k not in names},
                unsupported=dict.fromkeys(names, "not in this test"),
            )

    return Limited(config.arm_from_ref("bds@1.26.52.3"), config.default_machine())


def chaos_scenario(
    *, n: int = 4, jitter: float = 0.0, actions: Iterable[str] = ("kick",), duration: float = 30.0
) -> Scenario:
    return config.parse_scenario(
        {
            "server": {"csg": False, "view_distance": 4, "max_players": 20},
            "bots": {"n": n, "prefix": "a", "radius_jitter_s": jitter, "rejoin_on_drop": True},
            "phases": {"chaos": {"seed": 3, "duration_s": duration, "actions": list(actions)}},
        },
        name="chaos-exec",
    )


def cmd(command: str, bot: int | None = None, **params: Any) -> chaos.StepCommand:
    return chaos.StepCommand(command, params, bot)


def step(id: int, t: float, action: str, name: str, *commands: chaos.StepCommand, **fields: Any) -> chaos.Step:
    return chaos.Step(id=id, t_s=t, action=action, step=name, commands=commands, **fields)


def execute(session: FakeSession, steps: list[chaos.Step], *, duration: float, **options: Any) -> list[dict[str, Any]]:
    header = {
        "plan": chaos.PLAN_FORMAT,
        "sha256": chaos.plan_sha256(steps),
        "steps": len(steps),
        "duration_s": duration,
        "actions": sorted({s.action for s in steps}),
    }
    path = session.run_dir.file(CHAOS_EXEC_FILE)
    executor = chaos.Executor(
        session, chaos.Plan(tuple(steps), header), CATALOGUE, journal_path=path, clock=session.clock.ns, **options
    )
    executor.run()
    return read_journal(path)


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def of(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Records of one kind in the order they were written (steps close in completion order, not id order)."""
    return [r for r in records if r["kind"] == kind]


def steps_of(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Step records by id."""
    return sorted(of(records, "step"), key=lambda r: r["id"])


def at_ns(t: float) -> int:
    """Harness ns at ``t`` seconds into the chaos phase."""
    return ORIGIN_NS + round((START + t) * 1e9)


@pytest.fixture
def session(tmp_path: Path) -> FakeSession:
    return FakeSession(tmp_path, chaos_scenario())


# --------------------------------------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------------------------------------


def test_steps_are_sent_on_time(session: FakeSession) -> None:
    records = execute(
        session,
        [
            step(0, 1.0, "list", "list", cmd("list")),
            step(1, 2.5, "weather", "set", cmd("weather", kind="rain")),
            step(2, 4.0, "kick", "kick", cmd("kick", 1, reason="chaos"), cmd("kick", 3, reason="chaos")),
        ],
        duration=6.0,
    )
    assert session.console.texts() == ["list", "weather rain", "kick a01 chaos", "kick a03 chaos"]
    assert {c.origin for c in session.console.commands} == {"chaos"}
    assert records[0]["kind"] == "start" and records[0]["t0_ns"] == at_ns(0) and records[0]["online"] == 4
    assert records[-1]["kind"] == "end" and records[-1]["dispatched"] == records[-1]["steps"] == 3
    steps = steps_of(records)
    assert [r["status"] for r in steps] == ["sent"] * 3
    for record, t in zip(steps, (1.0, 2.5, 4.0), strict=True):
        assert record["scheduled_ns"] == record["sent_ns"] == at_ns(t)
        assert record["failed"] == record["queued"] == 0 and record["applied"] is None
    assert steps[2]["targets"] == [1, 3] and steps[2]["offline"] == []
    assert steps[2]["texts"] == ["kick a01 chaos", "kick a03 chaos"]


def test_unsupported_actions_are_counted_and_never_sent(tmp_path: Path) -> None:
    session = FakeSession(tmp_path, chaos_scenario(), without("weather"))
    records = execute(
        session,
        [step(0, 1.0, "weather", "set", cmd("weather", kind="rain")), step(1, 2.0, "list", "list", cmd("list"))],
        duration=3.0,
    )
    assert session.console.texts() == ["list"]
    assert [(r["action"], r["status"]) for r in steps_of(records)] == [("weather", "unsupported"), ("list", "sent")]
    assert steps_of(records)[0]["missing"] == ["weather"]
    assert records[0]["unsupported"] == {"weather": ["weather"]}


def test_optional_commands_the_adapter_lacks_are_left_out(tmp_path: Path) -> None:
    session = FakeSession(tmp_path, chaos_scenario(), without("effect_clear"))
    records = execute(session, [step(0, 1.0, "kill", "kill", cmd("effect_clear", 1), cmd("kill", 1))], duration=2.0)
    assert session.console.texts() == ["kill a01"]
    assert steps_of(records)[0]["status"] == "sent"


def test_offline_bots_are_skipped(session: FakeSession) -> None:
    session.at(0.5, lambda: session.fleet.emit("left", 2))
    records = execute(
        session,
        [
            step(0, 1.0, "kick", "kick", cmd("kick", 1, reason="chaos"), cmd("kick", 2, reason="chaos")),
            step(1, 2.0, "kick", "kick", cmd("kick", 2, reason="chaos")),
        ],
        duration=3.0,
    )
    first, second = steps_of(records)
    assert first["status"] == "sent" and first["texts"] == ["kick a01 chaos"] and first["offline"] == [2]
    assert second["status"] == "skip_offline" and second["targets"] == [2] and second["sent_ns"] is None
    assert session.console.texts() == ["kick a01 chaos"]


def test_follow_ups_only_address_bots_the_first_step_reached(session: FakeSession) -> None:
    for bot in (2, 3):
        session.at(0.5, lambda bot=bot: session.fleet.emit("left", bot))
        session.at(
            2.0, lambda bot=bot: session.fleet.emit("spawned", bot, i=bot, session=2, join_ms=1, radius=4, dim=0)
        )
    spot = {"x": 7680.5, "y": 300.0, "z": 1920.5}
    records = execute(
        session,
        [
            step(0, 1.0, "stack", "stack", cmd("tp", 1, **spot), cmd("tp", 3, **spot)),
            step(1, 1.5, "stack", "stack", cmd("tp", 2, **spot)),
            step(
                2,
                3.0,
                "stack",
                "home",
                cmd("tp", 1, x=1920, y=300.0, z=1280),
                cmd("tp", 3, x=3200, y=300.0, z=1280),
                parent=0,
            ),
            step(3, 3.5, "stack", "home", cmd("tp", 2, x=2560, y=300.0, z=1280), parent=1),
        ],
        duration=4.0,
    )
    assert [r["status"] for r in steps_of(records)] == ["sent", "skip_offline", "sent", "skip_offline"]
    home = steps_of(records)[2]
    assert home["targets"] == [1] and home["texts"] == ["tp a01 1920 300 1280"]
    assert steps_of(records)[3]["targets"] == []


def test_overrun_stops_every_later_step(session: FakeSession) -> None:
    session.at(1.5, lambda: setattr(session.console, "lag", 61.0))
    session.at(1.6, lambda: session.fleet.emit("respawned", 1))
    records = execute(
        session,
        [step(i, float(i + 1), "list", "list", cmd("list")) for i in range(3)],
        duration=6.0,
        overrun_lag_s=60.0,
    )
    assert session.console.texts() == ["list"]
    assert [r["status"] for r in steps_of(records)] == ["sent", "overrun", "overrun"]
    assert records[-1]["overrun_ns"] == at_ns(2.0) and records[-1]["statuses"] == {"overrun": 2, "sent": 1}
    assert of(records, "restore") == []


def test_reconnect_operations_go_to_the_bot(session: FakeSession) -> None:
    session.at(1.1, lambda: session.fleet.emit("left", 1, session=1))
    session.at(3.0, lambda: session.fleet.emit("rejoin", 1, session=2))
    session.at(3.6, lambda: session.fleet.emit("spawned", 1, i=1, session=2, join_ms=600, radius=4, dim=0))
    records = execute(
        session,
        [
            chaos.Step(0, 1.0, "churn", "reconnect", ops=(chaos.BotOp("reconnect", 1, 2000),)),
            step(1, 2.0, "kick", "kick", cmd("kick", 1, reason="chaos")),
            step(2, 4.0, "kick", "kick", cmd("kick", 1, reason="chaos")),
        ],
        duration=5.0,
    )
    assert session.fleet.controls == [("reconnect", {"bot": "a01", "delay_ms": 2000})]
    churn, away, back = steps_of(records)
    assert churn["status"] == "sent" and churn["ops"] == [{"op": "reconnect", "bot": "a01", "delay_ms": 2000}]
    assert churn["sent_ns"] == at_ns(1.0)
    assert away["status"] == "skip_offline" and back["status"] == "sent", "online state follows the bot's events"


def test_an_ignored_reconnect_leaves_the_bot_online(session: FakeSession) -> None:
    records = execute(
        session,
        [
            chaos.Step(0, 1.0, "churn", "reconnect", ops=(chaos.BotOp("reconnect", 1, 2000),)),
            step(1, 2.0, "kick", "kick", cmd("kick", 1, reason="chaos")),
        ],
        duration=3.0,
    )
    assert steps_of(records)[1]["status"] == "sent", "no left event, so the bot never went offline"


# --------------------------------------------------------------------------------------------------------------------
# Applied lines
# --------------------------------------------------------------------------------------------------------------------


def tickspeed(id: int, t: float, value: int) -> chaos.Step:
    return step(id, t, "tickspeed", "set", cmd("randomtickspeed", value=value), confirm="randomtickspeed", expect=value)


def test_tickspeed_is_confirmed_by_its_applied_line(session: FakeSession) -> None:
    session.at(1.2, lambda: session.console.say("Game rule randomtickspeed has been updated to 3"))
    session.at(1.5, lambda: session.console.say("Game rule randomtickspeed has been updated to 100"))
    records = execute(session, [tickspeed(0, 1.0, 100)], duration=3.0)
    (record,) = steps_of(records)
    assert record["applied"] == {"tickspeed": 100} and record["applied_ns"] == at_ns(1.5)
    assert record["texts"] == ["gamerule randomtickspeed 100"]
    parsed = chaos_metrics.records(steps_of(records), ORIGIN_NS)
    assert chaos_metrics.regime_changes(parsed) == [(START + 1.5, 100)]


def test_a_line_before_the_command_was_written_confirms_nothing(session: FakeSession) -> None:
    session.at(0.5, lambda: session.console.say("Game rule randomtickspeed has been updated to 100"))
    records = execute(session, [tickspeed(0, 1.0, 100)], duration=3.0)
    assert steps_of(records)[0]["applied_ns"] is None


def test_an_unconfirmed_step_closes_after_the_timeout(session: FakeSession) -> None:
    records = execute(session, [tickspeed(0, 1.0, 100), step(1, 40.0, "list", "list", cmd("list"))], duration=41.0)
    steps = steps_of(records)
    assert [r["id"] for r in steps] == [0, 1], "the tick speed record is written once its wait is over"
    assert steps[0]["applied"] is None and steps[0]["applied_ns"] is None


# --------------------------------------------------------------------------------------------------------------------
# Keeping bots usable
# --------------------------------------------------------------------------------------------------------------------


def test_a_respawned_bot_gets_its_effects_and_goes_home(session: FakeSession) -> None:
    session.at(1.5, lambda: session.fleet.emit("died", 1))
    session.at(2.0, lambda: session.fleet.emit("respawned", 1))
    records = execute(session, [step(0, 1.0, "kill", "kill", cmd("effect_clear", 1), cmd("kill", 1))], duration=6.0)
    effects = session.adapter.cmd_effects("a01")
    assert session.console.texts() == ["effect a01 clear", "kill a01", *effects, "tp a01 1920 300 1280"]
    (restore,) = of(records, "restore")
    assert restore["cause"] == "respawned" and restore["bot"] == "a01" and "action" not in restore
    assert restore["sent_ns"] >= at_ns(2.0 + chaos.RESTORE_DELAY_S)
    assert records[-1]["restores"] == 1


def test_a_rejoined_bot_is_moved_back(session: FakeSession) -> None:
    def rejoin(bot: int, line_first: bool) -> None:
        session.at(4.0, lambda: session.fleet.emit("rejoin", bot, session=2))
        spawned = (4.5, lambda: session.fleet.emit("spawned", bot, i=bot, session=2, join_ms=1, radius=4, dim=0))
        line = (4.4 if line_first else 5.0, lambda: session.console.say(f"Player Spawned: a0{bot} xuid: {bot}"))
        for t, action in (spawned, line):
            session.at(t, action)

    for bot in (1, 2):
        session.at(1.5, lambda bot=bot: session.fleet.emit("kicked", bot))
    rejoin(1, line_first=False)
    rejoin(2, line_first=True)
    nether = {"dimension": "nether", "x": 320, "y": 100, "z": 160}
    records = execute(
        session,
        [
            step(0, 1.0, "gamemode", "set", cmd("gamemode", 1, mode="spectator"), displaces=chaos.GAMEMODE),
            step(1, 1.0, "dimension", "leave", cmd("tp_dimension", 2, **nether), displaces=chaos.DIMENSION),
            step(2, 3.0, "gamemode", "restore", cmd("gamemode", 1, mode="survival"), parent=0, restores=chaos.GAMEMODE),
            step(
                3,
                3.0,
                "dimension",
                "return",
                cmd("tp_dimension", 2, dimension="overworld", x=2560, y=300.0, z=1280),
                parent=1,
                restores=chaos.DIMENSION,
            ),
        ],
        duration=9.0,
    )
    assert [r["status"] for r in steps_of(records)] == ["sent", "sent", "skip_offline", "skip_offline"]
    restores = {r["bot"]: r for r in of(records, "restore")}
    assert restores["a01"]["texts"] == ["gamemode survival a01"] and restores["a01"]["undo"] == ["gamemode"]
    assert restores["a02"]["texts"] == ["execute in overworld run tp a02 2560 300 1280"]
    assert restores["a02"]["undo"] == ["dimension"] and restores["a02"]["cause"] == "rejoined"


def test_an_undone_bot_is_not_moved_again_on_rejoin(session: FakeSession) -> None:
    session.at(3.0, lambda: session.fleet.emit("kicked", 1))
    session.at(4.0, lambda: session.fleet.emit("spawned", 1, i=1, session=2, join_ms=1, radius=4, dim=0))
    session.at(4.2, lambda: session.console.say("Player Spawned: a01 xuid: 1"))
    records = execute(
        session,
        [
            step(0, 1.0, "gamemode", "set", cmd("gamemode", 1, mode="creative"), displaces=chaos.GAMEMODE),
            step(1, 2.0, "gamemode", "restore", cmd("gamemode", 1, mode="survival"), parent=0, restores=chaos.GAMEMODE),
        ],
        duration=8.0,
    )
    assert of(records, "restore") == []


# --------------------------------------------------------------------------------------------------------------------
# Phase lifecycle
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("jitter", [20.0, 0.0])
def test_radius_jitter_runs_during_the_phase_only(tmp_path: Path, jitter: float) -> None:
    session = FakeSession(tmp_path, chaos_scenario(jitter=jitter))
    records = execute(session, [step(0, 1.0, "list", "list", cmd("list"))], duration=2.0)
    expected = [("jitter", {"on": True}), ("jitter", {"on": False})] if jitter else []
    assert session.fleet.controls == expected
    assert records[0]["jitter"] is bool(jitter)


def test_a_server_death_ends_the_phase_with_a_closed_journal(tmp_path: Path) -> None:
    session = FakeSession(tmp_path, chaos_scenario(jitter=20.0))
    session.death_at = 2.5
    with pytest.raises(Died):
        execute(session, [step(i, float(i + 1), "list", "list", cmd("list")) for i in range(4)], duration=6.0)
    records = read_journal(session.run_dir.file(CHAOS_EXEC_FILE))
    assert [r["id"] for r in of(records, "step")] == [0, 1]
    assert records[-1]["kind"] == "end" and records[-1]["dispatched"] == 2 and records[-1]["steps"] == 4
    assert session.fleet.controls[-1] == ("jitter", {"on": False})


def test_a_renderer_bug_stops_the_phase_before_anything_is_sent(tmp_path: Path) -> None:
    session = FakeSession(tmp_path, chaos_scenario())
    with pytest.raises(chaos.ChaosError, match=r"step 0 \(weather/set\)"):
        execute(session, [step(0, 1.0, "weather", "set", cmd("weather", kind="snow"))], duration=2.0)
    assert session.console.commands == [] and not session.run_dir.file(CHAOS_EXEC_FILE).exists()


def test_commands_still_queued_at_the_end_get_a_grace(session: FakeSession) -> None:
    session.console.hold = True
    records = execute(session, [step(0, 1.0, "list", "list", cmd("list"))], duration=2.0)
    (record,) = steps_of(records)
    assert record["status"] == "sent" and record["sent_ns"] is None and record["queued"] == 1
    assert records[-1]["t_ns"] >= at_ns(2.0 + chaos.FINISH_GRACE_S)


def test_run_phase_writes_the_plan_and_a_record_per_step(tmp_path: Path) -> None:
    scenario = chaos_scenario(actions=("kick", "list", "weather", "stack", "churn"), duration=60.0)
    session = FakeSession(tmp_path, scenario)
    spec = scenario.phase("chaos")
    assert spec is not None
    chaos.run_phase(session, spec)
    plan = chaos.Plan.read(session.run_dir.file(CHAOS_PLAN_FILE))
    assert plan.sha256 == chaos.build_plan(scenario).sha256
    raw = read_journal(session.run_dir.file(CHAOS_EXEC_FILE))
    assert raw[0]["plan_sha256"] == plan.sha256
    assert sorted(r["id"] for r in of(raw, "step")) == [s.id for s in plan.steps]
    totals, per_action = chaos_metrics.actions(chaos_metrics.records(raw, ORIGIN_NS))
    assert totals.planned == len(plan.steps) and totals.unsupported == totals.overrun == 0
    assert set(per_action) == {"kick", "list", "weather", "stack", "churn"}


def test_bds_applied_lines_drive_the_regime_split(session: FakeSession) -> None:
    """Tick speed regimes come from BDS's applied lines, and probe samples that straddle a change are left out."""
    changes = [(10.0, 100), (40.0, 1000), (70.0, 1)]
    for t, value in changes:
        session.at(
            t + 0.3, lambda value=value: session.console.say(f"Game rule randomtickspeed has been updated to {value}")
        )
    records = execute(session, [tickspeed(i, t, v) for i, (t, v) in enumerate(changes)], duration=100.0)
    applied = chaos_metrics.regime_changes(chaos_metrics.records(steps_of(records), ORIGIN_NS))

    assert applied == pytest.approx([(START + t + 0.3, v) for t, v in changes])

    rate = {1: 20.0, 100: 19.0, 1000: 12.0}
    probe: list[ProbeSample] = []
    tick = 0.0
    for k in range(21):  # a reply every 5 s; probe times are ns since the run origin
        t = START + 5.0 * k
        probe.append(ProbeSample(round((t - 0.01) * 1e9), round(t * 1e9), round(tick)))
        tick += 5.0 * rate[[v for when, v in [(START, 1), *applied] if when <= t][-1]]
    split = chaos_metrics.regimes(probe, applied, START, START + 100.0)
    assert split.mixed == 3 and split.applied == 3
    assert {regime: set(values) for regime, values in split.per_regime.items()} == {
        1: {20.0},
        100: {19.0},
        1000: {12.0},
    }
    assert split.seconds == pytest.approx({1: 40.0, 100: 30.0, 1000: 30.0})
