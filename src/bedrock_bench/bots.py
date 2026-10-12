# SPDX-License-Identifier: Apache-2.0
"""The load bot, seen from the harness: build or download it, find it, and run a fleet of bots.

The bot is a Go program under ``bots/`` (see ``bots/README.md``). One process runs every bot of a run, writes JSON
lines to stdout (``schemas/bot-event.schema.json``) and takes commands on stdin. :class:`BotFleet` starts it, stamps
every event with the harness monotonic clock on receipt (``rx_ns``) and stops it cleanly: ``quit`` on stdin first,
so the bot can write its per-bot summaries, then an interrupt, then a kill.

Rule R2 (one bot source, identical flags for every arm) is enforced by :func:`bot_args`: the arguments are derived
from the scenario alone, and only allowlisted compatibility flags may be added per adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import IO, Any

from bedrock_bench import __version__
from bedrock_bench.adapters.base import ALLOWED_COMPAT
from bedrock_bench.clock import mono_ns
from bedrock_bench.config import ConfigError, project_root
from bedrock_bench.model import BotParams, ExitCode

log = logging.getLogger(__name__)

#: Bot modules, each a Go module under ``bots/``.
MODULES = ("loadbot", "legacy1001")

#: Network protocol spoken by each module. Adapters declare ``protocol``; :func:`module_for_protocol` picks the bot.
MODULE_PROTOCOLS = {"loadbot": 2193, "legacy1001": 1001}

#: Where release binaries are published: ``<base>/<tag>/<asset>`` plus ``<base>/<tag>/SHA256SUMS``.
RELEASE_BASE_URL = "https://github.com/EndstoneMC/bedrock-bench/releases/download"
ENV_RELEASE_TAG = "BBENCH_BOTS_TAG"
ENV_RELEASE_URL = "BBENCH_BOTS_URL"

#: Seconds the bot gets to write its summaries after ``quit``, and again after an interrupt, before it is killed.
QUIT_TIMEOUT_S = 15.0
SIGNAL_TIMEOUT_S = 10.0

DOWNLOAD_TIMEOUT_S = 60.0


class BotExit(IntEnum):
    """Exit codes of the bot process."""

    OK = 0
    USAGE = 1
    ALL_JOINS_FAILED = 2
    DECODE_ERROR = 3  # only with -decode-errors fail


class BotError(RuntimeError):
    """The bot cannot be built, fetched, found or started."""


class ToolchainError(BotError):
    """The Go toolchain needed to build the bot is missing or failed."""


# --------------------------------------------------------------------------------------------------------------------
# Targets and paths
# --------------------------------------------------------------------------------------------------------------------

_GOOS = {"win32": "windows", "linux": "linux", "darwin": "darwin"}
_GOARCH = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


@dataclass(frozen=True, order=True)
class Target:
    """A Go build target."""

    goos: str
    goarch: str

    @classmethod
    def host(cls) -> Target:
        goos = _GOOS.get(sys.platform)
        goarch = _GOARCH.get(platform.machine().lower())
        if goos is None or goarch is None:
            raise BotError(f"unsupported host {sys.platform}/{platform.machine()}")
        return cls(goos, goarch)

    @classmethod
    def parse(cls, text: str) -> Target:
        """``linux/amd64`` (``-`` also accepted as the separator)."""
        goos, sep, goarch = text.replace("-", "/").partition("/")
        if not sep or goos not in _GOOS.values() or goarch not in set(_GOARCH.values()):
            raise ConfigError(f"not a supported GOOS/GOARCH target: {text!r}")
        return cls(goos, goarch)

    @property
    def slug(self) -> str:
        return f"{self.goos}-{self.goarch}"

    @property
    def exe_suffix(self) -> str:
        return ".exe" if self.goos == "windows" else ""

    def __str__(self) -> str:
        return f"{self.goos}/{self.goarch}"


def module_for_protocol(protocol: int) -> str:
    """The bot module that speaks an adapter's network protocol."""
    for module, proto in MODULE_PROTOCOLS.items():
        if proto == protocol:
            return module
    raise BotError(f"no bot speaks protocol {protocol}")


def _check_module(module: str) -> None:
    if module not in MODULES:
        raise ConfigError(f"unknown bot module {module!r}; expected one of {', '.join(MODULES)}")


def source_dir(module: str, root: Path | None = None) -> Path:
    _check_module(module)
    return (root or project_root()) / "bots" / module


def available_modules(root: Path | None = None) -> list[str]:
    """Modules whose source exists in this checkout."""
    return [m for m in MODULES if (source_dir(m, root) / "go.mod").is_file()]


def binary_path(module: str, target: Target | None = None, root: Path | None = None) -> Path:
    """``bots/bin/<goos>-<goarch>/<module>[.exe]`` in the checkout."""
    _check_module(module)
    target = target or Target.host()
    return (root or project_root()) / "bots" / "bin" / target.slug / f"{module}{target.exe_suffix}"


def asset_name(module: str, target: Target) -> str:
    """Release asset name, e.g. ``loadbot-linux-amd64`` or ``loadbot-windows-amd64.exe``."""
    _check_module(module)
    return f"{module}-{target.slug}{target.exe_suffix}"


def _manifest_path(binary: Path) -> Path:
    return binary.with_name(binary.name + ".json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------------------------------
# Binaries
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BotBinary:
    """A bot executable and what it reports about itself (``-version``)."""

    module: str
    path: Path
    sha256: str
    target: Target
    info: Mapping[str, Any] = field(default_factory=dict)  # the -version JSON; empty for a cross-built binary
    source: str = "build"  # build | release

    @property
    def protocol(self) -> int:
        return int(self.info.get("protocol") or MODULE_PROTOCOLS[self.module])

    def command(self, args: Sequence[str]) -> list[str]:
        """The full command line for these bot arguments."""
        return [str(self.path), *args]

    def provenance(self, args: Sequence[str]) -> dict[str, Any]:
        """The ``bot`` block of run.json. The executable path is replaced by the module name."""
        return {
            "module": self.module,
            "protocol": self.protocol,
            "sha256": self.sha256,
            "version": self.info.get("version"),
            "vcs": self.info.get("vcs") or None,
            "go": self.info.get("go"),
            "gophertunnel": self.info.get("gophertunnel"),
            "patches": list(self.info.get("patches") or []),
            "argv": [self.module, *args],
        }


def version_info(path: Path, timeout_s: float = 30.0) -> dict[str, Any]:
    """Run ``<bot> -version`` and return its JSON."""
    try:
        proc = subprocess.run(
            [str(path), "-version"], capture_output=True, timeout=timeout_s, check=True, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BotError(f"{path.name} -version failed: {exc}") from exc
    try:
        info = json.loads(proc.stdout)
    except ValueError as exc:
        raise BotError(f"{path.name} -version printed no JSON: {proc.stdout[:200]!r}") from exc
    if not isinstance(info, dict):
        raise BotError(f"{path.name} -version printed {type(info).__name__}, not an object")
    return info


def _write_manifest(binary: BotBinary, extra: Mapping[str, Any] | None = None) -> None:
    data = {
        "module": binary.module,
        "target": str(binary.target),
        "sha256": binary.sha256,
        "source": binary.source,
        "info": dict(binary.info),
        **(extra or {}),
    }
    _manifest_path(binary.path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def locate(module: str, *, target: Target | None = None, root: Path | None = None) -> BotBinary:
    """The bot binary in ``bots/bin`` for this host (or ``target``), with its hash checked.

    The manifest written by :func:`build` or :func:`fetch` supplies the ``-version`` information when its hash still
    matches the file; otherwise a host binary is asked directly.
    """
    target = target or Target.host()
    path = binary_path(module, target, root)
    if not path.is_file():
        raise BotError(f"{module} for {target} not found at {path}; run 'bbench bots build' or 'bbench bots fetch'")
    digest = sha256_file(path)
    info: dict[str, Any] = {}
    source = "build"
    try:
        manifest = json.loads(_manifest_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if isinstance(manifest, dict) and manifest.get("sha256") == digest:
        info = dict(manifest.get("info") or {})
        source = str(manifest.get("source") or "build")
    elif target == Target.host():
        info = version_info(path)
    if info and info.get("module") not in (None, module):
        raise BotError(f"{path} reports module {info.get('module')!r}, expected {module!r}")
    return BotBinary(module=module, path=path, sha256=digest, target=target, info=info, source=source)


# --------------------------------------------------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------------------------------------------------


def _go_bool(value: bool) -> str:
    return "true" if value else "false"


def _go_duration(seconds: float) -> str:
    """Seconds as a Go duration in whole milliseconds; the bot parses it exactly."""
    return f"{round(seconds * 1000)}ms"


def bot_args(
    params: BotParams,
    *,
    addr: str,
    csg: bool,
    compat_flags: Iterable[str] = (),
    control: bool = True,
) -> list[str]:
    """Bot arguments for a scenario.

    Every arm of a matrix gets the same list (rule R2). ``csg`` follows the scenario's ``server.csg``; ``compat_flags``
    come from the adapter and must be in ``ALLOWED_COMPAT``. Booleans are spelled out so the recorded argv states
    every setting. Radius jitter starts paused; the chaos phase turns it on.
    """
    compat = sorted(set(compat_flags))
    outside = [f for f in compat if f not in ALLOWED_COMPAT]
    if outside:
        raise ConfigError(f"bot flags outside the compatibility allowlist {sorted(ALLOWED_COMPAT)}: {outside}")
    args = [
        "-addr", addr,
        "-n", str(params.n),
        "-prefix", params.prefix,
        "-stagger", f"{params.stagger_ms}ms",
        f"-csg={_go_bool(csg)}",
        "-radius", str(params.radius),
        f"-loadscreen={_go_bool(params.loadscreen)}",
        "-walk", params.walk,
        "-spawn-timeout", _go_duration(params.spawn_timeout_s),
        "-join-retries", str(params.join_retries),
        "-identity-seed", params.identity_seed,
        "-progress-interval", _go_duration(params.progress_interval_s),
        "-stat-interval", _go_duration(params.stat_interval_s),
        "-pkt-interval", _go_duration(params.pkt_interval_s),
        "-probe-bots", str(params.probe_bots),
        "-decode-errors", params.decode_errors,
        f"-control={_go_bool(control)}",
        f"-rejoin-on-drop={_go_bool(params.rejoin_on_drop)}",
    ]  # fmt: skip
    if params.radius_jitter_s > 0:
        args += ["-radius-jitter", _go_duration(params.radius_jitter_s), "-jitter-paused=true"]
    return args + compat


# --------------------------------------------------------------------------------------------------------------------
# Build and fetch
# --------------------------------------------------------------------------------------------------------------------


def _git_commit(root: Path) -> str:
    """Short commit of the checkout, with ``-dirty`` when ``bots/`` has uncommitted changes; empty without git."""
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        head = subprocess.run(
            [git, "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
        status = subprocess.run(
            [git, "status", "--porcelain", "--", "bots"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{head}-dirty" if status else head


def build_command(go: str, version: str, commit: str, output: Path) -> list[str]:
    """The reproducible ``go build`` invocation: same source and toolchain give the same bytes."""
    ldflags = f"-s -w -buildid= -X main.version={version} -X main.commit={commit}"
    return [go, "build", "-trimpath", "-buildvcs=false", "-ldflags", ldflags, "-o", str(output), "."]


def build_env(target: Target, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(
        {
            "CGO_ENABLED": "0",
            "GOOS": target.goos,
            "GOARCH": target.goarch,
            "GOFLAGS": "-mod=readonly",
            "GOWORK": "off",  # build each module from its own go.mod and go.sum
        }
    )
    return env


def build(
    module: str,
    target: Target | None = None,
    *,
    root: Path | None = None,
    go: str | None = None,
    version: str | None = None,
    commit: str | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> BotBinary:
    """Build a bot module into ``bots/bin/<target>/`` and record its hash and ``-version`` output."""
    root = root or project_root()
    target = target or Target.host()
    src = source_dir(module, root)
    if not (src / "go.mod").is_file():
        raise BotError(f"no Go module at {src}")
    go = go or shutil.which("go")
    if go is None:
        raise ToolchainError("the Go toolchain (>= 1.25) is not on PATH; install it or use 'bbench bots fetch'")
    out = binary_path(module, target, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    cmd = build_command(go, version or __version__, _git_commit(root) if commit is None else commit, tmp)
    log.info("building %s for %s", module, target)
    try:
        run(cmd, cwd=src, env=build_env(target), check=True)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        raise ToolchainError(f"go build of {module} for {target} failed with exit code {exc.returncode}") from exc
    except OSError as exc:
        raise ToolchainError(f"cannot run {go}: {exc}") from exc
    os.replace(tmp, out)
    info = version_info(out) if target == Target.host() else {}
    binary = BotBinary(module=module, path=out, sha256=sha256_file(out), target=target, info=info, source="build")
    _write_manifest(binary)
    return binary


def release_tag() -> str:
    """The release whose binaries match this harness version, or ``$BBENCH_BOTS_TAG``."""
    override = os.environ.get(ENV_RELEASE_TAG)
    if override:
        return override
    if not re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        raise BotError(
            f"bedrock-bench {__version__} is not a release, so there are no release bots; "
            f"build them ('bbench bots build') or set {ENV_RELEASE_TAG}"
        )
    return f"v{__version__}"


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``sha256sum`` output: ``<hex>  <name>`` or ``<hex> *<name>`` per line."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition(" ")
        name = name.strip().lstrip("*")
        digest = digest.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or not name:
            raise BotError(f"malformed SHA256SUMS line: {line!r}")
        sums[name] = digest
    return sums


def _download(url: str, dest: IO[bytes], urlopen: Callable[..., Any]) -> str:
    digest = hashlib.sha256()
    with urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as resp:
        for chunk in iter(lambda: resp.read(1 << 16), b""):
            digest.update(chunk)
            dest.write(chunk)
    return digest.hexdigest()


def fetch(
    module: str,
    target: Target | None = None,
    *,
    root: Path | None = None,
    tag: str | None = None,
    base_url: str | None = None,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> BotBinary:
    """Download a release binary into ``bots/bin/<target>/`` after checking it against the release's SHA256SUMS."""
    root = root or project_root()
    target = target or Target.host()
    tag = tag or release_tag()
    base = (base_url or os.environ.get(ENV_RELEASE_URL) or RELEASE_BASE_URL).rstrip("/")
    name = asset_name(module, target)
    sums_url = f"{base}/{tag}/SHA256SUMS"
    try:
        with urlopen(sums_url, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            sums = parse_sha256sums(resp.read().decode("utf-8"))
    except OSError as exc:
        raise BotError(f"cannot download {sums_url}: {exc}") from exc
    expected = sums.get(name)
    if expected is None:
        raise BotError(f"release {tag} has no {name}")
    out = binary_path(module, target, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"{base}/{tag}/{name}"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            got = _download(url, fh, urlopen)
        if got != expected:
            raise BotError(f"{name}: sha256 {got} does not match SHA256SUMS ({expected})")
        tmp.chmod(0o755)
        os.replace(tmp, out)
    except OSError as exc:
        raise BotError(f"cannot download {url}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    info = version_info(out) if target == Target.host() else {}
    binary = BotBinary(module=module, path=out, sha256=expected, target=target, info=info, source="release")
    _write_manifest(binary, {"url": url, "tag": tag})
    return binary


def build_cli(args: argparse.Namespace) -> int:
    """``bbench bots build|fetch [--module M] [--target OS/ARCH ...]``."""
    root = project_root()
    targets = [Target.parse(t) for t in args.targets] if args.targets else [Target.host()]
    if args.module:
        _check_module(args.module)
        modules = [args.module]
    elif args.action == "build":
        modules = available_modules(root)
    else:
        modules = ["loadbot"]
    if not modules:
        raise ConfigError(f"no bot sources under {root / 'bots'}")
    try:
        for module in modules:
            for target in targets:
                if args.action == "build":
                    binary = build(module, target, root=root)
                else:
                    binary = fetch(module, target, root=root)
                rel = binary.path.relative_to(root).as_posix()
                print(f"{module} {target} sha256={binary.sha256} {rel}")
    except BotError as exc:
        print(f"bbench bots {args.action}: {exc}", file=sys.stderr)
        return ExitCode.ENV_GATE
    return ExitCode.OK


# --------------------------------------------------------------------------------------------------------------------
# Running a fleet
# --------------------------------------------------------------------------------------------------------------------

EventMatch = str | Callable[[dict[str, Any]], bool]


def _matcher(match: EventMatch) -> Callable[[dict[str, Any]], bool]:
    if isinstance(match, str):
        return lambda ev: ev.get("ev") == match
    return match


class BotFleet:
    """One running bot process.

    ``argv`` is the full command line (usually ``BotBinary.command(bot_args(...))``; tests pass a fake bot). Events
    are kept in arrival order with ``rx_ns`` added, and handed to ``on_event`` from the reader thread, which must not
    block. ``cpus`` pins the process (Linux before exec, Windows right after start) and sets ``GOMAXPROCS`` to match.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cpus: Sequence[int] | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        stderr_path: Path | None = None,
        clock: Callable[[], int] = mono_ns,
    ) -> None:
        if not argv:
            raise ValueError("BotFleet needs a command line")
        self.argv = list(argv)
        self.cpus = tuple(cpus) if cpus else ()
        self._env = dict(env or {})
        self._cwd = cwd
        self._on_event = on_event
        self._stderr_path = stderr_path
        self._clock = clock
        self._proc: subprocess.Popen[bytes] | None = None
        self._cond = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._last: dict[str, dict[str, Any]] = {}
        self._eof = False
        self._junk: deque[str] = deque(maxlen=50)
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._stdin_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stderr_file: IO[bytes] | None = None

    # -- lifecycle ----------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("BotFleet already started")
        env = os.environ.copy()
        env.update(self._env)
        if self.cpus:
            env["GOMAXPROCS"] = str(len(self.cpus))
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # lets stop() send CTRL_BREAK to it alone
        else:
            kwargs["start_new_session"] = True
            if self.cpus:
                if not hasattr(os, "sched_setaffinity"):
                    raise BotError(f"CPU pinning is not supported on {sys.platform}")
                cpus = set(self.cpus)
                kwargs["preexec_fn"] = lambda: os.sched_setaffinity(0, cpus)
        stderr: Any = subprocess.PIPE
        if self._stderr_path is not None:
            self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_file = self._stderr_path.open("wb")
            stderr = self._stderr_file
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                cwd=self._cwd,
                env=env,
                **kwargs,
            )
        except OSError as exc:
            self._close_stderr_file()
            raise BotError(f"cannot start the bot: {exc}") from exc
        if self.cpus and sys.platform == "win32":
            import psutil

            try:
                psutil.Process(self._proc.pid).cpu_affinity(list(self.cpus))
            except (psutil.Error, OSError) as exc:
                self.kill()
                raise BotError(f"cannot pin the bot to CPUs {list(self.cpus)}: {exc}") from exc
        self._spawn_thread(self._read_stdout, "bots-stdout")
        if self._stderr_file is None:
            self._spawn_thread(self._read_stderr, "bots-stderr")

    def _spawn_thread(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def __enter__(self) -> BotFleet:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.poll() if self._proc else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, quit_timeout_s: float = QUIT_TIMEOUT_S, signal_timeout_s: float = SIGNAL_TIMEOUT_S) -> int | None:
        """Stop the bot and return its exit code: ``quit``, then an interrupt, then a kill."""
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            self.quit()
            if not self._wait(quit_timeout_s):
                log.warning("bot ignored quit for %.0f s; interrupting it", quit_timeout_s)
                self._interrupt()
                if not self._wait(signal_timeout_s):
                    log.warning("bot ignored the interrupt; killing it")
                    self.kill()
                    self._wait(10.0)
        self._close_stdin()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._close_stderr_file()
        return proc.poll()

    def kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _wait(self, timeout_s: float) -> bool:
        assert self._proc is not None
        try:
            self._proc.wait(timeout=timeout_s)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _interrupt(self) -> None:
        assert self._proc is not None
        try:
            if sys.platform == "win32":
                os.kill(self._proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)
        except OSError:
            pass

    def _close_stdin(self) -> None:
        with self._stdin_lock:
            if self._proc is not None and self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass

    def _close_stderr_file(self) -> None:
        if self._stderr_file is not None:
            self._stderr_file.close()
            self._stderr_file = None

    # -- stdin --------------------------------------------------------------------------------------------------------

    def send(self, line: str) -> bool:
        """Write one line to the bot's stdin. Returns False when the bot is gone."""
        if "\n" in line or "\r" in line:
            raise ValueError("a control line must not contain line breaks")
        with self._stdin_lock:
            if self._proc is None or self._proc.stdin is None or self._proc.stdin.closed:
                return False
            try:
                self._proc.stdin.write(line.encode("utf-8") + b"\n")
                self._proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def control(self, op: str, **params: Any) -> bool:
        """Send a JSON control command (needs ``-control``), e.g. ``control("reconnect", i=7, delay_ms=0)``."""
        return self.send(json.dumps({"op": op, **params}, separators=(",", ":"), sort_keys=True))

    def quit(self) -> bool:
        return self.send("quit")

    # -- events -------------------------------------------------------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            rx_ns = self._clock()
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                event = None
            if not isinstance(event, dict) or not isinstance(event.get("ev"), str):
                self._junk.append(text[:500])
                log.warning("bot wrote a line that is not an event: %.200s", text)
                continue
            event["rx_ns"] = rx_ns
            with self._cond:
                self._events.append(event)
                self._last[event["ev"]] = event
                self._cond.notify_all()
            if self._on_event is not None:
                try:
                    self._on_event(event)
                except Exception:
                    log.exception("bot event handler failed")
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for raw in self._proc.stderr:
            self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())

    def __len__(self) -> int:
        with self._cond:
            return len(self._events)

    def events(self, match: EventMatch | None = None, since: int = 0) -> list[dict[str, Any]]:
        """Events received so far from index ``since`` on, optionally only those that match."""
        with self._cond:
            selected = self._events[since:]
        if match is None:
            return list(selected)
        test = _matcher(match)
        return [ev for ev in selected if test(ev)]

    def last(self, ev: str) -> dict[str, Any] | None:
        """The most recent event of a kind."""
        with self._cond:
            return self._last.get(ev)

    @property
    def hello(self) -> dict[str, Any] | None:
        return self.last("hello")

    @property
    def finished(self) -> bool:
        """True once the bot wrote ``done``: every summary is in."""
        return self.last("done") is not None

    def wait_for(self, match: EventMatch, timeout_s: float, since: int = 0) -> dict[str, Any] | None:
        """Block until an event at index ``since`` or later matches, the bot's output ends, or the timeout passes."""
        test = _matcher(match)
        deadline = mono_ns() + int(timeout_s * 1e9)
        index = since
        with self._cond:
            while True:
                while index < len(self._events):
                    event = self._events[index]
                    index += 1
                    if test(event):
                        return event
                if self._eof:
                    return None
                remaining = (deadline - mono_ns()) / 1e9
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    @property
    def junk_lines(self) -> list[str]:
        """Lines on stdout that were not events (kept for diagnostics, at most 50)."""
        return list(self._junk)

    @property
    def stderr_tail(self) -> list[str]:
        """The last stderr lines when stderr is not written to a file."""
        return list(self._stderr_tail)
