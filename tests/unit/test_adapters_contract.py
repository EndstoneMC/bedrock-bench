# SPDX-License-Identifier: Apache-2.0
"""The server adapter contract (SPEC.md section 5.1), checked for every registered adapter and every fake adapter.

Per adapter: the class attributes, the dialect against fixture log lines, well-formed commands, commands against a
committed snapshot, chaos actions rendered or marked unsupported, bot flags inside the allowlist, and a feature file
that covers every key the scenarios exercise. The last section covers the shared code in ``adapters/base.py`` and the
registry.

Fixture lines come from ``tests/fixtures/logs/<name>*`` (any text file) for real adapters and from
``fake_server.sample_log`` for the fakes. Snapshots are ``tests/unit/snapshots/adapters/<name>.json``; run with
``BBENCH_UPDATE_SNAPSHOTS=1`` to write or refresh them after reviewing a change.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import adapters, config
from bedrock_bench.adapters import AdapterError
from bedrock_bench.adapters.base import (
    ALLOWED_COMPAT,
    ChaosDialect,
    Dialect,
    ServerAdapter,
    UnsupportedAction,
    apply_tick_rule,
    check_tick_rule,
    format_coord,
    quote_player,
)
from bedrock_bench.config import ConfigError, project_root
from bedrock_bench.model import PLATFORMS, Layout, Machine, ThreadInfo, WorldInfo
from fakes import fake_server
from fakes.bench import FAKE_SCENARIOS, register_entry_point
from fakes.fake_adapter import FAKE_ADAPTERS, FakeAdapter, FakeBds

ROOT = project_root()
FIXTURE_LOGS = ROOT / "tests" / "fixtures" / "logs"
SNAPSHOTS = Path(__file__).resolve().parent / "snapshots" / "adapters"
UPDATE_SNAPSHOTS = os.environ.get("BBENCH_UPDATE_SNAPSHOTS") == "1"

#: Protocols with a bot build: 2193 -> bots/loadbot, 1001 -> bots/legacy1001.
BOT_PROTOCOLS = frozenset({2193, 1001})
#: Files in scenarios/ that are not scenarios.
NOT_SCENARIOS = frozenset({"chaos-actions"})
FAKE_VERSION = "1.0.0"
PLAYER = "a07"

REGISTERED = sorted(adapters.all_adapters().values(), key=lambda cls: cls.name)
EVERY_ADAPTER = [*REGISTERED, *FAKE_ADAPTERS]


def adapter_id(cls: type[ServerAdapter]) -> str:
    return cls.name


every_adapter = pytest.mark.parametrize("cls", EVERY_ADAPTER, ids=adapter_id)
real_adapters = pytest.mark.parametrize("cls", REGISTERED, ids=adapter_id)


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def is_fake(cls: type[ServerAdapter]) -> bool:
    return issubclass(cls, FakeAdapter)


def pinned_versions(cls: type[ServerAdapter]) -> list[str]:
    """Versions pinned in ``artifacts/<name>.toml``; empty when the file does not exist."""
    if is_fake(cls):
        return [FAKE_VERSION]
    path = ROOT / "artifacts" / f"{cls.name}.toml"
    if not path.is_file():
        return []
    return sorted(config.load_artifacts(path).versions)


def make(cls: type[ServerAdapter], variant: str | None = None, machine: Machine | None = None) -> ServerAdapter:
    version = (pinned_versions(cls) or ["0.0.0"])[0]
    arm = config.arm_from_ref(f"{cls.name}@{version}", variant=variant)
    return cls(arm, machine or config.default_machine())


def fixture_lines(cls: type[ServerAdapter]) -> list[str]:
    if is_fake(cls):
        return fake_server.sample_log(cls.family, FAKE_VERSION)
    files = [p for p in FIXTURE_LOGS.glob(f"{cls.name}*") if p.is_file()]
    if (FIXTURE_LOGS / cls.name).is_dir():
        files += [p for p in (FIXTURE_LOGS / cls.name).rglob("*") if p.is_file()]
    files = [p for p in files if p.suffix.lower() not in (".json", ".toml", ".py", ".gz")]
    if not files:
        pytest.fail(f"no fixture logs for {cls.name}: add line samples as tests/fixtures/logs/{cls.name}*.log")
    return [line for path in sorted(files) for line in path.read_text(encoding="utf-8").splitlines()]


def scenarios_for(cls: type[ServerAdapter]) -> list[Any]:
    """The checked-in scenarios (and the fake-quick scenario for fakes). Scenario validity is test_config's job."""
    found = []
    for name in config.available("scenarios"):
        if name in NOT_SCENARIOS:
            continue
        try:
            found.append(config.load_scenario(name))
        except ConfigError:
            continue
    if is_fake(cls):
        found += [config.load_scenario(path) for path in sorted(FAKE_SCENARIOS.glob("*.toml"))]
    return found


def chaos_commands() -> dict[str, dict[str, Any]]:
    """The ``[command.<name>]`` tables of the chaos catalogue: the console commands a chaos dialect renders."""
    path = config.resolve("scenarios", "chaos-actions")
    return dict(tomllib.loads(path.read_text(encoding="utf-8"))["command"])


def rendered_commands(adapter: ServerAdapter) -> dict[str, Any]:
    """Every console command the adapter renders for a fixed input, in a JSON-ready form."""
    dialect = adapter.chaos_dialect()
    chaos: dict[str, Any] = {}
    for command, entry in sorted(chaos_commands().items()):
        if not dialect.supports(command):
            chaos[command] = {"unsupported": dialect.unsupported.get(command, "")}
        else:
            chaos[command] = dialect.render(command, **entry["example"])
    return {
        "tick": adapter.dialect.tick_cmd,
        "stop": adapter.dialect.stop_cmd,
        "effects": adapter.cmd_effects(PLAYER),
        "effects_all": adapter.cmd_effects("@a"),
        "tp": adapter.cmd_tp(PLAYER, 5760.5, 300, 1280.5),
        "positions": adapter.cmd_positions(),
        "chaos": chaos,
        "chaos_supported": sorted(dialect.actions),
        "chaos_unsupported": sorted(dialect.unsupported),
    }


def iter_commands(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != "unsupported":
                yield from iter_commands(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_commands(item)


# --------------------------------------------------------------------------------------------------------------------
# Per-adapter contract
# --------------------------------------------------------------------------------------------------------------------


@every_adapter
def test_class_attributes(cls: type[ServerAdapter]) -> None:
    assert not inspect.isabstract(cls)
    assert cls.class_problems() == []
    assert cls.protocol in BOT_PROTOCOLS, f"no bot build speaks protocol {cls.protocol}"
    assert isinstance(cls.dialect, Dialect)


@every_adapter
def test_bot_compat_flags_are_allowlisted(cls: type[ServerAdapter]) -> None:
    assert set(cls.bot_compat_flags) <= ALLOWED_COMPAT
    assert len(set(cls.bot_compat_flags)) == len(cls.bot_compat_flags)


@every_adapter
def test_construction_and_variants(cls: type[ServerAdapter], monkeypatch: pytest.MonkeyPatch) -> None:
    assert make(cls).arm.server == cls.name
    for variant in cls.variants:
        constructs = any(_constructs(cls, variant, system, monkeypatch) for system in PLATFORMS)
        assert constructs, f"variant {variant!r} never constructs"
    with pytest.raises(ConfigError, match="no-such-variant"):
        make(cls, "no-such-variant")


def _constructs(cls: type[ServerAdapter], variant: str, system: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Whether the variant constructs on ``system``; a variant may be limited to some platforms."""
    module = sys.modules[cls.__module__]
    if hasattr(module, "host_os"):
        monkeypatch.setattr(module, "host_os", lambda: system)
    try:
        adapter = make(cls, variant)
    except ConfigError as exc:
        assert "no variant" not in str(exc), f"declared variant {variant!r} is rejected as unknown"
        return False
    return adapter.arm.variant == variant


@every_adapter
def test_dialect_reads_fixture_lines(cls: type[ServerAdapter]) -> None:
    dialect = cls.dialect
    lines = fixture_lines(cls)
    texts = [dialect.strip(line) for line in lines]
    assert any(dialect.prefix.match(line) for line in lines), "the prefix matches no fixture line"
    assert any(dialect.is_ready(text) for text in texts), "the ready pattern matches no fixture line"
    versions = [v for v in map(dialect.parse_version, texts) if v]
    assert versions, "the version pattern matches no fixture line"
    if is_fake(cls):
        assert versions == [FAKE_VERSION]
    if dialect.tick_monotonic:
        assert any(dialect.parse_tick(text) is not None for text in texts), "the tick pattern matches no fixture line"
    assert any(dialect.player_entered(text) for text in texts), "the player-in-world pattern matches no fixture line"
    raw_levels = set()
    for line in lines:
        match = dialect.prefix.match(line)
        if match is not None and match.groupdict().get("level"):
            raw_levels.add(match["level"])
    assert raw_levels, "no fixture line carries a level"
    unmapped = sorted(raw_levels - set(dialect.level_map))
    assert not unmapped, f"levels without a mapping: {unmapped}"


def dialect_samples(cls: type[ServerAdapter]) -> list[dict[str, Any]] | None:
    """``[[sample]]`` tables of ``tests/fixtures/logs/<name>.toml``: one console line per dialect role."""
    path = FIXTURE_LOGS / f"{cls.name}.toml"
    if not path.is_file():
        return None
    return list(tomllib.loads(path.read_text(encoding="utf-8")).get("sample", []))


@real_adapters
def test_dialect_samples(cls: type[ServerAdapter]) -> None:
    samples = dialect_samples(cls)
    if samples is None:
        pytest.skip(f"no tests/fixtures/logs/{cls.name}.toml")
    dialect = cls.dialect
    for sample in samples:
        role, line, value = sample["role"], sample["line"], sample.get("value")
        level, _, text = dialect.split(line)
        if role == "ready":
            assert dialect.is_ready(text), line
        elif role == "version":
            assert dialect.parse_version(text) == value, line
        elif role == "player_in_world":
            assert dialect.player_entered(text) == value, line
        elif role == "tick":
            assert dialect.parse_tick(text) == value, line
        elif role == "level":
            assert level == value, line
        elif role == "strip":
            assert text == value, line
        elif role == "benign":
            assert level in ("error", "fatal") and dialect.is_benign(text), line
        elif role == "not_benign":
            assert not dialect.is_benign(text), line
        else:
            pytest.fail(f"unknown sample role {role!r} in tests/fixtures/logs/{cls.name}.toml")


@every_adapter
def test_ready_line_is_not_a_tick_or_player_line(cls: type[ServerAdapter]) -> None:
    dialect = cls.dialect
    for text in map(dialect.strip, fixture_lines(cls)):
        if dialect.is_ready(text):
            assert dialect.parse_tick(text) is None
            assert dialect.player_entered(text) is None


@every_adapter
def test_commands_are_single_lines(cls: type[ServerAdapter]) -> None:
    adapter = make(cls)
    rendered = rendered_commands(adapter)
    for command in iter_commands(rendered):
        assert command.strip() == command and command, f"bad command {command!r}"
        assert "\n" not in command and "\r" not in command
    assert all(PLAYER in command for command in rendered["effects"])
    assert PLAYER in rendered["tp"]
    assert "5760.5" in rendered["tp"] and "1280.5" in rendered["tp"]


@real_adapters
def test_commands_match_snapshot(cls: type[ServerAdapter]) -> None:
    rendered = rendered_commands(make(cls))
    path = SNAPSHOTS / f"{cls.name}.json"
    if UPDATE_SNAPSHOTS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rendered, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return
    if not path.is_file():
        pytest.skip(f"no snapshot {path.relative_to(ROOT).as_posix()}; create it with BBENCH_UPDATE_SNAPSHOTS=1")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert rendered == expected, "rendered commands changed; review, then refresh with BBENCH_UPDATE_SNAPSHOTS=1"


@every_adapter
def test_chaos_commands_rendered_or_unsupported(cls: type[ServerAdapter]) -> None:
    dialect = make(cls).chaos_dialect()
    undeclared = sorted(c for c in chaos_commands() if not dialect.supports(c) and c not in dialect.unsupported)
    assert not undeclared, f"commands neither rendered nor marked unsupported: {undeclared}"


@every_adapter
def test_unknown_positions_are_none(cls: type[ServerAdapter]) -> None:
    """An empty or missing reply is unknown, never zero players (the spread is then invalid)."""
    assert make(cls).parse_positions([]) is None


@every_adapter
def test_feature_file_covers_exercised_keys(cls: type[ServerAdapter]) -> None:
    path = Path(cls.features_file)
    if not path.is_absolute():
        path = ROOT / path
    assert path.is_file(), f"missing feature file {cls.features_file}"
    features = config.load_features(path)
    assert features.server == cls.name
    exercised = {key for scenario in scenarios_for(cls) for key in scenario.exercises}
    versions = pinned_versions(cls)
    rows = [(v, features.row(v)) for v in versions] if versions else [(r.prefix, r) for r in features.ranges]
    for version, row in rows:
        assert row is not None, f"no feature range covers pinned version {version}"
        missing = sorted(exercised - set(row.cells))
        assert not missing, f"range {row.prefix!r} does not declare exercised keys {missing}"


# --------------------------------------------------------------------------------------------------------------------
# Shared behaviour in adapters/base.py and the registry
# --------------------------------------------------------------------------------------------------------------------


def _dialect(**changes: Any) -> Dialect:
    fields: dict[str, Any] = {
        "ready": re.compile(r"^Server started\."),
        "prefix": re.compile(r"^\[(?P<level>[A-Z]+)\] (?:\[(?P<logger>[^\]]+)\] )?"),
        "level_map": {"INFO": "info", "WRN": "warn"},
        "benign": (re.compile(r"^TRANSPORT"),),
        "version": re.compile(r"^Version: (?P<version>\S+)$"),
        "player_in_world": re.compile(r"^Player Spawned: (?P<player>\S+)"),
        "tick_cmd": "time query gametime",
        "tick_re": re.compile(r"^Gametime is (?P<tick>\d+)$"),
    }
    return Dialect(**(fields | changes))


def test_dialect_splits_prefix_level_and_logger() -> None:
    dialect = _dialect()
    assert dialect.split("[WRN] [Server] Gametime is 5") == ("warn", "Server", "Gametime is 5")
    assert dialect.split("[INFO] Server started.") == ("info", None, "Server started.")
    assert dialect.split("   bare continuation") == (None, None, "   bare continuation")
    assert dialect.split("[DBG] x") == (None, None, "x"), "an unmapped level reads as None"
    assert dialect.parse_tick(dialect.split("[INFO] Gametime is 0")[2]) == 0
    assert dialect.parse_version("Version: 1.26.52.3") == "1.26.52.3"
    assert dialect.player_entered("Player Spawned: a07 xuid: 1") == "a07"
    assert dialect.is_benign("TRANSPORT TYPE ERROR") and not dialect.is_benign("real error")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": re.compile(r"Version: \S+")}, "version"),
        ({"player_in_world": re.compile(r"Spawned")}, "player"),
        ({"tick_re": re.compile(r"\d+")}, "tick"),
        ({"mspt_cmd": "mspt"}, "go together"),
        ({"mspt_cmd": "mspt", "mspt_re": re.compile(r"(?P<ms>\d+)")}, "mspt"),
        ({"level_map": {"INFO": "information"}}, "unknown levels"),
        ({"newline": "\r"}, "newline"),
    ],
)
def test_dialect_validation(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _dialect(**changes)


THREADS = [
    ThreadInfo(tid=40, name="MC_SERVER", created_ns=300),
    ThreadInfo(tid=12, name="Main", created_ns=100),
    ThreadInfo(tid=17, name="MC_SERVER", created_ns=200),
    ThreadInfo(tid=21, name="main", created_ns=None),
]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("lowest_tid_name:MC_SERVER", 17),
        ("name:MC_SERVER", None),
        ("name:Main", 12),
        ("java_main", 21),
        ("first_created", 12),
        ("none", None),
        ("lowest_tid_name:Missing", None),
    ],
)
def test_tick_thread_rules(rule: str, expected: int | None) -> None:
    assert apply_tick_rule(rule, THREADS) == expected


def test_first_created_falls_back_to_lowest_tid() -> None:
    threads = [ThreadInfo(tid=9, name="b"), ThreadInfo(tid=3, name="a")]
    assert apply_tick_rule("first_created", threads) == 3
    assert apply_tick_rule("first_created", []) is None


@pytest.mark.parametrize("rule", ["name:", "lowest_tid_name", "fastest", "java_main:x", "tid:3"])
def test_bad_tick_rules(rule: str) -> None:
    with pytest.raises(ValueError):
        check_tick_rule(rule)


def test_chaos_dialect_renders_and_refuses() -> None:
    dialect = ChaosDialect(
        renderers={"kick": lambda player, reason="x": f"kick {player} {reason}", "pair": lambda: ["a", "b"]},
        unsupported={"summon": "no mobs"},
    )
    assert dialect.render("kick", player="a01") == ["kick a01 x"]
    assert dialect.render("pair") == ["a", "b"]
    assert dialect.actions == {"kick", "pair"}
    with pytest.raises(UnsupportedAction):
        dialect.render("summon")
    with pytest.raises(ValueError, match="both"):
        ChaosDialect(renderers={"kick": lambda: "kick"}, unsupported={"kick": "?"})


@pytest.mark.parametrize(("value", "text"), [(1280, "1280"), (1280.0, "1280"), (1280.5, "1280.5"), (-0.125, "-0.12")])
def test_format_coord(value: float, text: str) -> None:
    assert format_coord(value) == text


def test_default_commands_use_vanilla_syntax() -> None:
    adapter = make(FakeBds)
    assert adapter.cmd_tp("a07", 1280.5, 300, 1920) == "tp a07 1280.5 300 1920"
    assert adapter.cmd_effects("a b")[0].startswith('effect "a b" resistance infinite 255')
    assert quote_player("a07") == "a07"


def test_features_file_defaults_to_the_adapter_name() -> None:
    class Probe(ServerAdapter):
        name = "probe"

    assert Probe.features_file == "features/probe.toml"


def test_crash_artifacts_are_files_modified_since_the_run_started(tmp_path: Path) -> None:
    folder = tmp_path / "crash_reports"
    folder.mkdir()
    old, new = folder / "old.txt", folder / "new.txt"
    old.write_text("old")
    new.write_text("new")
    past = time.time() - 3600
    os.utime(old, (past, past))
    layout = Layout(server_dir=tmp_path, overrides=(), config_files=(), world=WorldInfo(True, "vanilla"))
    since_ms = int((time.time() - 60) * 1000)
    assert make(FakeBds).crash_artifacts(layout, since_ms) == [new]


def test_registry_rejects_an_adapter_that_breaks_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "broken_adapter.py").write_text(
        "from fakes.fake_adapter import FakeAdapter\n\n\n"
        "class Broken(FakeAdapter):\n"
        '    name = "Broken Name"\n'
        '    family = "bds"\n'
        "    protocol = 0\n",
        encoding="utf-8",
    )
    dist = register_entry_point(tmp_path) / "bedrock_bench_fakes-0.0.0.dist-info"
    (dist / "entry_points.txt").write_text("[bedrock_bench.adapters]\nbroken = broken_adapter\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    adapters.refresh()
    try:
        with pytest.raises(AdapterError, match="breaks the adapter contract"):
            adapters.names()
    finally:
        monkeypatch.undo()
        adapters.refresh()


def test_registry_unknown_server_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="unknown server"):
        adapters.get("no_such_server")
