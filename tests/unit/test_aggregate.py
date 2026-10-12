# SPDX-License-Identifier: Apache-2.0
"""Aggregation of repeats into summary.json: statistics, the difference rule, loading, grouping and publish rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench.model import FeatureCell, FeatureClass, FeatureFile, FeatureRange, Fidelity, canonical_json
from bedrock_bench.report.aggregate import (
    ReportError,
    aggregate_metric,
    compare,
    load_runs,
    make_record,
    order_stats,
    publish_problems,
    separated,
    separation_p,
    statuses,
    summarize,
    write_summary,
)

EXERCISES = ["player.server_authoritative_movement", "terrain.vanilla_generator"]
VANILLA_ROW = {feature: "vanilla" for feature in EXERCISES}


def run_doc(
    arm: str,
    server: str,
    version: str,
    *,
    repeat: int = 1,
    bds: str | None = None,
    scenario: str = "spread100",
    row: dict[str, str] | None = None,
    csg: bool = True,
    profile: str = "a" * 64,
    fingerprint: str = "m1",
    scenario_sha: str = "b" * 64,
    **extra: Any,
) -> dict[str, Any]:
    doc = {
        "run_id": f"20261012T120000Z-{arm}-{scenario}-r{repeat}-000{repeat}",
        "status": "ok",
        "matrix": {"name": "m", "repeat": repeat, "order_index": repeat, "order": "rotate"},
        "arm": {"id": arm, "server": server, "version": version, "variant": None, "bds": bds},
        "scenario": {
            "name": scenario,
            "sha256": scenario_sha,
            "resolved": {"exercises": EXERCISES, "requires": [], "server": {"csg": csg}, "bots": {"n": 4}},
        },
        "machine": {"fingerprint_id": fingerprint, "profile_sha256": profile, "label": "reference"},
        "parity": {"file_sha256": "f" * 64, "row": dict(VANILLA_ROW if row is None else row), "fidelity": "V0"},
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


def record(arm: str, server: str, version: str, tps: float = 20.0, *, status: str = "ok", **kwargs: Any):
    return make_record(run_doc(arm, server, version, **kwargs), metrics_doc(status, tps_window_steady=tps))


def cells(summary: dict[str, Any], scenario: str = "spread100") -> dict[str, dict[str, Any]]:
    table = next(s for s in summary["scenarios"] if s["name"] == scenario)
    return {c["arm"]["id"]: c for c in table["cells"]}


# --------------------------------------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------------------------------------


class TestOrderStats:
    def test_exact(self) -> None:
        assert order_stats([(3.0, False), (1.0, False), (2.0, False)]) == {"median": 2.0, "min": 1.0, "max": 3.0}

    def test_even_count_takes_the_mean_of_the_middle(self) -> None:
        assert order_stats([(1.0, False), (2.0, False), (3.0, False), (4.0, False)])["median"] == 2.5

    def test_censored_value_above_the_median_leaves_it_exact(self) -> None:
        stats = order_stats([(5.0, False), (6.0, False), (100.0, True)])
        assert (stats["median"], stats["min"], stats["max"]) == (6.0, 5.0, 100.0)
        assert stats["lower_bounds"] == ["max"]

    def test_censored_value_below_the_median_makes_it_a_lower_bound(self) -> None:
        stats = order_stats([(5.0, False), (10.0, False), (3.0, True)])
        assert (stats["median"], stats["min"]) == (5.0, 3.0)
        assert stats["lower_bounds"] == ["median", "min", "max"]


class TestSeparation:
    def test_p_value_of_complete_separation(self) -> None:
        assert separation_p(3, 3) == pytest.approx(1 / 20)
        assert separation_p(2, 2) == pytest.approx(1 / 6)
        assert separation_p(1, 19) == pytest.approx(1 / 20)

    def test_three_against_three(self) -> None:
        a = [(20.0, False), (19.9, False), (19.95, False)]
        b = [(9.4, False), (9.5, False), (9.3, False)]
        assert separated(a, b, higher_is_better=True)
        assert not separated(b, a, higher_is_better=True)
        assert separated(b, a, higher_is_better=False)

    def test_too_few_runs_never_separate(self) -> None:
        assert not separated([(20.0, False), (20.0, False)], [(1.0, False), (1.0, False)], higher_is_better=True)

    def test_overlap_and_ties_do_not_separate(self) -> None:
        a = [(10.0, False), (11.0, False), (12.0, False)]
        assert not separated(a, [(9.0, False), (9.5, False), (10.5, False)], higher_is_better=True)
        assert not separated(a, [(8.0, False), (9.0, False), (10.0, False)], higher_is_better=True)

    def test_censored_values_are_lower_bounds(self) -> None:
        exact = [(30.0, False), (31.0, False), (32.0, False)]
        censored = [(100.0, True), (120.0, False), (110.0, False)]
        # Lower is better: the exact arm recovers faster than one that has a lower bound of 100 s.
        assert separated(exact, censored, higher_is_better=False)
        # A censored value in the arm that must be lower can never be shown to be lower.
        assert not separated(censored, exact, higher_is_better=False)
        assert separated(censored, exact, higher_is_better=True)
        assert not separated(exact, [(100.0, False), (31.5, True), (110.0, False)], higher_is_better=False)


class TestCompare:
    def test_better_with_ratio_interval(self) -> None:
        result = compare([(20.0, False)] * 3, [(10.0, False), (8.0, False), (9.0, False)], higher_is_better=True)
        assert result["verdict"] == "better"
        assert result["ratio"] == pytest.approx(20 / 9)
        assert (result["ratio_low"], result["ratio_high"]) == (pytest.approx(2.0), pytest.approx(2.5))

    def test_worse_and_undecided(self) -> None:
        low = [(1.0, False), (2.0, False), (3.0, False)]
        high = [(4.0, False), (5.0, False), (6.0, False)]
        assert compare(low, high, higher_is_better=True)["verdict"] == "worse"
        assert compare(low, [(2.5, False), (4.0, False), (5.0, False)], higher_is_better=True)["verdict"] == "~"

    def test_no_ratio_for_censored_or_non_positive_values(self) -> None:
        assert "ratio" not in compare([(1.0, True)] * 3, [(2.0, False)] * 3, higher_is_better=False)
        assert "ratio" not in compare([(0.0, False)] * 3, [(2.0, False)] * 3, higher_is_better=False)


class TestAggregateMetric:
    def test_scalar(self) -> None:
        agg = aggregate_metric([{"value": v, "unit": "tps", "availability": "measured"} for v in (19.9, 20.0, 19.95)])
        assert (agg["n"], agg["median"], agg["min"], agg["max"], agg["unit"]) == (3, 19.95, 19.9, 20.0, "tps")
        assert agg["availability"] == "measured" and "censored" not in agg

    def test_partly_unavailable(self) -> None:
        agg = aggregate_metric(
            [
                {"value": 1.0, "availability": "measured"},
                {"value": None, "availability": "unavailable", "reason": "sampler died"},
                None,
            ]
        )
        assert agg["values"] == [1.0, None, None] and agg["n"] == 1
        assert agg["reasons"] == ["not computed", "sampler died"]

    def test_not_applicable_everywhere(self) -> None:
        entry = {"value": None, "availability": "not_applicable", "reason": "no tick thread"}
        agg = aggregate_metric([entry, entry])
        assert agg["availability"] == "not_applicable" and agg["n"] == 0 and "median" not in agg

    def test_censored_flags_follow_the_values(self) -> None:
        agg = aggregate_metric(
            [
                {"value": 410.0, "availability": "measured", "censored": True},
                {"value": 30.0, "availability": "measured"},
            ]
        )
        assert agg["censored"] == [True, False]
        assert agg["lower_bounds"] == ["median", "max"]

    def test_distribution_parts(self) -> None:
        agg = aggregate_metric(
            [
                {"value": {"median": 80.0, "p90": 120.0, "n": 100}, "availability": "measured"},
                {"value": {"median": 90.0, "p90": 130.0, "n": 100}, "availability": "measured"},
            ]
        )
        assert set(agg["parts"]) == {"median", "n", "p90"}
        assert agg["parts"]["median"]["median"] == 85.0 and agg["parts"]["p90"]["max"] == 130.0

    def test_booleans(self) -> None:
        same = aggregate_metric([{"value": True, "availability": "measured"}] * 2)
        assert same["value"] is True and "median" not in same
        mixed = aggregate_metric(
            [{"value": True, "availability": "measured"}, {"value": False, "availability": "measured"}]
        )
        assert mixed["value"] is None and mixed["values"] == [True, False]


# --------------------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------------------


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestLoadRuns:
    def test_status_sources(self, tmp_path: Path) -> None:
        analysed = run_doc("bds", "bds", "1.0", status="server_failed")
        _write(tmp_path / "a" / "run.json", analysed)
        _write(tmp_path / "a" / "metrics.json", metrics_doc("ok"))
        _write(tmp_path / "b" / "run.json", run_doc("bds", "bds", "1.0", repeat=2))
        _write(tmp_path / "c" / "run.partial.json", {**run_doc("bds", "bds", "1.0", repeat=3), "status": None})
        (tmp_path / "d").mkdir()
        (tmp_path / "summary.json").write_text("{}", encoding="utf-8")

        records = {r.repeat: r for r in load_runs(tmp_path)}
        assert sorted(records) == [1, 2, 3]
        assert records[1].status == "ok"  # metrics.json is recomputed by bbench analyze and wins
        assert records[2].status == "invalid" and "metrics.json missing" in records[2].reasons[0]
        assert records[3].status == "invalid" and "did not finish" in records[3].reasons[0]

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


def _matrix() -> list:
    runs = []
    for repeat, (bds, e13, e14) in enumerate(((9.4, 11.0, 20.0), (9.5, 9.0, 19.9), (9.3, 10.2, 19.95)), start=1):
        runs.append(record("bds@1.26.52.3", "bds", "1.26.52.3", bds, repeat=repeat))
        runs.append(record("endstone@0.11.13", "endstone", "0.11.13", e13, repeat=repeat, bds="1.26.52.3"))
        runs.append(record("endstone@0.11.14", "endstone", "0.11.14", e14, repeat=repeat, bds="1.26.52.3"))
    return runs


class TestSummarize:
    def test_cells_and_counts(self) -> None:
        runs = _matrix()
        runs.append(
            record("endstone@0.11.14", "endstone", "0.11.14", 1.0, repeat=4, bds="1.26.52.3", status="server_failed")
        )
        summary = summarize(runs, features=None)
        assert summary["matrix"] == "m" and summary["n_runs"] == 10
        assert summary["status_counts"] == {"ok": 9, "server_failed": 1, "invalid": 0}
        by_arm = cells(summary)
        assert list(by_arm) == ["bds@1.26.52.3", "endstone@0.11.13", "endstone@0.11.14"]
        e14 = by_arm["endstone@0.11.14"]
        assert (e14["n_total"], e14["n_ok"], e14["n_server_failed"]) == (4, 3, 1)
        assert e14["metrics"]["tps_window_steady"]["values"] == [20.0, 19.9, 19.95]  # failed run left out
        assert e14["metrics"]["tps_window_steady"]["median"] == 19.95
        assert statuses(summary).count("server_failed") == 1

    def test_same_build_baseline(self) -> None:
        runs = [*_matrix(), record("levi", "levilamina", "1.0", bds="1.26.51.1")]
        by_arm = cells(summarize(runs, features=None))
        assert by_arm["endstone@0.11.14"]["baseline"] == "bds@1.26.52.3"
        assert by_arm["bds@1.26.52.3"]["baseline"] is None
        assert by_arm["levi"]["baseline"] is None and by_arm["levi"]["flags"] == {"different_bds_build": 1}

    def test_fidelity_sections(self) -> None:
        reduced = {"player.server_authoritative_movement": "absent"}
        runs = [record("bds", "bds", "1.0"), record("pmmp", "pocketmine", "5.0", row=reduced)]
        by_arm = cells(summarize(runs, features=None))
        assert by_arm["bds"]["parity"]["full_fidelity"] is True
        assert by_arm["pmmp"]["parity"]["full_fidelity"] is False
        assert by_arm["pmmp"]["parity"]["does_less"] == [
            {"feature": "player.server_authoritative_movement", "class": "absent"},
            {"feature": "terrain.vanilla_generator", "class": "unknown"},
        ]

    def test_cross_server_tables(self) -> None:
        assert summarize(_matrix(), features=None)["scenarios"][0]["cross_server"] is False
        mixed = [record("bds", "bds", "1.0"), record("pmmp", "pocketmine", "5.0")]
        assert summarize(mixed, features=None)["scenarios"][0]["cross_server"] is True

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"scenario_sha": "c" * 64}, "different scenario files"),
            ({"profile": "d" * 64}, "different machine profiles"),
            ({"fingerprint": "m2"}, "different machines"),
        ],
    )
    def test_refuses_mixed_tables(self, kwargs: dict[str, Any], message: str) -> None:
        runs = [record("a", "bds", "1.0"), record("b", "bds", "1.1", **kwargs)]
        with pytest.raises(ReportError, match=message):
            summarize(runs, features=None)

    def test_refuses_mixed_analysis_versions(self) -> None:
        newer = metrics_doc(tps_window_steady=20.0) | {"analysis_version": 2}
        runs = [record("a", "bds", "1.0"), make_record(run_doc("b", "bds", "1.1"), newer)]
        with pytest.raises(ReportError, match="analysis versions"):
            summarize(runs, features=None)

    def test_refuses_mixed_matrices(self) -> None:
        other = run_doc("b", "bds", "1.1")
        other["matrix"] = {**other["matrix"], "name": "other"}
        with pytest.raises(ReportError, match="different matrices"):
            summarize([record("a", "bds", "1.0"), make_record(other, metrics_doc())], features=None)

    def test_deterministic_and_path_free(self, tmp_path: Path) -> None:
        first = tmp_path / "one.json"
        second = tmp_path / "two.json"
        write_summary(summarize(_matrix(), features=None), first)
        write_summary(summarize(list(reversed(_matrix())), features=None), second)
        assert first.read_bytes() == second.read_bytes()
        assert str(tmp_path) not in first.read_text(encoding="utf-8")


class TestObservedParity:
    def features(self, sha: str) -> FeatureFile:
        cell = FeatureCell(
            key="player.server_authoritative_movement",
            cls=FeatureClass.VANILLA,
            observed_by="corrections_per_bot_s",
        )
        rng = FeatureRange(prefix="*", fidelity=Fidelity.V0, cells={cell.key: cell})
        return FeatureFile(server="bds", path=Path("bds.toml"), sha256=sha, ranges=(rng,))

    def runs(self, corrections: float) -> list:
        return [
            make_record(run_doc("bds", "bds", "1.0", repeat=k), metrics_doc(corrections_per_bot_s=corrections))
            for k in (1, 2)
        ]

    def test_declared_vanilla_but_nothing_observed(self) -> None:
        summary = summarize(self.runs(0.0), features=lambda server: self.features("f" * 64))
        cell = cells(summary)["bds"]
        observed = cell["observed"]["player.server_authoritative_movement"]
        assert observed == {"metric": "corrections_per_bot_s", "declared": "vanilla", "median": 0.0, "mismatches": 2}
        assert cell["flags"] == {"parity_mismatch:player.server_authoritative_movement": 2}

    def test_agreement(self) -> None:
        cell = cells(summarize(self.runs(0.8), features=lambda server: self.features("f" * 64)))["bds"]
        assert cell["observed"]["player.server_authoritative_movement"]["mismatches"] == 0
        assert cell["flags"] == {}

    def test_changed_feature_file_is_not_used(self) -> None:
        cell = cells(summarize(self.runs(0.0), features=lambda server: self.features("0" * 64)))["bds"]
        assert cell["observed"] == {} and cell["flags"] == {}


class TestPublish:
    def test_clean_matrix_passes(self) -> None:
        assert publish_problems(summarize(_matrix(), features=None)) == []

    def test_problems(self) -> None:
        runs = [
            record("few", "bds", "1.0"),
            *(record("dead", "bds", "1.1", status="server_failed", repeat=k) for k in (1, 2, 3)),
            *(record("local", "bds", "1.2", repeat=k) for k in (1, 2, 3)),
        ]
        local = [run_doc("local2", "bds", "1.3", repeat=k, official=False, nonstandard=["dirty"]) for k in (1, 2, 3)]
        for doc in local:
            doc["arm"]["artifact"] = "bedrock-server.zip"
        runs += [make_record(doc, metrics_doc()) for doc in local]
        unknown = [run_doc("unk", "bds", "1.4", repeat=k, row={}) for k in (1, 2, 3)]
        runs += [make_record(doc, metrics_doc()) for doc in unknown]
        problems = publish_problems(summarize(runs, features=None))
        text = "\n".join(problems)
        assert "spread100 / few: 1 ok runs" in text
        assert "dead" not in text  # a cell where every run failed is a publishable result
        assert "local2: ran a local artifact" in text
        assert "local2: includes runs marked not official (dirty)" in text
        assert "unk: parity unknown for exercised features" in text
        assert "spread100 / local:" not in text

    def test_cross_server_needs_equal_chunks(self) -> None:
        def table(csg: bool) -> list:
            return [
                record(arm, server, "1.0", repeat=k, csg=csg)
                for arm, server in (("bds", "bds"), ("pmmp", "pocketmine"))
                for k in (1, 2, 3)
            ]

        assert any("equal-chunk" in p for p in publish_problems(summarize(table(True), features=None)))
        assert publish_problems(summarize(table(False), features=None)) == []


def test_summary_is_canonical_json() -> None:
    summary = summarize(_matrix(), features=None)
    assert json.loads(canonical_json(summary)) == json.loads(json.dumps(summary))
