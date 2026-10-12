import json, sys
from pathlib import Path
R = Path(sys.argv[1])
TSC = 2100000000.0
def cores(r, t0, t1, exclude=()):
    pts = [(t, s) for t, s in r["thread_series"] if t0 <= t <= t1]
    (ta, a), (tb, b) = pts[0], pts[-1]
    wall = tb - ta
    cyc = sum(b[k][0] - a.get(k, [0, 0])[0] for k in b if k not in exclude)
    sec = sum(b[k][1] - a.get(k, [0, 0])[1] for k in b if k not in exclude)
    gone = [k for k in a if k not in b]
    return cyc / TSC / wall, sec / wall, wall, len(b), len(gone)
for tag in sys.argv[2:]:
    r = json.load(open(R / tag / "results.json"))
    m = r["marks"]
    tsc = r.get("tsc_hz", TSC)
    for win in ("steady_unprof", "storm_full", "chaos"):
        w = r["windows"].get(win)
        if not w:
            continue
        c, s, wall, n, gone = cores(r, w["t0"], w["t1"])
        dt = r.get("devtools_tid")
        extra = ""
        if dt:
            c2, s2, *_ = cores(r, w["t0"], w["t1"], exclude=(str(dt),))
            extra = f" | without DevTools: {c2:.2f} cyc, {s2:.2f} times"
        print(f"{tag:16s} {win:14s} cores(cycles)={c:.2f} cores(thread times)={s:.2f} wall={wall:.0f}s threads={n} exited={gone} mem={w.get('mem_mb')}{extra}")
