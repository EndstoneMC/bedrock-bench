# SPDX-License-Identifier: Apache-2.0
"""The sampler loop and process-group CPU accounting shared by the samplers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import psutil

from bedrock_bench import platform
from bedrock_bench.clock import mono_ns
from bedrock_bench.platform import ProcEntry, ProcessGone

log = logging.getLogger(__name__)


class PeriodicSampler(threading.Thread):
    """Calls :meth:`sample` every ``interval_s`` seconds in a daemon thread.

    The schedule is absolute, so it does not drift; a sample that overruns skips the missed slots and counts them in
    :attr:`overruns`. :meth:`stop` takes one last sample so a window that ends at stop has a reading at its end.
    When the target exits, :meth:`sample` raises :class:`ProcessGone` and the sampler ends quietly with the data it
    has. Any other exception ends the sampler too and is kept in :attr:`error` for the run's ``harness_ok`` gate.

    Subclasses implement :meth:`sample` and may override :meth:`setup` and :meth:`teardown`.
    """

    def __init__(self, name: str, interval_s: float, *, clock: Callable[[], int] = mono_ns) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        super().__init__(name=name, daemon=True)
        self.interval_s = interval_s
        self.clock = clock
        self.samples = 0
        self.overruns = 0
        self.error: BaseException | None = None
        self.ended: str | None = None  # stopped | target_exited | error
        self._halt = threading.Event()

    def setup(self) -> None:
        """Runs once in the sampler thread before the first sample."""

    def sample(self) -> None:
        raise NotImplementedError

    def teardown(self) -> None:
        """Runs once in the sampler thread after the last sample, also after an error."""

    def stop(self, timeout: float | None = 30.0) -> None:
        self._halt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def run(self) -> None:
        try:
            self.setup()
            deadline = time.perf_counter()
            while True:
                self.sample()
                self.samples += 1
                deadline += self.interval_s
                delay = deadline - time.perf_counter()
                if delay < 0:
                    missed = int(-delay // self.interval_s) + 1
                    self.overruns += missed
                    deadline += missed * self.interval_s
                    delay = deadline - time.perf_counter()
                if self._halt.wait(max(0.0, delay)):
                    self._final_sample()
                    self.ended = "stopped"
                    break
        except ProcessGone:
            self.ended = "target_exited"
        except BaseException as exc:
            self.error = exc
            self.ended = "error"
            log.exception("sampler %s failed", self.name)
        finally:
            try:
                self.teardown()
            except Exception as exc:
                log.exception("sampler %s failed to close", self.name)
                self.error = self.error or exc

    def _final_sample(self) -> None:
        try:
            self.sample()
            self.samples += 1
        except ProcessGone:
            pass


def children_map(table: Iterable[ProcEntry] | None = None) -> dict[int, list[int]]:
    """``{ppid: [pid, ...]}`` from one process table (read now if not given).

    A parent pid can outlive its process and be reused, so a child counts only if it started after its parent.
    """
    entries = list(platform.current().process_table() if table is None else table)
    started = {e.pid: e.started for e in entries}
    children: dict[int, list[int]] = {}
    for entry in entries:
        parent_started = started.get(entry.ppid)
        if entry.ppid != entry.pid and parent_started is not None and parent_started <= entry.started:
            children.setdefault(entry.ppid, []).append(entry.pid)
    return children


def descendants(children: Mapping[int, list[int]], pid: int) -> set[int]:
    """Every pid below ``pid`` in a :func:`children_map`."""
    found: set[int] = set()
    stack = list(children.get(pid, ()))
    while stack:
        child = stack.pop()
        if child not in found:
            found.add(child)
            stack.extend(children.get(child, ()))
    return found


@dataclass
class _Member:
    proc: psutil.Process
    user_s: float = 0.0
    sys_s: float = 0.0
    base_user_s: float = 0.0
    base_sys_s: float = 0.0
    alive: bool = True


class ProcessGroup:
    """CPU time of one or more process trees, counted from the moment each process joined the group.

    Membership is refreshed from a :func:`children_map`; reading CPU touches only known members, so it is cheap
    enough every second. Counting from the join keeps a newly found member from adding a jump of CPU it used before
    it was watched. A member that exits keeps its last reading, so the group total never goes down.
    """

    def __init__(self, roots: Iterable[psutil.Process | int] = ()) -> None:
        self._roots: dict[int, psutil.Process] = {}
        self._members: dict[int, _Member] = {}
        self._retired = (0.0, 0.0)  # CPU of exited members whose pid was reused inside the group
        for root in roots:
            self.add_root(root)

    @property
    def pids(self) -> set[int]:
        return {pid for pid, m in self._members.items() if m.alive}

    @property
    def root_pids(self) -> set[int]:
        return set(self._roots)

    def add_root(self, root: psutil.Process | int) -> None:
        proc = root if isinstance(root, psutil.Process) else psutil.Process(root)
        self._roots[proc.pid] = proc
        self._add(proc)

    def refresh(self, children: Mapping[int, list[int]], exclude: Iterable[int] = ()) -> set[int]:
        """Add every descendant of the roots, not descending into ``exclude``. Returns the live member pids."""
        skip = set(exclude)
        stack = [pid for pid in self._roots if pid not in skip]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            known = self._members.get(pid)
            if known is not None and not known.alive and pid in self._roots:
                continue  # a root that exited stays exited, even if its pid comes back
            if known is None or not known.alive:
                try:
                    proc = psutil.Process(pid)
                except psutil.Error:
                    continue
                if known is not None:
                    self._retire(known)
                self._add(proc)
            stack.extend(c for c in children.get(pid, ()) if c not in skip)
        return self.pids

    def cpu(self) -> tuple[float, float]:
        """``(user_s, sys_s)`` used by every member ever seen since it joined."""
        user, system = self._retired
        for member in self._members.values():
            if member.alive:
                self._read(member)
            user += member.user_s - member.base_user_s
            system += member.sys_s - member.base_sys_s
        return user, system

    def _retire(self, member: _Member) -> None:
        user, system = self._retired
        self._retired = (user + member.user_s - member.base_user_s, system + member.sys_s - member.base_sys_s)

    def _add(self, proc: psutil.Process) -> None:
        member = _Member(proc)
        self._read(member)
        member.base_user_s, member.base_sys_s = member.user_s, member.sys_s
        self._members[proc.pid] = member

    @staticmethod
    def _read(member: _Member) -> None:
        try:
            if not member.proc.is_running():  # exited, or the pid now belongs to another process
                member.alive = False
                return
            times = member.proc.cpu_times()
        except psutil.Error:
            member.alive = False
            return
        member.user_s, member.sys_s = times.user, times.system
