# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bedrock_bench import workload
from bedrock_bench.workload import PHASES, SMOKE, WORKLOAD, Workload


def test_the_benchmark_workload() -> None:
    w = WORKLOAD
    assert (w.bots.n, w.bots.radius, w.bots.walk) == (100, 16, "circle")
    assert (w.grid.columns, w.grid.spacing, w.grid.origin, w.grid.y) == (11, 640, 1280, 300.0)
    assert (w.world.seed, w.world.level_name) == (20261010, "bench")
    assert w.server.csg is False
    assert (w.server.view_distance, w.server.tick_distance) == (10, 4)
    assert (w.server.difficulty, w.server.gamemode) == ("easy", "survival")
    assert (w.phase("idle").delay_s, w.phase("idle").duration_s) == (10.0, 20.0)
    assert w.phase("join").cap_s == 300.0
    assert (w.phase("settle")["min_s"], w.phase("settle").cap_s) == (300.0, 900.0)
    assert (w.phase("steady").delay_s, w.phase("steady").duration_s) == (15.0, 90.0)


def test_smoke_is_short() -> None:
    assert SMOKE.bots.n == 4 and SMOKE.server.view_distance == 4 and SMOKE.server.csg is False
    assert SMOKE.phase("steady").duration_s < WORKLOAD.phase("steady").duration_s
    assert SMOKE.sha256 != WORKLOAD.sha256


@pytest.mark.parametrize("w", [WORKLOAD, SMOKE], ids=lambda w: w.name)
def test_builtins_are_consistent(w: Workload) -> None:
    assert tuple(p.name for p in w.phases) == PHASES
    assert w.bots.n <= w.server.max_players
    assert w.grid.spacing > 2 * 16 * w.server.view_distance, "neighbouring bots would share chunks"
    assert w.phase("settle")["min_s"] <= w.phase("settle").cap_s
    assert workload.is_builtin(w)


def test_record_round_trip() -> None:
    record = json.loads(json.dumps(WORKLOAD.record()))
    assert record["name"] == "benchmark" and record["sha256"] == WORKLOAD.sha256
    assert "name" not in record["params"]
    rebuilt = workload.from_record({"workload": record})
    assert rebuilt == WORKLOAD and rebuilt.sha256 == WORKLOAD.sha256


def test_a_record_without_a_workload_reads_as_the_benchmark() -> None:
    assert workload.from_record({}) is WORKLOAD


def test_overrides() -> None:
    w = workload.with_overrides(WORKLOAD, {"bots": {"n": 4}, "phases": {"settle": {"min_s": 1.0, "cap_s": 6.0}}})
    assert w.bots.n == 4 and w.bots.radius == 16
    assert (w.phase("settle")["min_s"], w.phase("settle").cap_s) == (1.0, 6.0)
    assert w.phase("settle")["rate_per_bot"] == WORKLOAD.phase("settle")["rate_per_bot"]
    assert w.sha256 != WORKLOAD.sha256 and not workload.is_builtin(w)
    with pytest.raises(ValueError, match="no 'minimum'"):
        workload.with_overrides(WORKLOAD, {"phases": {"settle": {"minimum": 1.0}}})
    with pytest.raises(TypeError):
        workload.with_overrides(WORKLOAD, {"bots": {"count": 4}})


def test_for_run_reads_the_test_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(workload.ENV_TEST_WORKLOAD, raising=False)
    assert workload.for_run(SMOKE) is SMOKE
    path = tmp_path / "workload.json"
    path.write_text(json.dumps({"bots": {"n": 2}}), encoding="utf-8")
    monkeypatch.setenv(workload.ENV_TEST_WORKLOAD, str(path))
    w = workload.for_run(SMOKE)
    assert w.name == "smoke" and w.bots.n == 2 and w.server.view_distance == 4
