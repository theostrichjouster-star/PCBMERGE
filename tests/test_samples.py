"""Integration tests against the real Adafruit designs in examples/."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

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


def test_all_eight_designs_merge(merged):
    report, _, prefixes = merged
    assert len(report.designs) == 8
    assert report.parts > 300
    assert report.sheets == 1


def test_ground_joins_across_every_design(merged):
    report, resolver, prefixes = merged
    ground = resolver.group_for("GND")
    assert ground.design_count == 8
    assert ground.action is Action.JOIN

    brd = EagleDoc.load(report.brd_path)
    gnd = [s for s in brd.signals() if s.get("name") == "GND"]
    assert len(gnd) == 1
    prefixes = {c.get("element").split("_")[0] for c in gnd[0].iterfind(".//contactref")}
    assert len(prefixes) == 8, "GND gathers copper from all eight boards"


def test_the_i2c_bus_is_offered_as_a_question_not_assumed(merged):
    _, resolver, prefixes = merged
    questions = {g.key for g in resolver.open_questions()}
    assert {"SDA", "SCL", "VIN"} <= questions


def test_divergent_libraries_are_all_preserved(merged):
    report, _, prefixes = merged
    sch = EagleDoc.load(report.sch_path)
    library = [lib for lib in sch.libraries() if lib.get("name") == "microbuilder"][0]
    packages = [p.get("name") for p in library.iterfind("packages/package")]
    assert len(packages) == len(set(packages))
    assert any("$" in name for name in packages), "clashing footprints kept side by side"


def test_every_library_reference_resolves(merged):
    report, _, prefixes = merged
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


def test_devicesets_point_at_symbols_and_packages_that_exist(merged):
    report, _, prefixes = merged
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


def test_reference_designators_are_unique_across_all_eight(merged):
    report, _, prefixes = merged
    sch = EagleDoc.load(report.sch_path)
    names = [p.get("name") for p in sch.parts()]
    assert len(names) == len(set(names))


def test_no_two_boards_overlap(merged):
    report, _, prefixes = merged
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
    report, _, prefixes = merged
    prefix = prefixes["Adafruit_MAX31850"]
    source = EagleDoc.load(samples / "Adafruit MAX31850.brd")
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
    prefix = prefixes["Adafruit_MAX31850"]
    source = {e.get("name"): e.get("rot", "")
              for e in EagleDoc.load(samples / "Adafruit MAX31850.brd").elements()}
    merged_doc = EagleDoc.load(report.brd_path)
    for element in merged_doc.elements():
        name = element.get("name")
        if name.startswith(prefix):
            assert element.get("rot", "") == source[name[len(prefix):]]


def test_merged_files_reopen_cleanly(merged):
    report, _, prefixes = merged
    for path in (report.sch_path, report.brd_path):
        ET.parse(path)  # raises on malformed output
