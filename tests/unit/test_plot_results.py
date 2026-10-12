# SPDX-License-Identifier: Apache-2.0
"""scripts/plot_results.py on made-up CSV files; skipped where matplotlib is not installed."""

from __future__ import annotations

import csv
import importlib.util
import random
from pathlib import Path

import pytest

from bedrock_bench.config import project_root

pytest.importorskip("matplotlib")

METRICS = (
    "tps",
    "tick_cpu_ms_per_tick",
    "process_cpu_cores",
    "memory_mib",
    "view_load_lowest_tps",
    "view_load_p50_s",
    "view_load_p90_s",
    "view_load_max_s",
    "view_load_chunks_per_s",
    "chunks_per_player",
)
SERVERS = (("bds", "1.0.0"), ("endstone", "1.0.0"), ("dragonfly", "abc1234"), ("someserver", "2.0"))


def load_script():
    spec = importlib.util.spec_from_file_location("plot_results", project_root() / "scripts" / "plot_results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_session(directory: Path, servers=SERVERS, scenarios=("spread100",), players: int = 10) -> None:
    rng = random.Random(1)
    summary, tps, views = [], [], []
    for scenario in scenarios:
        for i, (server, version) in enumerate(servers):
            runs_ok = 2 if server == "dragonfly" else 3
            row: list[object] = [scenario, server, version, runs_ok, 3]
            for metric in METRICS:
                if server == "dragonfly" and metric == "tick_cpu_ms_per_tick":
                    row += ["", "", ""]
                else:
                    median = rng.uniform(1, 40)
                    row += [round(median, 4), round(median * 0.9, 4), round(median * 1.1, 4)]
            summary.append(row)
            for run in range(runs_ok):
                run_id = f"{server}-{run}"
                for t in range(-30, 600, 5):
                    tps.append([run_id, scenario, server, version, t, round(20 - rng.uniform(0, 2 + i), 4)])
                for player in range(players):
                    views.append([run_id, scenario, server, version, f"bot{player}", round(rng.uniform(5, 60), 4)])
    write(
        directory / "summary.csv",
        ["scenario", "server", "version", "runs_ok", "runs_total"]
        + [f"{m}_{s}" for m in METRICS for s in ("median", "min", "max")],
        summary,
    )
    write(directory / "tps.csv", ["run_id", "scenario", "server", "version", "t_s", "tps"], tps)
    write(directory / "view_load.csv", ["run_id", "scenario", "server", "version", "player", "view_load_s"], views)


def test_writes_every_figure(tmp_path: Path) -> None:
    write_session(tmp_path)
    out = tmp_path / "figures"
    assert load_script().main([str(tmp_path), "--out", str(out)]) == 0
    names = ["tps_over_time", "view_load", *METRICS]
    expected = sorted(f"{name}.{fmt}" for name in names for fmt in ("png", "svg"))
    assert sorted(p.name for p in out.iterdir()) == expected
    assert all(p.stat().st_size > 0 for p in out.iterdir())


def test_several_scenarios_need_a_choice(tmp_path: Path) -> None:
    write_session(tmp_path, scenarios=("smoke", "spread100"))
    script = load_script()
    args = [str(tmp_path), "--out", str(tmp_path / "figures"), "--format", "png"]
    with pytest.raises(SystemExit):
        script.main(args)
    assert script.main([*args, "--scenario", "smoke"]) == 0
    assert (tmp_path / "figures" / "tps.png").is_file()
