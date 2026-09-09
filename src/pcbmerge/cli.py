"""Command line for pcbmerge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import prompt
from .eagle import EagleDoc, EagleError
from .libraries import LibraryMerger
from .merge import build_resolver, load_designs, merge
from .nets import Action, Kind, NetResolver
from .plan import (
    DesignSpec, MergePlan, apply_plan, default_prefix, design_name,
    plan_from_resolver,
)

BOLD = "\033[1m"
DIM = "\033[2m"
OFF = "\033[0m"


def _color(enabled: bool):
    if enabled:
        return BOLD, DIM, OFF
    return "", "", ""


# --------------------------------------------------------------------------
# input discovery
# --------------------------------------------------------------------------

def collect_specs(inputs: list[str], prefixes: list[str] | None = None) -> list[DesignSpec]:
    """Turn command line paths into design specs, pairing .sch with .brd.

    Accepts a schematic, a board, a shared stem, or a directory.
    """
    stems: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            found = sorted({p.with_suffix("") for p in path.glob("*.sch")})
            if not found:
                raise EagleError(f"{path}: no .sch files in this directory")
            stems.extend(found)
            continue
        stem = path.with_suffix("") if path.suffix in (".sch", ".brd") else path
        if not stem.with_suffix(".sch").exists():
            raise EagleError(f"{stem.with_suffix('.sch')}: not found")
        if stem not in stems:
            stems.append(stem)

    overrides = list(prefixes or [])
    specs: list[DesignSpec] = []
    taken: set[str] = set()
    for index, stem in enumerate(stems):
        name = design_name(stem)
        if index < len(overrides):
            prefix = overrides[index]
            if prefix and not prefix.endswith("_"):
                prefix += "_"
        else:
            prefix = default_prefix(name, taken)
        taken.add(prefix)
        brd = stem.with_suffix(".brd")
        specs.append(
            DesignSpec(
                name=name,
                prefix=prefix,
                sch=str(stem.with_suffix(".sch")),
                brd=str(brd) if brd.exists() else None,
            )
        )
    return specs


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    b, d, o = _color(not args.no_color)
    specs = collect_specs(args.inputs)
    designs = load_designs(specs)

    print(f"{b}Designs{o}")
    for design in designs:
        board = "sch+brd" if design.brd else "sch only"
        parts = len(design.sch.parts())
        nets = len(design.sch.net_names())
        print(f"  {design.name:<44} {board:8}  {parts:>4} parts  {nets:>4} nets  prefix {design.prefix}")

    libs = LibraryMerger()
    for design in designs:
        docs = [design.sch] + ([design.brd] if design.brd else [])
        pooled = [lib for doc in docs for lib in doc.libraries()]
        renames = libs.add_design(design.name, pooled)
        if renames.count:
            print(f"  {d}{design.name}: {renames.count} library item(s) will be renamed{o}")

    print(f"\n{b}Merged libraries{o}")
    for key, value in libs.stats().items():
        print(f"  {key:<14} {value}")

    resolver = build_resolver(designs)
    resolver.finalize()
    _print_nets(resolver, b, d, o, verbose=args.verbose)
    return 0


def _print_nets(resolver: NetResolver, b: str, d: str, o: str, verbose: bool = False) -> None:
    joined = resolver.joined()
    questions = resolver.open_questions()
    anonymous = [g for g in resolver.all_groups() if g.kind is Kind.ANONYMOUS]
    unique = [g for g in resolver.all_groups() if g.kind is Kind.UNIQUE]

    print(f"\n{b}Nets joined automatically{o}")
    if not joined:
        print(f"  {d}none{o}")
    for group in joined:
        spellings = " / ".join(group.spellings)
        print(f"  {group.merged_name or group.display:<16} {group.design_count} designs   {d}{spellings}{o}")

    print(f"\n{b}Needs a decision{o}")
    if not questions:
        print(f"  {d}none{o}")
    for group in questions:
        spellings = " / ".join(group.spellings)
        print(f"  {spellings:<24} {group.design_count} designs   {d}{group.kind.value}{o}")
        if verbose:
            print(f"    {d}{', '.join(group.designs)}{o}")

    print(f"\n{b}Kept separate{o}")
    print(f"  {len(anonymous)} auto-generated name(s), {len(unique)} name(s) used by one design only")


def cmd_plan(args: argparse.Namespace) -> int:
    specs = collect_specs(args.inputs, args.prefix)
    designs = load_designs(specs)
    resolver = build_resolver(designs)
    resolver.finalize(default_action=Action(args.default))

    if args.interactive and prompt.is_interactive():
        prompt.ask_all(resolver, default=Action(args.default))

    plan = plan_from_resolver(
        resolver, specs, output=args.output, title=args.title or args.output,
        layout=args.layout, gap=args.gap, columns=args.columns,
    )
    path = plan.save(args.plan_out)
    open_count = len([g for g in resolver.open_questions() if g.decided_by == "auto"])
    print(f"Wrote {path}")
    print(f"  {len(plan.designs)} designs, {len(plan.nets)} net decisions")
    if open_count:
        print(f"  {open_count} still on the default; edit the plan or run merge to be asked")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    b, d, o = _color(not args.no_color)

    if args.plan:
        plan = MergePlan.load(args.plan)
        specs = plan.designs
        if args.output:
            plan.output = args.output
    else:
        specs = collect_specs(args.inputs, args.prefix)
        plan = MergePlan(
            output=args.output or "merged", title=args.title or args.output or "merged",
            designs=specs, layout=args.layout, gap=args.gap, columns=args.columns,
        )

    if not specs:
        print("nothing to merge: give me some .sch files", file=sys.stderr)
        return 2

    designs = load_designs(specs)
    resolver = build_resolver(designs)
    resolver.finalize(default_action=Action(args.default))

    if args.plan:
        missing = apply_plan(resolver, plan)
        for key in missing:
            print(f"  {d}plan mentions net {key}, which no design uses{o}")

    pending = [g for g in resolver.open_questions() if g.decided_by == "auto"]
    if pending and not args.yes:
        if prompt.is_interactive():
            prompt.ask_all(resolver, default=Action(args.default))
        else:
            print(f"{len(pending)} net(s) need a decision and this is not a terminal.")
            print(f"Re-run with --yes to take the default ({args.default}), or supply --plan.")
            for group in pending:
                print(f"  {' / '.join(group.spellings)}  ({group.design_count} designs)")
            return 3

    out_dir = Path(args.out_dir)
    stem = Path(plan.output).name
    report = merge(designs, resolver, plan, out_dir, stem)

    if args.save_plan:
        snapshot = plan_from_resolver(
            resolver, specs, output=plan.output, title=plan.title,
            layout=plan.layout, gap=plan.gap, columns=plan.columns,
        )
        snapshot.save(args.save_plan)
        print(f"Decisions saved to {args.save_plan}")

    _print_report(report, b, d, o)
    return 0


def _print_report(report, b: str, d: str, o: str) -> None:
    print(f"\n{b}Merged {len(report.designs)} designs{o}")
    print(f"  {report.parts} parts, {report.elements} board elements, "
          f"{report.sheets} sheets, {report.signals} signals")
    if report.renamed_parts:
        print(f"  {report.renamed_parts} reference designators prefixed")
    if report.renamed_library_items:
        print(f"  {report.renamed_library_items} clashing library items renamed")
    if report.dropped_urns:
        print(f"  {report.dropped_urns} managed-library links converted to local copies")

    if report.joined_nets:
        print(f"\n{b}Joined nets{o}")
        for name, designs in report.joined_nets:
            print(f"  {name:<16} {len(designs)} designs")

    if report.placements:
        print(f"\n{b}Board placement{o}")
        for place in report.placements:
            print(f"  {place.design:<44} {place.width:7.2f} x {place.height:7.2f} mm  "
                  f"at row {place.row}, col {place.column}")

    if report.warnings:
        print(f"\n{b}Warnings{o}")
        for warning in report.warnings:
            print(f"  {warning}")

    print()
    if report.sch_path:
        print(f"  {report.sch_path}")
    if report.brd_path:
        print(f"  {report.brd_path}")
    print(f"\n{d}Open the board in EAGLE and run DRC; joined nets show as airwires "
          f"between the sub-boards.{o}")


def cmd_check(args: argparse.Namespace) -> int:
    """Verify a .sch/.brd pair is internally consistent.

    Dangling references are real breakage.  A footprint that exists only on the
    board, or a part that exists only on the schematic, is ordinary in hand-drawn
    designs -- silkscreen labels, frames and mounting holes all look like that --
    so those are reported separately and do not fail the check.
    """
    b, d, o = _color(not args.no_color)
    stem = Path(args.design)
    if stem.suffix in (".sch", ".brd"):
        stem = stem.with_suffix("")
    sch = EagleDoc.load(stem.with_suffix(".sch"))
    brd_path = stem.with_suffix(".brd")

    problems: list[str] = []
    notes: list[str] = []

    part_list = [p.get("name", "") for p in sch.parts()]
    part_names = set(part_list)
    for name in sorted({n for n in part_list if part_list.count(n) > 1}):
        problems.append(f"duplicate part {name}")

    library_items = _library_index(sch)
    for part in sch.parts():
        lib, deviceset = part.get("library", ""), part.get("deviceset", "")
        if deviceset not in library_items.get(lib, {}).get("devicesets", set()):
            problems.append(f"part {part.get('name')} references missing deviceset {lib}/{deviceset}")

    instances = {i.get("part", "") for i in sch.section.iterfind("sheets/sheet/instances/instance")}
    for name in sorted(instances - part_names):
        problems.append(f"instance references missing part {name}")

    for pinref in sch.section.iterfind("sheets/sheet/nets/net//pinref"):
        if pinref.get("part", "") not in part_names:
            problems.append(f"pinref references missing part {pinref.get('part')}")

    net_count = len(sch.net_names())
    element_count = 0
    if brd_path.exists():
        brd = EagleDoc.load(brd_path)
        element_names = {e.get("name", "") for e in brd.elements()}
        element_count = len(element_names)
        board_items = _library_index(brd)
        for element in brd.elements():
            lib, package = element.get("library", ""), element.get("package", "")
            if package not in board_items.get(lib, {}).get("packages", set()):
                problems.append(
                    f"element {element.get('name')} references missing package {lib}/{package}")
        for contact in brd.section.iterfind("signals/signal//contactref"):
            if contact.get("element", "") not in element_names:
                problems.append(
                    f"contactref references missing element {contact.get('element')}")
        board_only = element_names - part_names
        schematic_only = part_names - element_names
        if board_only:
            notes.append(f"{len(board_only)} footprint(s) placed on the board only")
        if schematic_only:
            notes.append(f"{len(schematic_only)} part(s) drawn on the schematic only")
        stray = set(brd.net_names()) - set(sch.net_names())
        for name in sorted(stray):
            problems.append(f"board signal {name} has no matching schematic net")

    seen: set[str] = set()
    unique = [p for p in problems if not (p in seen or seen.add(p))]

    print(f"{b}{stem.name}{o}")
    print(f"  {len(part_names)} parts, {element_count} board elements, {net_count} nets")
    for note in notes:
        print(f"  {d}{note}{o}")
    if not unique:
        print(f"  {b}consistent{o}")
        return 0
    print(f"  {len(unique)} problem(s):")
    for problem in unique[:40]:
        print(f"    {problem}")
    if len(unique) > 40:
        print(f"    ... and {len(unique) - 40} more")
    return 1


def _library_index(doc: EagleDoc) -> dict[str, dict[str, set[str]]]:
    """Names defined by each library in a drawing, for reference checking."""
    index: dict[str, dict[str, set[str]]] = {}
    for lib in doc.libraries():
        index[lib.get("name", "")] = {
            "packages": {p.get("name", "") for p in lib.iterfind("packages/package")},
            "symbols": {s.get("name", "") for s in lib.iterfind("symbols/symbol")},
            "devicesets": {ds.get("name", "") for ds in lib.iterfind("devicesets/deviceset")},
        }
    return index


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcbmerge",
        description="Combine several EAGLE .sch/.brd designs into one, "
                    "resolving net names automatically or by asking.",
    )
    parser.add_argument("--no-color", action="store_true", help="plain output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_inputs(sub):
        sub.add_argument("inputs", nargs="+",
                         help=".sch files, shared stems, or a directory of designs")

    def add_layout(sub):
        sub.add_argument("--layout", choices=("grid", "row", "column"), default="grid",
                         help="how source boards are tiled (default: grid)")
        sub.add_argument("--gap", type=float, default=5.0,
                         help="millimetres between tiled boards (default: 5)")
        sub.add_argument("--columns", type=int, default=0,
                         help="force a column count for the grid layout")

    def add_policy(sub):
        sub.add_argument("--default", choices=("join", "split"), default="split",
                         help="what to do with contested nets when not asking "
                              "(default: split, which keeps designs electrically apart)")

    inspect = subparsers.add_parser("inspect", help="report parts, libraries and net conflicts")
    add_inputs(inspect)
    inspect.add_argument("-v", "--verbose", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    planner = subparsers.add_parser("plan", help="write a merge plan you can edit and replay")
    add_inputs(planner)
    add_layout(planner)
    add_policy(planner)
    planner.add_argument("-o", "--plan-out", default="merge-plan.json")
    planner.add_argument("--output", default="merged", help="stem for the merged files")
    planner.add_argument("--title", default="")
    planner.add_argument("--prefix", action="append",
                         help="reference-designator prefix per input, in order")
    planner.add_argument("-i", "--interactive", action="store_true",
                         help="ask about contested nets while building the plan")
    planner.set_defaults(func=cmd_plan)

    merger = subparsers.add_parser("merge", help="write the merged .sch and .brd")
    merger.add_argument("inputs", nargs="*",
                        help=".sch files, shared stems, or a directory (omit when using --plan)")
    add_layout(merger)
    add_policy(merger)
    merger.add_argument("-o", "--output", default="", help="stem for the merged files")
    merger.add_argument("--out-dir", default="out", help="where to write (default: out)")
    merger.add_argument("--title", default="")
    merger.add_argument("--prefix", action="append",
                        help="reference-designator prefix per input, in order")
    merger.add_argument("--plan", help="read decisions from a plan file")
    merger.add_argument("--save-plan", help="write the decisions actually used")
    merger.add_argument("-y", "--yes", action="store_true",
                        help="never ask; take the --default for contested nets")
    merger.set_defaults(func=cmd_merge)

    checker = subparsers.add_parser("check", help="verify a .sch/.brd pair is consistent")
    checker.add_argument("design", help="a .sch, .brd, or shared stem")
    checker.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EagleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
