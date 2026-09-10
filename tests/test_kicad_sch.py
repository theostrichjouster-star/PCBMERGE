"""Converting a KiCad schematic drawing rather than redrawing it from the board.

The board is still where the netlist comes from; what the drawing supplies is
symbols, placement and wires.  The pair only opens if the two agree, so most of
what is pinned here is agreement: the same part names, the same net names, and
no pinref to a part that was never made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbmerge import kicad, kicad_sch
from pcbmerge.eagle import EagleDoc, with_ext

from test_kicad import BOARD

# A resistor drawn as a real symbol: a body outline, two pins, and a unit 0
# that everything belongs to, which is how KiCad stores a one-unit part.
SCHEMATIC = """\
(kicad_sch
  (version 20230121)
  (generator "eeschema")
  (paper "A4")
  (lib_symbols
    (symbol "MyLib:R"
      (property "Reference" "R" (at 0 0 0))
      (symbol "R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (pin passive line (at 0 3.81 270) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GND"
      (power)
      (symbol "GND_0_1"
        (polyline (pts (xy -1.27 -1.27) (xy 0 -2.54) (xy 1.27 -1.27))
          (stroke (width 0.254) (type default)) (fill (type none)))
        (pin power_in line (at 0 0 270) (length 0)
          (name "GND" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (junction (at 50.8 50.8) (diameter 0) (color 0 0 0 0))
  (wire (pts (xy 50.8 46.99) (xy 50.8 50.8)) (stroke (width 0) (type default)))
  (wire (pts (xy 50.8 50.8) (xy 76.2 50.8)) (stroke (width 0) (type default)))
  (wire (pts (xy 76.2 50.8) (xy 76.2 46.99)) (stroke (width 0) (type default)))
  (wire (pts (xy 50.8 58.42) (xy 50.8 63.5)) (stroke (width 0) (type default)))
  (label "MIDDLE" (at 63.5 50.8 0) (effects (font (size 1.27 1.27))))
  (symbol (lib_id "MyLib:R") (at 50.8 54.61 0) (unit 1)
    (property "Reference" "R1" (at 53.34 54.61 0))
    (property "Value" "10k" (at 53.34 57.15 0))
  )
  (symbol (lib_id "MyLib:R") (at 76.2 54.61 0) (unit 1)
    (property "Reference" "R2" (at 78.74 54.61 0))
    (property "Value" "1k" (at 78.74 57.15 0))
  )
  (symbol (lib_id "power:GND") (at 50.8 63.5 0) (unit 1)
    (property "Reference" "#PWR01" (at 50.8 69.85 0))
    (property "Value" "GND" (at 50.8 68.58 0))
  )
)
"""


@pytest.fixture
def drawn(tmp_path) -> Path:
    """A KiCad board with the schematic that was actually drawn for it."""
    stem = tmp_path / "widget_V1.5"
    with_ext(stem, kicad.PCB_SUFFIX).write_text(BOARD, encoding="utf-8")
    with_ext(stem, kicad.SCH_SUFFIX).write_text(SCHEMATIC, encoding="utf-8")
    return stem


@pytest.fixture
def reading(drawn) -> kicad_sch.Drawing:
    return kicad_sch.read(with_ext(drawn, kicad.SCH_SUFFIX))


# --------------------------------------------------------------------------
# reading the drawing
# --------------------------------------------------------------------------

def test_the_symbols_and_the_placements_are_read(reading):
    assert set(reading.symbols) == {"MyLib:R", "power:GND"}
    assert [p.ref for p in reading.placed] == ["R1", "R2", ""]
    assert len(reading.wires) == 4
    assert reading.junctions == [(50.8, 50.8)]
    assert reading.labels == [("MIDDLE", 63.5, 50.8)]


def test_a_power_symbol_is_recognised_as_drawing_only(reading):
    """KiCad hides a reference behind a `#` when the part is not a part."""
    power = reading.placed[-1]
    assert power.symbol.power
    assert power.ref == ""


def test_a_symbol_drawn_only_in_unit_zero_is_still_a_unit(reading):
    """Unit 0 is what every unit shares, and most two-pin parts use only it."""
    resistor = reading.symbols["MyLib:R"]
    assert sorted(resistor.units) == [0]
    assert resistor.real_units() == [1]
    assert len(resistor.pins_of(1)) == 2
    assert len(resistor.shapes_of(1)) == 4       # a rectangle is four wires


def test_a_sheet_with_no_wiring_is_not_worth_drawing(tmp_path):
    lonely = tmp_path / "empty.kicad_sch"
    lonely.write_text('(kicad_sch (version 20230121) (lib_symbols))', encoding="utf-8")

    assert not kicad_sch.read(lonely).usable


def test_a_child_sheet_that_is_not_there_is_recorded(tmp_path):
    root = tmp_path / "top.kicad_sch"
    root.write_text(
        '(kicad_sch (version 20230121) (lib_symbols)'
        ' (sheet (at 10 10) (property "Sheetname" "One")'
        ' (property "Sheetfile" "one.kicad_sch")))', encoding="utf-8")

    assert kicad_sch.read(root).missing == ["one.kicad_sch"]


def test_a_child_sheet_that_is_there_is_read(tmp_path):
    (tmp_path / "one.kicad_sch").write_text(SCHEMATIC, encoding="utf-8")
    root = tmp_path / "top.kicad_sch"
    root.write_text(
        '(kicad_sch (version 20230121) (lib_symbols)'
        ' (sheet (at 10 10) (property "Sheetname" "One")'
        ' (property "Sheetfile" "one.kicad_sch")))', encoding="utf-8")

    drawing = kicad_sch.read(root)

    assert not drawing.missing
    assert [p.ref for p in drawing.placed] == ["R1", "R2", ""]


def test_a_sheet_that_points_at_itself_is_read_once(tmp_path):
    root = tmp_path / "loop.kicad_sch"
    root.write_text(
        SCHEMATIC.rstrip()[:-1]
        + ' (sheet (at 10 10) (property "Sheetfile" "loop.kicad_sch")))',
        encoding="utf-8")

    assert len(kicad_sch.read(root).placed) == 3


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def test_a_pin_lands_where_the_wire_ends(reading):
    """The whole conversion rests on this: symbol space is y-up, sheet is y-down."""
    r1 = reading.placed[0]
    lower = next(p for p in r1.symbol.pins_of(1) if p.number == "2")

    x, y = kicad_sch.pin_at(r1, lower)

    assert (round(x, 2), round(y, 2)) == (50.8, 58.42)   # the fourth wire's end


@pytest.mark.parametrize("angle, expected", [
    (0, (50.8, 58.42)), (180, (50.8, 50.8)), (90, (54.61, 54.61)),
])
def test_rotation_turns_a_pin_about_its_symbol(reading, angle, expected):
    place = reading.placed[0]
    place.angle = angle
    lower = next(p for p in place.symbol.pins_of(1) if p.number == "2")

    x, y = kicad_sch.pin_at(place, lower)

    assert (round(x, 2), round(y, 2)) == expected


def test_mirroring_flips_a_pin_across_the_symbol(reading):
    place = reading.placed[0]
    place.angle = 90
    place.mirror = "y"
    lower = next(p for p in place.symbol.pins_of(1) if p.number == "2")

    x, _y = kicad_sch.pin_at(place, lower)

    assert round(x, 2) == 46.99      # the other side of the symbol's origin


# --------------------------------------------------------------------------
# the converted document
# --------------------------------------------------------------------------

@pytest.fixture
def converted(drawn):
    return kicad.convert(drawn)


def test_the_drawing_is_used_when_there_is_one(converted):
    assert any("converted as drawn" in note for note in converted.notes)
    assert not any("box" in note for note in converted.notes)


def test_the_symbols_are_drawn_not_boxed(converted):
    symbols = [s for lib in converted.schematic.libraries()
               for s in lib.iterfind("symbols/symbol")]
    shapes = [node.tag for s in symbols for node in s]

    assert "wire" in shapes
    assert shapes.count("pin") == 3        # two on the resistor, one on ground


def test_every_pinref_names_a_part_that_exists(converted):
    doc = converted.schematic
    parts = {p.get("name") for p in doc.parts()}
    refs = {p.get("part") for p in doc.section.iterfind("sheets/sheet/nets/net//pinref")}

    assert refs and refs <= parts


def test_the_parts_keep_the_names_the_board_uses(converted):
    parts = {p.get("name") for p in converted.schematic.parts()}
    elements = {e.get("name") for e in converted.board.elements()}

    assert elements <= parts


def test_the_nets_are_the_board_s_nets(converted):
    """A drawing that named its own nets would disagree on every unnamed one."""
    signals = set(converted.board.net_names())
    nets = set(converted.schematic.net_names())

    assert signals <= nets
    assert "VCC_3V3" in nets


def test_a_label_names_a_net_the_board_says_nothing_about(drawn):
    """MIDDLE joins two pads the board puts on different nets, so it survives."""
    doc = kicad.convert(drawn).schematic

    assert any(name in doc.net_names() for name in ("MIDDLE", "GND", "VCC_3V3"))


def test_the_wires_and_junctions_come_across(converted):
    doc = converted.schematic
    wires = list(doc.section.iterfind("sheets/sheet/nets/net/segment/wire"))
    junctions = list(doc.section.iterfind("sheets/sheet/nets/net/segment/junction"))

    assert len(wires) == 4
    assert len(junctions) == 1


def test_the_sheet_is_the_right_way_up(converted):
    """Sheet coordinates are y-down in KiCad and y-up in EAGLE."""
    instances = list(converted.schematic.section.iterfind(
        "sheets/sheet/instances/instance"))

    assert instances
    assert all(float(i.get("y")) < 0 for i in instances)


def test_a_missing_drawing_still_falls_back_to_the_netlist(tmp_path):
    stem = tmp_path / "boardonly"
    with_ext(stem, kicad.PCB_SUFFIX).write_text(BOARD, encoding="utf-8")

    out = kicad.convert(stem)

    assert any("board netlist" in note for note in out.notes)
    assert len(out.schematic.parts()) == 2


def test_a_drawing_with_nothing_wired_falls_back_and_says_why(tmp_path):
    stem = tmp_path / "stub"
    with_ext(stem, kicad.PCB_SUFFIX).write_text(BOARD, encoding="utf-8")
    with_ext(stem, kicad.SCH_SUFFIX).write_text(
        '(kicad_sch (version 20230121) (lib_symbols)'
        ' (sheet (at 10 10) (property "Sheetfile" "gone.kicad_sch")))',
        encoding="utf-8")

    out = kicad.convert(stem)

    assert any("child sheets that are not here" in note for note in out.notes)
    assert any("gone.kicad_sch" in note for note in out.notes)


# --------------------------------------------------------------------------
# the board's older spellings
# --------------------------------------------------------------------------

def test_a_reference_written_the_old_way_is_still_read(tmp_path):
    """KiCad 8 moved reference and value into `property`; boards predate that."""
    older = BOARD.replace('(property "Reference" "R1")',
                          '(fp_text reference "R1" (at 0 0) (layer "F.SilkS"))')
    older = older.replace('(property "Value" "10k")',
                          '(fp_text value "10k" (at 0 0) (layer "F.Fab"))')
    stem = tmp_path / "older"
    with_ext(stem, kicad.PCB_SUFFIX).write_text(older, encoding="utf-8")

    names = {e.get("name") for e in kicad.convert(stem).board.elements()}

    assert "R1" in names
