# SPDX-License-Identifier: Apache-2.0
"""The phases of one run: boot, idle, join, settle, spread, steady and teardown (docs/methodology.md#run-phases).

:class:`Session` drives one prepared server through the workload's phases. It knows the server only through its
adapter: the launch command, the console dialect, the placement and position commands and the tick-thread rule.
Every phase records its marks (seconds since the run's time origin) and rewrites ``run.partial.json``.

==========  =======================  ======================  ==========================================================
Phase       Start                    End                     Rule
==========  =======================  ======================  ==========================================================
boot        launch                   ``ready``               the dialect's ready line within ``cap_s``; the banner
                                                             version must match the arm
idle        ``ready`` + delay_s      + duration_s            no bots
join        ``bots_start``           ``alljoined``           the bot's ``alljoined`` event within ``cap_s``;
                                                             ``bots_start`` is the end of idle plus ``pre_bots_s``
settle      ``alljoined``            ``settled``             :mod:`bedrock_bench.settle`; re-teleports and nudges
spread      ``settled``                                      effects to every player, positions read and checked
steady      ``settled`` + delay_s    + duration_s            only the tick probe is sent
teardown    any failure or the end                           bots quit, server stop command, kill after a timeout,
                                                             crash collection
==========  =======================  ======================  ==========================================================

Placement: when a bot reports ``spawned`` and the server prints its ``player_in_world`` line, whichever comes second,
the harness sends the adapter's effects and teleports the bot to its grid cell at the workload's height.

A failure never raises out of :meth:`Session.run`. A server that exits early or bots that cannot join end the phases
and go straight to teardown; the run's status comes from the recorded facts afterwards. A harness exception is
recorded as a ``harness_ok`` reason.

The load bot is located with :func:`bedrock_bench.bots.locate`. ``$BBENCH_BOT_CMD``, a JSON array, replaces the
binary with another program that speaks the same protocol (the test suite's fake bot); runs made that way are marked
``nonstandard``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bedrock_bench import bots, host, process
from bedrock_bench.clock import Origin, Stamp, mono_ns
from bedrock_bench.config import ConfigError
from bedrock_bench.console import Console
from bedrock_bench.grid import cell, verify_spread
from bedrock_bench.metrics import WINDOW_MARKS
from bedrock_bench.metrics.cpu import normalize_name
from bedrock_bench.model import Flag, Layout, LogLine, Machine, Placement, PlayerPos
from bedrock_bench.provenance import sha256_file
from bedrock_bench.results import (
    BOTS_FILE,
    BOTS_STDERR_FILE,
    CONFIG_AFTER_DIR,
    CONFIG_WRITTEN_DIR,
    CRASH_DIR,
    JOURNAL_FILE,
    LOG_FILE,
    LOG_TAIL_FILE,
    JsonLines,
    RunDir,
    empty_spread,
    interference_record,
)
from bedrock_bench.sampling import MachineSampler, ProcessSampler, ThreadSampler, TickProbe, TickSampler
from bedrock_bench.sampling import series as series_files
from bedrock_bench.settle import Nudge, Reteleport, Settle, SettleRules
from bedrock_bench.workload import Workload

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.adapters.base import ServerAdapter

log = logging.getLogger(__name__)

ENV_BOT_CMD = "BBENCH_BOT_CMD"

#: The bot module of the current protocol; any other module flags the run ``bot_protocol_differs`` (rule R2).
HEADLINE_BOT = "loadbot"

#: How often waiting loops check for a dead server, a dead bot and new bot events.
POLL_S = 0.2
#: Grace time for the console to report the end of output once the server has exited.
EXIT_GRACE_S = 5.0
#: Bot interrupt timeout after the workload's quit timeout.
BOT_SIGNAL_TIMEOUT_S = 10.0
#: Longest wait for queued console commands to be written before a measured window starts.
DRAIN_TIMEOUT_S = 30.0
#: Time after each smoke command in which an error line counts against it.
COMMAND_REPLY_S = 2.0


class PhaseAbort(Exception):
    """The phases cannot continue; teardown follows. The run's status comes from what was recorded."""


class ServerDied(PhaseAbort):
    """The server (or its launcher) exited before teardown."""


# --------------------------------------------------------------------------------------------------------------------
# The bot command
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BotCommand:
    """The bot command line for a run and the ``bot`` block of run.json."""

    argv: tuple[str, ...]
    provenance: Mapping[str, Any]
    override: bool = False  # True when $BBENCH_BOT_CMD replaced the bot binary


def bot_command(adapter: ServerAdapter, workload: Workload, machine: Machine) -> BotCommand:
    """The bot for the adapter's protocol with the workload's flags (rule R2).

    Raises :class:`bedrock_bench.bots.BotError` when the bot has not been built or fetched.
    """
    module = bots.module_for_protocol(adapter.protocol)
    args = bots.bot_args(
        workload.bots,
        addr=f"127.0.0.1:{machine.port}",
        csg=workload.server.csg,
        compat_flags=adapter.bot_compat_flags,
    )
    override = os.environ.get(ENV_BOT_CMD)
    if override:
        try:
            prefix = json.loads(override)
        except ValueError:
            prefix = None
        if not isinstance(prefix, list) or not prefix or not all(isinstance(p, str) for p in prefix):
            raise ConfigError(f"${ENV_BOT_CMD} must be a JSON array of strings, the bot's argv prefix")
        provenance = {
            "module": module,
            "protocol": adapter.protocol,
            "sha256": None,
            "version": None,
            "vcs": None,
            "go": None,
            "gophertunnel": None,
            "patches": [],
            "argv": [module, *args],
        }
        return BotCommand((*prefix, *args), provenance, override=True)
    binary = bots.locate(module)
    return BotCommand(tuple(binary.command(args)), binary.provenance(args))


# --------------------------------------------------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Placed:
    """One set of placement commands sent for a bot."""

    bot: str
    index: int
    t_ns: int


class Placer:
    """Sends a bot's effects and teleport once it has spawned and the server says it is in the world.

    Both signals arrive on reader threads (the bot's stdout and the server's console), in either order; the second
    one triggers the placement. A new dial or rejoin of the bot forgets stale signals of its previous session.
    """

    def __init__(self, adapter: ServerAdapter, workload: Workload, console: Console) -> None:
        self.adapter = adapter
        self.grid = workload.grid
        self.console = console
        self.enabled = True
        self._lock = threading.Lock()
        self._spawned: dict[str, int] = {}  # bot -> index, spawned and waiting for the in-world line
        self._in_world: set[str] = set()  # bots the server reported in the world, waiting for their spawn event
        self.placements: list[Placed] = []

    def on_bot_event(self, event: Mapping[str, Any]) -> None:
        kind, bot = event.get("ev"), event.get("bot")
        if not isinstance(bot, str):
            return
        with self._lock:
            if kind in ("dial", "rejoin"):
                self._spawned.pop(bot, None)
                self._in_world.discard(bot)
            elif kind == "spawned" and isinstance(event.get("i"), int):
                if bot in self._in_world:
                    self._in_world.discard(bot)
                    self._place(bot, event["i"])
                else:
                    self._spawned[bot] = event["i"]

    def on_line(self, line: LogLine) -> None:
        name = self.adapter.dialect.player_entered(line.text)
        if name is None:
            return
        with self._lock:
            index = self._spawned.pop(name, None)
            if index is None:
                self._in_world.add(name)
            else:
                self._place(name, index)

    def place(self, bot: str, index: int) -> None:
        """Send the placement commands for a bot now (re-teleports)."""
        with self._lock:
            self._place(bot, index, force=True)

    def _place(self, bot: str, index: int, *, force: bool = False) -> None:
        if not self.enabled and not force:
            return
        x, z = cell(index, self.grid)
        for command in (*self.adapter.cmd_effects(bot), self.adapter.cmd_tp(bot, x, self.grid.y, z)):
            self.console.send(command, origin="phase")
        self.placements.append(Placed(bot, index, mono_ns()))

    def snapshot(self) -> list[Placed]:
        with self._lock:
            return list(self.placements)


# --------------------------------------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _Samplers:
    threads: ThreadSampler | None = None
    tick: TickSampler | None = None
    process: ProcessSampler | None = None
    machine: MachineSampler | None = None

    def all(self) -> list[Any]:
        return [s for s in (self.tick, self.threads, self.process, self.machine) if s is not None]


@dataclass
class SessionResult:
    """What the phases leave for the runner besides the record: live-only reasons, flags and an interrupt."""

    reasons: list[str] = field(default_factory=list)  # "<gate>: <detail>" for harness_ok and config_readback
    flags: set[str] = field(default_factory=set)
    interrupted: bool = False


class Session:
    """One run of one prepared server."""

    def __init__(
        self,
        *,
        adapter: ServerAdapter,
        workload: Workload,
        placement: Placement,
        layout: Layout,
        run_dir: RunDir,
        record: dict[str, Any],
        origin: Origin,
        bot: BotCommand,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.workload = workload
        self.placement = placement
        self.layout = layout
        self.run_dir = run_dir
        self.record = record
        self.origin = origin
        self.bot = bot
        self.progress = progress or (lambda text: None)
        self.result = SessionResult()

        self.launched: process.Launched | None = None
        self.server: psutil.Process | None = None
        self.console: Console | None = None
        self.probe: TickProbe | None = None
        self.fleet: bots.BotFleet | None = None
        self.placer: Placer | None = None
        self.death: process.DeathWatch | None = None
        self.samplers = _Samplers()
        self._bot_log: JsonLines | None = None
        self._bot_cursor = 0
        self._settle: Settle | None = None
        self._placements_fed = 0
        self._placement_checks: list[host.PlacementCheck] = []
        self._early_exit: int | None = None  # exit code when the launcher exited before the server was found
        self._kill_on_stop = False  # True when the server never got ready: kill instead of asking it to stop
        self._launched_at: float | None = None
        self._no_death = threading.Event()  # stands in for the death watch until the server process is known

    # ---- time -------------------------------------------------------------------------------------------------------

    @property
    def marks(self) -> dict[str, float | None]:
        return self.record["marks"]

    @property
    def died(self) -> threading.Event:
        """Set when the server exited before teardown."""
        return self.death.died if self.death is not None else self._no_death

    def now(self) -> float:
        """Seconds since the run's time origin."""
        return self.origin.seconds(mono_ns())

    def seconds(self, t_ns: int) -> float:
        return self.origin.seconds(t_ns)

    def mark(self, name: str, t: float | None = None) -> float:
        value = round(self.now() if t is None else t, 6)
        self.marks[name] = value
        log.info("mark %s at %.1f s", name, value)
        self.progress(f"{value:7.1f} s  {name}")
        return value

    def sleep_until(self, t: float) -> None:
        """Wait until ``t`` seconds after the origin. Raises :class:`ServerDied` if the server exits meanwhile."""
        while True:
            self._check_alive()
            remaining = t - self.now()
            if remaining <= 0:
                return
            self.died.wait(min(remaining, POLL_S))

    def _check_alive(self) -> None:
        if self.died.is_set():
            raise ServerDied("the server exited")

    def save(self) -> None:
        """Rewrite run.partial.json."""
        self.run_dir.write_partial(self.record)

    # ---- the run ----------------------------------------------------------------------------------------------------

    def run(self) -> SessionResult:
        """Every phase, then teardown and result collection. Never raises, except on a second interrupt."""
        try:
            self._start()
            self._boot()
            self._idle()
            self._join()
            self._settle_phase()
            self._spread()
            self._steady()
        except PhaseAbort as abort:
            log.warning("phases ended early: %s", abort)
            self.progress(f"phases ended early: {abort}")
        except KeyboardInterrupt:
            log.warning("interrupted; tearing down")
            self.result.interrupted = True
            self.result.reasons.append("harness_ok: interrupted")
        except Exception as exc:
            log.exception("harness error")
            self.result.reasons.append(f"harness_ok: {type(exc).__name__}: {exc}")
        try:
            self._teardown()
        except Exception as exc:
            log.exception("teardown failed")
            self.result.reasons.append(f"harness_ok: teardown failed: {type(exc).__name__}: {exc}")
        try:
            self._collect()
        except Exception as exc:
            log.exception("result collection failed")
            self.result.reasons.append(f"harness_ok: result collection failed: {type(exc).__name__}: {exc}")
        return self.result

    # ---- launch -----------------------------------------------------------------------------------------------------

    def _start(self) -> None:
        layout = self.layout
        world = layout.world
        if world.fresh and world.path is not None and Path(world.path).exists():
            self.record.setdefault("world", {})["fresh"] = False
            log.warning("the world directory exists before launch: %s", world.path)
        _copy_configs(layout, self.run_dir.file(CONFIG_WRITTEN_DIR))

        spec = self.adapter.launch(layout)
        self.record["server"]["launch"] = {
            "argv": list(spec.argv),
            "env_set": dict(spec.env_set),
            "env_remove": list(spec.env_remove),
        }
        self._launched_at = self.now()
        self.launched = launched = process.launch(spec, self.placement, role="server")
        self.record["placement"]["method"] = launched.method
        assert launched.stdout is not None
        console = self.console = Console(
            launched.stdout,
            launched.stdin,
            self.adapter.dialect,
            log_path=self.run_dir.file(LOG_FILE),
            journal_path=self.run_dir.file(JOURNAL_FILE),
            rate=lambda: self.probe.rate() if self.probe is not None else None,
        )
        console.subscribe(self._on_line)
        console.start()
        for line in spec.stdin_lines:
            console.send(line, origin="phase")

        try:
            server = self.adapter.server_process(launched.process)
        except Exception:
            code = launched.wait(EXIT_GRACE_S) if launched.poll() is None else launched.poll()
            if code is not None:
                self._early_exit = code
                raise ServerDied(f"the server exited during start-up with code {code}") from None
            raise
        self.server = server
        if server.pid != launched.pid:
            process.apply_placement(server, self.placement.server_cpus, self.placement.priority)
            self.record["server"]["aux_pids"] = [launched.pid]
        self.record["server"]["pid"] = server.pid
        self.death = process.DeathWatch(launched, server).start()

        self.samplers.threads = ThreadSampler(server.pid, self.run_dir.series(series_files.THREADS_FILE))
        self.samplers.process = ProcessSampler(
            server,
            self.run_dir.series(series_files.PROCESS_FILE),
            launcher=launched.process if launched.pid != server.pid else None,
        )
        machine = MachineSampler(
            self.run_dir.series(series_files.MACHINE_FILE),
            self.run_dir.series(series_files.FOREIGN_TOP_FILE),
            self.placement.server_cpus,
        )
        machine.add_root("server", launched.process)
        self.samplers.machine = machine
        for sampler in (self.samplers.threads, self.samplers.process, machine):
            sampler.start()
        self.save()

    def _on_line(self, line: LogLine) -> None:
        if self.probe is not None:
            self.probe.feed(line.t_ns, line.text)
        if self.placer is not None:
            self.placer.on_line(line)

    # ---- boot -------------------------------------------------------------------------------------------------------

    def _boot(self) -> None:
        spec = self.workload.phase("boot")
        assert self.console is not None and spec.cap_s is not None and self._launched_at is not None
        deadline = self._launched_at + spec.cap_s
        console = self.console
        while not console.ready.is_set():
            self._check_alive()
            if console.eof.is_set():
                self.died.wait(EXIT_GRACE_S)
                self._check_alive()
                raise PhaseAbort("the server closed its console before it was ready")
            if self.now() >= deadline:
                self._kill_on_stop = True
                raise PhaseAbort(f"the server was not ready within {spec.cap_s:g} s")
            console.ready.wait(POLL_S)
        assert console.ready_line is not None
        self.mark("ready", self.seconds(console.ready_line.t_ns))
        self.record["server"]["banner_version"] = console.banner_version
        self._verify_placement("ready")
        assert self.server is not None
        self.record["server"]["facts"] = dict(self.adapter.facts(self.layout, self.server))

        probe_params = self.workload.probe
        self.probe = TickProbe(
            self.adapter.dialect.tick_cmd,
            self.adapter.dialect.parse_tick,
            lambda text, on_sent: console.send(text, lane="priority", origin="probe", on_sent=on_sent),
            self.run_dir.series(series_files.PROBE_FILE),
            interval_s=probe_params.interval_s,
            timeout_s=probe_params.max_gap_s,
        ).start()
        self._start_tick_sampler()
        self.save()

        banner, version = console.banner_version, self.adapter.arm.version
        if banner is not None and banner != version:
            raise PhaseAbort(f"the server reports version {banner!r}, the arm is {version!r}")

    def _start_tick_sampler(self) -> None:
        threads = self.samplers.threads
        if self.samplers.tick is not None or threads is None or self.server is None:
            return
        tid = self.adapter.tick_thread(threads.infos())
        if tid is None:
            if self.adapter.tick_thread_rule != "none":
                log.warning("no tick thread found by rule %s yet", self.adapter.tick_thread_rule)
            return
        self.samplers.tick = TickSampler(self.server.pid, tid, self.run_dir.series(series_files.TICK_FILE))
        self.samplers.tick.start()

    def _verify_placement(self, when: str) -> None:
        if self.server is None:
            return
        check = host.verify_placement(self.server, self.placement.server_cpus, self.placement.priority)
        if check.process_cpus is None:
            log.info("placement check at %s skipped: the server process is gone", when)
            return  # a dead server is a server_alive failure, not a placement mismatch
        self._placement_checks.append(check)
        if not check.ok:
            log.warning("placement check at %s failed: %s", when, "; ".join(check.problems))
        self.record["placement"]["verified"] = all(c.ok for c in self._placement_checks)

    # ---- idle -------------------------------------------------------------------------------------------------------

    def _idle(self) -> None:
        spec = self.workload.phase("idle")
        ready = self.marks["ready"]
        assert ready is not None and spec.delay_s is not None and spec.duration_s is not None
        self.sleep_until(ready + spec.delay_s)
        idle0 = self.mark("idle0")
        self.sleep_until(idle0 + spec.duration_s)
        self.mark("idle1")
        self._record_census()
        self.save()

    def _record_census(self) -> None:
        threads = self.samplers.threads
        if threads is None:
            return
        census = Counter(normalize_name(info.name) for info in threads.infos())
        self.record["server"]["facts"]["thread_census_idle"] = dict(sorted(census.items()))

    # ---- join -------------------------------------------------------------------------------------------------------

    def _join(self) -> None:
        spec = self.workload.phase("join")
        assert spec.cap_s is not None and self.console is not None
        start = self.marks["idle1"] if self.marks["idle1"] is not None else self.marks["ready"]
        assert start is not None
        self.sleep_until(start + spec["pre_bots_s"])
        self._start_tick_sampler()

        self.placer = Placer(self.adapter, self.workload, self.console)
        self._bot_log = JsonLines(self.run_dir.file(BOTS_FILE))
        self.fleet = fleet = bots.BotFleet(
            self.bot.argv,
            cpus=self.placement.bot_cpus,
            on_event=self._on_bot_event,
            stderr_path=self.run_dir.file(BOTS_STDERR_FILE),
        )
        bots_start = self.mark("bots_start")
        fleet.start()
        assert fleet.pid is not None and self.samplers.machine is not None
        self.samplers.machine.add_root("bot", fleet.pid)

        deadline = bots_start + spec.cap_s
        joined = self._wait_bot_event("alljoined", deadline, "all bots joined")
        self.mark("alljoined", self.seconds(int(joined["rx_ns"])))
        self.save()
        if not joined.get("joined"):
            raise PhaseAbort("no bot joined")

    def _on_bot_event(self, event: dict[str, Any]) -> None:
        """Bot reader thread: log the event and let the placer react. Must not block."""
        if self._bot_log is not None:
            self._bot_log.write(event)
        if self.placer is not None:
            self.placer.on_bot_event(event)

    def _new_bot_events(self) -> list[dict[str, Any]]:
        if self.fleet is None:
            return []
        events = self.fleet.events(since=self._bot_cursor)
        self._bot_cursor += len(events)
        if self._settle is not None:
            for event in events:
                self._feed_settle(event)
        return events

    def _wait_bot_event(self, kind: str, deadline: float, what: str) -> dict[str, Any]:
        """The next bot event of ``kind``. Raises :class:`PhaseAbort` at ``deadline`` or when the bot exits first."""
        assert self.fleet is not None
        while True:
            for event in self._new_bot_events():
                if event.get("ev") == kind:
                    return event
            self._check_alive()
            if self.fleet.returncode is not None:
                if self.fleet.wait_for(kind, EXIT_GRACE_S, since=self._bot_cursor) is not None:
                    continue  # the reader had not delivered it yet
                raise PhaseAbort(f"the bot exited with code {self.fleet.returncode} before {what}")
            if self.now() >= deadline:
                raise PhaseAbort(f"not {what} by {deadline:.0f} s")
            self.died.wait(POLL_S)

    # ---- settle -----------------------------------------------------------------------------------------------------

    def _settle_phase(self) -> None:
        spec = self.workload.phase("settle")
        alljoined = self.marks["alljoined"]
        assert alljoined is not None and self.fleet is not None and self.placer is not None
        rules = SettleRules.from_phase(spec)
        settle = self._settle = Settle(rules, n=self.workload.bots.n, start=alljoined, grid=self.workload.grid)
        history = self.fleet.events()  # replay everything since bots_start
        for event in history:
            self._feed_settle(event)
        self._bot_cursor = len(history)
        self._feed_placements()

        while True:
            events = self._new_bot_events()
            self._feed_placements()
            self._check_alive()
            if self.fleet.returncode is not None and not events:
                raise PhaseAbort(f"the bot exited with code {self.fleet.returncode} during settle")
            now = self.now()
            latest = settle.latest()
            progress_arrived = any(e.get("ev") == "progress" for e in events)
            stale = latest is None or now - latest[0] > rules.max_data_age_s
            if settle.due(now) and (progress_arrived or stale):
                decision = settle.evaluate(now)
                if decision.done:
                    break
                self._intervene(decision.actions)
            self.died.wait(POLL_S)

        assert settle.settled_at is not None
        self.placer.enabled = False
        self.mark("settled", settle.settled_at)
        self.record["settle"] = settle.settle_record()
        self.record["interventions"] = settle.interventions_record()
        self.save()

    def _feed_settle(self, event: Mapping[str, Any]) -> None:
        settle = self._settle
        if settle is None or "rx_ns" not in event:
            return
        t = self.seconds(int(event["rx_ns"]))
        kind, bot = event.get("ev"), event.get("bot")
        if kind == "progress":
            settle.progress(t, event.get("rows") or [])
        elif kind == "tp" and isinstance(bot, str):
            settle.teleported(bot, t)
        elif kind in ("left", "dropped", "kicked") and isinstance(bot, str):
            settle.left(bot, t)

    def _feed_placements(self) -> None:
        if self._settle is None or self.placer is None:
            return
        placements = self.placer.snapshot()
        for placement in placements[self._placements_fed :]:
            self._settle.placed(placement.bot, placement.index, self.seconds(placement.t_ns))
        self._placements_fed = len(placements)

    def _intervene(self, actions: Sequence[Reteleport | Nudge]) -> None:
        assert self.placer is not None and self.console is not None
        for action in actions:
            if isinstance(action, Reteleport):
                log.info("re-teleporting %s (attempt %d)", action.bot, action.attempt)
                self.placer.place(action.bot, action.index)
            else:
                log.info("nudging %s to %.0f %.0f %.0f", action.bot, action.x, action.y, action.z)
                self.console.send(self.adapter.cmd_tp(action.bot, action.x, action.y, action.z), origin="phase")

    # ---- spread -----------------------------------------------------------------------------------------------------

    def _spread(self) -> None:
        spec = self.workload.phase("spread")
        assert self.console is not None
        timeout = float(spec["query_timeout_s"])
        for command in self.adapter.cmd_effects("@a"):
            self.console.send(command, origin="phase")
        positions_cmd = self.adapter.cmd_positions()
        if positions_cmd is not None:
            source = "querytarget" if positions_cmd.split()[0].lower() == "querytarget" else "helper"
            positions = self._query_positions(positions_cmd, timeout)
        else:
            source = "bots"
            positions = self._bot_positions(timeout)
        self._check_alive()  # positions lost to a dying server are not a spread result
        if positions is None:
            log.warning("player positions unknown (%s)", source)
            self.record["spread"] = empty_spread(source)
        else:
            spread = verify_spread(positions, self.workload.bots.n, self.workload.grid)
            self.record["spread"] = spread.to_json(source)
            if spread.offset_warning(self.workload.grid):
                log.warning("a bot is %.0f blocks from its cell", spread.worst_offset_blocks or 0.0)
        self.save()

    def _query_positions(self, command: str, timeout: float) -> list[PlayerPos] | None:
        assert self.console is not None
        start = self.console.mark()
        sent = self.console.send(command, origin="phase")
        if not sent.wait(timeout):
            self.died.wait(EXIT_GRACE_S if sent.done else 0.0)  # a refused write usually means the server is gone
            self._check_alive()
            return None
        deadline = self.now() + timeout
        while True:
            positions = self.adapter.parse_positions(self.console.lines(start))
            if positions is not None:
                return positions
            self._check_alive()
            if self.now() >= deadline:
                return None
            self.died.wait(POLL_S)

    def _bot_positions(self, timeout: float) -> list[PlayerPos] | None:
        """Positions from the next ``progress`` event; bots that report no position leave the set incomplete."""
        try:
            event = self._wait_bot_event("progress", self.now() + timeout, "a progress report")
        except PhaseAbort as exc:
            log.warning("%s", exc)
            return None
        positions = []
        for row in event.get("rows") or ():
            if len(row) >= 7 and all(v is not None for v in row[3:6]):
                positions.append(PlayerPos(str(row[0]), float(row[3]), float(row[4]), float(row[5]), int(row[6] or 0)))
        return positions

    # ---- steady -----------------------------------------------------------------------------------------------------

    def _steady(self) -> None:
        spec = self.workload.phase("steady")
        settled = self.marks["settled"]
        assert settled is not None and spec.delay_s is not None and spec.duration_s is not None
        self._drain_console()
        self.sleep_until(max(settled + spec.delay_s, self.now()))
        steady0 = self.mark("steady0")
        self._verify_placement("steady0")
        self.save()
        self.sleep_until(steady0 + spec.duration_s)
        self.mark("steady1")
        self._verify_placement("steady1")
        self.save()

    def _drain_console(self) -> None:
        """Wait until every queued phase command is written, so none lands in the measured window."""
        assert self.console is not None
        deadline = self.now() + DRAIN_TIMEOUT_S
        while self.console.backlog() and self.now() < deadline:
            self._check_alive()
            self.died.wait(POLL_S / 4)

    # ---- teardown ---------------------------------------------------------------------------------------------------

    def _teardown(self) -> None:
        spec = self.workload.phase("teardown")
        self.mark("stop")
        if self.placer is not None:
            self.placer.enabled = False
        if self.probe is not None:
            self.probe.stop()
        if self.fleet is not None:
            code = self.fleet.stop(
                quit_timeout_s=float(spec["bot_quit_timeout_s"]), signal_timeout_s=BOT_SIGNAL_TIMEOUT_S
            )
            if code == bots.BotExit.USAGE:
                self.result.reasons.append("harness_ok: the bot rejected its command line")
        if self._bot_log is not None:
            self._bot_log.close()
        if self.death is not None:
            self.death.expect_exit()

        launched = self.launched
        if launched is not None:
            stop_timeout = float(spec["stop_timeout_s"])
            stopped = False
            if launched.poll() is None and not self.died.is_set() and not self._kill_on_stop:
                assert self.console is not None
                stop = self.console.send(self.adapter.dialect.stop_cmd, origin="phase")
                if not stop.wait(EXIT_GRACE_S):
                    launched.interrupt()  # the console no longer takes input
                launched.wait(stop_timeout)
                stopped = True
            elif launched.poll() is None and self.died.is_set():
                launched.wait(EXIT_GRACE_S)
            if launched.poll() is None or (self.server is not None and _running(self.server)):
                log.warning("killing the server process tree")
                if stopped:
                    self.result.flags.add(Flag.UNCLEAN_STOP.value)
                launched.kill_tree()
        if self.death is not None:
            self.death.stop()
        for sampler in self.samplers.all():
            sampler.stop()
        if self.console is not None:
            self.console.eof.wait(EXIT_GRACE_S)
            self.console.close()
        if launched is not None:
            launched.close()

    # ---- results ----------------------------------------------------------------------------------------------------

    def _collect(self) -> None:
        record = self.record
        end = Stamp.now()
        record["time"].update(
            end_mono_ns=end.mono_ns, end_epoch_ms=end.epoch_ms, drift_ms=round(self.origin.drift_ms(end), 3)
        )
        if self.console is not None and self.console.banner_version is not None:
            record["server"]["banner_version"] = self.console.banner_version
        self._collect_health()
        self._collect_configs()
        self._collect_bots()
        self._collect_samplers()
        windows = {}
        for name in ("idle", "load", "steady"):
            start, end_mark = WINDOW_MARKS[name]
            t0, t1 = self.marks.get(start), self.marks.get(end_mark)
            if t0 is not None and t1 is not None and t1 >= t0:
                windows[name] = (t0, t1)
        record["interference"]["windows"] = interference_record(self.run_dir, self.origin, windows)
        if self.bot.provenance.get("module") != HEADLINE_BOT:
            self.result.flags.add(Flag.BOT_PROTOCOL_DIFFERS.value)

    def _collect_health(self) -> None:
        health = self.record["health"]
        launched = self.launched
        info = self.death.info if self.death is not None else None
        died = info is not None or self._early_exit is not None
        exit_code: int | None = None
        if info is not None and info.exit_code is not None:
            exit_code = info.exit_code
        elif self._early_exit is not None:
            exit_code = self._early_exit
        elif launched is not None:
            exit_code = launched.poll()
        health["died"] = died
        health["server_exit"] = exit_code
        if died or exit_code not in (0, None) or self._kill_on_stop:
            health["crash_files"] = self._collect_crash()

    def _collect_crash(self) -> list[str]:
        crash_dir = self.run_dir.file(CRASH_DIR)
        crash_dir.mkdir(exist_ok=True)
        lines = int(self.workload.phase("teardown")["crash_log_lines"])
        if self.console is not None:
            tail = "".join(line.raw + "\n" for line in self.console.tail(lines))
            (crash_dir / LOG_TAIL_FILE).write_text(tail, encoding="utf-8", newline="\n")
        copied = []
        server_dir = self.layout.server_dir.resolve()
        for path in self.adapter.crash_artifacts(self.layout, self.origin.epoch_ms):
            resolved = path.resolve()
            rel = resolved.relative_to(server_dir) if resolved.is_relative_to(server_dir) else Path(path.name)
            target = crash_dir / rel
            if target.exists():
                target = target.with_name(f"{target.stem}-{len(copied)}{target.suffix}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, target)
            except OSError as exc:
                log.warning("cannot copy crash file %s: %s", path, exc)
                continue
            copied.append(target.relative_to(self.run_dir.path).as_posix())
        return copied

    def _collect_configs(self) -> None:
        layout = self.layout
        after = _copy_configs(layout, self.run_dir.file(CONFIG_AFTER_DIR))
        for entry in self.record["config_files"]:
            entry["after_sha256"] = after.get(entry["path"])
        readback = getattr(self.adapter, "readback", None)
        if readback is not None and self.marks.get("ready") is not None:
            self.result.reasons.extend(f"config_readback: {problem}" for problem in readback(layout))
        executable = getattr(self.adapter, "executable", None)
        before = self.record["install"].get("exe_sha256_before")
        if executable is not None and before is not None:
            path = Path(executable(layout))
            now = sha256_file(path) if path.is_file() else None
            self.record["install"]["exe_sha256_after"] = now
            if now != before:
                self.result.reasons.append("harness_ok: the server executable changed during the run")

    def _collect_bots(self) -> None:
        if self.fleet is None:
            return
        self._new_bot_events()
        health = self.record["health"]
        stat = self.fleet.last("stat")
        alljoined = self.fleet.last("alljoined")
        if stat is not None:
            health.update(
                joined=int(stat.get("joined", 0)),
                failed=int(stat.get("failed", 0)),
                dropped=int(stat.get("dropped", 0)),
                decode_errors=int(stat.get("decode_errors", 0)),
            )
        elif alljoined is not None:
            health.update(joined=int(alljoined.get("joined", 0)), failed=int(alljoined.get("failed", 0)))
        reported = [int(e.get("total", 1)) for e in self.fleet.events("decode_error")]
        if reported:
            health["decode_errors"] = max([health.get("decode_errors") or 0, *reported])
        hello = self.fleet.hello
        build = hello.get("build") if hello else None
        if isinstance(build, Mapping):
            for key in ("version", "vcs", "go", "gophertunnel"):
                if self.record["bot"].get(key) is None and build.get(key) is not None:
                    self.record["bot"][key] = str(build[key])
            if not self.record["bot"].get("patches") and build.get("patches"):
                self.record["bot"]["patches"] = [str(p) for p in build["patches"]]
        stderr = self.run_dir.file(BOTS_STDERR_FILE)
        if stderr.is_file() and stderr.stat().st_size == 0:
            stderr.unlink()

    def _collect_samplers(self) -> None:
        for sampler in self.samplers.all():
            if sampler.error is not None:
                self.result.reasons.append(f"harness_ok: sampler {sampler.name} failed: {sampler.error}")
        if self.probe is not None and self.probe.unmatched:
            log.info("%d tick replies arrived without a probe waiting", self.probe.unmatched)


def _running(proc: psutil.Process) -> bool:
    import psutil

    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _copy_configs(layout: Layout, target: Path) -> dict[str, str | None]:
    """Copy the layout's rendered config files under ``target``; returns their sha256 (None for a missing file)."""
    hashes: dict[str, str | None] = {}
    for config_file in layout.config_files:
        source = layout.server_dir / config_file.path
        if not source.is_file():
            hashes[config_file.path] = None
            continue
        destination = target / config_file.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        hashes[config_file.path] = sha256_file(destination)
    return hashes


__all__ = [
    "ENV_BOT_CMD",
    "BotCommand",
    "PhaseAbort",
    "Placer",
    "ServerDied",
    "Session",
    "SessionResult",
    "bot_command",
]
