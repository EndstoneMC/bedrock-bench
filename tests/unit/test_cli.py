# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from bedrock_bench import __version__, cli
from bedrock_bench.config import ConfigError
from bedrock_bench.model import ExitCode

#: Command -> entry point. Changing one is a contract change.
ENTRY_POINTS = {
    "doctor": "host:doctor",
    "fetch": "fetch:fetch_cli",
    "bots": "bots:build_cli",
    "smoke": "runner:smoke",
    "run": "runner:run_matrix",
    "run-one": "runner:run_one",
    "analyze": "metrics:analyze_cli",
    "report": "report:report_cli",
    "validate": "results:validate_cli",
    "docs": "metrics.registry:render_docs",
}


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


def test_entry_points_match_spec() -> None:
    assert {c.name: c.entry for c in cli.COMMANDS} == ENTRY_POINTS


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    for name in ENTRY_POINTS:
        assert name in out


@pytest.mark.parametrize("name", list(ENTRY_POINTS))
def test_subcommand_help(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main([name, "--help"])
    assert info.value.code == 0
    assert f"bbench {name}" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"bbench {__version__}"


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == ExitCode.USAGE
    assert "usage: bbench" in capsys.readouterr().err


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bedrock_bench", "--help"], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0
    assert "run-one" in result.stdout


# --------------------------------------------------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------------------------------------------------


class TestArguments:
    def test_run(self) -> None:
        args = parse(
            "run",
            "endstone-0.11.14",
            "--repeats",
            "5",
            "--order",
            "seeded:7",
            "--only",
            "a",
            "--only",
            "b",
            "--dry-run",
            "--machine",
            "box.toml",
            "--out",
            "out",
        )
        assert (args.matrix, args.repeats, args.order, args.only) == ("endstone-0.11.14", 5, "seeded:7", ["a", "b"])
        assert args.dry_run and args.machine == Path("box.toml") and args.out == Path("out")
        assert args.entry == "runner:run_matrix"

    def test_run_defaults(self) -> None:
        args = parse("run", "m")
        assert args.repeats is None and args.order is None and args.only is None and not args.dry_run

    def test_bad_order(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as info:
            parse("run", "m", "--order", "shuffle")
        assert info.value.code == ExitCode.USAGE
        assert "order must be" in capsys.readouterr().err

    def test_run_one_by_server(self) -> None:
        args = parse("run-one", "--server", "bds@1.26.52.3", "--repeat", "2", "--variant", "x")
        assert (args.server, args.repeat, args.variant) == ("bds@1.26.52.3", 2, "x")
        assert args.matrix is None and args.arm is None and args.order_index is None

    @pytest.mark.parametrize(
        "argv",
        [
            ["run-one"],
            ["run-one", "--matrix", "m"],
            ["run-one", "--server", "bds@1", "--matrix", "m", "--arm", "a"],
        ],
    )
    def test_run_one_needs_one_arm_source(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as info:
            cli.main(argv)
        assert info.value.code == ExitCode.USAGE

    def test_fetch(self) -> None:
        args = parse("fetch", "bds@1.26.52.3", "endstone-0.11.14", "--accept-eula", "--artifact-dir", "dl")
        assert args.targets == ["bds@1.26.52.3", "endstone-0.11.14"]
        assert args.accept_eula and args.artifact_dir == Path("dl")
        with pytest.raises(SystemExit):
            parse("fetch")

    def test_bots(self) -> None:
        args = parse("bots", "build", "--module", "legacy1001", "--target", "linux/amd64", "--target", "windows/amd64")
        assert (args.action, args.module, args.targets) == ("build", "legacy1001", ["linux/amd64", "windows/amd64"])
        assert parse("bots", "fetch").module is None
        with pytest.raises(SystemExit):
            parse("bots", "install")

    def test_other_commands(self) -> None:
        assert parse("doctor", "--json").json
        assert parse("smoke", "bds@1.26.52.3", "--artifact", "x.zip").artifact == Path("x.zip")
        assert parse("analyze", "a", "b").run_dirs == [Path("a"), Path("b")]
        report = parse("report", "runs/m", "--format", "json", "--publish")
        assert (report.matrix_dir, report.format, report.publish) == (Path("runs/m"), "json", True)
        assert parse("validate", "r1", "r2").paths == [Path("r1"), Path("r2")]
        docs = parse("docs", "metrics", "--check")
        assert docs.target == "metrics" and docs.check


# --------------------------------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A throwaway package standing in for bedrock_bench in resolve_entry tests."""
    pkg = tmp_path / "fakebench"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "sub" / "__init__.py").write_text("")
    (pkg / "good.py").write_text("def run(args):\n    return 3\n\nVALUE = 1\n")
    (pkg / "sub" / "deep.py").write_text("def run(args):\n    return None\n")
    (pkg / "broken.py").write_text("import a_module_that_does_not_exist_anywhere\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(cli, "PACKAGE", "fakebench")
    for name in [m for m in sys.modules if m == "fakebench" or m.startswith("fakebench.")]:
        monkeypatch.delitem(sys.modules, name)
    return "fakebench"


class TestResolveEntry:
    def test_resolves_function(self, fake_package: str) -> None:
        assert cli.resolve_entry("good:run")(argparse.Namespace()) == 3
        assert cli.resolve_entry("sub.deep:run")(argparse.Namespace()) is None

    def test_missing_entries_raise(self, fake_package: str) -> None:
        with pytest.raises(ModuleNotFoundError):
            cli.resolve_entry("missing:run")
        with pytest.raises(AttributeError):
            cli.resolve_entry("good:missing")

    def test_import_errors_inside_a_module_propagate(self, fake_package: str) -> None:
        with pytest.raises(ModuleNotFoundError, match="a_module_that_does_not_exist_anywhere"):
            cli.resolve_entry("broken:run")

    @pytest.mark.parametrize("command", cli.COMMANDS, ids=lambda command: command.name)
    def test_every_command_resolves(self, command: cli.Command) -> None:
        assert callable(cli.resolve_entry(command.entry))


class TestMain:
    def test_dispatches_with_parsed_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[str, argparse.Namespace]] = []

        def fake_resolve(entry: str):
            def run(args: argparse.Namespace) -> int:
                seen.append((entry, args))
                return ExitCode.SERVER_FAILED

            return run

        monkeypatch.setattr(cli, "resolve_entry", fake_resolve)
        assert cli.main(["analyze", "runs/x"]) == ExitCode.SERVER_FAILED
        entry, args = seen[0]
        assert entry == "metrics:analyze_cli" and args.run_dirs == [Path("runs/x")] and args.command == "analyze"

    def test_none_means_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "resolve_entry", lambda entry: lambda args: None)
        assert cli.main(["doctor"]) == ExitCode.OK

    def test_config_error(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        def bad(args: argparse.Namespace) -> int:
            raise ConfigError("unknown key", file="configs/matrices/x.toml", key="arm[0].steady")

        monkeypatch.setattr(cli, "resolve_entry", lambda entry: bad)
        assert cli.main(["run", "x"]) == ExitCode.USAGE
        assert "configs/matrices/x.toml: arm[0].steady: unknown key" in capsys.readouterr().err

    def test_interrupted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def interrupted(args: argparse.Namespace) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "resolve_entry", lambda entry: interrupted)
        assert cli.main(["run", "x"]) == ExitCode.INTERRUPTED

    def test_other_errors_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def buggy(args: argparse.Namespace) -> int:
            raise ZeroDivisionError

        monkeypatch.setattr(cli, "resolve_entry", lambda entry: buggy)
        with pytest.raises(ZeroDivisionError):
            cli.main(["report", "runs/m"])
