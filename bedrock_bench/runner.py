# SPDX-License-Identifier: Apache-2.0
"""Running benchmarks: ``bbench run-one``, ``bbench run`` and ``bbench smoke`` (docs/methodology.md#run-phases).

``run-one`` measures one arm once::

    lock -> placement and pre-flight checks -> fetch -> idle gate -> run directory -> prepare -> phases -> results

It refuses to start (exit 3, no run directory) when the machine lock is held, the placement cannot be made, the port
is taken, the bot or the install is missing, or the machine is not idle. Once the run directory exists every outcome
is recorded, and the exit code is derived from the recorded status: 0 ok, 4 server failed, 5 invalid, 130
interrupted. A server death can therefore never exit 0.

``run`` is one session: the given tags, or every server's benchmark default, each run ``--repeats`` times. It plans
the order (``rotate``: block ``k`` runs the servers rotated by ``k``; ``sequential``; or ``seeded:N``), takes the
machine lock for the whole session, fetches every server, then starts one ``bbench run-one`` subprocess per run with a
cooldown between runs. The runs share a session id and land in ``<out>/<session>/``. The child inherits the lock
through :meth:`bedrock_bench.lock.MachineLock.child_env`. An ``invalid`` run is rerun up to ``--rerun-invalid`` times;
``server_failed`` is a result and never rerun. The session exits with the worst final status and writes the report.

``smoke`` is ``run-one`` with the short smoke workload plus adapter checks: every harness command accepted
without an error line, and movement corrections seen where the feature file declares movement validation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from bedrock_bench import adapters, bots, config, host, platform, provenance, results
from bedrock_bench.adapters.base import ServerAdapter, host_os
from bedrock_bench.cache import Cache, remove_tree
from bedrock_bench.clock import NS_PER_S, Origin
from bedrock_bench.config import ConfigError
from bedrock_bench.fetch import FetchError
from bedrock_bench.lock import MachineLock, holder
from bedrock_bench.model import (
    Arm,
    ExitCode,
    FeatureClass,
    Flag,
    Machine,
    Placement,
    RunContext,
    Status,
)
from bedrock_bench.phases import COMMAND_REPLY_S, Session, bot_command
from bedrock_bench.workload import SMOKE, WORKLOAD, Workload, for_run, is_builtin

log = logging.getLogger(__name__)

DEFAULT_OUT = Path("runs")

#: Feature whose declared parity makes ``smoke`` expect movement corrections.
MOVEMENT_FEATURE = "player.server_authoritative_movement"


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRequest:
    """Everything ``run-one`` needs, resolved from the command line."""

    arm: Arm
    workload: Workload
    machine: Machine
    session: str = field(default_factory=results.make_session_id)
    order: str | None = None
    repeat: int = 1
    order_index: int | None = None
    out: Path = DEFAULT_OUT

    @property
    def session_dir(self) -> Path:
        """``<out>/<session>``; a run started on its own is a session of one run."""
        return self.out / self.session


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    run_dir: Path | None = None
    outcome: results.Outcome | None = None


def run_one(args: argparse.Namespace) -> int:
    """``bbench run-one``: measure one arm once."""
    return execute(_request(args)).exit_code


def _request(args: argparse.Namespace) -> RunRequest:
    arm = config.arm_from_ref(args.server, artifact=args.artifact, bds=args.bds, python=args.python)
    if args.repeat < 1:
        raise ConfigError("--repeat must be at least 1")
    if args.session is not None and not results.SESSION_RE.match(args.session):
        raise ConfigError(f"bad session id {args.session!r}")
    return RunRequest(
        arm=arm,
        workload=for_run(WORKLOAD),
        machine=config.load_machine(args.machine),
        session=args.session or results.make_session_id(),
        order=args.order,
        repeat=args.repeat,
        order_index=args.order_index,
        out=args.out or DEFAULT_OUT,
    )


def execute(request: RunRequest) -> RunResult:
    """Run one arm once. Returns exit code 3 without a run directory when an environment check refuses."""
    arm, workload, machine = request.arm, request.workload, request.machine
    adapter = adapters.create(arm, machine)
    check_platform(adapter)
    try:
        bot = bot_command(adapter, workload, machine)
    except bots.BotError as exc:
        _say(f"bbench run-one: {exc}")
        return RunResult(ExitCode.ENV_GATE)

    lock = MachineLock(label=f"{arm.id} {workload.name}")
    if not lock.acquire(timeout=0):
        info = holder(lock.path)
        _say(f"bbench run-one: the machine lock is held by {info.describe() if info else 'another process'}")
        return RunResult(ExitCode.ENV_GATE)
    try:
        topology = host.detect_topology()
        try:
            placement = host.plan_placement(machine, topology)
        except host.PlacementError as exc:
            _say(f"bbench run-one: cannot place the run: {exc}")
            return RunResult(ExitCode.ENV_GATE)
        problems = preflight(machine, placement, topology)
        if problems:
            for problem in problems:
                _say(f"bbench run-one: {problem}")
            return RunResult(ExitCode.ENV_GATE)
        cache = Cache()
        try:
            install = adapter.fetch(cache)
        except FetchError as exc:
            _say(f"bbench run-one: cannot fetch {arm.id}: {exc}")
            return RunResult(ExitCode.ENV_GATE)
        tsc_hz = host.calibrate_tsc()
        idle = host.idle_check(machine, placement)
        if not idle.ok:
            _say("bbench run-one: the machine is not idle: " + "; ".join(idle.reasons))
            for entry in idle.top[:5]:
                _say(f"  {entry['name']} (pid {entry['pid']}): {entry['cores']:.2f} cores")
            return RunResult(ExitCode.ENV_GATE)
        return _measure(request, adapter, placement, topology, install, tsc_hz, idle.foreign_cores, bot)
    finally:
        lock.release()


def _measure(
    request: RunRequest,
    adapter: ServerAdapter,
    placement: Placement,
    topology: host.Topology,
    install: Any,
    tsc_hz: float | None,
    idle_foreign: float,
    bot: Any,
) -> RunResult:
    arm, workload, machine = request.arm, request.workload, request.machine
    run_dir = results.RunDir.create(request.session_dir, arm.id, request.repeat)
    _say(f"run {run_dir.run_id}: {arm.id}")
    harness = provenance.harness_info(backend())
    nonstandard = []
    flags = set()
    if arm.artifact is not None:
        nonstandard.append(Flag.UNPUBLISHED_ARTIFACT.value)
        flags.add(Flag.UNPUBLISHED_ARTIFACT.value)
    if bot.override:
        nonstandard.append("bot_override")
    if not is_builtin(workload):
        nonstandard.append("test_workload")
    if harness["dirty"]:
        nonstandard.append("dirty_harness")
    origin = Origin.capture()
    record = results.new_record(
        run_id=run_dir.run_id,
        arm=arm,
        workload=workload,
        session={
            "id": request.session,
            "repeat": request.repeat,
            "order_index": request.order_index,
            "order": request.order,
        },
        harness=harness,
        machine=results.machine_record(machine, provenance.machine_fingerprint(), host.machine_facts(topology), tsc_hz),
        placement=placement,
        install=install,
        parity=results.parity_record(adapter.features_file, arm.version, workload),
        bot=bot.provenance,
        origin=origin,
        idle_foreign_cores=idle_foreign,
        official=not nonstandard,
        nonstandard=nonstandard,
    )
    run_dir.write_partial(record)

    work_dir = Cache().work_dir(run_dir.run_id)
    reasons: list[str] = []
    interrupted = False
    try:
        ctx = RunContext(
            run_id=run_dir.run_id,
            run_dir=run_dir.path,
            work_dir=work_dir,
            arm=arm,
            workload=workload,
            machine=machine,
            placement=placement,
            repeat=request.repeat,
        )
        try:
            layout = adapter.prepare(install, ctx)
        except Exception as exc:
            log.exception("prepare failed")
            reasons.append(f"harness_ok: prepare failed: {type(exc).__name__}: {exc}")
            layout = None
        if layout is not None:
            results.apply_layout(record, layout)
            run_dir.write_partial(record)
            session = Session(
                adapter=adapter,
                workload=workload,
                placement=placement,
                layout=layout,
                run_dir=run_dir,
                record=record,
                origin=origin,
                bot=bot,
                progress=lambda text: _say(f"  {text}"),
            )
            session_result = session.run()
            reasons += session_result.reasons
            flags |= session_result.flags
            interrupted = session_result.interrupted
    except KeyboardInterrupt:
        interrupted = True
        reasons.append("harness_ok: interrupted")
    final = results.finalize(
        run_dir,
        record,
        reasons=reasons,
        flags=flags,
        scrub_roots={"<server>": work_dir, "<run>": run_dir.path},
    )
    _remove_work_dir(work_dir)
    _say(f"run {run_dir.run_id}: {final.status.value}")
    for reason in final.reasons:
        _say(f"  {reason}")
    code = ExitCode.INTERRUPTED if interrupted else final.exit_code
    return RunResult(code, run_dir.path, final)


def _remove_work_dir(work_dir: Path) -> None:
    for attempt in range(10):
        try:
            remove_tree(work_dir)
            return
        except OSError as exc:  # Windows may hold a file of a process that just exited for a moment
            if attempt == 9:
                log.warning("cannot remove the server copy %s: %s", work_dir, exc)
            time.sleep(0.5)


def backend() -> str:
    """The ``harness.backend`` of run.json."""
    return f"native-{platform.name()}"


def check_platform(adapter: ServerAdapter) -> None:
    """Refuse an arm its adapter cannot run here (a ConfigError, exit 2)."""
    here = host_os()
    if here not in adapter.platforms:
        supported = ", ".join(sorted(adapter.platforms))
        raise ConfigError(f"{adapter.name} does not run on {here} (supported: {supported})")


def preflight(machine: Machine, placement: Placement, topology: host.Topology) -> list[str]:
    """Environment problems that make a run impossible or unfair: a taken port, a priority that cannot be set,
    affinity masks that cannot express the placement.

    Fewer than ``host.MIN_BOT_CPUS`` bot CPUs is a ``bbench doctor`` failure but not a refusal here: the
    ``bot_saturated`` gate judges every run on what its bots actually used.
    """
    problems = []
    ports = host.check_ports(machine.port)
    if ports.level == "fail":
        problems.append(ports.summary)
    if not platform.current().can_set_priority(machine.priority):
        problems.append(f"priority {machine.priority} cannot be set (needs root, CAP_SYS_NICE or RLIMIT_NICE)")
    for check in host.placement_checks(placement, topology):
        if check.level == "fail" and check.id != "bot_cpus":
            problems.append(check.summary)
        elif check.level != "ok":
            log.warning("%s", check.summary)
    return problems


# --------------------------------------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------------------------------------


def tag_arm(tag: str) -> Arm:
    """The arm a tag names. The server's benchmark default brings its ``bds`` and ``python``."""
    arm = config.arm_from_ref(tag)
    if not (config.project_root() / config.CONFIG_PATHS["servers"].format(arm.server)).is_file():
        return arm
    default = config.load_benchmark(arm.server)
    return default.arm if default.version == arm.version else arm


def session_arms(tags: Sequence[str]) -> tuple[list[Arm], list[str]]:
    """The arms of a session and the servers left out, each with its reason.

    With tags, exactly those. Without, every server's benchmark default, except a server marked ``include = false`` or
    one that does not run on this platform.
    """
    if tags:
        arms = [tag_arm(tag) for tag in tags]
        seen: set[str] = set()
        for arm in arms:
            if arm.id in seen:
                raise ConfigError(f"{arm.id} is given twice")
            seen.add(arm.id)
        return arms, []
    arms, skipped = [], []
    here = host_os()
    for default in config.benchmark_defaults():
        arm = default.arm
        if not default.include:
            skipped.append(f"{arm.id}: {default.reason}")
            continue
        platforms = adapters.get(default.server).platforms
        if here not in platforms:
            skipped.append(f"{arm.id}: runs on {', '.join(sorted(platforms))} only")
            continue
        arms.append(arm)
    return arms, skipped


@dataclass(frozen=True)
class PlannedRun:
    """One run of a session in execution order."""

    order_index: int
    repeat: int
    arm: Arm

    HEADER: ClassVar[str] = f"{'order':>5}  {'repeat':<6}  server"

    def describe(self) -> str:
        return f"{self.order_index:>5}  {'r' + str(self.repeat):<6}  {self.arm.id}"


def plan_order(arms: Sequence[Arm], *, repeats: int, order: str) -> list[PlannedRun]:
    """The runs of a session in execution order.

    Block ``k`` (repeat ``k + 1``) runs every arm once: in the given order rotated by ``k`` (``rotate``), unrotated
    (``sequential``) or shuffled by a seeded generator (``seeded:N``).
    """
    config.validate_order(order)
    rng = random.Random(int(order.partition(":")[2])) if order.startswith("seeded:") else None
    planned: list[PlannedRun] = []
    for block in range(repeats):
        members = list(arms)
        if order == "rotate" and members:
            shift = block % len(members)
            members = members[shift:] + members[:shift]
        elif rng is not None:
            rng.shuffle(members)
        first = len(planned)
        planned.extend(PlannedRun(first + i, block + 1, arm) for i, arm in enumerate(members))
    return planned


@dataclass
class _Progress:
    finals: list[tuple[PlannedRun, int]] = field(default_factory=list)


def run_session(args: argparse.Namespace) -> int:
    """``bbench run [TAG ...]``: one session, one ``run-one`` subprocess per run, then the report."""
    if args.repeats < 1:
        raise ConfigError("--repeats must be at least 1")
    if args.cooldown < 0:
        raise ConfigError("--cooldown must not be negative")
    if args.rerun_invalid < 0:
        raise ConfigError("--rerun-invalid must not be negative")
    arms, skipped = session_arms(args.tags)
    for line in skipped:
        _say(f"skipping {line}")
    if not arms:
        raise ConfigError("no server to run")
    machine = config.load_machine(args.machine)
    for arm in arms:
        check_platform(adapters.create(arm, machine))
    plan = plan_order(arms, repeats=args.repeats, order=args.order)
    out = args.out or DEFAULT_OUT
    session = results.make_session_id()
    session_dir = out / session

    if args.dry_run:
        print(f"session {session}: {len(plan)} runs, order {args.order}, results in {session_dir.as_posix()}")
        print(PlannedRun.HEADER)
        for run in plan:
            print(run.describe())
        return ExitCode.OK

    lock = MachineLock(label=f"session {session}")
    if not lock.acquire(timeout=0):
        info = holder(lock.path)
        _say(f"bbench run: the machine lock is held by {info.describe() if info else 'another process'}")
        return ExitCode.ENV_GATE
    try:
        code = _prepare_session(plan, machine)
        if code != ExitCode.OK:
            return code
        _say(f"session {session}: {len(plan)} runs")
        progress = _Progress()
        code = _execute_plan(args, session, plan, out, lock, progress)
    finally:
        lock.release()
    _summarize(session, progress)
    _write_report(session_dir)
    return code


def _prepare_session(plan: Sequence[PlannedRun], machine: Machine) -> int:
    """Placement, pre-flight checks, bots and installs for every arm before the first run starts."""
    topology = host.detect_topology()
    try:
        placement = host.plan_placement(machine, topology)
    except host.PlacementError as exc:
        _say(f"bbench run: cannot place the runs: {exc}")
        return ExitCode.ENV_GATE
    problems = preflight(machine, placement, topology)
    if problems:
        for problem in problems:
            _say(f"bbench run: {problem}")
        return ExitCode.ENV_GATE
    cache = Cache()
    for arm in {run.arm.id: run.arm for run in plan}.values():
        adapter = adapters.create(arm, machine)
        try:
            bot_command(adapter, WORKLOAD, machine)
            _say(f"fetching {arm.id}")
            adapter.fetch(cache)
        except (FetchError, bots.BotError) as exc:
            _say(f"bbench run: {arm.id}: {exc}")
            return ExitCode.ENV_GATE
    return ExitCode.OK


def _child_command(args: argparse.Namespace, session: str, run: PlannedRun, out: Path) -> list[str]:
    command = [sys.executable, "-m", "bedrock_bench", *["-v"] * min(getattr(args, "verbose", 0), 2)]
    command += [
        "run-one",
        "--server",
        run.arm.ref,
        "--session",
        session,
        "--repeat",
        str(run.repeat),
        "--order-index",
        str(run.order_index),
        "--order",
        args.order,
        "--out",
        str(out),
    ]
    if run.arm.bds:
        command += ["--bds", run.arm.bds]
    if run.arm.python:
        command += ["--python", run.arm.python]
    if args.machine:
        command += ["--machine", str(config.input_file(args.machine, "machine profile").resolve())]
    return command


def _execute_plan(
    args: argparse.Namespace,
    session: str,
    plan: Sequence[PlannedRun],
    out: Path,
    lock: MachineLock,
    progress: _Progress,
) -> int:
    env = {**os.environ, **lock.child_env()}
    started = False
    for run in plan:
        attempts = 0
        while True:
            if started and args.cooldown > 0:
                _say(f"cooldown {args.cooldown:g} s")
                time.sleep(args.cooldown)
            started = True
            _say(f"[{run.order_index + 1}/{len(plan)}] {run.arm.id} r{run.repeat}")
            try:
                code = subprocess.run(_child_command(args, session, run, out), env=env, check=False).returncode
            except KeyboardInterrupt:
                _say("bbench run: interrupted")
                return ExitCode.INTERRUPTED
            if code in (ExitCode.USAGE, ExitCode.ENV_GATE, ExitCode.INTERRUPTED):
                _say(f"bbench run: stopping the session (run-one exited with {code})")
                return code
            if code not in (ExitCode.OK, ExitCode.SERVER_FAILED, ExitCode.INVALID):
                log.error("run-one exited with unexpected code %s; counting the run as invalid", code)
                code = ExitCode.INVALID
            if code == ExitCode.INVALID and attempts < args.rerun_invalid:
                attempts += 1
                _say(f"invalid run; rerunning ({attempts}/{args.rerun_invalid})")
                continue
            progress.finals.append((run, code))
            break
    return max((code for _, code in progress.finals), default=ExitCode.OK, key=_severity)


_STATUS_OF_CODE = {
    ExitCode.OK: Status.OK,
    ExitCode.SERVER_FAILED: Status.SERVER_FAILED,
    ExitCode.INVALID: Status.INVALID,
}


def _severity(code: int) -> int:
    return {ExitCode.OK: 0, ExitCode.SERVER_FAILED: 1, ExitCode.INVALID: 2}.get(code, 3)


def _summarize(session: str, progress: _Progress) -> None:
    if not progress.finals:
        return
    counts: dict[str, int] = {}
    for _, code in progress.finals:
        status = _STATUS_OF_CODE.get(ExitCode(code), Status.INVALID).value
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    _say(f"session {session}: {summary}")


def _write_report(session_dir: Path) -> None:
    if not session_dir.is_dir() or not any(session_dir.iterdir()):
        return
    try:
        from bedrock_bench import report
    except ImportError:
        return
    try:
        written = report.write(session_dir)
    except report.ReportError as exc:
        _say(f"bbench run: no report: {exc}")
        return
    except Exception:  # the runs are on disk; 'bbench report' can be run again
        log.exception("the report failed")
        return
    _say(f"wrote {written.report_path.as_posix()}")


# --------------------------------------------------------------------------------------------------------------------
# Smoke
# --------------------------------------------------------------------------------------------------------------------


def smoke(args: argparse.Namespace) -> int:
    """``bbench smoke server@version``: a short strict run that checks an adapter end to end."""
    arm = config.arm_from_ref(args.server, artifact=args.artifact)
    machine = config.load_machine(args.machine)
    adapter = adapters.create(arm, machine)
    request = RunRequest(
        arm=arm,
        workload=for_run(SMOKE),
        machine=machine,
        out=args.out or DEFAULT_OUT,
    )
    result = execute(request)
    if result.run_dir is None:
        return result.exit_code
    if result.exit_code != ExitCode.OK:
        _say(f"smoke {arm.id}: FAILED (run status {result.outcome.status.value if result.outcome else '?'})")
        return result.exit_code
    problems = smoke_problems(result.run_dir, adapter)
    if problems:
        _say(f"smoke {arm.id}: FAILED")
        for problem in problems:
            _say(f"  {problem}")
        return ExitCode.INVALID
    _say(f"smoke {arm.id}: passed")
    return ExitCode.OK


def smoke_problems(run_dir: Path, adapter: ServerAdapter) -> list[str]:
    """Adapter wiring problems a run's status does not show."""
    data = results.load_run(run_dir)
    metrics = json.loads((run_dir / results.METRICS_FILE).read_text(encoding="utf-8"))["metrics"]
    problems: list[str] = []

    declared = (data.run.get("parity") or {}).get("row", {}).get(MOVEMENT_FEATURE)
    if declared in (FeatureClass.VANILLA, FeatureClass.PARTIAL, FeatureClass.ALTERED):
        entry = metrics.get("corrections_per_bot_s") or {}
        if entry.get("availability") != "measured" or not entry.get("value"):
            problems.append(f"no movement corrections in steady, but the feature file declares {MOVEMENT_FEATURE}")

    errors = [
        line for line in data.log if line.level in ("error", "fatal") and not adapter.dialect.is_benign(line.text)
    ]
    window_ns = int(COMMAND_REPLY_S * NS_PER_S)
    stop = adapter.dialect.stop_cmd
    sent = [c for c in data.commands if c.get("origin") == "phase"]
    for command in sent:
        send_ns = command.get("send_ns")
        if command.get("text") == stop:
            continue  # errors while the server shuts down are not answers to a command
        if command.get("error"):
            problems.append(f"command {command.get('text')!r} was not sent: {command['error']}")
            continue
        replies = [e for e in errors if send_ns is not None and send_ns <= e.t_ns < send_ns + window_ns]
        if replies:
            problems.append(f"command {command.get('text')!r} was answered with: {replies[0].text}")
    return list(dict.fromkeys(problems))


__all__ = [
    "DEFAULT_OUT",
    "PlannedRun",
    "RunRequest",
    "RunResult",
    "backend",
    "check_platform",
    "execute",
    "plan_order",
    "preflight",
    "run_one",
    "run_session",
    "session_arms",
    "smoke",
    "smoke_problems",
    "tag_arm",
]
