# SPDX-License-Identifier: Apache-2.0
"""A temporary workspace that runs ``bbench`` against the fake server and the fake bots.

``FakeBench.create(tmp_path)`` lays out a small project mirror and an environment for ``bbench`` subprocesses:

- ``configs/`` copies, so names resolve the same way as in a checkout; ``BBENCH_ROOT`` points at the workspace and
  ``BBENCH_CACHE`` into it;
- ``BBENCH_TEST_WORKLOAD``, which shrinks the workload to ``fake-quick.json``: four bots, every phase in seconds;
- a ``.dist-info`` folder that registers ``fakes.fake_adapter`` under the ``bedrock_bench.adapters`` entry point
  group, with ``tests/`` and that folder on ``PYTHONPATH``;
- ``BBENCH_BOT_CMD``, the JSON argv that replaces the loadbot binary with ``fake_bots.py``;
- a free port and a machine profile that splits this host's CPUs between server and bots.

The profile's gates are loose enough for a busy desktop; ``strict_gates=True`` restores the built-in defaults for the
contamination tests.

    bench = FakeBench.create(tmp_path)
    run = bench.run_one("fake_bds", variant="crash-after-join")
    assert run.exit_code == 4
"""

from __future__ import annotations

import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from bedrock_bench.workload import ENV_TEST_WORKLOAD, WORKLOAD, Workload, with_overrides
from fakes.fake_adapter import BOTS_SCRIPT, FAKE_ADAPTERS, SERVERS_DIR, interpreter

#: Environment variable the bot fleet reads instead of the loadbot binary path: a JSON array, the argv prefix.
ENV_BOT_CMD = "BBENCH_BOT_CMD"

TESTS_DIR = Path(__file__).resolve().parent.parent
FAKE_WORKLOAD = Path(__file__).resolve().parent / "fake-quick.json"
DIST_INFO = "bedrock_bench_fakes-0.0.0.dist-info"
DEFAULT_VERSION = "1.0.0"

#: Gates loose enough that a busy developer machine never makes a fake run invalid.
RELAXED_GATES = {
    "idle_check_s": 1.0,
    "foreign_cores_mean": 1024.0,
    "foreign_cores_peak_10s": 1024.0,
    "foreign_server_set_cores_mean": 1024.0,
    "bot_cpu_frac_max": 1.0,
}
#: The built-in defaults with a short idle check.
STRICT_GATES = {
    "idle_check_s": 1.0,
    "foreign_cores_mean": 0.25,
    "foreign_cores_peak_10s": 1.5,
    "foreign_server_set_cores_mean": 0.25,
    "bot_cpu_frac_max": 0.85,
}


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def fake_workload() -> Workload:
    """The workload of runs against the fakes, as ``bbench`` builds it from ``$BBENCH_TEST_WORKLOAD``."""
    return with_overrides(WORKLOAD, json.loads(FAKE_WORKLOAD.read_text(encoding="utf-8")))


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        merged[key] = _merge(merged[key], value) if isinstance(value, Mapping) and key in merged else value
    return merged


def register_entry_point(folder: Path) -> Path:
    """Write a ``.dist-info`` that publishes the fake adapters; put ``folder`` on ``sys.path`` to activate it."""
    dist = folder / DIST_INFO
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: bedrock-bench-fakes\nVersion: 0.0.0\n", encoding="utf-8", newline="\n"
    )
    (dist / "entry_points.txt").write_text(
        "[bedrock_bench.adapters]\nfakes = fakes.fake_adapter\n", encoding="utf-8", newline="\n"
    )
    return folder


def _bindable(port: int) -> bool:
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(socket.AF_INET, kind) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
    return True


def free_port() -> int:
    """A port that is free for TCP and UDP, together with the next one (the IPv6 port of BDS-style servers)."""
    for _ in range(200):
        # Below the dynamic range, where Windows may reserve blocks of ports.
        port = random.randrange(20000, 49150)
        if _bindable(port) and _bindable(port + 1):
            return port
    raise RuntimeError("no free port pair found")


def host_cpus() -> list[int]:
    try:
        return sorted(psutil.Process().cpu_affinity())
    except (AttributeError, psutil.Error):
        return list(range(os.cpu_count() or 1))


def cpu_list(cpus: Sequence[int]) -> str:
    """``[0, 1, 2, 5]`` -> ``"0-2,5"``."""
    parts: list[str] = []
    ordered = sorted(cpus)
    start = prev = ordered[0]
    for cpu in [*ordered[1:], None]:
        if cpu is not None and cpu == prev + 1:
            prev = cpu
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if cpu is not None:
            start = prev = cpu
    return ",".join(parts)


def toml_dumps(data: Mapping[str, Any], prefix: str = "") -> str:
    """Enough TOML for machine profiles and matrices: scalars, arrays, tables and arrays of tables."""
    lines: list[str] = []
    tables: list[str] = []
    for key, value in data.items():
        name = f"{prefix}{_toml_key(key)}"
        if isinstance(value, Mapping):
            body = toml_dumps(value, f"{name}.")
            tables.append(f"[{name}]\n{body}")
        elif isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
            tables.extend(f"[[{name}]]\n{toml_dumps(item, f'{name}.')}" for item in value)
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    head = "\n".join(lines) + ("\n" if lines else "")
    return head + "".join(f"\n{table}" for table in tables)


def _toml_key(key: str) -> str:
    return key if key.replace("_", "").replace("-", "").isalnum() else json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def server_tag(server: str) -> str:
    """``fake_bds`` -> ``fake_bds@1.0.0``; a tag that has a version stays as it is."""
    return server if "@" in server else f"{server}@{DEFAULT_VERSION}"


def _arm_table(arm: str | Mapping[str, Any]) -> dict[str, Any]:
    table = {"server": arm} if isinstance(arm, str) else dict(arm)
    return {**table, "server": server_tag(str(table["server"]))}


@contextmanager
def cpu_burner(cpus: Sequence[int]) -> Iterator[psutil.Process]:
    """A busy-looping process pinned to ``cpus``, outside every process tree the harness owns."""
    proc = subprocess.Popen([interpreter(), "-I", "-c", "while True: pass"])
    try:
        burner = psutil.Process(proc.pid)
        burner.cpu_affinity(list(cpus))
        yield burner
    finally:
        proc.kill()
        proc.wait(10)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """One finished ``bbench`` invocation and the run directory it created, if any."""

    process: subprocess.CompletedProcess[str]
    run_dir: Path | None

    @property
    def exit_code(self) -> int:
        return self.process.returncode

    def describe(self) -> str:
        """Exit code and output tails, for assertion messages."""
        return (
            f"exit code {self.exit_code}\n--- stdout ---\n{self.process.stdout[-3000:]}\n"
            f"--- stderr ---\n{self.process.stderr[-3000:]}"
        )

    def require_dir(self) -> Path:
        assert self.run_dir is not None, f"no run directory was created\n{self.describe()}"
        return self.run_dir

    @property
    def run_json(self) -> dict[str, Any]:
        return read_json(self.require_dir() / "run.json")

    @property
    def metrics_json(self) -> dict[str, Any]:
        return read_json(self.require_dir() / "metrics.json")


@dataclass(frozen=True)
class Background:
    """A ``bbench`` process started by :meth:`FakeBench.start`; output goes to files so it can never block."""

    proc: subprocess.Popen[bytes]
    stdout: Path
    stderr: Path

    def poll(self) -> int | None:
        return self.proc.poll()

    def finish(self, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
        """Wait for the exit; on timeout kill the whole tree and fail."""
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(f"bbench did not finish within {timeout:.0f} s") from None
        stdout = self.stdout.read_text(encoding="utf-8", errors="replace")
        stderr = self.stderr.read_text(encoding="utf-8", errors="replace")
        return subprocess.CompletedProcess(self.proc.args, code, stdout, stderr)

    def kill(self) -> None:
        try:
            tree = psutil.Process(self.proc.pid).children(recursive=True)
        except psutil.Error:
            tree = []
        for child in tree:
            try:
                child.kill()
            except psutil.Error:
                pass
        self.proc.kill()
        self.proc.wait(10)


@dataclass
class FakeBench:
    root: Path
    port: int
    server_cpus: tuple[int, ...]
    bot_cpus: tuple[int, ...]
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path) -> FakeBench:
        root.mkdir(parents=True, exist_ok=True)
        (root / "configs").mkdir(exist_ok=True)
        shutil.copyfile(TESTS_DIR.parent / "configs" / "benchmark.toml", root / "configs" / "benchmark.toml")
        for adapter in FAKE_ADAPTERS:
            (root / "configs" / adapter.name).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SERVERS_DIR / adapter.name / "server.toml", root / "configs" / adapter.name / "server.toml")
        entry_points = register_entry_point(root / "entry-points")

        cpus = host_cpus()
        half = max(1, len(cpus) // 2)
        server_cpus, bot_cpus = tuple(cpus[:half]), tuple(cpus[half:] or cpus[:half])

        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        python_path = [str(TESTS_DIR), str(entry_points)]
        if env.get("PYTHONPATH"):
            python_path.append(env["PYTHONPATH"])
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(python_path),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "BBENCH_ROOT": str(root),
                "BBENCH_CACHE": str(root / "cache"),
                ENV_BOT_CMD: json.dumps([interpreter(), "-I", "-u", str(BOTS_SCRIPT)]),
                ENV_TEST_WORKLOAD: str(FAKE_WORKLOAD),
            }
        )
        bench = cls(root=root, port=free_port(), server_cpus=server_cpus, bot_cpus=bot_cpus, env=env)
        bench.machine()
        return bench

    @property
    def out(self) -> Path:
        return self.root / "runs"

    # ---- inputs ----------------------------------------------------------------------------------------------------

    def workload(self, name: str, **tables: Mapping[str, Any]) -> Path:
        """``fake-quick.json`` with ``tables`` merged in, for ``run_one(workload=...)``."""
        data = _merge(json.loads(FAKE_WORKLOAD.read_text(encoding="utf-8")), tables)
        path = self.root / f"workload-{name}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")
        return path

    def machine(
        self,
        name: str = "fake",
        *,
        strict_gates: bool = False,
        server_cpus: Sequence[int] | None = None,
        bot_cpus: Sequence[int] | None = None,
    ) -> Path:
        """A machine profile at :meth:`machine_path` on this host's CPUs and the workspace port."""
        profile = {
            "label": name,
            "placement": {
                "server_cpus": cpu_list(server_cpus or self.server_cpus),
                "bot_cpus": cpu_list(bot_cpus or self.bot_cpus),
                "harness_cpus": "bot",
                "priority": "normal",
            },
            "runtime": {"port": self.port, "jvm_heap": "1G"},
            "gates": STRICT_GATES if strict_gates else RELAXED_GATES,
        }
        path = self.machine_path(name)
        path.write_text(toml_dumps(profile), encoding="utf-8", newline="\n")
        return path

    def machine_path(self, name: str = "fake") -> Path:
        return self.root / f"machine-{name}.toml"

    def matrix(
        self,
        name: str,
        arms: Sequence[str | Mapping[str, Any]],
        *,
        scenarios: Sequence[str] = ("fake-quick",),
        repeats: int = 1,
        order: str = "rotate",
    ) -> Path:
        """``configs/matrices/<name>.toml`` with no cooldown and no reruns.

        An arm is a server tag (``fake_bds`` gets ``@1.0.0``) or an ``[[arm]]`` table whose ``server`` is one.
        """
        data = {
            "matrix": {
                "name": name,
                "scenarios": list(scenarios),
                "repeats": repeats,
                "order": order,
                "cooldown_s": 0.0,
                "rerun_invalid": 0,
            },
            "arm": [_arm_table(arm) for arm in arms],
        }
        path = self.root / "configs" / "matrices" / f"{name}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(toml_dumps(data), encoding="utf-8", newline="\n")
        return path

    # ---- commands --------------------------------------------------------------------------------------------------

    def command(self, *args: str | Path) -> list[str]:
        return [sys.executable, "-m", "bedrock_bench", *map(str, args)]

    def bbench(
        self, *args: str | Path, timeout: float = 300.0, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self.command(*args),
                cwd=self.root,
                env={**self.env, **(env or {})},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"bbench {' '.join(map(str, args))} timed out after {timeout:.0f} s") from exc

    def start(self, *args: str | Path) -> Background:
        """``bbench`` in the background, for tests that act while a run is in progress."""
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        index = len(list(logs.glob("*.out")))
        stdout, stderr = logs / f"bbench-{index}.out", logs / f"bbench-{index}.err"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            proc = subprocess.Popen(self.command(*args), cwd=self.root, env=self.env, stdout=out, stderr=err)
        return Background(proc, stdout, stderr)

    def run_one(
        self,
        server: str = "fake_bds",
        *,
        variant: str | None = None,
        workload: Path | None = None,
        machine: str = "fake",
        timeout: float = 300.0,
    ) -> Run:
        """``bbench run-one`` for ``server@1.0.0``; the run directory is the one it adds under ``out``."""
        before = set(self.run_dirs())
        args: list[str | Path] = [
            "run-one",
            "--server",
            server_tag(server),
            "--machine",
            self.machine_path(machine),
            "--out",
            self.out,
        ]
        if variant is not None:
            args += ["--variant", variant]
        env = {ENV_TEST_WORKLOAD: str(workload)} if workload is not None else None
        process = self.bbench(*args, timeout=timeout, env=env)
        created = sorted(set(self.run_dirs()) - before)
        assert len(created) <= 1, f"run-one created {len(created)} run directories: {created}"
        return Run(process, created[0] if created else None)

    def run_dirs(self, out: Path | None = None) -> list[Path]:
        """Every run directory (a folder holding run.json or run.partial.json) under ``out``."""
        root = out or self.out
        if not root.is_dir():
            return []
        found = {p.parent for pattern in ("run.json", "run.partial.json") for p in root.rglob(pattern)}
        return sorted(found)

    def wait_for_mark(self, mark: str, proc: Background, timeout: float = 120.0) -> bool:
        """Wait until a partial run.json under ``out`` records ``mark``; False if the run ends or time runs out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and proc.poll() is None:
            for path in self.out.rglob("run.partial.json") if self.out.is_dir() else ():
                try:
                    marks = read_json(path).get("marks") or {}
                except (OSError, ValueError):
                    continue
                if marks.get(mark) is not None:
                    return True
            time.sleep(0.1)
        return False
