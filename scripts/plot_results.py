# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.2"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Draw the figures of one benchmark session from the CSV files ``bbench report`` writes next to report.md.

    uv run scripts/plot_results.py runs/<session> [--out DIR] [--format png,svg]

Input files, all with a header row. ``server`` is the server id, ``server@version`` the tag, and an empty cell means
not measured:

``summary.csv``
    ``server,version,runs_ok,runs_total``, then ``<metric>_median,<metric>_min,<metric>_max`` across the ok runs for
    each core metric of the report.
``tps.csv``
    ``run_id,server,version,t_s,tps``: one row per probe sample, ``t_s`` in seconds since all bots joined.
``view_load.csv``
    ``run_id,server,version,player,view_load_s``: one row per teleported player per ok run, ``view_load_s`` in
    seconds from the player's teleport until it holds 90% of its view, empty when it never got there.

Figures, written to ``--out`` (default ``results/``) in each format:

``tps_over_time``
    TPS of each tag's median run, the run whose mean TPS is the median of the tag's runs.
``<metric>``
    One chart per core metric: the median of the ok runs, with a line from the lowest to the highest run.
``view_load``
    View load time of every player in every ok run, per tag.

Chart titles come from the ``columns`` of ``summary.json``, written next to the CSV files.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

import matplotlib
from matplotlib.figure import Figure

ROOT = Path(__file__).resolve().parent.parent

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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def number(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def tag(row: dict[str, str]) -> str:
    return f"{row['server']}@{row['version']}"


def titles(session: Path) -> dict[str, str]:
    """Chart title of each core metric, from the session's summary.json."""
    path = session / "summary.json"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return {c["column"]: c["title"] for c in json.load(f).get("columns", ())}


def colours(servers: set[str]) -> dict[str, str]:
    """A colour per server, in the order of the servers/ folders; servers without a folder follow alphabetically."""
    known = sorted(p.parent.name for p in (ROOT / "servers").glob("*/server.toml"))
    order = [*known, *sorted(servers - set(known))]
    return {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(order)}


def order_tags(summary: list[dict[str, str]], colour: dict[str, str]) -> list[tuple[str, str]]:
    """``(tag, server)`` of every summary row in server order; versions keep their order in the file."""
    order = list(colour)
    return [(tag(row), row["server"]) for row in sorted(summary, key=lambda row: order.index(row["server"]))]


def label(row: dict[str, str], *notes: str) -> str:
    """The tag, with the share of ok runs when some failed and any other notes."""
    ok, total = row["runs_ok"], row["runs_total"]
    notes = (f"{ok} of {total} runs ok",) * (ok != total) + notes
    return tag(row) + (f"  ({', '.join(notes)})" if notes else "")


def save(fig: Figure, out: Path, name: str, formats: Sequence[str]) -> list[Path]:
    paths = []
    for fmt in formats:
        path = out / f"{name}.{fmt}"
        fig.savefig(path)
        paths.append(path)
    return paths


def plot_tps(summary: list[dict[str, str]], rows: list[dict[str, str]], colour: dict[str, str]) -> Figure:
    runs: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        t, tps = number(row["t_s"]), number(row["tps"])
        if t is not None and tps is not None:
            runs[tag(row)][row["run_id"]].append((t, tps))
    by_tag = {tag(row): row for row in summary}
    tags = order_tags(summary, colour)

    ncols = 1 if len(tags) <= 4 else 2
    nrows = math.ceil(len(tags) / ncols)
    fig = Figure(figsize=(7.2, 0.9 + 1.1 * nrows), layout="constrained")
    axes = fig.subplots(nrows, ncols, sharex=True, sharey=True, squeeze=False).flatten()
    top = max([20.0, *(tps for series in runs.values() for run in series.values() for _, tps in run)])
    for ax, (name, server) in zip(axes, tags, strict=False):
        ax.set_title(label(by_tag[name]), fontsize=8, pad=3)
        ax.set_ylim(0, top * 1.05)
        ax.set_yticks([0, 10, 20])
        ax.grid(axis="y")
        if name not in runs:
            ax.text(0.5, 0.5, "no ok run", transform=ax.transAxes, ha="center", va="center", color=MUTED)
            continue
        means = sorted((statistics.fmean(v for _, v in run), run_id) for run_id, run in runs[name].items())
        median_run = means[(len(means) - 1) // 2][1]
        t, tps = zip(*sorted(runs[name][median_run]), strict=True)
        ax.plot(t, tps, color=colour[server], linewidth=1.2)
    for ax in axes[len(tags) :]:
        ax.set_visible(False)
    for ax in axes[len(tags) - ncols : len(tags)]:
        ax.xaxis.set_tick_params(labelbottom=True)
    fig.suptitle("TPS over time, median run", x=0.01, ha="left", fontsize=10)
    fig.supxlabel("seconds since all bots joined", fontsize=8, color=SECONDARY)
    return fig


def plot_metric(summary: list[dict[str, str]], metric: str, title: str, colour: dict[str, str]) -> Figure:
    by_tag = {tag(row): row for row in summary}
    tags = order_tags(summary, colour)

    fig = Figure(figsize=(6.4, 0.85 + 0.3 * len(tags)), layout="constrained")
    ax = fig.subplots()
    right = 0.0
    for i, (name, server) in enumerate(tags):
        row = by_tag[name]
        median = number(row[f"{metric}_median"])
        if median is None:
            ax.text(0, i, "  no ok run" if row["runs_ok"] == "0" else "  not measured", va="center", color=MUTED)
            continue
        low, high = (number(row[f"{metric}_{stat}"]) for stat in ("min", "max"))
        low, high = median if low is None else low, median if high is None else high
        right = max(right, high, median)
        ax.hlines(i, low, high, color=colour[server], linewidth=1.5)
        ax.plot(median, i, "o", color=colour[server], markersize=6, markeredgecolor="white", markeredgewidth=1)
    ax.set_yticks(range(len(tags)), [label(by_tag[name]) for name, _ in tags])
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(len(tags) - 0.5, -0.5)
    ax.set_xlim(0, (right or 1.0) * 1.15)
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    ax.set_title(title)
    ax.set_xlabel("dot: median of the ok runs, line: lowest to highest run")
    return fig


def plot_view_load(summary: list[dict[str, str]], rows: list[dict[str, str]], colour: dict[str, str]) -> Figure:
    times: dict[str, list[float]] = defaultdict(list)
    never: Counter[str] = Counter()
    for row in rows:
        if (seconds := number(row["view_load_s"])) is None:
            never[tag(row)] += 1
        else:
            times[tag(row)].append(seconds)
    by_tag = {tag(row): row for row in summary}
    tags = order_tags(summary, colour)

    fig = Figure(figsize=(6.4, 0.85 + 0.42 * len(tags)), layout="constrained")
    ax = fig.subplots()
    jitter = random.Random(0)
    labels = []
    for i, (name, server) in enumerate(tags):
        n = never[name]
        labels.append(label(by_tag[name], *([f"{n} player{'s' * (n != 1)} never loaded"] if n else [])))
        if by_tag[name]["runs_ok"] == "0":
            ax.text(0, i, "  no ok run", va="center", color=MUTED)
        ys = [i + jitter.uniform(-0.25, 0.25) for _ in times[name]]
        ax.scatter(times[name], ys, s=5, color=colour[server], alpha=0.5, linewidths=0)
    drawn = [i for i, (name, _) in enumerate(tags) if times[name]]
    ax.boxplot(
        [times[tags[i][0]] for i in drawn],
        positions=drawn,
        orientation="horizontal",
        whis=(0, 100),
        widths=0.6,
        showfliers=False,
        boxprops={"color": INK, "linewidth": 0.8},
        whiskerprops={"color": INK, "linewidth": 0.8},
        capprops={"color": INK, "linewidth": 0.8},
        medianprops={"color": INK, "linewidth": 1.6},
    )
    ax.set_yticks(range(len(tags)), labels)
    ax.tick_params(axis="y", length=0)
    ax.set_ylim(len(tags) - 0.5, -0.5)
    ax.set_xlim(0, max([1.0, *(max(v) for v in times.values() if v)]) * 1.05)
    ax.grid(axis="x")
    ax.set_axisbelow(True)
    ax.set_title("View load time per player (s)")
    ax.set_xlabel("one dot per player per ok run\nbox: middle half and median, whiskers: fastest to slowest")
    return fig


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draw the figures of one bbench session from its CSV files.")
    parser.add_argument("session", type=Path, help="directory with summary.csv, tps.csv and view_load.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--format", default="png,svg", help="comma-separated file formats (default: png,svg)")
    args = parser.parse_args(argv)

    summary_path = args.session / "summary.csv"
    if not summary_path.is_file():
        parser.error(f"{summary_path} not found; run `bbench report {args.session}` first")
    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    args.out.mkdir(parents=True, exist_ok=True)

    summary = read_csv(summary_path)
    if not summary:
        parser.error(f"{summary_path} has no rows")
    tps = read_csv(args.session / "tps.csv") if (args.session / "tps.csv").is_file() else []
    views = read_csv(args.session / "view_load.csv") if (args.session / "view_load.csv").is_file() else []
    colour = colours({row["server"] for row in summary})
    title = titles(args.session)
    metrics = [c.removesuffix("_median") for c in summary[0] if c.endswith("_median")]

    written: list[Path] = []
    with matplotlib.rc_context(STYLE):
        written += save(plot_tps(summary, tps, colour), args.out, "tps_over_time", formats)
        for metric in metrics:
            if f"{metric}_min" in summary[0] and f"{metric}_max" in summary[0]:
                fig = plot_metric(summary, metric, title.get(metric, metric.replace("_", " ").capitalize()), colour)
                written += save(fig, args.out, metric, formats)
        written += save(plot_view_load(summary, views, colour), args.out, "view_load", formats)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
