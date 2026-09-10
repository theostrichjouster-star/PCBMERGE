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
from dataclasses import dataclass, replace, field

from .eagle import bbox, quarter_turns

# EAGLE layer 20 is Dimension: the board outline lives there.
DIMENSION_LAYER = "20"

STYLES = ("grid", "row", "column", "pack")


@dataclass
class Placement:
    """Where one board lands: a turn about its own corner, then a shift.

    `width` and `height` are the turned size, `x` and `y` the bottom-left
    corner after placing, and `dx`, `dy` the shift that puts the turned
    geometry there.  `transform` applies the whole thing to one point, so
    everything that maps a board-local coordinate into the merged board goes
    through the same arithmetic as the board itself.
    """

    design: str
    dx: float
    dy: float
    width: float
    height: float
    x: float = 0.0   # left edge after placing
    y: float = 0.0   # bottom edge after placing
    column: int = 0
    row: int = 0
    rotation: int = 0       # degrees counter-clockwise, a multiple of 90
    pivot_x: float = 0.0    # the point the board turns about, in its own coordinates
    pivot_y: float = 0.0

    def transform(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = x - self.pivot_x, y - self.pivot_y
        turns = quarter_turns(self.rotation)
        if turns == 1:
            dx, dy = -dy, dx
        elif turns == 2:
            dx, dy = -dx, -dy
        elif turns == 3:
            dx, dy = dy, -dx
        return self.pivot_x + dx + self.dx, self.pivot_y + dy + self.dy


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
    """One board instance waiting to be placed, at some rotation.

    The size and the corner describe the board as it will be laid out, which
    is after any turn.  The pivot is the corner it was drawn with and turns
    about, so a turned board can always be measured again upright.
    """

    design: str
    width: float
    height: float
    min_x: float
    min_y: float
    rotation: int = 0            # degrees counter-clockwise, a multiple of 90
    pivot_x: float | None = None
    pivot_y: float | None = None

    def __post_init__(self) -> None:
        # An upright board turns about its own corner unless told otherwise.
        if self.pivot_x is None:
            self.pivot_x = self.min_x
        if self.pivot_y is None:
            self.pivot_y = self.min_y

    def turned(self, degrees: float) -> "Board":
        """This board at the given rotation, measured from its upright size."""
        turns = quarter_turns(degrees)
        if quarter_turns(self.rotation) % 2:
            width, height = self.height, self.width
        else:
            width, height = self.width, self.height
        x, y = self.pivot_x, self.pivot_y
        if turns == 1:
            box = (x - height, y, height, width)
        elif turns == 2:
            box = (x - width, y - height, width, height)
        elif turns == 3:
            box = (x, y - width, height, width)
        else:
            box = (x, y, width, height)
        return Board(self.design, box[2], box[3], box[0], box[1],
                     turns * 90, x, y)


def measure(boards: list[tuple[str, ET.Element]]) -> list[Board]:
    out: list[Board] = []
    for name, board in boards:
        x1, y1, x2, y2 = board_extent(board)
        out.append(Board(name, x2 - x1, y2 - y1, x1, y1))
    return out


def _placed(board: Board, x: float, y: float, column: int, row: int) -> Placement:
    """A placement putting this board's corner at (x, y)."""
    return Placement(board.design, x - board.min_x, y - board.min_y,
                     board.width, board.height, x, y, column, row,
                     rotation=board.rotation,
                     pivot_x=board.pivot_x, pivot_y=board.pivot_y)


def place(boards: list[Board], style: str = "grid", gap: float = 5.0,
          columns: int = 0, origin: tuple[float, float] = (0.0, 0.0),
          max_width: float = 0.0) -> list[Placement]:
    """Turn an ordered list of boards into placements.

    `max_width` constrains packing to a given width, which is how boards are
    kept inside a board outline of a fixed size.
    """
    if not boards:
        return []
    if style == "pack":
        return _shelf(boards, gap, origin, max_width)
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
        placements.append(_placed(board, x, y, column, row))
    return placements


def shelf_positions(sizes: list[tuple[float, float]], gap: float,
                    aspect: float = 1.0,
                    max_width: float = 0.0) -> list[tuple[float, float, int, int]]:
    """Pack rectangles into rows sized to their tallest member.

    Returns the bottom-left corner of each item plus its row and column, with
    the first row at y = 0 and later rows below it.  The shelf width targets
    the given width-to-height ratio: square for a panel, wider for a drawing
    sheet that will be read on screen.
    """
    if not sizes:
        return []
    total_area = sum(w * h for w, h in sizes)
    widest = max(w for w, _ in sizes)
    if max_width > 0:
        # A fixed width wins, except that a board wider than it still has to
        # go somewhere; it overflows and the caller reports that.
        target = max(widest, max_width)
    else:
        target = max(widest, math.sqrt(total_area * aspect) * 1.15)

    out: list[tuple[float, float, int, int]] = []
    cursor_x = 0.0
    shelf_y = 0.0
    shelf_height = 0.0
    row = column = 0

    for width, height in sizes:
        if cursor_x > 0 and cursor_x + width > target:
            shelf_y -= shelf_height + gap
            cursor_x = 0.0
            shelf_height = 0.0
            row += 1
            column = 0
        out.append((cursor_x, shelf_y - height, row, column))
        cursor_x += width + gap
        shelf_height = max(shelf_height, height)
        column += 1
    return out


def _shelf(boards: list[Board], gap: float, origin: tuple[float, float],
           max_width: float = 0.0) -> list[Placement]:
    """Shelf packing for boards: no wasted uniform cell, roughly square."""
    sizes = [(b.width, b.height) for b in boards]
    packed = shelf_positions(sizes, gap, max_width=max_width)
    placements: list[Placement] = []
    for board, (px, py, row, column) in zip(boards, packed):
        x, y = origin[0] + px, origin[1] + py
        placements.append(_placed(board, x, y, column, row))
    return placements


def pin(placements: list[Placement], boards: list[Board],
        spots: dict[str, tuple]) -> list[Placement]:
    """Move named boards to exact positions and leave the rest where they are.

    A spot is `(x, y)` or `(x, y, rotation)`; a spot without a rotation is an
    upright board, which is what every plan written before boards could turn
    means.  A placement carries both the translation applied to the board's
    geometry and the edges that translation produces.  Setting one without
    the other would draw a board in one place and write it out in another, so
    the whole placement is recomputed from the board measured at that
    rotation.
    """
    if not spots:
        return placements
    upright = {b.design: b.turned(0) for b in boards}
    out: list[Placement] = []
    for place in placements:
        spot = spots.get(place.design)
        if spot is None or place.design not in upright:
            out.append(place)
            continue
        x, y, *rest = spot
        board = upright[place.design].turned(rest[0] if rest else 0)
        out.append(replace(
            place, x=x, y=y, dx=x - board.min_x, dy=y - board.min_y,
            width=board.width, height=board.height, rotation=board.rotation,
            pivot_x=board.pivot_x, pivot_y=board.pivot_y))
    return out


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
    placed = {p.design: p for p in placements}
    by_net: dict[str, list[tuple[float, float]]] = {}
    for design, nets in centroids.items():
        place = placed.get(design)
        for net, (x, y) in nets.items():
            point = place.transform(x, y) if place else (x, y)
            by_net.setdefault(net, []).append(point)

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
    origin: tuple[float, float] = (0.0, 0.0),
    max_width: float = 0.0,
) -> tuple[list[Placement], LayoutStats, LayoutStats]:
    """Search board orderings and quarter turns for a cheaper arrangement.

    Returns the chosen placements plus the stats before and after, so the
    caller can report what the search actually bought.  A board is only ever
    turned a quarter, never upside down: the quarter turn is what changes how
    boards pack, and a board silently flipped for a millimetre of airwire is
    a surprise nobody asked for.  A half turn is there for the hand.
    """
    def lay(order: list[int], turns: list[int]) -> list[Placement]:
        items = [boards[i].turned(turns[i]) for i in order]
        return place(items, style, gap, columns, origin, max_width)

    order = list(range(len(boards)))
    turns = [0] * len(boards)
    baseline = lay(order, turns)
    base_stats = stats(baseline, boards, centroids)

    weight_air, weight_area = WEIGHTS.get(goal, WEIGHTS["balanced"])
    if goal == "none" or len(boards) < 2:
        return baseline, base_stats, base_stats

    def cost(measured: LayoutStats) -> float:
        # Normalised against the starting arrangement so the two terms, which
        # are measured in different units, can be added meaningfully.
        air = measured.airwire / base_stats.airwire if base_stats.airwire else 0.0
        area = measured.area / base_stats.area if base_stats.area else 0.0
        return weight_air * air + weight_area * area

    rng = random.Random(seed)
    best_order = list(order)
    best_turns = list(turns)
    best_stats = base_stats
    best_cost = cost(base_stats)
    improved = 0

    steps = max(0, iterations)
    for step in range(steps):
        trial = list(best_order)
        trial_turns = list(best_turns)
        roll = rng.random()
        if len(trial) > 3 and roll < 0.3:
            # Occasionally move one board instead of swapping two, which
            # reshuffles the shelves rather than just trading slots.
            src = rng.randrange(len(trial))
            item = trial.pop(src)
            trial.insert(rng.randrange(len(trial) + 1), item)
        elif roll < 0.5:
            which = rng.randrange(len(trial_turns))
            trial_turns[which] = 90 - trial_turns[which]
        else:
            a, b = rng.sample(range(len(trial)), 2)
            trial[a], trial[b] = trial[b], trial[a]

        candidate = lay(trial, trial_turns)
        trial_stats = stats(candidate, boards, centroids)
        if cost(trial_stats) < best_cost - 1e-9:
            best_cost = cost(trial_stats)
            best_order = trial
            best_turns = trial_turns
            best_stats = trial_stats
            improved += 1

    final = lay(best_order, best_turns)
    best_stats = stats(final, boards, centroids)
    best_stats.iterations = steps
    best_stats.improved = improved
    return final, base_stats, best_stats


def stats(placements: list[Placement], boards: list[Board],
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

# A drawing is read on screen, so a wider-than-tall page beats a square one.
SHEET_ASPECT = 1.6

# What `--outline` gives you unless told otherwise: a plain rectangle big
# enough for a handful of breakout boards and small enough to be cheap.
# Landscape, because the packer fills rows left to right before starting a
# new one, so a wide board wastes less on a part-filled last row.
DEFAULT_OUTLINE = "150x100"


def keeps_source_outlines(text: str) -> bool:
    """Whether the sub-boards' own outlines survive.

    Only `keep` preserves them.  A size replaces them with one rectangle, and
    `none` removes them without drawing a replacement, which is what you want
    when the board shape is coming from somewhere else.
    """
    return text.strip().lower() in ("keep", "")


def parse_outline(text: str) -> tuple[float, float] | None:
    """Read `150x100` into millimetres. `keep` and `none` return None."""
    cleaned = text.strip().lower().replace(" ", "")
    if cleaned in ("keep", "none", ""):
        return None
    width, _, height = cleaned.partition("x")
    try:
        size = (float(width), float(height))
    except ValueError as exc:
        raise ValueError(f"outline should look like 150x100, not {text!r}") from exc
    if size[0] <= 0 or size[1] <= 0:
        raise ValueError(f"outline must be positive, not {text!r}")
    return size


def sheet_tiles(sizes: list[tuple[str, float, float]],
                gap: float = 25.4) -> dict[str, tuple[float, float]]:
    """Lay every design out on one sheet.

    Rows are sized to their tallest drawing rather than to the largest one
    anywhere, so a page of one big and three small drawings does not pay for
    the big one four times over.

    Returns design -> (x, y of the drawing's bottom-left corner).
    """
    packed = shelf_positions([(w, h) for _, w, h in sizes], gap, aspect=SHEET_ASPECT)
    return {name: (x, y) for (name, _, _), (x, y, _, _) in zip(sizes, packed)}
