# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import dataclasses
import re
from typing import Any

import pytest

from bedrock_bench.metrics import compute, health
from bedrock_bench.metrics.health import GATES, ServerTraits, evaluate, failed_gates, gates_of
from bedrock_bench.model import Gate, LogLine, RunData, Status
from bedrock_bench.workload import from_record
from fixtures.runs import synthetic


@pytest.fixture(scope="module")
def steady() -> tuple[RunData, dict[str, Any]]:
    run = synthetic.build("synthetic-steady")
    return run, compute(run)


def check(
    steady: tuple[RunData, dict[str, Any]],
    metrics: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
    traits: ServerTraits | None = None,
    **set_values: Any,
) -> tuple[Status, list[str], list[str]]:
    data, doc = steady
    meta = copy.deepcopy(run if run is not None else data.run)
    values = copy.deepcopy(metrics if metrics is not None else doc["metrics"])
    for metric_id, value in set_values.items():
        values[metric_id] = {**values[metric_id], "value": value, "availability": "measured"}
    return evaluate(meta, values, from_record(meta), traits or ServerTraits())


def test_the_steady_fixture_passes_every_gate(steady: tuple[RunData, dict[str, Any]]) -> None:
    assert check(steady) == (Status.OK, [], [])


def test_every_gate_is_documented() -> None:
    assert [g.gate for g in GATES] == list(Gate)


@pytest.mark.parametrize(
    ("values", "gate"),
    [
        ({"probe_tick_decreases": 3}, Gate.PROBE_MONOTONIC),
        ({"probe_idle_ticks": 0}, Gate.PROBE_MONOTONIC),
        ({"probe_max_gap_s_steady": 20.0}, Gate.PROBE_COVERAGE),
        ({"probe_latency_ms_steady": {"p50": 10.0, "p99": 1500.0, "max": 1600.0, "n": 18}}, Gate.PROBE_LAGGING),
        ({"foreign_cores_steady": 0.5}, Gate.FOREIGN_LOAD),
        ({"foreign_cores_peak_10s_load": 2.0}, Gate.FOREIGN_LOAD),
        ({"foreign_server_set_cores_steady": 0.3}, Gate.FOREIGN_LOAD),
        ({"decode_errors": 2}, Gate.DECODE_ERRORS),
        ({"bot_cpu_frac_load": 0.9}, Gate.BOT_SATURATED),
        ({"interventions_in_measured": 1}, Gate.INTERVENTIONS_IN_MEASURED),
    ],
)
def test_invalid_gates_from_metrics(steady: tuple[RunData, dict[str, Any]], values: dict[str, Any], gate: Gate) -> None:
    status, reasons, _ = check(steady, **values)
    assert status is Status.INVALID
    assert failed_gates(reasons) == [gate.value]


def test_unmeasured_foreign_load_is_invalid(steady: tuple[RunData, dict[str, Any]]) -> None:
    metrics = copy.deepcopy(steady[1]["metrics"])
    metrics["foreign_cores_steady"] = {"value": None, "availability": "unavailable", "reason": "x"}
    status, reasons, _ = check(steady, metrics=metrics)
    assert status is Status.INVALID
    assert reasons == ["foreign_load: foreign load not measured in steady"]


def test_machine_profile_gates_come_from_run_json(steady: tuple[RunData, dict[str, Any]]) -> None:
    run = copy.deepcopy(steady[0].run)
    run["machine"]["gates"] = {"foreign_cores_mean": 1.0, "unknown_gate": 9}
    assert gates_of(run).foreign_cores_mean == 1.0
    assert check(steady, run=run, foreign_cores_steady=0.5)[0] is Status.OK


def test_traits_gates(steady: tuple[RunData, dict[str, Any]]) -> None:
    status, reasons, _ = check(steady, traits=ServerTraits(tick_monotonic=False))
    assert status is Status.INVALID and failed_gates(reasons) == ["probe_monotonic"]
    csg = ServerTraits(supports_csg=True)
    assert check(steady, traits=csg)[0] is Status.OK
    status, reasons, _ = check(steady, traits=csg, csg_effective=True)
    assert failed_gates(reasons) == ["csg_matches"]
    assert check(steady, csg_effective=True)[0] is Status.OK  # server without CSG support: not checked


def edit(steady: tuple[RunData, dict[str, Any]], path: str, value: Any) -> dict[str, Any]:
    run = copy.deepcopy(steady[0].run)
    *parents, key = path.split(".")
    target = run
    for part in parents:
        target = target[part]
    target[key] = value
    return run


@pytest.mark.parametrize(
    ("path", "value", "gate", "status"),
    [
        ("placement.verified", False, Gate.PLACEMENT_MATCH, Status.INVALID),
        ("spread.cells_occupied", 5, Gate.SPREAD, Status.INVALID),
        ("spread.max_per_cell", 2, Gate.SPREAD, Status.INVALID),
        ("spread.source", None, Gate.SPREAD, Status.INVALID),
        ("server.banner_version", "1.1", Gate.VERSION_MATCH, Status.INVALID),
        ("world.fresh", False, Gate.WORLD_FRESH, Status.INVALID),
        ("partial", True, Gate.HARNESS_OK, Status.INVALID),
    ],
)
def test_gates_from_run_json(
    steady: tuple[RunData, dict[str, Any]], path: str, value: Any, gate: Gate, status: Status
) -> None:
    result, reasons, _ = check(steady, run=edit(steady, path, value))
    assert result is status
    assert failed_gates(reasons) == [gate.value]


def test_outcome_gates(steady: tuple[RunData, dict[str, Any]]) -> None:
    status, reasons, _ = check(steady, died_during_run=True)
    assert status is Status.SERVER_FAILED
    assert reasons == ["server_alive: the server exited before teardown"]
    assert check(steady, server_exit_code=1)[1] == ["server_alive: exit code 1"]
    run = edit(steady, "spread.cells_occupied", 5)
    run["spread"]["players"] = 5
    status, reasons, _ = check(steady, run=run, joined=5)
    assert status is Status.SERVER_FAILED
    assert reasons == ["joined_all: joined 5/6"]
    # the spread is checked against the bots that joined
    assert failed_gates(check(steady, joined=5)[1]) == ["spread", "joined_all"]


def test_invalid_beats_server_failed_and_reasons_follow_gate_order(steady: tuple[RunData, dict[str, Any]]) -> None:
    status, reasons, _ = check(steady, died_during_run=True, decode_errors=1)
    assert status is Status.INVALID
    assert failed_gates(reasons) == ["decode_errors", "server_alive"]


def test_dropped_bots_are_a_flag_not_a_status(steady: tuple[RunData, dict[str, Any]]) -> None:
    assert check(steady, dropped=2) == (Status.OK, [], ["dropped"])


def test_recorded_reasons_survive_and_are_not_repeated(steady: tuple[RunData, dict[str, Any]]) -> None:
    run = edit(
        steady,
        "status_reasons",
        [
            "harness_ok: sampler thread raised OSError",
            "config_readback: view-distance read back as 12",
            "spread: stale reason from an older analysis",
        ],
    )
    status, reasons, _ = check(steady, run=run)
    assert status is Status.INVALID
    assert reasons == ["harness_ok: sampler thread raised OSError", "config_readback: view-distance read back as 12"]
    run["status_reasons"] = reasons
    assert check(steady, run=run)[1] == reasons


def test_flags(steady: tuple[RunData, dict[str, Any]]) -> None:
    run = edit(steady, "settle", {"settle_s": 900.0, "capped": True, "views_incomplete": True})
    run["flags"] = ["unpublished_artifact"]
    _, _, flags = check(steady, run=run)
    assert flags == ["settle_capped", "unpublished_artifact", "views_incomplete"]


def test_censored_view_median_sets_views_incomplete(steady: tuple[RunData, dict[str, Any]]) -> None:
    metrics = copy.deepcopy(steady[1]["metrics"])
    metrics["view_t_n_s"] = {"value": None, "availability": "unavailable", "reason": "median censored: 4 of 6 bots"}
    assert check(steady, metrics=metrics)[2] == ["views_incomplete"]


def test_missing_raw_series_is_a_harness_fault(steady: tuple[RunData, dict[str, Any]]) -> None:
    run = dataclasses.replace(steady[0], process=[])
    doc = compute(run)
    assert doc["status"] == "invalid"
    assert "harness_ok: the process sampler produced no samples" in doc["status_reasons"]


def test_a_death_skips_the_gates_of_windows_never_reached() -> None:
    doc = compute(synthetic.build("synthetic-death"))
    assert doc["status"] == "server_failed"
    assert failed_gates(doc["status_reasons"]) == ["server_alive"]


class FakeDialect:
    tick_monotonic = False
    benign = (re.compile(r"TRANSPORT"),)


class FakeAdapter:
    dialect = FakeDialect()
    supports_csg = True
    tick_thread_rule = "name:main"
    thread_groups = {"pool": "^Pool"}


def test_server_traits() -> None:
    traits = ServerTraits.of(FakeAdapter)
    assert traits == ServerTraits(False, True, "name:main", {"pool": "^Pool"}, (re.compile("TRANSPORT"),))
    assert traits.is_benign("TRANSPORT TYPE ERROR") and not traits.is_benign("boom")
    assert ServerTraits.of(traits) is traits
    assert ServerTraits.of(None) == ServerTraits()
    assert ServerTraits.lookup("no_such_server") == ServerTraits()
    assert ServerTraits.lookup(None) == ServerTraits()


def test_log_counts_follow_levels_and_the_benign_list(steady: tuple[RunData, dict[str, Any]]) -> None:
    def line(t: float, level: str | None, text: str) -> LogLine:
        return LogLine(synthetic.ORIGIN_NS + round(t * 1e9), 0, level, None, text, text)

    log = [
        line(1, "info", "starting"),
        line(2, "warn", "slow"),
        line(3, "error", "TRANSPORT banner"),
        line(4, "error", "real problem 42"),
        line(5, "fatal", "gone"),
    ]
    run = dataclasses.replace(steady[0], log=log)
    doc = compute(run, adapter=ServerTraits(benign=(re.compile("TRANSPORT"),)))
    counts = {k: doc["metrics"][f"log_{k}_lines"]["value"] for k in ("warn", "error", "fatal")}
    assert counts == {"warn": 1, "error": 1, "fatal": 1}
    assert doc["tables"]["log_errors_first"]["rows"] == [[4.0, "error", "real problem 42"], [5.0, "fatal", "gone"]]


def test_module_exports() -> None:
    assert health.RECORDED_GATES == (Gate.HARNESS_OK, Gate.CONFIG_READBACK, Gate.SERVER_ALIVE)
