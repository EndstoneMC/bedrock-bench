# SPDX-License-Identifier: Apache-2.0
"""Linux /proc and /sys parsing. Pure functions and fake trees, so these run on every OS."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bedrock_bench.platform import ProcessGone
from bedrock_bench.platform import linux as lin

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "proc"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------------------------------
# stat, schedstat, status, smaps_rollup, maps
# --------------------------------------------------------------------------------------------------------------------


def test_stat_name_with_space_and_parentheses() -> None:
    task = lin.parse_stat(fixture("task_stat_rest_thread.txt"))
    assert task.comm == "REST Thread(0)"
    assert task.state == "S"
    assert task.ppid == 4100
    assert (task.utime, task.stime) == (812, 97)
    assert task.num_threads == 112
    assert task.starttime == 98765
    assert task.processor == 7


@pytest.mark.parametrize("comm", ["a) b", ") (", "x)) 1 2 3 (", "plain", ""])
def test_stat_splits_after_last_paren(comm: str) -> None:
    rest = fixture("task_stat_rest_thread.txt").split(") ", 1)[1]
    task = lin.parse_stat(f"99 ({comm}) {rest}")
    assert task.comm == comm
    assert task.utime == 812
    assert task.starttime == 98765


def test_stat_without_processor_field() -> None:
    short = "1 (init) S 0 1 1 0 -1 4194560 100 0 0 0 5 6 0 0 20 0 1 0 42"
    task = lin.parse_stat(short)
    assert (task.utime, task.stime, task.starttime, task.processor) == (5, 6, 42, None)


def test_schedstat_first_field_is_ns_on_cpu() -> None:
    assert lin.parse_schedstat(fixture("schedstat.txt")) == 7351249812


def test_status_memory_and_affinity() -> None:
    status = lin.parse_status(fixture("status.txt"))
    assert status["Name"] == "bedrock_server"
    assert lin.status_kib(status, "VmRSS") == 2097152
    assert lin.status_kib(status, "RssAnon") == 1835008
    assert lin.status_kib(status, "VmSwap") == 10240
    assert lin.status_kib(status, "Missing") is None
    assert status["Threads"] == "112"
    assert lin.parse_cpu_list(status["Cpus_allowed_list"]) == tuple(range(16))


def test_smaps_rollup_in_bytes() -> None:
    rollup = lin.parse_smaps_rollup(fixture("smaps_rollup.txt"))
    assert rollup["Rss"] == 2097152 * 1024
    assert rollup["Anonymous"] == 1835008 * 1024
    assert rollup["Swap"] == 10240 * 1024
    assert "[rollup]" not in "".join(rollup)


def test_maps_modules() -> None:
    assert lin.parse_maps_modules(fixture("maps.txt")) == [
        "bedrock_server",
        "bench",
        "ld-linux-x86-64.so.2",
        "libc.so.6",
        "libmimalloc.so.2",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0-3,8,10-11\n", (0, 1, 2, 3, 8, 10, 11)),
        ("5", (5,)),
        ("", ()),
        ("\n", ()),
        ("0-1, 4", (0, 1, 4)),
    ],
)
def test_kernel_cpu_list(text: str, expected: tuple[int, ...]) -> None:
    assert lin.parse_cpu_list(text) == expected


# --------------------------------------------------------------------------------------------------------------------
# Fake /proc tree
# --------------------------------------------------------------------------------------------------------------------


def stat_line(tid: int, comm: str, utime: int, stime: int, starttime: int, state: str = "S") -> str:
    rest = fixture("task_stat_rest_thread.txt").split(") ", 1)[1].split()
    rest[0], rest[11], rest[12], rest[19] = state, str(utime), str(stime), str(starttime)
    return f"{tid} ({comm}) {' '.join(rest)}\n"


def make_proc(
    root: Path, pid: int, threads: dict[int, tuple[str, int, int, int, int | None]], *, schedstat: bool
) -> Path:
    """threads: tid -> (comm, utime, stime, starttime, schedstat_ns)."""
    base = root / str(pid)
    (base / "task").mkdir(parents=True)
    (base / "status").write_text(fixture("status.txt"), encoding="utf-8")
    (base / "maps").write_text(fixture("maps.txt"), encoding="utf-8")
    if schedstat:
        (base / "schedstat").write_text("1 2 3\n", encoding="utf-8")
    for tid, (comm, utime, stime, start, ns) in threads.items():
        task = base / "task" / str(tid)
        task.mkdir()
        (task / "stat").write_text(stat_line(tid, comm, utime, stime, start), encoding="utf-8")
        if schedstat and ns is not None:
            (task / "schedstat").write_text(f"{ns} 0 0\n", encoding="utf-8")
    return base


def test_thread_reader_schedstat(tmp_path: Path) -> None:
    make_proc(
        tmp_path,
        4100,
        {4100: ("bedrock_server", 10, 2, 500, 120_000_000), 4242: ("REST Thread(0)", 1, 1, 900, 5_000_000)},
        schedstat=True,
    )
    clock = iter(range(1000, 2000, 10))
    reader = lin.ThreadReader(
        4100, proc_root=tmp_path, clock=lambda: next(clock), ticks_per_s=100, boot_offset_ns=-1_000_000_000
    )
    assert reader.uses_schedstat
    stats = reader.snapshot()
    assert [(s.tid, s.name, s.cpu_ns) for s in stats] == [
        (4100, "bedrock_server", 120_000_000),
        (4242, "REST Thread(0)", 5_000_000),
    ]
    assert [s.t_ns for s in stats] == [1000, 1010]  # stamped after each read
    assert stats[0].created_ns == 500 * 10_000_000 - 1_000_000_000
    assert all(s.cycles is None and s.alive for s in stats)


def test_thread_reader_tick_fallback_uses_clk_tck(tmp_path: Path) -> None:
    make_proc(tmp_path, 7, {7: ("java", 250, 50, 10, None)}, schedstat=False)
    reader = lin.ThreadReader(7, proc_root=tmp_path, ticks_per_s=250, boot_offset_ns=0)
    assert not reader.uses_schedstat
    (stat,) = reader.snapshot()
    assert stat.cpu_ns == 300 * 1_000_000_000 // 250


def test_thread_reader_skips_vanished_thread_and_detects_exit(tmp_path: Path) -> None:
    base = make_proc(tmp_path, 9, {9: ("main", 1, 1, 1, 10)}, schedstat=True)
    (base / "task" / "10").mkdir()  # listed but its files are gone: exited mid-read
    reader = lin.ThreadReader(9, proc_root=tmp_path, ticks_per_s=100, boot_offset_ns=0)
    assert [s.tid for s in reader.snapshot()] == [9]
    (base / "task" / "9" / "stat").write_text(stat_line(9, "main", 1, 1, 1, state="Z"), encoding="utf-8")
    with pytest.raises(ProcessGone):
        reader.snapshot()
    with pytest.raises(ProcessGone):
        lin.ThreadReader(12345, proc_root=tmp_path, ticks_per_s=100, boot_offset_ns=0).snapshot()


@pytest.mark.skipif(not hasattr(__import__("os"), "pread"), reason="os.pread is POSIX only")
def test_tick_reader_rereads_open_file(tmp_path: Path) -> None:
    base = make_proc(tmp_path, 5, {5: ("MC_SERVER", 1, 1, 1, 1000)}, schedstat=True)
    reader = lin.TickReader(5, 5, proc_root=tmp_path, ticks_per_s=100)
    assert reader.read() == (1000, None)
    (base / "task" / "5" / "schedstat").write_text("2500 0 0\n", encoding="utf-8")
    assert reader.read() == (2500, None)
    reader.close()
    with pytest.raises(ProcessGone):
        reader.read()
    with pytest.raises(ProcessGone):
        lin.TickReader(5, 6, proc_root=tmp_path, ticks_per_s=100)


def test_process_memory_and_modules(tmp_path: Path) -> None:
    make_proc(tmp_path, 4100, {4100: ("bedrock_server", 1, 1, 1, 1)}, schedstat=True)
    resident, private = lin.process_memory(4100, proc_root=tmp_path)
    assert resident == pytest.approx(2048.0)
    assert private == pytest.approx((1835008 + 10240) / 1024)
    assert "libmimalloc.so.2" in lin.loaded_modules(4100, proc_root=tmp_path)
    with pytest.raises(ProcessGone):
        lin.process_memory(1, proc_root=tmp_path)


# --------------------------------------------------------------------------------------------------------------------
# Fake /sys tree: topology, virtualization, power
# --------------------------------------------------------------------------------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_sys(root: Path, cores: list[list[int]], *, core_cpus: str | None = None, atom_cpus: str | None = None) -> Path:
    cpu_dir = root / "devices" / "system" / "cpu"
    cpus = sorted(c for core in cores for c in core)
    write(cpu_dir / "online", f"0-{cpus[-1]}\n")
    for core_id, core in enumerate(cores):
        for cpu in core:
            write(cpu_dir / f"cpu{cpu}" / "topology" / "core_id", f"{core_id * 4}\n")
            write(cpu_dir / f"cpu{cpu}" / "topology" / "physical_package_id", "0\n")
    if core_cpus is not None and atom_cpus is not None:
        write(root / "devices" / "cpu_core" / "cpus", core_cpus + "\n")
        write(root / "devices" / "cpu_atom" / "cpus", atom_cpus + "\n")
    return root


def test_cpu_records_hybrid_with_smt(tmp_path: Path) -> None:
    cores = [[2 * i, 2 * i + 1] for i in range(8)] + [[c] for c in range(16, 28)]
    root = make_sys(tmp_path, cores, core_cpus="0-15", atom_cpus="16-27")
    records = lin.cpu_records(sys_root=root, allowed=range(28))
    assert [r.cpu for r in records] == list(range(28))
    assert records[0].core == records[1].core != records[2].core
    assert {r.efficiency for r in records if r.cpu < 16} == {1}
    assert {r.efficiency for r in records if r.cpu >= 16} == {0}
    assert len({r.core for r in records}) == 20


def test_cpu_records_respect_allowed_set(tmp_path: Path) -> None:
    root = make_sys(tmp_path, [[0, 4], [1, 5], [2, 6], [3, 7]])
    records = lin.cpu_records(sys_root=root, allowed=[0, 4, 1])
    assert [r.cpu for r in records] == [0, 1, 4]
    assert {r.efficiency for r in records} == {0}
    by_cpu = {r.cpu: r.core for r in records}
    assert by_cpu[0] == by_cpu[4] != by_cpu[1]


def test_cpu_capacity_classes(tmp_path: Path) -> None:
    root = make_sys(tmp_path, [[0], [1], [2], [3]])
    for cpu, capacity in enumerate((1024, 1024, 446, 446)):
        write(root / "devices" / "system" / "cpu" / f"cpu{cpu}" / "cpu_capacity", f"{capacity}\n")
    records = lin.cpu_records(sys_root=root, allowed=range(4))
    assert [r.efficiency for r in records] == [1, 1, 0, 0]


def test_virtualization(tmp_path: Path) -> None:
    proc, sys_root = tmp_path / "proc", tmp_path / "sys"
    write(proc / "sys" / "kernel" / "osrelease", "6.8.0-45-generic\n")
    write(proc / "cpuinfo", "flags\t\t: fpu vme sse2\n")
    assert lin.virtualization(proc, sys_root) is None
    write(sys_root / "class" / "dmi" / "id" / "sys_vendor", "QEMU\n")
    assert lin.virtualization(proc, sys_root) == "kvm"
    (sys_root / "class" / "dmi" / "id" / "sys_vendor").unlink()
    write(proc / "cpuinfo", fixture("cpuinfo_vm.txt"))
    assert lin.virtualization(proc, sys_root) == "hypervisor"
    write(proc / "sys" / "kernel" / "osrelease", "5.15.153.1-microsoft-standard-WSL2\n")
    assert lin.virtualization(proc, sys_root) == "wsl2"


def test_cpu_model(tmp_path: Path) -> None:
    write(tmp_path / "cpuinfo", fixture("cpuinfo_vm.txt"))
    assert lin.cpu_model(tmp_path) == "Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz"


def test_power_settings(tmp_path: Path) -> None:
    cpu_dir = tmp_path / "devices" / "system" / "cpu"
    for cpu in range(2):
        write(cpu_dir / f"cpu{cpu}" / "cpufreq" / "scaling_governor", "performance\n")
        write(cpu_dir / f"cpu{cpu}" / "cpufreq" / "energy_performance_preference", "performance\n")
    write(cpu_dir / "cpu0" / "cpufreq" / "scaling_driver", "intel_pstate\n")
    assert lin.power_settings(tmp_path) == {
        "driver": "intel_pstate",
        "governors": ["performance"],
        "energy_performance_preference": ["performance"],
    }
    assert lin.power_settings(tmp_path / "missing") == {}


def test_preexec_is_a_closure_without_side_effects() -> None:
    apply = lin.preexec([0, 1], "normal")
    assert callable(apply)
    assert lin.NICE == {"normal": 0, "above_normal": -5}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="live /proc")
def test_live_proc_self() -> None:
    import os

    with lin.ThreadReader(os.getpid()) as reader:
        stats = reader.snapshot()
    assert os.getpid() in {s.tid for s in stats}
    assert all(s.cpu_ns >= 0 and s.created_ns is not None for s in stats)
    resident, private = lin.process_memory(os.getpid())
    assert resident > 0 and private > 0
