"""Comparison metrics for run_endstone.py / run_levi.py results dirs (read-only on the dirs).

  uv run --no-project python -I levi_metrics.py --out <comparison.md> <label>=<results-dir> [...]
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "endstone-bench"))
import bench_metrics as bm  # noqa: E402

ERR = re.compile(r"\b(ERROR|CRITICAL|ERR|FTL|FATAL)\b|exception|assert", re.I)
BANNER = re.compile(r"TRANSPORT TYPE ERROR|connection type is not set to NetherNet|will not be able to connect|"
                    r"set 'transport=nethernet'|bedrock_server_how_to\.html|={20,}|(ERROR\]|ERR \[[^\]]*\])\s*$")
PHASES = (("join", "bots_start", "alljoined"), ("settle", "alljoined", "settled"), ("load", "bots_start", "settled"),
          ("steady", "unprof0", "unprof1"))


def lowest(gametime, t0, t1, span):
    """Lowest TPS over any window of `span` consecutive 5 s probe intervals inside [t0, t1]."""
    pts = [(t, g) for t, g in gametime if t0 <= t <= t1]
    vals = [(pts[i + span][1] - pts[i][1]) / (pts[i + span][0] - pts[i][0])
            for i in range(len(pts) - span) if pts[i + span][0] > pts[i][0]]
    return round(min(vals), 2) if vals else None


def thread_ms(r, window, pred):
    w = (r.get("windows") or {}).get(window) or {}
    rows = (w.get("threads") or {}).get("threads") or []
    return [(x["name"], x["ms_per_s"]) for x in rows if pred(x["name"] or "")]


def proc_cores(r, t0, t1):
    pts = [(t, s) for t, s in r["thread_series"] if t0 <= t <= t1]
    if len(pts) < 2:
        return None
    (ta, a), (tb, b) = pts[0], pts[-1]
    return round(sum(b[k][1] - a[k][1] for k in b if k in a) / (tb - ta), 2)


def summarize(d):
    d = Path(d)
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    gt = r.get("gametime") or []
    series = bm.tps_series(gt)
    out = {"dir": str(d), "tps": {}, "low5": {}, "low15": {}}
    for name, a, b in PHASES:
        if a in m and b in m:
            out["tps"][name] = bm.stats([v for t, v in series if m[a] <= t <= m[b]])
            out["low5"][name] = lowest(gt, m[a], m[b], 1)
            out["low15"][name] = lowest(gt, m[a], m[b], 3)
    mc = thread_ms(r, "steady_unprof", lambda n: n == "MC_SERVER")
    out["main_ms_s_steady"] = round(mc[0][1]) if mc else None
    mcl = thread_ms(r, "storm_full", lambda n: n == "MC_SERVER")
    out["main_ms_s_load"] = round(mcl[0][1]) if mcl else None
    st = out["tps"].get("steady", {}).get("median")
    out["ms_per_tick_steady"] = round(out["main_ms_s_steady"] / st, 1) if out["main_ms_s_steady"] and st else None
    for w in ("storm_prof", "storm_full"):
        gen = thread_ms(r, w, lambda n: n.startswith("Streaming Pool"))
        out[f"gen_{w}"] = {"threads": len(gen), "sum_ms_s": round(sum(v for _, v in gen)),
                           "busy_gt_100": sum(v > 100 for _, v in gen), "max_ms_s": round(max((v for _, v in gen), default=0))}
    llp = thread_ms(r, "storm_full", lambda n: n.startswith("ll_default_thread_pool") or n.startswith("logger"))
    out["ll_threads_storm_full_ms_s"] = round(sum(v for _, v in llp)) if llp else None
    if r.get("thread_series") and "unprof0" in m:
        out["cores_steady"] = proc_cores(r, m["unprof0"], m["unprof1"])
        out["cores_load"] = proc_cores(r, m["bots_start"], m["settled"])
    mem = ((r.get("windows") or {}).get("steady_unprof") or {}).get("mem_mb") or {}
    out["mem_steady_end"] = mem
    out["settle_s"] = round(r["settle_s"], 1) if r.get("settle_s") else None
    out.update(bm.bot_metrics(d))
    last = {}
    for line in (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) > 2 and p[1] == "BOT":
            mm = re.search(r"uniq=(\d+)", line)
            if mm:
                last[p[2]] = int(mm.group(1))
    out["final_uniq_per_bot"] = bm.stats(list(last.values()), (("min", 0), ("median", 0.5), ("max", 1)))
    out["uniq_total"] = r.get("uniq_total")
    out["cells"] = {k: (r.get("cells") or {}).get(k) for k in ("players", "cells_occupied", "max_per_cell")}
    errs = [l.rstrip() for l in (d / "bds.log").read_text(encoding="utf-8", errors="replace").splitlines()
            if ERR.search(l) and not BANNER.search(l)]
    out["log_errors"] = {"count": len(errs), "first": errs[:10]}
    worst = {}
    for w, v in (r.get("windows") or {}).items():
        for ms, name, _pid in ((v.get("other_load") or {}).get("top_others_ms_s") or []):
            if name.lower() != "python.exe" and ms > worst.get(w, (0, ""))[0]:
                worst[w] = (ms, name)
    out["foreign_top"] = worst
    out["died_during_run"] = r.get("died_during_run")
    out["server_exit"] = r.get("server_exit")
    out["version"] = (r.get("endstone_lines") or [])[:2]
    return out


def f(v, nd=2):
    if v is None:
        return "-"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def column(s):
    t = s["tps"]

    def tm(k):
        v = t.get(k)
        return f"{f(v['median'])} / {f(s['low5'].get(k))} / {f(s['low15'].get(k))}" if v and v.get("n") else "-"

    g1, g2 = s["gen_storm_prof"], s["gen_storm_full"]
    mem = s["mem_steady_end"]
    return {
        "steady TPS median / lowest 5 s / lowest 15 s": tm("steady"),
        "loading TPS (join+settle) median / lowest 5 s / lowest 15 s": tm("load"),
        "join-storm TPS median / lowest 5 s": f"{f(t['join']['median'])} / {f(s['low5']['join'])}" if t.get("join") else "-",
        "main thread ms/s steady (ms/tick)": f"{f(s['main_ms_s_steady'])} ({f(s['ms_per_tick_steady'], 1)})",
        "main thread ms/s while loading": f(s["main_ms_s_load"]),
        "view load t90 after first TP, median / p90": f"{f(s['t90_after_tp_s']['median'], 1)} s / {f(s['t90_after_tp_s']['p90'], 1)} s",
        "last bot at 90% after first connect": f"{f(s['last_bot_90pct_after_first_connect_s'], 0)} s",
        "join time median / p90": f"{f(s['join_s']['median'])} s / {f(s['join_s']['p90'])} s",
        "gen workers, first 60 s: busy (>100 ms/s) / sum ms/s": f"{g1['busy_gt_100']} of {g1['threads']} / {g1['sum_ms_s']}",
        "gen workers, whole load: sum ms/s (max thread)": f"{g2['sum_ms_s']} ({g2['max_ms_s']})",
        "process cores steady / loading": f"{f(s.get('cores_steady'))} / {f(s.get('cores_load'))}",
        "memory end of steady, private / working set (MB)": f"{f(mem.get('private'), 0)} / {f(mem.get('ws'), 0)}",
        "unique chunks per bot (median)": f(s["final_uniq_per_bot"]["median"]),
        "settle": f"{f(s['settle_s'], 0)} s",
        "crash / error lines": f"{'CRASH' if s['died_during_run'] else 'no'} / {s['log_errors']['count']}",
        "busiest foreign process, loading / steady (ms/s)": " / ".join(
            f"{s['foreign_top'][w][1]} {s['foreign_top'][w][0]}" if w in s["foreign_top"] else "-"
            for w in ("storm_full", "steady_unprof")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("runs", nargs="+")
    a = ap.parse_args()
    cols, summ = [], {}
    for spec in a.runs:
        label, d = spec.split("=", 1)
        s = summarize(d)
        summ[label] = s
        cols.append((label, column(s)))
    rows = list(cols[0][1])
    lines = ["| metric | " + " | ".join(c[0] for c in cols) + " |", "|---|" + "---|" * len(cols)]
    for k in rows:
        lines.append(f"| {k} | " + " | ".join(c[1].get(k, "-") for c in cols) + " |")
    txt = "\n".join(lines) + "\n"
    Path(a.out).write_text(txt, encoding="utf-8")
    Path(a.out).with_suffix(".json").write_text(json.dumps(summ, indent=1), encoding="utf-8")
    print(txt)
    for label, s in summ.items():
        if s["log_errors"]["count"]:
            print(f"{label}: {s['log_errors']['count']} error lines, first:")
            for l in s["log_errors"]["first"][:5]:
                print("   ", l[:220])


if __name__ == "__main__":
    main()
