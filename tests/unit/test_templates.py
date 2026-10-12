# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from bedrock_bench.config import ConfigError
from bedrock_bench.templates import KeyChange, apply_templates, load_config_map, merge, render

VARIABLES = {"workload": {"server": {"view_distance": 10, "simulation_distance": 4}}, "run": {"port": 19132}}


def make_folder(root: Path, config: str, templates: dict[str, str]) -> Path:
    folder = root / "configs" / "srv"
    folder.mkdir(parents=True)
    (folder / "server.toml").write_text(f"schema = 1\n\n[config]\n{config}", encoding="utf-8", newline="\n")
    for name, text in templates.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return folder


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class TestRender:
    def test_variables(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, "", {"server.properties": "view-distance={{ workload.server.view_distance }}\n"})
        assert render(folder, "server.properties", VARIABLES) == "view-distance=10\n"

    def test_undefined_variable_fails(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, "", {"server.properties": "level-name={{ run.level_name }}\n"})
        with pytest.raises(ConfigError, match="level_name"):
            render(folder, "server.properties", VARIABLES)

    def test_values_are_not_escaped(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, "", {"server.properties": "motd={{ motd }}\n"})
        assert render(folder, "server.properties", {"motd": "<a & 'b'>"}) == "motd=<a & 'b'>\n"


class TestConfigMap:
    def test_load(self, tmp_path: Path) -> None:
        folder = make_folder(
            tmp_path,
            '"server.properties" = "server.properties"\n"plugins/bstats/config.toml" = "bstats.toml"\n',
            {"server.properties": "", "bstats.toml": ""},
        )
        assert load_config_map(folder) == {
            "server.properties": "server.properties",
            "plugins/bstats/config.toml": "bstats.toml",
        }

    @pytest.mark.parametrize(
        "line",
        [
            '"server.properties" = "../bds/server.properties"',
            '"server.properties" = "sub/../../server.properties"',
            '"server.properties" = "/etc/server.properties"',
            '"server.properties" = "C:/server.properties"',
            '"server.properties" = "..\\\\bds\\\\server.properties"',
            '"../server.properties" = "server.properties"',
            '"/tmp/server.properties" = "server.properties"',
        ],
    )
    def test_paths_must_stay_inside_the_folder(self, tmp_path: Path, line: str) -> None:
        (tmp_path / "configs" / "bds").mkdir(parents=True)
        (tmp_path / "configs" / "bds" / "server.properties").write_text("", encoding="utf-8")
        folder = make_folder(tmp_path, line + "\n", {"server.properties": ""})
        with pytest.raises(ConfigError, match="inside the folder"):
            load_config_map(folder)

    def test_missing_template(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, '"server.properties" = "server.properties"\n', {})
        with pytest.raises(ConfigError, match="does not exist"):
            load_config_map(folder)

    def test_unknown_format(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, '"server.ini" = "server.ini"\n', {"server.ini": ""})
        with pytest.raises(ConfigError, match="no known format"):
            load_config_map(folder)

    def test_old_key_tables_are_rejected(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, '"server.properties" = { view-distance = 10 }\n', {})
        with pytest.raises(ConfigError, match="template path"):
            load_config_map(folder)


class TestProperties:
    def test_set_and_add_keep_the_rest(self) -> None:
        existing = "# Comment\nserver-name=Dedicated Server\nview-distance=32\nlevel-seed=\n"
        text, keys = merge("properties", existing, "# why\nview-distance=10\nlevel-seed=20261010\nnew-key=x\n")
        assert text == ("# Comment\nserver-name=Dedicated Server\nview-distance=10\nlevel-seed=20261010\nnew-key=x\n")
        assert keys == [
            KeyChange("view-distance", "32", "10"),
            KeyChange("level-seed", "", "20261010"),
            KeyChange("new-key", None, "x"),
        ]

    def test_crlf_is_kept(self) -> None:
        text, _ = merge("properties", "a=1\r\nb=2\r\n", "b=3\n")
        assert text == "a=1\r\nb=3\r\n"

    def test_template_lines_must_be_key_value(self) -> None:
        with pytest.raises(ConfigError, match="key=value"):
            merge("properties", "", "view-distance 10\n")


class TestYaml:
    EXISTING = (
        "# PocketMine-MP\n"
        "settings:\n"
        "  # comment kept\n"
        "  async-workers: auto  # inline\n"
        "  enable-profiling: false\n"
        "chunk-sending:\n"
        "  per-tick: 4\n"
        "worlds:\n"
        "  - one\n"
        "  - two\n"
    )

    def test_deep_merge(self) -> None:
        rendered = (
            "settings:\n"
            "  # why\n"
            "  async-workers: 4\n"
            "chunk-sending:\n"
            "  per-tick: 8\n"
            "  spawn-radius: 2\n"
            "memory:\n"
            "  global-limit: 0\n"
        )
        text, keys = merge("yaml", self.EXISTING, rendered)
        assert text == (
            "# PocketMine-MP\n"
            "settings:\n"
            "  # comment kept\n"
            "  async-workers: 4\n"
            "  enable-profiling: false\n"
            "chunk-sending:\n"
            "  per-tick: 8\n"
            "  spawn-radius: 2\n"
            "worlds:\n"
            "  - one\n"
            "  - two\n"
            "memory:\n"
            "  global-limit: 0\n"
        )
        assert keys == [
            KeyChange("settings.async-workers", "auto", "4"),
            KeyChange("chunk-sending.per-tick", "4", "8"),
            KeyChange("chunk-sending.spawn-radius", None, "2"),
            KeyChange("memory.global-limit", None, "0"),
        ]

    def test_quoted_values(self) -> None:
        text, keys = merge("yaml", "motd: 'old'\n", 'motd: "new # not a comment"\n')
        assert text == 'motd: "new # not a comment"\n'
        assert keys == [KeyChange("motd", "old", "new # not a comment")]

    def test_empty_mapping_replaces(self) -> None:
        text, keys = merge("yaml", "aliases:\n  a: b\n  c: d\nport: 1\n", "aliases: {}\n")
        assert text == "aliases: {}\nport: 1\n"
        assert keys == [KeyChange("aliases", None, "{}")]

    def test_block_lists_in_a_template_are_rejected(self) -> None:
        with pytest.raises(ConfigError, match="block mappings"):
            merge("yaml", "", "worlds:\n  - one\n")

    def test_value_under_a_scalar_fails(self) -> None:
        with pytest.raises(ConfigError, match="is a value"):
            merge("yaml", "settings: off\n", "settings:\n  a: 1\n")


class TestToml:
    def test_deep_merge(self) -> None:
        existing = '[network]\naddress = ":19132"\n\n[world]\nname = "World"\nsimulation-distance = 8\n'
        rendered = "# why\n[world]\nsimulation-distance = 4\n\n[players]\nmax = 100\n"
        text, keys = merge("toml", existing, rendered)
        assert tomllib.loads(text) == {
            "network": {"address": ":19132"},
            "world": {"name": "World", "simulation-distance": 4},
            "players": {"max": 100},
        }
        assert keys == [KeyChange("world.simulation-distance", 8, 4), KeyChange("players.max", None, 100)]

    def test_value_types_round_trip(self) -> None:
        existing = 'a = 1.5\nb = [1, 2]\nc = 2024-01-01T00:00:00Z\nd = {x = "y"}\n"dotted.key" = "q\\"uote"\n'
        text, _ = merge("toml", existing, "e = true\n")
        assert tomllib.loads(text) == {**tomllib.loads(existing), "e": True}


class TestJson:
    def test_deep_merge(self) -> None:
        existing = json.dumps({"version": 1, "modules": {"crashLogger": {"enabled": True}, "list": [1, 2]}})
        rendered = '{"modules": {"crashLogger": {"enabled": false}, "list": [3]}, "new": "x"}'
        text, keys = merge("json", existing, rendered)
        assert json.loads(text) == {
            "version": 1,
            "modules": {"crashLogger": {"enabled": False}, "list": [3]},
            "new": "x",
        }
        assert keys == [
            KeyChange("modules.crashLogger.enabled", True, False),
            KeyChange("modules.list", [1, 2], [3]),
            KeyChange("new", None, "x"),
        ]

    def test_invalid_rendered_json(self) -> None:
        with pytest.raises(ConfigError, match="not valid JSON"):
            merge("json", "{}", "{nope")


class TestAbsentFile:
    @pytest.mark.parametrize(
        ("fmt", "rendered", "key", "after"),
        [
            ("properties", "# why\nview-distance=10\n", "view-distance", "10"),
            ("yaml", "# why\nsettings:\n  port: 19132\n", "settings.port", "19132"),
            ("toml", "# why\n[network]\nport = 19132\n", "network.port", 19132),
            ("json", '{\n  "port": 19132\n}', "port", 19132),
        ],
    )
    def test_rendered_text_is_written_as_is(self, fmt: str, rendered: str, key: str, after: object) -> None:
        text, keys = merge(fmt, None, rendered)
        assert text == rendered
        assert keys == [KeyChange(key, None, after)]


class TestApplyTemplates:
    def test_writes_every_mapped_file(self, tmp_path: Path) -> None:
        folder = make_folder(
            tmp_path,
            '"server.properties" = "server.properties"\n"plugins/bstats/config.toml" = "bstats.toml"\n',
            {
                "server.properties": "# why\nview-distance={{ workload.server.view_distance }}\n"
                "server-port={{ run.port }}\n",
                "bstats.toml": "# why\nenabled = false\n",
                "helper.py": "not a config file\n",
            },
        )
        server_dir = tmp_path / "server"
        write(server_dir / "server.properties", "server-name=x\r\nview-distance=32\r\n")

        written = apply_templates(folder, server_dir, VARIABLES)

        props = (server_dir / "server.properties").read_bytes()
        bstats = (server_dir / "plugins" / "bstats" / "config.toml").read_bytes()
        assert props == b"server-name=x\r\nview-distance=10\r\nserver-port=19132\r\n"
        assert bstats == b"# why\nenabled = false\n"
        assert not (server_dir / "helper.py").exists()
        assert [w.to_json() for w in written] == [
            {
                "path": "server.properties",
                "template": "server.properties",
                "sha256": hashlib.sha256(props).hexdigest(),
                "created": False,
                "keys": [
                    {"key": "view-distance", "before": "32", "after": "10"},
                    {"key": "server-port", "before": None, "after": "19132"},
                ],
            },
            {
                "path": "plugins/bstats/config.toml",
                "template": "bstats.toml",
                "sha256": hashlib.sha256(bstats).hexdigest(),
                "created": True,
                "keys": [{"key": "enabled", "before": None, "after": False}],
            },
        ]

    def test_error_names_template_and_target(self, tmp_path: Path) -> None:
        folder = make_folder(tmp_path, '"config.json" = "config.json"\n', {"config.json": '{"a": 1}'})
        write(tmp_path / "server" / "config.json", "[1, 2]")
        with pytest.raises(ConfigError, match=r"config\.json -> config\.json: the existing file must be a JSON object"):
            apply_templates(folder, tmp_path / "server", VARIABLES)
