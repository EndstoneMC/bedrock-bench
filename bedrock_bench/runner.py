# SPDX-License-Identifier: Apache-2.0
"""Running benchmarks: ``bbench run-one``, ``bbench run`` and ``bbench smoke`` (docs/methodology.md#run-phases).

``run-one`` measures one arm once::

    lock -> placement and pre-flight checks -> fetch -> idle gate -> run directory -> prepare -> phases -> results

It refuses to start (exit 3, no run directory) when the machine lock is held, the placement cannot be made, the port
is taken, the bot or the install is missing, or the machine is not idle. Once the run directory exists every outcome
is recorded, and the exit code is derived from the recorded status: 0 ok, 4 server failed, 5 invalid, 130
interrupted. A server death can therefore never exit 0.

``run`` executes a matrix. It plans the order (``rotate``: block ``k`` runs each scenario's arms rotated by ``k``;
``sequential``; or ``seeded:N``), refuses to add runs to a matrix directory that holds runs from another machine
profile, machine or scenario version, takes the machine lock for the whole matrix, fetches every arm, then starts one
``bbench run-one`` subprocess per run with a cooldown between runs. The child inherits the lock through
:meth:`bedrock_bench.lock.MachineLock.child_env` and the effective run order through ``$BBENCH_MATRIX_ORDER``. An
``invalid`` run is rerun up to ``rerun_invalid`` times; ``server_failed`` is a result and never rerun. The matrix
exits with the worst final status and writes the report.

``smoke`` is ``run-one`` with ``configs/scenarios/smoke.toml`` plus adapter checks: every harness command accepted
without an error line, movement corrections seen where the feature file declares movement validation, and every
chaos-dialect command sent once.
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

from bedrock_bench import adapters, bots, chaos, config, host, platform, provenance, results
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
    Matrix,
    Placement,
    RunContext,
    Scenario,
    Status,
)
from bedrock_bench.phases import COMMAND_REPLY_S, Session, bot_command

log = logging.getLogger(__name__)

DEFAULT_OUT = Path("runs")
SMOKE_SCENARIO = "smoke"

#: Set by ``bbench run`` for its ``run-one`` children: the effective run order, which may differ from the matrix file.
ENV_MATRIX_ORDER = "BBENCH_MATRIX_ORDER"

#: Feature whose declared parity makes ``smoke`` expect movement corrections.
MOVEMENT_FEATURE = "player.server_authoritative_movement"


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRequest:
    """Everything ``run-one`` needs, resolved from the command line or a matrix."""

    arm: Arm
    scenario: Scenario
    machine: Machine
    matrix: Matrix | None = None
    order: str | None = None
    repeat: int = 1
    order_index: int | None = None
    out: Path = DEFAULT_OUT
    extra_commands: tuple[str, ...] = ()

    @property
    def matrix_dir(self) -> Path:
        """``<out>/<matrix>``; a run outside a matrix groups under its scenario name."""
        return self.out / (self.matrix.name if self.matrix is not None else self.scenario.name)


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    run_dir: Path | None = None
    outcome: results.Outcome | None = None


def run_one(args: argparse.Namespace) -> int:
    """``bbench run-one``: measure one arm once."""
    return execute(_request(args)).exit_code


def _request(args: argparse.Namespace) -> RunRequest:
    matrix: Matrix | None = None
    if args.server:
        arm = config.arm_from_ref(args.server, variant=args.variant, artifact=args.artifact)
    else:
        if args.variant is not None or args.artifact is not None:
            raise ConfigError("--variant and --artifact go with --server; a matrix arm defines its own")
        matrix = config.load_matrix(args.matrix)
        try:
            arm = matrix.arm(args.arm)
        except KeyError as exc:
            raise ConfigError(str(exc.args[0])) from None
    scenario = config.load_scenario(args.scenario)
    if matrix is not None and scenario.name not in matrix.scenarios_for(arm):
        raise ConfigError(f"arm {arm.id} of matrix {matrix.name} does not run scenario {scenario.name}")
    if args.repeat < 1:
        raise ConfigError("--repeat must be at least 1")
    machine = config.load_machine(args.machine)
    order = None
    if matrix is not None:
        machine = config.with_priority(machine, matrix.priority_override)
        order = config.validate_order(os.environ.get(ENV_MATRIX_ORDER) or matrix.order)
    return RunRequest(
        arm=arm,
        scenario=scenario,
        machine=machine,
        matrix=matrix,
        order=order,
        repeat=args.repeat,
        order_index=args.order_index,
        out=args.out or DEFAULT_OUT,
    )


def execute(request: RunRequest) -> RunResult:
    """Run one arm once. Returns exit code 3 without a run directory when an environment check refuses."""
    arm, scenario, machine = request.arm, request.scenario, request.machine
    adapter = adapters.create(arm, machine)
    check_platform(adapter)
    try:
        bot = bot_command(adapter, scenario, machine)
    except bots.BotError as exc:
        _say(f"bbench run-one: {exc}")
        return RunResult(ExitCode.ENV_GATE)

    lock = MachineLock(label=f"{arm.id} {scenario.name}")
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
        template = template_for(cache, arm, scenario)
        tsc_hz = host.calibrate_tsc()
        idle = host.idle_check(machine, placement)
        if not idle.ok:
            _say("bbench run-one: the machine is not idle: " + "; ".join(idle.reasons))
            for entry in idle.top[:5]:
                _say(f"  {entry['name']} (pid {entry['pid']}): {entry['cores']:.2f} cores")
            return RunResult(ExitCode.ENV_GATE)
        return _measure(request, adapter, placement, topology, install, template, tsc_hz, idle.foreign_cores, bot)
    finally:
        lock.release()


def _measure(
    request: RunRequest,
    adapter: ServerAdapter,
    placement: Placement,
    topology: host.Topology,
    install: Any,
    template: Path | None,
    tsc_hz: float | None,
    idle_foreign: float,
    bot: Any,
) -> RunResult:
    arm, scenario, machine = request.arm, request.scenario, request.machine
    run_dir = results.RunDir.create(request.matrix_dir, arm.id, scenario.name, request.repeat)
    _say(f"run {run_dir.run_id}: {arm.id} on {scenario.name}")
    harness = provenance.harness_info(backend())
    nonstandard = []
    flags = set()
    if arm.artifact is not None:
        nonstandard.append(Flag.UNPUBLISHED_ARTIFACT.value)
        flags.add(Flag.UNPUBLISHED_ARTIFACT.value)
    if bot.override:
        nonstandard.append("bot_override")
    if harness["dirty"]:
        nonstandard.append("dirty_harness")
    origin = Origin.capture()
    record = results.new_record(
        run_id=run_dir.run_id,
        arm=arm,
        scenario=scenario,
        matrix={
            "name": request.matrix.name if request.matrix is not None else None,
            "repeat": request.repeat,
            "order_index": request.order_index,
            "order": request.order,
        },
        harness=harness,
        machine=results.machine_record(machine, provenance.machine_fingerprint(), host.machine_facts(topology), tsc_hz),
        placement=placement,
        install=install,
        parity=results.parity_record(adapter.features_file, arm.version, scenario),
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
            scenario=scenario,
            machine=machine,
            placement=placement,
            repeat=request.repeat,
            template=template,
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
                scenario=scenario,
                placement=placement,
                layout=layout,
                run_dir=run_dir,
                record=record,
                origin=origin,
                bot=bot,
                extra_commands=request.extra_commands,
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


def template_for(cache: Cache, arm: Arm, scenario: Scenario) -> Path | None:
    """The validated world template of a template scenario: ``Cache.template_dir(server, version, scenario)``."""
    if scenario.world.source != "template":
        return None
    path = cache.template_dir(arm.server, arm.version, scenario.name)
    if not path.is_dir():
        raise ConfigError(
            f"no world template for {arm.ref} and scenario {scenario.name}; build one with "
            f"'bbench prepare-world {arm.ref} --scenario {scenario.name}'"
        )
    return path


# --------------------------------------------------------------------------------------------------------------------
# Matrices
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedRun:
    """One run of a matrix in execution order."""

    order_index: int
    repeat: int
    arm: Arm
    scenario: str

    HEADER: ClassVar[str] = f"{'order':>5}  {'repeat':<6}  {'arm':<36}  scenario"

    def describe(self) -> str:
        return f"{self.order_index:>5}  {'r' + str(self.repeat):<6}  {self.arm.id:<36}  {self.scenario}"


def plan_order(matrix: Matrix, *, repeats: int, order: str, only: Sequence[str] = ()) -> list[PlannedRun]:
    """The runs of a matrix in execution order.

    Block ``k`` (repeat ``k + 1``) runs every scenario in matrix order; within a scenario the arms run in matrix order
    rotated by ``k`` (``rotate``), unrotated (``sequential``) or shuffled by a seeded generator (``seeded:N``).
    """
    config.validate_order(order)
    arms = list(matrix.arms)
    if only:
        known = {arm.id for arm in arms}
        unknown = [arm_id for arm_id in only if arm_id not in known]
        if unknown:
            raise ConfigError(f"matrix {matrix.name} has no arm {unknown[0]!r} (arms: {', '.join(sorted(known))})")
        arms = [arm for arm in arms if arm.id in set(only)]
    rng = random.Random(int(order.partition(":")[2])) if order.startswith("seeded:") else None
    planned: list[PlannedRun] = []
    for block in range(repeats):
        for scenario in matrix.scenarios:
            members = [arm for arm in arms if scenario in matrix.scenarios_for(arm)]
            if not members:
                continue
            if order == "rotate":
                shift = block % len(members)
                members = members[shift:] + members[:shift]
            elif rng is not None:
                rng.shuffle(members)
            first = len(planned)
            planned.extend(PlannedRun(first + i, block + 1, arm, scenario) for i, arm in enumerate(members))
    return planned


def existing_records(matrix_dir: Path) -> list[dict[str, Any]]:
    """run.json (or run.partial.json) of every run already in a matrix directory."""
    records = []
    if not matrix_dir.is_dir():
        return records
    for run_dir in sorted(p for p in matrix_dir.iterdir() if p.is_dir()):
        for name in (results.RUN_FILE, results.PARTIAL_FILE):
            path = run_dir / name
            if path.is_file():
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError) as exc:
                    log.warning("cannot read %s: %s", path, exc)
                break
    return records


def mixing_problems(
    records: Sequence[dict[str, Any]], machine: Machine, fingerprint_id: str, scenarios: dict[str, Scenario]
) -> list[str]:
    """Reasons a matrix directory must not receive new runs: rule R3 (one placement per table) and R1 (one scenario)."""
    problems: list[str] = []

    def add(text: str) -> None:
        if text not in problems:
            problems.append(text)

    for record in records:
        recorded_machine = record.get("machine") or {}
        profile = recorded_machine.get("profile_sha256")
        if profile and profile != machine.profile_sha256:
            add("it holds runs made with another machine profile (CPU sets, priority, runtime, heap or gates)")
        fingerprint = recorded_machine.get("fingerprint_id")
        if fingerprint and fingerprint != fingerprint_id:
            add("it holds runs made on another machine")
        recorded_scenario = record.get("scenario") or {}
        name = recorded_scenario.get("name")
        if name in scenarios and recorded_scenario.get("sha256") != scenarios[name].sha256:
            add(f"it holds runs of an earlier version of scenario {name}")
    return problems


@dataclass
class _MatrixProgress:
    finals: list[tuple[PlannedRun, int]] = field(default_factory=list)


def run_matrix(args: argparse.Namespace) -> int:
    """``bbench run``: execute a matrix, one ``run-one`` subprocess per run."""
    matrix = config.load_matrix(args.matrix)
    machine = config.with_priority(config.load_machine(args.machine), matrix.priority_override)
    repeats = args.repeats if args.repeats is not None else matrix.repeats
    if repeats < 1:
        raise ConfigError("--repeats must be at least 1")
    order = args.order or matrix.order
    plan = plan_order(matrix, repeats=repeats, order=order, only=args.only or ())
    if not plan:
        raise ConfigError(f"matrix {matrix.name} plans no runs")
    scenarios = {name: config.load_scenario(name) for name in matrix.scenarios}
    for arm in {run.arm.id: run.arm for run in plan}.values():
        adapter = adapters.create(arm, machine)
        check_platform(adapter)
    out = args.out or DEFAULT_OUT
    matrix_dir = out / matrix.name

    problems = mixing_problems(
        existing_records(matrix_dir), machine, provenance.machine_fingerprint()["fingerprint_id"], scenarios
    )
    if problems:
        for problem in problems:
            _say(f"bbench run: refusing to add runs to {matrix_dir.as_posix()}: {problem}")
        _say("bbench run: use another --out directory")
        return ExitCode.USAGE

    if args.dry_run:
        print(f"matrix {matrix.name}: {len(plan)} runs, order {order}, results in {matrix_dir.as_posix()}")
        print(PlannedRun.HEADER)
        for run in plan:
            print(run.describe())
        return ExitCode.OK

    lock = MachineLock(label=f"matrix {matrix.name}")
    if not lock.acquire(timeout=0):
        info = holder(lock.path)
        _say(f"bbench run: the machine lock is held by {info.describe() if info else 'another process'}")
        return ExitCode.ENV_GATE
    try:
        code = _prepare_matrix(matrix, plan, machine)
        if code != ExitCode.OK:
            return code
        progress = _MatrixProgress()
        code = _execute_plan(args, matrix, plan, order, out, lock, progress)
    finally:
        lock.release()
    _summarize(matrix, progress)
    _write_report(matrix_dir)
    return code


def _prepare_matrix(matrix: Matrix, plan: Sequence[PlannedRun], machine: Machine) -> int:
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
            for name in {run.scenario for run in plan if run.arm.id == arm.id}:
                scenario = config.load_scenario(name)
                bot_command(adapter, scenario, machine)
                template_for(cache, arm, scenario)
            _say(f"fetching {arm.id}")
            adapter.fetch(cache)
        except (FetchError, bots.BotError) as exc:
            _say(f"bbench run: {arm.id}: {exc}")
            return ExitCode.ENV_GATE
    _say(f"matrix {matrix.name}: {len(plan)} runs")
    return ExitCode.OK


def _child_command(args: argparse.Namespace, matrix: Matrix, run: PlannedRun, out: Path) -> list[str]:
    command = [sys.executable, "-m", "bedrock_bench", *["-v"] * min(getattr(args, "verbose", 0), 2)]
    command += [
        "run-one",
        "--matrix",
        str(matrix.path.resolve()),
        "--arm",
        run.arm.id,
        "--scenario",
        run.scenario,
        "--repeat",
        str(run.repeat),
        "--order-index",
        str(run.order_index),
        "--out",
        str(out),
    ]
    if args.machine:
        command += ["--machine", str(config.input_file(args.machine, "machine profile").resolve())]
    return command


def _execute_plan(
    args: argparse.Namespace,
    matrix: Matrix,
    plan: Sequence[PlannedRun],
    order: str,
    out: Path,
    lock: MachineLock,
    progress: _MatrixProgress,
) -> int:
    env = {**os.environ, **lock.child_env(), ENV_MATRIX_ORDER: order}
    started = False
    for run in plan:
        attempts = 0
        while True:
            if started and matrix.cooldown_s > 0:
                _say(f"cooldown {matrix.cooldown_s:g} s")
                time.sleep(matrix.cooldown_s)
            started = True
            _say(f"[{run.order_index + 1}/{len(plan)}] {run.arm.id} {run.scenario} r{run.repeat}")
            try:
                code = subprocess.run(_child_command(args, matrix, run, out), env=env, check=False).returncode
            except KeyboardInterrupt:
                _say("bbench run: interrupted")
                return ExitCode.INTERRUPTED
            if code in (ExitCode.USAGE, ExitCode.ENV_GATE, ExitCode.INTERRUPTED):
                _say(f"bbench run: stopping the matrix (run-one exited with {code})")
                return code
            if code not in (ExitCode.OK, ExitCode.SERVER_FAILED, ExitCode.INVALID):
                log.error("run-one exited with unexpected code %s; counting the run as invalid", code)
                code = ExitCode.INVALID
            if code == ExitCode.INVALID and attempts < matrix.rerun_invalid:
                attempts += 1
                _say(f"invalid run; rerunning ({attempts}/{matrix.rerun_invalid})")
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


def _summarize(matrix: Matrix, progress: _MatrixProgress) -> None:
    if not progress.finals:
        return
    counts: dict[str, int] = {}
    for _, code in progress.finals:
        status = _STATUS_OF_CODE.get(ExitCode(code), Status.INVALID).value
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    _say(f"matrix {matrix.name}: {summary}")


def _write_report(matrix_dir: Path) -> None:
    if not matrix_dir.is_dir() or not any(matrix_dir.iterdir()):
        return
    try:
        from bedrock_bench import report
    except ImportError:
        return
    try:
        written = report.write(matrix_dir)
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
    arm = config.arm_from_ref(args.server, variant=args.variant, artifact=args.artifact)
    machine = config.load_machine(args.machine)
    adapter = adapters.create(arm, machine)
    request = RunRequest(
        arm=arm,
        scenario=config.load_scenario(SMOKE_SCENARIO),
        machine=machine,
        out=args.out or DEFAULT_OUT,
        extra_commands=tuple(chaos_smoke_commands(adapter)),
    )
    result = execute(request)
    if result.run_dir is None:
        return result.exit_code
    if result.exit_code != ExitCode.OK:
        _say(f"smoke {arm.id}: FAILED (run status {result.outcome.status.value if result.outcome else '?'})")
        return result.exit_code
    problems = smoke_problems(result.run_dir, adapter, request.extra_commands)
    if problems:
        _say(f"smoke {arm.id}: FAILED")
        for problem in problems:
            _say(f"  {problem}")
        return ExitCode.INVALID
    _say(f"smoke {arm.id}: passed")
    return ExitCode.OK


def chaos_smoke_commands(adapter: ServerAdapter) -> list[str]:
    """One console command per supported chaos action."""
    return list(chaos.smoke_commands(adapter))


def smoke_problems(run_dir: Path, adapter: ServerAdapter, extra_commands: Sequence[str] = ()) -> list[str]:
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
    texts = {c.get("text") for c in sent if not c.get("error")}
    for text in extra_commands:
        if text not in texts:
            problems.append(f"chaos command {text!r} was never sent")
    return list(dict.fromkeys(problems))


__all__ = [
    "DEFAULT_OUT",
    "ENV_MATRIX_ORDER",
    "PlannedRun",
    "RunRequest",
    "RunResult",
    "backend",
    "check_platform",
    "execute",
    "existing_records",
    "mixing_problems",
    "plan_order",
    "preflight",
    "run_matrix",
    "run_one",
    "smoke",
    "smoke_problems",
    "template_for",
]
