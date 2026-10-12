# SPDX-License-Identifier: Apache-2.0
"""Aggregate the runs of a report directory into ``summary.json``.

The summary is plain JSON data and ``report.md`` is rendered from it alone. It holds no wall-clock time and no paths,
so regenerating it from the same run directories gives the same bytes.

Rules (docs/methodology.md#statistics-and-run-status):

- One row per server tag. Only ``ok`` runs enter the statistics; ``server_failed`` and ``invalid`` runs are counted
  and listed with their reasons, never averaged.
- Each statistic is the median, min and max over the ok runs.
- A report holds one machine, one machine profile and one config; mixed inputs are refused, not averaged.

The core metrics, their order and their titles are fixed here, so every report has the same columns.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench import ANALYSIS_VERSION, __version__
from bedrock_bench.model import Availability, Flag, Status, canonical_json

SUMMARY_SCHEMA = "bedrock-bench/summary/2"

#: Written by ``bbench run`` next to the run directories: ``{"skipped": ["<tag>: <reason>", ...]}``.
SESSION_FILE = "session.json"

#: Minimum ok runs per server for ``bbench report --publish`` (unless every run of it failed).
PUBLISH_MIN_OK = 3


class ReportError(ValueError):
    """A directory cannot be reported: missing, empty or mixing inputs that must not be mixed."""


@dataclass(frozen=True)
class CoreMetric:
    """One column of the Results table and of summary.csv."""

    column: str  # key in summary.json, column prefix in summary.csv
    metric: str  # metrics.json id
    title: str  # report.md header and chart title, with the unit
    stat: str | None = None  # key of a distribution value
    decimals: int | None = None  # None: three significant digits


CORE_METRICS: tuple[CoreMetric, ...] = (
    CoreMetric("tps", "tps_window_steady", "TPS", decimals=2),
    CoreMetric("tick_cpu_ms_per_tick", "tick_cpu_ms_per_tick_steady", "Tick-thread CPU per tick (ms)"),
    CoreMetric("process_cpu_cores", "process_cores_steady", "Server CPU (cores)", decimals=2),
    CoreMetric("memory_mib", "private_mib_steady", "Memory (MiB)", decimals=0),
    CoreMetric("view_load_lowest_tps", "tps_load_low5", "Lowest TPS while views load", decimals=2),
    CoreMetric("view_load_p50_s", "view_t90_own_s", "View load time, median player (s)", stat="median"),
    CoreMetric("view_load_p90_s", "view_t90_own_s", "View load time, p90 player (s)", stat="p90"),
    CoreMetric("view_load_max_s", "view_t90_own_s", "View load time, slowest player (s)", stat="max"),
    CoreMetric("view_load_chunks_per_s", "chunks_per_s_load", "Chunks per second while views load"),
    CoreMetric("chunks_in_view_per_player", "chunks_per_bot", "Chunks in view per player", stat="median", decimals=0),
)


# --------------------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """One run directory: run.json plus metrics.json, with the status the report uses."""

    run_id: str
    status: Status
    reasons: tuple[str, ...]
    flags: tuple[str, ...]
    run: Mapping[str, Any]
    metrics: Mapping[str, Any]  # metric id -> metrics.json entry; empty without metrics.json
    analysis_version: int | None  # of metrics.json
    path: Path | None = None  # the run directory

    @property
    def arm(self) -> Mapping[str, Any]:
        return self.run.get("arm") or {}

    @property
    def tag(self) -> str:
        return f"{self.arm.get('server')}@{self.arm.get('version')}"


def load_runs(report_dir: Path) -> list[RunRecord]:
    """Read every run directory directly below ``report_dir``.

    A run with only ``run.partial.json`` did not finish and counts as ``invalid``. A finished run without
    ``metrics.json`` also counts as ``invalid`` until ``bbench analyze`` produces it. When metrics.json exists its
    status wins, because ``bbench analyze`` recomputes it from the raw files.
    """
    report_dir = Path(report_dir)
    if not report_dir.is_dir():
        raise ReportError(f"{report_dir.as_posix()}: not a directory")
    records = []
    for child in sorted(p for p in report_dir.iterdir() if p.is_dir()):
        finished = (child / "run.json").is_file()
        run_file = child / ("run.json" if finished else "run.partial.json")
        if not run_file.is_file():
            continue
        run = _read_json(run_file)
        metrics_file = child / "metrics.json"
        metrics_doc = _read_json(metrics_file) if finished and metrics_file.is_file() else None
        records.append(make_record(run, metrics_doc, finished=finished, path=child))
    if not records:
        raise ReportError(f"{report_dir.as_posix()}: no run directories (expected <run_id>/run.json)")
    return records


def load_skipped(report_dir: Path) -> list[str]:
    """The servers the session left out, each as ``"<tag>: <reason>"``; empty without a session file."""
    path = Path(report_dir) / SESSION_FILE
    if not path.is_file():
        return []
    return [str(line) for line in _read_json(path).get("skipped") or ()]


def write_skipped(report_dir: Path, skipped: Sequence[str]) -> None:
    text = json.dumps({"skipped": list(skipped)}, indent=2) + "\n"
    (Path(report_dir) / SESSION_FILE).write_text(text, encoding="utf-8", newline="\n")


def make_record(
    run: Mapping[str, Any], metrics_doc: Mapping[str, Any] | None, *, finished: bool = True, path: Path | None = None
) -> RunRecord:
    """Build a :class:`RunRecord` from parsed run.json (or run.partial.json) and metrics.json documents."""
    run_id = str(run.get("run_id") or "?")
    flags = list(run.get("flags") or ())
    if not finished:
        status, reasons = Status.INVALID, ["run did not finish (only run.partial.json)"]
    elif metrics_doc is None:
        status, reasons = Status.INVALID, ["metrics.json missing; run 'bbench analyze'"]
    else:
        raw = metrics_doc.get("status") or run.get("status")
        try:
            status = Status(raw)
        except ValueError:
            raise ReportError(f"{run_id}: unknown status {raw!r}") from None
        reasons = list(metrics_doc.get("status_reasons") or run.get("status_reasons") or ())
        flags += metrics_doc.get("flags") or ()
    return RunRecord(
        run_id=run_id,
        status=status,
        reasons=tuple(reasons),
        flags=tuple(sorted(set(flags))),
        run=run,
        metrics=(metrics_doc or {}).get("metrics") or {},
        analysis_version=(metrics_doc or {}).get("analysis_version"),
        path=path,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ReportError(f"{path.parent.name}/{path.name}: cannot read: {exc}") from None
    if not isinstance(data, dict):
        raise ReportError(f"{path.parent.name}/{path.name}: not a JSON object")
    return data


# --------------------------------------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def core_metric(entries: Sequence[Mapping[str, Any] | None], part: str | None) -> dict[str, Any]:
    """Median, min and max of one core metric over the ok runs of a server.

    ``entries`` are the metrics.json entries, one per ok run, ``None`` where the run lacks the metric; ``part`` picks
    a key of a distribution value. ``values`` stays aligned with the runs (``None`` where not measured).
    """
    values: list[float | None] = []
    reasons: set[str] = set()
    not_applicable = 0
    for entry in entries:
        value = None
        if entry is None:
            reasons.add("not computed")
        elif entry.get("availability") == Availability.MEASURED:
            raw = entry.get("value")
            value = raw.get(part) if part is not None and isinstance(raw, Mapping) else raw
            if not _is_number(value):
                value = None
                reasons.add("not computed")
        else:
            not_applicable += entry.get("availability") == Availability.NOT_APPLICABLE
            reasons.add(str(entry.get("reason") or entry.get("availability")))
        values.append(float(value) if value is not None else None)
    measured = [v for v in values if v is not None]
    out: dict[str, Any] = {"values": values, "n": len(measured)}
    if measured:
        out.update(median=statistics.median(measured), min=min(measured), max=max(measured))
        out["availability"] = Availability.MEASURED.value
    elif entries and not_applicable == len(entries):
        out["availability"] = Availability.NOT_APPLICABLE.value
    else:
        out["availability"] = Availability.UNAVAILABLE.value
    if reasons and len(measured) < len(entries):
        out["reasons"] = sorted(reasons)
    return out


# --------------------------------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------------------------------


def summarize(runs: Sequence[RunRecord], skipped: Sequence[str] = ()) -> dict[str, Any]:
    """Build the ``summary.json`` document from loaded runs and the servers the session skipped."""
    if not runs:
        raise ReportError("no runs to report")
    versions = sorted({r.analysis_version for r in runs if r.analysis_version is not None})
    if len(versions) > 1:
        raise ReportError(
            f"runs were analysed with different analysis versions {versions}; run 'bbench analyze' on every run"
        )
    _check_same(runs)
    by_tag: dict[str, list[RunRecord]] = {}
    for record in runs:
        by_tag.setdefault(record.tag, []).append(record)
    servers = [_server(sorted(by_tag[tag], key=lambda r: r.run_id)) for tag in sorted(by_tag)]
    _flag_missing_builds(servers)
    return {
        "schema": SUMMARY_SCHEMA,
        "harness_version": __version__,
        "analysis_version": versions[0] if versions else None,
        "current_analysis_version": ANALYSIS_VERSION,
        "n_runs": len(runs),
        "status_counts": {status.value: sum(r.status is status for r in runs) for status in Status},
        "environment": _environment(runs, servers),
        "columns": [{"column": m.column, "title": m.title, "decimals": m.decimals} for m in CORE_METRICS],
        "servers": servers,
        "skipped": list(skipped),
    }


def _distinct(items: Iterable[Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        if item:
            seen.setdefault(canonical_json(item), dict(item))
    return [seen[key] for key in sorted(seen)]


def _pick(mapping: Mapping[str, Any] | None, keys: Iterable[str]) -> dict[str, Any]:
    mapping = mapping or {}
    return {key: mapping.get(key) for key in keys if key in mapping}


def _check_same(runs: Sequence[RunRecord]) -> None:
    """Refuse a directory that mixes configs, machine profiles or machines."""
    configs = sorted({str((r.run.get("workload") or {}).get("sha256")) for r in runs})
    if len(configs) > 1:
        raise ReportError(f"runs used different configs (sha256 {', '.join(configs)})")
    machines = [r.run.get("machine") or {} for r in runs]
    profiles = sorted({str(m.get("profile_sha256")) for m in machines})
    if len(profiles) > 1:
        raise ReportError(f"runs used different machine profiles (rule R3): {', '.join(profiles)}")
    fingerprints = sorted({str(m.get("fingerprint_id")) for m in machines})
    if len(fingerprints) > 1:
        raise ReportError(f"runs come from different machines: {', '.join(fingerprints)}")


_MACHINE_KEYS = ("cpu", "logical", "physical", "ram_gib", "os", "kernel", "virtualized")
_PLACEMENT_KEYS = ("server_cpus", "bot_cpus", "kind")
_BOT_KEYS = ("module", "protocol", "gophertunnel")


def _environment(runs: Sequence[RunRecord], servers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    harness = [r.run.get("harness") or {} for r in runs]
    return {
        "machine": _pick(runs[-1].run.get("machine"), _MACHINE_KEYS),
        "placements": _distinct(_pick(r.run.get("placement"), _PLACEMENT_KEYS) for r in runs),
        "python": sorted({str(h["python"]) for h in harness if h.get("python")}),
        "harness": _distinct(_pick(h, ("version", "git", "dirty")) for h in harness),
        "bots": _distinct(_pick(r.run.get("bot"), _BOT_KEYS) for r in runs),
        "config": _config(runs[-1].run),
        "servers": [s["arm"] for s in servers],
    }


def _config(run: Mapping[str, Any]) -> dict[str, Any]:
    """Name, bot count and hash of the config a run used."""
    workload = run.get("workload") or {}
    bots = ((workload.get("params") or {}).get("bots") or {}).get("n")
    return {**_pick(workload, ("name", "sha256")), **({"bots": bots} if bots is not None else {})}


def _server(runs: Sequence[RunRecord]) -> dict[str, Any]:
    ok = [r for r in runs if r.status is Status.OK]
    flags: dict[str, int] = {}
    for record in runs:
        for flag in record.flags:
            flags[flag] = flags.get(flag, 0) + 1
    return {
        "tag": runs[-1].tag,
        "arm": _pick(runs[-1].arm, ("server", "version", "bds", "python", "artifact")),
        "n_total": len(runs),
        "n_ok": len(ok),
        "n_server_failed": sum(r.status is Status.SERVER_FAILED for r in runs),
        "n_invalid": sum(r.status is Status.INVALID for r in runs),
        "runs": [
            {
                "run_id": r.run_id,
                "status": r.status.value,
                "reasons": list(r.reasons),
                "official": r.run.get("official"),
                "nonstandard": list(r.run.get("nonstandard") or ()),
            }
            for r in runs
        ],
        "flags": dict(sorted(flags.items())),
        "metrics": {m.column: core_metric([r.metrics.get(m.metric) for r in ok], m.stat) for m in CORE_METRICS},
    }


def _flag_missing_builds(servers: Sequence[dict[str, Any]]) -> None:
    """Flag a server that hosts a BDS build when the report has no vanilla run of that build."""
    tags = {s["tag"] for s in servers}
    for server in servers:
        build = server["arm"].get("bds")
        if build and f"bds@{build}" not in tags:
            server["flags"][Flag.DIFFERENT_BDS_BUILD.value] = server["n_total"]
            server["flags"] = dict(sorted(server["flags"].items()))


# --------------------------------------------------------------------------------------------------------------------
# Publication rules
# --------------------------------------------------------------------------------------------------------------------


def publish_problems(summary: Mapping[str, Any]) -> list[str]:
    """Reasons ``bbench report --publish`` refuses this summary; empty when it may be published.

    Each server needs at least 3 ok runs unless every run of it failed, and only released artifacts and official runs.
    """
    problems = []
    for server in summary.get("servers", ()):
        tag = server["tag"]
        all_failed = server["n_server_failed"] == server["n_total"]
        if server["n_ok"] < PUBLISH_MIN_OK and not all_failed:
            problems.append(f"{tag}: {server['n_ok']} ok runs, publication needs at least {PUBLISH_MIN_OK}")
        if server["arm"].get("artifact") or Flag.UNPUBLISHED_ARTIFACT.value in server["flags"]:
            problems.append(f"{tag}: ran a local artifact, not a released one")
        unofficial = sorted({n for run in server["runs"] if run.get("official") is False for n in run["nonstandard"]})
        if any(run.get("official") is False for run in server["runs"]):
            detail = f" ({', '.join(unofficial)})" if unofficial else ""
            problems.append(f"{tag}: includes runs marked not official{detail}")
    return problems


def statuses(summary: Mapping[str, Any]) -> list[str]:
    """Every run status in the summary, for the exit code."""
    return [run["status"] for s in summary.get("servers", ()) for run in s["runs"]]


def write_summary(summary: Mapping[str, Any], path: Path) -> None:
    text = json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")


__all__ = [
    "CORE_METRICS",
    "PUBLISH_MIN_OK",
    "SUMMARY_SCHEMA",
    "CoreMetric",
    "ReportError",
    "RunRecord",
    "core_metric",
    "load_runs",
    "load_skipped",
    "make_record",
    "publish_problems",
    "statuses",
    "summarize",
    "write_skipped",
    "write_summary",
]
