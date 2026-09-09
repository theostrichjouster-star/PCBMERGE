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
