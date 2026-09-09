"""Connecting nets whose names do not match."""

from __future__ import annotations

import pytest

from pcbmerge import linking
from pcbmerge.linking import parse_link, similarity, suggest, tokens
from pcbmerge.nets import Action, NetResolver


def resolver_with(*designs: tuple[str, list[str]]) -> NetResolver:
    r = NetResolver()
    for name, nets in designs:
        r.add(name, nets)
    r.finalize()
    return r


# -- tokenising ------------------------------------------------------------

def test_names_break_into_signal_words():
    assert tokens("I2C_DATA") == ["I2C", "SDA"]
    assert tokens("i2cData") == ["I2C", "SDA"]


def test_house_styles_fold_to_one_word():
    assert tokens("MOSI") == tokens("SDI") == tokens("SDA")
    assert tokens("NRST") == tokens("RESET")


def test_a_channel_number_is_dropped_from_a_real_word():
    assert tokens("SDA1") == tokens("SDA")


def test_a_pin_label_keeps_its_number():
    """In A1 the digit is the identity, not a channel marker."""
    assert tokens("A1") != tokens("A2")


# -- scoring ---------------------------------------------------------------

@pytest.mark.parametrize("left,right", [
    ("SDA", "I2C_DATA"),
    ("SCL", "I2C_CLK"),
    ("RESET", "NRST"),
    ("MOSI", "SDI"),
    ("SPI_CS", "CS"),
    ("USB_DP", "D+"),
])
def test_real_pairs_are_proposed(left, right):
    score, reason = similarity(left, right)
    assert score >= linking.MIN_SCORE, f"{left} / {right} should be offered"
    assert reason


@pytest.mark.parametrize("left,right", [
    ("A1", "ADDR0"),      # a header pin against a real signal
    ("A0", "WARNING"),
    ("D10", "ADDR0"),
    ("VIN", "VOUT"),      # opposite ends of a regulator
    ("CRITICAL", "WARNING"),
    ("N$1", "N$2"),
    ("3.3V", "5V"),
    ("SDA", "SDA"),       # already the same name, nothing to propose
])
def test_unrelated_pairs_are_not_proposed(left, right):
    score, _ = similarity(left, right)
    assert score < linking.MIN_SCORE, f"{left} / {right} should not be offered"


def test_pin_labels_never_match_each_other():
    assert similarity("A1", "A2")[0] == 0.0
    assert similarity("D3", "D4")[0] == 0.0


# -- suggesting ------------------------------------------------------------

def test_a_cross_design_pair_is_found():
    resolver = resolver_with(("controller", ["SDA", "GND"]),
                             ("sensor", ["I2C_DATA", "GND"]))
    found = suggest(resolver)
    assert len(found) == 1
    assert {found[0].left_name, found[0].right_name} == {"SDA", "I2C_DATA"}


def test_two_nets_inside_one_design_are_not_proposed():
    """Both names already exist on that board and were drawn apart on purpose."""
    resolver = resolver_with(("only", ["SDA", "I2C_DATA"]))
    assert suggest(resolver) == []


def test_rails_are_left_out_because_rules_already_handle_them():
    resolver = resolver_with(("a", ["GND", "3.3V"]), ("b", ["GND", "3.3V"]))
    assert suggest(resolver) == []


def test_a_linked_pair_stops_being_suggested():
    resolver = resolver_with(("controller", ["SDA"]), ("sensor", ["I2C_DATA"]))
    first = suggest(resolver)
    assert first
    resolver.link([first[0].left, first[0].right], "BUS_SDA")
    resolver.finalize()
    assert suggest(resolver) == []


def test_the_shorter_name_is_the_default():
    resolver = resolver_with(("a", ["SDA"]), ("b", ["I2C_DATA"]))
    assert suggest(resolver)[0].default_name == "SDA"


# -- applying --------------------------------------------------------------

def test_linking_merges_two_groups_into_one_net():
    resolver = resolver_with(("controller", ["SDA"]), ("sensor", ["I2C_DATA"]))
    resolver.link(["SDA", "I2C_DATA"], "BUS_SDA")
    resolver.finalize()

    group = resolver.group_for("SDA")
    assert group is resolver.group_for("I2C_DATA")
    assert group.action is Action.JOIN
    assert group.merged_name == "BUS_SDA"
    assert group.source_count == 2
    assert sorted(group.spellings) == ["I2C_DATA", "SDA"]


def test_a_link_without_a_name_keeps_the_commonest_spelling():
    resolver = resolver_with(("a", ["SDA"]), ("b", ["SDA"]), ("c", ["I2C_DATA"]))
    resolver.link(["SDA", "I2C_DATA"])
    resolver.finalize()
    assert resolver.group_for("I2C_DATA").merged_name == "SDA"


def test_three_nets_can_be_linked_into_one():
    resolver = resolver_with(("a", ["SDA"]), ("b", ["I2C_DATA"]), ("c", ["SDI"]))
    resolver.link(["SDA", "I2C_DATA", "SDI"], "BUS")
    resolver.finalize()
    group = resolver.group_for("SDI")
    assert group.source_count == 3
    assert group.merged_name == "BUS"


def test_chained_links_all_land_on_one_group():
    resolver = resolver_with(("a", ["SDA"]), ("b", ["I2C_DATA"]), ("c", ["SDI"]))
    resolver.link(["SDA", "I2C_DATA"])
    resolver.link(["I2C_DATA", "SDI"], "BUS")
    resolver.finalize()
    assert resolver.group_for("SDA") is resolver.group_for("SDI")
    assert resolver.group_for("SDA").source_count == 3


# -- parsing ---------------------------------------------------------------

def test_a_link_argument_parses():
    assert parse_link("SDA=I2C_DATA") == (["SDA", "I2C_DATA"], "")
    assert parse_link("SDA=I2C_DATA:BUS") == (["SDA", "I2C_DATA"], "BUS")


def test_a_link_argument_needs_two_names():
    with pytest.raises(ValueError):
        parse_link("SDA")
