# SPDX-License-Identifier: Apache-2.0
"""Matrix reports: ``bbench report <matrix_dir>``.

``write`` aggregates the runs of a matrix directory into ``summary.json`` and renders ``report.md`` next to them,
optionally checking the publication rules. The runner calls it after every matrix; the CLI entry point is
``report_cli``.

Exit codes: 2 when the directory cannot be reported or ``--publish`` refuses; otherwise the worst run status (0 all ok,
4 a server failed, 5 a run is invalid), as for ``bbench run``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench.model import ExitCode
from bedrock_bench.report import export, markdown
from bedrock_bench.report.aggregate import (
    FeatureLookup,
    ReportError,
    default_features,
    load_runs,
    publish_problems,
    statuses,
    summarize,
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


def build(
    matrix_dir: Path,
    *,
    publish: bool = False,
    features: FeatureLookup | None = default_features,
) -> tuple[dict[str, Any], str, tuple[str, ...] | None]:
    """Summary, markdown and publication problems for a matrix directory, without writing anything."""
    matrix_dir = Path(matrix_dir)
    summary = summarize(load_runs(matrix_dir), default_name=matrix_dir.name, features=features)
    problems = tuple(publish_problems(summary)) if publish else None
    if problems is not None:
        summary["publish"] = {"problems": list(problems)}
    return summary, markdown.render(summary), problems


def write(
    matrix_dir: Path,
    *,
    publish: bool = False,
    features: FeatureLookup | None = default_features,
) -> Report:
    """Write ``summary.json``, ``report.md`` and the CSV files into ``matrix_dir``. Run directories are not touched."""
    matrix_dir = Path(matrix_dir)
    summary, text, problems = build(matrix_dir, publish=publish, features=features)
    summary_path = matrix_dir / SUMMARY_FILE
    report_path = matrix_dir / REPORT_FILE
    write_summary(summary, summary_path)
    report_path.write_text(text, encoding="utf-8", newline="\n")
    export.write(matrix_dir)
    return Report(summary, text, summary_path, report_path, problems)


def report_cli(args: argparse.Namespace) -> int:
    """``bbench report <matrix_dir> [--format md|json] [--publish]``."""
    try:
        report = write(args.matrix_dir, publish=args.publish)
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
