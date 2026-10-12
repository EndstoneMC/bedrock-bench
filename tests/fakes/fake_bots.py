#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A stand-in for the ``loadbot`` binary, for tests only.

It takes the loadbot command line (SPEC.md section 5.2), joins the fake server over its JSON-lines TCP protocol (see
``README.md`` in this folder) and writes loadbot's JSONL events (``bedrock_bench/schemas/bot-event.schema.json``) to
stdout. Chunk delivery, movement corrections and the packet mix follow the rates the fake server sends at spawn, so
the numbers come from the server's behaviour script rather than from the bot.

stdin takes ``quit`` and, with ``-control``, JSON operations. ``quit``, end of input, SIGINT, SIGTERM and CTRL_BREAK
all stop the bots cleanly: ``left`` per online bot, ``bot_summary`` per bot that joined, a final ``stat``, ``done``.

Exit codes: 0 ok, 1 usage, 2 every join failed, 3 decode error with ``-decode-errors fail``.

Standard library only.

    python fake_bots.py -addr 127.0.0.1:19672 -n 4 -prefix a -stagger 50ms
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

MODULE = "loadbot"
VERSION = "0.0.0-fake"
PROTOCOL = 2193
BUILD = {
    "module": MODULE,
    "version": VERSION,
    "vcs": "fake",
    "go": "none",
    "gophertunnel": "none",
    "patches": [],
    "protocol": PROTOCOL,
}

EXIT_OK, EXIT_USAGE, EXIT_NO_JOINS, EXIT_DECODE = 0, 1, 2, 3

XUID_BASE = 2535400000000000
STEP_S = 0.05  # simulation step: one game tick
WALK_SPEED = 0.2158  # blocks per tick
WALK_TURN = 0.01  # radians per tick
GOLDEN_ANGLE = math.radians(137.508)
SUBCHUNKS_PER_COLUMN = 24
MOVE_REPORT_S = 0.5
RETRY_DELAY_S = 1.0
REJOIN_DELAY_S = 1.0
RESPAWN_DELAY_S = 0.3

#: Received packet kinds counted in ``pkt`` events, with a typical size in bytes.
PACKETS = {
    "LevelChunk": 9000,
    "CorrectPlayerMovePrediction": 60,
    "MoveActorDelta": 30,
    "UpdateBlock": 20,
    "PlayerLocation": 40,
}
RATE_PACKETS = {
    "corrections": "CorrectPlayerMovePrediction",
    "actor_moves": "MoveActorDelta",
    "block_updates": "UpdateBlock",
    "locator": "PlayerLocation",
}


# --------------------------------------------------------------------------------------------------------------------
# Command line (Go flag syntax: -name value, -name=value, bare -bool)
# --------------------------------------------------------------------------------------------------------------------

_DURATION_RE = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|ms|s|m|h)")
_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def go_duration(text: str) -> float:
    """``300ms``, ``1m30s``, ``0`` -> seconds."""
    if text in ("0", "+0", "-0"):
        return 0.0
    pos, total = 0, 0.0
    while pos < len(text):
        match = _DURATION_RE.match(text, pos)
        if match is None:
            raise argparse.ArgumentTypeError(f"invalid duration {text!r}")
        total += float(match[1]) * _DURATION_UNITS[match[2]]
        pos = match.end()
    if pos == 0:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r}")
    return total


def go_bool(text: str) -> bool:
    if text in ("1", "t", "T", "true", "TRUE", "True"):
        return True
    if text in ("0", "f", "F", "false", "FALSE", "False"):
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean {text!r}")


def choice(*values: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        if text not in values:
            raise argparse.ArgumentTypeError(f"must be one of {', '.join(values)}")
        return text

    return check


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"loadbot: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def parse_flags(argv: Sequence[str]) -> argparse.Namespace:
    parser = _Parser(prog="loadbot", allow_abbrev=False, description="Fake loadbot for tests.")

    def flag(name: str, kind: Callable[[str], Any], default: Any, help_text: str) -> None:
        extra: dict[str, Any] = {"nargs": "?", "const": True} if kind is go_bool else {}
        parser.add_argument(f"-{name}", f"--{name}", dest=name.replace("-", "_"), type=kind, default=default, **extra)

    flag("addr", str, None, "server address HOST:PORT")
    flag("n", int, 1, "bots")
    flag("prefix", str, "b", "name prefix")
    flag("stagger", go_duration, 0.3, "join start spacing")
    flag("csg", go_bool, False, "client-side chunk generation")
    flag("radius", int, 16, "requested chunk radius")
    flag("loadscreen", go_bool, True, "send loading screen start and end")
    flag("walk", choice("circle", "none"), "circle", "movement")
    flag("spawn-timeout", go_duration, 60.0, "dial + login + spawn per bot")
    flag("join-retries", int, 0, "join retries")
    flag("fake-xuid", go_bool, False, "fake XUIDs")
    flag("identity-seed", str, "bedrock-bench", "UUIDv5 namespace")
    flag("control", go_bool, False, "accept JSON operations on stdin")
    flag("rejoin-on-drop", go_bool, False, "rejoin after a drop or kick")
    flag("radius-jitter", go_duration, 0.0, "mean radius re-request interval")
    flag("jitter-paused", go_bool, False, "jitter off until 'jitter on'")
    flag("progress-interval", go_duration, 5.0, "progress event interval")
    flag("stat-interval", go_duration, 10.0, "stat event interval")
    flag("pkt-interval", go_duration, 60.0, "packet-mix event interval")
    flag("probe-bots", int, 0, "bots that measure round-trip time")
    flag("decode-errors", choice("count", "fail"), "count", "count or fail on decode errors")
    flag("selftest-output", go_bool, False, "print one of every event and exit")
    flag("version", go_bool, False, "print build info as JSON and exit")
    flags = parser.parse_args(argv)
    if flags.version or flags.selftest_output:
        return flags
    if not flags.addr or ":" not in flags.addr:
        parser.error("-addr HOST:PORT is required")
    if flags.n < 1:
        parser.error("-n must be at least 1")
    for name in ("progress_interval", "stat_interval", "pkt_interval"):
        if getattr(flags, name) <= 0:
            parser.error(f"-{name.replace('_', '-')} must be positive")
    return flags


# --------------------------------------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------------------------------------


class Events:
    """Writes one JSON object per line; safe from any thread."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self._lock = threading.Lock()

    def emit(self, ev: str, **fields: Any) -> None:
        record = {
            "v": 1,
            "ev": ev,
            "t": time.time_ns() // 1_000_000,
            "mono_ms": round((time.monotonic() - self.start) * 1000, 3),
            **fields,
        }
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass


def selftest(events: Events) -> int:
    """One of every event the real bot writes, with plausible values."""
    events.emit("hello", build=BUILD, flags={"n": 1}, intervals_ms={"progress": 5000, "stat": 10000, "pkt": 60000})
    events.emit("dial", bot="a00", i=0)
    events.emit("spawned", bot="a00", i=0, session=1, join_ms=812, radius=10, dim=0)
    events.emit("join_failed", bot="a01", i=1, stage="spawn", err="timed out waiting for spawn")
    events.emit("tp", bot="a00", pos=[1280.5, 300.0, 1280.5], dim=0, n=1)
    events.emit("first_chunk", bot="a00", dt_ms=140)
    events.emit("progress", rows=[["a00", 317, 317, 1280.5, 300.0, 1280.5, 0, 3, 1], ["a01", 0, 0, *[None] * 4, 0, 0]])
    events.emit("stat", **_zero_stat() | {"online": 1, "joined": 1, "failed": 1, "uniq": 317, "in_view": 317})
    events.emit("pkt", rx={name: 1 for name in PACKETS})
    events.emit("rtt", n=10, p50_ms=2.0, p99_ms=8.0, max_ms=9.5, lost=0)
    for ev in ("died", "respawned", "left"):
        events.emit(ev, bot="a00")
    events.emit("dim", bot="a00", dim=1)
    events.emit("dropped", bot="a00", err="connection closed by server")
    events.emit("rejoin", bot="a00", session=2)
    events.emit("kicked", bot="a00", reason="Kicked by an operator")
    events.emit("decode_error", bot="a00", pkt_id=255, err="fake: undecodable packet", total=1)
    events.emit("alljoined", joined=1, failed=1)
    events.emit("bot_summary", bot="a00", uniq=317, in_view=317, chunks=317, corrections=3, first_seen_ms=[140, 2, 2])
    events.emit("done")
    return EXIT_OK


def _zero_stat() -> dict[str, int]:
    keys = (
        "online joined failed dropped left rejoins deaths rx_pkts tx_pkts rx_bytes tx_bytes corrections "
        "level_chunks subchunks uniq in_view decode_errors"
    )
    return dict.fromkeys(keys.split(), 0)


# --------------------------------------------------------------------------------------------------------------------
# Bots
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class Bot:
    index: int
    name: str
    xuid: str
    uuid: str
    yaw: float
    state: str = "waiting"  # waiting | joining | online | offline | failed
    next_dial: float | None = None  # monotonic time of the next join attempt
    rejoin: bool = False
    attempts: int = 0
    session: int = 0
    sock: socket.socket | None = None
    dial_at: float = 0.0
    deadline: float = 0.0
    joined: bool = False
    # placement and view
    spawn: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pos: tuple[float, float, float] | None = None  # None until the server teleports the bot
    dim: int = 0
    radius: int = 0
    csg: bool = False
    rates: dict[str, float] = field(default_factory=dict)
    chunks: dict[tuple[int, int, int], float | None] = field(default_factory=dict)  # -> ms since first tp
    budget: float = 0.0
    first_tp: float | None = None
    first_chunk_sent: bool = False
    teleports: int = 0
    angle: float = 0.0
    last_move: float = 0.0
    respawn_at: float | None = None
    # counters
    corrections: float = 0.0
    level_chunks: int = 0
    rx: dict[str, float] = field(default_factory=lambda: dict.fromkeys(PACKETS, 0.0))
    deaths: int = 0
    rejoins: int = 0

    @property
    def center(self) -> tuple[float, float, float]:
        return self.pos if self.pos is not None else self.spawn

    def chunk(self) -> tuple[int, int]:
        x, _, z = self.center
        return math.floor(x / 16), math.floor(z / 16)

    def in_view(self) -> int:
        cx, cz = self.chunk()
        limit = (self.radius + 1) ** 2
        return sum(1 for d, x, z in self.chunks if d == self.dim and (x - cx) ** 2 + (z - cz) ** 2 <= limit)

    def first_seen_ms(self) -> list[int]:
        """Arrival times of the chunks now in view, ms since the first teleport, delta-encoded."""
        cx, cz = self.chunk()
        limit = (self.radius + 1) ** 2
        times = sorted(
            round(ms)
            for (d, x, z), ms in self.chunks.items()
            if ms is not None and d == self.dim and (x - cx) ** 2 + (z - cz) ** 2 <= limit
        )
        deltas, previous = [], 0
        for value in times:
            deltas.append(value - previous)
            previous = value
        return deltas


def _offsets(radius: int) -> list[tuple[int, int]]:
    """Chunk offsets within ``radius``, nearest first."""
    cells = [(dx, dz) for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)]
    return sorted((c for c in cells if c[0] ** 2 + c[1] ** 2 <= radius**2), key=lambda c: (c[0] ** 2 + c[1] ** 2, c))


class Fleet:
    def __init__(self, flags: argparse.Namespace, events: Events) -> None:
        self.flags = flags
        self.events = events
        host, _, port = flags.addr.rpartition(":")
        self.address = (host or "127.0.0.1", int(port))
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, flags.identity_seed)
        self.bots = [
            Bot(
                index=i,
                name=f"{flags.prefix}{i:02d}",
                xuid=str(XUID_BASE + i) if flags.fake_xuid else "",
                uuid=str(uuid.uuid5(namespace, f"{flags.prefix}{i:02d}")),
                yaw=(i * GOLDEN_ANGLE) % math.tau,
            )
            for i in range(flags.n)
        ]
        self.inbox: queue.Queue[tuple[str, int, Any, Any]] = queue.Queue()
        self.quit = threading.Event()
        self.alljoined = False
        self.failed = 0
        self.dropped = 0
        self.left = 0
        self.decode_errors = 0
        self.fatal: int | None = None
        self.tx_pkts = 0.0
        self.jitter = flags.radius_jitter > 0 and not flags.jitter_paused
        self.offsets: dict[int, list[tuple[int, int]]] = {}
        self.last_pkt = dict.fromkeys(PACKETS, 0)

    # ---- main loop -------------------------------------------------------------------------------------------------

    def run(self) -> int:
        flags = self.flags
        self.events.emit(
            "hello",
            build=BUILD,
            flags={key: value for key, value in sorted(vars(flags).items())},
            intervals_ms={
                "progress": max(1, round(flags.progress_interval * 1000)),
                "stat": max(1, round(flags.stat_interval * 1000)),
                "pkt": max(1, round(flags.pkt_interval * 1000)),
            },
        )
        threading.Thread(target=self._read_stdin, name="stdin", daemon=True).start()
        start = time.monotonic()
        for bot in self.bots:
            bot.next_dial = start + bot.index * flags.stagger
        due = {"progress": start + flags.progress_interval, "stat": start + flags.stat_interval}
        due["pkt"] = start + flags.pkt_interval
        last = start
        while True:
            now = time.monotonic()
            self._drain(now)
            if self.quit.is_set():
                return self._finish(EXIT_OK)
            if self.fatal is not None:
                return self._finish(self.fatal)
            self._joins(now)
            if not self.alljoined and all(b.state in ("online", "offline", "failed") for b in self.bots):
                self.alljoined = True
                joined = sum(b.joined for b in self.bots)
                self.events.emit("alljoined", joined=joined, failed=self.failed)
                if joined == 0:
                    return self._finish(EXIT_NO_JOINS)
            self._simulate(now, now - last)
            last = now
            if now >= due["progress"]:
                self.events.emit("progress", rows=[self._row(b) for b in self.bots])
                due["progress"] = now + flags.progress_interval
            if now >= due["stat"]:
                self.events.emit("stat", **self._stat())
                if flags.probe_bots > 0:
                    self._rtt()
                due["stat"] = now + flags.stat_interval
            if now >= due["pkt"]:
                self._pkt()
                due["pkt"] = now + flags.pkt_interval
            time.sleep(max(0.0, STEP_S - (time.monotonic() - now)))

    def _finish(self, code: int) -> int:
        for bot in self.bots:
            if bot.state == "online":
                self._send(bot, {"op": "quit"})
                self._close(bot)
                bot.state = "offline"
                self.left += 1
                self.events.emit("left", bot=bot.name)
        for bot in self.bots:
            if bot.joined:
                self.events.emit(
                    "bot_summary",
                    bot=bot.name,
                    uniq=len(bot.chunks),
                    in_view=bot.in_view(),
                    chunks=bot.level_chunks,
                    corrections=int(bot.corrections),
                    first_seen_ms=bot.first_seen_ms(),
                )
        self.events.emit("stat", **self._stat())
        self._pkt()
        self.events.emit("done")
        return code

    # ---- joining ---------------------------------------------------------------------------------------------------

    def _joins(self, now: float) -> None:
        for bot in self.bots:
            if bot.next_dial is not None and now >= bot.next_dial:
                bot.next_dial = None
                self._dial(bot, now)
            elif bot.state == "joining" and now >= bot.deadline:
                self._join_failed(bot, "spawn", "timed out waiting for spawn", now)

    def _dial(self, bot: Bot, now: float) -> None:
        if bot.rejoin:
            bot.rejoins += 1
            self.events.emit("rejoin", bot=bot.name, session=bot.session + 1)
        else:
            bot.attempts += 1
            self.events.emit("dial", bot=bot.name, i=bot.index)
        bot.state = "joining"
        bot.dial_at = now
        bot.deadline = now + self.flags.spawn_timeout
        try:
            sock = socket.create_connection(self.address, timeout=2.0)
        except OSError as exc:
            self._join_failed(bot, "dial", str(exc), now)
            return
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        bot.sock = sock
        threading.Thread(target=self._read_server, args=(bot.index, sock), name=bot.name, daemon=True).start()
        self._send(
            bot,
            {
                "op": "login",
                "name": bot.name,
                "i": bot.index,
                "n": len(self.bots),
                "xuid": bot.xuid,
                "uuid": bot.uuid,
                "radius": self.flags.radius,
                "csg": self.flags.csg,
                "session": bot.session + 1,
            },
        )

    def _join_failed(self, bot: Bot, stage: str, err: str, now: float) -> None:
        self._close(bot)
        if bot.rejoin:
            self.events.emit("dropped", bot=bot.name, err=f"rejoin failed: {err}")
            bot.state = "offline"
            return
        self.events.emit("join_failed", bot=bot.name, i=bot.index, stage=stage, err=err)
        if bot.attempts <= self.flags.join_retries:
            bot.state = "waiting"
            bot.next_dial = now + RETRY_DELAY_S
        else:
            bot.state = "failed"
            self.failed += 1

    def _spawned(self, bot: Bot, message: dict[str, Any], now: float) -> None:
        bot.state = "online"
        bot.session += 1
        bot.joined = True
        bot.rejoin = False
        bot.spawn = (float(message["x"]), float(message["y"]), float(message["z"]))
        bot.pos = None
        bot.dim = int(message.get("dim", 0))
        bot.radius = int(message.get("radius", self.flags.radius))
        bot.csg = bool(message.get("csg", False))
        bot.rates = {key: float(value) for key, value in dict(message.get("rates", {})).items()}
        bot.chunks.clear()
        bot.budget = 0.0
        self.events.emit(
            "spawned",
            bot=bot.name,
            i=bot.index,
            session=bot.session,
            join_ms=round((now - bot.dial_at) * 1000),
            radius=bot.radius,
            dim=bot.dim,
        )

    def _offline(self, bot: Bot, now: float) -> None:
        self._close(bot)
        bot.state = "offline"
        if self.flags.rejoin_on_drop and not self.quit.is_set():
            bot.rejoin = True
            bot.next_dial = now + REJOIN_DELAY_S

    # ---- messages --------------------------------------------------------------------------------------------------

    def _read_server(self, index: int, sock: socket.socket) -> None:
        try:
            with sock.makefile("rb") as stream:
                for data in stream:
                    try:
                        message = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        self.inbox.put(("server", index, sock, message))
        except OSError:
            pass
        self.inbox.put(("eof", index, sock, None))

    def _read_stdin(self) -> None:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        for line in sys.stdin:
            text = line.strip()
            if text == "quit":
                break
            if self.flags.control and text.startswith("{"):
                try:
                    op = json.loads(text)
                except json.JSONDecodeError:
                    print(f"loadbot: bad control line {text!r}", file=sys.stderr)
                    continue
                if isinstance(op, dict):
                    self.inbox.put(("control", -1, None, op))
            elif text:
                print(f"loadbot: ignoring stdin line {text!r}", file=sys.stderr)
        self.quit.set()

    def _drain(self, now: float) -> None:
        while True:
            try:
                kind, index, sock, message = self.inbox.get_nowait()
            except queue.Empty:
                return
            if kind == "control":
                self._control(message, now)
                continue
            bot = self.bots[index]
            if sock is not bot.sock:
                continue  # stale connection
            if kind == "eof":
                if bot.state == "joining":
                    self._join_failed(bot, "spawn", "connection closed before spawn", now)
                elif bot.state == "online":
                    self.dropped += 1
                    self.events.emit("dropped", bot=bot.name, err="connection closed by server")
                    self._offline(bot, now)
                continue
            self._message(bot, message, now)

    def _message(self, bot: Bot, message: dict[str, Any], now: float) -> None:
        op = message.get("op")
        if op == "spawn" and bot.state == "joining":
            self._spawned(bot, message, now)
        elif op == "reject" and bot.state == "joining":
            self._join_failed(bot, "login", str(message.get("reason", "rejected")), now)
        elif bot.state != "online":
            return
        elif op == "tp":
            bot.teleports += 1
            bot.pos = (float(message["x"]), float(message["y"]), float(message["z"]))
            bot.dim = int(message.get("dim", bot.dim))
            bot.angle = bot.yaw
            if bot.first_tp is None:
                bot.first_tp = now
            self.events.emit("tp", bot=bot.name, pos=list(bot.pos), dim=bot.dim, n=bot.teleports)
        elif op == "kick":
            self.events.emit("kicked", bot=bot.name, reason=str(message.get("reason", "")))
            self._offline(bot, now)
        elif op == "die":
            bot.deaths += 1
            bot.respawn_at = now + RESPAWN_DELAY_S
            self.events.emit("died", bot=bot.name)
        elif op == "respawned":
            bot.pos = (float(message["x"]), float(message["y"]), float(message["z"]))
            self.events.emit("respawned", bot=bot.name)
        elif op == "dim":
            bot.dim = int(message.get("dim", 0))
            self.events.emit("dim", bot=bot.name, dim=bot.dim)
        elif op == "garbage":
            self.decode_errors += 1
            self.events.emit(
                "decode_error", bot=bot.name, pkt_id=255, err="fake: undecodable packet", total=self.decode_errors
            )
            if self.flags.decode_errors == "fail":
                self.fatal = EXIT_DECODE

    def _control(self, op: dict[str, Any], now: float) -> None:
        name = op.get("op")
        bot = next((b for b in self.bots if b.name == op.get("bot")), None)
        if name == "stop":
            self.quit.set()
        elif name == "jitter":
            self.jitter = bool(op.get("on", True))
        elif name == "disconnect" and bot is not None and bot.state == "online":
            self._send(bot, {"op": "quit"})
            self._close(bot)
            bot.state = "offline"
            self.left += 1
            self.events.emit("left", bot=bot.name)
        elif name == "reconnect" and bot is not None and bot.state == "offline":
            bot.rejoin = True
            bot.next_dial = now
        elif name == "radius" and bot is not None and bot.state == "online":
            self._send(bot, {"op": "radius", "radius": int(op.get("radius", self.flags.radius))})
        else:
            print(f"loadbot: ignoring control operation {op!r}", file=sys.stderr)

    def _send(self, bot: Bot, message: dict[str, Any]) -> None:
        if bot.sock is None:
            return
        try:
            bot.sock.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        except OSError:
            pass

    @staticmethod
    def _close(bot: Bot) -> None:
        sock, bot.sock = bot.sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    # ---- simulation ------------------------------------------------------------------------------------------------

    def _simulate(self, now: float, dt: float) -> None:
        ticks = dt / STEP_S
        for bot in self.bots:
            if bot.state != "online":
                continue
            self.tx_pkts += 20 * dt
            if bot.respawn_at is not None and now >= bot.respawn_at:
                bot.respawn_at = None
                self._send(bot, {"op": "respawn"})
            if self.flags.walk == "circle" and bot.pos is not None and bot.respawn_at is None:
                x, y, z = bot.pos
                for _ in range(max(1, round(ticks))):
                    x += WALK_SPEED * math.cos(bot.angle)
                    z += WALK_SPEED * math.sin(bot.angle)
                    bot.angle += WALK_TURN
                bot.pos = (x, y, z)
                if now - bot.last_move >= MOVE_REPORT_S:
                    bot.last_move = now
                    self._send(bot, {"op": "move", "x": x, "y": y, "z": z})
            self._deliver_chunks(bot, now, dt)
            for rate, packet in RATE_PACKETS.items():
                amount = bot.rates.get(rate, 0.0) * dt
                bot.rx[packet] += amount
                if rate == "corrections":
                    bot.corrections += amount

    def _deliver_chunks(self, bot: Bot, now: float, dt: float) -> None:
        radius = max(1, bot.radius // 2) if bot.csg else bot.radius
        offsets = self.offsets.setdefault(radius, _offsets(radius))
        cx, cz = bot.chunk()
        missing = [(bot.dim, cx + dx, cz + dz) for dx, dz in offsets if (bot.dim, cx + dx, cz + dz) not in bot.chunks]
        if not missing:
            bot.budget = 0.0
            return
        bot.budget += bot.rates.get("chunks", 0.0) * dt
        count = min(int(bot.budget), len(missing))
        bot.budget -= count
        since_tp = None if bot.first_tp is None else (now - bot.first_tp) * 1000
        for key in missing[:count]:
            bot.chunks[key] = since_tp
            bot.level_chunks += 1
            bot.rx["LevelChunk"] += 1
        if count and since_tp is not None and not bot.first_chunk_sent:
            bot.first_chunk_sent = True
            self.events.emit("first_chunk", bot=bot.name, dt_ms=round(since_tp))

    # ---- reports ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _row(bot: Bot) -> list[Any]:
        """``[bot, uniq, in_view, x, y, z, dim, corrections, teleports]``; no position before the first teleport."""
        if bot.pos is None:
            position: list[Any] = [None, None, None, None]
        else:
            position = [round(bot.pos[0], 2), round(bot.pos[1], 2), round(bot.pos[2], 2), bot.dim]
        return [bot.name, len(bot.chunks), bot.in_view(), *position, int(bot.corrections), bot.teleports]

    def _stat(self) -> dict[str, int]:
        rx_pkts = sum(int(sum(b.rx.values())) for b in self.bots)
        rx_bytes = sum(int(b.rx[name] * size) for b in self.bots for name, size in PACKETS.items())
        return {
            "online": sum(b.state == "online" for b in self.bots),
            "joined": sum(b.joined for b in self.bots),
            "failed": self.failed,
            "dropped": self.dropped,
            "left": self.left,
            "rejoins": sum(b.rejoins for b in self.bots),
            "deaths": sum(b.deaths for b in self.bots),
            "rx_pkts": rx_pkts,
            "tx_pkts": int(self.tx_pkts),
            "rx_bytes": rx_bytes,
            "tx_bytes": int(self.tx_pkts) * 80,
            "corrections": sum(int(b.corrections) for b in self.bots),
            "level_chunks": sum(b.level_chunks for b in self.bots),
            "subchunks": sum(b.level_chunks for b in self.bots if not b.csg) * SUBCHUNKS_PER_COLUMN,
            "uniq": sum(len(b.chunks) for b in self.bots),
            "in_view": sum(b.in_view() for b in self.bots),
            "decode_errors": self.decode_errors,
        }

    def _pkt(self) -> None:
        totals = {name: sum(int(b.rx[name]) for b in self.bots) for name in PACKETS}
        delta = {name: max(0, totals[name] - self.last_pkt[name]) for name in PACKETS}
        self.last_pkt = totals
        self.events.emit("pkt", rx=delta)

    def _rtt(self) -> None:
        probes = min(self.flags.probe_bots, sum(b.state == "online" for b in self.bots))
        samples = probes * max(1, round(self.flags.stat_interval))
        if samples == 0:
            self.events.emit("rtt", n=0, p50_ms=None, p99_ms=None, max_ms=None, lost=0)
        else:
            self.events.emit("rtt", n=samples, p50_ms=2.0, p99_ms=8.0, max_ms=9.5, lost=0)


# --------------------------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    flags = parse_flags(sys.argv[1:] if argv is None else argv)
    if flags.version:
        print(json.dumps(BUILD, separators=(",", ":")))
        return EXIT_OK
    events = Events()
    if flags.selftest_output:
        return selftest(events)
    fleet = Fleet(flags, events)

    def stop(signum: int, frame: object) -> None:
        fleet.quit.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    return fleet.run()


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the stdin thread may still block on input; skip interpreter finalisation
