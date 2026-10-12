# SPDX-License-Identifier: Apache-2.0
"""Download, verify and unpack pinned artifacts.

Every artifact pinned in ``servers/<server>/server.toml`` carries a URL, a size and a sha256. A verified copy
is kept in the cache under ``dl/<sha256>/<name>``; nothing is used before its hash has been checked. Sources are tried
in order: the cache, an artifact directory (``--artifact-dir`` or ``$BBENCH_ARTIFACT_DIR``) holding files under their
pinned names, then the URL.

Mojang's server is covered by the Minecraft EULA. Downloading it needs ``--accept-eula`` or
``BBENCH_ACCEPT_MINECRAFT_EULA=1``. A copy that is already cached, or that the user supplies in an artifact directory,
does not.

Installs are unpacked once into the cache, made read-only and recorded with their tree hash. Every later fetch
re-hashes the tree, so a cache entry that changed after install is refused instead of measured. Each run then gets a
fresh, writable copy (:func:`copy_install`).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bedrock_bench import __version__
from bedrock_bench.cache import ENV_CACHE, Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.model import ArtifactRef, ArtifactSpec, ExitCode
from bedrock_bench.provenance import DEFAULT_TREE_EXCLUDES, sha256_file, tree_sha256

log = logging.getLogger(__name__)

ENV_ACCEPT_EULA = "BBENCH_ACCEPT_MINECRAFT_EULA"
ENV_ARTIFACT_DIR = "BBENCH_ARTIFACT_DIR"

#: Licences an artifact's ``eula`` key can name: key -> (name, where to read it).
EULAS = {"minecraft": ("Minecraft End User License Agreement", "https://www.minecraft.net/eula")}

USER_AGENT = f"Mozilla/5.0 (compatible; bedrock-bench/{__version__})"  # minecraft.net stalls agents that carry a URL

#: Longest path Windows APIs accept without long-path support (MAX_PATH minus the terminating NUL). Servers are not
#: necessarily long-path aware, so installs and run copies must stay below it.
WINDOWS_MAX_PATH = 259

_CHUNK = 1 << 20


class FetchError(RuntimeError):
    """A download failed, or a file or install did not match its pinned hash."""


class EulaNotAccepted(ConfigError):
    """A download needs a licence the user has not accepted."""


# --------------------------------------------------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------------------------------------------------


def _log_report(message: str) -> None:
    log.info("%s", message)


@dataclass(frozen=True)
class FetchOptions:
    """How artifacts may be obtained. ``report`` receives one line per download or copy."""

    accept_eula: bool = False
    artifact_dir: Path | None = None
    report: Callable[[str], None] = field(default=_log_report, compare=False)

    @classmethod
    def from_env(cls) -> FetchOptions:
        accepted = os.environ.get(ENV_ACCEPT_EULA, "").strip().lower() in ("1", "true", "yes")
        directory = os.environ.get(ENV_ARTIFACT_DIR)
        return cls(accept_eula=accepted, artifact_dir=Path(directory) if directory else None)


_options: ContextVar[FetchOptions | None] = ContextVar("bedrock_bench_fetch_options", default=None)


def current_options() -> FetchOptions:
    """The options set by :func:`using`, else the ones in the environment."""
    return _options.get() or FetchOptions.from_env()


@contextlib.contextmanager
def using(options: FetchOptions) -> Iterator[FetchOptions]:
    """Apply ``options`` to every fetch in the block, including fetches made by adapters."""
    token = _options.set(options)
    try:
        yield options
    finally:
        _options.reset(token)


# --------------------------------------------------------------------------------------------------------------------
# Single files
# --------------------------------------------------------------------------------------------------------------------


def obtain(spec: ArtifactSpec, cache: Cache, options: FetchOptions | None = None) -> tuple[Path, ArtifactRef]:
    """The verified local copy of a pinned artifact, downloading it if needed."""
    opts = options or current_options()
    if not spec.sha256:
        raise ConfigError(f"{spec.name} is not pinned: add its sha256 to the artifacts file before fetching it")
    ref = ArtifactRef(name=spec.name, url=spec.url, sha256=spec.sha256, size=spec.size, source=spec.source)
    final = cache.download_path(spec.sha256, spec.name)
    if final.is_file():
        _verify_file(final, spec.sha256, spec.size, label=spec.name)
        return final, ref

    if opts.artifact_dir is not None:
        candidate = Path(opts.artifact_dir) / spec.name
        if candidate.is_file():
            _verify_file(candidate, spec.sha256, spec.size, label=str(candidate))
            with cache.staged_file(final) as tmp:
                shutil.copyfile(candidate, tmp)
            opts.report(f"copied {spec.name} from the artifact directory")
            return final, ref

    if spec.eula:
        name, link = EULAS.get(spec.eula, (f"{spec.eula} licence", ""))
        where = f" ({link})" if link else ""
        if not opts.accept_eula:
            raise EulaNotAccepted(
                f"{spec.name} is covered by the {name}{where}. Read it, then accept it with --accept-eula or "
                f"{ENV_ACCEPT_EULA}=1, or put the file in an artifact directory (--artifact-dir)"
            )
        opts.report(f"{spec.name} is covered by the {name}{where}; downloading it as accepted")
    size = f" ({spec.size / 2**20:.1f} MiB)" if spec.size else ""
    opts.report(f"downloading {spec.name}{size} from {spec.url}")
    with cache.staged_file(final) as tmp:
        download(spec.url, tmp, sha256=spec.sha256, size=spec.size)
    return final, ref


def adopt_local(path: Path | str, cache: Cache, *, source: str = "local") -> tuple[Path, ArtifactRef]:
    """Hash a user-supplied artifact and keep a copy in the cache, so later edits of the original change nothing."""
    original = Path(path)
    if not original.is_file():
        raise ConfigError(f"local artifact not found: {original}")
    digest = sha256_file(original)
    final = cache.download_path(digest, original.name)
    if not final.is_file():
        with cache.staged_file(final) as tmp:
            shutil.copyfile(original, tmp)
    _verify_file(final, digest, None, label=original.name)
    ref = ArtifactRef(name=original.name, url=None, sha256=digest, size=final.stat().st_size, source=source)
    return final, ref


def download(
    url: str,
    dest: Path,
    *,
    sha256: str,
    size: int | None = None,
    timeout_s: float = 60.0,
    attempts: int = 3,
) -> None:
    """Stream ``url`` to ``dest`` and check its size and sha256. ``dest`` is deleted when the check fails."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _download_once(url, dest, timeout_s)
            last_error = None
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            dest.unlink(missing_ok=True)
            client_error = isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500
            if client_error or attempt == attempts:
                break
            time.sleep(2.0 * attempt)
    if last_error is not None:
        raise FetchError(f"download of {url} failed: {last_error}") from last_error
    try:
        _verify_file(dest, sha256, size, label=url)
    except FetchError:
        dest.unlink(missing_ok=True)
        raise


def _download_once(url: str, dest: Path, timeout_s: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout_s) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out, _CHUNK)


def _verify_file(path: Path, sha256: str, size: int | None, *, label: str) -> None:
    actual_size = path.stat().st_size
    if size is not None and actual_size != size:
        raise FetchError(f"{label}: size {actual_size} does not match the pinned size {size}")
    actual = sha256_file(path)
    if actual != sha256.lower():
        raise FetchError(f"{label}: sha256 {actual} does not match the pinned {sha256}")


# --------------------------------------------------------------------------------------------------------------------
# Installs
# --------------------------------------------------------------------------------------------------------------------


def extract_zip(archive: Path, dest: Path, *, executables: Iterable[str] = ()) -> None:
    """Unpack a zip archive into ``dest``, keeping Unix permission bits and setting the exec bit on ``executables``.

    Python's own extractor drops permission bits, so a Linux server binary would not start. Members that would land
    outside ``dest`` are refused, and so is an archive whose deepest file would exceed the Windows path limit there.
    """
    wanted = {PurePosixPath(name).as_posix() for name in executables}
    root = dest.resolve()
    found: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if os.name == "nt" and members:
            deepest = max(len(info.filename) for info in members)
            if len(str(root)) + 1 + deepest > WINDOWS_MAX_PATH:
                raise FetchError(
                    f"{archive.name}: its deepest file needs a path of {len(str(root)) + 1 + deepest} characters "
                    f"under {root}, over the Windows limit of {WINDOWS_MAX_PATH}; set {ENV_CACHE} to a shorter "
                    "directory"
                )
        for info in members:
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or (name.parts and name.parts[0].endswith(":")):
                raise FetchError(f"{archive.name}: member {info.filename!r} would escape the install directory")
            target = (dest / Path(*name.parts)) if name.parts else dest
            if not target.resolve().is_relative_to(root):
                raise FetchError(f"{archive.name}: member {info.filename!r} would escape the install directory")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
            if os.name == "posix":
                mode = (info.external_attr >> 16) & 0o777 or 0o644
                if name.as_posix() in wanted:
                    mode |= 0o755
                os.chmod(target, mode | stat.S_IRUSR | stat.S_IWUSR)
            found.add(name.as_posix())
    missing = sorted(wanted - found)
    if missing:
        raise FetchError(f"{archive.name}: expected executables missing from the archive: {', '.join(missing)}")


def ensure_install(
    cache: Cache,
    final: Path,
    build: Callable[[Path], None],
    *,
    exclude_dirs: Iterable[str] = DEFAULT_TREE_EXCLUDES,
) -> str:
    """Build ``final`` once with ``build(staging_dir)`` and return its tree hash.

    The finished tree is made read-only and its hash recorded next to it (``<final>.json``). Every call re-hashes the
    tree and raises :class:`FetchError` when it no longer matches, so a modified cache entry is never used.
    """
    excludes = tuple(exclude_dirs)
    sidecar = final.with_name(final.name + ".json")
    if not final.is_dir():
        with cache.staging(final) as tmp:
            build(tmp)
            built = tree_sha256(tmp, exclude_dirs=excludes)
            make_read_only(tmp)
        actual = tree_sha256(final, exclude_dirs=excludes)
        if actual != built:
            raise FetchError(f"{final.name}: another process installed a different tree at the same time")
        sidecar.write_text(json.dumps({"tree_sha256": actual}) + "\n", encoding="utf-8")
        return actual
    actual = tree_sha256(final, exclude_dirs=excludes)
    try:
        recorded = json.loads(sidecar.read_text(encoding="utf-8"))["tree_sha256"]
    except (OSError, ValueError, KeyError):
        sidecar.write_text(json.dumps({"tree_sha256": actual}) + "\n", encoding="utf-8")
        return actual
    if recorded != actual:
        raise FetchError(
            f"cache entry {final} changed after it was installed (tree {actual[:12]}, recorded {recorded[:12]}); "
            "delete it and fetch again"
        )
    return actual


def make_read_only(root: Path) -> None:
    """Clear the write bits under ``root`` (directories too on POSIX). Symlinks are left alone."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath, name)
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.stat().st_mode) & ~0o222)
        if os.name == "posix":
            for name in dirnames:
                path = Path(dirpath, name)
                if not path.is_symlink():
                    os.chmod(path, stat.S_IMODE(path.stat().st_mode) & ~0o222)
    if os.name == "posix":
        os.chmod(root, stat.S_IMODE(root.stat().st_mode) & ~0o222)


def copy_install(src: Path, dst: Path) -> None:
    """Copy a (read-only) install into ``dst`` as a writable tree. ``dst`` must be absent or empty.

    File contents and exec bits are copied; write permission is added for the owner so the server can write its own
    files. Symlinks are recreated, not followed.
    """
    dst.mkdir(parents=True, exist_ok=True)
    if any(dst.iterdir()):
        raise FileExistsError(f"refusing to copy an install into a non-empty directory: {dst}")
    for dirpath, dirnames, filenames in os.walk(src):
        here = Path(dirpath)
        target_dir = dst / here.relative_to(src)
        kept = []
        for name in dirnames:
            path = here / name
            if path.is_symlink():
                os.symlink(os.readlink(path), target_dir / name, target_is_directory=True)
            else:
                (target_dir / name).mkdir()
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = here / name
            target = target_dir / name
            if path.is_symlink():
                os.symlink(os.readlink(path), target)
                continue
            shutil.copyfile(path, target)
            if os.name == "posix":
                os.chmod(target, stat.S_IMODE(path.stat().st_mode) | stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------------------------------------------------------
# bbench fetch
# --------------------------------------------------------------------------------------------------------------------


def fetch_cli(args: argparse.Namespace) -> int:
    """``bbench fetch [server@version ...]``: download, verify and install, by default every benchmark default."""
    from bedrock_bench import adapters, config, runner

    def report(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    env = FetchOptions.from_env()
    opts = FetchOptions(
        accept_eula=bool(args.accept_eula) or env.accept_eula,
        artifact_dir=args.artifact_dir or env.artifact_dir,
        report=report,
    )
    machine = config.default_machine()
    cache = Cache()
    failed = 0
    arms, skipped = runner.session_arms(args.tags, machine)
    for line in skipped:
        report(f"skipping {line}")
    with using(opts):
        for arm in arms:
            adapter = adapters.create(arm, machine)
            try:
                install = adapter.fetch(cache)
            except FetchError as exc:
                print(f"{arm.ref}: {exc}", file=sys.stderr)
                failed += 1
                continue
            sources = ", ".join(f"{a.name} ({a.source})" for a in install.artifacts)
            print(f"{arm.ref}: ok, tree {install.tree_sha256[:12]}: {sources}")
    return ExitCode.ENV_GATE if failed else ExitCode.OK
