# SPDX-License-Identifier: Apache-2.0
"""The machine lock: exclusive, released on process death, inherited by run-one children."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bedrock_bench import lock
from bedrock_bench.lock import ENV_TOKEN, LockHeld, MachineLock, holder

SRC = Path(__file__).resolve().parents[2]

CHILD = """
import sys, time
from pathlib import Path
from bedrock_bench.lock import MachineLock
held = MachineLock(Path(sys.argv[1]), label="child")
print("held" if held.acquire() else "busy", flush=True)
time.sleep(60)
"""


def python_exe() -> str:
    base = getattr(sys, "_base_executable", None)
    return base if base and os.path.exists(base) else sys.executable


def child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != ENV_TOKEN}
    env["PYTHONPATH"] = str(SRC)
    env.update(extra)
    return env


def test_default_path_follows_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BBENCH_LOCK", str(tmp_path / "x.lock"))
    assert lock.default_path() == tmp_path / "x.lock"
    monkeypatch.delenv("BBENCH_LOCK")
    monkeypatch.setenv("BBENCH_CACHE", str(tmp_path / "cache"))
    assert lock.default_path() == tmp_path / "cache" / "machine.lock"


def test_exclusive_within_and_across_handles(tmp_path: Path) -> None:
    path = tmp_path / "machine.lock"
    first = MachineLock(path, label="first")
    assert holder(path) is None
    assert first.acquire()
    assert first.held
    info = holder(path)
    assert info is not None and info.pid == os.getpid() and info.label == "first"
    second = MachineLock(path)
    assert not second.acquire(timeout=0.2, poll_s=0.05)
    with pytest.raises(LockHeld, match="first"):
        with second:
            pass
    first.release()
    assert holder(path) is None
    assert not (tmp_path / "machine.lock.json").exists()
    with second:
        assert second.held
    assert not second.held


def test_stale_info_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "machine.lock"
    path.touch()
    (tmp_path / "machine.lock.json").write_text(
        '{"pid": 1, "label": "gone", "since_epoch_ms": 0, "harness_version": "0", "token": "t"}', encoding="utf-8"
    )
    assert holder(path) is None
    assert MachineLock(path).acquire()


def test_released_when_the_holder_dies(tmp_path: Path) -> None:
    path = tmp_path / "machine.lock"
    child = subprocess.Popen([python_exe(), "-c", CHILD, str(path)], stdout=subprocess.PIPE, env=child_env(), text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        info = holder(path)
        assert info is not None and info.label == "child" and info.pid == child.pid
        assert not MachineLock(path).acquire()
    finally:
        child.kill()
        child.wait(30)
    deadline = time.monotonic() + 10
    mine = MachineLock(path)
    while not mine.acquire() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert mine.held
    mine.release()


def test_children_inherit_through_the_token(tmp_path: Path) -> None:
    path = tmp_path / "machine.lock"
    parent = MachineLock(path, label="matrix")
    assert parent.acquire()
    try:
        env = parent.child_env()
        assert set(env) == {ENV_TOKEN}
        child = subprocess.run(
            [python_exe(), "-c", CHILD.replace("time.sleep(60)", ""), str(path)],
            capture_output=True,
            text=True,
            env=child_env(**env),
            timeout=60,
        )
        assert child.stdout.strip() == "held"
        wrong = subprocess.run(
            [python_exe(), "-c", CHILD.replace("time.sleep(60)", ""), str(path)],
            capture_output=True,
            text=True,
            env=child_env(**{ENV_TOKEN: "0" * 32}),
            timeout=60,
        )
        assert wrong.stdout.strip() == "busy"
        assert holder(path) is not None and holder(path).label == "matrix"
    finally:
        parent.release()


def test_inherited_hold_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "machine.lock"
    parent = MachineLock(path)
    assert parent.acquire()
    try:
        monkeypatch.setenv(ENV_TOKEN, parent.child_env()[ENV_TOKEN])
        child = MachineLock(path)
        assert child.acquire()
        assert child.inherited
        assert child.child_env() == parent.child_env()
        child.release()
        assert holder(path) is not None  # releasing an inherited hold leaves the parent's lock alone
    finally:
        parent.release()
