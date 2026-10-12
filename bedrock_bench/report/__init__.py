# SPDX-License-Identifier: Apache-2.0
"""Reports: ``bbench report <dir>``.

``write`` aggregates the run directories below ``<dir>`` into ``summary.json``, renders ``report.md`` and writes the
CSV files next to them, optionally checking the publication rules. The runner calls it at the end of ``bbench run``
with the servers it skipped, which are kept in ``session.json``; the CLI entry point is ``report_cli``.

Exit codes: 2 when the directory cannot be reported or ``--publish`` refuses; otherwise the worst run status (0 all ok,
4 a server failed, 5 a run is invalid), as for ``bbench run``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench.model import ExitCode
from bedrock_bench.report import export, markdown
from bedrock_bench.report.aggregate import (
    ReportError,
    RunRecord,
    load_runs,
    load_skipped,
    publish_problems,
    statuses,
    summarize,
    write_skipped,
    write_summary,
)

SUMMARY_FILE = "summary.json"
REPORT_FILE = "report.md"


@dataclass(frozen=True)
class Report:
    summary: dict[str, Any]
    markdown: str
    summary_path: Path
    report_path: Path
    problems: tuple[str, ...] | None  # None unless publication rules were checked

    @property
    def exit_code(self) -> ExitCode:
        if self.problems:
            return ExitCode.USAGE
        return ExitCode.for_statuses(statuses(self.summary))


def build(report_dir: Path, *, publish: bool = False) -> tuple[dict[str, Any], str, tuple[str, ...] | None]:
    """Summary, markdown and publication problems for a directory of runs, without writing anything."""
    return _build(load_runs(Path(report_dir)), load_skipped(Path(report_dir)), publish)


def _build(
    runs: list[RunRecord], skipped: Sequence[str], publish: bool
) -> tuple[dict[str, Any], str, tuple[str, ...] | None]:
    summary = summarize(runs, skipped)
    problems = tuple(publish_problems(summary)) if publish else None
    if problems is not None:
        summary["publish"] = {"problems": list(problems)}
    return summary, markdown.render(summary), problems


def write(report_dir: Path, *, publish: bool = False, skipped: Sequence[str] | None = None) -> Report:
    """Write ``summary.json``, ``report.md`` and the CSV files into ``report_dir``. Run directories are not touched.

    ``skipped`` replaces the servers recorded in ``session.json``; None keeps them.
    """
    report_dir = Path(report_dir)
    runs = load_runs(report_dir)
    if skipped is not None:
        write_skipped(report_dir, skipped)
    summary, text, problems = _build(runs, load_skipped(report_dir), publish)
    summary_path = report_dir / SUMMARY_FILE
    report_path = report_dir / REPORT_FILE
    write_summary(summary, summary_path)
    report_path.write_text(text, encoding="utf-8", newline="\n")
    export.write(report_dir, summary, runs)
    return Report(summary, text, summary_path, report_path, problems)


def report_cli(args: argparse.Namespace) -> int:
    """``bbench report <dir> [--format md|json] [--publish]``."""
    try:
        report = write(args.dir, publish=args.publish)
    except ReportError as exc:
        print(f"bbench report: {exc}", file=sys.stderr)
        return ExitCode.USAGE
    if args.format == "json":
        _emit(json.dumps(report.summary, indent=2, sort_keys=True) + "\n")
    else:
        _emit(report.markdown)
    print(f"wrote {report.summary_path.as_posix()} and {report.report_path.as_posix()}", file=sys.stderr)
    for problem in report.problems or ():
        print(f"bbench report: not publishable: {problem}", file=sys.stderr)
    return report.exit_code


def _emit(text: str) -> None:
    """Write to stdout, replacing characters the console encoding cannot show."""
    stream = sys.stdout
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = stream.encoding or "ascii"
        stream.write(text.encode(encoding, errors="replace").decode(encoding))
    stream.flush()


__all__ = ["REPORT_FILE", "SUMMARY_FILE", "Report", "ReportError", "build", "report_cli", "write"]
