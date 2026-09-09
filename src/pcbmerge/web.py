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

import json
import threading
import traceback
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import linking, pruning
from .eagle import EagleDoc, EagleError
from .merge import Merger, build_resolver, load_designs, merge
from .nets import Action, Kind
from .plan import DesignSpec, MergePlan, apply_plan, default_prefix, design_name, expand

HOST = "127.0.0.1"
STATIC = Path(__file__).parent / "static"

# A folder to open on load, so `pcbmerge web somewhere` lands ready to use.
START: str = ""

# Parsed documents survive between requests: re-reading several megabytes of
# XML after every click would make the interface feel broken.
_documents: dict[str, EagleDoc] = {}


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

            self._json({"ok": True, "version": __version__, "start": START})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - required name
        route = urlparse(self.path).path
        actions = {
            "/api/scan": scan,
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
    stems = sorted({p.with_suffix("") for p in folder.glob("*.sch")})
    if not stems:
        raise ValueError(f"no .sch files in {folder}")

    designs: list[dict] = []
    taken: set[str] = set()
    for stem in stems:
        name = design_name(stem)
        prefix = default_prefix(name, taken)
        taken.add(prefix)
        board = stem.with_suffix(".brd")
        designs.append({
            "name": name,
            "prefix": prefix,
            "sch": str(stem.with_suffix(".sch")),
            "brd": str(board) if board.exists() else None,
            "count": 1,
            "use": True,
        })
    return {"folder": str(folder), "designs": designs}


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
        sheet_layout=options.get("sheetLayout", "per-design"),
        sheets_per_page=int(options.get("sheetsPerPage", 1)),
    )
    return specs, plan


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
        "totals": {
            "parts": sum(len(d.sch.parts()) - len(d.dropped_parts) for d in designs),
            "dropped": merger.report.dropped_parts,
            "instances": len(designs),
            "designs": len(specs),
        },
        "warnings": merger.report.warnings,
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


def run_merge(body: dict) -> dict:
    """Write the files. The only action that touches the disk."""
    specs, plan, designs, resolver, _ = _resolve(body)
    out_dir = Path((body.get("outDir") or "out").strip()).expanduser()
    stem = Path(plan.output).name or "merged"

    report = merge(designs, resolver, plan, out_dir, stem)
    saved = None
    if body.get("savePlan"):
        from .plan import plan_from_resolver

        saved = str(plan_from_resolver(
            resolver, specs, output=plan.output, title=plan.output,
            drops=plan.drops, layout=plan.layout, optimize=plan.optimize,
            outline=plan.outline, gap=plan.gap,
            sheet_layout=plan.sheet_layout, sheets_per_page=plan.sheets_per_page,
        ).save(out_dir / f"{stem}-plan.json"))

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
    print("Nothing is written until you press Merge.  Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
