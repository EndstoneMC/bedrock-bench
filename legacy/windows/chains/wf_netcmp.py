"""Compare 100-bot perf runs (run_endstone.py results dirs) side by side.

  python -I wf_netcmp.py <results-root> <tag> [<tag> ...]
"""

import json
import re
import statistics
import sys
from pathlib import Path

R = Path(sys.argv[1])
TAGS = sys.argv[2:]
sys.path.insert(0, str(R.parent))
import bench_metrics  # noqa: E402


def uniq(d):
    last = {}
    for line in (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "BOT":
            m = re.search(r"uniq=(\d+)", line)
            if m:
                last[p[2]] = int(m.group(1))
    v = sorted(last.values())
    nz = [x for x in v if x > 0]
    return v and statistics.median(v), nz and statistics.median(nz), len(v) - len(nz)


def cores(r, w):
    pts = [(t, s) for t, s in r["thread_series"] if w["t0"] <= t <= w["t1"]]
    (ta, a), (tb, b) = pts[0], pts[-1]
    return sum(b[k][1] - a.get(k, [0, 0])[1] for k in b) / (tb - ta)


def threads(w):
    return {(x["name"] or str(x["tid"])): x["ms_per_s"] for x in (w.get("threads") or {}).get("threads", [])}


def generic(name):
    return re.sub(r"^\d+@", "@", name)


rows = {}
thr = {}
for tag in TAGS:
    d = R / tag
    r = json.loads((d / "results.json").read_text())
    s = bench_metrics.summarize(d)
    W = r["windows"]
    st, sf = W.get("steady_unprof"), W.get("storm_full")
    lines = (d / "bds.log").read_text(encoding="utf-8", errors="replace").splitlines()
    bots = (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines()
    u = uniq(d)
    rows[tag] = {
        "steady TPS median / min": f"{s['tps']['steady']['median']} / {s['tps']['steady']['min']}",
        "loading TPS median / min": f"{s['tps']['settle']['median']} / {s['tps']['settle']['min']}",
        "join TPS median / min": f"{s['tps']['join']['median']} / {s['tps']['join']['min']}",
        "main thread steady ms/s": round(threads(st).get("MC_SERVER", 0)) if st else None,
        "main thread loading ms/s": round(threads(sf).get("MC_SERVER", 0)) if sf else None,
        "main ms per tick steady": round(threads(st).get("MC_SERVER", 0) / st["tps"], 1) if st and st["tps"] else None,
        "view load median / p90 / max s": f"{s['t90_after_tp_s']['median']} / {s['t90_after_tp_s']['p90']} / {s['t90_after_tp_s']['max']}",
        "join s median / p90 / max": f"{s['join_s']['median']} / {s['join_s']['p90']} / {s['join_s']['max']}",
        "chunks per bot median (non-zero) zeros": f"{u[0]} ({u[1]}) {u[2]}",
        "steady cores": round(cores(r, st), 2) if st else None,
        "loading cores": round(cores(r, sf), 2) if sf else None,
        "settle s": s["settle_s"],
        "spread": {k: r["cells"].get(k) for k in ("players", "cells_occupied", "max_per_cell", "worst_offset_blocks")},
        "mem steady MB": st and st.get("mem_mb"),
        "server exit / died": f"{r.get('server_exit')} / {r.get('died_during_run')}",
        "log error lines": s["log_errors"]["count"],
        "Player disconnected lines": sum("Player disconnected" in l for l in lines),
        "bot LEFT / DIED / dropped": f"{sum(' LEFT ' in l for l in bots)} / {sum(' DIED ' in l for l in bots)} / "
                                     f"{(re.search(r'dropped=(\d+)', ' '.join(r.get('bot_done') or [])) or [None, '?'])[1]}",
        "bot_done": (r.get("bot_done") or [""])[-1][:200],
        "retp": len(r.get("retp", [])),
        "other load steady pcore ms/s": st and st.get("other_load") and round(st["other_load"]["pcore_ms_s"]),
        "endstone line": (r.get("endstone_lines") or ["", ""])[1:2],
    }
    thr[tag] = {k: threads(W[k]) for k in ("storm_prof", "storm_full", "steady_unprof") if W.get(k)}
    rows[tag]["first errors"] = s["log_errors"]["first"][:6]

w = max(len(k) for k in next(iter(rows.values())))
print(f"{'':{w}s}  " + "  ".join(f"{t:>40s}" for t in TAGS))
for k in next(iter(rows.values())):
    print(f"{k:{w}s}  " + "  ".join(f"{json.dumps(rows[t][k]) if not isinstance(rows[t][k], str) else rows[t][k]:>40s}"
                                    for t in TAGS))
for win in ("storm_prof", "storm_full", "steady_unprof"):
    print(f"\n-- threads, {win} (ms/s)")
    names = []
    for t in TAGS:
        for n, v in sorted(thr[t].get(win, {}).items(), key=lambda x: -x[1]):
            if v >= 1 and n not in names:
                names.append(n)
    for n in names:
        print(f"   {n:32s}" + "".join(f"{thr[t].get(win, {}).get(n, float('nan')):>10.1f}" for t in TAGS))
    print("   generic sums (unnamed by start module):")
    for t in TAGS:
        agg = {}
        for n, v in thr[t].get(win, {}).items():
            if "@" in n:
                agg[generic(n)] = agg.get(generic(n), 0) + v
        print(f"     {t}: " + ", ".join(f"{k} {v:.0f}" for k, v in sorted(agg.items(), key=lambda x: -x[1]) if v >= 1))
