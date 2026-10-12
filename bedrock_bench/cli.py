# SPDX-License-Identifier: Apache-2.0
"""The ``bbench`` command line.

Each subcommand is bound to a fixed entry point ``"<module>:<function>"`` inside this package and imported only when
it runs, so ``bbench --help`` stays fast and a missing optional dependency only affects its own command. Entry points
take the parsed :class:`argparse.Namespace` and return an exit code (``None`` means 0). The argument ``dest`` names
below are part of the contract with the modules that implement the commands.

Exit codes are :class:`bedrock_bench.model.ExitCode`: 2 for usage and config errors, 130 when interrupted; everything
else comes from the entry point.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from bedrock_bench import __version__
from bedrock_bench.model import ExitCode

PACKAGE = "bedrock_bench"

EntryPoint = Callable[[argparse.Namespace], "int | None"]


@dataclass(frozen=True)
class Command:
    name: str
    entry: str  # "<module>:<function>", relative to the package
    help: str
    configure: Callable[[argparse.ArgumentParser], None]


# --------------------------------------------------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------------------------------------------------


def _order(value: str) -> str:
    from bedrock_bench.config import validate_order

    try:
        return validate_order(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_machine(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--machine",
        type=Path,
        metavar="FILE",
        help="machine profile TOML (default: built-in defaults, CPU sets auto)",
    )


def _configure_doctor(p: argparse.ArgumentParser) -> None:
    _add_machine(p)
    p.add_argument("--json", action="store_true", help="print the report as JSON")


def _configure_fetch(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "targets",
        nargs="+",
        metavar="TARGET",
        help="a matrix (a name in configs/matrices/ or a path), or server@version",
    )
    p.add_argument(
        "--accept-eula",
        action="store_true",
        help="accept the Minecraft EULA for BDS downloads (or set BBENCH_ACCEPT_MINECRAFT_EULA=1)",
    )
    p.add_argument("--artifact-dir", type=Path, metavar="DIR", help="take artifacts from DIR instead of downloading")


def _configure_bots(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=("build", "fetch"), help="build from source, or download a release binary")
    p.add_argument("--module", choices=("loadbot", "legacy1001"), help="bot module (default: loadbot)")
    p.add_argument(
        "--target",
        dest="targets",
        action="append",
        metavar="OS/ARCH",
        help="GOOS/GOARCH to build for, repeatable (default: this host)",
    )


def _configure_prepare_world(p: argparse.ArgumentParser) -> None:
    p.add_argument("server", metavar="SERVER@VERSION")
    p.add_argument(
        "--scenario", required=True, metavar="S", help="template scenario: a name in configs/scenarios/ or a path"
    )
    _add_machine(p)
    p.add_argument("--variant", metavar="V")


def _configure_smoke(p: argparse.ArgumentParser) -> None:
    p.add_argument("server", metavar="SERVER@VERSION")
    _add_machine(p)
    p.add_argument("--variant", metavar="V")
    p.add_argument("--artifact", type=Path, metavar="PATH", help="local artifact instead of the pinned download")
    p.add_argument("--out", type=Path, metavar="DIR", help="results root (default: runs/)")


def _configure_run(p: argparse.ArgumentParser) -> None:
    p.add_argument("matrix", metavar="MATRIX", help="a name in configs/matrices/ or a path")
    _add_machine(p)
    p.add_argument("--repeats", type=int, metavar="N", help="override the matrix repeat count")
    p.add_argument("--order", type=_order, metavar="ORDER", help="rotate, sequential or seeded:N")
    p.add_argument("--only", action="append", metavar="ARM", help="run only this arm id, repeatable")
    p.add_argument("--out", type=Path, metavar="DIR", help="results root (default: runs/)")
    p.add_argument("--dry-run", action="store_true", help="print the run order and exit")


def _configure_run_one(p: argparse.ArgumentParser) -> None:
    p.add_argument("--server", metavar="S@V", help="server@version to run (or use --matrix and --arm)")
    p.add_argument("--matrix", metavar="MATRIX", help="matrix that defines the arm: a name or a path")
    p.add_argument("--arm", metavar="ID", help="arm id inside --matrix")
    p.add_argument("--scenario", required=True, metavar="S", help="a name in configs/scenarios/ or a path")
    _add_machine(p)
    p.add_argument("--variant", metavar="V")
    p.add_argument("--repeat", type=int, default=1, metavar="K", help="repeat index (default: 1)")
    p.add_argument("--order-index", type=int, metavar="N", help="position in the matrix run order")
    p.add_argument("--artifact", type=Path, metavar="PATH", help="local artifact instead of the pinned download")
    p.add_argument("--out", type=Path, metavar="DIR", help="results root (default: runs/)")


def _configure_analyze(p: argparse.ArgumentParser) -> None:
    p.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR")


def _configure_report(p: argparse.ArgumentParser) -> None:
    p.add_argument("matrix_dir", type=Path, metavar="MATRIX_DIR")
    p.add_argument("--format", choices=("md", "json"), default="md")
    p.add_argument("--claims", type=Path, metavar="FILE", help="claims TOML to check")
    p.add_argument(
        "--publish",
        action="store_true",
        help="enforce publication rules: n_ok >= 3, official runs only, no client-side generation across servers",
    )


def _configure_validate(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="run directory, run.json, metrics.json, ...")


def _configure_docs(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", choices=("metrics",), help="metrics: regenerate docs/metrics.md")
    p.add_argument("--check", action="store_true", help="exit nonzero if the file is out of date instead of writing")


COMMANDS: tuple[Command, ...] = (
    Command(
        "doctor", "host:doctor", "check topology, power, idle load, toolchains and the machine lock", _configure_doctor
    ),
    Command("fetch", "fetch:fetch_cli", "download and verify server artifacts", _configure_fetch),
    Command("bots", "bots:build_cli", "build or download the load bot", _configure_bots),
    Command(
        "prepare-world",
        "pregen:prepare_world",
        "build and validate a pre-generated world template",
        _configure_prepare_world,
    ),
    Command("smoke", "runner:smoke", "short strict run that checks an adapter end to end", _configure_smoke),
    Command("run", "runner:run_matrix", "run a matrix", _configure_run),
    Command("run-one", "runner:run_one", "run one arm once", _configure_run_one),
    Command("analyze", "metrics:analyze_cli", "recompute metrics.json and status from raw files", _configure_analyze),
    Command(
        "report", "report:report_cli", "aggregate a matrix directory into summary.json and report.md", _configure_report
    ),
    Command("validate", "results:validate_cli", "check result files against the schemas", _configure_validate),
    Command("docs", "metrics.registry:render_docs", "regenerate generated documentation", _configure_docs),
)


# --------------------------------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------------------------------


def resolve_entry(entry: str) -> EntryPoint:
    """Import ``"<module>:<function>"`` from this package."""
    module_name, _, func_name = entry.partition(":")
    func: EntryPoint = getattr(importlib.import_module(f"{PACKAGE}.{module_name}"), func_name)
    return func


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bbench",
        description="Reproducible, fair benchmarks for Minecraft Bedrock servers.",
        epilog="Run 'bbench <command> --help' for the options of one command.",
    )
    parser.add_argument("--version", action="version", version=f"bbench {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more log output (-vv for debug)")
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="commands")
    for command in COMMANDS:
        child = sub.add_parser(command.name, help=command.help, description=command.help)
        command.configure(child)
        child.set_defaults(entry=command.entry)
    return parser


def _check_run_one(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    by_matrix = args.matrix is not None or args.arm is not None
    if args.server and by_matrix:
        parser.error("run-one: use either --server or --matrix with --arm, not both")
    if not args.server and not (args.matrix and args.arm):
        parser.error("run-one: needs --server S@V, or --matrix and --arm")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return ExitCode.USAGE
    if args.command == "run-one":
        _check_run_one(parser, args)

    logging.basicConfig(
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from bedrock_bench.config import ConfigError

    try:
        func = resolve_entry(args.entry)
        result = func(args)
    except ConfigError as exc:
        print(f"bbench {args.command}: {exc}", file=sys.stderr)
        return ExitCode.USAGE
    except KeyboardInterrupt:
        print(f"bbench {args.command}: interrupted", file=sys.stderr)
        return ExitCode.INTERRUPTED
    return int(result) if result is not None else ExitCode.OK


if __name__ == "__main__":
    raise SystemExit(main())
