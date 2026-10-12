# SPDX-License-Identifier: Apache-2.0
"""Every metric and table ``metrics.json`` can hold, with its unit, window, direction, inputs and formula.

This is the single source of docs/metrics.md (``bbench docs metrics``). ``compute`` emits exactly the declared ids,
each with an availability, so every metrics.json of one analysis version has the same keys.

Ids carry their unit: ``tick_ms_per_s_steady`` is in ms per second, ``process_cores_load`` in cores. Counts, booleans
and exit codes are dimensionless and carry none. Per-window metrics are declared once with ``<W>`` in the id.
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
        Unit("ms", "milliseconds", ("ms", "mspt")),
        Unit("ms_per_s", "CPU ms per second", ("ms_per_s",)),
        Unit("ms_per_tick", "CPU ms per tick", ("ms_per_tick",)),
        Unit("frac", "fraction (0 to 1)", ("frac",)),
        Unit("ratio", "ratio", ("ratio",)),
        Unit("cores", "CPU seconds per second", ("cores",)),
        Unit("mib", "MiB (2^20 bytes)", ("mib",)),
        Unit("mib_per_h", "MiB per hour", ("mib_per_h",)),
        Unit("chunks", "chunk columns", ("chunks",)),
        Unit("chunks_per_s", "chunk columns per second", ("chunks_per_s",)),
        Unit("chunks_per_cpu_s", "chunk columns per server CPU second", ("chunks_per_cpu_s",)),
        Unit("ticks_per_cpu_s", "ticks per server CPU second", ("ticks_per_cpu_s",)),
        Unit("per_bot_s", "per bot per second", ("per_bot_s",)),
        Unit("kib_per_bot_s", "KiB per bot per second", ("kib_per_bot_s",)),
        Unit("count", "count", ()),
        Unit("bool", "true or false", ()),
        Unit("code", "process exit code", ()),
    )
}

#: Raw inputs a metric reads (the run directory files behind them).
INPUTS = {
    "probe": "series/probe.csv",
    "tick": "series/tick.csv.gz",
    "threads": "series/threads.csv.gz",
    "process": "series/process.csv.gz",
    "machine": "series/machine.csv.gz",
    "bots": "bots.jsonl.gz",
    "log": "server.log.gz",
    "commands": "commands.jsonl.gz",
    "chaos": "chaos-exec.jsonl",
    "run": "run.json",
}

#: Windows of a run (``window()`` in ``metrics/__init__.py``); ``run`` means the whole run.
ALL_WINDOWS = ("idle", "join", "settle", "load", "steady", "chaos")
CPU_WINDOWS = ("idle", "load", "steady", "chaos")
MEASURED = ("load", "steady", "chaos")
DIRECTIONS = ("higher", "lower", None)

SECTIONS = {
    "tps": "Tick rate and headroom",
    "tick": "Tick thread CPU",
    "cpu": "Process, bot and machine CPU",
    "memory": "Memory",
    "load": "Join, view load and chunk work",
    "work": "Work counters",
    "efficiency": "Efficiency",
    "chaos": "Chaos",
    "health": "Health",
}

SECTION_NOTES = {
    "tps": (
        "TPS comes only from a counter that advances once per executed tick, read through the console's priority "
        "lane every 5 s. Values above 20 in a single sample are reply jitter: kept, never clamped, never published "
        "alone."
    ),
    "tick": (
        "Tick-thread CPU is CPU time, not wall time, so it is not MSPT. It leaves out the time the thread is "
        "blocked on a lock, I/O or a worker: a tick that waits 20 ms for a worker and computes for 30 ms takes "
        "50 ms of wall time and 30 ms of CPU. Below saturation it is a lower bound on MSPT, and tick_busy_frac shows "
        "how close the thread is to full. Wall time per tick needs a hook or the server's own readout, so it is "
        "reported only where it is exact without one: mspt_saturated, below 19.5 TPS, when ticks run back to back. "
        "Servers whose adapter declares no tick thread report these as not_applicable; compare process_cores across "
        "servers instead."
    ),
    "cpu": (
        "process_cores is the cross-server CPU metric. Bot and foreign load are interference checks, read from the "
        "per-CPU busy counters of the whole machine."
    ),
    "memory": (
        "resident_mib is the working set on Windows and VmRSS on Linux; private_mib is PrivateUsage on Windows and "
        "RssAnon + VmSwap on Linux. Never compared across operating systems or runtimes; JVM servers run with a "
        "fixed heap."
    ),
    "load": (
        "U_b is a bot's in-view unique chunk count at settled: chunks within view radius + 1 of the bot's current "
        "chunk, keyed by dimension, so chunks streamed for an earlier position do not count."
    ),
    "work": (
        "Per bot per second over steady. Packet counts use only the bots' packet-mix intervals that lie wholly "
        "inside the window."
    ),
    "efficiency": (
        "Comparable only between arms with equal parity on the scenario's required features; reports show them "
        "next to the parity rows."
    ),
    "chaos": (
        "Chaos runs are shown, never ranked. The tick speed regime comes from applied log lines, never send times."
    ),
    "health": "Log counts follow each adapter's level map and benign list and are never compared across servers.",
}


@dataclass(frozen=True)
class Metric:
    id: str
    unit: str
    window: str | None  # one of ALL_WINDOWS, or "run"
    direction: str | None  # "higher" or "lower" is better; None for work counters and diagnostics
    requires: tuple[str, ...]  # keys of INPUTS
    formula: str
    doc: str
    section: str
    template: str  # the id with <W> for per-window metrics
    stats: tuple[str, ...] = ()  # statistic names of a distribution value; empty for a scalar
    censorable: bool = False  # the value can be a lower bound (``censored: true``)

    @property
    def kind(self) -> str:
        return "distribution" if self.stats else "scalar"


@dataclass(frozen=True)
class Table:
    id: str
    window: str | None
    columns: tuple[str, ...]
    doc: str
    section: str
    template: str


_metrics: list[Metric] = []
_tables: list[Table] = []


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
    censorable: bool = False,
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
                censorable=censorable,
            )
        )


def _table(
    template: str,
    section: str,
    columns: Sequence[str],
    doc: str,
    *,
    window: str | None = None,
    windows: Sequence[str] | None = None,
) -> None:
    for table_id, w in _windows(template, windows):
        _tables.append(Table(table_id, w or window, tuple(columns), doc, section, template))


# --------------------------------------------------------------------------------------------------------------------
# Tick rate and headroom
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "tps_window_<W>",
    "tps",
    "tps",
    ["probe"],
    "(g_b - g_a) / (t_b - t_a) over the replies inside W; unavailable when t_b - t_a < 0.9 (t1 - t0)",
    direction="higher",
    windows=MEASURED,
    doc="The published steady TPS is tps_window_steady. Short phases (idle, join) have only tps_stats: a 5 s probe "
    "cannot cover 90% of them.",
)
_metric(
    "tps_stats_<W>",
    "tps",
    "tps",
    ["probe"],
    "series samples (g_{i+1} - g_i) / (t_{i+1} - t_i) whose two replies lie in W",
    direction="higher",
    windows=ALL_WINDOWS,
    stats=("min", "p5", "median", "max"),
    doc="p5 is left out when n < 20.",
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
    "tps_load_low15",
    "tps",
    "tps",
    ["probe"],
    "as tps_load_low5 with t_j - t_i >= 14.5 s",
    direction="higher",
    window="load",
    doc="Lowest 15 s TPS while views load.",
)
_metric(
    "tps_chaos_low15",
    "tps",
    "tps",
    ["probe"],
    "as tps_load_low15 over the chaos window",
    window="chaos",
)
_metric(
    "ticks_lost_load",
    "ticks",
    "tps",
    ["probe"],
    "20 (t_b - t_a) - (g_b - g_a) over load",
    direction="lower",
    window="load",
    doc="Area of the TPS dip. Reply jitter can make a dip-free window slightly negative; not clamped.",
)
_metric(
    "tick_deficit_frac_load",
    "frac",
    "tps",
    ["probe"],
    "ticks_lost_load / (20 (t_b - t_a))",
    direction="lower",
    window="load",
)
_metric(
    "time_below_19_5_load_s",
    "s",
    "tps",
    ["probe"],
    "sum of (t_{i+1} - t_i) over the load series samples below 19.5",
    direction="lower",
    window="load",
)
_metric(
    "recovery_s",
    "s",
    "tps",
    ["probe"],
    "first reply time t* at or after the end of the first series sample below 19.5 (after bots_start) such that "
    "the window rate over [t*, t* + 30 s] is >= 19.5 and every series sample in it is >= 19.0, minus bots_start; "
    "0 if the rate never drops below 19.5; censored at steady1 - 30 s - bots_start if it never recovers",
    direction="lower",
    window="load",
    censorable=True,
    doc="Searched from bots_start to the end of steady.",
)
_metric(
    "probe_latency_ms_<W>",
    "ms",
    "tps",
    ["probe"],
    "t_i - s_i (receive - send) of the replies received in W",
    direction="lower",
    windows=("load", "steady"),
    stats=("p50", "p99", "max"),
    doc="probe_lagging gate: p99 above 1000 ms in steady.",
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
    "g_b - g_a over idle (over the whole run when the scenario has no idle phase)",
    window="idle",
    doc="probe_monotonic gate: must be positive, so the counter advances with nobody online.",
)

# --------------------------------------------------------------------------------------------------------------------
# Tick thread CPU
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "tick_ms_per_s_<W>",
    "ms_per_s",
    "tick",
    ["tick"],
    "1000 dCPU_tick / dt between the first and last 10 Hz tick-thread sample inside W",
    direction="lower",
    windows=CPU_WINDOWS,
    doc="CPU_tick: GetThreadTimes on Windows, schedstat (fallback stat / SC_CLK_TCK) on Linux.",
)
_metric(
    "tick_busy_frac_<W>",
    "frac",
    "tick",
    ["tick"],
    "tick_ms_per_s / 1000",
    direction="lower",
    windows=CPU_WINDOWS,
    doc="Share of wall time the tick thread was on a CPU; blocked time is not counted.",
)
_metric(
    "tick_cpu_ms_per_tick_<W>",
    "ms_per_tick",
    "tick",
    ["tick", "probe"],
    "1000 dCPU_tick / dG, with CPU interpolated at the replies a and b bounding W and dG = g_b - g_a",
    direction="lower",
    windows=CPU_WINDOWS,
    doc="Tick-thread CPU per executed tick. Not MSPT.",
)
_metric(
    "tick_util_frac_<W>",
    "frac",
    "tick",
    ["tick"],
    "per-second dCPU_tick from the 10 Hz samples, interpolated on a 1 s grid starting at the first sample in W",
    windows=CPU_WINDOWS,
    stats=("p50", "p99"),
)
_metric(
    "tick_saturated_frac_<W>",
    "frac",
    "tick",
    ["tick"],
    "share of the seconds of tick_util_frac_<W> with utilisation >= 0.95",
    direction="lower",
    windows=CPU_WINDOWS,
    doc="A saturated tick thread runs ticks back to back; its TPS then hides how much more work it has.",
)
_metric(
    "tick_cycles_ms_per_s_<W>",
    "ms_per_s",
    "tick",
    ["tick", "run"],
    "1000 dcycles / tsc_hz / dt with the calibrated TSC frequency from run.json",
    windows=CPU_WINDOWS,
    doc="Secondary column, Windows only; not_applicable where cycle counts or the TSC calibration are missing.",
)
_metric(
    "mspt_saturated_<W>",
    "ms",
    "tick",
    ["probe"],
    "1000 / tps_window_<W> when tps_window_<W> < 19.5; not_applicable otherwise",
    direction="lower",
    windows=MEASURED,
    doc="Wall-clock ms per tick, exact only when the server runs ticks back to back.",
)

# --------------------------------------------------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "process_cores_<W>",
    "cores",
    "cpu",
    ["process"],
    "d(server process user + system CPU s) / dt between the first and last 1 Hz sample inside W",
    direction="lower",
    windows=CPU_WINDOWS,
    doc="The cross-server CPU metric. Includes threads that exited.",
)
_metric(
    "aux_cores_<W>",
    "cores",
    "cpu",
    ["process"],
    "d(launcher tree CPU s, excluding the server process) / dt",
    direction="lower",
    windows=CPU_WINDOWS,
    doc="A launcher that stays running next to the server (Endstone's Python CLI); not_applicable without one.",
)
_metric(
    "total_cores_<W>",
    "cores",
    "cpu",
    ["process"],
    "process_cores_<W> + aux_cores_<W> (process_cores_<W> without a launcher)",
    direction="lower",
    windows=CPU_WINDOWS,
)
_metric(
    "bot_cores_<W>",
    "cores",
    "cpu",
    ["machine", "run"],
    "mean over the 1 Hz machine samples in W of the summed busy fraction of the bot CPUs (placement.bot_cpus)",
    windows=CPU_WINDOWS,
    doc="Everything that ran on the bot CPUs: the bots, the harness when it shares them, and any foreign load.",
)
_metric(
    "bot_cpu_frac_<W>",
    "frac",
    "cpu",
    ["machine", "run"],
    "bot_cores_<W> / number of bot CPUs",
    windows=CPU_WINDOWS,
    doc="bot_saturated gate: above the machine profile's bot_cpu_frac_max (0.85) in a measured window.",
)
_metric(
    "harness_cores_<W>",
    "cores",
    "cpu",
    ["run"],
    "harness process tree CPU per second over W, as recorded by the runner (run.json interference.windows)",
    windows=MEASURED,
)
_metric(
    "foreign_cores_<W>",
    "cores",
    "cpu",
    ["machine"],
    "mean over W of machine busy cores minus the server, bot and harness trees",
    windows=CPU_WINDOWS,
    doc="foreign_load gate: machine profile foreign_cores_mean (0.25) in a measured window.",
)
_metric(
    "foreign_cores_peak_10s_<W>",
    "cores",
    "cpu",
    ["machine"],
    "highest mean of the foreign busy cores over 10 consecutive 1 s machine samples in W",
    windows=CPU_WINDOWS,
    doc="foreign_load gate: machine profile foreign_cores_peak_10s (1.5).",
)
_metric(
    "foreign_server_set_cores_<W>",
    "cores",
    "cpu",
    ["machine"],
    "mean over W of the foreign busy cores on the server CPUs",
    windows=CPU_WINDOWS,
    doc="foreign_load gate: machine profile foreign_server_set_cores_mean (0.25).",
)
_table(
    "threads_top_<W>",
    "cpu",
    ("name", "threads", "sum_ms_per_s", "max_ms_per_s", "busy"),
    "Threads grouped by name with digits replaced by #: count, summed and highest CPU ms/s, and how many exceed "
    "100 ms/s. Threads created inside W count from 0; threads that exited count up to their last sample.",
    windows=MEASURED,
)
_table(
    "thread_groups_<W>",
    "cpu",
    ("group", "threads", "sum_ms_per_s", "max_ms_per_s"),
    "The adapter's diagnostic thread groups (thread name regexes). Diagnostic only, never a headline.",
    windows=MEASURED,
)
_table(
    "thread_census_idle",
    "cpu",
    ("name", "threads"),
    "Threads alive at the end of idle, by name with digits replaced by #: the pool sizes the server chose.",
    window="idle",
)

# --------------------------------------------------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------------------------------------------------

for _what, _label in (("private_mib", "private_mib"), ("resident_mib", "resident_mib")):
    _metric(
        f"{_what}_<W>",
        "mib",
        "memory",
        ["process"],
        f"mean of {_label} over the 1 Hz process samples in W",
        direction="lower",
        windows=CPU_WINDOWS,
    )
    _metric(
        f"{_what}_max_<W>",
        "mib",
        "memory",
        ["process"],
        f"maximum of {_label} over the process samples in W",
        direction="lower",
        windows=CPU_WINDOWS,
    )
_metric(
    "threads_max_<W>",
    "count",
    "memory",
    ["process"],
    "maximum thread count of the server process over W",
    windows=CPU_WINDOWS,
)
_metric(
    "handles_max_<W>",
    "count",
    "memory",
    ["process"],
    "maximum handle count of the server process over W",
    windows=CPU_WINDOWS,
    doc="Windows only.",
)

# --------------------------------------------------------------------------------------------------------------------
# Join, view load and chunk work
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "join_ms",
    "ms",
    "load",
    ["bots"],
    "bot clock, spawned - dial of each bot's first session",
    window="join",
    stats=("median", "p90", "max"),
    doc="Not a headline: the bot does not acknowledge chunk blobs before it spawns.",
)
_metric(
    "join_failed",
    "count",
    "load",
    ["bots"],
    "bots that reported join_failed and never spawned",
    direction="lower",
    window="join",
)
_metric(
    "first_chunk_ms",
    "ms",
    "load",
    ["bots"],
    "per bot, its first teleport to its first in-view LevelChunk (bot clock)",
    direction="lower",
    window="load",
    stats=("median", "p90", "max"),
)
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
    "chunks_zero_bots",
    "count",
    "load",
    ["bots"],
    "bots with U_b = 0",
    window="load",
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
    doc="The same chunk count for every server. A null value sets the views_incomplete flag.",
)
_metric(
    "view_t_n_censored",
    "count",
    "load",
    ["bots"],
    "bots that never reached 80 in-view chunks",
    window="load",
)
_metric(
    "delivery_t50_s",
    "s",
    "load",
    ["bots"],
    "first teleport of any bot to the summed in-view chunks of all bots (5 s progress) reaching 50% of their "
    "value at settled",
    direction="lower",
    window="load",
)
_metric(
    "delivery_t90_s",
    "s",
    "load",
    ["bots"],
    "as delivery_t50_s for 90%",
    direction="lower",
    window="load",
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
    "chunks_per_cpu_s_load",
    "chunks_per_cpu_s",
    "load",
    ["bots", "process"],
    "sum of U_b / server process CPU seconds over load",
    direction="higher",
    window="load",
    doc="Efficiency: shown only next to parity.",
)
_metric(
    "resend_ratio",
    "ratio",
    "load",
    ["bots"],
    "LevelChunk packets / unique chunks, from the last stat event at or before settled",
    window="load",
)
_metric(
    "view_fill_frac",
    "frac",
    "load",
    ["bots", "run"],
    "median U_b / disc(r), r the scenario view distance, disc(r) the chunk columns with dx^2 + dz^2 <= r^2 "
    "(disc(10) = 317)",
    window="load",
)
_metric(
    "view_radius_chunks",
    "chunks",
    "load",
    ["bots"],
    "median chunk radius the server granted at spawn",
    window="join",
)
_metric(
    "csg_effective",
    "bool",
    "load",
    ["bots", "run"],
    "median U_b < 0.5 disc(r)",
    window="load",
    doc="csg_matches gate: must equal the scenario's CSG setting on servers that support client-side generation.",
)
_metric(
    "settle_s",
    "s",
    "load",
    ["run"],
    "settled - alljoined, as recorded by the settle rule",
    window="settle",
    doc="Not a load-speed metric: settle is floor-bound by its minimum duration.",
)

# --------------------------------------------------------------------------------------------------------------------
# Work counters
# --------------------------------------------------------------------------------------------------------------------

for _id, _formula, _doc in (
    (
        "corrections_per_bot_s",
        "d(stat corrections) / (bots * dt)",
        "Movement validation; a cost real clients cause less.",
    ),
    ("rx_pkts_per_bot_s", "d(stat rx_pkts) / (bots * dt)", ""),
    ("tx_pkts_per_bot_s", "d(stat tx_pkts) / (bots * dt)", ""),
    ("actor_moves_per_bot_s", "(MoveActorDelta + MoveActorAbsolute) / (bots * dt)", ""),
    ("block_updates_per_bot_s", "(UpdateBlock + UpdateSubChunkBlocks + BlockEvent) / (bots * dt)", ""),
    ("locator_pkts_per_bot_s", "PlayerLocation / (bots * dt)", ""),
):
    _metric(_id, "per_bot_s", "work", ["bots"], _formula, window="steady", doc=_doc)
_metric(
    "rx_kib_per_bot_s",
    "kib_per_bot_s",
    "work",
    ["bots"],
    "d(stat rx_bytes) / 1024 / (bots * dt)",
    window="steady",
)
_metric(
    "players_steady",
    "count",
    "work",
    ["bots"],
    "median of stat online over steady",
    window="steady",
    doc="The bots divisor of the per-bot rates and efficiency metrics.",
)
_metric(
    "airborne_frac_steady",
    "frac",
    "work",
    ["bots", "run"],
    "share of bots at or above the teleport height - 10 in the last progress snapshot of steady",
    window="steady",
    doc="Bots the server never pulled down: no player physics.",
)
_table(
    "pkt_mix_steady",
    "work",
    ("packet", "per_bot_s"),
    "Packets received per bot per second over the packet-mix intervals inside steady, highest first.",
    window="steady",
)
_table(
    "y_distribution_steady",
    "work",
    ("y_from", "bots"),
    "Bot heights in the last progress snapshot of steady, in 32-block bins.",
    window="steady",
)

# --------------------------------------------------------------------------------------------------------------------
# Efficiency
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "cpu_ms_per_player_tick_steady",
    "ms",
    "efficiency",
    ["process", "probe", "bots"],
    "1000 process_cores_steady / (players_steady * tps_window_steady)",
    direction="lower",
    window="steady",
)
_metric(
    "ticks_per_cpu_s_steady",
    "ticks_per_cpu_s",
    "efficiency",
    ["process", "probe"],
    "tps_window_steady / process_cores_steady",
    direction="higher",
    window="steady",
)

# --------------------------------------------------------------------------------------------------------------------
# Chaos
# --------------------------------------------------------------------------------------------------------------------

_metric(
    "tps_chaos_normal",
    "tps",
    "chaos",
    ["probe", "chaos"],
    "median of the chaos TPS samples under random tick speed 0, 1 or 3; samples a change fell inside are excluded",
    window="chaos",
)
_metric(
    "tps_chaos_high",
    "tps",
    "chaos",
    ["probe", "chaos"],
    "as tps_chaos_normal under random tick speed 100 or 1000",
    window="chaos",
)
_metric(
    "chaos_mixed_samples",
    "count",
    "chaos",
    ["probe", "chaos"],
    "TPS samples with a tick speed change strictly between their two replies",
    window="chaos",
)
_metric(
    "tickspeed_changes_queued",
    "count",
    "chaos",
    ["chaos"],
    "tick speed commands sent inside the chaos window",
    window="chaos",
)
_metric(
    "tickspeed_changes_applied",
    "count",
    "chaos",
    ["chaos"],
    "tick speed changes the server's log confirmed inside the chaos window",
    window="chaos",
)
for _what, _formula in (
    ("planned", "actions in the executor journal"),
    ("sent", "actions written to the console"),
    ("applied", "actions the server's log confirmed"),
    ("late", "actions sent more than 5 s after their scheduled time"),
    ("unsupported", "actions the adapter cannot render (counted, never sent)"),
    ("skip_offline", "actions whose target bot was offline"),
    ("overrun", "actions not sent because the console writer fell more than the overrun lag behind"),
):
    _metric(f"chaos_actions_{_what}", "count", "chaos", ["chaos"], _formula, window="chaos")
_metric(
    "chaos_lag_s",
    "s",
    "chaos",
    ["chaos"],
    "sent - scheduled of every sent action",
    window="chaos",
    stats=("p50", "p99", "max"),
)
_metric(
    "not_respawned",
    "count",
    "chaos",
    ["bots"],
    "bots whose last death after chaos0 was not followed by a respawn",
    window="chaos",
)
_metric(
    "memory_slope_mib_per_h_chaos",
    "mib_per_h",
    "chaos",
    ["process"],
    "least-squares slope of private_mib over the chaos window",
    window="chaos",
    doc="Labelled weak (reason) when the window is shorter than 30 min.",
)
for _what in ("threads", "handles"):
    for _end in ("start", "end"):
        _metric(
            f"{_what}_{_end}_chaos",
            "count",
            "chaos",
            ["process"],
            f"{_what} of the server process at the {'first' if _end == 'start' else 'last'} sample of chaos",
            window="chaos",
        )
_table(
    "chaos_regimes",
    "chaos",
    ("regime", "seconds", "samples", "median_tps"),
    "Time and TPS samples per applied random tick speed.",
    window="chaos",
)
_table(
    "chaos_actions",
    "chaos",
    ("action", "planned", "sent", "applied", "late", "unsupported", "skip_offline", "lag_p50_s", "lag_p99_s"),
    "Per-action counts from the executor journal.",
    window="chaos",
)
_table(
    "log_errors_chaos",
    "chaos",
    ("category", "lines"),
    "Error and fatal lines inside chaos with numbers and quoted strings replaced, most frequent first (top 30).",
    window="chaos",
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
_metric("crash_files", "count", "health", ["run"], "crash files collected into crash/", window="run")
for _level in ("warn", "error", "fatal"):
    _metric(
        f"log_{_level}_lines",
        "count",
        "health",
        ["log"],
        f"server log lines at level {_level} after the adapter's level map"
        + ("; lines on the adapter's benign list are not counted" if _level != "warn" else ""),
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
    "stat dropped at the end of steady (before chaos starts)",
    window="run",
    doc="Warning when nonzero outside chaos.",
)
_metric(
    "rejoin_failed",
    "count",
    "health",
    ["bots"],
    "join_failed events from bots that had spawned before",
    window="run",
)
_metric("nudges", "count", "health", ["run"], "settle nudges (run.json interventions)", window="settle")
_metric("retp", "count", "health", ["run"], "settle re-teleports (run.json interventions)", window="settle")
_metric(
    "interventions_in_measured",
    "count",
    "health",
    ["run", "commands"],
    "the larger of the runner's count and the console commands with an origin other than the probe sent inside steady",
    window="steady",
    doc="interventions_in_measured gate: must be 0.",
)
_table(
    "log_errors_first",
    "health",
    ("t_s", "level", "text"),
    "The first 20 error and fatal lines that are not on the adapter's benign list.",
    window="run",
)

METRICS: tuple[Metric, ...] = tuple(_metrics)
TABLES: tuple[Table, ...] = tuple(_tables)
_BY_ID = {m.id: m for m in METRICS}
_TABLE_BY_ID = {t.id: t for t in TABLES}


def get(metric_id: str) -> Metric:
    return _BY_ID[metric_id]


def ids() -> list[str]:
    return [m.id for m in METRICS]


def table(table_id: str) -> Table:
    return _TABLE_BY_ID[table_id]


def problems() -> list[str]:
    """Declaration errors: duplicate ids, unknown units, sections or inputs, ids that do not carry their unit."""
    found: list[str] = []
    seen: set[str] = set()
    for metric in METRICS:
        if metric.id in seen or metric.id in _TABLE_BY_ID:
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
    for tbl in TABLES:
        if tbl.section not in SECTIONS:
            found.append(f"{tbl.id}: unknown section {tbl.section!r}")
    if len(_TABLE_BY_ID) != len(TABLES):
        found.append("a table id is declared twice")
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

Every metric in `metrics.json` (schema `{schema}`), analysis version {analysis}. `bbench analyze` recomputes the
file byte for byte from a run's raw files; any change to a formula bumps the analysis version.

## Conventions

- **Time.** Every sample is stamped with the harness monotonic clock. Phase marks are seconds since the run's origin.
  Bot events are mapped onto the same timebase through their own monotonic clock (fallback: their UTC stamp and the
  run's origin pair), so nothing depends on the time of day.
- **Windows.** `idle` = [idle0, idle1], `join` = [bots_start, alljoined], `settle` = [alljoined, settled],
  `load` = [bots_start, settled], `steady` = [steady0, steady1], `chaos` = [chaos0, chaos1]. A window whose end mark
  is missing was not reached; its metrics are `unavailable`. A window the scenario does not have is
  `not_applicable`.
- **Probe notation.** Replies `(s_i, t_i, g_i)` = send time, receive time, tick counter. In a window `[t0, t1]`,
  `a = min{{i: t_i >= t0}}` and `b = max{{i: t_i <= t1}}`. A series sample, the rate between two consecutive
  replies, belongs to a window when both replies do.
- **Rates over samples** use the span the samples cover inside the window: first sample at or after `t0` to last
  at or before `t1`.
- **Percentiles** are nearest rank, `sorted[round(q (n - 1))]` with halves rounded up; medians are conventional.
  Every statistic reports its `n`. p5 is left out when n < 20.
- **Units.** Each id carries its unit (`_s`, `_ms`, `_mib`, `_cores`, `_ms_per_s`, `_frac`, `_per_bot_s`, ...).
  MiB = 2^20 bytes; reports show GiB = MiB / 1024. Counts and booleans carry no suffix.
- **Availability.** `measured`, `not_applicable` (the server or scenario has no such quantity) or `unavailable`
  (should exist but could not be computed; `reason` says why). A censored value is a lower bound (`censored: true`,
  shown as `>value`).
- **Direction.** `higher` or `lower` is better; `-` marks work counters and diagnostics, which are never ranked.
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
            if metric.censorable:
                value += ", censorable"
            formula = metric.formula + (f". {metric.doc}" if metric.doc else "")
            out.append(
                f"| `{metric.template}` | {', '.join(str(w) for w in windows)} | {UNITS[metric.unit].label} | "
                f"{value} | {metric.direction or '-'} | {', '.join(metric.requires)} | {_cell(formula)} |"
            )
        out.append("")
        tables = [t for t in TABLES if t.section == section]
        if tables:
            out.append("Tables:\n")
            out.append("| Table | Windows | Columns | Content |")
            out.append("|---|---|---|---|")
            done = set()
            for tbl in tables:
                if tbl.template in done:
                    continue
                done.add(tbl.template)
                windows = [t.window for t in TABLES if t.template == tbl.template]
                out.append(
                    f"| `{tbl.template}` | {', '.join(str(w) for w in windows)} | {', '.join(tbl.columns)} | "
                    f"{_cell(tbl.doc)} |"
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
