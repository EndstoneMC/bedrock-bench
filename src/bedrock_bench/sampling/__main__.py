# SPDX-License-Identifier: Apache-2.0
"""``python -m bedrock_bench.sampling``: the process samplers as a standalone sidecar.

The Docker runtime runs this in a container that shares the server container's PID namespace
(``--pid container:<server>``), so samples are stamped where they are read instead of after a ``docker exec`` round
trip. Stamps use this process's monotonic clock plus ``--offset-ns``; on native Linux the offset is 0 because every
container shares the host kernel's clock.

    python -m bedrock_bench.sampling --out /series --exe /data/bedrock_server

Control is JSON lines on stdin; events are JSON lines on stdout:

- ``{"op": "threads"}`` answers ``{"ev": "threads", "threads": [{"tid", "name", "start_module", "created_ns"}]}``.
- ``{"op": "tick", "tid": N}`` starts the tick sampler on thread N and answers ``{"ev": "tick", "tid": N}``.
- ``{"op": "stop"}``, the end of stdin, SIGTERM or SIGINT stop every sampler.

It prints ``{"ev": "hello", "pid", "mono_ns", "epoch_ms"}`` once the server is found, ``{"ev": "clock", "mono_ns"}``
every ``--clock-interval`` seconds so the runner can fit the clock offset, and
``{"ev": "done", "samplers": {name: {"samples", "ended", "error"}}}`` at exit.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psutil

from bedrock_bench.clock import Stamp, mono_ns
from bedrock_bench.model import ThreadInfo
from bedrock_bench.platform import ProcessGone, thread_infos
from bedrock_bench.sampling.base import PeriodicSampler
from bedrock_bench.sampling.process import ProcessSampler
from bedrock_bench.sampling.series import PROCESS_FILE, THREADS_FILE, TICK_FILE
from bedrock_bench.sampling.threads import ThreadSampler
from bedrock_bench.sampling.tick import TickSampler

_print_lock = threading.Lock()


def emit(event: dict[str, Any]) -> None:
    with _print_lock:
        sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def find_process(exe: str, timeout_s: float) -> psutil.Process:
    """The first process whose executable path is ``exe``, waiting up to ``timeout_s`` seconds."""
    deadline = time.monotonic() + timeout_s
    while True:
        for proc in psutil.process_iter(["exe"]):
            path = proc.info.get("exe")
            if path and (path == exe or os.path.realpath(path) == os.path.realpath(exe)):
                return proc
        if time.monotonic() >= deadline:
            raise SystemExit(f"no process running {exe} after {timeout_s:.0f} s")
        time.sleep(0.2)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bedrock_bench.sampling", description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pid", type=int, help="server process id")
    target.add_argument("--exe", help="server executable path, as the server's own mount namespace sees it")
    parser.add_argument("--launcher-pid", type=int, help="process that started the server, for the tree columns")
    parser.add_argument("--out", type=Path, required=True, help="directory for the series files")
    parser.add_argument("--wait", type=float, default=600.0, help="seconds to wait for --exe (default 600)")
    parser.add_argument("--offset-ns", type=int, default=0, help="added to every stamp (default 0)")
    parser.add_argument("--threads-interval", type=float, default=1.0)
    parser.add_argument("--process-interval", type=float, default=1.0)
    parser.add_argument("--tick-interval", type=float, default=0.1)
    parser.add_argument("--clock-interval", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    offset = args.offset_ns

    def clock() -> int:
        return mono_ns() + offset

    server = psutil.Process(args.pid) if args.pid is not None else find_process(args.exe, args.wait)
    launcher = psutil.Process(args.launcher_pid) if args.launcher_pid is not None else None
    stamp = Stamp.now()
    emit({"ev": "hello", "pid": server.pid, "mono_ns": stamp.mono_ns + offset, "epoch_ms": stamp.epoch_ms})

    args.out.mkdir(parents=True, exist_ok=True)
    samplers: dict[str, PeriodicSampler] = {
        "threads": ThreadSampler(server.pid, args.out / THREADS_FILE, interval_s=args.threads_interval, clock=clock),
        "process": ProcessSampler(
            server, args.out / PROCESS_FILE, launcher=launcher, interval_s=args.process_interval, clock=clock
        ),
    }
    for sampler in samplers.values():
        sampler.start()

    done = threading.Event()

    def heartbeat() -> None:
        while not done.wait(args.clock_interval):
            emit({"ev": "clock", "mono_ns": clock()})

    threading.Thread(target=heartbeat, name="bbench-sidecar-clock", daemon=True).start()

    def on_signal(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                command = json.loads(line)
            except ValueError:
                emit({"ev": "error", "error": "not JSON", "line": line.strip()[:200]})
                continue
            op = command.get("op")
            if op == "stop":
                break
            try:
                if op == "threads":
                    infos = thread_infos(server.pid)
                    emit({"ev": "threads", "threads": [_info_json(i, offset) for i in infos]})
                elif op == "tick" and "tick" not in samplers:
                    tid = int(command["tid"])
                    tick = TickSampler(
                        server.pid, tid, args.out / TICK_FILE, interval_s=args.tick_interval, clock=clock
                    )
                    samplers["tick"] = tick
                    tick.start()
                    emit({"ev": "tick", "tid": tid})
                else:
                    emit({"ev": "error", "error": f"unknown or repeated op {op!r}"})
            except (ProcessGone, KeyError, ValueError) as exc:
                emit({"ev": "error", "op": op, "error": repr(exc)})
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        for sampler in samplers.values():
            sampler.stop()
        emit(
            {
                "ev": "done",
                "samplers": {
                    name: {"samples": s.samples, "ended": s.ended, "error": repr(s.error) if s.error else None}
                    for name, s in samplers.items()
                },
            }
        )
    return 0


def _info_json(info: ThreadInfo, offset_ns: int) -> dict[str, Any]:
    created = None if info.created_ns is None else info.created_ns + offset_ns
    return {"tid": info.tid, "name": info.name, "start_module": info.start_module, "created_ns": created}


if __name__ == "__main__":
    raise SystemExit(main())
