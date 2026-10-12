# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from bedrock_bench.metrics import cpu
from bedrock_bench.model import CpuSample, MachineSample, ProbeSample, ProcSample, ThreadSample, ThreadSeries

NS = 1_000_000_000


def series(*threads: tuple[int, str, float, float, float]) -> ThreadSeries:
    """Threads ``(tid, name, born_s, exited_s, cores)`` sampled once a second from 0 to 10 s."""
    samples = []
    for t in range(11):
        for tid, name, born, exited, cores in threads:
            if born <= t <= exited:
                samples.append(ThreadSample(t * NS, tid, name, round((t - born) * cores * NS)))
    return ThreadSeries(samples)


class TestThreadDeltas:
    def test_steady_thread(self) -> None:
        rows = cpu.thread_rows(series((1, "tick", 0, 99, 0.4)), 0, 10)
        assert len(rows) == 1
        assert rows[0].ms_per_s == pytest.approx(400.0)
        assert not rows[0].born and not rows[0].exited

    def test_thread_born_inside_the_window_counts_from_zero(self) -> None:
        rows = cpu.thread_rows(series((1, "tick", 0, 99, 0.4), (2, "pool(3)", 5.5, 99, 0.5)), 0, 10)
        born = next(r for r in rows if r.tid == 2)
        assert born.born
        assert born.ms_per_s == pytest.approx(1000 * 4.5 * 0.5 / 10)

    def test_thread_that_exited_counts_to_its_last_sample(self) -> None:
        rows = cpu.thread_rows(series((1, "tick", 0, 99, 0.4), (3, "gone", 0, 4.5, 0.5)), 0, 10)
        gone = next(r for r in rows if r.tid == 3)
        assert gone.exited
        assert gone.ms_per_s == pytest.approx(1000 * 4 * 0.5 / 10)

    def test_reused_tid_is_two_threads(self) -> None:
        threads = series((1, "tick", 0, 99, 0.4), (7, "first", 0, 3.5, 0.5), (7, "second", 6, 99, 0.5))
        rows = [r for r in cpu.thread_rows(threads, 0, 10) if r.tid == 7]
        assert sorted(r.name for r in rows) == ["first", "second"]
        assert cpu.thread_deltas(threads, 0, 10)[7] == pytest.approx(1000 * (3 * 0.5 + 4 * 0.5) / 10)

    def test_reused_tid_with_the_same_name_is_split_by_the_counter(self) -> None:
        samples = [ThreadSample(t * NS, 9, "w", t * NS // 2) for t in range(5)]
        samples += [ThreadSample(t * NS, 9, "w", (t - 5) * NS // 4) for t in range(5, 11)]
        rows = cpu.thread_rows(ThreadSeries(samples), 0, 10)
        assert len(rows) == 2

    def test_window_bounds_use_the_sweeps_inside(self) -> None:
        rows = cpu.thread_rows(series((1, "tick", 0, 99, 0.4)), 2.5, 7.5)
        assert rows[0].ms_per_s == pytest.approx(400.0)  # sweeps 3..7
        assert cpu.thread_rows(series((1, "tick", 0, 99, 0.4)), 2.5, 3.5) == []

    def test_tables(self) -> None:
        threads = series(
            (1, "tick", 0, 99, 0.4),
            (2, "Pool(0)", 0, 99, 0.2),
            (3, "Pool(1)", 0, 99, 0.05),
            (4, "", 0, 99, 0.01),
        )
        rows = cpu.thread_rows(threads, 0, 10)
        top = cpu.top_threads(rows)
        assert top[0] == ["tick", 1, pytest.approx(400.0), pytest.approx(400.0), 1]
        assert top[1] == ["Pool(#)", 2, pytest.approx(250.0), pytest.approx(200.0), 1]
        assert top[2][0] == "(unnamed)"
        groups = cpu.group_threads(rows, {"pool": r"^Pool\(", "none": r"^nothing$"})
        assert groups == [["none", 0, 0.0, 0.0], ["pool", 2, pytest.approx(250.0), pytest.approx(200.0)]]

    def test_census(self) -> None:
        threads = series((1, "tick", 0, 99, 0.4), (2, "Pool(0)", 0, 99, 0.2), (3, "Pool(1)", 0, 6.5, 0.2))
        assert cpu.census(threads, 5.5) == {"Pool(#)": 2, "tick": 1}
        assert cpu.census(threads, 10) == {"Pool(#)": 1, "tick": 1}
        assert cpu.census(threads, -1) == {}


def tick_samples(util: float, until: float = 20.0, step: float = 0.1) -> list[CpuSample]:
    n = round(until / step)
    return [
        CpuSample(round(k * step * NS), round(k * step * util * NS), round(k * step * util * 3e9)) for k in range(n + 1)
    ]


def replies(rate: float, until: float = 20.0) -> list[ProbeSample]:
    return [ProbeSample(t * NS, t * NS, round(t * rate)) for t in range(0, int(until) + 1, 5)]


class TestTick:
    def test_rate_and_per_tick_cost(self) -> None:
        result = cpu.tick(tick_samples(0.5), replies(20.0), 0, 20)
        assert result is not None
        assert result.ms_per_s == pytest.approx(500.0)
        assert result.busy_frac == pytest.approx(0.5)
        assert result.ms_per_tick == pytest.approx(25.0)  # 1000 * dCPU / dG
        assert result.ticks == 400

    def test_per_tick_divides_by_executed_ticks(self) -> None:
        result = cpu.tick(tick_samples(0.5), replies(10.0), 0, 20)
        assert result is not None and result.ms_per_tick == pytest.approx(50.0)

    def test_saturation(self) -> None:
        samples = [
            CpuSample(round(k * 0.1 * NS), round(min(k * 0.1, 10) * NS + max(0, k * 0.1 - 10) * 0.3 * NS))
            for k in range(201)
        ]
        result = cpu.tick(samples, replies(20.0), 0, 20)
        assert result is not None
        assert len(result.util) == 20
        assert result.saturated_frac == pytest.approx(0.5)
        assert sorted(result.util) == pytest.approx([0.3] * 10 + [1.0] * 10)

    def test_calibrated_tsc_cycles(self) -> None:
        result = cpu.tick(tick_samples(0.5), replies(20.0), 0, 20, tsc_hz=3e9)
        assert result is not None and result.cycles_ms_per_s == pytest.approx(500.0)
        no_tsc = cpu.tick(tick_samples(0.5), replies(20.0), 0, 20)
        assert no_tsc is not None and no_tsc.cycles_ms_per_s is None

    def test_needs_two_samples(self) -> None:
        assert cpu.tick(tick_samples(0.5), replies(20.0), 30, 40) is None

    def test_per_tick_is_unavailable_without_replies(self) -> None:
        result = cpu.tick(tick_samples(0.5), [], 0, 20)
        assert result is not None and result.ms_per_tick is None


def proc(t: float, cpu_s: float, tree: float | None = None) -> ProcSample:
    return ProcSample(round(t * NS), cpu_s * 0.75, cpu_s * 0.25, 100.0, 80.0, 10, None, tree)


def test_process_and_aux_cores() -> None:
    samples = [proc(t, 1.5 * t, 0.2 * t) for t in range(11)]
    assert cpu.process_cores(samples, 0, 10) == pytest.approx(1.5)
    assert cpu.process_cpu_s(samples, 2, 4) == pytest.approx(3.0)
    assert cpu.aux_cores(samples, 0, 10) == pytest.approx(0.2)
    assert cpu.aux_cores([proc(t, t) for t in range(5)], 0, 4) is None
    assert cpu.process_cores(samples, 3.5, 3.9) is None


def test_foreign_load_and_cpu_set_busy() -> None:
    values = [0.0] * 10 + [2.0] * 10 + [0.0] * 10
    samples = [MachineSample(t * NS, (0.5, 0.5, 1.0, 0.0), v, v / 4) for t, v in enumerate(values)]
    result = cpu.foreign(samples, 0, 29)
    assert result is not None
    assert result.cores == pytest.approx(2 / 3)
    assert result.peak_10s == pytest.approx(2.0)
    assert result.server_set_cores == pytest.approx(1 / 6)
    short = cpu.foreign(samples[:3], 0, 29)
    assert short is not None and short.peak_10s == pytest.approx(0.0)
    assert cpu.foreign(samples, 100, 200) is None
    assert cpu.cpu_set_busy(samples, [0, 2], 0, 29) == pytest.approx(1.5)
    assert cpu.cpu_set_busy(samples, [], 0, 29) is None


def test_normalize_name() -> None:
    assert cpu.normalize_name("Pool(12)") == "Pool(#)"
    assert cpu.normalize_name("REST Thread(0)") == "REST Thread(#)"
    assert cpu.normalize_name("") == "(unnamed)"
