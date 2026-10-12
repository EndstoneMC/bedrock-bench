# SPDX-License-Identifier: Apache-2.0
"""``bbench run`` over a matrix: rotate order, one subprocess and directory per run, refusal to mix machine
profiles in one matrix directory, and a report that groups arms by declared fidelity (rule R10, ADR 0002).

The matrix has a full-fidelity arm (``fake_bds``, every exercised feature vanilla) and a reduced one
(``fake_dragonfly``, physics and movement validation absent), each run twice.
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
from fakes.bench import FakeBench, needs, read_json

pytestmark = [pytest.mark.integration, needs("runner:run_matrix")]

MATRIX = "order"
FULL, REDUCED = "fake_bds@1.0.0", "fake_dragonfly@1.0.0"
ARMS = [FULL, REDUCED]
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-fake_(bds|dragonfly)@1\.0\.0-fake-quick-r[12]-[0-9a-f]{4}$")


@pytest.fixture(scope="module")
def matrix_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[FakeBench, subprocess.CompletedProcess[str]]:
    bench = FakeBench.create(tmp_path_factory.mktemp("bench"))
    matrix = bench.matrix(MATRIX, ARMS, repeats=2, order="rotate")
    machine = bench.root / "machines" / "fake.toml"
    result = bench.bbench("run", matrix, "--machine", machine, "--out", bench.out, timeout=900)
    return bench, result


def records(bench: FakeBench) -> list[dict[str, Any]]:
    return [read_json(run_dir / "run.json") for run_dir in bench.run_dirs()]


def completed(matrix_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> FakeBench:
    """The bench of a matrix run that finished every run; tests that build on it need that first."""
    bench, result = matrix_run
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    assert len(bench.run_dirs()) == 4
    return bench


def test_every_arm_and_repeat_runs_once(matrix_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench, result = matrix_run
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    runs = records(bench)
    assert len(runs) == 4
    assert all(run["status"] == Status.OK for run in runs), [run["status_reasons"] for run in runs]
    assert sorted((run["arm"]["id"], run["matrix"]["repeat"]) for run in runs) == [
        (FULL, 1),
        (FULL, 2),
        (REDUCED, 1),
        (REDUCED, 2),
    ]
    ids = [run["run_id"] for run in runs]
    assert len(set(ids)) == 4
    assert all(RUN_ID.match(run_id) for run_id in ids), ids
    assert all(run_dir.parent == bench.out / MATRIX for run_dir in bench.run_dirs())


def test_blocks_rotate_the_arm_order(matrix_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench = completed(matrix_run)
    runs = sorted(records(bench), key=lambda run: run["matrix"]["order_index"])
    assert [(run["arm"]["id"], run["matrix"]["repeat"]) for run in runs] == [
        (FULL, 1),
        (REDUCED, 1),
        (REDUCED, 2),
        (FULL, 2),
    ]
    assert {run["matrix"]["order"] for run in runs} == {"rotate"}
    assert {run["machine"]["profile_sha256"] for run in runs} == {runs[0]["machine"]["profile_sha256"]}


def test_dry_run_prints_the_order_and_runs_nothing(tmp_path: Path) -> None:
    bench = FakeBench.create(tmp_path)
    matrix = bench.matrix(MATRIX, ARMS, repeats=2, order="rotate")
    machine = bench.root / "machines" / "fake.toml"
    result = bench.bbench("run", matrix, "--machine", machine, "--out", bench.out, "--dry-run")
    assert result.returncode == ExitCode.OK, result.stderr
    order = [arm for line in result.stdout.splitlines() for arm in ARMS if arm in line]
    assert order == [FULL, REDUCED, REDUCED, FULL]
    assert bench.run_dirs() == []


@needs("report:report_cli")
def test_report_groups_arms_by_fidelity(matrix_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench = completed(matrix_run)
    matrix_dir = bench.out / MATRIX
    result = bench.bbench("report", matrix_dir)
    assert result.returncode == ExitCode.OK, result.stdout + result.stderr
    assert (matrix_dir / "summary.json").is_file()
    report = (matrix_dir / "report.md").read_text(encoding="utf-8")
    full, reduced = report.find("Full vanilla fidelity"), report.find("Reduced fidelity")
    assert full >= 0 and reduced >= 0, "the report has both fidelity sections"

    def section(start: int) -> str:
        return report[start : min(end for end in (full, reduced, len(report)) if end > start)]

    full_section, reduced_section = section(full), section(reduced)
    assert FULL in full_section and REDUCED not in full_section
    assert REDUCED in reduced_section and FULL not in reduced_section
    assert "does less" in reduced_section.lower()


@needs("report:report_cli")
def test_report_refuses_mixed_machine_profiles(
    matrix_run: tuple[FakeBench, subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    bench = completed(matrix_run)
    copy = tmp_path / MATRIX
    shutil.copytree(bench.out / MATRIX, copy, ignore=shutil.ignore_patterns("summary.json", "report.md"))
    run_json = sorted(copy.glob("*/run.json"))[0]
    record = read_json(run_json)
    record["machine"]["profile_sha256"] = "0" * 64
    run_json.write_text(json.dumps(record, indent=2), encoding="utf-8")
    result = bench.bbench("report", copy)
    assert result.returncode != ExitCode.OK, "a report never mixes machine profiles"
    assert not (copy / "report.md").exists()


def test_runner_refuses_mixed_placement(matrix_run: tuple[FakeBench, subprocess.CompletedProcess[str]]) -> None:
    bench = completed(matrix_run)
    if len(bench.server_cpus + bench.bot_cpus) < 2:
        pytest.skip("needs two CPUs to swap the server and bot sets")
    other = bench.machine("swapped", server_cpus=bench.bot_cpus, bot_cpus=bench.server_cpus)
    before = bench.run_dirs()
    matrix = bench.root / "matrices" / f"{MATRIX}.toml"
    result = bench.bbench("run", matrix, "--machine", other, "--out", bench.out, "--repeats", "3")
    refused = (ExitCode.USAGE, ExitCode.ENV_GATE)
    assert result.returncode in refused, "adding runs on another placement to a matrix directory is refused"
    assert bench.run_dirs() == before
