# SPDX-License-Identifier: Apache-2.0
"""``bbench run-one`` end to end against the fake server and the fake bots.

A good run leaves a complete, schema-valid run directory whose metrics ``bbench analyze`` recomputes byte for byte.
A broken run is classified by the gate that fails, with the matching status and exit code. Every console family goes
through the same phases.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench.model import MARKS, ExitCode, Status
from fakes.bench import FakeBench, Run
from fakes.fake_adapter import FAKE_ADAPTERS, FakeAdapter
from fakes.fake_server import FAMILIES

pytestmark = pytest.mark.integration

#: Raw files every run directory holds (docs/methodology.md#results-files).
RAW_FILES = (
    "server.log.gz",
    "commands.jsonl.gz",
    "bots.jsonl.gz",
    "series/probe.csv",
    "series/threads.csv.gz",
    "series/tick.csv.gz",
    "series/process.csv.gz",
    "series/machine.csv.gz",
)


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> FakeBench:
    return FakeBench.create(tmp_path_factory.mktemp("bench"))


@pytest.fixture(scope="module")
def good_run(bench: FakeBench) -> Run:
    run = bench.run_one("fake_bds")
    assert run.exit_code == ExitCode.OK, run.describe()
    return run


def read_lines(path: Path) -> list[str]:
    opener: Callable[..., Any] = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return fh.read().splitlines()


# --------------------------------------------------------------------------------------------------------------------
# A good run
# --------------------------------------------------------------------------------------------------------------------


def test_good_run_is_ok(good_run: Run) -> None:
    record = good_run.run_json
    assert record["status"] == Status.OK, record.get("status_reasons")
    assert record["exit_code"] == ExitCode.OK
    assert record["status_reasons"] == []
    assert good_run.metrics_json["status"] == Status.OK


def test_run_directory_is_complete(good_run: Run) -> None:
    run_dir = good_run.require_dir()
    for name in ("run.json", "metrics.json", *RAW_FILES):
        assert (run_dir / name).is_file(), f"missing {name}"
    assert (run_dir / "config").is_dir(), "rendered configs are kept under config/"
    assert not (run_dir / "run.partial.json").exists(), "the partial record is replaced by run.json"
    assert not (run_dir / "crash").exists()


def test_every_raw_file_is_hashed(good_run: Run) -> None:
    run_dir = good_run.require_dir()
    files = good_run.run_json["files"]
    assert set(RAW_FILES) <= set(files)
    for name, entry in files.items():
        data = (run_dir / name).read_bytes()
        assert entry == {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}, name


def test_results_match_the_schemas(good_run: Run, schema_validator: Callable[[str], Any]) -> None:
    schema_validator("run").validate(good_run.run_json)
    schema_validator("metrics").validate(good_run.metrics_json)
    events = [json.loads(line) for line in read_lines(good_run.require_dir() / "bots.jsonl.gz")]
    assert events, "bots.jsonl.gz is empty"
    validator = schema_validator("bot-event")
    for event in events:
        validator.validate(event)
        assert isinstance(event.get("rx_ns"), int), "the harness stamps every bot event with rx_ns"


def test_server_log_is_stamped(good_run: Run) -> None:
    lines = read_lines(good_run.require_dir() / "server.log.gz")
    parts = [line.split("\t", 2) for line in lines]
    assert all(len(p) == 3 and p[0].isdigit() and p[1].isdigit() for p in parts), "lines are <mono_ns>\\t<epoch_ms>\\t"
    assert any(p[2].endswith("Server started.") for p in parts)
    mono = [int(p[0]) for p in parts]
    assert mono == sorted(mono)


def test_run_records_phases_and_outcome(good_run: Run) -> None:
    record = good_run.run_json
    assert (record["arm"]["server"], record["arm"]["version"]) == ("fake_bds", "1.0.0")
    assert record["server"]["banner_version"] == "1.0.0"
    marks = record["marks"]
    reached = [marks[name] for name in MARKS if name in marks and marks[name] is not None]
    assert reached == sorted(reached), f"marks out of order: {marks}"
    for name in ("ready", "bots_start", "alljoined", "settled", "steady0", "steady1", "stop"):
        assert marks.get(name) is not None, f"mark {name} missing"
    assert record["spread"]["source"] == "querytarget"
    spread = {k: record["spread"][k] for k in ("players", "cells_occupied", "max_per_cell")}
    assert spread == {"players": 4, "cells_occupied": 4, "max_per_cell": 1}
    health = record["health"]
    assert (health["died"], health["server_exit"], health["joined"], health["failed"]) == (False, 0, 4, 0)
    assert health["decode_errors"] == 0
    assert record["world"]["fresh"] is True
    assert record["interventions"]["in_measured"] == 0


def test_config_keys_and_launch_are_recorded(good_run: Run) -> None:
    (written,) = good_run.run_json["config_files"]
    assert (written["path"], written["template"], written["created"]) == (
        "server.properties",
        "server.properties",
        True,
    )
    assert {"key": "online-mode", "before": None, "after": "false"} in written["keys"]
    assert written["after_sha256"] is not None
    launch = good_run.run_json["server"]["launch"]
    assert "--family" in launch["argv"] and "PYTHONPATH" in launch["env_remove"]


def test_steady_tps_is_about_20(good_run: Run) -> None:
    metrics = good_run.metrics_json["metrics"]
    steady = {
        key: value
        for key, value in metrics.items()
        if key.startswith("tps") and "steady" in key and isinstance(value.get("value"), (int, float))
    }
    assert steady, f"no scalar steady TPS metric among {sorted(metrics)}"
    for key, value in steady.items():
        assert value["availability"] == "measured", key
        assert 18.0 <= value["value"] <= 21.0, f"{key} = {value['value']}"


def test_probe_series_is_monotonic(good_run: Run) -> None:
    rows = [line.split(",") for line in read_lines(good_run.require_dir() / "series" / "probe.csv")]
    header, data = rows[0], rows[1:]
    assert header[:3] == ["send_ns", "rx_ns", "tick"]
    ticks = [int(row[2]) for row in data]
    assert len(ticks) >= 10
    assert ticks == sorted(ticks)
    assert all(int(row[1]) >= int(row[0]) for row in data)


def test_run_json_holds_no_local_paths(bench: FakeBench, good_run: Run) -> None:
    text = (good_run.require_dir() / "run.json").read_text(encoding="utf-8")
    for path in (bench.root, Path.home(), bench.root / "cache"):
        for spelling in {str(path), str(path).replace("\\", "/"), json.dumps(str(path))[1:-1]}:
            assert spelling not in text, f"run.json contains the local path {spelling}"
    host = socket.gethostname()
    if len(host) >= 4:
        assert host not in text, "run.json contains the host name"


def test_analyze_recomputes_identical_metrics(bench: FakeBench, good_run: Run) -> None:
    run_dir = good_run.require_dir()
    before_metrics = (run_dir / "metrics.json").read_bytes()
    before_status = good_run.run_json["status"]
    result = bench.bbench("analyze", run_dir)
    assert result.returncode == ExitCode.OK, result.stderr
    assert (run_dir / "metrics.json").read_bytes() == before_metrics
    assert good_run.run_json["status"] == before_status


def test_validate_accepts_the_run(bench: FakeBench, good_run: Run) -> None:
    result = bench.bbench("validate", good_run.require_dir())
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr


# --------------------------------------------------------------------------------------------------------------------
# Broken runs are invalid, by the gate that fails
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "gate"),
    [
        ("positions-truncated", "spread"),
        ("tick-stalls-idle", "probe_monotonic"),
        ("probe-silent-after-join", "probe"),
        ("decode-error", "decode_errors"),
        ("wrong-version", "version_match"),
    ],
)
def test_broken_run_is_invalid(bench: FakeBench, behaviour: str, gate: str) -> None:
    run = bench.run_one("fake_bds", behaviour=behaviour)
    assert run.exit_code == ExitCode.INVALID, run.describe()
    record = run.run_json
    assert record["status"] == Status.INVALID
    assert record["exit_code"] == ExitCode.INVALID
    assert any(gate in reason for reason in record["status_reasons"]), record["status_reasons"]
    assert (run.require_dir() / "metrics.json").is_file(), "invalid runs are analysed and kept"


# --------------------------------------------------------------------------------------------------------------------
# Every console family
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [c for c in FAKE_ADAPTERS if c.name != "fake_bds"], ids=lambda c: c.name)
def test_every_console_family_completes(bench: FakeBench, cls: type[FakeAdapter]) -> None:
    run = bench.run_one(cls.name)
    assert run.exit_code == ExitCode.OK, run.describe()
    record = run.run_json
    assert record["status"] == Status.OK, record["status_reasons"]
    assert record["spread"]["players"] == 4
    expected_sources = ("bots",) if FAMILIES[cls.family].positions_cmd is None else ("querytarget", "helper")
    assert record["spread"]["source"] in expected_sources
    assert record["server"]["banner_version"] == "1.0.0"
