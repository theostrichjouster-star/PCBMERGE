"""Searching the vendor accounts and bringing designs down.

Nothing here touches the network.  Every test stands in for GitHub with canned
payloads, which is the only way to pin behaviour that would otherwise depend on
what Adafruit happened to publish this week.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from pcbmerge import cli, sources, web
from pcbmerge.sources import RemoteDesign, SourceError

# Kept before any fixture replaces it, so the real one can still be tested.
REAL_TOKEN_PATH = sources.token_path


@pytest.fixture(autouse=True)
def cold_cache():
    sources.clear_cache()
    yield
    sources.clear_cache()


@pytest.fixture(autouse=True)
def no_token(monkeypatch, tmp_path):
    """No token from anywhere, whatever the machine running this has saved.

    A token saved on the developer's machine would otherwise change what these
    tests exercise, and only on that machine.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(sources, "token_path", lambda: tmp_path / "token")


# --------------------------------------------------------------------------
# canned answers
# --------------------------------------------------------------------------

def repo_item(owner: str, name: str, stars: int = 5) -> dict:
    return {
        "name": name,
        "owner": {"login": owner},
        "description": f"{name} hardware files",
        "stargazers_count": stars,
        "pushed_at": "2024-03-01T10:00:00Z",
        "default_branch": "master",
        "html_url": f"https://github.com/{owner}/{name}",
    }


def blob(path: str, size: int = 2048) -> dict:
    return {"path": path, "type": "blob", "size": size}


def fake_github(monkeypatch, pages: dict):
    """Answer `_get_json` from a table keyed by a fragment of the URL."""
    seen: list[str] = []

    def answer(url: str) -> dict:
        seen.append(url)
        # A tree URL contains the repository URL, so the fragment that sits
        # deepest in the path is the one that was really asked for.
        hits = [(url.find(fragment), payload)
                for fragment, payload in pages.items() if fragment in url]
        if hits:
            return max(hits, key=lambda hit: hit[0])[1]
        raise SourceError(f"unexpected request to {url}")

    monkeypatch.setattr(sources, "_get_json", answer)
    return seen


# --------------------------------------------------------------------------
# the token
# --------------------------------------------------------------------------

def test_no_token_anywhere_means_no_token():
    assert sources.token() == ""
    assert sources.token_source() == ""
    assert sources.token_hint() == ""


def test_a_saved_token_is_used_by_the_next_run(monkeypatch):
    sources.save_token("github_pat_abcdefghijklmnop")

    assert sources.token() == "github_pat_abcdefghijklmnop"
    assert sources.token_source() == "saved"
    assert sources.token_hint() == "...mnop"


def test_the_environment_wins_over_what_was_saved(monkeypatch):
    """A shell has to be able to override the saved one for a single run."""
    sources.save_token("github_pat_saved_one")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_from_the_shell")

    assert sources.token() == "github_pat_from_the_shell"
    assert sources.token_source() == "environment"


def test_a_token_is_never_saved_inside_a_project(monkeypatch, tmp_path):
    """A token in a working tree is a token waiting to be committed."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    where = REAL_TOKEN_PATH()

    assert where.is_absolute()
    assert "pcbmerge" in where.parts
    assert not str(where).startswith(str(Path.cwd()))


def test_saving_nothing_is_refused():
    with pytest.raises(SourceError, match="no token"):
        sources.save_token("   ")


def test_something_with_a_space_in_it_is_not_a_token():
    """The commonest paste accident is picking up the words around it."""
    with pytest.raises(SourceError, match="space in it"):
        sources.save_token("Bearer github_pat_abcdefghijkl")


def test_removing_says_whether_there_was_one():
    assert sources.clear_token() is False

    sources.save_token("github_pat_abcdefghijklmnop")

    assert sources.clear_token() is True
    assert sources.token() == ""


def test_a_hint_is_too_little_to_be_worth_anything():
    assert sources.token_hint("github_pat_abcdefghijklmnop") == "...mnop"
    assert sources.token_hint("short") == ""


def test_a_token_is_checked_before_it_is_trusted(monkeypatch):
    body = json.dumps(
        {"resources": {"core": {"limit": 5000}, "code_search": {"limit": 10}}})

    class Answer:
        def read(self):
            return body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sent = {}

    def urlopen(request, timeout=0):
        sent["auth"] = request.headers.get("Authorization")
        return Answer()

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)

    checked = sources.check_token("github_pat_abcdefghijklmnop")

    assert checked == {"requests": 5000, "files": True}
    # The token being checked, not whatever happens to be saved.
    assert sent["auth"] == "Bearer github_pat_abcdefghijklmnop"


def test_a_token_github_will_not_take_is_reported(monkeypatch):
    error = http_error(401, {})
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(error))

    with pytest.raises(SourceError, match="rejected that token"):
        sources.check_token("github_pat_abcdefghijklmnop")


# -- the endpoint the page uses --------------------------------------------

def test_the_page_is_told_about_the_token_and_never_told_it():
    sources.save_token("github_pat_abcdefghijklmnop")

    state = web._token_state()

    assert state["hasToken"] is True
    assert state["tokenSource"] == "saved"
    assert state["tokenHint"] == "...mnop"
    assert "abcdefghijklmnop" not in json.dumps(state)


def test_saving_through_the_page_checks_first(monkeypatch):
    monkeypatch.setattr(sources, "check_token",
                        lambda value: {"requests": 5000, "files": True})

    out = web.set_token({"token": "github_pat_abcdefghijklmnop"})

    assert out["requests"] == 5000
    assert sources.token() == "github_pat_abcdefghijklmnop"
    assert "abcdefghijklmnop" not in json.dumps({k: v for k, v in out.items()
                                                 if k != "saved"})


def test_a_token_the_page_sends_is_not_saved_if_it_does_not_work(monkeypatch):
    def refuse(value):
        raise SourceError("GitHub rejected that token")

    monkeypatch.setattr(sources, "check_token", refuse)

    with pytest.raises(SourceError):
        web.set_token({"token": "github_pat_abcdefghijklmnop"})
    assert sources.token() == ""


def test_the_page_is_warned_when_a_shell_will_win(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_from_the_shell")
    monkeypatch.setattr(sources, "check_token",
                        lambda value: {"requests": 5000, "files": True})

    out = web.set_token({"token": "github_pat_abcdefghijklmnop"})

    assert out["shadowed"] is True


def test_the_page_can_remove_a_saved_token():
    sources.save_token("github_pat_abcdefghijklmnop")

    out = web.set_token({"remove": True})

    assert out["removed"] is True
    assert out["hasToken"] is False


def test_saving_an_empty_token_through_the_page_is_refused():
    with pytest.raises(ValueError, match="paste a token"):
        web.set_token({"token": ""})


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def test_search_takes_a_turn_from_each_vendor(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1"), repo_item("adafruit", "A2")]},
        "org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]},
        "org%3ASeeed-Studio": {"items": [repo_item("Seeed-Studio", "Z1")]},
        "repos/Seeed-Studio/OPL_Kicad_Library": repo_item("Seeed-Studio",
                                                          "OPL_Kicad_Library"),
        "git/trees": PAIR,
    })

    found = sources.search("board")

    # One from each account before a second from any: no vendor crowds the rest
    # out.  Seeed's catalogue is always looked in, so it leads that account.
    assert [r.full_name for r in found.repos] == [
        "adafruit/A1", "sparkfun/S1", "Seeed-Studio/OPL_Kicad_Library",
        "adafruit/A2", "Seeed-Studio/Z1"]


def test_search_honours_a_chosen_vendor(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]},
        "git/trees": PAIR,
    })

    found = sources.search("qwiic", orgs=["sparkfun"])

    assert [r.full_name for r in found.repos] == ["sparkfun/S1"]


def test_a_name_that_answers_the_query_is_opened_first(monkeypatch):
    """An account holds the board, its library and its hookup guide.

    All three mention the part, so the board has to be picked out by its own
    name or the budget is spent on the writing about it.
    """
    query = "pro micro esp32c3"
    board = sources.Repo("sparkfun", "SparkFun_Pro_Micro-ESP32C3",
                         "Pro Micro ESP32-C3")
    guide = sources.Repo("sparkfun", "SparkFun_Pro_Micro_Hookup_Guide",
                         "Hookup guide for the Pro Micro ESP32C3")
    library = sources.Repo("sparkfun", "SparkFun_Qwiic_Arduino_Library",
                           "Arduino library for the Pro Micro ESP32C3")

    order = sorted([library, guide, board],
                   key=lambda r: sources.rank(r, query), reverse=True)

    assert [r.name for r in order][0] == "SparkFun_Pro_Micro-ESP32C3"


def test_hardware_still_beats_software_when_neither_is_named(monkeypatch):
    pcb = sources.Repo("adafruit", "Widget-PCB", "PCB files for the widget")
    lib = sources.Repo("adafruit", "Widget_Library", "Arduino library")

    assert sources.rank(pcb, "") > sources.rank(lib, "")


def test_a_design_nested_in_a_hardware_folder_is_found(monkeypatch):
    """Vendors keep the design files in a subfolder, not at the top."""
    fake_github(monkeypatch, {"git/trees": tree(
        "README.md", "Firmware/main.c",
        "Hardware/SparkFun_Dev_ESP32_C3_MINI.sch",
        "Hardware/SparkFun_Dev_ESP32_C3_MINI.brd")})

    found = sources.designs("sparkfun/SparkFun_Pro_Micro-ESP32C3", "main")

    assert [(d.folder, d.name) for d in found] == [
        ("Hardware", "SparkFun_Dev_ESP32_C3_MINI")]
    assert found[0].complete


def test_a_repository_with_no_designs_is_not_a_result(monkeypatch):
    """A name that matches and no hardware behind it is not worth showing."""
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1"),
                                     repo_item("adafruit", "A2")]},
        "repos/adafruit/A1/git/trees": tree("README.md", "src/main.c"),
        "repos/adafruit/A2/git/trees": PAIR,
    })

    found = sources.search("thing", orgs=["adafruit"])

    assert [r.full_name for r in found.repos] == ["adafruit/A2"]
    assert found.inspected == 2


def test_a_result_arrives_with_its_designs_already(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]},
        "git/trees": PAIR,
    })

    found = sources.search("board", orgs=["adafruit"])

    assert [d.name for d in found.repos[0].designs] == ["board"]


def code_hit(owner: str, name: str, path: str) -> dict:
    return {"path": path, "repository": {
        "full_name": f"{owner}/{name}", "name": name,
        "owner": {"login": owner}, "description": "", "html_url": ""}}


def test_without_a_token_no_file_search_is_attempted(monkeypatch):
    """GitHub refuses code search outright when it is not authenticated."""
    seen = fake_github(monkeypatch, {"org%3Asparkfun": {"items": []}})

    assert sources.file_candidates("x", "sparkfun") == []
    assert not any("search/code" in url for url in seen)


def test_a_token_buys_a_search_of_the_files_themselves(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    fake_github(monkeypatch, {"search/code": {"items": [
        code_hit("sparkfun", "Lumenati_90L", "Hardware/Lumenati_90L.kicad_pcb"),
        code_hit("sparkfun", "Lumenati_90L", "Production/panel.kicad_pcb"),
        code_hit("sparkfun", "Roller", "Hardware/Roller.kicad_pcb"),
    ]}})

    found = sources.file_candidates("lumenati", "sparkfun")

    assert [r.full_name for r in found] == ["sparkfun/Lumenati_90L", "sparkfun/Roller"]
    # The branch is not in a code search result, so it is looked up later.
    assert all(r.branch == "" for r in found)
    assert all(r.described is False for r in found)


def test_the_file_search_looks_for_the_tool_asked_for(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    seen = fake_github(monkeypatch, {"search/code": {"items": []}})

    sources.file_candidates("x", "sparkfun", tool="eagle")

    assert "extension%3Abrd" in seen[0]


def test_a_refused_file_search_does_not_sink_the_rest(monkeypatch):
    """Code search is refused for some accounts and rejects some queries."""
    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    def answer(url: str) -> dict:
        if "search/code" in url:
            raise SourceError("GitHub answered 422 Unprocessable Entity")
        if "git/trees" in url:
            return PAIR
        return {"items": [repo_item("sparkfun", "S1")]}

    monkeypatch.setattr(sources, "_get_json", answer)

    assert [r.full_name for r in sources.search("board", orgs=["sparkfun"]).repos]         == ["sparkfun/S1"]


def test_a_repository_found_both_ways_is_opened_once(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    fake_github(monkeypatch, {
        "search/code": {"items": [code_hit("sparkfun", "S1", "Hardware/x.kicad_pcb")]},
        "org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]},
        "git/trees": PAIR,
    })

    found = sources.search("board", orgs=["sparkfun"])

    assert found.inspected == 1


def test_a_catalogue_is_opened_even_when_its_name_says_nothing(monkeypatch):
    """Repository search reads a name and a description, never the files."""
    fake_github(monkeypatch, {
        "org%3ASeeed-Studio": {"items": []},
        "repos/Seeed-Studio/OPL_Kicad_Library": repo_item("Seeed-Studio",
                                                          "OPL_Kicad_Library"),
        "git/trees": tree("XIAO Family/XIAO.kicad_pcb", "XIAO Family/XIAO.kicad_sch",
                          "Other/Widget.kicad_pcb"),
    })

    found = sources.search("xiao", orgs=["Seeed-Studio"])

    assert [r.full_name for r in found.repos] == ["Seeed-Studio/OPL_Kicad_Library"]
    # Only the designs that answer the query, or a catalogue answers everything.
    assert [d.name for d in found.repos[0].designs] == ["XIAO"]


def test_a_catalogue_with_nothing_matching_is_dropped(monkeypatch):
    fake_github(monkeypatch, {
        "org%3ASeeed-Studio": {"items": []},
        "repos/Seeed-Studio/OPL_Kicad_Library": repo_item("Seeed-Studio",
                                                          "OPL_Kicad_Library"),
        "git/trees": tree("Other/Widget.kicad_pcb"),
    })

    assert sources.search("xiao", orgs=["Seeed-Studio"]).repos == []


def test_opening_repositories_can_be_skipped(monkeypatch):
    """The cheap path: one request per vendor, and no filtering."""
    seen = fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]}})

    found = sources.search("x", orgs=["adafruit"], inspect=False)

    assert [r.full_name for r in found.repos] == ["adafruit/A1"]
    assert found.inspected == 0
    assert len(seen) == 1


def test_the_search_looks_past_the_first_few_names(monkeypatch):
    """Only the last of these holds hardware, and it still has to be found.

    Offering the inspection only as many candidates as there are results to
    show starves it: the first names a vendor returns are usually libraries,
    and a search that can open nothing else reports the vendor has nothing.
    """
    items = [repo_item("sparkfun", f"Lib{n}") for n in range(9)]
    items.append(repo_item("sparkfun", "Board"))

    def answer(url: str) -> dict:
        if "git/trees" in url:
            return PAIR if "/Board/" in url else tree("README.md")
        return {"items": items}

    monkeypatch.setattr(sources, "_get_json", answer)

    found = sources.search("thing", orgs=["sparkfun"], limit=3, budget=12)

    assert [r.full_name for r in found.repos] == ["sparkfun/Board"]
    assert found.inspected == 10


def test_a_search_stops_when_it_has_opened_enough(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", f"A{n}") for n in range(9)]},
        "git/trees": tree("README.md"),
    })

    found = sources.search("x", orgs=["adafruit"], budget=3)

    assert found.inspected == 3
    assert "GITHUB_TOKEN" in found.stopped


def test_a_refusal_partway_through_is_reported_not_raised(monkeypatch):
    calls = {"n": 0}

    def answer(url: str) -> dict:
        if "git/trees" in url:
            calls["n"] += 1
            if calls["n"] > 1:
                raise SourceError("GitHub's rate limit is used up")
            return PAIR
        return {"items": [repo_item("adafruit", "A1"), repo_item("adafruit", "A2")]}

    monkeypatch.setattr(sources, "_get_json", answer)

    found = sources.search("x", orgs=["adafruit"])

    assert [r.full_name for r in found.repos] == ["adafruit/A1"]
    assert "rate limit" in found.stopped


def test_search_stops_at_the_limit(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", f"A{n}") for n in range(20)]},
        "git/trees": PAIR,
    })

    assert len(sources.search("", orgs=["adafruit"], limit=4).repos) == 4


def test_an_empty_query_ranks_by_popularity(monkeypatch):
    seen = fake_github(monkeypatch, {"org%3Aadafruit": {"items": []}})

    sources.search("", orgs=["adafruit"])

    assert "sort=stars" in seen[0]


def test_one_failing_vendor_does_not_sink_the_search(monkeypatch):
    def answer(url: str) -> dict:
        if "sparkfun" in url:
            raise SourceError("GitHub answered 502 Bad Gateway")
        if "git/trees" in url:
            return PAIR
        if "adafruit" in url:
            return {"items": [repo_item("adafruit", "A1")]}
        raise SourceError("nothing there")

    monkeypatch.setattr(sources, "_get_json", answer)

    assert [r.full_name for r in sources.search("board").repos] == ["adafruit/A1"]


def test_every_vendor_failing_is_reported(monkeypatch):
    def answer(url: str) -> dict:
        raise SourceError("could not reach api.github.com")

    monkeypatch.setattr(sources, "_get_json", answer)

    with pytest.raises(SourceError, match="could not reach"):
        sources.search("x")


def test_a_repository_carries_its_vendor_name(monkeypatch):
    fake_github(monkeypatch, {"repos/adafruit/A1": repo_item("adafruit", "A1")})

    repo = sources.repository("adafruit/A1")

    assert repo.vendor == "Adafruit"
    assert repo.branch == "master"


@pytest.mark.parametrize("written", [
    "adafruit/A1", "https://github.com/adafruit/A1", "  adafruit/A1/  ",
])
def test_a_repository_is_recognised_however_it_is_written(monkeypatch, written):
    fake_github(monkeypatch, {"repos/adafruit/A1": repo_item("adafruit", "A1")})

    assert sources.repository(written).full_name == "adafruit/A1"


@pytest.mark.parametrize("written", ["adafruit", "", "a/b/c"])
def test_a_name_that_is_not_a_repository_is_refused(written):
    with pytest.raises(SourceError, match="owner/name"):
        sources.repository(written)


# --------------------------------------------------------------------------
# listing a repository
# --------------------------------------------------------------------------

def tree(*paths: str) -> dict:
    return {"tree": [blob(p) for p in paths]}


PAIR = tree("board.sch", "board.brd")


def test_the_two_halves_of_an_eagle_design_are_one_entry(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree(
        "Adafruit BME280.sch", "Adafruit BME280.brd", "README.md")})

    found = sources.designs("adafruit/A1", "master")

    assert len(found) == 1
    assert found[0].name == "Adafruit BME280"
    assert found[0].tool == "eagle"
    assert found[0].complete
    assert set(found[0].files) == {".sch", ".brd"}


def test_a_kicad_project_is_one_entry(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree(
        "hardware/XIAO.kicad_pcb", "hardware/XIAO.kicad_sch", "hardware/XIAO.kicad_pro")})

    found = sources.designs("Seeed-Studio/Z1", "main")

    assert [d.tool for d in found] == ["kicad"]
    assert found[0].label == "hardware/XIAO"
    assert found[0].complete


def test_a_board_ported_to_kicad_is_two_designs(monkeypatch):
    """A vendor porting a board keeps both beside each other under one name.

    Folded together they become one entry that pulls all four files down and
    hides whichever half you wanted.
    """
    fake_github(monkeypatch, {"git/trees": tree(
        "Hardware/Board.sch", "Hardware/Board.brd",
        "Hardware/Board.kicad_pcb", "Hardware/Board.kicad_sch")})

    found = sources.designs("sparkfun/X", "main")

    assert [(d.tool, sorted(d.files)) for d in found] == [
        ("eagle", [".brd", ".sch"]), ("kicad", [".kicad_pcb", ".kicad_sch"])]
    assert all(d.complete for d in found)


def test_a_search_can_ask_for_one_tool(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]},
        "git/trees": tree("Board.sch", "Board.brd", "Board.kicad_pcb"),
    })

    found = sources.search("board", orgs=["sparkfun"], tool="kicad")

    assert [d.tool for d in found.repos[0].designs] == ["kicad"]


def test_asking_for_a_tool_nothing_uses_finds_nothing(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]},
        "git/trees": tree("Board.sch", "Board.brd"),
    })

    assert sources.search("board", orgs=["sparkfun"], tool="kicad").repos == []


def test_an_unknown_tool_is_refused():
    with pytest.raises(SourceError, match="unknown tool"):
        sources.search("x", tool="altium")


def test_a_kicad_drawing_without_a_board_is_not_offered(monkeypatch):
    # The converter reads the board for the netlist, so a sheet alone is no use.
    fake_github(monkeypatch, {"git/trees": tree(
        "top.kicad_pcb", "top.kicad_sch", "01 Descriptions.kicad_sch")})

    assert [d.name for d in sources.designs("Seeed-Studio/Z1", "main")] == ["top"]


def test_a_board_without_a_schematic_is_not_offered(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree("orphan.brd")})

    assert sources.designs("adafruit/A1", "master") == []


def test_designs_in_different_folders_stay_apart(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree(
        "v1/board.sch", "v1/board.brd", "v2/board.sch", "v2/board.brd")})

    found = sources.designs("adafruit/A1", "master")

    assert [d.label for d in found] == ["v1/board", "v2/board"]


def test_complete_pairs_are_listed_first(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree(
        "aaa.sch", "zzz.sch", "zzz.brd")})

    assert [d.name for d in sources.designs("adafruit/A1", "master")] == ["zzz", "aaa"]


def test_a_filename_with_a_version_keeps_all_of_it(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree(
        "XIAO ESP32S3_V1.5.kicad_pcb", "XIAO ESP32S3_V1.5.kicad_sch")})

    assert sources.designs("Seeed-Studio/Z1", "main")[0].name == "XIAO ESP32S3_V1.5"


def test_extensions_are_matched_whatever_their_case(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree("Board.SCH", "Board.BRD")})

    found = sources.designs("adafruit/A1", "master")

    assert found and found[0].complete
    assert found[0].files[".sch"] == "Board.SCH"       # the real path is kept


def test_directories_are_ignored(monkeypatch):
    payload = {"tree": [{"path": "hardware.sch", "type": "tree"}, blob("real.sch")]}
    fake_github(monkeypatch, {"git/trees": payload})

    assert [d.name for d in sources.designs("adafruit/A1", "master")] == ["real"]


def test_the_default_branch_is_looked_up_when_not_given(monkeypatch):
    seen = fake_github(monkeypatch, {
        "repos/adafruit/A1": repo_item("adafruit", "A1"),
        "git/trees": tree("a.sch", "a.brd"),
    })

    sources.designs("adafruit/A1")

    assert any("git/trees/master" in url for url in seen)


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def fake_raw(monkeypatch, body: bytes = b"<eagle/>"):
    """Stand in for raw.githubusercontent.com, recording what was asked for."""
    asked: list[str] = []

    def answer(url: str, accept: str = "") -> bytes:
        asked.append(url)
        return body

    monkeypatch.setattr(sources, "_request", answer)
    return asked


def design(**extra) -> RemoteDesign:
    fields = dict(repo="adafruit/A1", name="BME280", folder="", tool="eagle",
                  branch="master", files={".sch": "BME280.sch", ".brd": "BME280.brd"})
    fields.update(extra)
    return RemoteDesign(**fields)


def test_both_halves_land_under_one_stem(monkeypatch, tmp_path):
    fake_raw(monkeypatch)

    written = sources.fetch(design(), tmp_path)

    assert sorted(p.name for p in written) == ["BME280.brd", "BME280.sch"]
    assert all(p.read_bytes() == b"<eagle/>" for p in written)


def test_a_second_copy_gets_its_own_stem(monkeypatch, tmp_path):
    fake_raw(monkeypatch)

    sources.fetch(design(), tmp_path)
    again = sources.fetch(design(), tmp_path)

    # Both halves move together, or the merge would not find the board.
    assert sorted(p.name for p in again) == ["BME280_2.brd", "BME280_2.sch"]


def test_a_crafted_name_cannot_write_outside_the_folder(monkeypatch, tmp_path):
    fake_raw(monkeypatch)
    dest = tmp_path / "downloads"

    written = sources.fetch(design(name="../../etc/passwd"), dest)

    assert all(p.parent == dest for p in written)
    assert not (tmp_path / "etc").exists()


def test_a_crafted_path_cannot_choose_the_extension(monkeypatch, tmp_path):
    fake_raw(monkeypatch)

    written = sources.fetch(
        design(files={".sch": "ok.sch", ".bat": "evil.bat"}), tmp_path)

    assert [p.suffix for p in written] == [".sch"]


def test_a_design_with_nothing_downloadable_is_refused(tmp_path):
    with pytest.raises(SourceError, match="no design files"):
        sources.fetch(design(files={".bat": "evil.bat"}), tmp_path)


def test_spaces_in_a_path_are_encoded(monkeypatch, tmp_path):
    asked = fake_raw(monkeypatch)

    sources.fetch(design(files={".sch": "hardware/Adafruit BME280.sch"}), tmp_path)

    assert asked == ["https://raw.githubusercontent.com/adafruit/A1/master/"
                     "hardware/Adafruit%20BME280.sch"]


def test_fetching_nothing_is_refused(tmp_path):
    with pytest.raises(SourceError, match="no designs selected"):
        sources.fetch_all([], tmp_path)


def test_several_designs_land_in_one_folder(monkeypatch, tmp_path):
    fake_raw(monkeypatch)

    written = sources.fetch_all(
        [design(), design(name="MAX31850", files={".sch": "MAX31850.sch"})], tmp_path)

    assert len(written) == 3
    assert {p.parent for p in written} == {tmp_path}


# --------------------------------------------------------------------------
# failures GitHub actually produces
# --------------------------------------------------------------------------

def http_error(code: int, headers: dict) -> urllib.error.HTTPError:
    import email.message

    message = email.message.Message()
    for key, value in headers.items():
        message[key] = value
    return urllib.error.HTTPError("https://api.github.com/x", code, "Forbidden",
                                  message, None)


def test_a_spent_rate_limit_says_what_to_do_about_it(monkeypatch):
    error = http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(error))

    with pytest.raises(SourceError, match="GITHUB_TOKEN"):
        sources._request("https://api.github.com/x")


def test_being_offline_names_the_host(monkeypatch):
    error = urllib.error.URLError("getaddrinfo failed")
    monkeypatch.setattr(sources.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(error))

    with pytest.raises(SourceError, match="could not reach api.github.com"):
        sources._request("https://api.github.com/x")


def test_a_token_is_sent_when_one_is_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    sent: dict = {}

    class Response:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=0):
        sent.update(request.headers)
        return Response()

    monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    sources._request("https://api.github.com/x")

    assert sent.get("Authorization") == "Bearer secret"


def test_answers_are_cached_for_the_life_of_the_process(monkeypatch):
    calls = []

    def answer(url: str, accept: str = "") -> bytes:
        calls.append(url)
        return json.dumps({"items": []}).encode()

    monkeypatch.setattr(sources, "_request", answer)
    sources._get_json("https://api.github.com/x")
    sources._get_json("https://api.github.com/x")

    assert len(calls) == 1


# --------------------------------------------------------------------------
# the web endpoints
# --------------------------------------------------------------------------

def test_the_search_endpoint_shapes_results_for_the_page(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]},
        "git/trees": PAIR,
    })

    out = web.search({"query": "board", "vendors": ["adafruit"]})

    assert out["repos"][0]["repo"] == "adafruit/A1"
    assert out["repos"][0]["vendor"] == "Adafruit"
    assert [d["name"] for d in out["repos"][0]["designs"]] == ["board"]
    assert out["hasToken"] is False


def test_the_repo_endpoint_lists_designs(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree("a.sch", "a.brd", "b.sch")})

    out = web.repository({"repo": "adafruit/A1", "branch": "master"})

    assert [d["name"] for d in out["designs"]] == ["a", "b"]
    assert out["complete"] == 1


def test_the_repo_endpoint_needs_a_repository():
    with pytest.raises(ValueError, match="owner/name"):
        web.repository({"repo": "   "})


def test_importing_downloads_then_opens_the_folder(monkeypatch, designs):
    # Downloads land in the project folder being merged, beside the designs
    # already there, because that folder is what the merge reads.
    source = sorted(designs.glob("*.sch"))[0]
    payload = {".sch": source.read_bytes(),
               ".brd": source.with_suffix(".brd").read_bytes()}

    def answer(url: str, accept: str = "") -> bytes:
        return payload[".brd"] if url.endswith(".brd") else payload[".sch"]

    monkeypatch.setattr(sources, "_request", answer)

    out = web.import_designs({
        "dest": str(designs),
        "designs": [{
            "repo": "adafruit/A1", "name": "Imported", "folder": "", "tool": "eagle",
            "branch": "master", "files": {".sch": "x.sch", ".brd": "x.brd"},
        }],
    })

    assert sorted(out["imported"]) == ["Imported.brd", "Imported.sch"]
    assert "Imported" in [d["name"] for d in out["designs"]]
    assert out["folder"] == str(designs)


def test_importing_nothing_is_refused():
    with pytest.raises(ValueError, match="at least one"):
        web.import_designs({"designs": []})


def one_design() -> list[dict]:
    return [{"repo": "adafruit/A1", "name": "Imported", "folder": "",
             "tool": "eagle", "branch": "master", "files": {".sch": "x.sch"}}]


def test_importing_with_nowhere_to_put_it_is_refused():
    """Inventing a folder would put files somewhere nobody asked for."""
    with pytest.raises(ValueError, match="open the project folder"):
        web.import_designs({"designs": one_design(), "dest": "   "})


def test_importing_into_a_folder_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a folder"):
        web.import_designs({"designs": one_design(),
                            "dest": str(tmp_path / "nope")})


def test_an_import_lands_beside_the_designs_already_open(monkeypatch, designs):
    before = {p.name for p in designs.iterdir()}
    source = sorted(designs.glob("*.sch"))[0]
    monkeypatch.setattr(sources, "_request",
                        lambda url, accept="": source.read_bytes())

    web.import_designs({"dest": str(designs), "designs": one_design()})

    assert {p.name for p in designs.iterdir()} - before == {"Imported.sch"}


def test_an_imported_design_must_name_its_files():
    with pytest.raises(ValueError, match="no files"):
        web.import_designs({"designs": [{"name": "x", "files": {}}]})


def test_health_advertises_what_can_be_searched():
    payload = {"vendors": [{"org": v.org, "label": v.label} for v in sources.VENDORS]}

    assert [v["org"] for v in payload["vendors"]] == list(sources.ORGS)


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------

def test_search_prints_each_repository_with_its_designs(monkeypatch, capsys):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]},
        "git/trees": tree("BME280.sch", "BME280.brd"),
    })

    assert cli.main(["search", "bme280", "--vendor", "adafruit"]) == 0

    out = capsys.readouterr().out
    assert "adafruit/A1" in out
    assert "BME280" in out                          # the design, not just the repo
    assert "GITHUB_TOKEN" in out                    # the rate-limit hint


def test_search_can_list_repositories_without_opening_them(monkeypatch, capsys):
    seen = fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]}})

    assert cli.main(["search", "x", "--vendor", "adafruit", "--any"]) == 0

    assert "adafruit/A1" in capsys.readouterr().out
    assert len(seen) == 1


def test_a_search_with_no_hits_reports_it(monkeypatch, capsys):
    fake_github(monkeypatch, {"org%3Aadafruit": {"items": []}})

    assert cli.main(["search", "nothing", "--vendor", "adafruit"]) == 1


def test_a_name_the_console_cannot_print_does_not_stop_the_listing(monkeypatch, capsys):
    """Vendors name designs in their own scripts; consoles are often cp1252."""
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]},
        "git/trees": tree("\u5e73\u677f/board.sch", "\u5e73\u677f/board.brd"),
    })

    assert cli.main(["search", "board", "--vendor", "adafruit"]) == 0
    assert "board" in capsys.readouterr().out


def test_an_unknown_vendor_is_refused(capsys):
    assert cli.main(["search", "x", "--vendor", "digikey"]) == 2
    assert "unknown vendor" in capsys.readouterr().err


@pytest.mark.parametrize("written", ["adafruit", "Adafruit", "seeed studio"])
def test_a_vendor_is_recognised_by_either_name(written):
    assert cli._orgs([written])


def test_fetch_lists_what_is_inside_before_taking_anything(monkeypatch, capsys, tmp_path):
    fake_github(monkeypatch, {
        "repos/adafruit/A1": repo_item("adafruit", "A1"),
        "git/trees": tree("BME280.sch", "BME280.brd"),
    })

    assert cli.main(["fetch", "adafruit/A1", "--dest", str(tmp_path)]) == 0

    assert not list(tmp_path.iterdir())               # nothing written
    assert "BME280" in capsys.readouterr().out


def test_fetch_all_downloads_every_complete_pair(monkeypatch, tmp_path):
    fake_github(monkeypatch, {
        "repos/adafruit/A1": repo_item("adafruit", "A1"),
        "git/trees": tree("BME280.sch", "BME280.brd", "loose.sch"),
    })
    fake_raw(monkeypatch)

    assert cli.main(["fetch", "adafruit/A1", "--all", "--dest", str(tmp_path)]) == 0

    assert sorted(p.name for p in tmp_path.iterdir()) == ["BME280.brd", "BME280.sch"]


def test_fetch_takes_the_design_named(monkeypatch, tmp_path):
    fake_github(monkeypatch, {
        "repos/adafruit/A1": repo_item("adafruit", "A1"),
        "git/trees": tree("BME280.sch", "BME280.brd", "MAX31850.sch", "MAX31850.brd"),
    })
    fake_raw(monkeypatch)

    cli.main(["fetch", "adafruit/A1", "--design", "max*", "--dest", str(tmp_path)])

    assert sorted(p.stem for p in tmp_path.iterdir()) == ["MAX31850", "MAX31850"]


def test_a_design_that_is_not_there_is_refused(monkeypatch, capsys, tmp_path):
    fake_github(monkeypatch, {
        "repos/adafruit/A1": repo_item("adafruit", "A1"),
        "git/trees": tree("BME280.sch", "BME280.brd"),
    })

    assert cli.main(["fetch", "adafruit/A1", "--design", "nope",
                     "--dest", str(tmp_path)]) == 2
    assert "no design matching" in capsys.readouterr().err


def test_fetching_from_an_empty_repository_reports_it(monkeypatch, capsys, tmp_path):
    fake_github(monkeypatch, {
        "repos/adafruit/A1": repo_item("adafruit", "A1"),
        "git/trees": tree("README.md"),
    })

    assert cli.main(["fetch", "adafruit/A1", "--all", "--dest", str(tmp_path)]) == 1
    assert "no EAGLE or KiCad designs" in capsys.readouterr().out
