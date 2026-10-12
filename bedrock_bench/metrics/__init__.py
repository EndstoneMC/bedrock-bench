# SPDX-License-Identifier: Apache-2.0
"""Metrics: pure functions from a run's raw data to ``metrics.json`` (docs/metrics.md).

``compute(run)`` is deterministic: the same raw files give the same bytes on every platform, so ``bbench analyze``
can recompute any stored run. It emits every metric declared in :mod:`bedrock_bench.metrics.registry`, each with an
availability, then derives the run status from the validity gates in :mod:`bedrock_bench.metrics.health`.

Every series is moved onto the run's timebase first: ``t_ns`` fields become nanoseconds since the run origin and
window bounds are seconds since the origin, the same unit as the phase marks in run.json.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from bedrock_bench import ANALYSIS_VERSION, METRICS_SCHEMA, SCHEMA_MINOR
from bedrock_bench.metrics import chaos, cpu, health, load, memory, registry, tps, workload
from bedrock_bench.metrics.health import ServerTraits
from bedrock_bench.metrics.stats import Censored, censored_median, distribution, median
from bedrock_bench.model import (
    WINDOWS,
    Availability,
    BotParams,
    CpuSample,
    ExitCode,
    Grid,
    MachineSample,
    PhaseSpec,
    ProbeParams,
    ProbeSample,
    ProcSample,
    RunData,
    Scenario,
    ServerParams,
    Status,
    ThreadSample,
    ThreadSeries,
    WorldParams,
)

#: Decimals kept for every float in metrics.json.
FLOAT_DIGITS = 4

#: Marks that open and close each window.
WINDOW_MARKS: Mapping[str, tuple[str, str]] = {
    "idle": ("idle0", "idle1"),
    "join": ("bots_start", "alljoined"),
    "settle": ("alljoined", "settled"),
    "load": ("bots_start", "settled"),
    "steady": ("steady0", "steady1"),
    "chaos": ("chaos0", "chaos1"),
}

#: The scenario phase each window belongs to, to tell "not in this scenario" from "not reached".
_WINDOW_PHASE = {
    "idle": "idle",
    "join": "join",
    "settle": "settle",
    "load": "settle",
    "steady": "steady",
    "chaos": "chaos",
}

#: Metrics computed from the tick counter: unavailable when the adapter declares no monotonic counter.
_TICK_COUNTER_PREFIXES = (
    "tps_",
    "ticks_lost_",
    "tick_deficit_",
    "time_below_",
    "recovery_s",
    "tick_cpu_ms_per_tick_",
    "mspt_saturated_",
    "cpu_ms_per_player_tick_",
    "ticks_per_cpu_s_",
)

NS_PER_S = 1e9


def window(run: RunData | Mapping[str, Any], name: str) -> tuple[float, float] | None:
    """``(t0, t1)`` of a window in seconds since the run origin; None when a bounding mark is missing."""
    meta = run.run if isinstance(run, RunData) else run
    marks = meta.get("marks") or {}
    start, end = WINDOW_MARKS[name]
    t0, t1 = marks.get(start), marks.get(end)
    if t0 is None or t1 is None or t1 < t0:
        return None
    return float(t0), float(t1)


def scenario_of(run: Mapping[str, Any]) -> Scenario:
    """The scenario a run used, rebuilt from run.json ``scenario.resolved`` (defaults when it is missing)."""

    def build(cls: type, data: Mapping[str, Any] | None, **required: Any) -> Any:
        names = {f.name for f in dataclasses.fields(cls)}
        values = {**required, **{k: v for k, v in (data or {}).items() if k in names}}
        return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in values.items()})

    info = run.get("scenario") or {}
    resolved = info.get("resolved") or {}
    phases = tuple(
        PhaseSpec(
            name=p["name"],
            delay_s=p.get("delay_s", 0.0),
            duration_s=p.get("duration_s"),
            cap_s=p.get("cap_s"),
            options=dict(p.get("options") or {}),
        )
        for p in resolved.get("phases") or ()
    )
    return Scenario(
        name=info.get("name", "unknown"),
        sha256=info.get("sha256", ""),
        bots=build(BotParams, resolved.get("bots")),
        grid=build(Grid, resolved.get("grid")),
        world=build(WorldParams, resolved.get("world")),
        server=build(ServerParams, resolved.get("server"), csg=False),
        phases=phases,
        probe=build(ProbeParams, resolved.get("probe")),
        exercises=tuple(resolved.get("exercises") or ()),
        requires=tuple(resolved.get("requires") or ()),
        resolved=resolved,
    )


# --------------------------------------------------------------------------------------------------------------------
# One run on its own timebase
# --------------------------------------------------------------------------------------------------------------------


class _Run:
    """A run prepared for the formulas: series rebased to the origin, bot events timed, windows resolved."""

    def __init__(self, data: RunData, traits: ServerTraits) -> None:
        self.data = data
        self.meta = data.run
        self.traits = traits
        self.scenario = scenario_of(self.meta)
        self.marks: Mapping[str, Any] = self.meta.get("marks") or {}
        time_info = self.meta.get("time") or {}
        o = self.origin_ns = int(time_info.get("origin_mono_ns", 0))
        epoch = int(time_info.get("origin_epoch_ms", 0))
        self.probe = [ProbeSample(s.send_ns - o, s.rx_ns - o, s.tick) for s in data.probe]
        self.tick = [CpuSample(s.t_ns - o, s.cpu_ns, s.cycles) for s in data.tick]
        self.threads = ThreadSeries(
            [ThreadSample(s.t_ns - o, s.tid, s.name, s.cpu_ns, s.cycles) for s in data.threads.samples]
        )
        self.process: list[ProcSample] = [dataclasses.replace(s, t_ns=s.t_ns - o) for s in data.process]
        self.machine: list[MachineSample] = [dataclasses.replace(s, t_ns=s.t_ns - o) for s in data.machine]
        self.bots = load.timed_events(data.bots, o, epoch)
        self.chaos = chaos.records(data.chaos, o)
        self.errors = [
            line for line in data.log if line.level in ("error", "fatal") and not traits.is_benign(line.text)
        ]

    def seconds(self, mono_ns: int) -> float:
        return (mono_ns - self.origin_ns) / NS_PER_S

    def bounds(self, name: str) -> tuple[float, float] | None:
        return window(self.meta, name)

    def has_phase(self, name: str) -> bool:
        if self.scenario.phases:
            return self.scenario.has_phase(name)
        return self.marks.get(WINDOW_MARKS[name][0]) is not None

    def why_not(self, name: str) -> tuple[Availability, str]:
        phase = _WINDOW_PHASE[name]
        if not self.has_phase(phase):
            return Availability.NOT_APPLICABLE, f"the scenario has no {phase} phase"
        return Availability.UNAVAILABLE, f"the {name} window was not reached"

    def process_n(self, t0: float, t1: float) -> int:
        return sum(1 for p in self.process if t0 <= p.t_ns / NS_PER_S <= t1)

    def machine_n(self, t0: float, t1: float) -> int:
        return sum(1 for m in self.machine if t0 <= m.t_ns / NS_PER_S <= t1)


class _Out:
    """Metric values and tables as sections produce them."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.tables: dict[str, list[list[Any]]] = {}

    def measured(
        self, metric_id: str, value: Any, *, n: int | None = None, censored: bool = False, reason: str | None = None
    ) -> None:
        registry.get(metric_id)  # an undeclared id fails loudly
        entry: dict[str, Any] = {"value": value, "n": n, "availability": Availability.MEASURED.value}
        if censored:
            entry["censored"] = True
        if reason:
            entry["reason"] = reason
        self.values[metric_id] = entry

    def missing(self, metric_id: str, availability: Availability, reason: str) -> None:
        registry.get(metric_id)
        self.values[metric_id] = {"value": None, "n": None, "availability": availability.value, "reason": reason}

    def value(self, metric_id: str, value: Any, reason: str, *, n: int | None = None) -> None:
        """Measured when ``value`` is not None, else unavailable with ``reason``."""
        if value is None:
            self.missing(metric_id, Availability.UNAVAILABLE, reason)
        else:
            self.measured(metric_id, value, n=n)

    def table(self, table_id: str, rows: list[list[Any]]) -> None:
        registry.table(table_id)
        self.tables[table_id] = rows

    def get(self, metric_id: str) -> Any:
        entry = self.values.get(metric_id)
        return entry["value"] if entry and entry["availability"] == Availability.MEASURED.value else None


def _each_window(r: _Run, windows: Sequence[str], fill: Callable[[str, float, float], None]) -> None:
    """``fill(window, t0, t1)`` for each reached window; metrics of the others are filled in by ``compute``."""
    for w in windows:
        bounds = r.bounds(w)
        if bounds is not None:
            fill(w, *bounds)


def _median_int(values: Sequence[int]) -> int | None:
    med = median(values)
    return None if med is None else round(med)


# --------------------------------------------------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------------------------------------------------


def _tps_section(out: _Out, r: _Run) -> None:
    probe = r.probe

    def tps_window(w: str, t0: float, t1: float) -> None:
        out.value(
            f"tps_window_{w}",
            tps.window(probe, t0, t1),
            "probe replies cover less than 90% of the window",
            n=tps.count(probe, t0, t1),
        )

    def tps_stats(w: str, t0: float, t1: float) -> None:
        stats = tps.stats(probe, t0, t1)
        out.value(f"tps_stats_{w}", stats, "fewer than two probe replies in the window", n=stats and stats["n"])

    _each_window(r, registry.MEASURED, tps_window)
    _each_window(r, WINDOWS, tps_stats)

    for metric_id, w, span in (
        ("tps_load_low5", "load", 4.5),
        ("tps_load_low15", "load", 14.5),
        ("tps_chaos_low15", "chaos", 14.5),
    ):
        bounds = r.bounds(w)
        if bounds is not None:
            out.value(
                metric_id,
                tps.lowest(probe, *bounds, span),
                f"no stretch of {span:g} s inside the window",
                n=tps.count(probe, *bounds),
            )

    load_bounds = r.bounds("load")
    if load_bounds is not None:
        n = tps.count(probe, *load_bounds)
        lost = tps.ticks_lost(probe, *load_bounds)
        span = tps.covered(probe, *load_bounds)
        out.value("ticks_lost_load", lost, "fewer than two probe replies in load", n=n)
        deficit = lost / (tps.TARGET_TPS * span) if lost is not None and span else None
        out.value("tick_deficit_frac_load", deficit, "fewer than two probe replies in load", n=n)
        out.value(
            "time_below_19_5_load_s",
            tps.time_below(probe, *load_bounds),
            "no probe samples in load",
            n=len(tps.samples_in(probe, *load_bounds)),
        )
        end = r.marks.get("steady1") or r.marks["settled"]
        recovery = tps.recovery_s(probe, load_bounds[0], float(end))
        if recovery is None:
            out.missing("recovery_s", Availability.UNAVAILABLE, "no probe samples after bots_start")
        elif isinstance(recovery, Censored):
            out.measured("recovery_s", recovery.bound, censored=True)
        else:
            out.measured("recovery_s", recovery)

    def latency(w: str, t0: float, t1: float) -> None:
        values = tps.latency_ms(probe, t0, t1)
        out.value(
            f"probe_latency_ms_{w}",
            distribution(values, ("p50", "p99", "max")),
            "no probe replies in the window",
            n=len(values),
        )

    def gap(w: str, t0: float, t1: float) -> None:
        out.measured(f"probe_max_gap_s_{w}", tps.max_gap(probe, t0, t1), n=tps.count(probe, t0, t1))

    _each_window(r, ("load", "steady"), latency)
    _each_window(r, registry.MEASURED, gap)

    out.value("probe_tick_decreases", tps.decreases(probe) if probe else None, "no probe replies", n=len(probe))
    idle = r.bounds("idle")
    if idle is None and not r.has_phase("idle") and probe:
        replies = tps.replies(probe)
        idle = (replies[0].t, replies[-1].t)
    if idle is not None:
        out.value(
            "probe_idle_ticks", tps.advanced(probe, *idle), "fewer than two probe replies", n=tps.count(probe, *idle)
        )


def _tick_section(out: _Out, r: _Run) -> None:
    tsc_hz = (r.meta.get("machine") or {}).get("tsc_hz_calibrated")
    names = (
        "tick_ms_per_s",
        "tick_busy_frac",
        "tick_cpu_ms_per_tick",
        "tick_util_frac",
        "tick_saturated_frac",
        "tick_cycles_ms_per_s",
    )

    def fill(w: str, t0: float, t1: float) -> None:
        if not r.tick and r.traits.tick_thread_rule == "none":
            for name in names:
                out.missing(f"{name}_{w}", Availability.NOT_APPLICABLE, "the server has no single tick thread")
            return
        result = cpu.tick(r.tick, r.probe, t0, t1, tsc_hz=tsc_hz)
        if result is None:
            for name in names:
                out.missing(f"{name}_{w}", Availability.UNAVAILABLE, "fewer than two tick-thread samples in the window")
            return
        out.measured(f"tick_ms_per_s_{w}", result.ms_per_s, n=result.samples)
        out.measured(f"tick_busy_frac_{w}", result.busy_frac, n=result.samples)
        out.value(
            f"tick_cpu_ms_per_tick_{w}",
            result.ms_per_tick,
            "no executed ticks between probe replies covered by tick samples",
            n=result.ticks,
        )
        short = "the window is shorter than one second"
        out.value(f"tick_util_frac_{w}", distribution(result.util, ("p50", "p99")), short, n=len(result.util))
        out.value(f"tick_saturated_frac_{w}", result.saturated_frac, short, n=len(result.util))
        if result.cycles_ms_per_s is None:
            out.missing(
                f"tick_cycles_ms_per_s_{w}",
                Availability.NOT_APPLICABLE,
                "no cycle counts or no calibrated TSC frequency",
            )
        else:
            out.measured(f"tick_cycles_ms_per_s_{w}", result.cycles_ms_per_s, n=result.samples)

    def mspt(w: str, t0: float, t1: float) -> None:
        rate = tps.window(r.probe, t0, t1)
        if rate is None:
            out.missing(f"mspt_saturated_{w}", Availability.UNAVAILABLE, "no window TPS")
        elif rate >= tps.HEALTHY_TPS or rate <= 0:
            out.missing(
                f"mspt_saturated_{w}", Availability.NOT_APPLICABLE, "tps_window >= 19.5: ticks are not back to back"
            )
        else:
            out.measured(f"mspt_saturated_{w}", 1000.0 / rate, n=tps.count(r.probe, t0, t1))

    _each_window(r, registry.CPU_WINDOWS, fill)
    _each_window(r, registry.MEASURED, mspt)


def _cpu_section(out: _Out, r: _Run) -> None:
    bot_cpus = list((r.meta.get("placement") or {}).get("bot_cpus") or ())
    recorded = (r.meta.get("interference") or {}).get("windows") or {}

    def fill(w: str, t0: float, t1: float) -> None:
        n_proc, n_machine = r.process_n(t0, t1), r.machine_n(t0, t1)
        short = "fewer than two process samples in the window"
        process = cpu.process_cores(r.process, t0, t1)
        aux = cpu.aux_cores(r.process, t0, t1)
        out.value(f"process_cores_{w}", process, short, n=n_proc)
        if aux is None and process is not None:
            out.missing(f"aux_cores_{w}", Availability.NOT_APPLICABLE, "the server has no launcher process")
            out.measured(f"total_cores_{w}", process, n=n_proc)
        else:
            out.value(f"aux_cores_{w}", aux, short, n=n_proc)
            total = process + aux if process is not None and aux is not None else None
            out.value(f"total_cores_{w}", total, short, n=n_proc)

        busy = cpu.cpu_set_busy(r.machine, bot_cpus, t0, t1)
        reason = "no machine samples in the window" if bot_cpus else "no bot CPUs in run.json placement"
        out.value(f"bot_cores_{w}", busy, reason, n=n_machine)
        out.value(f"bot_cpu_frac_{w}", busy / len(bot_cpus) if busy is not None else None, reason, n=n_machine)
        foreign = cpu.foreign(r.machine, t0, t1)
        for metric_id, attr in (
            (f"foreign_cores_{w}", "cores"),
            (f"foreign_cores_peak_10s_{w}", "peak_10s"),
            (f"foreign_server_set_cores_{w}", "server_set_cores"),
        ):
            out.value(
                metric_id, getattr(foreign, attr) if foreign else None, "no machine samples in the window", n=n_machine
            )

    def harness(w: str, t0: float, t1: float) -> None:
        value = (recorded.get(w) or {}).get("harness_cores")
        out.value(f"harness_cores_{w}", None if value is None else float(value), "not recorded in run.json")

    def threads(w: str, t0: float, t1: float) -> None:
        rows = cpu.thread_rows(r.threads, t0, t1)
        out.table(f"threads_top_{w}", cpu.top_threads(rows))
        out.table(f"thread_groups_{w}", cpu.group_threads(rows, r.traits.thread_groups) if rows else [])

    _each_window(r, registry.CPU_WINDOWS, fill)
    _each_window(r, registry.MEASURED, harness)
    _each_window(r, registry.MEASURED, threads)
    _each_window(
        r,
        ("idle",),
        lambda w, t0, t1: out.table("thread_census_idle", [[k, v] for k, v in cpu.census(r.threads, t1).items()]),
    )


def _memory_section(out: _Out, r: _Run) -> None:
    names = ("private_mib", "private_mib_max", "resident_mib", "resident_mib_max", "threads_max", "handles_max")

    def fill(w: str, t0: float, t1: float) -> None:
        mem = memory.window(r.process, t0, t1)
        if mem is None:
            for name in names:
                out.missing(f"{name}_{w}", Availability.UNAVAILABLE, "no process samples in the window")
            return
        out.measured(f"private_mib_{w}", mem.private_mean, n=mem.samples)
        out.measured(f"private_mib_max_{w}", mem.private_max, n=mem.samples)
        out.measured(f"resident_mib_{w}", mem.resident_mean, n=mem.samples)
        out.measured(f"resident_mib_max_{w}", mem.resident_max, n=mem.samples)
        out.measured(f"threads_max_{w}", mem.threads_max, n=mem.samples)
        if mem.handles_max is None:
            out.missing(f"handles_max_{w}", Availability.NOT_APPLICABLE, "handle counts exist on Windows only")
        else:
            out.measured(f"handles_max_{w}", mem.handles_max, n=mem.samples)

    _each_window(r, registry.CPU_WINDOWS, fill)


def _load_section(out: _Out, r: _Run) -> None:
    bots = r.bots
    if r.marks.get("bots_start") is not None:
        joins = load.join(bots)
        out.value(
            "join_ms", distribution(joins.join_ms, ("median", "p90", "max")), "no bot spawned", n=len(joins.join_ms)
        )
        out.measured("join_failed", joins.failed)
        out.value("view_radius_chunks", load.granted_radius(bots), "no spawned events with a radius")

    settle = (r.meta.get("settle") or {}).get("settle_s")
    settle_bounds = r.bounds("settle")
    if settle is None and settle_bounds is not None:
        settle = settle_bounds[1] - settle_bounds[0]
    if settle is not None:
        out.measured("settle_s", float(settle))

    bounds = r.bounds("load")
    if bounds is None:
        return
    bots_start, settled = bounds
    first = load.first_chunk_ms(bots)
    out.value("first_chunk_ms", distribution(first, ("median", "p90", "max")), "no first_chunk events", n=len(first))

    view = load.view(bots, settled)
    u = list(view.u.values())
    n = len(u)
    no_progress = "no progress snapshot before settled"
    out.value("chunks_per_bot", distribution(u, ("min", "median", "max")), no_progress, n=n)
    out.value("chunks_zero_bots", sum(1 for v in u if v == 0) if u else None, no_progress, n=n)
    out.value(
        "view_t90_own_s",
        distribution(view.t90_own, ("median", "p90", "max")),
        "no bot reached 90% of its view",
        n=len(view.t90_own),
    )

    censored = sum(1 for v in view.t_n if isinstance(v, Censored))
    t_n = censored_median(view.t_n)
    if not view.t_n:
        out.missing("view_t_n_s", Availability.UNAVAILABLE, "no teleported bots with progress")
        out.missing("view_t_n_censored", Availability.UNAVAILABLE, "no teleported bots with progress")
    else:
        if t_n is None:
            out.missing(
                "view_t_n_s",
                Availability.UNAVAILABLE,
                f"median censored: {censored} of {len(view.t_n)} bots never reached {view.n_target} chunks",
            )
        else:
            out.measured("view_t_n_s", t_n, n=len(view.t_n))
        out.measured("view_t_n_censored", censored, n=len(view.t_n))

    start = load.first_tp(bots)
    shares = load.delivery(bots, start, settled) if start is not None else {}
    out.value("delivery_t50_s", shares.get(0.5), "no teleports or no in-view chunks at settled")
    out.value("delivery_t90_s", shares.get(0.9), "no teleports or no in-view chunks at settled")

    total = sum(u)
    out.value(
        "chunks_per_s_load", total / (settled - bots_start) if u and settled > bots_start else None, no_progress, n=n
    )
    cpu_s = cpu.process_cpu_s(r.process, bots_start, settled)
    out.value(
        "chunks_per_cpu_s_load",
        total / cpu_s if u and cpu_s else None,
        "no progress snapshot or no process CPU over load",
        n=n,
    )
    out.value("resend_ratio", load.resend_ratio(bots, settled), "no stat event with chunks before settled")
    radius = r.scenario.server.view_distance
    med = median(u)
    out.value("view_fill_frac", None if med is None else med / load.disc(radius), no_progress, n=n)
    out.value("csg_effective", load.csg_effective(view.u, radius), no_progress, n=n)


def _work_section(out: _Out, r: _Run) -> None:
    bounds = r.bounds("steady")
    if bounds is None:
        return
    online = workload.online(r.bots, *bounds)
    players = _median_int(online)
    out.value("players_steady", players, "no stat events in steady", n=len(online))
    n = players or out.get("joined") or r.scenario.bots.n
    rates = workload.counters(r.bots, *bounds, n)
    for name in ("corrections", "rx_pkts", "tx_pkts"):
        out.value(f"{name}_per_bot_s", rates[name], "fewer than two stat events in steady")
    out.value("rx_kib_per_bot_s", rates["rx_kib"], "fewer than two stat events in steady")
    for name in workload.PACKET_GROUPS:
        out.value(f"{name}_per_bot_s", rates[name], "no packet-mix interval lies wholly inside steady")
    ys = workload.heights(r.bots, *bounds)
    out.value(
        "airborne_frac_steady",
        workload.airborne_frac(ys, r.scenario.grid.y),
        "no placed bots in a progress snapshot of steady",
        n=len(ys),
    )
    out.table("pkt_mix_steady", workload.mix_table(workload.packet_mix(r.bots, *bounds), n))
    out.table("y_distribution_steady", workload.y_table(ys))


def _efficiency_section(out: _Out, r: _Run) -> None:
    if r.bounds("steady") is None:
        return
    cores = out.get("process_cores_steady")
    rate = out.get("tps_window_steady")
    players = out.get("players_steady")
    per_player = 1000.0 * cores / (players * rate) if cores is not None and rate and players else None
    out.value("cpu_ms_per_player_tick_steady", per_player, "needs process_cores, tps_window and players in steady")
    out.value(
        "ticks_per_cpu_s_steady",
        rate / cores if rate is not None and cores else None,
        "needs tps_window and process_cores in steady",
    )


def _chaos_section(out: _Out, r: _Run) -> None:
    bounds = r.bounds("chaos")
    if bounds is None:
        return
    t0, t1 = bounds
    split = chaos.regimes(r.probe, chaos.regime_changes(r.chaos), t0, t1)
    normal, high = split.group(chaos.NORMAL_REGIMES), split.group(chaos.HIGH_REGIMES)
    out.value("tps_chaos_normal", median(normal), "no unmixed samples under tick speed 0, 1 or 3", n=len(normal))
    out.value("tps_chaos_high", median(high), "no unmixed samples under tick speed 100 or 1000", n=len(high))
    out.measured("chaos_mixed_samples", split.mixed)
    out.measured("tickspeed_changes_queued", chaos.tickspeed_queued(r.chaos, t0, t1))
    out.measured("tickspeed_changes_applied", split.applied)
    out.table("chaos_regimes", chaos.regime_table(split))

    totals, per_action = chaos.actions(r.chaos)
    for key in ("planned", "sent", "applied", "late", "unsupported", "skip_offline", "overrun"):
        out.measured(f"chaos_actions_{key}", getattr(totals, key))
    out.value("chaos_lag_s", distribution(totals.lags, ("p50", "p99", "max")), "no action was sent", n=len(totals.lags))
    out.table("chaos_actions", chaos.action_table(per_action))
    out.measured("not_respawned", chaos.not_respawned(r.bots, t0))
    out.table("log_errors_chaos", chaos.error_table(e for e in r.errors if t0 <= r.seconds(e.t_ns) <= t1))

    slope = memory.slope_mib_per_h(r.process, t0, t1)
    if slope is None:
        out.missing("memory_slope_mib_per_h_chaos", Availability.UNAVAILABLE, "fewer than two process samples")
    else:
        weak = "weak: window shorter than 30 min" if t1 - t0 < memory.SLOPE_MIN_S else None
        out.measured("memory_slope_mib_per_h_chaos", slope, n=r.process_n(t0, t1), reason=weak)
    ends = memory.start_end(r.process, t0, t1)
    for name, sample in (("start", ends and ends[0]), ("end", ends and ends[1])):
        out.value(f"threads_{name}_chaos", sample.threads if sample else None, "no process samples in chaos")
        if sample and sample.handles is None:
            out.missing(f"handles_{name}_chaos", Availability.NOT_APPLICABLE, "handle counts exist on Windows only")
        else:
            out.value(f"handles_{name}_chaos", sample.handles if sample else None, "no process samples in chaos")


def _health_section(out: _Out, r: _Run) -> None:
    recorded = r.meta.get("health") or {}
    out.value("server_exit_code", recorded.get("server_exit"), "not recorded in run.json")
    out.value("died_during_run", recorded.get("died"), "not recorded in run.json")
    crash = recorded.get("crash_files")
    out.value("crash_files", None if crash is None else len(crash), "not recorded in run.json")

    log = r.data.log
    out.measured("log_warn_lines", sum(1 for line in log if line.level == "warn"), n=len(log))
    for level in ("error", "fatal"):
        out.measured(f"log_{level}_lines", sum(1 for line in r.errors if line.level == level), n=len(log))
    out.table("log_errors_first", [[r.seconds(e.t_ns), e.level, e.text] for e in r.errors[:20]])

    bots = r.bots
    totals = [int(e.data.get("decode_errors", 0)) for e in bots if e.ev == "stat"]
    totals += [int(e.data.get("total", 1)) for e in bots if e.ev == "decode_error"]
    if recorded.get("decode_errors") is not None:
        totals.append(int(recorded["decode_errors"]))
    out.value("decode_errors", max(totals, default=None), "no bot events")
    if bots:
        joins = load.join(bots)
        out.measured("joined", joins.joined)
        out.measured("rejoin_failed", joins.rejoin_failed)
    else:
        out.value("joined", recorded.get("joined"), "no bot events")
        out.missing("rejoin_failed", Availability.UNAVAILABLE, "no bot events")
    end = r.marks.get("chaos0") or r.marks.get("steady1") or r.marks.get("stop")
    last = load.last_at(bots, "stat", float(end)) if end is not None else None
    dropped = int(last.data.get("dropped", 0)) if last is not None else recorded.get("dropped")
    out.value("dropped", dropped, "no stat event before chaos")

    interventions = r.meta.get("interventions") or {}
    for key in ("nudges", "retp"):
        out.value(key, interventions.get(key), "not recorded in run.json")
    steady = r.bounds("steady")
    if steady is not None:
        sent = sum(
            1
            for c in r.data.commands
            if c.get("origin") not in (None, "probe")
            and c.get("send_ns") is not None
            and steady[0] <= r.seconds(c["send_ns"]) <= steady[1]
        )
        out.measured("interventions_in_measured", max(sent, int(interventions.get("in_measured") or 0)))


_SECTIONS = (
    _health_section,
    _tps_section,
    _tick_section,
    _cpu_section,
    _memory_section,
    _load_section,
    _work_section,
    _efficiency_section,
    _chaos_section,
)


# --------------------------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------------------------


def _round(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite metric value")
        rounded = round(value, FLOAT_DIGITS)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, Mapping):
        return {str(k): _round(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v) for v in value]
    raise TypeError(f"cannot store {type(value).__name__} in metrics.json")


def compute(run: RunData, adapter: Any = None) -> dict[str, Any]:
    """metrics.json content for one run.

    ``adapter`` is an adapter class, instance or :class:`~bedrock_bench.metrics.health.ServerTraits`; by default the
    registered adapter for ``run.json arm.server`` is used, or neutral defaults when it is not installed.
    """
    if adapter is None:
        traits = ServerTraits.lookup((run.run.get("arm") or {}).get("server"))
    else:
        traits = ServerTraits.of(adapter)
    r = _Run(run, traits)
    out = _Out()
    for section in _SECTIONS:
        section(out, r)

    metrics: dict[str, Any] = {}
    for metric in registry.METRICS:
        entry = out.values.get(metric.id)
        if not traits.tick_monotonic and metric.id.startswith(_TICK_COUNTER_PREFIXES):
            entry = None
            availability, reason = Availability.UNAVAILABLE, "the server has no monotonic tick counter"
        elif entry is None and metric.window in WINDOW_MARKS and r.bounds(metric.window) is None:
            availability, reason = r.why_not(metric.window)
        else:
            availability, reason = Availability.UNAVAILABLE, "not computed"
        if entry is None:
            entry = {"value": None, "n": None, "availability": availability.value, "reason": reason}
        metrics[metric.id] = {"unit": metric.unit, "window": metric.window, **_round(entry)}
    tables = {t.id: {"columns": list(t.columns), "rows": _round(out.tables.get(t.id, []))} for t in registry.TABLES}

    status, reasons, flags = health.evaluate(run, metrics, r.scenario, traits)
    return {
        "schema": METRICS_SCHEMA,
        "schema_minor": SCHEMA_MINOR,
        "analysis_version": ANALYSIS_VERSION,
        "run_id": run.run.get("run_id", ""),
        "status": status.value,
        "status_reasons": reasons,
        "flags": flags,
        "metrics": metrics,
        "tables": tables,
    }


def dumps(doc: Mapping[str, Any]) -> str:
    """The canonical text of a metrics.json document (sorted keys, two-space indent, LF, trailing newline)."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def write(path: Path, doc: Mapping[str, Any]) -> None:
    path.write_text(dumps(doc), encoding="utf-8", newline="\n")


def analyze_cli(args: argparse.Namespace) -> int:
    """``bbench analyze RUN_DIR...``: recompute metrics.json and the status of each run from its raw files."""
    try:
        from bedrock_bench.results import load_run
    except ImportError:
        raise NotImplementedError("bbench analyze needs bedrock_bench.results.load_run") from None

    statuses: list[Status] = []
    for run_dir in map(Path, args.run_dirs):
        data = load_run(run_dir)
        doc = compute(data)
        target = run_dir / "metrics.json"
        previous = target.read_text(encoding="utf-8") if target.is_file() else None
        write(target, doc)
        status = Status(doc["status"])
        statuses.append(status)
        change = "unchanged" if previous == dumps(doc) else "rewritten"
        recorded = data.run.get("status")
        note = f" (run.json says {recorded})" if recorded not in (None, status.value) else ""
        print(f"{run_dir.name}: {status.value}, metrics.json {change}{note}")
        for reason in doc["status_reasons"]:
            print(f"  {reason}")
    return ExitCode.for_statuses(statuses)


__all__ = ["FLOAT_DIGITS", "WINDOW_MARKS", "analyze_cli", "compute", "dumps", "scenario_of", "window", "write"]
