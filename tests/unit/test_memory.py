# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from bedrock_bench.metrics import memory
from bedrock_bench.model import ProcSample

NS = 1_000_000_000


def sample(t: float, private: float) -> ProcSample:
    return ProcSample(round(t * NS), 0.0, 0.0, 2 * private, private, 40)


def test_window_mean() -> None:
    mem = memory.window([sample(t, 1000.0 + t) for t in range(11)], 0, 10)
    assert mem is not None
    assert mem.samples == 11
    assert mem.private_mean == pytest.approx(1005.0)


def test_empty_window_is_none() -> None:
    assert memory.window([sample(t, 1.0) for t in range(3)], 5, 10) is None
