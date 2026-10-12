"""Endstone (BDS 1.26.52.3) under spread bots, with the runtime binaries taken from --arm.
Same phases as work-profile/scripts/run_ab.py: idle, join storm, settle with nudges, steady unprofiled, steady profiled.

  uv run --no-project --with psutil python -I run_endstone.py --tag before-100 --arm dll-before
  uv run --no-project --with psutil python -I run_endstone.py --tag vanilla-100 --vanilla
"""

import argparse
import collections
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

B = Path(__file__).resolve().parent
WP = B.parent / "work-profile"
sys.path.insert(0, str(WP / "scripts"))
sys.path.insert(0, str(B))
import harness as h  # noqa: E402
import psutil  # noqa: E402
import threads  # noqa: E402
from run2 import log, total_uniq, uniq_rate  # noqa: E402

import bench_metrics  # noqa: E402
import intest  # noqa: E402
from chaos import Chaos, ChaosBots  # noqa: E402

SERVER = B / "server"
VENV_PY = B / "venv" / "Scripts" / "python.exe"
PKG = B / "venv" / "Lib" / "site-packages" / "endstone"
PRISTINE = B / "dll-pypi"
SWAP = ["endstone_runtime.dll", "endstone_runtime.pdb", "_python.cp313-win_amd64.pyd", "_python.pdb"]
CFG = WP / "scripts" / "cpu4k.json"
PORT = 19672
VSD_STORM, VSD_STEADY = 81, 82
TARGET = {
    "server-name": "endstone-bench",
    "max-players": "200",
    "online-mode": "false",
    "allow-list": "false",
    "server-port": str(PORT),
    "server-portv6": str(PORT + 1),
    "transport": "raknet",
    "enable-lan-visibility": "false",
    "view-distance": "10",
    "tick-distance": "4",
    "max-threads": "8",
    "level-name": "bench",
    "level-seed": "20261010",
    "client-side-chunk-generation-enabled": "true",
    "server-build-radius-ratio": "Disabled",
}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

h.PORT = PORT

psapi = ctypes.WinDLL("psapi")
k32 = ctypes.WinDLL("kernel32")
user32 = ctypes.WinDLL("user32")
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenThread.restype = wt.HANDLE
k32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.SuspendThread.argtypes = [wt.HANDLE]
k32.ResumeThread.argtypes = [wt.HANDLE]
k32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("size", wt.DWORD), ("entry", ctypes.c_void_p)]


psapi.EnumProcessModules.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p), wt.DWORD, ctypes.POINTER(wt.DWORD)]
psapi.GetModuleInformation.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MODULEINFO), wt.DWORD]
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]


def modules(pid):
    """[(name, base, size)] for every module, exe first."""
    hp = k32.OpenProcess(0x0410, False, pid)
    try:
        arr = (ctypes.c_void_p * 1024)()
        need = wt.DWORD()
        psapi.EnumProcessModules(hp, arr, ctypes.sizeof(arr), ctypes.byref(need))
        out = []
        for i in range(need.value // ctypes.sizeof(ctypes.c_void_p)):
            mi = MODULEINFO()
            psapi.GetModuleInformation(hp, arr[i], ctypes.byref(mi), ctypes.sizeof(mi))
            buf = ctypes.create_unicode_buffer(260)
            psapi.GetModuleBaseNameW(hp, arr[i], buf, 260)
            out.append((buf.value, mi.base or 0, mi.size))
        return out
    finally:
        k32.CloseHandle(hp)


def where(addr, mods):
    for name, base, size in mods:
        if base <= addr < base + size:
            return name, addr - base
    return "?", addr


def devtools_tid(pid):
    """Thread that owns the hidden 'Endstone - DevTools' GLFW window."""
    found = []

    def cb(hwnd, _):
        p = wt.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == "Endstone - DevTools":
                found.append(tid)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def suspend_in(tid, mods, module, tries=5000):
    """Suspend a thread at a moment its RIP is inside `module`; returns (open handle or None, tries)."""
    th = k32.OpenThread(0x2 | 0x8 | 0x40, False, tid)
    if not th:
        return None, 0
    raw = ctypes.create_string_buffer(1232 + 16)
    addr = (ctypes.addressof(raw) + 15) & ~15
    for i in range(tries):
        ctypes.c_uint32.from_address(addr + 0x30).value = 0x100003
        if k32.SuspendThread(th) == 0xFFFFFFFF:
            break
        if k32.GetThreadContext(th, addr) and \
                where(ctypes.c_uint64.from_address(addr + 0xF8).value, mods)[0].lower() == module:
            return th, i + 1
        k32.ResumeThread(th)
        time.sleep(0.001)
    k32.CloseHandle(th)
    return None, tries


def sha256(path):
    d = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()


def our_bds():
    """bedrock_server.exe processes running from the bench server folder."""
    out = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info["name"] or "").lower() == "bedrock_server.exe" and p.info["exe"] and \
                    Path(p.info["exe"]).resolve().parent == SERVER.resolve():
                out.append(p)
        except (psutil.Error, OSError):
            pass
    return out


def install(arm):
    """Copy the arm's binaries into the venv's endstone package; files the arm lacks come from dll-pypi."""
    if our_bds():
        raise RuntimeError("a bedrock_server from the bench server folder is still running")
    got = {}
    for name in SWAP:
        src = arm / name if (arm / name).exists() else PRISTINE / name
        tmp = PKG / (name + ".swap")
        shutil.copyfile(src, tmp)
        os.replace(tmp, PKG / name)
        digest = sha256(PKG / name)
        if digest != sha256(src):
            raise RuntimeError(f"installed {name} does not match {src}")
        got[name] = {"from": str(src), "sha256": digest}
    return got


class Server:
    """endstone CLI -> bedrock_server.exe with the runtime injected; BDS inherits our stdin/stdout pipes.
    With vanilla, bedrock_server.exe is launched directly."""

    def __init__(self, log_path, pace=0.0, vanilla=False):
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        env["PYTHONSAFEPATH"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.p = subprocess.Popen(
            [str(SERVER / "bedrock_server.exe")] if vanilla else
            [str(VENV_PY), "-m", "endstone", "-y", "--no-interactive", "-s", str(SERVER)],
            cwd=SERVER if vanilla else B, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.started = threading.Event()
        self.lines = []
        self.lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self.gametime = []
        self.bds = None
        self.pace = pace
        self.high, self.normal = collections.deque(), collections.deque()
        self.queued = threading.Condition()
        self.sent = 0
        threading.Thread(target=self._pump, daemon=True).start()
        if pace:
            threading.Thread(target=self._writer, daemon=True).start()

    @property
    def pid(self):
        return self.bds.pid

    def _pump(self):
        for line in self.p.stdout:
            line = ANSI.sub("", line)
            now = time.time()
            self.log.write(f"{time.strftime('%H:%M:%S')}.{int(now * 1000) % 1000:03d} {line}")
            self.log.flush()
            with self.lock:
                self.lines.append((now, line))
            if "Server started." in line:
                self.started.set()
            m = re.search(r"(?i)gametime is (\d+)", line)
            if m:
                self.gametime.append((time.perf_counter(), int(m.group(1))))

    def find_bds(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            try:
                me = psutil.Process(self.p.pid)
                for c in [me, *me.children(recursive=True)]:
                    if c.name().lower() == "bedrock_server.exe":
                        self.bds = c
                        return c
            except psutil.Error:
                pass
            if self.p.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError("bedrock_server.exe child not found")

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
            (self.high if s.startswith("time query") else self.normal).append(s)
            self.queued.notify()

    def backlog(self):
        return len(self.normal) + len(self.high)

    def _gap(self):
        """At least one server tick at the current TPS, since Endstone drops console lines when BDS's queue is full."""
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
            self.p.wait(120)
        except Exception as e:
            log("stop failed", repr(e), "- killing our server")
            for p in ([self.bds] if self.bds else []):
                try:
                    p.kill()
                except psutil.Error:
                    pass
            try:
                self.p.kill()
                self.p.wait(10)
            except Exception:
                pass
        if self.bds is not None:
            try:
                self.bds.wait(30)
            except psutil.Error:
                pass
        self.log.close()


class ServerDied(RuntimeError):
    pass


class DeathWatch(threading.Thread):
    """Sets `died` if bedrock_server.exe exits before `stopping` is set."""

    def __init__(self, bp, srv):
        super().__init__(daemon=True)
        self.bp, self.srv = bp, srv
        self.died = threading.Event()
        self.stopping = threading.Event()
        self.at = None

    def run(self):
        while not self.stopping.is_set():
            if not self.bp.is_running() or self.srv.p.poll() is not None:
                if not self.stopping.is_set():
                    self.at = time.time()
                    self.died.set()
                    log("SERVER DIED", time.strftime("%H:%M:%S", time.localtime(self.at)))
                return
            self.stopping.wait(0.5)


def collect_crash(out, srv, t0_wall):
    """Last 100 log lines plus files written since the run started (outside worlds/) that look like crash output."""
    with srv.lock:
        tail = [l.rstrip() for _, l in srv.lines[-100:]]
    found = []
    roots = [SERVER, Path(os.environ.get("LOCALAPPDATA", "")) / "CrashDumps"]
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime >= t0_wall and "worlds" not in p.parts and \
                        (p.suffix.lower() in (".dmp", ".mdmp", ".envelope", ".txt", ".json")
                         or "crash" in str(p).lower() or ".sentry-native" in p.parts):
                    found.append(str(p))
            except OSError:
                pass
    (out / "crash.txt").write_text("files:\n" + "\n".join(found) + "\n\nlast 100 log lines:\n" + "\n".join(tail),
                                   encoding="utf-8")
    return found, tail


PREFIX = re.compile(r"^\[(\d{4}-\d\d-\d\d )?\d\d:\d\d:\d\d(:\d{3})? \w+\]:? ?")


def query_positions(srv, wait=3.0):
    """h.query_positions with Endstone's per-line '[hh:mm:ss LEVEL]: ' or vanilla's '[date hh:mm:ss:ms LEVEL] '
    prefix stripped."""
    with srv.lock:
        start = len(srv.lines)
    srv.cmd("querytarget @a")
    time.sleep(wait)
    with srv.lock:
        txt = "".join(PREFIX.sub("", l) for _, l in srv.lines[start:])
    i = txt.find("Target data:")
    if i < 0:
        return []
    j = txt.find("[", i)
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
    except Exception:
        return []
    return [(e["position"]["x"], e["position"]["y"], e["position"]["z"]) for e in data]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--arm", help="directory holding endstone_runtime.dll (+ .pdb, _python pyd)")
    ap.add_argument("--vanilla", action="store_true", help="run bedrock_server.exe directly, without Endstone")
    ap.add_argument("--n", type=int, default=None, help="bots (default 100, or 2 with --intest)")
    ap.add_argument("--storm-prof", type=int, default=90)
    ap.add_argument("--steady-unprof", type=int, default=90)
    ap.add_argument("--steady-prof", type=int, default=75)
    ap.add_argument("--min-settle", type=int, default=180)
    ap.add_argument("--max-settle", type=int, default=2400)
    ap.add_argument("--settle-rate", type=float, default=0.1)
    ap.add_argument("--min-view", type=int, default=0, help="settle only once every bot has this many unique chunks")
    ap.add_argument("--prop", action="append", default=[], help="server.properties override, key=value")
    ap.add_argument("--bot-args", default="-csg", help="extra loadbot arguments")
    ap.add_argument("--nudge", type=int, default=30, help="re-teleport hovering bots with fewer unique chunks than this")
    ap.add_argument("--no-prof", action="store_true", help="skip the VS collector; windows are still measured")
    ap.add_argument("--devtools", choices=["suspend", "run"], default="run",
                    help="suspend the hidden DevTools render thread (needed before its fix, where it spins a core)")
    ap.add_argument("--chaos", type=float, default=0, help="minutes of chaos after settle instead of the steady windows")
    ap.add_argument("--chaos-seed", type=int, default=None)
    ap.add_argument("--console-gap", type=float, default=50, help="chaos: min ms between console commands")
    ap.add_argument("--intest", action="store_true", help="run the endstone_test plugin's in-server suites")
    ap.add_argument("--intest-cmd", default="/test;/test sender;/test broadcast",
                    help="intest: commands a00 sends as a player after it is opped")
    ap.add_argument("--intest-wait", type=float, default=60, help="intest: seconds per join round")
    args = ap.parse_args()
    if args.vanilla and args.intest:
        ap.error("--intest needs Endstone")
    if not args.vanilla and not args.arm:
        ap.error("--arm is required without --vanilla")
    if args.n is None:
        args.n = 2 if args.intest else 100
    global VENV_PY, PKG
    if args.intest:
        VENV_PY = B / "venv-intest" / "Scripts" / "python.exe"
        PKG = B / "venv-intest" / "Lib" / "site-packages" / "endstone"

    arm = None
    if not args.vanilla:
        arm = Path(args.arm)
        if not arm.is_absolute():
            arm = B / arm
        if not (arm / "endstone_runtime.dll").exists():
            raise SystemExit(f"{arm} has no endstone_runtime.dll")
    if our_bds():
        raise SystemExit("a bedrock_server from the bench server folder is already running")

    out = B / "results" / args.tag
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    psutil.Process().cpu_affinity(h.E_CORES)

    props = SERVER / "server.properties"
    props_orig = props.read_text(encoding="utf-8")
    text = props_orig
    overrides = dict(TARGET)
    for kv in args.prop:
        k, v = kv.split("=", 1)
        overrides[k] = v
    for k, v in overrides.items():
        text, n = re.subn(rf"(?m)^{re.escape(k)}=.*$", lambda _m: f"{k}={v}", text)
        if not n:
            text += f"\n{k}={v}\n"
    level = overrides["level-name"]
    shutil.rmtree(SERVER / "worlds" / level, ignore_errors=True)

    marks = {}
    res = {"args": vars(args), "arm": arm and str(arm), "props": overrides, "marks": marks, "windows": {},
           "tsc_hz": h.TSC_HZ, "cells": {}, "profiled": not args.no_prof}

    def mark(k):
        marks[k] = time.perf_counter()
        log("mark", k)

    srv = None
    bots_list = []
    sampler = mon = tps = table = watch = None
    dt_handle = None
    chaos = None
    t0_wall = time.time()

    def nap(sec):
        if watch and watch.died.wait(sec):
            raise ServerDied("bedrock_server.exe exited during the run")
        if not watch:
            time.sleep(sec)

    def wait_event(ev, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if ev.wait(0.5):
                return True
            if watch and watch.died.is_set():
                raise ServerDied("bedrock_server.exe exited during the run")
        return False

    try:
        if arm:
            res["installed"] = install(arm)
            log("arm", arm.name, {k: v["sha256"][:16] + (" (pypi)" if Path(v["from"]).parent == PRISTINE else "")
                                  for k, v in res["installed"].items()})
        props.write_text(text, encoding="utf-8")
        srv = Server(out / "bds.log", pace=args.console_gap / 1000 if args.chaos else 0, vanilla=args.vanilla)
        if not srv.started.wait(300):
            raise RuntimeError("server did not start")
        bp = srv.find_bds(10)
        bp.cpu_affinity(h.P_CORES)
        bp.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        if srv.p.pid != bp.pid:
            psutil.Process(srv.p.pid).cpu_affinity(h.E_CORES)
        watch = DeathWatch(bp, srv)
        watch.start()
        res["pid"] = bp.pid
        res["cli_pid"] = srv.p.pid
        mods = modules(bp.pid)
        res["modules"] = {name: hex(base) for name, base, _ in mods
                          if name.lower().startswith(("endstone", "python", "_python", "bedrock"))}
        with srv.lock:
            res["endstone_lines"] = [l.rstrip() for _, l in srv.lines if "Endstone" in l or "Version:" in l][:10]
        log("server", bp.pid, hex(h.exe_base(bp.pid)), res["endstone_lines"][:3])
        table = threads.ThreadTable(bp.pid)
        sampler = threads.Sampler(table, 1.0)
        sampler.start()
        own = {srv.p.pid, bp.pid}
        try:
            own.update(c.pid for c in bp.children(recursive=True))
        except psutil.Error:
            pass
        mon = h.MachineMon(lambda: own)
        mon.start()
        tps = h.TpsProbe(srv, 5.0)
        tps.start()

        dt = None
        for _ in range(0 if args.vanilla else 60):
            dt = devtools_tid(bp.pid)
            if dt:
                break
            nap(0.5)
        res["devtools_tid"] = dt
        if dt:
            a = table.snapshot()
            nap(3)
            b_ = table.snapshot()
            res["devtools_before_ms_s"] = 1000 * (b_[dt][0] - a[dt][0]) / h.TSC_HZ / 3 if dt in a and dt in b_ else None
            if args.devtools == "suspend":
                dt_handle, tries = suspend_in(dt, modules(bp.pid), "endstone_runtime.dll")
                res["devtools_suspended"] = dt_handle is not None
                res["devtools_suspend_tries"] = tries
                if not dt_handle:
                    log("WARNING could not suspend the DevTools thread; it keeps running")
        log("devtools tid", dt, f"{res.get('devtools_before_ms_s') or 0:.0f} ms/s", "suspended",
            res.get("devtools_suspended"))

        if args.intest:
            nap(5)
            mark("bots_start")
            b = ChaosBots(B / "loadbot-chaos.exe", args.n, "a", "900s", out / "bots-a.log", srv, 0,
                          on_join=lambda _b, name: name == "a00" and srv.cmd("op a00"),
                          extra=[*args.bot_args.split(), "-control", "-cmd", args.intest_cmd, "-cmd-delay", "10s"])
            bots_list.append(b)
            own.add(b.p.pid)
            res["intest"] = intest.run(srv, b, out, log, nap, wait_event, args.intest_wait)
            return

        def labels():
            ms = modules(bp.pid)
            out_ = {}
            for tid in list(table.names):
                name = table.names.get(tid) or ""
                if tid == dt:
                    name = "DevTools"
                elif not name:
                    name = f"{tid}@{where(table.starts.get(tid, 0), ms)[0]}"
                out_[tid] = name
            return out_

        nap(10)
        mark("idle0")
        nap(20)
        mark("idle1")
        res["thread_names_start"] = dict(table.names)
        names0 = set(table.names.values())
        log("thread names", sorted(n for n in names0 if n))
        if "MC_SERVER" not in names0:
            log("WARNING no MC_SERVER thread")

        def foreign():
            return [(p.pid, p.info["name"]) for p in psutil.process_iter(["name"])
                    if (p.info["name"] or "").lower().startswith(("bedrock_server", "loadbot")) and p.pid not in own]

        def save():
            res["thread_names"] = dict(table.names)
            h.save_json(out / "results.json", res)

        def window(name, t0, t1):
            fb = foreign()
            if fb:
                log("WARNING foreign BDS/bots running", fb)
            lab = labels()
            mi = bp.memory_info()
            w = {"foreign_bds": fb, "t0": t0, "t1": t1, "tps": h.tps_between(srv.gametime, t0, t1),
                 "threads": h.thread_summary(sampler.series, lab, t0, t1),
                 "other_load": h.other_load(mon, t0, t1, own),
                 "mem_mb": {"ws": round(mi.rss / 2 ** 20, 1), "private": round(mi.private / 2 ** 20, 1),
                            "peak_ws": round(mi.peak_wset / 2 ** 20, 1)}}
            res["windows"][name] = w
            th = w["threads"]["threads"][:8] if w["threads"] else []
            dtr = next((r for r in (w["threads"] or {}).get("threads", []) if r["name"] == "DevTools"), None)
            log("window", name, "tps", w["tps"], [(r["name"] or r["tid"], round(r["ms_per_s"])) for r in th],
                "other", w["other_load"] and round(w["other_load"]["pcore_ms_s"]),
                "devtools", dtr and round(dtr["ms_per_s"]), "mem", w["mem_mb"])
            save()

        window("idle", marks["idle0"], marks["idle1"])

        if not args.no_prof:
            log(h.vsd_start(VSD_STORM, bp.pid, CFG).splitlines()[-1:])
        mark("prof_start")
        nap(12)
        mark("bots_start")
        if args.chaos:
            dur = f"{int(args.max_settle + args.chaos * 60 + 3600)}s"
            b = ChaosBots(B / "loadbot-chaos.exe", args.n, "a", dur, out / "bots-a.log", srv, 0,
                          on_join=h.tp_to_cell(srv), on_event=lambda *e: chaos and chaos.on_event(*e),
                          extra=[*args.bot_args.split(), "-control", "-rejoin-on-drop", "-radius-jitter", "20s",
                                 "-jitter-paused"])
        else:
            b = h.Bots(args.n, "a", "7200s", out / "bots-a.log", srv, 0, on_join=h.tp_to_cell(srv),
                       extra=args.bot_args.split())
        bots_list.append(b)
        own.add(b.p.pid)
        if not wait_event(b.allj, 300):
            raise RuntimeError("bots did not join")
        mark("alljoined")
        rem = args.storm_prof - (time.perf_counter() - marks["bots_start"])
        if rem > 0:
            nap(rem)
        mark("storm_prof_end")
        if not args.no_prof:
            log(h.vsd_stop(VSD_STORM, out / "prof-storm.diagsession").splitlines()[-2:])
        window("storm_prof", marks["bots_start"], marks["storm_prof_end"])

        nudges = {}
        while True:
            nap(30)
            now = time.perf_counter()
            el = now - marks["alljoined"]
            ur = uniq_rate(bots_list, 60)
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
                        srv.cmd(f"tp {bname} {x + 16 * (nudges[bname] % 2)} 300 {z}")
                        nudged += 1
            retp = [n for n, t in list(b.joined.items()) if n not in b.tp and now - t > 20]
            for n in retp:
                h.tp_to_cell(srv)(b, n)
            if retp:
                res.setdefault("retp", []).extend(retp)
                log("re-teleported", retp)
            views.sort()
            vmin = views[0] if views else 0
            vmed = views[len(views) // 2] if views else 0
            log(f"settle {el:.0f}s uniq={total_uniq(bots_list)} uniq/s={ur:.1f} ({ur / args.n:.2f}/bot) tps30={t30} "
                f"view_min={vmin} view_med={vmed} below={sum(v < args.min_view for v in views)} airborne={airborne} "
                f"deaths={sum(len(x.deaths) for x in bots_list)} nudged={nudged}")
            if el >= args.max_settle:
                log("settle cap reached")
                break
            if args.min_view:
                if el >= args.min_settle and len(views) == args.n and vmin >= args.min_view:
                    break
            elif el >= args.min_settle and ur / args.n <= args.settle_rate:
                break
        mark("settled")
        res["settle_s"] = marks["settled"] - marks["alljoined"]
        res["uniq_total"] = total_uniq(bots_list)
        res["nudges"] = nudges
        window("storm_full", marks["bots_start"], marks["settled"])
        for e in h.EFFECTS:
            srv.cmd(f"effect @a {e}")
        pos = query_positions(srv, wait=15)
        res["cells"] = h.verify_spread(pos, args.n)
        log("spread", {k: res["cells"][k] for k in ("players", "cells_occupied", "max_per_cell", "worst_offset_blocks")})

        if args.chaos:
            chaos = Chaos(srv, b, bp, sampler, table, out, args.n, args.chaos, watch.died, log, args.chaos_seed)
            chaos.online = set(b.joined)
            mark("chaos0")
            res["chaos"] = chaos.run()
            mark("chaos1")
            if not watch.died.is_set():
                window("chaos", marks["chaos0"], marks["chaos1"])
            brief = {k: v for k, v in res["chaos"].items()
                     if k not in ("first_errors", "first_command_errors", "verify")}
            log("chaos summary", json.dumps(brief))
            bench_metrics.show_verify(res["chaos"]["verify"])
            for l in res["chaos"]["first_errors"]:
                log("  ERRLINE", l[:300])
            if watch.died.is_set():
                raise ServerDied("bedrock_server.exe exited during chaos")
        else:
            mark("unprof0")
            nap(args.steady_unprof)
            mark("unprof1")
            window("steady_unprof", marks["unprof0"], marks["unprof1"])
            if args.steady_prof > 0:
                if not args.no_prof:
                    log(h.vsd_start(VSD_STEADY, bp.pid, CFG).splitlines()[-1:])
                mark("sprof_start")
                nap(12)
                mark("sprof0")
                nap(args.steady_prof)
                mark("sprof1")
                if not args.no_prof:
                    log(h.vsd_stop(VSD_STEADY, out / "prof-steady.diagsession").splitlines()[-2:])
                window("steady_prof", marks["sprof0"], marks["sprof1"])
        for x in bots_list:
            x.stop()
        res["bot_done"] = [line for _, line in b.stats[-1:]]
        save()
    except ServerDied as e:
        log("ABORT", e)
    finally:
        died = bool(watch and watch.died.is_set())
        if watch:
            watch.stopping.set()
        res["died_during_run"] = died
        res["died_at"] = watch.at if watch else None
        if died:
            files, tail = collect_crash(out, srv, t0_wall)
            res["crash_files"] = files
            log("crash files", files)
            log("last log lines:")
            for l in tail[-100:]:
                print("   ", l, flush=True)
        for x in bots_list:
            if x.p.poll() is None:
                x.p.kill()
        for s in (sampler, mon, tps):
            if s:
                s.halt.set()
        if dt_handle:
            k32.ResumeThread(dt_handle)
            k32.CloseHandle(dt_handle)
        if srv:
            srv.stop()
            res["server_exit"] = srv.p.returncode
            log("server exit", srv.p.returncode)
            with srv.lock:
                res["log_errors"] = [l.rstrip() for _, l in srv.lines if re.search(r"\b(ERROR|CRITICAL)\b", l)][:20]
        props.write_text(props_orig, encoding="utf-8")
        if arm:
            try:
                install(PRISTINE)
                log("restored dll-pypi")
            except Exception as e:
                log("restore failed", repr(e))
        if table:
            res["thread_series"] = [(t, {str(k): v for k, v in snap.items()}) for t, snap in sampler.series]
            res["gametime"] = srv.gametime
            h.save_json(out / "results.json", res)
            if not args.intest:
                try:
                    bench_metrics.write_tps_csv(out)
                    bench_metrics.show(bench_metrics.summarize(out))
                except Exception as e:
                    log("metrics failed", repr(e))

if __name__ == "__main__":
    main()
