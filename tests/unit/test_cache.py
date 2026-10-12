# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from bedrock_bench import cache as cache_module
from bedrock_bench.cache import Cache, default_root, remove_tree, short_key


class TestDefaultRoot:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("BBENCH_CACHE", str(tmp_path / "c"))
        assert default_root() == tmp_path / "c"
        assert Cache().root == tmp_path / "c"

    def test_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("BBENCH_CACHE", raising=False)
        monkeypatch.setattr(cache_module.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_root() == tmp_path / "bedrock-bench"

    def test_linux_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("BBENCH_CACHE", raising=False)
        monkeypatch.setattr(cache_module.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert default_root() == tmp_path / "bedrock-bench"
        monkeypatch.delenv("XDG_CACHE_HOME")
        assert default_root() == Path.home() / ".cache" / "bedrock-bench"

    def test_tests_never_use_the_real_cache(self) -> None:
        assert os.environ["BBENCH_CACHE"]
        assert Cache().root == Path(os.environ["BBENCH_CACHE"])


def test_short_key() -> None:
    assert short_key("bds", "1.26.52.3") == short_key("bds", "1.26.52.3")
    assert short_key("bds", "1.26.52.3") != short_key("bds1", ".26.52.3")
    assert len(short_key("x")) == 12 and len(short_key("x", length=8)) == 8
    with pytest.raises(ValueError):
        short_key()


class TestPaths:
    def test_layout(self, tmp_path: Path) -> None:
        c = Cache(tmp_path)
        sha = "0" * 64
        assert c.download_path(sha, "server.zip") == tmp_path / "dl" / sha / "server.zip"
        install = c.install_dir("bds", "1.26.52.3", "windows", sha)
        assert install.parent == tmp_path / "i" and len(install.name) == 12
        assert c.venv_dir("endstone-0.11.14-cp313") == tmp_path / "venv" / "endstone-0.11.14-cp313"
        assert c.template_dir("nukkit", "1.0", "spread100-pregen").parent == tmp_path / "tpl"
        work = c.work_dir("20261012T135800Z-endstone@0.11.14-spread100-r1-3fa2")
        assert work.parent == tmp_path / "w" and len(work.name) == 10
        assert not tmp_path.joinpath("dl").exists(), "paths must not create anything"

    def test_short_install_paths(self, tmp_path: Path) -> None:
        relative = Cache(tmp_path).install_dir("powernukkitx", "2.0.0-dev.12345", "linux", "f" * 64)
        assert len(relative.relative_to(tmp_path).as_posix()) <= 16

    @pytest.mark.parametrize("sha", ["", "0" * 63, "G" * 64, "A" * 64])
    def test_download_path_rejects_bad_hash(self, tmp_path: Path, sha: str) -> None:
        with pytest.raises(ValueError):
            Cache(tmp_path).download_path(sha, "x.zip")

    @pytest.mark.parametrize("name", ["", ".", "..", "a/b.zip", "../x.zip"])
    def test_download_path_rejects_bad_name(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(ValueError):
            Cache(tmp_path).download_path("0" * 64, name)

    @pytest.mark.parametrize("name", ["", ".hidden", "a/b"])
    def test_venv_dir_rejects_bad_name(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(ValueError):
            Cache(tmp_path).venv_dir(name)


class TestStaging:
    def test_success_moves_into_place(self, tmp_path: Path) -> None:
        c = Cache(tmp_path)
        final = c.install_dir("x")
        with c.staging(final) as tmp:
            (tmp / "file.txt").write_text("ok")
            assert not final.exists()
        assert (final / "file.txt").read_text() == "ok"
        assert [p.name for p in final.parent.iterdir()] == [final.name]

    def test_failure_leaves_nothing(self, tmp_path: Path) -> None:
        c = Cache(tmp_path)
        final = c.install_dir("x")
        with pytest.raises(RuntimeError), c.staging(final) as tmp:
            (tmp / "file.txt").write_text("partial")
            raise RuntimeError("download failed")
        assert not final.exists()
        assert list(final.parent.iterdir()) == []

    def test_existing_entry_wins(self, tmp_path: Path) -> None:
        c = Cache(tmp_path)
        final = c.install_dir("x")
        final.mkdir(parents=True)
        (final / "theirs.txt").write_text("first")
        with c.staging(final) as tmp:
            (tmp / "mine.txt").write_text("second")
        assert sorted(p.name for p in final.iterdir()) == ["theirs.txt"]
        assert [p.name for p in final.parent.iterdir()] == [final.name]

    def test_staged_file(self, tmp_path: Path) -> None:
        c = Cache(tmp_path)
        final = c.download_path("a" * 64, "server.zip")
        with c.staged_file(final) as tmp:
            tmp.write_bytes(b"zip")
            assert not final.exists()
        assert final.read_bytes() == b"zip"
        with pytest.raises(OSError), c.staged_file(final) as tmp:
            tmp.write_bytes(b"truncated")
            raise OSError("connection reset")
        assert final.read_bytes() == b"zip"
        assert [p.name for p in final.parent.iterdir()] == ["server.zip"]


def test_remove_tree_handles_read_only_files(tmp_path: Path) -> None:
    tree = tmp_path / "venv"
    (tree / "lib").mkdir(parents=True)
    target = tree / "lib" / "module.py"
    target.write_text("x")
    os.chmod(target, stat.S_IREAD)
    if sys.platform != "win32":
        os.chmod(tree / "lib", stat.S_IREAD | stat.S_IEXEC)
    remove_tree(tree)
    assert not tree.exists()
    remove_tree(tree)  # absent is fine


def test_prune_staging(tmp_path: Path) -> None:
    c = Cache(tmp_path)
    old = c.installs / ".tmp-old"
    fresh = c.installs / ".tmp-fresh"
    nested = c.downloads / ("b" * 64) / ".tmp-file"
    keep = c.installs / "abcdef123456"
    for d in (old, fresh, keep):
        d.mkdir(parents=True)
    nested.parent.mkdir(parents=True)
    nested.write_text("partial")
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))
    os.utime(nested, (past, past))
    removed = c.prune_staging()
    assert sorted(removed) == sorted([old, nested])
    assert fresh.exists() and keep.exists() and not old.exists() and not nested.exists()
