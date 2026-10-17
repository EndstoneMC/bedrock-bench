# SPDX-License-Identifier: Apache-2.0
"""Golden recompute: the stored fixture runs give exactly their stored metrics.json under the current analysis version.

A failure here means a formula changed. If that was intended, bump ``ANALYSIS_VERSION`` and regenerate the fixtures
with ``uv run python tests/fixtures/runs/synthetic.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bedrock_bench import ANALYSIS_VERSION
from bedrock_bench.metrics import compute, dumps
from bedrock_bench.results import load_run
from fixtures.runs import synthetic

FIXTURES = synthetic.HERE


def stored(name: str) -> str:
    return (FIXTURES / name / "metrics.json").read_text(encoding="utf-8")


def stored_doc(name: str) -> dict[str, Any]:
    return json.loads(stored(name))


@pytest.mark.parametrize("name", synthetic.NAMES)
def test_formulas_reproduce_the_golden_metrics(name: str) -> None:
    assert dumps(compute(synthetic.build(name))) == stored(name)


@pytest.mark.parametrize("name", synthetic.NAMES)
def test_golden_files_match_the_analysis_version(name: str) -> None:
    assert stored_doc(name)["analysis_version"] == ANALYSIS_VERSION


@pytest.mark.parametrize("name", synthetic.NAMES)
def test_raw_run_directories_recompute_exactly(name: str) -> None:
    assert dumps(compute(load_run(FIXTURES / name))) == stored(name)


def test_raw_files_are_what_the_generator_writes() -> None:
    assert synthetic.main(["--check"]) == 0


@pytest.mark.parametrize("name", synthetic.NAMES)
def test_fixture_files_match_the_schemas(name: str, schema_validator: Any) -> None:
    run = json.loads((FIXTURES / name / "run.json").read_text(encoding="utf-8"))
    schema_validator("run").validate(run)
    schema_validator("metrics").validate(stored_doc(name))
    for path in sorted((FIXTURES / name).rglob("*")):
        if path.is_file() and path.name not in ("run.json", "metrics.json"):
            assert path.relative_to(FIXTURES / name).as_posix() in run["files"], path


def test_midnight_and_epoch_mapping_change_nothing() -> None:
    steady, midnight = stored_doc("synthetic-steady"), stored_doc("synthetic-midnight")
    steady.pop("run_id")
    midnight.pop("run_id")
    assert steady == midnight


def value(doc: dict[str, Any], metric_id: str) -> Any:
    entry = doc["metrics"][metric_id]
    assert entry["availability"] == "measured", (metric_id, entry)
    return entry["value"]


def test_known_answers_of_the_steady_run() -> None:
    doc = stored_doc("synthetic-steady")
    assert doc["status"] == "ok" and doc["status_reasons"] == [] and doc["flags"] == []
    assert value(doc, "tps_window_steady") == pytest.approx(20.0, abs=0.01)
    assert value(doc, "tps_load_low5") == pytest.approx(20.0, abs=0.02)
    assert value(doc, "tick_cpu_ms_per_tick_steady") == pytest.approx(20.0, abs=0.01)
    assert value(doc, "chunks_per_bot") == {"min": 290, "median": 297.5, "max": 305, "n": 6}
    assert value(doc, "chunks_per_s_load") == pytest.approx(
        sum(synthetic.u_final(i) for i in range(6)) / 65.0, abs=1e-4
    )
    assert value(doc, "csg_effective") is False
    assert value(doc, "joined") == 6


def test_known_answers_of_the_dip_run() -> None:
    doc = stored_doc("synthetic-dip")
    # 10 TPS from 50 s to 70 s while the tick thread saturates
    assert value(doc, "tps_load_low5") == pytest.approx(10.0, abs=0.01)
    assert value(doc, "tps_window_steady") == pytest.approx(20.0, abs=0.01)


def test_known_answers_of_the_death_run() -> None:
    doc = stored_doc("synthetic-death")
    assert doc["status"] == "server_failed"
    assert doc["status_reasons"] == ["server_alive: the server exited before teardown"]
    assert "unclean_stop" not in doc["flags"], "a death before teardown is a failure, not an unclean stop"
    assert doc["flags"] == ["dropped"]
    assert doc["metrics"]["tps_window_steady"]["reason"] == "the steady window was not reached"
    assert value(doc, "joined") == 6


def test_fixture_directory_has_no_stray_runs() -> None:
    dirs = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir() and not p.name.startswith("__"))
    assert dirs == sorted(synthetic.NAMES)
