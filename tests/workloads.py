# SPDX-License-Identifier: Apache-2.0
"""Workloads for unit tests, loaded from the project's configs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bedrock_bench import config
from bedrock_bench.workload import Workload

CONFIGS = Path(__file__).resolve().parents[1] / config.CONFIGS_DIR

WORKLOAD = config.load_workload(CONFIGS / "spread-100.toml")
SMOKE = config.load_workload(CONFIGS / "smoke.toml")


def with_overrides(workload: Workload, data: Mapping[str, Any]) -> Workload:
    """``workload`` with some values replaced, as a config that inherits from it would set them."""
    return config.parse_workload(config.deep_merge(workload.params, data), name=workload.name, base=workload.base)
