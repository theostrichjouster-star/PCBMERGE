"""Net classification and the join/split policy."""

from __future__ import annotations

import pytest

from pcbmerge.nets import (
    Action, Kind, NetGroup, NetRef, NetResolver, classify, normalize,
    plan_design_names,
)


@pytest.mark.parametrize("name,expected", [
    ("GND", "GND"), ("gnd", "GND"), ("VSS", "GND"), ("0V", "GND"), ("GROUND", "GND"),
    ("3.3V", "3V3"), ("+3V3", "3V3"), ("3V3", "3V3"), ("+3.3V", "3V3"),
    ("5V", "5V0"), ("+5V", "5V0"), ("12V", "12V0"),
    ("SDA", "SDA"), ("N$1", "N$1"),
])
def test_normalize_folds_equivalent_spellings(name, expected):
    assert normalize(name) == expected


def test_ground_and_explicit_rails_join_automatically():
    assert classify("GND", 3, 3) is Kind.GROUND
    assert classify("3.3V", 2, 2) is Kind.RAIL
    assert classify("VBUS", 2, 2) is Kind.RAIL


def test_role_named_rails_are_never_assumed_equal():
    for name in ("VCC", "VDD", "VIN", "AGND", "VREF"):
        assert classify(name, 2, 2) is Kind.AMBIGUOUS


def test_anonymous_nets_never_join_even_in_every_design():
    assert classify("N$1", 8, 8) is Kind.ANONYMOUS


def test_a_name_only_one_design_uses_is_not_a_conflict():
    assert classify("SDA", 1, 1) is Kind.UNIQUE


def test_copies_of_one_design_are_a_replica_question_not_a_clash():
    """Four copies of one board share names by construction, not by accident."""
    assert classify("SIGNAL", 1, 4) is Kind.REPLICA


def test_rails_still_join_across_copies_of_one_design():
    assert classify("GND", 1, 4) is Kind.GROUND
    assert classify("3.3V", 1, 4) is Kind.RAIL


def test_resolver_sorts_names_into_the_three_buckets():
    resolver = NetResolver()
    resolver.add("a", ["GND", "3.3V", "N$1", "SDA", "VCC", "ONLY_A"])
    resolver.add("b", ["GND", "+3V3", "N$1", "SDA", "VCC"])
    resolver.finalize()

    joined = {g.merged_name for g in resolver.joined()}
    assert joined == {"GND", "3.3V"}

    questions = {g.key for g in resolver.open_questions()}
    assert questions == {"SDA", "VCC"}

    assert resolver.group_for("ONLY_A").kind is Kind.UNIQUE


def test_differently_spelled_rails_join_under_the_commonest_spelling():
    resolver = NetResolver()
    resolver.add("a", ["3.3V"])
    resolver.add("b", ["3.3V"])
    resolver.add("c", ["+3V3"])
    resolver.finalize()
    group = resolver.group_for("3V3")
    assert group.action is Action.JOIN
    assert group.merged_name == "3.3V"


def test_two_nets_inside_one_design_are_never_fused():
    """A board carrying both 3.3V and +3V3 has two nodes, not one."""
    resolver = NetResolver()
    resolver.add("a", ["3.3V", "+3V3"])
    resolver.add("b", ["3.3V"])
    resolver.finalize()
    group = resolver.group_for("3V3")

    taken: set[str] = set()
    mapping = plan_design_names(group, "a", "A_", taken)
    assert len(set(mapping.values())) == 2, "the two nets must stay apart"
    assert "3.3V" in mapping.values()
    assert "A_+3V3" in mapping.values()


def test_split_localizes_names_per_design():
    resolver = NetResolver()
    resolver.add("a", ["SDA"])
    resolver.add("b", ["SDA"])
    resolver.finalize(default_action=Action.SPLIT)
    group = resolver.group_for("SDA")

    taken: set[str] = set()
    first = plan_design_names(group, "a", "A_", taken)
    second = plan_design_names(group, "b", "B_", taken)
    assert first["SDA"] == "A_SDA"
    assert second["SDA"] == "B_SDA"


def test_join_keeps_one_name_for_every_design():
    resolver = NetResolver()
    resolver.add("a", ["SDA"])
    resolver.add("b", ["SDA"])
    resolver.finalize(default_action=Action.JOIN)
    group = resolver.group_for("SDA")

    taken: set[str] = set()
    first = plan_design_names(group, "a", "A_", taken)
    second = plan_design_names(group, "b", "B_", taken)
    assert first["SDA"] == second["SDA"] == "SDA"


def test_group_reports_where_a_name_came_from():
    group = NetGroup(key="GND", display="GND", kind=Kind.GROUND, refs=[
        NetRef(design="a", source="a", raw="GND"),
        NetRef(design="b", source="b", raw="gnd"),
    ])
    assert group.design_count == 2
    assert group.source_count == 2
    assert group.spellings == ["GND", "gnd"]


def test_a_group_separates_copies_from_distinct_designs():
    group = NetGroup(key="SIGNAL", display="SIGNAL", refs=[
        NetRef(design="relay #1", source="relay", raw="SIGNAL"),
        NetRef(design="relay #2", source="relay", raw="SIGNAL"),
    ])
    assert group.design_count == 2, "two instances"
    assert group.source_count == 1, "but only one design"
