# SPDX-License-Identifier: Apache-2.0
"""Linux implementation of :mod:`bedrock_bench.platform` (``/proc`` and ``/sys``, no extra dependencies).

- CPU time per thread is the first field of ``/proc/<pid>/task/<tid>/schedstat`` (nanoseconds on CPU). Kernels
  without schedstat fall back to ``utime + stime`` from ``stat`` divided by ``sysconf(SC_CLK_TCK)``, never a
  hard-coded 100.
- ``stat`` is split after the *last* ``)``: thread names may contain spaces and parentheses (``REST Thread(0)``).
- Creation times come from ``stat`` field 22 (clock ticks since boot) and are mapped onto the monotonic clock.
- Memory comes from ``/proc/<pid>/status``: resident is ``VmRSS``, private is ``RssAnon + VmSwap``.
- Topology comes from sysfs: SMT siblings share ``core_id`` within a package, and Intel hybrid parts list their
  performance and efficiency CPUs in ``/sys/devices/cpu_core/cpus`` and ``/sys/devices/cpu_atom/cpus``. Everything is
  intersected with the harness's own affinity, so a cgroup cpuset is respected.
- Affinity and nice are per thread on Linux. New processes get them in ``preexec_fn`` before ``exec``, so every
  thread the server ever starts inherits them.

Every function that reads ``/proc`` or ``/sys`` takes the root as an argument so tests can point it at fixtures.
"""

from __future__ import annotations

import os
import platform as _stdlib_platform
import signal
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bedrock_bench.clock import mono_ns
from bedrock_bench.platform import CpuRecord, ProcEntry, ProcessGone, ThreadStat, sanitize_name

PROC = Path("/proc")
SYS = Path("/sys")

#: Nice values for the profile priorities. Lowering nice below 0 needs root, CAP_SYS_NICE or RLIMIT_NICE.
NICE = {"normal": 0, "above_normal": -5}

_CAP_SYS_NICE = 23

_HYPERVISORS = (
    ("qemu", "kvm"),
    ("kvm", "kvm"),
    ("vmware", "vmware"),
    ("virtualbox", "virtualbox"),
    ("innotek", "virtualbox"),
    ("xen", "xen"),
    ("amazon ec2", "aws"),
    ("google", "gce"),
    ("parallels", "parallels"),
    ("virtual machine", "hyper-v"),
)


# --------------------------------------------------------------------------------------------------------------------
# Parsers (pure)
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStat:
    """The fields of ``/proc/<pid>[/task/<tid>]/stat`` the harness uses."""

    comm: str
    state: str
    ppid: int
    utime: int  # clock ticks
    stime: int  # clock ticks
    num_threads: int
    starttime: int  # clock ticks since boot
    processor: int | None  # CPU the task last ran on


def parse_stat(text: str) -> TaskStat:
    """Parse a stat line. The name sits between the first ``(`` and the last ``)`` and may contain both."""
    start = text.index("(")
    end = text.rindex(")")
    rest = text[end + 1 :].split()
    # rest[i] is field i + 3 of proc(5): state is field 3, utime 14, stime 15, num_threads 20, starttime 22.
    return TaskStat(
        comm=text[start + 1 : end],
        state=rest[0],
        ppid=int(rest[1]),
        utime=int(rest[11]),
        stime=int(rest[12]),
        num_threads=int(rest[17]),
        starttime=int(rest[19]),
        processor=int(rest[36]) if len(rest) > 36 else None,
    )


def parse_schedstat(text: str) -> int:
    """Nanoseconds on CPU: the first of the three fields of ``schedstat``."""
    return int(text.split()[0])


def parse_status(text: str) -> dict[str, str]:
    """``/proc/<pid>/status`` as ``{key: value}`` with values stripped."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def status_kib(status: dict[str, str], key: str) -> int | None:
    """A ``kB`` value of a parsed status file, e.g. ``status_kib(s, "VmRSS")``."""
    value = status.get(key)
    if not value:
        return None
    return int(value.split()[0])


def parse_smaps_rollup(text: str) -> dict[str, int]:
    """``/proc/<pid>/smaps_rollup`` sizes in bytes, e.g. ``{"Rss": ..., "Pss": ..., "Anonymous": ...}``."""
    out: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition(":")
        parts = value.split()
        if sep and len(parts) == 2 and parts[1] == "kB":
            out[key.strip()] = int(parts[0]) * 1024
    return out


def parse_cpu_list(text: str) -> tuple[int, ...]:
    """Kernel CPU list format (``0-3,8,10-11``); empty text is an empty tuple."""
    cpus: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        cpus.update(range(int(lo), int(hi) + 1) if sep else (int(lo),))
    return tuple(sorted(cpus))


def parse_maps_modules(text: str) -> list[str]:
    """Base names of the files mapped in ``/proc/<pid>/maps``, sorted and unique."""
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[5].startswith("/"):
            names.add(parts[5].rsplit("/", 1)[-1].removesuffix(" (deleted)"))
    return sorted(names)


def _read(path: Path) -> str:
    """Read a whole /proc or /sys file with plain system calls (cheaper than a Python file object)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _read_optional(path: Path) -> str | None:
    try:
        return _read(path)
    except OSError:
        return None


def clk_tck() -> int:
    return os.sysconf("SC_CLK_TCK")


def schedstat_available(pid: int | str = "self", proc_root: Path = PROC) -> bool:
    return (proc_root / str(pid) / "schedstat").is_file()


def _boot_to_mono_offset() -> int:
    """Harness clock minus ``CLOCK_BOOTTIME``; ``stat`` start times count from boot."""
    before = mono_ns()
    boot = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    after = mono_ns()
    return (before + after) // 2 - boot


# --------------------------------------------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------------------------------------------


class ThreadReader:
    """Reads every thread of one process from ``/proc/<pid>/task``."""

    cycles_supported = False

    def __init__(
        self,
        pid: int,
        *,
        clock: Callable[[], int] = mono_ns,
        proc_root: Path = PROC,
        ticks_per_s: int | None = None,
        boot_offset_ns: int | None = None,
    ) -> None:
        self.pid = pid
        self._task_dir = proc_root / str(pid) / "task"
        self._clock = clock
        self._tck = ticks_per_s or clk_tck()
        self._boot_offset = _boot_to_mono_offset() if boot_offset_ns is None else boot_offset_ns
        self.uses_schedstat = schedstat_available(pid, proc_root)
        self._started: int | None = None  # start time of the leader, to tell a later process with the same pid

    def close(self) -> None:
        pass

    def __enter__(self) -> ThreadReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def snapshot(self) -> list[ThreadStat]:
        try:
            tids = sorted(int(entry) for entry in os.listdir(self._task_dir) if entry.isdigit())
        except FileNotFoundError:
            raise ProcessGone(self.pid) from None
        stats: list[ThreadStat] = []
        states: list[str] = []
        for tid in tids:
            base = self._task_dir / str(tid)
            try:
                task = parse_stat(_read(base / "stat"))
                if self.uses_schedstat:
                    cpu_ns = parse_schedstat(_read(base / "schedstat"))
                else:
                    cpu_ns = (task.utime + task.stime) * 1_000_000_000 // self._tck
            except (OSError, ValueError, IndexError):
                continue  # the thread exited between listing and reading
            t_ns = self._clock()
            if tid == self.pid:
                if self._started is None:
                    self._started = task.starttime
                elif task.starttime != self._started:
                    raise ProcessGone(self.pid)
            states.append(task.state)
            stats.append(
                ThreadStat(
                    tid=tid,
                    name=sanitize_name(task.comm),
                    cpu_ns=cpu_ns,
                    t_ns=t_ns,
                    created_ns=task.starttime * 1_000_000_000 // self._tck + self._boot_offset,
                )
            )
        if not states or all(state in ("Z", "X") for state in states):
            raise ProcessGone(self.pid)
        return stats


class TickReader:
    """Reads one thread's CPU time with a single ``pread`` on a file kept open; cheap enough for 10 Hz."""

    cycles_supported = False

    def __init__(self, pid: int, tid: int, *, proc_root: Path = PROC, ticks_per_s: int | None = None) -> None:
        self.pid = pid
        self.tid = tid
        self._schedstat = schedstat_available(pid, proc_root)
        self._tck = ticks_per_s or clk_tck()
        path = proc_root / str(pid) / "task" / str(tid) / ("schedstat" if self._schedstat else "stat")
        try:
            self._fd: int | None = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            raise ProcessGone(f"process {pid} has no thread {tid}") from None

    def read(self) -> tuple[int, int | None]:
        if self._fd is None:
            raise ProcessGone(self.tid)
        try:
            text = os.pread(self._fd, 1024, 0).decode("utf-8", errors="replace")
        except OSError as exc:  # ESRCH once the thread is gone
            raise ProcessGone(self.tid) from exc
        if not text:
            raise ProcessGone(self.tid)
        if self._schedstat:
            return parse_schedstat(text), None
        task = parse_stat(text)
        if task.state in ("Z", "X"):
            raise ProcessGone(self.tid)
        return (task.utime + task.stime) * 1_000_000_000 // self._tck, None

    def exited(self) -> bool:
        """Linux reports an exited thread as a read error, so :meth:`read` raises instead."""
        return self._fd is None

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


# --------------------------------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------------------------------


def process_table(proc_root: Path = PROC, ticks_per_s: int | None = None) -> list[ProcEntry]:
    """Every process with its parent, name and CPU time: one ``stat`` read per process."""
    tck = ticks_per_s or clk_tck()
    out = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        try:
            task = parse_stat(_read(proc_root / entry / "stat"))
        except (OSError, ValueError, IndexError):
            continue
        cpu_s = (task.utime + task.stime) / tck
        out.append(ProcEntry(int(entry), task.ppid, sanitize_name(task.comm), cpu_s, task.starttime))
    return out


def process_memory(pid: int, proc_root: Path = PROC) -> tuple[float, float]:
    """``(VmRSS MiB, RssAnon + VmSwap MiB)``."""
    text = _read_optional(proc_root / str(pid) / "status")
    if text is None:
        raise ProcessGone(pid)
    status = parse_status(text)
    rss = status_kib(status, "VmRSS") or 0
    private = (status_kib(status, "RssAnon") or 0) + (status_kib(status, "VmSwap") or 0)
    return rss / 1024, private / 1024


def process_handles(pid: int) -> int | None:
    """Handle counts are a Windows quantity."""
    return None


def process_affinity(pid: int) -> tuple[int, ...]:
    try:
        return tuple(sorted(os.sched_getaffinity(pid)))
    except ProcessLookupError:
        raise ProcessGone(pid) from None


def thread_affinity(pid: int, tid: int) -> tuple[int, ...] | None:
    try:
        return tuple(sorted(os.sched_getaffinity(tid)))
    except OSError:
        return None


def allowed_cpus() -> tuple[int, ...]:
    return tuple(sorted(os.sched_getaffinity(0)))


def _tasks(pid: int, proc_root: Path = PROC) -> list[int]:
    try:
        return [int(t) for t in os.listdir(proc_root / str(pid) / "task") if t.isdigit()]
    except FileNotFoundError:
        raise ProcessGone(pid) from None


def set_affinity(pid: int, cpus: Sequence[int]) -> None:
    """Pin every current thread of a process. New processes are pinned in ``preexec`` instead."""
    for tid in _tasks(pid):
        try:
            os.sched_setaffinity(tid, cpus)
        except ProcessLookupError:
            continue


def set_priority(pid: int, priority: str) -> None:
    """Apply the profile priority to every current thread of a process."""
    for tid in _tasks(pid):
        try:
            os.setpriority(os.PRIO_PROCESS, tid, NICE[priority])
        except ProcessLookupError:
            continue


def get_priority(pid: int) -> str:
    try:
        nice = os.getpriority(os.PRIO_PROCESS, pid)
    except ProcessLookupError:
        raise ProcessGone(pid) from None
    for label, value in NICE.items():
        if value == nice:
            return label
    return f"nice_{nice}"


def can_set_priority(priority: str, proc_root: Path = PROC) -> bool:
    """Whether this process may start children at ``priority``: root, CAP_SYS_NICE or a high enough RLIMIT_NICE."""
    nice = NICE[priority]
    if nice >= 0 or os.geteuid() == 0:
        return True
    import resource

    soft, _ = resource.getrlimit(resource.RLIMIT_NICE)
    if soft == resource.RLIM_INFINITY or 20 - soft <= nice:
        return True
    status = parse_status(_read_optional(proc_root / "self" / "status") or "")
    cap_eff = int(status.get("CapEff", "0"), 16)
    return bool(cap_eff >> _CAP_SYS_NICE & 1)


def preexec(cpus: Sequence[int] | None, priority: str) -> Callable[[], None]:
    """A ``preexec_fn`` that pins and renices the child between ``fork`` and ``exec``.

    The child makes two system calls and nothing else, so the program starts with its placement already applied.
    """
    nice = NICE[priority]
    cpu_set = frozenset(cpus) if cpus else None

    def apply() -> None:
        if cpu_set is not None:
            os.sched_setaffinity(0, cpu_set)
        if nice:
            os.setpriority(os.PRIO_PROCESS, 0, nice)

    return apply


def interrupt(pid: int) -> None:
    os.kill(pid, signal.SIGINT)


def loaded_modules(pid: int, proc_root: Path = PROC) -> list[str]:
    text = _read_optional(proc_root / str(pid) / "maps")
    if text is None:
        raise ProcessGone(pid)
    return parse_maps_modules(text)


def smaps_rollup(pid: int, proc_root: Path = PROC) -> dict[str, int]:
    """Whole-process memory summary in bytes (Rss, Pss, Anonymous, Swap...). Costlier than ``status``."""
    text = _read_optional(proc_root / str(pid) / "smaps_rollup")
    return parse_smaps_rollup(text) if text else {}


# --------------------------------------------------------------------------------------------------------------------
# Machine
# --------------------------------------------------------------------------------------------------------------------


def cpu_records(sys_root: Path = SYS, allowed: Iterable[int] | None = None) -> list[CpuRecord]:
    """Online logical CPUs the harness may use, with physical core and efficiency class."""
    cpu_dir = sys_root / "devices" / "system" / "cpu"
    online_text = _read_optional(cpu_dir / "online")
    online = parse_cpu_list(online_text) if online_text else tuple(range(os.cpu_count() or 1))
    allowed_set = set(allowed) if allowed is not None else set(allowed_cpus())
    efficiency = _efficiency_classes(sys_root, online)
    core_ids: dict[tuple[int, int, int], int] = {}
    records = []
    for cpu in online:
        topo = cpu_dir / f"cpu{cpu}" / "topology"
        package = _read_int(topo / "physical_package_id", 0)
        key = (package, _read_int(topo / "die_id", 0), _read_int(topo / "core_id", cpu))
        core_ids.setdefault(key, len(core_ids))
        if cpu in allowed_set:
            records.append(CpuRecord(cpu=cpu, core=core_ids[key], package=package, efficiency=efficiency.get(cpu, 0)))
    return records


def _read_int(path: Path, default: int) -> int:
    text = _read_optional(path)
    try:
        return int(text.strip()) if text else default
    except ValueError:
        return default


def _efficiency_classes(sys_root: Path, online: Sequence[int]) -> dict[int, int]:
    """1 for performance and 0 for efficiency CPUs on hybrid parts; empty when all CPUs are alike."""
    core = _read_optional(sys_root / "devices" / "cpu_core" / "cpus")
    atom = _read_optional(sys_root / "devices" / "cpu_atom" / "cpus")
    if core is not None and atom is not None:
        classes = {cpu: 0 for cpu in parse_cpu_list(atom)}
        classes.update({cpu: 1 for cpu in parse_cpu_list(core)})
        return classes
    capacity = {
        cpu: _read_int(sys_root / "devices" / "system" / "cpu" / f"cpu{cpu}" / "cpu_capacity", -1) for cpu in online
    }
    levels = sorted({c for c in capacity.values() if c >= 0})
    if len(levels) > 1:
        return {cpu: levels.index(value) for cpu, value in capacity.items() if value >= 0}
    return {}


def cpu_model(proc_root: Path = PROC) -> str:
    text = _read_optional(proc_root / "cpuinfo") or ""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("model name", "Model", "Hardware"):
            return value.strip()
    return _stdlib_platform.processor() or "unknown"


def virtualization(proc_root: Path = PROC, sys_root: Path = SYS) -> str | None:
    """``wsl2``, a hypervisor name, or None on bare metal. Containers on bare metal count as bare metal."""
    release = (_read_optional(proc_root / "sys" / "kernel" / "osrelease") or "").lower()
    if "microsoft" in release or "wsl" in release:
        return "wsl2"
    dmi = sys_root / "class" / "dmi" / "id"
    text = " ".join((_read_optional(dmi / name) or "").strip() for name in ("sys_vendor", "product_name")).lower()
    for needle, label in _HYPERVISORS:
        if needle in text:
            return label
    flags = next(
        (line for line in (_read_optional(proc_root / "cpuinfo") or "").splitlines() if line.startswith("flags")),
        "",
    )
    return "hypervisor" if " hypervisor" in flags else None


def in_container(root: Path = Path("/")) -> bool:
    return (root / ".dockerenv").exists() or (root / "run" / ".containerenv").exists()


def power_settings(sys_root: Path = SYS) -> dict[str, object]:
    """CPU frequency governors and driver, e.g. ``{"driver": "intel_pstate", "governors": ["performance"]}``."""
    cpu_dir = sys_root / "devices" / "system" / "cpu"
    governors, epp = set(), set()
    for freq in sorted(cpu_dir.glob("cpu[0-9]*/cpufreq")):
        governor = _read_optional(freq / "scaling_governor")
        if governor:
            governors.add(governor.strip())
        preference = _read_optional(freq / "energy_performance_preference")
        if preference:
            epp.add(preference.strip())
    out: dict[str, object] = {}
    driver = _read_optional(cpu_dir / "cpu0" / "cpufreq" / "scaling_driver")
    if driver:
        out["driver"] = driver.strip()
    if governors:
        out["governors"] = sorted(governors)
    if epp:
        out["energy_performance_preference"] = sorted(epp)
    return out


__all__ = [
    "NICE",
    "ThreadReader",
    "TickReader",
    "allowed_cpus",
    "can_set_priority",
    "cpu_model",
    "cpu_records",
    "get_priority",
    "in_container",
    "interrupt",
    "loaded_modules",
    "parse_cpu_list",
    "parse_maps_modules",
    "parse_schedstat",
    "parse_smaps_rollup",
    "parse_stat",
    "parse_status",
    "power_settings",
    "preexec",
    "process_affinity",
    "process_handles",
    "process_memory",
    "process_table",
    "set_affinity",
    "set_priority",
    "smaps_rollup",
    "thread_affinity",
    "virtualization",
]
