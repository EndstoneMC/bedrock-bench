# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.10"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Draw the figures of one benchmark session from the CSV files ``bbench report`` writes next to report.md.

    uv run scripts/plot_results.py runs/<session> [--out DIR] [--format png,svg] [--scenario NAME]

Input files, all with a header row. ``server`` is the server id, ``server@version`` the tag, and an empty cell means
not measured:

``summary.csv``
    ``scenario,server,version,runs_ok,runs_total``, then ``<metric>_median,<metric>_min,<metric>_max`` across the ok
    runs for each core metric: ``tps``, ``tick_cpu_ms_per_tick``, ``process_cpu_cores``, ``memory_mib``,
    ``view_load_lowest_tps``, ``view_load_p50_s``, ``view_load_p90_s``, ``view_load_max_s``,
    ``view_load_chunks_per_s``, ``chunks_per_player``.
``tps.csv``
    ``run_id,scenario,server,version,t_s,tps``: one row per probe sample, ``t_s`` in seconds since all bots joined.
``view_load.csv``
    ``run_id,scenario,server,version,player,view_load_s``: one row per player per run, ``view_load_s`` in seconds
    from the player's teleport until it holds 90% of its view.

Figures, written to ``--out`` (default ``results/``) in each format:

``tps_over_time``
    TPS of each tag's median run, the run whose mean TPS is the median of the tag's runs.
``<metric>``
    One chart per core metric: the median of the ok runs, with a line from the lowest to the highest run.
``view_load``
    View load time of every player in every ok run, per tag.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import matplotlib
from matplotlib.figure import Figure

#: Every server in a fixed order; a server keeps its colour in every figure. New servers go at the end.
SERVERS = (
    "bds",
    "endstone",
    "levilamina",
    "pocketmine",
    "altay",
    "nukkit",
    "nukkitmot",
    "powernukkitx",
    "allay",
    "dragonfly",
    "chorus",
)

#: Colour-blind-safe colours in a fixed order; neighbours stay apart under protanopia and deuteranopia.
PALETTE = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#1a9fc9",
    "#a0672b",
    "#a347c2",
    "#8c9a1a",
)

METRICS = {
    "tps": "TPS",
    "tick_cpu_ms_per_tick": "Tick-thread CPU per tick (ms)",
    "process_cpu_cores": "Server CPU (cores)",
    "memory_mib": "Memory (MiB)",
    "view_load_lowest_tps": "Lowest TPS while views load",
    "view_load_p50_s": "View load time, median player (s)",
    "view_load_p90_s": "View load time, p90 player (s)",
    "view_load_max_s": "View load time, slowest player (s)",
    "view_load_chunks_per_s": "Chunks per second while views load",
    "chunks_per_player": "Chunks sent per player",
}

INK = "#0b0b0b"
SECONDARY = "#52514e"
MUTED = "#898781"

STYLE = {
    "font.size": 8,
    "axes.titlesize": 10,
    "axes.titlelocation": "left",
    "axes.titlepad": 8,
    "axes.edgecolor": "#c3c2b7",
    "axes.linewidth": 0.8,
    "axes.labelcolor": SECONDARY,
    "axes.labelsize": 8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": False,
    "grid.color": "#e1e0d9",
    "grid.linewidth": 0.6,
    "xtick.color": "#c3c2b7",
    "ytick.color": "#c3c2b7",
    "xtick.labelcolor": SECONDARY,
    "ytick.labelcolor": INK,
    "text.color": INK,
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "savefig.dpi": 200,
}


def read_csv(path: Path, scenario: str | None) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [row for row in rows if scenario is None or row["scenario"] == scenario]


def number(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def tag(row: dict[str, str]) -> str:
    return f"{row['server']}@{row['version']}"


def rank(server: str) -> tuple[int, str]:
    return (SERVERS.index(server) if server in SERVERS else len(SERVERS), server)


def colours(servers: set[str]) -> dict[str, str]:
    slots = {s: i for i, s in enumerate((*SERVERS, *sorted(servers - set(SERVERS))))}
    return {s: PALETTE[slots[s] % len(PALETTE)] for s in servers}


def order_tags(rows: list[dict[str, str]]) -> list[tuple[str, str]]:
    """``(tag, server)`` pairs in the fixed server order; versions keep their order in the file."""
    seen: dict[str, str] = {}
    for row in sorted(rows, key=lambda row: rank(row["server"])):
        seen.setdefault(tag(row), row["server"])
    return list(seen.items())


def label(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.2f}"


def save(fig: Figure, out: Path, name: str, formats: Sequence[str]) -> list[Path]:
    paths = []
    for fmt in formats:
        path = out / f"{name}.{fmt}"
        fig.savefig(path)
        paths.append(path)
    return paths


def plot_tps(rows: list[dict[str, str]], colour: dict[str, str]) -> Figure:
    runs: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        t, tps = number(row["t_s"]), number(row["tps"])
        if t is not None and tps is not None:
            runs[tag(row)][row["run_id"]].append((t, tps))
    tags = [(t, s) for t, s in order_tags(rows) if t in runs]

    ncols = 1 if len(tags) <= 4 else 2
    nrows = math.ceil(len(tags) / ncols)
    fig = Figure(figsize=(7.2, 0.9 + 1.1 * nrows), layout="constrained")
    axes = fig.subplots(nrows, ncols, sharex=True, sharey=True, squeeze=False).flatten()
    top = max(20.0, max(tps for series in runs.values() for run in series.values() for _, tps in run))
    for ax, (name, server) in zip(axes, tags, strict=False):
        means = sorted((statistics.fmean(v for _, v in run), run_id) for run_id, run in runs[name].items())
        median_run = means[(len(means) - 1) // 2][1]
        t, tps = zip(*sorted(runs[name][median_run]), strict=True)
        ax.plot(t, tps, color=colour[server], linewidth=1.2)
        ax.set_title(name, fontsize=8, pad=3)
        ax.set_ylim(0, top * 1.05)
        ax.set_yticks([0, 10, 20])
        ax.grid(axis="y")
    for ax in axes[len(tags) :]:
        ax.set_visible(False)
    for ax in axes[len(tags) - ncols : len(tags)]:
        ax.xaxis.set_tick_params(labelbottom=True)
    fig.suptitle("TPS over time, median run", x=0.01, ha="left", fontsize=10)
    fig.supxlabel("seconds since all bots joined", fontsize=8, color=SECONDARY)
    return fig


def plot_metric(rows: list[dict[str, str]], metric: str, colour: dict[str, str]) -> Figure:
    by_tag = {tag(row): row for row in rows}
    tags = order_tags(rows)

    fig = Figure(figsize=(6.4, 0.85 + 0.3 * len(tags)), layout="constrained")
    ax = fig.subplots()
    labels, right = [], 0.0
    for i, (name, server) in enumerate(tags):
        row = by_tag[name]
        ok, total = row["runs_ok"], row["runs_total"]
        labels.append(name if ok == total else f"{name}  ({ok} of {total} runs ok)")
        median = number(row[f"{metric}_median"])
        if median is None:
            ax.text(0, i, "  not measured", va="center", color=MUTED)
            continue
        low, high = (number(row[f"{metric}_{stat}"]) for stat in ("min", "max"))
        low, high = median if low is None else low, median if high is None else high
        right = max(right, high, median)
        ax.hlines(i, low, high, color=colour[server], linewidth=1.5)
        ax.plot(median, i, "o", color=colour[server], markersize=6, markeredgecolor="white", markeredgewidth=1)
        ax.annotate(
            label(median),
            (max(high, median), i),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            color=SECONDARY,
        )
    ax.set_yticks(range(len(tags)), labels)
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(len(tags) - 0.5, -0.5)
    ax.set_xlim(0, (right or 1.0) * 1.15)
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    ax.set_title(METRICS.get(metric, metric.replace("_", " ").capitalize()))
    ax.set_xlabel("dot: median of the ok runs, line: lowest to highest run")
    return fig


def plot_view_load(rows: list[dict[str, str]], colour: dict[str, str]) -> Figure:
    times: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if (seconds := number(row["view_load_s"])) is not None:
            times[tag(row)].append(seconds)
    tags = [(t, s) for t, s in order_tags(rows) if t in times]

    fig = Figure(figsize=(6.4, 0.85 + 0.42 * len(tags)), layout="constrained")
    ax = fig.subplots()
    jitter = random.Random(0)
    for i, (name, server) in enumerate(tags):
        ys = [i + jitter.uniform(-0.25, 0.25) for _ in times[name]]
        ax.scatter(times[name], ys, s=5, color=colour[server], alpha=0.5, linewidths=0)
    ax.boxplot(
        [times[name] for name, _ in tags],
        positions=range(len(tags)),
        orientation="horizontal",
        whis=(0, 100),
        widths=0.6,
        showfliers=False,
        boxprops={"color": INK, "linewidth": 0.8},
        whiskerprops={"color": INK, "linewidth": 0.8},
        capprops={"color": INK, "linewidth": 0.8},
        medianprops={"color": INK, "linewidth": 1.6},
    )
    ax.set_yticks(range(len(tags)), [name for name, _ in tags])
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(len(tags) - 0.5, -0.5)
    ax.set_xlim(0, max(max(v) for v in times.values()) * 1.05)
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    ax.set_title("View load time per player (s)")
    ax.set_xlabel("one dot per player per ok run\nbox: middle half and median, whiskers: fastest to slowest")
    return fig


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draw the figures of one bbench session from its CSV files.")
    parser.add_argument("session", type=Path, help="directory with summary.csv, tps.csv and view_load.csv")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "results")
    parser.add_argument("--format", default="png,svg", help="comma-separated file formats (default: png,svg)")
    parser.add_argument("--scenario", help="the scenario to plot when the files hold more than one")
    args = parser.parse_args(argv)

    summary_path = args.session / "summary.csv"
    if not summary_path.is_file():
        parser.error(f"{summary_path} not found; run `bbench report {args.session}` first")
    scenarios = {row["scenario"] for row in read_csv(summary_path, None)}
    if args.scenario is None and len(scenarios) > 1:
        parser.error(f"the files hold several scenarios ({', '.join(sorted(scenarios))}); pick one with --scenario")
    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    args.out.mkdir(parents=True, exist_ok=True)

    summary = read_csv(summary_path, args.scenario)
    tps = read_csv(args.session / "tps.csv", args.scenario) if (args.session / "tps.csv").is_file() else []
    views = (
        read_csv(args.session / "view_load.csv", args.scenario) if (args.session / "view_load.csv").is_file() else []
    )
    colour = colours({row["server"] for row in (*summary, *tps, *views)})
    metrics = [c.removesuffix("_median") for c in summary[0] if c.endswith("_median")] if summary else []

    written: list[Path] = []
    with matplotlib.rc_context(STYLE):
        if tps:
            written += save(plot_tps(tps, colour), args.out, "tps_over_time", formats)
        for metric in metrics:
            if f"{metric}_min" in summary[0] and f"{metric}_max" in summary[0]:
                written += save(plot_metric(summary, metric, colour), args.out, metric, formats)
        if views:
            written += save(plot_view_load(views, colour), args.out, "view_load", formats)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
