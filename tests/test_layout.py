"""Board placement: packing, airwire cost, and the search over arrangements."""

from __future__ import annotations

import pytest

from pcbmerge import layout
from pcbmerge.layout import Board, LayoutStats, Placement


def boards(*sizes: tuple[float, float]) -> list[Board]:
    return [Board(f"b{i}", w, h, 0.0, 0.0) for i, (w, h) in enumerate(sizes)]


def overlaps(placements: list[Placement]) -> int:
    count = 0
    for i, first in enumerate(placements):
        for second in placements[i + 1:]:
            if (first.x < second.x + second.width and second.x < first.x + first.width
                    and first.y < second.y + second.height
                    and second.y < first.y + first.height):
                count += 1
    return count


@pytest.mark.parametrize("style", ["grid", "row", "column", "pack"])
def test_no_style_ever_overlaps_boards(style):
    items = boards((30, 20), (10, 40), (25, 25), (50, 5), (15, 15))
    placed = layout.place(items, style=style, gap=2.0)
    assert len(placed) == len(items)
    assert overlaps(placed) == 0


def test_packing_beats_uniform_cells_on_mixed_sizes():
    """Uniform cells pay for the largest board on every slot."""
    items = boards((60, 10), (10, 60), (20, 20), (15, 15))
    grid = layout.place(items, style="grid", gap=2.0)
    packed = layout.place(items, style="pack", gap=2.0)

    grid_w, grid_h = layout.extent_of(grid)
    pack_w, pack_h = layout.extent_of(packed)
    assert pack_w * pack_h < grid_w * grid_h


def test_row_and_column_are_single_lines():
    items = boards((10, 10), (10, 10), (10, 10))
    row = layout.place(items, style="row", gap=1.0)
    column = layout.place(items, style="column", gap=1.0)
    assert len({p.y for p in row}) == 1
    assert len({p.x for p in column}) == 1


def test_a_board_keeps_its_size_wherever_it_lands():
    items = boards((30, 20), (10, 40))
    for placement in layout.place(items, style="pack", gap=2.0):
        source = next(b for b in items if b.design == placement.design)
        assert (placement.width, placement.height) == (source.width, source.height)


def test_airwire_is_zero_when_nothing_is_shared():
    placements = layout.place(boards((10, 10), (10, 10)), style="row", gap=5.0)
    centroids = {"b0": {"A": (0.0, 0.0)}, "b1": {"B": (0.0, 0.0)}}
    assert layout.airwire_length(placements, centroids) == 0.0


def test_airwire_grows_with_distance():
    items = boards((10, 10), (10, 10))
    centroids = {"b0": {"GND": (5.0, 5.0)}, "b1": {"GND": (5.0, 5.0)}}
    near = layout.airwire_length(layout.place(items, "row", gap=1.0), centroids)
    far = layout.airwire_length(layout.place(items, "row", gap=50.0), centroids)
    assert far > near > 0


def test_a_shared_net_across_three_boards_uses_a_spanning_tree():
    """Cost is the cheapest set of hops, not every pair."""
    items = boards((10, 10), (10, 10), (10, 10))
    placements = layout.place(items, style="row", gap=0.0)
    centroids = {b.design: {"GND": (5.0, 5.0)} for b in items}
    total = layout.airwire_length(placements, centroids)
    assert total == pytest.approx(20.0), "two hops of 10mm, not three"


def test_optimizer_leaves_things_alone_when_told_to():
    items = boards((10, 10), (20, 10), (10, 20), (15, 15))
    centroids = {b.design: {} for b in items}
    placed, before, after = layout.optimize(items, centroids, goal="none")
    assert before is after
    assert [p.design for p in placed] == [b.design for b in items]


def test_optimizer_shortens_the_airwires():
    """Two boards that share a net should end up beside each other."""
    items = boards((20, 20), (20, 20), (20, 20), (20, 20), (20, 20), (20, 20))
    # The first and last boards are the only pair sharing anything.
    centroids = {b.design: {} for b in items}
    centroids["b0"] = {"LINK": (10.0, 10.0)}
    centroids["b5"] = {"LINK": (10.0, 10.0)}

    _, before, after = layout.optimize(items, centroids, goal="airwire",
                                       gap=5.0, iterations=800, seed=1)
    assert after.airwire < before.airwire


def test_optimizer_never_returns_an_overlapping_arrangement():
    items = boards((30, 20), (10, 40), (25, 25), (50, 5), (15, 15), (22, 33))
    centroids = {b.design: {"GND": (1.0, 1.0)} for b in items}
    placed, _, _ = layout.optimize(items, centroids, goal="balanced",
                                   gap=3.0, iterations=500, seed=2)
    assert overlaps(placed) == 0


def test_stats_describe_how_full_the_board_is():
    stats = LayoutStats(width=10.0, height=10.0, used_area=50.0)
    assert stats.area == 100.0
    assert stats.utilization == pytest.approx(50.0)


def test_sheet_tiles_give_every_design_its_own_slot():
    sizes = [(f"d{i}", 100.0, 80.0) for i in range(5)]
    tiles = layout.sheet_tiles(sizes)
    assert len(tiles) == 5
    assert len(set(tiles.values())) == 5, "no two designs share a position"
