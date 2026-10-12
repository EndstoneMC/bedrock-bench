# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from bedrock_bench import adapters, config
from bedrock_bench.adapters import bdsfamily
from bedrock_bench.adapters import endstone as endstone_module
from bedrock_bench.adapters.bdsfamily import Properties, fetch_bds
from bedrock_bench.adapters.endstone import (
    ENDSTONE_DIALECT,
    EndstoneAdapter,
    known_issues,
    venv_commands,
    venv_python,
    version_marker,
)
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import FEATURE_KEYS, Arm, Install, OverrideCategory, Placement, RunContext

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "tests" / "fixtures" / "logs"

SHIPPED_PROPERTIES = "server-name=Dedicated Server\nonline-mode=true\nview-distance=32\nlevel-name=Bedrock level\n"


def bds_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name in ("bedrock_server.exe", "bedrock_server"):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100755 << 16
            zf.writestr(info, b"server binary")
        zf.writestr("server.properties", SHIPPED_PROPERTIES)
    return path


def arm(**changes: Any) -> Arm:
    fields: dict[str, Any] = {"id": "endstone@0.11.14", "server": "endstone", "version": "0.11.14", "python": "3.13"}
    fields.update(changes)
    return Arm(**fields)


def make_ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        run_id="20260101T120000Z-endstone-r1-abcd",
        run_dir=tmp_path / "run",
        work_dir=tmp_path / "work",
        arm=arm(),
        scenario=config.parse_scenario({"server": {"csg": True}}, name="test"),
        machine=config.default_machine(),
        placement=Placement((0, 1), (2, 3), (2, 3), "normal", "physical"),
    )


def fake_install(tmp_path: Path, adapter: EndstoneAdapter) -> Install:
    """An Endstone install as fetch() returns it, with a local BDS zip and a stand-in environment."""
    bds = fetch_bds(Cache(tmp_path / "cache"), "1.26.52.3", adapter.platform, artifact=bds_zip(tmp_path / "bds.zip"))
    venv = tmp_path / "venv"
    return Install(
        root=venv,
        tree_sha256="0" * 64,
        artifacts=bds.artifacts,
        extra={
            **bds.extra,
            "bds_root": bds.root,
            "version_marker": "26.52",
            "known_issues": [],
            "venv": venv,
            "python": venv_python(venv),
            "python_version": "3.13.7",
            "endstone_version": "0.11.14",
        },
    )


@pytest.fixture
def adapter() -> EndstoneAdapter:
    return EndstoneAdapter(arm(), config.default_machine())


# --------------------------------------------------------------------------------------------------------------------


class TestContract:
    def test_registered(self) -> None:
        assert adapters.get("endstone") is EndstoneAdapter
        assert EndstoneAdapter.class_problems() == []
        assert EndstoneAdapter.features_file == "features/endstone.toml"
        assert EndstoneAdapter.tick_thread_rule == "lowest_tid_name:MC_SERVER"
        assert "crash_reports" in EndstoneAdapter.crash_paths

    def test_dialect_samples(self) -> None:
        with open(LOGS / "endstone.toml", "rb") as fh:
            samples = tomllib.load(fh)["sample"]
        for sample in samples:
            level, _, text = ENDSTONE_DIALECT.split(sample["line"])
            role = sample["role"]
            if role == "ready":
                assert ENDSTONE_DIALECT.is_ready(text)
            elif role == "version":
                assert ENDSTONE_DIALECT.parse_version(text) == sample["value"]
            elif role == "player_in_world":
                assert ENDSTONE_DIALECT.player_entered(text) == sample["value"]
            elif role == "tick":
                assert ENDSTONE_DIALECT.parse_tick(text) == sample["value"]
            elif role == "level":
                assert level == sample["value"]
            elif role == "strip":
                assert text == sample["value"]
            elif role == "benign":
                assert level == "error" and ENDSTONE_DIALECT.is_benign(text)
            elif role == "not_benign":
                assert level == "error" and not ENDSTONE_DIALECT.is_benign(text)

    def test_bds_version_line_is_not_the_banner(self) -> None:
        assert ENDSTONE_DIALECT.parse_version("Version: 1.26.52.3") is None

    def test_shares_bds_commands(self, adapter: EndstoneAdapter) -> None:
        assert adapter.cmd_positions() == "querytarget @a"
        assert adapter.chaos_dialect().actions == bdsfamily.BdsFamily.chaos_dialect(adapter).actions
        assert ENDSTONE_DIALECT.tick_cmd == "time query gametime"


class TestPins:
    def test_catalog(self) -> None:
        bds = config.load_artifacts("bds")
        catalog = config.load_artifacts("endstone")
        assert {"0.11.13", "0.11.14"} <= set(catalog.versions)
        for version, entry in catalog.versions.items():
            meta = dict(entry.meta)
            assert meta["bds"] in bds.versions, version
            assert meta["version_marker"] == version_marker(meta["bds"])
            assert meta["linux_allocator"] in ("glibc", "mimalloc")
            for platform in ("windows", "linux"):
                spec = entry.platforms[f"{platform}-cp313"]
                assert spec.sha256 and spec.size and spec.source == "pypi"
                assert spec.name == f"endstone-{version}-cp313-cp313-" + (
                    "win_amd64.whl" if platform == "windows" else "manylinux_2_31_x86_64.whl"
                )
                assert spec.url.startswith("https://files.pythonhosted.org/")

    def test_requirement_locks_are_hashed(self) -> None:
        for version in config.load_artifacts("endstone").versions:
            lock = ROOT / "servers" / "endstone" / "requirements" / f"{version}.txt"
            text = lock.read_text(encoding="utf-8")
            requirements = re.findall(r"^([A-Za-z0-9_.-]+)==", text, flags=re.MULTILINE)
            assert requirements, version
            assert "endstone" not in requirements, "the wheel is pinned in artifacts/endstone.toml"
            blocks = re.split(r"^(?=[A-Za-z0-9_.-]+==)", text, flags=re.MULTILINE)[1:]
            assert all("--hash=sha256:" in block for block in blocks), version

    def test_version_marker(self) -> None:
        assert version_marker("1.26.52.3") == "26.52"
        assert version_marker("1.26.51.1") == "26.51"
        with pytest.raises(ConfigError):
            version_marker("26.52")

    def test_known_issues(self) -> None:
        meta = dict(config.load_artifacts("endstone").versions["0.11.13"].meta)
        assert len(known_issues(meta, "windows")) == 2
        assert len(known_issues(meta, "linux")) == 1
        assert known_issues(dict(config.load_artifacts("endstone").versions["0.11.14"].meta), "windows") == []

    def test_features(self) -> None:
        features = config.load_features("endstone")
        old, new = features.row("0.11.13"), features.row("0.11.14")
        assert old is not None and new is not None
        assert (old.fidelity.value, new.fidelity.value) == ("V0", "V1")
        assert set(old.cells) == set(new.cells) == FEATURE_KEYS
        assert features.row("0.12.0") is None, "an unpinned release has no parity row"


class TestFetch:
    def test_unpinned_version(self, tmp_path: Path) -> None:
        adapter = EndstoneAdapter(arm(version="0.9.0", id="endstone@0.9.0"), config.default_machine())
        with pytest.raises(ConfigError, match="not pinned"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_glibc_variant_needs_mimalloc_release(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(endstone_module, "host_os", lambda: "linux")
        adapter = EndstoneAdapter(
            arm(version="0.11.13", id="endstone@0.11.13+glibc-alloc", variant="glibc-alloc"), config.default_machine()
        )
        with pytest.raises(ConfigError, match="already runs on glibc"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_bds_mismatch(self, tmp_path: Path) -> None:
        adapter = EndstoneAdapter(arm(bds="1.26.51.1"), config.default_machine())
        with pytest.raises(ConfigError, match="runs bds@1.26.52.3"):
            adapter.fetch(Cache(tmp_path / "cache"))

    def test_venv_commands_with_lock(self, tmp_path: Path) -> None:
        lock = tmp_path / "0.11.14.txt"
        wheel = tmp_path / "endstone-0.11.14-cp313-cp313-win_amd64.whl"
        commands = venv_commands("uv", tmp_path / "env", "3.13", lock=lock, wheel=wheel)
        assert commands[0][:2] == ["uv", "venv"] and "--relocatable" in commands[0] and "3.13" in commands[0]
        assert commands[1][:3] == ["uv", "pip", "install"]
        assert {"--require-hashes", "--no-deps", "-r", str(lock)} <= set(commands[1])
        assert {"--no-deps", "--no-index", str(wheel)} <= set(commands[2])
        assert commands[3][:3] == ["uv", "pip", "check"]
        assert all("--no-config" in c for c in commands)

    def test_venv_commands_without_lock(self, tmp_path: Path) -> None:
        wheel = tmp_path / "endstone-0.11.15.dev1-cp313-cp313-win_amd64.whl"
        commands = venv_commands("uv", tmp_path / "env", "3.13", lock=None, wheel=wheel)
        assert len(commands) == 3 and "--no-deps" not in commands[1] and str(wheel) in commands[1]

    def test_fetch_orchestration(self, tmp_path: Path, fake_tooling: list[list[str]]) -> None:
        """fetch() with uv and the downloads replaced: naming, verification, read-only result, Install fields."""
        cache = Cache(tmp_path / "cache")
        adapter = EndstoneAdapter(arm(artifact=local_wheel(tmp_path)), config.default_machine())
        install = adapter.fetch(cache)
        assert install.root.parent == cache.venvs and install.root.name.startswith("endstone-0.11.14-cp313-")
        assert [a.source for a in install.artifacts] == ["local", "local"]
        assert install.extra["version_marker"] == "26.52"
        assert install.extra["endstone_version"] == "0.11.14" and install.extra["python_version"] == "3.13.7"
        assert install.extra["deps_lock_sha256"], "the pinned version's dependency lock is used"
        assert Path(install.extra["bds_root"]).is_dir()
        assert not Path(install.extra["python"]).stat().st_mode & 0o222, "the environment is read-only"
        assert len(fake_tooling) == 4
        assert adapter.fetch(cache) == install and len(fake_tooling) == 4, "a second fetch reuses the environment"

    def test_wrong_endstone_in_environment(
        self, tmp_path: Path, fake_tooling: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bedrock_bench.fetch import FetchError

        monkeypatch.setattr(endstone_module, "venv_info", lambda python: {"python": "3.13.7", "endstone": "0.11.13"})
        monkeypatch.setattr(
            endstone_module.fetch,
            "obtain",
            lambda spec, cache: endstone_module.fetch.adopt_local(local_wheel(tmp_path), cache, source="pypi"),
        )
        adapter = EndstoneAdapter(arm(), config.default_machine())
        with pytest.raises(FetchError, match="expected 0.11.14"):
            adapter.fetch(Cache(tmp_path / "cache"))


def local_wheel(tmp_path: Path) -> Path:
    wheel = tmp_path / "endstone-0.11.14-cp313-cp313-win_amd64.whl"
    wheel.write_bytes(b"wheel")
    return wheel


@pytest.fixture
def fake_tooling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the BDS download, uv and the environment probe; returns the uv commands that ran."""
    zip_path = bds_zip(tmp_path / "bds.zip")
    ran: list[list[str]] = []

    def fake_fetch_bds(cache: Cache, version: str, platform: str, *, artifact: Path | None = None) -> Install:
        assert version == "1.26.52.3"
        return fetch_bds(cache, version, platform, artifact=zip_path)

    def fake_run_all(commands: list[list[str]]) -> None:
        ran.extend(commands)
        target = Path(commands[0][-1])
        python = venv_python(target)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python")
        (target / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")

    monkeypatch.setattr(endstone_module, "fetch_bds", fake_fetch_bds)
    monkeypatch.setattr(endstone_module, "_run_all", fake_run_all)
    monkeypatch.setattr(endstone_module, "find_uv", lambda: "uv")
    monkeypatch.setattr(endstone_module, "venv_info", lambda python: {"python": "3.13.7", "endstone": "0.11.14"})
    return ran


class TestPrepareAndLaunch:
    def test_prepare(self, adapter: EndstoneAdapter, tmp_path: Path) -> None:
        layout = adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        server = layout.server_dir
        assert (server / "version.txt").read_text(encoding="utf-8") == "26.52"
        bstats = tomllib.loads((server / "plugins" / "bstats" / "config.toml").read_text(encoding="utf-8"))
        assert bstats["enabled"] is False and bstats["server-uuid"]
        assert Properties.load(server / "server.properties").get("online-mode") == "false"
        assert [c.path for c in layout.config_files] == [
            "server.properties",
            "version.txt",
            "plugins/bstats/config.toml",
        ]
        by_file_key = {(o.file, o.key): o for o in layout.overrides}
        assert by_file_key[("version.txt", "contents")].category is OverrideCategory.HARNESS
        assert by_file_key[("plugins/bstats/config.toml", "enabled")].value == "false"
        assert by_file_key[("<environment>", "PYTHONSAFEPATH")].value == "1"
        assert by_file_key[("<environment>", "PYTHONPATH")].value == "(unset)"
        assert layout.extra["endstone_version"] == "0.11.14"
        assert adapter.readback(layout) == []

    @pytest.mark.parametrize("system", ["windows", "linux"])
    def test_launch(self, system: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bdsfamily, "host_os", lambda: system)
        monkeypatch.setattr(endstone_module, "host_os", lambda: system)
        adapter = EndstoneAdapter(arm(), config.default_machine())
        layout = adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        spec = adapter.launch(layout)
        assert spec.argv[1:] == ("-m", "endstone", "-y", "--no-interactive", "-s", ".")
        assert Path(spec.argv[0]) == venv_python(tmp_path / "venv")
        assert spec.cwd == layout.server_dir
        assert spec.env_set["PYTHONSAFEPATH"] == "1"
        assert {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} <= set(spec.env_remove)
        assert ("LD_PRELOAD" in spec.env_remove) == (system == "linux")
        assert spec.image is None

    def test_glibc_variant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bdsfamily, "host_os", lambda: "linux")
        monkeypatch.setattr(endstone_module, "host_os", lambda: "linux")
        adapter = EndstoneAdapter(arm(variant="glibc-alloc"), config.default_machine())
        layout = adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        spec = adapter.launch(layout)
        assert spec.env_set["LD_PRELOAD"] == "libc.so.6" and "LD_PRELOAD" not in spec.env_remove
        preload = next(o for o in layout.overrides if o.key == "LD_PRELOAD")
        assert preload.category is OverrideCategory.PARITY

    def test_glibc_variant_is_linux_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(endstone_module, "host_os", lambda: "windows")
        with pytest.raises(ConfigError, match="Linux"):
            EndstoneAdapter(arm(variant="glibc-alloc"), config.default_machine())
        with pytest.raises(ConfigError, match="no variant"):
            EndstoneAdapter(arm(variant="turbo"), config.default_machine())

    def test_docker_launch(self, tmp_path: Path) -> None:
        machine = config.parse_machine({"runtime": {"kind": "docker"}})
        adapter = EndstoneAdapter(arm(), machine)
        layout = adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        spec = adapter.launch(layout)
        assert spec.argv[0] == "python" and spec.image == "endstone"

    def test_launch_needs_an_environment(self, adapter: EndstoneAdapter, tmp_path: Path) -> None:
        install = fake_install(tmp_path, adapter)
        install = Install(install.root, install.tree_sha256, install.artifacts, {**install.extra, "python": None})
        layout = adapter.prepare(install, make_ctx(tmp_path))
        with pytest.raises(ConfigError):
            adapter.launch(layout)


class FakeProcess:
    def __init__(self, exe: str, children: list[FakeProcess] | None = None, running: bool = True) -> None:
        self._exe = exe
        self._children = children or []
        self.running = running

    def children(self, recursive: bool = False) -> list[FakeProcess]:
        if not self.running:
            raise psutil.NoSuchProcess(1)
        return list(self._children)

    def exe(self) -> str:
        return self._exe

    def is_running(self) -> bool:
        return self.running

    def status(self) -> str:
        return psutil.STATUS_RUNNING


class TestServerProcess:
    def test_finds_child_by_exe(self, adapter: EndstoneAdapter, tmp_path: Path) -> None:
        layout = adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        exe = str(adapter.executable(layout))
        helper = FakeProcess(str(tmp_path / "crashpad_handler"))
        server = FakeProcess(exe.upper() if sys.platform == "win32" else exe)
        launcher = FakeProcess(sys.executable, [helper, server])
        assert adapter.server_process(launcher) is server  # type: ignore[arg-type]

    def test_waits_then_times_out(self, adapter: EndstoneAdapter, tmp_path: Path) -> None:
        adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        launcher = FakeProcess(sys.executable, [])
        with pytest.raises(TimeoutError):
            adapter.server_process(launcher, timeout_s=0.05, poll_s=0.01)  # type: ignore[arg-type]

    def test_launcher_exited(self, adapter: EndstoneAdapter, tmp_path: Path) -> None:
        adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        with pytest.raises(RuntimeError, match="exited"):
            adapter.server_process(FakeProcess(sys.executable, running=False))  # type: ignore[arg-type]

    def test_needs_prepare(self, adapter: EndstoneAdapter) -> None:
        with pytest.raises(RuntimeError):
            adapter.server_process(FakeProcess(sys.executable))  # type: ignore[arg-type]


class TestFacts:
    def test_facts(self, adapter: EndstoneAdapter, tmp_path: Path) -> None:
        layout = adapter.prepare(fake_install(tmp_path, adapter), make_ctx(tmp_path))
        proc = SimpleNamespace(memory_maps=lambda grouped=True: [SimpleNamespace(path="/venv/libmimalloc.so")])
        facts = adapter.facts(layout, proc)  # type: ignore[arg-type]
        assert facts["allocator"] == "mimalloc"
        assert facts["endstone_version"] == "0.11.14" and facts["python_version"] == "3.13.7"
        assert facts["usage_statistics"] == "off" and "no switch" in facts["crash_upload"]
        assert facts["csg_setting"] is True and facts["known_issues"] == []
