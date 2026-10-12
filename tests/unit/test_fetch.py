# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import zipfile
from pathlib import Path

import pytest

from bedrock_bench import fetch
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.fetch import EulaNotAccepted, FetchError, FetchOptions
from bedrock_bench.model import ArtifactSpec, ExitCode
from bedrock_bench.provenance import tree_sha256

POSIX = os.name == "posix"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def served(tmp_path: Path, name: str, data: bytes) -> str:
    """A file:// URL for a file with ``data`` (urllib reads file URLs like any other)."""
    source = tmp_path / "upstream" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    return source.as_uri()


def spec(
    url: str, data: bytes, *, name: str = "server.zip", eula: str | None = None, **changes: object
) -> ArtifactSpec:
    fields: dict[str, object] = {
        "name": name,
        "url": url,
        "sha256": sha(data),
        "size": len(data),
        "eula": eula,
        "source": "web",
    }
    fields.update(changes)
    return ArtifactSpec(**fields)  # type: ignore[arg-type]


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


@pytest.fixture
def quiet() -> FetchOptions:
    return FetchOptions(report=lambda _: None)


class TestObtain:
    def test_download_verify_and_cache(self, tmp_path: Path, cache: Cache, quiet: FetchOptions) -> None:
        data = b"server bytes"
        s = spec(served(tmp_path, "server.zip", data), data)
        path, ref = fetch.obtain(s, cache, quiet)
        assert path == cache.download_path(sha(data), "server.zip")
        assert path.read_bytes() == data
        assert (ref.name, ref.sha256, ref.size, ref.source) == ("server.zip", sha(data), len(data), "web")
        (tmp_path / "upstream" / "server.zip").unlink()
        assert fetch.obtain(s, cache, quiet)[0] == path, "second fetch must come from the cache"

    def test_hash_mismatch_leaves_nothing(self, tmp_path: Path, cache: Cache, quiet: FetchOptions) -> None:
        data = b"server bytes"
        s = spec(served(tmp_path, "server.zip", data), data, sha256="0" * 64, size=None)
        with pytest.raises(FetchError, match="sha256"):
            fetch.obtain(s, cache, quiet)
        assert not cache.download_path("0" * 64, "server.zip").exists()
        assert not any(p.is_file() for p in cache.downloads.rglob("*"))

    def test_size_mismatch(self, tmp_path: Path, cache: Cache, quiet: FetchOptions) -> None:
        data = b"server bytes"
        s = spec(served(tmp_path, "server.zip", data), data, size=len(data) + 1)
        with pytest.raises(FetchError, match="size"):
            fetch.obtain(s, cache, quiet)

    def test_missing_upstream(
        self, tmp_path: Path, cache: Cache, quiet: FetchOptions, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(fetch.time, "sleep", sleeps.append)
        url = (tmp_path / "nothing.zip").as_uri()
        with pytest.raises(FetchError, match="download"):
            fetch.download(url, tmp_path / "out.zip", sha256="0" * 64, attempts=1)
        assert sleeps == []
        s = spec(url, b"x")
        with pytest.raises(FetchError):
            fetch.obtain(s, cache, quiet)
        assert sleeps == [2.0, 4.0], "transient failures are retried with backoff"

    def test_unpinned_artifact_refused(self, tmp_path: Path, cache: Cache, quiet: FetchOptions) -> None:
        s = spec(served(tmp_path, "server.zip", b"x"), b"x", sha256=None)
        with pytest.raises(ConfigError, match="not pinned"):
            fetch.obtain(s, cache, quiet)

    def test_corrupted_cache_entry_detected(self, tmp_path: Path, cache: Cache, quiet: FetchOptions) -> None:
        data = b"server bytes"
        s = spec(served(tmp_path, "server.zip", data), data)
        path, _ = fetch.obtain(s, cache, quiet)
        path.write_bytes(b"tampered!!!!")
        with pytest.raises(FetchError):
            fetch.obtain(s, cache, quiet)


class TestEula:
    def test_download_needs_acceptance(self, tmp_path: Path, cache: Cache, quiet: FetchOptions) -> None:
        data = b"mojang bytes"
        s = spec(served(tmp_path, "bds.zip", data), data, name="bds.zip", eula="minecraft")
        with pytest.raises(EulaNotAccepted, match="minecraft.net/eula"):
            fetch.obtain(s, cache, quiet)
        assert not cache.downloads.exists() or not any(cache.downloads.rglob("*.zip"))
        accepted = FetchOptions(accept_eula=True, report=lambda _: None)
        assert fetch.obtain(s, cache, accepted)[0].is_file()
        assert fetch.obtain(s, cache, quiet)[0].is_file(), "a cached copy needs no acceptance"

    def test_environment_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(fetch.ENV_ACCEPT_EULA, "1")
        assert FetchOptions.from_env().accept_eula is True
        monkeypatch.setenv(fetch.ENV_ACCEPT_EULA, "0")
        assert FetchOptions.from_env().accept_eula is False

    def test_context_options(self, tmp_path: Path, cache: Cache, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(fetch.ENV_ACCEPT_EULA, raising=False)
        data = b"mojang bytes"
        s = spec(served(tmp_path, "bds.zip", data), data, name="bds.zip", eula="minecraft")
        with fetch.using(FetchOptions(accept_eula=True, report=lambda _: None)):
            assert fetch.current_options().accept_eula
            fetch.obtain(s, cache)
        assert not fetch.current_options().accept_eula

    def test_artifact_dir_needs_no_acceptance(self, tmp_path: Path, cache: Cache) -> None:
        data = b"mojang bytes"
        s = spec((tmp_path / "unreachable.zip").as_uri(), data, name="bds.zip", eula="minecraft")
        supplied = tmp_path / "supplied"
        supplied.mkdir()
        (supplied / "bds.zip").write_bytes(data)
        messages: list[str] = []
        opts = FetchOptions(artifact_dir=supplied, report=messages.append)
        path, _ = fetch.obtain(s, cache, opts)
        assert path.read_bytes() == data and "artifact directory" in messages[0]

    def test_artifact_dir_wrong_hash(self, tmp_path: Path, cache: Cache) -> None:
        s = spec((tmp_path / "unreachable.zip").as_uri(), b"right", name="bds.zip")
        supplied = tmp_path / "supplied"
        supplied.mkdir()
        (supplied / "bds.zip").write_bytes(b"wrong")
        with pytest.raises(FetchError):
            fetch.obtain(s, cache, FetchOptions(artifact_dir=supplied, report=lambda _: None))


class TestLocal:
    def test_adopt_local(self, tmp_path: Path, cache: Cache) -> None:
        wheel = tmp_path / "endstone-0.11.15.dev1-cp313-cp313-win_amd64.whl"
        wheel.write_bytes(b"wheel")
        path, ref = fetch.adopt_local(wheel, cache)
        assert path.name == wheel.name and ref.source == "local" and ref.url is None
        wheel.write_bytes(b"edited later")
        assert path.read_bytes() == b"wheel"

    def test_missing_local(self, tmp_path: Path, cache: Cache) -> None:
        with pytest.raises(ConfigError):
            fetch.adopt_local(tmp_path / "nope.zip", cache)


def make_zip(path: Path, members: dict[str, tuple[bytes, int | None]]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, (data, mode) in members.items():
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = (stat.S_IFREG | mode) << 16
            zf.writestr(info, data)
    return path


class TestExtract:
    def test_extract_with_modes(self, tmp_path: Path) -> None:
        archive = make_zip(
            tmp_path / "a.zip",
            {"bedrock_server": (b"bin", None), "lib/helper.sh": (b"#!/bin/sh", 0o755), "data.txt": (b"d", 0o600)},
        )
        dest = tmp_path / "out"
        fetch.extract_zip(archive, dest, executables=["bedrock_server"])
        assert (dest / "lib" / "helper.sh").read_bytes() == b"#!/bin/sh"
        if POSIX:
            assert os.stat(dest / "bedrock_server").st_mode & 0o111
            assert os.stat(dest / "lib" / "helper.sh").st_mode & 0o111
            assert not os.stat(dest / "data.txt").st_mode & 0o111

    def test_missing_executable(self, tmp_path: Path) -> None:
        archive = make_zip(tmp_path / "a.zip", {"x": (b"x", None)})
        with pytest.raises(FetchError, match="bedrock_server"):
            fetch.extract_zip(archive, tmp_path / "out", executables=["bedrock_server"])

    @pytest.mark.skipif(os.name != "nt", reason="the path limit is a Windows constraint")
    def test_windows_path_limit(self, tmp_path: Path) -> None:
        deep = "/".join(["d" * 50] * 5) + "/file.nbt"
        archive = make_zip(tmp_path / "a.zip", {deep: (b"x", None)})
        with pytest.raises(FetchError, match="BBENCH_CACHE"):
            fetch.extract_zip(archive, tmp_path / "out")
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "a/../../evil.txt", "C:/evil.txt"])
    def test_zip_slip_refused(self, tmp_path: Path, name: str) -> None:
        archive = make_zip(tmp_path / "a.zip", {name: (b"x", None)})
        with pytest.raises(FetchError, match="escape"):
            fetch.extract_zip(archive, tmp_path / "out")


class TestInstalls:
    def test_ensure_install_builds_once_and_verifies(self, cache: Cache) -> None:
        calls: list[Path] = []

        def build(tmp: Path) -> None:
            calls.append(tmp)
            (tmp / "a.txt").write_text("a", encoding="utf-8")

        final = cache.install_dir("test", "1")
        first = fetch.ensure_install(cache, final, build)
        assert first == tree_sha256(final)
        assert fetch.ensure_install(cache, final, build) == first
        assert len(calls) == 1
        assert not (final / "a.txt").stat().st_mode & 0o222, "installs are read-only"

    def test_modified_install_refused(self, cache: Cache) -> None:
        final = cache.install_dir("test", "2")
        fetch.ensure_install(cache, final, lambda tmp: (tmp / "a.txt").write_text("a", encoding="utf-8"))
        target = final / "a.txt"
        os.chmod(target, stat.S_IWUSR | stat.S_IRUSR)
        target.write_text("b", encoding="utf-8")
        with pytest.raises(FetchError, match="changed after it was installed"):
            fetch.ensure_install(cache, final, lambda tmp: None)

    def test_failed_build_leaves_nothing(self, cache: Cache) -> None:
        final = cache.install_dir("test", "3")

        def build(tmp: Path) -> None:
            (tmp / "partial").write_text("x", encoding="utf-8")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            fetch.ensure_install(cache, final, build)
        assert not final.exists()
        assert list(final.parent.iterdir()) == []

    def test_copy_install_is_writable(self, cache: Cache, tmp_path: Path) -> None:
        final = cache.install_dir("test", "4")

        def build(tmp: Path) -> None:
            (tmp / "sub").mkdir()
            (tmp / "sub" / "f.txt").write_text("f", encoding="utf-8")
            (tmp / "server.properties").write_text("a=b\n", encoding="utf-8")

        fetch.ensure_install(cache, final, build)
        work = tmp_path / "work"
        fetch.copy_install(final, work)
        (work / "server.properties").write_text("a=c\n", encoding="utf-8")
        (work / "sub" / "new.txt").write_text("n", encoding="utf-8")
        assert (final / "server.properties").read_text(encoding="utf-8") == "a=b\n"
        with pytest.raises(FileExistsError):
            fetch.copy_install(final, work)

    def test_read_only_install_removable(self, cache: Cache) -> None:
        from bedrock_bench.cache import remove_tree

        final = cache.install_dir("test", "5")
        fetch.ensure_install(cache, final, lambda tmp: (tmp / "a.txt").write_text("a", encoding="utf-8"))
        remove_tree(final)
        assert not final.exists()


class TestCli:
    def test_a_tag_twice_is_a_config_error(self) -> None:
        args = argparse.Namespace(tags=["bds@1.26.52.3", "bds@1.26.52.3"], accept_eula=False, artifact_dir=None)
        with pytest.raises(ConfigError, match="twice"):
            fetch.fetch_cli(args)

    def test_no_tag_fetches_every_default(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from bedrock_bench import adapters, runner
        from bedrock_bench.adapters.base import ServerAdapter

        fetched: list[str] = []

        def fake_fetch(self: ServerAdapter, cache: Cache) -> object:
            fetched.append(self.arm.id)
            raise FetchError("not here")

        for name in adapters.names():
            monkeypatch.setattr(adapters.get(name), "fetch", fake_fetch)
        monkeypatch.setattr(runner, "host_os", lambda: "windows")
        args = argparse.Namespace(tags=[], accept_eula=False, artifact_dir=None)
        assert fetch.fetch_cli(args) == ExitCode.ENV_GATE
        assert "endstone@0.11.14" in fetched and "levilamina@26.51.6" in fetched
        assert not any(tag.startswith("chorus@") for tag in fetched)
        assert "skipping chorus@95ca835" in capsys.readouterr().err

    def test_unknown_server_is_a_config_error(self) -> None:
        args = argparse.Namespace(tags=["nosuchserver@1.0"], accept_eula=False, artifact_dir=None)
        with pytest.raises(ConfigError, match="unknown server"):
            fetch.fetch_cli(args)

    def test_fetch_failure_exit_code(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from bedrock_bench.adapters import bds

        def failing_fetch(self: bds.BdsAdapter, cache: Cache) -> object:
            raise FetchError("simulated failure")

        monkeypatch.setattr(bds.BdsAdapter, "fetch", failing_fetch)
        args = argparse.Namespace(tags=["bds@1.26.52.3"], accept_eula=False, artifact_dir=None)
        assert fetch.fetch_cli(args) == ExitCode.ENV_GATE
        assert "bds@1.26.52.3: simulated failure" in capsys.readouterr().err
