"""Chaos-window TPS by randomtickspeed regime, plus cores, memory and error kinds, for results dirs."""
import csv, json, re, statistics, sys
from collections import Counter
from pathlib import Path

ERR = re.compile(r"\b(ERROR|CRITICAL)\b|exception|assert|crash", re.I)
N = {0, 1, 3}


def tod(s):
    hh, mm, rest = s.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def med(v):
    return [round(statistics.median(v), 2), len(v)] if v else None


def analyze(d):
    d = Path(d)
    r = json.loads((d / "results.json").read_text())
    m = r["marks"]
    tsc = r.get("tsc_hz", 2.1e9)
    lines = (d / "bds.log").read_text(encoding="utf-8", errors="replace").splitlines()
    g, ex = [], []
    for line in lines:
        mm = re.search(r"(?i)gametime is (\d+)", line)
        if mm:
            g.append((tod(line.split()[0]), int(mm.group(1))))
        mm = re.search(r"randomtickspeed has been updated to (\d+)", line)
        if mm:
            ex.append((tod(line.split()[0]), int(mm.group(1))))
    q = []
    for line in (d / "chaos.log").read_text(encoding="utf-8", errors="replace").splitlines():
        mm = re.match(r"(\d\d:\d\d:\d\d) CHAOS randomtickspeed (\d+)", line)
        if mm:
            q.append((tod(mm.group(1)), int(mm.group(2))))
    wall_of = {}
    for t, gg in g:
        wall_of.setdefault(gg, t)
    off = statistics.median(wall_of[gg] - t for t, gg in r["gametime"] if gg in wall_of)

    def reg(changes, w):
        v = 1
        for t, x in changes:
            if t <= w:
                v = x
        return v

    c0 = m["chaos0"]
    c1 = m.get("chaos1") or (r["died_at"] and None) or max(t for t, _ in r["gametime"])
    gt = [(t + off, gg) for t, gg in r["gametime"] if c0 <= t <= c1]
    s5 = [(ta, tb, (gb - ga) / (tb - ta)) for (ta, ga), (tb, gb) in zip(gt, gt[1:]) if tb > ta]
    mon = list(csv.DictReader(open(d / "chaos-mon.csv")))
    w0 = c0 + off
    mrows = [(w0 + float(x["t_s"]) - 12, w0 + float(x["t_s"]), float(x["tps10"])) for x in mon
             if x["tps10"] not in ("None", "")]
    out = {"tag": d.name, "chaos_s": round(c1 - c0), "tps5_all": med([v for *_, v in s5]),
           "tps5_min": round(min(v for *_, v in s5), 2) if s5 else None, "mon_tps10_all": med([v for *_, v in mrows])}
    for lab, ch in (("executed", ex), ("queued", q)):
        out[f"tps5_normal_{lab}"] = med([v for a, b, v in s5 if reg(ch, b) in N])
        out[f"tps5_100_1000_{lab}"] = med([v for a, b, v in s5 if reg(ch, b) not in N])
        out[f"mon_normal_{lab}"] = med([v for a, b, v in mrows if reg(ch, b) in N])
        out[f"mon_100_1000_{lab}"] = med([v for a, b, v in mrows if reg(ch, b) not in N])
        out[f"tps5_per_regime_{lab}"] = {k: med([v for a, b, v in s5 if reg(ch, b) == k]) for k in (0, 1, 3, 100, 1000)}
    out["tickspeed_changes_executed"] = len(ex)
    out["tickspeed_changes_queued"] = len(q)
    out["time_in_1000_executed_s"] = round(sum(
        (ex[i + 1][0] if i + 1 < len(ex) else c1 + off) - t for i, (t, x) in enumerate(ex) if x == 1000))
    pts = [(t, s) for t, s in r["thread_series"] if c0 <= t <= c1]
    if len(pts) >= 2:
        (ta, a), (tb, b) = pts[0], pts[-1]
        out["proc_cores_chaos"] = round(sum(b[k][0] - a.get(k, [0, 0])[0] for k in b) / tsc / (tb - ta), 2)
        dt = r.get("devtools_tid")
        if dt and str(dt) in b:
            out["proc_cores_chaos_no_devtools"] = round(
                sum(b[k][0] - a.get(k, [0, 0])[0] for k in b if k != str(dt)) / tsc / (tb - ta), 2)
    pv = [float(x["private_mb"]) for x in mon if x["private_mb"] not in ("None", "")]
    ws = [float(x["ws_mb"]) for x in mon if x["ws_mb"] not in ("None", "")]
    hd = [int(x["handles"]) for x in mon if x["handles"] not in ("None", "")]
    th = [int(x["threads"]) for x in mon if x["threads"] not in ("None", "")]
    if pv:
        out["private_mb_start_end_peak"] = [pv[0], pv[-1], max(pv)]
        out["ws_mb_start_end_peak"] = [ws[0], ws[-1], max(ws)]
        out["handles_start_end"] = [hd[0], hd[-1]]
        out["threads_start_end"] = [th[0], th[-1]]
    w = r["windows"].get("chaos")
    out["window_chaos_mem_mb"] = w and w.get("mem_mb")
    out["window_chaos_main_ms_s"] = w and next((round(x["ms_per_s"]) for x in w["threads"]["threads"]
                                                if x["name"] == "MC_SERVER"), None) if w and w.get("threads") else None
    s = json.loads((d / "chaos-summary.json").read_text()) if (d / "chaos-summary.json").exists() else {}
    out["crash"] = bool(s.get("crash")) or bool(r.get("died_during_run"))
    out["server_exit"] = r.get("server_exit")
    out["crash_files"] = r.get("crash_files")
    out["summary_error_lines"] = s.get("error_lines")
    out["summary_cmd_feedback"] = s.get("command_feedback_errors")
    out["harness_errors"] = s.get("harness_errors")
    out["not_respawned"] = s.get("not_respawned")
    out["actions"] = s.get("actions")
    out["bot_events"] = s.get("bot_events")
    kinds = Counter()
    for line in lines:
        if not ERR.search(line) or "[verify" in line:
            continue
        try:
            t = tod(line.split()[0])
        except Exception:
            continue
        if not (c0 + off <= t <= c1 + off + 5):
            continue
        body = re.sub(r"^\S+ \[[^\]]*\]:? ?", "", line).strip()
        key = re.sub(r"\d+", "#", body)
        key = re.sub(r"'[^']*'", "'…'", key)
        kinds[key[:110]] += 1
    out["error_total_chaos"] = sum(kinds.values())
    out["error_kinds_chaos"] = kinds.most_common(30)
    other = []
    for line in lines:
        if ERR.search(line) and "[verify" not in line:
            try:
                t = tod(line.split()[0])
            except Exception:
                continue
            if t < c0 + off or t > c1 + off + 5:
                other.append(line[:200])
    out["error_lines_outside_chaos"] = len(other)
    out["error_lines_outside_chaos_first"] = other[:12]
    return out


if __name__ == "__main__":
    for a in sys.argv[1:]:
        print(json.dumps(analyze(a), indent=1, ensure_ascii=False))
