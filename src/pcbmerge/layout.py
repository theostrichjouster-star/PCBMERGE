"""Placing source boards on the merged board.

Two things matter and they pull against each other: the merged board should be
compact, and boards that share a lot of nets should sit next to each other so
the airwires between them are short.  This module measures both, then searches
for an arrangement that does well on the weighted combination.
"""

from __future__ import annotations

import math
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .eagle import bbox

# EAGLE layer 20 is Dimension: the board outline lives there.
DIMENSION_LAYER = "20"

STYLES = ("grid", "row", "column", "pack")


@dataclass
class Placement:
    design: str
    dx: float
    dy: float
    width: float
    height: float
    x: float = 0.0   # left edge after placing
    y: float = 0.0   # bottom edge after placing
    column: int = 0
    row: int = 0


@dataclass
class LayoutStats:
    """What an arrangement costs, for reporting and for comparing runs."""

    airwire: float = 0.0
    width: float = 0.0
    height: float = 0.0
    used_area: float = 0.0
    iterations: int = 0
    improved: int = 0

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def utilization(self) -> float:
        return (self.used_area / self.area * 100.0) if self.area else 0.0


def board_extent(board: ET.Element) -> tuple[float, float, float, float]:
    """Bounds of a board, preferring its dimension-layer outline."""
    outline = ET.Element("outline")
    plain = board.find("plain")
    if plain is not None:
        for node in plain:
            if isinstance(node.tag, str) and node.get("layer") == DIMENSION_LAYER:
                outline.append(node)
    box = bbox(outline) if len(outline) else None
    if box is None:
        # No outline drawn: fall back to everything placed on the board.
        probe = ET.Element("probe")
        for tag in ("elements", "signals", "plain"):
            node = board.find(tag)
            if node is not None:
                probe.append(node)
        box = bbox(probe)
    if box is None:
        return 0.0, 0.0, 0.0, 0.0
    return box


# --------------------------------------------------------------------------
# arrangement
# --------------------------------------------------------------------------

@dataclass
class Board:
    """One board instance waiting to be placed."""

    design: str
    width: float
    height: float
    min_x: float
    min_y: float


def measure(boards: list[tuple[str, ET.Element]]) -> list[Board]:
    out: list[Board] = []
    for name, board in boards:
        x1, y1, x2, y2 = board_extent(board)
        out.append(Board(name, x2 - x1, y2 - y1, x1, y1))
    return out


def place(boards: list[Board], style: str = "grid", gap: float = 5.0,
          columns: int = 0, origin: tuple[float, float] = (0.0, 0.0)) -> list[Placement]:
    """Turn an ordered list of boards into placements."""
    if not boards:
        return []
    if style == "pack":
        return _shelf(boards, gap, origin)
    return _grid(boards, style, gap, columns, origin)


def _grid(boards: list[Board], style: str, gap: float, columns: int,
          origin: tuple[float, float]) -> list[Placement]:
    """Uniform cells: predictable, easy to re-place by hand, wasteful."""
    count = len(boards)
    if style == "row":
        cols = count
    elif style == "column":
        cols = 1
    elif columns > 0:
        cols = columns
    else:
        cols = max(1, math.ceil(math.sqrt(count)))
    cols = max(1, min(cols, count))

    cell_w = max(b.width for b in boards) + gap
    cell_h = max(b.height for b in boards) + gap

    placements: list[Placement] = []
    for index, board in enumerate(boards):
        column, row = index % cols, index // cols
        x = origin[0] + column * cell_w
        y = origin[1] - row * cell_h
        placements.append(Placement(board.design, x - board.min_x, y - board.min_y,
                                    board.width, board.height, x, y, column, row))
    return placements


def _shelf(boards: list[Board], gap: float, origin: tuple[float, float]) -> list[Placement]:
    """Shelf packing: rows sized to their tallest board, no wasted uniform cell.

    The shelf width targets a roughly square result, which is what fabricators
    price best and what fits a panel.
    """
    total_area = sum(b.width * b.height for b in boards)
    widest = max(b.width for b in boards)
    target = max(widest, math.sqrt(total_area) * 1.15)

    placements: list[Placement] = []
    cursor_x = 0.0
    shelf_y = 0.0
    shelf_height = 0.0
    row = 0
    column = 0

    for board in boards:
        if cursor_x > 0 and cursor_x + board.width > target:
            shelf_y -= shelf_height + gap
            cursor_x = 0.0
            shelf_height = 0.0
            row += 1
            column = 0
        x = origin[0] + cursor_x
        y = origin[1] + shelf_y - board.height
        placements.append(Placement(board.design, x - board.min_x, y - board.min_y,
                                    board.width, board.height, x, y, column, row))
        cursor_x += board.width + gap
        shelf_height = max(shelf_height, board.height)
        column += 1
    return placements


def extent_of(placements: list[Placement]) -> tuple[float, float]:
    if not placements:
        return 0.0, 0.0
    left = min(p.x for p in placements)
    right = max(p.x + p.width for p in placements)
    bottom = min(p.y for p in placements)
    top = max(p.y + p.height for p in placements)
    return right - left, top - bottom


# --------------------------------------------------------------------------
# airwire cost
# --------------------------------------------------------------------------

def airwire_length(placements: list[Placement],
                   centroids: dict[str, dict[str, tuple[float, float]]]) -> float:
    """Total length of the connections that still have to be routed.

    For each net touching more than one board, the cost is a minimum spanning
    tree over the board-local centroids of its pads.  That is the cheapest set
    of hops that could connect them, which is what a router would aim for.
    """
    offsets = {p.design: (p.dx, p.dy) for p in placements}
    by_net: dict[str, list[tuple[float, float]]] = {}
    for design, nets in centroids.items():
        dx, dy = offsets.get(design, (0.0, 0.0))
        for net, (x, y) in nets.items():
            by_net.setdefault(net, []).append((x + dx, y + dy))

    total = 0.0
    for points in by_net.values():
        if len(points) > 1:
            total += _mst_length(points)
    return total


def _mst_length(points: list[tuple[float, float]]) -> float:
    """Prim's algorithm on Manhattan distance. Point counts here are tiny."""
    remaining = list(range(1, len(points)))
    best = [_manhattan(points[0], points[i]) for i in remaining]
    total = 0.0
    while remaining:
        pick = min(range(len(remaining)), key=lambda i: best[i])
        node = remaining.pop(pick)
        total += best.pop(pick)
        for i, other in enumerate(remaining):
            d = _manhattan(points[node], points[other])
            if d < best[i]:
                best[i] = d
    return total


def _manhattan(a: tuple[float, float], b: tuple[float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

WEIGHTS = {
    "none": (0.0, 0.0),
    "area": (0.0, 1.0),
    "airwire": (1.0, 0.0),
    "balanced": (1.0, 0.6),
}


def optimize(
    boards: list[Board],
    centroids: dict[str, dict[str, tuple[float, float]]],
    style: str = "pack",
    gap: float = 5.0,
    columns: int = 0,
    goal: str = "balanced",
    iterations: int = 4000,
    seed: int = 0,
) -> tuple[list[Placement], LayoutStats, LayoutStats]:
    """Search board orderings for a cheaper arrangement.

    Returns the chosen placements plus the stats before and after, so the
    caller can report what the search actually bought.
    """
    order = list(range(len(boards)))
    baseline = place([boards[i] for i in order], style, gap, columns)
    base_stats = _stats(baseline, boards, centroids)

    weight_air, weight_area = WEIGHTS.get(goal, WEIGHTS["balanced"])
    if goal == "none" or len(boards) < 3:
        return baseline, base_stats, base_stats

    def cost(stats: LayoutStats) -> float:
        # Normalised against the starting arrangement so the two terms, which
        # are measured in different units, can be added meaningfully.
        air = stats.airwire / base_stats.airwire if base_stats.airwire else 0.0
        area = stats.area / base_stats.area if base_stats.area else 0.0
        return weight_air * air + weight_area * area

    rng = random.Random(seed)
    best_order = list(order)
    best_stats = base_stats
    best_cost = cost(base_stats)
    improved = 0

    steps = max(0, iterations)
    for step in range(steps):
        trial = list(best_order)
        if len(trial) > 3 and rng.random() < 0.3:
            # Occasionally move one board instead of swapping two, which
            # reshuffles the shelves rather than just trading slots.
            src = rng.randrange(len(trial))
            item = trial.pop(src)
            trial.insert(rng.randrange(len(trial) + 1), item)
        else:
            a, b = rng.sample(range(len(trial)), 2)
            trial[a], trial[b] = trial[b], trial[a]

        candidate = place([boards[i] for i in trial], style, gap, columns)
        stats = _stats(candidate, boards, centroids)
        if cost(stats) < best_cost - 1e-9:
            best_cost = cost(stats)
            best_order = trial
            best_stats = stats
            improved += 1

    final = place([boards[i] for i in best_order], style, gap, columns)
    best_stats = _stats(final, boards, centroids)
    best_stats.iterations = steps
    best_stats.improved = improved
    return final, base_stats, best_stats


def _stats(placements: list[Placement], boards: list[Board],
           centroids: dict[str, dict[str, tuple[float, float]]]) -> LayoutStats:
    width, height = extent_of(placements)
    return LayoutStats(
        airwire=airwire_length(placements, centroids),
        width=width,
        height=height,
        used_area=sum(b.width * b.height for b in boards),
    )


# --------------------------------------------------------------------------
# schematic sheets
# --------------------------------------------------------------------------

def sheet_tiles(sizes: list[tuple[str, float, float]], per_sheet: int,
                gap: float = 20.0) -> dict[str, tuple[int, float, float]]:
    """Assign designs to sheets and positions when packing several per sheet.

    Returns design -> (sheet index, dx, dy).
    """
    tiles: dict[str, tuple[int, float, float]] = {}
    if per_sheet < 1:
        per_sheet = 1
    columns = max(1, math.ceil(math.sqrt(per_sheet)))
    cell_w = max((w for _, w, _ in sizes), default=0.0) + gap
    cell_h = max((h for _, _, h in sizes), default=0.0) + gap

    for index, (name, _, _) in enumerate(sizes):
        sheet, slot = divmod(index, per_sheet)
        column, row = slot % columns, slot // columns
        tiles[name] = (sheet, column * cell_w, -row * cell_h)
    return tiles
