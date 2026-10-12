# SPDX-License-Identifier: Apache-2.0
"""Windows implementation of :mod:`bedrock_bench.platform` (ctypes over kernel32 and ntdll, psutil for the rest).

- Thread CPU and creation times come from one ``NtQuerySystemInformation`` call per sample (the process table
  with its threads), not from a call per thread: a sampler that makes hundreds of short native
  calls per second stalls behind any other busy Python thread, because each call gives up and re-takes the GIL.
- Each thread is also opened once and the handle is kept until the thread is gone. That reserves the thread id, so
  it cannot be reused while the sampler tracks it, and gives the exact final CPU time of a thread that exited.
- CPU time is user + kernel time in 100 ns units, advanced at the scheduler tick (about 15.6 ms). The tick reader
  also reads ``QueryThreadCycleTime`` as a finer secondary column, converted with :func:`calibrate_tsc`.
- Names are thread descriptions (``GetThreadDescription``), read when a thread appears and refreshed every
  ``name_refresh_s`` seconds. The start module, the module that contains the thread's start address
  (``NtQueryInformationThread`` class 9), is read once when a thread appears.
- Topology comes from ``GetSystemCpuSetInformation``: ``EfficiencyClass`` separates performance from efficiency
  cores and ``CoreIndex`` groups SMT siblings.
- Processes start with ``CREATE_SUSPENDED``; ``process.py`` sets affinity, priority and the job, then calls
  :func:`resume_process`.

Affinity masks cover processor group 0 only (64 logical CPUs). Needs 64-bit Python. The DLLs are loaded on first
use, so this module imports on any OS (its tests are skipped elsewhere).
"""

from __future__ import annotations

import ctypes
import functools
import os
import re
import signal
import struct
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import psutil

from bedrock_bench.clock import mono_ns
from bedrock_bench.platform import MIB, CpuRecord, ProcEntry, ProcessGone, ThreadStat, sanitize_name

# Process creation flags (subprocess.Popen creationflags).
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Access rights.
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_INFORMATION = 0x0040
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
SYNCHRONIZE = 0x00100000

WAIT_OBJECT_0 = 0
LIST_MODULES_ALL = 0x03
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# NtQuerySystemInformation(SystemProcessInformation): a chain of process records (x64 layout), each followed by
# its thread records.
SYSTEM_PROCESS_INFORMATION = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_PROCESS_RECORD = 256
_THREAD_RECORD = 80
_THREAD_TERMINATED = 4
THREAD_QUERY_SET_WIN32_START_ADDRESS = 9

#: FILETIME of the Unix epoch (100 ns intervals since 1601-01-01).
_EPOCH_AS_FILETIME = 116_444_736_000_000_000

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_INVALID_HANDLE = ctypes.c_void_p(-1).value

PRIORITY_CLASSES = {"normal": 0x20, "above_normal": 0x8000}
_CLASS_NAMES = {
    0x40: "idle",
    0x4000: "below_normal",
    0x20: "normal",
    0x8000: "above_normal",
    0x80: "high",
    0x100: "realtime",
}

# Power schemes that ship with Windows; custom schemes are reported by name.
POWER_SCHEMES = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "high_performance",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "power_saver",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "ultimate_performance",
}

# BIOS strings of common hypervisors (HKLM\HARDWARE\DESCRIPTION\System\BIOS).
_HYPERVISORS = (
    ("vmware", "vmware"),
    ("virtualbox", "virtualbox"),
    ("innotek", "virtualbox"),
    ("qemu", "kvm"),
    ("kvm", "kvm"),
    ("xen", "xen"),
    ("parallels", "parallels"),
    ("amazon ec2", "aws"),
    ("google compute engine", "gce"),
    ("virtual machine", "hyper-v"),
)


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", _DWORD), ("high", _DWORD)]

    @property
    def value(self) -> int:
        return (self.high << 32) | self.low


class _GROUP_AFFINITY(ctypes.Structure):
    _fields_ = [("Mask", ctypes.c_size_t), ("Group", ctypes.c_uint16), ("Reserved", ctypes.c_uint16 * 3)]


class _MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", _DWORD), ("EntryPoint", ctypes.c_void_p)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# --------------------------------------------------------------------------------------------------------------------
# DLL prototypes (loaded on first use)
# --------------------------------------------------------------------------------------------------------------------


@functools.cache
def _k32() -> ctypes.WinDLL:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sig: dict[str, tuple[object, list[object]]] = {
        "OpenProcess": (_HANDLE, [_DWORD, _BOOL, _DWORD]),
        "OpenThread": (_HANDLE, [_DWORD, _BOOL, _DWORD]),
        "CloseHandle": (_BOOL, [_HANDLE]),
        "GetThreadTimes": (_BOOL, [_HANDLE] + [ctypes.POINTER(_FILETIME)] * 4),
        "QueryThreadCycleTime": (_BOOL, [_HANDLE, ctypes.POINTER(ctypes.c_uint64)]),
        "GetThreadGroupAffinity": (_BOOL, [_HANDLE, ctypes.POINTER(_GROUP_AFFINITY)]),
        "WaitForSingleObject": (_DWORD, [_HANDLE, _DWORD]),
        "ResumeThread": (_DWORD, [_HANDLE]),
        "GetCurrentThread": (_HANDLE, []),
        "LocalFree": (ctypes.c_void_p, [ctypes.c_void_p]),
        "CreateJobObjectW": (_HANDLE, [ctypes.c_void_p, ctypes.c_wchar_p]),
        "SetInformationJobObject": (_BOOL, [_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD]),
        "AssignProcessToJobObject": (_BOOL, [_HANDLE, _HANDLE]),
        "TerminateJobObject": (_BOOL, [_HANDLE, ctypes.c_uint]),
        "K32EnumProcessModulesEx": (
            _BOOL,
            [_HANDLE, ctypes.POINTER(ctypes.c_void_p), _DWORD, ctypes.POINTER(_DWORD), _DWORD],
        ),
        "K32GetModuleInformation": (_BOOL, [_HANDLE, ctypes.c_void_p, ctypes.POINTER(_MODULEINFO), _DWORD]),
        "K32GetModuleBaseNameW": (_DWORD, [_HANDLE, ctypes.c_void_p, ctypes.c_wchar_p, _DWORD]),
        "GetSystemCpuSetInformation": (
            _BOOL,
            [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), _HANDLE, ctypes.c_ulong],
        ),
    }
    for fname, (restype, argtypes) in sig.items():
        fn = getattr(k32, fname)
        fn.restype = restype
        fn.argtypes = argtypes
    if hasattr(k32, "GetThreadDescription"):  # Windows 10 1607 and later
        k32.GetThreadDescription.restype = ctypes.c_long
        k32.GetThreadDescription.argtypes = [_HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    return k32


@functools.cache
def _ntdll() -> ctypes.WinDLL:
    nt = ctypes.WinDLL("ntdll")
    nt.NtQuerySystemInformation.restype = ctypes.c_long
    nt.NtQuerySystemInformation.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    nt.NtQueryInformationThread.restype = ctypes.c_long
    nt.NtQueryInformationThread.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    return nt


def _close(handle: int | None) -> None:
    if handle:
        _k32().CloseHandle(handle)


def _winerror(what: str) -> OSError:
    return ctypes.WinError(ctypes.get_last_error(), f"{what} failed")


# --------------------------------------------------------------------------------------------------------------------
# System process table
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemThread:
    tid: int
    cpu_100ns: int  # user + kernel
    created_filetime: int
    terminated: bool


@dataclass(frozen=True)
class SystemProcess:
    pid: int
    ppid: int
    name: str
    created_filetime: int
    cpu_100ns: int
    threads: tuple[SystemThread, ...]  # only for the processes asked for


_buffer_size = 1 << 20


def _query() -> tuple[ctypes.Array[ctypes.c_char], int]:
    """One ``NtQuerySystemInformation`` call, growing the buffer until it fits."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise OSError("bedrock-bench needs 64-bit Python on Windows")
    global _buffer_size
    nt = _ntdll()
    while True:
        buf = ctypes.create_string_buffer(_buffer_size)
        needed = ctypes.c_ulong(0)
        status = nt.NtQuerySystemInformation(SYSTEM_PROCESS_INFORMATION, buf, _buffer_size, ctypes.byref(needed))
        status &= 0xFFFFFFFF
        if status == STATUS_INFO_LENGTH_MISMATCH:
            _buffer_size = max(_buffer_size * 2, needed.value + (256 << 10))
            continue
        if status >= 0x80000000:
            raise OSError(f"NtQuerySystemInformation failed with status 0x{status:08x}")
        return buf, needed.value or _buffer_size


def system_processes(threads_of: int | None = None) -> Iterator[SystemProcess]:
    """Every process from one snapshot; threads are decoded only for ``threads_of``."""
    buf, length = _query()
    raw = memoryview(buf).cast("B")[:length]
    base = ctypes.addressof(buf)
    offset = 0
    while offset + _PROCESS_RECORD <= len(raw):
        next_offset, n_threads = struct.unpack_from("<II", raw, offset)
        created, user, kernel = struct.unpack_from("<qqq", raw, offset + 32)
        name_bytes, name_ptr = struct.unpack_from("<H6xQ", raw, offset + 56)
        pid, ppid = struct.unpack_from("<QQ", raw, offset + 80)
        name = ""
        if name_bytes and name_ptr:
            start = name_ptr - base
            if 0 <= start <= len(raw) - name_bytes:
                name = bytes(raw[start : start + name_bytes]).decode("utf-16-le", errors="replace")
        threads: list[SystemThread] = []
        if pid == threads_of:
            for i in range(n_threads):
                at = offset + _PROCESS_RECORD + i * _THREAD_RECORD
                t_kernel, t_user, t_created = struct.unpack_from("<qqq", raw, at)
                (tid,) = struct.unpack_from("<Q", raw, at + 48)
                (state,) = struct.unpack_from("<I", raw, at + 68)
                threads.append(SystemThread(tid, t_kernel + t_user, t_created, state == _THREAD_TERMINATED))
        yield SystemProcess(pid, ppid, name if pid else "System Idle Process", created, user + kernel, tuple(threads))
        if next_offset == 0:
            break
        offset += next_offset


def system_process(pid: int) -> SystemProcess | None:
    """``pid`` with its threads, or None when it is not in the table."""
    for proc in system_processes(threads_of=pid):
        if proc.pid == pid:
            return proc
    return None


def process_table() -> list[ProcEntry]:
    """Every process with its parent, name and CPU time, from one system call."""
    return [
        ProcEntry(
            pid=p.pid, ppid=p.ppid, name=sanitize_name(p.name), cpu_s=p.cpu_100ns / 1e7, started=p.created_filetime
        )
        for p in system_processes()
    ]


def thread_ids(pid: int) -> list[int]:
    proc = system_process(pid)
    return [t.tid for t in proc.threads if not t.terminated] if proc is not None else []


# --------------------------------------------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------------------------------------------


def _thread_times(handle: int) -> tuple[int, int]:
    """``(creation FILETIME, user + kernel in 100 ns units)``."""
    created, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
    ok = _k32().GetThreadTimes(
        handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
    )
    if not ok:
        raise _winerror("GetThreadTimes")
    return created.value, kernel.value + user.value


def _thread_cycles(handle: int) -> int | None:
    value = ctypes.c_uint64()
    if not _k32().QueryThreadCycleTime(handle, ctypes.byref(value)):
        return None
    return value.value


def _thread_description(handle: int) -> str:
    k32 = _k32()
    if not hasattr(k32, "GetThreadDescription"):
        return ""
    text = ctypes.c_wchar_p()
    if k32.GetThreadDescription(handle, ctypes.byref(text)) < 0:
        return ""
    try:
        return sanitize_name(text.value or "")
    finally:
        if text:
            k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _thread_start_address(handle: int) -> int | None:
    address = ctypes.c_void_p()
    status = _ntdll().NtQueryInformationThread(
        handle, THREAD_QUERY_SET_WIN32_START_ADDRESS, ctypes.byref(address), ctypes.sizeof(address), None
    )
    return address.value if status >= 0 and address.value else None


def _exited(handle: int) -> bool:
    return _k32().WaitForSingleObject(handle, 0) == WAIT_OBJECT_0


def _epoch_to_mono_offset() -> int:
    """``mono_ns - epoch_ns`` right now; adding it maps a wall-clock time onto the harness clock."""
    before = mono_ns()
    wall = time.time_ns()
    after = mono_ns()
    return (before + after) // 2 - wall


@dataclass
class _Module:
    base: int
    size: int
    name: str


class _ModuleMap:
    """Maps addresses in a process to module base names; refreshed when an address is not covered."""

    def __init__(self, pid: int) -> None:
        self._handle = _k32().OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        self._modules: list[_Module] = []

    def close(self) -> None:
        _close(self._handle)
        self._handle = None

    def names(self) -> list[str]:
        self._refresh()
        return sorted({m.name for m in self._modules}, key=str.lower)

    def lookup(self, address: int) -> str | None:
        found = self._find(address)
        if found is None:
            self._refresh()
            found = self._find(address)
        return found

    def _find(self, address: int) -> str | None:
        for module in self._modules:
            if module.base <= address < module.base + module.size:
                return module.name
        return None

    def _refresh(self) -> None:
        if not self._handle:
            return
        k32 = _k32()
        count = 512
        while True:
            array = (ctypes.c_void_p * count)()
            needed = _DWORD()
            if not k32.K32EnumProcessModulesEx(
                self._handle, array, ctypes.sizeof(array), ctypes.byref(needed), LIST_MODULES_ALL
            ):
                return
            if needed.value <= ctypes.sizeof(array):
                break
            count = needed.value // ctypes.sizeof(ctypes.c_void_p) + 16
        modules = []
        name_buf = ctypes.create_unicode_buffer(260)
        for hmod in array[: needed.value // ctypes.sizeof(ctypes.c_void_p)]:
            info = _MODULEINFO()
            if not hmod or not k32.K32GetModuleInformation(self._handle, hmod, ctypes.byref(info), ctypes.sizeof(info)):
                continue
            if k32.K32GetModuleBaseNameW(self._handle, hmod, name_buf, len(name_buf)):
                modules.append(_Module(info.lpBaseOfDll or 0, info.SizeOfImage, name_buf.value))
        self._modules = modules


@dataclass
class _Tracked:
    handle: int | None
    name: str
    created_ns: int
    start_module: str | None


class ThreadReader:
    """Reads every thread of one process. Keep one per process and call :meth:`snapshot` periodically."""

    cycles_supported = False  # per-thread cycles would cost a native call per thread; the tick reader has them

    def __init__(
        self,
        pid: int,
        *,
        clock: Callable[[], int] = mono_ns,
        start_modules: bool = True,
        name_refresh_s: float = 10.0,
    ) -> None:
        proc = system_process(pid)
        if proc is None:
            raise ProcessGone(pid)
        self.pid = pid
        self._started = proc.created_filetime  # tells this process from a later one that reuses the pid
        self._clock = clock
        self._tracked: dict[int, _Tracked] = {}
        self._offset = _epoch_to_mono_offset()
        self._modules = _ModuleMap(pid) if start_modules else None
        self._name_refresh_ns = int(name_refresh_s * 1e9)
        self._names_read_ns: int | None = None

    def close(self) -> None:
        for tracked in self._tracked.values():
            _close(tracked.handle)
        self._tracked.clear()
        if self._modules is not None:
            self._modules.close()

    def __enter__(self) -> ThreadReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def snapshot(self) -> list[ThreadStat]:
        """Live threads, plus one final reading (``alive=False``) for each tracked thread that exited."""
        proc = system_process(self.pid)
        t_ns = self._clock()
        if proc is not None and proc.created_filetime != self._started:
            proc = None
        if proc is None and not self._tracked:
            raise ProcessGone(self.pid)
        current = {t.tid: t for t in proc.threads} if proc is not None else {}
        refresh_names = self._names_read_ns is None or t_ns - self._names_read_ns >= self._name_refresh_ns
        if refresh_names:
            self._names_read_ns = t_ns
        stats: list[ThreadStat] = []
        for tid in sorted(current.keys() | self._tracked.keys()):
            info = current.get(tid)
            tracked = self._tracked.get(tid) or self._track(info)
            if info is not None and not info.terminated:
                if refresh_names and tracked.handle:
                    tracked.name = _thread_description(tracked.handle)
                stats.append(self._stat(tid, tracked, info.cpu_100ns, t_ns, alive=True))
                continue
            stats.append(self._final(tid, tracked, info, t_ns))
        return stats

    def _track(self, info: SystemThread | None) -> _Tracked:
        assert info is not None
        handle = _k32().OpenThread(THREAD_QUERY_INFORMATION | SYNCHRONIZE, False, info.tid) or None
        name = _thread_description(handle) if handle else ""
        module = None
        if handle and self._modules is not None:
            address = _thread_start_address(handle)
            module = self._modules.lookup(address) if address else None
        created = (info.created_filetime - _EPOCH_AS_FILETIME) * 100 + self._offset
        tracked = _Tracked(handle, name, created, module)
        self._tracked[info.tid] = tracked
        return tracked

    def _final(self, tid: int, tracked: _Tracked, info: SystemThread | None, t_ns: int) -> ThreadStat:
        cpu = info.cpu_100ns if info is not None else None
        if tracked.handle:
            try:
                _, cpu = _thread_times(tracked.handle)
            except OSError:
                pass
            _close(tracked.handle)
        del self._tracked[tid]
        return self._stat(tid, tracked, cpu or 0, t_ns, alive=False)

    @staticmethod
    def _stat(tid: int, tracked: _Tracked, cpu_100ns: int, t_ns: int, *, alive: bool) -> ThreadStat:
        return ThreadStat(
            tid=tid,
            name=tracked.name,
            cpu_ns=cpu_100ns * 100,
            t_ns=t_ns,
            created_ns=tracked.created_ns,
            start_module=tracked.start_module,
            alive=alive,
        )


class TickReader:
    """Reads one thread's CPU time and cycles: two native calls, cheap enough for 10 Hz."""

    cycles_supported = True

    def __init__(self, pid: int, tid: int) -> None:
        self.pid = pid
        self.tid = tid
        if tid not in thread_ids(pid):
            raise ProcessGone(f"process {pid} has no live thread {tid}")
        self._handle = _k32().OpenThread(THREAD_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, tid) or None
        if self._handle is None:
            raise ProcessGone(f"thread {tid} of process {pid}")

    def read(self) -> tuple[int, int | None]:
        if self._handle is None:
            raise ProcessGone(self.tid)
        try:
            _, cpu = _thread_times(self._handle)
        except OSError as exc:
            raise ProcessGone(self.tid) from exc
        return cpu * 100, _thread_cycles(self._handle)

    def exited(self) -> bool:
        return self._handle is None or _exited(self._handle)

    def close(self) -> None:
        _close(self._handle)
        self._handle = None


def calibrate_tsc(seconds: float = 0.25, rounds: int = 3) -> float:
    """TSC ticks per second as counted by ``QueryThreadCycleTime`` on a spinning thread.

    Preemption can only lower a round's estimate, so the highest of ``rounds`` is returned.
    """
    me = _k32().GetCurrentThread()
    best = 0.0
    for _ in range(rounds):
        c0 = _thread_cycles(me) or 0
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            pass
        c1 = _thread_cycles(me) or 0
        best = max(best, (c1 - c0) / (time.perf_counter() - t0))
    return best


# --------------------------------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------------------------------


def process_memory(pid: int) -> tuple[float, float]:
    """``(working set MiB, private bytes MiB)``."""
    try:
        info = psutil.Process(pid).memory_info()
    except psutil.NoSuchProcess as exc:
        raise ProcessGone(pid) from exc
    return info.rss / MIB, info.private / MIB


def process_handles(pid: int) -> int | None:
    try:
        return psutil.Process(pid).num_handles()
    except psutil.NoSuchProcess as exc:
        raise ProcessGone(pid) from exc


def process_affinity(pid: int) -> tuple[int, ...]:
    try:
        return tuple(sorted(psutil.Process(pid).cpu_affinity()))
    except psutil.NoSuchProcess as exc:
        raise ProcessGone(pid) from exc


def thread_affinity(pid: int, tid: int) -> tuple[int, ...] | None:
    """Logical CPUs a thread may run on, or None if it cannot be read."""
    handle = _k32().OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
    if not handle:
        return None
    try:
        affinity = _GROUP_AFFINITY()
        if not _k32().GetThreadGroupAffinity(handle, ctypes.byref(affinity)):
            return None
        base = affinity.Group * 64
        return tuple(base + bit for bit in range(64) if affinity.Mask >> bit & 1)
    finally:
        _close(handle)


def allowed_cpus() -> tuple[int, ...]:
    """CPUs this process may use."""
    return tuple(sorted(psutil.Process().cpu_affinity()))


def set_affinity(pid: int, cpus: Sequence[int]) -> None:
    psutil.Process(pid).cpu_affinity(sorted(cpus))


def set_priority(pid: int, priority: str) -> None:
    psutil.Process(pid).nice(PRIORITY_CLASSES[priority])


def get_priority(pid: int) -> str:
    value = int(psutil.Process(pid).nice())
    return _CLASS_NAMES.get(value, f"class_0x{value:x}")


def can_set_priority(priority: str) -> bool:
    """Any user may raise a process to above normal on Windows."""
    return priority in PRIORITY_CLASSES


def resume_process(pid: int) -> int:
    """Resume every thread of a process started with ``CREATE_SUSPENDED``. Returns how many were resumed."""
    k32 = _k32()
    resumed = 0
    for tid in thread_ids(pid):
        handle = k32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
        if not handle:
            raise _winerror(f"OpenThread({tid})")
        try:
            if k32.ResumeThread(handle) == 0xFFFFFFFF:
                raise _winerror(f"ResumeThread({tid})")
            resumed += 1
        finally:
            _close(handle)
    return resumed


def interrupt(pid: int) -> None:
    """CTRL_BREAK to a process started with ``CREATE_NEW_PROCESS_GROUP`` that shares this console."""
    os.kill(pid, signal.CTRL_BREAK_EVENT)


class Job:
    """A job object that kills every process in it when the harness exits or :meth:`close` is called.

    Processes started by a job member join the job too, so the whole server tree goes down with the harness even if
    the harness is killed. Nested jobs need Windows 8 or later.
    """

    def __init__(self) -> None:
        k32 = _k32()
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise _winerror("CreateJobObjectW")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            _close(handle)
            raise _winerror("SetInformationJobObject")
        self._handle: int | None = handle

    def assign(self, pid: int) -> None:
        k32 = _k32()
        proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            raise _winerror(f"OpenProcess({pid})")
        try:
            if not k32.AssignProcessToJobObject(self._handle, proc):
                raise _winerror("AssignProcessToJobObject")
        finally:
            _close(proc)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle:
            _k32().TerminateJobObject(self._handle, exit_code)

    def close(self) -> None:
        """Release the job; every process still in it is killed."""
        _close(self._handle)
        self._handle = None


def loaded_modules(pid: int) -> list[str]:
    """Base names of the modules loaded in a process (allocator and runtime facts)."""
    modules = _ModuleMap(pid)
    try:
        return modules.names()
    finally:
        modules.close()


# --------------------------------------------------------------------------------------------------------------------
# Machine
# --------------------------------------------------------------------------------------------------------------------


def _cpu_set_entries() -> Iterator[tuple[int, int, int, int]]:
    """``(group, logical index, core index, efficiency class)`` per CPU set."""
    k32 = _k32()
    length = ctypes.c_ulong(0)
    k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(length), None, 0)
    if not length.value:
        raise _winerror("GetSystemCpuSetInformation")
    buf = ctypes.create_string_buffer(length.value)
    if not k32.GetSystemCpuSetInformation(buf, length, ctypes.byref(length), None, 0):
        raise _winerror("GetSystemCpuSetInformation")
    raw = buf.raw[: length.value]
    offset = 0
    while offset + 20 <= len(raw):
        size, kind = struct.unpack_from("<II", raw, offset)
        if size == 0:
            break
        if kind == 0:  # CpuSetInformation
            group, logical, core, _llc, _numa, efficiency = struct.unpack_from("<HBBBBB", raw, offset + 12)
            yield group, logical, core, efficiency
        offset += size


def cpu_records() -> list[CpuRecord]:
    """Logical CPUs this process may use, with physical core and efficiency class."""
    allowed = set(allowed_cpus())
    try:
        entries = sorted(_cpu_set_entries())
    except OSError:
        entries = []
    if not entries:
        return [CpuRecord(cpu=c, core=c) for c in sorted(allowed)]
    core_ids: dict[tuple[int, int], int] = {}
    records = []
    for group, logical, core, efficiency in entries:
        cpu = group * 64 + logical
        core_id = core_ids.setdefault((group, core), len(core_ids))
        if cpu in allowed:
            records.append(CpuRecord(cpu=cpu, core=core_id, efficiency=efficiency))
    return records


def _registry_value(path: str, name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value).strip()


def cpu_model() -> str:
    return _registry_value(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString") or "unknown"


def virtualization() -> str | None:
    """The hypervisor this machine runs under, from the BIOS strings; None on bare metal.

    A host with Hyper-V or VBS enabled reports its real hardware here, so it counts as bare metal.
    """
    bios = r"HARDWARE\DESCRIPTION\System\BIOS"
    parts = (_registry_value(bios, "SystemManufacturer"), _registry_value(bios, "SystemProductName"))
    text = " ".join(p for p in parts if p).lower()
    for needle, label in _HYPERVISORS:
        if needle in text:
            return label
    return None


def power_settings() -> dict[str, object]:
    """Active power scheme, e.g. ``{"scheme": "high_performance", "guid": "8c5e7fda-..."}``."""
    try:
        out = subprocess.run(
            ["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    match = re.search(r"([0-9a-fA-F-]{36})\s*(?:\((.*)\))?", out)
    if not match:
        return {}
    guid = match.group(1).lower()
    return {"scheme": POWER_SCHEMES.get(guid, (match.group(2) or "custom").strip()), "guid": guid}
