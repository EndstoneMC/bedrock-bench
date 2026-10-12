# SPDX-License-Identifier: Apache-2.0
"""Provenance: what exactly ran, and on what, without recording who ran it or where.

- :func:`tree_sha256` hashes an install tree (paths, sizes, contents, exec bits), so a cache entry that changed after
  it was installed is caught before it is measured.
- :func:`harness_info` records the bedrock-bench version and the ``git describe`` and dirty state of the checkout.
- :func:`machine_fingerprint` describes the hardware (OS, CPU model, core counts, RAM) and derives a stable id from it.
- :class:`Scrubber` removes absolute paths, the user name and the host name from anything written to a result file;
  :meth:`Scrubber.leaks` finds what is left.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bedrock_bench import __version__
from bedrock_bench.model import sha256_json

#: Directory names left out of install-tree hashes: interpreter caches that differ between machines and runs.
DEFAULT_TREE_EXCLUDES = ("__pycache__",)

_CHUNK = 1 << 20


# --------------------------------------------------------------------------------------------------------------------
# Content hashes
# --------------------------------------------------------------------------------------------------------------------


def sha256_file(path: Path | str) -> str:
    """sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class TreeEntry:
    path: str  # POSIX path relative to the tree root
    kind: str  # "f" file, "x" executable file (POSIX only), "l" symlink
    size: int
    sha256: str  # content hash; for a symlink, the hash of its target string


def tree_manifest(root: Path | str, *, exclude_dirs: Iterable[str] = DEFAULT_TREE_EXCLUDES) -> list[TreeEntry]:
    """Every file and symlink under ``root``, sorted by path. Symlinks are recorded, never followed.

    Empty directories and modification times are not part of the manifest. The exec bit only counts on POSIX:
    Windows derives it from the file extension.
    """
    base = Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"not a directory: {base}")
    skip = frozenset(exclude_dirs)
    entries: list[TreeEntry] = []
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        kept = []
        for name in dirnames:
            path = current / name
            if path.is_symlink():
                entries.append(_link_entry(base, path))
            elif name not in skip:
                kept.append(name)
        dirnames[:] = sorted(kept)
        for name in filenames:
            path = current / name
            if path.is_symlink():
                entries.append(_link_entry(base, path))
                continue
            info = path.stat()
            executable = os.name == "posix" and bool(info.st_mode & stat.S_IXUSR)
            rel = path.relative_to(base).as_posix()
            entries.append(TreeEntry(rel, "x" if executable else "f", info.st_size, sha256_file(path)))
    entries.sort(key=lambda entry: entry.path)
    return entries


def _link_entry(base: Path, path: Path) -> TreeEntry:
    target = os.readlink(path)
    data = target.encode("utf-8", "surrogateescape")
    return TreeEntry(path.relative_to(base).as_posix(), "l", len(data), hashlib.sha256(data).hexdigest())


def tree_sha256(root: Path | str, *, exclude_dirs: Iterable[str] = DEFAULT_TREE_EXCLUDES) -> str:
    """One hash for a whole directory tree; see :func:`tree_manifest` for what it covers."""
    digest = hashlib.sha256()
    for entry in tree_manifest(root, exclude_dirs=exclude_dirs):
        digest.update(f"{entry.path}\t{entry.kind}\t{entry.size}\t{entry.sha256}\n".encode())
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------------------------------
# Harness and machine
# --------------------------------------------------------------------------------------------------------------------


#: The folders whose tracked files decide a run's harness: a change to one of them marks the run dirty.
DIRTY_PATHS = ("bedrock_bench", "bots", "configs", "servers")


def git_describe(repo: Path | str | None = None) -> tuple[str | None, bool | None]:
    """``(describe, dirty)`` of a git checkout, e.g. ``("v0.1.0-3-g1a2b3c4", False)``.

    Both are None when ``repo`` is not a checkout (an installed wheel) or git is not available. Only changes to tracked
    files under :data:`DIRTY_PATHS` count as dirty; untracked files, ``runs/`` and ``results/`` never do.
    """
    if repo is None:
        from bedrock_bench.config import project_root

        repo = project_root()
    root = Path(repo)
    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        return None, None
    try:
        describe = _git(git, root, "describe", "--tags", "--long", "--always", "--abbrev=7")
        status = _git(git, root, "status", "--porcelain", "--untracked-files=no", "--", *DIRTY_PATHS)
    except (OSError, subprocess.SubprocessError):
        return None, None
    return describe or None, bool(status)


def _git(git: str, root: Path, *args: str) -> str:
    result = subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def harness_info(backend: str, repo: Path | str | None = None) -> dict[str, Any]:
    """The ``harness`` block of run.json."""
    describe, dirty = git_describe(repo)
    return {
        "version": __version__,
        "git": describe,
        "dirty": dirty,
        "python": platform.python_version(),
        "backend": backend,
    }


def machine_fingerprint() -> dict[str, Any]:
    """Hardware description for run.json's ``machine`` block (the part that does not need CPU topology).

    ``fingerprint_id`` hashes the OS family, CPU model, logical and physical core counts and RAM rounded to whole GiB,
    so it survives OS updates and reboots but changes with the hardware. It contains no host or user name.
    ``host.py`` adds core classes, power settings and the TSC calibration.
    """
    import psutil

    os_name, kernel = _os_description()
    cpu = cpu_model()
    logical = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    physical = psutil.cpu_count(logical=False)
    ram_gib = round(psutil.virtual_memory().total / 2**30, 1)
    identity = {
        "os": _os_family(),
        "cpu": cpu,
        "logical": logical,
        "physical": physical,
        "ram_gib": round(ram_gib),
    }
    return {
        "fingerprint_id": sha256_json(identity)[:16],
        "os": os_name,
        "kernel": kernel,
        "cpu": cpu,
        "logical": logical,
        "physical": physical,
        "ram_gib": ram_gib,
        "virtualized": _virtualized(),
    }


def _os_family() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _os_description() -> tuple[str, str | None]:
    if sys.platform == "win32":
        return f"Windows {platform.release()}", platform.version()
    if sys.platform.startswith("linux"):
        try:
            release = platform.freedesktop_os_release()
            name = release.get("PRETTY_NAME") or release.get("NAME") or "Linux"
        except OSError:
            name = "Linux"
        return name, platform.release()
    return platform.platform(terse=True), platform.release()


def cpu_model() -> str:
    """The CPU's marketing name, e.g. ``Intel(R) Core(TM) i7-14700K``."""
    if sys.platform == "win32":
        try:
            import winreg

            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if str(value).strip():
                    return " ".join(str(value).split())
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, _, value = line.partition(":")
                    if key.strip() == "model name" and value.strip():
                        return " ".join(value.split())
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def _virtualized() -> bool | None:
    """True when the CPU reports a hypervisor (Linux only; None where it cannot be told cheaply)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("flags"):
                    return "hypervisor" in line.split(":", 1)[-1].split()
    except OSError:
        return None
    return False


# --------------------------------------------------------------------------------------------------------------------
# Path scrubbing
# --------------------------------------------------------------------------------------------------------------------

_DEFAULT = object()

#: Absolute Windows paths (drive or UNC) that survived root replacement.
_WINDOWS_ABS = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])[^\s\"'<>|]*")
#: POSIX locations that identify a user or a machine; system paths such as /usr/lib are left alone.
_POSIX_PRIVATE = re.compile(
    r"(?<![\w.~>-])/(?:home|Users|root|tmp|mnt|media|var/folders|var/tmp|run/user)(?:/[^\s\"'<>|]*)?"
)
#: Names too common to replace as words: they would mangle ordinary text.
_COMMON_WORDS = frozenset(
    {"admin", "administrator", "user", "users", "root", "runner", "ubuntu", "debian", "guest", "test", "server"}
)
_REST = r"(?P<rest>(?:[\\/][^\s\"'<>|\\/]*)*)"
#: A root ends at a separator or where the path token ends, so ``/w/abc`` does not match inside ``/w/abc1``.
_ROOT_END = r"(?![^\\/\s\"'<>|])"


class Scrubber:
    """Replaces machine-specific details in strings and in nested JSON-like data.

    ``roots`` maps a placeholder to a directory, e.g. ``{"<run>": run_dir, "<server>": work_dir}``. A path under a root
    becomes ``<placeholder>/rest/with/forward/slashes``; the most specific root wins. The cache root, the project
    checkout, the temporary directory and the home directory are always known. Any absolute path that is left becomes
    ``<abs>/<file name>``. The user name and the host name are replaced as words.
    """

    def __init__(
        self,
        roots: Mapping[str, Path | str] | None = None,
        *,
        user: str | None | object = _DEFAULT,
        host: str | None | object = _DEFAULT,
    ) -> None:
        known: dict[str, str] = {}
        for placeholder, path in _default_roots().items():
            known[placeholder] = path
        for placeholder, path in (roots or {}).items():
            known[placeholder] = str(path)
        pairs = [(placeholder, path) for placeholder, path in known.items() if path and len(Path(path).parts) > 1]
        pairs.sort(key=lambda pair: len(pair[1]), reverse=True)
        flags = re.IGNORECASE if os.name == "nt" else 0
        self._roots = [(placeholder, re.compile(_root_pattern(path) + _REST, flags)) for placeholder, path in pairs]
        self._words = []
        for placeholder, word in (
            ("<user>", _user() if user is _DEFAULT else user),
            ("<host>", _host() if host is _DEFAULT else host),
        ):
            if isinstance(word, str) and _replaceable_word(word):
                self._words.append((placeholder, re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])", re.IGNORECASE)))

    def text(self, value: str) -> str:
        """Scrub one string."""
        for placeholder, pattern in self._roots:
            value = pattern.sub(lambda m, p=placeholder: p + m.group("rest").replace("\\", "/"), value)
        value = _WINDOWS_ABS.sub(_abs_placeholder, value)
        value = _POSIX_PRIVATE.sub(_abs_placeholder, value)
        for placeholder, pattern in self._words:
            value = pattern.sub(placeholder, value)
        return value

    def __call__(self, obj: Any) -> Any:
        """A scrubbed copy of a JSON-like value: strings everywhere, including mapping keys, are scrubbed."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, os.PathLike):
            return self.text(os.fspath(obj))
        if isinstance(obj, Mapping):
            return {self(key) if isinstance(key, str) else key: self(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self(item) for item in obj]
        return obj

    def leaks(self, obj: Any) -> list[str]:
        """Strings in ``obj`` that still contain an absolute private path, the user name or the host name."""
        found: list[str] = []
        for value in _strings(obj):
            if _WINDOWS_ABS.search(value) or _POSIX_PRIVATE.search(value):
                found.append(value)
            elif any(pattern.search(value) for _, pattern in self._words):
                found.append(value)
        return found


def scrub(obj: Any, roots: Mapping[str, Path | str] | None = None) -> Any:
    """Scrub ``obj`` with a :class:`Scrubber` for ``roots`` and the default roots."""
    return Scrubber(roots)(obj)


def _root_pattern(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    body = r"[\\/]+".join(re.escape(part) for part in parts) + _ROOT_END
    if path.startswith(("\\\\", "//")):
        return r"[\\/]{2}" + body
    if path.startswith(("/", "\\")):
        return r"(?<![\w.>])[\\/]" + body
    return r"(?<![A-Za-z0-9])" + body


def _abs_placeholder(match: re.Match[str]) -> str:
    name = re.split(r"[\\/]", match.group(0).rstrip("\\/"))[-1]
    return f"<abs>/{name}" if name and not name.endswith(":") else "<abs>"


def _strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for key, value in obj.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _strings(item)


def _replaceable_word(word: str) -> bool:
    return len(word) >= 3 and word.lower() not in _COMMON_WORDS and word.lower() != "localhost"


def _default_roots() -> dict[str, str]:
    from bedrock_bench.cache import default_root
    from bedrock_bench.config import project_root

    roots = {
        "<cache>": str(default_root()),
        "<repo>": str(project_root()),
        "<tmp>": tempfile.gettempdir(),
        "<home>": str(Path.home()),
    }
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots["<localappdata>"] = local
    return roots


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):
        return None


def _host() -> str | None:
    try:
        return socket.gethostname().split(".")[0] or None
    except OSError:
        return None
