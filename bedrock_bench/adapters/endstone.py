# SPDX-License-Identifier: Apache-2.0
"""Endstone, the plugin API for BDS: installed from PyPI and started through its own launcher.

Fetch builds two things. The BDS build this Endstone version supports comes from ``configs/bds/server.toml``,
verified exactly as for vanilla BDS. Endstone gets a virtual environment per version, Python, wheel and dependency
lock, built with uv: the dependencies from ``configs/endstone/requirements-<version>.txt`` with
``--require-hashes``, then the Endstone wheel pinned in ``configs/endstone/server.toml`` and verified by
sha256, then ``uv pip check``. The environment is read-only once built.

Prepare writes the launcher's installed-BDS marker (``version.txt``), so the launcher accepts the verified copy
instead of downloading BDS again, and switches bStats off. ``endstone.toml`` is created by the launcher with
Endstone's defaults.

Launch runs ``<venv python> -m endstone -y --no-interactive -s .`` with ``PYTHONSAFEPATH=1`` and without
``PYTHONPATH``, ``PYTHONHOME`` and ``VIRTUAL_ENV``. The launcher starts ``bedrock_server`` as its child, which is the
measured process. It is found by executable path because Endstone renames the server's main thread (and with it the
process name on Linux). The launcher's own CPU is reported separately. See configs/endstone/README.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, fetch
from bedrock_bench.adapters.base import Dialect, host_os
from bedrock_bench.adapters.bdsfamily import BdsFamily, bds_dialect, fetch_bds, same_path
from bedrock_bench.cache import short_key
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    ArtifactVersion,
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    Override,
    OverrideCategory,
    RunContext,
    sha256_json,
)
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

DEFAULT_PYTHON = "3.13"
REQUIREMENTS = "configs/endstone/requirements-{version}.txt"
BSTATS_TEMPLATE = "configs/endstone/bstats.toml"
BSTATS_FILE = "plugins/bstats/config.toml"
VERSION_MARKER_FILE = "version.txt"
ENVIRONMENT = "<environment>"

#: ``[12:00:00 INFO]: text`` or ``[12:00:00 INFO]: [logger] text``, the same for the launcher and the server.
ENDSTONE_PREFIX = re.compile(r"^\s*\[\d{2}:\d{2}:\d{2} (?P<level>[A-Z]+)\]:(?: |$)(?:\[(?P<logger>[^\]\s][^\]]*)\] )?")
ENDSTONE_LEVELS: Mapping[str, str] = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "fatal",
}
ENDSTONE_DIALECT = bds_dialect(
    prefix=ENDSTONE_PREFIX,
    level_map=ENDSTONE_LEVELS,
    version=re.compile(r"running Endstone version: (?P<version>[0-9A-Za-z][0-9A-Za-z.+_-]*)"),
)


class EndstoneAdapter(BdsFamily):
    name: ClassVar[str] = "endstone"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    dialect: ClassVar[Dialect] = ENDSTONE_DIALECT
    thread_groups: ClassVar[Mapping[str, str]] = {**BdsFamily.thread_groups, "crash_reporter": r"^sentry"}
    crash_paths: ClassVar[tuple[str, ...]] = ("crash_reports", ".sentry-native", "%LOCALAPPDATA%/CrashDumps")

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        entry = self._catalog_entry()
        meta = dict(entry.meta) if entry is not None else {}
        bds_version = self._bds_version(meta)
        platform = self.platform
        bds = fetch_bds(cache, bds_version, platform)
        common = {
            **bds.extra,
            "bds_root": bds.root,
            "version_marker": str(meta.get("version_marker") or version_marker(bds_version)),
            "known_issues": known_issues(meta, platform),
        }
        python = self.arm.python or str(meta.get("python") or DEFAULT_PYTHON)
        if not re.match(r"^3\.\d+$", python):
            raise ConfigError(f"python must look like 3.13, got {python!r}")
        tag = "cp" + python.replace(".", "")
        if self.arm.artifact is not None:
            wheel, wheel_ref = fetch.adopt_local(self.arm.artifact, cache)
        else:
            assert entry is not None
            spec = entry.platforms.get(f"{platform}-{tag}")
            if spec is None:
                pinned = ", ".join(sorted(entry.platforms))
                raise ConfigError(
                    f"endstone {self.arm.version}: no wheel pinned for {platform}-{tag} (pinned: {pinned})"
                )
            wheel, wheel_ref = fetch.obtain(spec, cache)
        lock = self._requirements_lock()

        lock_sha = sha256_file(lock) if lock is not None else "unlocked"
        name = f"endstone-{self.arm.version}-{tag}-{short_key(wheel_ref.sha256, lock_sha, python, length=8)}"
        venv = cache.venv_dir(name)
        uv = find_uv()
        venv_tree = fetch.ensure_install(
            cache, venv, lambda tmp: _run_all(venv_commands(uv, tmp, python, lock=lock, wheel=wheel))
        )
        info = venv_info(venv_python(venv))
        if self.arm.artifact is None and info["endstone"] != self.arm.version:
            raise fetch.FetchError(f"{name} holds endstone {info['endstone']}, expected {self.arm.version}")
        return Install(
            root=venv,
            tree_sha256=sha256_json({"bds": bds.tree_sha256, "venv": venv_tree}),
            artifacts=(*bds.artifacts, wheel_ref),
            extra={
                **common,
                "venv": venv,
                "python": venv_python(venv),
                "python_version": info["python"],
                "endstone_version": info["endstone"],
                "deps_lock_sha256": None if lock is None else lock_sha,
            },
        )

    def _catalog_entry(self) -> ArtifactVersion | None:
        catalog = config.load_artifacts("endstone")
        entry = catalog.versions.get(self.arm.version)
        if entry is None and self.arm.artifact is None:
            pinned = ", ".join(sorted(catalog.versions))
            raise ConfigError(f"endstone {self.arm.version} is not pinned (pinned: {pinned})", file=catalog.path)
        return entry

    def _bds_version(self, meta: Mapping[str, Any]) -> str:
        pinned = meta.get("bds")
        if self.arm.bds and pinned and self.arm.bds != pinned:
            raise ConfigError(f"{self.arm.ref} runs bds@{pinned}, but the arm asks for bds@{self.arm.bds}")
        version = self.arm.bds or pinned
        if not version:
            raise ConfigError(f"endstone {self.arm.version}: say which BDS version it runs (arm key 'bds')")
        return str(version)

    def _requirements_lock(self) -> Path | None:
        path = config.project_root() / REQUIREMENTS.format(version=self.arm.version)
        if path.is_file():
            return path
        if self.arm.artifact is not None:
            return None
        raise ConfigError(
            f"no dependency lock for endstone {self.arm.version}; see configs/endstone/README.md", file=path
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        harness = OverrideCategory.HARNESS
        marker = install.extra["version_marker"]
        marker_path = server_dir / VERSION_MARKER_FILE
        marker_path.write_text(marker, encoding="utf-8")

        bstats_path = server_dir / BSTATS_FILE
        bstats_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.project_root() / BSTATS_TEMPLATE, bstats_path)

        overrides = [
            Override(
                harness,
                VERSION_MARKER_FILE,
                "contents",
                None,
                marker,
                "the launcher's installed-BDS marker; without it the launcher downloads BDS over the verified copy",
            ),
            Override(harness, BSTATS_FILE, "enabled", "true", "false", "no usage statistics upload during measurement"),
            *self._environment()[2],
        ]
        files = [
            ConfigFile(VERSION_MARKER_FILE, sha256_file(marker_path)),
            ConfigFile(BSTATS_FILE, sha256_file(bstats_path)),
        ]
        extra = {
            "python": install.extra.get("python"),
            "python_version": install.extra.get("python_version"),
            "endstone_version": install.extra.get("endstone_version"),
            "version_marker": marker,
            "known_issues": list(install.extra.get("known_issues", ())),
        }
        return overrides, files, extra

    def _environment(self) -> tuple[dict[str, str], tuple[str, ...], list[Override]]:
        """Variables set and removed for the launcher, and the matching overrides."""
        harness = OverrideCategory.HARNESS
        env_set = {
            "PYTHONSAFEPATH": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        reasons_set = {
            "PYTHONSAFEPATH": "the working directory can never shadow an installed package",
            "PYTHONIOENCODING": "console text is UTF-8 on every platform",
            "PYTHONUNBUFFERED": "launcher output reaches the console as it is written",
            "PYTHONDONTWRITEBYTECODE": "the installed environment is read-only and already compiled",
        }
        removed = ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"]
        overrides = [
            Override(harness, ENVIRONMENT, key, None, value, reasons_set[key]) for key, value in env_set.items()
        ]
        overrides += [
            Override(harness, ENVIRONMENT, key, None, "(unset)", "the virtual environment runs exactly as installed")
            for key in removed
        ]
        if self.platform == "linux":
            removed.append("LD_PRELOAD")
            overrides.append(
                Override(harness, ENVIRONMENT, "LD_PRELOAD", None, "(unset)", "no allocator preloaded by accident")
            )
        return env_set, tuple(removed), overrides

    def launch(self, layout: Layout) -> LaunchSpec:
        env_set, env_remove, _ = self._environment()
        self._server_exe = self.executable(layout)
        return LaunchSpec(
            argv=(str(layout.extra["python"]), "-m", "endstone", "-y", "--no-interactive", "-s", "."),
            cwd=layout.server_dir,
            env_set=env_set,
            env_remove=env_remove,
        )

    def server_process(
        self, launched: psutil.Process, *, timeout_s: float = 120.0, poll_s: float = 0.1
    ) -> psutil.Process:
        """The ``bedrock_server`` child of the launcher, matched by executable path. Waits until it appears."""
        import psutil

        if self._server_exe is None:
            raise RuntimeError("server_process needs prepare() or launch() to have run on this adapter")
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                children = launched.children(recursive=True)
            except psutil.NoSuchProcess:
                raise RuntimeError("the Endstone launcher exited before it started the server") from None
            for child in children:
                try:
                    if same_path(child.exe(), self._server_exe):
                        return child
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            if not launched.is_running() or launched.status() == psutil.STATUS_ZOMBIE:
                raise RuntimeError("the Endstone launcher exited before it started the server")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"the Endstone launcher did not start {self._server_exe.name} in {timeout_s:g} s")
            time.sleep(poll_s)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = super().facts(layout, proc)
        facts.update(
            {
                "endstone_version": layout.extra.get("endstone_version"),
                "python_version": layout.extra.get("python_version"),
                "usage_statistics": "off",
                "crash_upload": "on; this Endstone version has no switch for it",
                "known_issues": list(layout.extra.get("known_issues", ())),
            }
        )
        return facts


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def version_marker(bds_version: str) -> str:
    """The launcher's ``version.txt`` marker for a BDS build: ``<minor>.<patch>``, e.g. ``26.52`` for 1.26.52.3."""
    parts = bds_version.split(".")
    if len(parts) < 3 or not all(part.isdigit() for part in parts):
        raise ConfigError(f"not a BDS version: {bds_version!r}")
    return f"{parts[1]}.{parts[2]}"


def known_issues(meta: Mapping[str, Any], platform: str) -> list[str]:
    """Issues of the pinned Endstone version that affect measurements on ``platform`` (from its server file)."""
    issues = []
    for item in meta.get("known_issues", ()):
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
            raise ConfigError("known_issues entries are tables with text, platforms and source")
        platforms = item.get("platforms") or ["windows", "linux"]
        if platform in platforms:
            issues.append(item["text"])
    return issues


def find_uv() -> str:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise ConfigError("uv is needed to install Endstone; see https://docs.astral.sh/uv/")
    return uv


def venv_python(venv: Path) -> Path:
    return venv / "Scripts" / "python.exe" if host_os() == "windows" else venv / "bin" / "python"


def venv_commands(uv: str, target: Path, python: str, *, lock: Path | None, wheel: Path) -> list[list[str]]:
    """The uv commands that build an Endstone environment in ``target``.

    With a lock, dependencies are installed hash-checked and without resolution, then the verified wheel without
    dependencies or index access. Without one (a local wheel of an unpinned version) the wheel's dependencies are
    resolved from the index. ``uv pip check`` confirms the result is complete.
    """
    py = str(venv_python(target))
    base = ["--no-config", "--python", py]
    commands = [
        [uv, "venv", "--no-config", "--no-project", "--relocatable", "--clear", "--python", python, str(target)]
    ]
    if lock is not None:
        commands.append(
            [uv, "pip", "install", *base, "--require-hashes", "--no-deps", "--compile-bytecode", "-r", str(lock)]
        )
        commands.append([uv, "pip", "install", *base, "--no-deps", "--no-index", "--compile-bytecode", str(wheel)])
    else:
        commands.append([uv, "pip", "install", *base, "--compile-bytecode", str(wheel)])
    commands.append([uv, "pip", "check", *base])
    return commands


_VENV_INFO = (
    "import importlib.metadata, json, platform; "
    "print(json.dumps({'python': platform.python_version(), 'endstone': importlib.metadata.version('endstone')}))"
)


def venv_info(python: Path) -> dict[str, str]:
    """Python and Endstone versions of an environment, read without importing Endstone itself."""
    result = _run([str(python), "-I", "-c", _VENV_INFO])
    try:
        info = json.loads(result.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise fetch.FetchError(f"could not read the Endstone environment at {python.parent.parent}") from None
    return {"python": str(info["python"]), "endstone": str(info["endstone"])}


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    return env


def _run(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_clean_env(),
            check=False,
        )
    except OSError as exc:
        raise fetch.FetchError(f"could not run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-2000:]
        raise fetch.FetchError(f"{' '.join(command[1:3])} failed with exit code {result.returncode}:\n{tail}")
    return result.stdout


def _run_all(commands: Sequence[Sequence[str]]) -> None:
    for command in commands:
        _run(command)
