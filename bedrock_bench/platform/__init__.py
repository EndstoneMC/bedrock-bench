# SPDX-License-Identifier: Apache-2.0
"""Operating-system access: per-thread CPU time, memory, affinity, topology and process start-up.

:func:`current` returns the implementation for this host, :mod:`bedrock_bench.platform.windows` or
:mod:`bedrock_bench.platform.linux`. Both expose the same names:

- ``ThreadReader(pid)``: ``snapshot() -> list[ThreadStat]`` reads every thread of a process; ``close()``.
- ``TickReader(pid, tid)``: ``read() -> (cpu_ns, cycles | None)`` reads one thread cheaply, ``exited()``,
  ``close()``; raises :class:`ProcessGone` once the thread is gone.
- ``process_table() -> list[ProcEntry]``: every process with parent, name and CPU time, in one cheap pass.
- ``process_memory(pid) -> (resident_mib, private_mib)``, ``process_handles(pid) -> int | None``.
- ``process_affinity(pid)``, ``thread_affinity(pid, tid)``, ``allowed_cpus()``, ``set_affinity(pid, cpus)``.
- ``get_priority(pid)``, ``set_priority(pid, priority)``, ``can_set_priority(priority)``, ``interrupt(pid)``.
- ``cpu_records() -> list[CpuRecord]``, ``cpu_model()``, ``virtualization()``, ``power_settings()``,
  ``loaded_modules(pid)``.

This module adds :func:`thread_infos`, the live threads of a process as :class:`bedrock_bench.model.ThreadInfo`.
Everything above this package goes through :func:`current` and never checks the OS itself.

Servers are black boxes here: everything comes from what the operating system reports about a process.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from types import ModuleType

from bedrock_bench.model import ThreadInfo

MIB = 1024 * 1024

#: Scheduling priorities a machine profile may ask for (``model.PRIORITIES``), mapped per OS by the implementations.
PRIORITIES = ("normal", "above_normal")


class ProcessGone(Exception):
    """The process or thread being read has exited."""


class PlatformUnsupported(RuntimeError):
    """This operating system has no implementation (analysis and reports still work everywhere)."""


@dataclass(frozen=True)
class ThreadStat:
    """One reading of one thread."""

    tid: int
    name: str  # OS thread name: Windows thread description, Linux ``comm``; "" when unnamed
    cpu_ns: int  # user + kernel CPU time since the thread started
    t_ns: int  # harness monotonic time taken right after the read
    cycles: int | None = None  # Windows QueryThreadCycleTime (TSC ticks while running)
    created_ns: int | None = None  # creation time on the harness monotonic clock
    start_module: str | None = None  # module that holds the thread's start address (Windows)
    alive: bool = True  # False on the final reading of a thread that has exited


@dataclass(frozen=True)
class ProcEntry:
    """One row of the process table."""

    pid: int
    ppid: int
    name: str
    cpu_s: float  # user + system CPU seconds since the process started
    started: int  # start time in OS units; with the pid it identifies a process across pid reuse


@dataclass(frozen=True)
class CpuRecord:
    """One logical CPU available to the harness."""

    cpu: int  # index used in affinity masks
    core: int  # physical core; SMT siblings share it
    package: int = 0
    efficiency: int = 0  # higher is the faster class; equal on every CPU of a non-hybrid machine


def name() -> str:
    """``windows`` or ``linux``; anything else raises :class:`PlatformUnsupported`."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    raise PlatformUnsupported(f"no platform support for {sys.platform!r}; only analyze and report work here")


def current() -> ModuleType:
    """The implementation module for this host."""
    return importlib.import_module(f"{__name__}.{name()}")


def supported() -> bool:
    try:
        name()
    except PlatformUnsupported:
        return False
    return True


def sanitize_name(text: str) -> str:
    """A thread or process name safe for one CSV field on one line."""
    return "".join(" " if ord(ch) < 32 else ch for ch in text).strip()


def thread_infos(pid: int) -> list[ThreadInfo]:
    """The live threads of a process, for ``ServerAdapter.tick_thread`` and the thread census."""
    reader = current().ThreadReader(pid)
    try:
        return [to_thread_info(stat) for stat in reader.snapshot() if stat.alive]
    finally:
        reader.close()


def to_thread_info(stat: ThreadStat) -> ThreadInfo:
    return ThreadInfo(tid=stat.tid, name=stat.name, start_module=stat.start_module, created_ns=stat.created_ns)
