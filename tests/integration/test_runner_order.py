# SPDX-License-Identifier: Apache-2.0
"""``bbench run`` as one session: rotate order, one subprocess and directory per run, one session id and directory,
and a report with one Results row per server.

The session runs two servers, ``fake_bds`` and ``fake_dragonfly``, each twice.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench.model import ExitCode, Status
from fakes.bench import FakeBench, read_json

pytestmark = pytest.mark.integration

BDS, DRAGONFLY = "fake_bds@1.0.0", "fake_dragonfly@1.0.0"
ARMS = [BDS, DRAGONFLY]
SESSION_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-fake_(bds|dragonfly)@1\.0\.0-r[12]-[0-9a-f]{4}$")


@pytest.fixture(scope="module")
def session_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[FakeBench, subprocess.CompletedProcess[str]]:
    bench = FakeBench.create(tmp_path_factory.mktemp("bench"))
    result = bench.bbench(*bench.session_args(*ARMS, repeats=2), timeout=900)
    return bench, result


def records(bench: FakeBench) -> list[dict[str, Any]]:
    return [read_json(run_dir / "run.json") for run_dir in bench.run_dirs()]


def session_dir(bench: FakeBench) -> Path:
    (found,) = [p for p in bench.out.iterdir() if p.is_dir()]
    return found


def completed(session_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> FakeBench:
    """The bench of a session that finished every run; tests that build on it need that first."""
    bench, result = session_run
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    assert len(bench.run_dirs()) == 4
    return bench


def test_every_server_and_repeat_runs_once(session_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench, result = session_run
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    runs = records(bench)
    assert len(runs) == 4
    assert all(run["status"] == Status.OK for run in runs), [run["status_reasons"] for run in runs]
    assert sorted((run["arm"]["id"], run["session"]["repeat"]) for run in runs) == [
        (BDS, 1),
        (BDS, 2),
        (DRAGONFLY, 1),
        (DRAGONFLY, 2),
    ]
    ids = [run["run_id"] for run in runs]
    assert len(set(ids)) == 4
    assert all(RUN_ID.match(run_id) for run_id in ids), ids
    sessions = {run["session"]["id"] for run in runs}
    assert len(sessions) == 1 and SESSION_ID.match(next(iter(sessions))), sessions
    assert all(run_dir.parent == bench.out / next(iter(sessions)) for run_dir in bench.run_dirs())


def test_blocks_rotate_the_server_order(session_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench = completed(session_run)
    runs = sorted(records(bench), key=lambda run: run["session"]["order_index"])
    assert [(run["arm"]["id"], run["session"]["repeat"]) for run in runs] == [
        (BDS, 1),
        (DRAGONFLY, 1),
        (DRAGONFLY, 2),
        (BDS, 2),
    ]
    assert {run["session"]["order"] for run in runs} == {"rotate"}
    assert {run["machine"]["profile_sha256"] for run in runs} == {runs[0]["machine"]["profile_sha256"]}


def test_dry_run_prints_the_order_and_runs_nothing(tmp_path: Path) -> None:
    bench = FakeBench.create(tmp_path)
    result = bench.bbench(*bench.session_args(*ARMS, repeats=2), "--dry-run")
    assert result.returncode == ExitCode.OK, result.stderr
    order = [arm for line in result.stdout.splitlines() for arm in ARMS if arm in line]
    assert order == [BDS, DRAGONFLY, DRAGONFLY, BDS]
    assert bench.run_dirs() == []


def test_report_has_one_row_per_server(session_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench = completed(session_run)
    directory = session_dir(bench)
    assert (directory / "report.md").is_file(), "bbench run writes the report at the end"
    result = bench.bbench("report", directory)
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    assert (directory / "summary.json").is_file()
    report = (directory / "report.md").read_text(encoding="utf-8")
    results = report.split("## Results\n")[1].split("\n## ")[0]
    rows = [line for line in results.splitlines() if line.startswith("| `")]
    assert [row.split(" | ")[1] for row in rows] == ["2/2", "2/2"]
    assert BDS in rows[0] and DRAGONFLY in rows[1]


def test_report_refuses_mixed_machine_profiles(
    session_run: tuple[FakeBench, subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    bench = completed(session_run)
    copy = tmp_path / "session"
    shutil.copytree(session_dir(bench), copy, ignore=shutil.ignore_patterns("summary.json", "report.md", "*.csv"))
    run_json = sorted(copy.glob("*/run.json"))[0]
    record = read_json(run_json)
    record["machine"]["profile_sha256"] = "0" * 64
    run_json.write_text(json.dumps(record, indent=2), encoding="utf-8")
    result = bench.bbench("report", copy)
    assert result.returncode != ExitCode.OK, "a report never mixes machine profiles"
    assert not (copy / "report.md").exists()
