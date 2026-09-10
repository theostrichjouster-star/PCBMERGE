"""A local web front end for the merge engine.

The command line makes you decide before you can see anything.  This serves the
same engine over HTTP so the decisions can be made against a picture: where the
boards land inside the outline, how far the airwires stretch, what each net
choice costs.  Nothing is written until you ask for it.

The server binds to the loopback address only.  It reads and writes files on
this machine as the person running it, which is fine for a tool you start
yourself and would not be fine exposed to a network.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
import threading
import traceback
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import kicad, linking, pruning, sources
from .eagle import EagleDoc, EagleError, design_stem
from .merge import Merger, build_resolver, load_designs, merge
from .nets import Action, Kind
from .plan import (
    DesignSpec, MergePlan, Spot, apply_plan, default_prefix, design_name, expand,
)

HOST = "127.0.0.1"
STATIC = Path(__file__).parent / "static"

# A folder to open on load, so `pcbmerge web somewhere` lands ready to use.
START: str = ""

# Parsed documents survive between requests: re-reading several megabytes of
# XML after every click would make the interface feel broken.
_documents: dict[str, EagleDoc] = {}

# A browser will not tell a page where a chosen folder really lives, so the
# folder picker is the operating system's own, opened by the server.  It runs
# in a separate process: a modal dialog on a request thread would hold the
# server for as long as someone left it open, and Tk dislikes worker threads.
PICKER = textwrap.dedent("""
    import sys
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    chosen = filedialog.askdirectory(title="Choose a folder of EAGLE designs")
    root.destroy()
    sys.stdout.write(chosen or "")
""")
PICKER_TIMEOUT = 600


def can_browse() -> bool:
    """Whether this machine can show a folder dialog at all."""
    return importlib.util.find_spec("tkinter") is not None


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "pcbmerge"

    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- plumbing -----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - required name
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            page = (STATIC / "app.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
        elif route == "/api/health":
            from . import __version__

            self._json({"ok": True, "version": __version__, "start": START,
                        "canBrowse": can_browse(),
                        "vendors": [{"org": v.org, "label": v.label, "note": v.note}
                                    for v in sources.VENDORS],
                        "hasToken": bool(sources.token())})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - required name
        route = urlparse(self.path).path
        actions = {
            "/api/browse": browse,
            "/api/scan": scan,
            "/api/search": search,
            "/api/repo": repository,
            "/api/import": import_designs,
            "/api/analyze": analyze,
            "/api/merge": run_merge,
        }
        action = actions.get(route)
        if action is None:
            self._json({"error": "not found"}, 404)
            return
        try:
            self._json(action(self._body()))
        except (EagleError, ValueError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - unexpected, still reported
            self._json({"error": f"{exc}", "trace": traceback.format_exc()}, 500)


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def scan(body: dict) -> dict:
    """Find the designs in a folder, or beside the files named."""
    raw = (body.get("path") or "").strip().strip('"')
    if not raw:
        raise ValueError("give a folder or a .sch file to start from")
    target = Path(raw).expanduser()
    if not target.exists():
        raise ValueError(f"{target} does not exist")

    folder = target if target.is_dir() else target.parent
    stems = sorted({design_stem(p) for p in folder.glob("*.sch")}
                   | set(kicad.find_stems(folder)))
    if not stems:
        raise ValueError(f"no EAGLE or KiCad designs in {folder}")

    designs: list[dict] = []
    taken: set[str] = set()
    for stem in stems:
        name = design_name(stem)
        prefix = default_prefix(name, taken)
        taken.add(prefix)
        from .cli import design_files

        drawing, board = design_files(stem)
        designs.append({
            "name": name,
            "prefix": prefix,
            "sch": str(drawing),
            "brd": str(board) if board else None,
            "count": 1,
            "use": True,
        })
    return {"folder": str(folder), "designs": designs}


def browse(body: dict) -> dict:
    """Ask the operating system for a folder, then scan whatever comes back."""
    if not can_browse():
        raise ValueError(
            "no folder dialog on this machine; type or paste the path instead")
    try:
        done = subprocess.run(
            [sys.executable, "-c", PICKER],
            capture_output=True, text=True, timeout=PICKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    except OSError as exc:
        raise ValueError(f"could not open a folder dialog: {exc}") from exc

    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        raise ValueError(detail[-1] if detail else "the folder dialog failed")

    chosen = done.stdout.strip()
    if not chosen:
        return {"cancelled": True}
    return scan({"path": chosen})


# --------------------------------------------------------------------------
# published designs
# --------------------------------------------------------------------------

def search(body: dict) -> dict:
    """Repositories in the vendor accounts matching what was typed."""
    query = (body.get("query") or "").strip()
    vendors = [v for v in (body.get("vendors") or []) if isinstance(v, str)]
    limit = max(1, min(int(body.get("limit") or 12), 30))

    tool = (body.get("tool") or "").strip()
    found = sources.search(query, orgs=vendors or None, limit=limit, tool=tool)
    return {
        "query": query,
        "repos": [_repo_json(repo) for repo in found.repos],
        "inspected": found.inspected,
        "stopped": found.stopped,
        "hasToken": bool(sources.token()),
    }


def _repo_json(repo) -> dict:
    return {
        "repo": repo.full_name, "name": repo.name, "owner": repo.owner,
        "vendor": repo.vendor, "description": repo.description,
        "stars": repo.stars, "updated": repo.updated,
        "branch": repo.branch, "url": repo.url,
        "described": repo.described,
        "designs": [_design_json(design) for design in repo.designs],
    }


def repository(body: dict) -> dict:
    """The designs inside one repository. One request to GitHub per call."""
    name = (body.get("repo") or "").strip()
    if not name:
        raise ValueError("give a repository as owner/name")
    branch = (body.get("branch") or "").strip()

    found = sources.designs(name, branch)
    return {
        "repo": name,
        "designs": [_design_json(design) for design in found],
        "complete": sum(1 for design in found if design.complete),
    }


def _design_json(design) -> dict:
    return {
        "repo": design.repo, "name": design.name, "folder": design.folder,
        "tool": design.tool, "branch": design.branch, "files": design.files,
        "label": design.label, "summary": design.summary,
        "complete": design.complete, "size": design.size,
        "partial": design.partial,
    }


def import_designs(body: dict) -> dict:
    """Download the chosen designs into the open project folder.

    They land beside the designs already being merged, because that folder is
    what the merge reads; a separate downloads folder would only have to be
    opened again afterwards.  So the folder has to exist and has to have been
    opened: inventing one here would put files somewhere nobody asked for.

    The page sends back the same descriptions it was given, so nothing here
    trusts a path from the repository: only the file extension survives into
    the name written, and the folder is one this server itself scanned.
    """
    chosen = [_remote(item) for item in body.get("designs") or []]
    if not chosen:
        raise ValueError("pick at least one design to import")

    raw = (body.get("dest") or "").strip().strip('"')
    if not raw:
        raise ValueError("open the project folder these should be saved into first")
    dest = Path(raw).expanduser()
    if not dest.is_dir():
        raise ValueError(f"{dest} is not a folder that exists")

    written = sources.fetch_all(chosen, dest)

    result = scan({"path": str(dest)})
    result["imported"] = [file.name for file in written]
    result["designsAdded"] = len(chosen)
    return result


def _remote(item: dict) -> sources.RemoteDesign:
    if not isinstance(item, dict):
        raise ValueError("a design must be an object")
    files = {k: v for k, v in (item.get("files") or {}).items()
             if isinstance(k, str) and isinstance(v, str)}
    if not files:
        raise ValueError(f"{item.get('name') or 'design'} lists no files")
    return sources.RemoteDesign(
        repo=item.get("repo") or "", name=item.get("name") or "design",
        folder=item.get("folder") or "", tool=item.get("tool") or "eagle",
        branch=item.get("branch") or "main", files=files,
    )


def _specs(body: dict) -> list[DesignSpec]:
    chosen = [d for d in body.get("designs", []) if d.get("use", True)]
    if not chosen:
        raise ValueError("pick at least one design")
    return [DesignSpec(
        name=d["name"], prefix=d.get("prefix") or "", sch=d["sch"],
        brd=d.get("brd") or None, count=max(1, int(d.get("count") or 1)),
    ) for d in chosen]


def _plan(body: dict) -> tuple[list[DesignSpec], MergePlan]:
    specs = _specs(body)
    options = body.get("options", {})
    plan = MergePlan(
        output=body.get("output") or "merged",
        designs=specs,
        drops=list(body.get("drops") or []),
        layout=options.get("layout", "pack"),
        optimize=options.get("optimize", "balanced"),
        outline=options.get("outline", MergePlan().outline),
        gap=float(options.get("gap", 5.0)),
        positions=_positions(body),
    )
    return specs, plan


def _positions(body: dict) -> list[Spot]:
    """Hand placements the page is holding, one per design and view."""
    out: list[Spot] = []
    for item in body.get("positions") or []:
        if not isinstance(item, dict):
            continue
        view = item.get("view")
        if view not in ("board", "sheet") or not item.get("design"):
            continue
        try:
            out.append(Spot(design=str(item["design"]), view=view,
                            x=float(item["x"]), y=float(item["y"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _resolve(body: dict):
    """Load the designs and apply every decision the page is holding."""
    specs, plan = _plan(body)
    designs = load_designs(expand(specs), cache=_documents)
    resolver = build_resolver(designs)

    default = Action(body.get("options", {}).get("default", "split"))
    replica = Action(body.get("options", {}).get("replicas", "split"))
    resolver.finalize(default_action=default, replica_action=replica)

    names = [d.name for d in designs]
    available = resolver.nets_by_design()
    for pair in body.get("connections") or []:
        members = [(m.get("design"), m.get("net")) for m in pair.get("members", [])]
        members = [(d, n) for d, n in members if n in available.get(d, [])]
        if len({d for d, _ in members}) > 1:
            resolver.connect(members, pair.get("name") or members[0][1])

    for link in body.get("links") or []:
        keys = [k for k in link.get("keys", []) if k in resolver.groups]
        if len(keys) > 1:
            resolver.link(keys, link.get("name") or "")

    if body.get("connections") or body.get("links"):
        resolver.finalize(default_action=default, replica_action=replica)

    for key, choice in (body.get("decisions") or {}).items():
        group = resolver.groups.get(key)
        if group is None:
            continue
        group.action = Action(choice.get("action", group.action.value))
        group.decided_by = "web"
        if group.action is Action.JOIN:
            group.merged_name = choice.get("name") or group.display

    return specs, plan, designs, resolver, names


def analyze(body: dict) -> dict:
    """Everything the page needs to draw itself, recomputed from scratch."""
    specs, plan, designs, resolver, names = _resolve(body)

    merger = Merger(designs, resolver, plan)
    merger.prepare()
    preview = merger.preview()

    return {
        "instances": [
            {"name": d.name, "source": d.source, "prefix": d.prefix,
             "hasBoard": d.brd is not None,
             "parts": len(d.sch.parts()),
             "dropped": len(d.dropped_parts)}
            for d in designs
        ],
        "parts": [
            {"kind": g.kind, "value": g.value, "count": g.count,
             "designs": len(g.designs), "mechanical": g.mechanical, "note": g.note}
            for g in pruning.catalog(designs)
        ],
        "nets": _nets(resolver),
        "suggestions": [
            {"left": s.left, "right": s.right, "leftName": s.left_name,
             "rightName": s.right_name, "score": round(s.score, 2),
             "reason": s.reason, "name": s.default_name}
            for s in linking.suggest(resolver)
        ],
        "availableNets": resolver.nets_by_design(),
        "board": _board(preview, merger),
        "sheet": _sheet(preview, merger),
        "totals": {
            "parts": sum(len(d.sch.parts()) - len(d.dropped_parts) for d in designs),
            "dropped": merger.report.dropped_parts,
            "instances": len(designs),
            "designs": len(specs),
        },
        "warnings": merger.report.warnings,
        "converted": merger.report.converted,
    }


def _nets(resolver) -> dict:
    def entry(group) -> dict:
        return {
            "key": group.key,
            "name": group.merged_name or group.display,
            "display": group.display,
            "kind": group.kind.value,
            "action": group.action.value,
            "designs": group.designs,
            "sources": group.sources,
            "spellings": group.spellings,
            "decidedBy": group.decided_by,
        }

    groups = resolver.all_groups()
    return {
        "joined": [entry(g) for g in resolver.joined()],
        "questions": [entry(g) for g in resolver.open_questions()],
        "replicas": [entry(g) for g in resolver.replica_questions()],
        "anonymous": sum(1 for g in groups if g.kind is Kind.ANONYMOUS),
        "unique": sum(1 for g in groups if g.kind is Kind.UNIQUE),
    }


def _board(preview: dict, merger: Merger) -> dict:
    outline = preview["outline"]
    stats = merger.report.after
    return {
        "outline": list(outline) if outline else None,
        "placements": [
            {"design": p.design, "x": round(p.x, 3), "y": round(p.y, 3),
             "width": round(p.width, 3), "height": round(p.height, 3)}
            for p in preview["placements"]
        ],
        "airwires": [
            {"net": a["net"],
             "points": [{"x": round(pt["x"], 2), "y": round(pt["y"], 2)}
                        for pt in a["points"]]}
            for a in preview["airwires"][:120]
        ],
        "stats": {
            "airwire": round(stats.airwire, 1) if stats else 0,
            "width": round(stats.width, 1) if stats else 0,
            "height": round(stats.height, 1) if stats else 0,
            "fill": round(stats.utilization, 1) if stats else 0,
        },
    }


def _sheet(preview: dict, merger: Merger) -> dict:
    """The schematic sheet: where each drawing sits, and how big the page is."""
    blocks = preview.get("sheet") or []
    if not blocks:
        return {"blocks": [], "extent": None}
    left = min(b["x"] for b in blocks)
    bottom = min(b["y"] for b in blocks)
    right = max(b["x"] + b["width"] for b in blocks)
    top = max(b["y"] + b["height"] for b in blocks)
    return {
        "blocks": [
            {"design": b["design"], "x": round(b["x"], 2), "y": round(b["y"], 2),
             "width": round(b["width"], 2), "height": round(b["height"], 2)}
            for b in blocks
        ],
        "extent": [round(left, 2), round(bottom, 2),
                   round(right, 2), round(top, 2)],
        "single": len(blocks) < 2,
    }


def run_merge(body: dict) -> dict:
    """Write the files. The only action that touches the disk."""
    specs, plan, designs, resolver, _ = _resolve(body)
    out_dir = Path((body.get("outDir") or "out").strip()).expanduser()
    stem = Path(plan.output).name or "merged"

    report = merge(designs, resolver, plan, out_dir, stem)
    saved = None
    if body.get("savePlan"):
        from .plan import plan_from_resolver

        keep = plan_from_resolver(
            resolver, specs, output=plan.output, title=plan.output,
            drops=plan.drops, layout=plan.layout, optimize=plan.optimize,
            outline=plan.outline, gap=plan.gap,
        )
        keep.positions = plan.positions
        saved = str(keep.save(out_dir / f"{stem}-plan.json"))

    return {
        "sch": str(report.sch_path) if report.sch_path else None,
        "brd": str(report.brd_path) if report.brd_path else None,
        "plan": saved,
        "parts": report.parts,
        "elements": report.elements,
        "sheets": report.sheets,
        "signals": report.signals,
        "dropped": report.dropped_parts,
        "renamedParts": report.renamed_parts,
        "renamedLibraryItems": report.renamed_library_items,
        "joined": [{"name": name, "count": len(designs)}
                   for name, designs in report.joined_nets],
        "warnings": report.warnings,
        "placedByHand": report.placed_by_hand,
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def serve(port: int = 8765, open_browser: bool = True, verbose: bool = False,
          start: str = "") -> None:
    """Run the front end until interrupted."""
    global START
    START = start
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.verbose = verbose
    url = f"http://{HOST}:{port}/"

    print(f"pcbmerge is running at {url}")
    # Whether a token was picked up is worth saying out loud: setting one is
    # easy to get wrong, and the only symptom otherwise is a search that finds
    # less than it should an hour later.
    if sources.token():
        print("GITHUB_TOKEN found; searching files as well as names.")
    else:
        print("No GITHUB_TOKEN; searching names only, a few times an hour.")
    print("Nothing is written until you press Merge.  Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
