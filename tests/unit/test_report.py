# SPDX-License-Identifier: Apache-2.0
"""The markdown report and ``bbench report`` end to end, on schema-valid synthetic run directories."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import cli
from bedrock_bench.config import project_root
from bedrock_bench.report import REPORT_FILE, SUMMARY_FILE, build, write
from bedrock_bench.report.markdown import (
    CHAOS_ROWS,
    COST_ROWS,
    EFFICIENCY_ROWS,
    WORK_ROWS,
    format_number,
    format_stat,
    layout_metric_ids,
    render,
)

FIXTURES = project_root() / "tests" / "fixtures"
MATRIX = "fixture"
BASE_RUN = json.loads((FIXTURES / "run.ok.json").read_text(encoding="utf-8"))
EXERCISES = BASE_RUN["scenario"]["resolved"]["exercises"]
CLAIMS = FIXTURES / "claims.toml"

RESCUE = {
    "category": "rescue",
    "file": "pocketmine.yml",
    "key": "memory.main-limit",
    "default": "1024",
    "value": "0",
    "reason": "the default limit crashes the server at about 23 s",
}


def metric(value: Any, unit: str, window: str = "steady", **extra: Any) -> dict[str, Any]:
    return {"value": value, "unit": unit, "window": window, "n": None, "availability": "measured", **extra}


def write_run(
    matrix_dir: Path,
    arm: str,
    repeat: int,
    metrics: dict[str, Any],
    *,
    scenario: str = "spread100-csg",
    bds: str | None = None,
    status: str = "ok",
    reasons: Sequence[str] = (),
    row: dict[str, str] | None = None,
    fidelity: str = "V0",
    overrides: Sequence[dict[str, Any]] = (),
    flags: Sequence[str] = (),
    profile: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    server, _, version = arm.partition("@")
    digest = hashlib.sha256(f"{arm}/{scenario}/{repeat}".encode()).hexdigest()
    run_id = f"20261012T1200{repeat:02d}Z-{arm}-{scenario}-r{repeat}-{digest[:4]}"
    run = copy.deepcopy(BASE_RUN)
    run.update(
        run_id=run_id,
        status=status,
        status_reasons=list(reasons),
        flags=list(flags),
        exit_code={"ok": 0, "server_failed": 4, "invalid": 5}[status],
        overrides=run["overrides"] + list(overrides),
    )
    run["matrix"].update(name=MATRIX, repeat=repeat, order_index=repeat)
    run["arm"].update(id=arm, server=server, version=version, bds=bds, python="3.13" if bds else None)
    run["server"]["banner_version"] = version
    resolved = run["scenario"]["resolved"]
    resolved["name"] = scenario
    resolved["server"]["csg"] = scenario == "spread100-csg"
    run["scenario"].update(name=scenario, sha256=hashlib.sha256(scenario.encode()).hexdigest())
    run["parity"].update(row=dict(row or dict.fromkeys(EXERCISES, "vanilla")), fidelity=fidelity)
    if profile:
        run["machine"]["profile_sha256"] = profile
    doc = {
        "schema": "bedrock-bench/metrics/1",
        "analysis_version": 1,
        "run_id": run_id,
        "status": status,
        "status_reasons": list(reasons),
        "flags": list(flags),
        "metrics": metrics,
    }
    run_dir = matrix_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return run, doc


def headline(tps: float, tick: float, cores: float, private: float, t90: float, **extra: Any) -> dict[str, Any]:
    return {
        "tps_window_steady": metric(tps, "tps"),
        "tick_cpu_ms_per_tick_steady": metric(tick, "ms"),
        "process_cores_steady": metric(cores, "cores"),
        "private_mib_steady": metric(private, "mib"),
        "view_t90_own_s": metric({"median": t90, "p90": t90 * 1.5, "max": t90 * 2}, "s", "load"),
        "corrections_per_bot_s": metric(0.85, "per_bot_s"),
        "csg_effective": metric(True, "bool", "load"),
        **extra,
    }


def endstone_matrix(matrix_dir: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """BDS 1.26.52.3 and two Endstone builds hosting it, three repeats each, spread100-csg."""
    docs = []
    for k, (bds, e13, e14) in enumerate(((9.4, 11.0, 20.0), (9.5, 9.0, 19.9), (9.3, 10.2, 19.95)), start=1):
        recovery = metric(400.0 + k, "s", "load", censored=True)
        docs.append(
            write_run(
                matrix_dir,
                "bds@1.26.52.3",
                k,
                headline(bds, 105.0 + k, 1.07, 4198.4, 192.0, recovery_s=recovery),
            )
        )
        docs.append(
            write_run(
                matrix_dir,
                "endstone@0.11.13",
                k,
                headline(e13, 89.0 + k, 1.81, 5017.6, 189.0),
                bds="1.26.52.3",
                fidelity="V1",
                flags=["known_issue:devtools_spin"],
            )
        )
        docs.append(
            write_run(
                matrix_dir,
                "endstone@0.11.14",
                k,
                headline(e14, 42.0 + k * 0.5, 1.04, 5017.6, 81.0, recovery_s=metric(30.0 + k, "s", "load")),
                bds="1.26.52.3",
                fidelity="V1",
            )
        )
    return docs


def cross_server_scenario(matrix_dir: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """spread100-pregen: vanilla BDS and a server that does less, with one failed run and a rescue override."""
    docs = []
    reduced = dict.fromkeys(EXERCISES, "vanilla") | {
        "terrain.vanilla_generator": "absent",
        "player.server_authoritative_movement": "absent",
        "player.physics": "absent",
    }
    no_tick = {
        "value": None,
        "unit": "ms",
        "window": "steady",
        "availability": "not_applicable",
        "reason": "no tick thread",
    }
    for k in (1, 2, 3):
        docs.append(
            write_run(
                matrix_dir, "bds@1.26.52.3", k, headline(9.0, 110.0, 1.1, 4300.0, 300.0), scenario="spread100-pregen"
            )
        )
        failed = k == 3
        docs.append(
            write_run(
                matrix_dir,
                "pocketmine@5.44.3",
                k,
                {} if failed else headline(4.6, 0.0, 1.07, 6348.8, 400.0, tick_cpu_ms_per_tick_steady=no_tick),
                scenario="spread100-pregen",
                status="server_failed" if failed else "ok",
                reasons=["server_alive: exited with code 255"] if failed else (),
                row=reduced,
                fidelity="V2",
                overrides=[RESCUE],
            )
        )
    return docs


@pytest.fixture
def matrix_dir(tmp_path: Path) -> Path:
    path = tmp_path / MATRIX
    endstone_matrix(path)
    cross_server_scenario(path)
    return path


@pytest.fixture
def clean_dir(tmp_path: Path) -> Path:
    path = tmp_path / "clean" / MATRIX
    endstone_matrix(path)
    return path


def table_row(markdown: str, label: str) -> list[str]:
    """Cells of the first table row whose label starts with ``label``."""
    for line in markdown.splitlines():
        if line.startswith(f"| {label}"):
            return [c.strip() for c in line.strip("|").split(" | ")]
    raise AssertionError(f"no row {label!r}")


# --------------------------------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------------------------------


def test_synthetic_runs_are_schema_valid(tmp_path: Path, schema_validator: Any) -> None:
    docs = endstone_matrix(tmp_path / "a") + cross_server_scenario(tmp_path / "a")
    for run, metrics in docs:
        assert list(schema_validator("run").iter_errors(run)) == [], run["run_id"]
        assert list(schema_validator("metrics").iter_errors(metrics)) == [], run["run_id"]


# --------------------------------------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------------------------------------


class TestRender:
    @pytest.fixture
    def markdown(self, matrix_dir: Path) -> str:
        _, text, _ = build(matrix_dir, claims=CLAIMS, features=None)
        return text

    def test_sections(self, markdown: str) -> None:
        assert markdown.startswith(f"# Matrix `{MATRIX}`\n")
        assert "15 runs: 14 ok, 1 server_failed, 0 invalid." in markdown
        spread, equal = markdown.split("## Scenario `spread100-csg`")[1].split("## Scenario `spread100-pregen`")
        assert "### Full vanilla fidelity" in spread and "### Reduced fidelity" not in spread
        assert "### Full vanilla fidelity" in equal and "### Reduced fidelity" in equal
        reduced = equal.split("### Reduced fidelity")[1]
        assert (
            "- `pocketmine@5.44.3`: `terrain.vanilla_generator` (absent), "
            "`player.server_authoritative_movement` (absent), `player.physics` (absent)"
        ) in reduced
        assert "Warning:** this table compares different servers" not in markdown

    def test_conditions(self, markdown: str) -> None:
        assert "- Machine: `reference`, Intel(R) Core(TM) i7-14700F, 28 logical / 20 physical CPUs" in markdown
        assert "server on CPUs 0-15, bots on 16-27, harness on 16-27, priority above_normal, physical cores" in markdown
        assert "- Bot: loadbot, protocol 2193, gophertunnel v1.62.0, no patches, sha256 `000000000000`." in markdown

    def test_cost_values(self, markdown: str) -> None:
        assert table_row(markdown, "Steady TPS")[1:4] == [
            "9.40 [9.30, 9.50]",
            "10.20 [9.00, 11.00]",
            "19.95 [19.90, 20.00]",
        ]
        assert table_row(markdown, "Private memory, steady")[1:4] == [
            "4.10 GiB [4.10, 4.10]",
            "4.90 GiB [4.90, 4.90]",
            "4.90 GiB [4.90, 4.90]",
        ]
        assert table_row(markdown, "View load t90 of own view")[1] == "192 s [192, 192]"
        assert re.match(r"^>402 s \[>401, >403\]$", table_row(markdown, "TPS recovery after the join")[1])

    def test_difference_rule(self, markdown: str) -> None:
        differences = markdown.split("#### Differences from the hosted vanilla build")[1].split("\n## ")[0]
        header = table_row(differences, " | `endstone@0.11.13` vs")
        assert header[1:] == ["`endstone@0.11.13` vs `bds@1.26.52.3`", "`endstone@0.11.14` vs `bds@1.26.52.3`"]
        assert table_row(differences, "Steady TPS")[1:] == ["~, 1.09x [0.95, 1.18]", "better, 2.12x [2.09, 2.15]"]
        assert table_row(differences, "Tick-thread CPU per tick")[2] == "better, 0.40x [0.39, 0.41]"
        assert table_row(differences, "TPS recovery after the join")[1:] == ["-", "better"]

    def test_failed_runs_and_not_applicable(self, markdown: str) -> None:
        equal = markdown.split("## Scenario `spread100-pregen`")[1]
        full, reduced = equal.split("### Reduced fidelity")
        assert table_row(full, "Runs ok")[1] == "3/3"
        assert re.fullmatch(r"2/3\[\^\d+\]", table_row(reduced, "Runs ok")[1])
        assert "server_failed in 1 of 3 runs (run 3: server_alive: exited with code 255)" in markdown
        # A row that applies to no arm of a table is left out.
        assert "| Tick-thread CPU per tick" not in reduced

    def test_not_applicable_next_to_measured(self, matrix_dir: Path) -> None:
        summary, _, _ = build(matrix_dir, features=None)
        scenario = next(s for s in summary["scenarios"] if s["name"] == "spread100-csg")
        cell = next(c for c in scenario["cells"] if c["arm"]["id"] == "endstone@0.11.13")
        cell["metrics"]["tick_cpu_ms_per_tick_steady"] = {
            "unit": "ms",
            "window": "steady",
            "values": [None, None, None],
            "n": 0,
            "availability": "not_applicable",
            "reasons": ["no tick thread"],
        }
        markdown = render(summary)
        assert re.fullmatch(r"n/a\[\^\d+\]", table_row(markdown, "Tick-thread CPU per tick")[2])
        assert "Tick-thread CPU per tick, steady: not applicable (no tick thread)." in markdown

    def test_footnotes(self, markdown: str) -> None:
        refs = set(re.findall(r"\[\^(\d+)\](?!:)", markdown))
        definitions = re.findall(r"^\[\^(\d+)\]: ", markdown, flags=re.MULTILINE)
        assert refs == set(definitions) and len(definitions) == len(set(definitions))
        assert definitions == [str(i) for i in range(1, len(definitions) + 1)]
        assert "Rescue override: `pocketmine.yml` `memory.main-limit` = `0` (default `1024`)" in markdown
        assert "Known issue `devtools_spin` affects this build; see docs/servers/endstone.md." in markdown
        assert "It is not MSPT" in markdown

    def test_parity_and_work(self, markdown: str) -> None:
        assert table_row(markdown, "`player.physics`")[1:] == ["vanilla", "vanilla", "vanilla"]
        assert table_row(markdown, "Client-side chunk generation in effect")[1:] == ["yes", "yes", "yes"]
        assert table_row(markdown, "Overrides")[1] == "harness 1, parity 1"

    def test_claims(self, markdown: str) -> None:
        claims = markdown.split("## Claims")[1]
        assert "From `claims.toml`: 3 pass, 0 fail, 0 inconclusive, 0 error." in claims
        assert table_row(claims, "endstone@0.11.14 holds at least 19 TPS")[1] == "pass"

    def test_ascii(self, markdown: str) -> None:
        markdown.encode("ascii")


class TestFormatting:
    @pytest.mark.parametrize(
        ("value", "unit", "text"),
        [
            (19.951, "tps", "19.95"),
            (5017.6, "gib", "4.90"),
            (0.953, "pct", "95"),
            (40.94, "ms", "40.9"),
            (105.7, "ms", "106"),
            (0.5204, "ms", "0.520"),
            (0.0, "rate", "0"),
            (1.044, "cores", "1.04"),
            (12.4, "count", "12"),
        ],
    )
    def test_numbers(self, value: float, unit: str, text: str) -> None:
        assert format_number(value, unit) == text

    def test_stats(self) -> None:
        assert format_stat({"median": 81.0, "min": 78.0, "max": 85.0, "n": 3}, "s") == "81.0 s [78.0, 85.0]"
        assert format_stat({"median": 81.0, "min": 81.0, "max": 81.0, "n": 1}, "s") == "81.0 s"
        censored = {"median": 410.0, "min": 30.0, "max": 420.0, "n": 3, "lower_bounds": ["median", "max"]}
        assert format_stat(censored, "s") == ">410 s [30.0, >420]"
        assert format_stat({"values": [True, False], "value": None}, "bool") == "mixed (1 of 2 yes)"

    def test_layout_ids(self) -> None:
        ids = layout_metric_ids()
        assert ids == sorted(set(ids))
        assert {"tps_window_steady", "tick_cpu_ms_per_tick_steady", "corrections_per_bot_s"} <= set(ids)

    def test_layout_matches_the_metric_registry(self) -> None:
        registry = pytest.importorskip("bedrock_bench.metrics.registry")
        assert sorted(set(layout_metric_ids()) - set(registry.ids())) == []
        for row in (*COST_ROWS, *WORK_ROWS, *EFFICIENCY_ROWS, *CHAOS_ROWS):
            # A row may decline to rank a metric (chaos), never rank it the other way round.
            assert row.better in (None, registry.get(row.metric).direction), row.metric


# --------------------------------------------------------------------------------------------------------------------
# Files and CLI
# --------------------------------------------------------------------------------------------------------------------


class TestWrite:
    def test_files_are_deterministic_and_rerenderable(self, matrix_dir: Path) -> None:
        report = write(matrix_dir, claims=CLAIMS, publish=True, features=None)
        summary_bytes = (matrix_dir / SUMMARY_FILE).read_bytes()
        assert report.report_path == matrix_dir / REPORT_FILE
        write(matrix_dir, claims=CLAIMS, publish=True, features=None)
        assert (matrix_dir / SUMMARY_FILE).read_bytes() == summary_bytes
        summary = json.loads(summary_bytes)
        assert render(summary) == (matrix_dir / REPORT_FILE).read_text(encoding="utf-8")
        assert str(matrix_dir.parent) not in summary_bytes.decode("utf-8")
        assert summary["schema"] == "bedrock-bench/summary/1"

    def test_publish_refusal(self, matrix_dir: Path) -> None:
        report = write(matrix_dir, publish=True, features=None)
        assert report.problems == ("spread100-pregen / pocketmine@5.44.3: 2 ok runs, publication needs at least 3",)
        assert report.exit_code == 2
        assert "## Publication check\n\nRefused:\n- spread100-pregen / pocketmine@5.44.3" in report.markdown

    def test_publish_pass(self, clean_dir: Path) -> None:
        report = write(clean_dir, publish=True, features=None)
        assert report.problems == () and report.exit_code == 0
        assert "## Publication check\n\nPassed:" in report.markdown


class TestCli:
    def test_markdown_to_stdout(self, matrix_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", str(matrix_dir), "--claims", str(CLAIMS)]) == 4
        out, err = capsys.readouterr()
        assert out.startswith(f"# Matrix `{MATRIX}`") and "## Claims" in out
        assert "wrote" in err and (matrix_dir / SUMMARY_FILE).is_file() and (matrix_dir / REPORT_FILE).is_file()

    def test_json_to_stdout(self, clean_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", str(clean_dir), "--format", "json"]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["matrix"] == MATRIX and summary["status_counts"]["ok"] == 9

    def test_publish_refused(self, matrix_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", str(matrix_dir), "--publish"]) == 2
        assert "not publishable: spread100-pregen / pocketmine@5.44.3" in capsys.readouterr().err

    def test_mixed_machine_profiles(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        write_run(tmp_path, "bds@1.26.52.3", 1, headline(9.4, 105, 1.0, 4000, 190))
        write_run(tmp_path, "bds@1.26.52.3", 2, headline(9.4, 105, 1.0, 4000, 190), profile="1" * 64)
        assert cli.main(["report", str(tmp_path)]) == 2
        assert "different machine profiles" in capsys.readouterr().err
        assert not (tmp_path / SUMMARY_FILE).exists()

    def test_bad_claims_file(self, clean_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        claims = tmp_path / "bad.toml"
        claims.write_text(
            f'schema = 1\nmatrix = "{MATRIX}"\n[[claim]]\nid = "x"\ntext = "t"\nexpr = "open(\'f\')"\n',
            encoding="utf-8",
        )
        assert cli.main(["report", str(clean_dir), "--claims", str(claims)]) == 2
        assert "claim[0].expr: function 'open' is not allowed" in capsys.readouterr().err
