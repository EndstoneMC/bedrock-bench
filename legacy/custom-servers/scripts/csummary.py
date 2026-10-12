"""Summary metrics for a cbench.py results dir, in the same shape as endstone-bench/bench_metrics.py.

  python -I csummary.py <results-dir> [...]
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "endstone-bench"))
from bench_metrics import pct, stats, tps_series  # noqa: E402
from bench_metrics import tod as _tod  # noqa: E402

PHASES = (("idle", "idle0", "idle1"), ("join", "bots_start", "alljoined"), ("settle", "alljoined", "settled"),
          ("load", "bots_start", "settled"), ("steady", "unprof0", "unprof1"), ("chaos", "chaos0", "chaos1"))
ERR = re.compile(r"\b(ERROR|CRITICAL|SEVERE|FATAL)\b|Exception|panic:|crash", re.I)


def bot_metrics(d, connected_wall):
    import time as _t
    base = []

    def tod(s):
        t = _tod(s)
        if not base:
            base.append(t)
        return t + 86400 if t < base[0] - 3600 else t
    joined, tp, bot, events = {}, {}, {}, {}
    for line in (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
        if "CTL jitter on" in line:
            break
        p = line.split()
        if len(p) < 3:
            continue
        if p[1] == "JOINED" and p[2] not in joined:
            joined[p[2]] = tod(p[0])
        elif p[1] == "TP" and p[2] not in tp:
            tp[p[2]] = tod(p[0])
        elif p[1] == "BOT":
            m = re.search(r"uniq=(\d+)", line)
            if m:
                bot.setdefault(p[2], []).append((tod(p[0]), int(m.group(1))))
        elif len(p) >= 4 and p[2] in ("DIAL", "SPAWN", "READ", "REJOIN"):
            events[f"{p[2]} {p[3]}"] = events.get(f"{p[2]} {p[3]}", 0) + 1
    conn = {}
    for n, w in connected_wall.items():
        lt = _t.localtime(w)
        c = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (w % 1)
        conn[n] = c + 86400 if base and c < base[0] - 3600 else c
    join_s = [joined[n] - conn[n] for n in joined if n in conn and joined[n] >= conn[n] - 1]
    t90, finals = [], []
    for n, rows in bot.items():
        if n not in tp or not rows:
            continue
        final = rows[-1][1]
        finals.append(final)
        hit = next((t for t, u in rows if u >= 0.9 * final), None)
        if hit is not None and final > 0:
            t90.append(hit - tp[n])
    q = (("median", 0.5), ("p90", 0.9), ("max", 1))
    return {"joined": len(joined), "join_s": stats(join_s, q), "t90_after_tp_s": stats(t90, q),
            "final_uniq_per_bot": stats(finals, (("min", 0), ("median", 0.5), ("max", 1))), "bot_events": events}


def summarize(d):
    d = Path(d)
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    series = tps_series(r.get("gametime") or [])
    out = {"tag": d.name, "server": r.get("server"), "version": (r.get("version_lines") or [])[:3], "tps": {},
           "settle_s": round(r["settle_s"], 1) if r.get("settle_s") else None,
           "settle_capped": r.get("settle_capped", False)}
    for name, a, b in PHASES:
        if a in m and b in m:
            out["tps"][name] = stats([v for t, v in series if m[a] <= t <= m[b]])
    w = r.get("windows", {})
    out["main_ms_s"] = {k: (round(v["main_ms_s"]) if v.get("main_ms_s") is not None else None) for k, v in w.items()}
    out["proc"] = {k: v.get("proc") for k, v in w.items()}
    out["top_threads"] = {k: v.get("top_threads_ms_s", [])[:4] for k, v in w.items()}
    mem = r.get("mem_series") or []
    if mem:
        out["ws_mb_peak"] = round(max(x[1] for x in mem))
        out["private_mb_peak"] = round(max(x[2] for x in mem))
    out.update(bot_metrics(d, r.get("connected") or {}))
    out["died_during_run"] = r.get("died_during_run")
    out["server_exit"] = r.get("server_exit")
    out["abort"] = r.get("abort")
    log = (d / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()
    errs = [l for l in log if ERR.search(l)]
    out["log_errors"] = {"count": len(errs), "first": errs[:8]}
    c = r.get("chaos")
    if c:
        out["chaos"] = {k: c.get(k) for k in ("tps_min", "tps_p5", "tps_median", "mc_ms_s_median", "private_mb_start_end",
                                              "private_mb_per_h", "error_lines", "command_feedback_errors", "crash",
                                              "actions", "bot_events", "not_respawned", "dropped_unsupported_commands",
                                              "skipped_actions")}
    return out


def write_tps_csv(d):
    d = Path(d)
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    t0 = m.get("bots_start", 0)

    def phase(t):
        for name, a, b in PHASES:
            if name != "load" and a in m and b in m and m[a] <= t <= m[b]:
                return name
        return ""

    with open(d / "tps.csv", "w") as f:
        f.write("t_rel_bots_start_s,phase,tps\n")
        for t, v in tps_series(r.get("gametime") or []):
            f.write(f"{t - t0:.1f},{phase(t)},{v:.3f}\n")


def show(s):
    print(f"== {s['tag']} ({s['server']}) {s['version'][:2]}")
    for k, v in s["tps"].items():
        print(f"  tps {k:7s} {v}")
    print(f"  settle_s {s['settle_s']} capped={s['settle_capped']}  joined {s['joined']}  join_s {s['join_s']}")
    print(f"  t90_after_tp_s {s['t90_after_tp_s']}  final_uniq_per_bot {s['final_uniq_per_bot']}")
    print(f"  main_ms_s {s['main_ms_s']}")
    for k, v in s["proc"].items():
        print(f"  proc {k:10s} {v}")
    print(f"  ws_mb_peak {s.get('ws_mb_peak')} private_mb_peak {s.get('private_mb_peak')}")
    print(f"  bot_events {s['bot_events']}")
    print(f"  died {s['died_during_run']} exit {s['server_exit']} abort {s['abort']} log_errors {s['log_errors']['count']}")
    for l in s["log_errors"]["first"]:
        print(f"    {l[:220]}")
    if s.get("chaos"):
        print(f"  chaos {json.dumps(s['chaos'])}")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        write_tps_csv(a)
        s = summarize(a)
        (Path(a) / "summary.json").write_text(json.dumps(s, indent=1))
        show(s)
