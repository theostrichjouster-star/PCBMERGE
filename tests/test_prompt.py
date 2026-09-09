"""The interactive net questions."""

from __future__ import annotations

import builtins

import pytest

from pcbmerge import prompt
from pcbmerge.nets import Action, Kind, NetResolver


@pytest.fixture
def resolver() -> NetResolver:
    r = NetResolver()
    r.add("a", ["GND", "SDA", "SCL", "VCC"])
    r.add("b", ["GND", "SDA", "SCL", "VCC"])
    r.finalize()
    return r


def answers(monkeypatch, replies: list[str]) -> list[str]:
    """Feed scripted answers to input(), recording the prompts shown."""
    seen: list[str] = []
    queue = list(replies)

    def fake_input(text: str = "") -> str:
        seen.append(text)
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(builtins, "input", fake_input)
    return seen


def test_only_contested_names_are_asked_about(resolver, monkeypatch, capsys):
    answers(monkeypatch, ["s", "s", "s"])
    count = prompt.ask_all(resolver)
    assert count == 3, "GND is settled by rule and never asked about"
    assert resolver.group_for("GND").action is Action.JOIN


def test_answering_join_merges_the_net(resolver, monkeypatch, capsys):
    answers(monkeypatch, ["j", "j", "j"])
    prompt.ask_all(resolver)
    for key in ("SDA", "SCL", "VCC"):
        group = resolver.group_for(key)
        assert group.action is Action.JOIN
        assert group.merged_name == key
        assert group.decided_by == "prompt"


def test_answering_split_keeps_the_nets_apart(resolver, monkeypatch, capsys):
    answers(monkeypatch, ["s", "s", "s"])
    prompt.ask_all(resolver)
    assert all(resolver.group_for(k).action is Action.SPLIT for k in ("SDA", "SCL", "VCC"))


def test_rename_joins_under_a_chosen_name(resolver, monkeypatch, capsys):
    # Questions come in a stable order: SCL, SDA, then VCC.
    answers(monkeypatch, ["s", "r", "I2C_SDA", "s"])
    prompt.ask_all(resolver)
    sda = resolver.group_for("SDA")
    assert sda.action is Action.JOIN
    assert sda.merged_name == "I2C_SDA"
    assert resolver.group_for("SCL").action is Action.SPLIT


def test_all_applies_one_answer_to_the_rest(resolver, monkeypatch, capsys):
    answers(monkeypatch, ["a"])
    count = prompt.ask_all(resolver, default=Action.JOIN)
    assert count == 3
    assert all(resolver.group_for(k).action is Action.JOIN for k in ("SDA", "SCL", "VCC"))


def test_pressing_enter_takes_the_default(resolver, monkeypatch, capsys):
    answers(monkeypatch, ["", "", ""])
    prompt.ask_all(resolver, default=Action.JOIN)
    assert all(resolver.group_for(k).action is Action.JOIN for k in ("SDA", "SCL", "VCC"))


def test_an_unrecognised_answer_asks_again(resolver, monkeypatch, capsys):
    seen = answers(monkeypatch, ["wat", "j", "s", "s"])
    prompt.ask_all(resolver)
    assert len(seen) == 4, "the bad answer did not count"
    out = capsys.readouterr().out
    assert "answer j, s, r, a or ?" in out


def test_help_is_available_without_answering(resolver, monkeypatch, capsys):
    answers(monkeypatch, ["?", "s", "s", "s"])
    prompt.ask_all(resolver)
    assert "join" in capsys.readouterr().out


def test_the_question_says_why_it_is_being_asked(resolver, capsys):
    group = resolver.group_for("VCC")
    text = prompt.describe(group)
    assert "VCC" in text
    assert "2 designs" in text
    assert "role" in text


def test_nothing_to_ask_returns_zero():
    r = NetResolver()
    r.add("a", ["GND", "N$1"])
    r.add("b", ["GND", "N$1"])
    r.finalize()
    assert prompt.ask_all(r) == 0


# -- copies ----------------------------------------------------------------

def test_asking_for_copies_updates_the_specs(monkeypatch, capsys):
    from pcbmerge.plan import DesignSpec

    specs = [DesignSpec(name="ctrl", prefix="C_", sch="c.sch"),
             DesignSpec(name="relay", prefix="R_", sch="r.sch")]
    answers(monkeypatch, ["", "4"])
    changed = prompt.ask_counts(specs)
    assert changed == 1
    assert [s.count for s in specs] == [1, 4]


def test_a_bad_copy_count_is_rejected(monkeypatch, capsys):
    from pcbmerge.plan import DesignSpec

    specs = [DesignSpec(name="relay", prefix="R_", sch="r.sch")]
    seen = answers(monkeypatch, ["nope", "0", "3"])
    prompt.ask_counts(specs)
    assert len(seen) == 3
    assert specs[0].count == 3


# -- nets shared between copies --------------------------------------------

def replica_resolver() -> NetResolver:
    r = NetResolver()
    for index in (1, 2, 3):
        r.add(f"relay #{index}", ["GND", "SIGNAL", "COIL", "N$1"], source="relay")
    r.finalize()
    return r


def test_replica_questions_cover_only_the_ambiguous_nets(monkeypatch, capsys):
    resolver = replica_resolver()
    names = {g.display for g in resolver.replica_questions()}
    assert names == {"SIGNAL", "COIL"}, "GND is a rail, N$1 is anonymous"


def test_choosing_a_replica_net_makes_it_common(monkeypatch, capsys):
    resolver = replica_resolver()
    answers(monkeypatch, ["2"])  # questions are listed alphabetically: COIL, SIGNAL
    prompt.ask_replicas(resolver)
    assert resolver.group_for("SIGNAL").action is Action.JOIN
    assert resolver.group_for("COIL").action is Action.SPLIT


def test_choosing_none_leaves_every_copy_separate(monkeypatch, capsys):
    resolver = replica_resolver()
    answers(monkeypatch, [""])
    prompt.ask_replicas(resolver)
    assert all(resolver.group_for(k).action is Action.SPLIT for k in ("SIGNAL", "COIL"))


def test_choosing_all_makes_every_replica_net_common(monkeypatch, capsys):
    resolver = replica_resolver()
    answers(monkeypatch, ["a"])
    prompt.ask_replicas(resolver)
    assert all(resolver.group_for(k).action is Action.JOIN for k in ("SIGNAL", "COIL"))


def test_several_numbers_can_be_given_at_once(monkeypatch, capsys):
    resolver = replica_resolver()
    answers(monkeypatch, ["1, 2"])
    prompt.ask_replicas(resolver)
    assert all(resolver.group_for(k).action is Action.JOIN for k in ("SIGNAL", "COIL"))


# -- differently named nets ------------------------------------------------

def link_resolver() -> NetResolver:
    r = NetResolver()
    r.add("controller", ["SDA", "GND"])
    r.add("sensor", ["I2C_DATA", "GND"])
    r.finalize()
    return r


def test_accepting_a_suggestion_connects_the_nets(monkeypatch, capsys):
    from pcbmerge import linking

    resolver = link_resolver()
    answers(monkeypatch, ["y"])
    made = prompt.ask_links(resolver, linking.suggest(resolver))
    assert made == 1
    resolver.finalize()
    assert resolver.group_for("SDA") is resolver.group_for("I2C_DATA")


def test_declining_a_suggestion_changes_nothing(monkeypatch, capsys):
    from pcbmerge import linking

    resolver = link_resolver()
    answers(monkeypatch, ["n"])
    assert prompt.ask_links(resolver, linking.suggest(resolver)) == 0
    resolver.finalize()
    assert resolver.group_for("SDA") is not resolver.group_for("I2C_DATA")


def test_the_default_answer_to_a_suggestion_is_no(monkeypatch, capsys):
    from pcbmerge import linking

    resolver = link_resolver()
    answers(monkeypatch, [""])
    assert prompt.ask_links(resolver, linking.suggest(resolver)) == 0


def test_a_suggestion_can_be_accepted_under_a_new_name(monkeypatch, capsys):
    from pcbmerge import linking

    resolver = link_resolver()
    answers(monkeypatch, ["r", "BUS_SDA"])
    prompt.ask_links(resolver, linking.suggest(resolver))
    resolver.finalize()
    assert resolver.group_for("SDA").merged_name == "BUS_SDA"


def test_no_suggestions_asks_nothing():
    resolver = NetResolver()
    resolver.add("a", ["GND"])
    resolver.finalize()
    assert prompt.ask_links(resolver, []) == 0
