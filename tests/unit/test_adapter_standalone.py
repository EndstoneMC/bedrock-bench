# SPDX-License-Identifier: Apache-2.0
"""Shared machinery of the PocketMine-MP, Nukkit and PowerNukkitX adapters (``adapters/standalone.py``)."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from bedrock_bench import config, fetch
from bedrock_bench.adapters import standalone
from bedrock_bench.adapters.standalone import (
    ConfigPlan,
    extract_tar,
    load_config_plan,
    main_thread_from_jcmd,
    parse_helper_positions,
    read_back,
    write_config,
    yaml_scalars,
    yaml_set,
)
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import LogLine, OverrideCategory

ROOT = Path(__file__).resolve().parents[2]

#: Block YAML in the shapes the servers ship: one-space indentation (Nukkit), comments, inline comments, a list.
SHIPPED_YAML = """# Advanced configuration file
settings:
 #Multi-language setting
 language: "eng"
 async-workers: auto # comment that goes with the value
level-settings:
 auto-tick-rate: true
 auto-tick-rate-limit: 20
experiments:
- data_driven
worlds:
 #Worlds that the server will use.
 world:
  #seed: 404
  generator: normal
 nether:
  generator: nether
"""


def line(text: str) -> LogLine:
    return LogLine(t_ns=0, epoch_ms=0, level="info", logger=None, text=text, raw=text)


# --------------------------------------------------------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------------------------------------------------------


class TestYaml:
    def test_scalars_by_path(self) -> None:
        values = yaml_scalars(SHIPPED_YAML)
        assert values["settings.language"] == "eng"
        assert values["settings.async-workers"] == "auto"
        assert values["level-settings.auto-tick-rate"] == "true"
        assert values["worlds.world.generator"] == "normal"
        assert "experiments" not in values and "worlds" not in values

    def test_quoted_values(self) -> None:
        values = yaml_scalars("a: 'it''s # not a comment'\nb: \"x # y\"\nc: plain # comment\n")
        assert values == {"a": "it's # not a comment", "b": "x # y", "c": "plain"}

    def test_replace_scalar_keeps_the_rest(self) -> None:
        text = yaml_set(SHIPPED_YAML, "level-settings.auto-tick-rate", "false")
        assert " auto-tick-rate: false\n auto-tick-rate-limit: 20\n" in text
        assert yaml_scalars(text)["level-settings.auto-tick-rate-limit"] == "20"
        assert text.count("\n") == SHIPPED_YAML.count("\n")

    def test_replace_drops_an_inline_comment(self) -> None:
        text = yaml_set(SHIPPED_YAML, "settings.async-workers", "4")
        assert " async-workers: 4\n" in text and "goes with the value" not in text

    def test_replace_mapping_with_its_children(self) -> None:
        text = yaml_set(SHIPPED_YAML, "worlds", "{}")
        assert text.endswith("worlds: {}\n")
        assert "generator" not in text and "#seed" not in text
        assert yaml_scalars(text)["worlds"] == "{}"

    def test_add_under_existing_parent_with_its_indentation(self) -> None:
        text = yaml_set(SHIPPED_YAML, "level-settings.base-tick-rate", "1")
        assert " auto-tick-rate-limit: 20\n base-tick-rate: 1\nexperiments:" in text

    def test_add_new_sections(self) -> None:
        text = yaml_set("", "network-settings.raknet.packetLimit", "1000000")
        assert text == "network-settings:\n  raknet:\n    packetLimit: 1000000\n"
        text = yaml_set(text, "settings.port", "19672")
        assert yaml_scalars(text) == {"network-settings.raknet.packetLimit": "1000000", "settings.port": "19672"}

    def test_cannot_nest_under_a_scalar(self) -> None:
        with pytest.raises(ConfigError, match="is a value"):
            yaml_set("debug: 1\n", "debug.level", "2")

    def test_crlf_is_kept(self) -> None:
        text = yaml_set("a:\r\n  b: 1\r\n", "a.b", "2")
        assert text == "a:\r\n  b: 2\r\n"

    @pytest.mark.parametrize(
        ("text", "kind", "expected"),
        [("4", "int", "4"), ("on", "bool", "true"), ("false", "bool", "false"), ("bench", "str", '"bench"'),
         ("123", "str", '"123"'), ("{}", "map", "{}")],
    )  # fmt: skip
    def test_values(self, text: str, kind: str, expected: str) -> None:
        assert standalone.yaml_value(text, kind) == expected

    @pytest.mark.parametrize(("text", "kind"), [("maybe", "bool"), ("{a: 1}", "map")])
    def test_bad_values(self, text: str, kind: str) -> None:
        with pytest.raises(ConfigError):
            standalone.yaml_value(text, kind)


# --------------------------------------------------------------------------------------------------------------------
# Config plans
# --------------------------------------------------------------------------------------------------------------------

PLAN = """schema = 1

[config.file."server.properties"]
format = "properties"

[config.file."server.properties".harness]
server-port = { value = "{port}", default = "19132", reason = "port" }
flag = { value = "on", default = "on", reason = "same as the default" }

[config.file."config.yml"]
format = "yaml"

[config.file."config.yml".parity]
"settings.async-workers" = { value = "{server_cpu_count}", type = "int", reason = "workers" }
worlds = { value = "{}", type = "map", reason = "one level" }

[config.file."config.yml".rescue]
"memory.limit" = { value = "0", type = "int", default = "1024", reason = "limit" }
"""


@pytest.fixture
def plan(tmp_path: Path) -> ConfigPlan:
    path = tmp_path / "config.toml"
    path.write_text(PLAN, encoding="utf-8")
    return load_config_plan(path)


CONTEXT = {"port": "19672", "server_cpu_count": "8"}
YAML_A = 'schema = 1\n[config.file."a"]\nformat = "yaml"\n'
PARITY_A = '[config.file."a".parity]\n'


class TestConfigPlan:
    def test_load(self, plan: ConfigPlan) -> None:
        assert plan.files() == ["server.properties", "config.yml"]
        assert [p.key for p in plan.pins_for("config.yml")] == ["settings.async-workers", "worlds", "memory.limit"]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ('schema = 2\n[config.file."a"]\nformat = "yaml"\n', "schema"),
            ('schema = 1\n[config.file."a"]\nformat = "ini"\n', "format"),
            ('schema = 1\n[config.file."../a"]\nformat = "yaml"\n', "relative"),
            (YAML_A + '[config.file."a".tuning]\nk = { value = "1", reason = "r" }\n', "unknown table"),
            (YAML_A + PARITY_A + 'k = { value = "1" }\n', "reason"),
            (YAML_A + PARITY_A + 'k = { value = "1", reason = "r", type = "list" }\n', "type"),
            (YAML_A + PARITY_A + 'k = { value = "1", reason = "r", variants = ["nope"] }\n', "optional default"),
            (YAML_A + PARITY_A + 'k = { value = "1", reason = "r" }\n'
             '[config.file."a".harness]\nk = { value = "2", reason = "r" }\n', "twice"),
            ('schema = 1\n[artifacts]\n', "config: is required"),
        ],
    )  # fmt: skip
    def test_rejects(self, tmp_path: Path, body: str, message: str) -> None:
        path = tmp_path / "config.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_config_plan(path)

    def test_properties_from_scratch(self, plan: ConfigPlan, tmp_path: Path) -> None:
        overrides, written = write_config(plan, "server.properties", tmp_path, None, CONTEXT)
        assert written == {"server-port": "19672", "flag": "on"}
        assert [(o.key, o.default, o.value, o.category) for o in overrides] == [
            ("server-port", "19132", "19672", OverrideCategory.HARNESS)
        ]
        assert (tmp_path / "server.properties").read_text(encoding="utf-8") == "server-port=19672\nflag=on\n"

    def test_yaml_on_a_shipped_file(self, plan: ConfigPlan, tmp_path: Path) -> None:
        shipped = "settings:\n  async-workers: auto\nmemory:\n  limit: 1024\nworlds:\n  world:\n    generator: normal\n"
        overrides, written = write_config(plan, "config.yml", tmp_path, shipped, CONTEXT)
        by_key = {o.key: o for o in overrides}
        assert by_key["settings.async-workers"].default == "auto" and by_key["settings.async-workers"].value == "8"
        assert by_key["worlds"].default == "world" and by_key["worlds"].value == "{}"
        assert by_key["memory.limit"].category is OverrideCategory.RESCUE
        text = (tmp_path / "config.yml").read_text(encoding="utf-8")
        assert yaml_scalars(text) == {"settings.async-workers": "8", "memory.limit": "0", "worlds": "{}"}
        assert written == {"settings.async-workers": "8", "worlds": "{}", "memory.limit": "0"}

    def test_read_back(self, plan: ConfigPlan, tmp_path: Path) -> None:
        _, props = write_config(plan, "server.properties", tmp_path, None, CONTEXT)
        _, yml = write_config(plan, "config.yml", tmp_path, None, CONTEXT)
        written = {"server.properties": props, "config.yml": yml}
        assert read_back(plan, tmp_path, written) == []
        # A server that rewrites its files in its own style (booleans as true, strings unquoted) still reads back.
        (tmp_path / "server.properties").write_text("#generated\nflag=true\nserver-port=19672\nmotd=x\n", "utf-8")
        assert read_back(plan, tmp_path, written) == []
        (tmp_path / "config.yml").write_text("settings:\n  async-workers: 4\nworlds: {}\n", encoding="utf-8")
        problems = read_back(plan, tmp_path, written)
        assert any("settings.async-workers was written as '8' and read back as '4'" in p for p in problems)
        assert any("memory.limit" in p and "None" in p for p in problems)
        (tmp_path / "server.properties").unlink()
        assert "server.properties is missing after the run" in read_back(plan, tmp_path, written)

    def test_bad_placeholder(self, plan: ConfigPlan, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="placeholder"):
            write_config(plan, "server.properties", tmp_path, None, {})

    @pytest.mark.parametrize("server", ["pocketmine", "nukkit", "powernukkitx"])
    def test_committed_plans_render(self, server: str, tmp_path: Path) -> None:
        from bedrock_bench import adapters

        cls = adapters.get(server)
        plan = load_config_plan(ROOT / cls.config_template)
        context = {key: "1" for key in ("port", "port_v6", "view_distance", "tick_distance", "max_players")}
        context |= {"difficulty_id": "1", "gamemode_id": "0", "gamemode_upper": "SURVIVAL", "level_name": "bench"}
        context |= {"seed": "20261010", "autosave_ticks": "6000", "save_player_data": "true", "server_cpu_count": "8"}
        for file in plan.files():
            write_config(plan, file, tmp_path, None, context)


# --------------------------------------------------------------------------------------------------------------------
# Archives and downloads
# --------------------------------------------------------------------------------------------------------------------


def make_tar(path: Path, members: list[tuple[str, bytes | None, str | None]]) -> Path:
    """``(name, data or None for a directory, symlink target or None)``."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data, target in members:
            info = tarfile.TarInfo(name)
            if target is not None:
                info.type, info.linkname = tarfile.SYMTYPE, target
                tf.addfile(info)
            elif data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(info)
            else:
                info.size, info.mode = len(data), 0o644
                tf.addfile(info, io.BytesIO(data))
    return path


class TestTar:
    def test_extracts_and_marks_executables(self, tmp_path: Path) -> None:
        archive = make_tar(tmp_path / "rt.tar.gz", [("bin", None, None), ("bin/php", b"#!php", None)])
        extract_tar(archive, tmp_path / "out", executables=["bin/php"])
        php = tmp_path / "out" / "bin" / "php"
        assert php.read_bytes() == b"#!php"
        if os.name == "posix":
            assert php.stat().st_mode & 0o111

    @pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")
    def test_relative_symlinks_inside_are_kept(self, tmp_path: Path) -> None:
        archive = make_tar(tmp_path / "rt.tar.gz", [("lib/libz.so.1", b"z", None), ("lib/libz.so", None, "libz.so.1")])
        extract_tar(archive, tmp_path / "out")
        assert (tmp_path / "out" / "lib" / "libz.so").resolve().name == "libz.so.1"

    @pytest.mark.parametrize(
        "members",
        [
            [("../evil", b"x", None)],
            [("/abs", b"x", None)],
            [("lib/link", None, "../../outside")],
            [("lib/link", None, "/etc/passwd")],
        ],
    )
    def test_refuses_escapes(self, tmp_path: Path, members: list[tuple[str, bytes | None, str | None]]) -> None:
        archive = make_tar(tmp_path / "rt.tar.gz", members)
        with pytest.raises(fetch.FetchError, match="escape|outside"):
            extract_tar(archive, tmp_path / "out")

    def test_missing_executable(self, tmp_path: Path) -> None:
        archive = make_tar(tmp_path / "rt.tar.gz", [("bin/php", b"x", None)])
        with pytest.raises(fetch.FetchError, match="missing"):
            extract_tar(archive, tmp_path / "out", executables=["bin/php7/bin/php"])

    def test_extract_archive_dispatch(self, tmp_path: Path) -> None:
        with zipfile.ZipFile(tmp_path / "rt.zip", "w") as zf:
            zf.writestr("bin/php/php.exe", b"exe")
        standalone.extract_archive(tmp_path / "rt.zip", tmp_path / "zip")
        assert (tmp_path / "zip" / "bin" / "php" / "php.exe").is_file()
        (tmp_path / "rt.7z").write_bytes(b"")
        with pytest.raises(fetch.FetchError, match="unknown archive"):
            standalone.extract_archive(tmp_path / "rt.7z", tmp_path / "7z")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with pinned artifacts and an artifact directory holding them."""
    root = tmp_path / "project"
    (root / "configs" / "demo").mkdir(parents=True)
    store = tmp_path / "store"
    store.mkdir()
    runtime = make_tar(store / "rt-linux.tar.gz", [("rt/bin/tool", b"tool", None)])
    with zipfile.ZipFile(store / "rt-windows.zip", "w") as zf:
        zf.writestr("rt/bin/tool.exe", b"tool")
    release = store / "server.jar"
    release.write_bytes(b"jar bytes")
    rt_linux, rt_windows = runtime.read_bytes(), (store / "rt-windows.zip").read_bytes()
    (root / config.RUNTIMES_FILE).write_text(
        f"""schema = 1
[artifacts.version."rt-1".linux]
url = "https://example.invalid/rt-linux.tar.gz"
sha256 = "{sha(rt_linux)}"
size = {len(rt_linux)}
executables = ["rt/bin/tool"]
tool = "rt/bin/tool"
[artifacts.version."rt-1".windows]
url = "https://example.invalid/rt-windows.zip"
sha256 = "{sha(rt_windows)}"
size = {len(rt_windows)}
tool = "rt/bin/tool.exe"
""",
        encoding="utf-8",
    )
    (root / "configs" / "demo" / "server.toml").write_text(
        f"""schema = 1
server = "demo"
[artifacts.version."1.0"]
runtime = "rt-1"
[artifacts.version."1.0".any]
url = "https://example.invalid/server.jar"
sha256 = "{sha(b"jar bytes")}"
size = 9
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(config.ENV_ROOT, str(root))
    monkeypatch.setenv(fetch.ENV_ARTIFACT_DIR, str(store))
    return root


class TestFetch:
    @pytest.mark.parametrize("platform", ["linux", "windows"])
    def test_runtime(self, project: Path, tmp_path: Path, platform: str) -> None:
        runtime = standalone.fetch_runtime(Cache(tmp_path / "cache"), "rt-1", platform)
        assert runtime.path("tool").read_bytes() == b"tool"
        assert runtime.artifact.name.startswith("rt-")
        again = standalone.fetch_runtime(Cache(tmp_path / "cache"), "rt-1", platform)
        assert again.tree_sha256 == runtime.tree_sha256 and again.root == runtime.root
        with pytest.raises(ConfigError, match="no 'javac' entry"):
            runtime.path("javac")

    def test_unpinned_runtime(self, project: Path, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no pinned version"):
            standalone.fetch_runtime(Cache(tmp_path / "cache"), "rt-2", "linux")

    def test_release(self, project: Path, tmp_path: Path) -> None:
        path, ref, entry = standalone.fetch_release(Cache(tmp_path / "cache"), "demo", "1.0")
        assert path.read_bytes() == b"jar bytes" and ref.sha256 == sha(b"jar bytes")
        assert entry is not None and entry.meta["runtime"] == "rt-1"
        with pytest.raises(ConfigError, match="not pinned"):
            standalone.fetch_release(Cache(tmp_path / "cache"), "demo", "2.0")

    def test_local_release(self, project: Path, tmp_path: Path) -> None:
        local = tmp_path / "custom.jar"
        local.write_bytes(b"custom")
        path, ref, entry = standalone.fetch_release(Cache(tmp_path / "cache"), "demo", "2.0", artifact=local)
        assert ref.source == "local" and path.read_bytes() == b"custom" and entry is None


def test_deterministic_zip(tmp_path: Path) -> None:
    files = {"b.class": b"b", "plugin.yml": b"name: x\n", "a/A.class": b"a"}
    standalone.deterministic_zip(tmp_path / "one.jar", files)
    standalone.deterministic_zip(tmp_path / "two.jar", dict(reversed(list(files.items()))))
    assert (tmp_path / "one.jar").read_bytes() == (tmp_path / "two.jar").read_bytes()
    with zipfile.ZipFile(tmp_path / "one.jar") as zf:
        assert zf.namelist() == sorted(files)


# --------------------------------------------------------------------------------------------------------------------
# Helper output and the JVM
# --------------------------------------------------------------------------------------------------------------------


class TestHelperPositions:
    def test_complete_reply(self) -> None:
        lines = [line("noise"), line("positions: 2"), line("position: a00 1280.50 300.00 1280.50 0"),
                 line("Gametime is 5"), line("position: a01 -1.00 64.25 1920.00 1")]  # fmt: skip
        positions = parse_helper_positions(lines)
        assert positions is not None
        assert [(p.name, p.x, p.y, p.z, p.dim) for p in positions] == [
            ("a00", 1280.5, 300.0, 1280.5, 0),
            ("a01", -1.0, 64.25, 1920.0, 1),
        ]

    def test_incomplete_and_missing_replies_are_unknown(self) -> None:
        assert parse_helper_positions([line("positions: 2"), line("position: a00 1 2 3 0")]) is None
        assert parse_helper_positions([line("Gametime is 5")]) is None
        assert parse_helper_positions([]) is None

    def test_no_players(self) -> None:
        assert parse_helper_positions([line("positions: 0")]) == []


class TestJcmd:
    def test_jdk21_format(self) -> None:
        text = (
            "12345:\n2026-10-12 12:00:00\nFull thread dump OpenJDK 64-Bit Server VM (21.0.12.1+1 mixed mode):\n\n"
            '"main" #1 [9876] prio=5 os_prio=0 cpu=81234.12ms elapsed=420.50s tid=0x00007f nid=9876 runnable  [0x7]\n'
            '"Reference Handler" #9 [9880] daemon prio=10 os_prio=0 tid=0x00007f nid=9880 waiting on condition\n'
        )
        assert main_thread_from_jcmd(text) == 9876

    def test_old_hex_format(self) -> None:
        assert main_thread_from_jcmd('"main" #1 prio=5 os_prio=0 tid=0x7f nid=0x2694 runnable\n') == 0x2694

    def test_no_main_thread(self) -> None:
        assert main_thread_from_jcmd('"mainly" #1 prio=5 nid=0x1 runnable\n') is None
        assert main_thread_from_jcmd("") is None

    @pytest.mark.parametrize(("heap", "mib"), [("8G", 8192), ("512M", 512)])
    def test_heap(self, heap: str, mib: int) -> None:
        assert standalone.heap_mib(heap) == mib

    def test_bad_heap(self) -> None:
        with pytest.raises(ConfigError):
            standalone.heap_mib("8 GB")

    def test_java_release(self, tmp_path: Path) -> None:
        (tmp_path / "release").write_text('IMPLEMENTOR="Eclipse Adoptium"\nJAVA_RUNTIME_VERSION="21.0.12.1+1"\n')
        assert standalone.java_release(tmp_path) == "21.0.12.1+1"
        assert standalone.java_release(tmp_path / "missing") is None
