# SPDX-License-Identifier: Apache-2.0
"""Claims: the expression whitelist, verdicts, and loading a claims file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import config
from bedrock_bench.config import ConfigError
from bedrock_bench.model import Claim, ClaimSet
from bedrock_bench.report.aggregate import make_record, summarize
from bedrock_bench.report.claims import check_claims, evaluate, evaluate_claim, parse_expression

E14 = "endstone@0.11.14"
BDS = "bds@1.26.52.3"


def _entry(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "availability" in value:
        return {"unit": "x", "value": None, **value}
    if isinstance(value, tuple):
        return {"unit": "s", "availability": "measured", "value": value[0], "censored": value[1]}
    return {"unit": "x", "availability": "measured", "value": value}


def _record(arm: str, repeat: int, metrics: dict[str, Any], *, scenario: str, matrix: str, label: str):
    run = {
        "run_id": f"20261012T120000Z-{arm}-{scenario}-r{repeat}-000{repeat}",
        "status": "ok",
        "matrix": {"name": matrix, "repeat": repeat},
        "arm": {"id": arm, "server": arm.split("@")[0], "version": arm.split("@", 1)[1]},
        "scenario": {"name": scenario, "sha256": "b" * 64},
        "machine": {"fingerprint_id": "m", "profile_sha256": "a" * 64, "label": label},
    }
    doc = {"analysis_version": 1, "status": "ok", "metrics": {k: _entry(v) for k, v in metrics.items()}}
    return make_record(run, doc)


def summary(
    *,
    label: str = "reference",
    bds_tps: tuple[float, ...] = (12.0, 12.2, 11.8),
    matrix: str = "endstone-0.11.14",
    scenario: str = "spread100",
) -> dict[str, Any]:
    runs = []
    for k, (e14_tps, tick, bds) in enumerate(zip((19.6, 19.5, 19.7), (45.6, 44.8, 45.2), bds_tps, strict=True)):
        runs.append(
            _record(
                E14,
                k + 1,
                {
                    "tps_window_steady": e14_tps,
                    "tick_cpu_ms_per_tick_steady": tick,
                    "view_t90_own_s": {"availability": "measured", "value": {"median": 110.0 + k, "p90": 160.0}},
                    "recovery_s": 30.0 + k,
                    "private_mib_steady": {"availability": "unavailable", "reason": "sampler died"},
                },
                scenario=scenario,
                matrix=matrix,
                label=label,
            )
        )
        runs.append(
            _record(
                BDS,
                k + 1,
                {"tps_window_steady": bds, "tick_cpu_ms_per_tick_steady": 82.0, "recovery_s": (400.0 + k, True)},
                scenario=scenario,
                matrix=matrix,
                label=label,
            )
        )
    return summarize(runs, features=None)


def claim(expr: str, **kwargs: Any) -> Claim:
    return Claim(id="c", text="a claim", expr=expr, **kwargs)


# --------------------------------------------------------------------------------------------------------------------
# Whitelist
# --------------------------------------------------------------------------------------------------------------------


class TestParseExpression:
    @pytest.mark.parametrize(
        "expr",
        [
            "median('a', 'tps_window_steady') >= 19.4",
            "ratio('a', 'b', 'tps_window_steady') >= 1.5",
            "abs(median('a', 'm') / 40 - 1) <= 0.15",
            "0.85 <= low('a', 'm') < high('a', 'm') != 3",
            "not above('a', 'b', 'm') or below('a', 'b', 'm') and n_ok('a') == 3",
            "max(median('a', 'm'), 1, -2) - min(1.5, +2) * 2 > 0",
            "median('a', 'view_t90_own_s.p90') < 120",
            "True",
        ],
    )
    def test_accepts(self, expr: str) -> None:
        parse_expression(expr)

    @pytest.mark.parametrize(
        ("expr", "message"),
        [
            ("().__class__", "attribute access"),
            ("x > 1", "name 'x'"),
            ("eval('1')", "function 'eval'"),
            ("__import__('os').system('true')", "attribute access"),
            ("open('f')", "function 'open'"),
            ("(lambda: 1)()", "Lambda"),
            ("[1][0]", "Subscript"),
            ("[x for x in ()]", "ListComp"),
            ("median(arm='a', metric='m')", "keyword arguments to median()"),
            ("median(1, 'm') > 0", "must be a string literal"),
            ("median('a' + 'b', 'm') > 0", "must be a string literal"),
            ("median('a') > 0", "takes 2 arguments"),
            ("max() > 0", "at least one argument"),
            ("2 ** 10 > 0", "operator Pow"),
            ("3 % 2 == 1", "operator Mod"),
            ("1 if True else 2", "IfExp"),
            ("None == None", "constant None"),
            ("1 in (1,)", "operator In"),
            ("'a' is 'a'", "operator Is"),
            ("f'{1}' == '1'", "JoinedStr"),
            ("max(*(1, 2)) > 0", "Starred"),
            ("1 >", "not a valid expression"),
        ],
    )
    def test_rejects(self, expr: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_expression(expr)


# --------------------------------------------------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------------------------------------------------


class TestEvaluateClaim:
    def test_pass_and_fail_record_values(self) -> None:
        result = evaluate_claim(claim(f"median('{E14}', 'tps_window_steady') >= 19.4"), summary())
        assert result["verdict"] == "pass" and result["reason"] == "" and result["scenario"] == "spread100"
        assert result["values"] == {f"median('{E14}', 'tps_window_steady')": 19.6}
        assert evaluate_claim(claim(f"high('{BDS}', 'tps_window_steady') >= 19.4"), summary())["verdict"] == "fail"

    def test_relational_functions(self) -> None:
        s = summary()
        for expr in (
            f"ratio('{E14}', '{BDS}', 'tps_window_steady') > 1.6",
            f"above('{E14}', '{BDS}', 'tps_window_steady')",
            f"below('{E14}', '{BDS}', 'tick_cpu_ms_per_tick_steady')",
            f"not above('{BDS}', '{E14}', 'tps_window_steady')",
            f"low('{E14}', 'tps_window_steady') == 19.5 and n_ok('{E14}') == 3",
            f"median('{E14}', 'view_t90_own_s.median') == 111",
            f"below('{E14}', '{BDS}', 'recovery_s')",
        ):
            assert evaluate_claim(claim(expr), s)["verdict"] == "pass", expr

    def test_inconclusive_if(self) -> None:
        expr = f"ratio('{E14}', '{BDS}', 'tps_window_steady') >= 1.5"
        gate = f"median('{BDS}', 'tps_window_steady') >= 19.5"
        assert evaluate_claim(claim(expr, inconclusive_if=gate), summary())["verdict"] == "pass"
        fast = evaluate_claim(claim(expr, inconclusive_if=gate), summary(bds_tps=(19.9, 20.0, 19.8)))
        assert fast["verdict"] == "inconclusive" and fast["reason"].startswith("inconclusive_if holds")
        assert fast["values"][f"median('{BDS}', 'tps_window_steady')"] == 19.9

    def test_machine_scope(self) -> None:
        expr = f"median('{E14}', 'tps_window_steady') >= 19.4"
        assert evaluate_claim(claim(expr, machine="reference"), summary())["verdict"] == "pass"
        other = evaluate_claim(claim(expr, machine="reference"), summary(label="laptop"))
        assert other["verdict"] == "inconclusive" and "'laptop'" in other["reason"]

    @pytest.mark.parametrize(
        ("expr", "reason"),
        [
            ("median('nobody', 'tps_window_steady') > 1", "no runs of arm 'nobody'"),
            (f"median('{E14}', 'unknown_metric_s') > 1", "unknown_metric_s is not recorded"),
            (f"median('{E14}', 'private_mib_steady') > 1", "not measured for endstone@0.11.14 (sampler died)"),
            (f"median('{BDS}', 'recovery_s') > 1", "is censored (>401)"),
            (f"1 / (n_ok('{E14}') - 3) > 0", "division by zero"),
        ],
    )
    def test_undecided_data_is_inconclusive(self, expr: str, reason: str) -> None:
        result = evaluate_claim(claim(expr), summary())
        assert result["verdict"] == "inconclusive"
        assert reason in result["reason"]

    @pytest.mark.parametrize(
        ("expr", "reason"),
        [
            (f"median('{E14}', 'tps_window_steady')", "does not evaluate to true or false"),
            (f"median('{E14}', 'tps_window_steady') < 'a'", "not supported"),
            (f"median('{E14}', 'view_t90_own_s') > 1", "is a distribution"),
            (f"median('{E14}', 'view_t90_own_s.p99') > 1", "has no part 'p99'"),
        ],
    )
    def test_broken_claims_are_errors(self, expr: str, reason: str) -> None:
        result = evaluate_claim(claim(expr), summary())
        assert result["verdict"] == "error"
        assert reason in result["reason"]

    def test_scenario_selection(self) -> None:
        s = summary()
        s["scenarios"].append({**s["scenarios"][0], "name": "chaos"})
        assert evaluate_claim(claim("1 < 2"), s)["verdict"] == "error"
        assert evaluate_claim(claim("1 < 2", scenario="chaos"), s)["verdict"] == "pass"
        missing = evaluate_claim(claim("1 < 2", scenario="spread100-pregen"), s)
        assert missing["verdict"] == "inconclusive" and "no runs in this matrix directory" in missing["reason"]


class TestEvaluate:
    def claim_set(self, *claims: Claim, matrix: str = "endstone-0.11.14") -> ClaimSet:
        return ClaimSet(matrix=matrix, path=Path("x.toml"), sha256="0" * 64, claims=claims)

    def test_counts(self) -> None:
        claims = self.claim_set(
            claim(f"median('{E14}', 'tps_window_steady') >= 19.4"),
            Claim(id="d", text="t", expr=f"median('{BDS}', 'tps_window_steady') >= 19.4"),
        )
        result = evaluate(claims, summary())
        assert result["counts"] == {"pass": 1, "fail": 1, "inconclusive": 0, "error": 0}
        assert result["file"] == "x.toml" and [r["id"] for r in result["results"]] == ["c", "d"]

    def test_wrong_matrix(self) -> None:
        with pytest.raises(ConfigError, match="claims are for matrix 'other'"):
            evaluate(self.claim_set(claim("1 < 2"), matrix="other"), summary())

    def test_disallowed_expression_names_the_claim(self) -> None:
        bad = self.claim_set(claim("1 < 2", inconclusive_if="__import__('os')"))
        with pytest.raises(ConfigError, match=r"claim\[0\]\.inconclusive_if: function '__import__'"):
            check_claims(bad)


# --------------------------------------------------------------------------------------------------------------------
# Claims files
# --------------------------------------------------------------------------------------------------------------------

FIXTURE = config.project_root() / "tests" / "fixtures" / "claims.toml"


def test_fixture_against_a_synthetic_matrix() -> None:
    claims = config.load_claims(FIXTURE)
    check_claims(claims)
    where = {"matrix": "fixture", "scenario": "spread100-csg"}
    verdicts = {r["id"]: r["verdict"] for r in evaluate(claims, summary(**where))["results"]}
    assert verdicts == {
        "endstone-steady-tps": "pass",
        "endstone-vs-vanilla-tps": "pass",
        "endstone-tick-cpu": "pass",
    }
    elsewhere = {r["id"]: r["verdict"] for r in evaluate(claims, summary(label="laptop", **where))["results"]}
    assert elsewhere["endstone-tick-cpu"] == "inconclusive"


def test_claims_are_loaded_by_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="no such claims file"):
        config.load_claims("fixture")
