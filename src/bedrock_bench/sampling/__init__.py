# SPDX-License-Identifier: Apache-2.0
"""Samplers that write a run's ``series/`` files while it runs.

- :class:`ThreadSampler`, 1 Hz, ``threads.csv.gz``: CPU time of every server thread; exited threads keep their rows.
- :class:`TickSampler`, 10 Hz, ``tick.csv.gz``: CPU time of the tick thread.
- :class:`ProcessSampler`, 1 Hz, ``process.csv.gz``: process CPU, memory, threads, handles and the launcher tree.
- :class:`MachineSampler`, 1 Hz, ``machine.csv.gz`` and ``foreign_top.jsonl``: per-CPU busy, CPU of our own process
  trees, foreign load, and the processes behind foreign load every 10 s.
- :class:`TickProbe`, every 5 s, ``probe.csv``: the server's tick counter through the console priority lane.

Every stamp is taken right after the read it belongs to, on the harness monotonic clock. Samplers end quietly when
the process they watch exits. ``python -m bedrock_bench.sampling`` runs the process samplers as a standalone sidecar
(the Docker runtime starts one next to the server container). File formats and readers are in
:mod:`bedrock_bench.sampling.series`.
"""

from __future__ import annotations

from bedrock_bench.sampling.machine import MachineSampler, measure_foreign
from bedrock_bench.sampling.probe import TickProbe
from bedrock_bench.sampling.process import ProcessSampler
from bedrock_bench.sampling.threads import ThreadSampler
from bedrock_bench.sampling.tick import TickSampler

__all__ = ["MachineSampler", "ProcessSampler", "ThreadSampler", "TickProbe", "TickSampler", "measure_foreign"]
