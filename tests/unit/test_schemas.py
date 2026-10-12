# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import BOT_SCHEMA, METRICS_SCHEMA, RUN_SCHEMA, SCHEMA_VERSION
from bedrock_bench.config import load_schema
from bedrock_bench.model import (
    ARTIFACT_SOURCES,
    PLACEMENT_KINDS,
    PRIORITIES,
    Availability,
    FeatureClass,
    Fidelity,
    OverrideCategory,
    Status,
    sha256_json,
)

SCHEMAS = ("run", "metrics", "bot-event")


@pytest.fixture(scope="module")
def fixtures(project_root: Path) -> Path:
    return project_root / "tests" / "fixtures"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def errors(validator: Any, instance: Any) -> list[str]:
    return [e.message for e in validator.iter_errors(instance)]


def partial_run(run: dict[str, Any]) -> dict[str, Any]:
    """run.partial.json as the runner writes it right after the server is ready."""
    keep = ("schema", "analysis_version", "run_id", "matrix", "harness", "arm", "scenario", "time")
    partial = {key: copy.deepcopy(run[key]) for key in keep}
    partial.update(partial=True, status=None, marks={"ready": run["marks"]["ready"]})
    return partial


@pytest.fixture(scope="module")
def run_example(fixtures: Path) -> dict[str, Any]:
    return load_json(fixtures / "run.ok.json")


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_is_valid(schema_validator, name: str) -> None:
    schema_validator(name)  # check_schema runs inside the fixture


def test_schema_ids_match_constants() -> None:
    def const(name: str) -> Any:
        return load_schema(name)["properties"]

    assert const("run")["schema"]["const"] == RUN_SCHEMA
    assert const("metrics")["schema"]["const"] == METRICS_SCHEMA
    assert const("bot-event")["v"]["const"] == SCHEMA_VERSION == int(BOT_SCHEMA.rsplit("/", 1)[1])


def test_schema_enums_match_model() -> None:
    run = load_schema("run")
    props, defs = run["properties"], run["$defs"]
    assert defs["status"]["enum"] == [s.value for s in Status]
    assert props["overrides"]["items"]["properties"]["category"]["enum"] == [c.value for c in OverrideCategory]
    assert props["artifacts"]["items"]["properties"]["source"]["enum"] == list(ARTIFACT_SOURCES)
    assert props["parity"]["properties"]["row"]["additionalProperties"]["enum"] == [c.value for c in FeatureClass]
    assert props["parity"]["properties"]["fidelity"]["enum"][:-1] == [f.value for f in Fidelity]
    assert props["placement"]["properties"]["priority"]["enum"] == list(PRIORITIES)
    assert props["placement"]["properties"]["kind"]["enum"] == list(PLACEMENT_KINDS)
    assert props["harness"]["properties"]["backend"]["enum"] == ["native-windows", "native-linux"]
    metrics = load_schema("metrics")
    assert metrics["$defs"]["metric"]["properties"]["availability"]["enum"] == [a.value for a in Availability]


# --------------------------------------------------------------------------------------------------------------------
# run.json
# --------------------------------------------------------------------------------------------------------------------


class TestRun:
    def test_fixture_validates(self, schema_validator, run_example: dict) -> None:
        validator = schema_validator("run")
        assert errors(validator, run_example) == []
        assert errors(validator, partial_run(run_example)) == []

    def test_example_scenario_hash_is_consistent(self, run_example: dict) -> None:
        scenario = run_example["scenario"]
        assert sha256_json(scenario["resolved"]) == scenario["sha256"]

    def test_ok_run_must_be_complete(self, schema_validator, run_example: dict) -> None:
        validator = schema_validator("run")
        for key in ("files", "health", "placement", "bot", "overrides"):
            broken = copy.deepcopy(run_example)
            del broken[key]
            assert errors(validator, broken), key

    def test_failed_run_may_be_sparse(self, schema_validator, run_example: dict) -> None:
        keep = ("schema", "analysis_version", "run_id", "harness", "arm", "scenario", "time")
        sparse = {k: run_example[k] for k in keep}
        sparse.update(status="invalid", status_reasons=["harness_ok"], flags=[], exit_code=5)
        assert errors(schema_validator("run"), sparse) == []

    def test_final_run_needs_status(self, schema_validator, run_example: dict) -> None:
        partial = partial_run(run_example)
        del partial["partial"]
        assert errors(schema_validator("run"), partial)
        partial.update(status="server_failed", status_reasons=["server_alive"], flags=[], exit_code=4)
        assert errors(schema_validator("run"), partial) == []

    @pytest.mark.parametrize(
        "path",
        [
            "/home/user/runs/server.properties",
            "C:\\Users\\me\\server.properties",
            "c:/x",
            "~/x",
            "../outside",
            "a/../../b",
            "\\\\host\\share\\x",
        ],
    )
    def test_absolute_paths_rejected(self, schema_validator, run_example: dict, path: str) -> None:
        broken = copy.deepcopy(run_example)
        broken["config_files"][0]["path"] = path
        assert errors(schema_validator("run"), broken)

    @pytest.mark.parametrize("path", ["server.properties", "config/pocketmine.yml", "series/probe.csv", "a..b"])
    def test_relative_paths_accepted(self, schema_validator, run_example: dict, path: str) -> None:
        fine = copy.deepcopy(run_example)
        fine["config_files"][0]["path"] = path
        assert errors(schema_validator("run"), fine) == []

    @pytest.mark.parametrize(
        ("mutate", "field"),
        [
            (lambda r: r.update(run_id="endstone-run-1"), "run_id"),
            (lambda r: r.update(status="crashed"), "status"),
            (lambda r: r.update(exit_code=1), "exit_code"),
            (lambda r: r.update(hostname="bench-01"), "unknown top-level key"),
            (lambda r: r["overrides"][0].update(category="tuning"), "override category"),
            (lambda r: r["overrides"][0].update(reason=""), "empty reason"),
            (lambda r: r["install"].update(tree_sha256="xyz"), "sha256"),
            (lambda r: r["placement"].update(kind="container"), "placement kind"),
            (lambda r: r["parity"]["row"].update({"player.physics": "maybe"}), "parity class"),
            (lambda r: r["marks"].update(steady0="late"), "mark type"),
            (lambda r: r["arm"].update(server="Endstone"), "server name"),
        ],
    )
    def test_rejects(self, schema_validator, run_example: dict, mutate, field: str) -> None:
        broken = copy.deepcopy(run_example)
        mutate(broken)
        assert errors(schema_validator("run"), broken), field


# --------------------------------------------------------------------------------------------------------------------
# metrics.json
# --------------------------------------------------------------------------------------------------------------------


class TestMetrics:
    def test_fixture_validates(self, schema_validator, fixtures: Path) -> None:
        assert errors(schema_validator("metrics"), load_json(fixtures / "metrics.json")) == []

    @pytest.mark.parametrize(
        "metric",
        [
            {"value": None, "unit": "tps", "availability": "measured"},
            {"value": 19.9, "unit": "tps", "availability": "unavailable", "reason": "x"},
            {"value": None, "unit": "tps", "availability": "unavailable"},
            {"value": 1, "unit": "tps"},
            {"value": 1, "unit": "tps", "availability": "measured", "extra": 1},
            {"value": 1, "unit": "tps", "availability": "estimated"},
        ],
    )
    def test_metric_rules(self, schema_validator, fixtures: Path, metric: dict) -> None:
        doc = load_json(fixtures / "metrics.json")
        doc["metrics"]["tps_window_steady"] = metric
        assert errors(schema_validator("metrics"), doc)

    def test_metric_id_format(self, schema_validator, fixtures: Path) -> None:
        doc = load_json(fixtures / "metrics.json")
        doc["metrics"]["TPS-steady"] = {"value": 1, "unit": "tps", "availability": "measured"}
        assert errors(schema_validator("metrics"), doc)


# --------------------------------------------------------------------------------------------------------------------
# bot events
# --------------------------------------------------------------------------------------------------------------------


class TestBotEvents:
    def test_harness_receipt_stamp_allowed(self, schema_validator) -> None:
        dial = {"v": 1, "ev": "dial", "t": 1791813526400, "mono_ms": 2100, "bot": "a07", "i": 7}
        assert errors(schema_validator("bot-event"), dial) == []
        assert errors(schema_validator("bot-event"), {**dial, "rx_ns": 123456789}) == []

    @pytest.mark.parametrize(
        "event",
        [
            {"v": 2, "ev": "done", "t": 1, "mono_ms": 0},
            {"v": 1, "ev": "teleport", "t": 1, "mono_ms": 0},
            {"v": 1, "ev": "done", "mono_ms": 0},
            {"v": 1, "ev": "done", "t": 1.5, "mono_ms": 0},
            {"v": 1, "ev": "dial", "t": 1, "mono_ms": 0, "bot": "a00"},
            {"v": 1, "ev": "join_failed", "t": 1, "mono_ms": 0, "bot": "a00", "i": 0, "stage": "auth", "err": "x"},
            {"v": 1, "ev": "tp", "t": 1, "mono_ms": 0, "bot": "a00", "pos": [1, 2], "dim": 0, "n": 1},
            {"v": 1, "ev": "progress", "t": 1, "mono_ms": 0, "rows": [["a00", 1, 1, 0, 0, 0, 0, 0]]},
            {"v": 1, "ev": "stat", "t": 1, "mono_ms": 0, "online": 1},
            {"v": 1, "ev": "kicked", "t": 1, "mono_ms": 0},
            {"v": 1, "ev": "decode_error", "t": 1, "mono_ms": 0, "bot": "a00", "pkt_id": 1, "err": "x", "total": 0},
            {
                "v": 1,
                "ev": "bot_summary",
                "t": 1,
                "mono_ms": 0,
                "bot": "a00",
                "uniq": 1,
                "in_view": 1,
                "chunks": 1,
                "corrections": 0,
                "first_seen_ms": [-5],
            },
        ],
    )
    def test_rejects(self, schema_validator, event: dict) -> None:
        assert errors(schema_validator("bot-event"), event)
