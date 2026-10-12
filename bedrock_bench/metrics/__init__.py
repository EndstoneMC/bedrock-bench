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
from bedrock_bench.metrics import cpu, health, load, memory, registry, tps
from bedrock_bench.metrics.health import ServerTraits
from bedrock_bench.metrics.stats import Censored, censored_median, distribution
from bedrock_bench.model import (
    Availability,
    CpuSample,
    ExitCode,
    MachineSample,
    ProbeSample,
    ProcSample,
    RunData,
    Status,
)
from bedrock_bench.workload import from_record

#: Decimals kept for every float in metrics.json.
FLOAT_DIGITS = 4

#: Marks that open and close each window.
WINDOW_MARKS: Mapping[str, tuple[str, str]] = {
    "idle": ("idle0", "idle1"),
    "join": ("bots_start", "alljoined"),
    "settle": ("alljoined", "settled"),
    "load": ("bots_start", "settled"),
    "steady": ("steady0", "steady1"),
}

#: The workload phase each window belongs to, to tell "not in this workload" from "not reached".
_WINDOW_PHASE = {
    "idle": "idle",
    "join": "join",
    "settle": "settle",
    "load": "settle",
    "steady": "steady",
}

#: Metrics computed from the tick counter: unavailable when the adapter declares no monotonic counter.
_TICK_COUNTER_PREFIXES = ("tps_", "tick_cpu_ms_per_tick_")

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


# --------------------------------------------------------------------------------------------------------------------
# One run on its own timebase
# --------------------------------------------------------------------------------------------------------------------


class _Run:
    """A run prepared for the formulas: series rebased to the origin, bot events timed, windows resolved."""

    def __init__(self, data: RunData, traits: ServerTraits) -> None:
        self.data = data
        self.meta = data.run
        self.traits = traits
        self.workload = from_record(self.meta)
        self.marks: Mapping[str, Any] = self.meta.get("marks") or {}
        time_info = self.meta.get("time") or {}
        o = self.origin_ns = int(time_info.get("origin_mono_ns", 0))
        epoch = int(time_info.get("origin_epoch_ms", 0))
        self.probe = [ProbeSample(s.send_ns - o, s.rx_ns - o, s.tick) for s in data.probe]
        self.tick = [CpuSample(s.t_ns - o, s.cpu_ns, s.cycles) for s in data.tick]
        self.process: list[ProcSample] = [dataclasses.replace(s, t_ns=s.t_ns - o) for s in data.process]
        self.machine: list[MachineSample] = [dataclasses.replace(s, t_ns=s.t_ns - o) for s in data.machine]
        self.bots = load.timed_events(data.bots, o, epoch)

    def seconds(self, mono_ns: int) -> float:
        return (mono_ns - self.origin_ns) / NS_PER_S

    def bounds(self, name: str) -> tuple[float, float] | None:
        return window(self.meta, name)

    def has_phase(self, name: str) -> bool:
        if self.workload.phases:
            return self.workload.has_phase(name)
        return self.marks.get(WINDOW_MARKS[name][0]) is not None

    def why_not(self, name: str) -> tuple[Availability, str]:
        phase = _WINDOW_PHASE[name]
        if not self.has_phase(phase):
            return Availability.NOT_APPLICABLE, f"the workload has no {phase} phase"
        return Availability.UNAVAILABLE, f"the {name} window was not reached"

    def process_n(self, t0: float, t1: float) -> int:
        return sum(1 for p in self.process if t0 <= p.t_ns / NS_PER_S <= t1)

    def machine_n(self, t0: float, t1: float) -> int:
        return sum(1 for m in self.machine if t0 <= m.t_ns / NS_PER_S <= t1)


class _Out:
    """Metric values as sections produce them."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def measured(self, metric_id: str, value: Any, *, n: int | None = None) -> None:
        registry.get(metric_id)  # an undeclared id fails loudly
        self.values[metric_id] = {"value": value, "n": n, "availability": Availability.MEASURED.value}

    def missing(self, metric_id: str, availability: Availability, reason: str) -> None:
        registry.get(metric_id)
        self.values[metric_id] = {"value": None, "n": None, "availability": availability.value, "reason": reason}

    def value(self, metric_id: str, value: Any, reason: str, *, n: int | None = None) -> None:
        """Measured when ``value`` is not None, else unavailable with ``reason``."""
        if value is None:
            self.missing(metric_id, Availability.UNAVAILABLE, reason)
        else:
            self.measured(metric_id, value, n=n)


def _each_window(r: _Run, windows: Sequence[str], fill: Callable[[str, float, float], None]) -> None:
    """``fill(window, t0, t1)`` for each reached window; metrics of the others are filled in by ``compute``."""
    for w in windows:
        bounds = r.bounds(w)
        if bounds is not None:
            fill(w, *bounds)


# --------------------------------------------------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------------------------------------------------


def _tps_section(out: _Out, r: _Run) -> None:
    probe = r.probe
    steady = r.bounds("steady")
    if steady is not None:
        out.value(
            "tps_window_steady",
            tps.window(probe, *steady),
            "probe replies cover less than 90% of the window",
            n=tps.count(probe, *steady),
        )
        latency = tps.latency_ms(probe, *steady)
        out.value(
            "probe_latency_ms_steady",
            distribution(latency, ("p50", "p99", "max")),
            "no probe replies in the window",
            n=len(latency),
        )

    load_bounds = r.bounds("load")
    if load_bounds is not None:
        out.value(
            "tps_load_low5",
            tps.lowest(probe, *load_bounds, 4.5),
            "no stretch of 4.5 s inside the window",
            n=tps.count(probe, *load_bounds),
        )

    def gap(w: str, t0: float, t1: float) -> None:
        out.measured(f"probe_max_gap_s_{w}", tps.max_gap(probe, t0, t1), n=tps.count(probe, t0, t1))

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
    steady = r.bounds("steady")
    if steady is None:
        return
    if not r.tick and r.traits.tick_thread_rule == "none":
        out.missing("tick_cpu_ms_per_tick_steady", Availability.NOT_APPLICABLE, "the server has no single tick thread")
        return
    result = cpu.tick(r.tick, r.probe, *steady)
    if result is None:
        out.missing(
            "tick_cpu_ms_per_tick_steady", Availability.UNAVAILABLE, "fewer than two tick-thread samples in the window"
        )
        return
    out.value(
        "tick_cpu_ms_per_tick_steady",
        result.ms_per_tick,
        "no executed ticks between probe replies covered by tick samples",
        n=result.ticks,
    )


def _cpu_section(out: _Out, r: _Run) -> None:
    bot_cpus = list((r.meta.get("placement") or {}).get("bot_cpus") or ())
    steady = r.bounds("steady")
    if steady is not None:
        process = cpu.process_cores(r.process, *steady)
        short = "fewer than two process samples in the window"
        out.value("process_cores_steady", process, short, n=r.process_n(*steady))

    def interference(w: str, t0: float, t1: float) -> None:
        n_machine = r.machine_n(t0, t1)
        busy = cpu.cpu_set_busy(r.machine, bot_cpus, t0, t1)
        reason = "no machine samples in the window" if bot_cpus else "no bot CPUs in run.json placement"
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

    _each_window(r, registry.MEASURED, interference)


def _memory_section(out: _Out, r: _Run) -> None:
    steady = r.bounds("steady")
    if steady is None:
        return
    mem = memory.window(r.process, *steady)
    if mem is None:
        out.missing("private_mib_steady", Availability.UNAVAILABLE, "no process samples in the window")
    else:
        out.measured("private_mib_steady", mem.private_mean, n=mem.samples)


def _load_section(out: _Out, r: _Run) -> None:
    bounds = r.bounds("load")
    if bounds is None:
        return
    bots_start, settled = bounds
    view = load.view(r.bots, settled)
    u = list(view.u.values())
    n = len(u)
    no_progress = "no progress snapshot before settled"
    out.value("chunks_per_bot", distribution(u, ("min", "median", "max")), no_progress, n=n)
    out.value(
        "view_t90_own_s",
        distribution(view.t90_own.values(), ("median", "p90", "max")),
        "no bot reached 90% of its view",
        n=len(view.t90_own),
    )

    t_n = censored_median(view.t_n)
    if not view.t_n:
        out.missing("view_t_n_s", Availability.UNAVAILABLE, "no teleported bots with progress")
    elif t_n is None:
        censored = sum(1 for v in view.t_n if isinstance(v, Censored))
        out.missing(
            "view_t_n_s",
            Availability.UNAVAILABLE,
            f"median censored: {censored} of {len(view.t_n)} bots never reached {view.n_target} chunks",
        )
    else:
        out.measured("view_t_n_s", t_n, n=len(view.t_n))

    out.value(
        "chunks_per_s_load", sum(u) / (settled - bots_start) if u and settled > bots_start else None, no_progress, n=n
    )
    out.value("csg_effective", load.csg_effective(view.u, r.workload.server.view_distance), no_progress, n=n)


def _health_section(out: _Out, r: _Run) -> None:
    recorded = r.meta.get("health") or {}
    out.value("server_exit_code", recorded.get("server_exit"), "not recorded in run.json")
    out.value("died_during_run", recorded.get("died"), "not recorded in run.json")

    bots = r.bots
    totals = [int(e.data.get("decode_errors", 0)) for e in bots if e.ev == "stat"]
    totals += [int(e.data.get("total", 1)) for e in bots if e.ev == "decode_error"]
    if recorded.get("decode_errors") is not None:
        totals.append(int(recorded["decode_errors"]))
    out.value("decode_errors", max(totals, default=None), "no bot events")
    if bots:
        out.measured("joined", load.joined(bots))
    else:
        out.value("joined", recorded.get("joined"), "no bot events")
    end = r.marks.get("steady1") or r.marks.get("stop")
    last = load.last_at(bots, "stat", float(end)) if end is not None else None
    dropped = int(last.data.get("dropped", 0)) if last is not None else recorded.get("dropped")
    out.value("dropped", dropped, "no stat event before the end of steady")

    interventions = r.meta.get("interventions") or {}
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

    status, reasons, flags = health.evaluate(run, metrics, r.workload, traits)
    return {
        "schema": METRICS_SCHEMA,
        "schema_minor": SCHEMA_MINOR,
        "analysis_version": ANALYSIS_VERSION,
        "run_id": run.run.get("run_id", ""),
        "status": status.value,
        "status_reasons": reasons,
        "flags": flags,
        "metrics": metrics,
    }


def dumps(doc: Mapping[str, Any]) -> str:
    """The canonical text of a metrics.json document (sorted keys, two-space indent, LF, trailing newline)."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def write(path: Path, doc: Mapping[str, Any]) -> None:
    path.write_text(dumps(doc), encoding="utf-8", newline="\n")


def analyze_cli(args: argparse.Namespace) -> int:
    """``bbench analyze RUN_DIR...``: recompute metrics.json and the status of each run from its raw files."""
    from bedrock_bench.results import load_run

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


__all__ = ["FLOAT_DIGITS", "WINDOW_MARKS", "analyze_cli", "compute", "dumps", "window", "write"]
