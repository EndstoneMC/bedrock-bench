# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from bedrock_bench.metrics import memory
from bedrock_bench.model import ProcSample

NS = 1_000_000_000


def sample(t: float, private: float, resident: float, threads: int = 40, handles: int | None = 900) -> ProcSample:
    return ProcSample(round(t * NS), 0.0, 0.0, resident, private, threads, handles)


def test_window_mean_and_max() -> None:
    samples = [sample(t, 1000.0 + t, 2000.0 + 2 * t, threads=40 + t) for t in range(11)]
    mem = memory.window(samples, 0, 10)
    assert mem is not None
    assert mem.samples == 11
    assert mem.private_mean == pytest.approx(1005.0)
    assert mem.private_max == 1010.0
    assert mem.resident_mean == pytest.approx(2010.0)
    assert mem.resident_max == 2020.0
    assert mem.threads_max == 50
    assert mem.handles_max == 900


def test_handles_are_optional_and_empty_windows_are_none() -> None:
    samples = [sample(t, 1.0, 1.0, handles=None) for t in range(3)]
    mem = memory.window(samples, 0, 10)
    assert mem is not None and mem.handles_max is None
    assert memory.window(samples, 5, 10) is None


def test_slope_in_mib_per_hour() -> None:
    samples = [sample(t * 60, 1000.0 + t, 0.0) for t in range(31)]  # +1 MiB per minute
    assert memory.slope_mib_per_h(samples, 0, 1800) == pytest.approx(60.0)
    assert memory.slope_mib_per_h(samples[:1], 0, 1800) is None


def test_start_end() -> None:
    samples = [sample(t, float(t), 0.0, threads=t) for t in range(10)]
    ends = memory.start_end(samples, 2.5, 7.5)
    assert ends is not None
    assert (ends[0].threads, ends[1].threads) == (3, 7)
    assert memory.start_end(samples, 20, 30) is None
