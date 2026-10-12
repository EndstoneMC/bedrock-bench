# SPDX-License-Identifier: Apache-2.0
"""The local cache: verified downloads, installs, virtual environments, world templates and server work copies.

The root is ``$BBENCH_CACHE`` if set, else ``%LOCALAPPDATA%\\bedrock-bench`` on Windows and
``$XDG_CACHE_HOME/bedrock-bench`` (default ``~/.cache/bedrock-bench``) elsewhere. Directory names under it are short
hashes, so deep server trees stay well below the 260-character ``MAX_PATH`` limit on Windows.

Everything is built in a staging directory and moved into place in one rename, so an entry that exists is complete.
Entries are immutable once in place; a changed input gets a new key.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

ENV_CACHE = "BBENCH_CACHE"
APP_DIR = "bedrock-bench"
STAGING_PREFIX = ".tmp-"


def default_root() -> Path:
    """The cache root for this user and platform."""
    override = os.environ.get(ENV_CACHE)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / APP_DIR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_DIR
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_DIR


def short_key(*parts: str, length: int = 12) -> str:
    """A short, stable directory name for a tuple of strings."""
    if not parts:
        raise ValueError("short_key needs at least one part")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def remove_tree(path: Path) -> None:
    """Delete a directory tree, including read-only files (installs and venvs are made read-only)."""

    def make_writable_and_retry(func: Callable[[str], object], target: str, *_: object) -> None:
        for entry in (os.path.dirname(target), target):
            try:
                os.chmod(entry, stat.S_IRWXU)
            except OSError:
                pass
        func(target)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


class Cache:
    """Paths inside the cache root. Creating an instance touches nothing on disk."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    def __repr__(self) -> str:
        return f"Cache({str(self.root)!r})"

    @property
    def downloads(self) -> Path:
        return self.root / "dl"

    @property
    def installs(self) -> Path:
        return self.root / "i"

    @property
    def venvs(self) -> Path:
        return self.root / "venv"

    @property
    def templates(self) -> Path:
        return self.root / "tpl"

    @property
    def bots(self) -> Path:
        return self.root / "bots"

    @property
    def work(self) -> Path:
        return self.root / "w"

    def download_path(self, sha256: str, filename: str) -> Path:
        """Where a verified download with this content hash is kept."""
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError(f"not a lowercase sha256 hex digest: {sha256!r}")
        if not filename or Path(filename).name != filename or filename in (".", ".."):
            raise ValueError(f"not a plain file name: {filename!r}")
        return self.downloads / sha256 / filename

    def install_dir(self, *key: str) -> Path:
        """An install keyed by its inputs, e.g. ``("bds", "1.26.52.3", "windows", <zip sha256>)``."""
        return self.installs / short_key(*key)

    def venv_dir(self, name: str) -> Path:
        """A virtual environment with a readable name such as ``endstone-0.11.14-cp313``."""
        if not name or Path(name).name != name or name.startswith("."):
            raise ValueError(f"not a plain directory name: {name!r}")
        return self.venvs / name

    def template_dir(self, *key: str) -> Path:
        """A pre-generated world template keyed by server, version and scenario."""
        return self.templates / short_key(*key)

    def work_dir(self, run_id: str) -> Path:
        """A short scratch directory for one run's fresh server copy. Not created here."""
        return self.work / short_key(run_id, length=10)

    @contextmanager
    def staging(self, final: Path) -> Iterator[Path]:
        """Build a directory next to ``final`` and move it into place in one rename when the block succeeds.

        If another process put ``final`` in place first, its copy wins and this one is discarded. On error the
        staging directory is removed and ``final`` is untouched.
        """
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=final.parent))
        try:
            yield tmp
        except BaseException:
            remove_tree(tmp)
            raise
        try:
            os.replace(tmp, final)
        except OSError:
            remove_tree(tmp)
            if not final.is_dir():
                raise

    @contextmanager
    def staged_file(self, final: Path) -> Iterator[Path]:
        """Write a file under a temporary name next to ``final`` and rename it into place on success."""
        final.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=STAGING_PREFIX, dir=final.parent)
        os.close(fd)
        tmp = Path(name)
        try:
            yield tmp
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)

    def prune_staging(self, older_than_s: float = 86400.0) -> list[Path]:
        """Remove staging leftovers of interrupted builds. Returns what was removed."""
        removed: list[Path] = []
        cutoff = time.time() - older_than_s
        for area in (self.downloads, self.installs, self.venvs, self.templates, self.bots, self.work):
            if not area.is_dir():
                continue
            for parent in (area, *[p for p in area.iterdir() if p.is_dir()]):
                for entry in parent.glob(STAGING_PREFIX + "*"):
                    try:
                        if entry.stat().st_mtime >= cutoff:
                            continue
                        if entry.is_dir():
                            remove_tree(entry)
                        else:
                            entry.unlink()
                        removed.append(entry)
                    except OSError:
                        continue
        return removed
