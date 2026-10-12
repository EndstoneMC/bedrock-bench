"""Third-party Bedrock servers under the Endstone spread-bot workload: idle, join storm, settle with nudges,
steady window, then chaos. Mirrors endstone-bench/run_endstone.py phases and metrics.

  uv run --no-project --with psutil python -I cbench.py --server pmmp --tag pmmp-100
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import collections
from pathlib import Path

CB = Path(__file__).resolve().parent.parent
SP = CB.parent
sys.path.insert(0, str(SP / "work-profile" / "scripts"))
sys.path.insert(0, str(SP / "endstone-bench"))
sys.path.insert(0, str(CB / "scripts"))
import harness as h  # noqa: E402
import psutil  # noqa: E402
import threads  # noqa: E402
from run2 import log, total_uniq, uniq_rate  # noqa: E402

import bench_metrics  # noqa: E402
from chaos import Chaos, ChaosBots  # noqa: E402

JAVA = Path(r"C:\Program Files\Java\jdk-21\bin\java.exe")
JCMD = Path(r"C:\Program Files\Java\jdk-21\bin\jcmd.exe")
PHP = CB / "dl" / "php" / "php82" / "bin" / "php" / "php.exe"
BOT_2193 = SP / "endstone-bench" / "loadbot-chaos.exe"
BOT_1001 = CB / "build" / "loadbot-1001.exe"
BOT_2193X = CB / "build" / "loadbot-2193x.exe"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CREATE_SUSPENDED = 0x4
EFFECTS = [("resistance", 255), ("fire_resistance", 0), ("water_breathing", 0), ("saturation", 255)]


def rel(s):
    """'~-5' -> -5, '~' -> 0."""
    s = s[1:]
    return int(s) if s else 0


class Adapter:
    name = ""
    port = 0
    bot = BOT_2193
    tick_cmd = ""
    tick_re = re.compile(r"Gametime is (\d+)")
    ready_re = re.compile(r"Done \(")
    connected_re = None
    status_cmd = "status"
    bot_extra = ()
    unsupported = set()
    main_thread = "first"

    def __init__(self):
        self.dir = CB / "servers" / self.name

    def prepare(self):
        pass

    def argv(self):
        raise NotImplementedError

    def tp(self, n, x, y, z):
        return [f"tp {n} {x} {y} {z}"]

    def effect(self, n, e, amp):
        return [f"effect {n} {e} infinite {amp} true"]

    def translate(self, s):
        """BDS console syntax from chaos.py -> this server's syntax; [] drops the command."""
        return [s]


class PMMP(Adapter):
    name = "pmmp"
    port = 19682
    bot = BOT_1001
    tick_cmd = "time query"
    tick_re = re.compile(r"Time is (-?\d+)")
    ready_re = re.compile(r"Done \(")
    connected_re = re.compile(r"\] Player: (\S+)\s*$")
    unsupported = {"waypoints", "summon", "cleanup", "dimension", "tickspeed", "weather", "blocks", "tickingarea"}

    def prepare(self):
        shutil.rmtree(self.dir / "worlds", ignore_errors=True)
        shutil.rmtree(self.dir / "players", ignore_errors=True)

    def argv(self):
        return [str(PHP), str(self.dir / "PocketMine-MP.phar"), "--no-wizard", "--disable-ansi",
                f"--data={self.dir}", f"--plugins={self.dir / 'plugins'}"]

    def translate(self, s):
        v = s.split()
        if v[0] in ("tp", "kill", "kick", "list", "status", "gamemode") or s.endswith(" clear") and v[0] == "effect":
            return [s]
        return []


class Nukkit(Adapter):
    name = "nukkit"
    port = 19692
    bot = BOT_2193X
    bot_extra = ("-fake-xuid",)
    tick_cmd = "gametime"
    ready_re = re.compile(r"Done \(")
    connected_re = re.compile(r"\] (\S+)\[/127\.0\.0\.1:\d+\] logged in")
    unsupported = {"waypoints", "tickingarea"}
    main_thread = "main"

    def prepare(self):
        shutil.rmtree(self.dir / "worlds", ignore_errors=True)
        shutil.rmtree(self.dir / "players", ignore_errors=True)

    def argv(self):
        return [str(JAVA), "-jar", str(self.dir / "nukkit.jar")]

    def effect(self, n, e, amp):
        return [f"effect {n} {e} 1000000 {amp} true"]

    def translate(self, s):
        v = s.split()
        if v[0] in ("tp", "kill", "kick", "list", "status", "gamemode", "weather"):
            return ["kill @e"] if s == "kill @e[type=!player]" else [s]
        if v[0] == "effect":
            return [s]
        if v[0] == "gamerule" and v[1] == "randomtickspeed":
            return [f"gamerule randomTickSpeed {v[2]}"]
        if v[:2] == ["execute", "at"]:
            n, rest = v[2], v[4:]
            if rest[0] == "summon":
                return [f"summon {rest[1].capitalize()} {n}"]
            if rest[0] == "fill":
                y1 = rel(rest[2]) if rest[2].startswith("~") else int(rest[2]) - 300
                y2 = rel(rest[5]) if rest[5].startswith("~") else int(rest[5]) - 300
                return [f"fillnear {n} {rel(rest[1])} {y1} {rel(rest[3])} {rel(rest[4])} {y2} {rel(rest[6])} {rest[7]}"]
            if rest[0] == "setblock":
                a = [rel(x) for x in rest[1:4]]
                return [f"fillnear {n} {a[0]} {a[1]} {a[2]} {a[0]} {a[1]} {a[2]} {rest[4]}"]
            return []
        if v[:2] == ["execute", "in"]:
            dim = {"nether": "nether", "the_end": "the_end", "overworld": "overworld"}[v[2]]
            return [f"dim {v[5]} {dim} {v[6]} {v[7]} {v[8]}"]
        return []


class PNX(Adapter):
    name = "pnx"
    port = 19702
    bot = BOT_2193X
    bot_extra = ("-fake-xuid",)
    tick_cmd = "time query gametime"
    ready_re = re.compile(r"Done \(")
    connected_re = re.compile(r"\] (\S+)\[/127\.0\.0\.1:\d+\] logged in")
    unsupported = {"waypoints"}
    main_thread = "main"

    def prepare(self):
        shutil.rmtree(self.dir / "worlds", ignore_errors=True)
        shutil.rmtree(self.dir / "players", ignore_errors=True)
        tpl = json.loads((CB / "scripts" / "pnx-world-config.json").read_text())
        for g in tpl["generators"].values():
            g["seed"] = 20261010
        (self.dir / "worlds" / "bench").mkdir(parents=True)
        (self.dir / "worlds" / "bench" / "config.json").write_text(json.dumps(tpl, indent=2))

    def argv(self):
        return [str(JAVA), "-jar", str(self.dir / "powernukkitx.jar")]

    def translate(self, s):
        if " loot spawn " in s:
            return []
        return [s]


class Dragonfly(Adapter):
    name = "dragonfly"
    port = 19712
    tick_cmd = "time query gametime"
    ready_re = re.compile(r"Server started\.")
    connected_re = re.compile(r"Player connected: (\S+)")
    status_cmd = None
    unsupported = {"waypoints", "summon", "cleanup", "tickspeed", "tickingarea"}

    def prepare(self):
        for d in ("world", "players"):
            shutil.rmtree(self.dir / d, ignore_errors=True)

    def argv(self):
        return [str(CB / "build" / "dfbench" / "dfbench.exe")]

    def translate(self, s):
        v = s.split()
        if v[0] in ("tp", "kill", "kick", "list", "gamemode", "weather", "effect"):
            return [s]
        if v[:2] == ["execute", "at"]:
            n, rest = v[2], v[4:]
            if rest[0] == "fill":
                y1 = rel(rest[2]) if rest[2].startswith("~") else int(rest[2]) - 300
                y2 = rel(rest[5]) if rest[5].startswith("~") else int(rest[5]) - 300
                return [f"fillnear {n} {rel(rest[1])} {y1} {rel(rest[3])} {rel(rest[4])} {y2} {rel(rest[6])} {rest[7]}"]
            if rest[0] == "setblock":
                a = [rel(x) for x in rest[1:4]]
                return [f"fillnear {n} {a[0]} {a[1]} {a[2]} {a[0]} {a[1]} {a[2]} {rest[4]}"]
            return []
        if v[:2] == ["execute", "in"]:
            return [f"dim {v[5]} {v[2]} {v[6]} {v[7]} {v[8]}"]
        return []


ADAPTERS = {a.name: a for a in (PMMP, Nukkit, PNX, Dragonfly)}


class Server:
    """Server process with piped stdin/stdout, a gametime probe parser and the chaos console pacing."""

    def __init__(self, ad, log_path, cpus, prio, pace=0.0):
        self.ad = ad
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.p = subprocess.Popen(ad.argv(), cwd=ad.dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                  creationflags=CREATE_SUSPENDED)
        self.bp = psutil.Process(self.p.pid)
        self.bp.cpu_affinity(cpus)
        self.bp.nice(prio)
        self.bp.resume()
        self.started = threading.Event()
        self.lines = []
        self.lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self.gametime = []
        self.connected = {}
        self.pace = pace
        self.high, self.normal = collections.deque(), collections.deque()
        self.queued = threading.Condition()
        self.sent = 0
        threading.Thread(target=self._pump, daemon=True).start()
        if pace:
            threading.Thread(target=self._writer, daemon=True).start()

    @property
    def pid(self):
        return self.p.pid

    def _pump(self):
        for line in self.p.stdout:
            line = ANSI.sub("", line)
            now = time.time()
            self.log.write(f"{time.strftime('%H:%M:%S')}.{int(now * 1000) % 1000:03d} {line}")
            self.log.flush()
            with self.lock:
                self.lines.append((now, line))
            if self.ad.ready_re.search(line):
                self.started.set()
            m = self.ad.tick_re.search(line)
            if m:
                self.gametime.append((time.perf_counter(), int(m.group(1))))
            if self.ad.connected_re:
                m = self.ad.connected_re.search(line)
                if m:
                    self.connected.setdefault(m.group(1), now)

    def enable_pacing(self, gap):
        if not self.pace and gap:
            self.pace = gap
            threading.Thread(target=self._writer, daemon=True).start()

    def _write(self, s):
        with self.cmd_lock:
            self.p.stdin.write(s + "\n")
            self.p.stdin.flush()
        self.sent += 1

    def cmd(self, s):
        if not self.pace:
            self._write(s)
            return
        with self.queued:
            (self.high if s == self.ad.tick_cmd else self.normal).append(s)
            self.queued.notify()

    def backlog(self):
        return len(self.normal) + len(self.high)

    def _gap(self):
        gt = self.gametime[-2:]
        tps = (gt[1][1] - gt[0][1]) / (gt[1][0] - gt[0][0]) if len(gt) == 2 and gt[1][0] > gt[0][0] else 20
        return max(self.pace, 1 / max(tps, 1))

    def _writer(self):
        while self.p.poll() is None:
            with self.queued:
                while not self.high and not self.normal:
                    self.queued.wait(1)
                    if self.p.poll() is not None:
                        return
                s = (self.high or self.normal).popleft()
            try:
                self._write(s)
            except OSError:
                return
            time.sleep(self._gap())

    def stop(self):
        try:
            if self.p.poll() is None:
                self._write("stop")
            self.p.wait(180)
        except Exception as e:
            log("stop failed", repr(e), "- killing our server")
            try:
                for c in self.bp.children(recursive=True):
                    c.kill()
                self.p.kill()
                self.p.wait(10)
            except Exception:
                pass
        self.log.close()


class TpsProbe(threading.Thread):
    def __init__(self, srv, every=5.0):
        super().__init__(daemon=True)
        self.srv, self.every = srv, every
        self.halt = threading.Event()

    def run(self):
        nxt = time.perf_counter()
        while not self.halt.is_set():
            self.srv.cmd(self.srv.ad.tick_cmd)
            nxt += self.every
            self.halt.wait(max(0.0, nxt - time.perf_counter()))


class MemProbe(threading.Thread):
    """Working set, private bytes, threads and process CPU every `every` seconds."""

    def __init__(self, bp, every=5.0):
        super().__init__(daemon=True)
        self.bp, self.every = bp, every
        self.series = []
        self.halt = threading.Event()

    def run(self):
        nxt = time.perf_counter()
        while not self.halt.is_set():
            try:
                mi = self.bp.memory_info()
                ct = self.bp.cpu_times()
                self.series.append((time.perf_counter(), mi.rss / 2 ** 20, mi.private / 2 ** 20,
                                    self.bp.num_threads(), ct.user + ct.system))
            except psutil.Error:
                return
            nxt += self.every
            self.halt.wait(max(0.0, nxt - time.perf_counter()))


def window_stats(mem, t0, t1):
    rows = [r for r in mem if t0 <= r[0] <= t1]
    if len(rows) < 2:
        return None
    cores = (rows[-1][4] - rows[0][4]) / (rows[-1][0] - rows[0][0])
    return {"process_cores": round(cores, 2), "ws_mb_max": round(max(r[1] for r in rows)),
            "ws_mb_end": round(rows[-1][1]), "private_mb_max": round(max(r[2] for r in rows)),
            "private_mb_end": round(rows[-1][2]), "threads_end": rows[-1][3]}


def java_main_tid(pid, name="main"):
    try:
        r = subprocess.run([str(JCMD), str(pid), "Thread.print"], capture_output=True, text=True, timeout=60)
    except Exception as e:
        return None, repr(e)
    for line in r.stdout.splitlines():
        if line.startswith(f'"{name}" '):
            m = re.search(r"nid=(0x[0-9a-fA-F]+|\d+)", line)
            if m:
                return int(m.group(1), 0), line.strip()
    return None, r.stdout[:400]


def java_diag(pid, out, tag, srv, log):
    """jstack x10 (3 s apart), GC.heap_info, jstat -gcutil, a status line for entity counts; tallies main's top frames."""
    d = out / f"diag-{tag}"
    d.mkdir(exist_ok=True)
    jbin = JCMD.parent
    srv.cmd("status")
    try:
        (d / "heap_info.txt").write_text(subprocess.run([str(JCMD), str(pid), "GC.heap_info"], capture_output=True,
                                                         text=True, timeout=60).stdout)
        js = subprocess.Popen([str(jbin / "jstat.exe"), "-gcutil", str(pid), "1000", "10"], stdout=open(d / "jstat.txt", "w"),
                              stderr=subprocess.STDOUT)
        tops = {}
        for i in range(10):
            txt = subprocess.run([str(jbin / "jstack.exe"), str(pid)], capture_output=True, text=True, timeout=60).stdout
            (d / f"jstack-{i}.txt").write_text(txt)
            blk = txt.split(chr(10) + '"main" ', 1)
            if len(blk) == 2:
                frames = [l.strip()[3:] for l in blk[1].split(chr(10) * 2, 1)[0].splitlines() if l.strip().startswith("at ")]
                for depth, key in ((1, "top1"), (3, "top3"), (8, "top8")):
                    if frames:
                        k = " <- ".join(f.split("(")[0] for f in frames[:depth])
                        tops.setdefault(key, {})
                        tops[key][k] = tops[key].get(k, 0) + 1
            time.sleep(3)
        js.wait(30)
        summary = {k: sorted(v.items(), key=lambda kv: -kv[1])[:8] for k, v in tops.items()}
        (d / "main_top_frames.json").write_text(json.dumps(summary, indent=1))
        log("diag", tag, json.dumps(summary.get("top3", [])[:5])[:900])
    except Exception as e:
        log("diag failed", repr(e))


def first_tid(pid):
    """The thread created first (the process main thread)."""
    import ctypes.wintypes as wt
    k32 = threads.k32
    best = None
    for t in psutil.Process(pid).threads():
        hnd = k32.OpenThread(0x0800, False, t.id)
        if not hnd:
            continue
        c, e, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        k32.GetThreadTimes(hnd, ctypes_byref(c), ctypes_byref(e), ctypes_byref(kt), ctypes_byref(ut))
        k32.CloseHandle(hnd)
        v = (c.dwHighDateTime << 32) | c.dwLowDateTime
        if best is None or v < best[0]:
            best = (v, t.id)
    return best[1] if best else None


def ctypes_byref(x):
    import ctypes
    return ctypes.byref(x)


def thread_ms(series, tid, t0, t1):
    pts = [(t, s) for t, s in series if t0 <= t <= t1]
    if tid is None or len(pts) < 2 or tid not in pts[0][1] or tid not in pts[-1][1]:
        return None
    return 1000 * (pts[-1][1][tid][1] - pts[0][1][tid][1]) / (pts[-1][0] - pts[0][0])


def top_threads(series, t0, t1, k=6):
    pts = [(t, s) for t, s in series if t0 <= t <= t1]
    if len(pts) < 2:
        return []
    (ta, a), (tb, b) = pts[0], pts[-1]
    rows = [(round(1000 * (b[tid][1] - a[tid][1]) / (tb - ta)), tid) for tid in b if tid in a]
    return sorted(rows, reverse=True)[:k]


class CChaos(Chaos):
    """chaos.Chaos with commands translated to the server's console syntax and its own main-thread probe."""

    def __init__(self, ad, main_tid, *a, **kw):
        super().__init__(*a, **kw)
        self.ad = ad
        self.main_tid = main_tid
        self.ACTIONS = [x for x in Chaos.ACTIONS if x[0] not in ad.unsupported]
        self.dropped = {}

    def cmd(self, s):
        out = self.ad.translate(s)
        if not out:
            k = " ".join(s.split()[:2]) if s.startswith("execute") else s.split()[0]
            self.dropped[k] = self.dropped.get(k, 0) + 1
        for c in out:
            self.srv.cmd(c)
            self.chaos_log.write(f"{time.strftime('%H:%M:%S')}   > {c}\n")

    def effects(self, name):
        for e, amp in EFFECTS:
            for c in self.ad.effect(name, e, amp):
                self.srv.cmd(c)

    def mc_ms_s(self, t0, t1):
        return thread_ms(list(self.sampler.series), self.main_tid, t0, t1)

    def summary(self):
        s = super().summary()
        s["dropped_unsupported_commands"] = self.dropped
        s["skipped_actions"] = sorted(self.ad.unsupported)
        (self.out / "chaos-summary.json").write_text(json.dumps(s, indent=1))
        return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, choices=sorted(ADAPTERS))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--min-settle", type=int, default=300)
    ap.add_argument("--max-settle", type=int, default=600)
    ap.add_argument("--settle-rate", type=float, default=0.1)
    ap.add_argument("--steady", type=int, default=90)
    ap.add_argument("--chaos", type=float, default=5, help="minutes of chaos after the steady window (0 = none)")
    ap.add_argument("--chaos-seed", type=int, default=None)
    ap.add_argument("--console-gap", type=float, default=50)
    ap.add_argument("--nudge", type=int, default=30)
    ap.add_argument("--bot-args", default="-csg")
    ap.add_argument("--server-cpus", default="0-15")
    ap.add_argument("--bot-cpus", default="16-27")
    ap.add_argument("--low", action="store_true", help="BelowNormal priority for server and bots (smoke tests)")
    ap.add_argument("--smoke", type=int, default=0, help="smoke test: seconds after all joined, then stop")
    ap.add_argument("--world-template", default=None, help="copy <dir>/worlds into the server before start")
    ap.add_argument("--extra-bot-args", default="", help="appended to the adapter's bot arguments")
    ap.add_argument("--diag", action="store_true", help="java servers: jstack/jstat/heap diagnostics in settle and steady")
    args = ap.parse_args()

    def cpus(s):
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))

    srv_cpus, bot_cpus = cpus(args.server_cpus), cpus(args.bot_cpus)
    h.E_CORES = bot_cpus
    ad = ADAPTERS[args.server]()
    h.PORT = ad.port
    out = CB / "results" / args.tag
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    prio = psutil.BELOW_NORMAL_PRIORITY_CLASS if args.low else psutil.ABOVE_NORMAL_PRIORITY_CLASS
    try:
        psutil.Process().cpu_affinity(bot_cpus)
    except psutil.Error:
        pass

    marks = {}
    res = {"args": vars(args), "server": ad.name, "port": ad.port, "bot": str(ad.bot), "marks": marks, "windows": {}}

    def mark(k):
        marks[k] = time.perf_counter()
        log("mark", k)

    srv = sampler = tps = mem = None
    b = None
    died = threading.Event()
    t0_wall = time.time()

    def nap(sec):
        if died.wait(sec):
            raise RuntimeError("server exited during the run")

    def watch():
        while True:
            if srv.p.poll() is not None:
                if not stopping.is_set():
                    res["died_at"] = time.time()
                    log("SERVER DIED rc", srv.p.returncode)
                    died.set()
                return
            time.sleep(0.5)

    stopping = threading.Event()
    try:
        ad.prepare()
        if args.world_template:
            for name in ("worlds", "world"):
                if (Path(args.world_template) / name).exists():
                    shutil.copytree(Path(args.world_template) / name, ad.dir / name, dirs_exist_ok=True)
            res["world_template"] = args.world_template
        srv = Server(ad, out / "server.log", srv_cpus, prio)
        if not srv.started.wait(300):
            raise RuntimeError("server did not start")
        threading.Thread(target=watch, daemon=True).start()
        bp = srv.bp
        res["pid"] = bp.pid
        with srv.lock:
            res["version_lines"] = [l.rstrip() for _, l in srv.lines
                                    if re.search(r"version|Version|running", l)][:8]
        log("server", ad.name, bp.pid, res["version_lines"][:3])
        table = threads.ThreadTable(bp.pid)
        sampler = threads.Sampler(table, 1.0)
        sampler.start()
        tps = TpsProbe(srv, 5.0)
        tps.start()
        mem = MemProbe(bp, 5.0)
        mem.start()
        nap(8)
        if ad.main_thread == "main":
            main_tid, info = java_main_tid(bp.pid)
        else:
            main_tid, info = first_tid(bp.pid), "first-created thread"
        res["main_tid"], res["main_tid_info"] = main_tid, info
        log("main thread", main_tid, str(info)[:160])

        nap(2)
        mark("idle0")
        nap(20)
        mark("idle1")

        def win(name, t0, t1):
            w = {"t0": t0, "t1": t1, "tps": h.tps_between(srv.gametime, t0, t1),
                 "main_ms_s": thread_ms(sampler.series, main_tid, t0, t1),
                 "top_threads_ms_s": top_threads(sampler.series, t0, t1), "proc": window_stats(mem.series, t0, t1)}
            res["windows"][name] = w
            log("window", name, "tps", w["tps"] and round(w["tps"], 2), "main", w["main_ms_s"] and round(w["main_ms_s"]),
                "top", w["top_threads_ms_s"][:5], "proc", w["proc"])
            h.save_json(out / "results.json", res)

        win("idle", marks["idle0"], marks["idle1"])
        chaos = None
        on_join_cmds = []

        def on_join(bots, name):
            x, z = h.cell(bots.index(name))
            for e, amp in EFFECTS:
                for c in ad.effect(name, e, amp):
                    srv.cmd(c)
            for c in ad.tp(name, x, 300, z):
                srv.cmd(c)
            on_join_cmds.append(name)

        mark("bots_start")
        dur = f"{int(args.max_settle + args.steady + args.chaos * 60 + 3600)}s"
        b = ChaosBots(ad.bot, args.n, "a", dur, out / "bots-a.log", srv, 0, on_join=on_join,
                      on_event=lambda *e: chaos and chaos.on_event(*e),
                      extra=[*args.bot_args.split(), *ad.bot_extra, *args.extra_bot_args.split(), "-control", "-rejoin-on-drop", "-radius-jitter", "20s",
                             "-jitter-paused"])
        try:
            psutil.Process(b.p.pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if args.low
                                         else psutil.NORMAL_PRIORITY_CLASS)
        except psutil.Error:
            pass
        if not wait(b.allj, 300, died):
            raise RuntimeError("bots did not join")
        mark("alljoined")
        res["joined_at_alljoined"] = len(b.joined)

        if args.smoke:
            nap(args.smoke)
            mark("settled")
            win("smoke", marks["alljoined"], marks["settled"])
        else:
            nudges = {}
            while True:
                nap(30)
                now = time.perf_counter()
                el = now - marks["alljoined"]
                ur = uniq_rate([b], 60)
                t30 = h.tps_between(srv.gametime, now - 35, now)
                views, airborne, nudged = [], 0, 0
                for bname, line in list(b.botlines.items()):
                    m = re.search(r"uniq=(\d+)", line)
                    p = re.search(r"pos=[-\d.]+,([-\d.]+),", line)
                    if m:
                        views.append(int(m.group(1)))
                    if p and float(p.group(1)) > 290:
                        airborne += 1
                        if args.nudge and el >= 60 and m and int(m.group(1)) < args.nudge:
                            x, z = h.cell(b.index(bname))
                            nudges[bname] = nudges.get(bname, 0) + 1
                            for c in ad.tp(bname, x + 16 * (nudges[bname] % 2), 300, z):
                                srv.cmd(c)
                            nudged += 1
                retp = [n for n, t in list(b.joined.items()) if n not in b.tp and now - t > 20]
                for n in retp:
                    on_join(b, n)
                if retp:
                    res.setdefault("retp", []).extend(retp)
                    log("re-teleported", retp)
                views.sort()
                vmin = views[0] if views else 0
                vmed = views[len(views) // 2] if views else 0
                main_now = thread_ms(sampler.series, main_tid, now - 30, now)
                log(f"settle {el:.0f}s uniq={total_uniq([b])} uniq/s={ur:.1f} ({ur / args.n:.2f}/bot) "
                    f"tps30={t30 and round(t30, 2)} view_min={vmin} view_med={vmed} airborne={airborne} "
                    f"deaths={len(b.deaths)} nudged={nudged} main={main_now and round(main_now)}ms/s "
                    f"ws={mem.series[-1][1]:.0f}MB")
                if args.diag and ad.main_thread == "main" and el >= 90 and "settle" not in res.setdefault("diag", []):
                    res["diag"].append("settle")
                    java_diag(bp.pid, out, "settle", srv, log)
                if el >= args.max_settle:
                    log("settle cap reached")
                    res["settle_capped"] = True
                    break
                if el >= args.min_settle and ur / args.n <= args.settle_rate:
                    break
            mark("settled")
            res["settle_s"] = marks["settled"] - marks["alljoined"]
            res["uniq_total"] = total_uniq([b])
            res["nudges"] = nudges
            win("storm_full", marks["bots_start"], marks["settled"])
            win("settle", marks["alljoined"], marks["settled"])
            pos = []
            for line in b.botlines.values():
                m = re.search(r"pos=([-\d.]+),([-\d.]+),([-\d.]+)", line)
                if m:
                    pos.append(tuple(float(x) for x in m.groups()))
            res["cells"] = h.verify_spread(pos, args.n)
            log("spread (bot-reported)", {k: res["cells"][k] for k in ("players", "cells_occupied", "max_per_cell",
                                                                         "worst_offset_blocks")})
            mark("unprof0")
            if args.diag and ad.main_thread == "main":
                t_d = time.perf_counter()
                java_diag(bp.pid, out, "steady", srv, log)
                nap(max(0, args.steady - (time.perf_counter() - t_d)))
            else:
                nap(args.steady)
            mark("unprof1")
            win("steady", marks["unprof0"], marks["unprof1"])
        if args.chaos:
            chaos = CChaos(ad, main_tid, srv, b, bp, sampler, table, out, args.n, args.chaos, died, log,
                           args.chaos_seed)
            chaos.online = set(b.joined)
            srv.enable_pacing(args.console_gap / 1000)
            mark("chaos0")
            res["chaos"] = chaos.run()
            mark("chaos1")
            win("chaos", marks["chaos0"], marks["chaos1"])
            log("chaos summary", json.dumps({k: v for k, v in res["chaos"].items()
                                             if k not in ("first_errors", "first_command_errors", "verify")}))
            for l in res["chaos"]["first_errors"]:
                log("  ERRLINE", l[:300])
        b.stop()
        res["bot_done"] = [line for _, line in b.stats[-1:]]
    except Exception as e:
        import traceback
        log("ABORT", repr(e))
        res["abort"] = repr(e)
        res["abort_tb"] = traceback.format_exc()
    finally:
        res["died_during_run"] = died.is_set()
        stopping.set()
        if b and b.p.poll() is None:
            b.p.kill()
        for s in (sampler, tps, mem):
            if s:
                s.halt.set()
        if srv:
            if died.is_set():
                with srv.lock:
                    (out / "crash.txt").write_text("\n".join(l.rstrip() for _, l in srv.lines[-150:]), encoding="utf-8")
            srv.stop()
            res["server_exit"] = srv.p.returncode
            log("server exit", srv.p.returncode)
            with srv.lock:
                res["log_errors"] = [l.rstrip() for _, l in srv.lines
                                     if re.search(r"\b(ERROR|CRITICAL|SEVERE|FATAL)\b|Exception|panic:", l)][:30]
            res["connected"] = srv.connected
            res["gametime"] = srv.gametime
        if mem:
            res["mem_series"] = mem.series
        if sampler:
            res["thread_series_main"] = [(t, s.get(res.get("main_tid"))) for t, s in sampler.series]
        h.save_json(out / "results.json", res)
        try:
            import csummary
            csummary.write_tps_csv(out)
            sm = csummary.summarize(out)
            (out / "summary.json").write_text(json.dumps(sm, indent=1))
            csummary.show(sm)
        except Exception as e:
            log("summary failed", repr(e))


def wait(ev, timeout, died):
    end = time.time() + timeout
    while time.time() < end:
        if ev.wait(0.5):
            return True
        if died.is_set():
            raise RuntimeError("server exited during the run")
    return False


if __name__ == "__main__":
    main()
