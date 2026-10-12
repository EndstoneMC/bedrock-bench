"""Chaos phase for run_endstone.py: randomised churn, stacking, dimension hops, kills, mobs, block edits and
gamerule cycling against the patched Endstone paths, with a monitor line every 10 s."""

import heapq
import json
import random
import re
import signal
import statistics
import subprocess
import threading
import time
from pathlib import Path

import harness as h
import psutil

import bench_metrics

ERR = re.compile(r"\b(ERROR|CRITICAL)\b|exception|assert|crash", re.I)
CMD_FEEDBACK = re.compile(r"Syntax error|No targets matched|Unknown command|is not a valid|Invalid |not found|"
                          r"Could not|Cannot |out of the world|not loaded|No ticking area|already exists|"
                          r"Too many|commands\.", re.I)
EVENT_KINDS = {"JOINED", "LEFT", "DIED", "RESPAWNED", "DIM", "DIMDONE", "RADIUS", "TP", "CMD", "CMDOUT"}
MOBS = ["zombie", "cow", "chicken", "pig", "skeleton", "sheep"]
LOOT = ["entities/zombie", "entities/cow", "entities/chicken", "entities/skeleton"]
TICKSPEED = [0, 1, 3, 100, 1000, 1]
WEATHER = ["thunder", "clear", "rain", "clear"]


class ChaosBots(h.Bots):
    """h.Bots with a stdin control pipe, a chosen loadbot binary and an event hook for chaos bookkeeping."""

    def __init__(self, exe, n, prefix, dur, logpath, srv, offset, stagger="300ms", on_join=None, on_event=None,
                 extra=()):
        self.n = n
        self.prefix = prefix
        self.offset = offset
        self.srv = srv
        self.log = open(logpath, "w", encoding="utf-8")
        self.joined = {}
        self.tp = {}
        self.deaths = []
        self.stats = []
        self.botlines = {}
        self.allj = threading.Event()
        self.done = threading.Event()
        self.on_join = on_join
        self.on_event = on_event
        self.stdin_lock = threading.Lock()
        self.p = subprocess.Popen([str(exe), "-addr", f"127.0.0.1:{h.PORT}", "-n", str(n), "-prefix", prefix,
                                   "-d", dur, "-stagger", stagger, *extra],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  bufsize=1, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        psutil.Process(self.p.pid).cpu_affinity(h.E_CORES)
        threading.Thread(target=self._pump, daemon=True).start()

    def send(self, line):
        with self.stdin_lock:
            try:
                self.p.stdin.write(line + "\n")
                self.p.stdin.flush()
            except (OSError, ValueError):
                pass

    def stop(self, timeout=30):
        try:
            self.p.send_signal(signal.CTRL_BREAK_EVENT)
            self.done.wait(timeout)
            self.p.wait(timeout)
        except Exception:
            self.p.kill()

    def _pump(self):
        for line in self.p.stdout:
            self.log.write(line)
            self.log.flush()
            parts = line.split()
            if len(parts) < 2:
                continue
            kind = parts[1]
            if kind == "JOINED" and len(parts) >= 3:
                self.joined.setdefault(parts[2], time.perf_counter())
                if self.on_join:
                    try:
                        self.on_join(self, parts[2])
                    except Exception as e:
                        print(f"on_join failed: {e!r}", flush=True)
            elif kind == "TP" and len(parts) >= 6:
                self.tp.setdefault(parts[2], []).append((time.perf_counter(), tuple(float(x) for x in parts[3:6])))
            elif kind == "DIED" and len(parts) >= 3:
                self.deaths.append(parts[2])
            elif kind == "ALLJOINED":
                self.allj.set()
            elif kind == "STAT":
                self.stats.append((time.perf_counter(), line.strip()))
            elif kind == "BOT" and len(parts) >= 3:
                self.botlines[parts[2]] = line.strip()
            elif kind == "DONE":
                self.done.set()
            if self.on_event:
                try:
                    if len(parts) >= 4 and parts[2] in ("READ", "REJOIN", "DIAL", "SPAWN"):
                        self.on_event(f"{parts[2]}_{parts[3].rstrip(':')}", parts[1], line.strip())
                    elif len(parts) >= 3 and kind in EVENT_KINDS:
                        self.on_event(kind, parts[2], line.strip())
                except Exception as e:
                    print(f"on_event failed: {e!r}", flush=True)


def slope_per_h(pts):
    """Least-squares slope of [(t_s, v)] in units per hour."""
    if len(pts) < 3:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return sum((t - mt) * (v - mv) for t, v in pts) / den * 3600 if den else None


class Chaos:
    def __init__(self, srv, bots, bp, sampler, table, out, n, minutes, died, log, seed=None):
        self.srv, self.bots, self.bp, self.sampler, self.table = srv, bots, bp, sampler, table
        self.out, self.n, self.minutes, self.died, self.log = Path(out), n, minutes, died, log
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.timers = []
        self.seq = 0
        self.online = {f"{bots.prefix}{i:02d}" for i in range(n)}
        self.away = set()
        self.flash = set()
        self.killed = {}
        self.counts = {}
        self.events = {}
        self.mon = []
        self.cmd_errors = []
        self.harness_errors = []
        self.err_lines = []
        self.err_idx = 0
        self.verify = bench_metrics.VerifyScan()
        self.n_mon = 0
        self.tick_i = 0
        self.weather_i = 0
        self.area_i = 0
        self.areas = []
        self.chaos_log = open(self.out / "chaos.log", "w", encoding="utf-8")
        self.mon_csv = open(self.out / "chaos-mon.csv", "w", encoding="utf-8")
        self.mon_csv.write("t_s,tps10,mc_ms_s,ws_mb,private_mb,handles,threads,online,errors,backlog\n")

    # ------------------------------------------------------------ helpers

    def note(self, action, msg):
        self.counts[action] = self.counts.get(action, 0) + 1
        line = f"{time.strftime('%H:%M:%S')} CHAOS {action} {msg}"
        self.chaos_log.write(line + "\n")
        self.chaos_log.flush()
        self.log(f"CHAOS {action} {msg}")

    def cmd(self, s):
        self.srv.cmd(s)
        self.chaos_log.write(f"{time.strftime('%H:%M:%S')}   > {s}\n")

    def later(self, delay, fn, *a):
        with self.lock:
            self.seq += 1
            heapq.heappush(self.timers, (time.perf_counter() + delay, self.seq, fn, a))

    def k(self, x):
        return max(1, min(self.n, round(x * self.n / 100)))

    def pick(self, k, pool=None):
        with self.lock:
            pool = sorted(pool if pool is not None else self.online)
        return self.rng.sample(pool, min(k, len(pool)))

    def home(self, name):
        return h.cell(self.bots.index(name))

    def far(self):
        return h.cell(self.n + self.rng.randrange(400))

    def send_home(self, name):
        x, z = self.home(name)
        self.cmd(f"tp {name} {x} 300 {z}")

    def effects(self, name):
        for e in h.EFFECTS:
            self.cmd(f"effect {name} {e}")

    # ------------------------------------------------------------ bot events

    def on_event(self, kind, name, line):
        with self.lock:
            self.events[kind] = self.events.get(kind, 0) + 1
            if kind == "JOINED":
                self.online.add(name)
                self.away.discard(name)
                flash = "rejoin=true" in line and name in self.flash
                if flash:
                    self.flash.discard(name)
            elif kind in ("LEFT", "READ_FAILED", "REJOIN_FAILED"):
                self.online.discard(name)
                flash = False
            else:
                flash = False
        if kind in ("JOINED", "LEFT", "READ_FAILED", "REJOIN_FAILED", "DIED", "RESPAWNED", "DIM", "DIMDONE"):
            self.chaos_log.write(f"{time.strftime('%H:%M:%S')}   bot {line}\n")
        if flash:
            d = self.rng.uniform(1, 2)
            self.later(d, self._flash, name, d)
        if kind == "RESPAWNED":
            with self.lock:
                self.killed.pop(name, None)
            self.later(2, self._after_respawn, name)

    def _flash(self, name, d):
        self.note("flash_leave", f"{name} {d:.1f}s after rejoin")
        self.bots.send(f"reconnect {name}")

    def _after_respawn(self, name):
        self.effects(name)
        self.send_home(name)

    # ------------------------------------------------------------ actions

    def a_churn(self):
        names = self.pick(self.k(10))
        flash = [n for n in names if self.rng.random() < 0.3]
        with self.lock:
            self.flash.update(flash)
        for n in names:
            self.bots.send(f"reconnect {n}")
        self.note("churn", f"{len(names)} reconnects {names} flash={flash}")

    def a_stack(self):
        names = self.pick(max(2, self.k(self.rng.randint(5, 10))))
        x, z = self.far()
        for n in names:
            self.cmd(f"tp {n} {x}.5 300 {z}.5")
        self.note("stack", f"{len(names)} bots on {x}.5 300 {z}.5 {names}")
        self.later(self.rng.uniform(15, 30), self._unstack, names)

    def _unstack(self, names):
        for n in names:
            self.send_home(n)
        self.note("unstack", f"{len(names)} bots home")

    def a_waypoints(self):
        self.cmd("gamerule playerwaypoints off")
        self.cmd("gamerule locatorbar false")
        self.note("waypoints_off", "playerwaypoints off, locatorbar false")
        self.later(self.rng.uniform(10, 20), self._waypoints_on)

    def _waypoints_on(self):
        self.cmd("gamerule playerwaypoints everyone")
        self.cmd("gamerule locatorbar true")
        self.note("waypoints_on", "playerwaypoints everyone, locatorbar true")

    def a_gamemode(self):
        names = self.pick(self.rng.randint(1, 3))
        mode = self.rng.choice(["spectator", "spectator", "creative", "adventure"])
        for n in names:
            self.cmd(f"gamemode {mode} {n}")
        self.note("gamemode", f"{mode} {names}")
        self.later(self.rng.uniform(10, 25), self._survival, names)

    def _survival(self, names):
        for n in names:
            self.cmd(f"gamemode survival {n}")
        self.note("gamemode", f"survival {names}")

    def a_kill(self):
        names = self.pick(self.k(2))
        now = time.perf_counter()
        for n in names:
            with self.lock:
                self.killed[n] = now
            self.cmd(f"effect {n} clear")
            self.cmd(f"kill {n}")
        self.note("kill", f"{names}")

    def a_summon(self):
        names = self.pick(self.rng.randint(1, 3))
        if not names:
            return
        total = self.rng.randint(20, 50)
        for i in range(total):
            n = names[i % len(names)]
            mob = self.rng.choice(MOBS)
            self.cmd(f"execute at {n} run summon {mob} ~{self.rng.randint(-6, 6)} ~1 ~{self.rng.randint(-6, 6)}")
        for n in names:
            for _ in range(self.rng.randint(2, 4)):
                self.cmd(f'execute at {n} run loot spawn ~ ~2 ~ loot "{self.rng.choice(LOOT)}"')
        self.note("summon", f"{total} mobs + loot near {names}")
        for n in names:
            if self.rng.random() < 0.5:
                self.later(self.rng.uniform(2, 8), self._churn_one, n, "near mobs")

    def _churn_one(self, name, why):
        self.bots.send(f"reconnect {name}")
        self.note("churn_one", f"{name} ({why})")

    def a_cleanup(self):
        self.cmd("kill @e[type=!player]")
        self.note("cleanup", "kill @e[type=!player]")

    def a_kick(self):
        names = self.pick(self.rng.randint(1, 2))
        for n in names:
            self.cmd(f"kick {n} chaos")
        self.note("kick", f"{names}")

    def a_dimension(self):
        with self.lock:
            pool = self.online - self.away
        names = self.pick(self.rng.randint(1, 3), pool)
        for n in names:
            x, z = self.home(n)
            if self.rng.random() < 0.6:
                self.cmd(f"execute in nether run tp {n} {x // 8} 100 {z // 8}")
                where = "nether"
            else:
                self.cmd(f"execute in the_end run tp {n} {self.rng.randint(-40, 40)} 80 {self.rng.randint(-40, 40)}")
                where = "the_end"
            with self.lock:
                self.away.add(n)
            self.later(self.rng.uniform(15, 30), self._overworld, n)
            self.note("dimension", f"{n} -> {where}")

    def _overworld(self, name):
        x, z = self.home(name)
        self.cmd(f"execute in overworld run tp {name} {x} 300 {z}")
        with self.lock:
            self.away.discard(name)
        self.note("dimension", f"{name} -> overworld")

    def a_list(self):
        self.cmd("list")
        self.cmd("status")
        self.counts["list_status"] = self.counts.get("list_status", 0) + 1

    def a_far_tp(self):
        names = self.pick(self.rng.randint(3, 5))
        for n in names:
            x, z = self.far()
            self.cmd(f"tp {n} {x} 300 {z}")
        self.note("far_tp", f"{names}")

    def a_view_disconnect(self):
        names = self.pick(1)
        for n in names:
            x, z = self.far()
            self.cmd(f"tp {n} {x} 300 {z}")
            d = self.rng.uniform(1, 3)
            self.later(d, self._churn_one, n, f"view loading, {d:.1f}s after far tp")
        self.note("view_disconnect", f"{names}")

    def a_tickspeed(self):
        v = TICKSPEED[self.tick_i % len(TICKSPEED)]
        self.tick_i += 1
        self.cmd(f"gamerule randomtickspeed {v}")
        self.note("randomtickspeed", str(v))

    def a_weather(self):
        w = WEATHER[self.weather_i % len(WEATHER)]
        self.weather_i += 1
        self.cmd(f"weather {w}")
        self.note("weather", w)

    def a_blocks(self):
        with self.lock:
            pool = self.online - self.away
        names = self.pick(self.rng.randint(1, 2), pool)
        edits = [
            "fill ~-5 ~-1 ~-5 ~5 ~-1 ~5 grass_block",
            "fill ~-3 ~-1 ~-3 ~3 ~-1 ~3 farmland",
            "fill ~-4 ~4 ~-4 ~4 ~5 ~4 oak_leaves",
            "fill ~-2 ~ ~-2 ~2 ~ ~2 oak_sapling",
            "fill ~-3 315 ~-3 ~3 318 ~3 oak_leaves",
            "setblock ~1 ~-1 ~1 grass_block",
        ]
        for n in names:
            for e in self.rng.sample(edits, 3):
                self.cmd(f"execute at {n} run {e}")
        self.note("blocks", f"{names}")

    def a_tickingarea(self):
        if len(self.areas) >= 3:
            self._area_remove()
        self.area_i += 1
        name = f"chaos{self.area_i}"
        x = self.rng.choice([-1, 1]) * self.rng.randint(20000, 60000)
        z = self.rng.choice([-1, 1]) * self.rng.randint(20000, 60000)
        self.cmd(f"tickingarea add circle {x} 64 {z} 2 {name}")
        self.areas.append(name)
        self.note("tickingarea_add", f"{name} at {x} {z}")
        self.later(self.rng.uniform(20, 40), self._area_remove, name)

    def _area_remove(self, name=None):
        if not self.areas:
            return
        if name is None:
            name = self.areas[0]
        if name not in self.areas:
            return
        self.areas.remove(name)
        self.cmd(f"tickingarea remove {name}")
        self.note("tickingarea_remove", name)

    ACTIONS = [
        ("churn", 20, "a_churn"),
        ("stack", 25, "a_stack"),
        ("waypoints", 60, "a_waypoints"),
        ("gamemode", 15, "a_gamemode"),
        ("kill", 30, "a_kill"),
        ("summon", 30, "a_summon"),
        ("cleanup", 90, "a_cleanup"),
        ("kick", 40, "a_kick"),
        ("dimension", 20, "a_dimension"),
        ("list", 10, "a_list"),
        ("far_tp", 15, "a_far_tp"),
        ("view_disconnect", 30, "a_view_disconnect"),
        ("tickspeed", 30, "a_tickspeed"),
        ("weather", 45, "a_weather"),
        ("blocks", 20, "a_blocks"),
        ("tickingarea", 40, "a_tickingarea"),
    ]

    # ------------------------------------------------------------ monitor

    def mc_ms_s(self, t0, t1):
        tid = next((t for t, n in list(self.table.names.items()) if n == "MC_SERVER"), None)
        pts = [(t, s) for t, s in list(self.sampler.series) if t0 <= t <= t1]
        if tid is None or len(pts) < 2 or tid not in pts[0][1] or tid not in pts[-1][1]:
            return None
        return 1000 * (pts[-1][1][tid][0] - pts[0][1][tid][0]) / h.TSC_HZ / (pts[-1][0] - pts[0][0])

    verify_details_logged = 0

    def monitor(self, t_start, final=False):
        now = time.perf_counter()
        tps = h.tps_between(self.srv.gametime, now - 12, now)
        mc = self.mc_ms_s(now - 10, now)
        try:
            mi = self.bp.memory_info()
            ws, priv = mi.rss / 2 ** 20, mi.private / 2 ** 20
            handles, nthreads = self.bp.num_handles(), self.bp.num_threads()
        except psutil.Error:
            ws = priv = handles = nthreads = None
        with self.srv.lock:
            new = [l.rstrip() for _, l in self.srv.lines[self.err_idx:]]
            self.err_idx = len(self.srv.lines)
        hits = [l for l in new if ERR.search(l) and "[verify" not in l]
        errs = [l for l in hits if not CMD_FEEDBACK.search(l)]
        self.cmd_errors.extend(l for l in hits if CMD_FEEDBACK.search(l))
        self.err_lines.extend(errs)
        for l in new:
            self.verify.feed(l)
        backlog = self.srv.backlog() if hasattr(self.srv, "backlog") else 0
        with self.lock:
            online = len(self.online)
            events = dict(sorted(self.events.items()))
            killed = dict(self.killed)
        row = {"t": round(now - t_start, 1), "tps10": tps and round(tps, 2), "mc_ms_s": mc and round(mc),
               "ws_mb": ws and round(ws, 1), "private_mb": priv and round(priv, 1), "handles": handles,
               "threads": nthreads, "online": online, "errors": len(self.err_lines), "backlog": backlog}
        self.mon.append(row)
        self.mon_csv.write(",".join(str(row[k]) for k in ("t", "tps10", "mc_ms_s", "ws_mb", "private_mb", "handles",
                                                          "threads", "online", "errors", "backlog")) + "\n")
        self.mon_csv.flush()
        stale = [n for n, t in killed.items() if now - t > 60]
        self.log(f"MON t={row['t']:.0f}s tps10={row['tps10']} mc={row['mc_ms_s']}ms/s ws={row['ws_mb']}MB "
                 f"priv={row['private_mb']}MB handles={handles} threads={nthreads} online={online}/{self.n} "
                 f"errors={len(self.err_lines)} (+{len(errs)}) backlog={backlog} "
                 f"cmd_errors={len(self.cmd_errors)} harness_errors={len(self.harness_errors)} ev={events}"
                 + (f" NOT_RESPAWNED={stale}" if stale else ""))
        for l in errs[:5]:
            self.log(f"  ERRLINE {l[:300]}")
        self.n_mon += 1
        if self.n_mon % 3 == 0 or final:
            dnew = sum(self.verify.details.values()) - self.verify_details_logged
            self.log(f"VERIFY {self.verify.short() or '(no verify lines yet)'}"
                     + (f" new_detail_lines={dnew}" if dnew else ""))
            for l in self.verify.recent_details[-min(dnew, 3):] if dnew else []:
                self.log(f"  VERIFYLINE {l[:300]}")
            self.verify_details_logged = sum(self.verify.details.values())

    # ------------------------------------------------------------ main loop

    def safe(self, what, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            import traceback
            self.harness_errors.append(f"{what}: {e!r}")
            if len(self.harness_errors) <= 5:
                self.log(f"HARNESS ERROR in {what}: {e!r}\n{traceback.format_exc()}")

    def run(self):
        self.bots.send("jitter on")
        t_start = time.perf_counter()
        end = t_start + self.minutes * 60
        due = {name: t_start + self.rng.uniform(3, min(iv, 60)) for name, iv, _ in self.ACTIONS}
        next_mon = t_start + 10
        self.log(f"chaos start {self.minutes} min, {len(self.ACTIONS)} actions")
        while time.perf_counter() < end:
            if self.died.is_set():
                self.log("chaos: server process died")
                break
            now = time.perf_counter()
            while True:
                with self.lock:
                    if not self.timers or self.timers[0][0] > now:
                        break
                    _, _, fn, a = heapq.heappop(self.timers)
                self.safe(getattr(fn, "__name__", "timer"), fn, *a)
            busy = hasattr(self.srv, "backlog") and self.srv.backlog() > 150
            for name, iv, meth in self.ACTIONS:
                if due[name] <= now:
                    if busy and name != "list":
                        self.counts["skipped_backlog"] = self.counts.get("skipped_backlog", 0) + 1
                    else:
                        self.safe(meth, getattr(self, meth))
                    due[name] = now + iv * self.rng.uniform(0.6, 1.4)
            if now >= next_mon:
                self.safe("monitor", self.monitor, t_start)
                next_mon += 10
            self.died.wait(0.5)
        self.bots.send("jitter off")
        for name in list(self.areas):
            self.safe("tickingarea_remove", self._area_remove, name)
        self.safe("monitor", self.monitor, t_start, True)
        return self.summary()

    def summary(self):
        tps = [r["tps10"] for r in self.mon if r["tps10"] is not None]
        mc = [r["mc_ms_s"] for r in self.mon if r["mc_ms_s"] is not None]
        priv = [(r["t"], r["private_mb"]) for r in self.mon if r["private_mb"] is not None]
        ws = [(r["t"], r["ws_mb"]) for r in self.mon if r["ws_mb"] is not None]
        hd = [(r["t"], r["handles"]) for r in self.mon if r["handles"] is not None]
        s = {
            "minutes": self.minutes,
            "tps_min": min(tps) if tps else None,
            "tps_p5": sorted(tps)[round(0.05 * (len(tps) - 1))] if tps else None,
            "tps_median": statistics.median(tps) if tps else None,
            "mc_ms_s_median": statistics.median(mc) if mc else None,
            "private_mb_start_end": [priv[0][1], priv[-1][1]] if priv else None,
            "private_mb_per_h": slope_per_h(priv),
            "ws_mb_per_h": slope_per_h(ws),
            "handles_start_end": [hd[0][1], hd[-1][1]] if hd else None,
            "handles_per_h": slope_per_h(hd),
            "error_lines": len(self.err_lines),
            "first_errors": self.err_lines[:10],
            "command_feedback_errors": len(self.cmd_errors),
            "first_command_errors": self.cmd_errors[:5],
            "harness_errors": self.harness_errors[:20],
            "crash": self.died.is_set(),
            "actions": dict(sorted(self.counts.items())),
            "bot_events": dict(sorted(dict(self.events).items())),
            "not_respawned": sorted(dict(self.killed)),
            "verify": self.verify.verdict(),
        }
        (self.out / "chaos-summary.json").write_text(json.dumps(s, indent=1))
        self.chaos_log.close()
        self.mon_csv.close()
        return s
