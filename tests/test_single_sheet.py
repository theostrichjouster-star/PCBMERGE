"""One schematic sheet, always.

Multi-sheet output was built and withdrawn: EAGLE 9.6.2 would not reliably open
it.  These tests hold the replacement to the shape EAGLE does accept.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from pcbmerge import layout
from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.merge import INFO_LAYER

# What a hand-drawn EAGLE sheet contains, in the order the DTD wants it.
SHEET_CHILDREN = ["description", "plain", "instances", "busses", "nets"]


@pytest.fixture
def combined(designs, tmp_path):
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(designs), "--out-dir", str(out),
                 "-o", "combo", "--yes"]) == 0
    return EagleDoc.load(out / "combo.sch")


@pytest.fixture
def many(tmp_path, design_writer):
    """Five designs, so the sheet has to pack more than one row."""
    source = tmp_path / "src"
    source.mkdir()
    for index in range(5):
        design_writer(source, f"board {index}", ["GND", "3.3V", f"LOCAL{index}", "N$1"])
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(source), "--out-dir", str(out),
                 "-o", "combo", "--yes"]) == 0
    return EagleDoc.load(out / "combo.sch")


def blocks(sheet) -> dict[str, list[float]]:
    """Bounding box of each design's instances, keyed by its prefix."""
    boxes: dict[str, list[float]] = {}
    for instance in sheet.iterfind("instances/instance"):
        prefix = instance.get("part").split("_")[0]
        x, y = float(instance.get("x")), float(instance.get("y"))
        box = boxes.setdefault(prefix, [x, y, x, y])
        box[0], box[1] = min(box[0], x), min(box[1], y)
        box[2], box[3] = max(box[2], x), max(box[3], y)
    return boxes


# -- the shape EAGLE accepts -----------------------------------------------

def test_there_is_exactly_one_sheet(combined):
    assert len(combined.sheets()) == 1


def test_five_designs_still_make_one_sheet(many):
    assert len(many.sheets()) == 1
    assert len(blocks(many.sheets()[0])) == 5


def test_the_sheet_holds_only_what_a_drawn_sheet_holds(combined):
    """The withdrawn multi-sheet path emitted an empty <moduleinsts/>.

    No hand-drawn file carries one, and EAGLE would not open the result.
    """
    sheet = combined.sheets()[0]
    assert [c.tag for c in sheet] == SHEET_CHILDREN


def test_no_moduleinsts_anywhere(combined):
    assert combined.section.find(".//moduleinsts") is None


def test_the_sheet_is_named_after_what_is_on_it(combined):
    assert combined.sheets()[0].findtext("description") == "alpha_board, beta_board"


# -- nets ------------------------------------------------------------------

def test_a_joined_net_is_one_element_not_several(combined):
    """EAGLE writes one net per name per sheet, carrying several segments."""
    sheet = combined.sheets()[0]
    names = [n.get("name") for n in sheet.iterfind("nets/net")]
    duplicates = {n for n, count in collections.Counter(names).items() if count > 1}
    assert duplicates == set()


def test_a_folded_net_keeps_every_segment(combined):
    sheet = combined.sheets()[0]
    gnd = [n for n in sheet.iterfind("nets/net") if n.get("name") == "GND"]
    assert len(gnd) == 1
    assert len(gnd[0].findall("segment")) >= 2


def test_a_folded_net_reaches_both_designs(combined):
    sheet = combined.sheets()[0]
    gnd = [n for n in sheet.iterfind("nets/net") if n.get("name") == "GND"][0]
    assert len({p.get("part").split("_")[0] for p in gnd.iterfind(".//pinref")}) == 2


def test_split_nets_stay_separate(combined):
    names = {n.get("name") for n in combined.sheets()[0].iterfind("nets/net")}
    assert "SDA" not in names
    assert len({n for n in names if n.endswith("SDA")}) == 2


# -- arrangement -----------------------------------------------------------

def test_designs_do_not_overlap(many):
    boxes = blocks(many.sheets()[0])
    keys = sorted(boxes)
    for index, first in enumerate(keys):
        for second in keys[index + 1:]:
            a, b = boxes[first], boxes[second]
            assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]), \
                f"{first} overlaps {second}"


def test_each_block_is_captioned(many):
    captions = [t.text for t in many.sheets()[0].iterfind("plain/text")
                if t.get("layer") == INFO_LAYER]
    assert len(captions) == 5
    assert all(c and c.startswith("board_") for c in captions)


def test_no_caption_lands_on_top_of_a_drawing(many):
    """Captions live in the gap each block reserves above itself."""
    sheet = many.sheets()[0]
    boxes = blocks(sheet)
    captions = [(t.text, float(t.get("x")), float(t.get("y")))
                for t in sheet.iterfind("plain/text") if t.get("layer") == INFO_LAYER]
    assert captions
    for name, x, y in captions:
        for prefix, box in boxes.items():
            inside = box[0] <= x <= box[2] and box[1] <= y <= box[3]
            assert not inside, f"caption {name} sits inside block {prefix}"


def test_page_borders_are_dropped_when_designs_share_the_sheet(samples, tmp_path, capsys):
    """Eight overlapping A4 frames on one page would be nothing but noise."""
    main(["--no-color", "merge", str(samples), "--out-dir", str(tmp_path / "out"),
          "-o", "combo", "--yes"])
    assert "page border" in capsys.readouterr().out


# -- a design on its own ---------------------------------------------------

def test_a_lone_design_is_left_where_it_was_drawn(tmp_path, design_writer):
    """Nothing to make room for, so its coordinates should not move."""
    source = tmp_path / "src"
    source.mkdir()
    design_writer(source, "solo", ["GND", "N$1"])
    out = tmp_path / "out"
    main(["--no-color", "merge", str(source), "--out-dir", str(out), "-o", "combo",
          "--yes"])

    original = {i.get("part"): (i.get("x"), i.get("y"))
                for i in EagleDoc.load(source / "solo.sch")
                .section.iterfind("sheets/sheet/instances/instance")}
    merged = EagleDoc.load(out / "combo.sch")
    for instance in merged.section.iterfind("sheets/sheet/instances/instance"):
        name = instance.get("part").split("_", 1)[1]
        assert (instance.get("x"), instance.get("y")) == original[name]


def test_a_lone_design_keeps_its_page_border(tmp_path, design_writer, capsys):
    source = tmp_path / "src"
    source.mkdir()
    design_writer(source, "solo", ["GND", "N$1"])
    main(["--no-color", "merge", str(source), "--out-dir", str(tmp_path / "out"),
          "-o", "combo", "--yes"])
    assert "page border" not in capsys.readouterr().out


def test_a_lone_design_gets_no_caption(tmp_path, design_writer):
    source = tmp_path / "src"
    source.mkdir()
    design_writer(source, "solo", ["GND", "N$1"])
    out = tmp_path / "out"
    main(["--no-color", "merge", str(source), "--out-dir", str(out), "-o", "combo",
          "--yes"])
    sheet = EagleDoc.load(out / "combo.sch").sheets()[0]
    assert [t for t in sheet.iterfind("plain/text")
            if t.get("layer") == INFO_LAYER] == []


# -- consistency -----------------------------------------------------------

def test_the_board_still_matches_the_schematic(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes"])
    assert main(["--no-color", "check", str(out / "combo")]) == 0


# -- packing ---------------------------------------------------------------

def test_shelf_positions_never_overlap():
    sizes = [(30.0, 20.0), (10.0, 40.0), (25.0, 25.0), (50.0, 5.0), (15.0, 15.0)]
    placed = layout.shelf_positions(sizes, gap=2.0)
    boxes = [(x, y, x + w, y + h) for (x, y, _, _), (w, h) in zip(placed, sizes)]
    for index, a in enumerate(boxes):
        for b in boxes[index + 1:]:
            assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])


def test_shelf_rows_grow_downward():
    placed = layout.shelf_positions([(100.0, 10.0)] * 4, gap=1.0)
    assert len({row for _, _, row, _ in placed}) > 1
    assert min(y for _, y, _, _ in placed) < 0


def test_a_wider_target_puts_more_on_each_row():
    sizes = [(50.0, 50.0)] * 6
    square = layout.shelf_positions(sizes, gap=0.0, aspect=1.0)
    wide = layout.shelf_positions(sizes, gap=0.0, aspect=4.0)
    assert max(c for _, _, _, c in wide) > max(c for _, _, _, c in square)


def test_rows_are_sized_to_their_own_tallest_drawing():
    """A big drawing must not space out the small ones beside it."""
    sizes = [("a", 10.0, 10.0), ("b", 10.0, 10.0), ("huge", 500.0, 500.0)]
    tiles = layout.sheet_tiles(sizes)
    assert abs(tiles["a"][0] - tiles["b"][0]) < 100.0
