"""Endstone spread-bot benchmark on Linux (Docker) at the target config.

Usage: uv run --no-project python -I bench.py <tag> --wheel <endstone-*.whl> [--n 100] [...]
       uv run --no-project python -I bench.py <tag> --vanilla [...]

--vanilla runs /data/bedrock_server directly in the image built from --wheel (default: the 0.11.14 wheel), so the
container userland is the same and only Endstone is missing.

Target config: view-distance 10, tick-distance 4, client-side chunk generation on, server-build-radius-ratio Disabled.
The server container is pinned to --server-cpus and the bot container to --bot-cpus. The bots share the server's
network namespace and dial 127.0.0.1:19132, so no Docker UDP proxy sits in the path. Every run starts from a fresh
copy of BDS (fresh world and Endstone config) with seed 20261010. Bots are teleported on join to y=300 in their own
640-block cell of an 11x11 grid, hovering ones are nudged sideways until they load chunks, and the run settles once
the unique-chunk rate drops to --settle-rate per bot. Then a --steady window measures TPS from gametime deltas and
MC_SERVER CPU from /proc by thread name.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

B = Path(__file__).resolve().parent
BENCH_MOUNT = str(B).replace("\\", "/")
VANILLA_IMAGE_WHEEL = B / "wheels" / "after" / "endstone-0.11.14.dev4-cp314-cp314-manylinux_2_31_x86_64.whl"
BDS_ZIPS = "C:/Users/Vincent/.bedrock_server/linux"
SPACING, GRID, ORIGIN = 640, 11, 1280
PRELOAD = {"default": None, "glibc": "/lib/x86_64-linux-gnu/libc.so.6"}
PROPS = {"server-name": "endstone-bench", "transport": "raknet", "online-mode": "false", "allow-list": "false",
         "max-players": "200", "server-port": "19132", "server-portv6": "19133", "enable-lan-visibility": "false",
         "level-name": "bench", "level-seed": "20261010", "view-distance": "10", "tick-distance": "4",
         "client-side-chunk-generation-enabled": "true", "server-build-radius-ratio": "Disabled"}
EFFECTS = ["resistance infinite 255 true", "fire_resistance infinite 0 true", "water_breathing infinite 0 true",
           "saturation infinite 255 true"]
STRIP = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\u00a7.")
PREFIX = re.compile(r"^\[[^\]]*\]:? ?")
FIND = ("for p in /proc/[0-9]*; do case \"$(readlink $p/exe 2>/dev/null)\" in */bedrock_server) "
        "pid=${p#/proc/};; esac; done; ")

OUT = None


def log(*a):
    line = " ".join(str(x) for x in (time.strftime("%H:%M:%S"),) + a)
    print(line, flush=True)
    if OUT:
        with open(OUT / "bench.out", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run(args, check=True, **kw):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args[:4])} failed ({r.returncode}): {r.stderr.strip()[-500:]}")
    return r


def cell(i):
    return ORIGIN + (i % GRID) * SPACING, ORIGIN + (i // GRID) * SPACING


def build_image(wheel):
    ver = re.match(r"endstone-([^-]+)-", wheel.name).group(1)
    ctx = B / "build" / wheel.stem
    ctx.mkdir(parents=True, exist_ok=True)
    for f in ctx.glob("*.whl"):
        f.unlink()
    shutil.copy2(wheel, ctx / wheel.name)
    shutil.copy2(B / "image" / "Dockerfile", ctx / "Dockerfile")
    tag = "endstone-bench:" + re.sub(r"[^A-Za-z0-9_.-]", "_", ver)
    image_id = run(["docker", "build", "-q", "-t", tag, str(ctx)]).stdout.strip()
    return tag, image_id, ver


def prepare_volumes(image, bds, run_vol, props):
    """BDS unpacked once into a base volume; each run gets a fresh copy with the bench server.properties."""
    base = f"endstone-bench-bds-{bds}"
    mc = ".".join(bds.split(".")[1:3])
    run(["docker", "volume", "create", base])
    populate = (f"[ -f /base/bedrock_server ] || ( python -m zipfile -e /zip/bedrock-server-{bds}.zip /base"
                f" && printf {mc} > /base/version.txt ); cat /base/version.txt")
    r = run(["docker", "run", "--rm", "-v", f"{base}:/base", "-v", f"{BDS_ZIPS}:/zip:ro", "--entrypoint", "sh",
             image, "-c", populate])
    log("bds", bds, "version.txt", r.stdout.strip())
    run(["docker", "volume", "rm", "-f", run_vol], check=False)
    run(["docker", "volume", "create", run_vol])
    sed = " && ".join(f"(grep -q '^{k}=' server.properties && sed -i 's|^{k}=.*|{k}={v}|' server.properties"
                      f" || echo '{k}={v}' >> server.properties)" for k, v in props.items())
    script = ("cp -a /base/. /data/ && cd /data && " + sed +
              " && grep -E '^(" + "|".join(props) + ")=' server.properties | tr '\\n' ' '")
    r = run(["docker", "run", "--rm", "-v", f"{base}:/base:ro", "-v", f"{run_vol}:/data", "--entrypoint", "sh",
             image, "-c", script])
    log("props", r.stdout.strip())


def tps_between(gt, t0, t1):
    pts = [(t, g) for t, g in gt if t0 <= t <= t1]
    if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
        return None
    return (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0])


def tps_min(gt, t0, t1, span=15.0):
    """Lowest TPS over consecutive `span`-second sub-windows."""
    lows, a = [], t0
    while a + span <= t1 + 0.5:
        v = tps_between(gt, a, a + span)
        if v is not None:
            lows.append(v)
        a += span
    return min(lows) if lows else None


class Server:
    def __init__(self, name, image, run_vol, cpus, alloc, log_path, vanilla=False, extra_env=None):
        self.name = name
        env = ["-e", f"LD_PRELOAD={PRELOAD[alloc]}"] if PRELOAD[alloc] else []
        for k, v in (extra_env or {}).items():
            env += ["-e", f"{k}={v}"]
        tail = []
        if vanilla:
            # zipfile extraction drops the exec bit; Endstone's launcher sets it itself
            env += ["-e", "LD_LIBRARY_PATH=/data", "--entrypoint", "/bin/sh"]
            tail = ["-c", "chmod +x /data/bedrock_server && exec /data/bedrock_server"]
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.p = subprocess.Popen(
            ["docker", "run", "-i", "--rm", "--name", name, "-v", f"{run_vol}:/data", "--cpuset-cpus", cpus, *env,
             image, *tail], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
        self.p.stdin.reconfigure(newline="\n")
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self.lines = []
        self.gametime = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for raw in self.p.stdout:
            now = time.perf_counter()
            wall = time.time()
            line = STRIP.sub("", raw)
            self.log.write(f"{time.strftime('%H:%M:%S', time.localtime(wall))}.{int(wall * 1000) % 1000:03d} {line}")
            self.log.flush()
            with self.lock:
                self.lines.append(line)
            if "Server started." in line:
                self.started.set()
            if m := re.search(r"(?i)gametime is (\d+)", line):
                with self.lock:
                    self.gametime.append((now, int(m.group(1))))

    def cmd(self, s):
        with self.cmd_lock:
            self.p.stdin.write(s + "\n")
            self.p.stdin.flush()

    def grep(self, pattern):
        with self.lock:
            for line in self.lines:
                if m := re.search(pattern, line):
                    return m.group(0).strip()
        return None

    def exec(self, script):
        return run(["docker", "exec", self.name, "sh", "-c", script], check=False).stdout

    def info(self):
        script = FIND + ("grep -E '^(Cpus_allowed_list|Threads)' /proc/$pid/status; echo maps"
                         " $(tr -s ' ' < /proc/$pid/maps | cut -d' ' -f6 | grep -E 'malloc|/libc\\.so' | sort -u);"
                         " echo cpu $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2); echo nproc $(nproc --all)")
        rows = (line.split(None, 1) for line in self.exec(script).splitlines() if len(line.split(None, 1)) == 2)
        return {k.rstrip(":"): v.strip() for k, v in rows}

    def snapshot(self):
        """Per-thread CPU seconds and comm of bedrock_server, plus RSS."""
        script = FIND + ("grep VmRSS /proc/$pid/status; for t in /proc/$pid/task/*; do "
                         "echo T ${t##*/} $(sed 's/^.*) //' $t/stat | awk '{print $12, $13}') "
                         "$(tr ' ' '_' < $t/comm); done")
        snap = {"t": time.perf_counter(), "threads": {}, "rss_mb": None}
        for line in self.exec(script).splitlines():
            parts = line.split()
            if parts[:1] == ["VmRSS:"]:
                snap["rss_mb"] = int(parts[1]) / 1024
            elif parts[:1] == ["T"] and len(parts) >= 5:
                snap["threads"][parts[1]] = ((int(parts[2]) + int(parts[3])) / 100.0, parts[4])
        return snap

    def window(self, s0, s1):
        with self.lock:
            gt = list(self.gametime)
        tps = tps_between(gt, s0["t"], s1["t"])
        wall = s1["t"] - s0["t"]
        by_name, mc, per_tid = {}, {}, {}
        for tid, (cpu, name) in s1["threads"].items():
            prev = s0["threads"].get(tid)
            d = cpu - prev[0] if prev else 0.0
            if d > 0:
                per_tid[tid] = [name, round(1000 * d / wall)]
            n, tot = by_name.get(name, (0, 0.0))
            by_name[name] = (n + 1, tot + d)
            if name == "MC_SERVER":
                mc[int(tid)] = round(1000 * d / wall)
        # threads spawned by the tick thread inherit its name; the tick thread is the oldest
        mc_ms_s = mc[min(mc)] if mc else 0
        top = sorted(((round(1000 * tot / wall), name, n) for name, (n, tot) in by_name.items()), reverse=True)
        return {"wall_s": round(wall, 1), "tps": tps, "tps_min_15s": tps_min(gt, s0["t"], s1["t"]),
                "mc_server_ms_s": round(mc_ms_s), "mc_server_ms_tick": round(mc_ms_s / tps, 2) if tps else None,
                "mc_server_tids_ms_s": {str(k): mc[k] for k in sorted(mc)},
                "proc_cores": round(sum(tot for _, tot in by_name.values()) / wall, 2),
                "threads_ms_s": [f"{name}x{n}={ms}" for ms, name, n in top[:6]],
                "tids_ms_s": dict(sorted(per_tid.items(), key=lambda kv: -kv[1][1])),
                "rss_mb": round(s1["rss_mb"]) if s1["rss_mb"] else None}

    def query_positions(self, wait=10.0):
        with self.lock:
            start = len(self.lines)
        self.cmd("querytarget @a")
        time.sleep(wait)
        with self.lock:
            txt = "".join(PREFIX.sub("", line) for line in self.lines[start:])
        i = txt.find("Target data:")
        j = txt.find("[", i) if i >= 0 else -1
        if j < 0:
            return []
        depth, k = 0, j
        for k in range(j, len(txt)):
            if txt[k] == "[":
                depth += 1
            elif txt[k] == "]":
                depth -= 1
                if depth == 0:
                    break
        try:
            data = json.loads(txt[j:k + 1])
        except ValueError:
            return []
        return [(e["position"]["x"], e["position"]["y"], e["position"]["z"]) for e in data]

    def stop(self):
        try:
            self.cmd("stop")
            self.p.wait(180)
        except Exception:
            self.p.kill()
            self.p.wait(10)


class Probe(threading.Thread):
    """Polls `time query gametime` so TPS = d(gametime)/d(wall)."""

    def __init__(self, srv, every=5.0):
        super().__init__(daemon=True)
        self.srv, self.every, self.halt = srv, every, threading.Event()

    def run(self):
        nxt = time.perf_counter()
        while not self.halt.is_set():
            self.srv.cmd("time query gametime")
            nxt += self.every
            self.halt.wait(max(0.0, nxt - time.perf_counter()))


class Bots:
    """One loadbot container inside the server's network namespace. Each bot is sent to its own cell on join."""

    def __init__(self, srv, image, n, prefix, cpus, extra, stagger, logpath):
        self.srv, self.n, self.prefix = srv, n, prefix
        self.name = f"{srv.name}-bots"
        self.joined, self.botlines, self.stats = {}, {}, []
        self.failed, self.dropped, self.deaths = 0, 0, []
        self.allj = threading.Event()
        self.log = open(logpath, "w", encoding="utf-8")
        self.p = subprocess.Popen(
            ["docker", "run", "-i", "--rm", "--name", self.name, "--network", f"container:{srv.name}",
             "--cpuset-cpus", cpus, "-v", f"{BENCH_MOUNT}:/bench:ro", "--entrypoint", "/bench/loadbot-linux", image,
             "-addr", "127.0.0.1:19132", "-n", str(n), "-prefix", prefix, "-d", "7200s", "-stagger", stagger,
             *extra], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def index(self, name):
        return int(name[len(self.prefix):])

    def tp_to_cell(self, name, y=300):
        x, z = cell(self.index(name))
        for e in EFFECTS:
            self.srv.cmd(f"effect {name} {e}")
        self.srv.cmd(f"tp {name} {x} {y} {z}")

    def _pump(self):
        for line in self.p.stdout:
            self.log.write(line)
            self.log.flush()
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "JOINED":
                self.joined[parts[2]] = time.perf_counter()
                self.tp_to_cell(parts[2])
            elif len(parts) >= 3 and parts[1] == "BOT":
                self.botlines[parts[2]] = line.strip()
            elif len(parts) >= 2 and parts[1] == "STAT":
                kv = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
                self.stats.append((time.perf_counter(), {k: float(kv[k]) for k in
                                                         ("online", "dropped", "tp", "deaths", "uniq")}))
            elif len(parts) >= 3 and parts[1] == "DIED":
                self.deaths.append(parts[2])
            elif "READ FAILED" in line:
                self.dropped += 1
            elif "DIAL FAILED" in line or "SPAWN FAILED" in line:
                self.failed += 1
            elif "ALLJOINED" in line:
                self.allj.set()

    def last(self, key):
        return self.stats[-1][1][key] if self.stats else 0.0

    def uniq_rate(self, secs=60):
        pts = self.stats[-(secs // 10 + 1):]
        if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
            return 0.0
        return (pts[-1][1]["uniq"] - pts[0][1]["uniq"]) / (pts[-1][0] - pts[0][0])

    def cpus(self):
        r = run(["docker", "exec", self.name, "sh", "-c", "grep Cpus_allowed_list /proc/1/status"], check=False)
        return r.stdout.split(":", 1)[-1].strip()

    def stop(self):
        run(["docker", "kill", "--signal", "INT", self.name], check=False)
        try:
            self.p.wait(30)
        except Exception:
            run(["docker", "rm", "-f", self.name], check=False)
            self.p.kill()


def verify_spread(positions, n_cells):
    occ, worst = {}, 0.0
    for x, y, z in positions:
        best = min(range(n_cells), key=lambda i: (cell(i)[0] - x) ** 2 + (cell(i)[1] - z) ** 2)
        cx, cz = cell(best)
        worst = max(worst, ((cx - x) ** 2 + (cz - z) ** 2) ** 0.5)
        occ[best] = occ.get(best, 0) + 1
    return {"players": len(positions), "cells_occupied": len(occ), "max_per_cell": max(occ.values()) if occ else 0,
            "worst_offset_blocks": round(worst, 1)}


def foreign(own):
    """Other containers, and host-side BDS or loadbot processes, that would skew the numbers."""
    names = run(["docker", "ps", "--format", "{{.Names}}"], check=False).stdout.split()
    procs = run(["tasklist", "/FO", "CSV", "/NH"], check=False).stdout.splitlines()
    host = sorted({p.split('","')[0].strip('"') for p in procs
                   if p.strip('"').lower().startswith(("bedrock_server", "loadbot", "cibuildwheel"))})
    return [n for n in names if not n.startswith(own)] + host


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--wheel", type=Path)
    ap.add_argument("--vanilla", action="store_true", help="run bedrock_server directly, without Endstone")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--alloc", choices=list(PRELOAD), default="default", help="glibc disables the bundled mimalloc")
    ap.add_argument("--bds", default="1.26.52.3", help=f"BDS zip version under {BDS_ZIPS}")
    ap.add_argument("--server-cpus", default="0-15")
    ap.add_argument("--bot-cpus", default="16-27")
    ap.add_argument("--bot-args", default="-csg", help="extra loadbot arguments")
    ap.add_argument("--stagger", default="300ms")
    ap.add_argument("--prop", action="append", default=[], help="server.properties override, key=value")
    ap.add_argument("--min-settle", type=int, default=300)
    ap.add_argument("--max-settle", type=int, default=900)
    ap.add_argument("--settle-rate", type=float, default=0.1, help="unique chunks/s per bot")
    ap.add_argument("--nudge", type=int, default=30, help="re-teleport hovering bots with fewer unique chunks")
    ap.add_argument("--steady", type=int, default=90)
    ap.add_argument("--keep-volume", action="store_true")
    a = ap.parse_args()
    if not a.wheel and not a.vanilla:
        ap.error("--wheel is required without --vanilla")

    wheel = (a.wheel or VANILLA_IMAGE_WHEEL).resolve()
    OUT = B / "results" / a.tag
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    name = f"es-bench-{a.tag}"
    run_vol = name
    props = dict(PROPS)
    for kv in a.prop:
        k, v = kv.split("=", 1)
        props[k] = v

    res = {"args": {k: str(v) for k, v in vars(a).items()}, "vanilla": a.vanilla, "wheel": wheel.name,
           "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "props": props, "windows": {},
           "settle_series": []}
    marks = {}

    def mark(k):
        marks[k] = time.perf_counter()
        log("mark", k)

    def save():
        res["marks"] = {k: round(v - marks["start"], 1) for k, v in marks.items()}
        (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))

    mark("start")
    fg = foreign(name)
    res["foreign_at_start"] = fg
    if fg:
        log("WARNING other containers/processes running:", fg)
    image, image_id, ver = build_image(wheel)
    res.update(image=image, image_id=image_id, version=ver)
    log("image", image, image_id[:19], wheel.name)
    run(["docker", "rm", "-f", name, f"{name}-bots"], check=False)
    prepare_volumes(image, a.bds, run_vol, props)

    srv = Server(name, image, run_vol, a.server_cpus, a.alloc, OUT / "server.log", vanilla=a.vanilla)
    bots = None
    probe = Probe(srv)
    try:
        if not srv.started.wait(600):
            raise RuntimeError("server did not start")
        mark("started")
        res["server"] = srv.info()
        res["endstone"] = srv.grep(r"This server is running Endstone version.*|Endstone v[\w.+-]+")
        res["bds_version"] = srv.grep(r"Version: [\d.]+")
        log("server", json.dumps(res["server"]), res["endstone"] or "vanilla", res["bds_version"])
        probe.start()
        time.sleep(10)
        s0 = srv.snapshot()
        time.sleep(20)
        s1 = srv.snapshot()
        res["windows"]["idle"] = srv.window(s0, s1)
        log("window idle", json.dumps(res["windows"]["idle"]))

        mark("bots_start")
        s_storm = srv.snapshot()
        bots = Bots(srv, image, a.n, "b", a.bot_cpus, a.bot_args.split(), a.stagger, OUT / "bots.log")
        if not bots.allj.wait(300):
            log("WARNING bots did not report ALLJOINED")
        mark("alljoined")
        res["bot_cpus_allowed"] = bots.cpus()
        log("joined", len(bots.joined), "failed", bots.failed, "bot cpus", res["bot_cpus_allowed"])

        nudges = {}
        prev = srv.snapshot()
        while True:
            time.sleep(30)
            el = time.perf_counter() - marks["alljoined"]
            snap = srv.snapshot()
            w = srv.window(prev, snap)
            prev = snap
            views, airborne, nudged = [], 0, 0
            for bname, line in list(bots.botlines.items()):
                m = re.search(r"uniq=(\d+)", line)
                p = re.search(r"pos=[-\d.]+,([-\d.]+),", line)
                if m:
                    views.append(int(m.group(1)))
                if p and float(p.group(1)) > 290:
                    airborne += 1
                    if a.nudge and el >= 60 and m and int(m.group(1)) < a.nudge:
                        x, z = cell(bots.index(bname))
                        nudges[bname] = nudges.get(bname, 0) + 1
                        srv.cmd(f"tp {bname} {x + 16 * (nudges[bname] % 2)} 300 {z}")
                        nudged += 1
            views.sort()
            ur = bots.uniq_rate(60)
            row = {"el": round(el), "uniq": bots.last("uniq"), "uniq_s": round(ur, 1),
                   "uniq_s_bot": round(ur / a.n, 3), "tps30": w["tps"] and round(w["tps"], 2),
                   "mc_ms_s": w["mc_server_ms_s"], "rss_mb": w["rss_mb"], "online": bots.last("online"),
                   "view_min": views[0] if views else 0, "view_med": views[len(views) // 2] if views else 0,
                   "airborne": airborne, "nudged": nudged, "deaths": len(bots.deaths)}
            res["settle_series"].append(row)
            res.setdefault("settle_tids", []).append({"el": row["el"], "tids_ms_s": w["tids_ms_s"]})
            log("settle", " ".join(f"{k}={v}" for k, v in row.items()))
            if el >= a.max_settle:
                log("settle cap reached")
                res["settle_capped"] = True
                break
            if el >= a.min_settle and ur / a.n <= a.settle_rate:
                break
        mark("settled")
        s_settled = srv.snapshot()
        res["settle_s"] = round(marks["settled"] - marks["alljoined"])
        res["windows"]["storm_full"] = srv.window(s_storm, s_settled)
        log("window storm_full", json.dumps(res["windows"]["storm_full"]))
        for e in EFFECTS:
            srv.cmd(f"effect @a {e}")
        res["spread"] = verify_spread(srv.query_positions(), a.n)
        log("spread", json.dumps(res["spread"]))
        save()

        mark("steady0")
        s0 = srv.snapshot()
        u0 = bots.last("uniq")
        time.sleep(a.steady)
        s1 = srv.snapshot()
        mark("steady1")
        w = srv.window(s0, s1)
        w["uniq_gain"] = bots.last("uniq") - u0
        w["foreign"] = foreign(name)
        res["windows"]["steady"] = w
        log("window steady", json.dumps(w))
        res["bots"] = {"joined": len(bots.joined), "failed": bots.failed, "dropped": bots.dropped,
                       "online": bots.last("online"), "teleported": bots.last("tp"), "deaths": len(bots.deaths),
                       "uniq_total": bots.last("uniq")}
        probe.halt.set()
        save()
    finally:
        probe.halt.set()
        if bots and bots.p.poll() is None:
            bots.stop()
        srv.stop()
        res["server_exit"] = srv.p.returncode
        log("server exit", srv.p.returncode)
        run(["docker", "rm", "-f", name, f"{name}-bots"], check=False)
        if not a.keep_volume:
            run(["docker", "volume", "rm", "-f", run_vol], check=False)
        with srv.lock:
            res["gametime"] = [(round(t - marks["start"], 2), g) for t, g in srv.gametime]
        save()
        st = res["windows"].get("steady", {})
        log("RESULT", f"tag={a.tag}", f"wheel={'vanilla' if a.vanilla else wheel.name}", f"n={a.n}", f"online={res.get('bots', {}).get('online')}",
            f"settle_s={res.get('settle_s')}", f"tps={st.get('tps') and round(st['tps'], 2)}",
            f"tps_min_15s={st.get('tps_min_15s') and round(st['tps_min_15s'], 2)}",
            f"mc_server_ms_tick={st.get('mc_server_ms_tick')}", f"mc_server_ms_s={st.get('mc_server_ms_s')}",
            f"rss_mb={st.get('rss_mb')}", f"server_cpus={res.get('server', {}).get('Cpus_allowed_list')}")


if __name__ == "__main__":
    main()
