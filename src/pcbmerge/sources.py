"""Find published designs on GitHub and bring them down to a local folder.

Adafruit, SparkFun and Seeed Studio all publish their hardware as EAGLE or
KiCad source in public repositories, which is exactly the input this tool
wants.  Searching them by hand means knowing which repository a board lives in,
finding the two halves of the pair inside it, and downloading each one.  This
does that, and lands the result in a single folder the rest of the tool can
open like any other.

Only the standard library is used, so the whole conversation with GitHub is
`urllib` against the public REST API.  Unauthenticated calls are rate limited
(60 an hour for the general API, ten a minute for search), which is why every
response is cached for the life of the process and why repository contents are
only listed when someone asks for them.  Setting `GITHUB_TOKEN` raises the
limit considerably.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .eagle import DESIGN_SUFFIXES, EagleError

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"

TIMEOUT = 20
CACHE_SECONDS = 600

# A schematic is a few hundred kilobytes.  Anything past this is a packaged
# release or a video someone committed, and is not what was asked for.
MAX_FILE = 40 * 1024 * 1024


class SourceError(EagleError):
    """A search or download did not work.

    Deliberately an `EagleError`: the command line and the web front end both
    already turn one of those into a readable message, and a network failure
    while collecting input is the same kind of problem to whoever asked.
    """


@dataclass(frozen=True)
class Source:
    """A vendor whose hardware repositories can be searched."""

    org: str
    label: str
    note: str


VENDORS: tuple[Source, ...] = (
    Source("adafruit", "Adafruit", "Breakouts and Feather boards, mostly EAGLE"),
    Source("sparkfun", "SparkFun", "Qwiic and RedBoard hardware, mostly EAGLE"),
    Source("Seeed-Studio", "Seeed Studio", "Grove, XIAO and Wio hardware, mostly KiCad"),
)

ORGS = tuple(s.org for s in VENDORS)


def source_for(org: str) -> Source | None:
    for source in VENDORS:
        if source.org.lower() == org.lower():
            return source
    return None


# --------------------------------------------------------------------------
# what a search finds
# --------------------------------------------------------------------------

@dataclass
class Repo:
    """One repository in a vendor's account."""

    owner: str
    name: str
    description: str = ""
    stars: int = 0
    updated: str = ""
    branch: str = "main"
    url: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def vendor(self) -> str:
        source = source_for(self.owner)
        return source.label if source else self.owner


@dataclass
class RemoteDesign:
    """A pair of design files sitting together inside a repository."""

    repo: str
    name: str
    folder: str
    tool: str                                   # "eagle" or "kicad"
    branch: str = "main"
    files: dict[str, str] = field(default_factory=dict)   # suffix -> path
    size: int = 0

    @property
    def complete(self) -> bool:
        """Whether both halves are present.

        EAGLE keeps the drawing and the board apart, KiCad keeps the board
        whole, so completeness means different things to each.
        """
        if self.tool == "kicad":
            return ".kicad_pcb" in self.files
        return ".sch" in self.files and ".brd" in self.files

    @property
    def label(self) -> str:
        return f"{self.folder}/{self.name}" if self.folder else self.name

    @property
    def summary(self) -> str:
        return ", ".join(sorted(self.files))


# --------------------------------------------------------------------------
# talking to GitHub
# --------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}


def token() -> str:
    """A personal access token, if one is in the environment."""
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _request(url: str, accept: str = "application/vnd.github+json") -> bytes:
    """One GET, with the failures GitHub actually produces spelled out."""
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "pcbmerge",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    auth = token()
    if auth:
        request.add_header("Authorization", f"Bearer {auth}")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(_http_message(exc, url)) from exc
    except urllib.error.URLError as exc:
        raise SourceError(
            f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SourceError(f"{urllib.parse.urlsplit(url).netloc} did not answer "
                           f"within {TIMEOUT}s") from exc


def _http_message(exc: urllib.error.HTTPError, url: str) -> str:
    remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
    if exc.code in (403, 429) and remaining == "0":
        reset = _reset_time(exc.headers.get("X-RateLimit-Reset"))
        hint = ("set GITHUB_TOKEN to a personal access token to raise the limit"
                if not token() else "wait for the window to reset")
        return f"GitHub's rate limit is used up{reset}; {hint}"
    if exc.code == 404:
        return f"GitHub has nothing at {urllib.parse.urlsplit(url).path}"
    if exc.code == 401:
        return "GitHub rejected the token in GITHUB_TOKEN"
    return f"GitHub answered {exc.code} {exc.reason}"


def _reset_time(raw: str | None) -> str:
    try:
        when = time.localtime(int(raw or ""))
    except (TypeError, ValueError):
        return ""
    return f" until {time.strftime('%H:%M', when)}"


def _get_json(url: str) -> dict:
    hit = _cache.get(url)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]                                    # type: ignore[return-value]
    try:
        payload = json.loads(_request(url).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SourceError(f"GitHub sent something unreadable: {exc}") from exc
    _cache[url] = (time.monotonic(), payload)
    return payload


def clear_cache() -> None:
    _cache.clear()


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def search(query: str, orgs: list[str] | None = None, limit: int = 12) -> list[Repo]:
    """Repositories matching `query` across the vendor accounts.

    One search per account rather than one search with several `org:`
    qualifiers, so no single vendor can crowd the others out: the results are
    taken a row at a time from each.
    """
    wanted = [o for o in (orgs or ORGS) if o]
    if not wanted:
        raise SourceError("no vendor selected to search")

    query = (query or "").strip()
    per_org = max(3, min(limit, 20))
    found: list[list[Repo]] = []
    problems: list[str] = []

    for org in wanted:
        try:
            found.append(_search_org(query, org, per_org))
        except SourceError as exc:
            problems.append(f"{org}: {exc}")

    if not found and problems:
        raise SourceError(problems[0].split(": ", 1)[-1])

    out: list[Repo] = []
    for row in range(per_org):
        for group in found:
            if row < len(group):
                out.append(group[row])
    return out[:limit]


def _search_org(query: str, org: str, per_page: int) -> list[Repo]:
    terms = f"{query} org:{org}".strip() if query else f"org:{org}"
    # Ask for more than will be shown, because the hardware is often ranked
    # below the software written for it and has to be pulled back up.
    params = {"q": terms, "per_page": str(min(60, per_page * 4))}
    if not query:
        # With nothing to rank by, the best guess at "interesting" is popular.
        params["sort"] = "stars"
    url = f"{API}/search/repositories?{urllib.parse.urlencode(params)}"
    payload = _get_json(url)
    found = [_repo(item) for item in payload.get("items", []) if isinstance(item, dict)]
    found.sort(key=hardware_rank, reverse=True)
    return found[:per_page]


# Searching for a part number finds the driver library long before the board
# it drives, because that is what people star and link to.  These words say
# which of the two a repository is, and reorder the answer accordingly.  It is
# only a reordering: nothing a search returned is hidden.
HARDWARE_WORDS = (
    "pcb", "breakout", "board", "hardware", "shield", "wing", "hat", "bonnet",
    "qwiic", "grove", "eagle", "kicad", "schematic", "feather", "carrier",
)
SOFTWARE_WORDS = (
    "library", "libraries", "driver", "firmware", "arduino", "python",
    "circuitpython", "micropython", "sdk", "examples", "docs", "guide",
    "tutorial", "learn", "node", "javascript",
)

WORD = re.compile(r"[a-z0-9]+")


def hardware_rank(repo: Repo) -> int:
    """How much a repository looks like published hardware rather than code."""
    words = set(WORD.findall(f"{repo.name} {repo.description}".lower()))
    return (sum(word in words for word in HARDWARE_WORDS)
            - sum(word in words for word in SOFTWARE_WORDS))


def _repo(item: dict) -> Repo:
    owner = (item.get("owner") or {}).get("login", "")
    return Repo(
        owner=owner,
        name=item.get("name", ""),
        description=(item.get("description") or "").strip(),
        stars=int(item.get("stargazers_count") or 0),
        updated=(item.get("pushed_at") or item.get("updated_at") or "")[:10],
        branch=item.get("default_branch") or "main",
        url=item.get("html_url") or f"https://github.com/{owner}/{item.get('name', '')}",
    )


def repository(full_name: str) -> Repo:
    """One repository by `owner/name`, for when a search was skipped."""
    owner, name = _split(full_name)
    return _repo(_get_json(f"{API}/repos/{owner}/{name}"))


def _split(full_name: str) -> tuple[str, str]:
    """`owner`, `name`, from either form a person is likely to paste."""
    text = (full_name or "").strip().strip("/")
    if text.startswith("http"):
        parts = urllib.parse.urlsplit(text).path.strip("/").split("/")
        text = "/".join(parts[:2])
    owner, _, name = text.partition("/")
    if not owner or not name or "/" in name:
        raise SourceError(f"{full_name!r} is not a repository; write it as owner/name")
    return owner, name


# --------------------------------------------------------------------------
# listing the designs inside a repository
# --------------------------------------------------------------------------

def designs(full_name: str, branch: str = "") -> list[RemoteDesign]:
    """Every design pair in a repository, whichever tool drew it.

    The whole file list arrives in one request, so this costs a single call no
    matter how large the repository is.
    """
    owner, name = _split(full_name)
    if not branch:
        branch = repository(f"{owner}/{name}").branch

    reference = urllib.parse.quote(branch, safe="")
    payload = _get_json(f"{API}/repos/{owner}/{name}/git/trees/{reference}?recursive=1")

    grouped: dict[tuple[str, str], RemoteDesign] = {}
    for entry in payload.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        suffix = _design_suffix(path)
        if suffix is None:
            continue

        folder, _, filename = path.rpartition("/")
        stem = filename[: -len(suffix)]
        key = (folder, stem.lower())
        design = grouped.get(key)
        if design is None:
            design = RemoteDesign(repo=f"{owner}/{name}", name=stem, folder=folder,
                                  tool="eagle", branch=branch)
            grouped[key] = design
        design.files[suffix] = path
        design.size += int(entry.get("size") or 0)

    out = []
    for design in grouped.values():
        if ".kicad_pcb" in design.files or ".kicad_sch" in design.files:
            design.tool = "kicad"
        # A KiCad drawing on its own carries no netlist, and the converter
        # reads the board; an EAGLE schematic alone still merges.
        if design.tool == "kicad" and ".kicad_pcb" not in design.files:
            continue
        if design.tool == "eagle" and ".sch" not in design.files:
            continue
        out.append(design)

    out.sort(key=lambda d: (not d.complete, d.folder.lower(), d.name.lower()))
    return out


def _design_suffix(path: str) -> str | None:
    """The design extension a repository path ends with, in canonical case."""
    lowered = path.lower()
    for suffix in DESIGN_SUFFIXES:
        if lowered.endswith(suffix) and len(path) > len(suffix):
            return suffix
    return None


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_stem(name: str) -> str:
    """A filename stem this machine will accept, keeping it recognisable."""
    cleaned = UNSAFE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "design"


def fetch(design: RemoteDesign, dest: Path, overwrite: bool = False) -> list[Path]:
    """Download both halves of one design into `dest`.

    Every file of a design is written under one shared stem, because the merge
    finds the two halves by name.  Renaming one without the other would hide
    the board.  Nothing from the repository is used as a path: only the
    extension survives, so a crafted filename cannot write outside `dest`.
    """
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    wanted = {suffix: path for suffix, path in design.files.items()
              if suffix in DESIGN_SUFFIXES}
    if not wanted:
        raise SourceError(f"{design.name} has no design files to download")

    stem = _free_stem(dest, safe_stem(design.name), design, overwrite)
    written: list[Path] = []
    for suffix, path in sorted(wanted.items()):
        target = dest / f"{stem}{suffix}"
        target.write_bytes(_raw(design, path))
        written.append(target)
    return written


def _free_stem(dest: Path, stem: str, design: RemoteDesign, overwrite: bool) -> str:
    """A stem no other design in this folder is already using."""
    if overwrite:
        return stem
    suffixes = list(design.files)
    candidate, counter = stem, 2
    while any((dest / f"{candidate}{suffix}").exists() for suffix in suffixes):
        candidate = f"{stem}_{counter}"
        counter += 1
    return candidate


def _raw(design: RemoteDesign, path: str) -> bytes:
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
    url = f"{RAW}/{design.repo}/{urllib.parse.quote(design.branch, safe='')}/{quoted}"
    data = _request(url, accept="application/vnd.github.raw")
    if len(data) > MAX_FILE:
        raise SourceError(f"{path} is {len(data) // 1_000_000} MB, which is too "
                           f"large to be a design file")
    return data


def fetch_all(chosen: list[RemoteDesign], dest: Path) -> list[Path]:
    """Download several designs into one folder, ready to be opened."""
    if not chosen:
        raise SourceError("no designs selected")
    written: list[Path] = []
    for design in chosen:
        written.extend(fetch(design, dest))
    return written
