"""In-server tests on Linux, modelled on endstone-bench/intest.py and run_endstone.py --intest.

Usage: uv run --no-project python -I intest_linux.py <tag> --wheel <endstone-*.whl> [--wait 60]

The image is the bench image plus the endstone_test plugin (tests/plugin at a1b4e89bb, the pytest 8.3 fixture fix) and pytest. The
plugin runs its on_enable suite at startup and its on_player_join suite on every join. Two bots join (loadbot-linux-
intest, the Windows loadbot-chaos source built for Linux), a00 is opped on join and sends the /test probes as a player,
then a00 rejoins as op and the join suite runs again. Pytest session summaries are parsed from the server output.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402
from bench import BENCH_MOUNT, PROPS, Server, foreign, log, prepare_volumes, run  # noqa: E402

PLUGIN = bench.B / "plugin-a1b4e89bb" / "tests" / "plugin"
LOADBOT = bench.B / "loadbot-linux-intest"
PINS = ["pytest==9.1.1", "pillow==12.3.0", "babel==2.18.0"]
DOCKERFILE = """FROM python:3.14-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libcurl4 && rm -rf /var/lib/apt/lists/*
COPY endstone-*.whl /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/endstone-*.whl && rm /tmp/endstone-*.whl
COPY plugin /tmp/plugin
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/plugin {pins} && rm -rf /tmp/plugin
WORKDIR /data
ENTRYPOINT ["endstone", "-s", "/data", "-y", "--no-interactive"]
"""

PREFIX = re.compile(r"^\[\d\d:\d\d:\d\d \w+\]:? ?")
SESSION = re.compile(r"=+ test session starts =+")
RESULT = re.compile(r"=+ (.*?\b\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?|"
                    r"no tests ran)\b.*?) in [\d.]+s(?: \([^)]*\))? =+")
FAILLINE = re.compile(r"^(FAILED|ERROR) \S+")
NAME = re.compile(r"\[Test\] Name: (\S+)")


def build_image(wheel):
    ver = re.match(r"endstone-([^-]+)-", wheel.name).group(1)
    ctx = bench.B / "build" / f"intest-{ver}"
    shutil.rmtree(ctx, ignore_errors=True)
    ctx.mkdir(parents=True)
    shutil.copy2(wheel, ctx / wheel.name)
    shutil.copytree(PLUGIN, ctx / "plugin", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".ruff_cache"))
    (ctx / "Dockerfile").write_text(DOCKERFILE.format(pins=" ".join(PINS)), encoding="utf-8", newline="\n")
    tag = "endstone-bench-intest:" + re.sub(r"[^A-Za-z0-9_.-]", "_", ver)
    image_id = run(["docker", "build", "-q", "-t", tag, str(ctx)]).stdout.strip()
    return tag, image_id, ver


class IntestBots:
    """Two loadbots in the server's network namespace with a stdin control pipe; a00 is opped on every join."""

    def __init__(self, srv, image, n, cpus, extra, logpath):
        self.srv = srv
        self.name = f"{srv.name}-bots"
        self.joined = {}
        self.allj = threading.Event()
        self.lock = threading.Lock()
        self.log = open(logpath, "w", encoding="utf-8")
        self.p = subprocess.Popen(
            ["docker", "run", "-i", "--rm", "--name", self.name, "--network", f"container:{srv.name}",
             "--cpuset-cpus", cpus, "-v", f"{BENCH_MOUNT}:/bench:ro", "--entrypoint", "/bench/" + LOADBOT.name, image,
             "-addr", "127.0.0.1:19132", "-n", str(n), "-prefix", "a", "-d", "900s", "-stagger", "300ms", *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.p.stdin.reconfigure(newline="\n")
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            self.log.write(line)
            self.log.flush()
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "JOINED":
                self.joined.setdefault(parts[2], time.perf_counter())
                if parts[2] == "a00":
                    self.srv.cmd("op a00")
            elif "ALLJOINED" in line:
                self.allj.set()

    def send(self, s):
        with self.lock:
            try:
                self.p.stdin.write(s + "\n")
                self.p.stdin.flush()
            except OSError:
                pass

    def stop(self):
        run(["docker", "kill", "--signal", "INT", self.name], check=False)
        try:
            self.p.wait(30)
        except Exception:
            run(["docker", "rm", "-f", self.name], check=False)
            self.p.kill()


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


def report(out, srv_lines, bot_log):
    sessions = parse_pytest(srv_lines)
    probes = [ln.rstrip() for ln in Path(bot_log).read_text(encoding="utf-8", errors="replace").splitlines()
              if " CMD " in ln or " CMDOUT " in ln or " TEXT " in ln]
    plugin = [PREFIX.sub("", ln.rstrip()) for ln in srv_lines
              if "[Test]" in ln or "endstone_test" in ln or "Traceback" in ln or "Hello from" in ln][:40]
    total = {}
    for s in sessions:
        for k, v in counts(s["summary"]).items():
            total[k] = total.get(k, 0) + v
    res = {"sessions": [{k: v for k, v in s.items() if k != "failures_text"} for s in sessions],
           "total": total, "probes": probes, "plugin_lines": plugin,
           "verdict": ("NO TESTS RAN" if not sessions else
                       "FAIL" if total.get("failed") or total.get("error") or total.get("errors")
                       or any(s["summary"] is None for s in sessions) else "PASS")}
    (out / "intest.json").write_text(json.dumps(res, indent=1))
    (out / "intest-failures.txt").write_text(
        "\n\n".join(f"## {s['suite']} {s['player'] or ''}\n" + "\n".join(s["failures_text"])
                    for s in sessions if s["failures_text"]), encoding="utf-8")
    log(f"INTEST VERDICT: {res['verdict']}  total {total}")
    for s in sessions:
        log(f"  {s['suite']:15s} {s['player'] or '-':5s} {s['summary']}")
        for f in s["failed"][:20]:
            log(f"    {f[:240]}")
    for p in probes[:30]:
        log(f"  probe {p[:200]}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--wheel", required=True, type=Path)
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--bds", default="1.26.52.3")
    ap.add_argument("--server-cpus", default="0-15")
    ap.add_argument("--bot-cpus", default="16-27")
    ap.add_argument("--bot-args", default="-csg")
    ap.add_argument("--cmd", default="/test;/test sender;/test broadcast",
                    help="commands a00 sends as a player after it is opped")
    ap.add_argument("--wait", type=float, default=60, help="seconds per join round")
    a = ap.parse_args()
    if not LOADBOT.exists():
        raise SystemExit(f"{LOADBOT} is missing")

    wheel = a.wheel.resolve()
    out = bench.B / "results" / a.tag
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    bench.OUT = out
    name = f"es-bench-{a.tag}"
    run_vol = name
    res = {"args": {k: str(v) for k, v in vars(a).items()}, "wheel": wheel.name, "pins": PINS}

    fg = foreign(name)
    res["foreign_at_start"] = fg
    if fg:
        log("WARNING other containers/processes running:", fg)
    image, image_id, ver = build_image(wheel)
    res.update(image=image, image_id=image_id, version=ver)
    log("image", image, image_id[:19], wheel.name)
    res["pip"] = run(["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
                      "pip list 2>/dev/null | grep -i -E '^(endstone|pytest|pillow|babel|pluggy) '"],
                     check=False).stdout.split("\n")
    log("pip", res["pip"])
    run(["docker", "rm", "-f", name, f"{name}-bots"], check=False)
    prepare_volumes(image, a.bds, run_vol, dict(PROPS))

    srv = Server(name, image, run_vol, a.server_cpus, "default", out / "server.log",
                 extra_env={"PYTHONUNBUFFERED": "1"})
    bots = None
    try:
        if not srv.started.wait(600):
            raise RuntimeError("server did not start")
        res["endstone"] = srv.grep(r"This server is running Endstone version.*")
        log("server", res["endstone"])
        time.sleep(5)
        bots = IntestBots(srv, image, a.n, a.bot_cpus,
                          [*a.bot_args.split(), "-control", "-cmd", a.cmd, "-cmd-delay", "10s"], out / "bots.log")
        if not bots.allj.wait(120):
            raise RuntimeError("bots did not join")
        log("intest: bots joined; waiting for the on_player_join suites and /test probes")
        time.sleep(a.wait)
        log("intest: a00 rejoins as op")
        bots.send("reconnect a00")
        time.sleep(a.wait)
        bots.stop()
        time.sleep(2)
        with srv.lock:
            lines = list(srv.lines)
        res["intest"] = report(out, lines, out / "bots.log")
    finally:
        if bots and bots.p.poll() is None:
            bots.stop()
        srv.stop()
        res["server_exit"] = srv.p.returncode
        log("server exit", srv.p.returncode)
        run(["docker", "rm", "-f", name, f"{name}-bots"], check=False)
        run(["docker", "volume", "rm", "-f", run_vol], check=False)
        (out / "result.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
