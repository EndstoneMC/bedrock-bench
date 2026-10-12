"""Endstone reference numbers in the cbench shape: perf run (after-100) and the first 5 min of the Windows chaos run.

  python -I endstone_ref.py
"""

import csv
import json
import re
import statistics
import sys
from pathlib import Path

CB = Path(__file__).resolve().parent.parent
R = CB.parent / "endstone-bench" / "results"
sys.path.insert(0, str(CB.parent / "endstone-bench"))
import bench_metrics as bm  # noqa: E402


def proc_cores(series, t0, t1):
    pts = [(t, s) for t, s in series if t0 <= t <= t1]
    (ta, a), (tb, b) = pts[0], pts[-1]
    return sum(b[k][1] - a[k][1] for k in b if k in a) / (tb - ta)


def main():
    d = R / "after-100"
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    series = bm.tps_series(r["gametime"])
    out = {"tps": {}}
    for name, a, b in (("join", "bots_start", "alljoined"), ("settle", "alljoined", "settled"),
                       ("load", "bots_start", "settled"), ("steady", "unprof0", "unprof1")):
        out["tps"][name] = bm.stats([v for t, v in series if m[a] <= t <= m[b]])
    ts = [(t, {k: tuple(v) for k, v in s.items()}) for t, s in r["thread_series"]]
    names = r["thread_names"]
    mc = next(k for k, v in names.items() if v == "MC_SERVER")
    for w, a, b in (("steady", "unprof0", "unprof1"), ("load", "bots_start", "settled")):
        pts = [(t, s) for t, s in ts if m[a] <= t <= m[b]]
        (ta, sa), (tb, sb) = pts[0], pts[-1]
        out[f"main_ms_s_{w}"] = round(1000 * (sb[mc][1] - sa[mc][1]) / (tb - ta))
        out[f"process_cores_{w}"] = round(proc_cores(ts, m[a], m[b]), 2)
    out["settle_s"] = round(r["settle_s"], 1)
    out.update(bm.bot_metrics(d))
    finals = []
    for line in (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) > 2 and p[1] == "BOT":
            mm = re.search(r"uniq=(\d+)", line)
            if mm:
                finals.append((p[2], int(mm.group(1))))
    last = dict(finals)
    out["final_uniq_per_bot"] = bm.stats(list(last.values()), (("min", 0), ("median", 0.5), ("max", 1)))
    out["log_errors"] = bm.log_errors(d)["count"]
    c = R / "chaos-after-win"
    rows = list(csv.DictReader(open(c / "chaos-mon.csv")))
    first = [x for x in rows if float(x["t_s"]) <= 300]
    tps = [float(x["tps10"]) for x in first if x["tps10"] not in ("", "None")]
    mcv = [float(x["mc_ms_s"]) for x in first if x["mc_ms_s"] not in ("", "None")]
    ws = [float(x["ws_mb"]) for x in rows if x["ws_mb"] not in ("", "None")]
    pv = [float(x["private_mb"]) for x in rows if x["private_mb"] not in ("", "None")]
    cs = json.loads((c / "chaos-summary.json").read_text())
    out["chaos_first5"] = {"tps_min": min(tps), "tps_median": statistics.median(tps), "mc_ms_s_median": statistics.median(mcv),
                           "errors_at_5min": int(first[-1]["errors"]), "online_min": min(int(x["online"]) for x in first),
                           "ws_mb_first_last": [float(first[0]["ws_mb"]), float(first[-1]["ws_mb"])],
                           "private_mb_first_last": [float(first[0]["private_mb"]), float(first[-1]["private_mb"])]}
    out["chaos_full"] = {k: cs.get(k) for k in ("minutes", "tps_min", "tps_median", "crash", "error_lines",
                                                "command_feedback_errors", "not_respawned")}
    out["memory_chaos_run"] = {"ws_mb_max": max(ws), "private_mb_max": max(pv), "ws_mb_at_chaos_start": ws[0],
                               "private_mb_at_chaos_start": pv[0]}
    (CB / "results" / "endstone-ref.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
