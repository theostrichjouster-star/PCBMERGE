"""Placing several copies of one design."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.merge import build_resolver, load_designs, merge
from pcbmerge.nets import Action, Kind
from pcbmerge.plan import DesignSpec, MergePlan, expand


@pytest.fixture
def relay(tmp_path, design_writer) -> Path:
    """One design with a rail, a local signal and an anonymous net."""
    source = tmp_path / "src"
    source.mkdir()
    design_writer(source, "relay", ["GND", "3.3V", "SIGNAL", "N$1"])
    return source


def run(source: Path, out: Path, count: int, replicas: Action = Action.SPLIT):
    specs = collect_specs([str(source)], counts=[str(count)])
    designs = load_designs(expand(specs))
    resolver = build_resolver(designs)
    resolver.finalize(replica_action=replicas)
    plan = MergePlan(output="merged", designs=specs, layout="grid", optimize="none")
    report = merge(designs, resolver, plan, out, "merged")
    return report, resolver, [d.prefix for d in designs]


# -- expansion -------------------------------------------------------------

def test_one_copy_is_left_unnumbered():
    spec = DesignSpec(name="relay", prefix="RELAY_", sch="relay.sch", count=1)
    instances = expand([spec])
    assert len(instances) == 1
    assert instances[0].prefix == "RELAY_"
    assert instances[0].name == "relay"


def test_copies_are_numbered_in_the_prefix():
    spec = DesignSpec(name="relay", prefix="RELAY_", sch="relay.sch", count=4)
    instances = expand([spec])
    assert [i.prefix for i in instances] == ["RELAY1_", "RELAY2_", "RELAY3_", "RELAY4_"]
    assert all(i.source == "relay" for i in instances)
    assert [i.index for i in instances] == [1, 2, 3, 4]


def test_a_count_can_be_written_on_the_input_path(relay):
    specs = collect_specs([f"{relay / 'relay'}*3"])
    assert specs[0].count == 3


def test_count_flag_overrides_the_path_suffix(relay):
    specs = collect_specs([f"{relay / 'relay'}*3"], counts=["5"])
    assert specs[0].count == 5


# -- merged output ---------------------------------------------------------

def test_every_copy_gets_its_own_parts(relay, tmp_path):
    report, _, prefixes = run(relay, tmp_path / "out", 4)
    sch = EagleDoc.load(report.sch_path)
    names = [p.get("name") for p in sch.parts()]
    assert len(names) == len(set(names))
    assert len(set(prefixes)) == 4, "one prefix per copy"
    for prefix in prefixes:
        assert any(n.startswith(prefix) for n in names)


def test_local_signals_increment_with_the_copy(relay, tmp_path):
    report, _, prefixes = run(relay, tmp_path / "out", 3)
    sch = EagleDoc.load(report.sch_path)
    names = {n.get("name") for n in sch.nets()}
    assert {f"{p}SIGNAL" for p in prefixes} <= names
    assert "SIGNAL" not in names


def test_anonymous_nets_increment_too(relay, tmp_path):
    report, _, prefixes = run(relay, tmp_path / "out", 3)
    sch = EagleDoc.load(report.sch_path)
    names = {n.get("name") for n in sch.nets()}
    assert {f"{p}N$1" for p in prefixes} <= names


def test_rails_stay_common_across_every_copy(relay, tmp_path):
    report, _, _ = run(relay, tmp_path / "out", 4)
    sch = EagleDoc.load(report.sch_path)
    names = [n.get("name") for n in sch.nets()]
    assert names.count("GND") == 4, "one GND net drawn on each copy's sheet"
    assert not any(n.endswith("_GND") for n in names)

    brd = EagleDoc.load(report.brd_path)
    gnd = [s for s in brd.signals() if s.get("name") == "GND"]
    assert len(gnd) == 1, "and a single GND signal on the board"


def test_a_replicated_signal_is_offered_as_a_question(relay, tmp_path):
    _, resolver, _ = run(relay, tmp_path / "out", 4)
    questions = {g.display for g in resolver.replica_questions()}
    assert "SIGNAL" in questions
    assert "GND" not in questions, "rails are settled by rule"


def test_replica_signals_can_be_made_common(relay, tmp_path):
    report, _, prefixes = run(relay, tmp_path / "out", 3, replicas=Action.JOIN)
    sch = EagleDoc.load(report.sch_path)
    names = [n.get("name") for n in sch.nets()]
    assert names.count("SIGNAL") == 3
    assert f"{prefixes[0]}SIGNAL" not in names


def test_copies_do_not_overlap_on_the_board(relay, tmp_path):
    report, _, _ = run(relay, tmp_path / "out", 4)
    brd = EagleDoc.load(report.brd_path)
    boxes: dict[str, list[float]] = {}
    for element in brd.elements():
        prefix = element.get("name").split("_")[0]
        x, y = float(element.get("x")), float(element.get("y"))
        box = boxes.setdefault(prefix, [x, y, x, y])
        box[0], box[1] = min(box[0], x), min(box[1], y)
        box[2], box[3] = max(box[2], x), max(box[3], y)

    keys = sorted(boxes)
    assert len(keys) == 4
    for i, first in enumerate(keys):
        for second in keys[i + 1:]:
            a, b = boxes[first], boxes[second]
            assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])


def test_each_copy_becomes_its_own_sheet(relay, tmp_path):
    report, _, _ = run(relay, tmp_path / "out", 3)
    sch = EagleDoc.load(report.sch_path)
    labels = [s.findtext("description") for s in sch.sheets()]
    assert labels == ["relay #1", "relay #2", "relay #3"]


def test_the_report_says_how_many_copies_were_placed(relay, tmp_path):
    report, _, _ = run(relay, tmp_path / "out", 4)
    assert report.copies == {"relay": 4}


def test_source_files_are_only_parsed_once_per_path(relay):
    specs = collect_specs([str(relay)], counts=["4"])
    designs = load_designs(expand(specs))
    assert len({id(d.sch) for d in designs}) == 1, "four instances share one parse"


def test_cli_places_copies_from_the_star_syntax(relay, tmp_path):
    code = main(["--no-color", "merge", f"{relay / 'relay'}*3",
                 "--out-dir", str(tmp_path / "out"), "-o", "combo", "--yes"])
    assert code == 0
    sch = EagleDoc.load(tmp_path / "out" / "combo.sch")
    assert len(sch.sheets()) == 3
