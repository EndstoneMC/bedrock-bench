# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import BOT_SCHEMA, METRICS_SCHEMA, RUN_SCHEMA, SCHEMA_VERSION, config
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

SCHEMAS = ("run", "metrics", "bot-event", "claims")


@pytest.fixture(scope="module")
def examples(project_root: Path) -> Path:
    return project_root / "schemas" / "examples"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def errors(validator: Any, instance: Any) -> list[str]:
    return [e.message for e in validator.iter_errors(instance)]


@pytest.fixture(scope="module")
def run_example(examples: Path) -> dict[str, Any]:
    return load_json(examples / "run.ok.json")


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_is_valid(schema_validator, name: str) -> None:
    schema_validator(name)  # check_schema runs inside the fixture


def test_schema_ids_match_constants(project_root: Path) -> None:
    def const(name: str) -> Any:
        return load_json(project_root / "schemas" / f"{name}.schema.json")["properties"]

    assert const("run")["schema"]["const"] == RUN_SCHEMA
    assert const("metrics")["schema"]["const"] == METRICS_SCHEMA
    assert const("bot-event")["v"]["const"] == SCHEMA_VERSION == int(BOT_SCHEMA.rsplit("/", 1)[1])


def test_schema_enums_match_model(project_root: Path) -> None:
    run = load_json(project_root / "schemas" / "run.schema.json")
    props, defs = run["properties"], run["$defs"]
    assert defs["status"]["enum"] == [s.value for s in Status]
    assert props["overrides"]["items"]["properties"]["category"]["enum"] == [c.value for c in OverrideCategory]
    assert props["artifacts"]["items"]["properties"]["source"]["enum"] == list(ARTIFACT_SOURCES)
    assert props["parity"]["properties"]["row"]["additionalProperties"]["enum"] == [c.value for c in FeatureClass]
    assert props["parity"]["properties"]["fidelity"]["enum"][:-1] == [f.value for f in Fidelity]
    assert props["placement"]["properties"]["priority"]["enum"] == list(PRIORITIES)
    assert props["placement"]["properties"]["kind"]["enum"] == list(PLACEMENT_KINDS)
    metrics = load_json(project_root / "schemas" / "metrics.schema.json")
    assert metrics["$defs"]["metric"]["properties"]["availability"]["enum"] == [a.value for a in Availability]


# --------------------------------------------------------------------------------------------------------------------
# run.json
# --------------------------------------------------------------------------------------------------------------------


class TestRun:
    def test_examples_validate(self, schema_validator, examples: Path) -> None:
        validator = schema_validator("run")
        for name in ("run.ok.json", "run.partial.json"):
            assert errors(validator, load_json(examples / name)) == [], name

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

    def test_final_run_needs_status(self, schema_validator, examples: Path) -> None:
        partial = load_json(examples / "run.partial.json")
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
    def test_example_validates(self, schema_validator, examples: Path) -> None:
        assert errors(schema_validator("metrics"), load_json(examples / "metrics.json")) == []

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
    def test_metric_rules(self, schema_validator, examples: Path, metric: dict) -> None:
        doc = load_json(examples / "metrics.json")
        doc["metrics"]["tps_window_steady"] = metric
        assert errors(schema_validator("metrics"), doc)

    def test_metric_id_format(self, schema_validator, examples: Path) -> None:
        doc = load_json(examples / "metrics.json")
        doc["metrics"]["TPS-steady"] = {"value": 1, "unit": "tps", "availability": "measured"}
        assert errors(schema_validator("metrics"), doc)


# --------------------------------------------------------------------------------------------------------------------
# bot events
# --------------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lines(examples: Path) -> list[dict[str, Any]]:
    text = (examples / "bot-events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestBotEvents:
    def test_examples_validate(self, schema_validator, lines: list[dict]) -> None:
        validator = schema_validator("bot-event")
        for line in lines:
            assert errors(validator, line) == [], line["ev"]

    def test_examples_cover_every_core_event(self, project_root: Path, lines: list[dict]) -> None:
        schema = load_json(project_root / "schemas" / "bot-event.schema.json")
        seen = {line["ev"] for line in lines}
        lifecycle = {"died", "respawned", "dim", "left", "dropped", "rejoin", "kicked"}
        missing = set(schema["properties"]["ev"]["enum"]) - seen - lifecycle
        assert not missing
        assert seen & lifecycle

    def test_harness_receipt_stamp_allowed(self, schema_validator, lines: list[dict]) -> None:
        assert errors(schema_validator("bot-event"), {**lines[1], "rx_ns": 123456789}) == []

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


# --------------------------------------------------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------------------------------------------------


class TestClaims:
    def test_example_validates_and_loads(self, schema_validator, examples: Path) -> None:
        path = examples / "claims.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert errors(schema_validator("claims"), data) == []
        assert len(config.load_claims(path).claims) == len(data["claim"])

    @pytest.mark.parametrize(
        "data",
        [
            {"schema": 2, "matrix": "m", "claim": [{"id": "a", "text": "t", "expr": "x"}]},
            {"schema": 1, "matrix": "m", "claim": []},
            {"schema": 1, "matrix": "m", "claim": [{"id": "a", "text": "t"}]},
            {"schema": 1, "matrix": "m", "claim": [{"id": "a", "text": "t", "expr": "x", "weight": 1}]},
        ],
    )
    def test_rejects(self, schema_validator, data: dict) -> None:
        assert errors(schema_validator("claims"), data)
        with pytest.raises(config.ConfigError):
            config.parse_claims(data)
