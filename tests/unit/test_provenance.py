# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bedrock_bench import __version__, provenance
from bedrock_bench.provenance import sha256_file, tree_manifest, tree_sha256


def make_tree(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "server").write_bytes(b"\x7fELF fake")
    (root / "data").mkdir()
    (root / "data" / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "data" / "b.txt").write_text("beta", encoding="utf-8")
    (root / "empty").mkdir()
    return root


class TestTreeHash:
    def test_deterministic_and_location_independent(self, tmp_path: Path) -> None:
        a = make_tree(tmp_path / "a")
        b = make_tree(tmp_path / "nested" / "b")
        assert tree_sha256(a) == tree_sha256(a) == tree_sha256(b)

    def test_content_change(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "t")
        before = tree_sha256(root)
        (root / "data" / "a.txt").write_text("alphA", encoding="utf-8")
        assert tree_sha256(root) != before

    def test_rename_and_new_file(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "t")
        before = tree_sha256(root)
        (root / "data" / "a.txt").rename(root / "data" / "c.txt")
        renamed = tree_sha256(root)
        assert renamed != before
        (root / "data" / "d.txt").write_text("", encoding="utf-8")
        assert tree_sha256(root) != renamed

    def test_empty_dirs_and_caches_ignored(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "t")
        before = tree_sha256(root)
        (root / "another_empty").mkdir()
        (root / "data" / "__pycache__").mkdir()
        (root / "data" / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"cache")
        assert tree_sha256(root) == before

    def test_manifest_entries(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "t")
        entries = {e.path: e for e in tree_manifest(root)}
        assert sorted(entries) == ["bin/server", "data/a.txt", "data/b.txt"]
        assert entries["data/a.txt"].size == 5
        assert entries["data/a.txt"].sha256 == sha256_file(root / "data" / "a.txt")

    @pytest.mark.skipif(os.name != "posix", reason="exec bits are a POSIX concept")
    def test_exec_bit_counts_on_posix(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "t")
        before = tree_sha256(root)
        os.chmod(root / "bin" / "server", 0o755)
        assert tree_sha256(root) != before
        assert {e.path: e.kind for e in tree_manifest(root)}["bin/server"] == "x"

    @pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
    def test_symlinks_recorded_not_followed(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path / "t")
        os.symlink("data/a.txt", root / "link")
        entries = {e.path: e for e in tree_manifest(root)}
        assert entries["link"].kind == "l"

    def test_not_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(NotADirectoryError):
            tree_sha256(tmp_path / "missing")


class TestGit:
    def test_not_a_checkout(self, tmp_path: Path) -> None:
        assert provenance.git_describe(tmp_path) == (None, None)

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_describe_and_dirty(self, tmp_path: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "bench@example.org")
        git("config", "user.name", "bench")
        git("config", "commit.gpgsign", "false")
        for folder in ("configs", "results"):
            (tmp_path / folder).mkdir()
            (tmp_path / folder / "f.txt").write_text("1", encoding="utf-8")
        (tmp_path / "README.md").write_text("1", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "first")
        git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        describe, dirty = provenance.git_describe(tmp_path)
        assert describe is not None and describe.startswith("v0.1.0-0-g")
        assert dirty is False
        (tmp_path / "configs" / "mine.toml").write_text("1", encoding="utf-8")
        (tmp_path / "results" / "f.txt").write_text("2", encoding="utf-8")
        (tmp_path / "README.md").write_text("2", encoding="utf-8")
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "run.json").write_text("{}", encoding="utf-8")
        assert provenance.git_describe(tmp_path)[1] is False, "untracked files and changes outside the harness"
        (tmp_path / "configs" / "f.txt").write_text("2", encoding="utf-8")
        assert provenance.git_describe(tmp_path)[1] is True

    def test_harness_info(self, tmp_path: Path) -> None:
        info = provenance.harness_info("native-windows", repo=tmp_path)
        assert info["version"] == __version__
        assert info["backend"] == "native-windows"
        assert info["git"] is None and info["dirty"] is None
        assert info["python"].count(".") == 2


class TestMachine:
    def test_fingerprint_shape(self) -> None:
        fp = provenance.machine_fingerprint()
        assert set(fp) == {"fingerprint_id", "os", "kernel", "cpu", "logical", "physical", "ram_gib", "virtualized"}
        assert len(fp["fingerprint_id"]) == 16
        assert fp["logical"] >= 1 and fp["ram_gib"] > 0
        assert fp["fingerprint_id"] == provenance.machine_fingerprint()["fingerprint_id"]

    def test_fingerprint_has_no_identity(self) -> None:
        fp = provenance.machine_fingerprint()
        assert provenance.Scrubber().leaks(fp) == []

    def test_cpu_model(self) -> None:
        assert provenance.cpu_model().strip()
