"""Chaos run: N spread bots at the target config, settle, then hammer the server for --chaos minutes trying to crash it.

Usage: uv run --no-project python -I chaos.py <tag> --wheel <endstone-*.whl> --chaos <minutes> [--n 100] [...]
       uv run --no-project python -I chaos.py <tag> --vanilla --chaos <minutes> [...]

Setup, settle and nudging are the same as bench.py. The bots run loadbot-linux-chaos with -chaos (stdin rejoin,
reconnect after kicks, respawn, dimension acks) and -radius-jitter. During the chaos window randomised timers fire
churn, kicks, kills, gamemode flips, dimension hops, far and group teleports, mob summons, block fills, gamerule and
weather cycling, ticking areas, and list/status, each logged with a timestamp. A monitor samples TPS, MC_SERVER
ms/s, RSS and thread count every 10 s and scans the server output for errors. If the server process dies the run
stops at once and collects the Endstone crash report, sentry dumps and the last 100 log lines.
Each sample records the randomtickspeed in effect at the start (rts0) and end (rts1) of its 10 s window.
"""

import argparse
import heapq
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402
from bench import (  # noqa: E402
    BENCH_MOUNT,
    EFFECTS,
    PROPS,
    VANILLA_IMAGE_WHEEL,
    Probe,
    Server,
    build_image,
    cell,
    foreign,
    log,
    prepare_volumes,
    run,
)

SEVERE = re.compile(r"(?i)crash|fatal|segmentation|sigsegv|sigabrt|abort|assert|traceback|exception|stack ?trace"
                    r"|terminate called|core dumped|panic")
ERROR_LVL = re.compile(r"ERROR\]|\[ERROR|(?i:\berror:)")
CMD_ERR = re.compile(r"(?i)syntax error|unknown command|unknown block|is not a valid|invalid|no targets matched"
                     r"|no player|not found|not online|cannot|could not|unable to|already exists|does not exist"
                     r"|failed to execute|couldn't be placed|outside of the world")
MOBS = ["zombie", "skeleton", "creeper", "spider", "cow", "pig", "sheep", "chicken", "wolf", "villager", "slime",
        "enderman", "witch", "horse"]
TICK_SPEEDS = [0, 1, 3, 100, 1000, 1]


class ChaosBots:
    """loadbot-linux-chaos in the server's network namespace, with stdin for rejoin commands."""

    def __init__(self, srv, image, n, prefix, cpus, extra, stagger, logpath, events):
        self.srv, self.n, self.prefix, self.events = srv, n, prefix, events
        self.name = f"{srv.name}-bots"
        self.joined, self.botlines, self.stats = {}, {}, []
        self.failed, self.deaths, self.online = 0, [], set()
        self.home = {f"{prefix}{i:02d}": cell(i) for i in range(n)}
        self.quick = set()
        self.c = Counter()
        self.lock, self.olock = threading.Lock(), threading.Lock()
        self.allj = threading.Event()
        self.log = open(logpath, "w", encoding="utf-8")
        self.p = subprocess.Popen(
            ["docker", "run", "-i", "--rm", "--name", self.name, "--network", f"container:{srv.name}",
             "--cpuset-cpus", cpus, "-v", f"{BENCH_MOUNT}:/bench:ro", "--entrypoint", "/bench/loadbot-linux-chaos",
             image, "-addr", "127.0.0.1:19132", "-n", str(n), "-prefix", prefix, "-d", "86400s", "-stagger", stagger,
             *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.p.stdin.reconfigure(newline="\n")
        threading.Thread(target=self._pump, daemon=True).start()

    def online_list(self):
        with self.olock:
            return sorted(self.online)

    def send(self, s):
        with self.lock:
            try:
                self.p.stdin.write(s + "\n")
                self.p.stdin.flush()
            except OSError:
                pass

    def go_home(self, name):
        x, z = self.home[name]
        for e in EFFECTS:
            self.srv.cmd(f"effect {name} {e}")
        self.srv.cmd(f"gamemode survival {name}")
        self.srv.cmd(f"execute in overworld run tp {name} {x} 300 {z}")

    def _pump(self):
        for line in self.p.stdout:
            self.log.write(line)
            self.log.flush()
            parts = line.split()
            if len(parts) < 2:
                continue
            kind, who = parts[1], parts[2] if len(parts) >= 3 else ""
            if kind == "JOINED":
                first = who not in self.joined
                self.joined.setdefault(who, time.perf_counter())
                with self.olock:
                    self.online.add(who)
                if first:
                    x, z = self.home[who]
                    for e in EFFECTS:
                        self.srv.cmd(f"effect {who} {e}")
                    self.srv.cmd(f"tp {who} {x} 300 {z}")
                else:
                    self.c["rejoined"] += 1
                    self.go_home(who)
                    if who in self.quick:
                        self.quick.discard(who)
                        self.events.later(random.uniform(1.0, 2.0), "quick_disconnect", who)
            elif kind == "LEFT":
                with self.olock:
                    self.online.discard(who)
            elif kind == "RESPAWNED":
                self.c["respawned"] += 1
                self.go_home(who)
            elif kind == "DIED":
                self.deaths.append(who)
            elif kind == "DIM":
                self.c["dim_acks"] += 1
            elif kind == "BOT":
                self.botlines[who] = line.strip()
            elif kind == "STAT":
                kv = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
                self.stats.append((time.perf_counter(), {k: float(v) for k, v in kv.items()
                                                         if re.fullmatch(r"-?[\d.]+", v)}))
            elif "READ FAILED" in line:
                with self.olock:
                    self.online.discard(parts[1])
                self.c["read_failed"] += 1
            elif "REJOIN FAILED" in line and "interrupt" not in line:
                self.c["rejoin_failed"] += 1
            elif "REJOIN GAVE UP" in line:
                self.c["rejoin_gave_up"] += 1
            elif "DIAL FAILED" in line or "SPAWN FAILED" in line:
                self.failed += 1
            elif "ALLJOINED" in line:
                self.allj.set()

    def last(self, key):
        return self.stats[-1][1].get(key, 0.0) if self.stats else 0.0

    def uniq_rate(self, secs=60):
        pts = self.stats[-(secs // 10 + 1):]
        if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
            return 0.0
        return (pts[-1][1]["uniq"] - pts[0][1]["uniq"]) / (pts[-1][0] - pts[0][0])

    def stop(self):
        run(["docker", "kill", "--signal", "INT", self.name], check=False)
        try:
            self.p.wait(30)
        except Exception:
            run(["docker", "rm", "-f", self.name], check=False)
            self.p.kill()


class Chaos:
    def __init__(self, srv, out, seed):
        self.srv, self.bots = srv, None
        self.rng = random.Random(seed)
        self.c = Counter()
        self.heap, self.hlock, self.seq = [], threading.Lock(), 0
        self.elog = open(out / "chaos_events.log", "w", encoding="utf-8")
        self.tick_i, self.weather, self.waypoints, self.areas = 0, False, True, []
        self.rts = 1
        self.away_dim = {}

    def event(self, action, detail=""):
        self.c[action] += 1
        now = time.time()
        self.elog.write(f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now * 1000) % 1000:03d} "
                        f"{action} {detail}\n")
        self.elog.flush()

    def later(self, delay, action, *args):
        with self.hlock:
            self.seq += 1
            heapq.heappush(self.heap, (time.perf_counter() + delay, self.seq, action, args))

    def due(self):
        now, out = time.perf_counter(), []
        with self.hlock:
            while self.heap and self.heap[0][0] <= now:
                out.append(heapq.heappop(self.heap)[2:])
        return out

    def pick(self, lo, hi):
        pool = self.bots.online_list()
        k = min(len(pool), self.rng.randint(lo, hi))
        return self.rng.sample(pool, k) if k > 0 else []

    def far(self):
        return self.rng.randrange(-60000, 60000, 16), self.rng.randrange(-60000, 60000, 16)

    def cmd(self, s):
        self.srv.cmd(s)

    # locator bar
    def a_churn(self):
        names = self.pick(max(1, self.bots.n // 10), max(1, self.bots.n // 10))
        for nm in names:
            if self.rng.random() < 0.3:
                self.bots.quick.add(nm)
            self.bots.send(f"rejoin {nm}")
        self.event("churn", " ".join(names) + (f" quick={sorted(self.bots.quick & set(names))}" if names else ""))

    def a_group_tp(self):
        names = self.pick(5, 10)
        x, z = self.far()
        for nm in names:
            self.cmd(f"execute in overworld run tp {nm} {x} 200 {z}")
        self.event("group_tp", f"{x},200,{z} " + " ".join(names))

    def a_waypoints(self):
        self.waypoints = not self.waypoints
        self.cmd(f"gamerule playerwaypoints {'everyone' if self.waypoints else 'off'}")
        self.cmd(f"gamerule locatorbar {str(self.waypoints).lower()}")
        self.event("gamerule_playerwaypoints", "everyone" if self.waypoints else "off")

    def a_gamemode(self):
        names = self.pick(2, 5)
        for nm in names:
            self.cmd(f"gamemode spectator {nm}")
            self.later(self.rng.uniform(10, 20), "survival", nm)
        self.event("spectator", " ".join(names))

    def a_kill(self):
        names = self.pick(2, 3)
        for nm in names:
            self.cmd(f"kill {nm}")
        self.event("kill", " ".join(names))

    # player scans
    def a_summon(self):
        k = self.rng.randint(20, 50)
        pool = self.bots.online_list()
        if not pool:
            return
        for _ in range(k):
            nm, mob = self.rng.choice(pool), self.rng.choice(MOBS)
            self.cmd(f"execute at {nm} run summon {mob} ~{self.rng.randint(-8, 8)} ~ ~{self.rng.randint(-8, 8)}")
        self.event("summon", f"{k} mobs near {len(pool)} bots")

    def a_kick(self):
        names = self.pick(1, 3)
        for nm in names:
            self.cmd(f"kick {nm} chaos")
        self.event("kick", " ".join(names))

    def a_dim_hop(self):
        names = self.pick(2, 4)
        for nm in names:
            dim = self.rng.choice(["nether", "the_end"])
            x, z = self.bots.home[nm]
            if dim == "nether":
                self.cmd(f"execute in nether run tp {nm} {x // 8} 64 {z // 8}")
            else:
                self.cmd(f"execute in the_end run tp {nm} {self.rng.randint(-40, 40)} 70 {self.rng.randint(-40, 40)}")
            self.later(self.rng.uniform(15, 30), "dim_back", nm)
            self.event("dim_hop", f"{nm} {dim}")

    def a_list_status(self):
        self.cmd("list")
        self.cmd("status")
        self.event("list_status")

    # chunk publisher
    def a_far_tp(self):
        names = self.pick(3, 5)
        for nm in names:
            x, z = self.far()
            self.bots.home[nm] = (x, z)
            self.cmd(f"execute in overworld run tp {nm} {x} 200 {z}")
        self.event("far_tp", " ".join(names))

    def a_load_disconnect(self):
        names = self.pick(1, 2)
        for nm in names:
            x, z = self.far()
            self.bots.home[nm] = (x, z)
            self.cmd(f"execute in overworld run tp {nm} {x} 200 {z}")
            self.later(self.rng.uniform(1.0, 2.0), "rejoin", nm)
        self.event("load_disconnect", " ".join(names))

    # random tick
    def a_randomtick(self):
        v = TICK_SPEEDS[self.tick_i % len(TICK_SPEEDS)]
        self.tick_i += 1
        self.rts = v
        self.cmd(f"gamerule randomtickspeed {v}")
        self.event("randomtickspeed", str(v))

    def a_weather(self):
        self.weather = not self.weather
        self.cmd("weather thunder" if self.weather else "weather clear")
        self.event("weather", "thunder" if self.weather else "clear")

    def a_blocks(self):
        names = self.pick(2, 3)
        for nm in names:
            at = f"execute at {nm} run"
            self.cmd(f"{at} fill ~-4 ~-1 ~-4 ~4 ~-1 ~4 grass_block")
            self.cmd(f"{at} fill ~-2 ~ ~-2 ~2 ~ ~2 oak_sapling")
            self.cmd(f"{at} fill ~-3 ~3 ~-3 ~3 ~4 ~3 oak_leaves")
            self.cmd(f"{at} fill ~5 ~-1 ~-4 ~7 ~-1 ~4 farmland")
            self.cmd(f"{at} fill ~5 ~ ~-4 ~7 ~ ~4 wheat")
            self.cmd(f"{at} setblock ~ ~-1 ~ grass_block")
        self.event("blocks", " ".join(names))

    def a_tickingarea(self):
        if self.areas and (len(self.areas) >= 5 or self.rng.random() < 0.4):
            nm = self.areas.pop(self.rng.randrange(len(self.areas)))
            self.cmd(f"tickingarea remove {nm}")
            self.event("tickingarea_remove", nm)
            return
        pool = self.bots.online_list()
        if not pool:
            return
        x, z = self.bots.home[self.rng.choice(pool)]
        nm = f"chaos{self.c['tickingarea_add']}"
        self.areas.append(nm)
        self.cmd(f"tickingarea add circle {x} 64 {z} {self.rng.randint(1, 4)} {nm}")
        self.event("tickingarea_add", f"{nm} {x},{z}")

    def a_cleanup(self):
        self.cmd("kill @e[type=!player]")
        self.event("cleanup", "kill @e[type=!player]")

    def delayed(self, action, args):
        nm = args[0]
        if action == "survival":
            self.cmd(f"gamemode survival {nm}")
            self.event("survival", nm)
        elif action == "dim_back":
            x, z = self.bots.home[nm]
            self.cmd(f"execute in overworld run tp {nm} {x} 300 {z}")
            self.event("dim_back", nm)
        elif action in ("rejoin", "quick_disconnect"):
            self.bots.send(f"rejoin {nm}")
            self.event(action, nm)

    ACTIONS = {"churn": 20, "group_tp": 25, "waypoints": 30, "gamemode": 20, "kill": 30, "summon": 30, "kick": 40,
               "dim_hop": 30, "list_status": 10, "far_tp": 15, "load_disconnect": 45, "randomtick": 30,
               "weather": 45, "blocks": 20, "tickingarea": 40, "cleanup": 300}


def slope_per_hour(pts):
    if len(pts) < 3:
        return None
    xs, ys = [t / 3600 for t, _ in pts], [v for _, v in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 1) if den else None


def collect_crash(image, run_vol, out):
    dest = str(out).replace("\\", "/")
    script = ("ls -la /data/crash_reports /data/.sentry-native 2>&1; "
              "mkdir -p /out/crash && cp -a /data/crash_reports /data/.sentry-native /out/crash/ 2>/dev/null; "
              "for f in /data/crash_reports/*.txt; do [ -f \"$f\" ] && echo \"== $f\" && cat \"$f\"; done")
    r = run(["docker", "run", "--rm", "-v", f"{run_vol}:/data:ro", "-v", f"{dest}:/out", "--entrypoint", "sh",
             image, "-c", script], check=False)
    (out / "crash_report.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
    return "== /data/crash_reports/" in r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--wheel", type=Path)
    ap.add_argument("--vanilla", action="store_true", help="run bedrock_server directly, without Endstone")
    ap.add_argument("--chaos", type=float, required=True, help="minutes of chaos after settle")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--bds", default="1.26.52.3")
    ap.add_argument("--server-cpus", default="0-15")
    ap.add_argument("--bot-cpus", default="16-27")
    ap.add_argument("--bot-args", default="-csg -chaos -radius-jitter 20s", help="loadbot-linux-chaos arguments")
    ap.add_argument("--stagger", default="300ms")
    ap.add_argument("--prop", action="append", default=[])
    ap.add_argument("--min-settle", type=int, default=300)
    ap.add_argument("--max-settle", type=int, default=900)
    ap.add_argument("--settle-rate", type=float, default=0.1)
    ap.add_argument("--nudge", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20261010)
    a = ap.parse_args()
    if not a.wheel and not a.vanilla:
        ap.error("--wheel is required without --vanilla")

    wheel = (a.wheel or VANILLA_IMAGE_WHEEL).resolve()
    out = bench.B / "results" / a.tag
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    bench.OUT = out
    name = f"es-bench-{a.tag}"
    run_vol = name
    props = dict(PROPS)
    for kv in a.prop:
        k, v = kv.split("=", 1)
        props[k] = v
    res = {"args": {k: str(v) for k, v in vars(a).items()}, "vanilla": a.vanilla, "wheel": wheel.name, "props": props,
           "samples": [], "settle_series": []}

    def save():
        (out / "chaos.json").write_text(json.dumps(res, indent=1, default=str))

    fg = foreign(name)
    res["foreign_at_start"] = fg
    if fg:
        log("WARNING other containers/processes running:", fg)
    image, image_id, ver = build_image(wheel)
    res.update(image=image, image_id=image_id, version=ver)
    log("image", image, image_id[:19], wheel.name)
    run(["docker", "rm", "-f", name, f"{name}-bots"], check=False)
    prepare_volumes(image, a.bds, run_vol, props)

    srv = Server(name, image, run_vol, a.server_cpus, "default", out / "server.log", vanilla=a.vanilla)
    chaos = Chaos(srv, out, a.seed)
    probe = Probe(srv, every=2.0)
    bots = None
    crashed = False
    try:
        if not srv.started.wait(600):
            raise RuntimeError("server did not start")
        res["server"] = srv.info()
        res["endstone"] = srv.grep(r"This server is running Endstone version.*")
        res["bds_version"] = srv.grep(r"Version: [\d.]+")
        log("server", json.dumps(res["server"]), res["endstone"] or "vanilla", res["bds_version"])
        probe.start()

        bots = ChaosBots(srv, image, a.n, "b", a.bot_cpus, a.bot_args.split(), a.stagger, out / "bots.log", chaos)
        chaos.bots = bots
        if not bots.allj.wait(300):
            log("WARNING bots did not report ALLJOINED")
        t_join = time.perf_counter()
        log("joined", len(bots.joined), "failed", bots.failed)

        nudges = {}
        while True:
            time.sleep(30)
            if srv.p.poll() is not None:
                raise RuntimeError("server exited during settle")
            el = time.perf_counter() - t_join
            views, airborne = [], 0
            for bname, line in list(bots.botlines.items()):
                m = re.search(r"uniq=(\d+)", line)
                p = re.search(r"pos=[-\d.]+,([-\d.]+),", line)
                if m:
                    views.append(int(m.group(1)))
                if p and float(p.group(1)) > 290:
                    airborne += 1
                    if a.nudge and el >= 60 and m and int(m.group(1)) < a.nudge:
                        x, z = bots.home[bname]
                        nudges[bname] = nudges.get(bname, 0) + 1
                        srv.cmd(f"tp {bname} {x + 16 * (nudges[bname] % 2)} 300 {z}")
            ur = bots.uniq_rate(60)
            row = {"el": round(el), "uniq": bots.last("uniq"), "uniq_s_bot": round(ur / a.n, 3),
                   "online": bots.last("online"), "view_min": min(views) if views else 0, "airborne": airborne}
            res["settle_series"].append(row)
            log("settle", " ".join(f"{k}={v}" for k, v in row.items()))
            if el >= a.max_settle or (el >= a.min_settle and ur / a.n <= a.settle_rate):
                break
        save()

        log("chaos start", f"{a.chaos} min")
        t0 = time.perf_counter()
        res["chaos_t0_wall"] = t0 + time.time() - time.perf_counter()
        end = t0 + a.chaos * 60
        span = a.chaos * 60
        rng = chaos.rng
        due = {k: t0 + rng.uniform(2, max(3, min(p, span / 2))) for k, p in Chaos.ACTIONS.items()}
        nxt_mon, line_i = t0 + 10, len(srv.lines)
        prev = srv.snapshot()
        rts0 = chaos.rts
        severe, errors, cmd_errs, cmd_err_lines = [], [], 0, []
        while time.perf_counter() < end:
            now = time.perf_counter()
            if srv.p.poll() is not None:
                crashed = True
                break
            if bots.p.poll() is not None:
                log("bots container exited", bots.p.returncode)
                res["bots_exited"] = bots.p.returncode
                break
            for k in sorted(due, key=due.get):
                if due[k] <= now:
                    getattr(chaos, f"a_{k}")()
                    due[k] = now + Chaos.ACTIONS[k] * rng.uniform(0.6, 1.4)
            for action, args in chaos.due():
                chaos.delayed(action, args)
            if now >= nxt_mon:
                nxt_mon += 10
                with srv.lock:
                    new = srv.lines[line_i:]
                    line_i += len(new)
                for ln in new:
                    if SEVERE.search(ln):
                        severe.append(ln.rstrip())
                    elif CMD_ERR.search(ln):
                        cmd_errs += 1
                        if len(cmd_err_lines) < 300:
                            cmd_err_lines.append(ln.rstrip())
                    elif ERROR_LVL.search(ln):
                        errors.append(ln.rstrip())
                snap = srv.snapshot()
                if not snap["threads"]:
                    time.sleep(2)
                    crashed = srv.p.poll() is not None or not srv.snapshot()["threads"]
                    if crashed:
                        break
                w = srv.window(prev, snap)
                prev = snap
                s = {"t": round(now - t0), "tps": w["tps"] and round(w["tps"], 2), "mc_ms_s": w["mc_server_ms_s"],
                     "rss_mb": w["rss_mb"], "threads": len(snap["threads"]), "online": len(bots.online_list()),
                     "severe": len(severe), "errors": len(errors), "cmd_errors": cmd_errs,
                     "actions": sum(chaos.c.values()), "rts0": rts0, "rts1": chaos.rts, "cores": w["proc_cores"]}
                rts0 = chaos.rts
                res["samples"].append(s)
                log("mon", " ".join(f"{k}={v}" for k, v in s.items()))
            time.sleep(0.2)
        res["chaos_s"] = round(time.perf_counter() - t0)
        res["severe_lines"] = severe[:500]
        res["error_lines"] = errors[:500]
        res["cmd_error_lines"] = cmd_err_lines
        if crashed:
            log("SERVER DIED", "exit", srv.p.poll())
    finally:
        probe.halt.set()
        time.sleep(1)
        with srv.lock:
            (out / "last100.log").write_text("".join(srv.lines[-100:]), encoding="utf-8")
        if bots and bots.p.poll() is None:
            bots.stop()
        if srv.p.poll() is None:
            srv.stop()
        else:
            crashed = True
        res["server_exit"] = srv.p.returncode
        off = time.time() - time.perf_counter()
        with srv.lock:
            res["gametime_wall"] = [(round(t + off, 3), g) for t, g in srv.gametime]
        res["crash_report"] = collect_crash(image, run_vol, out)
        run(["docker", "rm", "-f", name, f"{name}-bots"], check=False)
        if not crashed and not res["crash_report"]:
            run(["docker", "volume", "rm", "-f", run_vol], check=False)
        tps = [s["tps"] for s in res["samples"] if s["tps"] is not None]
        low = [s["tps"] for s in res["samples"] if s["tps"] is not None and s.get("rts0", 1) <= 3]
        high = [s["tps"] for s in res["samples"] if s["tps"] is not None and s.get("rts0", 1) > 3]
        res["actions"] = dict(chaos.c)
        res["bot_events"] = dict(bots.c) if bots else {}
        res["bot_stat"] = bots.stats[-1][1] if bots and bots.stats else {}
        res["summary"] = {
            "crash": bool(crashed or res["crash_report"]), "server_exit": srv.p.returncode,
            "chaos_s": res.get("chaos_s"), "tps_min": min(tps) if tps else None,
            "tps_median": round(statistics.median(tps), 2) if tps else None,
            "tps_median_rts_le3": round(statistics.median(low), 2) if low else None,
            "tps_median_rts_ge100": round(statistics.median(high), 2) if high else None,
            "rss_peak_mb": max((s["rss_mb"] for s in res["samples"] if s["rss_mb"]), default=None),
            "rss_slope_mb_h": slope_per_hour([(s["t"], s["rss_mb"]) for s in res["samples"] if s["rss_mb"]]),
            "rss_start_mb": res["samples"][0]["rss_mb"] if res["samples"] else None,
            "rss_end_mb": res["samples"][-1]["rss_mb"] if res["samples"] else None,
            "threads_start": res["samples"][0]["threads"] if res["samples"] else None,
            "threads_end": res["samples"][-1]["threads"] if res["samples"] else None,
            "severe_lines": res["samples"][-1]["severe"] if res["samples"] else None,
            "error_lines": res["samples"][-1]["errors"] if res["samples"] else None,
            "cmd_errors": res["samples"][-1]["cmd_errors"] if res["samples"] else None}
        save()
        log("CHAOS", json.dumps(res["summary"]))
        log("actions", json.dumps(res["actions"]))
        log("bot events", json.dumps(res["bot_events"]), json.dumps(res["bot_stat"]))


if __name__ == "__main__":
    main()
