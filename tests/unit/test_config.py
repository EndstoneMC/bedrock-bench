# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest

from bedrock_bench import config
from bedrock_bench.config import ConfigError
from bedrock_bench.model import FeatureClass, Fidelity, Gates, Machine, OverrideCategory


def toml(text: str) -> str:
    return textwrap.dedent(text).lstrip()


# --------------------------------------------------------------------------------------------------------------------
# Run order
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["rotate", "sequential", "seeded:0", "seeded:42"])
def test_orders(order: str) -> None:
    assert config.validate_order(order) == order


@pytest.mark.parametrize("order", ["", "seeded", "seeded:", "seeded:-1", "seeded:x", "shuffle"])
def test_bad_orders(order: str) -> None:
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
        assert base.profile_sha256 != config.parse_machine({"placement": {"priority": "above_normal"}}).profile_sha256


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
    arm = config.arm_from_ref("endstone@0.11.14", artifact=Path("x.whl"), bds="1.26.52.3", python="3.13")
    assert (arm.id, arm.artifact, arm.bds, arm.python) == ("endstone@0.11.14", Path("x.whl"), "1.26.52.3", "3.13")
    assert config.arm_from_ref("bds@1.26.52.3").id == "bds@1.26.52.3"
    with pytest.raises(ConfigError):
        config.arm_from_ref("bds@1+stock")


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
        (tmp_path / "configs" / "bds").mkdir(parents=True)
        (tmp_path / "configs" / "bds" / "server.toml").write_text("schema = 1\n")
        assert config.project_root() == tmp_path.resolve()
        assert config.resolve("servers", "bds") == tmp_path.resolve() / "configs" / "bds" / "server.toml"
        assert config.available("servers") == ["bds"]

    def test_dotted_name_is_a_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        (tmp_path / "configs" / "a.b").mkdir(parents=True)
        (tmp_path / "configs" / "a.b" / "server.toml").write_text("schema = 1\n")
        assert config.resolve("servers", "a.b") == tmp_path.resolve() / "configs" / "a.b" / "server.toml"

    def test_missing_lists_available(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
        (tmp_path / "configs" / "bds").mkdir(parents=True)
        (tmp_path / "configs" / "bds" / "server.toml").write_text("")
        with pytest.raises(ConfigError, match=r"no such server file \(available: bds\)"):
            config.resolve("servers", "nukkit")

    @pytest.mark.parametrize("kind", ["scenarios", "matrices", "worlds", "machines", "claims", "artifacts", "features"])
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
