# SPDX-License-Identifier: Apache-2.0
"""LeviLamina, a mod loader for BDS on Windows.

Fetch assembles one install per package set. It starts from the BDS build the release depends on
(``configs/bds/server.toml``, verified exactly as for vanilla BDS), adds the packages pinned in
``configs/levilamina/server.toml``, each placed where its own tooth.json puts it, and then runs
``PeEditor.exe -mb`` as ``lip install`` does: PeEditor writes ``bedrock_server_mod.exe``, a copy of the server binary
that loads PreLoader and through it LeviLamina, and renames the original to ``bedrock_server.exe.bak``. PeEditor runs
once, while the install is staged, so the install's tree hash covers the executable every run starts.

Prepare writes the pinned PreLoader and LeviLamina configs from ``configs/levilamina`` into every run, with crash
report upload and usage statistics switched off. LeviLamina merges an older config over its defaults when its config
version changes, so a file left from another install could carry other values.

Launch runs ``bedrock_server_mod.exe``; it is the measured process. See configs/levilamina/README.md.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, fetch
from bedrock_bench.adapters.base import Dialect, host_os
from bedrock_bench.adapters.bdsfamily import BdsFamily, bds_dialect, fetch_bds
from bedrock_bench.cache import remove_tree
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    ArtifactRef,
    ArtifactSpec,
    ArtifactVersion,
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    Override,
    OverrideCategory,
    RunContext,
)
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

MOD_EXE = "bedrock_server_mod.exe"
PEEDITOR = "PeEditor.exe"
#: ``-m`` writes bedrock_server_mod.exe, ``-b`` keeps the original as bedrock_server.exe.bak (LeviLamina's tooth.json).
PEEDITOR_ARGS = ("-mb",)
TEMPLATES_DIR = "configs/levilamina"
UNPACK_DIR = ".unpack"

PRELOADER_CONFIG = "PreLoaderConfig.json"
LEVILAMINA_CONFIG = "plugins/LeviLamina/config/Config.json"

#: Packages every install gets, in placement order.
BASE_PACKAGES = ("levilamina", "preloader", "peeditor", "crashlogger", "runtime-data")

# --------------------------------------------------------------------------------------------------------------------
# Console dialect
# --------------------------------------------------------------------------------------------------------------------

#: ``13:58:00.123 IFO [Server] text`` on every line, continuation lines of multi-line output included. PreLoader logs
#: before LeviLamina is loaded and spells its levels out (``INFO [PreLoader]``).
LEVILAMINA_PREFIX = re.compile(r"^\s*\d{2}:\d{2}:\d{2}\.\d{3} (?P<level>[A-Z]{3,7}) \[(?P<logger>[^\]]*)\](?: |$)")
LEVILAMINA_LEVELS: Mapping[str, str] = {
    "TRC": "debug",
    "DBG": "debug",
    "IFO": "info",
    "WRN": "warn",
    "ERR": "error",
    "FTL": "fatal",
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "ERROR": "error",
    "FATAL": "fatal",
}
LEVILAMINA_DIALECT = bds_dialect(
    ready=re.compile(r"^Server started(?:\.| in \()"),
    prefix=LEVILAMINA_PREFIX,
    level_map=LEVILAMINA_LEVELS,
    version=re.compile(r"^Version: .* with LeviLamina-(?P<version>[0-9A-Za-z][0-9A-Za-z.+_-]*)\s*$"),
)

# --------------------------------------------------------------------------------------------------------------------
# Config changes
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonChange:
    """One value written over a generated JSON config. ``path`` is the dotted key."""

    path: str
    value: Any
    category: OverrideCategory
    reason: str


#: Changes to LeviLamina's generated config in every run.
LEVILAMINA_CHANGES: tuple[JsonChange, ...] = (
    JsonChange(
        "modules.crashLogger.uploadToSentry",
        False,
        OverrideCategory.HARNESS,
        "no crash report upload during measurement; reports stay in logs/crash",
    ),
    JsonChange(
        "targeted.enableStatistics",
        False,
        OverrideCategory.HARNESS,
        "no usage statistics upload during measurement",
    ),
)

# --------------------------------------------------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    """One package pinned in ``configs/levilamina/server.toml``: its asset and where its files go."""

    name: str
    release: str
    spec: ArtifactSpec
    place: Mapping[str, str]  # archive path -> server path; a trailing "/" copies a directory


def packages_of(entry: ArtifactVersion, source: Path | None = None) -> dict[str, Package]:
    """The ``windows-<package>`` tables of one LeviLamina version."""
    found: dict[str, Package] = {}
    for key, spec in entry.platforms.items():
        where = f"version.{entry.version}.{key}"
        platform, _, name = key.partition("-")
        if platform != "windows" or not name:
            raise ConfigError("package tables are named windows-<package>", file=source, key=where)
        unknown = sorted(set(spec.extra) - {"release", "place"})
        if unknown:
            raise ConfigError(f"unknown keys {unknown}", file=source, key=where)
        release = spec.extra.get("release")
        if not isinstance(release, str) or not release:
            raise ConfigError('needs release = "<package version>"', file=source, key=where)
        place = spec.extra.get("place")
        if not isinstance(place, dict) or not place:
            raise ConfigError('needs place = { "<archive path>" = "<server path>" }', file=source, key=where)
        for src, dest in place.items():
            problem = None
            if not isinstance(dest, str) or not _is_relative(src) or not _is_relative(dest):
                problem = "both sides must be relative paths"
            elif src.endswith("/") != dest.endswith("/"):
                problem = "a directory goes to a directory (both end in /)"
            if problem is not None:
                raise ConfigError(f"place {src!r}: {problem}", file=source, key=where)
        found[name] = Package(name=name, release=release, spec=spec, place=dict(place))
    return found


def _is_relative(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and "\\" not in path and not pure.is_absolute() and ":" not in path and ".." not in pure.parts


def place_package(package: Package, unpacked: Path, server_dir: Path) -> None:
    """Copy a package's files from its unpacked archive into the server. Overwriting any file is an error."""

    def copy_new(src: str | Path, dst: str | Path) -> str:
        target = Path(dst)
        if target.exists():
            relative = target.relative_to(server_dir).as_posix()
            raise fetch.FetchError(f"{package.spec.name} would overwrite {relative}")
        shutil.copyfile(src, target)
        return str(target)

    for src, dest in package.place.items():
        source, target = unpacked / src, server_dir / dest
        if src.endswith("/"):
            if not source.is_dir():
                raise fetch.FetchError(f"{package.spec.name} has no directory {src}")
            shutil.copytree(source, target, dirs_exist_ok=True, copy_function=copy_new)
        else:
            if not source.is_file():
                raise fetch.FetchError(f"{package.spec.name} has no file {src}")
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_new(source, target)


def build_install(target: Path, bds_root: Path, packages: Sequence[tuple[Package, Path]]) -> None:
    """Fill an empty staging directory: the BDS tree, the packages in order, then PeEditor's executable."""
    fetch.copy_install(bds_root, target)
    scratch = target / UNPACK_DIR
    for package, archive in packages:
        unpacked = scratch / package.name
        fetch.extract_zip(archive, unpacked)
        place_package(package, unpacked, target)
    remove_tree(scratch)
    run_peeditor(target)


def run_peeditor(server_dir: Path, *, timeout_s: float = 300.0) -> None:
    """``PeEditor.exe -mb`` in the server directory; it must leave ``bedrock_server_mod.exe`` behind."""
    shown = " ".join([PEEDITOR, *PEEDITOR_ARGS])
    try:
        result = subprocess.run(
            [str(server_dir / PEEDITOR), *PEEDITOR_ARGS],
            cwd=server_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise fetch.FetchError(f"could not run {PEEDITOR}: {exc}") from exc
    tail = ((result.stdout or "") + (result.stderr or "")).strip()[-2000:]
    if result.returncode != 0:
        raise fetch.FetchError(f"{shown} failed with exit code {result.returncode}:\n{tail}")
    if not (server_dir / MOD_EXE).is_file():
        raise fetch.FetchError(f"{shown} did not write {MOD_EXE}:\n{tail}")


# --------------------------------------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------------------------------------


class LeviLaminaAdapter(BdsFamily):
    name: ClassVar[str] = "levilamina"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows"})
    dialect: ClassVar[Dialect] = LEVILAMINA_DIALECT
    thread_groups: ClassVar[Mapping[str, str]] = {
        **BdsFamily.thread_groups,
        "levilamina_pool": r"^ll_default_thread_pool\[",
        "levilamina_logger": r"^logger\[",
    }
    crash_paths: ClassVar[tuple[str, ...]] = ("logs/crash", "%LOCALAPPDATA%/CrashDumps")

    @property
    def platform(self) -> str:
        """LeviLamina exists for Windows only."""
        return "windows"

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        if host_os() != "windows":
            raise ConfigError("LeviLamina runs on Windows only")
        catalog = config.load_artifacts(self.name)
        entry = catalog.versions.get(self.arm.version)
        if entry is None:
            pinned = ", ".join(sorted(catalog.versions))
            raise ConfigError(f"levilamina {self.arm.version} is not pinned (pinned: {pinned})", file=catalog.path)
        meta = dict(entry.meta)
        bds_version = self._bds_version(meta)
        packages = packages_of(entry, catalog.path)
        wanted = list(BASE_PACKAGES)
        missing = [name for name in wanted if name not in packages]
        if missing:
            raise ConfigError(f"levilamina {self.arm.version}: no windows-{missing[0]} table", file=catalog.path)

        bds = fetch_bds(cache, bds_version, self.platform)
        obtained: list[tuple[Package, Path, ArtifactRef]] = []
        releases: dict[str, str] = {}
        for name in wanted:
            package = packages[name]
            if name == "levilamina" and self.arm.artifact is not None:
                archive, ref = fetch.adopt_local(self.arm.artifact, cache)
                releases[name] = "local"
            else:
                archive, ref = fetch.obtain(package.spec, cache)
                releases[name] = package.release
            obtained.append((package, archive, ref))

        key = [f"{package.name}={ref.sha256}" for package, _, ref in obtained]
        root = cache.install_dir(self.name, self.arm.version, self.platform, bds.tree_sha256, *key)
        tree = fetch.ensure_install(
            cache, root, lambda tmp: build_install(tmp, bds.root, [(p, archive) for p, archive, _ in obtained])
        )
        exe = root / MOD_EXE
        if not exe.is_file():
            raise fetch.FetchError(f"the LeviLamina install at {root} has no {MOD_EXE}")
        return Install(
            root=root,
            tree_sha256=tree,
            artifacts=(*bds.artifacts, *(ref for _, _, ref in obtained)),
            extra={
                "bds_version": bds_version,
                "platform": self.platform,
                "exe": MOD_EXE,
                "exe_sha256": sha256_file(exe),
                "levilamina_version": self.arm.version,
                "packages": releases,
                "allocator": meta.get("allocator"),
            },
        )

    def _bds_version(self, meta: Mapping[str, Any]) -> str:
        pinned = meta.get("bds")
        if not isinstance(pinned, str) or not pinned:
            raise ConfigError(f'levilamina {self.arm.version}: its version table needs bds = "<BDS version>"')
        if self.arm.bds and self.arm.bds != pinned:
            raise ConfigError(f"{self.arm.ref} runs bds@{pinned}, but the arm asks for bds@{self.arm.bds}")
        return pinned

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        overrides: list[Override] = []
        files: list[ConfigFile] = []
        written: dict[str, Any] = {}

        def write(relative: str, doc: Any) -> None:
            path = server_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(doc, indent=4, ensure_ascii=False) + "\n")
            files.append(ConfigFile(relative, sha256_file(path)))
            written[relative] = doc

        write(PRELOADER_CONFIG, load_template("PreLoaderConfig.json"))

        levilamina = load_template("LeviLamina.Config.json")
        for change in LEVILAMINA_CHANGES:
            default = get_path(levilamina, change.path)
            set_path(levilamina, change.path, change.value)
            overrides.append(
                Override(
                    change.category,
                    LEVILAMINA_CONFIG,
                    change.path,
                    json_text(default),
                    json_text(change.value),
                    change.reason,
                )
            )
        write(LEVILAMINA_CONFIG, levilamina)

        extra = {
            "json_configs": written,
            "levilamina_version": install.extra.get("levilamina_version"),
            "packages": dict(install.extra.get("packages", {})),
            "allocator": install.extra.get("allocator"),
        }
        return overrides, files, extra

    def launch(self, layout: Layout) -> LaunchSpec:
        self._server_exe = self.executable(layout)
        return LaunchSpec(argv=(str(self._server_exe),), cwd=layout.server_dir)

    def readback(self, layout: Layout) -> list[str]:
        """server.properties as for every BDS-hosted server, plus every JSON config written at prepare."""
        problems = super().readback(layout)
        for relative, written in (layout.extra.get("json_configs") or {}).items():
            path = layout.server_dir / relative
            try:
                found = json.loads(path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                problems.append(f"{relative} is missing after the run")
                continue
            except (OSError, ValueError) as exc:
                problems.append(f"{relative} could not be read after the run: {exc}")
                continue
            changed = sorted(json_diff(written, found))
            if changed:
                more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
                problems.append(f"{relative}: {', '.join(changed[:5])}{more} changed while the server ran")
        return problems

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        facts = super().facts(layout, proc)
        if layout.extra.get("allocator"):
            facts["allocator"] = layout.extra["allocator"]  # linked into LeviLamina.dll, so not visible as a module
        facts.update(
            {
                "levilamina_version": layout.extra.get("levilamina_version"),
                "packages": dict(layout.extra.get("packages") or {}),
                "usage_statistics": "off",
                "crash_upload": "off",
                "crash_logger": "LeviLamina starts CrashLogger.exe as a separate process attached to the server",
            }
        )
        return facts


# --------------------------------------------------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------------------------------------------------


def load_template(name: str) -> Any:
    """A JSON file from configs/levilamina."""
    path = config.project_root() / TEMPLATES_DIR / name
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {TEMPLATES_DIR}/{name}: {exc}") from None


def flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    """The leaves of a JSON document by dotted key. Lists and empty objects are leaves."""
    if isinstance(doc, dict) and doc:
        leaves: dict[str, Any] = {}
        for key, value in doc.items():
            leaves.update(flatten(value, f"{prefix}{key}."))
        return leaves
    return {prefix.removesuffix("."): doc}


def json_diff(old: Any, new: Any) -> dict[str, tuple[str | None, str | None]]:
    """Dotted keys whose value differs between two documents: ``key -> (old, new)`` as JSON text, None if absent."""
    a, b = flatten(old), flatten(new)
    out = {}
    for key in sorted(a.keys() | b.keys()):
        before = json_text(a[key]) if key in a else None
        after = json_text(b[key]) if key in b else None
        if before != after:
            out[key] = (before, after)
    return out


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def get_path(doc: Any, dotted: str) -> Any:
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"the LeviLamina config has no key {dotted}")
        node = node[part]
    return node


def set_path(doc: Any, dotted: str, value: Any) -> None:
    *parents, last = dotted.split(".")
    node = doc
    for part in parents:
        node = node[part]
    node[last] = value


__all__ = [
    "LEVILAMINA_DIALECT",
    "LeviLaminaAdapter",
    "Package",
    "build_install",
    "json_diff",
    "packages_of",
    "place_package",
    "run_peeditor",
]
