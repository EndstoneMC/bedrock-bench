# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import dataclasses
import json
import typing
from collections.abc import Callable
from pathlib import Path

import pytest

from bedrock_bench import config, workload
from bedrock_bench.config import ConfigError
from bedrock_bench.settle import SettleRules
from bedrock_bench.workload import PHASES

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
RUNNABLE = sorted(p.stem for p in CONFIGS.glob("*.toml") if p.stem != "spread")


@pytest.fixture
def configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], Path]:
    """Write ``configs/<name>.toml`` into a project root at ``tmp_path``."""
    monkeypatch.setenv(config.ENV_ROOT, str(tmp_path))
    (tmp_path / "configs").mkdir()

    def write(name: str, text: str) -> Path:
        path = tmp_path / "configs" / f"{name}.toml"
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    return write


def full_config() -> str:
    """configs/spread.toml with a bot count: a config that needs no base."""
    return (CONFIGS / "spread.toml").read_text(encoding="utf-8").replace("[bots]\n", "[bots]\nn = 8\n", 1)


@pytest.mark.parametrize("name", RUNNABLE)
def test_every_config_loads(name: str) -> None:
    w = config.load_workload(CONFIGS / f"{name}.toml")
    assert w.name == name and w.base == ("spread",)
    assert tuple(p.name for p in w.phases) == PHASES
    assert w.grid.spacing > 2 * 16 * w.server.view_distance, "neighbouring bots would share chunks"
    assert w.phase("settle")["min_s"] <= w.phase("settle").cap_s


def test_the_spread_configs_differ_only_in_bot_count() -> None:
    loaded = {n: config.load_workload(n) for n in ("spread-100", "spread-50", "spread-25")}
    assert [w.bots.n for w in loaded.values()] == [100, 50, 25]
    params = [w.params for w in loaded.values()]
    for p in params:
        del p["bots"]["n"]
    assert params[0] == params[1] == params[2]
    assert len({w.sha256 for w in loaded.values()}) == 3


def test_smoke_is_short() -> None:
    smoke, reference = config.load_workload("smoke"), config.load_workload("spread-100")
    assert (smoke.bots.n, smoke.run.repeats) == (4, 1)
    assert smoke.phase("steady").duration_s < reference.phase("steady").duration_s
    assert smoke.server.csg is reference.server.csg is False


def test_the_base_does_not_run_on_its_own() -> None:
    with pytest.raises(ConfigError, match=r"bots\.n: is required"):
        config.load_workload(CONFIGS / "spread.toml")


def test_settle_settings_are_the_settle_rules() -> None:
    spec = config.load_workload("spread-100").phase("settle")
    assert {*spec.options, "cap_s"} == {f.name for f in dataclasses.fields(SettleRules)}
    assert set(typing.get_type_hints(SettleRules)) == {*spec.options, "cap_s"}


def test_a_bare_name_resolves_in_configs(configs: Callable[[str, str], Path]) -> None:
    configs("mine", full_config())
    assert config.load_workload("mine").bots.n == 8
    with pytest.raises(ConfigError, match=r"no such config \(available: mine\)"):
        config.load_workload("other")


def test_inheritance_merges_tables_key_by_key(configs: Callable[[str, str], Path]) -> None:
    configs("full", full_config())
    configs("middle", 'base = "full"\n\n[bots]\nn = 6\n\n[phases.settle]\nmin_s = 1.0\n')
    path = configs("leaf", 'base = "middle"\n\n[server]\nview_distance = 4\n')
    w = config.load_workload(path)
    assert (w.name, w.base) == ("leaf", ("middle", "full"))
    assert (w.bots.n, w.bots.radius, w.server.view_distance, w.server.tick_distance) == (6, 16, 4, 4)
    assert (w.phase("settle")["min_s"], w.phase("settle").cap_s) == (1.0, 900.0)


def test_a_base_cycle_is_an_error(configs: Callable[[str, str], Path]) -> None:
    configs("a", 'base = "b"\n')
    configs("b", 'base = "a"\n')
    with pytest.raises(ConfigError, match="cycle: a -> b -> a"):
        config.load_workload("a")


def test_a_config_cannot_be_its_own_base(configs: Callable[[str, str], Path]) -> None:
    configs("a", 'base = "a"\n')
    with pytest.raises(ConfigError, match="cycle: a -> a"):
        config.load_workload("a")


@pytest.mark.parametrize(
    ("base", "message"),
    [('"missing"', "unknown base 'missing'"), ('"../spread"', "must name a config"), ("1", "must name a config")],
)
def test_a_bad_base_is_an_error(configs: Callable[[str, str], Path], base: str, message: str) -> None:
    configs("a", f"base = {base}\n")
    with pytest.raises(ConfigError, match=message):
        config.load_workload("a")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("[bots]\nwalk = 'run'\n", r"bots\.walk: must be one of circle, none"),
        ("[bots]\ncount = 4\n", r"bots\.count: unknown key"),
        ("[phases.settle]\nminimum = 1.0\n", r"phases\.settle\.minimum: unknown key"),
        ("[phases.chaos]\ncap_s = 1.0\n", r"phases\.chaos: unknown key"),
        ("[run]\norder = 'random'\n", r"run\.order: order must be rotate"),
        ("[run]\nrepeats = 0\n", r"run\.repeats: must be >= 1"),
        ("[server]\nmax_players = 4\n", "max_players must be at least bots.n"),
        ("[scenario]\nname = 'x'\n", r"scenario: unknown key"),
    ],
)
def test_the_resolved_config_is_validated(configs: Callable[[str, str], Path], extra: str, message: str) -> None:
    configs("full", full_config())
    configs("bad", f'base = "full"\n\n{extra}')
    with pytest.raises(ConfigError, match=message):
        config.load_workload("bad")


def test_record_round_trip() -> None:
    w = config.load_workload("spread-100")
    record = json.loads(json.dumps(w.record()))
    assert (record["name"], record["base"], record["sha256"]) == ("spread-100", ["spread"], w.sha256)
    assert record["params"]["bots"]["n"] == 100 and "base" not in record["params"]
    rebuilt = workload.from_record({"workload": record})
    assert rebuilt == w and rebuilt.sha256 == w.sha256


def test_a_record_without_a_workload_is_an_error() -> None:
    with pytest.raises(ConfigError, match="no workload"):
        workload.from_record({})


def test_the_hash_ignores_how_a_number_is_written(configs: Callable[[str, str], Path]) -> None:
    a = config.load_workload(configs("a", full_config()))
    b = config.load_workload(configs("b", full_config().replace("cap_s = 600.0", "cap_s = 600")))
    assert a.sha256 == b.sha256
