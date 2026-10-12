# SPDX-License-Identifier: Apache-2.0
"""Placement planning on recorded topologies, and launching processes with the placement in force."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from bedrock_bench import platform
from bedrock_bench.config import parse_machine
from bedrock_bench.host import (
    MIN_BOT_CPUS,
    PlacementError,
    Topology,
    format_cpus,
    placement_checks,
    plan_placement,
    verify_placement,
)
from bedrock_bench.model import LaunchSpec, Placement
from bedrock_bench.platform import CpuRecord
from bedrock_bench.process import DeathWatch, LaunchError, apply_placement, build_env, launch, wait_for_descendant

WINDOWS = sys.platform == "win32"


def topology(cores: list[tuple[list[int], int]], virtualization: str | None = None) -> Topology:
    """``cores``: (logical CPUs, efficiency class) per physical core."""
    records = tuple(CpuRecord(cpu=c, core=i, efficiency=eff) for i, (cpus, eff) in enumerate(cores) for c in cpus)
    return Topology(records, model="test", virtualization=virtualization)


HYBRID_8P_12E = topology([([2 * i, 2 * i + 1], 1) for i in range(8)] + [([16 + i], 0) for i in range(12)])
SMT_8C16T_ADJACENT = topology([([2 * i, 2 * i + 1], 0) for i in range(8)])  # Windows numbering
SMT_8C16T_SPLIT = topology([([i, i + 8], 0) for i in range(8)])  # common Linux numbering
SMT_16C32T = topology([([i, i + 16], 0) for i in range(16)])
VM_4 = topology([([i], 0) for i in range(4)], virtualization="kvm")


def machine(**placement: str):
    return parse_machine({"placement": placement})


def ids(checks) -> dict[str, str]:
    return {c.id: c.level for c in checks}


# --------------------------------------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------------------------------------


def test_hybrid_server_on_performance_cores() -> None:
    plan = plan_placement(machine(), HYBRID_8P_12E)
    assert plan.server_cpus == tuple(range(16))
    assert plan.bot_cpus == tuple(range(16, 28))
    assert plan.harness_cpus == plan.bot_cpus
    assert (plan.priority, plan.kind) == ("normal", "physical")
    assert ids(placement_checks(plan, HYBRID_8P_12E)) == {}


def test_smt_desktop_gives_bots_whole_cores_from_the_top() -> None:
    plan = plan_placement(machine(), SMT_8C16T_ADJACENT)
    assert plan.bot_cpus == (12, 13, 14, 15)
    assert plan.server_cpus == tuple(range(12))
    split = plan_placement(machine(), SMT_8C16T_SPLIT)
    assert split.bot_cpus == (6, 7, 14, 15)
    assert set(split.server_cpus) == set(range(16)) - {6, 7, 14, 15}
    assert ids(placement_checks(split, SMT_8C16T_SPLIT)) == {}


def test_bots_get_a_quarter_of_large_machines() -> None:
    plan = plan_placement(machine(), SMT_16C32T)
    assert len(plan.bot_cpus) == 8
    assert set(plan.bot_cpus) == {12, 13, 14, 15, 28, 29, 30, 31}


def test_small_vm_plans_but_doctor_fails_the_bot_minimum() -> None:
    plan = plan_placement(machine(), VM_4)
    assert (len(plan.server_cpus), len(plan.bot_cpus)) == (2, 2)
    assert plan.kind == "vcpu"
    assert ids(placement_checks(plan, VM_4))["bot_cpus"] == "fail"


def test_two_cores_and_one_core() -> None:
    plan = plan_placement(machine(), topology([([0, 1], 0), ([2, 3], 0)]))
    assert (plan.server_cpus, plan.bot_cpus) == ((0, 1), (2, 3))
    with pytest.raises(PlacementError, match="two physical cores"):
        plan_placement(machine(), topology([([0, 1], 0)]))


def test_hybrid_with_few_efficiency_cpus_falls_back_to_core_split() -> None:
    topo = topology([([2 * i, 2 * i + 1], 1) for i in range(6)] + [([12], 0), ([13], 0)])
    plan = plan_placement(machine(), topo)
    assert set(plan.bot_cpus) == {12, 13, 10, 11}  # both efficiency cores, then the highest performance core
    assert len(plan.bot_cpus) >= MIN_BOT_CPUS


def test_explicit_sets() -> None:
    plan = plan_placement(machine(server_cpus="0-7", bot_cpus="8-11", harness_cpus="12-13"), SMT_8C16T_ADJACENT)
    assert plan.server_cpus == tuple(range(8))
    assert plan.bot_cpus == (8, 9, 10, 11)
    assert plan.harness_cpus == (12, 13)


def test_auto_next_to_explicit_means_the_rest() -> None:
    plan = plan_placement(machine(server_cpus="0-9", harness_cpus="14-15"), SMT_8C16T_ADJACENT)
    assert plan.bot_cpus == (10, 11, 12, 13)
    plan = plan_placement(machine(bot_cpus="12-15"), SMT_8C16T_ADJACENT)
    assert plan.server_cpus == tuple(range(12))


def test_harness_cpus_are_reserved_from_auto() -> None:
    plan = plan_placement(machine(harness_cpus="27"), HYBRID_8P_12E)
    assert 27 not in plan.server_cpus + plan.bot_cpus
    assert plan.harness_cpus == (27,)


def test_explicit_cpus_must_exist() -> None:
    with pytest.raises(PlacementError, match="32-33"):
        plan_placement(machine(server_cpus="0-7", bot_cpus="32-33"), SMT_8C16T_ADJACENT)


def test_split_siblings_and_mixed_classes_warn() -> None:
    plan = plan_placement(machine(server_cpus="0-6", bot_cpus="7-15"), SMT_8C16T_ADJACENT)
    assert ids(placement_checks(plan, SMT_8C16T_ADJACENT))["smt_split"] == "warn"
    plan = plan_placement(machine(server_cpus="0-16", bot_cpus="17-27"), HYBRID_8P_12E)
    assert ids(placement_checks(plan, HYBRID_8P_12E))["server_classes"] == "warn"


def test_priority_comes_from_the_profile() -> None:
    plan = plan_placement(machine(priority="above_normal"), HYBRID_8P_12E)
    assert plan.priority == "above_normal"


def test_format_cpus() -> None:
    assert format_cpus([0, 1, 2, 3, 8, 10, 11]) == "0-3,8,10-11"
    assert format_cpus([5]) == "5"
    assert format_cpus([]) == "none"


# --------------------------------------------------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------------------------------------------------


def python_exe() -> str:
    """The real interpreter: a Windows venv's python.exe is a launcher that starts it as a child."""
    base = getattr(sys, "_base_executable", None)
    return base if base and os.path.exists(base) else sys.executable


AFFINITY_SCRIPT = """
import os
if hasattr(os, "sched_getaffinity"):
    cpus = sorted(os.sched_getaffinity(0))
else:
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    size_p = ctypes.POINTER(ctypes.c_size_t)
    k32.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, size_p, size_p]
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    k32.GetProcessAffinityMask(k32.GetCurrentProcess(), ctypes.byref(mask), ctypes.byref(system))
    cpus = [i for i in range(64) if mask.value >> i & 1]
print(",".join(map(str, cpus)), flush=True)
"""

PRIORITY_SCRIPT = """
import os
if hasattr(os, "getpriority"):
    print(os.getpriority(os.PRIO_PROCESS, 0), flush=True)
else:
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    print(hex(k32.GetPriorityClass(k32.GetCurrentProcess())), flush=True)
"""


def spec(tmp_path: Path, code: str, **kw) -> LaunchSpec:
    return LaunchSpec(argv=(python_exe(), "-c", code), cwd=tmp_path, **kw)


def some_cpus(n: int) -> tuple[int, ...]:
    allowed = platform.current().allowed_cpus()
    if len(allowed) < n + 1:
        pytest.skip(f"needs more than {n} usable CPUs")
    return tuple(allowed[-n:])


def run_to_end(launched) -> str:
    out, _ = launched.popen.communicate(timeout=30)
    launched.close()
    return out.decode().strip()


def test_launch_pins_from_the_first_instruction(tmp_path: Path) -> None:
    cpus = some_cpus(2)
    placement = Placement(server_cpus=cpus, bot_cpus=(), harness_cpus=(), priority="normal", kind="physical")
    launched = launch(spec(tmp_path, AFFINITY_SCRIPT), placement)
    assert launched.method == ("suspended-launch" if WINDOWS else "preexec")
    assert launched.cpus == cpus
    output = run_to_end(launched)
    assert output == ",".join(map(str, cpus))
    assert launched.popen.returncode == 0


def test_launch_verifies_and_reports_unpinned(tmp_path: Path) -> None:
    launched = launch(spec(tmp_path, "import time; time.sleep(5)"), cpus=some_cpus(1))
    try:
        assert launched.verified
        check = verify_placement(launched.process, launched.cpus or ())
        assert check.ok, check.problems
        wrong = verify_placement(launched.process, platform.current().allowed_cpus()[:1])
        assert not wrong.ok
    finally:
        launched.kill_tree()
        launched.close()
    unpinned = launch(spec(tmp_path, "pass"))
    assert unpinned.method == "none" and unpinned.verified
    run_to_end(unpinned)


def test_launch_priority(tmp_path: Path) -> None:
    impl = platform.current()
    if not impl.can_set_priority("above_normal"):
        with pytest.raises(LaunchError, match="CAP_SYS_NICE"):
            launch(spec(tmp_path, PRIORITY_SCRIPT), priority="above_normal")
        return
    launched = launch(spec(tmp_path, PRIORITY_SCRIPT), priority="above_normal")
    assert run_to_end(launched) == ("0x8000" if WINDOWS else "-5")


def test_launch_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BBENCH_TEST_REMOVE", "1")
    code = "import os; print(os.environ.get('BBENCH_TEST_REMOVE'), os.environ.get('BBENCH_TEST_SET'))"
    launched = launch(spec(tmp_path, code, env_set={"BBENCH_TEST_SET": "yes"}, env_remove=("bbench_test_remove",)))
    expected = "None yes" if WINDOWS else "1 yes"  # removal is case-insensitive only on Windows
    assert run_to_end(launched) == expected


def test_build_env_is_pure() -> None:
    base = {"PATH": "/bin", "PYTHONPATH": "x", "KEEP": "1"}
    env = build_env(LaunchSpec(argv=("x",), cwd=Path("."), env_set={"NEW": "2"}, env_remove=("PYTHONPATH",)), base)
    assert env == {"PATH": "/bin", "KEEP": "1", "NEW": "2"}
    assert base["PYTHONPATH"] == "x"


@pytest.mark.skipif(not WINDOWS, reason="CREATE_SUSPENDED is Windows only")
def test_suspended_until_resumed() -> None:
    win = platform.current()
    popen = subprocess.Popen([python_exe(), "-c", "pass"], creationflags=win.CREATE_SUSPENDED)
    try:
        proc = psutil.Process(popen.pid)
        assert proc.status() == psutil.STATUS_STOPPED
        assert win.resume_process(popen.pid) == 1
        assert popen.wait(30) == 0
    finally:
        if popen.poll() is None:
            popen.kill()


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows only")
def test_job_kills_the_tree_when_released(tmp_path: Path) -> None:
    launched = launch(spec(tmp_path, "import time; time.sleep(60)"))
    assert launched.job is not None
    launched.close()
    assert launched.wait(10) is not None


def test_apply_placement_to_a_running_process(tmp_path: Path) -> None:
    cpus = some_cpus(1)
    launched = launch(spec(tmp_path, "import time; time.sleep(5)"))
    try:
        apply_placement(launched.process, cpus, "normal")
        assert verify_placement(launched.process, cpus, "normal").ok
    finally:
        launched.kill_tree()
        launched.close()


def tree_spec(tmp_path: Path) -> LaunchSpec:
    """A root process whose executable differs from its child's (the real interpreter)."""
    sleeper = f"'{python_exe()}' -c 'import time; time.sleep(60)'"
    if WINDOWS:
        if os.path.normcase(sys.executable) == os.path.normcase(python_exe()):
            pytest.skip("needs a venv launcher to get a parent with another executable")
        return LaunchSpec(argv=(sys.executable, "-c", "import time; time.sleep(60)"), cwd=tmp_path)
    return LaunchSpec(argv=("/bin/sh", "-c", f"{sleeper}; true"), cwd=tmp_path)


def test_find_child_by_exe_and_kill_tree(tmp_path: Path) -> None:
    launched = launch(tree_spec(tmp_path))
    try:
        child = wait_for_descendant(launched.process, python_exe(), timeout_s=20)
        assert child.pid != launched.pid
        killed = launched.kill_tree()
        assert {launched.pid, child.pid} <= set(killed)
        assert not child.is_running()
    finally:
        launched.close()
    with pytest.raises(LaunchError, match="exited"):
        wait_for_descendant(launched.process, python_exe(), timeout_s=1)


def test_death_watch_reports_an_early_exit(tmp_path: Path) -> None:
    launched = launch(spec(tmp_path, "import sys, time; time.sleep(0.3); sys.exit(3)"))
    seen = []
    watch = DeathWatch(launched, interval_s=0.05, on_death=seen.append).start()
    try:
        assert watch.died.wait(20)
        assert watch.info is not None
        assert (watch.info.role, watch.info.exit_code, watch.info.pid) == ("server", 3, launched.pid)
        assert seen == [watch.info]
    finally:
        watch.stop()
        launched.close()


def test_death_watch_is_quiet_during_teardown(tmp_path: Path) -> None:
    launched = launch(spec(tmp_path, "import time; time.sleep(0.3)"))
    watch = DeathWatch(launched, interval_s=0.05).start()
    watch.expect_exit()
    assert launched.wait(20) == 0
    time.sleep(0.2)
    watch.stop()
    launched.close()
    assert not watch.died.is_set() and watch.info is None


def test_death_watch_follows_the_server_not_just_the_launcher(tmp_path: Path) -> None:
    launcher = launch(spec(tmp_path, "import time; time.sleep(60)"))
    server = launch(spec(tmp_path, "import time; time.sleep(60)"))
    watch = DeathWatch(launcher, server.process, interval_s=0.05).start()
    try:
        server.kill_tree()
        assert watch.died.wait(20)
        assert watch.info is not None and (watch.info.role, watch.info.pid) == ("server", server.pid)
    finally:
        watch.stop()
        launcher.kill_tree()
        launcher.close()
        server.close()


INTERRUPT_SCRIPT = """
import signal, sys, time
sig = signal.SIGBREAK if hasattr(signal, "SIGBREAK") else signal.SIGINT
signal.signal(sig, lambda *a: sys.exit(7))
print("ready", flush=True)
for _ in range(600):
    time.sleep(0.05)  # short sleeps: on Windows only SIGINT wakes a long sleep
"""


def test_interrupt_lets_the_program_exit_cleanly(tmp_path: Path) -> None:
    launched = launch(spec(tmp_path, INTERRUPT_SCRIPT))
    try:
        assert launched.stdout is not None
        assert launched.stdout.readline().strip() == b"ready"
        if not launched.interrupt():
            pytest.skip("no console to deliver CTRL_BREAK through")
        code = launched.wait(10)
        if code is None and WINDOWS:
            pytest.skip("CTRL_BREAK not delivered (no shared console)")
        assert code == 7
    finally:
        launched.kill_tree()
        launched.close()
