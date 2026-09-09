"""Wiring one design's net to another's, by hand."""

from __future__ import annotations

import pytest

from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc
from pcbmerge.merge import build_resolver, load_designs
from pcbmerge.nets import Action, NetResolver
from pcbmerge.plan import ConnectDecision, MergePlan, apply_plan, expand, plan_from_resolver
from pcbmerge.prompt import parse_connection


def farm() -> NetResolver:
    """A controller and three copies of one relay board."""
    r = NetResolver()
    r.add("esp32", ["GPIO5", "GPIO6", "GND"], source="esp32")
    for index in (1, 2, 3):
        r.add(f"relay #{index}", ["SIGNAL", "GND"], source="relay")
    r.finalize()
    return r


# -- the gap this closes ---------------------------------------------------

def test_a_link_cannot_reach_one_copy_only():
    """Links act on a name everywhere, which is why connections exist."""
    resolver = farm()
    resolver.link(["GPIO5", "SIGNAL"], "CTRL")
    resolver.finalize()
    assert len(resolver.group_for("SIGNAL").designs) == 4


def test_a_connection_reaches_exactly_the_named_designs():
    resolver = farm()
    resolver.connect([("esp32", "GPIO5"), ("relay #1", "SIGNAL")], "CTRL1")
    resolver.finalize()

    wired = resolver.group_for("SIGNAL", design="relay #1")
    assert sorted(wired.designs) == ["esp32", "relay #1"]
    assert wired.merged_name == "CTRL1"

    alone = resolver.group_for("SIGNAL", design="relay #2")
    assert alone is not wired
    assert sorted(alone.designs) == ["relay #2", "relay #3"]


def test_several_connections_fan_out_to_different_copies():
    resolver = farm()
    resolver.connect([("esp32", "GPIO5"), ("relay #1", "SIGNAL")], "CTRL1")
    resolver.connect([("esp32", "GPIO6"), ("relay #2", "SIGNAL")], "CTRL2")
    resolver.finalize()

    assert sorted(resolver.group_for("GPIO5").designs) == ["esp32", "relay #1"]
    assert sorted(resolver.group_for("GPIO6").designs) == ["esp32", "relay #2"]
    assert resolver.group_for("SIGNAL", design="relay #3").designs == ["relay #3"]


def test_a_connection_is_a_join():
    resolver = farm()
    resolver.connect([("esp32", "GPIO5"), ("relay #1", "SIGNAL")], "CTRL")
    resolver.finalize()
    assert resolver.group_for("GPIO5").action is Action.JOIN
    assert resolver.group_for("GPIO5").decided_by == "link"


def test_one_member_is_not_a_connection():
    resolver = farm()
    assert resolver.connect([("esp32", "GPIO5")]) == ""


def test_nets_by_design_lists_what_can_be_connected():
    resolver = farm()
    available = resolver.nets_by_design()
    assert available["esp32"] == ["GPIO5", "GPIO6", "GND"]
    assert available["relay #2"] == ["SIGNAL", "GND"]


# -- parsing ---------------------------------------------------------------

DESIGNS = ["esp32", "relay #1", "relay #2"]


def test_a_connection_can_name_designs_by_number():
    assert parse_connection("1:GPIO5 = 2:SIGNAL", DESIGNS) == [
        ("esp32", "GPIO5"), ("relay #1", "SIGNAL")]


def test_a_connection_can_name_designs_by_name():
    assert parse_connection("esp32:GPIO5=relay #1:SIGNAL", DESIGNS) == [
        ("esp32", "GPIO5"), ("relay #1", "SIGNAL")]


def test_a_partial_design_name_is_enough_when_unambiguous():
    assert parse_connection("esp:GPIO5 = 2:SIGNAL", DESIGNS)[0][0] == "esp32"


def test_an_ambiguous_design_name_is_refused():
    assert parse_connection("relay:SIGNAL = 1:GPIO5", DESIGNS) is None


@pytest.mark.parametrize("text", ["nonsense", "1:GPIO5", "1:GPIO5 = ", "9:X = 1:Y",
                                  "1:A:B = 2:SIGNAL"])
def test_malformed_connections_are_refused(text):
    assert parse_connection(text, DESIGNS) is None


# -- plan ------------------------------------------------------------------

def test_a_connection_survives_a_plan_round_trip(designs):
    specs = collect_specs([str(designs)])
    loaded = load_designs(expand(specs))
    resolver = build_resolver(loaded)
    resolver.finalize()
    names = [d.name for d in loaded]
    resolver.connect([(names[0], "SDA"), (names[1], "VCC")], "TIED")
    resolver.finalize()

    plan = plan_from_resolver(resolver, specs, output="merged", title="merged")
    assert plan.connections
    again = MergePlan.from_json(plan.to_json())

    fresh = build_resolver(load_designs(expand(specs)))
    fresh.finalize()
    apply_plan(fresh, again)
    group = fresh.group_for("SDA", design=names[0])
    assert group is fresh.group_for("VCC", design=names[1])
    assert group.merged_name == "TIED"


# -- command line ----------------------------------------------------------

def test_cli_connects_named_designs(designs, tmp_path):
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
                 "-o", "combo", "--yes", "--connect", "1:SDA=2:VCC"])
    assert code == 0
    sch = EagleDoc.load(tmp_path / "out" / "combo.sch")
    names = {n.get("name") for n in sch.nets()}
    assert "SDA" in names


def test_cli_rejects_a_connection_to_a_net_that_does_not_exist(designs, tmp_path, capsys):
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
                 "-o", "combo", "--yes", "--connect", "1:NOSUCHNET=2:VCC"])
    assert code == 2
    assert "no such net" in capsys.readouterr().err


def test_cli_rejects_a_connection_within_one_design(designs, tmp_path, capsys):
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
                 "-o", "combo", "--yes", "--connect", "1:SDA=1:VCC"])
    assert code == 2
    assert "one design" in capsys.readouterr().err


def test_cli_rejects_a_malformed_connection(designs, tmp_path, capsys):
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
                 "-o", "combo", "--yes", "--connect", "rubbish"])
    assert code == 2
    assert "design:net=design:net" in capsys.readouterr().err
