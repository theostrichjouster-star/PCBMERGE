"""The API behind the visual front end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbmerge import web
from pcbmerge.eagle import EagleDoc


@pytest.fixture(autouse=True)
def clean_cache():
    web._documents.clear()
    yield
    web._documents.clear()


@pytest.fixture
def opened(designs):
    """A scan result, shaped the way the page holds it."""
    return web.scan({"path": str(designs)})


def body(opened, **extra) -> dict:
    payload = {"designs": opened["designs"], "options": {}}
    payload.update(extra)
    return payload


# -- scanning --------------------------------------------------------------

def test_a_folder_gives_up_its_designs(opened):
    assert len(opened["designs"]) == 2
    for design in opened["designs"]:
        assert design["sch"].endswith(".sch")
        assert design["brd"].endswith(".brd")
        assert design["count"] == 1
        assert design["use"] is True


def test_prefixes_are_assigned_at_scan_time(opened):
    prefixes = {d["prefix"] for d in opened["designs"]}
    assert len(prefixes) == 2
    assert all(p.endswith("_") for p in prefixes)


def test_naming_a_file_opens_its_folder(designs):
    one = sorted(designs.glob("*.sch"))[0]
    assert len(web.scan({"path": str(one)})["designs"]) == 2


def test_an_empty_path_is_refused():
    with pytest.raises(ValueError):
        web.scan({"path": "  "})


def test_a_missing_folder_is_refused(tmp_path):
    with pytest.raises(ValueError):
        web.scan({"path": str(tmp_path / "nope")})


def test_a_folder_without_schematics_is_refused(tmp_path):
    with pytest.raises(ValueError):
        web.scan({"path": str(tmp_path)})


# -- analysing -------------------------------------------------------------

def test_analyze_describes_everything_the_page_draws(opened):
    data = web.analyze(body(opened))
    assert {"instances", "parts", "nets", "suggestions",
            "availableNets", "board", "totals"} <= set(data)
    assert data["totals"]["instances"] == 2


def test_analyze_writes_nothing(opened, tmp_path, monkeypatch):
    """Looking has to be free, or nobody will explore their options."""
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    web.analyze(body(opened))
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_board_comes_back_ready_to_draw(opened):
    board = web.analyze(body(opened))["board"]
    assert board["outline"] == [150.0, 100.0]
    assert len(board["placements"]) == 2
    for placement in board["placements"]:
        assert {"design", "x", "y", "width", "height"} <= set(placement)
    assert board["stats"]["airwire"] > 0


def test_airwires_carry_the_points_to_join(opened):
    board = web.analyze(body(opened))["board"]
    assert board["airwires"]
    for wire in board["airwires"]:
        assert len(wire["points"]) > 1, "a wire spanning one board is not a wire"


def test_nets_are_split_into_the_buckets_the_page_shows(opened):
    nets = web.analyze(body(opened))["nets"]
    assert {n["name"] for n in nets["joined"]} == {"GND", "3.3V"}
    assert {n["key"] for n in nets["questions"]} == {"SDA", "VCC"}
    assert nets["anonymous"] >= 1


def test_copies_expand_into_instances(opened):
    payload = body(opened)
    payload["designs"][0]["count"] = 3
    data = web.analyze(payload)
    assert data["totals"]["instances"] == 4
    assert len(data["board"]["placements"]) == 4


def test_unticking_a_design_leaves_it_out(opened):
    payload = body(opened)
    payload["designs"][0]["use"] = False
    data = web.analyze(payload)
    assert data["totals"]["instances"] == 1


def test_every_design_unticked_is_refused(opened):
    payload = body(opened)
    for design in payload["designs"]:
        design["use"] = False
    with pytest.raises(ValueError):
        web.analyze(payload)


# -- decisions travel through ----------------------------------------------

def test_dropping_a_kind_removes_parts(opened):
    plain = web.analyze(body(opened))
    pruned = web.analyze(body(opened, drops=["RES"]))
    assert pruned["totals"]["parts"] < plain["totals"]["parts"]
    assert pruned["totals"]["dropped"] > 0


def test_joining_a_net_shows_up_in_the_airwires(opened):
    plain = web.analyze(body(opened))
    joined = web.analyze(body(opened, decisions={"SDA": {"action": "join"}}))
    assert len(joined["board"]["airwires"]) > len(plain["board"]["airwires"])
    assert "SDA" in {n["name"] for n in joined["nets"]["joined"]}


def test_a_decision_is_marked_as_yours(opened):
    data = web.analyze(body(opened, decisions={"SDA": {"action": "join"}}))
    sda = [n for n in data["nets"]["questions"] if n["key"] == "SDA"][0]
    assert sda["action"] == "join"
    assert sda["decidedBy"] == "web"


def test_a_connection_reaches_only_the_named_designs(opened):
    names = [d["name"] for d in opened["designs"]]
    data = web.analyze(body(opened, connections=[{
        "members": [{"design": names[0], "net": "SDA"},
                    {"design": names[1], "net": "VCC"}],
        "name": "TIED"}]))
    assert "TIED" in {n["name"] for n in data["nets"]["joined"]}


def test_a_connection_to_a_net_that_does_not_exist_is_ignored(opened):
    names = [d["name"] for d in opened["designs"]]
    data = web.analyze(body(opened, connections=[{
        "members": [{"design": names[0], "net": "NOSUCH"},
                    {"design": names[1], "net": "VCC"}]}]))
    assert data["nets"]["joined"]


def test_a_link_joins_two_names(opened):
    data = web.analyze(body(opened, links=[{"keys": ["SDA", "VCC"], "name": "BOTH"}]))
    assert "BOTH" in {n["name"] for n in data["nets"]["joined"]}


def test_options_reach_the_layout(opened):
    data = web.analyze(body(opened, options={"outline": "60x40", "gap": 1}))
    assert data["board"]["outline"] == [60.0, 40.0]


def test_a_nonsense_outline_is_refused(opened):
    with pytest.raises(ValueError):
        web.analyze(body(opened, options={"outline": "wide"}))


# -- merging ---------------------------------------------------------------

def test_merge_writes_the_pair(opened, tmp_path):
    out = tmp_path / "out"
    result = web.run_merge(body(opened, output="combo", outDir=str(out)))
    assert Path(result["sch"]).exists()
    assert Path(result["brd"]).exists()
    assert result["parts"] > 0


def test_merge_can_save_the_decisions(opened, tmp_path):
    out = tmp_path / "out"
    result = web.run_merge(body(
        opened, output="combo", outDir=str(out), savePlan=True,
        decisions={"SDA": {"action": "join"}}))
    saved = json.loads(Path(result["plan"]).read_text(encoding="utf-8"))
    assert [n for n in saved["nets"] if n["key"] == "SDA"][0]["action"] == "join"


def test_what_the_page_previewed_is_what_gets_written(opened, tmp_path):
    """The preview and the merge must not disagree about the arrangement."""
    payload = body(opened, output="combo", outDir=str(tmp_path / "out"))
    preview = web.analyze(payload)["board"]
    web.run_merge(payload)

    brd = EagleDoc.load(tmp_path / "out" / "combo.brd")
    drawn = {(round(float(w.get("x1")), 3), round(float(w.get("y1")), 3))
             for w in brd.section.iterfind("plain/wire") if w.get("layer") == "20"}
    assert (0.0, 0.0) in drawn
    assert preview["outline"] == [150.0, 100.0]


def test_merged_files_are_consistent(opened, tmp_path):
    from pcbmerge.cli import main

    out = tmp_path / "out"
    web.run_merge(body(opened, output="combo", outDir=str(out)))
    assert main(["--no-color", "check", str(out / "combo")]) == 0


# -- caching ---------------------------------------------------------------

def test_documents_are_parsed_once_across_requests(opened):
    web.analyze(body(opened))
    first = len(web._documents)
    assert first > 0
    web.analyze(body(opened))
    assert len(web._documents) == first, "a second look must not re-read the files"


def test_an_edited_file_is_read_again(opened, designs):
    web.analyze(body(opened))
    before = len(web._documents)
    target = sorted(designs.glob("*.sch"))[0]
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    web.analyze(body(opened))
    assert len(web._documents) > before, "the cache is keyed on the file's mtime"


# -- the page itself -------------------------------------------------------

def test_the_page_is_self_contained():
    """No CDN: this has to work on a bench machine with no internet."""
    page = (web.STATIC / "app.html").read_text(encoding="utf-8")
    assert "<title>pcbmerge</title>" in page
    for remote in ("http://", "https://"):
        assert f'src="{remote}' not in page
        assert f'href="{remote}' not in page


# -- the folder picker -----------------------------------------------------

class FakeRun:
    """Stands in for the picker subprocess."""

    def __init__(self, stdout="", returncode=0, stderr="", raises=None):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.raises = raises
        self.called_with = None

    def __call__(self, cmd, **kwargs):
        self.called_with = cmd
        if self.raises:
            raise self.raises
        return self


def test_the_picker_script_is_valid_python():
    """It is run by a separate interpreter, so nothing else would catch a typo."""
    compile(web.PICKER, "<picker>", "exec")


def test_the_picker_runs_in_its_own_process(designs, monkeypatch):
    """A modal dialog on a request thread would hold the server open."""
    fake = FakeRun(stdout=str(designs))
    monkeypatch.setattr(web.subprocess, "run", fake)
    web.browse({})
    assert fake.called_with[0] == web.sys.executable
    assert fake.called_with[1] == "-c"


def test_choosing_a_folder_scans_it(designs, monkeypatch):
    monkeypatch.setattr(web.subprocess, "run", FakeRun(stdout=str(designs) + "\n"))
    result = web.browse({})
    assert len(result["designs"]) == 2
    assert result["folder"] == str(designs)


def test_cancelling_is_not_an_error(monkeypatch):
    monkeypatch.setattr(web.subprocess, "run", FakeRun(stdout=""))
    assert web.browse({}) == {"cancelled": True}


def test_a_dialog_left_open_forever_gives_up_quietly(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(web.subprocess, "run",
                        FakeRun(raises=sp.TimeoutExpired("picker", 1)))
    assert web.browse({}) == {"cancelled": True}


def test_a_failing_dialog_is_reported(monkeypatch):
    monkeypatch.setattr(web.subprocess, "run",
                        FakeRun(returncode=1, stderr="ModuleNotFoundError: no tkinter"))
    with pytest.raises(ValueError, match="tkinter"):
        web.browse({})


def test_a_machine_without_a_dialog_says_so(monkeypatch):
    monkeypatch.setattr(web, "can_browse", lambda: False)
    with pytest.raises(ValueError, match="type or paste"):
        web.browse({})


def test_the_page_offers_the_picker_and_keeps_the_typed_path():
    page = (web.STATIC / "app.html").read_text(encoding="utf-8")
    assert 'id="browse"' in page
    assert 'id="browse-big"' in page, "the empty state needs a way in too"
    assert 'id="path"' in page, "pasting a path stays available"
    assert "/api/browse" in page
