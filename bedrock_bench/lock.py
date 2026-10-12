# SPDX-License-Identifier: Apache-2.0
"""The machine lock: one measured run at a time (rule R4).

The lock is an operating-system advisory lock on a file (``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere),
so it is released when the holder exits, however it exits; there is never a stale lock to clean up by hand. A small
JSON file next to it says who holds it, for ``bbench doctor`` and for error messages. That file is informational
only: :func:`holder` trusts it only while the lock is really held.

``bbench run`` holds the lock for the whole matrix and starts one ``bbench run-one`` subprocess per run. The child
inherits the hold through an environment variable carrying a random token, so it neither deadlocks on its parent nor
runs unlocked. A ``run-one`` started by hand takes the lock itself.

The lock file is ``$BBENCH_LOCK`` if set, else ``machine.lock`` in the cache root. Benchmarks that share a machine
must share the lock file, so keep the default cache or point ``BBENCH_LOCK`` at one common path.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

from bedrock_bench import __version__
from bedrock_bench.cache import Cache

ENV_LOCK = "BBENCH_LOCK"
ENV_TOKEN = "BBENCH_LOCK_TOKEN"


def default_path() -> Path:
    override = os.environ.get(ENV_LOCK)
    if override:
        return Path(override).expanduser()
    return Cache().root / "machine.lock"


@dataclass(frozen=True)
class LockInfo:
    """Who holds the lock. Written next to the lock file while it is held."""

    pid: int
    label: str
    since_epoch_ms: int
    harness_version: str
    token: str

    def describe(self) -> str:
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since_epoch_ms / 1000))
        label = f" ({self.label})" if self.label else ""
        return f"pid {self.pid}{label} since {since}"


class LockHeld(RuntimeError):
    """Another process holds the machine lock."""

    def __init__(self, path: Path, info: LockInfo | None) -> None:
        self.path = path
        self.info = info
        who = info.describe() if info else "another process"
        super().__init__(f"machine lock {path} is held by {who}")


def _info_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def _read_info(path: Path) -> LockInfo | None:
    try:
        data = json.loads(_info_path(path).read_text(encoding="utf-8"))
        return LockInfo(**data)
    except (OSError, ValueError, TypeError):
        return None


def _try_lock(fh: IO[bytes]) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class MachineLock:
    """The machine lock. Use as a context manager, or call :meth:`acquire` and :meth:`release`.

    ``acquire(timeout=0)`` tries once, ``timeout=None`` waits forever. A process whose environment carries the
    holder's token (see :meth:`child_env`) counts as holding the lock without touching the file.
    """

    def __init__(self, path: Path | None = None, *, label: str = "") -> None:
        self.path = path or default_path()
        self.label = label
        self._fh: IO[bytes] | None = None
        self._token: str | None = None
        self.inherited = False

    @property
    def held(self) -> bool:
        return self._fh is not None or self.inherited

    def acquire(self, timeout: float | None = 0.0, poll_s: float = 1.0) -> bool:
        """Take the lock. Returns False if it is still held by someone else when ``timeout`` runs out."""
        if self.held:
            return True
        if self._inherit():
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        fh = open(self.path, "a+b")  # kept open while the lock is held
        try:
            while not _try_lock(fh):
                if deadline is not None and time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(poll_s if deadline is None else max(0.0, min(poll_s, deadline - time.monotonic())))
        except BaseException:
            fh.close()
            raise
        self._fh = fh
        self._token = secrets.token_hex(16)
        info = LockInfo(os.getpid(), self.label, time.time_ns() // 1_000_000, __version__, self._token)
        tmp = _info_path(self.path).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(info)), encoding="utf-8")
        os.replace(tmp, _info_path(self.path))
        return True

    def release(self) -> None:
        if self.inherited:
            self.inherited = False
            return
        if self._fh is None:
            return
        try:
            info = _read_info(self.path)
            if info is not None and info.token == self._token:
                _info_path(self.path).unlink(missing_ok=True)
        finally:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None
            self._token = None

    def child_env(self) -> dict[str, str]:
        """Environment entries that let a child process run under this hold."""
        if self._token is None:
            token = os.environ.get(ENV_TOKEN) if self.inherited else None
            return {ENV_TOKEN: token} if token else {}
        return {ENV_TOKEN: self._token}

    def __enter__(self) -> MachineLock:
        if not self.acquire():
            raise LockHeld(self.path, holder(self.path))
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _inherit(self) -> bool:
        token = os.environ.get(ENV_TOKEN)
        if not token:
            return False
        info = holder(self.path)
        if info is not None and secrets.compare_digest(info.token, token):
            self.inherited = True
            return True
        return False


def holder(path: Path | None = None) -> LockInfo | None:
    """Who holds the lock, or None when it is free (a leftover info file from a dead holder is ignored)."""
    path = path or default_path()
    if not path.exists():
        return None
    info = _read_info(path)
    try:
        fh = open(path, "a+b")
    except OSError:
        return info
    with fh:
        if _try_lock(fh):
            _unlock(fh)
            return None
    return info
