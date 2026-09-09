"""The plan file, prefix naming, and the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbmerge.cli import collect_specs, main
from pcbmerge.eagle import EagleDoc, sanitize_name, unique_name
from pcbmerge.merge import build_resolver, load_designs
from pcbmerge.nets import Action
from pcbmerge.plan import MergePlan, apply_plan, default_prefix, plan_from_resolver


def test_inputs_pair_schematics_with_their_boards(designs):
    specs = collect_specs([str(designs)])
    assert len(specs) == 2
    for spec in specs:
        assert spec.sch.endswith(".sch")
        assert spec.brd and spec.brd.endswith(".brd")


def test_a_missing_schematic_is_reported(tmp_path):
    from pcbmerge.eagle import EagleError

    with pytest.raises(EagleError):
        collect_specs([str(tmp_path / "nope.sch")])


def test_prefixes_are_short_and_unique():
    taken: set[str] = set()
    for name in ("Adafruit_ESP32-S3_8MB", "Adafruit_INA3221_Breakout", "Adafruit_MAX31850"):
        prefix = default_prefix(name, taken)
        assert prefix.endswith("_")
        assert prefix not in taken
        assert "Adafruit" not in prefix, "the shared vendor word is dropped"
        taken.add(prefix)
    assert len(taken) == 3


def test_identical_names_still_get_distinct_prefixes():
    taken: set[str] = set()
    first = default_prefix("Widget", taken)
    taken.add(first)
    second = default_prefix("Widget", taken)
    assert first != second


def test_names_are_coerced_into_something_eagle_accepts():
    assert sanitize_name("Adafruit ESP32-S3 (8MB)") == "Adafruit_ESP32-S3_8MB"
    assert sanitize_name("!!!") == "X"


def test_unique_name_walks_the_dollar_sequence():
    taken = {"R", "R$2"}
    assert unique_name("R", taken) == "R$3"
    assert unique_name("C", taken) == "C"


def test_plan_round_trips_through_json(designs, tmp_path):
    specs = collect_specs([str(designs)])
    loaded = load_designs(specs)
    resolver = build_resolver(loaded)
    resolver.finalize()
    plan = plan_from_resolver(resolver, specs, output="merged", title="merged")

    path = plan.save(tmp_path / "plan.json")
    again = MergePlan.load(path)
    assert [d.name for d in again.designs] == [d.name for d in plan.designs]
    assert {n.key for n in again.nets} == {n.key for n in plan.nets}


def test_a_plan_records_why_each_decision_was_made(designs, tmp_path):
    specs = collect_specs([str(designs)])
    resolver = build_resolver(load_designs(specs))
    resolver.finalize()
    plan = plan_from_resolver(resolver, specs, output="merged", title="merged")

    ground = plan.decision_for("GND")
    assert ground.action == "join"
    assert "ground" in ground.note

    sda = plan.decision_for("SDA")
    assert sda.action == "split"
    assert sda.note


def test_editing_a_plan_changes_the_merge(designs, tmp_path):
    specs = collect_specs([str(designs)])
    resolver = build_resolver(load_designs(specs))
    resolver.finalize()
    plan = plan_from_resolver(resolver, specs, output="merged", title="merged")

    decision = plan.decision_for("SDA")
    decision.action = "join"
    decision.name = "I2C_SDA"

    fresh = build_resolver(load_designs(specs))
    fresh.finalize()
    missing = apply_plan(fresh, plan)
    assert missing == []

    group = fresh.group_for("SDA")
    assert group.action is Action.JOIN
    assert group.merged_name == "I2C_SDA"


def test_a_plan_naming_an_unknown_net_is_reported_not_fatal(designs):
    specs = collect_specs([str(designs)])
    resolver = build_resolver(load_designs(specs))
    resolver.finalize()
    plan = MergePlan(designs=specs)
    from pcbmerge.plan import NetDecision

    plan.nets.append(NetDecision(key="NOPE", name="NOPE", action="join", kind="signal"))
    assert apply_plan(resolver, plan) == ["NOPE"]


# -- command line ----------------------------------------------------------

def test_cli_inspect_lists_conflicts(designs, capsys):
    assert main(["--no-color", "inspect", str(designs)]) == 0
    out = capsys.readouterr().out
    assert "Nets joined automatically" in out
    assert "GND" in out
    assert "Needs a decision" in out


def test_cli_merge_writes_both_files(designs, tmp_path, capsys):
    code = main(["--no-color", "merge", str(designs),
                 "--out-dir", str(tmp_path / "out"), "-o", "combo", "--yes"])
    assert code == 0
    assert (tmp_path / "out" / "combo.sch").exists()
    assert (tmp_path / "out" / "combo.brd").exists()


def test_cli_refuses_to_guess_when_it_cannot_ask(designs, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("pcbmerge.prompt.is_interactive", lambda: False)
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out")])
    assert code == 3
    assert "need a decision" in capsys.readouterr().out


def test_cli_plan_then_merge(designs, tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    assert main(["--no-color", "plan", str(designs), "-o", str(plan_path)]) == 0

    data = json.loads(plan_path.read_text(encoding="utf-8"))
    for net in data["nets"]:
        if net["key"] == "SDA":
            net["action"] = "join"
    plan_path.write_text(json.dumps(data), encoding="utf-8")

    code = main(["--no-color", "merge", "--plan", str(plan_path),
                 "--out-dir", str(tmp_path / "out"), "-o", "combo"])
    assert code == 0

    sch = EagleDoc.load(tmp_path / "out" / "combo.sch")
    assert "SDA" in {n.get("name") for n in sch.nets()}


def test_cli_check_passes_on_merged_output(designs, tmp_path, capsys):
    main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
          "-o", "combo", "--yes"])
    capsys.readouterr()
    assert main(["--no-color", "check", str(tmp_path / "out" / "combo")]) == 0
    assert "consistent" in capsys.readouterr().out


def test_cli_check_catches_a_broken_pair(designs, tmp_path):
    main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
          "-o", "combo", "--yes"])
    path = tmp_path / "out" / "combo.sch"
    prefix = collect_specs([str(designs)])[0].prefix
    text = path.read_text(encoding="utf-8").replace(
        f'<part name="{prefix}R1"', '<part name="GONE"', 1)
    path.write_text(text, encoding="utf-8")
    assert main(["--no-color", "check", str(path)]) == 1


def test_prefix_can_be_chosen_by_hand(designs, tmp_path):
    code = main(["--no-color", "merge", str(designs), "--out-dir", str(tmp_path / "out"),
                 "-o", "combo", "--yes", "--prefix", "LEFT", "--prefix", "RIGHT"])
    assert code == 0
    sch = EagleDoc.load(tmp_path / "out" / "combo.sch")
    names = {p.get("name") for p in sch.parts()}
    assert "LEFT_R1" in names
    assert "RIGHT_R1" in names
