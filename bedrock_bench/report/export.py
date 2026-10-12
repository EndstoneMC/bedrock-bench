# SPDX-License-Identifier: Apache-2.0
"""CSV files for the figures, written next to report.md.

- ``summary.csv``: one row per scenario and server tag, with the core metrics as median, min and max over the ok runs.
- ``tps.csv``: the TPS of every probe sample, against seconds since all bots joined.
- ``view_load.csv``: seconds from each player's teleport until it holds 90% of its view.

Only ``ok`` runs enter the numbers, as in the report. ``scripts/plot_results.py`` reads these files.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench.metrics import load, tps, window
from bedrock_bench.model import ProbeSample, Status
from bedrock_bench.results import BOTS_FILE, SERIES_DIR
from bedrock_bench.sampling import series

SUMMARY_CSV = "summary.csv"
TPS_CSV = "tps.csv"
VIEW_LOAD_CSV = "view_load.csv"

#: summary.csv column name -> (metrics.json id, key inside a distribution value)
CORE_METRICS: dict[str, tuple[str, str | None]] = {
    "tps": ("tps_window_steady", None),
    "tick_cpu_ms_per_tick": ("tick_cpu_ms_per_tick_steady", None),
    "process_cpu_cores": ("process_cores_steady", None),
    "memory_mib": ("private_mib_steady", None),
    "view_load_lowest_tps": ("tps_load_low5", None),
    "view_load_p50_s": ("view_t90_own_s", "median"),
    "view_load_p90_s": ("view_t90_own_s", "p90"),
    "view_load_max_s": ("view_t90_own_s", "max"),
    "view_load_chunks_per_s": ("chunks_per_s_load", None),
    "chunks_per_player": ("chunks_per_bot", "median"),
}

KEY_COLUMNS = ("scenario", "server", "version")
SUMMARY_COLUMNS = (
    *KEY_COLUMNS,
    "runs_ok",
    "runs_total",
    *(f"{name}_{stat}" for name in CORE_METRICS for stat in ("median", "min", "max")),
)
TPS_COLUMNS = ("run_id", *KEY_COLUMNS, "t_s", "tps")
VIEW_LOAD_COLUMNS = ("run_id", *KEY_COLUMNS, "player", "view_load_s")


@dataclass(frozen=True)
class Run:
    path: Path
    run: Mapping[str, Any]  # run.json, or run.partial.json for a run that did not finish
    metrics: Mapping[str, Any] | None  # metrics.json of a finished run

    @property
    def run_id(self) -> str:
        return str(self.run.get("run_id") or self.path.name)

    @property
    def key(self) -> tuple[str, str, str]:
        arm = self.run.get("arm") or {}
        scenario = (self.run.get("scenario") or {}).get("name")
        return str(scenario or ""), str(arm.get("server") or ""), str(arm.get("version") or "")

    @property
    def ok(self) -> bool:
        return self.metrics is not None and self.metrics.get("status") == Status.OK.value


def find_runs(report_dir: Path) -> list[Run]:
    """The run directories directly below ``report_dir``."""
    runs = []
    for child in sorted(p for p in Path(report_dir).iterdir() if p.is_dir()):
        finished = (child / "run.json").is_file()
        run_file = child / ("run.json" if finished else "run.partial.json")
        if not run_file.is_file():
            continue
        metrics_file = child / "metrics.json"
        metrics = _read_json(metrics_file) if finished and metrics_file.is_file() else None
        runs.append(Run(child, _read_json(run_file), metrics))
    return runs


def summary_rows(runs: list[Run]) -> list[list[Any]]:
    groups: dict[tuple[str, str, str], list[Run]] = {}
    for run in runs:
        groups.setdefault(run.key, []).append(run)
    rows = []
    for key in sorted(groups):
        ok = [run for run in groups[key] if run.ok]
        row: list[Any] = [*key, len(ok), len(groups[key])]
        for metric_id, part in CORE_METRICS.values():
            values = [v for run in ok if (v := metric_value(run, metric_id, part)) is not None]
            row += [statistics.median(values), min(values), max(values)] if values else [None] * 3
        rows.append(row)
    return rows


def metric_value(run: Run, metric_id: str, part: str | None) -> float | None:
    entry = ((run.metrics or {}).get("metrics") or {}).get(metric_id) or {}
    if entry.get("availability") != "measured":
        return None
    value = entry.get("value")
    if part is not None:
        value = value.get(part) if isinstance(value, Mapping) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def tps_rows(run: Run) -> list[list[Any]]:
    joined = (run.run.get("marks") or {}).get("alljoined")
    probe_file = run.path / SERIES_DIR / series.PROBE_FILE
    if joined is None or not probe_file.is_file():
        return []
    origin = int((run.run.get("time") or {}).get("origin_mono_ns", 0))
    probe = [ProbeSample(s.send_ns - origin, s.rx_ns - origin, s.tick) for s in series.read_probe(probe_file)]
    return [[run.run_id, *run.key, t - joined, rate] for t, rate in tps.series(probe)]


def view_load_rows(run: Run) -> list[list[Any]]:
    bounds = window(run.run, "load")
    bots_file = run.path / BOTS_FILE
    if bounds is None or not bots_file.is_file():
        return []
    time_info = run.run.get("time") or {}
    events = load.timed_events(
        series.read_jsonl(bots_file),
        int(time_info.get("origin_mono_ns", 0)),
        int(time_info.get("origin_epoch_ms", 0)),
    )
    per_player = load.view(events, bounds[1]).t90_own
    return [[run.run_id, *run.key, player, seconds] for player, seconds in sorted(per_player.items())]


def write(report_dir: Path) -> list[Path]:
    """Write the three CSV files into ``report_dir`` and return their paths."""
    report_dir = Path(report_dir)
    runs = find_runs(report_dir)
    ok = sorted((run for run in runs if run.ok), key=lambda run: run.run_id)
    tables = (
        (SUMMARY_CSV, SUMMARY_COLUMNS, summary_rows(runs)),
        (TPS_CSV, TPS_COLUMNS, [row for run in ok for row in tps_rows(run)]),
        (VIEW_LOAD_CSV, VIEW_LOAD_COLUMNS, [row for run in ok for row in view_load_rows(run)]),
    )
    paths = []
    for name, columns, rows in tables:
        path = report_dir / name
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


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


__all__ = ["CORE_METRICS", "SUMMARY_COLUMNS", "TPS_COLUMNS", "VIEW_LOAD_COLUMNS", "find_runs", "write"]
