import re, sys, statistics
from pathlib import Path
R = Path(sys.argv[1])
for tag in sys.argv[2:]:
    last, mx = {}, {}
    for line in (R / tag / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "BOT":
            m = re.search(r"uniq=(\d+)", line)
            if m:
                u = int(m.group(1)); last[p[2]] = u; mx[p[2]] = max(mx.get(p[2], 0), u)
    lv = sorted(last.values()); mv = sorted(mx.values())
    print(tag, "bots", len(lv), "median_last", statistics.median(lv), "median_max", statistics.median(mv),
          "min", lv[0], "max", lv[-1], "sum_last", sum(lv))
print("--- detail")
for tag in sys.argv[2:]:
    last = {}
    times = set()
    for line in (R / tag / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] == "BOT":
            m = re.search(r"uniq=(\d+)", line)
            if m:
                last[p[2]] = int(m.group(1)); times.add(p[0][:8])
    lv = sorted(last.values())
    nz = [v for v in lv if v > 0]
    print(tag, "v[49]", lv[49], "v[50]", lv[50], "nonzero n", len(nz), "median nonzero", statistics.median(nz),
          "zeros", len(lv) - len(nz), "dump times", sorted(times)[-3:], "n dumps", len(times))
