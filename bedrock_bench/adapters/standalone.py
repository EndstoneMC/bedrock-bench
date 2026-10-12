# SPDX-License-Identifier: Apache-2.0
"""Shared behaviour of servers that do not host BDS: PocketMine-MP, Nukkit and PowerNukkitX.

These servers bring their own world generator, their own config files and a runtime (PHP or a JVM). Their adapters
are mostly data; this module holds the machinery they share.

- **Artifacts.** The server's own release (a phar or a jar) is pinned in ``servers/<server>/server.toml`` and
  its runtime in ``servers/runtimes.toml``; both are verified by sha256. An install has two parts:
  ``server/``, copied into every run's fresh server directory, and ``defaults/``, the config files the server would
  write on its first start, extracted from the release itself so they always match the pinned version. Runtimes are
  unpacked once into the cache and used in place, read-only.
- **Configs.** Prepare renders each of the adapter's ``config_templates`` from ``servers/<server>/`` onto the file
  of the same name, starting from the shipped default when the release has one. A file the release does not ship
  starts from the rendered template, and the server completes it with its built-in defaults.
- **Helpers.** Small plugins add what a console lacks: a tick counter and player positions, printed as
  ``Gametime is N`` and ``positions: N`` followed by one ``position: <name> <x> <y> <z> <dim>`` line per player.
- :class:`JavaServer` adds the JVM: a fixed heap from the machine profile, UTF-8 console streams and the tick thread,
  which is the JVM's ``main`` thread, found with ``jcmd <pid> Thread.print``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import config, fetch, templates
from bedrock_bench.adapters.base import ServerAdapter, host_os
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    Arm,
    ArtifactRef,
    ArtifactVersion,
    ConfigFile,
    Install,
    Layout,
    LogLine,
    Machine,
    Override,
    PlayerPos,
    RunContext,
    ThreadInfo,
    WorldInfo,
    sha256_json,
)
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

#: File names used for overrides that are command-line arguments or environment variables.
COMMAND_LINE = "<command line>"
ENVIRONMENT = "<environment>"
#: Folders of an install: copied into each run, and the server's shipped default configs.
SERVER_PART = "server"
DEFAULTS_PART = "defaults"


def yaml_scalars(text: str) -> dict[str, str]:
    """Every scalar of a block-style YAML document by dotted path, e.g. ``{"settings.port": "19132"}``."""
    return templates.values("yaml", text)


# --------------------------------------------------------------------------------------------------------------------
# Archives and runtimes
# --------------------------------------------------------------------------------------------------------------------


def extract_tar(archive: Path, dest: Path, *, executables: Iterable[str] = ()) -> None:
    """Unpack a tar archive (gzip or not) into ``dest``, keeping permission bits.

    Members that would land outside ``dest``, links that point outside it, and device or fifo entries are refused.
    ``executables`` must exist after extraction and get the exec bit.
    """
    root = dest.resolve()
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise fetch.FetchError(f"{archive.name}: member {member.name!r} would escape the install directory")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise fetch.FetchError(f"{archive.name}: member {member.name!r} is not a file, directory or link")
            if member.issym() or member.islnk():
                base = root / Path(*name.parent.parts) if member.issym() else root
                link = PurePosixPath(member.linkname)
                resolved = Path(os.path.normpath(base / Path(*link.parts)))
                if link.is_absolute() or not resolved.is_relative_to(root):
                    raise fetch.FetchError(f"{archive.name}: link {member.name!r} points outside the install directory")
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:  # Python 3.11.0-3.11.3; members were checked above
            tf.extractall(dest, members=members)
    for name in executables:
        path = dest / Path(*PurePosixPath(name).parts)
        if not path.is_file():
            raise fetch.FetchError(f"{archive.name}: expected executable {name} is missing from the archive")
        if os.name == "posix":
            os.chmod(path, stat.S_IMODE(path.stat().st_mode) | 0o755)


def extract_archive(archive: Path, dest: Path, *, executables: Iterable[str] = ()) -> None:
    """Unpack a zip or tar archive, chosen by file name."""
    name = archive.name.lower()
    if name.endswith(".zip"):
        fetch.extract_zip(archive, dest, executables=executables)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        extract_tar(archive, dest, executables=executables)
    else:
        raise fetch.FetchError(f"{archive.name}: unknown archive type")


@dataclass(frozen=True)
class Runtime:
    """A verified runtime (a JDK or a PHP build) unpacked in the cache."""

    key: str  # version key in servers/runtimes.toml
    root: Path
    tree_sha256: str
    artifact: ArtifactRef
    paths: Mapping[str, str]  # named paths inside the runtime, e.g. {"java": "jdk-21/bin/java"}

    def path(self, name: str) -> Path:
        try:
            relative = self.paths[name]
        except KeyError:
            raise ConfigError(f"runtime {self.key} has no {name!r} entry in {config.RUNTIMES_FILE}") from None
        return self.root / Path(*PurePosixPath(relative).parts)


def runtime_platform() -> str:
    """``windows`` or ``linux``: which runtime build this host runs."""
    system = host_os()
    if system not in ("windows", "linux"):
        raise ConfigError(f"runtimes are pinned for Windows and Linux only, not for {system}")
    return system


def fetch_runtime(cache: Cache, key: str, platform: str) -> Runtime:
    """Verify and unpack a runtime from ``servers/runtimes.toml`` (``[artifacts.version."<key>".<platform>]``).

    Platform tables may name paths inside the archive besides the download keys (``java = "jdk-21/bin/java"``).
    """
    catalog = config.load_artifacts(config.project_root() / config.RUNTIMES_FILE)
    try:
        spec = catalog.get(key, platform)
    except KeyError as exc:
        raise ConfigError(str(exc.args[0]), file=catalog.path) from None
    archive, ref = fetch.obtain(spec, cache)
    root = cache.install_dir("runtime", key, platform, ref.sha256)
    tree = fetch.ensure_install(cache, root, lambda tmp: extract_archive(archive, tmp, executables=spec.executables))
    paths = {name: value for name, value in spec.extra.items() if isinstance(value, str)}
    return Runtime(key=key, root=root, tree_sha256=tree, artifact=ref, paths=paths)


def fetch_release(
    cache: Cache, server: str, version: str, *, artifact: Path | None = None
) -> tuple[Path, ArtifactRef, ArtifactVersion | None]:
    """The verified server release file (``[artifacts.version."<v>".any]`` in its server file), or a local one.

    Also returns the version's table, whose plain keys (runtime, commit, protocol, ...) the adapter reads; it is None
    for a local artifact of a version that is not pinned.
    """
    catalog = config.load_artifacts(server)
    entry = catalog.versions.get(version)
    if artifact is not None:
        path, ref = fetch.adopt_local(artifact, cache)
        return path, ref, entry
    if entry is None:
        pinned = ", ".join(sorted(catalog.versions))
        raise ConfigError(f"{server} {version} is not pinned (pinned: {pinned})", file=catalog.path)
    try:
        spec = catalog.get(version, "any")
    except KeyError as exc:
        raise ConfigError(str(exc.args[0]), file=catalog.path) from None
    path, ref = fetch.obtain(spec, cache)
    return path, ref, entry


def zip_member(archive: Path, name: str) -> bytes:
    """One file from a zip or jar archive."""
    try:
        with zipfile.ZipFile(archive) as zf:
            return zf.read(name)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise fetch.FetchError(f"{archive.name}: cannot read {name}: {exc}") from exc


def deterministic_zip(dest: Path, files: Mapping[str, bytes]) -> None:
    """Write a zip whose bytes depend only on ``files``: sorted entries, fixed timestamps and permissions."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, files[name])


def run_tool(command: Sequence[str], *, cwd: Path | None = None, timeout_s: float = 300.0) -> str:
    """Run a build or inspection tool; its output on success, a FetchError with the tail of stderr on failure."""
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise fetch.FetchError(f"cannot run {Path(command[0]).name}: {exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-2000:]
        raise fetch.FetchError(f"{Path(command[0]).name} failed with exit code {result.returncode}:\n{tail}")
    return result.stdout


# --------------------------------------------------------------------------------------------------------------------
# Helper output
# --------------------------------------------------------------------------------------------------------------------

_POSITIONS_HEADER = re.compile(r"^positions: (?P<n>\d+)\s*$")
_POSITION_ROW = re.compile(
    r"^position: (?P<name>\S+) (?P<x>-?\d+(?:\.\d+)?) (?P<y>-?\d+(?:\.\d+)?) (?P<z>-?\d+(?:\.\d+)?) (?P<dim>-?\d+)\s*$"
)


def parse_helper_positions(lines: Sequence[LogLine]) -> list[PlayerPos] | None:
    """Positions printed by a helper's ``positions`` command; None until the reply is complete.

    The reply is ``positions: N`` and then N ``position: <name> <x> <y> <z> <dim>`` lines. Other lines in between are
    ignored.
    """
    for index, line in enumerate(lines):
        header = _POSITIONS_HEADER.match(line.text)
        if header is None:
            continue
        expected = int(header["n"])
        positions: list[PlayerPos] = []
        for later in lines[index + 1 :]:
            if len(positions) == expected:
                break
            row = _POSITION_ROW.match(later.text)
            if row is not None:
                positions.append(
                    PlayerPos(row["name"], float(row["x"]), float(row["y"]), float(row["z"]), int(row["dim"]))
                )
        return positions if len(positions) == expected else None
    return None


# --------------------------------------------------------------------------------------------------------------------
# Adapter base
# --------------------------------------------------------------------------------------------------------------------


class StandaloneServer(ServerAdapter):
    """Shared behaviour of servers that ship their own configs and runtime. Not registered itself: it has no name.

    Subclasses provide ``fetch`` (usually through :meth:`build_install`), ``launch``, the dialect and
    ``server_file`` (the release file inside the install's ``server/`` folder).
    """

    #: Release file inside ``server/``; its sha256 is checked before and after every run.
    server_file: ClassVar[str]
    #: Generator recorded in run.json (one of model.GENERATORS).
    generator: ClassVar[str] = "custom"
    #: Templates in the server folder, each rendered onto the file of the same name in the server directory.
    config_templates: ClassVar[tuple[str, ...]] = ()

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def build_install(
        self,
        cache: Cache,
        *,
        release: Path,
        release_ref: ArtifactRef,
        runtime: Runtime | None,
        build: Callable[[Path], None],
        key_parts: Sequence[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> Install:
        """Install the server once into the cache: ``build(staging_dir)`` fills ``server/`` and ``defaults/``.

        The install is keyed by the release, the runtime and ``key_parts`` (hashes of helper sources and the like).
        """
        runtime_sha = runtime.artifact.sha256 if runtime is not None else "-"
        root = cache.install_dir(self.name, self.arm.version, release_ref.sha256, runtime_sha, *key_parts)

        def staged(tmp: Path) -> None:
            (tmp / SERVER_PART).mkdir()
            (tmp / DEFAULTS_PART).mkdir()
            shutil.copyfile(release, tmp / SERVER_PART / self.server_file)
            build(tmp)

        tree = fetch.ensure_install(cache, root, staged)
        server_sha = sha256_file(root / SERVER_PART / self.server_file)
        if runtime is not None:
            combined = sha256_json({"server": tree, "runtime": runtime.tree_sha256})
            artifacts: tuple[ArtifactRef, ...] = (release_ref, runtime.artifact)
        else:
            combined, artifacts = tree, (release_ref,)
        return Install(
            root=root,
            tree_sha256=combined,
            artifacts=artifacts,
            extra={
                "server_file": self.server_file,
                "server_sha256": server_sha,
                "runtime": runtime.key if runtime is not None else None,
                "runtime_root": runtime.root if runtime is not None else None,
                "runtime_paths": dict(runtime.paths) if runtime is not None else {},
                **(extra or {}),
            },
        )

    # ---- prepare --------------------------------------------------------------------------------------------------

    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        server_dir = ctx.work_dir
        fetch.copy_install(install.root / SERVER_PART, server_dir)
        server_sha = sha256_file(server_dir / self.server_file)
        if server_sha != install.extra["server_sha256"]:
            raise fetch.FetchError(f"the fresh copy of {self.server_file} does not match the verified install")
        world = self.prepare_world(server_dir, ctx)
        files: list[ConfigFile] = []
        for name in self.config_templates:
            shipped = install.root / DEFAULTS_PART / name
            if shipped.is_file():
                shutil.copyfile(shipped, server_dir / name)
            files.append(self.write_config(server_dir, name, ctx))
        extra_overrides, extra_files, extra = self.prepare_extra(server_dir, install, ctx, world)
        return Layout(
            server_dir=server_dir,
            overrides=(*extra_overrides, *self.launch_overrides(install)),
            config_files=(*files, *extra_files),
            world=world,
            extra={
                "server_file": self.server_file,
                "exe_sha256": server_sha,
                "runtime": install.extra.get("runtime"),
                "runtime_root": install.extra.get("runtime_root"),
                "runtime_paths": dict(install.extra.get("runtime_paths", {})),
                **extra,
            },
        )

    def prepare_world(self, server_dir: Path, ctx: RunContext) -> WorldInfo:
        """``worlds/<level>``, which must not exist yet."""
        level_dir = server_dir / "worlds" / ctx.workload.world.level_name
        if level_dir.exists():
            raise RuntimeError(f"the fresh server copy already has a world at {level_dir}")
        return WorldInfo(fresh=True, generator=self.generator, path=self.world_data(level_dir))

    def world_data(self, level_dir: Path) -> Path:
        """The path that must not exist before a fresh run: the level folder, unless prepare writes into it."""
        return level_dir

    def prepare_extra(
        self, server_dir: Path, install: Install, ctx: RunContext, world: WorldInfo
    ) -> tuple[Sequence[Override], Sequence[ConfigFile], Mapping[str, Any]]:
        """Hook for files beyond ``config_templates``: ``(overrides, config files, layout extra)``."""
        return (), (), {}

    def launch_overrides(self, install: Install) -> Sequence[Override]:
        """Command-line arguments that change the server's behaviour, recorded like config overrides."""
        return ()

    # ---- after the run --------------------------------------------------------------------------------------------

    def executable(self, layout: Layout) -> Path:
        """The release file in the run's copy; its sha256 is checked before and after the run."""
        return layout.server_dir / layout.extra["server_file"]

    def pinned_runtime(self, entry: ArtifactVersion | None) -> str:
        """``runtime`` in this version's table: a version in servers/runtimes.toml."""
        runtime = entry.meta.get("runtime") if entry is not None else None
        if not isinstance(runtime, str):
            raise ConfigError(
                f'{self.arm.ref} needs runtime = "<version in {config.RUNTIMES_FILE}>" in its version table',
                file=config.server_dir(self.name) / config.SERVER_FILE,
            )
        return runtime

    def runtime_path(self, layout: Layout, name: str) -> Path:
        """A named path inside the layout's runtime (``java``, ``jcmd``, ``php``, ...)."""
        root, paths = layout.extra.get("runtime_root"), layout.extra.get("runtime_paths", {})
        if root is None or name not in paths:
            raise ConfigError(f"{self.name}: this layout has no runtime path {name!r}; fetch the arm natively first")
        return Path(root) / Path(*PurePosixPath(paths[name]).parts)


# --------------------------------------------------------------------------------------------------------------------
# JVM servers
# --------------------------------------------------------------------------------------------------------------------

#: ``"main" #1 [12345] prio=5 os_prio=0 ... tid=0x... nid=12345 runnable``; older JDKs print ``nid=0x3039``.
_JCMD_MAIN = re.compile(r'^"main" #\d+(?: \[(?P<tid>\d+)\])?.*?\bnid=(?P<nid>0x[0-9a-fA-F]+|\d+)')
UTF8_STREAMS = ("-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8")


def heap_mib(heap: str) -> int:
    """``8G`` -> 8192, ``512M`` -> 512."""
    match = re.fullmatch(r"([1-9][0-9]*)([MG])", heap)
    if match is None:
        raise ConfigError(f"JVM heap must look like 8G or 512M, got {heap!r}")
    return int(match[1]) * (1024 if match[2] == "G" else 1)


def main_thread_from_jcmd(text: str) -> int | None:
    """The OS thread id of the JVM's ``main`` thread in ``jcmd <pid> Thread.print`` output."""
    for line in text.splitlines():
        match = _JCMD_MAIN.match(line.strip())
        if match is not None:
            if match["tid"]:
                return int(match["tid"])
            return int(match["nid"], 0)
    return None


def java_release(home: Path) -> str | None:
    """``JAVA_RUNTIME_VERSION`` (else ``JAVA_VERSION``) from a JDK's ``release`` file."""
    try:
        text = (home / "release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', text, re.MULTILINE))
    return values.get("JAVA_RUNTIME_VERSION") or values.get("JAVA_VERSION")


class JavaServer(StandaloneServer):
    """A server that runs on the JVM: ``java -Xms<heap> -Xmx<heap> -jar <server_file>``.

    The heap comes from the machine profile and is the same for every JVM server (``-Xms`` equals ``-Xmx``). Console
    streams are UTF-8. The game loop runs on the JVM's ``main`` thread; its OS thread id comes from ``jcmd`` once the
    server is ready (before any measured window).
    """

    tick_thread_rule: ClassVar[str] = "java_main"
    crash_paths: ClassVar[tuple[str, ...]] = ("logs",)

    def __init__(self, arm: Arm, machine: Machine) -> None:
        super().__init__(arm, machine)
        self._process: psutil.Process | None = None
        self._jcmd: Path | None = None
        self._main_tid: int | None = None
        self._main_looked_up = False

    def jvm_args(self) -> list[str]:
        heap = self.machine.jvm_heap
        heap_mib(heap)
        return [f"-Xms{heap}", f"-Xmx{heap}", *UTF8_STREAMS]

    def java_launch(self, layout: Layout, *args: str) -> tuple[str, ...]:
        """The JVM command line for the release file plus ``args``."""
        self._jcmd = self.runtime_path(layout, "jcmd")
        return (str(self.runtime_path(layout, "java")), *self.jvm_args(), "-jar", layout.extra["server_file"], *args)

    def server_process(self, launched: psutil.Process) -> psutil.Process:
        self._process = launched
        return launched

    def tick_thread(self, threads: Sequence[ThreadInfo]) -> int | None:
        """The JVM ``main`` thread from ``jcmd`` (asked once); the ``java_main`` name rule when that fails."""
        if not self._main_looked_up and self._process is not None and self._jcmd is not None:
            self._main_looked_up = True
            try:
                output = run_tool([str(self._jcmd), str(self._process.pid), "Thread.print"], timeout_s=60.0)
            except fetch.FetchError:
                output = ""
            self._main_tid = main_thread_from_jcmd(output)
        live = {t.tid for t in threads}
        if self._main_tid is not None and self._main_tid in live:
            return self._main_tid
        return super().tick_thread(threads)

    def crash_artifacts(self, layout: Layout, since_epoch_ms: int) -> list[Path]:
        """Server logs plus JVM fatal error files (``hs_err_pid*.log``) written since the run started."""
        found = set(super().crash_artifacts(layout, since_epoch_ms))
        cutoff = since_epoch_ms / 1000
        for path in layout.server_dir.glob("hs_err_pid*.log"):
            try:
                if path.stat().st_mtime >= cutoff:
                    found.add(path)
            except OSError:
                continue
        return sorted(found)

    def jvm_facts(self, layout: Layout) -> dict[str, Any]:
        root, paths = layout.extra.get("runtime_root"), layout.extra.get("runtime_paths", {})
        version = java_release(Path(root) / paths["home"]) if root is not None and "home" in paths else None
        return {
            "allocator": "jvm",
            "jvm": version,
            "jvm_heap_mib": heap_mib(self.machine.jvm_heap),
            "jvm_flags": self.jvm_args(),
            "gc": "G1 (JVM default)",
            "csg_setting": False,
        }


__all__ = [
    "COMMAND_LINE",
    "ENVIRONMENT",
    "JavaServer",
    "Runtime",
    "StandaloneServer",
    "extract_archive",
    "extract_tar",
    "fetch_release",
    "fetch_runtime",
    "main_thread_from_jcmd",
    "parse_helper_positions",
    "yaml_scalars",
]
