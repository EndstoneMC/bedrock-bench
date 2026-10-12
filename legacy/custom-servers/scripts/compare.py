"""Comparison table: Endstone reference vs each third-party server run (summary.json from csummary.py).

  python -I compare.py <tag> [<tag> ...]   (tags under custom-bench/results)
"""

import json
import sys
from pathlib import Path

CB = Path(__file__).resolve().parent.parent
RES = CB / "results"


def f(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def col_endstone():
    r = json.loads((RES / "endstone-ref.json").read_text())
    c = r["chaos_first5"]
    m = r["memory_chaos_run"]
    return {
        "name": "Endstone 0.11.14 (BDS 1.26.52)",
        "steady TPS median / min": f"{f(r['tps']['steady']['median'])} / {f(r['tps']['steady']['min'])}",
        "loading TPS median / min (join+settle)": f"{f(r['tps']['load']['median'])} / {f(r['tps']['load']['min'])}",
        "join-storm TPS median / min": f"{f(r['tps']['join']['median'])} / {f(r['tps']['join']['min'])}",
        "main thread ms/s (steady)": f"{r['main_ms_s_steady']} (~{r['main_ms_s_steady'] / 20:.0f} ms/tick)",
        "process CPU cores (steady / loading)": f"{r['process_cores_steady']} / {r['process_cores_load']}",
        "memory (working set / private MB)": f"{m['ws_mb_at_chaos_start']:.0f} / {m['private_mb_at_chaos_start']:.0f} (chaos-run start)",
        "join time median (p90)": f"{f(r['join_s']['median'])} s ({f(r['join_s']['p90'])})",
        "time to 90% of view median (p90)": f"{f(r['t90_after_tp_s']['median'], 0)} s ({f(r['t90_after_tp_s']['p90'], 0)})",
        "unique chunks per bot (median)": f"{r['final_uniq_per_bot']['median']} (client-side generation on)",
        "settle": f"{r['settle_s']} s",
        "crash / error lines": f"no / {r['log_errors']}",
        "chaos (first 5 min) TPS median / min": f"{f(c['tps_median'])} / {f(c['tps_min'])}",
        "chaos outcome": f"no crash; main {c['mc_ms_s_median']:.0f} ms/s; {c['errors_at_5min']} error lines",
    }


def col(tag):
    d = RES / tag
    s = json.loads((d / "summary.json").read_text())
    r = json.loads((d / "results.json").read_text())
    p = s.get("proc", {})
    st = p.get("steady") or {}
    ld = p.get("storm_full") or {}
    ch = s.get("chaos") or {}
    main = s.get("main_ms_s", {})
    tps = s["tps"]

    def t(k):
        v = tps.get(k)
        return f"{f(v['median'])} / {f(v['min'])}" if v and v.get("n") else "-"

    crash = "CRASH" if s.get("died_during_run") else "no"
    chaos_txt = "-"
    if ch:
        nr = ch.get("not_respawned") or []
        chaos_txt = (f"{'CRASH' if ch.get('crash') else 'no crash'}; main {f(ch.get('mc_ms_s_median'), 0)} ms/s; "
                     f"{ch.get('error_lines')} error lines; {len(nr)} killed bots not respawned; "
                     f"skipped: {', '.join(ch.get('skipped_actions') or []) or 'none'}")
    return {
        "name": f"{s['server']} ({tag})",
        "steady TPS median / min": t("steady"),
        "loading TPS median / min (join+settle)": t("load"),
        "join-storm TPS median / min": t("join"),
        "main thread ms/s (steady)": ("n/a (Go: no main thread)" if s["server"] == "dragonfly" else
                                      f"{main.get('steady')} (~{main.get('steady') / tps['steady']['median']:.0f} ms/tick)"
                                      if main.get("steady") is not None and tps.get("steady", {}).get("median") else "-"),
        "process CPU cores (steady / loading)": f"{f(st.get('process_cores'))} / {f(ld.get('process_cores'))}",
        "memory (working set / private MB)": (f"{st.get('ws_mb_end')} / {st.get('private_mb_end')} (end of steady); "
                                              f"peak {s.get('ws_mb_peak')} / {s.get('private_mb_peak')}" if st else
                                              f"peak {s.get('ws_mb_peak')} / {s.get('private_mb_peak')}"),
        "join time median (p90)": f"{f(s['join_s']['median'])} s ({f(s['join_s']['p90'])})",
        "time to 90% of view median (p90)": ("view never filled" if (s["final_uniq_per_bot"]["median"] or 0) < 100 else
                                             f"{f(s['t90_after_tp_s']['median'], 0)} s ({f(s['t90_after_tp_s']['p90'], 0)})"),
        "unique chunks per bot (median)": f"{s['final_uniq_per_bot']['median']}",
        "settle": f"{s['settle_s']} s" + (" (capped)" if s.get("settle_capped") else ""),
        "crash / error lines": f"{crash} / {s['log_errors']['count']}",
        "chaos (first 5 min) TPS median / min": (f"{f(ch.get('tps_median'))} / {f(ch.get('tps_min'))}" if ch else "-"),
        "chaos outcome": chaos_txt,
    }


def main():
    cols = [col_endstone()] + [col(t) for t in sys.argv[1:]]
    rows = [k for k in cols[0] if k != "name"]
    out = ["| metric | " + " | ".join(c["name"] for c in cols) + " |",
           "|---|" + "---|" * len(cols)]
    for k in rows:
        out.append(f"| {k} | " + " | ".join(c.get(k, "-") for c in cols) + " |")
    txt = "\n".join(out)
    (RES / "comparison.md").write_text(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
