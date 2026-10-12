# SPDX-License-Identifier: Apache-2.0
"""Synthetic runs with known answers, for the metric unit tests and the golden recompute test.

Every run is built from closed-form profiles (tick rate, tick-thread utilisation, chunk delivery), so expected values
can be worked out by hand. ``write_run_dir`` writes a run in the raw run-directory format;
the committed directories next to this file were produced by it, together with their ``metrics.json`` golden files.

Regenerate after an intended formula change (and an ``ANALYSIS_VERSION`` bump)::

    uv run python tests/fixtures/runs/synthetic.py

``--check`` compares the committed files with a fresh build instead of writing (gzip members by content).

The runs use the server name ``synthetic``, which no adapter registers, so ``bbench analyze`` on these directories
uses neutral adapter defaults, exactly as the golden files do.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench.model import (
    CpuSample,
    LogLine,
    MachineSample,
    ProbeSample,
    ProcSample,
    RunData,
    ThreadSample,
    ThreadSeries,
)
from bedrock_bench.workload import WORKLOAD, with_overrides

HERE = Path(__file__).resolve().parent
NAMES = ("synthetic-steady", "synthetic-dip", "synthetic-death", "synthetic-midnight")

NS = 1_000_000_000
ORIGIN_NS = 5_000 * NS
EPOCH_MS = 1_791_813_480_000  # 2026-10-12T13:58:00Z
MIDNIGHT_EPOCH_MS = 1_791_849_600_000 - 60_000  # one minute before 2026-10-13T00:00:00Z

N_BOTS = 6
BOT_START_S = 35.0  # the bot process starts at bots_start; its mono_ms counts from here
BOT_DELAY_MS = 2  # pipe delay between a bot line and the harness reading it
SERVER_CPUS = (0, 1, 2, 3)
BOT_CPUS = (4, 5, 6, 7)

MARKS = {
    "ready": 5.0,
    "idle0": 15.0,
    "idle1": 35.0,
    "bots_start": 35.0,
    "alljoined": 37.5,
    "settled": 100.0,
    "steady0": 115.0,
    "steady1": 205.0,
    "stop": 207.0,
}

WORKLOAD_6 = with_overrides(
    WORKLOAD,
    {
        "bots": {"n": N_BOTS},
        "phases": {"settle": {"min_s": 60.0}},
    },
)


@dataclass(frozen=True)
class Variant:
    name: str
    dip: bool = False  # tick rate halves in [50, 70) while the tick thread saturates
    death_s: float | None = None  # the server process dies at this time
    epoch_ms: int = EPOCH_MS
    rx_ns: bool = True  # bot lines carry the harness receive time

    @property
    def end_s(self) -> float:
        return self.death_s if self.death_s is not None else 207.0


VARIANTS = {
    "synthetic-steady": Variant("synthetic-steady"),
    "synthetic-dip": Variant("synthetic-dip", dip=True),
    "synthetic-death": Variant("synthetic-death", death_s=90.0),
    "synthetic-midnight": Variant("synthetic-midnight", epoch_ms=MIDNIGHT_EPOCH_MS, rx_ns=False),
}


# --------------------------------------------------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------------------------------------------------


def _integral(breaks: list[tuple[float, float]], t: float) -> float:
    """Integral from 0 to ``t`` of a step function given as ``[(from_s, value), ...]`` sorted by ``from_s``."""
    total = 0.0
    for k, (start, value) in enumerate(breaks):
        end = breaks[k + 1][0] if k + 1 < len(breaks) else math.inf
        if t <= start:
            break
        total += value * (min(t, end) - start)
    return total


def tps_profile(v: Variant) -> list[tuple[float, float]]:
    if v.dip:
        return [(0.0, 20.0), (50.0, 10.0), (70.0, 20.0)]
    return [(0.0, 20.0)]


def tick_util(v: Variant) -> list[tuple[float, float]]:
    steps = [(0.0, 0.05), (35.0, 0.6), (100.0, 0.4)]
    if v.dip:
        steps = [(0.0, 0.05), (35.0, 0.6), (50.0, 1.0), (70.0, 0.6), (100.0, 0.4)]
    return steps


def ticks_at(v: Variant, t: float) -> int:
    return 100 + math.floor(_integral(tps_profile(v), t) - _integral(tps_profile(v), MARKS["ready"]))


def cpu_ns(steps: list[tuple[float, float]], t: float) -> int:
    return round(_integral(steps, t) * NS)


@dataclass(frozen=True)
class ThreadSpec:
    tid: int
    name: str
    born: float
    exited: float
    util: list[tuple[float, float]]


def threads(v: Variant) -> list[ThreadSpec]:
    worker = [(0.0, 0.01), (35.0, 0.2)]
    return [
        ThreadSpec(2000, "tick", 0.0, math.inf, tick_util(v)),
        ThreadSpec(2001, "worker(0)", 0.0, math.inf, worker),
        ThreadSpec(2002, "worker(1)", 0.0, math.inf, worker),
        ThreadSpec(2003, "worker(2)", 0.0, math.inf, worker),
        ThreadSpec(2004, "io", 0.0, math.inf, [(0.0, 0.02)]),
        ThreadSpec(2005, "worker(3)", 60.5, 150.5, [(0.0, 0.3)]),
        ThreadSpec(2006, "job(0)", 0.0, 80.5, [(0.0, 0.1)]),
        ThreadSpec(2006, "job(1)", 119.5, 180.5, [(0.0, 0.1)]),
    ]


def thread_cpu_ns(spec: ThreadSpec, t: float) -> int:
    return round((_integral(spec.util, t) - _integral(spec.util, spec.born)) * NS)


def u_final(i: int) -> int:
    return 290 + 3 * i


# --------------------------------------------------------------------------------------------------------------------
# Building a run
# --------------------------------------------------------------------------------------------------------------------


def _ns(t: float) -> int:
    return ORIGIN_NS + round(t * NS)


def _times(start: float, step: float, end: float) -> Iterable[float]:
    k = 0
    while True:
        t = round(start + k * step, 6)
        if t > end:
            return
        yield t
        k += 1


def _probe(v: Variant) -> list[ProbeSample]:
    out = []
    for i, sent in enumerate(_times(6.0, 5.0, v.end_s - 1.0)):
        latency = 0.020 + 0.004 * (i % 3)
        out.append(ProbeSample(_ns(sent), _ns(sent + latency), ticks_at(v, sent + latency / 2)))
    return out


def _tick(v: Variant) -> list[CpuSample]:
    return [CpuSample(_ns(t), cpu_ns(tick_util(v), t)) for t in _times(5.0, 0.1, v.end_s)]


def _threads(v: Variant) -> ThreadSeries:
    samples = []
    for t in _times(5.0, 1.0, v.end_s):
        for spec in threads(v):
            if spec.born <= t <= spec.exited:
                samples.append(ThreadSample(_ns(t), spec.tid, spec.name, thread_cpu_ns(spec, t)))
    return ThreadSeries(samples)


def _process(v: Variant) -> list[ProcSample]:
    out = []
    for t in _times(5.0, 1.0, v.end_s):
        live = [s for s in threads(v) if s.born <= t <= s.exited]
        busy = sum(thread_cpu_ns(s, min(t, s.exited)) for s in threads(v) if s.born <= t) / NS + 0.1 * t
        out.append(
            ProcSample(
                t_ns=_ns(t),
                cpu_user_s=round(busy * 0.9, 6),
                cpu_sys_s=round(busy * 0.1, 6),
                resident_mib=round(1500.0 + 2.0 * (t - 5.0), 3),
                private_mib=round(1200.0 + 1.5 * (t - 5.0), 3),
                threads=30 + len(live),
                handles=800 + (50 if t >= 35.0 else 0),
            )
        )
    return out


def _machine(v: Variant) -> list[MachineSample]:
    out = []
    for k, t in enumerate(_times(5.0, 1.0, MARKS["stop"])):
        server = 0.0 if t > v.end_s else 0.35
        bots = 0.25 if t >= BOT_START_S else 0.01
        busy = tuple([server] * len(SERVER_CPUS) + [bots] * len(BOT_CPUS))
        out.append(MachineSample(_ns(t), busy, round(0.05 + 0.02 * (k % 3), 4), 0.01))
    return out


def _log(v: Variant) -> list[LogLine]:
    lines = [(1.0, "synthetic server: starting"), (5.0, "synthetic server: ready")]
    lines += [(36.0 + i * 0.3, f"synthetic server: player a{i:02d} in world") for i in range(N_BOTS)]
    if v.death_s is not None:
        lines.append((v.death_s, "synthetic server: crash"))
    else:
        lines.append((206.0, "synthetic server: stopping"))
    return [LogLine(_ns(t), v.epoch_ms + round(t * 1000), None, None, text, text) for t, text in lines]


def _commands(v: Variant) -> list[dict[str, Any]]:
    out = [
        {"enqueue_ns": p.send_ns, "send_ns": p.send_ns, "lane": "priority", "origin": "probe", "text": "tick"}
        for p in _probe(v)
    ]
    for i in range(N_BOTS):
        t = _ns(36.5 + i * 0.3)
        out.append({"enqueue_ns": t, "send_ns": t, "lane": "normal", "origin": "phase", "text": f"tp a{i:02d}"})
    out.sort(key=lambda c: (c["send_ns"], c["origin"], c["text"]))
    return out


def _bots(v: Variant) -> list[dict[str, Any]]:
    """Bot events, stamped on the bot clock; mono_ms 0 is bots_start."""
    events: list[tuple[int, dict[str, Any]]] = []

    def emit(mono_ms: int, ev: str, **fields: Any) -> None:
        events.append((mono_ms, {"v": 1, "ev": ev, **fields}))

    end_ms = round((v.end_s - BOT_START_S) * 1000)
    emit(
        0,
        "hello",
        build={"module": "loadbot", "version": "0.1.0", "protocol": 2193},
        flags={"n": N_BOTS},
        intervals_ms={"progress": 5000, "stat": 10000, "pkt": 30000},
    )
    tp_ms = {}
    for i in range(N_BOTS):
        name = f"a{i:02d}"
        dial = 300 * i
        join_ms = 800 + 10 * i
        tp_ms[i] = dial + join_ms + 500
        emit(dial, "dial", bot=name, i=i)
        emit(dial + join_ms, "spawned", bot=name, i=i, session=1, join_ms=join_ms, radius=10, dim=0)
        x, z = 1280 + (i % 11) * 640, 1280 + (i // 11) * 640
        emit(tp_ms[i], "tp", bot=name, pos=[x + 0.5, 300.0, z + 0.5], dim=0, n=1)
        emit(tp_ms[i] + 1000 + 100 * i, "first_chunk", bot=name, dt_ms=1000 + 100 * i)
    emit(2500, "alljoined", joined=N_BOTS, failed=0)

    def in_view(i: int, mono: int) -> int:
        since = mono - tp_ms[i] - 1000 - 100 * i
        if since < 0:
            return 0
        return min(u_final(i), u_final(i) * since // 40_000 + 1)

    for mono in range(5000, end_ms + 1, 5000):
        rows = []
        for i in range(N_BOTS):
            view = in_view(i, mono)
            fallen = mono - tp_ms[i] >= 3000
            x, z = 1280 + (i % 11) * 640 + 0.5, 1280 + (i // 11) * 640 + 0.5
            corrections = max(0, (mono - tp_ms[i]) // 500)
            rows.append(
                [f"a{i:02d}", view + 5 if view else 0, view, x, 70.0 + i if fallen else 300.0, z, 0, corrections, 1]
            )
        emit(mono, "progress", rows=rows)

    for mono in range(10_000, end_ms + 1, 10_000):
        uniq = sum(in_view(i, mono) + (5 if in_view(i, mono) else 0) for i in range(N_BOTS))
        views = sum(in_view(i, mono) for i in range(N_BOTS))
        seconds = mono / 1000
        emit(
            mono,
            "stat",
            online=N_BOTS,
            joined=N_BOTS,
            failed=0,
            dropped=0,
            left=0,
            rejoins=0,
            deaths=0,
            rx_pkts=round(500 * N_BOTS * seconds),
            tx_pkts=round(25 * N_BOTS * seconds),
            rx_bytes=round(400_000 * N_BOTS * seconds),
            tx_bytes=round(5_000 * N_BOTS * seconds),
            corrections=sum(max(0, (mono - tp_ms[i]) // 500) for i in range(N_BOTS)),
            level_chunks=uniq + uniq // 20,
            subchunks=24 * uniq,
            uniq=uniq,
            in_view=views,
            decode_errors=0,
        )

    for mono in range(30_000, end_ms + 1, 30_000):
        per = 30 * N_BOTS
        emit(
            mono,
            "pkt",
            rx={
                "LevelChunk": 40 * N_BOTS if mono <= 60_000 else 0,
                "CorrectPlayerMovePrediction": 2 * per,
                "MoveActorDelta": 10 * per,
                "UpdateBlock": per // 2,
                "PlayerLocation": 5 * per,
            },
        )

    if v.death_s is not None:
        drop_ms = end_ms + 500
        for i in range(N_BOTS):
            emit(drop_ms, "dropped", bot=f"a{i:02d}", reason="connection closed")
        stats = [e for e in events if e[1]["ev"] == "stat"]
        last = dict(stats[-1][1])
        last.update(online=0, dropped=N_BOTS)
        events.append((drop_ms + 500, last))
        done_ms = drop_ms + 1000
    else:
        summary_ms = end_ms - 1000
        for i in range(N_BOTS):
            arrivals = [1000 + 100 * i + (40_000 * k) // u_final(i) for k in range(u_final(i))]
            deltas = [arrivals[0]] + [b - a for a, b in zip(arrivals, arrivals[1:], strict=False)]
            emit(
                summary_ms,
                "bot_summary",
                bot=f"a{i:02d}",
                uniq=u_final(i) + 5,
                in_view=u_final(i),
                chunks=u_final(i) + 20,
                corrections=max(0, (summary_ms - tp_ms[i]) // 500),
                first_seen_ms=deltas,
            )
        done_ms = end_ms - 500
    emit(done_ms, "done")

    events.sort(key=lambda e: e[0])
    out = []
    bot_epoch0 = v.epoch_ms + round(BOT_START_S * 1000) + BOT_DELAY_MS
    for mono, event in events:
        event = {**event, "t": bot_epoch0 + mono, "mono_ms": mono}
        if v.rx_ns:
            event["rx_ns"] = _ns(BOT_START_S) + (mono + BOT_DELAY_MS) * 1_000_000
        out.append(event)
    return out


def _marks(v: Variant) -> dict[str, float | None]:
    if v.death_s is None:
        return dict(MARKS)
    return {k: (t if t is not None and t <= v.death_s else None) for k, t in MARKS.items()} | {"stop": v.death_s + 2}


def _run_json(v: Variant) -> dict[str, Any]:
    ok = v.death_s is None
    hex4 = hashlib.sha256(v.name.encode()).hexdigest()[:4]
    run: dict[str, Any] = {
        "schema": "bedrock-bench/run/1",
        "schema_minor": 0,
        "analysis_version": 1,
        "run_id": f"20261012T135800Z-synthetic@1.0-r1-{hex4}",
        "status": "ok" if ok else "server_failed",
        "status_reasons": [] if ok else ["server_alive: the server exited before teardown"],
        "flags": [],
        "exit_code": 0 if ok else 4,
        "official": False,
        "nonstandard": [],
        "harness": {"version": "0.1.0.dev0", "git": None, "dirty": None, "python": "3.13", "backend": "native-linux"},
        "session": {"id": "20261012T135800Z-0001", "repeat": 1, "order_index": 0, "order": "rotate"},
        "arm": {"id": "synthetic@1.0", "server": "synthetic", "version": "1.0", "bds": None},
        "artifacts": [{"name": "synthetic", "url": None, "sha256": "0" * 64, "size": 0, "source": "local"}],
        "install": {"tree_sha256": "1" * 64, "exe_sha256_before": None, "exe_sha256_after": None},
        "server": {
            "pid": 4242,
            "aux_pids": [],
            "banner_version": "1.0",
            "facts": {"allocator": "default", "jvm_heap_mib": None, "thread_census_idle": {}, "csg_setting": False},
        },
        "config_files": [],
        "bot": {"module": "loadbot", "protocol": 2193, "sha256": None, "argv": []},
        "workload": WORKLOAD_6.record(),
        "machine": {
            "fingerprint_id": "synthetic",
            "label": "synthetic",
            "os": "Linux",
            "kernel": None,
            "cpu": "synthetic 8-core",
            "logical": 8,
            "physical": 8,
            "core_classes": {"performance": list(range(8)), "efficiency": []},
            "ram_gib": 16,
            "virtualized": False,
            "power": None,
            "tsc_hz_calibrated": None,
            "profile_sha256": "2" * 64,
        },
        "placement": {
            "server_cpus": list(SERVER_CPUS),
            "bot_cpus": list(BOT_CPUS),
            "harness_cpus": list(BOT_CPUS),
            "priority": "normal",
            "kind": "physical",
            "method": "preexec",
            "verified": True,
        },
        "time": {
            "origin_mono_ns": ORIGIN_NS,
            "origin_epoch_ms": v.epoch_ms,
            "end_mono_ns": _ns(MARKS["stop"] + 1),
            "end_epoch_ms": v.epoch_ms + round((MARKS["stop"] + 1) * 1000),
            "drift_ms": 0.0,
        },
        "world": {"fresh": True, "generator": "vanilla"},
        "marks": _marks(v),
        "settle": {"settle_s": 62.5 if ok or v.death_s > 100 else None, "capped": False, "views_incomplete": False},
        "interventions": {"nudges": 0, "nudged": [], "retp": 0, "retp_bots": [], "in_measured": 0},
        "health": {
            "died": not ok,
            "server_exit": 0 if ok else -6,
            "crash_files": [] if ok else ["crash/crash-report.txt"],
            "joined": N_BOTS,
            "failed": 0,
            "dropped": 0 if ok else N_BOTS,
            "decode_errors": 0,
        },
        "interference": {
            "idle_foreign_cores": 0.05,
            "windows": {
                w: {"foreign_cores": 0.07, "peak_10s": 0.09, "server_set": 0.01, "harness_cores": 0.08, "top": []}
                for w in (("load", "steady") if ok else ())
            },
        },
    }
    if ok:
        run["spread"] = {
            "source": "querytarget",
            "players": N_BOTS,
            "cells_occupied": N_BOTS,
            "max_per_cell": 1,
            "worst_offset_blocks": 0.5,
        }
    else:
        run["settle"] = {"settle_s": None, "capped": False, "views_incomplete": False}
        run["spread"] = {
            "source": None,
            "players": None,
            "cells_occupied": None,
            "max_per_cell": None,
            "worst_offset_blocks": None,
        }
    return run


def build(name: str) -> RunData:
    """The run ``name`` (one of NAMES) as loaded RunData."""
    v = VARIANTS[name]
    return RunData(
        run=_run_json(v),
        probe=_probe(v),
        threads=_threads(v),
        tick=_tick(v),
        process=_process(v),
        machine=_machine(v),
        bots=_bots(v),
        log=_log(v),
        commands=_commands(v),
    )


# --------------------------------------------------------------------------------------------------------------------
# Writing the raw run directory
# --------------------------------------------------------------------------------------------------------------------


def _gzip(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def _csv(header: list[str], rows: Iterable[list[Any]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else repr(v) if isinstance(v, float) else v for v in row])
    return buf.getvalue()


def _jsonl(records: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in records)


def raw_files(run: RunData) -> dict[str, bytes]:
    """The raw files of a run directory (everything except run.json and metrics.json)."""
    n_cpus = max((len(m.per_cpu_busy) for m in run.machine), default=0)
    files = {
        "server.log.gz": _gzip("".join(f"{line.t_ns}\t{line.epoch_ms}\t{line.raw}\n" for line in run.log)),
        "commands.jsonl.gz": _gzip(_jsonl(run.commands)),
        "bots.jsonl.gz": _gzip(_jsonl(run.bots)),
        "series/probe.csv": _csv(
            ["send_ns", "rx_ns", "tick"], ([p.send_ns, p.rx_ns, p.tick] for p in run.probe)
        ).encode("utf-8"),
        "series/threads.csv.gz": _gzip(
            _csv(["t_ns", "tid", "name", "cpu_ns"], ([s.t_ns, s.tid, s.name, s.cpu_ns] for s in run.threads.samples))
        ),
        "series/tick.csv.gz": _gzip(_csv(["t_ns", "cpu_ns"], ([s.t_ns, s.cpu_ns] for s in run.tick))),
        "series/process.csv.gz": _gzip(
            _csv(
                [
                    "t_ns",
                    "user_s",
                    "sys_s",
                    "resident_mib",
                    "private_mib",
                    "threads",
                    "handles",
                    "tree_user_s",
                    "tree_sys_s",
                ],
                (
                    [p.t_ns, p.cpu_user_s, p.cpu_sys_s, p.resident_mib, p.private_mib, p.threads, p.handles, None, None]
                    for p in run.process
                ),
            )
        ),
        "series/machine.csv.gz": _gzip(
            _csv(
                ["t_ns", "foreign_cores", "foreign_server_set_cores", *(f"cpu{i}" for i in range(n_cpus))],
                ([m.t_ns, m.foreign_cores, m.foreign_server_set_cores, *m.per_cpu_busy] for m in run.machine),
            )
        ),
    }
    return files


def write_run_dir(run: RunData, path: Path, metrics_doc: dict[str, Any] | None = None) -> None:
    """Write ``run`` as a run directory; ``run.json`` gets the hashes of the raw files."""
    files = raw_files(run)
    for rel, data in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    meta = dict(run.run)
    meta["files"] = {
        rel: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for rel, data in files.items()
    }
    (path / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    if metrics_doc is not None:
        from bedrock_bench.metrics import write

        write(path / "metrics.json", metrics_doc)


def _same(path: Path, data: bytes) -> bool:
    if not path.is_file():
        return False
    current = path.read_bytes()
    if path.suffix == ".gz":
        return gzip.decompress(current) == gzip.decompress(data)
    return current == data


def main(argv: list[str] | None = None) -> int:
    from bedrock_bench.metrics import compute, dumps

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare instead of writing")
    args = parser.parse_args(argv)
    stale: list[str] = []
    for name in NAMES:
        run = build(name)
        doc = compute(run)
        target = HERE / name
        if not args.check:
            write_run_dir(run, target, doc)
            print(f"wrote {name}: {doc['status']}")
            continue
        for rel, data in raw_files(run).items():
            if not _same(target / rel, data):
                stale.append(f"{name}/{rel}")
        metrics_path = target / "metrics.json"
        if not metrics_path.is_file() or metrics_path.read_text(encoding="utf-8") != dumps(doc):
            stale.append(f"{name}/metrics.json")
    if stale:
        print("out of date: " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
