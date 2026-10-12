# SPDX-License-Identifier: Apache-2.0
"""Samplers, series files and the tick probe; ends with a short live run against a CPU-burning child process."""

from __future__ import annotations

import gzip
import os
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import psutil
import pytest

from bedrock_bench import platform
from bedrock_bench.clock import mono_ns
from bedrock_bench.model import LaunchSpec, ProbeSample
from bedrock_bench.platform import ProcessGone, ThreadStat
from bedrock_bench.process import launch
from bedrock_bench.sampling import MachineSampler, ProcessSampler, ThreadSampler, TickProbe, TickSampler, series
from bedrock_bench.sampling.base import PeriodicSampler, ProcessGroup, children_map, descendants
from bedrock_bench.sampling.machine import CpuMeter, ForeignLoad, OwnGroups, _top, busy_total

WINDOWS = sys.platform == "win32"


def python_exe() -> str:
    base = getattr(sys, "_base_executable", None)
    return base if base and os.path.exists(base) else sys.executable


# --------------------------------------------------------------------------------------------------------------------
# Series files
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "digits", "text"),
    [(1.5, 6, "1.5"), (2.0, 6, "2"), (0.1234567, 4, "0.1235"), (-0.00001, 4, "0"), (None, 6, ""), (7, 6, "7")],
)
def test_fmt(value: float | None, digits: int, text: str) -> None:
    assert series.fmt(value, digits) == text


def test_round_trip_every_format(tmp_path: Path) -> None:
    with series.SeriesWriter(tmp_path / series.PROBE_FILE, series.PROBE_COLUMNS) as w:
        w.write([1, 5, 100])
    assert series.read_probe(tmp_path / series.PROBE_FILE) == [ProbeSample(1, 5, 100)]

    with series.SeriesWriter(tmp_path / series.THREADS_FILE, (*series.THREAD_COLUMNS, "cycles")) as w:
        w.write([10, 4242, "REST Thread(0)", 5000, 12345])
        w.write([10, 4243, 'odd "name", with comma', 6000, ""])
    threads = series.read_threads(tmp_path / series.THREADS_FILE)
    assert [(s.tid, s.name, s.cpu_ns, s.cycles) for s in threads.samples] == [
        (4242, "REST Thread(0)", 5000, 12345),
        (4243, 'odd "name", with comma', 6000, None),
    ]

    with series.SeriesWriter(tmp_path / series.TICK_FILE, series.TICK_COLUMNS) as w:
        w.write([1, 2])
    (tick,) = series.read_tick(tmp_path / series.TICK_FILE)
    assert (tick.t_ns, tick.cpu_ns, tick.cycles) == (1, 2, None)

    with series.SeriesWriter(tmp_path / series.PROCESS_FILE, series.PROCESS_COLUMNS) as w:
        w.write([1, "1.5", "0.25", "100", "80.5", 30, "", "0.5", "0.25"])
        w.write([2, "1.5", "0.25", "100", "80.5", 30, 400, "", ""])
    first, second = series.read_process(tmp_path / series.PROCESS_FILE)
    assert (first.cpu_user_s, first.handles, first.tree_cpu_s) == (1.5, None, 0.75)
    assert (second.handles, second.tree_cpu_s) == (400, None)

    columns = (*series.MACHINE_COLUMNS, "cpu0", "cpu1", "cpu10", "cpu2")
    with series.SeriesWriter(tmp_path / series.MACHINE_FILE, columns) as w:
        w.write([5, "0.1", "0.05", "1.2", "0.4", "0.02", "0.5", "0.6", "1", "0.7"])
    (machine,) = series.read_machine(tmp_path / series.MACHINE_FILE)
    assert machine.per_cpu_busy == (0.5, 0.6, 0.7, 1.0)  # ordered by CPU number, not by column text
    assert (machine.foreign_cores, machine.foreign_server_set_cores) == (0.1, 0.05)
    assert series.read_group_cores(tmp_path / series.MACHINE_FILE) == [
        (5, {"server": 1.2, "bot": 0.4, "harness": 0.02})
    ]


def test_writer_rejects_ragged_rows(tmp_path: Path) -> None:
    with series.SeriesWriter(tmp_path / "x.csv", ("a", "b")) as w, pytest.raises(ValueError, match="2"):
        w.write([1])


def test_gzip_output_is_reproducible(tmp_path: Path) -> None:
    for name in ("a.csv.gz", "b.csv.gz"):
        with series.SeriesWriter(tmp_path / name, series.TICK_COLUMNS) as w:
            w.write([1, 2])
    assert (tmp_path / "a.csv.gz").read_bytes() == (tmp_path / "b.csv.gz").read_bytes()


def test_readers_keep_everything_before_a_cut(tmp_path: Path) -> None:
    path = tmp_path / series.TICK_FILE
    with series.SeriesWriter(path, series.TICK_COLUMNS) as w:
        for i in range(2000):
            w.write([i, i * 10])
    data = path.read_bytes()
    cut = tmp_path / "cut.csv.gz"
    cut.write_bytes(data[: len(data) // 2])
    rows = series.read_tick(cut)
    assert 0 < len(rows) < 2000
    assert [r.t_ns for r in rows] == list(range(len(rows)))

    plain = tmp_path / "probe.csv"
    plain.write_text("send_ns,rx_ns,tick\n1,2,3\n4,5,", encoding="utf-8")
    assert series.read_probe(plain) == [ProbeSample(1, 2, 3)]


def test_flush_makes_rows_readable_while_open(tmp_path: Path) -> None:
    path = tmp_path / series.TICK_FILE
    writer = series.SeriesWriter(path, series.TICK_COLUMNS, flush_s=0.0)
    writer.write([1, 2])
    assert [r.cpu_ns for r in series.read_tick(path)] == [2]
    writer.close()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert fh.read() == "t_ns,cpu_ns\n1,2\n"


# --------------------------------------------------------------------------------------------------------------------
# Tick probe
# --------------------------------------------------------------------------------------------------------------------


class FakeConsole:
    """Records probe sends and stamps them with a scripted clock."""

    def __init__(self, stamps: list[int]) -> None:
        self.sent: list[str] = []
        self._stamps = iter(stamps)

    def send(self, text: str, on_sent) -> None:
        self.sent.append(text)
        on_sent(next(self._stamps))


def parse_gametime(text: str) -> int | None:
    prefix = "Time is "
    return int(text[len(prefix) :]) if text.startswith(prefix) else None


def make_probe(tmp_path: Path, console: FakeConsole, clock_values: list[int]) -> TickProbe:
    clock = iter(clock_values)
    return TickProbe(
        "time query gametime",
        parse_gametime,
        console.send,
        tmp_path / series.PROBE_FILE,
        timeout_s=10.0,
        clock=lambda: next(clock),
    )


def test_probe_pairs_replies_in_order(tmp_path: Path) -> None:
    console = FakeConsole([105, 205])
    probe = make_probe(tmp_path, console, [100, 200])
    probe.probe_once()
    probe.probe_once()
    assert console.sent == ["time query gametime"] * 2
    assert not probe.feed(150, "Unknown command")
    assert probe.feed(160, "Time is 1000")
    assert probe.feed(260, "Time is 1100")
    assert probe.samples == [ProbeSample(105, 160, 1000), ProbeSample(205, 260, 1100)]
    assert probe.feed(300, "Time is 1200")  # nobody asked
    assert probe.unmatched == 1
    probe.stop(drain_s=0)
    assert series.read_probe(tmp_path / series.PROBE_FILE) == probe.samples


def test_probe_rate_for_console_pacing(tmp_path: Path) -> None:
    console = FakeConsole([0, 5_000_000_000])
    probe = make_probe(tmp_path, console, [0, 5_000_000_000])
    assert probe.rate() is None
    probe.probe_once()
    probe.feed(1_000_000_000, "Time is 100")
    assert probe.rate() is None
    probe.probe_once()
    probe.feed(6_000_000_000, "Time is 175")
    assert probe.rate() == pytest.approx(15.0)
    probe.stop(drain_s=0)


def test_probe_drops_sends_that_time_out(tmp_path: Path) -> None:
    console = FakeConsole([0, 20_000_000_000])
    probe = make_probe(tmp_path, console, [0, 20_000_000_000])
    probe.probe_once()
    probe.probe_once()  # the first send is now 20 s old: lost
    assert probe.lost == 1
    probe.feed(20_100_000_000, "Time is 5")
    assert probe.samples == [ProbeSample(20_000_000_000, 20_100_000_000, 5)]
    probe.stop(drain_s=0)


def test_probe_counts_replies_that_beat_the_send_stamp(tmp_path: Path) -> None:
    probe = TickProbe("tick", parse_gametime, lambda text, on_sent: None, None, clock=lambda: 50)
    probe.probe_once()
    probe.feed(60, "Time is 9")
    assert probe.unstamped == 1
    assert probe.samples == [ProbeSample(50, 60, 9)]
    probe.stop(drain_s=0)
    assert probe.lost == 0


def test_probe_thread_sends_on_schedule_and_stops_when_the_console_closes(tmp_path: Path) -> None:
    sent = threading.Event()
    calls = []

    def send(text: str, on_sent) -> None:
        calls.append(text)
        on_sent(mono_ns())
        sent.set()
        if len(calls) == 3:
            raise BrokenPipeError("console closed")

    probe = TickProbe("tick", parse_gametime, send, tmp_path / series.PROBE_FILE, interval_s=0.02).start()
    assert sent.wait(5)
    deadline = time.monotonic() + 5
    while probe.error is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert isinstance(probe.error, BrokenPipeError)
    probe.stop(drain_s=0)
    assert len(calls) == 3 and probe.lost == 3


# --------------------------------------------------------------------------------------------------------------------
# Sampler loop and accounting helpers
# --------------------------------------------------------------------------------------------------------------------


class Counter(PeriodicSampler):
    def __init__(self, fail_at: int | None = None, gone_at: int | None = None) -> None:
        super().__init__("counter", 0.01)
        self.fail_at, self.gone_at = fail_at, gone_at
        self.closed = False

    def sample(self) -> None:
        if self.samples == self.gone_at:
            raise ProcessGone(1)
        if self.samples == self.fail_at:
            raise RuntimeError("boom")

    def teardown(self) -> None:
        self.closed = True


def test_sampler_stop_takes_a_final_sample() -> None:
    sampler = Counter()
    sampler.start()
    time.sleep(0.05)
    sampler.stop()
    assert sampler.ended == "stopped" and sampler.closed and sampler.samples >= 2
    assert sampler.error is None


def test_sampler_ends_quietly_when_the_target_exits() -> None:
    sampler = Counter(gone_at=3)
    sampler.start()
    sampler.join(5)
    assert (sampler.ended, sampler.samples, sampler.closed, sampler.error) == ("target_exited", 3, True, None)


def test_sampler_keeps_unexpected_errors() -> None:
    sampler = Counter(fail_at=2)
    sampler.start()
    sampler.join(5)
    assert sampler.ended == "error" and isinstance(sampler.error, RuntimeError) and sampler.closed


def test_descendants() -> None:
    tree = {1: [2, 3], 2: [4], 4: [5], 9: [10]}
    assert descendants(tree, 1) == {2, 3, 4, 5}
    assert descendants(tree, 5) == set()


Linux = namedtuple("Linux", "user nice system idle iowait irq softirq steal guest guest_nice")
Windows = namedtuple("Windows", "user system idle interrupt dpc")


def test_busy_total_per_os() -> None:
    assert busy_total(Linux(1, 1, 2, 5, 1, 0.5, 0.5, 1, 0, 0)) == (6.0, 12.0)
    assert busy_total(Windows(3, 2, 5, 0.5, 0.5)) == (5, 10)


def test_cpu_meter_fractions() -> None:
    samples = iter(
        [
            [Windows(0, 0, 0, 0, 0), Windows(0, 0, 0, 0, 0)],
            [Windows(0.5, 0.25, 0.25, 0, 0), Windows(0, 0, 1, 0, 0)],
        ]
    )
    meter = CpuMeter(lambda percpu: next(samples))
    assert meter.count == 2
    assert meter.read() == (0.75, 0.0)


def test_foreign_top_counts_new_processes_whole() -> None:
    before = {(1, 10): ("indexer", 1.0), (9, 90): ("gone", 5.0)}
    after = {(1, 10): ("indexer", 2.0), (2, 20): ("compiler", 0.5), (3, 30): ("idle", 0.001), (1, 11): ("reused", 0.3)}
    assert _top(before, after, 10.0, 10) == [
        {"pid": 1, "name": "indexer", "cores": 0.1},
        {"pid": 2, "name": "compiler", "cores": 0.05},
        {"pid": 1, "name": "reused", "cores": 0.03},
    ]
    assert _top(before, after, 10.0, 1) == [{"pid": 1, "name": "indexer", "cores": 0.1}]


def test_foreign_load_peak() -> None:
    load = ForeignLoad(1.0, tuple([0.1] * 10 + [2.0] * 10 + [0.1] * 10), (0.0,) * 30)
    assert load.peak(10.0) == pytest.approx(2.0)
    assert load.mean == pytest.approx(0.7333, abs=1e-3)
    short = ForeignLoad(1.0, (0.2, 0.4), (0.0, 0.0))
    assert short.peak(10.0) == pytest.approx(0.3)


SLEEPER = "import time; time.sleep(30)"
BURNER = """
import sys, time
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    pass
"""


def start(code: str, *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([python_exe(), "-c", code, *args])


def test_process_group_counts_from_join_and_keeps_exited_members() -> None:
    child = start(BURNER, "0.8")
    try:
        group = ProcessGroup([child.pid])
        assert sum(group.cpu()) < 0.3
        used = 0.0
        while child.poll() is None:  # samplers read every second; the last reading before exit is kept
            used = sum(group.cpu())
            time.sleep(0.05)
        assert used > 0.3
        assert sum(group.cpu()) == used
        assert group.pids == set()
    finally:
        if child.poll() is None:
            child.kill()


def test_children_map_ignores_reused_parent_pids() -> None:
    table = [
        platform.ProcEntry(pid=10, ppid=1, name="parent", cpu_s=0, started=100),
        platform.ProcEntry(pid=11, ppid=10, name="child", cpu_s=0, started=150),
        platform.ProcEntry(pid=12, ppid=10, name="orphan of an older pid 10", cpu_s=0, started=50),
    ]
    assert children_map(table) == {10: [11]}
    assert any(e.pid == os.getpid() for e in platform.current().process_table())


def test_own_groups_assign_each_process_once() -> None:
    server = start(SLEEPER)
    try:
        own = OwnGroups()
        own.add_root("server", server.pid)
        assigned = own.refresh(children_map())
        assert server.pid in own.groups["server"].pids
        assert server.pid not in own.groups["harness"].pids  # a child of the harness, but claimed by the server
        assert os.getpid() in own.groups["harness"].pids
        assert {server.pid, os.getpid()} <= assigned
    finally:
        server.kill()
        server.wait(30)


# --------------------------------------------------------------------------------------------------------------------
# Thread sampler with a scripted reader
# --------------------------------------------------------------------------------------------------------------------


class ScriptedReader:
    cycles_supported = True

    def __init__(self, snapshots: list[list[ThreadStat]]) -> None:
        self._snapshots = iter(snapshots)
        self.closed = False

    def snapshot(self) -> list[ThreadStat]:
        try:
            return next(self._snapshots)
        except StopIteration:
            raise ProcessGone(1) from None

    def close(self) -> None:
        self.closed = True


def test_thread_sampler_rows_and_infos(tmp_path: Path) -> None:
    reader = ScriptedReader(
        [
            [ThreadStat(1, "main", 100, 10, cycles=5, created_ns=1), ThreadStat(2, "", 0, 11, created_ns=2)],
            [ThreadStat(1, "Main", 200, 20, cycles=9, created_ns=1), ThreadStat(2, "pool #1", 50, 21, alive=False)],
        ]
    )
    sampler = ThreadSampler(1, tmp_path / series.THREADS_FILE, interval_s=0.01, reader=reader)
    sampler.start()
    sampler.join(5)
    assert sampler.ended == "target_exited" and reader.closed
    rows = series.read_threads(tmp_path / series.THREADS_FILE).samples
    assert [(r.t_ns, r.tid, r.name, r.cpu_ns, r.cycles) for r in rows] == [
        (10, 1, "main", 100, 5),
        (11, 2, "", 0, None),
        (20, 1, "Main", 200, 9),
        (21, 2, "pool #1", 50, None),
    ]
    assert [(i.tid, i.name) for i in sampler.infos()] == [(1, "Main")]  # thread 2 exited


# --------------------------------------------------------------------------------------------------------------------
# Live: a CPU-burning child, every sampler at a fast rate
# --------------------------------------------------------------------------------------------------------------------


def test_samplers_on_a_cpu_burner(tmp_path: Path) -> None:
    allowed = platform.current().allowed_cpus()
    if len(allowed) < 2:
        pytest.skip("needs two usable CPUs")
    cpus = allowed[-1:]
    out = tmp_path / "series"
    burner = launch(LaunchSpec(argv=(python_exe(), "-c", BURNER, "2.5"), cwd=tmp_path), cpus=cpus)
    try:
        threads = ThreadSampler(burner.pid, out / series.THREADS_FILE, interval_s=0.2)
        process = ProcessSampler(burner.process, out / series.PROCESS_FILE, interval_s=0.2)
        machine = MachineSampler(
            out / series.MACHINE_FILE, out / series.FOREIGN_TOP_FILE, cpus, interval_s=0.25, top_interval_s=0.5
        )
        machine.add_root("server", burner.process)
        main_tid = min(platform.thread_infos(burner.pid), key=lambda t: (t.created_ns or 0, t.tid)).tid
        tick = TickSampler(burner.pid, main_tid, out / series.TICK_FILE, interval_s=0.05)
        for sampler in (threads, process, machine, tick):
            sampler.start()
        time.sleep(1.5)
        assert {i.tid for i in threads.infos()} >= {main_tid}
        for sampler in (threads, process, machine, tick):
            sampler.stop()
            assert sampler.error is None, sampler.error
    finally:
        burner.kill_tree()
        burner.close()

    tick_rows = series.read_tick(out / series.TICK_FILE)
    assert len(tick_rows) >= 10
    assert tick_rows[-1].cpu_ns - tick_rows[0].cpu_ns > 0.5e9  # one core busy for ~1.5 s
    steps = [b.t_ns - a.t_ns for a, b in zip(tick_rows, tick_rows[1:], strict=False)]
    if time.get_clock_info("monotonic").resolution < 1e-3:
        assert all(step > 0 for step in steps)
    else:  # Python < 3.13 on Windows: monotonic ticks every 15.6 ms
        assert all(step >= 0 for step in steps)
    if WINDOWS:
        assert all(r.cycles is not None for r in tick_rows)

    thread_rows = series.read_threads(out / series.THREADS_FILE).samples
    assert main_tid in {r.tid for r in thread_rows}

    proc_rows = series.read_process(out / series.PROCESS_FILE)
    assert len(proc_rows) >= 4
    used = (proc_rows[-1].cpu_user_s + proc_rows[-1].cpu_sys_s) - (proc_rows[0].cpu_user_s + proc_rows[0].cpu_sys_s)
    assert used > 0.5
    assert all(r.resident_mib > 1 and r.threads >= 1 and r.tree_cpu_s is None for r in proc_rows)
    assert all((r.handles is not None) == WINDOWS for r in proc_rows)

    machine_rows = series.read_machine(out / series.MACHINE_FILE)
    groups = series.read_group_cores(out / series.MACHINE_FILE)
    assert len(machine_rows) >= 4
    assert len(machine_rows[0].per_cpu_busy) == psutil.cpu_count()
    server_cores = [g["server"] for _, g in groups[1:-1]]
    assert sum(server_cores) / len(server_cores) > 0.5  # the burner is ours, not foreign
    busy_on_set = [r.per_cpu_busy[cpus[0]] for r in machine_rows[1:-1]]
    assert sum(busy_on_set) / len(busy_on_set) > 0.5
    for line in series.read_jsonl(out / series.FOREIGN_TOP_FILE):
        assert burner.pid not in {t["pid"] for t in line["top"]}


# --------------------------------------------------------------------------------------------------------------------
# Windows thread reader
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="Windows thread descriptions")
def test_windows_thread_names_start_modules_and_final_rows() -> None:
    import ctypes

    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentThread.restype = ctypes.c_void_p
    k32.SetThreadDescription.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    named = threading.Event()
    release = threading.Event()
    tid_box: list[int] = []

    def worker() -> None:
        k32.SetThreadDescription(k32.GetCurrentThread(), "bench worker (1)")
        tid_box.append(threading.get_native_id())
        named.set()
        release.wait(10)

    thread = threading.Thread(target=worker)
    thread.start()
    assert named.wait(5)
    reader = platform.current().ThreadReader(os.getpid())
    try:
        stats = {s.tid: s for s in reader.snapshot()}
        mine = stats[tid_box[0]]
        assert mine.name == "bench worker (1)"
        assert mine.alive and mine.created_ns is not None
        assert mine.cycles is None  # per-thread cycles are left to the tick reader
        assert mine.start_module is not None and mine.start_module.lower().endswith(".dll")
        main = stats[threading.main_thread().native_id]
        assert main.start_module is not None and main.start_module.lower().endswith(".exe")
        release.set()
        thread.join(5)
        final = {s.tid: s for s in reader.snapshot()}
        assert final[tid_box[0]].alive is False  # one last row with the exact final CPU time
        assert tid_box[0] not in {s.tid for s in reader.snapshot()}
    finally:
        release.set()
        reader.close()


@pytest.mark.skipif(not WINDOWS, reason="QueryThreadCycleTime")
def test_tsc_calibration_is_plausible() -> None:
    hz = platform.current().calibrate_tsc(seconds=0.05, rounds=2)
    assert 0.5e9 < hz < 10e9
