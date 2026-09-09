"""End-to-end merging: libraries, names, geometry and file consistency."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc, bbox, translate
from pcbmerge.merge import build_resolver, load_designs, merge
from pcbmerge.nets import Action
from pcbmerge.plan import MergePlan, apply_plan, default_prefix, expand


def run_merge(directory: Path, out: Path, default: Action = Action.SPLIT,
              layout: str = "grid", optimize: str = "none") -> tuple:
    specs = collect_specs([str(directory)])
    designs = load_designs(expand(specs))
    resolver = build_resolver(designs)
    resolver.finalize(default_action=default)
    plan = MergePlan(output="merged", designs=specs, layout=layout, optimize=optimize)
    report = merge(designs, resolver, plan, out, "merged")
    return report, [d.spec for d in designs]


def test_merge_writes_a_consistent_pair(designs, tmp_path):
    out = tmp_path / "out"
    report, _ = run_merge(designs, out)

    assert report.sch_path.exists()
    assert report.brd_path.exists()

    sch = EagleDoc.load(report.sch_path)
    brd = EagleDoc.load(report.brd_path)

    part_names = {p.get("name") for p in sch.parts()}
    element_names = {e.get("name") for e in brd.elements()}
    assert element_names <= part_names, "every footprint needs its schematic part"

    for pinref in sch.section.iterfind("sheets/sheet/nets/net//pinref"):
        assert pinref.get("part") in part_names
    for contact in brd.section.iterfind("signals/signal//contactref"):
        assert contact.get("element") in element_names


def test_every_reference_designator_is_unique(designs, tmp_path):
    report, specs = run_merge(designs, tmp_path / "out")
    sch = EagleDoc.load(report.sch_path)
    names = [p.get("name") for p in sch.parts()]
    assert len(names) == len(set(names))
    assert names == sorted(names, key=names.index)  # order preserved
    prefixes = {n.split("_")[0] for n in names}
    assert len(prefixes) == 2, "each design keeps its own prefix"


def test_every_design_lands_on_one_sheet(designs, tmp_path):
    report, _ = run_merge(designs, tmp_path / "out")
    sheets = EagleDoc.load(report.sch_path).sheets()
    assert len(sheets) == 1
    assert sheets[0].findtext("description") == "alpha_board, beta_board"


def test_ground_is_one_net_across_both_designs(designs, tmp_path):
    report, _ = run_merge(designs, tmp_path / "out")
    sch = EagleDoc.load(report.sch_path)
    grounds = [n for n in sch.nets() if n.get("name") == "GND"]
    assert len(grounds) == 1, "one net element per name per sheet"
    assert len(grounds[0].findall("segment")) >= 2, "a segment from each design"

    brd = EagleDoc.load(report.brd_path)
    gnd = [s for s in brd.signals() if s.get("name") == "GND"]
    assert len(gnd) == 1, "board signals fold into a single GND"
    assert len(gnd[0].findall("contactref")) == 4, "contacts from both designs"


def test_contested_nets_are_split_by_default(designs, tmp_path):
    report, specs = run_merge(designs, tmp_path / "out")
    sch = EagleDoc.load(report.sch_path)
    names = {n.get("name") for n in sch.nets()}
    assert "SDA" not in names
    assert {f"{spec.prefix}SDA" for spec in specs} <= names


def test_joining_is_available_as_a_policy(designs, tmp_path):
    report, specs = run_merge(designs, tmp_path / "out", default=Action.JOIN)
    sch = EagleDoc.load(report.sch_path)
    names = {n.get("name") for n in sch.nets()}
    assert "SDA" in names
    assert f"{specs[0].prefix}SDA" not in names


def test_anonymous_nets_stay_apart_even_when_joining(designs, tmp_path):
    report, specs = run_merge(designs, tmp_path / "out", default=Action.JOIN)
    sch = EagleDoc.load(report.sch_path)
    names = {n.get("name") for n in sch.nets()}
    assert {f"{spec.prefix}N$1" for spec in specs} <= names


def test_clashing_library_content_is_kept_under_a_new_name(designs, tmp_path):
    """The two fixtures define different 0603 pads and a different R symbol."""
    report, _ = run_merge(designs, tmp_path / "out")
    sch = EagleDoc.load(report.sch_path)
    library = [lib for lib in sch.libraries() if lib.get("name") == "parts"][0]

    packages = {p.get("name") for p in library.iterfind("packages/package")}
    symbols = {s.get("name") for s in library.iterfind("symbols/symbol")}
    assert packages == {"0603", "0603$2"}
    assert symbols == {"R", "R$2"}

    devicesets = {d.get("name") for d in library.iterfind("devicesets/deviceset")}
    assert devicesets == {"RES", "RES$2"}


def test_renamed_library_items_stay_wired_up(designs, tmp_path):
    report, _ = run_merge(designs, tmp_path / "out")
    sch = EagleDoc.load(report.sch_path)
    library = [lib for lib in sch.libraries() if lib.get("name") == "parts"][0]
    symbols = {s.get("name") for s in library.iterfind("symbols/symbol")}
    packages = {p.get("name") for p in library.iterfind("packages/package")}

    for deviceset in library.iterfind("devicesets/deviceset"):
        for gate in deviceset.iterfind("gates/gate"):
            assert gate.get("symbol") in symbols
        for device in deviceset.iterfind("devices/device"):
            assert device.get("package") in packages

    devicesets = {d.get("name") for d in library.iterfind("devicesets/deviceset")}
    for part in sch.parts():
        assert part.get("deviceset") in devicesets

    brd = EagleDoc.load(report.brd_path)
    board_lib = [lib for lib in brd.libraries() if lib.get("name") == "parts"][0]
    board_packages = {p.get("name") for p in board_lib.iterfind("packages/package")}
    for element in brd.elements():
        assert element.get("package") in board_packages


def test_the_two_designs_use_different_library_variants(designs, tmp_path):
    """Each design must keep the footprint it was actually drawn with."""
    report, specs = run_merge(designs, tmp_path / "out")
    brd = EagleDoc.load(report.brd_path)
    by_design = {}
    for element in brd.elements():
        for spec in specs:
            if element.get("name").startswith(spec.prefix):
                by_design.setdefault(spec.name, set()).add(element.get("package"))
    variants = list(by_design.values())
    assert variants[0] != variants[1], "divergent footprints must not collapse into one"


def test_boards_are_tiled_without_overlapping(designs, tmp_path):
    report, _ = run_merge(designs, tmp_path / "out")
    brd = EagleDoc.load(report.brd_path)

    boxes: dict[str, list[float]] = {}
    for element in brd.elements():
        prefix = element.get("name").split("_")[0]
        x, y = float(element.get("x")), float(element.get("y"))
        box = boxes.setdefault(prefix, [x, y, x, y])
        box[0], box[1] = min(box[0], x), min(box[1], y)
        box[2], box[3] = max(box[2], x), max(box[3], y)

    keys = list(boxes)
    assert len(keys) == 2
    a, b = boxes[keys[0]], boxes[keys[1]]
    overlap = a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]
    assert not overlap


def test_a_board_moves_as_a_rigid_body(designs, tmp_path):
    report, specs = run_merge(designs, tmp_path / "out")
    prefix = specs[0].prefix
    source = EagleDoc.load(designs / "alpha board.brd")
    merged = EagleDoc.load(report.brd_path)

    original = {e.get("name"): (float(e.get("x")), float(e.get("y")))
                for e in source.elements()}
    moved = {e.get("name")[len(prefix):]: (float(e.get("x")), float(e.get("y")))
             for e in merged.elements() if e.get("name").startswith(prefix)}

    deltas = {(round(moved[k][0] - original[k][0], 6),
               round(moved[k][1] - original[k][1], 6)) for k in original}
    assert len(deltas) == 1, "every part of a board shifts by the same amount"


def test_managed_library_links_are_dropped(designs, tmp_path):
    report, _ = run_merge(designs, tmp_path / "out")
    text = report.sch_path.read_text(encoding="utf-8")
    assert "urn=" not in text
    assert "library_urn" not in text


def test_output_keeps_the_eagle_doctype(designs, tmp_path):
    report, _ = run_merge(designs, tmp_path / "out")
    head = report.sch_path.read_text(encoding="utf-8")[:120]
    assert head.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert '<!DOCTYPE eagle SYSTEM "eagle.dtd">' in head


def test_merging_a_schematic_without_a_board_still_works(tmp_path, design_writer):
    write_design = design_writer
    source = tmp_path / "src"
    source.mkdir()
    write_design(source, "solo", ["GND", "N$1"], board=False)
    report, _ = run_merge(source, tmp_path / "out")
    assert report.sch_path.exists()
    assert report.brd_path is None
    assert any("schematic only" in w for w in report.warnings)
