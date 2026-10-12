# SPDX-License-Identifier: Apache-2.0
"""Foreign load is guarded, not tolerated (rule R4): a process burning a server CPU during the measured windows makes
the run invalid, and foreign load before a session starts makes the runner refuse to begin.

The CPU burner is a child of the test process, so it is outside the server, bot and harness process trees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bedrock_bench.model import ExitCode, Status
from fakes.bench import FakeBench, cpu_burner, read_json

pytestmark = pytest.mark.integration


def test_cpu_burner_on_server_cpus_invalidates_the_run(tmp_path: Path) -> None:
    bench = FakeBench.create(tmp_path)
    machine = bench.machine("strict", strict_gates=True)
    args = (bench.fake_config, "--server", "fake_bds@1.0.0", "--machine", machine, "--out", bench.out)
    run = bench.start("run-one", *args)
    try:
        joined = bench.wait_for_mark("alljoined", run)
        if joined:
            with cpu_burner(bench.server_cpus[:1]):
                result = run.finish(300)
        else:
            result = run.finish(300)
    finally:
        if run.poll() is None:
            run.kill()
    assert joined, f"the run never recorded alljoined\n{result.stdout}\n{result.stderr}"
    assert result.returncode == ExitCode.INVALID, result.stdout + result.stderr
    (run_dir,) = bench.run_dirs()
    record = read_json(run_dir / "run.json")
    assert record["status"] == Status.INVALID
    assert any("foreign" in reason for reason in record["status_reasons"]), record["status_reasons"]


def test_foreign_load_before_a_session_is_refused(tmp_path: Path) -> None:
    bench = FakeBench.create(tmp_path)
    bench.machine("strict", strict_gates=True)
    with cpu_burner(bench.server_cpus[:1]):
        result = bench.bbench(*bench.session_args("fake_bds", machine="strict"))
    assert result.returncode == ExitCode.ENV_GATE, result.stdout + result.stderr
    assert bench.run_dirs() == [], "no run starts on a busy machine"
