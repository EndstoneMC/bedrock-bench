# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from bedrock_bench.metrics import cpu
from bedrock_bench.model import CpuSample, MachineSample, ProbeSample, ProcSample

NS = 1_000_000_000


def tick_samples(util: float, until: float = 20.0, step: float = 0.1) -> list[CpuSample]:
    n = round(until / step)
    return [
        CpuSample(round(k * step * NS), round(k * step * util * NS), round(k * step * util * 3e9)) for k in range(n + 1)
    ]


def replies(rate: float, until: float = 20.0) -> list[ProbeSample]:
    return [ProbeSample(t * NS, t * NS, round(t * rate)) for t in range(0, int(until) + 1, 5)]


class TestTick:
    def test_per_tick_cost(self) -> None:
        result = cpu.tick(tick_samples(0.5), replies(20.0), 0, 20)
        assert result is not None
        assert result.ms_per_tick == pytest.approx(25.0)  # 1000 * dCPU / dG
        assert result.ticks == 400

    def test_per_tick_divides_by_executed_ticks(self) -> None:
        result = cpu.tick(tick_samples(0.5), replies(10.0), 0, 20)
        assert result is not None and result.ms_per_tick == pytest.approx(50.0)

    def test_needs_two_samples(self) -> None:
        assert cpu.tick(tick_samples(0.5), replies(20.0), 30, 40) is None

    def test_per_tick_is_unavailable_without_replies(self) -> None:
        result = cpu.tick(tick_samples(0.5), [], 0, 20)
        assert result is not None and result.ms_per_tick is None


def proc(t: float, cpu_s: float) -> ProcSample:
    return ProcSample(round(t * NS), cpu_s * 0.75, cpu_s * 0.25, 100.0, 80.0, 10)


def test_process_cores() -> None:
    samples = [proc(t, 1.5 * t) for t in range(11)]
    assert cpu.process_cores(samples, 0, 10) == pytest.approx(1.5)
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
