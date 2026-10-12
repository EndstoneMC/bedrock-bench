# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench import config, pregen
from bedrock_bench.cache import Cache
from bedrock_bench.config import ConfigError
from bedrock_bench.grid import cell
from bedrock_bench.model import ExitCode, Grid, LogLine
from bedrock_bench.pregen import BotTour, CellCoverage, TemplateReport, TourSettings


def line(text: str) -> LogLine:
    return LogLine(t_ns=0, epoch_ms=0, level="info", logger=None, text=text, raw=text)


def scenario(**world: Any) -> Any:
    return config.parse_scenario({"server": {"csg": False}, "world": {"source": "template", **world}}, name="tpl")


def report_for(sc: Any, *, found: int = 441, server: str = "nukkit") -> TemplateReport:
    cells = tuple(CellCoverage(i, *pregen.cell_block(i, sc.grid), found, 441) for i in range(sc.bots.n))
    return TemplateReport(
        server=server,
        version="b3caad6",
        scenario=sc.name,
        pipeline=pregen.PIPELINE_PLUGIN,
        world=pregen.world_key(sc),
        min_coverage=sc.world.min_coverage,
        cells=cells,
    )


# --------------------------------------------------------------------------------------------------------------------
# Geometry and reports
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("radius", "chunks"), [(0, 1), (1, 5), (4, 49), (10, 317), (12, 441)])
def test_disc(radius: int, chunks: int) -> None:
    assert pregen.disc(radius) == chunks


def test_cells_match_the_spread_grid() -> None:
    grid = Grid()
    assert [pregen.cell_block(i, grid) for i in range(25)] == [cell(i, grid) for i in range(25)]
    assert pregen.chunk_of(1280.5) == 80 and pregen.chunk_of(-0.5) == -1


def test_world_key() -> None:
    key = pregen.world_key(scenario())
    assert key == {
        "seed": 20261010,
        "level_name": "bench",
        "radius": 12,
        "cells": 100,
        "grid": {"columns": 11, "spacing": 640, "origin": 1280},
    }


class TestReport:
    def test_round_trip(self) -> None:
        report = report_for(scenario())
        data = json.loads(json.dumps(report.to_json()))
        again = TemplateReport.from_json(data)
        assert again == report
        assert data["schema"] == "bedrock-bench/template/1" and data["coverage_min"] == 1.0

    def test_problems(self) -> None:
        sc = scenario()
        report = report_for(sc)
        assert report.problems() == []
        cells = list(report.cells)
        cells[7] = CellCoverage(7, 0, 0, 400, 441)
        short = TemplateReport(**{**report.__dict__, "cells": tuple(cells)})
        assert short.short_cells() == [cells[7]]
        assert "1 of 100 cells below 99% coverage (worst: cell 7 at 90.7%, 400/441 chunks)" in short.problems()[0]
        missing = TemplateReport(**{**report.__dict__, "cells": report.cells[:99]})
        assert "99 cells reported, 100 expected" in missing.problems()

    def test_unknown_schema(self) -> None:
        with pytest.raises(ValueError):
            TemplateReport.from_json({"schema": "other"})


class TestStoredTemplates:
    def make_world(self, root: Path) -> Path:
        world = root / "built" / "bench"
        (world / "db").mkdir(parents=True)
        (world / "db" / "CURRENT").write_text("MANIFEST-000001\n")
        (world / "level.dat").write_bytes(b"\x0a\x00")
        return world

    def test_store_and_verify(self, tmp_path: Path) -> None:
        sc = scenario()
        cache = Cache(tmp_path / "cache")
        final = cache.template_dir("nukkit", "b3caad6", sc.name)
        stored = pregen.store_template(cache, final, self.make_world(tmp_path), report_for(sc))
        assert (final / "level.dat").is_file() and pregen.report_path(final).is_file()
        assert pregen.verified_template(final, sc) == stored
        assert pregen.load_report(final).tree_sha256 == stored.tree_sha256

    def test_changed_tree_is_refused(self, tmp_path: Path) -> None:
        sc = scenario()
        cache = Cache(tmp_path / "cache")
        final = cache.template_dir("nukkit", "b3caad6", sc.name)
        pregen.store_template(cache, final, self.make_world(tmp_path), report_for(sc))
        assert stat.S_IMODE((final / "level.dat").stat().st_mode) & 0o222 == 0, "templates are read-only"
        os.chmod(final / "level.dat", stat.S_IREAD | stat.S_IWRITE)
        (final / "level.dat").write_bytes(b"edited")
        with pytest.raises(ConfigError, match="changed after it was validated"):
            pregen.verified_template(final, sc)

    def test_other_world_parameters_are_refused(self, tmp_path: Path) -> None:
        sc = scenario()
        cache = Cache(tmp_path / "cache")
        final = cache.template_dir("nukkit", "b3caad6", sc.name)
        pregen.store_template(cache, final, self.make_world(tmp_path), report_for(sc))
        with pytest.raises(ConfigError, match="other world parameters"):
            pregen.verified_template(final, scenario(seed=1))

    def test_failed_validation_is_refused(self, tmp_path: Path) -> None:
        sc = scenario()
        cache = Cache(tmp_path / "cache")
        final = cache.template_dir("nukkit", "b3caad6", sc.name)
        pregen.store_template(cache, final, self.make_world(tmp_path), report_for(sc, found=100))
        with pytest.raises(ConfigError, match="failed validation"):
            pregen.verified_template(final, sc)

    def test_missing_report(self, tmp_path: Path) -> None:
        (tmp_path / "tpl").mkdir()
        with pytest.raises(ConfigError, match="no report"):
            pregen.verified_template(tmp_path / "tpl")


# --------------------------------------------------------------------------------------------------------------------
# Plugin path
# --------------------------------------------------------------------------------------------------------------------


def test_plugin_cells() -> None:
    grid = Grid()
    lines = [
        line("[BenchTick] pregen: start 1323 chunks in 1 batches, radius 12"),
        line("[BenchTick] pregen: generated 1323 chunks in 12.5 s"),
        line("[BenchTick] pregen: cell 0 441 441"),
        line("[BenchTick] pregen: cell 2 437 441"),
        line("[BenchTick] pregen: done 3 cells"),
    ]
    cells = pregen.plugin_cells(lines, grid, 3)
    assert [(c.index, c.found, c.expected) for c in cells] == [(0, 441, 441), (1, 0, 0), (2, 437, 441)]
    assert (cells[1].x, cells[1].z) == (1920, 1280)
    assert cells[1].coverage == 0.0 and round(cells[2].coverage, 3) == 0.991


class FakeConsole:
    """Just enough of Console for the plugin pass and the server stop."""

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.received: list[LogLine] = [line("Done (1.0s)!")]
        self.closed = False

    def mark(self) -> int:
        return len(self.received)

    def send(self, text: str, **_: Any) -> None:
        self.sent.append(text)
        self.received += [line(reply) for reply in self.replies]

    def wait_for(self, what: re.Pattern[str], *, after: int, timeout: float) -> LogLine | None:
        return next((entry for entry in self.received[after:] if what.search(entry.text)), None)

    def lines(self, start: int = 0) -> list[LogLine]:
        return self.received[start:]

    def close(self) -> None:
        self.closed = True


def test_plugin_pass() -> None:
    sc = scenario()
    console = FakeConsole(
        [f"[BenchTick] pregen: cell {i} 441 441" for i in range(sc.bots.n)] + ["[BenchTick] pregen: done 100 cells"]
    )
    cells = pregen._plugin_pass(console, lambda r, g, n: f"pregen {r} {g.columns} {n}", sc, lambda _: None)  # type: ignore[arg-type]
    assert console.sent == ["pregen 12 11 100"]
    assert len(cells) == 100 and all(c.coverage == 1.0 for c in cells)


def test_plugin_pass_without_a_finish_line() -> None:
    sc = scenario()
    with pytest.raises(pregen.TemplateError, match="did not finish"):
        pregen._plugin_pass(FakeConsole(["[BenchTick] pregen: cell 0 1 441"]), lambda *a: "pregen", sc, print)  # type: ignore[arg-type]


class FakeLaunched:
    def __init__(self, stops: bool) -> None:
        self.stops = stops
        self.killed = self.closed = False

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int | None:
        return 0 if self.stops else None

    def kill_tree(self) -> list[int]:
        self.killed = True
        return []

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("stops", [True, False])
def test_stop(stops: bool) -> None:
    console, launched = FakeConsole([]), FakeLaunched(stops)
    pregen._stop(console, launched, "stop", 1.0)  # type: ignore[arg-type]
    assert console.sent == ["stop"] and console.closed and launched.closed
    assert launched.killed is not stops


# --------------------------------------------------------------------------------------------------------------------
# Bot tour
# --------------------------------------------------------------------------------------------------------------------


class FakeTourWorld:
    """Bots that receive ``rate`` chunks per second up to ``cap`` after arriving on a cell; time is simulated."""

    def __init__(self, cells: Sequence[tuple[int, int]], bots: Sequence[str], *, rate: float, cap: int) -> None:
        self.t = 0.0
        self.rate, self.cap = rate, cap
        self.at = {bot: (cells[j], 0.0) for j, bot in enumerate(bots)}
        self.sent: list[tuple[str, int, int, float]] = []
        self.stuck: set[str] = set()
        self.alive = True

    def send_tp(self, bot: str, x: int, z: int) -> None:
        self.sent.append((bot, x, z, self.t))
        if bot not in self.stuck:
            self.at[bot] = ((x, z), self.t)

    def rows(self) -> dict[str, list[Any]]:
        out = {}
        for bot, ((x, z), since) in self.at.items():
            in_view = min(self.cap, int(self.rate * (self.t - since)))
            out[bot] = [bot, in_view, in_view, x + 0.5, 300.0, z + 0.5, 0, 0, 1]
        return out

    def tour(self, cells: Sequence[tuple[int, int]], bots: Sequence[str], **settings: Any) -> BotTour:
        return BotTour(
            cells=cells,
            bots=bots,
            settings=TourSettings(radius=12, min_coverage=0.99, **settings),
            send_tp=self.send_tp,
            rows=self.rows,
            alive=lambda: self.alive,
            clock=lambda: self.t,
            sleep=self.advance,
        )

    def advance(self, seconds: float) -> None:
        self.t += seconds


CELLS = [pregen.cell_block(i, Grid()) for i in range(4)]
BOTS = ["t00", "t01"]


def test_tour_covers_every_cell() -> None:
    world = FakeTourWorld(CELLS, BOTS, rate=100.0, cap=441)
    messages: list[str] = []
    tour = world.tour(CELLS, BOTS)
    tour.progress = messages.append
    cells = tour.run()
    assert [c.coverage for c in cells] == [1.0] * 4
    assert [(bot, x, z) for bot, x, z, _ in world.sent] == [("t00", *CELLS[2]), ("t01", *CELLS[3])]
    assert world.sent[0][3] == 20.0, "the second round starts after the minimum dwell"
    assert messages[-1] == "round 2/2: cells 4/4, lowest 441 chunks"


def test_tour_leaves_a_stalled_round() -> None:
    world = FakeTourWorld(CELLS, BOTS, rate=100.0, cap=220)
    cells = world.tour(CELLS, BOTS).run()
    assert {c.found for c in cells} == {220}
    assert world.sent[0][3] == 35.0, "220 chunks at t=5, then 30 s without growth"


def test_tour_retries_a_teleport_and_reports_a_bot_that_never_arrives() -> None:
    cells_ = CELLS[:3]
    world = FakeTourWorld(cells_, BOTS, rate=100.0, cap=441)
    world.stuck.add("t00")
    result = world.tour(cells_, BOTS).run()
    retries = [s for s in world.sent if s[0] == "t00"]
    assert len(retries) == 2 and retries[1][3] - retries[0][3] >= 20.0
    assert result[2].found == 0, "t00 never left cell 0, so cell 2 got nothing"
    assert result[0].found == 441


def test_tour_stops_when_the_server_dies() -> None:
    world = FakeTourWorld(CELLS, BOTS, rate=1.0, cap=441)
    world.alive = False
    with pytest.raises(pregen.TemplateError, match="exited"):
        world.tour(CELLS, BOTS).run()


def test_tour_caps_the_dwell() -> None:
    world = FakeTourWorld(CELLS[:2], BOTS, rate=1.0, cap=441)
    cells = world.tour(CELLS[:2], BOTS, stall_s=1000.0, max_dwell_s=60.0).run()
    assert [c.found for c in cells] == [60, 60]


def test_tour_needs_bots() -> None:
    with pytest.raises(ValueError):
        FakeTourWorld(CELLS, [], rate=1, cap=1).tour(CELLS, [])


def test_tour_scenario() -> None:
    base = config.load_scenario("spread100-pregen")
    tour = pregen.tour_scenario(base, 10)
    assert tour.server.view_distance == base.world.template_radius == 12
    assert tour.world.source == "fresh" and tour.bots.n == 10 and tour.bots.prefix == "t"
    assert tour.grid == base.grid and tour.server.csg is False


def test_latest_rows() -> None:
    event = {
        "ev": "progress",
        "rows": [["t00", 1, 2, 3.0, 4.0, 5.0, 0, 0, 1], ["t01", 0, 0, None, None, None, 0, 0, 0]],
    }
    rows = pregen.latest_rows(event)
    assert set(rows) == {"t00", "t01"} and rows["t00"][2] == 2
    assert pregen.latest_rows(None) == {}


# --------------------------------------------------------------------------------------------------------------------
# bbench prepare-world
# --------------------------------------------------------------------------------------------------------------------


def args(server: str, scenario_name: str) -> argparse.Namespace:
    return argparse.Namespace(server=server, scenario=scenario_name, machine=None, variant=None)


def test_cli_refuses_a_fresh_world_scenario() -> None:
    with pytest.raises(ConfigError, match="does not use a world template"):
        pregen.prepare_world(args("powernukkitx@3.0.5", "spread100"))


def test_cli_keeps_a_valid_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sc = config.load_scenario("spread100-pregen")
    cache = Cache()
    world = tmp_path / "bench"
    world.mkdir()
    (world / "level.dat").write_bytes(b"x")
    final = cache.template_dir("powernukkitx", "3.0.5", sc.name)
    pregen.store_template(cache, final, world, report_for(sc, server="powernukkitx"))
    assert pregen.prepare_world(args("powernukkitx@3.0.5", sc.name)) == ExitCode.OK
    assert "already has a valid template" in capsys.readouterr().err
