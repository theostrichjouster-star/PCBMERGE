"""Reading KiCad designs and presenting them as EAGLE drawings."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

import pytest

from pcbmerge import kicad, sexp
from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc, design_stem, with_ext
from pcbmerge.merge import build_resolver, load_designs, merge
from pcbmerge.plan import MergePlan, design_name, expand

BOARD = """\
(kicad_pcb
  (version 20241229)
  (generator "pcbnew")
  (net 0 "")
  (net 1 "GND")
  (net 2 "/Sheet One/VCC_3V3")
  (net 3 "Net-(U1-Pad2)")
  (gr_line (start 0 0) (end 20 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 20 0) (end 20 10) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 20 10) (end 0 10) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 0 10) (end 0 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (footprint "MyLib:R0402"
    (layer "F.Cu")
    (at 5 4 90)
    (property "Reference" "R1")
    (property "Value" "10k")
    (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.1) (type default)) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask")
      (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask")
      (net 2 "/Sheet One/VCC_3V3"))
  )
  (footprint "MyLib:R0402"
    (layer "B.Cu")
    (at 12 6 0)
    (property "Reference" "R2")
    (property "Value" "1k")
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.4) (layers "*.Cu")
      (net 2 "/Sheet One/VCC_3V3"))
    (pad "2" smd rect (at 1 0) (size 0.5 0.6) (layers "B.Cu") (net 3 "Net-(U1-Pad2)"))
  )
  (segment (start 4.5 4) (end 4.5 8) (width 0.2) (layer "F.Cu") (net 1))
  (via (at 6 6) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""


@pytest.fixture
def kicad_design(tmp_path) -> Path:
    """A small but complete KiCad board, with a dot in its name on purpose."""
    stem = tmp_path / "widget_V1.5"
    with_ext(stem, kicad.PCB_SUFFIX).write_text(BOARD, encoding="utf-8")
    return stem


@pytest.fixture
def converted(kicad_design):
    return kicad.convert(kicad_design)


# -- the parser ------------------------------------------------------------

def test_nested_forms_parse():
    tree = sexp.parse('(root (a 1) (b "two words") (c (d 3)))')
    assert sexp.tag(tree) == "root"
    assert sexp.value(tree, "a") == "1"
    assert sexp.value(tree, "b") == "two words"
    assert sexp.value(sexp.first(tree, "c"), "d") == "3"


def test_escapes_inside_strings():
    tree = sexp.parse(r'(root (name "a\"b") (path "c\\d"))')
    assert sexp.value(tree, "name") == 'a"b'
    assert sexp.value(tree, "path") == "c\\d"


def test_unbalanced_input_is_refused():
    with pytest.raises(sexp.SexpError):
        sexp.parse("(root (a 1)")


def test_empty_input_is_refused():
    with pytest.raises(sexp.SexpError):
        sexp.parse("   ")


# -- filenames -------------------------------------------------------------

@pytest.mark.parametrize("name,stem", [
    ("board.kicad_pcb", "board"),
    ("board.kicad_sch", "board"),
    ("board.sch", "board"),
    ("widget_V1.5.kicad_pcb", "widget_V1.5"),
    ("widget_V1.5.brd", "widget_V1.5"),
])
def test_an_extension_is_stripped_without_cutting_the_name(name, stem):
    """A dot in the name is ordinary; `with_suffix` would eat part of it."""
    assert design_stem(Path("/tmp") / name).name == stem


def test_a_name_with_a_dot_survives_into_the_design_name():
    assert design_name(Path("/tmp/widget_V1.5")) == "widget_V1.5"


def test_the_other_half_is_named_by_adding_not_replacing():
    assert with_ext(Path("/tmp/widget_V1.5"), ".brd").name == "widget_V1.5.brd"


# -- what comes out --------------------------------------------------------

def test_a_board_becomes_an_eagle_pair(converted):
    assert converted.board.kind == "brd"
    assert converted.schematic.kind == "sch"


def test_every_footprint_becomes_an_element(converted):
    names = {e.get("name") for e in converted.board.elements()}
    assert names == {"R1", "R2"}


def test_a_shared_footprint_becomes_one_package(converted):
    packages = [p for lib in converted.board.libraries()
                for p in lib.iterfind("packages/package")]
    assert [p.get("name") for p in packages] == ["R0402"]


def test_values_carry_over(converted):
    values = {e.get("name"): e.get("value") for e in converted.board.elements()}
    assert values == {"R1": "10k", "R2": "1k"}


def test_the_outline_lands_on_the_dimension_layer(converted):
    plain = converted.board.section.find("plain")
    edges = [w for w in plain if w.get("layer") == "20"]
    assert len(edges) == 4


def test_the_board_keeps_its_size(converted):
    plain = converted.board.section.find("plain")
    xs = [float(w.get(a)) for w in plain for a in ("x1", "x2")]
    ys = [float(w.get(a)) for w in plain for a in ("y1", "y2")]
    assert max(xs) - min(xs) == pytest.approx(20.0)
    assert max(ys) - min(ys) == pytest.approx(10.0)


# -- the two conventions that differ ---------------------------------------

def test_y_is_measured_the_way_eagle_measures_it():
    """KiCad counts Y downwards; EAGLE counts it up."""
    assert kicad._xy(sexp.parse("(f (at 5 4))")) == (5.0, -4.0)


def test_rotation_turns_with_the_axis():
    """Flipping Y turns a counterclockwise rotation into a clockwise one."""
    assert kicad._angle(sexp.parse("(f (at 0 0 90))")) == 270.0
    assert kicad._angle(sexp.parse("(f (at 0 0 0))")) == 0.0


def test_a_placed_part_lands_where_it_was(converted):
    r1 = [e for e in converted.board.elements() if e.get("name") == "R1"][0]
    assert (float(r1.get("x")), float(r1.get("y"))) == (5.0, -4.0)
    assert r1.get("rot") == "R270"


def test_a_footprint_on_the_back_is_mirrored(converted):
    r2 = [e for e in converted.board.elements() if e.get("name") == "R2"][0]
    assert (r2.get("rot") or "").startswith("M")


def test_an_arc_becomes_a_wire_with_a_curve():
    """A quarter circle from (1,0) through (0.707,0.707) to (0,1)."""
    curve = kicad._arc_curve((1.0, 0.0), (math.sqrt(0.5), math.sqrt(0.5)), (0.0, 1.0))
    assert abs(abs(curve) - 90.0) < 1.0


def test_three_points_in_a_line_are_not_an_arc():
    assert kicad._arc_curve((0.0, 0.0), (1.0, 0.0), (2.0, 0.0)) == 0.0


# -- nets ------------------------------------------------------------------

def test_a_hierarchical_net_name_keeps_only_its_leaf(converted):
    """`/Sheet One/VCC_3V3` has to read as VCC_3V3 or no rail will match."""
    names = {s.get("name") for s in converted.board.signals()}
    assert "VCC_3V3" in names
    assert not any("Sheet" in n for n in names)


def test_a_name_kicad_invented_becomes_an_anonymous_one(converted):
    """So the resolver keeps it apart, exactly as it does EAGLE's N$1."""
    names = {s.get("name") for s in converted.board.signals()}
    assert "N$1" in names
    assert not any(n.startswith("Net-") for n in names)


def test_pads_are_filed_under_their_signal(converted):
    gnd = [s for s in converted.board.signals() if s.get("name") == "GND"][0]
    contacts = {(c.get("element"), c.get("pad")) for c in gnd.iterfind("contactref")}
    assert contacts == {("R1", "1")}


def test_tracks_and_vias_go_with_their_net(converted):
    gnd = [s for s in converted.board.signals() if s.get("name") == "GND"][0]
    assert len(gnd.findall("wire")) == 1
    assert len(gnd.findall("via")) == 1


def test_a_net_nothing_is_on_is_not_carried(converted):
    assert "" not in {s.get("name") for s in converted.board.signals()}


# -- the schematic drawn from the netlist ----------------------------------

def test_the_schematic_matches_the_board(converted):
    parts = {p.get("name") for p in converted.schematic.parts()}
    elements = {e.get("name") for e in converted.board.elements()}
    assert parts == elements

    sch_nets = set(converted.schematic.net_names())
    brd_nets = set(converted.board.net_names())
    assert brd_nets <= sch_nets


def test_every_pad_becomes_a_pin(converted):
    library = converted.schematic.libraries()[0]
    symbol = library.find("symbols/symbol")
    assert {p.get("name") for p in symbol.iterfind("pin")} == {"1", "2"}


def test_pins_map_to_the_pads_they_came_from(converted):
    library = converted.schematic.libraries()[0]
    connects = library.findall("devicesets/deviceset/devices/device/connects/connect")
    assert {(c.get("pin"), c.get("pad")) for c in connects} == {("1", "1"), ("2", "2")}


def test_the_conversion_says_the_schematic_is_generated(converted):
    assert any("board netlist" in note for note in converted.notes)


def test_a_board_without_a_schematic_still_converts(kicad_design):
    assert not with_ext(kicad_design, kicad.SCH_SUFFIX).exists()
    assert kicad.convert(kicad_design).schematic.parts()


def test_a_missing_board_is_refused(tmp_path):
    with pytest.raises(kicad.KiCadError):
        kicad.convert(tmp_path / "nothing")


def test_a_file_that_is_not_a_board_is_refused(tmp_path):
    stem = tmp_path / "wrong"
    with_ext(stem, kicad.PCB_SUFFIX).write_text("(kicad_sch (version 1))", encoding="utf-8")
    with pytest.raises(kicad.KiCadError):
        kicad.convert(stem)


# -- through the tool ------------------------------------------------------

def test_a_kicad_design_is_found_in_a_folder(kicad_design):
    specs = collect_specs([str(kicad_design.parent)])
    assert len(specs) == 1
    assert specs[0].brd.endswith(kicad.PCB_SUFFIX)


def test_a_kicad_design_can_be_named_directly(kicad_design):
    specs = collect_specs([str(with_ext(kicad_design, kicad.PCB_SUFFIX))])
    assert specs[0].name == "widget_V1.5"


def test_it_merges_like_any_other_design(kicad_design, tmp_path):
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(kicad_design.parent),
                 "--out-dir", str(out), "-o", "combo", "--yes"]) == 0
    assert main(["--no-color", "check", str(out / "combo")]) == 0


def test_kicad_and_eagle_merge_together(kicad_design, designs, tmp_path):
    """The whole point: one board built from designs drawn in both tools."""
    out = tmp_path / "out"
    inputs = [str(designs), str(with_ext(kicad_design, kicad.PCB_SUFFIX))]
    assert main(["--no-color", "merge", *inputs,
                 "--out-dir", str(out), "-o", "combo", "--yes"]) == 0

    brd = EagleDoc.load(out / "combo.brd")
    prefixes = {e.get("name").split("_")[0] for e in brd.elements()}
    assert len(prefixes) == 3, "two EAGLE designs and one from KiCad"
    assert main(["--no-color", "check", str(out / "combo")]) == 0


def test_ground_joins_across_both_tools(kicad_design, designs, tmp_path):
    specs = collect_specs([str(designs), str(with_ext(kicad_design, kicad.PCB_SUFFIX))])
    resolver = build_resolver(load_designs(expand(specs)))
    resolver.finalize()
    assert resolver.group_for("GND").design_count == 3


def test_the_merge_says_what_it_converted(kicad_design, tmp_path, capsys):
    main(["--no-color", "merge", str(kicad_design.parent), "--out-dir",
          str(tmp_path / "out"), "-o", "combo", "--yes"])
    assert "Converted from KiCad" in capsys.readouterr().out
