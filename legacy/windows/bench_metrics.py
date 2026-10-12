"""TPS, join and view-load metrics from a results dir written by run_endstone.py or work-profile run_ab.py.

  python -I bench_metrics.py <results-dir> [<results-dir> ...]
"""

import json
import re
import sys
from pathlib import Path

ERR = re.compile(r"\b(ERROR|CRITICAL)\b|exception|assert|crash", re.I)


def tod(s):
    hh, mm, rest = s.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))]


def stats(v, qs=(("min", 0), ("p5", 0.05), ("median", 0.5), ("max", 1))):
    return {k: (round(pct(v, q), 2) if v else None) for k, q in qs} | {"n": len(v)}


def tps_series(gametime):
    """[(t_end, tps)] between consecutive `time query gametime` answers."""
    out = []
    for (t0, g0), (t1, g1) in zip(gametime, gametime[1:]):
        if t1 > t0:
            out.append((t1, (g1 - g0) / (t1 - t0)))
    return out


def phase_of(t, m):
    for name, a, b in (("idle", "idle0", "bots_start"), ("join", "bots_start", "alljoined"),
                       ("settle", "alljoined", "settled"), ("steady", "unprof0", "unprof1"),
                       ("steady_prof", "sprof0", "sprof1")):
        if a in m and b in m and m[a] <= t <= m[b]:
            return name
    return ""


def bot_metrics(d):
    """Per-bot login->spawn time and post-teleport time to 90% of the final unique-chunk count."""
    connected = {}
    for line in (d / "bds.log").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"Player connected: ([^,\s]+)", line)
        if m and m.group(1) not in connected:
            connected[m.group(1)] = tod(line.split()[0])
    joined, tp, bot = {}, {}, {}
    for line in (d / "bots-a.log").read_text(encoding="utf-8", errors="replace").splitlines():
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
    join_s = [joined[n] - connected[n] for n in joined if n in connected and joined[n] >= connected[n]]
    t90, at90 = [], []
    t_first = min(connected.values()) if connected else None
    for n, rows in bot.items():
        if n not in tp or not rows:
            continue
        final = rows[-1][1]
        hit = next((t for t, u in rows if u >= 0.9 * final), None)
        if hit is not None and final > 0:
            t90.append(hit - tp[n])
            at90.append(hit)
    q = (("median", 0.5), ("p90", 0.9), ("max", 1))
    return {"join_s": stats(join_s, q), "t90_after_tp_s": stats(t90, q),
            "last_bot_90pct_after_first_connect_s": round(max(at90) - t_first, 1) if at90 and t_first else None,
            "bot_dump_period_s": _period(bot)}


def _period(bot):
    for rows in bot.values():
        if len(rows) >= 2:
            return round(rows[1][0] - rows[0][0])
    return None


VERIFY_DETAIL = re.compile(r"diverge|missed send|send order|grew", re.I)
KV = re.compile(r"(\w+)=(\d+)")


def verify_hooks(line):
    """{hook: (divergences, checks)} from one cumulative [verify]/[verify-perf] summary line."""
    out = {}
    for hook in ("Dimension::forEachPlayer", "Dimension::sendPacketForEntity", "Level::getActivePlayerCount"):
        m = re.search(re.escape(hook) + r" (\d+)/(\d+)", line)
        if m and "(divergences/checks)" in line:
            out[hook] = (int(m.group(1)), int(m.group(2)))
    kv = {k: int(v) for k, v in KV.findall(line)}
    if "locator pairs:" in line:
        out["locator pairs"] = (kv.get("send_divergences", 0) + kv.get("noop_divergences", 0)
                                + kv.get("table_divergences", 0), kv.get("checked", 0))
        out["locator pairs unexpected_sends"] = (kv.get("unexpected_sends", 0), kv.get("checked", 0))
    elif "locator waypoints:" in line:
        out["locator waypoints"] = (kv.get("grew_after_skip", 0), kv.get("checked", 0))
    elif "locator waypoint update:" in line:
        out["locator waypoint update"] = (kv.get("divergences", 0), kv.get("checked", 0))
    m = re.search(r"publisher: parked checked (\d+), missed sends (\d+), sends checked (\d+), "
                  r"equal-distance reorders (\d+), order divergences (\d+)", line)
    if m:
        pc, ms, sc, tie, od = map(int, m.groups())
        out["publisher missed sends"] = (ms, pc)
        out["publisher order"] = (od, sc)
    return out


class VerifyScan:
    """Latest cumulative verify summary per hook plus counts of divergence detail lines, fed line by line."""

    def __init__(self):
        self.hooks = {}
        self.raw = {}
        self.details = {}
        self.first_details = []
        self.recent_details = []
        self.lines = 0

    def feed(self, line):
        if "[verify" not in line:
            return
        self.lines += 1
        body = line[line.index("[verify"):].rstrip()
        hooks = verify_hooks(body)
        if hooks:
            self.hooks.update(hooks)
            self.raw[re.sub(r"\d+", "#", body)] = body
        elif VERIFY_DETAIL.search(body):
            key = re.sub(r"#\d+ ", "", body)
            key = re.split(r": |[(]| for | in dimension ", key, maxsplit=1)[0].strip()
            self.details[key] = self.details.get(key, 0) + 1
            if len(self.first_details) < 10:
                self.first_details.append(body)
            self.recent_details = (self.recent_details + [body])[-10:]
        else:
            self.raw[re.sub(r"\d+", "#", body)] = body

    def verdict(self):
        div = {k: v[0] for k, v in self.hooks.items()}
        clean = not any(div.values()) and not self.details
        return {"verify_lines": self.lines, "clean": clean if self.lines else None,
                "divergences_per_hook": {k: {"divergences": d, "checks": c} for k, (d, c) in sorted(self.hooks.items())},
                "detail_lines": dict(sorted(self.details.items())), "first_details": self.first_details,
                "latest_summaries": sorted(self.raw.values())}

    def short(self):
        return " ".join(f"{k}={v[0]}/{v[1]}" for k, v in sorted(self.hooks.items())) + \
            (f" details={sum(self.details.values())}" if self.details else "")


def verify_from_log(d):
    vs = VerifyScan()
    for line in (Path(d) / "bds.log").read_text(encoding="utf-8", errors="replace").splitlines():
        vs.feed(line)
    return vs.verdict()


def log_errors(d):
    lines = [l.rstrip() for l in (d / "bds.log").read_text(encoding="utf-8", errors="replace").splitlines()
             if ERR.search(l)]
    return {"count": len(lines), "first": lines[:10]}


def summarize(d):
    d = Path(d)
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    series = tps_series(r.get("gametime") or [])
    by = {}
    for t, v in series:
        by.setdefault(phase_of(t, m), []).append(v)
    out = {"tag": d.name, "tps": {k: stats(by.get(k, [])) for k in ("join", "settle", "steady")},
           "settle_s": round(r["settle_s"], 1) if r.get("settle_s") else None}
    out.update(bot_metrics(d))
    out["server_exit"] = r.get("server_exit")
    out["died_during_run"] = r.get("died_during_run")
    out["log_errors"] = log_errors(d)
    out["verify"] = verify_from_log(d)
    return out


def write_tps_csv(d):
    d = Path(d)
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    t0 = m.get("bots_start", 0)
    with open(d / "tps.csv", "w") as f:
        f.write("t_rel_bots_start_s,phase,tps\n")
        for t, v in tps_series(r.get("gametime") or []):
            f.write(f"{t - t0:.1f},{phase_of(t, m)},{v:.3f}\n")


def show(s):
    print(f"== {s['tag']}")
    for k in ("join", "settle", "steady"):
        print(f"  tps {k:7s} {s['tps'][k]}")
    print(f"  settle_s {s['settle_s']}  join_s {s['join_s']}")
    print(f"  t90_after_tp_s {s['t90_after_tp_s']}  last_bot_90pct_after_first_connect_s "
          f"{s['last_bot_90pct_after_first_connect_s']}  (BOT lines every {s['bot_dump_period_s']} s)")
    print(f"  server_exit {s['server_exit']}  died_during_run {s['died_during_run']}  "
          f"log_errors {s['log_errors']['count']}")
    for l in s["log_errors"]["first"]:
        print(f"    {l[:240]}")
    show_verify(s["verify"])


def show_verify(v):
    if not v["verify_lines"]:
        return
    print(f"  VERIFY VERDICT: {'CLEAN' if v['clean'] else 'DIVERGED'} ({v['verify_lines']} verify lines)")
    for k, c in v["divergences_per_hook"].items():
        print(f"    {k:34s} divergences {c['divergences']:>8} / checks {c['checks']}")
    for k, n in v["detail_lines"].items():
        print(f"    detail lines: {n:>4} x {k[:160]}")
    for l in v["first_details"]:
        print(f"      {l[:240]}")


if __name__ == "__main__":
    for a in sys.argv[1:]:
        write_tps_csv(a)
        show(summarize(a))
