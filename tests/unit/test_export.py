# SPDX-License-Identifier: Apache-2.0
"""CSV export for the figures, on the synthetic fixture runs."""

from __future__ import annotations

import csv
import json
import shutil
import statistics
from pathlib import Path

import pytest

from bedrock_bench import cli
from bedrock_bench.config import load_report_metrics
from bedrock_bench.report import SUMMARY_FILE, export, write
from fixtures.runs import synthetic

OK_RUNS = ("synthetic-dip", "synthetic-midnight", "synthetic-steady")


def read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    return list(reader.fieldnames or ()), rows


def run_id(name: str) -> str:
    return json.loads((synthetic.HERE / name / "run.json").read_text(encoding="utf-8"))["run_id"]


@pytest.fixture
def report_dir(tmp_path: Path) -> Path:
    for name in synthetic.NAMES:
        shutil.copytree(synthetic.HERE / name, tmp_path / name)
    write(tmp_path)
    return tmp_path


def test_summary_has_one_row_per_tag_with_median_min_max_over_ok_runs(report_dir: Path) -> None:
    columns, rows = read(report_dir / export.SUMMARY_CSV)
    metrics = [f"{m.column}_{stat}" for m in load_report_metrics() for stat in ("median", "min", "max")]
    assert columns == ["server", "version", "runs_ok", "runs_total", *metrics]
    assert len(rows) == 1
    row = rows[0]
    assert (row["server"], row["version"]) == ("synthetic", "1.0")
    assert (row["runs_ok"], row["runs_total"]) == ("3", "4")
    assert float(row["tps_median"]) == pytest.approx(20.0009)
    assert float(row["memory_mib_median"]) == pytest.approx(1432.5)
    # the dip run's tick rate halves while views load
    assert float(row["view_load_lowest_tps_min"]) == pytest.approx(9.992)
    assert float(row["view_load_lowest_tps_median"]) == pytest.approx(19.984)
    assert float(row["view_load_lowest_tps_max"]) == pytest.approx(19.984)
    assert float(row["view_load_p50_s_median"]) == pytest.approx(37.2155)
    assert float(row["view_load_max_s_max"]) == pytest.approx(37.434)
    assert float(row["view_load_chunks_per_s_median"]) == pytest.approx(27.4615)
    assert float(row["chunks_in_view_per_player_median"]) == pytest.approx(297.5)


def test_summary_csv_holds_the_numbers_of_summary_json(report_dir: Path) -> None:
    server = json.loads((report_dir / SUMMARY_FILE).read_text(encoding="utf-8"))["servers"][0]
    row = read(report_dir / export.SUMMARY_CSV)[1][0]
    for name, stat in server["metrics"].items():
        for key in ("median", "min", "max"):
            expected = "" if stat.get(key) is None else round(stat[key], 4)
            assert row[f"{name}_{key}"] == str(expected), name


def test_tps_rows_are_seconds_since_all_bots_joined(report_dir: Path) -> None:
    columns, rows = read(report_dir / export.TPS_CSV)
    assert tuple(columns) == export.TPS_COLUMNS
    assert {row["run_id"] for row in rows} == {run_id(name) for name in OK_RUNS}
    dip = [(float(r["t_s"]), float(r["tps"])) for r in rows if r["run_id"] == run_id("synthetic-dip")]
    assert dip == sorted(dip)
    assert dip[0][0] < 0  # idle samples come before the bots join at 37.5 s
    slow = [t for t, rate in dip if rate < 15]
    assert slow and min(slow) > 50 - 37.5 and max(slow) <= 75 - 37.5


def test_view_load_rows_match_the_run_metrics(report_dir: Path) -> None:
    columns, rows = read(report_dir / export.VIEW_LOAD_CSV)
    assert tuple(columns) == export.VIEW_LOAD_COLUMNS
    steady = [r for r in rows if r["run_id"] == run_id("synthetic-steady")]
    assert len(steady) == synthetic.N_BOTS
    assert [r["player"] for r in steady] == sorted(r["player"] for r in steady)
    seconds = [float(r["view_load_s"]) for r in steady]
    stored = json.loads((synthetic.HERE / "synthetic-steady" / "metrics.json").read_text(encoding="utf-8"))
    expected = stored["metrics"]["view_t90_own_s"]["value"]
    assert statistics.median(seconds) == pytest.approx(expected["median"], abs=1e-4)
    assert max(seconds) == pytest.approx(expected["max"], abs=1e-4)


def test_a_run_without_metrics_counts_as_not_ok(tmp_path: Path) -> None:
    shutil.copytree(synthetic.HERE / "synthetic-steady", tmp_path / "a")
    shutil.copytree(synthetic.HERE / "synthetic-dip", tmp_path / "b")
    (tmp_path / "b" / "metrics.json").unlink()
    write(tmp_path)
    _, rows = read(tmp_path / export.SUMMARY_CSV)
    assert (rows[0]["runs_ok"], rows[0]["runs_total"]) == ("1", "2")
    _, tps_rows = read(tmp_path / export.TPS_CSV)
    assert {row["run_id"] for row in tps_rows} == {run_id("synthetic-steady")}


def test_metrics_nobody_measured_are_empty_cells(tmp_path: Path) -> None:
    shutil.copytree(synthetic.HERE / "synthetic-death", tmp_path / "death")
    write(tmp_path)
    _, rows = read(tmp_path / export.SUMMARY_CSV)
    assert (rows[0]["runs_ok"], rows[0]["runs_total"]) == ("0", "1")
    assert rows[0]["tps_median"] == rows[0]["chunks_in_view_per_player_max"] == ""
    assert read(tmp_path / export.TPS_CSV)[1] == []


def test_bbench_report_writes_the_csv_files_next_to_report_md(tmp_path: Path) -> None:
    for name in synthetic.NAMES:
        shutil.copytree(synthetic.HERE / name, tmp_path / name)
    cli.main(["report", str(tmp_path)])
    for name in ("report.md", export.SUMMARY_CSV, export.TPS_CSV, export.VIEW_LOAD_CSV):
        assert (tmp_path / name).is_file()
