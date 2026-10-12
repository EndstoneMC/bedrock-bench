"""Loading-phase TPS minima (5 s / 15 s, with times), Streaming Pool usage, chunks delivered over time and memory.

  python -I wf_wake.py <results-root> <tag> [<tag> ...]
Times are seconds after bots_start (and after alljoined in brackets).
"""

import json
import re
import sys
from pathlib import Path

R = Path(sys.argv[1])


def tod(s):
    hh, mm, rest = s.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def minima(gt, t0, t1, span):
    pts = [(t, g) for t, g in gt if t0 <= t <= t1]
    best = None
    for i, (ta, ga) in enumerate(pts):
        for tb, gb in pts[i + 1:]:
            if tb - ta >= span - 0.5:
                v = (gb - ga) / (tb - ta)
                if best is None or v < best[0]:
                    best = (v, ta, tb)
                break
    return best


def med(v):
    v = sorted(v)
    return round(v[len(v) // 2], 2) if v else None


for tag in sys.argv[2:]:
    d = R / tag
    r = json.loads((d / "results.json").read_text())
    m, gt, W = r["marks"], r["gametime"], r["windows"]
    b0, aj = m["bots_start"], m["alljoined"]

    def rel(t):
        return f"{t - b0:.0f}s ({t - aj:+.0f}s)"

    s5 = [(ta, tb, (gb - ga) / (tb - ta)) for (ta, ga), (tb, gb) in zip(gt, gt[1:]) if tb > ta]
    print(f"== {tag}")
    for lab, a, b in (("join", "bots_start", "alljoined"), ("settle", "alljoined", "settled"),
                      ("join+settle", "bots_start", "settled"), ("steady", "unprof0", "unprof1")):
        v = [(ta, tb, x) for ta, tb, x in s5 if m[a] <= tb <= m[b]]
        lo = min(v, key=lambda x: x[2]) if v else None
        m15 = minima(gt, m[a], m[b], 15)
        print(f"  TPS {lab:12s} median {med([x for *_, x in v])}  "
              f"min5s {lo and round(lo[2], 2)} at {lo and rel(lo[1])}  "
              f"min15s {m15 and round(m15[0], 2)} over {m15 and rel(m15[1])}..{m15 and rel(m15[2])}  n={len(v)}")
    low = [(round(tb - b0), round(x, 1)) for ta, tb, x in s5 if b0 <= tb <= m["settled"] and x < 19.5]
    print(f"  5 s samples < 19.5 while loading (s after bots_start, TPS): {low}")
    for win in ("storm_prof", "storm_full", "steady_unprof"):
        w = W.get(win)
        if not w or not w.get("threads"):
            continue
        th = {x["name"]: x["ms_per_s"] for x in w["threads"]["threads"]}
        pools = [round(th.get(f"Streaming Pool({i})", 0)) for i in range(7)]
        print(f"  {win:14s} pools {pools} sum {sum(pools)} busy>=500 {sum(p >= 500 for p in pools)} "
              f">=100 {sum(p >= 100 for p in pools)}  MC_SERVER {round(th.get('MC_SERVER', 0))}  "
              f"REST {round(th.get('REST Thread(0)', 0))}  mem {w.get('mem_mb')}")
    # total unique chunks across bots at each BOT dump, by time after bots_start (wall clock from bds/bots logs)
    lines = (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines()
    first = next((tod(l.split()[0]) for l in lines if " JOINED " in l), None)
    dumps = {}
    for l in lines:
        p = l.split()
        if len(p) >= 3 and p[1] == "BOT":
            u = re.search(r"uniq=(\d+)", l)
            if u:
                key = p[0][:8]
                dumps.setdefault(key, {})[p[2]] = int(u.group(1))
    out = []
    for k in sorted(dumps):
        tot = sum(dumps[k].values())
        out.append(f"{tod(k) - first:.0f}s:{tot}")
    print(f"  uniq total at BOT dumps (s after first join): {' '.join(out[:6])}")
    stats = []
    for l in lines:
        p = l.split()
        if len(p) >= 2 and p[1] == "STAT":
            u = re.search(r"uniq=(\d+)", l)
            if u:
                stats.append((tod(p[0]) - first, int(u.group(1))))
    marks = []
    for s in (30, 45, 60, 75, 90, 120, 180):
        before = [u for t, u in stats if t <= s + 0.5]
        marks.append(f"{s}s:{before[-1] if before else 0}")
    print(f"  uniq total from STAT lines (s after first join): {' '.join(marks)}")
    log = (d / "bds.log").read_text(encoding="utf-8", errors="replace")
    print(f"  'Unexpected async worker pool' lines: {log.count('Unexpected async worker pool')}")
