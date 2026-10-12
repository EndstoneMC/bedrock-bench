# SPDX-License-Identifier: Apache-2.0
"""The markdown report and ``bbench report`` end to end, on synthetic run directories."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import ANALYSIS_VERSION, cli
from bedrock_bench.config import project_root
from bedrock_bench.report import REPORT_FILE, SUMMARY_FILE, build, write
from bedrock_bench.report.markdown import (
    availability_text,
    flag_text,
    format_number,
    format_stat,
    render,
)

BASE_RUN = json.loads((project_root() / "tests" / "fixtures" / "run.ok.json").read_text(encoding="utf-8"))
WORKLOAD = {"name": "spread-100", "sha256": "8" * 64, "params": {"bots": {"n": 100}}}


def metric(value: Any, window: str = "steady", **extra: Any) -> dict[str, Any]:
    return {"value": value, "unit": "x", "window": window, "n": None, "availability": "measured", **extra}


def write_run(
    report_dir: Path,
    tag: str,
    repeat: int,
    metrics: dict[str, Any],
    *,
    bds: str | None = None,
    status: str = "ok",
    reasons: Sequence[str] = (),
    flags: Sequence[str] = (),
    profile: str | None = None,
) -> None:
    server, _, version = tag.partition("@")
    run_id = f"20261012T1200{repeat:02d}Z-{tag}-r{repeat}"
    run = {key: copy.deepcopy(BASE_RUN[key]) for key in ("harness", "machine", "placement", "bot")}
    run.update(
        run_id=run_id,
        status=status,
        status_reasons=list(reasons),
        flags=list(flags),
        official=True,
        nonstandard=[],
        arm={"server": server, "version": version, "bds": bds, "python": "3.13" if bds else None, "artifact": None},
        workload=WORKLOAD,
    )
    if profile:
        run["machine"]["profile_sha256"] = profile
    doc = {
        "schema": "bedrock-bench/metrics/1",
        "analysis_version": ANALYSIS_VERSION,
        "run_id": run_id,
        "status": status,
        "status_reasons": list(reasons),
        "flags": list(flags),
        "metrics": metrics,
    }
    run_dir = report_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def headline(tps: float, tick: float, cores: float, private: float, t90: float, **extra: Any) -> dict[str, Any]:
    return {
        "tps_window_steady": metric(tps),
        "tick_cpu_ms_per_tick_steady": metric(tick),
        "process_cores_steady": metric(cores),
        "private_mib_steady": metric(private),
        "tps_load_low5": metric(tps - 4, "load"),
        "view_t90_own_s": metric({"median": t90, "p90": t90 * 1.5, "max": t90 * 2}, "load"),
        "chunks_per_s_load": metric(150.0, "load"),
        "chunks_per_bot": metric({"min": 300, "median": 317, "max": 321}, "load"),
        **extra,
    }


NO_TICK = {
    "value": None,
    "unit": "ms",
    "window": "steady",
    "availability": "not_applicable",
    "reason": "no tick thread",
}


def session(report_dir: Path, *, pocketmine_fails: bool = True) -> None:
    """BDS, Endstone hosting it and PocketMine-MP, three repeats each; one PocketMine-MP run fails."""
    for k, (bds, endstone) in enumerate(((12.0, 19.6), (12.2, 19.5), (11.8, 19.7)), start=1):
        write_run(report_dir, "bds@1.26.52.3", k, headline(bds, 82.0 + k, 1.5, 4096.0, 200.0))
        write_run(report_dir, "endstone@0.11.14", k, headline(endstone, 44.0 + k, 1.4, 4505.6, 120.0), bds="1.26.52.3")
        failed = pocketmine_fails and k == 3
        write_run(
            report_dir,
            "pocketmine@5.44.3",
            k,
            {} if failed else headline(5.0, 0.0, 1.3, 5120.0, 400.0, tick_cpu_ms_per_tick_steady=NO_TICK),
            status="server_failed" if failed else "ok",
            reasons=["server_alive: exited with code 255"] if failed else (),
        )


@pytest.fixture
def report_dir(tmp_path: Path) -> Path:
    path = tmp_path / "session"
    session(path)
    return path


@pytest.fixture
def clean_dir(tmp_path: Path) -> Path:
    path = tmp_path / "clean"
    session(path, pocketmine_fails=False)
    return path


def table_row(markdown: str, label: str) -> list[str]:
    """Cells of the first table row whose first cell starts with ``label``."""
    for line in markdown.splitlines():
        if line.startswith(f"| {label}"):
            return [c.strip() for c in line.strip("|").split(" | ")]
    raise AssertionError(f"no row {label!r}")


def section(markdown: str, title: str) -> str:
    return markdown.split(f"## {title}\n")[1].split("\n## ")[0]


# --------------------------------------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------------------------------------


class TestRender:
    @pytest.fixture
    def markdown(self, report_dir: Path) -> str:
        return build(report_dir)[1]

    def test_sections_in_paper_order(self, markdown: str) -> None:
        titles = [line for line in markdown.splitlines() if line.startswith("#")]
        assert titles == ["# Results", "## Environment", "## Results", "## Notes"]

    def test_config_and_bot_count_on_top(self, markdown: str) -> None:
        assert markdown.startswith("# Results\n\nConfig `spread-100`, 100 bots.\n\n## Environment\n")

    def test_environment(self, markdown: str) -> None:
        env = section(markdown, "Environment")
        assert table_row(env, "CPU") == ["CPU", "Intel(R) Core(TM) i7-14700F"]
        assert table_row(env, "Server CPUs")[1] == "0-15 (16 logical)"
        assert table_row(env, "Bot CPUs")[1] == "16-27 (12 logical)"
        assert table_row(env, "Python")[1] == "3.13.5"
        assert table_row(env, "Servers")[1] == (
            "`bds@1.26.52.3`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `pocketmine@5.44.3`"
        )
        assert table_row(env, "Bots")[1] == "loadbot, protocol 2193, gophertunnel v1.62.0"

    def test_results_table(self, report_dir: Path, markdown: str) -> None:
        results = section(markdown, "Results")
        header = table_row(results, "Server")
        assert header == ["Server", "Runs ok", *(c["title"] for c in build(report_dir)[0]["columns"])]
        bds = table_row(results, "`bds@1.26.52.3`")
        assert bds[1:4] == ["3/3", "12.00 [11.80, 12.20]", "84.0 [83.0, 85.0]"]
        assert bds[4:6] == ["1.50 [1.50, 1.50]", "4096 [4096, 4096]"]
        assert bds[7:] == ["200 [200, 200]", "300 [300, 300]", "400 [400, 400]", "150 [150, 150]", "317 [317, 317]"]
        pocketmine = table_row(results, "`pocketmine@5.44.3`")
        assert pocketmine[1:4] == ["2/3", "5.00 [5.00, 5.00]", "n/a"]

    def test_notes(self, markdown: str) -> None:
        notes = [line for line in section(markdown, "Notes").splitlines() if line]
        assert (
            "- `pocketmine@5.44.3` run `20261012T120003Z-pocketmine@5.44.3-r3`: server_failed: server_alive: "
            "exited with code 255"
        ) in notes
        assert "- `pocketmine@5.44.3`: Tick-thread CPU per tick (ms): not applicable (no tick thread)" in notes
        assert all(line.startswith("- ") for line in notes)

    def test_ascii(self, markdown: str) -> None:
        markdown.encode("ascii")


class TestNotes:
    def test_flags(self) -> None:
        arm = {"server": "levilamina", "bds": "1.26.51.1"}
        assert flag_text("different_bds_build", count=3, total=3, arm=arm) == (
            "the report has no run of the BDS build this server hosts (1.26.51.1)"
        )
        assert flag_text("settle_capped", count=1, total=3, arm=arm).endswith("(in 1 of 3 runs)")
        assert flag_text("dropped", count=2, total=3, arm=arm).endswith("during steady (in 2 of 3 runs)")
        assert flag_text("something_new", count=1, total=1, arm=arm) == "flag `something_new`"

    def test_availability(self) -> None:
        assert availability_text("TPS", {"n": 3, "availability": "measured"}, 3) is None
        partial = {"n": 2, "availability": "measured", "reasons": ["sampler died"]}
        assert availability_text("TPS", partial, 3) == "TPS: unavailable in 1 of 3 ok runs (sampler died)"
        assert availability_text("TPS", {"n": 0, "availability": "unavailable"}, 0) is None

    def test_skipped_servers(self, report_dir: Path) -> None:
        skipped = ["chorus@95ca835: its player list gives the bots decode errors", "levilamina@26.51.6"]
        write(report_dir, skipped=skipped)
        notes = section(build(report_dir)[1], "Notes")
        assert "- `chorus@95ca835`: skipped, its player list gives the bots decode errors\n" in notes
        assert "- `levilamina@26.51.6`: skipped\n" in notes
        assert json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))["skipped"] == skipped
        write(report_dir)
        assert json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))["skipped"] == skipped

    def test_not_ok_without_reasons(self, tmp_path: Path) -> None:
        write_run(tmp_path, "bds@1.0", 1, {}, status="invalid")
        assert "`bds@1.0` run `20261012T120001Z-bds@1.0-r1`: invalid: no reason recorded" in build(tmp_path)[1]


class TestFormatting:
    @pytest.mark.parametrize(
        ("value", "decimals", "text"),
        [
            (17.951, 2, "17.95"),
            (4505.6, 0, "4506"),
            (47.26, None, "47.3"),
            (123.4, None, "123"),
            (0.5204, None, "0.520"),
            (0.0, None, "0"),
            (1.456, 2, "1.46"),
        ],
    )
    def test_numbers(self, value: float, decimals: int | None, text: str) -> None:
        assert format_number(value, decimals) == text

    def test_stats(self) -> None:
        assert format_stat({"median": 64.0, "min": 61.0, "max": 68.0, "n": 3}) == "64.0 [61.0, 68.0]"
        assert format_stat({"median": 64.0, "min": 64.0, "max": 64.0, "n": 1}) == "64.0"
        assert format_stat({"n": 0, "availability": "not_applicable"}) == "n/a"
        assert format_stat({"n": 0, "availability": "unavailable"}) == "-"


# --------------------------------------------------------------------------------------------------------------------
# Files and CLI
# --------------------------------------------------------------------------------------------------------------------


class TestWrite:
    def test_files_are_deterministic_and_rerenderable(self, report_dir: Path) -> None:
        report = write(report_dir, publish=True)
        summary_bytes = (report_dir / SUMMARY_FILE).read_bytes()
        assert report.report_path == report_dir / REPORT_FILE
        write(report_dir, publish=True)
        assert (report_dir / SUMMARY_FILE).read_bytes() == summary_bytes
        summary = json.loads(summary_bytes)
        assert render(summary) == (report_dir / REPORT_FILE).read_text(encoding="utf-8")
        assert str(report_dir.parent) not in summary_bytes.decode("utf-8")
        assert summary["schema"] == "bedrock-bench/summary/2"

    def test_publish_refusal(self, report_dir: Path) -> None:
        report = write(report_dir, publish=True)
        assert report.problems == ("pocketmine@5.44.3: 2 ok runs, publication needs at least 3",)
        assert report.exit_code == 2
        assert "## Publication check\n\n- Refused: pocketmine@5.44.3: 2 ok runs" in report.markdown

    def test_publish_pass(self, clean_dir: Path) -> None:
        report = write(clean_dir, publish=True)
        assert report.problems == () and report.exit_code == 0
        assert "## Publication check\n\nPassed." in report.markdown


class TestCli:
    def test_markdown_to_stdout(self, report_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", str(report_dir)]) == 4
        out, err = capsys.readouterr()
        assert out.startswith("# Results\n")
        assert "wrote" in err and (report_dir / SUMMARY_FILE).is_file() and (report_dir / REPORT_FILE).is_file()

    def test_json_to_stdout(self, clean_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", str(clean_dir), "--format", "json"]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["status_counts"]["ok"] == 9 and len(summary["servers"]) == 3

    def test_publish_refused(self, report_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["report", str(report_dir), "--publish"]) == 2
        assert "not publishable: pocketmine@5.44.3" in capsys.readouterr().err

    def test_mixed_machine_profiles(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        write_run(tmp_path, "bds@1.26.52.3", 1, headline(12.0, 82, 1.0, 4000, 200))
        write_run(tmp_path, "bds@1.26.52.3", 2, headline(12.0, 82, 1.0, 4000, 200), profile="1" * 64)
        assert cli.main(["report", str(tmp_path)]) == 2
        assert "different machine profiles" in capsys.readouterr().err
        assert not (tmp_path / SUMMARY_FILE).exists()
