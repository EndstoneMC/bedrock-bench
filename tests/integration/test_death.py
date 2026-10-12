# SPDX-License-Identifier: Apache-2.0
"""Server failures are results (rule R9): a crash, a bad exit, a hang on stop or a refused join ends as
``server_failed`` with exit code 4, a kept run directory and the evidence collected, never as a dropped run.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from bedrock_bench.model import ExitCode, Status
from fakes.bench import FakeBench, Run

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> FakeBench:
    return FakeBench.create(tmp_path_factory.mktemp("bench"))


def assert_server_failed(run: Run, gate: str) -> dict[str, Any]:
    assert run.exit_code == ExitCode.SERVER_FAILED, run.describe()
    record = run.run_json
    assert record["status"] == Status.SERVER_FAILED
    assert record["exit_code"] == ExitCode.SERVER_FAILED
    assert any(gate in reason for reason in record["status_reasons"]), record["status_reasons"]
    assert (run.require_dir() / "metrics.json").is_file(), "failed runs are analysed and kept"
    return record


def test_crash_after_join(bench: FakeBench) -> None:
    run = bench.run_one(behaviour="crash-after-join")
    record = assert_server_failed(run, "server_alive")
    health = record["health"]
    assert health["died"] is True
    assert health["server_exit"] == 3
    assert health["crash_files"], "the crash report is listed"
    crash_dir = run.require_dir() / "crash"
    copied = [p for p in crash_dir.rglob("*") if p.is_file()]
    assert any("scripted crash after join" in p.read_text(encoding="utf-8", errors="replace") for p in copied), (
        "crash/ holds the server's crash report and the log tail"
    )


def test_crash_during_boot(bench: FakeBench) -> None:
    run = bench.run_one(behaviour="crash-at-boot")
    record = assert_server_failed(run, "server_alive")
    assert record["health"]["died"] is True
    assert record["marks"].get("ready") is None
    assert (run.require_dir() / "crash").is_dir()


def test_nonzero_exit_after_stop(bench: FakeBench) -> None:
    run = bench.run_one(behaviour="exit-nonzero")
    record = assert_server_failed(run, "server_alive")
    assert record["health"]["died"] is False, "the server lived until teardown"
    assert record["health"]["server_exit"] == 1


def test_hang_on_stop_is_killed(bench: FakeBench) -> None:
    short_stop = bench.config("short-stop", phases={"teardown": {"stop_timeout_s": 2.0}})
    started = time.monotonic()
    run = bench.run_one(behaviour="hang-on-stop", config=short_stop)
    assert time.monotonic() - started < 120, "the harness kills a server that ignores stop"
    record = assert_server_failed(run, "server_alive")
    assert record["health"]["server_exit"] != 0


def test_refused_join(bench: FakeBench) -> None:
    run = bench.run_one(behaviour="reject-join")
    record = assert_server_failed(run, "joined_all")
    assert (record["health"]["joined"], record["health"]["failed"]) == (3, 1)
