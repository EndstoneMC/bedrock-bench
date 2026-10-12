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
from bedrock_bench.adapters.standalone import extract_tar, main_thread_from_jcmd, parse_helper_positions, yaml_scalars
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import LogLine

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
    (root / "servers" / "demo").mkdir(parents=True)
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
    (root / "servers" / "demo" / "server.toml").write_text(
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
