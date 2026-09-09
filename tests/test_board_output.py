"""The board file as EAGLE has to receive it: layers and outline."""

from __future__ import annotations

import pytest

from pcbmerge import layout
from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.layout import DIMENSION_LAYER, parse_outline

# Copper, pads and silkscreen. A schematic hides all of these; a board cannot.
BOARD_LAYERS = ("1", "16", "17", "18", "21", "22", "25", "27")


@pytest.fixture
def merged(designs, tmp_path):
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(designs), "--out-dir", str(out),
                 "-o", "combo", "--yes"]) == 0
    return EagleDoc.load(out / "combo.brd"), EagleDoc.load(out / "combo.sch")


def layers_of(doc: EagleDoc) -> dict[str, tuple[str, str]]:
    return {l.get("number"): (l.get("visible"), l.get("active")) for l in doc.layers()}


# -- layers ----------------------------------------------------------------

def test_the_board_switches_its_own_layers_on(merged):
    """A schematic marks copper hidden. Inheriting that hides every footprint."""
    brd, _ = merged
    table = layers_of(brd)
    for number in BOARD_LAYERS:
        if number in table:
            assert table[number] == ("yes", "yes"), f"layer {number} is off on the board"


def test_the_board_layer_table_comes_from_the_boards(merged, designs):
    brd, _ = merged
    source = EagleDoc.load(sorted(designs.glob("*.brd"))[0])
    merged_table, source_table = layers_of(brd), layers_of(source)
    for number, state in source_table.items():
        assert merged_table.get(number) == state


def test_the_schematic_keeps_its_own_layer_table(merged, designs):
    _, sch = merged
    source = EagleDoc.load(sorted(designs.glob("*.sch"))[0])
    merged_table, source_table = layers_of(sch), layers_of(source)
    for number, state in source_table.items():
        assert merged_table.get(number) == state


def test_the_two_files_do_not_share_one_layer_table(merged):
    """They disagree on purpose, and a merge must not flatten that."""
    brd, sch = merged
    assert layers_of(brd) != layers_of(sch)


# -- outline ---------------------------------------------------------------

def outline_wires(doc: EagleDoc):
    return [w for w in doc.section.iterfind("plain/wire")
            if w.get("layer") == DIMENSION_LAYER]


def test_a_default_outline_is_drawn(merged):
    brd, _ = merged
    wires = outline_wires(brd)
    assert len(wires) == 4, "a rectangle is four wires"


def test_the_default_outline_is_100_by_150(merged):
    brd, _ = merged
    xs = [float(w.get(a)) for w in outline_wires(brd) for a in ("x1", "x2")]
    ys = [float(w.get(a)) for w in outline_wires(brd) for a in ("y1", "y2")]
    assert (min(xs), max(xs)) == (0.0, 100.0)
    assert (min(ys), max(ys)) == (0.0, 150.0)


def test_the_boards_land_inside_the_outline(merged):
    brd, _ = merged
    for element in brd.elements():
        x, y = float(element.get("x")), float(element.get("y"))
        assert 0 <= x <= 100, f"{element.get('name')} is outside the outline"
        assert 0 <= y <= 150, f"{element.get('name')} is outside the outline"


def test_the_source_outlines_are_replaced_not_stacked(designs, tmp_path):
    """Carrying every sub-board's outline over leaves a pile of rectangles."""
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes"])
    assert len(outline_wires(EagleDoc.load(out / "combo.brd"))) == 4


def test_keep_preserves_what_the_sources_drew(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes", "--outline", "keep"])
    # Two fixture boards, four outline wires each.
    assert len(outline_wires(EagleDoc.load(out / "combo.brd"))) == 8


def test_none_draws_no_outline_at_all(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes", "--outline", "none"])
    assert outline_wires(EagleDoc.load(out / "combo.brd")) == []


def test_a_custom_size_is_honoured(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes", "--outline", "80x40"])
    wires = outline_wires(EagleDoc.load(out / "combo.brd"))
    xs = [float(w.get(a)) for w in wires for a in ("x1", "x2")]
    ys = [float(w.get(a)) for w in wires for a in ("y1", "y2")]
    assert max(xs) == 80.0
    assert max(ys) == 40.0


def test_boards_too_big_for_the_outline_are_reported(designs, tmp_path, capsys):
    main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
          "-o", "combo", "--yes", "--outline", "10x10"])
    assert "overflow" in capsys.readouterr().out


def test_an_outline_still_leaves_a_consistent_pair(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes"])
    assert main(["--no-color", "check", str(out / "combo")]) == 0


# -- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("100x150", (100.0, 150.0)),
    ("80 X 60", (80.0, 60.0)),
    ("21.5x34", (21.5, 34.0)),
])
def test_sizes_parse(text, expected):
    assert parse_outline(text) == expected


@pytest.mark.parametrize("text", ["keep", "none", ""])
def test_the_words_that_mean_no_rectangle(text):
    assert parse_outline(text) is None


@pytest.mark.parametrize("text", ["12", "axb", "0x50", "-10x20"])
def test_nonsense_sizes_are_refused(text):
    with pytest.raises(ValueError):
        parse_outline(text)


def test_the_default_is_the_documented_one():
    assert parse_outline(layout.DEFAULT_OUTLINE) == (100.0, 150.0)


# -- packing to a width ----------------------------------------------------

def test_packing_respects_a_maximum_width():
    sizes = [(30.0, 10.0)] * 6
    packed = layout.shelf_positions(sizes, gap=0.0, max_width=60.0)
    assert max(x for x, _, _, _ in packed) + 30.0 <= 60.0
    assert max(row for _, _, row, _ in packed) >= 2


def test_a_board_wider_than_the_outline_still_gets_placed():
    """It overflows rather than vanishing, and the caller says so."""
    packed = layout.shelf_positions([(200.0, 10.0)], gap=0.0, max_width=50.0)
    assert len(packed) == 1
