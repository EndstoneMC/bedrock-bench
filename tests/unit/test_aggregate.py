# SPDX-License-Identifier: Apache-2.0
"""Aggregation of repeats into summary.json: core statistics, loading, one row per server and publish rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench.config import load_report_metrics
from bedrock_bench.metrics import registry
from bedrock_bench.report.aggregate import (
    ReportError,
    core_metric,
    load_runs,
    make_record,
    publish_problems,
    statuses,
    summarize,
    write_summary,
)


def run_doc(
    server: str,
    version: str,
    *,
    repeat: int = 1,
    bds: str | None = None,
    profile: str = "a" * 64,
    fingerprint: str = "m1",
    workload: str = "b" * 64,
    **extra: Any,
) -> dict[str, Any]:
    doc = {
        "run_id": f"20261012T1200{repeat:02d}Z-{server}@{version}-r{repeat}",
        "status": "ok",
        "arm": {"server": server, "version": version, "bds": bds},
        "workload": {"name": "benchmark", "sha256": workload},
        "machine": {"fingerprint_id": fingerprint, "profile_sha256": profile, "cpu": "Test CPU"},
        "official": True,
        "nonstandard": [],
    }
    doc.update(extra)
    return doc


def metrics_doc(status: str = "ok", *, reasons: tuple[str, ...] = (), **values: Any) -> dict[str, Any]:
    metrics = {}
    for metric, value in values.items():
        if isinstance(value, dict) and "availability" in value:
            metrics[metric] = {"unit": "x", "window": "steady", "value": None, **value}
        else:
            metrics[metric] = {"unit": "x", "window": "steady", "availability": "measured", "value": value}
    return {"analysis_version": 1, "status": status, "status_reasons": list(reasons), "flags": [], "metrics": metrics}


def record(server: str, version: str, tps: float = 20.0, *, status: str = "ok", **kwargs: Any):
    return make_record(run_doc(server, version, **kwargs), metrics_doc(status, tps_window_steady=tps))


def by_tag(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["tag"]: s for s in summary["servers"]}


def _session() -> list:
    runs = []
    for repeat, (bds, e13, e14) in enumerate(((12.0, 13.4, 19.6), (12.2, 11.4, 19.5), (11.8, 12.6, 19.7)), start=1):
        runs.append(record("bds", "1.26.52.3", bds, repeat=repeat))
        runs.append(record("endstone", "0.11.13", e13, repeat=repeat, bds="1.26.52.3"))
        runs.append(record("endstone", "0.11.14", e14, repeat=repeat, bds="1.26.52.3"))
    return runs


# --------------------------------------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------------------------------------


class TestCoreMetric:
    def test_scalar(self) -> None:
        stat = core_metric([{"value": v, "availability": "measured"} for v in (19.5, 19.7, 19.6)], None)
        assert (stat["n"], stat["median"], stat["min"], stat["max"]) == (3, 19.6, 19.5, 19.7)
        assert stat["availability"] == "measured" and "reasons" not in stat

    def test_even_count_takes_the_mean_of_the_middle(self) -> None:
        stat = core_metric([{"value": v, "availability": "measured"} for v in (1.0, 2.0, 3.0, 4.0)], None)
        assert stat["median"] == 2.5

    def test_part_of_a_distribution(self) -> None:
        entries = [
            {"value": {"median": 80.0, "p90": 120.0, "max": 150.0}, "availability": "measured"},
            {"value": {"median": 90.0, "p90": 130.0, "max": 170.0}, "availability": "measured"},
        ]
        assert core_metric(entries, "p90")["median"] == 125.0
        assert core_metric(entries, "max")["max"] == 170.0

    def test_partly_unavailable(self) -> None:
        stat = core_metric(
            [
                {"value": 1.0, "availability": "measured"},
                {"value": None, "availability": "unavailable", "reason": "sampler died"},
                None,
            ],
            None,
        )
        assert stat["values"] == [1.0, None, None] and stat["n"] == 1
        assert stat["reasons"] == ["not computed", "sampler died"]

    def test_not_applicable_everywhere(self) -> None:
        entry = {"value": None, "availability": "not_applicable", "reason": "no tick thread"}
        stat = core_metric([entry, entry], None)
        assert stat["availability"] == "not_applicable" and stat["n"] == 0 and "median" not in stat
        assert stat["reasons"] == ["no tick thread"]

    def test_no_runs(self) -> None:
        assert core_metric([], None) == {"values": [], "n": 0, "availability": "unavailable"}


# --------------------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------------------


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestLoadRuns:
    def test_status_sources(self, tmp_path: Path) -> None:
        _write(tmp_path / "a" / "run.json", run_doc("bds", "1.0", status="server_failed"))
        _write(tmp_path / "a" / "metrics.json", metrics_doc("ok"))
        _write(tmp_path / "b" / "run.json", run_doc("bds", "1.0", repeat=2))
        _write(tmp_path / "c" / "run.partial.json", {**run_doc("bds", "1.0", repeat=3), "status": None})
        (tmp_path / "d").mkdir()
        (tmp_path / "summary.json").write_text("{}", encoding="utf-8")

        records = load_runs(tmp_path)
        assert [r.run_id[-2:] for r in records] == ["r1", "r2", "r3"]
        assert records[0].status == "ok"  # metrics.json is recomputed by bbench analyze and wins
        assert records[1].status == "invalid" and "metrics.json missing" in records[1].reasons[0]
        assert records[2].status == "invalid" and "did not finish" in records[2].reasons[0]

    def test_empty_or_missing(self, tmp_path: Path) -> None:
        with pytest.raises(ReportError, match="no run directories"):
            load_runs(tmp_path)
        with pytest.raises(ReportError, match="not a directory"):
            load_runs(tmp_path / "nope")

    def test_unreadable_json(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "run.json").write_text("{", encoding="utf-8")
        with pytest.raises(ReportError, match="cannot read"):
            load_runs(tmp_path)


# --------------------------------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------------------------------


class TestSummarize:
    def test_one_row_per_server_tag(self) -> None:
        runs = _session()
        runs.append(record("bds", "1.26.52.3", status="server_failed", repeat=4))
        summary = summarize(runs)
        assert summary["n_runs"] == 10
        assert summary["status_counts"] == {"ok": 9, "server_failed": 1, "invalid": 0}
        servers = by_tag(summary)
        assert list(servers) == ["bds@1.26.52.3", "endstone@0.11.13", "endstone@0.11.14"]
        bds = servers["bds@1.26.52.3"]
        assert (bds["n_total"], bds["n_ok"], bds["n_server_failed"]) == (4, 3, 1)
        assert bds["metrics"]["tps"]["values"] == [12.0, 12.2, 11.8]
        assert (bds["metrics"]["tps"]["median"], bds["metrics"]["tps"]["min"]) == (12.0, 11.8)
        assert servers["endstone@0.11.14"]["metrics"]["tps"]["median"] == 19.6
        assert servers["endstone@0.11.14"]["arm"]["bds"] == "1.26.52.3"

    def test_every_core_metric_is_present(self) -> None:
        summary = summarize(_session())
        metrics = by_tag(summary)["bds@1.26.52.3"]["metrics"]
        assert list(metrics) == [c["column"] for c in summary["columns"]]
        assert [c["column"] for c in summary["columns"]] == [m.column for m in load_report_metrics()]
        assert metrics["memory_mib"] == {
            "values": [None, None, None],
            "n": 0,
            "availability": "unavailable",
            "reasons": ["not computed"],
        }

    def test_core_metrics_name_registered_metrics(self) -> None:
        for metric in load_report_metrics():
            declared = registry.get(metric.metric)
            assert metric.stat is None or metric.stat in declared.stats, metric.column

    def test_hosted_build_missing_from_the_report(self) -> None:
        runs = [r for r in _session() if r.arm["server"] == "endstone"]
        assert by_tag(summarize(runs))["endstone@0.11.14"]["flags"] == {"different_bds_build": 3}
        assert by_tag(summarize(_session()))["endstone@0.11.14"]["flags"] == {}

    def test_environment(self) -> None:
        env = summarize(_session())["environment"]
        assert env["machine"] == {"cpu": "Test CPU"}
        assert env["workload"] == {"name": "benchmark", "sha256": "b" * 64}
        assert [s["version"] for s in env["servers"]] == ["1.26.52.3", "0.11.13", "0.11.14"]

    def test_refuses_mixed_inputs(self) -> None:
        with pytest.raises(ReportError, match="different machine profiles"):
            summarize([record("bds", "1.0"), record("bds", "1.0", repeat=2, profile="c" * 64)])
        with pytest.raises(ReportError, match="different machines"):
            summarize([record("bds", "1.0"), record("bds", "1.0", repeat=2, fingerprint="m2")])
        with pytest.raises(ReportError, match="different workloads"):
            summarize([record("bds", "1.0"), record("bds", "1.0", repeat=2, workload="d" * 64)])
        other = metrics_doc(tps_window_steady=20.0) | {"analysis_version": 2}
        with pytest.raises(ReportError, match="different analysis versions"):
            summarize([record("bds", "1.0"), make_record(run_doc("bds", "1.0", repeat=2), other)])

    def test_deterministic(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a.json", tmp_path / "b.json"
        write_summary(summarize(_session()), first)
        write_summary(summarize(list(reversed(_session()))), second)
        assert first.read_bytes() == second.read_bytes()


class TestPublish:
    def test_clean_session_passes(self) -> None:
        assert publish_problems(summarize(_session())) == []

    def test_refusals(self) -> None:
        runs = _session()
        runs.append(record("pocketmine", "5.44.3", repeat=1))
        runs.append(record("pocketmine", "5.44.3", repeat=2, official=False, nonstandard=["dirty_harness"]))
        runs.append(record("chorus", "1", status="server_failed"))
        assert publish_problems(summarize(runs)) == [
            "pocketmine@5.44.3: 2 ok runs, publication needs at least 3",
            "pocketmine@5.44.3: includes runs marked not official (dirty_harness)",
        ]

    def test_local_artifact(self) -> None:
        arm = {"server": "bds", "version": "1.0", "artifact": "x.zip"}
        runs = [record("bds", "1.0", repeat=k, arm=arm) for k in (1, 2, 3)]
        assert publish_problems(summarize(runs)) == ["bds@1.0: ran a local artifact, not a released one"]


def test_statuses() -> None:
    runs = [record("bds", "1.0"), record("bds", "1.0", status="invalid", repeat=2)]
    assert sorted(statuses(summarize(runs))) == ["invalid", "ok"]
