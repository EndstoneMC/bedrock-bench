# SPDX-License-Identifier: Apache-2.0
"""CSV files for the figures, written next to report.md.

- ``summary.csv``: the summary's numbers, one row per server tag, with each core metric as median, min and max over
  the ok runs.
- ``tps.csv``: the TPS of every probe sample, against seconds since all bots joined.
- ``view_load.csv``: seconds from each player's teleport until it holds 90% of its view, empty if it never did.

Only ``ok`` runs enter the numbers, as in the report. ``scripts/plot_results.py`` reads these files.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bedrock_bench.metrics import load, tps, window
from bedrock_bench.model import ProbeSample, Status
from bedrock_bench.report.aggregate import RunRecord
from bedrock_bench.results import BOTS_FILE, SERIES_DIR
from bedrock_bench.sampling import series

SUMMARY_CSV = "summary.csv"
TPS_CSV = "tps.csv"
VIEW_LOAD_CSV = "view_load.csv"

KEY_COLUMNS = ("server", "version")
STATS = ("median", "min", "max")
TPS_COLUMNS = ("run_id", *KEY_COLUMNS, "t_s", "tps")
VIEW_LOAD_COLUMNS = ("run_id", *KEY_COLUMNS, "player", "view_load_s")


def summary_columns(summary: Mapping[str, Any]) -> list[str]:
    columns = [f"{c['column']}_{stat}" for c in summary["columns"] for stat in STATS]
    return [*KEY_COLUMNS, "runs_ok", "runs_total", *columns]


def summary_rows(summary: Mapping[str, Any]) -> list[list[Any]]:
    rows = []
    for server in summary["servers"]:
        row = [server["arm"].get("server"), server["arm"].get("version"), server["n_ok"], server["n_total"]]
        row += [server["metrics"][c["column"]].get(stat) for c in summary["columns"] for stat in STATS]
        rows.append(row)
    return rows


def _key(record: RunRecord) -> list[Any]:
    return [record.run_id, record.arm.get("server"), record.arm.get("version")]


def tps_rows(record: RunRecord) -> list[list[Any]]:
    joined = (record.run.get("marks") or {}).get("alljoined")
    probe_file = record.path / SERIES_DIR / series.PROBE_FILE
    if joined is None or not probe_file.is_file():
        return []
    origin = int((record.run.get("time") or {}).get("origin_mono_ns", 0))
    probe = [ProbeSample(s.send_ns - origin, s.rx_ns - origin, s.tick) for s in series.read_probe(probe_file)]
    return [[*_key(record), t - joined, rate] for t, rate in tps.series(probe)]


def view_load_rows(record: RunRecord) -> list[list[Any]]:
    bounds = window(record.run, "load")
    bots_file = record.path / BOTS_FILE
    if bounds is None or not bots_file.is_file():
        return []
    time_info = record.run.get("time") or {}
    events = load.timed_events(
        series.read_jsonl(bots_file),
        int(time_info.get("origin_mono_ns", 0)),
        int(time_info.get("origin_epoch_ms", 0)),
    )
    t90 = load.view(events, bounds[1]).t90_own
    return [[*_key(record), player, t90.get(player)] for player in sorted(load.first_per_bot(events, "tp"))]


def write(report_dir: Path, summary: Mapping[str, Any], runs: Sequence[RunRecord]) -> list[Path]:
    """Write the three CSV files into ``report_dir`` from the summary and its loaded runs; return their paths."""
    ok = sorted((r for r in runs if r.status is Status.OK and r.path is not None), key=lambda r: r.run_id)
    tables = (
        (SUMMARY_CSV, summary_columns(summary), summary_rows(summary)),
        (TPS_CSV, TPS_COLUMNS, [row for record in ok for row in tps_rows(record)]),
        (VIEW_LOAD_CSV, VIEW_LOAD_COLUMNS, [row for record in ok for row in view_load_rows(record)]),
    )
    paths = []
    for name, columns, rows in tables:
        path = Path(report_dir) / name
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(columns)
            writer.writerows([_cell(value) for value in row] for row in rows)
        paths.append(path)
    return paths


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 4)
    return value


__all__ = ["SUMMARY_CSV", "TPS_COLUMNS", "TPS_CSV", "VIEW_LOAD_COLUMNS", "VIEW_LOAD_CSV", "summary_columns", "write"]
