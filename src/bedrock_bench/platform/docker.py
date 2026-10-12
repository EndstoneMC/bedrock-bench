# SPDX-License-Identifier: Apache-2.0
"""The Docker runtime: Linux servers in containers, with the bots and samplers beside them.

A machine profile with ``runtime.kind = "docker"`` runs every server from its Linux build inside a container on a
Linux Docker engine: a native one, Docker Desktop on Windows or macOS, or an engine inside WSL2. One run is a small
pod of containers that share one PID namespace and one network namespace. The namespaces belong to a ``pod``
container that only sleeps, so the others can start and stop in any order without taking the namespaces with them.

=========  ===========  =========================================================================================
Container  CPU set      Job
=========  ===========  =========================================================================================
pod        harness      holds the PID and network namespaces (tini and ``sleep infinity``)
server     server       the adapter's launch command in a fresh volume mounted at ``/data``; console on stdin and
                        stdout, LF line endings
threads    harness      ``python -m bedrock_bench.sampling``: the per-thread, tick-thread and process samplers
machine    harness      the machine sampler, only when the engine runs in a VM (Docker Desktop)
bots       bot          the load bot binary from ``bots/bin/linux-amd64``, mounted read-only; dials 127.0.0.1
=========  ===========  =========================================================================================

Why it is built this way:

- **Bots share the server's network namespace** and connect to 127.0.0.1. Publishing the UDP port with ``-p`` goes
  through Docker Desktop's userspace proxy, which delivers a fraction of the native chunk traffic.
- **Samplers stamp where they read.** The ``threads`` sidecar reads ``/proc`` of the server from inside the pod and
  stamps each sample right after reading it. On a native Linux engine the sidecar and the harness share the kernel's
  monotonic clock, so stamps need no mapping. On a VM engine the sidecars' clock is mapped onto the harness clock by a
  minimum-delay fit over their heartbeat events, and their series files are shifted once they have stopped.
- **Pinning is the cgroup cpuset** (``--cpuset-cpus``). The kernel confines every thread of the container to it, so
  checking the container's cpuset checks every thread. The profile priority ``above_normal`` becomes ``nice -n -5``
  with ``CAP_SYS_NICE``, the same nice value as a native Linux run.
- **Server files are never part of an image.** The adapter prepares a fresh server copy from the verified cache as
  usual; the runtime streams it into a fresh volume per run and copies the config files, the executable and crash
  files back afterwards, so the run's checks read them from the host copy as on a native run.
- **The first line of the server container is the harness's own:** the start command prints the PID of the launch
  process in the pod's namespace (``MARKER``) and then replaces itself with the launch command, so the samplers know
  which process to watch without asking Docker. That line never reaches the console.
- **Images** are built from ``docker/*.Dockerfile`` with base images pinned by digest in ``artifacts/images.toml``
  and tagged by a hash of their inputs, so a changed Dockerfile, pin or lock file gives a new image.

On a VM engine (Docker Desktop, WSL2) the CPUs in the profile are virtual CPUs of the VM and the placement is
recorded as ``vcpu``; such runs are valid but not comparable with native ones.

Using the runtime for one run (the phases call these at the matching points of a native run)::

    runtime = DockerRuntime(run_id, placement)
    runtime.prepare_images(spec.image, arm)                 # before boot: builds missing images
    launched = runtime.start_server(spec, layout, arm=arm, server_exe=server_exe(adapter, layout))
    console = Console(launched.stdout, launched.stdin, ...)  # launched duck-types process.Launched
    server = runtime.start_samplers(run_dir / "series")      # duck-types psutil.Process for DeathWatch and facts
    tid = adapter.tick_thread(runtime.threads.infos()); runtime.start_tick(tid)
    runtime.verify_placement("ready")                        # host.PlacementCheck
    fleet = BotFleet(runtime.bot_command(bot_argv), ...)     # bot_argv from bots.locate(module, target=linux/amd64)
    for group, pid in runtime.machine_roots() + runtime.bot_roots(): ...  # host machine sampler, same kernel only
    ...                                                      # stop the bots and the server as usual
    runtime.stop_samplers(); runtime.copy_back(layout, copy_back_paths(adapter, layout)); runtime.close()

``python -m bedrock_bench.platform.docker`` holds the small helpers the runtime runs inside its containers (engine
probe, process inspection, machine sampler, UDP echo for the network namespace check).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING, Any

from bedrock_bench.cache import short_key
from bedrock_bench.clock import OffsetMap, Stamp, mono_ns
from bedrock_bench.config import ConfigError, file_sha256, load_artifacts, load_toml, parse_cpu_list, project_root
from bedrock_bench.model import Arm, LaunchSpec, Layout, Machine, Placement, ThreadInfo, sha256_json
from bedrock_bench.platform import CpuRecord

if TYPE_CHECKING:
    from bedrock_bench.adapters.base import ServerAdapter
    from bedrock_bench.host import Check, PlacementCheck, Topology

log = logging.getLogger(__name__)

#: JSON array that replaces the ``docker`` command (tests point it at a fake).
ENV_DOCKER = "BBENCH_DOCKER"
IMAGES_FILE = "artifacts/images.toml"
#: Label on every container and volume of a run; its value is the run id.
LABEL = "org.bedrock-bench.run"
LABEL_INPUTS = "org.bedrock-bench.inputs"
IMAGE_REPO = "bedrock-bench"
NAME_PREFIX = "bbench-"

DATA = "/data"  # the server copy inside the server container
SERIES = "/series"  # the run's series directory inside the sampler sidecars
BOT_PATH = "/opt/bedrock-bench/bot"  # the mounted bot binary
#: The bot build the bots container runs: ``bots.locate(module, target=Target.parse(BOT_TARGET))``.
BOT_TARGET = "linux/amd64"

#: Image keys the runtime itself uses: the sidecars, and the pod and the mounted bot binary.
SAMPLER_IMAGE = "sampler"
RUNTIME_IMAGE = "runtime"
ROLES = ("server", "harness", "build")

MARKER = "@@bedrock-bench-pid"
#: Start command of the server container: report the launch process's PID, then become the launch command.
WRAPPER = f'printf "{MARKER} %s\\n" "$$"; exec "$@"'
#: Lists the paths that exist under /data, NUL-separated, and writes them as one tar stream to stdout.
COPY_BACK_SCRIPT = 'cd /data && for p in "$@"; do [ -e "$p" ] && printf "%s\\0" "$p"; done | tar -c -f - --null -T -'

#: Nice values of the profile priorities; the same values a native Linux run uses.
NICE = {"normal": 0, "above_normal": -5}

#: Series files the ``threads`` and ``machine`` sidecars write; each stamps its rows in a ``t_ns`` column or key.
THREADS_SIDECAR_SERIES = ("threads.csv.gz", "tick.csv.gz", "process.csv.gz")
MACHINE_SIDECAR_SERIES = ("machine.csv.gz", "foreign_top.jsonl")

START_TIMEOUT_S = 120.0  # docker run until the marker line
HELLO_TIMEOUT_S = 180.0  # sidecar start until it has found the server (a launcher starts it as a child)
REQUEST_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 30.0
#: A shared clock that disagrees with the heartbeat fit by more than this is not trusted.
SHARED_CLOCK_TOLERANCE_NS = 50_000_000

#: ``name[:tag]@sha256:<digest>``; the name may carry a registry host and port.
_IMAGE_REF_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)*(?::[\w][\w.-]{0,127})?@sha256:[0-9a-f]{64}$"
)
_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_ARG_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TEMPLATE_FIELDS = {"version", "url", "sha256"}
_SKIP_CONTEXT = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}
ELF_MAGIC = b"\x7fELF"


class DockerError(RuntimeError):
    """A Docker command failed or the engine cannot run what was asked."""


# --------------------------------------------------------------------------------------------------------------------
# The docker command
# --------------------------------------------------------------------------------------------------------------------


def _default_argv() -> tuple[str, ...]:
    override = os.environ.get(ENV_DOCKER)
    if override:
        try:
            argv = json.loads(override)
        except ValueError:
            argv = None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ConfigError(f"${ENV_DOCKER} must be a JSON array of strings, the docker command")
        return tuple(argv)
    return (shutil.which("docker") or "docker",)


class DockerCli:
    """The ``docker`` command line. Every call is a separate process; nothing talks to the engine's socket."""

    def __init__(self, argv: Sequence[str] | None = None) -> None:
        self.argv = tuple(argv) if argv else _default_argv()

    def command(self, *args: str) -> list[str]:
        return [*self.argv, *args]

    def run(
        self,
        *args: str,
        input: bytes | None = None,
        timeout: float | None = 120.0,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                self.command(*args),
                input=input,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError:
            raise DockerError(f"{self.argv[0]} not found; install Docker or set ${ENV_DOCKER}") from None
        except subprocess.TimeoutExpired:
            raise DockerError(f"docker {' '.join(args[:2])} did not finish in {timeout:g} s") from None
        if check and result.returncode != 0:
            tail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()[-1500:]
            raise DockerError(f"docker {' '.join(args[:2])} failed with exit code {result.returncode}: {tail}")
        return result

    def text(self, *args: str, **kwargs: Any) -> str:
        return self.run(*args, **kwargs).stdout.decode("utf-8", errors="replace").strip()

    def json(self, *args: str, **kwargs: Any) -> Any:
        text = self.text(*args, **kwargs)
        try:
            return json.loads(text)
        except ValueError:
            raise DockerError(f"docker {' '.join(args[:2])} printed no JSON: {text[:200]!r}") from None

    def popen(
        self,
        *args: str,
        stdin: int | IO[Any] | None = subprocess.PIPE,
        stdout: int | IO[Any] | None = subprocess.PIPE,
        stderr: int | IO[Any] | None = subprocess.PIPE,
    ) -> subprocess.Popen[bytes]:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(self.command(*args), stdin=stdin, stdout=stdout, stderr=stderr, **kwargs)
        except FileNotFoundError:
            raise DockerError(f"{self.argv[0]} not found; install Docker or set ${ENV_DOCKER}") from None


def available(cli: DockerCli | None = None) -> bool:
    """Whether a Docker engine answers."""
    try:
        version = (cli or DockerCli()).text("version", "--format", "{{.Server.Version}}", timeout=20)
    except (DockerError, ConfigError):
        return False
    return bool(re.match(r"^\d", version))


# --------------------------------------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineInfo:
    """What ``docker info`` says about the engine. The engine's host name is deliberately not kept."""

    version: str
    os: str
    os_type: str
    kernel: str
    ncpu: int
    mem_bytes: int
    cgroup_version: str | None
    cpuset: bool  # --cpuset-cpus works
    architecture: str
    desktop: bool  # Docker Desktop: the engine runs in a VM
    security: tuple[str, ...] = ()

    @property
    def vm(self) -> bool:
        """The engine's kernel is a VM's kernel (Docker Desktop, WSL2)."""
        return self.desktop or "microsoft" in self.kernel.lower()

    def describe(self) -> str:
        where = "Docker Desktop VM" if self.desktop else self.os
        return (
            f"Docker {self.version} on {where}, kernel {self.kernel}, {self.ncpu} CPUs, cgroup v{self.cgroup_version}"
        )


def parse_engine_info(data: Mapping[str, Any]) -> EngineInfo:
    errors = data.get("ServerErrors")
    if errors:
        raise DockerError("the Docker engine is not reachable: " + "; ".join(str(e) for e in errors))
    os_name = str(data.get("OperatingSystem") or "")
    security = tuple(str(s) for s in data.get("SecurityOptions") or ())
    return EngineInfo(
        version=str(data.get("ServerVersion") or "unknown"),
        os=os_name,
        os_type=str(data.get("OSType") or ""),
        kernel=str(data.get("KernelVersion") or ""),
        ncpu=int(data.get("NCPU") or 0),
        mem_bytes=int(data.get("MemTotal") or 0),
        cgroup_version=str(data["CgroupVersion"]) if data.get("CgroupVersion") else None,
        cpuset=bool(data.get("CPUSet", False)),
        architecture=str(data.get("Architecture") or ""),
        desktop="docker desktop" in os_name.lower(),
        security=security,
    )


def engine_info(cli: DockerCli | None = None) -> EngineInfo:
    return parse_engine_info((cli or DockerCli()).json("info", "--format", "{{json .}}", timeout=30))


@dataclass(frozen=True)
class EngineProbe:
    """What a container sees of the engine's machine (``python -m bedrock_bench.platform.docker probe``)."""

    kernel: str
    boot_id: str | None
    virtualization: str | None  # wsl2, a hypervisor name, or None on bare metal
    cpu_model: str
    cpus: tuple[CpuRecord, ...]
    clk_tck: int | None = None
    schedstat: bool = True


def parse_probe(data: Mapping[str, Any]) -> EngineProbe:
    try:
        cpus = tuple(
            CpuRecord(
                cpu=int(c["cpu"]),
                core=int(c["core"]),
                package=int(c.get("package", 0)),
                efficiency=int(c.get("efficiency", 0)),
            )
            for c in data["cpus"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DockerError(f"unexpected engine probe output: {exc!r}") from None
    if not cpus:
        raise DockerError("the engine probe found no CPUs")
    return EngineProbe(
        kernel=str(data.get("kernel") or ""),
        boot_id=str(data["boot_id"]) if data.get("boot_id") else None,
        virtualization=str(data["virtualization"]) if data.get("virtualization") else None,
        cpu_model=str(data.get("cpu_model") or "unknown"),
        cpus=cpus,
        clk_tck=int(data["clk_tck"]) if data.get("clk_tck") else None,
        schedstat=bool(data.get("schedstat", True)),
    )


def probe_engine(
    cli: DockerCli | None = None, catalog: ImageCatalog | None = None, *, root: Path | None = None
) -> EngineProbe:
    """Run the engine probe in a throwaway sampler container (building the sampler image first if needed)."""
    cli = cli or DockerCli()
    sampler = ensure_image(cli, plan_build(catalog or load_images(root), SAMPLER_IMAGE, root=root))
    text = cli.text(
        "run",
        "--rm",
        "--log-driver",
        "none",
        sampler.ref,
        "python",
        "-m",
        "bedrock_bench.platform.docker",
        "probe",
        timeout=120,
    )
    return parse_probe(_last_json(text))


def engine_virtualization(engine: EngineInfo | None, probe: EngineProbe) -> str | None:
    """``wsl2``, ``docker-desktop`` or a hypervisor name when containers run on virtual CPUs, else None."""
    if probe.virtualization:
        return probe.virtualization
    if engine is not None and "microsoft" in engine.kernel.lower():
        return "wsl2"
    if engine is not None and engine.desktop:
        return "docker-desktop"
    return None


def engine_topology(probe: EngineProbe, engine: EngineInfo | None = None) -> Topology:
    """The CPUs containers can be pinned to, for ``host.plan_placement``. Virtual CPUs give ``kind = vcpu``."""
    from bedrock_bench.host import Topology

    return Topology(
        cpus=probe.cpus,
        model=probe.cpu_model,
        virtualization=engine_virtualization(engine, probe),
        machine_logical=len(probe.cpus),
        machine_physical=len({(c.package, c.core) for c in probe.cpus}),
    )


def host_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() or None
    except OSError:
        return None


def shared_clock(probe: EngineProbe) -> bool:
    """Whether containers run on this host's kernel, so their monotonic clock is the harness's clock."""
    return sys.platform.startswith("linux") and probe.boot_id is not None and probe.boot_id == host_boot_id()


def machine_facts(engine: EngineInfo, probe: EngineProbe, base: Mapping[str, Any]) -> dict[str, Any]:
    """The run.json ``machine`` block for a Docker run: the host's facts with the engine's kernel and CPUs."""
    topology = engine_topology(probe, engine)
    facts = dict(base)
    facts.update(
        kernel=engine.kernel or probe.kernel or None,
        cpu=probe.cpu_model,
        logical=len(probe.cpus),
        physical=topology.machine_physical,
        core_classes=topology.core_classes(),
        virtualized=topology.virtualization is not None,
    )
    if engine.mem_bytes:
        facts["ram_gib"] = round(engine.mem_bytes / 2**30, 1)
    return facts


# --------------------------------------------------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PerVersion:
    """Build inputs that depend on the arm's version, taken from a pinned artifact (``[image.<key>.per_version]``)."""

    artifacts: str  # artifacts/<name>.toml
    platform: str  # platform key of the pinned artifact, e.g. linux-cp313
    args: Mapping[str, str]  # templates over {version}, {url} and {sha256}


@dataclass(frozen=True)
class ImageSpec:
    """One image of ``artifacts/images.toml``: built from a Dockerfile, or a pinned base pulled as is."""

    key: str
    role: str  # server: a LaunchSpec may name it; harness: used by the runtime; build: tooling only
    description: str = ""
    dockerfile: str | None = None
    pull: str | None = None  # base key
    bases: Mapping[str, str] = field(default_factory=dict)  # build arg -> base key
    args: Mapping[str, str] = field(default_factory=dict)  # literal build args
    context: tuple[str, ...] = ()  # project-relative files and folders; may contain {version}
    per_version: PerVersion | None = None


@dataclass(frozen=True)
class ImageCatalog:
    path: Path
    sha256: str
    bases: Mapping[str, str]  # base key -> name:tag@sha256:digest
    images: Mapping[str, ImageSpec]

    def image(self, key: str) -> ImageSpec:
        try:
            return self.images[key]
        except KeyError:
            raise ConfigError(f"no image {key!r} (known: {', '.join(sorted(self.images))})", file=self.path) from None


def _relative(path: str, what: str, file: Path | None) -> str:
    pure = PurePosixPath(path)
    if not path or "\\" in path or pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", path):
        raise ConfigError(f"{what} must be a relative POSIX path inside the project, got {path!r}", file=file)
    return path


def _str_map(value: Any, what: str, file: Path | None, key_re: re.Pattern[str]) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ConfigError(f"{what} must be a table of strings", file=file)
    bad = [k for k in value if not key_re.match(k)]
    if bad:
        raise ConfigError(f"{what}: bad keys {bad}", file=file)
    return dict(value)


def parse_images(data: Mapping[str, Any], *, file: Path | None = None) -> ImageCatalog:
    """Validate ``artifacts/images.toml``."""
    known_top = {"schema", "base", "image"}
    unknown = set(data) - known_top
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}", file=file)
    if data.get("schema") != 1:
        raise ConfigError("only schema = 1 is supported", file=file, key="schema")
    bases = _str_map(data.get("base", {}), "[base]", file, _KEY_RE)
    for key, ref in bases.items():
        if not _IMAGE_REF_RE.match(ref):
            raise ConfigError(f"must be pinned as name:tag@sha256:<digest>, got {ref!r}", file=file, key=f"base.{key}")
    images: dict[str, ImageSpec] = {}
    raw_images = data.get("image", {})
    if not isinstance(raw_images, dict) or not raw_images:
        raise ConfigError("[image.<key>] tables are missing", file=file)
    for key, raw in raw_images.items():
        where = f"image.{key}"
        if not _KEY_RE.match(key) or not isinstance(raw, dict):
            raise ConfigError("is not a valid image table", file=file, key=where)
        allowed = {"role", "description", "dockerfile", "pull", "bases", "args", "context", "per_version"}
        extra = set(raw) - allowed
        if extra:
            raise ConfigError(f"unknown keys {sorted(extra)}", file=file, key=where)
        role = raw.get("role")
        if role not in ROLES:
            raise ConfigError(f"role must be one of {', '.join(ROLES)}", file=file, key=f"{where}.role")
        dockerfile, pull = raw.get("dockerfile"), raw.get("pull")
        if (dockerfile is None) == (pull is None):
            raise ConfigError("needs exactly one of dockerfile and pull", file=file, key=where)
        if pull is not None and pull not in bases:
            raise ConfigError(f"pulls unknown base {pull!r}", file=file, key=f"{where}.pull")
        if dockerfile is not None:
            dockerfile = _relative(str(dockerfile), f"{where}.dockerfile", file)
        image_bases = _str_map(raw.get("bases", {}), f"{where}.bases", file, _ARG_RE)
        missing = sorted(set(image_bases.values()) - set(bases))
        if missing:
            raise ConfigError(f"names unknown bases {missing}", file=file, key=f"{where}.bases")
        args = _str_map(raw.get("args", {}), f"{where}.args", file, _ARG_RE)
        context = raw.get("context", [])
        if not isinstance(context, list) or not all(isinstance(c, str) for c in context):
            raise ConfigError("must be a list of paths", file=file, key=f"{where}.context")
        per_version = None
        if "per_version" in raw:
            pv = raw["per_version"]
            if not isinstance(pv, dict) or set(pv) - {"artifacts", "platform", "args"}:
                raise ConfigError("needs artifacts, platform and args", file=file, key=f"{where}.per_version")
            templates = _str_map(pv.get("args", {}), f"{where}.per_version.args", file, _ARG_RE)
            for template in templates.values():
                fields = set(re.findall(r"\{(\w+)\}", template))
                if fields - _TEMPLATE_FIELDS:
                    raise ConfigError(
                        f"unknown template fields {sorted(fields - _TEMPLATE_FIELDS)}",
                        file=file,
                        key=f"{where}.per_version.args",
                    )
            if not isinstance(pv.get("artifacts"), str) or not isinstance(pv.get("platform"), str):
                raise ConfigError("needs artifacts and platform strings", file=file, key=f"{where}.per_version")
            per_version = PerVersion(pv["artifacts"], pv["platform"], templates)
        for entry in context:
            _relative(entry.replace("{version}", "0"), f"{where}.context", file)
            if "{" in entry.replace("{version}", ""):
                raise ConfigError(f"only {{version}} may appear in a context path: {entry!r}", file=file)
            if "{version}" in entry and per_version is None:
                raise ConfigError("{version} in a context path needs per_version", file=file, key=where)
        images[key] = ImageSpec(
            key=key,
            role=role,
            description=str(raw.get("description", "")),
            dockerfile=dockerfile,
            pull=pull,
            bases=image_bases,
            args=args,
            context=tuple(context),
            per_version=per_version,
        )
    for needed in (SAMPLER_IMAGE, RUNTIME_IMAGE):
        if needed not in images:
            raise ConfigError(f"the runtime needs an image {needed!r}", file=file)
    return ImageCatalog(
        path=file if file is not None else Path(IMAGES_FILE),
        sha256=file_sha256(file) if file is not None else sha256_json(data),
        bases=bases,
        images=images,
    )


def load_images(root: Path | None = None) -> ImageCatalog:
    path = (root or project_root()) / IMAGES_FILE
    return parse_images(load_toml(path), file=path)


@dataclass(frozen=True)
class ImageBuild:
    """Everything that goes into one image, and the tag derived from it."""

    key: str
    tag: str
    root: Path
    dockerfile: str | None  # None: pulled as is
    build_args: Mapping[str, str]
    files: tuple[str, ...]  # project-relative files sent as the build context (besides the Dockerfile)
    inputs_sha256: str
    bases: tuple[str, ...]  # pinned base references, for provenance

    @property
    def pulled(self) -> bool:
        return self.dockerfile is None


@dataclass(frozen=True)
class ImageRef:
    """An image present on the engine."""

    key: str
    ref: str  # tag (built) or pinned reference (pulled)
    id: str  # sha256:<hex>
    bases: tuple[str, ...] = ()

    def artifact(self) -> dict[str, Any]:
        """A run.json ``artifacts`` entry for this image."""
        return {
            "name": f"image:{self.key}",
            "url": self.bases[0] if self.bases else self.ref,
            "sha256": self.id.removeprefix("sha256:"),
            "size": None,
            "source": "build",
        }


def _context_files(root: Path, entries: Iterable[str]) -> list[str]:
    files: set[str] = set()
    for entry in entries:
        path = root / entry
        if path.is_file():
            files.add(entry)
        elif path.is_dir():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_CONTEXT)
                for name in filenames:
                    if name in _SKIP_CONTEXT or name.endswith((".pyc", ".pyo")):
                        continue
                    files.add((Path(dirpath) / name).relative_to(root).as_posix())
        else:
            raise ConfigError(f"build context entry {entry!r} does not exist", file=path)
    return sorted(files)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_build(catalog: ImageCatalog, key: str, *, version: str | None = None, root: Path | None = None) -> ImageBuild:
    """The inputs and tag of one image. ``version`` is required for images with per-version inputs."""
    root = root or project_root()
    spec = catalog.image(key)
    if spec.pull is not None:
        ref = catalog.bases[spec.pull]
        return ImageBuild(key, ref, root, None, {}, (), sha256_json({"pull": ref}), (ref,))
    assert spec.dockerfile is not None
    args = {arg: catalog.bases[base] for arg, base in spec.bases.items()}
    args.update(spec.args)
    context = list(spec.context)
    if spec.per_version is not None:
        if not version:
            raise ConfigError(f"image {key!r} is built per version; say which")
        pinned = load_artifacts(spec.per_version.artifacts).get(version, spec.per_version.platform)
        if not pinned.sha256:
            raise ConfigError(f"{spec.per_version.artifacts} {version} {spec.per_version.platform} is not pinned")
        values = {"version": version, "url": pinned.url, "sha256": pinned.sha256}
        args.update({arg: template.format(**values) for arg, template in spec.per_version.args.items()})
        context = [entry.replace("{version}", version) for entry in context]
    dockerfile = root / spec.dockerfile
    if not dockerfile.is_file():
        raise ConfigError("Dockerfile not found", file=dockerfile)
    files = _context_files(root, context)
    inputs = sha256_json(
        {
            "dockerfile": _file_digest(dockerfile),
            "args": args,
            "files": {name: _file_digest(root / name) for name in files},
        }
    )
    tag_version = f"{version}-" if spec.per_version is not None and version else ""
    tag = f"{IMAGE_REPO}/{key}:{tag_version}{inputs[:12]}"
    main_first = sorted(spec.bases.items(), key=lambda item: item[0] != "BASE_IMAGE")
    bases = tuple(catalog.bases[base] for _, base in main_first)
    return ImageBuild(key, tag, root, spec.dockerfile, args, tuple(files), inputs, bases)


def _tarinfo(name: str, size: int, mode: int, mtime: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def context_tar(build: ImageBuild) -> bytes:
    """The build context as a deterministic tar: the Dockerfile at its root and the declared files, nothing else.

    Context files keep their project-relative paths, so the Dockerfile reads them the same way as in a manual build
    from the project root.
    """
    if build.dockerfile is None:
        raise DockerError(f"image {build.key!r} is pulled, not built")
    entries = {"Dockerfile": build.root / build.dockerfile, **{name: build.root / name for name in build.files}}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(entries):
            data = entries[name].read_bytes()
            tar.addfile(_tarinfo(name, len(data), 0o644), io.BytesIO(data))
    return buffer.getvalue()


def image_id(cli: DockerCli, ref: str) -> str | None:
    result = cli.run("image", "inspect", "--format", "{{.Id}}", ref, check=False, timeout=60)
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return text if result.returncode == 0 and text.startswith("sha256:") else None


def ensure_image(cli: DockerCli, build: ImageBuild, report: Callable[[str], None] | None = None) -> ImageRef:
    """The image for ``build``: pulled or built when the engine does not have it yet."""
    found = image_id(cli, build.tag)
    if found is None:
        if build.pulled:
            (report or log.info)(f"pulling {build.tag}")
            cli.run("pull", "--quiet", build.tag, timeout=1800)
        else:
            assert build.dockerfile is not None
            (report or log.info)(f"building image {build.tag}")
            args = [f"--build-arg={key}={value}" for key, value in sorted(build.build_args.items())]
            cli.run(
                "build",
                "--tag",
                build.tag,
                "--label",
                f"{LABEL_INPUTS}={build.inputs_sha256}",
                *args,
                "-",
                input=context_tar(build),
                timeout=3600,
                env={"DOCKER_BUILDKIT": "1"},
            )
        found = image_id(cli, build.tag)
        if found is None:
            raise DockerError(f"image {build.tag} is still missing after the build")
    return ImageRef(build.key, build.tag, found, build.bases)


# --------------------------------------------------------------------------------------------------------------------
# Names and command lines
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunNames:
    """Container and volume names of one run. Short and derived from the run id, so a rerun of the id collides."""

    run_id: str
    prefix: str

    @classmethod
    def for_run(cls, run_id: str) -> RunNames:
        return cls(run_id, NAME_PREFIX + short_key(run_id, length=10))

    @property
    def pod(self) -> str:
        return f"{self.prefix}-pod"

    @property
    def server(self) -> str:
        return f"{self.prefix}-server"

    @property
    def threads(self) -> str:
        return f"{self.prefix}-threads"

    @property
    def machine(self) -> str:
        return f"{self.prefix}-machine"

    @property
    def bots(self) -> str:
        return f"{self.prefix}-bots"

    @property
    def inspect(self) -> str:
        return f"{self.prefix}-inspect"

    @property
    def volume(self) -> str:
        return f"{self.prefix}-data"

    def containers(self) -> tuple[str, ...]:
        """Every container name of the run, in the order they are removed."""
        return (self.bots, self.inspect, self.machine, self.threads, self.server, self.pod)

    def labels(self) -> list[str]:
        return ["--label", f"{LABEL}={self.run_id}"]


def cpu_list(cpus: Iterable[int]) -> str:
    """``(0, 1, 2, 5)`` -> ``"0-2,5"``, the form ``--cpuset-cpus`` takes."""
    values = sorted(set(cpus))
    if not values:
        raise DockerError("empty CPU set")
    parts: list[str] = []
    start = prev = values[0]
    for cpu in [*values[1:], None]:
        if cpu is not None and cpu == prev + 1:
            prev = cpu
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if cpu is not None:
            start = prev = cpu
    return ",".join(parts)


def mount(kind: str, source: str | Path, target: str, *, readonly: bool = False) -> str:
    """A ``--mount`` value; fields with commas or quotes are CSV-quoted (Windows paths may contain commas)."""
    fields = [f"type={kind}", f"source={source}", f"target={target}"]
    if readonly:
        fields.append("readonly")
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(fields)
    return buffer.getvalue()


def _base_run(names: RunNames, name: str, cpus: Iterable[int]) -> list[str]:
    return ["run", "--name", name, *names.labels(), "--log-driver", "none", "--cpuset-cpus", cpu_list(cpus)]


def pod_args(names: RunNames, image: str, cpus: Iterable[int]) -> list[str]:
    return [*_base_run(names, names.pod, cpus), "--detach", "--init", image, "sleep", "infinity"]


def volume_args(names: RunNames) -> list[str]:
    return ["volume", "create", *names.labels(), names.volume]


def copy_in_args(names: RunNames, image: str) -> list[str]:
    """Extract a tar stream from stdin into the run's volume."""
    return [
        "run",
        "--rm",
        "--interactive",
        *names.labels(),
        "--log-driver",
        "none",
        "--mount",
        mount("volume", names.volume, DATA),
        "--entrypoint",
        "tar",
        image,
        "-x",
        "-f",
        "-",
        "-C",
        DATA,
    ]


def copy_back_args(names: RunNames, image: str, paths: Sequence[str]) -> list[str]:
    """Write the given paths of the volume (those that exist) as one tar stream to stdout."""
    for path in paths:
        _relative(path, "copy-back path", None)
    return [
        "run",
        "--rm",
        *names.labels(),
        "--log-driver",
        "none",
        "--mount",
        mount("volume", names.volume, DATA, readonly=True),
        "--entrypoint",
        "sh",
        image,
        "-c",
        COPY_BACK_SCRIPT,
        "copy-back",
        *paths,
    ]


def server_command(argv: Sequence[str], priority: str, env_remove: Sequence[str] = ()) -> list[str]:
    """The launch command as the server container runs it: variables removed, priority applied, then exec."""
    if priority not in NICE:
        raise DockerError(f"unknown priority {priority!r}")
    command = list(argv)
    if NICE[priority]:
        command = ["nice", "-n", str(NICE[priority]), *command]
    if env_remove:
        command = ["env", *(arg for key in sorted(set(env_remove)) for arg in ("-u", key)), *command]
    return command


def server_args(
    names: RunNames,
    image: str,
    *,
    argv: Sequence[str],
    workdir: str,
    cpus: Iterable[int],
    priority: str,
    env: Mapping[str, str] | None = None,
    env_remove: Sequence[str] = (),
) -> list[str]:
    """``docker run`` for the server: in the pod's namespaces, on the server CPUs, with the volume at /data."""
    args = [
        *_base_run(names, names.server, cpus),
        "--interactive",
        "--pid",
        f"container:{names.pod}",
        "--network",
        f"container:{names.pod}",
        "--mount",
        mount("volume", names.volume, DATA),
        "--workdir",
        workdir,
    ]
    if NICE.get(priority):
        args += ["--cap-add", "SYS_NICE"]
    for key, value in sorted((env or {}).items()):
        args += ["--env", f"{key}={value}"]
    return [
        *args,
        "--entrypoint",
        "sh",
        image,
        "-c",
        WRAPPER,
        "bedrock-bench",
        *server_command(argv, priority, env_remove),
    ]


def sidecar_args(
    names: RunNames,
    image: str,
    series_dir: Path,
    cpus: Iterable[int],
    *,
    server_pid: int | None = None,
    server_exe: str | None = None,
    launcher_pid: int | None = None,
    wait_s: float = HELLO_TIMEOUT_S,
) -> list[str]:
    """The ``threads`` sidecar: ``python -m bedrock_bench.sampling`` watching the server from inside the pod."""
    if (server_pid is None) == (server_exe is None):
        raise DockerError("the sampler sidecar needs exactly one of server_pid and server_exe")
    target = ["--pid", str(server_pid)] if server_pid is not None else ["--exe", str(server_exe)]
    if launcher_pid is not None:
        target += ["--launcher-pid", str(launcher_pid)]
    return [
        *_base_run(names, names.threads, cpus),
        "--rm",
        "--interactive",
        "--pid",
        f"container:{names.pod}",
        "--mount",
        mount("bind", series_dir, SERIES),
        image,
        "python",
        "-m",
        "bedrock_bench.sampling",
        "--out",
        SERIES,
        *target,
        "--wait",
        f"{wait_s:g}",
    ]


def machine_sidecar_args(
    names: RunNames, image: str, series_dir: Path, cpus: Iterable[int], server_cpus: Iterable[int], server_pid: int
) -> list[str]:
    """The ``machine`` sidecar for VM engines: machine-wide CPU and foreign load as the VM's kernel sees them."""
    return [
        *_base_run(names, names.machine, cpus),
        "--rm",
        "--interactive",
        "--pid",
        f"container:{names.pod}",
        "--mount",
        mount("bind", series_dir, SERIES),
        image,
        "python",
        "-m",
        "bedrock_bench.platform.docker",
        "machine",
        "--out",
        SERIES,
        "--server-cpus",
        cpu_list(server_cpus),
        "--root",
        f"server={server_pid}",
        "--root",
        "harness=1",
        "--root-exe",
        f"bot={BOT_PATH}",
        "--root-cmd",
        "harness=bedrock_bench.sampling",
    ]


def inspect_args(names: RunNames, image: str, cpus: Iterable[int], pid: int) -> list[str]:
    return [
        *_base_run(names, names.inspect, cpus),
        "--rm",
        "--pid",
        f"container:{names.pod}",
        image,
        "python",
        "-m",
        "bedrock_bench.platform.docker",
        "inspect",
        "--pid",
        str(pid),
    ]


def bot_args_prefix(names: RunNames, image: str, binary: Path, cpus: Sequence[int]) -> list[str]:
    """``docker run`` for the bots, up to the bot's own arguments."""
    return [
        *_base_run(names, names.bots, cpus),
        "--rm",
        "--interactive",
        "--pid",
        f"container:{names.pod}",
        "--network",
        f"container:{names.pod}",
        "--env",
        f"GOMAXPROCS={len(set(cpus))}",
        "--mount",
        mount("bind", binary, BOT_PATH, readonly=True),
        "--entrypoint",
        BOT_PATH,
        image,
    ]


# --------------------------------------------------------------------------------------------------------------------
# Host paths inside the container
# --------------------------------------------------------------------------------------------------------------------


def container_path(server_dir: Path, host_path: str | Path) -> str:
    """Where a file of the host's server copy appears in the server container. ValueError if it is outside."""
    rel = Path(host_path).resolve().relative_to(Path(server_dir).resolve())
    return DATA if rel == Path(".") else posixpath.join(DATA, rel.as_posix())


def _map_value(server_dir: Path, value: str) -> str:
    if not Path(value).is_absolute():
        return value
    try:
        return container_path(server_dir, value)
    except ValueError:
        return value  # an absolute path in the image (e.g. /opt/java/bin/java) stays as it is


def map_command(spec: LaunchSpec, layout: Layout) -> tuple[list[str], str, dict[str, str]]:
    """``(argv, workdir, env)`` of a launch spec inside the server container."""
    try:
        workdir = container_path(layout.server_dir, spec.cwd)
    except ValueError:
        raise DockerError(f"the launch directory {spec.cwd} is outside the server copy") from None
    argv = [_map_value(layout.server_dir, arg) for arg in spec.argv]
    env = {key: _map_value(layout.server_dir, value) for key, value in spec.env_set.items()}
    return argv, workdir, env


def server_exe(adapter: ServerAdapter, layout: Layout) -> str | None:
    """The executable the samplers watch, as the server container sees it.

    None when the launch command's process is the server itself. An adapter whose ``server_process`` picks a child
    of a launcher must provide ``executable(layout)``; the child is then found by that path.
    """
    from bedrock_bench.adapters.base import ServerAdapter

    if type(adapter).server_process is ServerAdapter.server_process:
        return None
    path = _executable(adapter, layout)
    if path is None:
        raise DockerError(f"{adapter.name} starts its server through a launcher but has no executable(layout)")
    try:
        return container_path(layout.server_dir, path)
    except ValueError:
        raise DockerError(f"{adapter.name}: the server executable {path} is outside the server copy") from None


def _executable(adapter: ServerAdapter, layout: Layout) -> Path | None:
    """``adapter.executable(layout)`` when the adapter has one, else None."""
    executable = getattr(adapter, "executable", None)
    if executable is None:
        return None
    try:
        path = executable(layout)
    except (NotImplementedError, KeyError):
        return None
    return Path(path) if path is not None else None


def copy_back_paths(adapter: ServerAdapter, layout: Layout) -> list[str]:
    """Files copied from the volume to the host copy after a run: configs, the executable and crash folders."""
    paths = [config.path for config in layout.config_files]
    executable = _executable(adapter, layout)
    if executable is not None:
        try:
            paths.append(executable.resolve().relative_to(layout.server_dir.resolve()).as_posix())
        except ValueError:
            pass
    for entry in adapter.crash_paths:
        if "%" in entry or "$" in entry or Path(entry).is_absolute():
            continue
        paths.append(PurePosixPath(entry.replace("\\", "/")).as_posix())
    return sorted(set(paths))


# --------------------------------------------------------------------------------------------------------------------
# Tar streams
# --------------------------------------------------------------------------------------------------------------------


def _looks_executable(path: Path) -> bool:
    """On a host without exec bits (Windows): ELF binaries and scripts with a shebang."""
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head == ELF_MAGIC or head[:2] == b"#!"


def write_tree_tar(src: Path, fileobj: IO[bytes], *, executables: Iterable[str] = ()) -> int:
    """Write ``src`` as a tar stream (owner root, modes kept or inferred). Returns the number of files.

    POSIX hosts keep each file's permission bits. Windows hosts have none to keep, so ELF files, scripts and the
    ``executables`` (relative paths) become 0755 and everything else 0644.
    """
    src = Path(src)
    marked = {PurePosixPath(e).as_posix().removeprefix("./") for e in executables}
    count = 0
    with tarfile.open(fileobj=fileobj, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames.sort()
            here = Path(dirpath)
            for name in [*dirnames, *sorted(filenames)]:
                path = here / name
                rel = path.relative_to(src).as_posix()
                info = tar.gettarinfo(str(path), arcname=rel)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.isdir():
                    if os.name != "posix":
                        info.mode = 0o755
                    tar.addfile(info)
                elif info.issym():
                    tar.addfile(info)
                elif info.isfile():
                    if os.name != "posix":
                        executable = rel in marked or _looks_executable(path)
                        info.mode = 0o755 if executable else 0o644
                    elif rel in marked:
                        info.mode |= 0o111
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
                    count += 1
    return count


def _safe_member_path(name: str) -> PurePosixPath | None:
    """The member's relative path, or None for an absolute path, a drive letter or ``..``."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
        return None
    return pure


def extract_tar_stream(fileobj: IO[bytes], dest: Path) -> list[str]:
    """Extract regular files and folders from a tar stream into ``dest``, replacing what is there.

    Absolute names, ``..`` and links are skipped. File times are kept, so crash files can be told apart by age.
    """
    dest = Path(dest)
    written: list[str] = []
    with tarfile.open(fileobj=fileobj, mode="r|") as tar:
        for member in tar:
            rel = _safe_member_path(member.name)
            if rel is None:
                log.warning("skipping unsafe tar member %r", member.name)
                continue
            if not rel.parts:
                continue
            target = dest.joinpath(*rel.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                log.warning("skipping tar member %r that is not a regular file", member.name)
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                try:
                    target.unlink()
                except PermissionError:
                    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                    target.unlink()
            with target.open("wb") as out:
                shutil.copyfileobj(source, out)
            if os.name == "posix":
                os.chmod(target, stat.S_IMODE(member.mode) | stat.S_IRUSR | stat.S_IWUSR)
            os.utime(target, (member.mtime, member.mtime))
            written.append(rel.as_posix())
    return written


# --------------------------------------------------------------------------------------------------------------------
# Clocks and series files
# --------------------------------------------------------------------------------------------------------------------


class ClockFit:
    """Pairs of (sidecar monotonic ns, harness monotonic ns at receipt) and the offset they imply."""

    def __init__(self) -> None:
        self.pairs: list[tuple[int, int]] = []
        self._lock = threading.Lock()

    def add(self, source_ns: int, received_ns: int) -> None:
        with self._lock:
            self.pairs.append((int(source_ns), int(received_ns)))

    def extend(self, other: ClockFit) -> None:
        with self._lock:
            self.pairs.extend(other.pairs)

    def fit_ns(self) -> int | None:
        with self._lock:
            return OffsetMap.fit(self.pairs).offset_ns if self.pairs else None

    def offset_ns(self, shared: bool) -> int:
        """0 on a shared clock that the heartbeats confirm; otherwise the minimum-delay fit."""
        fit = self.fit_ns()
        if fit is None:
            if shared:
                return 0
            raise DockerError("no clock events from the sidecars; their samples cannot be placed in time")
        if shared and abs(fit) <= SHARED_CLOCK_TOLERANCE_NS:
            return 0
        if shared:
            log.warning("sidecar clock is %.1f ms away from the harness clock on a shared kernel", fit / 1e6)
        return fit


def _write_gzip_text(path: Path, lines: Iterable[str]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
            for line in lines:
                gz.write(line.encode("utf-8") + b"\n")
    os.replace(tmp, path)


def shift_series(path: Path, offset_ns: int, column: str = "t_ns") -> int:
    """Add ``offset_ns`` to one column of a series CSV (``.csv.gz``) or key of a ``.jsonl`` file, in place.

    A cut-off tail is dropped as the readers drop it. Returns the number of rows rewritten.
    """
    from bedrock_bench.sampling.series import iter_lines

    path = Path(path)
    if offset_ns == 0 or not path.is_file():
        return 0
    lines = list(iter_lines(path))
    out: list[str] = []
    if path.suffix == ".jsonl":
        for line in lines:
            if not line.strip():
                continue
            obj = json.loads(line)
            if isinstance(obj.get(column), int):
                obj[column] += offset_ns
            out.append(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("".join(f"{line}\n" for line in out), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        return len(out)
    if not lines:
        return 0
    header = next(csv.reader([lines[0]]))
    if column not in header:
        raise DockerError(f"{path.name} has no {column} column")
    index = header.index(column)
    out.append(lines[0])
    for row in csv.reader(lines[1:]):
        if len(row) != len(header):
            continue
        row[index] = str(int(row[index]) + offset_ns)
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(row)
        out.append(buffer.getvalue())
    if path.suffix == ".gz":
        _write_gzip_text(path, out)
    else:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("".join(f"{line}\n" for line in out), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    return len(out) - 1


# --------------------------------------------------------------------------------------------------------------------
# Running containers
# --------------------------------------------------------------------------------------------------------------------


def _inspect(cli: DockerCli, name: str) -> dict[str, Any] | None:
    result = cli.run("inspect", "--type", "container", name, check=False, timeout=30)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data[0] if isinstance(data, list) and data else None


def _state(info: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return (info or {}).get("State") or {}


@dataclass(eq=False)
class ContainerProcess:
    """The server container, shaped like :class:`bedrock_bench.process.Launched`.

    ``popen`` is the attached ``docker run``: its pipes are the server's console and its exit code is the
    container's. ``main_pid`` is the launch process in the pod's PID namespace.
    """

    popen: subprocess.Popen[bytes]
    cli: DockerCli
    name: str
    cpus: tuple[int, ...]
    priority: str
    main_pid: int
    started: Stamp
    method: str = "cpuset"
    verified: bool = False
    job: Any = None
    _closed: bool = field(default=False, repr=False)

    @property
    def pid(self) -> int:
        return self.popen.pid

    @property
    def process(self) -> Any:
        import psutil

        return psutil.Process(self.popen.pid)

    @property
    def stdin(self) -> IO[bytes] | None:
        return self.popen.stdin

    @property
    def stdout(self) -> IO[bytes] | None:
        return self.popen.stdout

    def poll(self) -> int | None:
        return self.popen.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def cpuset(self) -> tuple[int, ...] | None:
        """The container's cpuset as the engine reports it; None when the container is gone."""
        info = _inspect(self.cli, self.name)
        if info is None:
            return None
        text = ((info.get("HostConfig") or {}).get("CpusetCpus")) or ""
        return parse_cpu_list(text) if text else ()

    def verify(self) -> bool:
        return self.cpuset() == tuple(sorted(self.cpus))

    def descendants(self) -> list[Any]:
        return []

    def interrupt(self) -> bool:
        """SIGINT to the launch process, so the server can save and exit."""
        if self.poll() is not None:
            return False
        result = self.cli.run("kill", "--signal", "INT", self.name, check=False, timeout=30)
        return result.returncode == 0

    def kill_tree(self, timeout_s: float = 10.0) -> list[int]:
        alive = self.poll() is None
        self.cli.run("kill", self.name, check=False, timeout=30)
        self.wait(timeout_s)
        if self.poll() is None:
            self.popen.kill()
            self.wait(timeout_s)
        return [self.main_pid] if alive else []

    def oom_killed(self) -> bool | None:
        state = _state(_inspect(self.cli, self.name))
        return bool(state["OOMKilled"]) if "OOMKilled" in state else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for pipe in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


class ContainerServer:
    """The measured server process inside the pod, shaped like the parts of ``psutil.Process`` the phases use.

    The container exits when the server exits (a launcher waits for its server and exits with it), so liveness is
    the container's. ``pid`` is the server's PID in the pod's namespace.
    """

    def __init__(self, runtime: DockerRuntime, pid: int) -> None:
        self.runtime = runtime
        self.pid = pid

    def __repr__(self) -> str:
        return f"ContainerServer(pid={self.pid})"

    def is_running(self) -> bool:
        launched = self.runtime.launched
        return launched is not None and launched.poll() is None

    def status(self) -> str:
        return "running" if self.is_running() else "dead"

    def wait(self, timeout: float | None = None) -> int | None:
        launched = self.runtime.launched
        if launched is None:
            return None
        return launched.poll() if timeout == 0 else launched.wait(timeout)

    def memory_maps(self, grouped: bool = True) -> list[SimpleNamespace]:
        """Mapped files by base name, from the one-off inspection (enough to tell the allocator)."""
        modules = self.runtime.inspection().get("modules") or []
        return [SimpleNamespace(path=str(name)) for name in modules]


class RemoteSampler:
    """A sampler that runs in a sidecar, shaped like ``PeriodicSampler`` for the phases (name, error, stop)."""

    def __init__(self, name: str, sidecar: Sidecar) -> None:
        self.name = name
        self.sidecar = sidecar
        self.samples = 0
        self.ended: str | None = None
        self.overruns = 0

    @property
    def error(self) -> str | None:
        done = self.sidecar.done
        if done is None:
            return None if self.sidecar.running else f"the {self.sidecar.name} sidecar ended without reporting"
        report = (done.get("samplers") or {}).get(self.name)
        if report is None:
            return None
        self.samples = int(report.get("samples") or 0)
        self.ended = report.get("ended")
        return report.get("error")

    def stop(self, timeout: float | None = STOP_TIMEOUT_S) -> None:
        self.sidecar.stop(timeout or STOP_TIMEOUT_S)


class ThreadsSampler(RemoteSampler):
    """The sidecar's per-thread sampler; :meth:`infos` lists the server's live threads (tick rule, census)."""

    def infos(self) -> list[ThreadInfo]:
        return self.sidecar.threads()


class Sidecar:
    """A sampler sidecar speaking JSON lines (``python -m bedrock_bench.sampling`` or the ``machine`` helper).

    Events are read on a thread and stamped on receipt; ``hello`` and ``clock`` events feed :attr:`clock`.
    """

    def __init__(self, popen: subprocess.Popen[bytes], name: str, *, clock: Callable[[], int] = mono_ns) -> None:
        self.popen = popen
        self.name = name
        self.clock = ClockFit()
        self.hello: dict[str, Any] | None = None
        self.done: dict[str, Any] | None = None
        self._now = clock
        self._cond = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._stderr: list[str] = []
        self._stdin_lock = threading.Lock()
        self._stopped = False
        self._reader = threading.Thread(target=self._read, name=f"bbench-{name}-events", daemon=True)
        self._reader.start()
        if popen.stderr is not None:
            threading.Thread(target=self._read_stderr, name=f"bbench-{name}-stderr", daemon=True).start()

    @property
    def running(self) -> bool:
        return self.popen.poll() is None

    def _read(self) -> None:
        assert self.popen.stdout is not None
        for raw in self.popen.stdout:
            rx = self._now()
            try:
                event = json.loads(raw)
            except ValueError:
                log.debug("%s: %s", self.name, raw.decode("utf-8", errors="replace").rstrip())
                continue
            if not isinstance(event, dict):
                continue
            event["rx_ns"] = rx
            kind = event.get("ev")
            if kind in ("hello", "clock") and isinstance(event.get("mono_ns"), int):
                self.clock.add(event["mono_ns"], rx)
            with self._cond:
                if kind == "hello":
                    self.hello = event
                elif kind == "done":
                    self.done = event
                self._events.append(event)
                self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    def _read_stderr(self) -> None:
        assert self.popen.stderr is not None
        for raw in self.popen.stderr:
            self._stderr.append(raw.decode("utf-8", errors="replace").rstrip())
            del self._stderr[:-50]

    def stderr_tail(self) -> str:
        return " | ".join(self._stderr[-5:])

    def _wait(self, predicate: Callable[[dict[str, Any]], bool], since: int, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                for event in self._events[since:]:
                    if predicate(event):
                        return event
                if not self._reader.is_alive():
                    raise DockerError(f"the {self.name} sidecar exited: {self.stderr_tail() or 'no output'}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerError(f"the {self.name} sidecar did not answer in {timeout_s:g} s")
                self._cond.wait(min(remaining, 0.5))

    def wait_hello(self, timeout_s: float = HELLO_TIMEOUT_S) -> dict[str, Any]:
        return self._wait(lambda e: e.get("ev") == "hello", 0, timeout_s)

    def send(self, op: str, **params: Any) -> bool:
        line = json.dumps({"op": op, **params}, separators=(",", ":")) + "\n"
        with self._stdin_lock:
            stdin = self.popen.stdin
            if stdin is None or stdin.closed:
                return False
            try:
                stdin.write(line.encode("utf-8"))
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True

    def request(self, op: str, answer: str, timeout_s: float = REQUEST_TIMEOUT_S, **params: Any) -> dict[str, Any]:
        with self._cond:
            since = len(self._events)
        if not self.send(op, **params):
            raise DockerError(f"the {self.name} sidecar no longer takes commands")
        event = self._wait(lambda e: e.get("ev") in (answer, "error"), since, timeout_s)
        if event.get("ev") == "error":
            raise DockerError(f"the {self.name} sidecar refused {op}: {event.get('error')}")
        return event

    def threads(self) -> list[ThreadInfo]:
        event = self.request("threads", "threads")
        return [
            ThreadInfo(
                tid=int(t["tid"]),
                name=str(t.get("name") or ""),
                start_module=t.get("start_module"),
                created_ns=t.get("created_ns"),
            )
            for t in event.get("threads") or []
        ]

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> dict[str, Any] | None:
        """Ask the sidecar to stop and wait for its ``done`` event and exit. Idempotent."""
        if not self._stopped:
            self._stopped = True
            self.send("stop")
            with self._stdin_lock:
                if self.popen.stdin is not None:
                    try:
                        self.popen.stdin.close()
                    except OSError:
                        pass
        try:
            self.popen.wait(timeout_s)
        except subprocess.TimeoutExpired:
            log.warning("the %s sidecar did not stop in %.0f s; killing it", self.name, timeout_s)
            self.popen.kill()
            self.popen.wait(10)
        self._reader.join(5)
        return self.done


def _read_marker(stdout: IO[bytes], timeout_s: float) -> tuple[int | None, list[str]]:
    """Read lines until the marker line; returns the PID and whatever Docker printed before it."""
    found: dict[str, Any] = {"pid": None, "before": []}

    def read() -> None:
        while True:
            raw = stdout.readline()
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.startswith(MARKER + " "):
                try:
                    found["pid"] = int(text.split()[1])
                except (IndexError, ValueError):
                    found["before"].append(text)
                return
            found["before"].append(text)
            if len(found["before"]) > 50:
                return

    reader = threading.Thread(target=read, name="bbench-docker-marker", daemon=True)
    reader.start()
    reader.join(timeout_s)
    return found["pid"], list(found["before"])


class DockerRuntime:
    """One run's containers: see the module docstring for the pod and the call order."""

    def __init__(
        self,
        run_id: str,
        placement: Placement,
        *,
        cli: DockerCli | None = None,
        catalog: ImageCatalog | None = None,
        engine: EngineInfo | None = None,
        probe: EngineProbe | None = None,
        root: Path | None = None,
        report: Callable[[str], None] | None = None,
    ) -> None:
        self.names = RunNames.for_run(run_id)
        self.placement = placement
        self.cli = cli or DockerCli()
        self.root = root or project_root()
        self.catalog = catalog or load_images(self.root)
        self._engine = engine
        self._probe = probe
        self.report = report or log.info
        self.images: dict[str, ImageRef] = {}
        self.launched: ContainerProcess | None = None
        self.server: ContainerServer | None = None
        self.threads: ThreadsSampler | None = None
        self.process: RemoteSampler | None = None
        self.tick: RemoteSampler | None = None
        self.machine: RemoteSampler | None = None
        self.clock_offset_ns: int | None = None
        self._server_exe: str | None = None
        self._series_dir: Path | None = None
        self._sidecars: list[Sidecar] = []
        self._inspection: dict[str, Any] | None = None
        self._created_volume = False

    # ---- engine -----------------------------------------------------------------------------------------------------

    @property
    def engine(self) -> EngineInfo:
        if self._engine is None:
            self._engine = engine_info(self.cli)
        return self._engine

    @property
    def probe(self) -> EngineProbe:
        if self._probe is None:
            self._probe = probe_engine(self.cli, self.catalog, root=self.root)
        return self._probe

    @property
    def shared_clock(self) -> bool:
        return shared_clock(self.probe)

    @property
    def host_machine_sampling(self) -> bool:
        """True when the host's machine sampler sees the containers (same kernel): use :meth:`machine_roots`.

        Otherwise the ``machine`` sidecar writes ``machine.csv.gz`` from inside the VM and :attr:`machine` is its
        sampler.
        """
        return self.shared_clock

    @property
    def kind(self) -> str:
        """``vcpu`` when the containers run on a VM's virtual CPUs, else ``physical``."""
        return "vcpu" if engine_virtualization(self.engine, self.probe) else "physical"

    # ---- images -----------------------------------------------------------------------------------------------------

    def image(self, key: str, version: str | None = None) -> ImageRef:
        cached = self.images.get(key)
        if cached is not None:
            return cached
        ref = ensure_image(self.cli, plan_build(self.catalog, key, version=version, root=self.root), self.report)
        self.images[key] = ref
        return ref

    def prepare_images(self, key: str | None, arm: Arm) -> ImageRef:
        """Build or pull everything the run needs: the server image, the sampler and the runtime base."""
        if key is None:
            raise DockerError(f"{arm.server} gives no image for the docker runtime")
        spec = self.catalog.image(key)
        if spec.role != "server":
            raise DockerError(f"image {key!r} is not a server image")
        if arm.artifact is not None:
            raise DockerError("the docker runtime runs published releases only; a local artifact cannot be used")
        if spec.per_version is not None and arm.python:
            platform = spec.per_version.platform
            if not platform.endswith("cp" + arm.python.replace(".", "")):
                raise DockerError(f"the {key} image runs {platform}; the arm asks for Python {arm.python}")
        server = self.image(key, arm.version)
        self.image(SAMPLER_IMAGE)
        self.image(RUNTIME_IMAGE)
        return server

    # ---- server -----------------------------------------------------------------------------------------------------

    def start_server(
        self,
        spec: LaunchSpec,
        layout: Layout,
        *,
        arm: Arm,
        server_exe: str | None = None,
        executables: Iterable[str] = (),
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> ContainerProcess:
        """Create the pod and the volume, copy the server in and start it. Raises :class:`DockerError`."""
        image = self.prepare_images(spec.image, arm)
        runtime_image = self.image(RUNTIME_IMAGE).ref
        argv, workdir, env = map_command(spec, layout)
        self._server_exe = server_exe

        self.cli.run(*volume_args(self.names), timeout=60)
        self._created_volume = True
        marked = list(executables)
        if argv and argv[0].startswith("./"):
            marked.append(argv[0])
        self._copy_in(image.ref, layout.server_dir, marked)
        self.cli.run(*pod_args(self.names, runtime_image, self.placement.harness_cpus), timeout=120)

        popen = self.cli.popen(
            *server_args(
                self.names,
                image.ref,
                argv=argv,
                workdir=workdir,
                cpus=self.placement.server_cpus,
                priority=self.placement.priority,
                env=env,
                env_remove=spec.env_remove,
            ),
            stderr=subprocess.STDOUT,
        )
        started = Stamp.now()
        assert popen.stdout is not None
        pid, before = _read_marker(popen.stdout, start_timeout_s)
        if pid is None:
            popen.kill()
            popen.wait(10)
            detail = " | ".join(before[-5:]) or "no output"
            raise DockerError(f"the server container did not start: {detail}")
        for line in before:
            log.info("docker: %s", line)
        launched = ContainerProcess(
            popen=popen,
            cli=self.cli,
            name=self.names.server,
            cpus=tuple(sorted(self.placement.server_cpus)),
            priority=self.placement.priority,
            main_pid=pid,
            started=started,
        )
        launched.verified = launched.verify()
        self.launched = launched
        return launched

    def _copy_in(self, image: str, server_dir: Path, executables: Sequence[str]) -> None:
        proc = self.cli.popen(*copy_in_args(self.names, image), stdout=subprocess.DEVNULL)
        assert proc.stdin is not None
        try:
            count = write_tree_tar(server_dir, proc.stdin, executables=executables)
        except BrokenPipeError:
            count = -1
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            code = proc.wait(600)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise DockerError("copying the server into its volume took longer than 600 s") from None
        stderr = proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else ""
        if code != 0 or count < 0:
            raise DockerError(f"copying the server into its volume failed ({code}): {stderr[-1000:]}")
        log.info("copied %d files into %s", count, self.names.volume)

    # ---- samplers ---------------------------------------------------------------------------------------------------

    def start_samplers(self, series_dir: Path, *, hello_timeout_s: float = HELLO_TIMEOUT_S) -> ContainerServer:
        """Start the sampler sidecars; returns the server once the sidecar has found it."""
        launched = self._require_launched()
        series_dir = Path(series_dir)
        series_dir.mkdir(parents=True, exist_ok=True)
        self._series_dir = series_dir
        sampler = self.image(SAMPLER_IMAGE).ref
        harness = self.placement.harness_cpus
        if self._server_exe is None:
            target: dict[str, Any] = {"server_pid": launched.main_pid}
        else:
            target = {"server_exe": self._server_exe, "launcher_pid": launched.main_pid}
        threads = self._start_sidecar(
            "threads", sidecar_args(self.names, sampler, series_dir, harness, wait_s=hello_timeout_s, **target)
        )
        hello = threads.wait_hello(hello_timeout_s + 10)
        self.server = ContainerServer(self, int(hello["pid"]))
        self.threads = ThreadsSampler("threads", threads)
        self.process = RemoteSampler("process", threads)
        if not self.host_machine_sampling:
            machine = self._start_sidecar(
                "machine",
                machine_sidecar_args(
                    self.names, sampler, series_dir, harness, self.placement.server_cpus, launched.main_pid
                ),
            )
            machine.wait_hello(REQUEST_TIMEOUT_S * 4)
            self.machine = RemoteSampler("machine", machine)
        return self.server

    def _start_sidecar(self, name: str, args: Sequence[str]) -> Sidecar:
        sidecar = Sidecar(self.cli.popen(*args), name)
        self._sidecars.append(sidecar)
        return sidecar

    def start_tick(self, tid: int) -> RemoteSampler:
        """Start the sidecar's 10 Hz tick-thread sampler on ``tid`` (a PID-namespace tid from :attr:`threads`)."""
        if self.threads is None:
            raise DockerError("start_samplers() first")
        if self.tick is None:
            self.threads.sidecar.request("tick", "tick", tid=int(tid))
            self.tick = RemoteSampler("tick", self.threads.sidecar)
        return self.tick

    def samplers(self) -> list[RemoteSampler]:
        return [s for s in (self.tick, self.threads, self.process, self.machine) if s is not None]

    def machine_roots(self) -> list[tuple[str, int]]:
        """``(group, host pid)`` roots for the host's machine sampler; empty unless :attr:`host_machine_sampling`.

        The server container's main process heads the ``server`` group. The pod, the sidecar and the engine's
        per-container shims are the harness's own work. Add each root once: re-adding one restarts its count.
        """
        if not self.host_machine_sampling:
            return []
        roots: list[tuple[str, int]] = []
        for group, name in (
            ("server", self.names.server),
            ("harness", self.names.pod),
            ("harness", self.names.threads),
        ):
            roots += self._container_roots(group, name)
        return roots

    def _container_roots(self, group: str, name: str) -> list[tuple[str, int]]:
        pid = self._host_pid(name)
        if pid is None:
            return []
        shim = _parent_pid(pid)
        return [(group, pid)] + ([("harness", shim)] if shim is not None else [])

    def _host_pid(self, name: str) -> int | None:
        pid = _state(_inspect(self.cli, name)).get("Pid")
        return int(pid) if isinstance(pid, int) and pid > 0 else None

    # ---- bots -------------------------------------------------------------------------------------------------------

    def bot_command(self, argv: Sequence[str]) -> list[str]:
        """The full command line that runs a bot command (binary path, then its arguments) in the bots container."""
        if not argv:
            raise DockerError("empty bot command")
        binary = Path(argv[0])
        try:
            with binary.open("rb") as fh:
                magic = fh.read(4)
        except OSError:
            raise DockerError(f"bot binary {binary} not found; run 'bbench bots build --target {BOT_TARGET}'") from None
        if magic != ELF_MAGIC:
            raise DockerError(f"{binary.name} is not a Linux binary; the bots container needs the {BOT_TARGET} build")
        prefix = bot_args_prefix(self.names, self.image(RUNTIME_IMAGE).ref, binary, self.placement.bot_cpus)
        return self.cli.command(*prefix, *argv[1:])

    def bot_roots(self, timeout_s: float = 30.0) -> list[tuple[str, int]]:
        """Roots of the bots container for the host's machine sampler once it runs; empty on VM engines.

        On a VM engine the ``machine`` sidecar finds the bot by its executable path instead.
        """
        if not self.host_machine_sampling:
            return []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            roots = self._container_roots("bot", self.names.bots)
            if roots:
                return roots
            time.sleep(0.2)
        log.warning("the bots container did not report a PID in %.0f s", timeout_s)
        return []

    # ---- checks and facts -------------------------------------------------------------------------------------------

    def inspection(self) -> dict[str, Any]:
        """Threads, affinities, nice values and mapped files of the server, read once by a throwaway container."""
        if self._inspection is None:
            if self.server is None:
                return {}
            args = inspect_args(self.names, self.image(SAMPLER_IMAGE).ref, self.placement.harness_cpus, self.server.pid)
            try:
                self._inspection = _last_json(self.cli.text(*args, timeout=60))
            except DockerError as exc:
                log.warning("server inspection failed: %s", exc)
                self._inspection = {}  # not retried: later checks fall in measured windows
        return self._inspection

    def verify_placement(self, when: str = "ready") -> PlacementCheck:
        """The ``placement_match`` check: the container's cpuset, and at the first call every thread and the nice value.

        The kernel holds every thread of the container inside its cpuset, so later checks read the cpuset only and
        start no container during a measured window.
        """
        from bedrock_bench.host import PlacementCheck

        launched = self._require_launched()
        expected = tuple(sorted(self.placement.server_cpus))
        cpuset = launched.cpuset() if launched.poll() is None else None
        if cpuset is None:
            return PlacementCheck(False, expected, None, {}, {}, None, ("process exited",))
        return placement_check(expected, cpuset, self.inspection(), self.placement.priority)

    def facts(self) -> dict[str, Any]:
        """``server.facts.container`` of run.json."""
        engine = self._engine
        probe = self._probe
        image = next((ref for key, ref in self.images.items() if self.catalog.image(key).role == "server"), None)
        facts: dict[str, Any] = {
            "runtime": "docker",
            "engine": engine.version if engine else None,
            "engine_os": ("Docker Desktop" if engine.desktop else engine.os) if engine else None,
            "kernel": engine.kernel if engine else (probe.kernel if probe else None),
            "cgroup": engine.cgroup_version if engine else None,
            "seccomp": _seccomp(engine.security) if engine else None,
            "virtualization": engine_virtualization(engine, probe) if probe else None,
            "image": image.ref if image else None,
            "image_id": image.id if image else None,
            "namespaces": "pod: shared pid and network",
            "clock": None,
            "clock_offset_ns": self.clock_offset_ns,
        }
        if probe is not None:
            facts["clock"] = "shared" if self.shared_clock else "fitted"
        if self.launched is not None and self.launched.poll() is not None:
            facts["oom_killed"] = self.launched.oom_killed()
        return facts

    def artifacts(self) -> list[dict[str, Any]]:
        """run.json ``artifacts`` entries for the images the run used."""
        return [ref.artifact() for _, ref in sorted(self.images.items())]

    # ---- teardown ---------------------------------------------------------------------------------------------------

    def stop_samplers(self) -> int:
        """Stop the sidecars, then map their stamps onto the harness clock. Returns the offset applied (ns)."""
        fit = ClockFit()
        for sidecar in self._sidecars:
            sidecar.stop()
            fit.extend(sidecar.clock)
        if not self._sidecars:
            return 0
        offset = fit.offset_ns(self.shared_clock)
        self.clock_offset_ns = offset
        if offset and self._series_dir is not None:
            written = THREADS_SIDECAR_SERIES + (MACHINE_SIDECAR_SERIES if self.machine is not None else ())
            for name in written:
                shift_series(self._series_dir / name, offset)
        self._chown_series()
        return offset

    def _chown_series(self) -> None:
        """Give files the sidecars wrote as root back to the harness user (POSIX hosts)."""
        if self._series_dir is None or not hasattr(os, "getuid") or os.getuid() == 0:
            return
        owner = f"{os.getuid()}:{os.getgid()}"
        self.cli.run(
            "run",
            "--rm",
            "--log-driver",
            "none",
            "--mount",
            mount("bind", self._series_dir, SERIES),
            "--entrypoint",
            "chown",
            self.image(RUNTIME_IMAGE).ref,
            "-R",
            owner,
            SERIES,
            check=False,
            timeout=60,
        )

    def copy_back(self, layout: Layout, paths: Sequence[str]) -> list[str]:
        """Copy ``paths`` (relative to the server dir) from the volume over the host copy. Returns what was copied."""
        if not self._created_volume or not paths:
            return []
        image = next((ref.ref for key, ref in self.images.items() if self.catalog.image(key).role == "server"), None)
        image = image or self.image(RUNTIME_IMAGE).ref
        proc = self.cli.popen(*copy_back_args(self.names, image, paths), stdin=subprocess.DEVNULL)
        assert proc.stdout is not None
        try:
            written = extract_tar_stream(proc.stdout, layout.server_dir)
        except tarfile.TarError as exc:
            proc.kill()
            raise DockerError(f"copying files back from the volume failed: {exc}") from exc
        finally:
            proc.stdout.close()
        code = proc.wait(120)
        if code != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else ""
            raise DockerError(f"copying files back from the volume failed ({code}): {stderr[-500:]}")
        return written

    def close(self) -> None:
        """Remove every container and the volume of the run. Safe to call more than once."""
        for sidecar in self._sidecars:
            if sidecar.running:
                sidecar.popen.kill()
        self.cli.run("rm", "--force", "--volumes", *self.names.containers(), check=False, timeout=120)
        if self._created_volume:
            self.cli.run("volume", "rm", "--force", self.names.volume, check=False, timeout=60)
        if self.launched is not None:
            self.launched.close()

    def _require_launched(self) -> ContainerProcess:
        if self.launched is None:
            raise DockerError("start_server() first")
        return self.launched


def _parent_pid(pid: int) -> int | None:
    try:
        import psutil

        parent = psutil.Process(pid).ppid()
    except Exception:
        return None
    return parent if parent > 1 else None


def _seccomp(security: Sequence[str]) -> str | None:
    for option in security:
        if "name=seccomp" in option:
            profile = re.search(r"profile=([^,]+)", option)
            return profile.group(1) if profile else "enabled"
    return None


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise DockerError(f"no JSON object in the helper's output: {text[-300:]!r}")


def placement_check(
    expected: Sequence[int], cpuset: Sequence[int], details: Mapping[str, Any], priority: str | None
) -> PlacementCheck:
    """A ``host.PlacementCheck`` from the container's cpuset and an inspection (``inspect`` helper output)."""
    from bedrock_bench.host import PlacementCheck, format_cpus

    expected_t = tuple(sorted(set(expected)))
    cpuset_t = tuple(sorted(set(cpuset)))
    allowed = set(expected_t)
    problems: list[str] = []
    if cpuset_t != expected_t:
        problems.append(f"container cpuset {format_cpus(cpuset_t)} != planned {format_cpus(expected_t)}")
    outside: dict[int, tuple[int, ...]] = {}
    narrowed: dict[int, tuple[int, ...]] = {}
    for thread in details.get("threads") or ():
        cpus = tuple(sorted(int(c) for c in thread.get("cpus") or ()))
        if not cpus:
            continue
        if not set(cpus) <= allowed:
            outside[int(thread["tid"])] = cpus
        elif set(cpus) != allowed:
            narrowed[int(thread["tid"])] = cpus
    if outside:
        problems.append(f"{len(outside)} threads may run outside the planned CPUs")
    observed = None
    if details.get("nice") is not None:
        nice = int(details["nice"])
        observed = next((label for label, value in NICE.items() if value == nice), f"nice_{nice}")
        if priority is not None and observed != priority:
            problems.append(f"priority {observed} != planned {priority}")
    return PlacementCheck(not problems, expected_t, cpuset_t, outside, narrowed, observed, tuple(problems))


# --------------------------------------------------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------------------------------------------------


def chunk_total(run_dir: Path) -> float | None:
    """Summed in-view chunks of all bots at ``settled``: ``chunks_per_s_load`` times the load window."""
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    marks = run.get("marks") or {}
    rate = ((metrics.get("metrics") or {}).get("chunks_per_s_load") or {}).get("value")
    start, end = marks.get("bots_start"), marks.get("settled")
    if not isinstance(rate, (int, float)) or start is None or end is None or end <= start:
        return None
    return float(rate) * (end - start)


def compare_chunk_totals(native: Path, docker: Path, *, tolerance: float = 0.2) -> Check:
    """Warn when a Docker smoke delivered more than ``tolerance`` fewer or more chunks than a native one."""
    from bedrock_bench.host import Check

    a, b = chunk_total(native), chunk_total(docker)
    if a is None or b is None or a <= 0:
        return Check("docker_chunks", "info", "chunk totals of the smoke runs are not available")
    diff = (b - a) / a
    detail = {"native_run": native.name, "docker_run": docker.name, "native": round(a), "docker": round(b)}
    summary = f"Docker smoke delivered {b:.0f} chunks, native {a:.0f} ({diff:+.0%})"
    if abs(diff) > tolerance:
        return Check("docker_chunks", "warn", summary + "; the container network path changes the workload", detail)
    return Check("docker_chunks", "ok", summary, detail)


def latest_smokes(runs_dir: Path) -> tuple[Path, Path] | None:
    """The newest native and Docker smoke runs of the same arm under ``runs_dir``, or None."""
    found: dict[tuple[str, bool], tuple[str, Path]] = {}
    for run_json in runs_dir.glob("**/run.json") if runs_dir.is_dir() else ():
        try:
            run = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (run.get("scenario") or {}).get("name") != "smoke" or run.get("status") != "ok":
            continue
        arm = (run.get("arm") or {}).get("id")
        docker = (run.get("harness") or {}).get("backend") == "docker-linux"
        run_id = str(run.get("run_id") or run_json.parent.name)
        key = (str(arm), docker)
        if arm and (key not in found or found[key][0] < run_id):
            found[key] = (run_id, run_json.parent)
    pairs = [(found[(arm, False)], found[(arm, True)]) for arm, docker in found if docker and (arm, False) in found]
    if not pairs:
        return None
    native, docker = max(pairs, key=lambda pair: pair[1][0])
    return native[1], docker[1]


def netns_check(cli: DockerCli, image: str, port: int) -> Check:
    """Two containers, the second in the first's network namespace, exchange a UDP datagram over 127.0.0.1."""
    from bedrock_bench.host import Check

    name = f"{NAME_PREFIX}netns-{os.getpid()}-{int(time.time())}"
    labels = ["--label", f"{LABEL}=doctor"]
    helper = ["python", "-m", "bedrock_bench.platform.docker"]
    try:
        echo = ["run", "--detach", "--rm", "--name", name, *labels, "--log-driver", "none", image, *helper]
        cli.run(*echo, "udp-echo", "--port", str(port), "--timeout", "60", timeout=120)
        ping = ["run", "--rm", *labels, "--log-driver", "none", "--network", f"container:{name}", image, *helper]
        result = cli.run(*ping, "udp-ping", "--port", str(port), "--timeout", "20", check=False, timeout=120)
    except DockerError as exc:
        return Check("docker_netns", "fail", f"network namespace check could not run: {exc}")
    finally:
        cli.run("rm", "--force", name, check=False, timeout=60)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()[-300:]
        return Check("docker_netns", "fail", f"a container in another's network namespace got no UDP reply: {tail}")
    return Check("docker_netns", "ok", "bots can reach the server over 127.0.0.1 in its network namespace")


def doctor_checks(
    machine: Machine,
    *,
    cli: DockerCli | None = None,
    runs_dir: Path = Path("runs"),
    root: Path | None = None,
) -> list[Check]:
    """Docker checks for ``bbench doctor`` (called once ``docker version`` has answered)."""
    from bedrock_bench.host import Check, PlacementError, format_cpus, placement_checks, plan_placement

    cli = cli or DockerCli()
    checks: list[Check] = []
    try:
        engine = engine_info(cli)
    except DockerError as exc:
        return [Check("docker_engine", "fail", str(exc))]
    if engine.os_type != "linux":
        return [Check("docker_engine", "fail", f"the engine runs {engine.os_type or 'unknown'} containers, not Linux")]
    if not engine.cpuset:
        checks.append(Check("docker_engine", "fail", f"{engine.describe()}; it cannot pin containers to CPUs"))
    else:
        checks.append(Check("docker_engine", "info", engine.describe()))

    running = cli.run("ps", "--format", '{{.Names}}\t{{.Label "' + LABEL + '"}}', check=False, timeout=30)
    ours, others = [], []
    for line in running.stdout.decode("utf-8", errors="replace").splitlines():
        name, _, label = line.partition("\t")
        if name:
            (ours if label else others).append(name)
    if ours:
        checks.append(
            Check(
                "docker_containers",
                "fail",
                f"containers of earlier runs are still running: {', '.join(sorted(ours))};"
                f" remove them with docker rm -f",
            )
        )
    elif others:
        checks.append(Check("docker_containers", "warn", f"other containers are running: {', '.join(sorted(others))}"))
    else:
        checks.append(Check("docker_containers", "ok", "no other containers are running"))

    def report(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    try:
        catalog = load_images(root)
        sampler = ensure_image(cli, plan_build(catalog, SAMPLER_IMAGE, root=root), report)
        ensure_image(cli, plan_build(catalog, RUNTIME_IMAGE, root=root), report)
        probe = probe_engine(cli, catalog, root=root)
    except (DockerError, ConfigError) as exc:
        checks.append(Check("docker_images", "fail", f"sampler image or engine probe failed: {exc}"))
        return checks
    checks.append(Check("docker_images", "ok", f"sampler image {sampler.ref}"))

    topology = engine_topology(probe, engine)
    if topology.virtualization:
        checks.append(
            Check(
                "docker_topology",
                "warn",
                f"containers run on {len(probe.cpus)} virtual CPUs ({topology.virtualization}): placement is vcpu;"
                " publish numbers from bare-metal Linux",
            )
        )
    else:
        checks.append(Check("docker_topology", "info", topology.describe(), topology.to_json()))
    try:
        placement = plan_placement(machine, topology)
    except PlacementError as exc:
        checks.append(Check("docker_placement", "fail", f"on the engine's CPUs: {exc}"))
    else:
        checks.append(
            Check(
                "docker_placement",
                "ok",
                f"server {format_cpus(placement.server_cpus)}, bots {format_cpus(placement.bot_cpus)},"
                f" harness {format_cpus(placement.harness_cpus)} on the engine's CPUs ({placement.kind})",
            )
        )
        for check in placement_checks(placement, topology):
            checks.append(Check(f"docker_{check.id}", check.level, check.summary, check.detail))

    checks.append(netns_check(cli, sampler.ref, machine.port))
    if shared_clock(probe):
        checks.append(Check("docker_clock", "ok", "containers share the harness's monotonic clock"))
    else:
        checks.append(
            Check("docker_clock", "info", "sidecar samples are mapped onto the harness clock from heartbeat events")
        )
    smokes = latest_smokes(runs_dir)
    if smokes is None:
        checks.append(
            Check("docker_chunks", "info", "no native and Docker smoke of the same server yet to compare chunk totals")
        )
    else:
        checks.append(compare_chunk_totals(*smokes))
    return checks


# --------------------------------------------------------------------------------------------------------------------
# Helpers that run inside the containers (python -m bedrock_bench.platform.docker ...)
# --------------------------------------------------------------------------------------------------------------------


_EMIT_LOCK = threading.Lock()


def _emit(event: Mapping[str, Any]) -> None:
    with _EMIT_LOCK:
        sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def probe_info(proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")) -> dict[str, Any]:
    """What this container sees of the machine: kernel, boot id, virtualization and the CPUs it may use."""
    from bedrock_bench.platform import linux

    boot_id = None
    try:
        boot_id = (proc_root / "sys" / "kernel" / "random" / "boot_id").read_text(encoding="ascii").strip()
    except OSError:
        pass
    release = (proc_root / "sys" / "kernel" / "osrelease").read_text(encoding="ascii").strip()
    return {
        "kernel": release,
        "boot_id": boot_id,
        "virtualization": linux.virtualization(proc_root, sys_root),
        "cpu_model": linux.cpu_model(proc_root),
        "cpus": [
            {"cpu": r.cpu, "core": r.core, "package": r.package, "efficiency": r.efficiency}
            for r in linux.cpu_records(sys_root)
        ],
        "clk_tck": linux.clk_tck(),
        "schedstat": linux.schedstat_available("self", proc_root),
    }


def parse_stat_nice(text: str) -> int:
    """The nice value (field 19) of a ``/proc/<pid>/stat`` line."""
    return int(text[text.rindex(")") + 1 :].split()[16])


def inspect_info(pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Threads with their CPU affinity, the nice value and the mapped files of one process."""
    from bedrock_bench.platform import linux

    base = proc_root / str(pid)
    threads = []
    for entry in sorted((base / "task").iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        if not entry.name.isdigit():
            continue
        try:
            status = linux.parse_status((entry / "status").read_text(encoding="utf-8", errors="replace"))
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        threads.append(
            {
                "tid": int(entry.name),
                "name": comm,
                "cpus": list(linux.parse_cpu_list(status.get("Cpus_allowed_list", ""))),
            }
        )
    stat_text = (base / "stat").read_text(encoding="utf-8", errors="replace")
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        exe = None
    return {
        "pid": pid,
        "exe": exe,
        "nice": parse_stat_nice(stat_text),
        "modules": linux.loaded_modules(pid, proc_root),
        "threads": threads,
    }


def _parse_roots(values: Sequence[str], what: str) -> list[tuple[str, str]]:
    roots = []
    for value in values:
        group, sep, target = value.partition("=")
        if not sep or group not in ("server", "bot", "harness") or not target:
            raise SystemExit(f"{what} takes GROUP=VALUE with GROUP server, bot or harness, got {value!r}")
        roots.append((group, target))
    return roots


def _find_roots(pending: list[tuple[str, str, str]], own_pid: int) -> list[tuple[str, int, tuple[str, str, str]]]:
    """Processes matching pending ``(kind, group, value)`` specs: kind ``exe`` matches the path, ``cmd`` a substring."""
    import psutil

    found = []
    for proc in psutil.process_iter(["exe", "cmdline"]):
        if proc.pid == own_pid:
            continue
        exe = proc.info.get("exe") or ""
        cmdline = " ".join(proc.info.get("cmdline") or ())
        for spec in pending:
            kind, group, value = spec
            if (kind == "exe" and exe == value) or (kind == "cmd" and value in cmdline):
                found.append((group, proc.pid, spec))
    return found


def machine_main(args: argparse.Namespace) -> int:
    """The ``machine`` sidecar: ``MachineSampler`` with roots given by PID, executable path or command line."""
    from bedrock_bench.sampling.machine import MachineSampler
    from bedrock_bench.sampling.series import FOREIGN_TOP_FILE, MACHINE_FILE

    offset = args.offset_ns

    def clock() -> int:
        return mono_ns() + offset

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sampler = MachineSampler(
        out / MACHINE_FILE,
        out / FOREIGN_TOP_FILE,
        parse_cpu_list(args.server_cpus) if args.server_cpus else (),
        interval_s=args.interval,
        clock=clock,
    )

    def add_root(group: str, pid: int) -> bool:
        import psutil

        try:
            sampler.add_root(group, pid)
        except psutil.Error as exc:
            _emit({"ev": "error", "error": f"cannot watch {group} process {pid}: {exc!r}"})
            return False
        return True

    for group, pid in _parse_roots(args.root, "--root"):
        add_root(group, int(pid))
    pending = [("exe", g, v) for g, v in _parse_roots(args.root_exe, "--root-exe")]
    pending += [("cmd", g, v) for g, v in _parse_roots(args.root_cmd, "--root-cmd")]
    stamp = Stamp.now()
    _emit({"ev": "hello", "pid": os.getpid(), "mono_ns": stamp.mono_ns + offset, "epoch_ms": stamp.epoch_ms})
    sampler.start()
    done = threading.Event()

    def background() -> None:
        next_clock = time.monotonic()
        while not done.wait(0.5):
            if pending:
                for group, pid, spec in _find_roots(pending, os.getpid()):
                    if add_root(group, pid) and spec in pending:
                        pending.remove(spec)
            if time.monotonic() >= next_clock:
                _emit({"ev": "clock", "mono_ns": clock()})
                next_clock += args.clock_interval

    threading.Thread(target=background, name="bbench-machine-sidecar", daemon=True).start()

    def on_signal(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    try:
        for line in sys.stdin:
            try:
                command = json.loads(line) if line.strip() else {}
            except ValueError:
                command = {"op": line.strip()}
            if command.get("op") == "stop":
                break
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        sampler.stop()
        error = repr(sampler.error) if sampler.error else None
        _emit(
            {
                "ev": "done",
                "samplers": {"machine": {"samples": sampler.samples, "ended": sampler.ended, "error": error}},
            }
        )
    return 0


def udp_echo(port: int, timeout_s: float) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.settimeout(timeout_s)
        try:
            data, peer = sock.recvfrom(64)
        except TimeoutError:
            return 1
        sock.sendto(b"pong:" + data, peer)
    return 0


def udp_ping(port: int, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.5)
        while time.monotonic() < deadline:
            try:
                sock.sendto(b"ping", ("127.0.0.1", port))
                data, _ = sock.recvfrom(64)
            except OSError:
                time.sleep(0.2)  # the echo side may not be bound yet
                continue
            if data == b"pong:ping":
                return 0
    print(f"no UDP reply from 127.0.0.1:{port} in {timeout_s:g} s", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bedrock_bench.platform.docker",
        description="Helpers the Docker runtime runs inside its containers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="print the kernel, boot id, virtualization and CPUs this container sees")
    inspect_parser = sub.add_parser("inspect", help="print the threads, affinities and mapped files of a process")
    inspect_parser.add_argument("--pid", type=int, required=True)
    machine = sub.add_parser("machine", help="machine sampler sidecar (JSON lines: hello, clock, done; stdin: stop)")
    machine.add_argument("--out", required=True)
    machine.add_argument("--server-cpus", default="")
    machine.add_argument("--root", action="append", default=[], metavar="GROUP=PID")
    machine.add_argument("--root-exe", action="append", default=[], metavar="GROUP=PATH")
    machine.add_argument("--root-cmd", action="append", default=[], metavar="GROUP=TEXT")
    machine.add_argument("--offset-ns", type=int, default=0)
    machine.add_argument("--interval", type=float, default=1.0)
    machine.add_argument("--clock-interval", type=float, default=5.0)
    for name in ("udp-echo", "udp-ping"):
        udp = sub.add_parser(name, help="network namespace check")
        udp.add_argument("--port", type=int, required=True)
        udp.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.command == "probe":
        print(json.dumps(probe_info(), separators=(",", ":")))
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect_info(args.pid), separators=(",", ":")))
        return 0
    if args.command == "machine":
        return machine_main(args)
    if args.command == "udp-echo":
        return udp_echo(args.port, args.timeout)
    return udp_ping(args.port, args.timeout)


__all__ = [
    "BOT_TARGET",
    "ENV_DOCKER",
    "ClockFit",
    "ContainerProcess",
    "ContainerServer",
    "DockerCli",
    "DockerError",
    "DockerRuntime",
    "EngineInfo",
    "EngineProbe",
    "ImageBuild",
    "ImageCatalog",
    "ImageRef",
    "ImageSpec",
    "RemoteSampler",
    "RunNames",
    "Sidecar",
    "available",
    "compare_chunk_totals",
    "container_path",
    "context_tar",
    "copy_back_paths",
    "doctor_checks",
    "engine_info",
    "engine_topology",
    "ensure_image",
    "extract_tar_stream",
    "load_images",
    "machine_facts",
    "map_command",
    "parse_engine_info",
    "parse_images",
    "parse_probe",
    "placement_check",
    "plan_build",
    "probe_engine",
    "server_args",
    "server_exe",
    "shared_clock",
    "shift_series",
    "write_tree_tar",
]


if __name__ == "__main__":
    raise SystemExit(main())
