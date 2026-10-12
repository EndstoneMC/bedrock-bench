# SPDX-License-Identifier: Apache-2.0
"""Aggregate the runs of a matrix directory into ``summary.json``.

The summary is plain JSON data, and everything downstream (the markdown report, claims) reads only the summary. It
holds no wall-clock time and no paths, so regenerating it from the same run directories gives the same bytes.

Rules (SPEC.md sections 8, 9 and 11):

- A cell is one arm in one scenario. Only ``ok`` runs enter the statistics. ``server_failed`` and ``invalid`` runs are
  counted (``n_ok/n_total``) and listed with their reasons, never averaged.
- Statistics are the median, min and max over the ok runs, plus every value. A censored value is a lower bound; a
  statistic is marked censored when the censored values leave it undetermined.
- Every table needs one machine profile and one scenario hash; mixed inputs are refused, not averaged.
- A difference is called only on complete separation: every run of one arm beats every run of the other, and the
  chance of that ordering arising by luck (one-sided, ``1 / C(n_a + n_b, n_a)``) is at most 5%. With 3 runs per arm
  that is p = 1/20; fewer runs never get a verdict.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench import ANALYSIS_VERSION, __version__
from bedrock_bench.config import ConfigError, load_features
from bedrock_bench.model import Availability, FeatureClass, FeatureFile, Flag, OverrideCategory, Status, canonical_json

SUMMARY_SCHEMA = "bedrock-bench/summary/1"

#: Largest one-sided p-value of complete separation that still prints "better" or "worse".
SEPARATION_P_MAX = 0.05

#: Minimum ok runs per cell for ``bbench report --publish`` (unless every run of the cell failed).
PUBLISH_MIN_OK = 3

Sample = tuple[float, bool]  # (value, censored); a censored value is a lower bound
FeatureLookup = Callable[[str], "FeatureFile | None"]

_OVERRIDE_ORDER = {category.value: i for i, category in enumerate(OverrideCategory)}


class ReportError(ValueError):
    """A matrix directory cannot be reported: missing, empty or mixing inputs that must not be mixed."""


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

    @property
    def arm(self) -> Mapping[str, Any]:
        return self.run.get("arm") or {}

    @property
    def scenario(self) -> Mapping[str, Any]:
        return self.run.get("scenario") or {}

    @property
    def repeat(self) -> int:
        return int((self.run.get("matrix") or {}).get("repeat") or 1)

    @property
    def order_index(self) -> int | None:
        return (self.run.get("matrix") or {}).get("order_index")


def load_runs(matrix_dir: Path) -> list[RunRecord]:
    """Read every run directory directly below ``matrix_dir``.

    A run with only ``run.partial.json`` did not finish and counts as ``invalid``. A finished run without
    ``metrics.json`` also counts as ``invalid`` until ``bbench analyze`` produces it. When metrics.json exists its
    status wins, because ``bbench analyze`` recomputes it from the raw files.
    """
    matrix_dir = Path(matrix_dir)
    if not matrix_dir.is_dir():
        raise ReportError(f"{matrix_dir.as_posix()}: not a directory")
    records = []
    for child in sorted(p for p in matrix_dir.iterdir() if p.is_dir()):
        finished = (child / "run.json").is_file()
        run_file = child / ("run.json" if finished else "run.partial.json")
        if not run_file.is_file():
            continue
        run = _read_json(run_file)
        metrics_file = child / "metrics.json"
        metrics_doc = _read_json(metrics_file) if finished and metrics_file.is_file() else None
        records.append(make_record(run, metrics_doc, finished=finished))
    if not records:
        raise ReportError(f"{matrix_dir.as_posix()}: no run directories (expected <run_id>/run.json)")
    return records


def make_record(run: Mapping[str, Any], metrics_doc: Mapping[str, Any] | None, *, finished: bool = True) -> RunRecord:
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


def bounded(stat: Callable[[Sequence[float]], float], samples: Sequence[Sample]) -> tuple[float, bool]:
    """Apply an order statistic to samples that may be censored.

    Returns ``(value, censored)``. ``value`` is the statistic of the listed values, a lower bound of the true one
    because a censored value is a lower bound. The statistic is exact when replacing every censored value by
    infinity does not change it; otherwise ``censored`` is True.
    """
    low = stat([v for v, _ in samples])
    high = stat([math.inf if c else v for v, c in samples])
    return low, high != low


def order_stats(samples: Sequence[Sample]) -> dict[str, Any]:
    """Median, min and max of samples, with ``lower_bounds`` naming the statistics that are only lower bounds."""
    out: dict[str, Any] = {}
    lower = []
    for name, stat in (("median", statistics.median), ("min", min), ("max", max)):
        value, censored = bounded(stat, samples)
        out[name] = value
        if censored:
            lower.append(name)
    if lower:
        out["lower_bounds"] = lower
    return out


def separation_p(n_a: int, n_b: int) -> float:
    """One-sided p-value of complete separation of two samples: ``1 / C(n_a + n_b, n_a)``."""
    return 1.0 / math.comb(n_a + n_b, n_a)


def separated(a: Sequence[Sample], b: Sequence[Sample], *, higher_is_better: bool) -> bool:
    """True when every run of ``a`` beats every run of ``b`` and the separation is significant.

    Censored values are lower bounds: a censored value in the sample that must be lower can never be shown to be.
    Ties do not count as beating.
    """
    if not a or not b or separation_p(len(a), len(b)) > SEPARATION_P_MAX:
        return False
    high, low = (a, b) if higher_is_better else (b, a)
    if any(c for _, c in low):
        return False
    return min(v for v, _ in high) > max(v for v, _ in low)


def compare(a: Sequence[Sample], b: Sequence[Sample], *, higher_is_better: bool) -> dict[str, Any]:
    """Compare arm ``a`` with arm ``b``: verdict ``better``, ``worse`` or ``~``, and the ratio of medians.

    The ratio interval is ``[min_a / max_b, max_a / min_b]``. It is omitted when a value is censored or not positive.
    """
    if separated(a, b, higher_is_better=higher_is_better):
        verdict = "better"
    elif separated(b, a, higher_is_better=higher_is_better):
        verdict = "worse"
    else:
        verdict = "~"
    out: dict[str, Any] = {"verdict": verdict, "n": [len(a), len(b)]}
    values_a = [v for v, c in a if not c]
    values_b = [v for v, c in b if not c]
    if a and b and len(values_a) == len(a) and len(values_b) == len(b) and min(values_a + values_b) > 0:
        out["ratio"] = statistics.median(values_a) / statistics.median(values_b)
        out["ratio_low"] = min(values_a) / max(values_b)
        out["ratio_high"] = max(values_a) / min(values_b)
    return out


def samples(agg: Mapping[str, Any]) -> list[Sample]:
    """The measured samples of an aggregate from :func:`aggregate_metric`."""
    censored = agg.get("censored") or [False] * len(agg.get("values", ()))
    return [(float(v), bool(c)) for v, c in zip(agg.get("values", ()), censored, strict=True) if v is not None]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def aggregate_metric(entries: Sequence[Mapping[str, Any] | None]) -> dict[str, Any]:
    """Aggregate one metric over the ok runs of a cell.

    ``entries`` are the metrics.json entries, one per ok run, ``None`` where the run lacks the metric. ``values`` stays
    aligned with the runs (``None`` where not measured). Numbers get order statistics, booleans a common ``value``
    (``None`` when the runs disagree), and objects (distributions) one aggregate per numeric key under ``parts``.
    """
    first = next((e for e in entries if e is not None), {})
    out: dict[str, Any] = {"unit": first.get("unit"), "window": first.get("window")}
    values: list[Any] = []
    censored: list[bool] = []
    reasons: set[str] = set()
    not_applicable = 0
    for entry in entries:
        if entry is None:
            values.append(None)
            censored.append(False)
            reasons.add("not computed")
            continue
        availability = entry.get("availability")
        if availability == Availability.MEASURED and entry.get("value") is not None:
            values.append(entry["value"])
            censored.append(bool(entry.get("censored")))
            continue
        values.append(None)
        censored.append(False)
        not_applicable += availability == Availability.NOT_APPLICABLE
        reasons.add(entry.get("reason") or str(availability))

    measured = [v for v in values if v is not None]
    out["n"] = len(measured)
    if measured:
        out["availability"] = Availability.MEASURED.value
    elif entries and not_applicable == len(entries):
        out["availability"] = Availability.NOT_APPLICABLE.value
    else:
        out["availability"] = Availability.UNAVAILABLE.value
    if reasons:
        out["reasons"] = sorted(reasons)

    if measured and all(isinstance(v, Mapping) for v in measured):
        keys = sorted({k for v in measured for k, x in v.items() if _is_number(x)})
        out["parts"] = {
            key: _numeric_agg([v.get(key) if isinstance(v, Mapping) else None for v in values], censored)
            for key in keys
        }
    elif measured and all(isinstance(v, bool) for v in measured):
        out["values"] = values
        out["value"] = measured[0] if len(set(measured)) == 1 else None
    elif measured and all(_is_number(v) for v in measured):
        out.update(_numeric_agg(values, censored))
    else:
        out["values"] = values
    return out


def _numeric_agg(values: Sequence[Any], censored: Sequence[bool]) -> dict[str, Any]:
    clean = [v if _is_number(v) else None for v in values]
    out: dict[str, Any] = {"values": clean, "n": sum(v is not None for v in clean)}
    if any(c for v, c in zip(clean, censored, strict=True) if v is not None):
        out["censored"] = [bool(c) and v is not None for v, c in zip(clean, censored, strict=True)]
    pairs = [(float(v), bool(c)) for v, c in zip(clean, censored, strict=True) if v is not None]
    if pairs:
        out.update(order_stats(pairs))
    return out


# --------------------------------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------------------------------


def default_features(server: str) -> FeatureFile | None:
    """``features/<server>.toml`` from the checkout, or None when it is missing or invalid."""
    try:
        return load_features(server)
    except ConfigError:
        return None


def summarize(
    runs: Sequence[RunRecord],
    *,
    default_name: str | None = None,
    features: FeatureLookup | None = default_features,
) -> dict[str, Any]:
    """Build the ``summary.json`` document from loaded runs.

    The matrix name comes from the runs; ``default_name`` is used when none records one. ``features`` looks up a
    server's feature file for the declared-versus-observed parity check; it is used only when its hash equals the one
    the run recorded.
    """
    if not runs:
        raise ReportError("no runs to report")
    versions = sorted({r.analysis_version for r in runs if r.analysis_version is not None})
    if len(versions) > 1:
        raise ReportError(
            f"runs were analysed with different analysis versions {versions}; run 'bbench analyze' on every run"
        )
    names = sorted(
        {str((r.run.get("matrix") or {}).get("name")) for r in runs if (r.run.get("matrix") or {}).get("name")}
    )
    if len(names) > 1:
        raise ReportError(f"runs from different matrices in one directory: {', '.join(names)}")
    matrix_name = names[0] if names else default_name
    if not matrix_name:
        raise ReportError("cannot tell the matrix name: no run records one")

    by_scenario: dict[str, list[RunRecord]] = {}
    for record in runs:
        by_scenario.setdefault(str(record.scenario.get("name") or "?"), []).append(record)

    def scenario_key(name: str) -> tuple[float, str]:
        indices = [r.order_index for r in by_scenario[name] if r.order_index is not None]
        return (min(indices) if indices else math.inf, name)

    scenarios = [_scenario(name, by_scenario[name], features) for name in sorted(by_scenario, key=scenario_key)]
    counts = {status.value: sum(r.status is status for r in runs) for status in Status}
    return {
        "schema": SUMMARY_SCHEMA,
        "harness_version": __version__,
        "analysis_version": versions[0] if versions else None,
        "current_analysis_version": ANALYSIS_VERSION,
        "matrix": matrix_name,
        "n_runs": len(runs),
        "status_counts": counts,
        "conditions": _conditions(runs),
        "scenarios": scenarios,
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


_MACHINE_KEYS = ("label", "cpu", "logical", "physical", "ram_gib", "os", "kernel", "power", "virtualized")
_PLACEMENT_KEYS = ("server_cpus", "bot_cpus", "harness_cpus", "priority", "kind", "method", "verified")
_BOT_KEYS = ("module", "protocol", "sha256", "version", "go", "gophertunnel", "patches")


def _conditions(runs: Sequence[RunRecord]) -> dict[str, Any]:
    orders = sorted(
        {str((r.run.get("matrix") or {}).get("order")) for r in runs if (r.run.get("matrix") or {}).get("order")}
    )
    return {
        "harness": _distinct(_pick(r.run.get("harness"), ("version", "git", "dirty")) for r in runs),
        "backends": sorted({str((r.run.get("harness") or {}).get("backend")) for r in runs if r.run.get("harness")}),
        "machines": _distinct(_pick(r.run.get("machine"), _MACHINE_KEYS) for r in runs),
        "placements": _distinct(_pick(r.run.get("placement"), _PLACEMENT_KEYS) for r in runs),
        "bots": _distinct(_pick(r.run.get("bot"), _BOT_KEYS) for r in runs),
        "order": orders,
        "repeats": max(r.repeat for r in runs),
    }


def _check_same(runs: Sequence[RunRecord], scenario: str) -> dict[str, Any] | None:
    """Refuse a table that mixes scenario hashes or machine profiles; return the table's machine."""
    hashes = sorted({str(r.scenario.get("sha256")) for r in runs if r.scenario.get("sha256")})
    if len(hashes) > 1:
        raise ReportError(f"scenario {scenario!r}: runs used different scenario files (sha256 {', '.join(hashes)})")
    machines = _distinct(_pick(r.run.get("machine"), ("fingerprint_id", "profile_sha256", "label")) for r in runs)
    profiles = sorted({str(m.get("profile_sha256")) for m in machines})
    fingerprints = sorted({str(m.get("fingerprint_id")) for m in machines})
    if len(profiles) > 1:
        raise ReportError(
            f"scenario {scenario!r}: runs used different machine profiles (rule R3): {', '.join(profiles)}"
        )
    if len(fingerprints) > 1:
        raise ReportError(f"scenario {scenario!r}: runs come from different machines: {', '.join(fingerprints)}")
    return machines[0] if machines else None


def _scenario(name: str, runs: Sequence[RunRecord], features: FeatureLookup | None) -> dict[str, Any]:
    machine = _check_same(runs, name)
    resolved = next((r.scenario.get("resolved") for r in runs if r.scenario.get("resolved")), None) or {}
    exercises = list(resolved.get("exercises") or ())
    requires = list(resolved.get("requires") or ())

    by_arm: dict[str, list[RunRecord]] = {}
    for record in runs:
        by_arm.setdefault(str(record.arm.get("id") or "?"), []).append(record)
    cells = []
    for arm_id in sorted(by_arm):
        ordered = sorted(by_arm[arm_id], key=lambda r: (r.repeat, r.order_index or 0, r.run_id))
        cells.append(_cell(ordered, exercises, features))
    _link_baselines(cells)

    return {
        "name": name,
        "sha256": next((r.scenario.get("sha256") for r in runs if r.scenario.get("sha256")), None),
        "description": resolved.get("description") or "",
        "settings": _settings(resolved),
        "exercises": exercises,
        "requires": requires,
        "machine": machine,
        "cross_server": _cross_server([c["arm"] for c in cells]),
        "cells": cells,
    }


def _settings(resolved: Mapping[str, Any]) -> dict[str, Any]:
    bots = resolved.get("bots") or {}
    server = resolved.get("server") or {}
    world = resolved.get("world") or {}
    phases = {p.get("name"): p for p in resolved.get("phases") or () if isinstance(p, Mapping)}
    return {
        "bots": bots.get("n"),
        "view_distance": server.get("view_distance"),
        "tick_distance": server.get("tick_distance"),
        "csg": server.get("csg"),
        "world_source": world.get("source"),
        "seed": world.get("seed"),
        "steady_s": (phases.get("steady") or {}).get("duration_s"),
        "chaos_s": (phases.get("chaos") or {}).get("duration_s"),
    }


def _cross_server(arms: Sequence[Mapping[str, Any]]) -> bool:
    """True when a table holds different servers that are not all one BDS build and servers hosting it."""
    if len({a.get("server") for a in arms}) < 2:
        return False
    builds = {a.get("bds") for a in arms if a.get("bds")}

    def bds_family(arm: Mapping[str, Any]) -> bool:
        return bool(arm.get("bds")) or arm.get("version") in builds

    return not all(bds_family(a) for a in arms)


def _cell(runs: Sequence[RunRecord], exercises: Sequence[str], features: FeatureLookup | None) -> dict[str, Any]:
    ok = [r for r in runs if r.status is Status.OK]
    arm = _pick(runs[-1].arm, ("id", "server", "version", "variant", "bds", "python", "artifact"))
    flag_counts: dict[str, int] = {}
    for record in runs:
        for flag in record.flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    metric_ids = sorted({mid for r in ok for mid in r.metrics})
    metrics = {mid: aggregate_metric([r.metrics.get(mid) for r in ok]) for mid in metric_ids}

    parity = _parity(runs, exercises)
    observed = _observed(ok, parity, exercises, features, arm.get("server"))
    for feature, entry in observed.items():
        if entry.get("mismatches"):
            key = f"{Flag.PARITY_MISMATCH.value}:{feature}"
            flag_counts[key] = max(flag_counts.get(key, 0), entry["mismatches"])

    return {
        "arm": arm,
        "n_total": len(runs),
        "n_ok": len(ok),
        "n_server_failed": sum(r.status is Status.SERVER_FAILED for r in runs),
        "n_invalid": sum(r.status is Status.INVALID for r in runs),
        "runs": [
            {
                "run_id": r.run_id,
                "repeat": r.repeat,
                "status": r.status.value,
                "reasons": list(r.reasons),
                "official": r.run.get("official"),
                "nonstandard": list(r.run.get("nonstandard") or ()),
            }
            for r in runs
        ],
        "ok_runs": [r.run_id for r in ok],
        "flags": dict(sorted(flag_counts.items())),
        "facts": _facts(runs),
        "overrides": _overrides(runs),
        "parity": parity,
        "observed": observed,
        "baseline": None,
        "metrics": metrics,
    }


def _facts(runs: Sequence[RunRecord]) -> dict[str, list[Any]]:
    """Observed facts that differ between servers: distinct values over the runs, so a change between repeats shows."""
    collected: dict[str, list[Any]] = {}

    def add(key: str, value: Any) -> None:
        if value is not None and value not in collected.setdefault(key, []):
            collected[key].append(value)

    for record in runs:
        facts = ((record.run.get("server") or {}).get("facts")) or {}
        for key in ("allocator", "jvm_heap_mib", "csg_setting"):
            add(key, facts.get(key))
        world = record.run.get("world") or {}
        add("world_generator", world.get("generator"))
        if "fresh" in world:
            add("world", "fresh" if world.get("fresh") else "template")
        bot = record.run.get("bot") or {}
        if bot.get("module"):
            add("bot", f"{bot.get('module')}/{bot.get('protocol')}")
        placement = record.run.get("placement") or {}
        add("placement_kind", placement.get("kind"))
    return {key: collected[key] for key in sorted(collected) if collected[key]}


def _overrides(runs: Sequence[RunRecord]) -> list[dict[str, Any]]:
    found = _distinct(
        _pick(o, ("category", "file", "key", "default", "value", "reason"))
        for r in runs
        for o in r.run.get("overrides") or ()
    )
    return sorted(found, key=lambda o: (_OVERRIDE_ORDER.get(o.get("category"), 99), o.get("file"), o.get("key")))


def _parity(runs: Sequence[RunRecord], exercises: Sequence[str]) -> dict[str, Any]:
    """Declared parity of the cell and the features it does less on, among the exercised ones."""
    recorded = next((r.run.get("parity") for r in runs if r.run.get("parity")), None)
    if recorded is None:
        return {
            "recorded": False,
            "fidelity": None,
            "row": {},
            "does_less": [{"feature": f, "class": FeatureClass.UNKNOWN.value} for f in exercises],
            "full_fidelity": False,
        }
    row = dict(recorded.get("row") or {})
    does_less = [
        {"feature": f, "class": row.get(f, FeatureClass.UNKNOWN.value)}
        for f in exercises
        if row.get(f) != FeatureClass.VANILLA
    ]
    return {
        "recorded": True,
        "fidelity": recorded.get("fidelity"),
        "range": recorded.get("range"),
        "file_sha256": recorded.get("file_sha256"),
        "row": row,
        "does_less": does_less,
        "full_fidelity": not does_less,
    }


def observed_present(value: Any) -> bool | None:
    """Whether an observing counter shows the feature at work: True, False, or None when it cannot tell."""
    if isinstance(value, bool):
        return value
    if _is_number(value):
        return value > 0
    return None


def _observed(
    ok: Sequence[RunRecord],
    parity: Mapping[str, Any],
    exercises: Sequence[str],
    features: FeatureLookup | None,
    server: str | None,
) -> dict[str, dict[str, Any]]:
    """Declared versus observed parity for exercised features that name an ``observed_by`` counter.

    A declared ``vanilla`` feature whose counter shows nothing, or a declared ``absent`` feature whose counter shows
    work, is a mismatch. Other classes, and counters that are not a number or a boolean, are shown but not judged.
    """
    if not ok or not parity.get("recorded") or features is None or not server:
        return {}
    feature_file = features(server)
    if feature_file is None or feature_file.sha256 != parity.get("file_sha256"):
        return {}
    version = str(ok[0].arm.get("version") or "")
    feature_range = feature_file.row(version)
    if feature_range is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for feature in exercises:
        cell = feature_range.cells.get(feature)
        if cell is None or not cell.observed_by:
            continue
        declared = parity["row"].get(feature, FeatureClass.UNKNOWN.value)
        agg = aggregate_metric([r.metrics.get(cell.observed_by) for r in ok])
        mismatches = 0
        for value in agg.get("values") or ():
            present = observed_present(value)
            if present is None:
                continue
            if (declared == FeatureClass.VANILLA and not present) or (declared == FeatureClass.ABSENT and present):
                mismatches += 1
        out[feature] = {
            "metric": cell.observed_by,
            "declared": declared,
            "median": agg.get("median", agg.get("value")),
            "mismatches": mismatches,
        }
    return out


def _link_baselines(cells: list[dict[str, Any]]) -> None:
    """Point each arm that hosts a BDS build at the arm that is that build, or flag that the matrix lacks it.

    The baseline is the one arm with no host build, no variant and the hosted build as its version. With several such
    arms the baseline is ambiguous and none is linked.
    """
    for cell in cells:
        build = cell["arm"].get("bds")
        if not build:
            continue
        candidates = [
            other["arm"]["id"]
            for other in cells
            if other is not cell
            and not other["arm"].get("bds")
            and not other["arm"].get("variant")
            and other["arm"].get("version") == build
        ]
        if len(candidates) == 1:
            cell["baseline"] = candidates[0]
        elif not candidates:
            cell["flags"][Flag.DIFFERENT_BDS_BUILD.value] = cell["n_total"]
            cell["flags"] = dict(sorted(cell["flags"].items()))


# --------------------------------------------------------------------------------------------------------------------
# Publication rules
# --------------------------------------------------------------------------------------------------------------------


def publish_problems(summary: Mapping[str, Any]) -> list[str]:
    """Reasons ``bbench report --publish`` refuses this summary; empty when it may be published.

    Each cell needs at least 3 ok runs unless every run of it failed; only released artifacts and official runs;
    no ``unknown`` parity on an exercised feature; and a table that compares different servers must use equal chunk
    work (client-side generation off).
    """
    problems = []
    for scenario in summary.get("scenarios", ()):
        name = scenario["name"]
        if scenario.get("cross_server") and scenario.get("settings", {}).get("csg") is not False:
            problems.append(
                f"{name}: compares different servers with client-side chunk generation on; "
                "cross-server tables need an equal-chunk scenario"
            )
        for cell in scenario.get("cells", ()):
            where = f"{name} / {cell['arm']['id']}"
            all_failed = cell["n_server_failed"] == cell["n_total"]
            if cell["n_ok"] < PUBLISH_MIN_OK and not all_failed:
                problems.append(f"{where}: {cell['n_ok']} ok runs, publication needs at least {PUBLISH_MIN_OK}")
            if cell["arm"].get("artifact") or Flag.UNPUBLISHED_ARTIFACT.value in cell["flags"]:
                problems.append(f"{where}: ran a local artifact, not a released one")
            unofficial = sorted({n for run in cell["runs"] if run.get("official") is False for n in run["nonstandard"]})
            if any(run.get("official") is False for run in cell["runs"]):
                detail = f" ({', '.join(unofficial)})" if unofficial else ""
                problems.append(f"{where}: includes runs marked not official{detail}")
            unknown = [d["feature"] for d in cell["parity"]["does_less"] if d["class"] == FeatureClass.UNKNOWN]
            if unknown:
                problems.append(f"{where}: parity unknown for exercised features {', '.join(unknown)}")
    return problems


def statuses(summary: Mapping[str, Any]) -> list[str]:
    """Every run status in the summary, for the exit code."""
    return [run["status"] for s in summary.get("scenarios", ()) for c in s.get("cells", ()) for run in c["runs"]]


def write_summary(summary: Mapping[str, Any], path: Path) -> None:
    text = json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")


__all__ = [
    "PUBLISH_MIN_OK",
    "SEPARATION_P_MAX",
    "SUMMARY_SCHEMA",
    "ReportError",
    "RunRecord",
    "Sample",
    "aggregate_metric",
    "bounded",
    "compare",
    "default_features",
    "load_runs",
    "make_record",
    "observed_present",
    "order_stats",
    "publish_problems",
    "samples",
    "separated",
    "separation_p",
    "statuses",
    "summarize",
    "write_summary",
]
