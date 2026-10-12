# SPDX-License-Identifier: Apache-2.0
"""Every metric ``metrics.json`` holds, with its unit, window, direction, inputs and formula.

This is the single source of docs/metrics.md (``bbench docs metrics``). ``compute`` emits exactly the declared ids,
each with an availability, so every metrics.json of one analysis version has the same keys. metrics.json holds what
the report and the CSV files show and what the validity gates read, nothing else.

Ids carry their unit: ``tick_cpu_ms_per_tick_steady`` is in CPU ms per tick, ``process_cores_steady`` in cores.
Counts, booleans and exit codes are dimensionless and carry none. Per-window metrics are declared once with ``<W>`` in
the id.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from bedrock_bench import ANALYSIS_VERSION, METRICS_SCHEMA

# --------------------------------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    name: str
    label: str
    tokens: tuple[str, ...]  # an id must contain one of these as whole ``_``-separated words; empty = dimensionless


UNITS: dict[str, Unit] = {
    u.name: u
    for u in (
        Unit("tps", "ticks per second", ("tps",)),
        Unit("ticks", "ticks", ("ticks",)),
        Unit("s", "seconds", ("s",)),
        Unit("ms", "milliseconds", ("ms",)),
        Unit("ms_per_tick", "CPU ms per tick", ("ms_per_tick",)),
        Unit("frac", "fraction (0 to 1)", ("frac",)),
        Unit("cores", "CPU seconds per second", ("cores",)),
        Unit("mib", "MiB (2^20 bytes)", ("mib",)),
        Unit("chunks", "chunk columns", ("chunks",)),
        Unit("chunks_per_s", "chunk columns per second", ("chunks_per_s",)),
        Unit("count", "count", ()),
        Unit("bool", "true or false", ()),
        Unit("code", "process exit code", ()),
    )
}

#: Raw inputs a metric reads (the run directory files behind them).
INPUTS = {
    "probe": "series/probe.csv",
    "tick": "series/tick.csv.gz",
    "process": "series/process.csv.gz",
    "machine": "series/machine.csv.gz",
    "bots": "bots.jsonl.gz",
    "commands": "commands.jsonl.gz",
    "run": "run.json",
}

#: Windows of a run (``window()`` in ``metrics/__init__.py``); ``run`` means the whole run.
ALL_WINDOWS = ("idle", "join", "settle", "load", "steady")
MEASURED = ("load", "steady")
DIRECTIONS = ("higher", "lower", None)

SECTIONS = {
    "tps": "Tick rate",
    "tick": "Tick thread CPU",
    "cpu": "Process, bot and machine CPU",
    "memory": "Memory",
    "load": "View load and chunks",
    "health": "Health",
}

SECTION_NOTES = {
    "tps": (
        "TPS comes only from a counter that advances once per executed tick, read through the console's priority "
        "lane every 5 s."
    ),
    "tick": (
        "Tick-thread CPU is CPU time, not wall time, so it is not MSPT. It leaves out the time the thread is "
        "blocked on a lock, I/O or a worker: a tick that waits 20 ms for a worker and computes for 30 ms takes "
        "50 ms of wall time and 30 ms of CPU. Servers whose adapter declares no tick thread report it as "
        "not_applicable; compare process_cores across servers instead."
    ),
    "cpu": (
        "process_cores is the cross-server CPU metric. Bot and foreign load are interference checks, read from the "
        "per-CPU busy counters of the whole machine."
    ),
    "memory": (
        "private_mib is PrivateUsage on Windows and RssAnon + VmSwap on Linux. Never compared across operating "
        "systems; JVM servers run with a fixed heap."
    ),
    "load": (
        "U_b is a bot's in-view unique chunk count at settled: chunks within view radius + 1 of the bot's current "
        "chunk, keyed by dimension, so chunks streamed for an earlier position do not count."
    ),
}


@dataclass(frozen=True)
class Metric:
    id: str
    unit: str
    window: str | None  # one of ALL_WINDOWS, or "run"
    direction: str | None  # "higher" or "lower" is better; None for diagnostics
    requires: tuple[str, ...]  # keys of INPUTS
    formula: str
    doc: str
    section: str
    template: str  # the id with <W> for per-window metrics
    stats: tuple[str, ...] = ()  # statistic names of a distribution value; empty for a scalar

    @property
    def kind(self) -> str:
        return "distribution" if self.stats else "scalar"


_metrics: list[Metric] = []


def _windows(template: str, windows: Sequence[str] | None) -> Iterator[tuple[str, str | None]]:
    if "<W>" in template:
        assert windows, template
        for w in windows:
            yield template.replace("<W>", w), w
    else:
        yield template, None


def _metric(
    template: str,
    unit: str,
    section: str,
    requires: Sequence[str],
    formula: str,
    *,
    direction: str | None = None,
    window: str | None = None,
    windows: Sequence[str] | None = None,
    doc: str = "",
    stats: Sequence[str] = (),
) -> None:
    for metric_id, w in _windows(template, windows):
        _metrics.append(
            Metric(
                id=metric_id,
                unit=unit,
                window=w or window,
                direction=direction,
                requires=tuple(requires),
                formula=formula,
                doc=doc,
                section=section,
                template=template,
                stats=tuple(stats),
            )
        )


# --------------------------------------------------------------------------------------------------------------------
# Tick rate
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "tps_window_steady",
    "tps",
    "tps",
    ["probe"],
    "(g_b - g_a) / (t_b - t_a) over the replies inside steady; unavailable when t_b - t_a < 0.9 (t1 - t0)",
    direction="higher",
    window="steady",
)
_metric(
    "tps_load_low5",
    "tps",
    "tps",
    ["probe"],
    "min over replies i in load of (g_j - g_i) / (t_j - t_i), j the first reply in load with t_j - t_i >= 4.5 s",
    direction="higher",
    window="load",
    doc="Lowest 5 s TPS while views load.",
)
_metric(
    "probe_latency_ms_steady",
    "ms",
    "tps",
    ["probe"],
    "t_i - s_i (receive - send) of the replies received in steady",
    direction="lower",
    window="steady",
    stats=("p50", "p99", "max"),
    doc="probe_lagging gate: p99 above 1000 ms.",
)
_metric(
    "probe_max_gap_s_<W>",
    "s",
    "tps",
    ["probe"],
    "longest stretch of W without a reply, counting from t0 and up to t1",
    windows=MEASURED,
    doc="probe_coverage gate: above 15 s.",
)
_metric(
    "probe_tick_decreases",
    "count",
    "tps",
    ["probe"],
    "consecutive replies whose counter went down",
    window="run",
    doc="probe_monotonic gate: must be 0.",
)
_metric(
    "probe_idle_ticks",
    "ticks",
    "tps",
    ["probe"],
    "g_b - g_a over idle (over the whole run when the workload has no idle phase)",
    window="idle",
    doc="probe_monotonic gate: must be positive, so the counter advances with nobody online.",
)

# --------------------------------------------------------------------------------------------------------------------
# Tick thread CPU
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "tick_cpu_ms_per_tick_steady",
    "ms_per_tick",
    "tick",
    ["tick", "probe"],
    "1000 dCPU_tick / dG, with CPU interpolated at the replies a and b bounding steady and dG = g_b - g_a",
    direction="lower",
    window="steady",
    doc="CPU_tick: GetThreadTimes on Windows, schedstat (fallback stat / SC_CLK_TCK) on Linux. Not MSPT.",
)

# --------------------------------------------------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "process_cores_steady",
    "cores",
    "cpu",
    ["process"],
    "d(server process user + system CPU s) / dt between the first and last 1 Hz sample inside steady",
    direction="lower",
    window="steady",
    doc="Includes threads that exited.",
)
_metric(
    "bot_cpu_frac_<W>",
    "frac",
    "cpu",
    ["machine", "run"],
    "mean over the 1 Hz machine samples in W of the summed busy fraction of the bot CPUs (placement.bot_cpus), "
    "divided by the number of bot CPUs",
    windows=MEASURED,
    doc="bot_saturated gate: above the machine profile's bot_cpu_frac_max (0.85).",
)
_metric(
    "foreign_cores_<W>",
    "cores",
    "cpu",
    ["machine"],
    "mean over W of machine busy cores minus the server, bot and harness trees",
    windows=MEASURED,
    doc="foreign_load gate: machine profile foreign_cores_mean (0.25).",
)
_metric(
    "foreign_cores_peak_10s_<W>",
    "cores",
    "cpu",
    ["machine"],
    "highest mean of the foreign busy cores over 10 consecutive 1 s machine samples in W",
    windows=MEASURED,
    doc="foreign_load gate: machine profile foreign_cores_peak_10s (1.5).",
)
_metric(
    "foreign_server_set_cores_<W>",
    "cores",
    "cpu",
    ["machine"],
    "mean over W of the foreign busy cores on the server CPUs",
    windows=MEASURED,
    doc="foreign_load gate: machine profile foreign_server_set_cores_mean (0.25).",
)

# --------------------------------------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "private_mib_steady",
    "mib",
    "memory",
    ["process"],
    "mean of private_mib over the 1 Hz process samples in steady",
    direction="lower",
    window="steady",
)

# --------------------------------------------------------------------------------------------------------------------
# View load and chunks
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "chunks_per_bot",
    "chunks",
    "load",
    ["bots"],
    "U_b from the last 5 s progress snapshot at or before settled",
    window="load",
    stats=("min", "median", "max"),
)
_metric(
    "view_t90_own_s",
    "s",
    "load",
    ["bots"],
    "per bot, first teleport to ceil(0.9 U_b) in-view chunks, from the bot's chunk arrival times (first 5 s "
    "progress snapshot reaching the count as fallback)",
    direction="lower",
    window="load",
    stats=("median", "p90", "max"),
    doc="Relative to each bot's own final view: a server that sends fewer chunks finishes sooner.",
)
_metric(
    "view_t_n_s",
    "s",
    "load",
    ["bots"],
    "censored median over bots of first teleport to the 80th in-view chunk; a bot that never reaches 80 is "
    "censored at the last time it was observed; null when the median is censored",
    direction="lower",
    window="load",
    doc="A null value sets the views_incomplete flag.",
)
_metric(
    "chunks_per_s_load",
    "chunks_per_s",
    "load",
    ["bots"],
    "sum of U_b / (settled - bots_start)",
    window="load",
)
_metric(
    "csg_effective",
    "bool",
    "load",
    ["bots", "run"],
    "median U_b < 0.5 disc(r), r the workload view distance, disc(r) the chunk columns with dx^2 + dz^2 <= r^2",
    window="load",
    doc="csg_matches gate: must equal the workload's CSG setting on servers that support client-side generation.",
)

# --------------------------------------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------------------------------------

_metric("server_exit_code", "code", "health", ["run"], "the server's exit code after teardown", window="run")
_metric(
    "died_during_run",
    "bool",
    "health",
    ["run"],
    "the server process exited before teardown",
    window="run",
)
_metric(
    "decode_errors",
    "count",
    "health",
    ["bots"],
    "highest decode_errors total reported by the bots (stat and decode_error events)",
    window="run",
    doc="decode_errors gate: must be 0.",
)
_metric("joined", "count", "health", ["bots"], "bots that spawned at least once", window="run")
_metric(
    "dropped",
    "count",
    "health",
    ["bots"],
    "stat dropped at the end of steady",
    window="run",
    doc="Warning when nonzero.",
)
_metric(
    "interventions_in_measured",
    "count",
    "health",
    ["run", "commands"],
    "the larger of the runner's count and the console commands with an origin other than the probe sent inside steady",
    window="steady",
    doc="interventions_in_measured gate: must be 0.",
)

METRICS: tuple[Metric, ...] = tuple(_metrics)
_BY_ID = {m.id: m for m in METRICS}


def get(metric_id: str) -> Metric:
    return _BY_ID[metric_id]


def ids() -> list[str]:
    return [m.id for m in METRICS]


def problems() -> list[str]:
    """Declaration errors: duplicate ids, unknown units, sections or inputs, ids that do not carry their unit."""
    found: list[str] = []
    seen: set[str] = set()
    for metric in METRICS:
        if metric.id in seen:
            found.append(f"{metric.id}: declared twice")
        seen.add(metric.id)
        unit = UNITS.get(metric.unit)
        if unit is None:
            found.append(f"{metric.id}: unknown unit {metric.unit!r}")
        elif unit.tokens and not any(_has_words(metric.id, token) for token in unit.tokens):
            found.append(f"{metric.id}: id does not carry its unit ({' or '.join(unit.tokens)})")
        if metric.section not in SECTIONS:
            found.append(f"{metric.id}: unknown section {metric.section!r}")
        if metric.window not in (*ALL_WINDOWS, "run"):
            found.append(f"{metric.id}: unknown window {metric.window!r}")
        if metric.direction not in DIRECTIONS:
            found.append(f"{metric.id}: bad direction {metric.direction!r}")
        unknown = [r for r in metric.requires if r not in INPUTS]
        if unknown:
            found.append(f"{metric.id}: unknown inputs {unknown}")
        if not metric.formula:
            found.append(f"{metric.id}: no formula")
    return found


def _has_words(metric_id: str, token: str) -> bool:
    words, needle = metric_id.split("_"), token.split("_")
    return any(words[i : i + len(needle)] == needle for i in range(len(words) - len(needle) + 1))


# --------------------------------------------------------------------------------------------------------------------
# docs/metrics.md
# --------------------------------------------------------------------------------------------------------------------

_HEADER = """\
<!-- Generated by `bbench docs metrics` from bedrock_bench/metrics/registry.py. Do not edit. -->

# Metrics

Every metric in `metrics.json` (schema `{schema}`), analysis version {analysis}: what the report and the CSV
files show, and what the validity gates read. `bbench analyze` recomputes the file byte for byte from a run's raw
files; any change to a formula bumps the analysis version.

## Conventions

- **Time.** Every sample is stamped with the harness monotonic clock. Phase marks are seconds since the run's origin.
  Bot events are mapped onto the same timebase through their own monotonic clock (fallback: their UTC stamp and the
  run's origin pair), so nothing depends on the time of day.
- **Windows.** `idle` = [idle0, idle1], `join` = [bots_start, alljoined], `settle` = [alljoined, settled],
  `load` = [bots_start, settled], `steady` = [steady0, steady1]. A window whose end mark is missing was not reached;
  its metrics are `unavailable`. A window the workload does not have is `not_applicable`.
- **Probe notation.** Replies `(s_i, t_i, g_i)` = send time, receive time, tick counter. In a window `[t0, t1]`,
  `a = min{{i: t_i >= t0}}` and `b = max{{i: t_i <= t1}}`.
- **Rates over samples** use the span the samples cover inside the window: first sample at or after `t0` to last
  at or before `t1`.
- **Percentiles** are nearest rank, `sorted[round(q (n - 1))]` with halves rounded up; medians are conventional.
  Every statistic reports its `n`.
- **Units.** Each id carries its unit (`_s`, `_ms`, `_mib`, `_cores`, `_ms_per_tick`, `_frac`, ...). MiB = 2^20
  bytes. Counts and booleans carry no suffix.
- **Availability.** `measured`, `not_applicable` (the server or workload has no such quantity) or `unavailable`
  (should exist but could not be computed; `reason` says why).
- **Direction.** `higher` or `lower` is better; `-` marks gate inputs, which are never ranked.
- Floats are rounded to {digits} decimals.
"""


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown() -> str:
    """docs/metrics.md as text."""
    from bedrock_bench.metrics import FLOAT_DIGITS
    from bedrock_bench.metrics.health import GATES

    out = [_HEADER.format(schema=METRICS_SCHEMA, analysis=ANALYSIS_VERSION, digits=FLOAT_DIGITS)]
    out.append("## Inputs\n")
    out.append("| Input | Raw file |")
    out.append("|---|---|")
    for key, path in INPUTS.items():
        out.append(f"| `{key}` | `{path}` |")
    out.append("")

    for section, title in SECTIONS.items():
        out.append(f"## {title}\n")
        note = SECTION_NOTES.get(section)
        if note:
            out.append(note + "\n")
        out.append("| Metric | Windows | Unit | Value | Better | Inputs | Formula |")
        out.append("|---|---|---|---|---|---|---|")
        done: set[str] = set()
        for metric in METRICS:
            if metric.section != section or metric.template in done:
                continue
            done.add(metric.template)
            windows = [m.window for m in METRICS if m.template == metric.template]
            value = ", ".join(metric.stats) + ", n" if metric.stats else "scalar"
            formula = metric.formula + (f". {metric.doc}" if metric.doc else "")
            out.append(
                f"| `{metric.template}` | {', '.join(str(w) for w in windows)} | {UNITS[metric.unit].label} | "
                f"{value} | {metric.direction or '-'} | {', '.join(metric.requires)} | {_cell(formula)} |"
            )
        out.append("")

    out.append("## Validity gates\n")
    out.append(
        "A run is `invalid` if any invalid-level gate fails, else `server_failed` if an outcome gate fails, else "
        "`ok`. Gates on a window the run never reached are skipped, so a server that dies early is a "
        "`server_failed` result rather than an invalid run.\n"
    )
    out.append("| Gate | Level | Rule |")
    out.append("|---|---|---|")
    for gate in GATES:
        level = gate.gate.status.value if gate.gate.status is not None else "warning"
        out.append(f"| `{gate.gate.value}` | {level} | {_cell(gate.rule)} |")
    out.append("")
    return "\n".join(out)


def render_docs(args: argparse.Namespace) -> int:
    """``bbench docs metrics [--check]``: write docs/metrics.md, or check that it is current."""
    from bedrock_bench.config import project_root

    path = project_root() / "docs" / "metrics.md"
    text = render_markdown()
    if getattr(args, "check", False):
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current != text:
            print(f"{path.name} is out of date; run 'bbench docs metrics'")
            return 1
        print(f"{path.name} is up to date")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path.relative_to(project_root()).as_posix()}")
    return 0
