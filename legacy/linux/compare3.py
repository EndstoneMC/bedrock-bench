"""Side-by-side loading metrics for perf result dirs.

  uv run --no-project python -I compare3.py head-100 wake0-100 wake01-100
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics_linux as ml  # noqa: E402


def delivery(d):
    tp0, st = None, []
    for line in (d / "bots.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "TP" and tp0 is None:
            tp0 = ml.tod(p[0])
        if len(p) >= 2 and p[1] == "STAT" and (m := re.search(r"uniq=(\d+)", line)):
            st.append((ml.tod(p[0]), int(m.group(1))))
    final = st[-1][1]
    return {q: round(next(t for t, u in st if u >= q / 100 * final) - tp0) for q in (50, 75, 90, 95)}


def lowest5(d):
    r = json.loads((d / "result.json").read_text())
    m, gt = r["marks"], r["gametime"]
    s5 = [(g1 - g0) / (t1 - t0) for (t0, g0), (t1, g1) in zip(gt, gt[1:])
          if t1 > t0 and m["bots_start"] <= t0 <= m["settled"]]
    return round(min(s5), 2)


def rows(d):
    r = json.loads((d / "result.json").read_text())
    out = []
    for row, s in zip(r["settle_series"], r.get("settle_tids", [])):
        w = sorted((v[1] for v in s["tids_ms_s"].values() if v[0] == "Main"), reverse=True)
        out.append((row["el"], row["uniq"], row["uniq_s"], row["tps30"], row["mc_ms_s"], row["airborne"],
                    row["view_min"], sum(x > 100 for x in w), w[:8]))
    return out


def main():
    for tag in sys.argv[1:]:
        d = ml.B / "results" / tag
        x = ml.perf(d)
        rs = rows(d)
        at60 = next((r for r in rs if r[0] >= 58), None)
        print(f"== {tag}  {x['version']}")
        print(f"  delivery % -> s from first TP: {delivery(d)}   chunks at el~60: {at60 and at60[1]}  "
              f"view_min@60: {at60 and at60[6]}  airborne@60: {at60 and at60[5]}")
        vl = x["view_load_s"]
        print(f"  view load median/p90/max: {vl['median']:.0f} / {vl['p90']:.0f} / {vl['max']:.0f}")
        print(f"  loading TPS median {x['load_tps_median']}  lowest 5s {lowest5(d)}  lowest 15s {x['load_tps_min15']}"
              f"   steady {x['steady_tps_median']} / {x['steady_tps_min15']}")
        print(f"  main loading {x['storm_mc_ms_s']} ms/s {x['load_mc_ms_tick']} ms/tick   steady {x['mc_ms_s']} ms/s "
              f"{x['mc_ms_tick']} ms/tick")
        print(f"  cores loading/steady {x['load_proc_cores']} / {x['steady_proc_cores']}   RSS {x['rss_steady_mb']} MB   "
              f"errors {x['log_error_lines']}   pool warnings {x['worker_pool_warnings']}   join {x['join_s']['median']}"
              f"   bots {x['bots']['online']} cells {x['spread']['cells_occupied']} crashed {x['crashed']}")
        print(f"  workers over loading window: {x['load_workers']}")
        for r in rs:
            if r[0] > 160:
                break
            print(f"    el={r[0]:3d} uniq={r[1]:5.0f} uniq/s={r[2]:5.1f} tps30={r[3]} mc={r[4]} busy>100={r[7]} {r[8]}")


if __name__ == "__main__":
    main()
