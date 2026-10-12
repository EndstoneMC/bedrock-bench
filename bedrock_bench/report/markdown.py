# SPDX-License-Identifier: Apache-2.0
"""Render a matrix summary as ``report.md``.

Layout (rule R10): a conditions block, then for each scenario one section for the arms that are
``vanilla`` on every exercised feature and one for the rest. Each section shows the arms, cost, work, parity and
efficiency tables together. Sections are never ranked against each other and there is no composite score. Inside a
section, an arm that hosts a BDS build is compared with that build using the difference rule of
:mod:`bedrock_bench.report.aggregate`.

The renderer reads only the summary, so ``report.md`` can always be regenerated from ``summary.json``. Output is
ASCII apart from text taken from the inputs.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bedrock_bench.model import OverrideCategory
from bedrock_bench.report.aggregate import SEPARATION_P_MAX, Sample, compare, samples
from bedrock_bench.report.footnotes import Footnotes, availability_text, flag_text, override_text, status_text

HIGHER = "higher"
LOWER = "lower"


@dataclass(frozen=True)
class Row:
    """One table row: the metric it shows, its label and display unit, and which direction is better.

    ``better`` is None for rows that are never ranked (work counters). ``part`` picks the key shown when the
    metric is a distribution over bots. ``note`` is a footnote on the label.
    """

    metric: str
    label: str
    unit: str  # tps, ms, s, cores, gib, pct, rate, count, bool
    better: str | None = None
    part: str = "median"
    note: str | None = None


_TICK_CPU_NOTE = (
    "CPU time of the tick thread divided by the ticks it executed. It is not MSPT: a tick that waits is not counted."
)
_MSPT_NOTE = "1000 / TPS, shown only below 19.5 TPS, where ticks run back to back and the value is exact."
_RECOVERY_NOTE = "Seconds from the first join until TPS holds 19.5 for 30 s; `>` means it never did before the end."

COST_ROWS: tuple[Row, ...] = (
    Row("tps_window_steady", "Steady TPS", "tps", HIGHER),
    Row("tps_load_low5", "Lowest 5 s TPS while views load", "tps", HIGHER),
    Row("tps_load_low15", "Lowest 15 s TPS while views load", "tps", HIGHER),
    Row("ticks_lost_load", "Ticks lost while views load", "count", LOWER),
    Row("time_below_19_5_load_s", "Time below 19.5 TPS while views load", "s", LOWER),
    Row("recovery_s", "TPS recovery after the join", "s", LOWER, note=_RECOVERY_NOTE),
    Row("tick_cpu_ms_per_tick_steady", "Tick-thread CPU per tick, steady", "ms", LOWER, note=_TICK_CPU_NOTE),
    Row("tick_busy_frac_steady", "Tick thread busy, steady", "pct", LOWER),
    Row("tick_saturated_frac_steady", "Seconds with the tick thread saturated, steady", "pct", LOWER),
    Row("mspt_saturated_steady", "MSPT while saturated, steady", "ms", LOWER, note=_MSPT_NOTE),
    Row("process_cores_steady", "Server CPU cores, steady", "cores", LOWER),
    Row("aux_cores_steady", "Launcher CPU cores, steady", "cores", LOWER),
    Row("private_mib_steady", "Private memory, steady", "gib", LOWER),
    Row("resident_mib_steady", "Resident memory (working set or RSS), steady", "gib", LOWER),
    Row("view_t90_own_s", "View load t90 of own view, median bot", "s", LOWER),
    Row("view_t_n_s", "Time to 80 chunks in view, median bot", "s", LOWER),
    Row("delivery_t90_s", "Time until 90% of all chunks arrived", "s", LOWER),
    Row("first_chunk_ms", "First chunk after teleport, median bot", "ms", LOWER),
)

WORK_ROWS: tuple[Row, ...] = (
    Row("chunks_per_bot", "Chunks in view per bot at settle, median bot", "count"),
    Row("view_fill_frac", "View filled, of a full disc", "pct"),
    Row("csg_effective", "Client-side chunk generation in effect", "bool"),
    Row("resend_ratio", "Chunk sends per unique chunk", "rate"),
    Row("chunks_per_s_load", "Chunks delivered per second while views load", "rate"),
    Row("corrections_per_bot_s", "Movement corrections per bot per second", "rate"),
    Row("actor_moves_per_bot_s", "Actor moves per bot per second", "rate"),
    Row("block_updates_per_bot_s", "Block updates per bot per second", "rate"),
    Row("locator_pkts_per_bot_s", "Locator packets per bot per second", "rate"),
    Row("rx_pkts_per_bot_s", "Packets received per bot per second", "rate"),
    Row("rx_kib_per_bot_s", "KiB received per bot per second", "rate"),
    Row("tx_pkts_per_bot_s", "Packets sent per bot per second", "rate"),
)

EFFICIENCY_ROWS: tuple[Row, ...] = (
    Row("cpu_ms_per_player_tick_steady", "CPU ms per player per tick, steady", "ms", LOWER),
    Row("ticks_per_cpu_s_steady", "Ticks per CPU second, steady", "rate", HIGHER),
    Row("chunks_per_cpu_s_load", "Chunks per CPU second while views load", "rate", HIGHER),
)

def layout_metric_ids() -> list[str]:
    """Every metric id the report layout shows; each should exist in the metric registry."""
    rows = (*COST_ROWS, *WORK_ROWS, *EFFICIENCY_ROWS)
    return sorted({row.metric for row in rows})


# --------------------------------------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------------------------------------

_SUFFIX = {"ms": " ms", "s": " s", "gib": " GiB", "pct": "%"}
_FIXED_DECIMALS = {"tps": 2, "cores": 2, "gib": 2, "pct": 0, "count": 0}


def format_number(value: float, unit: str) -> str:
    """A number in a display unit, without the suffix. MiB become GiB (/1024) and fractions percent."""
    if unit == "gib":
        value /= 1024
    elif unit == "pct":
        value *= 100
    decimals = _FIXED_DECIMALS.get(unit)
    if decimals is None:  # three significant digits, never scientific notation
        decimals = 0 if value == 0 else max(0, 2 - math.floor(math.log10(abs(value))))
    return f"{value:.{decimals}f}"


def format_stat(stat: Mapping[str, Any], unit: str) -> str:
    """``median [min, max]`` of an aggregate, ``>`` before every statistic that is only a lower bound."""
    if unit == "bool" or "median" not in stat:
        return _format_bool(stat)
    lower = set(stat.get("lower_bounds") or ())

    def number(key: str) -> str:
        return (">" if key in lower else "") + format_number(stat[key], unit)

    text = number("median") + _SUFFIX.get(unit, "")
    if int(stat.get("n") or 0) > 1:
        text += f" [{number('min')}, {number('max')}]"
    return text


def _format_bool(stat: Mapping[str, Any]) -> str:
    measured = [v for v in stat.get("values") or () if v is not None]
    if not measured:
        return "-"
    if stat.get("value") is True:
        return "yes"
    if stat.get("value") is False:
        return "no"
    if all(isinstance(v, bool) for v in measured):
        return f"mixed ({sum(measured)} of {len(measured)} yes)"
    return "mixed"


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _cpus(cpus: Sequence[int] | None) -> str:
    """``[0, 1, 2, 3, 8]`` -> ``0-3,8``."""
    if not cpus:
        return "-"
    ordered = sorted(set(cpus))
    ranges = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu != prev + 1:
            ranges.append((start, prev))
            start = cpu
        prev = cpu
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def _cell_text(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], *, numeric: bool = True) -> list[str]:
    align = "---:" if numeric else "---"
    lines = [
        "| " + " | ".join(_cell_text(h) for h in header) + " |",
        "|---|" + "|".join([align] * (len(header) - 1)) + "|",
    ]
    lines += ["| " + " | ".join(_cell_text(c) for c in row) + " |" for row in rows]
    return lines


# --------------------------------------------------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------------------------------------------------


class _Doc:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def add(self, *lines: str) -> None:
        text = "\n".join(lines).strip("\n")
        if text:
            self.blocks.append(text)

    def text(self) -> str:
        return "\n\n".join(self.blocks) + "\n"


def render(summary: Mapping[str, Any]) -> str:
    """The markdown report of a summary, including its ``claims`` and ``publish`` results when present."""
    doc = _Doc()
    notes = Footnotes()
    _header(doc, summary)
    _conditions(doc, summary)
    if "publish" in summary:
        _publication(doc, summary["publish"].get("problems") or ())
    for scenario in summary.get("scenarios", ()):
        _scenario(doc, scenario, notes)
    if "claims" in summary:
        _claims(doc, summary["claims"])
    if len(notes):
        doc.add(notes.render())
    return doc.text()


def _header(doc: _Doc, summary: Mapping[str, Any]) -> None:
    counts = summary.get("status_counts") or {}
    doc.add(f"# Matrix `{summary.get('matrix')}`")
    doc.add(
        f"{summary.get('n_runs')} runs: {counts.get('ok', 0)} ok, {counts.get('server_failed', 0)} server_failed, "
        f"{counts.get('invalid', 0)} invalid. Each value is the median of an arm's ok runs, with the lowest and "
        "highest run in brackets; `>` marks a lower bound. Runs that are not ok are counted, never averaged. "
        "There is no overall score: read the parity rows before the cost rows."
    )


def _conditions(doc: _Doc, summary: Mapping[str, Any]) -> None:
    conditions = summary.get("conditions") or {}
    bullets = []
    for machine in conditions.get("machines") or ():
        parts = [f"`{machine.get('label')}`" if machine.get("label") else None, machine.get("cpu")]
        if machine.get("logical"):
            physical = f" / {machine['physical']} physical" if machine.get("physical") else ""
            parts.append(f"{machine['logical']} logical{physical} CPUs")
        if machine.get("ram_gib"):
            parts.append(f"{machine['ram_gib']:g} GiB RAM")
        parts.append(" ".join(p for p in (machine.get("os"), machine.get("kernel")) if p) or None)
        if machine.get("power"):
            parts.append(f"power plan {machine['power']}")
        if machine.get("virtualized"):
            parts.append("virtualized")
        bullets.append("Machine: " + ", ".join(p for p in parts if p) + ".")
    for placement in conditions.get("placements") or ():
        kind = {"physical": "physical cores", "vcpu": "virtual CPUs"}.get(placement.get("kind"), placement.get("kind"))
        verified = "verified" if placement.get("verified") else "not verified"
        server, bots, harness = (_cpus(placement.get(k)) for k in ("server_cpus", "bot_cpus", "harness_cpus"))
        bullets.append(
            f"Placement: server on CPUs {server}, bots on {bots}, harness on {harness}, "
            f"priority {placement.get('priority')}, {kind}, {verified}."
        )
    backends = ", ".join(conditions.get("backends") or ()) or "unknown"
    for harness in conditions.get("harness") or ({},):
        git = f" (`{harness['git']}`{', dirty' if harness.get('dirty') else ''})" if harness.get("git") else ""
        bullets.append(
            f"Harness: bedrock-bench {harness.get('version', '?')}{git}, backend {backends}, "
            f"analysis version {summary.get('analysis_version')}."
        )
    for bot in conditions.get("bots") or ():
        patches = ", ".join(bot.get("patches") or ()) or "no patches"
        parts = [str(bot.get("module")), f"protocol {bot.get('protocol')}"]
        if bot.get("gophertunnel"):
            parts.append(f"gophertunnel {bot['gophertunnel']}")
        parts.append(patches)
        if bot.get("sha256"):
            parts.append(f"sha256 `{str(bot['sha256'])[:12]}`")
        bullets.append("Bot: " + ", ".join(parts) + ".")
    order = ", ".join(conditions.get("order") or ()) or "unknown"
    bullets.append(f"Order: {order}, up to {conditions.get('repeats')} repeats per arm.")
    bullets.append(f"Report generated by bedrock-bench {summary.get('harness_version')}.")
    doc.add("## Conditions")
    doc.add(*(f"- {b}" for b in bullets))

    warnings = []
    if any(p.get("kind") == "vcpu" for p in conditions.get("placements") or ()):
        warnings.append(
            "CPU placement is on virtual CPUs, not physical cores; do not compare these numbers with bare-metal runs."
        )
    if any(h.get("dirty") for h in conditions.get("harness") or ()):
        warnings.append("The harness checkout had uncommitted changes.")
    current = summary.get("current_analysis_version")
    if summary.get("analysis_version") not in (None, current):
        warnings.append(
            f"Metrics were computed with analysis version {summary.get('analysis_version')}; this harness uses "
            f"{current}. Run `bbench analyze` to recompute them."
        )
    if warnings:
        doc.add(*(f"> **Warning:** {w}" for w in warnings))


def _publication(doc: _Doc, problems: Sequence[str]) -> None:
    doc.add("## Publication check")
    if not problems:
        doc.add("Passed: every cell has enough ok runs, only released artifacts, and equal work where servers differ.")
    else:
        doc.add("Refused:", *(f"- {p}" for p in problems))


def _scenario(doc: _Doc, scenario: Mapping[str, Any], notes: Footnotes) -> None:
    doc.add(f"## Scenario `{scenario['name']}`")
    if scenario.get("description"):
        doc.add(str(scenario["description"]))
    doc.add(_settings_line(scenario))
    exercises = scenario.get("exercises") or ()
    if exercises:
        doc.add("Exercised features: " + ", ".join(f"`{f}`" for f in exercises) + ".")
    if scenario.get("cross_server") and (scenario.get("settings") or {}).get("csg") is not False:
        doc.add(
            "> **Warning:** this table compares different servers with client-side chunk generation on, so the "
            "servers do different chunk work. Compare servers on an equal-chunk scenario."
        )

    cells = list(scenario.get("cells") or ())
    full = [c for c in cells if c["parity"]["full_fidelity"]]
    reduced = [c for c in cells if not c["parity"]["full_fidelity"]]
    if full:
        _section(doc, scenario, full, notes, full_fidelity=True)
    if reduced:
        _section(doc, scenario, reduced, notes, full_fidelity=False)


def _settings_line(scenario: Mapping[str, Any]) -> str:
    settings = scenario.get("settings") or {}
    parts = []
    if settings.get("bots") is not None:
        parts.append(f"{settings['bots']} bots")
    if settings.get("view_distance") is not None:
        parts.append(f"view distance {settings['view_distance']}")
    if settings.get("tick_distance") is not None:
        parts.append(f"simulation distance {settings['tick_distance']}")
    if settings.get("csg") is not None:
        parts.append(f"client-side chunk generation {'on' if settings['csg'] else 'off'}")
    if settings.get("world_source"):
        seed = f", seed {settings['seed']}" if settings.get("seed") is not None else ""
        parts.append(f"{settings['world_source']} world{seed}")
    if settings.get("steady_s"):
        parts.append(f"steady window {settings['steady_s']:g} s")
    line = (", ".join(parts) + ".") if parts else ""
    if scenario.get("sha256"):
        line += f" Scenario sha256 `{str(scenario['sha256'])[:12]}`."
    return line.strip()


def _section(
    doc: _Doc,
    scenario: Mapping[str, Any],
    cells: Sequence[Mapping[str, Any]],
    notes: Footnotes,
    *,
    full_fidelity: bool,
) -> None:
    if full_fidelity:
        doc.add("### Full vanilla fidelity", "", "Arms that are `vanilla` on every feature this scenario exercises.")
    else:
        doc.add("### Reduced fidelity")
        doc.add(
            "Arms that do less than vanilla on at least one exercised feature. "
            "They are not ranked against arms in another section."
        )
        lines = ["Does less than vanilla:"]
        for cell in cells:
            parity = cell["parity"]
            if not parity.get("recorded"):
                detail = "parity not recorded"
            else:
                detail = ", ".join(f"`{d['feature']}` ({d['class']})" for d in parity["does_less"])
            lines.append(f"- `{cell['arm']['id']}`: {detail}")
        doc.add(*lines)

    names = [f"`{c['arm']['id']}`" for c in cells]
    _arms_table(doc, cells, notes)
    _metric_table(doc, "Cost", COST_ROWS, cells, names, notes)
    _metric_table(doc, "Work", WORK_ROWS, cells, names, notes, intro="Work the server did; never ranked.")
    _parity_table(doc, scenario, cells, names)
    requires = scenario.get("requires") or ()
    intro = (
        "Comparable only between arms with equal parity on " + ", ".join(f"`{k}`" for k in requires) + "."
        if requires
        else "Compare only arms that do the same work."
    )
    _metric_table(doc, "Efficiency", EFFICIENCY_ROWS, cells, names, notes, intro=intro)
    _differences(doc, scenario, cells)


def _arms_table(doc: _Doc, cells: Sequence[Mapping[str, Any]], notes: Footnotes) -> None:
    header = [""]
    for cell in cells:
        refs = [flag_text(f, count=n, total=cell["n_total"], arm=cell["arm"]) for f, n in cell["flags"].items()]
        header.append(f"`{cell['arm']['id']}`" + notes.refs(refs))

    def server(cell: Mapping[str, Any]) -> str:
        arm = cell["arm"]
        variant = f" ({arm['variant']})" if arm.get("variant") else ""
        return f"{arm.get('server')} {arm.get('version')}{variant}"

    def runs(cell: Mapping[str, Any]) -> str:
        note = status_text(cell["runs"])
        return f"{cell['n_ok']}/{cell['n_total']}" + (notes.ref(note) if note else "")

    rows = [["Server", *(server(c) for c in cells)]]
    if any(c["arm"].get("bds") for c in cells):
        rows.append(["Hosts BDS", *(c["arm"].get("bds") or "-" for c in cells)])
    rows.append(["Runs ok", *(runs(c) for c in cells)])
    rows.append(["Fidelity", *(c["parity"].get("fidelity") or "unknown" for c in cells)])
    for key, label in _FACT_ROWS:
        if any(c["facts"].get(key) for c in cells):
            rows.append(
                [label, *(" / ".join(_format_fact(key, v) for v in c["facts"].get(key, ())) or "-" for c in cells)]
            )
    rows.append(["Overrides", *(_overrides(c, notes) for c in cells)])
    doc.add("#### Arms")
    doc.add(*_table(header, rows, numeric=False))


_FACT_ROWS = (
    ("allocator", "Allocator"),
    ("jvm_heap_mib", "JVM heap"),
    ("world", "World"),
    ("world_generator", "Terrain generator"),
    ("csg_setting", "Client-side generation setting"),
    ("bot", "Bot module / protocol"),
    ("placement_kind", "Placement"),
)


def _format_fact(key: str, value: Any) -> str:
    if key == "jvm_heap_mib" and isinstance(value, (int, float)):
        return f"{value / 1024:g} GiB"
    if key == "csg_setting" and isinstance(value, bool):
        return "on" if value else "off"
    return _format_value(value)


def _overrides(cell: Mapping[str, Any], notes: Footnotes) -> str:
    counts: dict[str, int] = {}
    refs = []
    for override in cell["overrides"]:
        counts[override["category"]] = counts.get(override["category"], 0) + 1
        text = override_text(override)
        if text:
            refs.append(text)
    if not counts:
        return "none"
    summary = ", ".join(f"{c.value} {counts[c.value]}" for c in OverrideCategory if c.value in counts)
    return summary + notes.refs(refs)


def _stat(cell: Mapping[str, Any], row: Row) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """``(metric aggregate, statistic shown)`` of a row in a cell; the statistic is a part for distributions."""
    agg = cell["metrics"].get(row.metric)
    if agg is None:
        return None, None
    parts = agg.get("parts")
    if parts is not None:
        return agg, parts.get(row.part)
    return agg, agg


def _has_data(cell: Mapping[str, Any], row: Row) -> bool:
    """A row is shown when some cell measured it or should have; rows that apply to no arm are left out."""
    agg = cell["metrics"].get(row.metric)
    return agg is not None and agg.get("availability") != "not_applicable"


def _row_samples(cell: Mapping[str, Any], row: Row) -> list[Sample]:
    _, stat = _stat(cell, row)
    return samples(stat) if stat is not None else []


def _metric_table(
    doc: _Doc,
    title: str,
    rows: Iterable[Row],
    cells: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    notes: Footnotes,
    *,
    intro: str | None = None,
) -> None:
    lines = []
    for row in rows:
        if not any(_has_data(c, row) for c in cells):
            continue
        label = row.label + (notes.ref(row.note) if row.note else "")
        lines.append([label, *(_metric_cell(c, row, notes) for c in cells)])
    if not lines:
        return
    doc.add(f"#### {title}")
    if intro:
        doc.add(intro)
    doc.add(*_table(["", *names], lines))


def _metric_cell(cell: Mapping[str, Any], row: Row, notes: Footnotes) -> str:
    if cell["n_ok"] == 0:
        return "-"
    agg, stat = _stat(cell, row)
    if agg is None:
        return "-"
    note = availability_text(row.label, {**agg, "n": (stat or {}).get("n", 0)}, cell["n_ok"])
    ref = notes.ref(note) if note else ""
    if stat is None or not int(stat.get("n") or 0):
        word = "n/a" if agg.get("availability") == "not_applicable" else "unavailable"
        return word + ref
    return format_stat(stat, row.unit) + ref


def _parity_table(
    doc: _Doc, scenario: Mapping[str, Any], cells: Sequence[Mapping[str, Any]], names: Sequence[str]
) -> None:
    exercises = scenario.get("exercises") or ()
    doc.add("#### Parity")
    if not exercises:
        doc.add("The scenario exercises no features.")
        return
    doc.add(
        "Declared class per exercised feature, with the observing counter's median where the feature file names one."
    )
    rows = []
    for feature in exercises:
        row = [f"`{feature}`"]
        for cell in cells:
            declared = cell["parity"]["row"].get(feature, "unknown") if cell["parity"].get("recorded") else "unknown"
            observed = cell.get("observed", {}).get(feature)
            if observed and observed.get("median") is not None:
                value = observed["median"]
                shown = _format_value(value) if isinstance(value, bool) else format_number(float(value), "rate")
                declared += f" ({observed['metric']} {shown})"
            row.append(declared)
        rows.append(row)
    doc.add(*_table(["Feature", *names], rows, numeric=False))


def _differences(doc: _Doc, scenario: Mapping[str, Any], cells: Sequence[Mapping[str, Any]]) -> None:
    by_id = {c["arm"]["id"]: c for c in cells}
    everywhere = {c["arm"]["id"] for c in scenario.get("cells") or ()}
    pairs = [(c, by_id[c["baseline"]]) for c in cells if c.get("baseline") in by_id]
    elsewhere = [c for c in cells if c.get("baseline") and c["baseline"] not in by_id and c["baseline"] in everywhere]
    for cell in elsewhere:
        doc.add(
            f"`{cell['arm']['id']}` is not compared with `{cell['baseline']}`, the build it hosts, because the two are "
            "in different fidelity sections."
        )
    if not pairs:
        return
    requires = scenario.get("requires") or ()

    def comparable(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
        return all(a["parity"]["row"].get(k) == b["parity"]["row"].get(k) for k in requires)

    header = ["", *(f"`{a['arm']['id']}` vs `{b['arm']['id']}`" for a, b in pairs)]
    rows = []
    for row in (*COST_ROWS, *EFFICIENCY_ROWS):
        if row.better is None:
            continue
        efficiency = row in EFFICIENCY_ROWS
        texts = []
        for a, b in pairs:
            if efficiency and not comparable(a, b):
                texts.append("not comparable")
                continue
            sa, sb = _row_samples(a, row), _row_samples(b, row)
            texts.append(_difference(sa, sb, row) if sa and sb else "-")
        if any(t != "-" for t in texts):
            rows.append([row.label, *texts])
    if not rows:
        return
    doc.add("#### Differences from the hosted vanilla build")
    doc.add(
        "`better` or `worse` only when every run of one arm beats every run of the other and that ordering has a "
        f"one-sided p of at most {SEPARATION_P_MAX:g} (3 runs against 3 give p = 0.05); otherwise `~`. Ratios are of "
        "medians, with [lowest / highest, highest / lowest] across runs."
    )
    doc.add(*_table(header, rows))


def _difference(a: Sequence[Sample], b: Sequence[Sample], row: Row) -> str:
    result = compare(a, b, higher_is_better=row.better == HIGHER)
    text = str(result["verdict"])
    if "ratio" in result:
        text += f", {result['ratio']:.2f}x [{result['ratio_low']:.2f}, {result['ratio_high']:.2f}]"
    return text


def _claims(doc: _Doc, claims: Mapping[str, Any]) -> None:
    counts = claims.get("counts") or {}
    doc.add("## Claims")
    doc.add(
        f"From `{claims.get('file')}`: "
        + ", ".join(f"{counts.get(v, 0)} {v}" for v in ("pass", "fail", "inconclusive", "error"))
        + "."
    )
    rows = []
    for result in claims.get("results") or ():
        details = [f"{call} = {_format_value(value)}" for call, value in (result.get("values") or {}).items()]
        if result.get("reason"):
            details.append(str(result["reason"]))
        rows.append([str(result.get("text")), str(result.get("verdict")), "; ".join(details) or "-"])
    doc.add(*_table(["Claim", "Verdict", "Values and reason"], rows, numeric=False))


__all__ = [
    "COST_ROWS",
    "EFFICIENCY_ROWS",
    "HIGHER",
    "LOWER",
    "WORK_ROWS",
    "Row",
    "format_number",
    "format_stat",
    "layout_metric_ids",
    "render",
]
