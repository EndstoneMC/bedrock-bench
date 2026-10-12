# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bedrock_bench import adapters, config, fetch
from bedrock_bench.adapters import levilamina as levilamina_module
from bedrock_bench.adapters.bdsfamily import Properties, fetch_bds
from bedrock_bench.adapters.levilamina import (
    BASE_PACKAGES,
    LEVILAMINA_CHANGES,
    LEVILAMINA_CONFIG,
    LEVILAMINA_DIALECT,
    LEVIOPTIMIZE,
    LEVIOPTIMIZE_ALL,
    LEVIOPTIMIZE_ALL_REASONS,
    LEVIOPTIMIZE_CONFIG,
    LEVIOPTIMIZE_MANIFEST,
    LEVIOPTIMIZE_TEMPLATES,
    MOD_EXE,
    PRELOADER_CONFIG,
    LeviLaminaAdapter,
    Package,
    flatten,
    json_diff,
    load_template,
    packages_of,
    place_package,
    run_peeditor,
)
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.fetch import FetchError
from bedrock_bench.model import (
    FEATURE_KEYS,
    Arm,
    ArtifactRef,
    ArtifactSpec,
    ArtifactVersion,
    FeatureClass,
    Install,
    Layout,
    OverrideCategory,
    Placement,
    RunContext,
)
from bedrock_bench.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"
VERSION = "26.51.6"
BDS_VERSION = "1.26.51.1"

SHIPPED_PROPERTIES = "server-name=Dedicated Server\nonline-mode=true\nview-distance=32\nlevel-name=Bedrock level\n"


def arm(variant: str | None = None, **changes: Any) -> Arm:
    fields: dict[str, Any] = {
        "id": f"levilamina@{VERSION}" + (f"+{variant}" if variant else ""),
        "server": "levilamina",
        "version": VERSION,
        "variant": variant,
        "bds": BDS_VERSION,
    }
    fields.update(changes)
    return Arm(**fields)


def make_ctx(tmp_path: Path, run_arm: Arm, *, csg: bool = False) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-levilamina-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=run_arm,
        scenario=config.parse_scenario({"server": {"csg": csg}}, name="test"),
        machine=config.default_machine(),
        placement=Placement((0, 1), (2, 3), (2, 3), "normal", "physical"),
    )


def bds_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("bedrock_server.exe", b"server binary")
        zf.writestr("server.properties", SHIPPED_PROPERTIES)
        zf.writestr("behavior_packs/vanilla/manifest.json", "{}")
    return path


def package_zip(package: Package, path: Path) -> Path:
    """A stand-in for a package's release asset: every placed path exists, plus a file nothing places."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("README.md", "not placed")
        for src in package.place:
            if src.endswith("/"):
                zf.writestr(f"{src}{package.name}.dll", f"{package.name} library")
                zf.writestr(f"{src}manifest.json", json.dumps({"name": package.name, "version": package.release}))
            else:
                zf.writestr(src, f"{package.name} file")
    return path


def pinned_packages() -> dict[str, Package]:
    catalog = config.load_artifacts("levilamina")
    return packages_of(catalog.versions[VERSION], catalog.path)


class FakeDownloads:
    """Records PeEditor runs and the packages fetched."""

    def __init__(self) -> None:
        self.peeditor_runs: list[Path] = []
        self.obtained: list[str] = []


@pytest.fixture
def downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDownloads:
    """Replace every download with a local stand-in and PeEditor with a file rename; pretend to be on Windows."""
    state = FakeDownloads()
    assets = tmp_path / "assets"
    assets.mkdir()
    bds = bds_zip(assets / "bds.zip")
    by_url = {p.spec.url: p for p in pinned_packages().values()}

    def fake_fetch_bds(cache: Cache, version: str, platform: str, *, artifact: Path | None = None) -> Install:
        assert (version, platform) == (BDS_VERSION, "windows")
        return fetch_bds(cache, version, platform, artifact=bds)

    def fake_obtain(spec: ArtifactSpec, cache: Cache) -> tuple[Path, ArtifactRef]:
        package = by_url[spec.url]
        state.obtained.append(package.name)
        path = package_zip(package, assets / spec.name)
        ref = ArtifactRef(spec.name, spec.url, sha256_file(path), path.stat().st_size, spec.source)
        return path, ref

    def fake_peeditor(server_dir: Path) -> None:
        state.peeditor_runs.append(server_dir)
        original = server_dir / "bedrock_server.exe"
        (server_dir / MOD_EXE).write_bytes(b"loads PreLoader: " + original.read_bytes())
        original.rename(server_dir / "bedrock_server.exe.bak")

    monkeypatch.setattr(levilamina_module, "host_os", lambda: "windows")
    monkeypatch.setattr(levilamina_module, "fetch_bds", fake_fetch_bds)
    monkeypatch.setattr(levilamina_module.fetch, "obtain", fake_obtain)
    monkeypatch.setattr(levilamina_module, "run_peeditor", fake_peeditor)
    return state


def fetch_and_prepare(tmp_path: Path, variant: str | None = None) -> tuple[LeviLaminaAdapter, Install, Layout]:
    run_arm = arm(variant)
    adapter = LeviLaminaAdapter(run_arm, config.default_machine())
    install = adapter.fetch(Cache(tmp_path / "cache"))
    layout = adapter.prepare(install, make_ctx(tmp_path, run_arm))
    return adapter, install, layout


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("levilamina") is LeviLaminaAdapter
        assert LeviLaminaAdapter.class_problems() == []
        assert LeviLaminaAdapter.features_file == "configs/servers/levilamina/server.toml"
        assert LeviLaminaAdapter.platforms == frozenset({"windows"})
        assert LeviLaminaAdapter.protocol == 2193
        assert LeviLaminaAdapter.tick_thread_rule == "lowest_tid_name:MC_SERVER"
        assert "logs/crash" in LeviLaminaAdapter.crash_paths
        assert set(LeviLaminaAdapter.variants) == {LEVIOPTIMIZE, LEVIOPTIMIZE_ALL}

    def test_thread_groups(self) -> None:
        groups = {label: re.compile(pattern) for label, pattern in LeviLaminaAdapter.thread_groups.items()}
        assert groups["levilamina_pool"].match("ll_default_thread_pool[12]")
        assert groups["levilamina_pool"].match("ll_default_thread_pool[sch]")
        assert groups["levilamina_logger"].match("logger[0]")
        assert groups["streaming_pool"].match("Streaming Pool(3)")

    def test_dialect_samples(self) -> None:
        with open(LOGS / "levilamina.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        assert {sample["role"] for sample in samples} >= {"ready", "version", "player_in_world", "tick", "benign"}
        for sample in samples:
            level, _, text = LEVILAMINA_DIALECT.split(sample["line"])
            role = sample["role"]
            if role == "ready":
                assert LEVILAMINA_DIALECT.is_ready(text)
            elif role == "version":
                assert LEVILAMINA_DIALECT.parse_version(text) == sample["value"]
            elif role == "player_in_world":
                assert LEVILAMINA_DIALECT.player_entered(text) == sample["value"]
            elif role == "tick":
                assert LEVILAMINA_DIALECT.parse_tick(text) == sample["value"]
            elif role == "level":
                assert level == sample["value"], sample["line"]
            elif role == "strip":
                assert text == sample["value"], sample["line"]
            elif role == "benign":
                assert level == "error" and LEVILAMINA_DIALECT.is_benign(text), sample["line"]
            elif role == "not_benign":
                assert level == "error" and not LEVILAMINA_DIALECT.is_benign(text)

    def test_session_log(self) -> None:
        lines = (LOGS / "levilamina-session.log").read_text(encoding="utf-8").splitlines()
        split = [LEVILAMINA_DIALECT.split(line) for line in lines]
        assert [text for _, _, text in split if LEVILAMINA_DIALECT.is_ready(text)] == [
            'Server started in (5.0s)! For help, type "help" or "?"'
        ]
        assert {logger for _, logger, _ in split} >= {"PreLoader", "LeviLamina", "Server", "CrashLogger"}
        assert split[-1] == (None, None, "Quit correctly")
        positions = LeviLaminaAdapter(arm(), config.default_machine()).parse_positions(
            [SimpleNamespace(text=text) for _, _, text in split]  # type: ignore[misc]
        )
        assert positions is not None and [(p.x, p.z) for p in positions] == [(1280.5, 1280.5)]

    def test_vanilla_lines(self) -> None:
        assert LEVILAMINA_DIALECT.parse_version("Version: 1.26.51.1") is None, "the BDS banner is not the version"
        assert LEVILAMINA_DIALECT.is_ready("Server started.")
        assert LEVILAMINA_DIALECT.tick_cmd == "time query gametime"
        assert LEVILAMINA_DIALECT.split("not a prefixed line") == (None, None, "not a prefixed line")

    def test_shares_bds_commands(self) -> None:
        adapter = LeviLaminaAdapter(arm(), config.default_machine())
        assert adapter.cmd_positions() == "querytarget @a"

    @pytest.mark.parametrize("system", ["windows", "linux"])
    def test_constructs_on_any_host(self, system: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """The runner refuses other platforms; construction never does, so the contract test runs everywhere."""
        monkeypatch.setattr(levilamina_module, "host_os", lambda: system)
        for variant in (None, *LeviLaminaAdapter.variants):
            assert LeviLaminaAdapter(arm(variant), config.default_machine()).platform == "windows"
        with pytest.raises(ConfigError, match="no variant"):
            LeviLaminaAdapter(arm("turbo"), config.default_machine())


class TestPins:
    def test_catalog(self) -> None:
        catalog = config.load_artifacts("levilamina")
        assert catalog.server == "levilamina"
        entry = catalog.versions[VERSION]
        assert dict(entry.meta) == {"bds": BDS_VERSION, "allocator": "mimalloc"}
        assert BDS_VERSION in config.load_artifacts("bds").versions
        packages = packages_of(entry, catalog.path)
        assert set(packages) == {*BASE_PACKAGES, LEVIOPTIMIZE}
        for package in packages.values():
            spec = package.spec
            assert spec.sha256 and spec.size and spec.source == "github", package.name
            assert spec.url.startswith("https://github.com/LiteLDev/") and "/releases/download/v" in spec.url
            assert f"/v{package.release}/" in spec.url, package.name
            assert spec.eula == ("levimc" if package.name in ("preloader", "peeditor") else None), package.name
        assert packages["levilamina"].release == VERSION

    @pytest.mark.parametrize("name", ["preloader", "peeditor"])
    def test_closed_source_packages_need_the_eula(self, name: str, tmp_path: Path) -> None:
        spec = pinned_packages()[name].spec
        with pytest.raises(fetch.EulaNotAccepted, match="levimc"):
            fetch.obtain(spec, Cache(tmp_path / "cache"), fetch.FetchOptions(accept_eula=False))

    def test_features(self) -> None:
        features = config.load_features("levilamina")
        row = features.row(VERSION)
        assert row is not None and row.fidelity.value == "V1"
        assert set(row.cells) == FEATURE_KEYS
        assert all(cell.cls is FeatureClass.VANILLA for cell in row.cells.values())
        assert features.row("26.52.0") is None, "an unpinned release has no parity row"

    def test_spread_scenarios_are_fully_declared(self) -> None:
        row = config.load_features("levilamina").row(VERSION)
        assert row is not None
        for name in ("spread100", "spread100-csg"):
            assert set(config.load_scenario(name).exercises) <= set(row.cells), name

    def test_templates(self) -> None:
        levilamina = load_template("LeviLamina.Config.json")
        for change in LEVILAMINA_CHANGES:
            assert flatten(levilamina)[change.path] is True, f"{change.path} is on in LeviLamina's defaults"
            assert change.value is False and change.category is OverrideCategory.HARNESS
        assert load_template("PreLoaderConfig.json")["modsPath"] == "./plugins/"
        defaults = load_template(LEVIOPTIMIZE_TEMPLATES[LEVIOPTIMIZE])
        everything = load_template(LEVIOPTIMIZE_TEMPLATES[LEVIOPTIMIZE_ALL])
        diff = json_diff(defaults, everything)
        assert set(diff) == set(LEVIOPTIMIZE_ALL_REASONS)
        assert set(diff.values()) == {("false", "true")}
        assert flatten(everything)["features.optPushEntity.maxPushTimes"] == -1, "the push cap stays off"

    def test_matrix(self) -> None:
        matrix = config.load_matrix("levilamina-26.51")
        assert [a.id for a in matrix.arms] == [
            f"bds@{BDS_VERSION}",
            f"levilamina@{VERSION}",
            f"levilamina@{VERSION}+{LEVIOPTIMIZE}",
            f"levilamina@{VERSION}+{LEVIOPTIMIZE_ALL}",
        ]
        for matrix_arm in matrix.arms[1:]:
            assert matrix_arm.bds == BDS_VERSION
            LeviLaminaAdapter(matrix_arm, config.default_machine())
        assert matrix.priority_override is None


def _version(**tables: dict[str, Any]) -> ArtifactVersion:
    def spec(extra: dict[str, Any]) -> ArtifactSpec:
        return ArtifactSpec(name="x.zip", url="https://example.org/x.zip", sha256="0" * 64, extra=extra)

    return ArtifactVersion(version=VERSION, platforms={key: spec(extra) for key, extra in tables.items()})


class TestPackages:
    def test_reads_tables(self) -> None:
        packages = packages_of(_version(**{"windows-thing": {"release": "1.0", "place": {"a/": "plugins/a/"}}}))
        assert packages["thing"].release == "1.0" and packages["thing"].place == {"a/": "plugins/a/"}

    @pytest.mark.parametrize(
        ("key", "extra", "message"),
        [
            ("linux-thing", {"release": "1", "place": {"a": "a"}}, "windows-<package>"),
            ("windows-thing", {"place": {"a": "a"}}, "release"),
            ("windows-thing", {"release": "1"}, "place"),
            ("windows-thing", {"release": "1", "place": {"/etc/a": "a"}}, "relative"),
            ("windows-thing", {"release": "1", "place": {"a": "../a"}}, "relative"),
            ("windows-thing", {"release": "1", "place": {"a": "C:/a"}}, "relative"),
            ("windows-thing", {"release": "1", "place": {"a/": "a"}}, "directory"),
            ("windows-thing", {"release": "1", "place": {"a": "a"}, "mode": "x"}, "unknown"),
        ],
    )
    def test_rejects(self, key: str, extra: dict[str, Any], message: str) -> None:
        with pytest.raises(ConfigError, match=re.escape(message)):
            packages_of(_version(**{key: extra}))

    def test_place_refuses_to_overwrite(self, tmp_path: Path) -> None:
        package = pinned_packages()["crashlogger"]
        unpacked = tmp_path / "unpacked"
        (unpacked).mkdir()
        (unpacked / "CrashLogger.exe").write_bytes(b"new")
        server = tmp_path / "server"
        (server / "plugins" / "LeviLamina").mkdir(parents=True)
        (server / "plugins" / "LeviLamina" / "CrashLogger.exe").write_bytes(b"old")
        with pytest.raises(FetchError, match="would overwrite plugins/LeviLamina/CrashLogger.exe"):
            place_package(package, unpacked, server)

    def test_place_needs_its_sources(self, tmp_path: Path) -> None:
        (tmp_path / "unpacked").mkdir()
        with pytest.raises(FetchError, match="no directory LeviLamina/"):
            place_package(pinned_packages()["levilamina"], tmp_path / "unpacked", tmp_path / "server")
        with pytest.raises(FetchError, match="no file bin/PreLoader.dll"):
            place_package(pinned_packages()["preloader"], tmp_path / "unpacked", tmp_path / "server")


class TestPeEditor:
    def test_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.update(command=command, **kwargs)
            (tmp_path / MOD_EXE).write_bytes(b"mod")
            return subprocess.CompletedProcess(command, 0, "done", "")

        monkeypatch.setattr(levilamina_module.subprocess, "run", fake_run)
        run_peeditor(tmp_path)
        assert seen["command"] == [str(tmp_path / "PeEditor.exe"), "-mb"]
        assert seen["cwd"] == tmp_path and seen["stdin"] is subprocess.DEVNULL and seen["timeout"] > 0

    def test_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        results = iter([(1, "cannot open bedrock_server.exe"), (0, "nothing to do")])

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            code, out = next(results)
            return subprocess.CompletedProcess(command, code, out, "")

        monkeypatch.setattr(levilamina_module.subprocess, "run", fake_run)
        with pytest.raises(FetchError, match="exit code 1(.|\n)*cannot open"):
            run_peeditor(tmp_path)
        with pytest.raises(FetchError, match=f"did not write {MOD_EXE}"):
            run_peeditor(tmp_path)

    def test_missing_binary(self, tmp_path: Path) -> None:
        with pytest.raises(FetchError, match="could not run PeEditor.exe"):
            run_peeditor(tmp_path)


class TestFetch:
    @pytest.mark.parametrize("system", ["linux", "darwin"])
    def test_windows_only(self, system: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(levilamina_module, "host_os", lambda: system)
        with pytest.raises(ConfigError, match="Windows only"):
            LeviLaminaAdapter(arm(), config.default_machine()).fetch(Cache(tmp_path / "cache"))

    def test_unpinned_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(levilamina_module, "host_os", lambda: "windows")
        adapter = LeviLaminaAdapter(arm(version="1.0.0", id="levilamina@1.0.0"), config.default_machine())
        with pytest.raises(ConfigError, match="not pinned"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_bds_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(levilamina_module, "host_os", lambda: "windows")
        adapter = LeviLaminaAdapter(arm(bds="1.26.52.3"), config.default_machine())
        with pytest.raises(ConfigError, match=f"runs bds@{BDS_VERSION}"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_install(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        cache = Cache(tmp_path / "cache")
        adapter = LeviLaminaAdapter(arm(), config.default_machine())
        install = adapter.fetch(cache)
        root = install.root
        assert root.parent == cache.installs
        for path in (
            "PreLoader.dll",
            "PeEditor.exe",
            "bedrock_runtime_data",
            "plugins/LeviLamina/levilamina.dll",
            "plugins/LeviLamina/manifest.json",
            "plugins/LeviLamina/CrashLogger.exe",
            "bedrock_server.exe.bak",
            MOD_EXE,
            "server.properties",
        ):
            assert (root / path).is_file(), path
        assert not (root / "bedrock_server.exe").exists(), "PeEditor keeps the original only as .bak"
        assert not (root / "README.md").exists() and not (root / ".unpack").exists()
        assert not (root / "plugins" / "LeviOptimize").exists()
        assert not (root / MOD_EXE).stat().st_mode & 0o222, "the install is read-only"
        assert downloads.obtained == list(BASE_PACKAGES)
        assert len(downloads.peeditor_runs) == 1

        assert install.extra["exe"] == MOD_EXE and install.extra["exe_sha256"] == sha256_file(root / MOD_EXE)
        assert install.extra["bds_version"] == BDS_VERSION and install.extra["allocator"] == "mimalloc"
        assert install.extra["packages"] == {name: pinned_packages()[name].release for name in BASE_PACKAGES}
        assert [a.source for a in install.artifacts] == ["local", *["github"] * len(BASE_PACKAGES)]

        assert adapter.fetch(cache) == install and len(downloads.peeditor_runs) == 1, "a second fetch reuses it"

    def test_variants_add_levioptimize(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        cache = Cache(tmp_path / "cache")
        base = LeviLaminaAdapter(arm(), config.default_machine()).fetch(cache)
        default = LeviLaminaAdapter(arm(LEVIOPTIMIZE), config.default_machine()).fetch(cache)
        everything = LeviLaminaAdapter(arm(LEVIOPTIMIZE_ALL), config.default_machine()).fetch(cache)
        assert (default.root / "plugins" / "LeviOptimize" / "levioptimize.dll").is_file()
        assert default.root != base.root and everything.root == default.root, "both variants share one install"
        assert default.extra["packages"][LEVIOPTIMIZE] == "0.17.0"
        assert len(downloads.peeditor_runs) == 2

    def test_local_levilamina(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        local = package_zip(pinned_packages()["levilamina"], tmp_path / "levilamina-dev.zip")
        adapter = LeviLaminaAdapter(arm(artifact=local), config.default_machine())
        install = adapter.fetch(Cache(tmp_path / "cache"))
        assert install.extra["packages"]["levilamina"] == "local"
        assert "levilamina" not in downloads.obtained
        assert [a.source for a in install.artifacts][:2] == ["local", "local"]


class TestPrepareAndLaunch:
    def test_prepare(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        adapter, _, layout = fetch_and_prepare(tmp_path)
        server = layout.server_dir
        assert [c.path for c in layout.config_files] == ["server.properties", PRELOADER_CONFIG, LEVILAMINA_CONFIG]
        assert Properties.load(server / "server.properties").get("online-mode") == "false"
        written = json.loads((server / LEVILAMINA_CONFIG).read_text(encoding="utf-8"))
        assert written["modules"]["crashLogger"]["uploadToSentry"] is False
        assert written["targeted"]["enableStatistics"] is False
        assert written["targeted"]["vulnerabilityFixes"]["limitIPFrequency"] is True, "left at LeviLamina's default"
        assert json.loads((server / PRELOADER_CONFIG).read_text(encoding="utf-8")) == load_template(PRELOADER_CONFIG)

        json_overrides = {o.key: o for o in layout.overrides if o.file == LEVILAMINA_CONFIG}
        assert set(json_overrides) == {change.path for change in LEVILAMINA_CHANGES}
        for override in json_overrides.values():
            assert (override.category, override.default, override.value) == (OverrideCategory.HARNESS, "true", "false")
        assert not [o for o in layout.overrides if o.category is OverrideCategory.NONVANILLA]
        assert not (server / LEVIOPTIMIZE_CONFIG).exists()
        assert adapter.readback(layout) == []

    def test_prepare_levioptimize(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        adapter, _, layout = fetch_and_prepare(tmp_path, LEVIOPTIMIZE)
        nonvanilla = [o for o in layout.overrides if o.category is OverrideCategory.NONVANILLA]
        assert [(o.file, o.key, o.value) for o in nonvanilla] == [(LEVIOPTIMIZE_MANIFEST, "version", "0.17.0")]
        written = json.loads((layout.server_dir / LEVIOPTIMIZE_CONFIG).read_text(encoding="utf-8"))
        assert written == load_template(LEVIOPTIMIZE_TEMPLATES[LEVIOPTIMIZE])
        assert layout.config_files[-1].path == LEVIOPTIMIZE_CONFIG
        assert layout.extra["levioptimize_config"] == "LeviOptimize.default.json"
        assert adapter.readback(layout) == []

    def test_prepare_levioptimize_all(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        _, _, layout = fetch_and_prepare(tmp_path, LEVIOPTIMIZE_ALL)
        nonvanilla = {o.key: o for o in layout.overrides if o.file == LEVIOPTIMIZE_CONFIG}
        assert set(nonvanilla) == set(LEVIOPTIMIZE_ALL_REASONS)
        for key, override in nonvanilla.items():
            assert override.category is OverrideCategory.NONVANILLA
            assert (override.default, override.value) == ("false", "true")
            assert override.reason == LEVIOPTIMIZE_ALL_REASONS[key]
        assert any(o.file == LEVIOPTIMIZE_MANIFEST for o in layout.overrides)
        written = json.loads((layout.server_dir / LEVIOPTIMIZE_CONFIG).read_text(encoding="utf-8"))
        assert written["features"]["optMovingBlock"] is True

    def test_variant_needs_its_install(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        base = LeviLaminaAdapter(arm(), config.default_machine()).fetch(Cache(tmp_path / "cache"))
        variant = LeviLaminaAdapter(arm(LEVIOPTIMIZE), config.default_machine())
        with pytest.raises(ConfigError, match="no LeviOptimize"):
            variant.prepare(base, make_ctx(tmp_path, variant.arm))

    def test_readback(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        adapter, _, layout = fetch_and_prepare(tmp_path, LEVIOPTIMIZE)
        server = layout.server_dir
        saved = server / LEVIOPTIMIZE_CONFIG
        saved.write_text(json.dumps(json.loads(saved.read_text(encoding="utf-8")), indent=2), encoding="utf-8")
        assert adapter.readback(layout) == [], "a server that saves the same values in another layout is fine"

        config_path = server / LEVILAMINA_CONFIG
        doc = json.loads(config_path.read_text(encoding="utf-8"))
        doc["targeted"]["enableStatistics"] = True
        config_path.write_text(json.dumps(doc), encoding="utf-8")
        (server / PRELOADER_CONFIG).unlink()
        problems = adapter.readback(layout)
        assert f"{PRELOADER_CONFIG} is missing after the run" in problems
        assert any(p.startswith(f"{LEVILAMINA_CONFIG}: targeted.enableStatistics changed") for p in problems)

    def test_launch(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        adapter, _, layout = fetch_and_prepare(tmp_path)
        spec = adapter.launch(layout)
        assert spec.argv == (str(layout.server_dir / MOD_EXE),)
        assert spec.cwd == layout.server_dir and not spec.env_set
        assert adapter.server_process(SimpleNamespace(pid=1)).pid == 1  # type: ignore[arg-type]

    def test_facts(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        adapter, _, layout = fetch_and_prepare(tmp_path, LEVIOPTIMIZE_ALL)
        proc = SimpleNamespace(memory_maps=lambda grouped=True: [SimpleNamespace(path="C:/srv/LeviLamina.dll")])
        facts = adapter.facts(layout, proc)  # type: ignore[arg-type]
        assert facts["allocator"] == "mimalloc", "declared: mimalloc is linked into LeviLamina.dll"
        assert facts["levilamina_version"] == VERSION and facts["packages"][LEVIOPTIMIZE] == "0.17.0"
        assert facts["levioptimize_config"] == "LeviOptimize.all.json"
        assert facts["usage_statistics"] == "off" and facts["crash_upload"] == "off"
        assert facts["csg_setting"] is False and facts["bds_version"] == BDS_VERSION

    def test_crash_files(self, tmp_path: Path, downloads: FakeDownloads) -> None:
        adapter, _, layout = fetch_and_prepare(tmp_path)
        crash = layout.server_dir / "logs" / "crash"
        crash.mkdir(parents=True)
        report = crash / "trace_2026-01-01_12-00-00.log"
        report.write_text("stack", encoding="utf-8")
        old = crash / "trace_2025-01-01_12-00-00.log"
        old.write_text("old", encoding="utf-8")
        past = time.time() - 3600
        os.utime(old, (past, past))
        assert adapter.crash_artifacts(layout, int((time.time() - 60) * 1000)) == [report]
