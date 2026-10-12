# SPDX-License-Identifier: Apache-2.0
"""Run directories, run.json assembly, the raw-file loader and ``bbench validate``."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import RUN_SCHEMA, results
from bedrock_bench.clock import Origin
from bedrock_bench.config import project_root
from bedrock_bench.model import (
    Arm,
    ArtifactRef,
    ConfigFile,
    ExitCode,
    Gates,
    Install,
    Layout,
    Machine,
    Override,
    OverrideCategory,
    Placement,
    WorldInfo,
)
from bedrock_bench.sampling.series import MACHINE_COLUMNS, JsonlWriter, SeriesWriter
from bedrock_bench.workload import WORKLOAD
from fixtures.runs import synthetic

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z][a-z0-9_]*@[0-9A-Za-z._+-]+-r[0-9]+-[0-9a-f]{4}$")
UNIT_WORKLOAD = WORKLOAD
ARM = Arm(id="bds@1.26.52.3", server="bds", version="1.26.52.3")
PLACEMENT = Placement(server_cpus=(0, 1), bot_cpus=(2, 3), harness_cpus=(2, 3), priority="normal", kind="physical")
MACHINE = Machine(
    server_cpus="0-1",
    bot_cpus="2-3",
    harness_cpus="bot",
    priority="normal",
    port=19672,
    jvm_heap="8G",
    gates=Gates(),
    profile_sha256="2" * 64,
    label="unit",
)
INSTALL = Install(
    root=Path("install"), tree_sha256="1" * 64, artifacts=(ArtifactRef("bds.zip", None, "0" * 64, 1, "local"),)
)


def record(**changes: Any) -> dict[str, Any]:
    base = results.new_record(
        run_id=results.make_run_id(ARM.id, 1, 1_791_813_480_000, "abcd"),
        arm=ARM,
        workload=UNIT_WORKLOAD,
        session={"id": "20261011T120000Z-a1b2", "repeat": 1, "order_index": None, "order": None},
        harness={"version": "0.1.0", "git": None, "dirty": None, "python": "3.13", "backend": "native-linux"},
        machine=results.machine_record(
            MACHINE, {"fingerprint_id": "f" * 16, "os": "Linux", "cpu": "test", "logical": 4}, {}, None
        ),
        placement=PLACEMENT,
        install=INSTALL,
        bot={"module": "loadbot", "protocol": 2193, "argv": ["loadbot"]},
        origin=Origin(1_000, 1_791_813_480_000),
        idle_foreign_cores=-0.01,
        official=True,
        nonstandard=[],
    )
    base.update(changes)
    return base


def copy_fixture(name: str, target: Path) -> Path:
    shutil.copytree(synthetic.HERE / name, target)
    return target


# --------------------------------------------------------------------------------------------------------------------
# Run directories
# --------------------------------------------------------------------------------------------------------------------


class TestRunDir:
    def test_run_id_format(self) -> None:
        run_id = results.make_run_id("endstone@0.11.14+glibc-alloc", 2, 1_791_813_480_000, "3fa2")
        assert run_id == "20261012T135800Z-endstone@0.11.14+glibc-alloc-r2-3fa2"
        assert RUN_ID_PATTERN.match(run_id)
        assert RUN_ID_PATTERN.match(results.make_run_id("bds@1.26.52.3", 1, 0))

    def test_create_never_reuses_a_directory(self, tmp_path: Path) -> None:
        dirs = {results.RunDir.create(tmp_path, ARM.id, 1).path for _ in range(20)}
        assert len(dirs) == 20
        assert all((d / "series").is_dir() for d in dirs)
        assert all(RUN_ID_PATTERN.match(d.name) for d in dirs)

    def test_partial_then_final(self, tmp_path: Path) -> None:
        run_dir = results.RunDir.create(tmp_path, ARM.id, 1)
        run_dir.write_partial({"partial": True})
        assert run_dir.read_record() == {"partial": True}
        run_dir.write_run({"final": 1})
        run_dir.remove_partial()
        assert run_dir.read_record() == {"final": 1}
        assert not (run_dir.path / results.PARTIAL_FILE).exists()
        assert not list(run_dir.path.glob("*.tmp"))

    def test_open_needs_a_record(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            results.RunDir.open(tmp_path)

    def test_raw_files_and_hashes_leave_out_the_records(self, tmp_path: Path) -> None:
        run_dir = results.RunDir.create(tmp_path, ARM.id, 1)
        for rel in ("run.json", "run.partial.json", "metrics.json", "server.log.gz", "series/probe.csv", "crash/x"):
            path = run_dir.path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")
        assert run_dir.raw_files() == ["crash/x", "series/probe.csv", "server.log.gz"]
        hashes = run_dir.file_hashes()
        assert hashes["server.log.gz"] == {
            "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
            "bytes": 4,
        }

    def test_json_lines_from_many_threads(self, tmp_path: Path) -> None:
        writer = results.JsonLines(tmp_path / "bots.jsonl.gz")
        threads = [
            threading.Thread(target=lambda k=k: [writer.write({"ev": "x", "k": k, "j": j}) for j in range(200)])
            for k in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        writer.close()
        writer.write({"ev": "late"})  # ignored after close
        with gzip.open(tmp_path / "bots.jsonl.gz", "rt", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh]
        assert len(lines) == 800 and writer.lines == 800


# --------------------------------------------------------------------------------------------------------------------
# run.json assembly
# --------------------------------------------------------------------------------------------------------------------


class TestRecord:
    def test_new_record_is_a_valid_partial_run(self, schema_validator: Callable[[str], Any]) -> None:
        doc = record()
        assert doc["machine"]["gates"] == {
            "idle_check_s": 15.0,
            "foreign_cores_mean": 0.25,
            "foreign_cores_peak_10s": 1.5,
            "foreign_server_set_cores_mean": 0.25,
            "bot_cpu_frac_max": 0.85,
        }
        machine = {k: v for k, v in doc["machine"].items() if k != "gates"}  # run/1 does not list machine.gates yet
        schema_validator("run").validate({**doc, "machine": machine})
        assert doc["partial"] is True and doc["status"] is None
        assert doc["marks"] == dict.fromkeys(doc["marks"])
        assert doc["interference"]["idle_foreign_cores"] == 0.0  # floored, never negative

    def test_machine_record_keeps_only_known_keys(self) -> None:
        machine = results.machine_record(
            MACHINE, {"fingerprint_id": "abc", "os": "Linux", "hostname": "box"}, {"cpu": "x", "logical": 8}, 2.1e9
        )
        assert "hostname" not in machine
        assert machine["label"] == "unit"
        assert machine["tsc_hz_calibrated"] == 2_100_000_000
        assert machine["gates"]["foreign_cores_mean"] == 0.25
        assert machine["profile_sha256"] == "2" * 64

    def test_arm_records_a_local_artifact_by_name(self, tmp_path: Path) -> None:
        arm = Arm(id="bds@1.0", server="bds", version="1.0", artifact=tmp_path / "builds" / "server.zip")
        assert results.arm_record(arm)["artifact"] == "server.zip"

    def test_apply_layout(self, tmp_path: Path) -> None:
        doc = record()
        layout = Layout(
            server_dir=tmp_path,
            overrides=(Override(OverrideCategory.HARNESS, "server.properties", "online-mode", "true", "false", "x"),),
            config_files=(ConfigFile("server.properties", "a" * 64),),
            world=WorldInfo(fresh=True, generator="vanilla", path=tmp_path / "worlds" / "bench"),
            extra={"exe_sha256": "b" * 64},
        )
        results.apply_layout(doc, layout)
        assert doc["overrides"][0]["category"] == "harness"
        assert doc["config_files"] == [{"path": "server.properties", "written_sha256": "a" * 64, "after_sha256": None}]
        assert doc["world"] == {"fresh": True, "generator": "vanilla"}
        assert doc["install"]["exe_sha256_before"] == "b" * 64


class TestInterference:
    def test_windows(self, tmp_path: Path) -> None:
        run_dir = results.RunDir.create(tmp_path, ARM.id, 1)
        origin = Origin(10**9, 0)
        with SeriesWriter(run_dir.series("machine.csv.gz"), (*MACHINE_COLUMNS, "cpu0")) as writer:
            for second in range(30):
                foreign = 2.0 if 10 <= second < 15 else -0.01
                writer.write([origin.mono_ns + second * 10**9, foreign, 0.0, 1.0, 1.0, 0.25, 0.5])
        top = JsonlWriter(run_dir.series("foreign_top.jsonl"))
        top.write(
            {"t_ns": origin.mono_ns + 12 * 10**9, "interval_s": 10.0, "top": [{"pid": 7, "name": "x", "cores": 2}]}
        )
        top.close()
        windows = results.interference_record(run_dir, origin, {"quiet": (20.0, 29.0), "busy": (10.0, 14.0)})
        assert windows["quiet"] == {
            "foreign_cores": 0.0,
            "peak_10s": 0.0,
            "server_set": 0.0,
            "harness_cores": 0.25,
            "top": [],
        }
        assert windows["busy"]["foreign_cores"] == 2.0
        assert windows["busy"]["top"] == [{"name": "x", "cores": 2.0}]

    def test_no_machine_series(self, tmp_path: Path) -> None:
        run_dir = results.RunDir.create(tmp_path, ARM.id, 1)
        assert results.interference_record(run_dir, Origin(0, 0), {"steady": (0.0, 1.0)}) == {}


# --------------------------------------------------------------------------------------------------------------------
# Loading and finishing runs
# --------------------------------------------------------------------------------------------------------------------


class TestLoadRun:
    @pytest.mark.parametrize("name", synthetic.NAMES)
    def test_fixture_directories_load_as_built(self, name: str) -> None:
        loaded, built = results.load_run(synthetic.HERE / name), synthetic.build(name)
        assert loaded.probe == built.probe
        assert loaded.tick == built.tick
        assert loaded.threads == built.threads
        assert loaded.process == built.process
        assert loaded.machine == built.machine
        assert loaded.bots == built.bots
        assert loaded.log == built.log
        assert loaded.commands == built.commands

    def test_missing_raw_files_load_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({"schema": RUN_SCHEMA, "arm": {"server": "nobody"}}))
        data = results.load_run(tmp_path)
        assert (data.probe, data.bots, data.log, data.process) == ([], [], [], [])

    def test_partial_runs_load(self, tmp_path: Path) -> None:
        (tmp_path / "run.partial.json").write_text(json.dumps({"schema": RUN_SCHEMA, "partial": True}))
        assert results.load_run(tmp_path).run["partial"] is True

    def test_unknown_schema_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({"schema": "bedrock-bench/run/99"}))
        with pytest.raises(ValueError, match="unsupported run schema"):
            results.load_run(tmp_path)


class TestFinalize:
    def finalize(self, tmp_path: Path, name: str, **kwargs: Any) -> tuple[results.Outcome, Path]:
        run_path = copy_fixture(name, tmp_path / name)
        (run_path / "metrics.json").unlink()
        doc = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        (run_path / "run.json").unlink()
        doc["partial"] = True
        results.RunDir(run_path).write_partial(doc)
        options: dict[str, Any] = {"reasons": [], "flags": []}
        options.update(kwargs)
        return results.finalize(results.RunDir(run_path), doc, **options), run_path

    @pytest.mark.parametrize("name", synthetic.NAMES)
    def test_metrics_match_a_recompute(self, tmp_path: Path, name: str) -> None:
        outcome, run_path = self.finalize(tmp_path, name)
        stored = (synthetic.HERE / name / "metrics.json").read_text(encoding="utf-8")
        assert (run_path / "metrics.json").read_text(encoding="utf-8") == stored
        final = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        expected = json.loads(stored)
        assert final["status"] == expected["status"] == outcome.status.value
        assert final["status_reasons"] == expected["status_reasons"]
        assert final["exit_code"] == outcome.exit_code
        assert "partial" not in final
        assert not (run_path / "run.partial.json").exists()
        assert final["files"] == results.RunDir(run_path).file_hashes()

    def test_recorded_reasons_survive(self, tmp_path: Path) -> None:
        outcome, run_path = self.finalize(
            tmp_path, "synthetic-steady", reasons=["harness_ok: sampler x failed", "config_readback: a changed"]
        )
        assert outcome.exit_code == ExitCode.INVALID
        metrics = json.loads((run_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["status_reasons"] == ["harness_ok: sampler x failed", "config_readback: a changed"]

    def test_paths_are_scrubbed(self, tmp_path: Path) -> None:
        secret = tmp_path / "work" / "server"
        outcome, run_path = self.finalize(
            tmp_path, "synthetic-steady", reasons=[f"harness_ok: cannot read {secret / 'x.txt'}"]
        )

        def strings(node: Any) -> list[str]:
            if isinstance(node, str):
                return [node]
            if isinstance(node, dict):
                return [s for key, value in node.items() for s in (key, *strings(value))]
            if isinstance(node, list):
                return [s for item in node for s in strings(item)]
            return []

        secrets = (str(tmp_path), str(tmp_path).replace("\\", "/"))
        for name in ("run.json", "metrics.json"):
            doc = json.loads((run_path / name).read_text(encoding="utf-8"))
            leaked = [s for s in strings(doc) if any(secret in s for secret in secrets)]
            assert not leaked, (name, leaked)
        assert any("x.txt" in reason for reason in outcome.reasons)

    def test_flags_are_recorded(self, tmp_path: Path) -> None:
        outcome, run_path = self.finalize(tmp_path, "synthetic-steady", flags=["unpublished_artifact"])
        assert "unpublished_artifact" in outcome.flags
        assert "unpublished_artifact" in json.loads((run_path / "run.json").read_text(encoding="utf-8"))["flags"]


# --------------------------------------------------------------------------------------------------------------------
# bbench validate
# --------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def validate(capsys: pytest.CaptureFixture[str]) -> Callable[..., tuple[int, str]]:
    pytest.importorskip("jsonschema")

    def run(*paths: Path) -> tuple[int, str]:
        code = results.validate_cli(argparse.Namespace(paths=list(paths)))
        out = capsys.readouterr()
        return code, out.out + out.err

    return run


class TestValidate:
    def test_fixture_files(self, validate: Callable[..., tuple[int, str]]) -> None:
        root = project_root()
        fixtures = root / "tests" / "fixtures"
        paths = (fixtures / "run.ok.json", fixtures / "metrics.json")
        code, output = validate(*paths, root / "bots" / "core" / "testdata" / "golden.jsonl")
        assert code == ExitCode.OK, output

    def test_fixture_run_directories(self, validate: Callable[..., tuple[int, str]]) -> None:
        code, output = validate(synthetic.HERE)
        assert code == ExitCode.OK, output

    def test_a_changed_raw_file_is_reported(self, tmp_path: Path, validate: Callable[..., tuple[int, str]]) -> None:
        run_path = copy_fixture("synthetic-steady", tmp_path / "run")
        (run_path / "series" / "probe.csv").write_text("send_ns,rx_ns,tick\n", encoding="utf-8")
        (run_path / "stray.txt").write_text("x", encoding="utf-8")
        code, output = validate(run_path)
        assert code == ExitCode.INVALID
        assert "series/probe.csv does not match its hash" in output
        assert "stray.txt is not listed" in output

    def test_a_schema_violation_is_reported(self, tmp_path: Path, validate: Callable[..., tuple[int, str]]) -> None:
        run_path = copy_fixture("synthetic-steady", tmp_path / "run")
        doc = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        doc["status"] = "great"
        (run_path / "run.json").write_text(json.dumps(doc), encoding="utf-8")
        code, output = validate(run_path / "run.json")
        assert code == ExitCode.INVALID
        assert "status" in output

    def test_unknown_documents_and_paths(self, tmp_path: Path, validate: Callable[..., tuple[int, str]]) -> None:
        other = tmp_path / "summary.json"
        other.write_text(json.dumps({"schema": "bedrock-bench/summary/1"}), encoding="utf-8")
        code, output = validate(other)
        assert code == ExitCode.INVALID and "no schema" in output
        code, output = validate(tmp_path / "missing")
        assert code == ExitCode.USAGE
