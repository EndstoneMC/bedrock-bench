"""In-server integration tests for run_endstone.py --intest: the endstone_test plugin runs its on_enable suite at
startup and its on_player_join suite for every join; a00 is opped and sends /test probes as a player."""

import json
import re
import time
from pathlib import Path

PREFIX = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} (\[\d\d:\d\d:\d\d \w+\]: ?)?")
SESSION = re.compile(r"=+ test session starts =+")
RESULT = re.compile(r"=+ (.*?\b\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?|"
                    r"no tests ran)\b.*?) in [\d.]+s(?: \([^)]*\))? =+")
FAILLINE = re.compile(r"^(FAILED|ERROR) \S+")
NAME = re.compile(r"\bName: (\S+)")


def parse_pytest(lines):
    """Pytest sessions in order: suite label, player, summary, FAILED/ERROR lines, and the FAILURES text."""
    sessions, cur, player, joins, in_fail = [], None, None, 0, False
    for raw in lines:
        line = PREFIX.sub("", raw.rstrip())
        m = NAME.search(line)
        if m:
            player = m.group(1)
            joins += 1
        if SESSION.search(line):
            cur = {"suite": "on_player_join" if joins else "on_enable", "player": player if joins else None,
                   "summary": None, "failed": [], "failures_text": []}
            sessions.append(cur)
            in_fail = False
            continue
        if cur is None:
            continue
        if re.search(r"=+ (FAILURES|ERRORS) =+", line):
            in_fail = True
        r = RESULT.search(line)
        if r:
            cur["summary"] = r.group(1)
            in_fail = False
            cur = None
            continue
        if FAILLINE.match(line):
            cur["failed"].append(line)
        if in_fail:
            cur["failures_text"].append(line)
    return sessions


def counts(summary):
    return {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)",
                                             summary or "")}


def report(out, srv_lines, bot_log, log):
    sessions = parse_pytest(srv_lines)
    probes = [l.rstrip() for l in Path(bot_log).read_text(encoding="utf-8", errors="replace").splitlines()
              if " CMD " in l or " CMDOUT " in l or " TEXT " in l]
    plugin = [PREFIX.sub("", l.rstrip()) for l in srv_lines
              if "[Test]" in l or "endstone_test" in l or "Traceback" in l or "Hello from" in l][:40]
    total = {}
    for s in sessions:
        for k, v in counts(s["summary"]).items():
            total[k] = total.get(k, 0) + v
    res = {"sessions": [{k: v for k, v in s.items() if k != "failures_text"} for s in sessions],
           "total": total, "probes": probes, "plugin_lines": plugin,
           "verdict": ("NO TESTS RAN" if not sessions else
                       "FAIL" if total.get("failed") or total.get("error") or total.get("errors")
                       or any(s["summary"] is None for s in sessions) else "PASS")}
    (Path(out) / "intest.json").write_text(json.dumps(res, indent=1))
    (Path(out) / "intest-failures.txt").write_text(
        "\n\n".join(f"## {s['suite']} {s['player'] or ''}\n" + "\n".join(s["failures_text"])
                    for s in sessions if s["failures_text"]), encoding="utf-8")
    log(f"INTEST VERDICT: {res['verdict']}  total {total}")
    for s in sessions:
        log(f"  {s['suite']:15s} {s['player'] or '-':5s} {s['summary']}")
        for f in s["failed"][:20]:
            log(f"    {f[:240]}")
    for p in probes[:20]:
        log(f"  probe {p[:200]}")
    return res


def run(srv, b, out, log, nap, wait_event, wait_s):
    """Join, op a00, let the join suite and the /test probes run, rejoin a00 as op, repeat, then report."""
    if not wait_event(b.allj, 120):
        raise RuntimeError("bots did not join")
    log("intest: bots joined; waiting for the on_player_join suites and /test probes")
    nap(wait_s)
    log("intest: a00 rejoins as op")
    b.send("reconnect a00")
    nap(wait_s)
    b.stop()
    time.sleep(2)
    with srv.lock:
        lines = [l for _, l in srv.lines]
    return report(out, lines, Path(out) / "bots-a.log", log)
