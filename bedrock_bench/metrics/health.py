# SPDX-License-Identifier: Apache-2.0
"""Validity gates and run status (docs/methodology.md#statistics-and-run-status).

``evaluate`` reads the computed metrics, run.json and the scenario; it never re-reads raw series. A gate on a window
the run never reached is skipped, so a server that dies during join is a ``server_failed`` result, not an invalid run.

Some facts only exist while the run is live: a harness exception, a parity key that read back differently after
shutdown, or a server that ignored its stop command and was killed. The runner records those as
``"<gate>: <detail>"`` entries in run.json ``status_reasons``; they are kept on every recompute (``RECORDED_GATES``).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bedrock_bench.model import Flag, Gate, Gates, RunData, Scenario, Status

#: Gates whose failures only the live runner can see; their recorded reasons survive a recompute.
RECORDED_GATES = (Gate.HARNESS_OK, Gate.CONFIG_READBACK, Gate.SERVER_ALIVE)
MEASURED = ("load", "steady", "chaos")


@dataclass(frozen=True)
class GateDef:
    gate: Gate
    rule: str


GATES: tuple[GateDef, ...] = (
    GateDef(
        Gate.HARNESS_OK,
        "no harness exception (recorded by the runner); run.json final; raw series present for every phase reached",
    ),
    GateDef(
        Gate.PROBE_MONOTONIC,
        "the adapter declares a monotonic counter, probe_tick_decreases = 0 and probe_idle_ticks > 0",
    ),
    GateDef(Gate.PROBE_COVERAGE, "probe_max_gap_s <= scenario probe.max_gap_s (15 s) in every measured window"),
    GateDef(Gate.PROBE_LAGGING, "probe_latency_ms_steady p99 <= scenario probe.lagging_p99_ms (1000 ms)"),
    GateDef(
        Gate.FOREIGN_LOAD,
        "foreign_cores, foreign_cores_peak_10s and foreign_server_set_cores within the machine "
        "profile gates in every measured window",
    ),
    GateDef(
        Gate.PLACEMENT_MATCH,
        "run.json placement.verified is not false (server threads matched the CPU plan at ready, steady0 and steady1)",
    ),
    GateDef(Gate.DECODE_ERRORS, "decode_errors = 0"),
    GateDef(Gate.BOT_SATURATED, "bot_cpu_frac <= machine profile bot_cpu_frac_max (0.85) in every measured window"),
    GateDef(Gate.SPREAD, "after settle: positions known, spread cells_occupied = joined and max_per_cell = 1"),
    GateDef(
        Gate.CSG_MATCHES, "csg_effective = the scenario's CSG setting, on servers that support client-side generation"
    ),
    GateDef(Gate.VERSION_MATCH, "the banner version equals the arm version"),
    GateDef(Gate.WORLD_FRESH, "world dir absent before launch, or a template scenario with its template hash recorded"),
    GateDef(
        Gate.CONFIG_READBACK, "parity keys read back after the run equal what was written (recorded by the runner)"
    ),
    GateDef(Gate.INTERVENTIONS_IN_MEASURED, "interventions_in_measured = 0"),
    GateDef(Gate.SERVER_ALIVE, "the server ran until teardown and exited with 0 on stop, without being killed"),
    GateDef(Gate.JOINED_ALL, "joined = the scenario's bot count (reported as joined/n)"),
    GateDef(Gate.DROPPED, "dropped = 0 outside chaos; a nonzero count adds the dropped flag"),
)

_REASON = re.compile(r"^(?P<gate>[a-z_]+):\s*(?P<detail>.*)$")


@dataclass(frozen=True)
class ServerTraits:
    """What the metrics need to know about a server's adapter. Defaults describe an unknown server."""

    tick_monotonic: bool = True
    supports_csg: bool = False
    tick_thread_rule: str = "none"
    thread_groups: Mapping[str, str] = field(default_factory=dict)
    benign: tuple[re.Pattern[str], ...] = ()

    @classmethod
    def of(cls, adapter: Any) -> ServerTraits:
        """Traits of an adapter class or instance; a ServerTraits passes through; None gives the defaults."""
        if adapter is None:
            return cls()
        if isinstance(adapter, ServerTraits):
            return adapter
        dialect = getattr(adapter, "dialect", None)
        return cls(
            tick_monotonic=bool(getattr(dialect, "tick_monotonic", True)),
            supports_csg=bool(getattr(adapter, "supports_csg", False)),
            tick_thread_rule=str(getattr(adapter, "tick_thread_rule", "none")),
            thread_groups=dict(getattr(adapter, "thread_groups", {}) or {}),
            benign=tuple(getattr(dialect, "benign", ()) or ()),
        )

    @classmethod
    def lookup(cls, server: str | None) -> ServerTraits:
        """Traits of a registered adapter by server name; defaults when it is not installed here."""
        if not server:
            return cls()
        from bedrock_bench import adapters
        from bedrock_bench.config import ConfigError

        try:
            return cls.of(adapters.get(server))
        except ConfigError:  # not installed on this host (a third-party adapter, or a fixture)
            return cls()

    def is_benign(self, text: str) -> bool:
        return any(p.search(text) for p in self.benign)


def gates_of(run: Mapping[str, Any]) -> Gates:
    """The machine profile's interference gates from run.json (``machine.gates``), or the defaults."""
    recorded = (run.get("machine") or {}).get("gates") or {}
    names = {f.name for f in dataclasses.fields(Gates)}
    return Gates(**{k: float(v) for k, v in recorded.items() if k in names})


# --------------------------------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------------------------------


def _value(metrics: Mapping[str, Mapping[str, Any]], metric_id: str) -> Any:
    entry = metrics.get(metric_id)
    if not entry or entry.get("availability") != "measured":
        return None
    return entry.get("value")


def _num(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


class _Gates:
    def __init__(self) -> None:
        self.failed: dict[Gate, list[str]] = {}

    def fail(self, gate: Gate, detail: str) -> None:
        details = self.failed.setdefault(gate, [])
        if detail not in details:
            details.append(detail)


def evaluate(
    run: RunData | Mapping[str, Any],
    metrics: Mapping[str, Mapping[str, Any]],
    scenario: Scenario,
    adapter: Any = None,
    *,
    gates: Gates | None = None,
) -> tuple[Status, list[str], list[str]]:
    """``(status, reasons, flags)`` of one run.

    ``run`` is the loaded run (or just its run.json dict), ``metrics`` the ``metrics`` object of metrics.json and
    ``adapter`` an adapter class, instance or :class:`ServerTraits`. Reasons read ``"<gate>: <detail>"`` in gate
    order; flags are sorted.
    """
    data = run if isinstance(run, RunData) else None
    meta: Mapping[str, Any] = run.run if isinstance(run, RunData) else run
    traits = ServerTraits.of(adapter)
    limits = gates if gates is not None else gates_of(meta)
    marks = meta.get("marks") or {}
    out = _Gates()

    def reached(name: str) -> bool:
        bounds = {
            "idle": ("idle0", "idle1"),
            "join": ("bots_start", "alljoined"),
            "settle": ("alljoined", "settled"),
            "load": ("bots_start", "settled"),
            "steady": ("steady0", "steady1"),
            "chaos": ("chaos0", "chaos1"),
        }[name]
        return all(marks.get(k) is not None for k in bounds)

    # harness_ok and other recorded gates
    for reason in meta.get("status_reasons") or ():
        match = _REASON.match(reason)
        if match and match["gate"] in {g.value for g in RECORDED_GATES}:
            out.fail(Gate(match["gate"]), match["detail"])
    if meta.get("partial"):
        out.fail(Gate.HARNESS_OK, "run did not finish (partial run.json)")
    if data is not None and marks.get("bots_start") is not None:
        if not data.process:
            out.fail(Gate.HARNESS_OK, "the process sampler produced no samples")
        if not data.machine:
            out.fail(Gate.HARNESS_OK, "the machine sampler produced no samples")
        if not data.bots:
            out.fail(Gate.HARNESS_OK, "no bot events")

    # probe
    if marks.get("ready") is not None and not traits.tick_monotonic:
        out.fail(Gate.PROBE_MONOTONIC, "the adapter declares no monotonic tick counter")
    decreases = _value(metrics, "probe_tick_decreases")
    if decreases:
        out.fail(Gate.PROBE_MONOTONIC, f"the tick counter went down {decreases} times")
    if reached("idle") or (not scenario.has_phase("idle") and reached("load")):
        idle_ticks = _value(metrics, "probe_idle_ticks")
        if idle_ticks is None:
            out.fail(Gate.PROBE_MONOTONIC, "no probe replies to check the counter with")
        elif idle_ticks <= 0:
            out.fail(Gate.PROBE_MONOTONIC, "the tick counter did not advance during idle")
    for window in MEASURED:
        if not reached(window):
            continue
        gap = _value(metrics, f"probe_max_gap_s_{window}")
        if gap is None:
            out.fail(Gate.PROBE_COVERAGE, f"no probe replies in {window}")
        elif gap > scenario.probe.max_gap_s:
            out.fail(Gate.PROBE_COVERAGE, f"{_num(gap)} s without a probe reply in {window}")
    if reached("steady"):
        latency = _value(metrics, "probe_latency_ms_steady")
        p99 = latency.get("p99") if isinstance(latency, Mapping) else None
        if p99 is not None and p99 > scenario.probe.lagging_p99_ms:
            out.fail(Gate.PROBE_LAGGING, f"probe latency p99 {_num(p99)} ms in steady")

    # interference
    for window in MEASURED:
        if not reached(window):
            continue
        checks = (
            (f"foreign_cores_{window}", limits.foreign_cores_mean, "foreign load"),
            (f"foreign_cores_peak_10s_{window}", limits.foreign_cores_peak_10s, "foreign load 10 s peak"),
            (f"foreign_server_set_cores_{window}", limits.foreign_server_set_cores_mean, "foreign load on server CPUs"),
        )
        for metric_id, limit, label in checks:
            value = _value(metrics, metric_id)
            if value is None:
                out.fail(Gate.FOREIGN_LOAD, f"{label} not measured in {window}")
            elif value > limit:
                out.fail(Gate.FOREIGN_LOAD, f"{label} {_num(value)} cores in {window} (limit {_num(limit)})")
        bot_frac = _value(metrics, f"bot_cpu_frac_{window}")
        if bot_frac is not None and bot_frac > limits.bot_cpu_frac_max:
            out.fail(
                Gate.BOT_SATURATED,
                f"bot CPUs {_num(bot_frac)} busy in {window} (limit {_num(limits.bot_cpu_frac_max)})",
            )
    if (meta.get("placement") or {}).get("verified") is False:
        out.fail(Gate.PLACEMENT_MATCH, "server threads did not match the CPU plan")

    # bots
    decode = _value(metrics, "decode_errors")
    if decode:
        out.fail(Gate.DECODE_ERRORS, f"{decode} bot decode errors")
    joined = _value(metrics, "joined")
    spread = meta.get("spread") or {}
    if marks.get("settled") is not None and not (_value(metrics, "died_during_run") and spread.get("source") is None):
        if spread.get("source") is None or spread.get("players") is None or spread.get("cells_occupied") is None:
            out.fail(Gate.SPREAD, "player positions unknown")
        else:
            if joined is not None and spread["cells_occupied"] != joined:
                out.fail(Gate.SPREAD, f"{spread['cells_occupied']} cells occupied by {joined} joined bots")
            if spread.get("max_per_cell") != 1:
                out.fail(Gate.SPREAD, f"up to {spread.get('max_per_cell')} bots in one cell")
    csg = _value(metrics, "csg_effective")
    if traits.supports_csg and csg is not None and csg != scenario.server.csg:
        out.fail(
            Gate.CSG_MATCHES,
            f"client-side generation {'in effect' if csg else 'not in effect'} but the scenario sets "
            f"csg = {str(scenario.server.csg).lower()}",
        )

    # identity of what ran
    if marks.get("ready") is not None:
        banner = (meta.get("server") or {}).get("banner_version")
        version = (meta.get("arm") or {}).get("version")
        if banner != version:
            out.fail(Gate.VERSION_MATCH, f"banner version {banner!r} for arm version {version!r}")
    world = meta.get("world") or {}
    if not world.get("fresh") and not (scenario.world.source == "template" and world.get("template_sha256")):
        out.fail(Gate.WORLD_FRESH, "the world existed before launch and no template hash is recorded")
    in_measured = _value(metrics, "interventions_in_measured")
    if in_measured:
        out.fail(Gate.INTERVENTIONS_IN_MEASURED, f"{in_measured} interventions in a measured window")

    # outcome
    if _value(metrics, "died_during_run"):
        out.fail(Gate.SERVER_ALIVE, "the server exited before teardown")
    exit_code = _value(metrics, "server_exit_code")
    if exit_code not in (None, 0):
        out.fail(Gate.SERVER_ALIVE, f"exit code {exit_code}")
    if marks.get("bots_start") is not None:
        n = scenario.bots.n
        if (joined or 0) < n:
            out.fail(Gate.JOINED_ALL, f"joined {joined or 0}/{n}")

    flags = {str(f) for f in meta.get("flags") or ()}
    dropped = _value(metrics, "dropped")
    if dropped:
        flags.add(Gate.DROPPED.value)
    settle = meta.get("settle") or {}
    if settle.get("capped"):
        flags.add(Flag.SETTLE_CAPPED.value)
    if settle.get("views_incomplete"):
        flags.add(Flag.VIEWS_INCOMPLETE.value)
    view_t_n = metrics.get("view_t_n_s") or {}
    if view_t_n.get("availability") == "unavailable" and view_t_n.get("reason", "").startswith("median censored"):
        flags.add(Flag.VIEWS_INCOMPLETE.value)
    if _value(metrics, "chaos_actions_overrun"):
        flags.add(Flag.CHAOS_OVERRUN.value)

    reasons = [f"{gate.value}: {detail}" for gate in Gate for detail in out.failed.get(gate, ())]
    statuses = [gate.status for gate in out.failed if gate.status is not None]
    return Status.worst(statuses), reasons, sorted(flags)


def failed_gates(reasons: Sequence[str]) -> list[str]:
    """Gate names of ``"<gate>: <detail>"`` reasons, in order, without repeats."""
    seen: list[str] = []
    for reason in reasons:
        match = _REASON.match(reason)
        if match and match["gate"] not in seen:
            seen.append(match["gate"])
    return seen
