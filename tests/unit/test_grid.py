# SPDX-License-Identifier: Apache-2.0
"""The spread grid: cell positions, nearest-cell mapping and the spread check (SPEC.md section 6.1)."""

from __future__ import annotations

import pytest

from bedrock_bench.grid import Spread, cell, nearest_cell, verify_spread
from bedrock_bench.model import Grid, PlayerPos


class TestCell:
    @pytest.mark.parametrize(
        ("i", "expected"),
        [
            (0, (1280, 1280)),
            (1, (1920, 1280)),
            (10, (1280 + 10 * 640, 1280)),
            (11, (1280, 1920)),
            (99, (1280, 1280 + 9 * 640)),
        ],
    )
    def test_default_grid_matches_the_spec_formula(self, i: int, expected: tuple[int, int]) -> None:
        assert cell(i) == expected

    def test_custom_grid(self) -> None:
        grid = Grid(columns=4, spacing=100, origin=0)
        assert [cell(i, grid) for i in (0, 3, 4, 9)] == [(0, 0), (300, 0), (0, 100), (100, 200)]

    def test_negative_index_is_refused(self) -> None:
        with pytest.raises(ValueError):
            cell(-1)


class TestNearestCell:
    def test_exact_cell(self) -> None:
        assert nearest_cell(1920.0, 1920.0, 100) == (12, 0.0)

    def test_offset_is_the_horizontal_distance(self) -> None:
        index, offset = nearest_cell(1280.0 + 3.0, 1280.0 + 4.0, 100)
        assert index == 0
        assert offset == pytest.approx(5.0)

    def test_only_the_first_n_cells_count(self) -> None:
        x, z = cell(50)
        index, offset = nearest_cell(x, z, 11)
        assert index < 11
        assert offset > 0

    def test_a_tie_goes_to_the_lower_index(self) -> None:
        assert nearest_cell(1600.0, 1280.0, 2)[0] == 0

    def test_needs_at_least_one_cell(self) -> None:
        with pytest.raises(ValueError):
            nearest_cell(0.0, 0.0, 0)


class TestVerifySpread:
    def test_one_bot_per_cell_is_valid(self) -> None:
        positions = [PlayerPos(f"a{i:02d}", cell(i)[0] + 0.5, 70.0, cell(i)[1] + 0.5) for i in range(100)]
        spread = verify_spread(positions, 100)
        assert (spread.players, spread.cells_occupied, spread.max_per_cell) == (100, 100, 1)
        assert spread.worst_offset_blocks == pytest.approx(0.71, abs=0.01)
        assert spread.valid(100)
        assert not spread.offset_warning()

    def test_two_bots_in_one_cell_are_caught(self) -> None:
        (x0, z0), (x1, z1) = cell(0), cell(1)
        positions = [(x0, 70.0, z0), (x0 + 10.0, 70.0, z0), (x1, 70.0, z1)]
        spread = verify_spread(positions, 3)
        assert (spread.players, spread.cells_occupied, spread.max_per_cell) == (3, 2, 2)
        assert not spread.valid(3)

    def test_a_missing_bot_fails_against_joined(self) -> None:
        positions = [(cell(i)[0], 300.0, cell(i)[1]) for i in range(3)]
        spread = verify_spread(positions, 4)
        assert spread.valid(3)
        assert not spread.valid(4)

    def test_a_far_bot_raises_the_offset_warning(self) -> None:
        spread = verify_spread([(cell(0)[0] + 250.0, 70.0, cell(0)[1])], 1)
        assert spread.worst_offset_blocks == pytest.approx(250.0)
        assert spread.offset_warning()

    def test_no_positions(self) -> None:
        spread = verify_spread([], 100)
        assert spread == Spread(players=0, cells_occupied=0, max_per_cell=0, worst_offset_blocks=None)
        assert not spread.valid(100)

    def test_run_json_block(self) -> None:
        spread = verify_spread([(1280.0, 70.0, 1280.0)], 1)
        assert spread.to_json("querytarget") == {
            "source": "querytarget",
            "players": 1,
            "cells_occupied": 1,
            "max_per_cell": 1,
            "worst_offset_blocks": 0.0,
        }
