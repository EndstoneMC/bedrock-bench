# SPDX-License-Identifier: Apache-2.0
"""bedrock-bench: reproducible, fair benchmarks for Minecraft Bedrock servers."""

__version__ = "0.1.0.dev0"

#: Major version of the result schemas (``bedrock-bench/run/1``, ``.../metrics/1``, ``.../bot/1``).
#: Removing, renaming or redefining a field bumps it; the old schema file is kept and results.py migrates.
SCHEMA_VERSION = 1

#: Minor version of the result schemas. Additive changes bump it.
SCHEMA_MINOR = 0

#: Version of the metric formulas. Any change to a formula in ``metrics/`` bumps it.
ANALYSIS_VERSION = 3

RUN_SCHEMA = f"bedrock-bench/run/{SCHEMA_VERSION}"
METRICS_SCHEMA = f"bedrock-bench/metrics/{SCHEMA_VERSION}"
BOT_SCHEMA = f"bedrock-bench/bot/{SCHEMA_VERSION}"

__all__ = [
    "ANALYSIS_VERSION",
    "BOT_SCHEMA",
    "METRICS_SCHEMA",
    "RUN_SCHEMA",
    "SCHEMA_MINOR",
    "SCHEMA_VERSION",
    "__version__",
]
