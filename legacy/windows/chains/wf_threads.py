import json, sys
from pathlib import Path
R = Path(sys.argv[1])
for tag in sys.argv[2:]:
    r = json.load(open(R / tag / "results.json"))
    names = r.get("thread_names", {})
    for win in ("storm_prof", "storm_full", "steady_unprof"):
        w = r["windows"].get(win)
        if not w or not w.get("threads"):
            continue
        rows = w["threads"]["threads"]
        tot = sum(x["ms_per_s"] for x in rows)
        print(f"{tag} {win} tps={w['tps']:.2f} total={tot:.0f} ms/s")
        for x in rows[:14]:
            print(f"    {x['ms_per_s']:7.1f}  {x['name'] or x['tid']}")
