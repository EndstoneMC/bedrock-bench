"""Report metrics from linux-bench results dirs, the same way for every arm.

  uv run --no-project python -I metrics_linux.py <results-dir> [...]

Perf dirs (result.json): TPS median of the 5 s probe samples and the lowest 15 s window, for the steady window and for
loading (bots_start -> settled); MC_SERVER ms/s and ms/tick over the steady window; view load time per bot (first TP
to the first BOT dump holding 90% of its final unique chunks); final unique chunks per bot; RSS.
Chaos dirs (chaos.json): TPS median of the 10 s monitor samples overall and by the randomtickspeed in effect at the
start of each sample window, crash, RSS, and the [verify] summaries in server.log.
"""

import importlib.util
import json
import re
import statistics
import sys
import time
from pathlib import Path

B = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("bench_metrics", B.parent / "endstone-bench" / "bench_metrics.py")
bench_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_metrics)


def tod(s):
    hh, mm, rest = s.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))] if v else None


def r2(x):
    return None if x is None else round(x, 2)


def tps_samples(gt, t0, t1):
    pts = [(t, g) for t, g in gt if t0 <= t <= t1]
    return [(g1 - g0) / (u1 - u0) for (u0, g0), (u1, g1) in zip(pts, pts[1:]) if u1 > u0]


def tps_min_window(gt, t0, t1, span=15.0):
    lows, a = [], t0
    while a + span <= t1 + 0.5:
        pts = [(t, g) for t, g in gt if a <= t <= a + span]
        if len(pts) >= 2 and pts[-1][0] > pts[0][0]:
            lows.append((pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]))
        a += span
    return min(lows) if lows else None


def view_load(bots_log):
    tp, rows = {}, {}
    for line in Path(bots_log).read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        if p[1] == "TP" and p[2] not in tp:
            tp[p[2]] = tod(p[0])
        elif p[1] == "BOT":
            m = re.search(r"uniq=(\d+)", line)
            if m:
                rows.setdefault(p[2], []).append((tod(p[0]), int(m.group(1))))
    t90, finals = [], []
    for n, rs in rows.items():
        final = rs[-1][1]
        finals.append(final)
        if n not in tp or final <= 0:
            continue
        hit = next((t for t, u in rs if u >= 0.9 * final), None)
        if hit is not None:
            t90.append(hit - tp[n])
    return {"t90_s": {"median": pct(t90, 0.5), "p90": pct(t90, 0.9), "max": pct(t90, 1), "n": len(t90)},
            "final_uniq_per_bot": {"median": pct(finals, 0.5), "min": pct(finals, 0), "mean": r2(
                statistics.fmean(finals)) if finals else None, "n": len(finals)}}


def join_times(d):
    """Seconds from "Player connected" (server.log, host clock) to the bot's first JOINED (bots.log, UTC)."""
    off, conn = None, {}
    for line in (d / "server.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        if off is None and (m := re.match(r"\[(?:\d{4}-\d\d-\d\d )?(\d\d:\d\d:\d\d)", p[1] + " " + p[2])):
            off = round((tod(p[0]) - tod(m.group(1))) / 3600) * 3600
        if (m := re.search(r"Player connected: ([^,\s]+)", line)) and m.group(1) not in conn:
            conn[m.group(1)] = tod(p[0])
    joined = {}
    for line in (d / "bots.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "JOINED" and p[2] not in joined:
            joined[p[2]] = tod(p[0])
    v = [joined[n] - (conn[n] - (off or 0)) for n in joined if n in conn]
    return {"median": r2(pct(v, 0.5)), "p90": r2(pct(v, 0.9)), "max": r2(pct(v, 1)), "n": len(v)}


def workers(w):
    """Unnamed BDS worker threads (they inherit "Main" under Endstone, "bedrock_server" in vanilla) by ms/s."""
    tids = (w or {}).get("tids_ms_s")
    if not tids:
        agg = [x for x in (w or {}).get("threads_ms_s", []) if x.startswith(("Mainx", "bedrock_serverx"))]
        return {"aggregate": agg}
    ms = sorted((v[1] for v in tids.values() if v[0] in ("Main", "bedrock_server")), reverse=True)
    return {"per_thread_ms_s": ms, "busy_over_100": sum(m > 100 for m in ms), "sum_ms_s": sum(ms)}


def log_scan(d):
    errs, pool = [], []
    for line in (d / "server.log").read_text(encoding="utf-8", errors="replace").splitlines():
        if "Unexpected async worker pool" in line:
            pool.append(line)
        if re.search(r"ERROR\]|\[ERROR|CRITICAL|Traceback", line):
            errs.append(line)
    return {"log_error_lines": len(errs), "log_errors_first": errs[:5], "worker_pool_warnings": len(pool)}


def perf(d):
    r = json.loads((d / "result.json").read_text())
    m, gt = r["marks"], r.get("gametime") or []
    st, sf = r["windows"].get("steady", {}), r["windows"].get("storm_full", {})
    steady = tps_samples(gt, m["steady0"], m["steady1"]) if "steady1" in m else []
    load = tps_samples(gt, m["bots_start"], m["settled"]) if "settled" in m else []
    vl = view_load(d / "bots.log")
    rss_series = [row["rss_mb"] for row in r.get("settle_series", []) if row.get("rss_mb")]
    return {
        "tag": d.name, "vanilla": r.get("vanilla", False), "version": r.get("endstone") or r.get("bds_version"),
        "wheel": r.get("wheel"), "server_cpus": r.get("server", {}).get("Cpus_allowed_list"),
        "bot_cpus": r.get("bot_cpus_allowed"), "foreign": r.get("foreign_at_start"),
        "bots": r.get("bots"), "spread": r.get("spread"), "settle_s": r.get("settle_s"),
        "settle_capped": r.get("settle_capped", False),
        "steady_tps_median": r2(statistics.median(steady)) if steady else None,
        "steady_tps_min15": r2(tps_min_window(gt, m["steady0"], m["steady1"])) if steady else None,
        "steady_tps_window": r2(st.get("tps")),
        "load_tps_median": r2(statistics.median(load)) if load else None,
        "load_tps_min15": r2(tps_min_window(gt, m["bots_start"], m["settled"])) if load else None,
        "load_tps_window": r2(sf.get("tps")),
        "mc_ms_s": st.get("mc_server_ms_s"), "mc_ms_tick": st.get("mc_server_ms_tick"),
        "storm_mc_ms_s": sf.get("mc_server_ms_s"),
        "steady_proc_cores": st.get("proc_cores"), "load_proc_cores": sf.get("proc_cores"),
        "load_mc_ms_tick": sf.get("mc_server_ms_tick"), "join_s": join_times(d),
        "load_workers": workers(sf), "steady_workers": workers(st),
        **log_scan(d),
        "steady_threads_ms_s": st.get("threads_ms_s"),
        "rss_steady_mb": st.get("rss_mb"), "rss_settle_peak_mb": max(rss_series, default=None),
        "view_load_s": vl["t90_s"], "chunks_per_bot": vl["final_uniq_per_bot"],
        "uniq_total": (r.get("bots") or {}).get("uniq_total"),
        "server_exit": r.get("server_exit"), "completed": "steady" in r["windows"],
        "crashed": not ("steady" in r["windows"] and r.get("server_exit") == 0),
    }


NORMAL = {0, 1, 3}


def regime(changes, t):
    v = 1
    for u, x in changes:
        if u <= t:
            v = x
    return v


def med(v):
    return r2(statistics.median(v)) if v else None


def chaos(d):
    """TPS by the randomtickspeed the server applied (its "Game rule randomtickspeed has been updated" line), each
    sample labelled by the regime at its end, like wf_chaos.py. Queued times are wrong because Endstone drops console
    commands when BDS's queue is full."""
    r = json.loads((d / "chaos.json").read_text())
    smp = r.get("samples", [])
    su = r.get("summary", {})
    log_lines = (d / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()
    ex = [(tod(ln.split()[0]), int(m.group(1))) for ln in log_lines
          if (m := re.search(r"randomtickspeed has been updated to (\d+)", ln))]
    if r.get("chaos_t0_wall"):
        lt = time.localtime(r["chaos_t0_wall"])
        t0 = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + r["chaos_t0_wall"] % 1
    else:
        t0 = next(tod(ln.split()[0]) for ln in (d / "bench.out").read_text().splitlines() if " chaos start " in ln)
    t1 = t0 + (su.get("chaos_s") or r.get("chaos_s") or 0)
    queued = [(tod(p[0]), int(p[2])) for p in (ln.split() for ln in (d / "chaos_events.log").read_text().splitlines())
              if len(p) >= 3 and p[1] == "randomtickspeed"]
    mon = [(t0 + s["t"], s["tps"]) for s in smp if s["tps"] is not None]
    out = {"mon10_all": med([v for _, v in mon]),
           "mon10_normal": med([v for t, v in mon if regime(ex, t) in NORMAL]),
           "mon10_100_1000": med([v for t, v in mon if regime(ex, t) not in NORMAL]),
           "mon10_per_regime": {k: med([v for t, v in mon if regime(ex, t) == k]) for k in (0, 1, 3, 100, 1000)},
           "mon10_n": {k: len([1 for t, _ in mon if regime(ex, t) == k]) for k in (0, 1, 3, 100, 1000)}}
    if r.get("gametime_wall"):
        gt = []
        for w, g in r["gametime_wall"]:
            lt = time.localtime(w)
            gt.append((lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + w % 1, g))
        gt = [p for p in gt if t0 <= p[0] <= t1]
        s5, i = [], 0
        while i < len(gt):
            j = next((k for k in range(i + 1, len(gt)) if gt[k][0] - gt[i][0] >= 5), None)
            if j is None:
                break
            s5.append((gt[j][0], (gt[j][1] - gt[i][1]) / (gt[j][0] - gt[i][0])))
            i = j
        out.update({"tps5_all": med([v for _, v in s5]),
                    "tps5_normal": med([v for t, v in s5 if regime(ex, t) in NORMAL]),
                    "tps5_100_1000": med([v for t, v in s5 if regime(ex, t) not in NORMAL]),
                    "tps5_min": r2(min(v for _, v in s5)) if s5 else None})
    inwin = [(t, x) for t, x in ex if t0 <= t <= t1]
    bounds = [(t0, regime(ex, t0))] + inwin
    secs = {}
    for k, (t, x) in enumerate(bounds):
        e = bounds[k + 1][0] if k + 1 < len(bounds) else t1
        secs[x] = secs.get(x, 0) + e - t
    out["seconds_per_regime"] = {k: round(v) for k, v in sorted(secs.items())}
    out["tickspeed_changes"] = {"queued": len([q for q in queued if t0 <= q[0] <= t1]), "applied": len(inwin)}
    cores = [s["cores"] for s in smp if s.get("cores") is not None]
    out["proc_cores_chaos_median"] = med(cores)
    vs = bench_metrics.VerifyScan()
    with open(d / "server.log", encoding="utf-8", errors="replace") as f:
        for line in f:
            vs.feed(line)
    wp = {}
    for line in open(d / "server.log", encoding="utf-8", errors="replace"):
        for pat in (r"Game rule (playerwaypoints|locatorbar) has been updated to (\S+)",
                    r"Syntax error: Unexpected \"[^\"]*\": at \"(waypoints|locatorbar) >>([^<]*)<<"):
            m = re.search(pat, line)
            if m:
                key = ("ok " if line.find("Game rule") >= 0 else "err ") + m.group(1) + " " + m.group(2)
                wp[key] = wp.get(key, 0) + 1
    return {
        "tag": d.name, "vanilla": r.get("vanilla", False), "version": r.get("endstone") or r.get("bds_version"),
        "wheel": r.get("wheel"), "chaos_s": su.get("chaos_s"), "crash": su.get("crash"),
        "server_exit": su.get("server_exit"), "crash_report": r.get("crash_report"),
        **out,
        "tps_min": su.get("tps_min"), "rss_start_mb": su.get("rss_start_mb"), "rss_end_mb": su.get("rss_end_mb"),
        "rss_peak_mb": max((s["rss_mb"] for s in smp if s.get("rss_mb")), default=None),
        "threads": (su.get("threads_start"), su.get("threads_end")), "severe_lines": su.get("severe_lines"),
        "error_lines": su.get("error_lines"), "cmd_errors": su.get("cmd_errors"),
        "actions": r.get("actions"), "bot_events": r.get("bot_events"),
        "waypoint_gamerule_responses": wp, "verify": vs.verdict(),
    }


def main():
    out = []
    for a in sys.argv[1:]:
        d = Path(a)
        if not d.is_absolute():
            d = B / "results" / a
        s = chaos(d) if (d / "chaos.json").exists() else perf(d)
        out.append(s)
        dest = B / "results" / "_metrics"
        dest.mkdir(exist_ok=True)
        (dest / f"{d.name}.json").write_text(json.dumps(s, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
