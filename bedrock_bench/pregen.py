# SPDX-License-Identifier: Apache-2.0
"""Pre-generated world templates: ``bbench prepare-world <server@version> --scenario S``.

A template scenario (``[world] source = "template"``, e.g. ``spread100-pregen``) runs a server on a world it generated
itself beforehand, so the measured run delivers chunks without generating them. Each server builds its own template
through its own pipeline; worlds are never copied between servers, because their formats and generators differ.

- **Plugin path.** An adapter with ``pregen_command`` (Nukkit) has a helper that populates every chunk within
  ``template_radius`` of each grid cell, reloads those chunks from storage and prints, per cell, how many it found.
- **Bot tour**, for every other server. A few bots visit the grid cells in rounds while the server's view distance is
  raised to ``template_radius``; the server generates what they see through its normal player pipeline and saves it on
  stop. A cell's coverage is the share of the view disc the bot standing on it received.

The template is accepted only when every cell reaches ``min_coverage`` (0.99 by default). It is stored in the cache at
``Cache.template_dir(server, version, scenario)`` with its report next to it (``<template>.json``): per-cell coverage,
the world parameters it was built for and its tree hash. Runs refuse a template whose tree or world parameters no
longer match. Logs of the build go to ``runs/prepare-world/<id>/``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bedrock_bench import __version__, config, fetch
from bedrock_bench.config import ConfigError
from bedrock_bench.model import ExitCode, Grid, LogLine, PhaseSpec, Scenario
from bedrock_bench.provenance import tree_sha256

if TYPE_CHECKING:
    from bedrock_bench.adapters.base import ServerAdapter
    from bedrock_bench.cache import Cache
    from bedrock_bench.console import Console
    from bedrock_bench.model import Install, Machine, Placement
    from bedrock_bench.process import Launched

log = logging.getLogger(__name__)

SCHEMA = "bedrock-bench/template/1"
LOG_ROOT = Path("runs") / "prepare-world"
TOUR_BOTS = 10
TOUR_PREFIX = "t"
PLUGIN_TIMEOUT_S = 4 * 3600.0

PIPELINE_PLUGIN = "plugin"
PIPELINE_TOUR = "bot_tour"


class TemplateError(RuntimeError):
    """A template could not be built: the server failed, or the bots could not tour the grid."""


# --------------------------------------------------------------------------------------------------------------------
# Grid geometry
# --------------------------------------------------------------------------------------------------------------------


def disc(radius: int) -> int:
    """Chunks within ``radius`` of a centre chunk (``dx² + dz² <= radius²``): 317 at radius 10, 441 at 12."""
    return sum(
        1 for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1) if dx * dx + dz * dz <= radius**2
    )


def cell_block(index: int, grid: Grid) -> tuple[int, int]:
    """Block coordinates of a grid cell centre."""
    return grid.origin + (index % grid.columns) * grid.spacing, grid.origin + (index // grid.columns) * grid.spacing


def chunk_of(block: float) -> int:
    return int(block // 16)


def world_key(scenario: Scenario) -> dict[str, Any]:
    """The world parameters a template is built for; a run checks them against its own scenario."""
    world, grid = scenario.world, scenario.grid
    return {
        "seed": world.seed,
        "level_name": world.level_name,
        "radius": world.template_radius,
        "cells": scenario.bots.n,
        "grid": {"columns": grid.columns, "spacing": grid.spacing, "origin": grid.origin},
    }


# --------------------------------------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CellCoverage:
    index: int
    x: int
    z: int
    found: int
    expected: int

    @property
    def coverage(self) -> float:
        return min(1.0, self.found / self.expected) if self.expected else 0.0


@dataclass(frozen=True)
class TemplateReport:
    """What ``<template>.json`` records about a template."""

    server: str
    version: str
    scenario: str
    pipeline: str
    world: Mapping[str, Any]
    min_coverage: float
    cells: tuple[CellCoverage, ...]
    tree_sha256: str | None = None
    variant: str | None = None
    seconds: float | None = None
    harness: str = __version__

    @property
    def coverage_min(self) -> float:
        return min((cell.coverage for cell in self.cells), default=0.0)

    def short_cells(self) -> list[CellCoverage]:
        """Cells below ``min_coverage``."""
        return [cell for cell in self.cells if cell.coverage < self.min_coverage]

    def problems(self) -> list[str]:
        problems = []
        if len(self.cells) != int(self.world.get("cells", len(self.cells))):
            problems.append(f"{len(self.cells)} cells reported, {self.world.get('cells')} expected")
        short = self.short_cells()
        if short:
            worst = min(short, key=lambda cell: cell.coverage)
            problems.append(
                f"{len(short)} of {len(self.cells)} cells below {self.min_coverage:.0%} coverage "
                f"(worst: cell {worst.index} at {worst.coverage:.1%}, {worst.found}/{worst.expected} chunks)"
            )
        return problems

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "server": self.server,
            "version": self.version,
            "variant": self.variant,
            "scenario": self.scenario,
            "pipeline": self.pipeline,
            "world": dict(self.world),
            "min_coverage": self.min_coverage,
            "coverage_min": round(self.coverage_min, 6),
            "cells": [[c.index, c.x, c.z, c.found, c.expected] for c in self.cells],
            "tree_sha256": self.tree_sha256,
            "seconds": None if self.seconds is None else round(self.seconds, 1),
            "harness": self.harness,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> TemplateReport:
        if data.get("schema") != SCHEMA:
            raise ValueError(f"not a {SCHEMA} report")
        return cls(
            server=str(data["server"]),
            version=str(data["version"]),
            variant=data.get("variant"),
            scenario=str(data["scenario"]),
            pipeline=str(data["pipeline"]),
            world=dict(data["world"]),
            min_coverage=float(data["min_coverage"]),
            cells=tuple(CellCoverage(*(int(v) for v in row)) for row in data["cells"]),
            tree_sha256=data.get("tree_sha256"),
            seconds=data.get("seconds"),
            harness=str(data.get("harness", "")),
        )


def report_path(template: Path) -> Path:
    return template.with_name(template.name + ".json")


def load_report(template: Path) -> TemplateReport:
    path = report_path(template)
    try:
        return TemplateReport.from_json(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise ConfigError(f"the template at {template} has no report; build it with bbench prepare-world") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"unreadable template report {path}: {exc}") from None


def verified_template(template: Path, scenario: Scenario | None = None) -> TemplateReport:
    """The report of a template that is unchanged since it was validated (and built for ``scenario``'s world)."""
    report = load_report(template)
    problems = report.problems()
    if problems:
        raise ConfigError(f"the template at {template} failed validation: {'; '.join(problems)}")
    actual = tree_sha256(template)
    if actual != report.tree_sha256:
        raise ConfigError(f"the template at {template} changed after it was validated; delete it and build it again")
    if scenario is not None and dict(report.world) != world_key(scenario):
        raise ConfigError(
            f"the template at {template} was built for other world parameters ({report.world}); "
            "delete it and build it again"
        )
    return report


# --------------------------------------------------------------------------------------------------------------------
# Plugin path
# --------------------------------------------------------------------------------------------------------------------

PLUGIN_CELL = re.compile(r"\bpregen: cell (?P<i>\d+) (?P<found>\d+) (?P<expected>\d+)\b")
PLUGIN_DONE = re.compile(r"\bpregen: done (?P<cells>\d+) cells\b")


def plugin_cells(lines: Sequence[LogLine], grid: Grid, cells: int) -> list[CellCoverage]:
    """Per-cell counts from a helper's ``pregen`` output; cells it did not report count as empty."""
    counts: dict[int, tuple[int, int]] = {}
    for line in lines:
        match = PLUGIN_CELL.search(line.text)
        if match is not None:
            counts[int(match["i"])] = (int(match["found"]), int(match["expected"]))
    out = []
    for index in range(cells):
        x, z = cell_block(index, grid)
        found, expected = counts.get(index, (0, 0))
        out.append(CellCoverage(index, x, z, found, expected))
    return out


# --------------------------------------------------------------------------------------------------------------------
# Bot tour
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TourSettings:
    radius: int
    min_coverage: float
    min_dwell_s: float = 20.0  # never leave a cell sooner
    stall_s: float = 30.0  # leave when no bot of the round received a new chunk for this long
    max_dwell_s: float = 300.0  # leave anyway
    retp_after_s: float = 20.0  # teleport again when a bot has not arrived by then
    poll_s: float = 5.0
    arrive_chunks: int = 4  # a bot within this many chunks of its cell centre is on the cell


@dataclass
class _Visit:
    bot: str
    index: int
    chunk: tuple[int, int]
    in_view: int = 0
    arrived: bool = False
    retp: int = 0


@dataclass
class BotTour:
    """Moves bots across the grid in rounds and measures each cell's coverage from the bots' progress rows.

    ``send_tp(bot, x, z)`` teleports a bot above a cell centre; ``rows()`` returns the latest progress row per bot
    (``[bot, uniq, in_view, x, y, z, dim, ...]``). Round 0 expects the bots to have been placed on cells 0..n-1
    already, which is what the harness's placer does when they join.
    """

    cells: Sequence[tuple[int, int]]
    bots: Sequence[str]
    settings: TourSettings
    send_tp: Callable[[str, int, int], None]
    rows: Callable[[], Mapping[str, Sequence[Any]]]
    alive: Callable[[], bool] = lambda: True
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    progress: Callable[[str], None] = lambda text: None
    expected: int = field(init=False)

    def __post_init__(self) -> None:
        if not self.bots:
            raise ValueError("a tour needs at least one bot")
        self.expected = disc(self.settings.radius)

    def run(self) -> list[CellCoverage]:
        found: dict[int, int] = {}
        rounds = range(0, len(self.cells), len(self.bots))
        for number, first in enumerate(rounds):
            indices = range(first, min(len(self.cells), first + len(self.bots)))
            visits = [self._visit(self.bots[j], index) for j, index in enumerate(indices)]
            if number > 0:
                for visit in visits:
                    self.send_tp(visit.bot, *self.cells[visit.index])
            found.update(self._dwell(visits))
            done = min(len(self.cells), first + len(self.bots))
            low = min((found[v.index] for v in visits), default=0)
            self.progress(f"round {number + 1}/{len(rounds)}: cells {done}/{len(self.cells)}, lowest {low} chunks")
        return [
            CellCoverage(i, x, z, min(found.get(i, 0), self.expected), self.expected)
            for i, (x, z) in enumerate(self.cells)
        ]

    def _visit(self, bot: str, index: int) -> _Visit:
        x, z = self.cells[index]
        return _Visit(bot, index, (chunk_of(x), chunk_of(z)))

    def _dwell(self, visits: list[_Visit]) -> dict[int, int]:
        s = self.settings
        start = last_growth = self.clock()
        target = s.min_coverage * self.expected
        while True:
            self.sleep(s.poll_s)
            if not self.alive():
                raise TemplateError("the server exited during the bot tour")
            now = self.clock()
            rows = self.rows()
            for visit in visits:
                in_view = self._in_view(visit, rows.get(visit.bot))
                if in_view > visit.in_view:
                    visit.in_view = in_view
                    last_growth = now
                if not visit.arrived and visit.retp == 0 and now - start >= s.retp_after_s:
                    visit.retp += 1
                    self.send_tp(visit.bot, *self.cells[visit.index])
            elapsed = now - start
            if elapsed < s.min_dwell_s:
                continue
            if all(v.in_view >= target for v in visits) or now - last_growth >= s.stall_s or elapsed >= s.max_dwell_s:
                return {v.index: v.in_view for v in visits}

    def _in_view(self, visit: _Visit, row: Sequence[Any] | None) -> int:
        """The bot's in-view chunk count if its last reported position is on its cell, else 0."""
        if row is None or len(row) < 6 or row[3] is None or row[5] is None:
            return 0
        dx = chunk_of(float(row[3])) - visit.chunk[0]
        dz = chunk_of(float(row[5])) - visit.chunk[1]
        if max(abs(dx), abs(dz)) > self.settings.arrive_chunks:
            return 0
        visit.arrived = True
        return int(row[2] or 0)


def tour_scenario(scenario: Scenario, bots: int) -> Scenario:
    """The scenario the tour server and bots run: view distance raised to the template radius, a fresh world."""
    world = dataclasses.replace(scenario.world, source="fresh")
    server = dataclasses.replace(scenario.server, view_distance=scenario.world.template_radius)
    bot_params = dataclasses.replace(scenario.bots, n=bots, prefix=TOUR_PREFIX)
    return dataclasses.replace(scenario, world=world, server=server, bots=bot_params)


def latest_rows(event: Mapping[str, Any] | None) -> dict[str, Sequence[Any]]:
    """Progress rows by bot name from a ``progress`` event."""
    if not event:
        return {}
    return {str(row[0]): row for row in event.get("rows") or () if row}


# --------------------------------------------------------------------------------------------------------------------
# Building a template
# --------------------------------------------------------------------------------------------------------------------


def build_template(
    adapter: ServerAdapter,
    install: Install,
    scenario: Scenario,
    machine: Machine,
    placement: Placement,
    work_dir: Path,
    log_dir: Path,
    *,
    say: Callable[[str], None] = print,
) -> TemplateReport:
    """Run the server once on a fresh world in ``work_dir``, generate the template and return its report.

    The world folder of the server copy in ``work_dir`` holds the template; the caller decides whether to keep it.
    """
    from bedrock_bench import process
    from bedrock_bench.console import Console
    from bedrock_bench.model import RunContext
    from bedrock_bench.phases import bot_command

    plugin = getattr(adapter, "pregen_command", None)
    pipeline = PIPELINE_PLUGIN if callable(plugin) else PIPELINE_TOUR
    run_scenario = tour_scenario(scenario, min(TOUR_BOTS, scenario.bots.n))
    bot_argv = None if callable(plugin) else bot_command(adapter, run_scenario, machine).argv
    ctx = RunContext(
        run_id=log_dir.name,
        run_dir=log_dir,
        work_dir=work_dir,
        arm=adapter.arm,
        scenario=run_scenario,
        machine=machine,
        placement=placement,
    )
    layout = adapter.prepare(install, ctx)
    spec = adapter.launch(layout)
    started = time.monotonic()
    launched = process.launch(spec, placement, role="server")
    console = Console(
        launched.stdout,
        launched.stdin,
        adapter.dialect,
        log_path=log_dir / "server.log.gz",
        journal_path=log_dir / "commands.jsonl.gz",
    )
    console.start()
    for line in spec.stdin_lines:
        console.send(line, origin="phase")
    try:
        adapter.server_process(launched.process)
        boot_cap = _phase(scenario, "boot").cap_s or 600.0
        if not console.wait_ready(timeout=boot_cap):
            raise TemplateError(f"the server was not ready within {boot_cap:g} s")
        say("server ready")
        if bot_argv is None:
            assert callable(plugin)
            cells = _plugin_pass(console, plugin, scenario, say)
        else:
            cells = _tour_pass(adapter, console, launched, bot_argv, run_scenario, scenario, placement, log_dir, say)
    finally:
        _stop(console, launched, adapter.dialect.stop_cmd, float(_phase(scenario, "teardown")["stop_timeout_s"]))
    world_dir = layout.server_dir / "worlds" / scenario.world.level_name
    if not world_dir.is_dir() or not any(world_dir.iterdir()):
        raise TemplateError(f"the server left no world at worlds/{scenario.world.level_name}")
    return TemplateReport(
        server=adapter.name,
        version=adapter.arm.version,
        variant=adapter.arm.variant,
        scenario=scenario.name,
        pipeline=pipeline,
        world=world_key(scenario),
        min_coverage=scenario.world.min_coverage,
        cells=tuple(cells),
        seconds=time.monotonic() - started,
    )


def _phase(scenario: Scenario, name: str) -> PhaseSpec:
    spec = scenario.phase(name)
    if spec is None:
        raise ConfigError(f"scenario {scenario.name} has no {name} phase")
    return spec


def _plugin_pass(
    console: Console, command: Callable[..., str], scenario: Scenario, say: Callable[[str], None]
) -> list[CellCoverage]:
    start = console.mark()
    console.send(command(scenario.world.template_radius, scenario.grid, scenario.bots.n), origin="phase")
    say("generating through the server's helper")
    if console.wait_for(PLUGIN_DONE, after=start, timeout=PLUGIN_TIMEOUT_S) is None:
        raise TemplateError("the helper did not finish pre-generation: the server exited or the time limit passed")
    return plugin_cells(console.lines(start), scenario.grid, scenario.bots.n)


def _tour_pass(
    adapter: ServerAdapter,
    console: Console,
    launched: Launched,
    bot_argv: Sequence[str],
    run_scenario: Scenario,
    scenario: Scenario,
    placement: Placement,
    log_dir: Path,
    say: Callable[[str], None],
) -> list[CellCoverage]:
    from bedrock_bench import bots as botlib
    from bedrock_bench.phases import Placer

    placer = Placer(adapter, run_scenario, console)
    console.subscribe(placer.on_line)
    radius = scenario.world.template_radius
    with (log_dir / "bots.jsonl").open("w", encoding="utf-8") as events:

        def on_event(event: dict[str, Any]) -> None:
            placer.on_bot_event(event)
            events.write(json.dumps(event, separators=(",", ":")) + "\n")

        fleet = botlib.BotFleet(bot_argv, cpus=placement.bot_cpus, on_event=on_event, stderr_path=log_dir / "bots.log")
        fleet.start()
        try:
            joined = fleet.wait_for("alljoined", timeout_s=_phase(run_scenario, "join").cap_s or 300.0)
            if joined is None or int(joined.get("joined", 0)) < run_scenario.bots.n:
                raise TemplateError(f"the tour bots did not all join: {joined}")
            granted = [int(e.get("radius", 0)) for e in fleet.events("spawned")]
            if granted and min(granted) < radius:
                raise TemplateError(f"the server granted a view of {min(granted)} chunks, below the template radius")
            say(f"{run_scenario.bots.n} tour bots joined; touring {scenario.bots.n} cells at radius {radius}")
            tour = BotTour(
                cells=[cell_block(i, scenario.grid) for i in range(scenario.bots.n)],
                bots=[f"{TOUR_PREFIX}{j:02d}" for j in range(run_scenario.bots.n)],
                settings=TourSettings(radius=radius, min_coverage=scenario.world.min_coverage),
                send_tp=lambda bot, x, z: console.send(adapter.cmd_tp(bot, x, scenario.grid.y, z), origin="phase"),
                rows=lambda: latest_rows(fleet.last("progress")),
                alive=lambda: launched.poll() is None,
                progress=say,
            )
            return tour.run()
        finally:
            fleet.stop()


def _stop(console: Console, launched: Launched, stop_cmd: str, timeout_s: float) -> None:
    if launched.poll() is None:
        console.send(stop_cmd, origin="phase")
        if launched.wait(timeout_s) is None:
            log.warning("the server did not stop within %.0f s; killing it", timeout_s)
            launched.kill_tree()
    console.close()
    launched.close()


def store_template(cache: Cache, final: Path, world_dir: Path, report: TemplateReport) -> TemplateReport:
    """Move a validated world into the cache as a read-only template and write its report next to it."""
    with cache.staging(final) as tmp:
        fetch.copy_install(world_dir, tmp)
        fetch.make_read_only(tmp)
    stored = dataclasses.replace(report, tree_sha256=tree_sha256(final))
    report_path(final).write_text(json.dumps(stored.to_json(), indent=1) + "\n", encoding="utf-8")
    return stored


# --------------------------------------------------------------------------------------------------------------------
# bbench prepare-world
# --------------------------------------------------------------------------------------------------------------------


def prepare_world(args: argparse.Namespace) -> int:
    """``bbench prepare-world <server@version> --scenario S [--machine M] [--variant V]``."""
    from bedrock_bench import adapters, bots, host
    from bedrock_bench.cache import Cache, remove_tree
    from bedrock_bench.lock import MachineLock, holder

    def say(text: str) -> None:
        print(f"prepare-world: {text}", file=sys.stderr, flush=True)

    arm = config.arm_from_ref(args.server, variant=args.variant)
    scenario = config.load_scenario(args.scenario)
    if scenario.world.source != "template":
        raise ConfigError(f'scenario {scenario.name} does not use a world template ([world] source = "template")')
    machine = config.load_machine(args.machine)
    adapter = adapters.create(arm, machine)
    cache = Cache()
    final = cache.template_dir(arm.server, arm.version, scenario.name)
    if final.exists():
        report = verified_template(final, scenario)
        say(f"{arm.ref} already has a valid template for {scenario.name} (lowest cell {report.coverage_min:.1%})")
        return ExitCode.OK

    lock = MachineLock(label=f"prepare-world {arm.id} {scenario.name}")
    if not lock.acquire(timeout=0):
        info = holder(lock.path)
        say(f"the machine lock is held by {info.describe() if info else 'another process'}")
        return ExitCode.ENV_GATE
    work_dir = cache.work_dir(f"prepare-world-{arm.id}-{scenario.name}-{time.time_ns()}")
    try:
        try:
            placement = host.plan_placement(machine, host.detect_topology())
            install = adapter.fetch(cache)
        except (host.PlacementError, fetch.FetchError) as exc:
            say(str(exc))
            return ExitCode.ENV_GATE
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        log_dir = LOG_ROOT / f"{stamp}-{arm.id}-{scenario.name}"
        log_dir.mkdir(parents=True, exist_ok=True)
        say(f"building the {scenario.name} template of {arm.id}; logs in {log_dir.as_posix()}")
        try:
            report = build_template(adapter, install, scenario, machine, placement, work_dir, log_dir, say=say)
        except bots.BotError as exc:
            say(str(exc))
            return ExitCode.ENV_GATE
        except TemplateError as exc:
            say(f"failed: {exc}")
            return ExitCode.SERVER_FAILED
        (log_dir / "report.json").write_text(json.dumps(report.to_json(), indent=1) + "\n", encoding="utf-8")
        problems = report.problems()
        if problems:
            for problem in problems:
                say(f"rejected: {problem}")
            return ExitCode.INVALID
        stored = store_template(cache, final, work_dir / "worlds" / scenario.world.level_name, report)
        tree = (stored.tree_sha256 or "?")[:12]
        say(f"template stored: {len(stored.cells)} cells, lowest {stored.coverage_min:.1%}, tree {tree}")
        return ExitCode.OK
    finally:
        lock.release()
        remove_tree(work_dir)


__all__ = [
    "BotTour",
    "CellCoverage",
    "TemplateError",
    "TemplateReport",
    "TourSettings",
    "build_template",
    "cell_block",
    "disc",
    "latest_rows",
    "load_report",
    "plugin_cells",
    "prepare_world",
    "report_path",
    "store_template",
    "tour_scenario",
    "verified_template",
    "world_key",
]
