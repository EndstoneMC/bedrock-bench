# SPDX-License-Identifier: Apache-2.0
"""The spread grid: where each bot is placed, and whether the bots ended up one per cell (docs/methodology.md#workload).

Bot ``i`` goes to ``cell(i) = (origin + (i % columns) * spacing, origin + (i // columns) * spacing)``. With the default
spacing of 640 blocks (40 chunks, more than two view distances of 10) no two bots share a chunk, which is the worst
case for a server: every player needs its own terrain generated, loaded, ticked and sent.

After settle the harness reads every player's position and maps it to the nearest cell. The spread is valid when every
joined bot occupies its own cell (``cells_occupied == joined`` and ``max_per_cell == 1``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from bedrock_bench.model import PlayerPos
from bedrock_bench.workload import Grid

DEFAULT_GRID = Grid()


def cell(i: int, grid: Grid = DEFAULT_GRID) -> tuple[int, int]:
    """``(x, z)`` of the cell of bot ``i``."""
    if i < 0:
        raise ValueError(f"bot index must not be negative, got {i}")
    return grid.origin + (i % grid.columns) * grid.spacing, grid.origin + (i // grid.columns) * grid.spacing


def nearest_cell(x: float, z: float, n: int, grid: Grid = DEFAULT_GRID) -> tuple[int, float]:
    """The closest of the first ``n`` cells to ``(x, z)`` and the horizontal distance to that cell's point.

    Ties go to the lower index, so the answer never depends on iteration order.
    """
    if n < 1:
        raise ValueError(f"need at least one cell, got {n}")
    best, best_d2 = 0, math.inf
    for i in range(n):
        cx, cz = cell(i, grid)
        d2 = (cx - x) ** 2 + (cz - z) ** 2
        if d2 < best_d2:
            best, best_d2 = i, d2
    return best, math.sqrt(best_d2)


@dataclass(frozen=True)
class Spread:
    """How a set of positions covers the grid."""

    players: int
    cells_occupied: int
    max_per_cell: int
    worst_offset_blocks: float | None  # largest distance from a position to its cell's point; None without positions

    def valid(self, joined: int) -> bool:
        """One bot per cell and every joined bot accounted for."""
        return self.cells_occupied == joined and self.max_per_cell == 1

    def offset_warning(self, grid: Grid = DEFAULT_GRID) -> bool:
        """True when a bot ended up further from its cell than the workload's warning threshold."""
        return self.worst_offset_blocks is not None and self.worst_offset_blocks > grid.warn_offset_blocks

    def to_json(self, source: str | None) -> dict[str, Any]:
        """The ``spread`` block of run.json."""
        return {
            "source": source,
            "players": self.players,
            "cells_occupied": self.cells_occupied,
            "max_per_cell": self.max_per_cell,
            "worst_offset_blocks": self.worst_offset_blocks,
        }


def verify_spread(
    positions: Iterable[PlayerPos | tuple[float, float, float]], n: int, grid: Grid = DEFAULT_GRID
) -> Spread:
    """Map every position to the nearest of the first ``n`` cells and report the occupancy.

    Positions are :class:`~bedrock_bench.model.PlayerPos` or ``(x, y, z)`` tuples; only ``x`` and ``z`` count.
    """
    occupancy: dict[int, int] = {}
    worst: float | None = None
    players = 0
    for pos in positions:
        x, z = (pos.x, pos.z) if isinstance(pos, PlayerPos) else (pos[0], pos[2])
        index, offset = nearest_cell(float(x), float(z), n, grid)
        occupancy[index] = occupancy.get(index, 0) + 1
        worst = offset if worst is None else max(worst, offset)
        players += 1
    return Spread(
        players=players,
        cells_occupied=len(occupancy),
        max_per_cell=max(occupancy.values(), default=0),
        worst_offset_blocks=None if worst is None else round(worst, 2),
    )


__all__ = ["DEFAULT_GRID", "Spread", "cell", "nearest_cell", "verify_spread"]
