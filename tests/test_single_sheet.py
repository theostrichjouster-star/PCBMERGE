"""Putting every design on one schematic sheet."""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from pcbmerge import layout
from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.merge import INFO_LAYER

SINGLE = ["--sheet-layout", "single"]


@pytest.fixture
def combined(designs, tmp_path):
    """Two clashing designs merged onto a single sheet."""
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(designs), "--out-dir", str(out),
                 "-o", "combo", "--yes", *SINGLE]) == 0
    return EagleDoc.load(out / "combo.sch")


@pytest.fixture
def many(tmp_path, design_writer):
    """Five designs, so a single sheet has to pack more than one row."""
    source = tmp_path / "src"
    source.mkdir()
    for index in range(5):
        design_writer(source, f"board {index}", ["GND", "3.3V", f"LOCAL{index}", "N$1"])
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(source), "--out-dir", str(out),
                 "-o", "combo", "--yes", *SINGLE]) == 0
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


# -- one sheet -------------------------------------------------------------

def test_everything_lands_on_one_sheet(combined):
    assert len(combined.sheets()) == 1


def test_one_sheet_holds_five_designs_too(many):
    assert len(many.sheets()) == 1
    assert len(blocks(many.sheets()[0])) == 5


def test_no_count_of_designs_needs_to_be_given(tmp_path, design_writer):
    """`single` means single, whatever --sheets-per-page happens to say."""
    source = tmp_path / "src"
    source.mkdir()
    for index in range(3):
        design_writer(source, f"board {index}", ["GND", f"LOCAL{index}"])
    out = tmp_path / "out"
    main(["--no-color", "merge", str(source), "--out-dir", str(out), "-o", "combo",
          "--yes", "--sheet-layout", "single", "--sheets-per-page", "1"])
    assert len(EagleDoc.load(out / "combo.sch").sheets()) == 1


def test_every_part_is_still_present(combined, designs):
    sources = [EagleDoc.load(p) for p in sorted(designs.glob("*.sch"))]
    expected = sum(len(doc.parts()) for doc in sources)
    # Page borders are dropped when designs share a sheet.
    assert 0 < len(combined.parts()) <= expected


def test_designs_do_not_overlap_on_the_sheet(many):
    boxes = blocks(many.sheets()[0])
    keys = sorted(boxes)
    for index, first in enumerate(keys):
        for second in keys[index + 1:]:
            a, b = boxes[first], boxes[second]
            assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]), \
                f"{first} overlaps {second}"


# -- nets ------------------------------------------------------------------

def test_a_joined_net_becomes_one_element_not_several(combined):
    """EAGLE writes one net per name per sheet, carrying several segments."""
    sheet = combined.sheets()[0]
    names = [n.get("name") for n in sheet.iterfind("nets/net")]
    duplicates = {n for n, count in collections.Counter(names).items() if count > 1}
    assert duplicates == set(), "same-named nets must fold together"


def test_a_folded_net_keeps_every_segment(combined):
    sheet = combined.sheets()[0]
    gnd = [n for n in sheet.iterfind("nets/net") if n.get("name") == "GND"]
    assert len(gnd) == 1
    assert len(gnd[0].findall("segment")) >= 2, "one segment per design"


def test_a_folded_net_reaches_both_designs(combined):
    sheet = combined.sheets()[0]
    gnd = [n for n in sheet.iterfind("nets/net") if n.get("name") == "GND"][0]
    prefixes = {p.get("part").split("_")[0] for p in gnd.iterfind(".//pinref")}
    assert len(prefixes) == 2


def test_split_nets_stay_separate_on_one_sheet(combined):
    sheet = combined.sheets()[0]
    names = {n.get("name") for n in sheet.iterfind("nets/net")}
    assert "SDA" not in names
    assert len({n for n in names if n.endswith("SDA")}) == 2


def test_the_board_still_matches_the_schematic(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes", *SINGLE])
    assert main(["--no-color", "check", str(out / "combo")]) == 0


# -- captions --------------------------------------------------------------

def test_each_block_is_captioned(many):
    sheet = many.sheets()[0]
    captions = [t.text for t in sheet.iterfind("plain/text")
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


def test_captions_are_annotation_not_netlist(many):
    """Layer 97 is Info, so nothing here affects connectivity."""
    sheet = many.sheets()[0]
    added = [t for t in sheet.iterfind("plain/text") if t.get("layer") == INFO_LAYER]
    assert added
    assert all(t.get("size") for t in added)


# -- per-design remains the default ----------------------------------------

def test_one_sheet_per_design_is_still_the_default(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out),
          "-o", "combo", "--yes"])
    sch = EagleDoc.load(out / "combo.sch")
    assert len(sch.sheets()) == 2


def test_a_lone_design_keeps_its_page_border(tmp_path, design_writer, capsys):
    """Nothing to overlap, so nothing to drop."""
    source = tmp_path / "src"
    source.mkdir()
    design_writer(source, "solo", ["GND", "N$1"])
    out = tmp_path / "out"
    main(["--no-color", "merge", str(source), "--out-dir", str(out), "-o", "combo",
          "--yes", *SINGLE])
    assert "page border" not in capsys.readouterr().out


# -- packing --------------------------------------------------------------

def test_shelf_positions_never_overlap():
    sizes = [(30.0, 20.0), (10.0, 40.0), (25.0, 25.0), (50.0, 5.0), (15.0, 15.0)]
    placed = layout.shelf_positions(sizes, gap=2.0)
    boxes = [(x, y, x + w, y + h) for (x, y, _, _), (w, h) in zip(placed, sizes)]
    for index, a in enumerate(boxes):
        for b in boxes[index + 1:]:
            assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])


def test_shelf_rows_grow_downward():
    sizes = [(100.0, 10.0)] * 4
    placed = layout.shelf_positions(sizes, gap=1.0)
    rows = {row for _, _, row, _ in placed}
    assert len(rows) > 1
    assert min(y for _, y, _, _ in placed) < 0


def test_a_wider_target_puts_more_on_each_row():
    sizes = [(50.0, 50.0)] * 6
    square = layout.shelf_positions(sizes, gap=0.0, aspect=1.0)
    wide = layout.shelf_positions(sizes, gap=0.0, aspect=4.0)
    assert max(c for _, _, _, c in wide) > max(c for _, _, _, c in square)


def test_packing_one_page_does_not_pay_for_the_next(tmp_path):
    """Each page is packed on its own, so a big design elsewhere costs nothing."""
    sizes = [("small1", 10.0, 10.0), ("small2", 10.0, 10.0), ("huge", 500.0, 500.0)]
    tiles = layout.sheet_tiles(sizes, per_sheet=2)
    assert tiles["small1"][0] == tiles["small2"][0] == 0
    assert tiles["huge"][0] == 1
    assert abs(tiles["small2"][1]) < 100.0, "not spaced out by the huge design"
