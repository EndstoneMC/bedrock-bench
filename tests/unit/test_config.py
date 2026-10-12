# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest

from bedrock_bench import config
from bedrock_bench.config import ConfigError
from bedrock_bench.model import FeatureClass, Fidelity, Gates, Machine, OverrideCategory

MINIMAL_SCENARIO = {"server": {"csg": False}}


def scenario(data: dict | None = None, name: str = "test") -> config.Scenario:
    return config.parse_scenario(data if data is not None else MINIMAL_SCENARIO, name=name)


def toml(text: str) -> str:
    return textwrap.dedent(text).lstrip()


# --------------------------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------------------------


class TestScenarioDefaults:
    def test_common_parameters(self) -> None:
        s = scenario()
        assert s.bots.n == 100 and s.bots.stagger_ms == 300 and s.bots.spawn_timeout_s == 180
        assert (s.grid.columns, s.grid.spacing, s.grid.origin, s.grid.y) == (11, 640, 1280, 300)
        assert s.world.seed == 20261010 and s.world.level_name == "bench" and s.world.source == "fresh"
        assert s.server.view_distance == 10 and s.server.tick_distance == 4
        assert s.server.difficulty == "easy" and s.server.max_players == 200 and s.server.autosave_ticks == 6000
        assert s.probe.interval_s == 5.0

    def test_default_phases_and_timings(self) -> None:
        s = scenario()
        assert [p.name for p in s.phases] == ["boot", "idle", "join", "settle", "spread", "steady", "teardown"]
        assert s.phase("boot").cap_s == 600
        assert (s.phase("idle").delay_s, s.phase("idle").duration_s) == (10, 20)
        assert s.phase("join").cap_s == 300 and s.phase("join")["pre_bots_s"] == 0
        settle = s.phase("settle")
        assert settle.cap_s == 900 and settle["min_s"] == 300 and settle["rate_per_bot"] == 0.1
        assert settle["rate_window_s"] == 60 and settle["retp_max"] == 3 and settle["nudge_max"] == 2
        assert (s.phase("steady").delay_s, s.phase("steady").duration_s) == (15, 90)
        assert s.phase("teardown")["stop_timeout_s"] == 180

    def test_csg_is_required(self) -> None:
        with pytest.raises(ConfigError, match=r"server: is required"):
            scenario({})
        with pytest.raises(ConfigError, match=r"server\.csg: is required"):
            scenario({"server": {}})

    def test_resolved_and_hash(self) -> None:
        s = scenario({"server": {"csg": True}, "exercises": ["player.physics"]})
        assert s.resolved["server"]["csg"] is True
        assert s.resolved["exercises"] == ["player.physics"]
        assert s.resolved["phases"][0]["name"] == "boot"
        assert s.sha256 == config.sha256_json(s.resolved)
        assert len(s.sha256) == 64

    def test_hash_ignores_description_and_explicit_defaults(self) -> None:
        plain = scenario()
        explicit = scenario(
            {"description": "docs only", "server": {"csg": False, "view_distance": 10}, "bots": {"n": 100}}
        )
        assert plain.sha256 == explicit.sha256
        assert scenario({"server": {"csg": True}}).sha256 != plain.sha256

    def test_hash_depends_on_name(self) -> None:
        assert scenario(name="a").sha256 != scenario(name="b").sha256


class TestScenarioValidation:
    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"server": {"csg": False}, "bogus": 1}, r"bogus: unknown key"),
            ({"server": {"csg": False}, "bots": {"count": 3}}, r"bots\.count: unknown key"),
            ({"server": {"csg": "yes"}}, r"server\.csg: expected true or false, got a string"),
            ({"server": {"csg": False}, "bots": {"n": "100"}}, r"bots\.n: expected an integer"),
            ({"server": {"csg": False}, "bots": {"n": True}}, r"bots\.n: expected an integer, got a boolean"),
            ({"server": {"csg": False}, "bots": {"n": 0}}, r"bots\.n: must be >= 1"),
            ({"server": {"csg": False}, "bots": {"walk": "zigzag"}}, r"bots\.walk: must be one of circle, none"),
            ({"server": {"csg": False}, "server_": 1}, r"unknown key"),
            ({"server": {"csg": False, "difficulty": "nightmare"}}, r"server\.difficulty"),
            ({"server": {"csg": False}, "probe": {"interval_s": 0}}, r"probe\.interval_s: must be > 0"),
            ({"server": {"csg": False}, "exercises": ["player.flying"]}, r"unknown feature key 'player\.flying'"),
            ({"server": {"csg": False}, "requires": ["player.physics", "player.physics"]}, r"duplicate"),
            ({"server": {"csg": False}, "steady": {"duration_s": 60}}, r"phase settings go under \[phases\.steady\]"),
            ({"server": {"csg": False}, "phases": {"warmup": {}}}, r"phases\.warmup: unknown phase"),
            ({"server": {"csg": False}, "phases": {"steady": {"cap_s": 5}}}, r"the steady phase has no cap_s"),
            ({"server": {"csg": False}, "phases": {"join": {"enabled": False}}}, r"the join phase always runs"),
            ({"server": {"csg": False}, "phases": {"settle": {"min": 10}}}, r"phases\.settle\.min: unknown key"),
            ({"server": {"csg": False}, "name": "other"}, r"name: must match the file name"),
        ],
    )
    def test_rejects(self, data: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            scenario(data)

    def test_more_bots_than_slots(self) -> None:
        with pytest.raises(ConfigError, match=r"bots\.n: is larger than server\.max_players"):
            scenario({"server": {"csg": False, "max_players": 10}, "bots": {"n": 20}})

    def test_views_must_not_overlap(self) -> None:
        with pytest.raises(ConfigError, match=r"grid\.spacing"):
            scenario({"server": {"csg": False, "view_distance": 20}})
        scenario({"server": {"csg": False, "view_distance": 19}})

    def test_settle_floor_above_cap(self) -> None:
        with pytest.raises(ConfigError, match=r"min_s"):
            scenario({"server": {"csg": False}, "phases": {"settle": {"min_s": 1000}}})

    def test_bad_name(self) -> None:
        with pytest.raises(ConfigError, match="bad scenario name"):
            scenario(name="Spread 100")


class TestScenarioPhases:
    def test_disable_optional_phases(self) -> None:
        s = scenario({"server": {"csg": False}, "phases": {"steady": {"enabled": False}, "idle": {"enabled": False}}})
        assert not s.has_phase("steady") and not s.has_phase("idle")

    def test_overrides(self) -> None:
        s = scenario(
            {
                "server": {"csg": False, "view_distance": 4},
                "bots": {"n": 4},
                "phases": {"settle": {"min_s": 30, "cap_s": 60}, "steady": {"duration_s": 20}},
            }
        )
        assert s.phase("settle").cap_s == 60 and s.phase("settle")["min_s"] == 30
        assert s.phase("steady").duration_s == 20 and s.bots.n == 4


class TestScenarioFiles:
    def test_load_by_path_and_extends(self, write_file) -> None:
        base = write_file(
            "configs/scenarios/base.toml",
            toml(
                """
                name = "base"
                description = "base scenario"
                exercises = ["player.physics"]

                [server]
                csg = true
                view_distance = 10

                [phases.steady]
                duration_s = 90
                """
            ),
        )
        child = write_file(
            "configs/scenarios/child.toml",
            toml(
                """
                extends = "base"

                [server]
                csg = false

                [phases.steady]
                delay_s = 5
                """
            ),
        )
        parent = config.load_scenario(base)
        s = config.load_scenario(child)
        assert s.name == "child" and s.description == ""
        assert s.server.csg is False and s.server.view_distance == 10
        assert s.exercises == ("player.physics",)
        assert (s.phase("steady").delay_s, s.phase("steady").duration_s) == (5, 90)
        assert parent.server.csg is True

    def test_extends_composition_does_not_change_hash(self, write_file) -> None:
        write_file("configs/scenarios/base.toml", "[server]\ncsg = true\n[bots]\nn = 50\n")
        composed = write_file("configs/scenarios/same.toml", 'extends = "base"\n# comment\n')
        flat = write_file("other/same.toml", "[bots]\nn = 50\n\n[server]\ncsg = true\n")
        assert config.load_scenario(composed).sha256 == config.load_scenario(flat).sha256

    def test_extends_cycle(self, write_file) -> None:
        write_file("configs/scenarios/a.toml", 'extends = "b"\n[server]\ncsg = true\n')
        b = write_file("configs/scenarios/b.toml", 'extends = "a"\n')
        with pytest.raises(ConfigError, match="circular extends"):
            config.load_scenario(b)

    def test_extends_missing(self, write_file) -> None:
        path = write_file("configs/scenarios/a.toml", 'extends = "nope"\n')
        with pytest.raises(ConfigError, match="no scenario 'nope'"):
            config.load_scenario(path)

    def test_invalid_toml(self, write_file) -> None:
        path = write_file("configs/scenarios/broken.toml", "[server\ncsg = true\n")
        with pytest.raises(ConfigError, match="invalid TOML") as info:
            config.load_scenario(path)
        assert "broken.toml" in str(info.value)

    def test_error_names_file_and_key(self, write_file) -> None:
        path = write_file("configs/scenarios/typo.toml", "[server]\ncsg = true\n[bots]\nprefx = 'a'\n")
        with pytest.raises(ConfigError) as info:
            config.load_scenario(path)
        err = info.value
        assert err.key == "bots.prefx" and Path(err.file) == path
        assert str(err).endswith(
            "typo.toml: bots.prefx: unknown key (allowed here: "
            + ", ".join(sorted(f for f in config.BotParams.__dataclass_fields__))
            + ")"
        )


# --------------------------------------------------------------------------------------------------------------------
# Matrices
# --------------------------------------------------------------------------------------------------------------------

MATRIX = toml(
    """
    [matrix]
    name = "endstone-0.11.14"
    scenarios = ["spread100"]
    repeats = 3
    order = "rotate"
    cooldown_s = 60
    rerun_invalid = 1
    priority_override = "above_normal"

    [[arm]]
    server = "bds@1.26.52.3"

    [[arm]]
    server = "endstone@0.11.14"
    bds = "1.26.52.3"
    python = "3.13"
    """
)


class TestMatrix:
    def test_load(self, write_file) -> None:
        path = write_file("configs/matrices/endstone-0.11.14.toml", MATRIX)
        m = config.load_matrix(path)
        assert m.name == "endstone-0.11.14" and m.scenarios == ("spread100",)
        assert (m.repeats, m.order, m.cooldown_s, m.rerun_invalid) == (3, "rotate", 60, 1)
        assert m.priority_override == "above_normal"
        assert [a.id for a in m.arms] == ["bds@1.26.52.3", "endstone@0.11.14"]
        endstone = m.arm("endstone@0.11.14")
        assert (endstone.server, endstone.version, endstone.bds, endstone.python) == (
            "endstone",
            "0.11.14",
            "1.26.52.3",
            "3.13",
        )
        assert endstone.ref == "endstone@0.11.14" and m.scenarios_for(endstone) == ("spread100",)
        assert len(m.sha256) == 64 and m.path == path
        with pytest.raises(KeyError):
            m.arm("nope")

    def test_hash_ignores_line_endings(self, tmp_path: Path) -> None:
        lf = tmp_path / "lf" / "m.toml"
        crlf = tmp_path / "crlf" / "m.toml"
        for path, text in ((lf, MATRIX), (crlf, MATRIX.replace("\n", "\r\n"))):
            path.parent.mkdir()
            path.write_bytes(text.encode())
        assert config.load_matrix(lf).sha256 == config.load_matrix(crlf).sha256

    def test_defaults_and_arm_id(self) -> None:
        m = config.parse_matrix(
            {
                "matrix": {"name": "x", "scenarios": ["smoke"]},
                "arm": [{"server": "endstone@0.11.14", "variant": "glibc-alloc"}],
            }
        )
        assert m.repeats == 1 and m.order == "rotate" and m.priority_override is None
        assert m.arms[0].id == "endstone@0.11.14+glibc-alloc"

    @pytest.mark.parametrize("key", ["phases", "steady", "settle", "bots", "world"])
    def test_rule_r1_per_arm_overrides(self, key: str) -> None:
        data = {
            "matrix": {"name": "x", "scenarios": ["s"]},
            "arm": [{"server": "bds@1.26.52.3", key: {"duration_s": 1}}],
        }
        with pytest.raises(ConfigError, match=r"rule R1"):
            config.parse_matrix(data)

    @pytest.mark.parametrize(
        ("arm", "message"),
        [
            ({"server": "bds@1", "version": "1"}, r"arm\[0\]\.version: write the arm as server"),
            ({"server": "bds@1", "id": "vanilla"}, r"arm\[0\]\.id: write the arm as server"),
            ({"server": "bds"}, r"arm\[0\]\.server: expected <server>@<version>"),
            ({"server": "BDS@1"}, r"arm\[0\]\.server: expected <server>@<version>"),
            ({"server": "bds@1+x"}, r"arm\[0\]\.server: expected <server>@<version>"),
            ({"server": "bds@1", "colour": "red"}, r"colour: unknown key"),
            ({"server": "bds@1", "python": "3"}, r"python: invalid value"),
            ({"server": "bds@1", "scenarios": ["other"]}, r"scenarios: must be one of s"),
        ],
    )
    def test_bad_arms(self, arm: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.parse_matrix({"matrix": {"name": "x", "scenarios": ["s"]}, "arm": [arm]})

    def test_duplicate_arm(self) -> None:
        arm = {"server": "bds@1"}
        with pytest.raises(ConfigError, match="duplicate arm id 'bds@1'"):
            config.parse_matrix({"matrix": {"name": "x", "scenarios": ["s"]}, "arm": [arm, arm]})

    @pytest.mark.parametrize(
        ("head", "message"),
        [
            ({"name": "x", "scenarios": []}, "at least one scenario"),
            ({"name": "x", "scenarios": ["s", "s"]}, "twice"),
            ({"name": "x", "scenarios": ["s"], "order": "random"}, "order must be"),
            ({"name": "x", "scenarios": ["s"], "repeats": 0}, "repeats: must be >= 1"),
            ({"name": "x", "scenarios": ["s"], "priority_override": "high"}, "priority_override"),
            ({"name": "x", "scenarios": ["s"], "phases": {}}, "phases: unknown key"),
        ],
    )
    def test_bad_head(self, head: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.parse_matrix({"matrix": head, "arm": [{"server": "bds@1"}]})

    def test_needs_arms(self) -> None:
        with pytest.raises(ConfigError, match=r"\[\[arm\]\]"):
            config.parse_matrix({"matrix": {"name": "x", "scenarios": ["s"]}})

    def test_arm_scenario_subset_and_artifact_path(self, write_file) -> None:
        path = write_file(
            "configs/matrices/cross.toml",
            toml(
                """
                [matrix]
                scenarios = ["spread100", "spread100-pregen"]

                [[arm]]
                server = "nukkit@1.0"
                scenarios = ["spread100-pregen"]
                artifact = "../local/nukkit.jar"
                """
            ),
        )
        m = config.load_matrix(path)
        assert m.name == "cross"
        arm = m.arms[0]
        assert m.scenarios_for(arm) == ("spread100-pregen",)
        assert arm.artifact == path.parent / "../local/nukkit.jar"

    @pytest.mark.parametrize("order", ["rotate", "sequential", "seeded:0", "seeded:42"])
    def test_orders(self, order: str) -> None:
        assert config.validate_order(order) == order

    @pytest.mark.parametrize("order", ["", "seeded", "seeded:", "seeded:-1", "seeded:x", "shuffle"])
    def test_bad_orders(self, order: str) -> None:
        with pytest.raises(ValueError):
            config.validate_order(order)


# --------------------------------------------------------------------------------------------------------------------
# Machines
# --------------------------------------------------------------------------------------------------------------------


class TestMachine:
    def test_defaults(self) -> None:
        m = config.load_machine(None)
        assert isinstance(m, Machine)
        assert (m.server_cpus, m.bot_cpus, m.harness_cpus, m.priority) == ("auto", "auto", "bot", "normal")
        assert (m.port, m.jvm_heap, m.label) == (19672, "8G", "default")
        assert m.gates.foreign_cores_mean == 0.25 and m.gates.bot_cpu_frac_max == 0.85

    def test_every_key(self, project_root: Path) -> None:
        path = project_root / "tests" / "fixtures" / "machine.toml"
        assert set(config.load_toml(path)["gates"]) == {f.name for f in dataclasses.fields(Gates)}
        m = config.load_machine(path)
        assert (m.label, m.server_cpus, m.bot_cpus, m.harness_cpus) == ("lab", "0-15", "16-27", "bot")
        assert (m.priority, m.port, m.jvm_heap) == ("above_normal", 20000, "4G")
        assert m.gates == Gates(30.0, 0.5, 2.0, 0.5, 0.9)

    def test_label_defaults_to_the_file_name(self, write_file) -> None:
        m = config.load_machine(write_file("box.toml", "[runtime]\nport = 20000\n"))
        assert m.label == "box" and m.port == 20000

    def test_load_takes_a_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigError, match="no such machine profile"):
            config.load_machine("reference")

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"placement": {"server_cpus": "0-7", "bot_cpus": "6-9"}}, r"share CPUs \[6, 7\]"),
            ({"placement": {"server_cpus": "0-7", "harness_cpus": "7"}}, r"harness_cpus and server_cpus share"),
            ({"placement": {"server_cpus": "7-0"}}, r"descending CPU range"),
            ({"placement": {"bot_cpus": "bot"}}, r"bad CPU list element"),
            ({"placement": {"priority": "high"}}, r"priority: must be one of normal, above_normal"),
            ({"runtime": {"port": 80}}, r"port: must be >= 1024"),
            ({"runtime": {"jvm_heap": "8GB"}}, r"jvm_heap: invalid value"),
            ({"runtime": {"kind": "native"}}, r"runtime\.kind: unknown key"),
            ({"gates": {"foreign_cores_mean": -1}}, r"must be >= 0"),
            ({"gates": {"cpu": 1}}, r"gates\.cpu: unknown key"),
        ],
    )
    def test_rejects(self, data: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.parse_machine(data)

    def test_profile_hash(self) -> None:
        base = config.parse_machine({})
        assert base.profile_sha256 == config.parse_machine({"label": "x", "runtime": {"port": 20000}}).profile_sha256
        assert base.profile_sha256 != config.parse_machine({"runtime": {"jvm_heap": "4G"}}).profile_sha256
        raised = config.with_priority(base, "above_normal")
        assert raised.priority == "above_normal" and raised.profile_sha256 != base.profile_sha256
        assert raised.profile_sha256 == config.parse_machine({"placement": {"priority": "above_normal"}}).profile_sha256
        assert config.with_priority(base, None) is base
        with pytest.raises(ConfigError):
            config.with_priority(base, "realtime")


@pytest.mark.parametrize(
    ("spec", "cpus"),
    [
        ("0", (0,)),
        ("0-3", (0, 1, 2, 3)),
        ("0-3,8,10-11", (0, 1, 2, 3, 8, 10, 11)),
        ("3,1,1-2", (1, 2, 3)),
        (" 4 - 5 ", (4, 5)),
    ],
)
def test_parse_cpu_list(spec: str, cpus: tuple[int, ...]) -> None:
    assert config.parse_cpu_list(spec) == cpus


@pytest.mark.parametrize("spec", ["", ",", "a", "1-", "-1", "3-1", "1,,2", "auto"])
def test_parse_cpu_list_rejects(spec: str) -> None:
    with pytest.raises(ValueError):
        config.parse_cpu_list(spec)


# --------------------------------------------------------------------------------------------------------------------
# Server references
# --------------------------------------------------------------------------------------------------------------------


def test_parse_server_ref() -> None:
    assert config.parse_server_ref("bds@1.26.52.3") == ("bds", "1.26.52.3")
    for bad in ("bds", "bds@", "@1.0", "BDS@1", "bds@1@2", "bds@1+x"):
        with pytest.raises(ConfigError):
            config.parse_server_ref(bad)


def test_arm_from_ref() -> None:
    arm = config.arm_from_ref("endstone@0.11.14", variant="glibc-alloc", artifact=Path("x.whl"))
    assert arm.id == "endstone@0.11.14+glibc-alloc" and arm.artifact == Path("x.whl")
    assert config.arm_from_ref("bds@1.26.52.3").id == "bds@1.26.52.3"
    with pytest.raises(ConfigError):
        config.arm_from_ref("bds@1", variant="Bad Variant")


# --------------------------------------------------------------------------------------------------------------------
# Server files
# --------------------------------------------------------------------------------------------------------------------

SHA = "ab" * 32

ARTIFACTS = toml(
    f"""
    [artifacts]
    source = "mojang"

    [artifacts.version."1.26.52.3".windows]
    url = "https://example.invalid/bin-win/bedrock-server-1.26.52.3.zip"
    sha256 = "{SHA.upper()}"
    size = 123
    executables = ["bedrock_server.exe"]
    eula = "minecraft"

    [artifacts.version."1.26.52.3".linux]
    url = "https://example.invalid/bin-linux/bedrock-server-1.26.52.3.zip?x=1"
    sha256 = ""
    size = 0
    executables = ["bedrock_server"]
    eula = "minecraft"
    launcher_marker = "1.26.52.3"

    [artifacts.version."0.11.14"]
    bds = "1.26.52.3"
    [artifacts.version."0.11.14".any]
    url = "https://example.invalid/endstone.whl"
    sha256 = "{SHA}"
    source = "pypi"
    """
)

FEATURES = toml(
    """
    [features.range."5.44"]
    fidelity = "V1"
    "terrain.vanilla_generator" = { class = "absent", evidence = "https://example.invalid/pm", note = "own generator" }
    "chunks.client_side_generation" = { class = "absent", observed_by = "csg_effective" }
    "entity.mob_ai" = { class = "unknown" }

    [features.range."5"]
    fidelity = "V2"
    "player.physics" = { class = "partial", evidence = "https://example.invalid/physics" }

    [features.range."*"]
    fidelity = "V0"
    """
)

CONFIG = toml(
    """
    [config]
    file = "server.properties"

    [config.harness]
    online-mode = { value = "false", reason = "offline bots" }
    """
)


def server_file(name: str, body: str, head: str = "schema = 1\n") -> str:
    return head + f'server = "{name}"\n' + body


class TestServerFile:
    def test_bare_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_file) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        path = write_file("configs/bds/server.toml", server_file("bds", ARTIFACTS + FEATURES + CONFIG))
        write_file("configs/runtimes.toml", "schema = 1\n" + ARTIFACTS)
        assert config.resolve("servers", "bds") == path.resolve()
        assert config.available("servers") == ["bds"]
        assert config.load_artifacts("bds").server == "bds"
        assert config.load_features("bds").server == "bds"
        file, table = config.load_server_table("bds", "config")
        assert file == path.resolve() and table["harness"]["online-mode"]["value"] == "false"
        runtimes = config.load_artifacts(tmp_path / config.RUNTIMES_FILE)
        assert runtimes.server == "runtimes" and "0.11.14" in runtimes.versions
        with pytest.raises(ConfigError, match=r"no such server file \(available: bds\)"):
            config.load_artifacts("nukkit")

    def test_features_hash_covers_the_table_only(self, write_file) -> None:
        base = config.load_features(write_file("a/x/server.toml", server_file("x", FEATURES)))
        other = config.load_features(write_file("b/x/server.toml", "# note\n" + server_file("x", ARTIFACTS + FEATURES)))
        edited = FEATURES.replace('fidelity = "V0"', 'fidelity = "V2"')
        changed = config.load_features(write_file("c/x/server.toml", server_file("x", edited)))
        assert base.sha256 == other.sha256 != changed.sha256

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (server_file("y", FEATURES), r"server: must match the folder name 'x'"),
            (server_file("x", FEATURES, head="schema = 2\n"), r"schema: only schema = 1 is supported"),
            (server_file("x", FEATURES, head=""), r"schema: is required"),
            (server_file("x", ARTIFACTS), r"features: is required"),
            (server_file("x", FEATURES + "[parity]\n"), r"parity: unknown key"),
            (server_file("x", 'features = "none"\n'), r"features: expected a table"),
        ],
    )
    def test_rejects(self, write_file, text: str, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.load_features(write_file("x/server.toml", text))

    def test_error_names_the_table(self, write_file) -> None:
        body = ARTIFACTS.replace("size = 123", 'size = "123"')
        with pytest.raises(ConfigError, match=r'artifacts\.version\.".+"\.windows\.size: expected an integer'):
            config.load_artifacts(write_file("x/server.toml", server_file("x", body)))


#: The benchmark default of every server, from the cleanup brief.
BENCHMARK_DEFAULTS = {
    "bds": ("1.26.52.3", None, None),
    "endstone": ("0.11.14", "1.26.52.3", "3.13"),
    "levilamina": ("26.51.6", None, None),
    "pocketmine": ("5.44.3", None, None),
    "altay": ("5.44.7", None, None),
    "nukkit": ("b3caad6", None, None),
    "nukkitmot": ("9cb0aba", None, None),
    "powernukkitx": ("nightly.86f70bc", None, None),
    "allay": ("0.14.0", None, None),
    "dragonfly": ("1793369", None, None),
    "chorus": ("95ca835", None, None),
}


class TestBenchmark:
    def test_every_server_has_its_default(self) -> None:
        defaults = {b.server: b for b in config.benchmark_defaults()}
        assert set(defaults) == set(BENCHMARK_DEFAULTS) == set(config.available("servers"))
        for server, (version, bds, python) in BENCHMARK_DEFAULTS.items():
            default = defaults[server]
            assert (default.version, default.bds, default.python) == (version, bds, python), server
            assert version in config.load_artifacts(server).versions, f"{server}@{version} is not pinned"
            if bds is not None:
                assert bds in config.load_artifacts("bds").versions
        excluded = {s: b.reason for s, b in defaults.items() if not b.include}
        assert excluded == {"chorus": "its player list gives the bots decode errors"}

    def test_arm(self) -> None:
        arm = config.load_benchmark("endstone").arm
        assert (arm.id, arm.server, arm.version, arm.bds, arm.python) == (
            "endstone@0.11.14",
            "endstone",
            "0.11.14",
            "1.26.52.3",
            "3.13",
        )

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("", "benchmark: is required"),
            ('[benchmark]\nbds = "1.26.52.3"\n', r"benchmark\.version: is required"),
            ("[benchmark]\nversion = 1\n", r"benchmark\.version: expected a string"),
            ('[benchmark]\nversion = "1"\ninclude = false\n', r"benchmark\.reason: is required with include = false"),
            ('[benchmark]\nversion = "1"\nreason = "slow"\n', r"benchmark\.reason: is required with include = false"),
            ('[benchmark]\nversion = "1"\npython = "2.7"\n', r"benchmark\.python: invalid value"),
            ('[benchmark]\nversion = "1"\nvariant = "x"\n', r"benchmark\.variant: unknown key"),
        ],
    )
    def test_rejects(self, write_file, body: str, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.load_benchmark(write_file("x/server.toml", server_file("x", body)))


class TestArtifacts:
    def test_load(self, write_file) -> None:
        catalog = config.load_artifacts(write_file("configs/bds/server.toml", server_file("bds", ARTIFACTS)))
        assert catalog.server == "bds" and set(catalog.versions) == {"1.26.52.3", "0.11.14"}
        win = catalog.get("1.26.52.3", "windows")
        assert win.sha256 == SHA and win.size == 123 and win.executables == ("bedrock_server.exe",)
        assert win.eula == "minecraft" and win.name == "bedrock-server-1.26.52.3.zip" and win.source == "mojang"
        linux = catalog.get("1.26.52.3", "linux")
        assert linux.sha256 is None and linux.size is None and linux.name == "bedrock-server-1.26.52.3.zip"
        assert linux.extra == {"launcher_marker": "1.26.52.3"}
        endstone = catalog.get("0.11.14", "linux")
        assert endstone.source == "pypi" and catalog.versions["0.11.14"].meta == {"bds": "1.26.52.3"}
        with pytest.raises(KeyError, match="no artifact for platform"):
            catalog.get("1.26.52.3", "macos")
        with pytest.raises(KeyError, match="no pinned version"):
            catalog.get("9.9", "windows")

    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            ({"url": "http://example.invalid/a.zip"}, r"url: invalid value"),
            ({"url": "https://example.invalid/a.zip", "sha256": "abc"}, r"sha256 hex digest"),
            ({"url": "https://example.invalid/a.zip", "executables": ["/bin/sh"]}, r"relative path"),
            ({"url": "https://example.invalid/a.zip", "executables": ["../x"]}, r"relative path"),
            ({"url": "https://example.invalid/a.zip", "source": "ftp"}, r"source: must be one of"),
            ({"sha256": SHA}, r"url: is required"),
        ],
    )
    def test_rejects(self, entry: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.parse_artifacts({"version": {"1.0": {"windows": entry}}}, server="x")

    def test_version_needs_platforms(self) -> None:
        with pytest.raises(ConfigError, match="no platform tables"):
            config.parse_artifacts({"version": {"1": {"bds": "1"}}}, server="x")


class TestFeatures:
    def test_load_and_row(self, write_file) -> None:
        f = config.load_features(write_file("configs/pocketmine/server.toml", server_file("pocketmine", FEATURES)))
        assert f.server == "pocketmine" and len(f.ranges) == 3
        row = f.row("5.44.3")
        assert row is not None and row.prefix == "5.44" and row.fidelity is Fidelity.V1
        cell = row.cells["terrain.vanilla_generator"]
        assert cell.cls is FeatureClass.ABSENT and cell.note == "own generator"
        assert row.cells["chunks.client_side_generation"].observed_by == "csg_effective"
        assert row.cells["entity.mob_ai"].cls is FeatureClass.UNKNOWN
        assert f.row("5.44").prefix == "5.44"
        assert f.row("5.441").prefix == "5"
        assert f.row("5.1").prefix == "5"
        assert f.row("6.0").prefix == "*"

    def test_no_match(self) -> None:
        f = config.parse_features({"range": {"1": {"fidelity": "V0"}}}, server="x")
        assert f.row("2.0") is None

    @pytest.mark.parametrize(
        ("rng", "message"),
        [
            ({"fidelity": "V3"}, r"fidelity: must be one of V0, V1, V2"),
            ({}, r"fidelity: is required"),
            ({"fidelity": "V0", "player.flying": {"class": "absent", "evidence": "x"}}, r"unknown feature key"),
            ({"fidelity": "V0", "player.physics": {"class": "absent"}}, r"needs an evidence link"),
            ({"fidelity": "V0", "player.physics": {"class": "gone", "evidence": "x"}}, r"class: must be one of"),
            ({"fidelity": "V0", "player.physics": {"class": "absent", "evidence": "x", "url": "y"}}, r"url: unknown"),
            ({"fidelity": "V0", "player.physics": {"class": "absent", "observed_by": "Bad-Id"}}, r"observed_by"),
        ],
    )
    def test_rejects(self, rng: dict, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            config.parse_features({"range": {"1.0": rng}}, server="x")


# --------------------------------------------------------------------------------------------------------------------
# Locating files
# --------------------------------------------------------------------------------------------------------------------


class TestResolve:
    def test_bare_name_resolves_in_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        (tmp_path / "configs" / "scenarios").mkdir(parents=True)
        (tmp_path / "configs" / "scenarios" / "smoke.toml").write_text("[server]\ncsg = false\n")
        assert config.project_root() == tmp_path.resolve()
        assert config.resolve("scenarios", "smoke") == tmp_path.resolve() / "configs" / "scenarios" / "smoke.toml"
        assert config.available("scenarios") == ["smoke"]
        assert config.load_scenario("smoke").name == "smoke"

    def test_dotted_name_is_a_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        (tmp_path / "configs" / "matrices").mkdir(parents=True)
        (tmp_path / "configs" / "matrices" / "endstone-0.11.14.toml").write_text(MATRIX)
        assert config.load_matrix("endstone-0.11.14").name == "endstone-0.11.14"

    def test_missing_lists_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        (tmp_path / "configs" / "scenarios").mkdir(parents=True)
        (tmp_path / "configs" / "scenarios" / "smoke.toml").write_text("")
        with pytest.raises(ConfigError, match=r"no such scenario \(available: smoke\)"):
            config.resolve("scenarios", "spread100")

    @pytest.mark.parametrize("kind", ["worlds", "machines", "claims", "artifacts", "features"])
    def test_unknown_kind(self, kind: str) -> None:
        with pytest.raises(ValueError):
            config.resolve(kind, "x")

    def test_schemas_ship_with_the_package(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        assert config.schema_path("run") == Path(config.__file__).resolve().parent / "schemas" / "run.schema.json"
        assert config.load_schema("run")["properties"]["schema"]["const"] == "bedrock-bench/run/1"


def test_override_category_footnotes() -> None:
    assert {c for c in OverrideCategory if c.footnoted} == {OverrideCategory.RESCUE, OverrideCategory.NONVANILLA}


class TestReportMetrics:
    def test_project_file(self) -> None:
        metrics = config.load_report_metrics()
        assert metrics[0] == config.ReportMetric("tps", "tps_window_steady", "TPS", decimals=2)
        assert len({m.column for m in metrics}) == len(metrics)

    @pytest.mark.parametrize(
        ("entries", "error"),
        [
            ('{ column = "tps", metric = "tps_window_steady", title = "TPS", unit = "tps" }', r"unit: unknown key"),
            ('{ column = "tps", title = "TPS" }', r"metric: is required"),
            ('{ column = "a", metric = "m", title = "A" }, { column = "a", metric = "n", title = "B" }', "twice"),
        ],
    )
    def test_rejects(self, tmp_path: Path, entries: str, error: str) -> None:
        path = tmp_path / "benchmark.toml"
        path.write_text(f"[report]\nmetrics = [{entries}]\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=error):
            config.load_report_metrics(path)
