# SPDX-License-Identifier: Apache-2.0
"""Render a summary as ``report.md``, laid out like the results section of a benchmark paper.

An Environment table, one Results table with a row per server, each server's differences from vanilla BDS, and
one-line notes: every run that is not ok, every flag, every ``rescue`` and ``nonvanilla`` override (rule R6) and every
metric an ok run could not provide. There is no composite score.

The renderer reads only the summary, so ``report.md`` can always be regenerated from ``summary.json``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bedrock_bench.model import Flag, Gate, OverrideCategory


@dataclass(frozen=True)
class Column:
    """One Results column: a key of ``CORE_METRICS``, its header with the unit, and the decimals it prints."""

    key: str
    header: str
    decimals: int | None = None  # None: three significant digits


COLUMNS: tuple[Column, ...] = (
    Column("tps", "TPS", 2),
    Column("tick_cpu_ms_per_tick", "Tick-thread CPU per tick (ms)"),
    Column("process_cpu_cores", "Server CPU (cores)", 2),
    Column("memory_mib", "Memory (MiB)", 0),
    Column("view_load_lowest_tps", "Lowest TPS while views load", 2),
    Column("view_load_p50_s", "View load, median player (s)"),
    Column("view_load_p90_s", "View load, p90 player (s)"),
    Column("view_load_max_s", "View load, slowest player (s)"),
    Column("view_load_chunks_per_s", "Chunk rate while views load (chunks/s)"),
    Column("chunks_per_player", "Chunks sent per player", 0),
)

_FLAG_TEXT: dict[str, str] = {
    Flag.SETTLE_CAPPED: "the settle phase hit its time cap before chunk delivery slowed down{runs}",
    Flag.VIEWS_INCOMPLETE: "views were still incomplete when the settle phase ended{runs}",
    Flag.BOT_PROTOCOL_DIFFERS: "the bots used a build for a different client protocol{runs}",
    Flag.DIFFERENT_BDS_BUILD: "the report has no run of the BDS build this server hosts ({bds})",
    Flag.UNPUBLISHED_ARTIFACT: "ran a local artifact instead of a pinned release{runs}",
    Gate.DROPPED: "the server dropped bots during steady{runs}",
}


# --------------------------------------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------------------------------------


def format_number(value: float, decimals: int | None = None) -> str:
    """A number with fixed decimals, or with three significant digits; never scientific notation."""
    if decimals is None:
        decimals = 0 if value == 0 else max(0, 2 - math.floor(math.log10(abs(value))))
    return f"{value:.{decimals}f}"


def format_stat(stat: Mapping[str, Any], decimals: int | None = None) -> str:
    """``median [min, max]``; ``n/a`` when the metric does not apply and ``-`` when nothing was measured."""
    if not stat.get("n"):
        return "n/a" if stat.get("availability") == "not_applicable" else "-"
    text = format_number(stat["median"], decimals)
    if stat["n"] > 1:
        text += f" [{format_number(stat['min'], decimals)}, {format_number(stat['max'], decimals)}]"
    return text


def _cpus(cpus: Sequence[int] | None) -> str:
    """``[0, 1, 2, 3, 8]`` -> ``0-3,8 (5 logical)``."""
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
    text = ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)
    return f"{text} ({len(ordered)} logical)"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], *, numeric: bool) -> list[str]:
    def cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    align = "---:" if numeric else "---"
    return [
        "| " + " | ".join(cell(h) for h in header) + " |",
        "|---|" + "|".join([align] * (len(header) - 1)) + "|",
        *("| " + " | ".join(cell(c) for c in row) + " |" for row in rows),
    ]


# --------------------------------------------------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------------------------------------------------


def _runs(count: int, total: int) -> str:
    return "" if count >= total else f" (in {count} of {total} runs)"


def flag_text(flag: str, *, count: int, total: int, arm: Mapping[str, Any]) -> str:
    """The note for a run flag."""
    template = _FLAG_TEXT.get(flag)
    if template is None:
        return f"flag `{flag}`{_runs(count, total)}"
    return template.format(runs=_runs(count, total), bds=arm.get("bds") or "?")


def override_text(override: Mapping[str, Any]) -> str:
    """The note for a ``rescue`` or ``nonvanilla`` override."""
    default = override.get("default")
    change = f"`{override.get('file')}` `{override.get('key')}` = `{override.get('value')}`"
    change += f" (default `{default}`)" if default is not None else " (not set by default)"
    lead = "rescue override" if override.get("category") == OverrideCategory.RESCUE else "non-vanilla override"
    return f"{lead} {change}: {str(override.get('reason') or '').rstrip('.')}"


def status_text(run: Mapping[str, Any]) -> str:
    """The note for a run that is not ok: its status and reasons."""
    return f"{run.get('status')}: {'; '.join(run.get('reasons') or ()) or 'no reason recorded'}"


def availability_text(label: str, stat: Mapping[str, Any], n_ok: int) -> str | None:
    """The note for a metric that some or all ok runs could not provide; None when every ok run measured it."""
    reasons = ", ".join(stat.get("reasons") or ())
    detail = f" ({reasons})" if reasons else ""
    if stat.get("availability") == "not_applicable":
        return f"{label}: not applicable{detail}"
    missing = n_ok - int(stat.get("n") or 0)
    if missing <= 0:
        return None
    return f"{label}: unavailable in {missing} of {n_ok} ok runs{detail}"


# --------------------------------------------------------------------------------------------------------------------
# Document
# --------------------------------------------------------------------------------------------------------------------


def render(summary: Mapping[str, Any]) -> str:
    """The markdown report of a summary, including its ``publish`` result when present."""
    servers = list(summary.get("servers") or ())
    blocks = ["# Results", *_environment(summary, servers)]
    if "publish" in summary:
        problems = summary["publish"].get("problems") or ()
        blocks += ["## Publication check", "\n".join(f"- Refused: {p}" for p in problems) or "Passed."]
    blocks += ["## Results", "Median [min, max] over the ok runs.", _results(servers)]
    blocks += ["## Differences from vanilla BDS", *_differences(servers)]
    notes = _notes(summary, servers)
    if notes:
        blocks += ["## Notes", "\n".join(f"- {n}" for n in notes)]
    return "\n\n".join(blocks) + "\n"


def _tag(server: Mapping[str, Any]) -> str:
    arm = server.get("arm") or {}
    hosted = ", ".join(f"{key} {arm[key]}" for key in ("bds", "python") if arm.get(key))
    return f"`{server['tag']}`" + (f" ({hosted})" if hosted else "")


def _environment(summary: Mapping[str, Any], servers: Sequence[Mapping[str, Any]]) -> list[str]:
    env = summary.get("environment") or {}
    machine = env.get("machine") or {}
    rows = [["CPU", str(machine.get("cpu") or "-")]]
    for placement in env.get("placements") or ({},):
        rows += [["Server CPUs", _cpus(placement.get("server_cpus"))], ["Bot CPUs", _cpus(placement.get("bot_cpus"))]]
    rows.append(["RAM", f"{machine['ram_gib']:g} GiB" if machine.get("ram_gib") else "-"])
    rows.append(["OS", " ".join(str(p) for p in (machine.get("os"), machine.get("kernel")) if p) or "-"])
    rows.append(["Python", ", ".join(env.get("python") or ()) or "-"])
    for harness in env.get("harness") or ({},):
        git = f" (`{harness['git']}`{', dirty' if harness.get('dirty') else ''})" if harness.get("git") else ""
        rows.append(["Harness", f"bedrock-bench {harness.get('version', '?')}{git}"])
    for bot in env.get("bots") or ():
        gophertunnel = f", gophertunnel {bot['gophertunnel']}" if bot.get("gophertunnel") else ""
        rows.append(["Bots", f"{bot.get('module')}, protocol {bot.get('protocol')}{gophertunnel}"])
    workload = env.get("workload") or {}
    if workload.get("sha256"):
        rows.append(["Workload", f"{workload.get('name')}, sha256 `{str(workload['sha256'])[:12]}`"])
    rows.append(["Servers", ", ".join(_tag(s) for s in servers) or "-"])
    return ["## Environment", "\n".join(_table(["", ""], rows, numeric=False))]


def _results(servers: Sequence[Mapping[str, Any]]) -> str:
    header = ["Server", "Runs ok", *(c.header for c in COLUMNS)]
    rows = [
        [
            f"`{server['tag']}`",
            f"{server['n_ok']}/{server['n_total']}",
            *(format_stat(server["metrics"].get(c.key) or {}, c.decimals) for c in COLUMNS),
        ]
        for server in servers
    ]
    return "\n".join(_table(header, rows, numeric=True))


def _differences(servers: Sequence[Mapping[str, Any]]) -> list[str]:
    blocks = []
    for server in servers:
        items = server.get("differences") or ()
        if not items:
            blocks.append(f"`{server['tag']}`: none.")
            continue
        lines = [f"`{server['tag']}`:", ""]
        for item in items:
            source = f" ([source]({item['source']}))" if item.get("source") else ""
            lines.append(f"- {item['text']}{source}")
        blocks.append("\n".join(lines))
    return blocks


def _notes(summary: Mapping[str, Any], servers: Sequence[Mapping[str, Any]]) -> list[str]:
    env = summary.get("environment") or {}
    notes = []
    if any(p.get("kind") == "vcpu" for p in env.get("placements") or ()):
        notes.append("CPU placement is on virtual CPUs, not physical cores.")
    if any(h.get("dirty") for h in env.get("harness") or ()):
        notes.append("The harness checkout had uncommitted changes.")
    current = summary.get("current_analysis_version")
    if summary.get("analysis_version") not in (None, current):
        notes.append(
            f"Metrics were computed with analysis version {summary.get('analysis_version')}; this harness uses "
            f"{current}. Run `bbench analyze` to recompute them."
        )
    for server in servers:
        tag = f"`{server['tag']}`"
        notes += [f"{tag} run `{run['run_id']}`: {status_text(run)}" for run in server["runs"] if run["status"] != "ok"]
        notes += [
            f"{tag}: " + flag_text(flag, count=count, total=server["n_total"], arm=server["arm"])
            for flag, count in server["flags"].items()
        ]
        notes += [f"{tag}: {override_text(o)}" for o in server.get("overrides") or ()]
        for column in COLUMNS:
            text = availability_text(column.header, server["metrics"].get(column.key) or {}, server["n_ok"])
            if text:
                notes.append(f"{tag}: {text}")
    return notes


__all__ = ["COLUMNS", "Column", "availability_text", "flag_text", "format_number", "format_stat", "render"]
