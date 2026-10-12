# SPDX-License-Identifier: Apache-2.0
"""Start, watch and stop processes with their CPU placement in force from the first instruction.

Pinning a process after it starts is too late: the JVM, BDS and most runtimes size their thread pools from the CPUs
they see at start-up. So:

- **Windows**: the process is created with ``CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP``, put in a kill-on-close
  job, given its affinity mask and priority class, and only then resumed. Children inherit the mask and the job but
  not an above-normal priority class, so call :func:`apply_placement` on a server that a launcher starts as a child.
- **Linux**: affinity and nice are set in ``preexec_fn`` between ``fork`` and ``exec``, and every thread and child
  inherits them. The child starts its own session, so the whole tree can be killed as one process group.

``LaunchSpec.stdin_lines`` are not written here: every console write goes through the console's journal.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import psutil

from bedrock_bench import platform
from bedrock_bench.clock import Stamp, mono_ns
from bedrock_bench.model import LaunchSpec, Placement

log = logging.getLogger(__name__)

ROLES = ("server", "bot", "harness")


class LaunchError(RuntimeError):
    """A process could not be started with the requested placement."""


def build_env(spec: LaunchSpec, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child environment: ``base`` (default ``os.environ``) minus ``env_remove``, plus ``env_set``."""
    source = dict(os.environ if base is None else base)
    if sys.platform == "win32":
        remove = {key.upper() for key in spec.env_remove}
        env = {k: v for k, v in source.items() if k.upper() not in remove}
    else:
        remove = set(spec.env_remove)
        env = {k: v for k, v in source.items() if k not in remove}
    env.update(spec.env_set)
    return env


def role_placement(placement: Placement, role: str) -> tuple[tuple[int, ...], str]:
    """CPU set and priority of one role. Only the server gets the profile priority; bots and harness run normal."""
    if role == "server":
        return placement.server_cpus, placement.priority
    if role == "bot":
        return placement.bot_cpus, "normal"
    if role == "harness":
        return placement.harness_cpus, "normal"
    raise ValueError(f"unknown role {role!r}; use one of {', '.join(ROLES)}")


@dataclass(eq=False)
class Launched:
    """A started process and how it was placed."""

    popen: subprocess.Popen[bytes]
    process: psutil.Process
    cpus: tuple[int, ...] | None
    priority: str
    method: str  # suspended-launch | preexec | none
    started: Stamp
    verified: bool = False
    job: Any = None  # Windows kill-on-close job
    _closed: bool = field(default=False, repr=False)

    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def stdin(self) -> IO[bytes] | None:
        return self.popen.stdin

    @property
    def stdout(self) -> IO[bytes] | None:
        return self.popen.stdout

    def poll(self) -> int | None:
        return self.popen.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        """The exit code, or None if still running after ``timeout`` seconds."""
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def verify(self) -> bool:
        """Whether the process affinity equals the planned CPU set (always True when unpinned)."""
        if self.cpus is None:
            return True
        try:
            return platform.current().process_affinity(self.pid) == tuple(sorted(self.cpus))
        except platform.ProcessGone:
            return False

    def descendants(self) -> list[psutil.Process]:
        try:
            return self.process.children(recursive=True)
        except psutil.Error:
            return []

    def interrupt(self) -> bool:
        """CTRL_BREAK (Windows) or SIGINT (Linux), so the program can flush and exit. False if it could not be sent."""
        if self.poll() is not None:
            return False
        try:
            platform.current().interrupt(self.pid)
        except OSError as exc:
            log.debug("interrupt to %d failed: %s", self.pid, exc)
            return False
        return True

    def kill_tree(self, timeout_s: float = 10.0) -> list[int]:
        """Kill the process and every descendant. Returns the pids that were still running."""
        victims = [self.process, *self.descendants()]
        alive = [p for p in victims if _running(p)]
        if self.job is not None:
            self.job.terminate()
        elif sys.platform != "win32":
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
        # Popen reaps its own child, so the exit code is kept; psutil would reap it first and leave Popen with 0.
        self.wait(timeout_s)
        psutil.wait_procs([p for p in alive if p.pid != self.pid], timeout=timeout_s)
        return [p.pid for p in alive]

    def close(self) -> None:
        """Close the pipes and, on Windows, release the job, which kills anything still in it."""
        if self._closed:
            return
        self._closed = True
        for pipe in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        if self.job is not None:
            self.job.close()
            self.job = None


def _running(proc: psutil.Process) -> bool:
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def launch(
    spec: LaunchSpec,
    placement: Placement | None = None,
    *,
    role: str = "server",
    cpus: Sequence[int] | None = None,
    priority: str | None = None,
    stdin: int | IO[Any] | None = subprocess.PIPE,
    stdout: int | IO[Any] | None = subprocess.PIPE,
    stderr: int | IO[Any] | None = subprocess.STDOUT,
    kill_on_close: bool = True,
) -> Launched:
    """Start ``spec`` with the CPU set and priority of ``role`` in ``placement``.

    ``cpus`` and ``priority`` override the placement. Without either the process runs unpinned at normal priority.
    Pipes are binary; the caller flushes stdin after each write. With ``kill_on_close`` (Windows) the process tree
    dies with the harness.
    """
    planned_cpus: tuple[int, ...] | None = None
    planned_priority = "normal"
    if placement is not None:
        planned_cpus, planned_priority = role_placement(placement, role)
    if cpus is not None:
        planned_cpus = tuple(sorted(set(cpus)))
    if priority is not None:
        planned_priority = priority
    if planned_priority not in platform.PRIORITIES:
        raise LaunchError(f"unknown priority {planned_priority!r}")
    if planned_cpus is not None and not planned_cpus:
        raise LaunchError("empty CPU set")

    argv = list(spec.argv)
    kwargs: dict[str, Any] = {
        "cwd": spec.cwd,
        "env": build_env(spec),
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
    }
    if sys.platform == "win32":
        return _launch_windows(argv, kwargs, planned_cpus, planned_priority, kill_on_close)
    return _launch_posix(argv, kwargs, planned_cpus, planned_priority)


def _launch_windows(
    argv: list[str], kwargs: dict[str, Any], cpus: tuple[int, ...] | None, priority: str, kill_on_close: bool
) -> Launched:
    win = platform.current()
    try:
        popen = subprocess.Popen(argv, creationflags=win.CREATE_SUSPENDED | win.CREATE_NEW_PROCESS_GROUP, **kwargs)
    except OSError as exc:
        raise LaunchError(f"cannot start {argv[0]}: {exc}") from exc
    started = Stamp.now()
    job = None
    try:
        if kill_on_close:
            try:
                job = win.Job()
                job.assign(popen.pid)
            except OSError as exc:
                log.warning("process %d runs outside a kill-on-close job: %s", popen.pid, exc)
                if job is not None:
                    job.close()
                job = None
        if cpus is not None:
            win.set_affinity(popen.pid, cpus)
        win.set_priority(popen.pid, priority)
        win.resume_process(popen.pid)
    except BaseException as exc:
        popen.kill()
        popen.wait()
        if job is not None:
            job.close()
        if isinstance(exc, (OSError, psutil.Error)):
            raise LaunchError(f"cannot place {argv[0]} on CPUs {cpus} at {priority}: {exc}") from exc
        raise
    launched = Launched(
        popen=popen,
        process=psutil.Process(popen.pid),
        cpus=cpus,
        priority=priority,
        method="suspended-launch" if cpus is not None else "none",
        started=started,
        job=job,
    )
    launched.verified = launched.verify()
    return launched


def _launch_posix(argv: list[str], kwargs: dict[str, Any], cpus: tuple[int, ...] | None, priority: str) -> Launched:
    lin = platform.current()
    if not lin.can_set_priority(priority):
        raise LaunchError(f"priority {priority} needs root, CAP_SYS_NICE or a higher RLIMIT_NICE")
    try:
        popen = subprocess.Popen(argv, start_new_session=True, preexec_fn=lin.preexec(cpus, priority), **kwargs)
    except subprocess.SubprocessError as exc:
        raise LaunchError(f"cannot place {argv[0]} on CPUs {cpus} at {priority}: {exc}") from exc
    except OSError as exc:
        raise LaunchError(f"cannot start {argv[0]}: {exc}") from exc
    launched = Launched(
        popen=popen,
        process=psutil.Process(popen.pid),
        cpus=cpus,
        priority=priority,
        method="preexec" if cpus is not None else "none",
        started=Stamp.now(),
    )
    launched.verified = launched.verify()
    return launched


def apply_placement(proc: psutil.Process, cpus: Sequence[int] | None, priority: str) -> None:
    """Pin and prioritise a process that is already running, e.g. a server a launcher started as a child."""
    impl = platform.current()
    if cpus is not None:
        impl.set_affinity(proc.pid, cpus)
    impl.set_priority(proc.pid, priority)


def pin_current_process(cpus: Sequence[int]) -> None:
    """Move the harness itself onto its CPU set."""
    platform.current().set_affinity(os.getpid(), cpus)


def _norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def wait_for_descendant(root: psutil.Process, exe: str | Path, timeout_s: float, poll_s: float = 0.1) -> psutil.Process:
    """The first process in ``root``'s tree (``root`` included) whose executable is ``exe``.

    Matching is by resolved executable path, never by name: launchers rename threads and processes.
    """
    wanted = _norm_path(exe)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if not _running(root):
                raise psutil.NoSuchProcess(root.pid)
            candidates = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            raise LaunchError(f"process {root.pid} exited before starting {exe}") from None
        for proc in candidates:
            try:
                if _norm_path(proc.exe()) == wanted:
                    return proc
            except (psutil.Error, OSError):
                continue
        if time.monotonic() >= deadline:
            raise LaunchError(f"no process running {exe} under pid {root.pid} after {timeout_s:.0f} s")
        time.sleep(poll_s)


# --------------------------------------------------------------------------------------------------------------------
# Death watch
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeathInfo:
    """An exit that happened before teardown began."""

    t_ns: int  # harness monotonic time the exit was noticed (within one poll interval)
    epoch_ms: int
    pid: int
    role: str  # "server" (the measured process) or "launcher" (the process the harness started)
    exit_code: int | None


class DeathWatch:
    """Sets :attr:`died` when the server or its launcher exits before :meth:`expect_exit` is called.

    A crash, a watchdog shutdown or an early exit is a ``server_failed`` outcome; this watch is how phases learn
    about it without waiting for a timeout. ``on_death`` runs on the watch thread before :attr:`died` is set, so
    keep it short.
    """

    def __init__(
        self,
        launched: Launched,
        server: psutil.Process | None = None,
        *,
        interval_s: float = 0.2,
        clock: Callable[[], int] = mono_ns,
        on_death: Callable[[DeathInfo], None] | None = None,
    ) -> None:
        self.launched = launched
        self.server = server
        self.interval_s = interval_s
        self.died = threading.Event()
        self.info: DeathInfo | None = None
        self._clock = clock
        self._on_death = on_death
        self._expecting = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bbench-deathwatch", daemon=True)

    def start(self) -> DeathWatch:
        self._thread.start()
        return self

    def expect_exit(self) -> None:
        """Teardown has begun; exits from now on are expected."""
        self._expecting.set()

    def stop(self) -> None:
        self._expecting.set()
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            found = self._check()
            if found is not None:
                if not self._expecting.is_set():
                    log.warning(
                        "%s process %d exited (code %s) before teardown", found.role, found.pid, found.exit_code
                    )
                    self.info = found
                    try:
                        if self._on_death is not None:
                            self._on_death(found)
                    finally:
                        self.died.set()  # last, so a waiter sees the callback's effects
                return
            self._stop.wait(self.interval_s)

    def _check(self) -> DeathInfo | None:
        server = self.server
        if server is not None and server.pid != self.launched.pid and not _running(server):
            return self._record(server.pid, "server", _exit_code(server))
        code = self.launched.poll()
        if code is not None:
            role = "server" if server is None or server.pid == self.launched.pid else "launcher"
            return self._record(self.launched.pid, role, code)
        return None

    def _record(self, pid: int, role: str, exit_code: int | None) -> DeathInfo:
        stamp = Stamp.now()
        return DeathInfo(t_ns=self._clock(), epoch_ms=stamp.epoch_ms, pid=pid, role=role, exit_code=exit_code)


def _exit_code(proc: psutil.Process) -> int | None:
    try:
        return proc.wait(timeout=0)
    except (psutil.Error, psutil.TimeoutExpired):
        return None
