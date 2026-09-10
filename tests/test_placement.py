"""Placing designs by hand, on the board and on the schematic sheet.

The packer decides everything by default.  A hand placement overrides it for
one design in one view and leaves the rest of the arrangement to the packer,
which is the whole point: you move the one board you care about and the others
still fill in around it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbmerge import layout, web
from pcbmerge.cli import collect_specs
from pcbmerge.eagle import EagleDoc
from pcbmerge.layout import Board, Placement
from pcbmerge.merge import Merger, build_resolver, load_designs, merge
from pcbmerge.nets import Action
from pcbmerge.plan import MergePlan, Spot, expand


# --------------------------------------------------------------------------
# the geometry
# --------------------------------------------------------------------------

def boards(*sizes: tuple[float, float]) -> list[Board]:
    return [Board(f"b{i}", w, h, 0.0, 0.0) for i, (w, h) in enumerate(sizes)]


def test_pinning_moves_only_the_board_named():
    items = boards((30, 20), (10, 40), (25, 25))
    placed = layout.place(items, style="pack", gap=2.0)
    before = {p.design: (p.x, p.y) for p in placed}

    pinned = layout.pin(placed, items, {"b1": (100.0, 50.0)})

    moved = {p.design: (p.x, p.y) for p in pinned}
    assert moved["b1"] == (100.0, 50.0)
    assert moved["b0"] == before["b0"]
    assert moved["b2"] == before["b2"]


def test_pinning_moves_the_geometry_with_the_edges():
    """A placement carries both a translation and the edges it produces.

    Setting one without the other would draw a board in one place and write it
    out in another.
    """
    items = [Board("b0", 30.0, 20.0, min_x=7.0, min_y=-3.0)]
    placed = layout.place(items, style="pack", gap=2.0)

    pinned = layout.pin(placed, items, {"b0": (100.0, 50.0)})[0]

    assert (pinned.x, pinned.y) == (100.0, 50.0)
    assert pinned.dx == 100.0 - 7.0
    assert pinned.dy == 50.0 - (-3.0)


def test_pinning_nothing_leaves_the_arrangement_alone():
    items = boards((30, 20), (10, 40))
    placed = layout.place(items, style="pack", gap=2.0)

    assert layout.pin(placed, items, {}) is placed


def test_a_pin_for_a_board_that_is_not_there_is_ignored():
    items = boards((30, 20))
    placed = layout.place(items, style="pack", gap=2.0)

    pinned = layout.pin(placed, items, {"gone": (5.0, 5.0)})

    assert [(p.design, p.x, p.y) for p in pinned] == [
        (p.design, p.x, p.y) for p in placed]


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------

def test_a_plan_carries_hand_placements_through_a_round_trip(tmp_path):
    plan = MergePlan(positions=[
        Spot(design="alpha", view="board", x=10.0, y=20.0),
        Spot(design="alpha", view="sheet", x=30.0, y=40.0),
    ])
    path = plan.save(tmp_path / "plan.json")

    back = MergePlan.load(path)

    assert back.spots("board") == {"alpha": (10.0, 20.0)}
    assert back.spots("sheet") == {"alpha": (30.0, 40.0)}


def test_a_plan_without_placements_still_loads(tmp_path):
    """Every plan written before this feature existed has no positions."""
    old = {"output": "merged", "designs": [], "version": 4}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    plan = MergePlan.load(path)

    assert plan.positions == []
    assert plan.spots("board") == {}


def test_a_design_can_be_pinned_in_one_view_and_packed_in_the_other():
    plan = MergePlan(positions=[Spot(design="alpha", view="sheet", x=1.0, y=2.0)])

    assert plan.spots("sheet") == {"alpha": (1.0, 2.0)}
    assert plan.spots("board") == {}


# --------------------------------------------------------------------------
# the merger
# --------------------------------------------------------------------------

def merger_for(folder: Path, **plan_args) -> Merger:
    specs = collect_specs([str(folder)])
    loaded = load_designs(expand(specs))
    resolver = build_resolver(loaded)
    resolver.finalize(default_action=Action.SPLIT, replica_action=Action.SPLIT)
    made = Merger(loaded, resolver, MergePlan(designs=specs, **plan_args))
    made.prepare()
    return made


def test_a_pinned_board_lands_exactly_where_it_was_put(designs):
    loose = merger_for(designs)
    name = loose.designs[0].name

    pinned = merger_for(designs, positions=[
        Spot(design=name, view="board", x=60.0, y=12.5)])
    spot = next(p for p in pinned.preview()["placements"] if p.design == name)

    assert (spot.x, spot.y) == (60.0, 12.5)


def test_the_other_boards_are_still_packed_around_it(designs):
    loose = merger_for(designs)
    names = [p.design for p in loose.preview()["placements"]]
    free = names[1]
    was = {p.design: (p.x, p.y) for p in loose.preview()["placements"]}

    pinned = merger_for(designs, positions=[
        Spot(design=names[0], view="board", x=200.0, y=200.0)])
    now = {p.design: (p.x, p.y) for p in pinned.preview()["placements"]}

    assert now[free] == was[free]


def test_the_reported_cost_is_measured_from_where_things_ended_up(designs):
    """Pinning happens after the search, so the stats have to be recomputed.

    Reporting the search's own numbers would describe an arrangement nobody is
    going to get.
    """
    loose = merger_for(designs)
    loose.preview()
    before = loose.report.after.width

    pinned = merger_for(designs, positions=[
        Spot(design=loose.designs[0].name, view="board", x=400.0, y=0.0)])
    pinned.preview()

    assert pinned.report.after.width > before


def test_the_merger_says_which_boards_were_placed_by_hand(designs):
    made = merger_for(designs)
    name = made.designs[0].name

    pinned = merger_for(designs, positions=[
        Spot(design=name, view="board", x=5.0, y=5.0)])
    pinned.preview()

    assert pinned.report.placed_by_hand == [name]


def test_a_board_pin_does_not_move_the_drawing(designs):
    """The two views are placed separately; a board pin is not a sheet pin."""
    loose = merger_for(designs)
    was = {b["design"]: (b["x"], b["y"]) for b in loose.sheet_preview()}

    pinned = merger_for(designs, positions=[
        Spot(design=loose.designs[0].name, view="board", x=300.0, y=300.0)])
    now = {b["design"]: (b["x"], b["y"]) for b in pinned.sheet_preview()}

    assert now == was


# --------------------------------------------------------------------------
# the sheet
# --------------------------------------------------------------------------

def test_the_sheet_preview_covers_every_design(designs):
    made = merger_for(designs)

    blocks = made.sheet_preview()

    assert {b["design"] for b in blocks} == {d.name for d in made.designs}
    assert all(b["width"] > 0 and b["height"] > 0 for b in blocks)


def test_a_pinned_drawing_takes_the_corner_it_was_given(designs):
    made = merger_for(designs)
    name = made.designs[0].name

    pinned = merger_for(designs, positions=[
        Spot(design=name, view="sheet", x=500.0, y=250.0)])
    block = next(b for b in pinned.sheet_preview() if b["design"] == name)

    assert (block["x"], block["y"]) == (500.0, 250.0)


def test_a_pinned_drawing_really_moves_on_the_written_sheet(designs, tmp_path):
    """The preview and the file have to agree, or the picture is a lie."""
    made = merger_for(designs)
    name = made.designs[0].name
    loose = EagleDoc.load(_write(designs, tmp_path / "loose", []) )

    far = _write(designs, tmp_path / "far",
                 [Spot(design=name, view="sheet", x=900.0, y=900.0)])
    moved = EagleDoc.load(far)

    assert _spread(moved) > _spread(loose)


def _write(folder: Path, out: Path, positions: list[Spot]) -> Path:
    specs = collect_specs([str(folder)])
    loaded = load_designs(expand(specs))
    resolver = build_resolver(loaded)
    resolver.finalize(default_action=Action.SPLIT, replica_action=Action.SPLIT)
    plan = MergePlan(designs=specs, positions=positions)
    report = merge(loaded, resolver, plan, out, "combo")
    return report.sch_path


def _spread(doc: EagleDoc) -> float:
    xs = [float(i.get("x", 0)) for i in
          doc.section.iterfind("sheets/sheet/instances/instance")]
    return max(xs) - min(xs) if xs else 0.0


# --------------------------------------------------------------------------
# the API the page talks to
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_cache():
    web._documents.clear()
    yield
    web._documents.clear()


def body(folder: Path, **extra) -> dict:
    payload = {"designs": web.scan({"path": str(folder)})["designs"], "options": {}}
    payload.update(extra)
    return payload


def test_analyze_reports_both_views(designs):
    out = web.analyze(body(designs))

    assert out["board"]["placements"]
    assert out["sheet"]["blocks"]
    assert len(out["sheet"]["extent"]) == 4


def test_the_page_can_pin_a_board(designs):
    first = web.analyze(body(designs))["board"]["placements"][0]["design"]

    out = web.analyze(body(designs, positions=[
        {"design": first, "view": "board", "x": 77.0, "y": 33.0}]))
    spot = next(p for p in out["board"]["placements"] if p["design"] == first)

    assert (spot["x"], spot["y"]) == (77.0, 33.0)


def test_the_page_can_pin_a_drawing(designs):
    first = web.analyze(body(designs))["sheet"]["blocks"][0]["design"]

    out = web.analyze(body(designs, positions=[
        {"design": first, "view": "sheet", "x": 640.0, "y": 480.0}]))
    block = next(b for b in out["sheet"]["blocks"] if b["design"] == first)

    assert (block["x"], block["y"]) == (640.0, 480.0)


@pytest.mark.parametrize("position", [
    {"design": "alpha", "view": "elevation", "x": 1, "y": 2},   # not a view
    {"design": "", "view": "board", "x": 1, "y": 2},            # no design
    {"design": "alpha", "view": "board", "x": "over there"},    # not a number
    "not an object",
])
def test_a_position_that_makes_no_sense_is_dropped(position):
    assert web._positions({"positions": [position]}) == []


def test_a_saved_plan_keeps_what_was_placed_by_hand(designs, tmp_path):
    out = tmp_path / "out"
    first = web.analyze(body(designs))["board"]["placements"][0]["design"]

    web.run_merge(body(
        designs, outDir=str(out), output="combo", savePlan=True,
        positions=[{"design": first, "view": "board", "x": 21.0, "y": 12.0}]))

    plan = MergePlan.load(out / "combo-plan.json")
    assert plan.spots("board") == {first: (21.0, 12.0)}
