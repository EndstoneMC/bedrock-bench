# SPDX-License-Identifier: Apache-2.0
"""scripts/plot_results.py on the CSV files the export writes for the synthetic fixture runs.

Skipped where matplotlib is not installed; CI runs it with the version the script pins.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from bedrock_bench.config import available, project_root
from bedrock_bench.report import export, write
from bedrock_bench.report.aggregate import CORE_METRICS
from fixtures.runs import synthetic

pytest.importorskip("matplotlib")


def load_script():
    spec = importlib.util.spec_from_file_location("plot_results", project_root() / "scripts" / "plot_results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session(directory: Path, servers: Sequence[str], names: Sequence[str] = synthetic.NAMES) -> Path:
    """The fixture runs, copied once per server with the server swapped in run.json, and their report."""
    for server in servers:
        for name in names:
            run_dir = shutil.copytree(synthetic.HERE / name, directory / f"{server}-{name}")
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run["arm"]["server"] = server
            (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    write(directory)
    return directory


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_writes_every_figure(tmp_path: Path) -> None:
    directory = session(tmp_path / "session", [*available("servers")[:5], "synthetic"])
    out = tmp_path / "figures"
    assert load_script().main([str(directory), "--out", str(out)]) == 0
    with (directory / export.SUMMARY_CSV).open(encoding="utf-8") as f:
        metrics = [c.removesuffix("_median") for c in next(csv.reader(f)) if c.endswith("_median")]
    names = ["tps_over_time", "view_load", *metrics]
    assert sorted(p.name for p in out.iterdir()) == sorted(f"{n}.{fmt}" for n in names for fmt in ("png", "svg"))
    assert all(p.stat().st_size > 0 for p in out.iterdir())


def test_titles_come_from_the_report(tmp_path: Path) -> None:
    directory = session(tmp_path / "session", ["synthetic"])
    titles = load_script().titles(directory)
    assert list(titles) == [m.column for m in CORE_METRICS]
    assert titles["view_load_p90_s"] == "View load time, p90 player (s)"


def test_a_tag_without_an_ok_run_keeps_its_place(tmp_path: Path) -> None:
    directory = session(tmp_path / "session", ["crashed"], names=("synthetic-death",))
    session(directory, ["fine"], names=("synthetic-steady",))
    script = load_script()
    summary = read_csv(directory / export.SUMMARY_CSV)
    colour = script.colours({row["server"] for row in summary})

    tps = script.plot_tps(summary, read_csv(directory / export.TPS_CSV), colour)
    panels = {ax.get_title(): [t.get_text() for t in ax.texts] for ax in tps.axes if ax.get_visible()}
    assert panels == {"crashed@1.0  (0 of 1 runs ok)": ["no ok run"], "fine@1.0": []}

    rows = read_csv(directory / export.VIEW_LOAD_CSV)
    rows.append({**rows[-1], "player": "never", "view_load_s": ""})
    views = script.plot_view_load(summary, rows, colour)
    labels = [t.get_text() for t in views.axes[0].get_yticklabels()]
    assert labels == ["crashed@1.0  (0 of 1 runs ok)", "fine@1.0  (1 player never loaded)"]
