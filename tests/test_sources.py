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


@pytest.fixture(autouse=True)
def cold_cache():
    sources.clear_cache()
    yield
    sources.clear_cache()


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


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
# search
# --------------------------------------------------------------------------

def test_search_takes_a_turn_from_each_vendor(monkeypatch):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1"), repo_item("adafruit", "A2")]},
        "org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]},
        "org%3ASeeed-Studio": {"items": [repo_item("Seeed-Studio", "Z1")]},
    })

    found = sources.search("bme280")

    # One from each account before a second from any: no vendor crowds the rest out.
    assert [r.full_name for r in found] == [
        "adafruit/A1", "sparkfun/S1", "Seeed-Studio/Z1", "adafruit/A2"]


def test_search_honours_a_chosen_vendor(monkeypatch):
    seen = fake_github(monkeypatch, {"org%3Asparkfun": {"items": [repo_item("sparkfun", "S1")]}})

    found = sources.search("qwiic", orgs=["sparkfun"])

    assert [r.full_name for r in found] == ["sparkfun/S1"]
    assert len(seen) == 1


def test_search_stops_at_the_limit(monkeypatch):
    fake_github(monkeypatch, {"org%3Aadafruit": {
        "items": [repo_item("adafruit", f"A{n}") for n in range(20)]}})

    assert len(sources.search("", orgs=["adafruit"], limit=4)) == 4


def test_an_empty_query_ranks_by_popularity(monkeypatch):
    seen = fake_github(monkeypatch, {"org%3Aadafruit": {"items": []}})

    sources.search("", orgs=["adafruit"])

    assert "sort=stars" in seen[0]


def test_one_failing_vendor_does_not_sink_the_search(monkeypatch):
    def answer(url: str) -> dict:
        if "sparkfun" in url:
            raise SourceError("GitHub answered 502 Bad Gateway")
        if "adafruit" in url:
            return {"items": [repo_item("adafruit", "A1")]}
        return {"items": []}

    monkeypatch.setattr(sources, "_get_json", answer)

    assert [r.full_name for r in sources.search("x")] == ["adafruit/A1"]


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
    fake_github(monkeypatch, {"org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]}})

    out = web.search({"query": "bme280", "vendors": ["adafruit"]})

    assert out["repos"][0]["repo"] == "adafruit/A1"
    assert out["repos"][0]["vendor"] == "Adafruit"
    assert out["hasToken"] is False


def test_the_repo_endpoint_lists_designs(monkeypatch):
    fake_github(monkeypatch, {"git/trees": tree("a.sch", "a.brd", "b.sch")})

    out = web.repository({"repo": "adafruit/A1", "branch": "master"})

    assert [d["name"] for d in out["designs"]] == ["a", "b"]
    assert out["complete"] == 1


def test_the_repo_endpoint_needs_a_repository():
    with pytest.raises(ValueError, match="owner/name"):
        web.repository({"repo": "   "})


def test_importing_downloads_then_opens_the_folder(monkeypatch, tmp_path, designs):
    # The two halves written are real EAGLE files, because the import scans the
    # folder afterwards and hands the page something it can merge.
    source = sorted(designs.glob("*.sch"))[0]
    payload = {".sch": source.read_bytes(),
               ".brd": source.with_suffix(".brd").read_bytes()}

    def answer(url: str, accept: str = "") -> bytes:
        return payload[".brd"] if url.endswith(".brd") else payload[".sch"]

    monkeypatch.setattr(sources, "_request", answer)
    dest = tmp_path / "downloads"

    out = web.import_designs({
        "dest": str(dest),
        "designs": [{
            "repo": "adafruit/A1", "name": "Imported", "folder": "", "tool": "eagle",
            "branch": "master", "files": {".sch": "x.sch", ".brd": "x.brd"},
        }],
    })

    assert sorted(out["imported"]) == ["Imported.brd", "Imported.sch"]
    assert [d["name"] for d in out["designs"]] == ["Imported"]
    assert out["folder"] == str(dest)


def test_importing_nothing_is_refused():
    with pytest.raises(ValueError, match="at least one"):
        web.import_designs({"designs": []})


def test_an_imported_design_must_name_its_files():
    with pytest.raises(ValueError, match="no files"):
        web.import_designs({"designs": [{"name": "x", "files": {}}]})


def test_health_advertises_what_can_be_searched():
    payload = {"vendors": [{"org": v.org, "label": v.label} for v in sources.VENDORS]}

    assert [v["org"] for v in payload["vendors"]] == list(sources.ORGS)


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------

def test_search_prints_each_repository(monkeypatch, capsys):
    fake_github(monkeypatch, {"org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]}})

    assert cli.main(["search", "bme280", "--vendor", "adafruit"]) == 0

    out = capsys.readouterr().out
    assert "adafruit/A1" in out
    assert "Adafruit" in out
    assert "GITHUB_TOKEN" in out                    # the rate-limit hint


def test_search_can_look_inside_each_result(monkeypatch, capsys):
    fake_github(monkeypatch, {
        "org%3Aadafruit": {"items": [repo_item("adafruit", "A1")]},
        "git/trees": tree("BME280.sch", "BME280.brd"),
    })

    cli.main(["search", "bme280", "--vendor", "adafruit", "--designs"])

    assert "BME280" in capsys.readouterr().out


def test_a_search_with_no_hits_reports_it(monkeypatch, capsys):
    fake_github(monkeypatch, {"org%3Aadafruit": {"items": []}})

    assert cli.main(["search", "nothing", "--vendor", "adafruit"]) == 1


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
