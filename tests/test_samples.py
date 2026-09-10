"""Integration tests against the real designs in `examples/basic`.

One was drawn in EAGLE and one in KiCad, which is the point: they merge without
the engine knowing the difference.

Nothing here names a design, a library or a net. The folder is meant to be
changed -- swap the examples and these still say something true -- so every test
derives what it needs from what is actually there and asserts the rule rather
than the answer.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from pcbmerge import kicad
from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.merge import build_resolver, load_designs, merge
from pcbmerge.nets import Action, Kind
from pcbmerge.plan import MergePlan, expand


@pytest.fixture(scope="module")
def merged(samples, tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    specs = collect_specs([str(samples)])
    designs = load_designs(expand(specs))
    resolver = build_resolver(designs)
    resolver.finalize(default_action=Action.SPLIT)
    plan = MergePlan(output="combo", designs=specs, layout="grid", optimize="none")
    report = merge(designs, resolver, plan, out, "combo")
    prefixes = {d.source: d.prefix for d in designs}
    return report, resolver, prefixes


def eagle_specs(samples):
    return [s for s in collect_specs([str(samples)]) if s.sch.endswith(".sch")]


def kicad_specs(samples):
    return [s for s in collect_specs([str(samples)])
            if s.sch.endswith((kicad.SCH_SUFFIX, kicad.PCB_SUFFIX))]


# --------------------------------------------------------------------------
# the merge as a whole
# --------------------------------------------------------------------------

def test_every_design_in_the_folder_merges(merged, samples):
    report, _, _ = merged
    expected = len(collect_specs([str(samples)]))

    assert expected >= 2, "the folder is meant to hold more than one design"
    assert len(report.designs) == expected
    assert report.parts > 0
    assert report.sheets == 1


def test_both_tools_are_represented(samples):
    """The samples exist to prove the two formats merge together."""
    assert eagle_specs(samples), "no EAGLE design in the folder"
    assert kicad_specs(samples), "no KiCad design in the folder"


def test_the_kicad_design_is_converted_and_says_so(merged, samples):
    report, _, _ = merged
    wanted = {s.name for s in kicad_specs(samples)}

    assert wanted <= set(report.designs)
    assert report.converted, "and the merge says what it did to it"


def test_a_drawn_kicad_schematic_is_used_as_drawn(merged, samples):
    """The fallback exists, but a published drawing should not need it."""
    report, _, _ = merged
    if not any(s.sch.endswith(kicad.SCH_SUFFIX) for s in kicad_specs(samples)):
        pytest.skip("no KiCad design here ships its drawing")

    assert any("converted as drawn" in note for note in report.converted)


def test_ground_joins_across_every_design(merged, samples):
    report, resolver, _ = merged
    ground = resolver.group_for("GND")

    assert ground.design_count == len(collect_specs([str(samples)]))
    assert ground.action is Action.JOIN

    brd = EagleDoc.load(report.brd_path)
    gnd = [s for s in brd.signals() if s.get("name") == "GND"]
    assert len(gnd) == 1
    reached = {c.get("element").split("_")[0] for c in gnd[0].iterfind(".//contactref")}
    assert len(reached) == len(report.designs), "GND gathers copper from every board"


def test_a_shared_name_that_is_not_a_rail_is_asked_about(merged):
    """Nothing contested is joined quietly; that is the whole net policy."""
    _, resolver, _ = merged
    asked = {g.key for g in resolver.open_questions()}

    for group in resolver.all_groups():
        if group.design_count > 1 and group.kind in (Kind.AMBIGUOUS, Kind.SIGNAL):
            assert group.key in asked or group.decided_by, group.key


# --------------------------------------------------------------------------
# what EAGLE will refuse to open
# --------------------------------------------------------------------------

def test_every_library_reference_resolves(merged):
    report, _, _ = merged
    sch = EagleDoc.load(report.sch_path)
    brd = EagleDoc.load(report.brd_path)

    for doc, wanted in ((sch, "deviceset"), (brd, "package")):
        index = {}
        for lib in doc.libraries():
            index[lib.get("name")] = {
                "deviceset": {d.get("name") for d in lib.iterfind("devicesets/deviceset")},
                "package": {p.get("name") for p in lib.iterfind("packages/package")},
            }
        items = doc.parts() if wanted == "deviceset" else doc.elements()
        for item in items:
            assert item.get(wanted) in index[item.get("library")][wanted], item.get("name")


def test_no_library_holds_two_things_of_one_name(merged):
    """Merging libraries renames what clashes; nothing may be silently lost."""
    report, _, _ = merged
    for path in (report.sch_path, report.brd_path):
        for lib in EagleDoc.load(path).libraries():
            for kind in ("packages/package", "symbols/symbol", "devicesets/deviceset"):
                names = [n.get("name") for n in lib.iterfind(kind)]
                assert len(names) == len(set(names)), f"{lib.get('name')} {kind}"


def test_devicesets_point_at_symbols_and_packages_that_exist(merged):
    report, _, _ = merged
    sch = EagleDoc.load(report.sch_path)
    for lib in sch.libraries():
        symbols = {s.get("name") for s in lib.iterfind("symbols/symbol")}
        packages = {p.get("name") for p in lib.iterfind("packages/package")}
        for deviceset in lib.iterfind("devicesets/deviceset"):
            for gate in deviceset.iterfind("gates/gate"):
                assert gate.get("symbol") in symbols
            for device in deviceset.iterfind("devices/device"):
                if device.get("package"):
                    assert device.get("package") in packages


def test_reference_designators_are_unique_across_them_all(merged):
    report, _, _ = merged
    names = [p.get("name") for p in EagleDoc.load(report.sch_path).parts()]

    assert len(names) == len(set(names))


def test_every_board_signal_has_a_schematic_net(merged):
    """The pair does not open otherwise, whatever else is right about it.

    Orphan copper is inherited from the source boards, so this allows what the
    sources already carry and nothing more.
    """
    report, _, _ = merged
    sch = set(EagleDoc.load(report.sch_path).net_names())
    brd = set(EagleDoc.load(report.brd_path).net_names())

    stray = sorted(brd - sch)
    assert not stray, f"board signals with no schematic net: {stray}"


def test_merged_files_reopen_cleanly(merged):
    report, _, _ = merged
    for path in (report.sch_path, report.brd_path):
        ET.parse(path)  # raises on malformed output


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def test_no_two_boards_overlap(merged):
    report, _, _ = merged
    brd = EagleDoc.load(report.brd_path)
    boxes: dict[str, list[float]] = {}
    for element in brd.elements():
        prefix = element.get("name").split("_")[0]
        x, y = float(element.get("x")), float(element.get("y"))
        box = boxes.setdefault(prefix, [x, y, x, y])
        box[0], box[1] = min(box[0], x), min(box[1], y)
        box[2], box[3] = max(box[2], x), max(box[3], y)

    keys = sorted(boxes)
    for i, first in enumerate(keys):
        for second in keys[i + 1:]:
            a, b = boxes[first], boxes[second]
            overlap = a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
            assert not overlap, f"{first} overlaps {second}"


def test_a_source_board_survives_as_a_rigid_translation(merged, samples):
    """A board is moved, never redrawn: one offset for every part on it."""
    report, _, prefixes = merged
    spec = eagle_specs(samples)[0]
    prefix = prefixes[spec.name]
    source = EagleDoc.load(spec.brd)
    merged_doc = EagleDoc.load(report.brd_path)

    original = {e.get("name"): (float(e.get("x")), float(e.get("y")))
                for e in source.elements()}
    moved = {e.get("name")[len(prefix):]: (float(e.get("x")), float(e.get("y")))
             for e in merged_doc.elements() if e.get("name").startswith(prefix)}

    assert set(original) == set(moved)
    deltas = {(round(moved[k][0] - original[k][0], 4),
               round(moved[k][1] - original[k][1], 4)) for k in original}
    assert len(deltas) == 1


def test_rotations_survive_the_move(merged, samples):
    report, _, prefixes = merged
    spec = eagle_specs(samples)[0]
    prefix = prefixes[spec.name]
    source = {e.get("name"): e.get("rot", "")
              for e in EagleDoc.load(spec.brd).elements()}

    for element in EagleDoc.load(report.brd_path).elements():
        name = element.get("name")
        if name.startswith(prefix):
            assert element.get("rot", "") == source[name[len(prefix):]]


# --------------------------------------------------------------------------
# the command line, end to end
# --------------------------------------------------------------------------

def test_the_check_command_passes_on_what_merge_writes(samples, tmp_path):
    """`check` is the gate; it should pass on a merge of the samples."""
    out = tmp_path / "out"
    assert main(["--no-color", "merge", str(samples), "-o", "combo",
                 "--out-dir", str(out), "--yes"]) == 0

    assert main(["--no-color", "check", str(out / "combo")]) == 0
