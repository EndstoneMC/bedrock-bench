# SPDX-License-Identifier: Apache-2.0
"""Topology, machine facts, the idle check and ``bbench doctor``."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
from pathlib import Path

import pytest

from bedrock_bench import host, platform
from bedrock_bench.config import parse_machine
from bedrock_bench.host import Check, Topology
from bedrock_bench.lock import MachineLock
from bedrock_bench.model import ExitCode, Placement
from bedrock_bench.platform import CpuRecord
from bedrock_bench.sampling import machine as machine_sampling
from bedrock_bench.sampling.machine import ForeignLoad

HYBRID = Topology(
    tuple(CpuRecord(cpu=c, core=c // 2, efficiency=1) for c in range(4))
    + tuple(CpuRecord(cpu=c, core=c - 2, efficiency=0) for c in range(4, 8)),
    model="Example CPU",
    machine_logical=8,
    machine_physical=6,
)


@pytest.fixture(autouse=True)
def private_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "machine.lock"
    monkeypatch.setenv("BBENCH_LOCK", str(path))
    return path


def test_topology_properties() -> None:
    assert HYBRID.logical == tuple(range(8))
    assert [c.cpus for c in HYBRID.cores()] == [(0, 1), (2, 3), (4,), (5,), (6,), (7,)]
    assert HYBRID.classes == {0: (4, 5, 6, 7), 1: (0, 1, 2, 3)}
    assert HYBRID.hybrid and HYBRID.smt and HYBRID.kind == "physical"
    assert HYBRID.core_classes() == {"performance": [0, 1, 2, 3], "efficiency": [4, 5, 6, 7]}
    assert HYBRID.describe() == ("Example CPU: 8 logical CPUs on 6 cores, SMT, hybrid: performance 0-3, efficiency 4-7")
    json.dumps(HYBRID.to_json())
    flat = Topology(tuple(CpuRecord(cpu=c, core=c) for c in range(4)), virtualization="kvm")
    assert not flat.hybrid and not flat.smt and flat.kind == "vcpu"
    assert flat.core_classes() == {"performance": [0, 1, 2, 3], "efficiency": []}


def test_detect_topology_on_this_host() -> None:
    topology = host.detect_topology()
    assert topology.logical == platform.current().allowed_cpus()
    assert topology.cores() and topology.model
    assert topology.machine_logical is not None and topology.machine_logical >= len(topology.logical)


def test_machine_facts_carry_no_identity() -> None:
    facts = host.machine_facts()
    assert {"os", "kernel", "cpu", "logical", "physical", "core_classes", "ram_gib", "virtualized", "power"} <= set(
        facts
    )
    assert isinstance(facts["virtualized"], bool) and facts["logical"] >= 1 and facts["ram_gib"] > 0
    text = json.dumps(facts).lower()
    for identity in (socket.gethostname(), getpass.getuser()):
        if len(identity) >= 4:  # very short names match by accident
            assert identity.lower() not in text
    assert os.sep + "Users" + os.sep not in text and "/home/" not in text


def test_check_levels_are_validated() -> None:
    with pytest.raises(ValueError):
        Check("x", "fatal", "nope")


def test_ports() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", 0))
        busy = sock.getsockname()[1]
        assert host.check_ports(busy).level == "fail"
    finally:
        sock.close()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", 0))
    free = probe.getsockname()[1]
    probe.close()
    if host._udp_free(free + 1, socket.AF_INET6) is not False:
        assert host.check_ports(free).level == "ok"


@pytest.mark.parametrize(
    ("output", "level"),
    [(None, "warn"), ("go version go1.24.1 linux/amd64", "warn"), ("go version go1.26.3 windows/amd64", "ok")],
)
def test_go_check(monkeypatch: pytest.MonkeyPatch, output: str | None, level: str) -> None:
    monkeypatch.setattr(host, "_tool_version", lambda argv: output)
    assert host.check_go().level == level


@pytest.mark.parametrize(
    ("settings", "level"),
    [
        ({"scheme": "power_saver", "guid": "a1841308-3541-4fab-bc81-f71556f20b4a"}, "warn"),
        ({"scheme": "high_performance"}, "info"),
        ({"driver": "acpi-cpufreq", "governors": ["powersave"]}, "warn"),
        ({"driver": "intel_pstate", "governors": ["powersave"]}, "info"),
    ],
)
def test_power_check(monkeypatch: pytest.MonkeyPatch, settings: dict, level: str) -> None:
    monkeypatch.setattr(platform.current(), "power_settings", lambda: settings)
    assert host.check_power().level == level


def fake_load(foreign: tuple[float, ...], server_set: tuple[float, ...] = ()) -> ForeignLoad:
    return ForeignLoad(1.0, foreign, server_set or tuple(0.0 for _ in foreign), [{"pid": 1, "name": "x", "cores": 1}])


def test_idle_check_against_the_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = parse_machine({})
    monkeypatch.setattr(machine_sampling, "measure_foreign", lambda *a, **k: fake_load((-0.02, 0.01, 0.0)))
    quiet = host.idle_check(machine, None, seconds=3)
    assert quiet.ok and quiet.foreign_cores == 0.0  # floored, never negative
    monkeypatch.setattr(machine_sampling, "measure_foreign", lambda *a, **k: fake_load((2.0,) * 20, (0.5,) * 20))
    busy = host.idle_check(machine, Placement((0,), (1,), (1,), "normal", "physical"), seconds=20)
    assert not busy.ok
    assert len(busy.reasons) == 3  # mean, 10 s peak and server set
    assert busy.top[0]["name"] == "x"


def test_run_doctor_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_tool_version", lambda argv: "go version go1.26.3")
    report = host.run_doctor(parse_machine({}), idle_s=0)
    ids = [c.id for c in report.checks]
    assert ids[:2] == ["platform", "topology"]
    assert {"virtualization", "placement", "priority", "power", "cpu_time", "lock", "ports", "go"} <= set(ids)
    assert "idle" not in ids and report.idle is None
    data = report.to_json()
    json.dumps(data)
    assert data["ok"] == (not report.failed)
    rendered = report.render()
    assert rendered.splitlines()[0].startswith("bbench doctor")
    assert rendered.splitlines()[-1].startswith("FAILED" if report.failed else "OK")


def test_run_doctor_reports_a_bad_profile_and_a_held_lock(monkeypatch: pytest.MonkeyPatch, private_lock: Path) -> None:
    monkeypatch.setattr(host, "_tool_version", lambda argv: None)
    held = MachineLock(private_lock, label="other run")
    assert held.acquire()
    try:
        bad = parse_machine({"placement": {"server_cpus": "0", "bot_cpus": "4096"}})
        report = host.run_doctor(bad, idle_s=1)
    finally:
        held.release()
    levels = {c.id: c.level for c in report.checks}
    assert levels["placement"] == "fail" and report.placement is None
    assert levels["lock"] == "warn" and levels["idle"] == "info"
    assert levels["go"] == "warn"
    assert "placement" in [c.id for c in report.failed]


def test_doctor_entry_point(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(machine_sampling, "measure_foreign", lambda *a, **k: fake_load((0.0,) * 15))
    code = host.doctor(argparse.Namespace(machine=None, json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["idle"]["foreign_cores"] == 0.0
    assert code == (ExitCode.OK if data["ok"] else ExitCode.ENV_GATE)
    code = host.doctor(argparse.Namespace(machine=None, json=False))
    out = capsys.readouterr()
    assert "placement" in out.out and "measuring idle load" in out.err
