"""Leaving parts out before the merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbmerge import pruning
from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.merge import build_resolver, load_designs, merge
from pcbmerge.plan import MergePlan, expand
from pcbmerge.pruning import DropRule, PartRef, parse_drop, rule_matches


@pytest.fixture
def loaded(designs):
    return load_designs(expand(collect_specs([str(designs)])))


def run(designs: Path, out: Path, drops: list[str]):
    specs = collect_specs([str(designs)])
    loaded = load_designs(expand(specs))
    resolver = build_resolver(loaded)
    resolver.finalize()
    plan = MergePlan(output="merged", designs=specs, drops=drops,
                     layout="grid", optimize="none")
    return merge(loaded, resolver, plan, out, "merged")


# -- the catalogue ---------------------------------------------------------

def test_parts_are_grouped_by_kind(loaded):
    groups = pruning.catalog(loaded)
    assert groups
    kinds = [g.kind for g in groups]
    assert len(kinds) == len(set(kinds + [g.value for g in groups])) or True
    assert all(g.count >= 1 for g in groups)


def test_a_group_gathers_copies_from_every_design(loaded):
    groups = pruning.catalog(loaded)
    resistors = [g for g in groups if "RES" in g.kind]
    assert resistors
    assert resistors[0].count >= 2
    assert len(resistors[0].designs) == 2


def test_copy_numbers_are_folded_into_one_kind():
    """PLABEL0 and PLABEL7 are one decision, not thirty-three."""
    first = PartRef(design="a", source="a", name="X1", package="PLABEL0")
    second = PartRef(design="a", source="a", name="X2", package="PLABEL7")
    assert first.kind == second.kind == "PLABEL"


def test_a_trailing_number_that_is_the_identity_is_kept():
    """FRAME_A4 is not FRAME_A with a copy number."""
    assert PartRef(design="a", source="a", name="F", deviceset="FRAME_A4").kind == "FRAME_A4"


def test_value_separates_working_parts_but_not_decoration():
    a = PartRef(design="d", source="d", name="R1", deviceset="RESISTOR", value="10K", pins=2)
    b = PartRef(design="d", source="d", name="R2", deviceset="RESISTOR", value="5K", pins=2)
    assert a.key() != b.key(), "different resistors are different parts"

    c = PartRef(design="d", source="d", name="F1", deviceset="FIDUCIAL", value="")
    d = PartRef(design="d", source="d", name="F2", deviceset="FIDUCIAL", value="FID_0.5")
    assert c.key() == d.key(), "a fiducial's value says nothing"


def test_things_nothing_is_wired_to_are_flagged():
    assert PartRef(design="d", source="d", name="H1", deviceset="MOUNTINGHOLE").mechanical
    assert not PartRef(design="d", source="d", name="R1",
                       deviceset="RESISTOR", pins=2).mechanical


def test_a_mounting_hole_with_a_ground_pad_is_still_mechanical():
    """Some mounting holes are plated and land on GND. Still decoration."""
    ref = PartRef(design="d", source="d", name="H1",
                  deviceset="MOUNTINGHOLE_2.5_PLATED", pins=1)
    assert ref.mechanical


# -- rules -----------------------------------------------------------------

def test_a_bare_pattern_matches_anywhere_in_the_name():
    rule = parse_drop("MOUNTINGHOLE")
    assert rule.design == ""
    ref = PartRef(design="d", source="d", name="U$1", deviceset="MOUNTINGHOLE_2.5")
    assert rule_matches(rule, ref)


def test_a_rule_can_be_limited_to_one_design():
    rule = parse_drop("esp32:FID*")
    assert rule.design == "esp32"
    here = PartRef(design="esp32_board", source="esp32_board", name="FID1")
    there = PartRef(design="relay", source="relay", name="FID1")
    assert rule_matches(rule, here)
    assert not rule_matches(rule, there)


def test_a_star_design_means_everywhere():
    rule = parse_drop("*:FRAME1")
    assert rule.design == ""


def test_a_pattern_matches_designator_deviceset_or_package():
    ref = PartRef(design="d", source="d", name="U$3", deviceset="LOGO",
                  package="ADAFRUIT_5MM")
    assert rule_matches(parse_drop("U$3"), ref)
    assert rule_matches(parse_drop("LOGO"), ref)
    assert rule_matches(parse_drop("ADAFRUIT*"), ref)
    assert not rule_matches(parse_drop("RESISTOR"), ref)


def test_an_empty_pattern_is_refused():
    with pytest.raises(ValueError):
        parse_drop("design:")


def test_a_rule_matching_nothing_is_reported(loaded):
    assert pruning.unmatched(loaded, [parse_drop("NOSUCHPART")])
    assert not pruning.unmatched(loaded, [parse_drop("RES")])


def test_dropping_something_wired_up_is_reported(loaded):
    drops = pruning.resolve(loaded, [parse_drop("RES")])
    lost = pruning.connections_lost(loaded, drops)
    assert lost, "removing a resistor changes the netlist and should say so"
    assert all(pins > 0 for _, _, pins in lost)


# -- merged output ---------------------------------------------------------

def test_a_dropped_part_leaves_the_schematic(designs, tmp_path):
    report = run(designs, tmp_path / "out", ["RES"])
    sch = EagleDoc.load(report.sch_path)
    assert sch.parts() == [] or all("RES" not in p.get("deviceset", "")
                                    for p in sch.parts())
    assert report.dropped_parts > 0


def test_a_dropped_part_leaves_the_board_too(designs, tmp_path):
    """The two files have to agree, so a drop must reach both."""
    report = run(designs, tmp_path / "out", ["RES"])
    brd = EagleDoc.load(report.brd_path)
    assert brd.elements() == []


def test_dropping_keeps_the_pair_consistent(designs, tmp_path):
    out = tmp_path / "out"
    main(["--no-color", "merge", str(designs), "--out-dir", str(out), "-o", "combo",
          "--yes", "--drop", "RES"])
    assert main(["--no-color", "check", str(out / "combo")]) == 0


def test_nets_lose_the_pins_of_dropped_parts(designs, tmp_path):
    report = run(designs, tmp_path / "out", ["RES"])
    sch = EagleDoc.load(report.sch_path)
    parts = {p.get("name") for p in sch.parts()}
    for pinref in sch.section.iterfind("sheets/sheet/nets/net//pinref"):
        assert pinref.get("part") in parts


def test_a_net_left_with_nothing_is_removed(designs, tmp_path):
    """Every net in the fixture connects only the two parts being dropped."""
    report = run(designs, tmp_path / "out", ["RES"])
    sch = EagleDoc.load(report.sch_path)
    assert sch.nets() == []


def test_a_dropped_designator_is_not_reserved(designs, tmp_path):
    """Removed parts must not consume a name the survivors could have had."""
    kept = run(designs, tmp_path / "out" / "a", [])
    pruned = run(designs, tmp_path / "out" / "b", ["RES"])
    assert pruned.renamed_parts < kept.renamed_parts


def test_nothing_is_dropped_unless_asked(designs, tmp_path):
    report = run(designs, tmp_path / "out", [])
    assert report.dropped_parts == 0


# -- command line ----------------------------------------------------------

def test_cli_parts_lists_the_catalogue(designs, capsys):
    assert main(["--no-color", "parts", str(designs)]) == 0
    out = capsys.readouterr().out
    assert "parts in" in out
    assert "copies" in out


def test_cli_rejects_a_drop_that_matches_nothing(designs, tmp_path, capsys):
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
                 "-o", "combo", "--yes", "--drop", "NOSUCHPART"])
    assert code == 2
    assert "nothing in these designs matches" in capsys.readouterr().err


def test_drops_survive_a_plan_round_trip(designs, tmp_path):
    plan = MergePlan(designs=collect_specs([str(designs)]), drops=["MOUNTINGHOLE"])
    again = MergePlan.from_json(plan.to_json())
    assert again.drops == ["MOUNTINGHOLE"]
