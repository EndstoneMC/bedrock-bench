# SPDX-License-Identifier: Apache-2.0
"""The Docker runtime: image pins, command lines, tar streams, clocks, doctor helpers and a whole run.

No test needs Docker. The run test drives :class:`DockerRuntime` through a fake ``docker`` command (``FAKE_DOCKER``
below) that keeps containers and volumes as local processes and directories, and runs the real sampler sidecar
natively against the fake server.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from bedrock_bench import adapters, config
from bedrock_bench.model import ConfigFile, LaunchSpec, Layout, Placement, WorldInfo
from bedrock_bench.platform import CpuRecord
from bedrock_bench.platform import docker as d
from bedrock_bench.sampling.series import THREAD_COLUMNS, SeriesWriter, iter_lines, read_threads

ROOT = config.project_root()

NATIVE_INFO = {
    "ServerVersion": "28.3.0",
    "OperatingSystem": "Ubuntu 24.04.3 LTS",
    "OSType": "linux",
    "KernelVersion": "6.8.0-85-generic",
    "NCPU": 16,
    "MemTotal": 67_000_000_000,
    "CgroupVersion": "2",
    "CPUSet": True,
    "Architecture": "x86_64",
    "Name": "some-host",
    "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"],
}
DESKTOP_INFO = {
    **NATIVE_INFO,
    "OperatingSystem": "Docker Desktop",
    "KernelVersion": "6.6.87.2-microsoft-standard-WSL2",
    "NCPU": 8,
}


def _probe(virtualization: str | None = None, boot_id: str = "not-this-host", cpus: int = 8) -> d.EngineProbe:
    records = tuple(CpuRecord(cpu=i, core=i // 2, package=0, efficiency=0) for i in range(cpus))
    return d.EngineProbe(
        kernel="6.8.0-85-generic", boot_id=boot_id, virtualization=virtualization, cpu_model="Test CPU", cpus=records
    )


def _placement(priority: str = "normal") -> Placement:
    return Placement(
        server_cpus=(2, 3), bot_cpus=(4, 5, 6, 7), harness_cpus=(4, 5, 6, 7), priority=priority, kind="physical"
    )


def _layout(server_dir: Path, exe: str = "bedrock_server") -> Layout:
    return Layout(
        server_dir=server_dir,
        overrides=(),
        config_files=(ConfigFile("server.properties", "0" * 64), ConfigFile("version.txt", "0" * 64)),
        world=WorldInfo(fresh=True, generator="vanilla"),
        extra={"exe": exe},
    )


def _docker_machine() -> config.Machine:
    return config.parse_machine({"runtime": {"kind": "docker"}})


# --------------------------------------------------------------------------------------------------------------------
# images.toml and the Dockerfiles
# --------------------------------------------------------------------------------------------------------------------


def _dockerfile_args(path: Path) -> dict[str, str]:
    """``ARG NAME=default`` lines of a Dockerfile."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^ARG\s+([A-Z_][A-Z0-9_]*)=(\S+)\s*$", line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def _built_images(catalog: d.ImageCatalog) -> list[d.ImageSpec]:
    return [spec for spec in catalog.images.values() if spec.dockerfile is not None]


def _some_version(spec: d.ImageSpec) -> str | None:
    if spec.per_version is None:
        return None
    catalog = config.load_artifacts(spec.per_version.artifacts)
    return next(v for v, entry in catalog.versions.items() if spec.per_version.platform in entry.platforms)


class TestCatalog:
    def test_every_base_is_pinned_by_digest(self) -> None:
        catalog = d.load_images()
        assert catalog.bases
        for ref in catalog.bases.values():
            assert re.search(r"@sha256:[0-9a-f]{64}$", ref), ref
            assert ":" in ref.split("@")[0].rsplit("/", 1)[-1], f"{ref} names no tag"

    def test_dockerfile_defaults_equal_the_pins(self) -> None:
        catalog = d.load_images()
        for spec in _built_images(catalog):
            assert spec.dockerfile is not None
            defaults = _dockerfile_args(ROOT / spec.dockerfile)
            for arg, base in spec.bases.items():
                assert defaults.get(arg) == catalog.bases[base], f"{spec.dockerfile}: ARG {arg}"
            for arg, value in spec.args.items():
                assert defaults.get(arg) == value, f"{spec.dockerfile}: ARG {arg}"

    def test_every_from_is_pinned(self) -> None:
        catalog = d.load_images()
        for spec in _built_images(catalog):
            assert spec.dockerfile is not None
            path = ROOT / spec.dockerfile
            defaults = _dockerfile_args(path)
            stages: set[str] = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
                if not match:
                    continue
                image = match.group(1)
                arg = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)\}", image)
                if arg:
                    assert "@sha256:" in defaults.get(arg.group(1), ""), f"{path.name}: {line}"
                else:
                    assert image == "scratch" or image in stages, f"{path.name}: unpinned {line}"
                if match.group(2):
                    stages.add(match.group(2))

    def test_build_contexts_hold_what_the_dockerfiles_read(self) -> None:
        catalog = d.load_images()
        for spec in _built_images(catalog):
            assert spec.dockerfile is not None
            build = d.plan_build(catalog, spec.key, version=_some_version(spec))
            text = (ROOT / spec.dockerfile).read_text(encoding="utf-8")
            sources = re.findall(r"--mount=type=bind,source=([^,\s]+)", text)
            sources += [s for s in re.findall(r"^COPY\s+(?!--from)(\S+)", text, re.MULTILINE)]
            for source in sources:
                if "${" in source:
                    continue  # per-module sources (bots) are checked by the context list itself
                assert any(f == source or f.startswith(source.rstrip("/") + "/") for f in build.files), (
                    f"{spec.dockerfile} reads {source}, which its context does not send"
                )

    def test_endstone_image_runs_the_wheel_python(self) -> None:
        catalog = d.load_images()
        spec = catalog.image("endstone")
        assert spec.per_version is not None
        python = re.match(r"python:(\d+)\.(\d+)", catalog.bases[spec.bases["BASE_IMAGE"]])
        assert python is not None
        assert spec.per_version.platform.endswith(f"cp{python.group(1)}{python.group(2)}")

    def test_go_image_matches_the_bot_toolchain(self) -> None:
        catalog = d.load_images()
        go = re.match(r"golang:(\d+\.\d+\.\d+)", catalog.bases["golang"])
        assert go is not None
        for module in ("core", "loadbot"):
            gomod = (ROOT / "bots" / module / "go.mod").read_text(encoding="utf-8")
            assert f"toolchain go{go.group(1)}" in gomod

    def test_manual_builds_have_an_ignore_file(self) -> None:
        for spec in _built_images(d.load_images()):
            assert spec.dockerfile is not None
            ignore = ROOT / f"{spec.dockerfile}.dockerignore"
            assert ignore.is_file(), ignore
            lines = [x for x in ignore.read_text(encoding="utf-8").splitlines() if x and not x.startswith("#")]
            assert lines[0] == "*"

    def test_server_images_and_adapter_keys(self) -> None:
        catalog = d.load_images()
        for key in ("bds", "endstone", "jvm", "php", "dragonfly"):  # the keys the adapters' LaunchSpec.image names
            assert catalog.image(key).role == "server", key
        assert catalog.image("sampler").role == "harness" and catalog.image("runtime").pull == "debian"
        assert catalog.image("bots").role == "build"


def _catalog(**image: object) -> dict[str, object]:
    return {
        "schema": 1,
        "base": {"debian": "debian:bookworm-slim@sha256:" + "a" * 64},
        "image": {
            "runtime": {"role": "harness", "pull": "debian"},
            "sampler": {"role": "harness", "dockerfile": "docker/x.Dockerfile", "bases": {"BASE_IMAGE": "debian"}},
            **image,
        },
    }


class TestParseImages:
    def test_minimal(self) -> None:
        catalog = d.parse_images(_catalog())
        assert set(catalog.images) == {"runtime", "sampler"}

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({**_catalog(), "base": {"debian": "debian:bookworm-slim"}}, "pinned"),
            (_catalog(srv={"role": "server", "dockerfile": "a", "pull": "debian"}), "exactly one"),
            (_catalog(srv={"role": "server", "dockerfile": "a", "bases": {"BASE_IMAGE": "nope"}}), "unknown bases"),
            (_catalog(srv={"role": "nope", "dockerfile": "a"}), "role"),
            (_catalog(srv={"role": "server", "dockerfile": "/abs/a"}), "relative"),
            (_catalog(srv={"role": "server", "dockerfile": "a", "context": ["x/{version}.txt"]}), "per_version"),
            (
                _catalog(
                    srv={
                        "role": "server",
                        "dockerfile": "a",
                        "per_version": {"artifacts": "x", "platform": "y", "args": {"A": "{user}"}},
                    }
                ),
                "template",
            ),
            ({"schema": 1, "base": {}, "image": {"runtime": {"role": "harness", "pull": "debian"}}}, "pulls unknown"),
            ({**_catalog(), "schema": 2}, "schema"),
            ({**_catalog(), "extra": 1}, "unknown keys"),
        ],
    )
    def test_rejects(self, data: dict[str, object], message: str) -> None:
        with pytest.raises(config.ConfigError, match=message):
            d.parse_images(data)

    def test_needs_the_runtime_images(self) -> None:
        data = _catalog()
        del data["image"]["sampler"]  # type: ignore[attr-defined]
        with pytest.raises(config.ConfigError, match="sampler"):
            d.parse_images(data)


class TestPlanBuild:
    def _root(self, tmp_path: Path) -> Path:
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / "x.Dockerfile").write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
        pkg = tmp_path / "src" / "pkg"
        (pkg / "__pycache__").mkdir(parents=True)
        (pkg / "a.py").write_text("print(1)\n", encoding="utf-8")
        (pkg / "__pycache__" / "a.cpython-313.pyc").write_bytes(b"junk")
        return tmp_path

    def test_tag_follows_the_inputs(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        data = _catalog()
        data["image"]["sampler"]["context"] = ["src/pkg"]  # type: ignore[index]
        catalog = d.parse_images(data)
        first = d.plan_build(catalog, "sampler", root=root)
        assert first.tag.startswith("bedrock-bench/sampler:") and first.files == ("src/pkg/a.py",)
        assert d.plan_build(catalog, "sampler", root=root).tag == first.tag
        (root / "src" / "pkg" / "a.py").write_text("print(2)\n", encoding="utf-8")
        assert d.plan_build(catalog, "sampler", root=root).tag != first.tag

    def test_context_tar_is_deterministic(self, tmp_path: Path) -> None:
        root = self._root(tmp_path)
        data = _catalog()
        data["image"]["sampler"]["context"] = ["src/pkg"]  # type: ignore[index]
        build = d.plan_build(d.parse_images(data), "sampler", root=root)
        blob = d.context_tar(build)
        assert blob == d.context_tar(build)
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            members = tar.getmembers()
        assert [m.name for m in members] == ["Dockerfile", "src/pkg/a.py"]
        assert all(m.mtime == 0 and m.uid == 0 and m.mode == 0o644 for m in members)

    def test_pulled_image(self) -> None:
        catalog = d.load_images()
        build = d.plan_build(catalog, "runtime")
        assert build.pulled and build.tag == catalog.bases["debian"]
        with pytest.raises(d.DockerError):
            d.context_tar(build)

    def test_per_version_args_come_from_the_artifact_pin(self) -> None:
        catalog = d.load_images()
        build = d.plan_build(catalog, "endstone", version="0.11.14")
        pin = config.load_artifacts("endstone").get("0.11.14", "linux-cp313")
        assert build.build_args["ENDSTONE_VERSION"] == "0.11.14"
        assert build.build_args["ENDSTONE_WHEEL_URL"] == pin.url
        assert build.build_args["ENDSTONE_WHEEL_SHA256"] == pin.sha256
        assert "servers/endstone/requirements/0.11.14.txt" in build.files
        assert build.tag.startswith("bedrock-bench/endstone:0.11.14-")
        with pytest.raises(config.ConfigError, match="per version"):
            d.plan_build(catalog, "endstone")


# --------------------------------------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------------------------------------


class TestEngine:
    def test_native_engine(self) -> None:
        engine = d.parse_engine_info(NATIVE_INFO)
        assert not engine.vm and engine.cpuset and engine.cgroup_version == "2" and engine.ncpu == 16
        assert "some-host" not in engine.describe()

    def test_desktop_engine_is_a_vm(self) -> None:
        engine = d.parse_engine_info(DESKTOP_INFO)
        assert engine.desktop and engine.vm
        assert d.engine_virtualization(engine, _probe()) == "wsl2"

    def test_unreachable_engine(self) -> None:
        with pytest.raises(d.DockerError, match="not reachable"):
            d.parse_engine_info({"ServerErrors": ["Cannot connect to the Docker daemon"]})

    def test_probe_parse(self) -> None:
        probe = d.parse_probe(
            {"kernel": "6.8", "boot_id": "b", "virtualization": None, "cpu_model": "X", "cpus": [{"cpu": 0, "core": 0}]}
        )
        assert probe.cpus == (CpuRecord(0, 0, 0, 0),) and probe.boot_id == "b"
        with pytest.raises(d.DockerError):
            d.parse_probe({"cpus": []})

    def test_topology_kind(self) -> None:
        from bedrock_bench import host

        native = d.engine_topology(_probe(), d.parse_engine_info(NATIVE_INFO))
        assert native.kind == "physical" and native.logical == tuple(range(8)) and native.machine_physical == 4
        vm = d.engine_topology(_probe(), d.parse_engine_info(DESKTOP_INFO))
        assert vm.kind == "vcpu"
        assert d.engine_topology(_probe(virtualization="hypervisor")).kind == "vcpu"
        placement = host.plan_placement(_docker_machine(), vm)
        assert placement.kind == "vcpu" and not set(placement.server_cpus) & set(placement.bot_cpus)

    def test_machine_facts_use_the_engine(self) -> None:
        facts = d.machine_facts(
            d.parse_engine_info(DESKTOP_INFO), _probe(), {"os": "Windows 11", "kernel": None, "logical": 28}
        )
        assert facts["os"] == "Windows 11" and facts["kernel"].endswith("WSL2")
        assert facts["logical"] == 8 and facts["virtualized"] is True and facts["ram_gib"] > 0

    def test_shared_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(d, "host_boot_id", lambda: "boot-1")
        monkeypatch.setattr(d.sys, "platform", "linux")
        assert d.shared_clock(_probe(boot_id="boot-1"))
        assert not d.shared_clock(_probe(boot_id="boot-2"))
        monkeypatch.setattr(d.sys, "platform", "win32")
        assert not d.shared_clock(_probe(boot_id="boot-1"))


def test_docker_command_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(d.ENV_DOCKER, json.dumps(["fake-docker", "--flag"]))
    assert d.DockerCli().command("ps") == ["fake-docker", "--flag", "ps"]
    monkeypatch.setenv(d.ENV_DOCKER, "fake-docker")
    with pytest.raises(config.ConfigError):
        d.DockerCli()


# --------------------------------------------------------------------------------------------------------------------
# Command lines
# --------------------------------------------------------------------------------------------------------------------


def _after(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]


class TestCommandLines:
    names = d.RunNames.for_run("20261012T120000Z-bds@1.26.52.3-smoke-r1-ab12")

    def test_names_are_short_and_labelled(self) -> None:
        assert all(re.fullmatch(r"bbench-[0-9a-f]{10}-[a-z]+", n) for n in self.names.containers())
        assert self.names.labels() == ["--label", f"{d.LABEL}={self.names.run_id}"]
        assert d.RunNames.for_run(self.names.run_id) == self.names

    def test_cpu_list(self) -> None:
        assert d.cpu_list([5, 0, 1, 2, 7, 8]) == "0-2,5,7-8"
        assert d.cpu_list([3]) == "3"
        with pytest.raises(d.DockerError):
            d.cpu_list([])

    def test_mount_quotes_commas(self) -> None:
        assert d.mount("bind", "/a/b", "/x", readonly=True) == "type=bind,source=/a/b,target=/x,readonly"
        assert d.mount("bind", r"C:\a,b", "/x") == r'type=bind,"source=C:\a,b",target=/x'

    def test_server_above_normal(self) -> None:
        args = d.server_args(
            self.names,
            "img",
            argv=["./bedrock_server"],
            workdir="/data",
            cpus=[2, 3],
            priority="above_normal",
            env={"LD_LIBRARY_PATH": "."},
            env_remove=["LD_PRELOAD"],
        )
        assert _after(args, "--cpuset-cpus") == ["2-3"]
        assert _after(args, "--pid") == [f"container:{self.names.pod}"]
        assert _after(args, "--network") == [f"container:{self.names.pod}"]
        assert _after(args, "--cap-add") == ["SYS_NICE"]
        assert _after(args, "--env") == ["LD_LIBRARY_PATH=."]
        assert _after(args, "--log-driver") == ["none"]
        assert f"type=volume,source={self.names.volume},target=/data" in _after(args, "--mount")
        assert "--interactive" in args and "--rm" not in args
        tail = args[args.index("img") :]
        assert tail[:3] == ["img", "-c", d.WRAPPER] and d.MARKER in d.WRAPPER
        assert tail[4:] == ["env", "-u", "LD_PRELOAD", "nice", "-n", "-5", "./bedrock_server"]

    def test_server_normal_priority(self) -> None:
        args = d.server_args(
            self.names, "img", argv=["python", "-m", "x"], workdir="/data", cpus=[0], priority="normal"
        )
        assert "--cap-add" not in args and "nice" not in args and "env" not in args
        assert args[-3:] == ["python", "-m", "x"]

    def test_sidecar(self, tmp_path: Path) -> None:
        by_pid = d.sidecar_args(self.names, "s", tmp_path, [4, 5], server_pid=17)
        assert by_pid[by_pid.index("s") + 1 :][:3] == ["python", "-m", "bedrock_bench.sampling"]
        assert _after(by_pid, "--pid") == [f"container:{self.names.pod}", "17"]
        assert "--network" not in by_pid and "--rm" in by_pid
        by_exe = d.sidecar_args(self.names, "s", tmp_path, [4], server_exe="/data/bedrock_server", launcher_pid=9)
        assert _after(by_exe, "--exe") == ["/data/bedrock_server"] and _after(by_exe, "--launcher-pid") == ["9"]
        with pytest.raises(d.DockerError):
            d.sidecar_args(self.names, "s", tmp_path, [4])

    def test_machine_sidecar_roots(self, tmp_path: Path) -> None:
        args = d.machine_sidecar_args(self.names, "s", tmp_path, [4], [2, 3], 12)
        assert _after(args, "--server-cpus") == ["2-3"]
        assert set(_after(args, "--root")) == {"server=12", "harness=1"}
        assert _after(args, "--root-exe") == [f"bot={d.BOT_PATH}"]

    def test_copy_back_rejects_escapes(self) -> None:
        args = d.copy_back_args(self.names, "img", ["server.properties", "crash_reports"])
        assert args[-3:] == ["copy-back", "server.properties", "crash_reports"]
        assert "readonly" in _after(args, "--mount")[0]
        with pytest.raises(config.ConfigError):
            d.copy_back_args(self.names, "img", ["../etc/passwd"])


class TestPaths:
    def test_map_command(self, tmp_path: Path) -> None:
        layout = _layout(tmp_path)
        spec = LaunchSpec(
            argv=("./bedrock_server", str(tmp_path / "plugins" / "x.jar"), "/opt/java/bin/java"),
            cwd=tmp_path,
            env_set={"LD_LIBRARY_PATH": ".", "HOME_DIR": str(tmp_path / "home")},
        )
        argv, workdir, env = d.map_command(spec, layout)
        assert workdir == "/data"
        assert argv == ["./bedrock_server", "/data/plugins/x.jar", "/opt/java/bin/java"]
        assert env == {"LD_LIBRARY_PATH": ".", "HOME_DIR": "/data/home"}
        outside = LaunchSpec(argv=("x",), cwd=tmp_path.parent)
        with pytest.raises(d.DockerError):
            d.map_command(outside, layout)

    def test_server_exe(self, tmp_path: Path) -> None:
        machine = _docker_machine()
        bds = adapters.create(config.arm_from_ref("bds@1.26.52.3"), machine)
        endstone = adapters.create(config.arm_from_ref("endstone@0.11.14"), machine)
        layout = _layout(tmp_path)
        assert d.server_exe(bds, layout) is None
        assert d.server_exe(endstone, layout) == "/data/bedrock_server"

    def test_copy_back_paths(self, tmp_path: Path) -> None:
        endstone = adapters.create(config.arm_from_ref("endstone@0.11.14"), _docker_machine())
        paths = d.copy_back_paths(endstone, _layout(tmp_path))
        assert {"server.properties", "version.txt", "bedrock_server", "crash_reports", ".sentry-native"} <= set(paths)
        assert not any("%" in p for p in paths)


# --------------------------------------------------------------------------------------------------------------------
# Tar streams
# --------------------------------------------------------------------------------------------------------------------


def _tree(root: Path) -> None:
    (root / "worlds").mkdir(parents=True)
    (root / "bedrock_server").write_bytes(d.ELF_MAGIC + b"\x02\x01\x01" + b"\0" * 100)
    (root / "start.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8", newline="\n")
    (root / "server.properties").write_text("a=1\n", encoding="utf-8", newline="\n")
    (root / "data.bin").write_bytes(b"\x00\x01")
    (root / "worlds" / "note.txt").write_text("x", encoding="utf-8")


class TestTar:
    def test_round_trip(self, tmp_path: Path) -> None:
        src, dest = tmp_path / "src", tmp_path / "dest"
        _tree(src)
        if os.name == "posix":
            (src / "bedrock_server").chmod(0o755)
            (src / "start.sh").chmod(0o755)
        buffer = io.BytesIO()
        assert d.write_tree_tar(src, buffer, executables=["./data.bin"]) == 5
        with tarfile.open(fileobj=io.BytesIO(buffer.getvalue())) as tar:
            modes = {m.name: m.mode & 0o777 for m in tar.getmembers()}
            assert all(m.uid == 0 and m.uname == "" for m in tar.getmembers())
        assert modes["bedrock_server"] & 0o111 and modes["start.sh"] & 0o111 and modes["data.bin"] & 0o111
        assert not modes["server.properties"] & 0o111
        buffer.seek(0)
        written = d.extract_tar_stream(buffer, dest)
        assert set(written) == {"bedrock_server", "start.sh", "server.properties", "data.bin", "worlds/note.txt"}
        for name in written:
            assert (dest / name).read_bytes() == (src / name).read_bytes()

    def test_extract_replaces_and_keeps_times(self, tmp_path: Path) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        old = dest / "server.properties"
        old.write_text("old\n", encoding="utf-8")
        old.chmod(0o444)
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w") as tar:
            data = b"new\n"
            info = tarfile.TarInfo("server.properties")
            info.size, info.mtime, info.mode = len(data), 1_700_000_000, 0o644
            tar.addfile(info, io.BytesIO(data))
        blob.seek(0)
        assert d.extract_tar_stream(blob, dest) == ["server.properties"]
        assert old.read_bytes() == b"new\n" and int(old.stat().st_mtime) == 1_700_000_000

    def test_extract_skips_escapes_and_links(self, tmp_path: Path) -> None:
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w") as tar:
            for name in ("../evil", "/abs", "ok/file"):
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
            link = tarfile.TarInfo("ok/link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
        blob.seek(0)
        dest = tmp_path / "dest"
        assert d.extract_tar_stream(blob, dest) == ["ok/file"]
        assert not (tmp_path / "evil").exists() and not (dest / "ok" / "link").exists()


# --------------------------------------------------------------------------------------------------------------------
# Clocks and series
# --------------------------------------------------------------------------------------------------------------------


class TestClock:
    def test_fit(self) -> None:
        fit = d.ClockFit()
        fit.add(100, 1_100)
        fit.add(200, 1_150)
        assert fit.fit_ns() == 950
        assert fit.offset_ns(shared=False) == 950
        assert fit.offset_ns(shared=True) == 0

    def test_shared_clock_that_disagrees_is_not_trusted(self) -> None:
        fit = d.ClockFit()
        fit.add(0, 2_000_000_000)
        assert fit.offset_ns(shared=True) == 2_000_000_000

    def test_no_events(self) -> None:
        assert d.ClockFit().offset_ns(shared=True) == 0
        with pytest.raises(d.DockerError):
            d.ClockFit().offset_ns(shared=False)

    def test_shift_csv(self, tmp_path: Path) -> None:
        path = tmp_path / "threads.csv.gz"
        with SeriesWriter(path, THREAD_COLUMNS) as writer:
            writer.write([1_000, 7, "MC_SERVER", 5])
            writer.write([2_000, 7, "MC_SERVER", 9])
        assert d.shift_series(path, 500) == 2
        rows = read_threads(path).samples
        assert [(s.t_ns, s.tid, s.name, s.cpu_ns) for s in rows] == [
            (1_500, 7, "MC_SERVER", 5),
            (2_500, 7, "MC_SERVER", 9),
        ]
        with gzip.open(path, "rb") as fh:
            fh.read()  # a complete gzip stream again
        assert d.shift_series(path, 0) == 0

    def test_shift_drops_a_cut_off_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "tick.csv.gz"
        full = gzip.compress(b"t_ns,cpu_ns\n" + b"".join(b"%d,%d\n" % (i, i) for i in range(2000)), mtime=0)
        path.write_bytes(full[: len(full) // 2])
        kept = d.shift_series(path, 10)
        assert 0 < kept < 2000
        assert list(iter_lines(path))[1] == "10,0"

    def test_shift_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "foreign_top.jsonl"
        path.write_text('{"t_ns":5,"top":[]}\n{"t_ns":6,"top":[]}\n', encoding="utf-8")
        assert d.shift_series(path, -5) == 2
        assert [json.loads(x)["t_ns"] for x in path.read_text(encoding="utf-8").splitlines()] == [0, 1]


# --------------------------------------------------------------------------------------------------------------------
# Placement checks and doctor helpers
# --------------------------------------------------------------------------------------------------------------------


class TestPlacementCheck:
    def test_all_inside(self) -> None:
        details = {"nice": -5, "threads": [{"tid": 1, "cpus": [2, 3]}, {"tid": 2, "cpus": [3]}]}
        check = d.placement_check([2, 3], [2, 3], details, "above_normal")
        assert check.ok and check.priority == "above_normal" and check.narrowed == {2: (3,)}

    def test_problems(self) -> None:
        details = {"nice": 0, "threads": [{"tid": 1, "cpus": [2, 3, 4]}]}
        check = d.placement_check([2, 3], [2, 3, 4], details, "above_normal")
        assert not check.ok and check.outside == {1: (2, 3, 4)}
        assert any("cpuset" in p for p in check.problems) and any("priority" in p for p in check.problems)

    def test_without_inspection(self) -> None:
        check = d.placement_check([2, 3], [3, 2], {}, "normal")
        assert check.ok and check.priority is None


def _smoke(runs: Path, run_id: str, arm: str, backend: str, rate: float, span: float = 100.0) -> Path:
    run_dir = runs / "smoke" / run_id
    run_dir.mkdir(parents=True)
    run = {
        "run_id": run_id,
        "status": "ok",
        "arm": {"id": arm},
        "scenario": {"name": "smoke"},
        "harness": {"backend": backend},
        "marks": {"bots_start": 10.0, "settled": 10.0 + span},
    }
    metrics = {"metrics": {"chunks_per_s_load": {"value": rate}}}
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return run_dir


class TestChunkTotals:
    def test_compare(self, tmp_path: Path) -> None:
        native = _smoke(tmp_path, "a1", "bds@1", "native-linux", 10.0)
        close = _smoke(tmp_path, "a2", "bds@1", "docker-linux", 9.0)
        far = _smoke(tmp_path, "a3", "bds@1", "docker-linux", 2.0)
        assert d.chunk_total(native) == pytest.approx(1000.0)
        assert d.compare_chunk_totals(native, close).level == "ok"
        warn = d.compare_chunk_totals(native, far)
        assert warn.level == "warn" and warn.detail["docker"] == 200
        assert d.compare_chunk_totals(native, tmp_path / "missing").level == "info"

    def test_latest_pair_of_one_arm(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        _smoke(runs, "20261001-a", "bds@1", "native-linux", 10.0)
        _smoke(runs, "20261002-a", "bds@1", "native-linux", 10.0)
        _smoke(runs, "20261003-b", "bds@1", "docker-linux", 10.0)
        _smoke(runs, "20261004-c", "endstone@1", "docker-linux", 10.0)
        pair = d.latest_smokes(runs)
        assert pair is not None and (pair[0].name, pair[1].name) == ("20261002-a", "20261003-b")
        assert d.latest_smokes(tmp_path / "nothing") is None


def test_parse_stat_nice() -> None:
    line = "1234 (REST Thread(0)) S 1 1234 1234 0 -1 4194560 100 0 0 0 5 6 0 0 15 -5 30 0 777 0 0"
    assert d.parse_stat_nice(line) == -5


def test_inspect_reads_threads_affinity_and_maps(tmp_path: Path) -> None:
    proc = tmp_path / "proc" / "41"
    for tid, name, cpus in ((41, "Main", "2-3"), (57, "MC_SERVER", "3")):
        task = proc / "task" / str(tid)
        task.mkdir(parents=True)
        (task / "status").write_text(f"Name:\t{name}\nCpus_allowed_list:\t{cpus}\n", encoding="utf-8")
        (task / "comm").write_text(f"{name}\n", encoding="utf-8")
    (proc / "stat").write_text(
        "41 (bedrock_server) S 1 41 41 0 -1 4194560 100 0 0 0 5 6 0 0 15 -5 30 0 777 0 0\n", encoding="utf-8"
    )
    (proc / "maps").write_text(
        "7f00-7f01 r-xp 00000000 08:01 12 /usr/lib/x86_64-linux-gnu/libc.so.6\n"
        "7f02-7f03 r-xp 00000000 08:01 13 /venv/endstone/_internal/libmimalloc.so.2\n",
        encoding="utf-8",
    )
    info = d.inspect_info(41, tmp_path / "proc")
    assert info["nice"] == -5 and info["exe"] is None
    assert info["modules"] == ["libc.so.6", "libmimalloc.so.2"]
    assert info["threads"] == [
        {"tid": 41, "name": "Main", "cpus": [2, 3]},
        {"tid": 57, "name": "MC_SERVER", "cpus": [3]},
    ]
    check = d.placement_check([2, 3], [2, 3], info, "above_normal")
    assert check.ok and check.narrowed == {57: (3,)}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_probe_and_inspect_on_linux() -> None:
    probe = d.parse_probe(d.probe_info())
    assert probe.cpus and probe.kernel == os.uname().release
    info = d.inspect_info(os.getpid())
    assert info["pid"] == os.getpid() and info["threads"] and "nice" in info


def test_udp_echo_and_ping() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    echo = subprocess.Popen(
        [sys.executable, "-m", "bedrock_bench.platform.docker", "udp-echo", "--port", str(port), "--timeout", "20"]
    )
    try:
        assert d.udp_ping(port, 15.0) == 0
        assert echo.wait(15) == 0
    finally:
        if echo.poll() is None:
            echo.kill()


def _read_events(proc: subprocess.Popen[bytes], until: str, timeout_s: float = 20.0) -> list[dict[str, object]]:
    assert proc.stdout is not None
    events: list[dict[str, object]] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        events.append(json.loads(line))
        if events[-1].get("ev") == until:
            break
    return events


def test_machine_sidecar(tmp_path: Path) -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bedrock_bench.platform.docker",
            "machine",
            "--out",
            str(tmp_path),
            "--server-cpus",
            "0",
            "--root",
            f"server={sleeper.pid}",
            "--root",
            "harness=999999999",
            "--interval",
            "0.2",
            "--clock-interval",
            "0.2",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        events = _read_events(proc, "clock")
        kinds = [e["ev"] for e in events]
        assert kinds[0] in ("error", "hello") and "hello" in kinds and kinds[-1] == "clock"
        time.sleep(1.0)
        assert proc.stdin is not None
        proc.stdin.write(b'{"op":"stop"}\n')
        proc.stdin.flush()
        done = _read_events(proc, "done")[-1]
        assert done["ev"] == "done"
        report = done["samplers"]["machine"]  # type: ignore[index]
        assert report["error"] is None and report["samples"] >= 3
        assert proc.wait(15) == 0
    finally:
        sleeper.kill()
        if proc.poll() is None:
            proc.kill()
    rows = list(iter_lines(tmp_path / "machine.csv.gz"))
    assert rows[0].startswith("t_ns,foreign_cores,foreign_server_set_cores,server_cores") and len(rows) >= 4


# --------------------------------------------------------------------------------------------------------------------
# A whole run against a fake docker command
# --------------------------------------------------------------------------------------------------------------------

FAKE_DOCKER = r'''
"""A fake docker CLI: containers are local processes, volumes are directories under $FAKE_DOCKER_STATE."""
import csv, hashlib, json, os, shutil, subprocess, sys, tarfile, threading
from pathlib import Path

import psutil

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
CONTAINERS, VOLUMES, IMAGES = STATE / "containers", STATE / "volumes", STATE / "images"
for folder in (CONTAINERS, VOLUMES, IMAGES):
    folder.mkdir(parents=True, exist_ok=True)
VALUE_FLAGS = {"--name", "--label", "--log-driver", "--cpuset-cpus", "--pid", "--network", "--mount", "--workdir",
               "--entrypoint", "--env", "--cap-add", "--format", "--type", "--signal", "--tag", "--build-arg"}


def key(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load(name):
    path = CONTAINERS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save(name, **fields):
    data = load(name) or {"name": name}
    data.update(fields)
    tmp = CONTAINERS / f".{name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, CONTAINERS / f"{name}.json")


def options(args):
    opts, rest, i = {}, [], 0
    while i < len(args):
        arg = args[i]
        if arg in VALUE_FLAGS:
            opts.setdefault(arg, []).append(args[i + 1])
            i += 2
        elif arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in VALUE_FLAGS:
            name, value = arg.split("=", 1)
            opts.setdefault(name, []).append(value)
            i += 1
        elif arg.startswith("--"):
            opts.setdefault(arg, []).append(True)
            i += 1
        else:
            rest = args[i:]
            break
    return opts, rest


def mounts(opts):
    out = {}
    for value in opts.get("--mount", []):
        fields = dict(f.split("=", 1) if "=" in f else (f, True) for f in next(csv.reader([value])))
        source = fields["source"]
        if fields["type"] == "volume":
            source = str(VOLUMES / source)
        out[fields["target"]] = source
    return out


def translate(arg, table):
    for target, source in table.items():
        if arg == target:
            return source
        if arg.startswith(target + "/"):
            return source + arg[len(target):]
    return arg


def alive(data):
    pid = data.get("pid")
    if not data.get("running"):
        return False
    if pid is None:
        return True
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def run(args):
    opts, _ = options(args)
    name = (opts.get("--name") or [f"anon-{os.getpid()}"])[0]
    try:
        return run_container(args, name)
    finally:
        if "--rm" in opts:
            (CONTAINERS / f"{name}.json").unlink(missing_ok=True)


def run_container(args, name):
    opts, rest = options(args)
    image, command = rest[0], rest[1:]
    table = mounts(opts)
    save(name, image=image, cpuset=(opts.get("--cpuset-cpus") or [""])[0], running=True, pid=None, args=args)
    entrypoint = (opts.get("--entrypoint") or [None])[0]
    if "--detach" in opts and "udp-echo" not in command:
        print("0" * 64)
        return 0
    env = dict(os.environ)
    for item in opts.get("--env", []):
        k, v = item.split("=", 1)
        env[k] = v
    if entrypoint == "tar" and "-x" in command:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
            try:
                tar.extractall(table["/data"], filter="data")
            except TypeError:
                tar.extractall(table["/data"])
        save(name, running=False, exit_code=0)
        return 0
    if entrypoint == "sh" and command and command[0] == "-c" and "tar -c" in command[1]:
        root = Path(table["/data"])
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
            for rel in command[3:]:
                if (root / rel).exists():
                    tar.add(root / rel, arcname=rel)
        save(name, running=False, exit_code=0)
        return 0
    if entrypoint == "sh" and command and command[0] == "-c" and "@@bedrock-bench-pid" in command[1]:
        argv = command[3:]
        while argv and argv[0] in ("env", "nice"):
            argv = argv[3:] if argv[0] == "nice" else argv[1:]
            while argv and argv[0] == "-u":
                argv = argv[2:]
        argv = [sys.executable if a == "python" else a for a in argv]
        workdir = translate((opts.get("--workdir") or ["/data"])[0], table)
        try:
            child = subprocess.Popen(argv, cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            print(f"fake docker: exec failed: {exc}", flush=True)
            save(name, running=False, exit_code=127)
            return 127
        save(name, pid=child.pid)
        out = sys.stdout.buffer
        out.write(f"@@bedrock-bench-pid {child.pid}\n".encode())
        out.flush()
        for line in child.stdout:
            out.write(line)
            out.flush()
        code = child.wait()
        save(name, running=False, exit_code=code)
        return code
    if "bedrock_bench.platform.docker" in command and "inspect" in command:
        pid = int(command[command.index("--pid") + 1])
        print(json.dumps({"pid": pid, "nice": 0, "modules": ["libc.so.6", "libmimalloc.so.2"],
                          "threads": [{"tid": pid, "name": "main", "cpus": [2, 3]}]}))
        save(name, running=False, exit_code=0)
        return 0
    if entrypoint is not None:
        command = [translate(entrypoint, table), *command]
    command = [sys.executable if a == "python" else translate(a, table) for a in command]
    child = subprocess.Popen(command, env=env)
    save(name, pid=child.pid)
    code = child.wait()
    save(name, running=False, exit_code=code)
    return code


def main():
    args = sys.argv[1:]
    with open(STATE / "calls.jsonl", "a") as fh:
        fh.write(json.dumps(args) + "\n")
    command = args[0]
    if command == "version":
        print("28.3.0")
    elif command == "info":
        print(json.dumps({"ServerVersion": "28.3.0", "OSType": "linux", "KernelVersion": "6.8.0", "NCPU": 8,
                          "CPUSet": True, "CgroupVersion": "2", "OperatingSystem": "Fake Linux"}))
    elif command == "image" and args[1] == "inspect":
        path = IMAGES / key(args[-1])
        if not path.exists():
            print("Error: No such image", file=sys.stderr)
            return 1
        print("sha256:" + path.read_text())
    elif command in ("build", "pull"):
        ref = args[args.index("--tag") + 1] if command == "build" else args[-1]
        if command == "build":
            data = sys.stdin.buffer.read()
            with tarfile.open(fileobj=__import__("io").BytesIO(data)) as tar:
                assert "Dockerfile" in tar.getnames()
        (IMAGES / key(ref)).write_text(hashlib.sha256(ref.encode()).hexdigest())
    elif command == "volume":
        if args[1] == "create":
            (VOLUMES / args[-1]).mkdir(exist_ok=True)
        else:
            shutil.rmtree(VOLUMES / args[-1], ignore_errors=True)
    elif command == "run":
        return run(args[1:])
    elif command == "inspect":
        _, names = options(args[1:])
        found = [load(n) for n in names]
        if any(f is None for f in found):
            print("Error: No such container", file=sys.stderr)
            return 1
        print(json.dumps([{"Name": "/" + f["name"],
                           "State": {"Running": alive(f), "Pid": f.get("pid") or 0,
                                     "ExitCode": f.get("exit_code", 0), "OOMKilled": False},
                           "HostConfig": {"CpusetCpus": f.get("cpuset", "")}} for f in found]))
    elif command in ("kill", "rm"):
        _, names = options(args[1:])
        for n in names:
            data = load(n)
            if data and data.get("pid"):
                try:
                    psutil.Process(data["pid"]).kill()
                except psutil.Error:
                    pass
            if command == "rm":
                (CONTAINERS / f"{n}.json").unlink(missing_ok=True)
    elif command == "ps":
        for path in CONTAINERS.glob("*.json"):
            data = json.loads(path.read_text())
            if alive(data):
                print(data["name"] + "\t")
    return 0


sys.exit(main())
'''

ECHO_SERVER = """
import pathlib, sys
print("server ready", flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == "stop":
        print("stopping", flush=True)
        break
    if command.startswith("set "):
        pathlib.Path("server.properties").write_text(command[4:] + "\\n")
        print("set done", flush=True)
    else:
        print("echo " + command, flush=True)
"""


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[d.DockerCli, Path]]:
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE_DOCKER, encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    yield d.DockerCli([sys.executable, str(script)]), state


def _calls(state: Path) -> list[list[str]]:
    return [json.loads(line) for line in (state / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def _readline(stream: io.BufferedReader | None, timeout_s: float = 20.0) -> str:
    assert stream is not None
    import threading

    box: list[bytes] = []
    reader = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout_s)
    assert box, "no line from the server in time"
    return box[0].decode().rstrip("\r\n")


def _runtime(cli: d.DockerCli, run_id: str = "20261012T120000Z-bds@1.26.52.3-smoke-r1-test") -> d.DockerRuntime:
    return d.DockerRuntime(
        run_id,
        _placement(),
        cli=cli,
        engine=d.parse_engine_info(NATIVE_INFO),
        probe=_probe(),
        report=lambda message: None,
    )


def _server_copy(tmp_path: Path) -> Path:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    (server_dir / "echo_server.py").write_text(ECHO_SERVER, encoding="utf-8")
    (server_dir / "server.properties").write_text("a=1\n", encoding="utf-8")
    (server_dir / "bedrock_server").write_bytes(d.ELF_MAGIC + b"\0" * 32)
    return server_dir


def test_a_whole_run(fake_docker: tuple[d.DockerCli, Path], tmp_path: Path) -> None:
    cli, state = fake_docker
    server_dir = _server_copy(tmp_path)
    layout = _layout(server_dir)
    spec = LaunchSpec(
        argv=("python", "echo_server.py"),
        cwd=server_dir,
        env_set={"FOO": "bar"},
        env_remove=("LD_PRELOAD",),
        image="bds",
    )
    runtime = _runtime(cli)
    arm = config.arm_from_ref("bds@1.26.52.3")
    series = tmp_path / "run" / "series"
    try:
        launched = runtime.start_server(spec, layout, arm=arm)
        volume = state / "volumes" / runtime.names.volume
        assert (volume / "server.properties").read_text(encoding="utf-8") == "a=1\n"
        assert launched.verified and launched.method == "cpuset" and launched.poll() is None

        assert _readline(launched.stdout) == "server ready"
        assert launched.stdin is not None
        launched.stdin.write(b"hello\n")
        launched.stdin.flush()
        assert _readline(launched.stdout) == "echo hello"

        server = runtime.start_samplers(series)
        assert server.pid == launched.main_pid and server.is_running() and server.status() == "running"
        assert runtime.machine is not None  # the probe says another kernel: machine sampling runs in a sidecar
        threads = runtime.threads
        assert threads is not None
        infos = threads.infos()
        assert infos and all(i.tid > 0 for i in infos)
        runtime.start_tick(infos[0].tid)

        check = runtime.verify_placement("ready")
        assert check.ok and check.process_cpus == (2, 3) and check.priority == "normal"
        assert [m.path for m in server.memory_maps()] == ["libc.so.6", "libmimalloc.so.2"]

        time.sleep(1.5)
        launched.stdin.write(b"set b=2\n")
        launched.stdin.flush()
        assert _readline(launched.stdout) == "set done"
        launched.stdin.write(b"stop\n")
        launched.stdin.flush()
        assert _readline(launched.stdout) == "stopping"
        assert launched.wait(20) == 0
        assert not server.is_running() and server.wait(0) == 0

        offset = runtime.stop_samplers()
        assert abs(offset) < d.SHARED_CLOCK_TOLERANCE_NS  # same host: the fitted offset is only the pipe delay
        assert all(s.error is None for s in runtime.samplers()), [s.error for s in runtime.samplers()]
        for name in ("threads.csv.gz", "process.csv.gz", "tick.csv.gz", "machine.csv.gz"):
            assert len(list(iter_lines(series / name))) >= 2, name

        copied = runtime.copy_back(layout, d.copy_back_paths(adapters.create(arm, _docker_machine()), layout))
        assert "server.properties" in copied and "bedrock_server" in copied
        assert (server_dir / "server.properties").read_text(encoding="utf-8") == "b=2\n"

        facts = runtime.facts()
        assert facts["runtime"] == "docker" and facts["image"].startswith("bedrock-bench/bds:")
        assert facts["clock"] == "fitted" and facts["oom_killed"] is False
        assert {a["name"] for a in runtime.artifacts()} == {"image:bds", "image:runtime", "image:sampler"}
    finally:
        runtime.close()

    assert not (state / "volumes" / runtime.names.volume).exists()
    assert not any((state / "containers").glob("*.json"))
    server_run = next(c for c in _calls(state) if c[:1] == ["run"] and runtime.names.server in c)
    assert server_run[server_run.index("--cpuset-cpus") + 1] == "2-3" and "FOO=bar" in server_run
    assert server_run[-5:] == ["env", "-u", "LD_PRELOAD", "python", "echo_server.py"]
    pod_run = next(c for c in _calls(state) if c[:1] == ["run"] and runtime.names.pod in c)
    assert pod_run[pod_run.index("--cpuset-cpus") + 1] == "4-7" and "--init" in pod_run


def test_kill_a_server_that_hangs(fake_docker: tuple[d.DockerCli, Path], tmp_path: Path) -> None:
    cli, _ = fake_docker
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    (server_dir / "hang.py").write_text("import time\nprint('up', flush=True)\ntime.sleep(120)\n", encoding="utf-8")
    runtime = _runtime(cli, "kill-test")
    try:
        launched = runtime.start_server(
            LaunchSpec(argv=("python", "hang.py"), cwd=server_dir, image="bds"),
            _layout(server_dir),
            arm=config.arm_from_ref("bds@1.26.52.3"),
        )
        assert _readline(launched.stdout) == "up"
        assert launched.kill_tree(timeout_s=20) == [launched.main_pid]
        assert launched.poll() is not None
        assert runtime.verify_placement("steady0").process_cpus is None
    finally:
        runtime.close()


def test_a_server_that_cannot_start(fake_docker: tuple[d.DockerCli, Path], tmp_path: Path) -> None:
    cli, _ = fake_docker
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    runtime = _runtime(cli, "fail-test")
    try:
        with pytest.raises(d.DockerError, match="did not start"):
            runtime.start_server(
                LaunchSpec(argv=("./no-such-server",), cwd=server_dir, image="bds"),
                _layout(server_dir),
                arm=config.arm_from_ref("bds@1.26.52.3"),
            )
    finally:
        runtime.close()


def test_runtime_refuses(fake_docker: tuple[d.DockerCli, Path], tmp_path: Path) -> None:
    cli, _ = fake_docker
    runtime = _runtime(cli, "refuse-test")
    with pytest.raises(d.DockerError, match="not a server image"):
        runtime.prepare_images("sampler", config.arm_from_ref("bds@1.26.52.3"))
    with pytest.raises(d.DockerError, match="Python 3.12"):
        runtime.prepare_images("endstone", config.arm_from_ref("endstone@0.11.14", python="3.12"))
    with pytest.raises(d.DockerError, match="no image"):
        runtime.prepare_images(None, config.arm_from_ref("bds@1.26.52.3"))
    text = tmp_path / "loadbot"
    text.write_bytes(b"MZ not an elf")
    with pytest.raises(d.DockerError, match="not a Linux binary"):
        runtime.bot_command([str(text), "-n", "4"])


def test_bot_command(fake_docker: tuple[d.DockerCli, Path], tmp_path: Path) -> None:
    cli, _ = fake_docker
    binary = tmp_path / "loadbot"
    binary.write_bytes(d.ELF_MAGIC + b"\0" * 16)
    runtime = _runtime(cli, "bot-test")
    argv = runtime.bot_command([str(binary), "-addr", "127.0.0.1:19672", "-n", "4"])
    assert argv[: len(cli.argv)] == list(cli.argv)
    assert argv[-5:] == [runtime.catalog.bases["debian"], "-addr", "127.0.0.1:19672", "-n", "4"]
    assert _after(argv, "--cpuset-cpus") == ["4-7"] and _after(argv, "--env") == ["GOMAXPROCS=4"]
    assert _after(argv, "--network") == [f"container:{runtime.names.pod}"]
    assert _after(argv, "--entrypoint") == [d.BOT_PATH]
    assert _after(argv, "--mount") == [d.mount("bind", binary, d.BOT_PATH, readonly=True)]
    assert runtime.bot_roots() == []  # not the host's kernel: the machine sidecar finds the bot itself
