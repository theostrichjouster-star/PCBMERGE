"""Placing source boards on the merged board without overlap.

Each input board is drawn around its own origin, so dropping them into one
file stacks them on top of each other.  This module measures each board and
hands back the translation that tiles them into a readable arrangement, which
the engineer then rearranges by hand.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .eagle import bbox

# EAGLE layer 20 is Dimension: the board outline lives there.
DIMENSION_LAYER = "20"


@dataclass
class Placement:
    design: str
    dx: float
    dy: float
    width: float
    height: float
    column: int
    row: int


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


def arrange(
    boards: list[tuple[str, ET.Element]],
    style: str = "grid",
    gap: float = 5.0,
    columns: int = 0,
    origin: tuple[float, float] = (0.0, 0.0),
) -> list[Placement]:
    """Work out where each board goes, and by how much it must move."""
    extents = [(name, board_extent(board)) for name, board in boards]
    sizes = [(name, x2 - x1, y2 - y1, x1, y1) for name, (x1, y1, x2, y2) in extents]

    count = len(sizes)
    if style == "row":
        cols = count
    elif style == "column":
        cols = 1
    elif columns > 0:
        cols = columns
    else:
        cols = max(1, math.ceil(math.sqrt(count)))
    cols = max(1, min(cols, count)) if count else 1

    # Uniform cells keep the arrangement predictable and easy to re-place.
    col_width = max((w for _, w, _, _, _ in sizes), default=0.0) + gap
    row_height = max((h for _, _, h, _, _ in sizes), default=0.0) + gap

    placements: list[Placement] = []
    for index, (name, width, height, min_x, min_y) in enumerate(sizes):
        column = index % cols
        row = index // cols
        target_x = origin[0] + column * col_width
        # Rows grow downward on screen, so later rows sit at lower Y.
        target_y = origin[1] - row * row_height
        placements.append(
            Placement(
                design=name,
                dx=target_x - min_x,
                dy=target_y - min_y,
                width=width,
                height=height,
                column=column,
                row=row,
            )
        )
    return placements
