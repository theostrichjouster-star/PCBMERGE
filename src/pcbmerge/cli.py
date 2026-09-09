"""Command line for pcbmerge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import layout, linking, prompt, pruning
from .eagle import EagleDoc, EagleError
from .libraries import LibraryMerger
from .merge import build_resolver, load_designs, merge
from .nets import Action, Kind, NetResolver
from .plan import (
    DesignSpec, MergePlan, apply_plan, default_prefix, design_name, expand,
    plan_from_resolver,
)

BOLD = "\033[1m"
DIM = "\033[2m"
OFF = "\033[0m"


def _color(enabled: bool):
    return (BOLD, DIM, OFF) if enabled else ("", "", "")


# --------------------------------------------------------------------------
# input discovery
# --------------------------------------------------------------------------

def collect_specs(inputs: list[str], prefixes: list[str] | None = None,
                  counts: list[str] | None = None) -> list[DesignSpec]:
    """Turn command line paths into design specs, pairing .sch with .brd.

    Accepts a schematic, a board, a shared stem, or a directory.  A count may
    be attached to any input as `path*4` or supplied positionally with
    --count.
    """
    stems: list[Path] = []
    wanted: dict[Path, int] = {}

    for raw in inputs:
        text, _, multiplier = raw.rpartition("*")
        if text and multiplier.isdigit():
            raw, count = text, max(1, int(multiplier))
        else:
            count = 1

        path = Path(raw)
        if path.is_dir():
            found = sorted({p.with_suffix("") for p in path.glob("*.sch")})
            if not found:
                raise EagleError(f"{path}: no .sch files in this directory")
            for stem in found:
                if stem not in stems:
                    stems.append(stem)
                wanted[stem] = count
            continue

        stem = path.with_suffix("") if path.suffix in (".sch", ".brd") else path
        if not stem.with_suffix(".sch").exists():
            raise EagleError(f"{stem.with_suffix('.sch')}: not found")
        if stem not in stems:
            stems.append(stem)
        wanted[stem] = count

    overrides = list(prefixes or [])
    count_args = list(counts or [])
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

        count = wanted.get(stem, 1)
        if index < len(count_args):
            try:
                count = max(1, int(count_args[index]))
            except ValueError as exc:
                raise EagleError(f"--count {count_args[index]!r} is not a number") from exc

        brd = stem.with_suffix(".brd")
        specs.append(DesignSpec(
            name=name, prefix=prefix, sch=str(stem.with_suffix(".sch")),
            brd=str(brd) if brd.exists() else None, count=count,
        ))
    return specs


def _resolve(specs: list[DesignSpec], default: Action, replica: Action):
    """Load, group and classify. Shared by every command."""
    designs = load_designs(expand(specs))
    resolver = build_resolver(designs)
    resolver.finalize(default_action=default, replica_action=replica)
    return designs, resolver


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    b, d, o = _color(not args.no_color)
    specs = collect_specs(args.inputs, args.prefix, args.count)
    designs, resolver = _resolve(specs, Action.SPLIT, Action.SPLIT)

    print(f"{b}Designs{o}")
    for spec in specs:
        board = "sch+brd" if spec.brd else "sch only"
        copies = f"x{spec.count}" if spec.count > 1 else "  "
        print(f"  {spec.name:<44} {board:8} {copies}  prefix {spec.prefix}")
    if len(designs) != len(specs):
        print(f"  {d}{len(specs)} designs expand to {len(designs)} board instances{o}")

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

    _print_nets(resolver, b, d, o, verbose=args.verbose)

    suggestions = linking.suggest(resolver)
    print(f"\n{b}Possible connections between differently named nets{o}")
    if not suggestions:
        print(f"  {d}none found{o}")
    for suggestion in suggestions:
        print(f"  {suggestion.left_name:<18} <-> {suggestion.right_name:<18} "
              f"{suggestion.score:.0%}  {d}{suggestion.reason}{o}")
    return 0


def _print_nets(resolver: NetResolver, b: str, d: str, o: str, verbose: bool = False) -> None:
    joined = resolver.joined()
    questions = resolver.open_questions()
    replicas = resolver.replica_questions()
    anonymous = [g for g in resolver.all_groups() if g.kind is Kind.ANONYMOUS]
    unique = [g for g in resolver.all_groups() if g.kind is Kind.UNIQUE]

    print(f"\n{b}Nets joined automatically{o}")
    if not joined:
        print(f"  {d}none{o}")
    for group in joined:
        spellings = " / ".join(group.spellings)
        print(f"  {group.merged_name or group.display:<16} {group.design_count} designs   "
              f"{d}{spellings}{o}")

    print(f"\n{b}Needs a decision{o}")
    if not questions:
        print(f"  {d}none{o}")
    for group in questions:
        spellings = " / ".join(group.spellings)
        print(f"  {spellings:<24} {group.source_count} designs   {d}{group.kind.value}{o}")
        if verbose:
            print(f"    {d}{', '.join(group.sources)}{o}")

    if replicas:
        print(f"\n{b}One per copy unless you say otherwise{o}")
        for group in replicas:
            print(f"  {group.display:<24} {group.design_count} copies   "
                  f"{d}{group.sources[0] if group.sources else ''}{o}")

    print(f"\n{b}Kept separate{o}")
    print(f"  {len(anonymous)} auto-generated name(s), "
          f"{len(unique)} name(s) used by one design only")


def cmd_plan(args: argparse.Namespace) -> int:
    specs = collect_specs(args.inputs, args.prefix, args.count)
    if args.interactive and prompt.is_interactive():
        prompt.ask_counts(specs)

    designs, resolver = _resolve(specs, Action(args.default), Action(args.replicas))
    _apply_cli_links(resolver, args.link, Action(args.default), Action(args.replicas))
    _apply_cli_connections(resolver, designs, args.connect,
                           Action(args.default), Action(args.replicas))
    drops = list(args.drop or [])

    if args.interactive and prompt.is_interactive():
        drops.extend(prompt.ask_drops(pruning.catalog(designs)))
        prompt.ask_connections(resolver, designs)
        resolver.finalize(default_action=Action(args.default),
                          replica_action=Action(args.replicas))
        prompt.ask_links(resolver, linking.suggest(resolver))
        resolver.finalize(default_action=Action(args.default),
                          replica_action=Action(args.replicas))
        prompt.ask_replicas(resolver)
        prompt.ask_all(resolver, default=Action(args.default))

    plan = plan_from_resolver(
        resolver, specs, output=args.output, title=args.title or args.output,
        drops=drops, outline=args.outline, layout=args.layout, optimize=args.optimize, gap=args.gap, columns=args.columns,
        sheet_layout=args.sheet_layout, sheets_per_page=args.sheets_per_page,
    )
    path = plan.save(args.plan_out)
    pending = len([g for g in resolver.open_questions() if g.decided_by == "auto"])
    print(f"Wrote {path}")
    print(f"  {len(plan.designs)} designs, {len(plan.instances)} instances, "
          f"{len(plan.nets)} net decisions, {len(plan.links)} link(s)")
    if pending:
        print(f"  {pending} still on the default; edit the plan or run merge to be asked")
    return 0


def _apply_cli_links(resolver: NetResolver, links: list[str] | None,
                     default: Action, replica: Action) -> None:
    """Apply --link arguments, refusing to act on nets that do not exist.

    Linking a real net to a typo would quietly join the real one across every
    design, which is exactly the kind of silent change this tool must not make.
    """
    if not links:
        return
    for text in links:
        try:
            keys, name = linking.parse_link(text)
        except ValueError as exc:
            raise EagleError(str(exc)) from exc
        unknown = [k for k in keys if k not in resolver.groups]
        if unknown:
            raise EagleError(
                f"--link {text!r}: no design has a net called {', '.join(unknown)}")
        resolver.link(keys, name)
    resolver.finalize(default_action=default, replica_action=replica)


def _apply_cli_connections(resolver: NetResolver, designs, connections,
                           default: Action, replica: Action) -> None:
    """Apply --connect arguments, refusing anything that names no such net."""
    if not connections:
        return
    names = [d.name for d in designs]
    available = resolver.nets_by_design()
    for text in connections:
        members = prompt.parse_connection(text, names)
        if members is None:
            raise EagleError(f"--connect {text!r}: write it as design:net=design:net")
        unknown = [f"{d}:{n}" for d, n in members if n not in available.get(d, [])]
        if unknown:
            raise EagleError(f"--connect {text!r}: no such net {', '.join(unknown)}")
        if len({d for d, _ in members}) < 2:
            raise EagleError(
                f"--connect {text!r}: both nets are on one design, which this "
                f"cannot join")
        resolver.connect(members, members[0][1])
    resolver.finalize(default_action=default, replica_action=replica)


def cmd_parts(args: argparse.Namespace) -> int:
    """List what is in the designs, grouped so it can be dropped by kind."""
    b, d, o = _color(not args.no_color)
    specs = collect_specs(args.inputs, args.prefix, args.count)
    designs, _ = _resolve(specs, Action.SPLIT, Action.SPLIT)
    groups = pruning.catalog(designs)

    total = sum(g.count for g in groups)
    mechanical = [g for g in groups if g.mechanical]
    print(f"{b}{total} parts in {len(designs)} design instances{o}")
    print(f"  {sum(g.count for g in mechanical)} of them are decoration: "
          f"borders, holes, fiducials, silkscreen labels")
    print()

    header = f"{'kind':<30}{'value':<12}{'copies':>7}{'designs':>9}   note"
    print(f"{b} {header}{o}")
    shown = groups if args.all else groups[:30]
    for group in shown:
        mark = "*" if group.mechanical else " "
        print(f"{mark}{group.kind:<30}{group.value:<12}{group.count:>7}"
              f"{len(group.designs):>9}   {d}{group.note}{o}")
    if len(shown) < len(groups):
        print(f"  {d}... and {len(groups) - len(shown)} more; pass --all{o}")
    example = groups[0].kind if groups else "FIDUCIAL"
    print()
    print(f"{d}* nothing is wired to these. "
          f"Leave a kind out with --drop {example}{o}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    b, d, o = _color(not args.no_color)
    interactive = prompt.is_interactive() and not args.yes

    if args.plan:
        plan = MergePlan.load(args.plan)
        specs = plan.designs
        if args.output:
            plan.output = args.output
    else:
        specs = collect_specs(args.inputs, args.prefix, args.count)
        if interactive and args.ask_counts:
            prompt.ask_counts(specs)
        plan = MergePlan(
            output=args.output or "merged", title=args.title or args.output or "merged",
            designs=specs, drops=list(args.drop or []),
            layout=args.layout, optimize=args.optimize, outline=args.outline,
            gap=args.gap, columns=args.columns, sheet_layout=args.sheet_layout,
            sheets_per_page=args.sheets_per_page,
        )

    if not specs:
        print("nothing to merge: give me some .sch files", file=sys.stderr)
        return 2

    default, replica = Action(args.default), Action(args.replicas)
    designs, resolver = _resolve(specs, default, replica)
    _apply_cli_links(resolver, args.link, default, replica)
    _apply_cli_connections(resolver, designs, args.connect, default, replica)

    unmatched = pruning.unmatched(designs, [pruning.parse_drop(x) for x in plan.drops])
    if unmatched:
        raise EagleError(
            f"--drop {unmatched[0]}: nothing in these designs matches")

    if args.plan:
        for key in apply_plan(resolver, plan, default, replica):
            print(f"  {d}plan mentions net {key}, which no design uses{o}")

    if interactive:
        if not args.no_prune:
            plan.drops.extend(prompt.ask_drops(pruning.catalog(designs)))
        prompt.ask_connections(resolver, designs)
        resolver.finalize(default_action=default, replica_action=replica)
        if args.plan:
            apply_plan(resolver, plan, default, replica)
        if not args.no_suggest:
            found = linking.suggest(resolver)
            if found and prompt.ask_links(resolver, found):
                resolver.finalize(default_action=default, replica_action=replica)
                if args.plan:
                    apply_plan(resolver, plan, default, replica)
        prompt.ask_replicas(resolver)
        prompt.ask_all(resolver, default=default)

    pending = [g for g in resolver.open_questions() if g.decided_by == "auto"]
    if pending and not args.yes and not interactive:
        print(f"{len(pending)} net(s) need a decision and this is not a terminal.")
        print(f"Re-run with --yes to take the default ({args.default}), or supply --plan.")
        for group in pending:
            print(f"  {' / '.join(group.spellings)}  ({group.source_count} designs)")
        return 3

    report = merge(designs, resolver, plan, Path(args.out_dir), Path(plan.output).name)

    if args.save_plan:
        plan_from_resolver(
            resolver, specs, output=plan.output, title=plan.title,
            drops=plan.drops, outline=plan.outline, layout=plan.layout, optimize=plan.optimize, gap=plan.gap,
            columns=plan.columns, sheet_layout=plan.sheet_layout,
            sheets_per_page=plan.sheets_per_page,
        ).save(args.save_plan)
        print(f"Decisions saved to {args.save_plan}")

    _print_report(report, b, d, o)
    return 0


def _delta(saved: float, label: str, unit: str) -> str:
    """Say which way a saving went, rather than leaving a signed number."""
    if abs(saved) < 0.5:
        return f"{label} unchanged"
    direction = "shorter" if unit == "mm" else "smaller"
    if saved < 0:
        direction = "longer" if unit == "mm" else "larger"
    return f"{label} {abs(saved):.0f} {unit} {direction}"


def _print_report(report, b: str, d: str, o: str) -> None:
    replicated = {k: v for k, v in report.copies.items() if v > 1}
    print(f"\n{b}Merged {len(report.copies)} designs into {len(report.designs)} instances{o}")
    for name, count in replicated.items():
        print(f"  {name} x{count}")
    print(f"  {report.parts} parts, {report.elements} board elements, "
          f"{report.sheets} sheets, {report.signals} signals")
    if report.renamed_parts:
        print(f"  {report.renamed_parts} reference designators prefixed")
    if report.renamed_library_items:
        print(f"  {report.renamed_library_items} clashing library items renamed")
    if report.dropped_parts:
        print(f"  {report.dropped_parts} part(s) left out")
    if report.dropped_frames:
        print(f"  {report.dropped_frames} page border(s) dropped for shared sheets")
    if report.sheet_extent:
        x1, y1, x2, y2 = report.sheet_extent
        print(f"  sheet is {x2 - x1:.0f} x {y2 - y1:.0f} mm")
    if report.dropped_urns:
        print(f"  {report.dropped_urns} managed-library links converted to local copies")

    if report.linked_nets:
        print(f"\n{b}Connected by hand{o}")
        for name, spellings in report.linked_nets:
            print(f"  {name:<16} {' + '.join(spellings)}")

    if report.joined_nets:
        print(f"\n{b}Joined nets{o}")
        for name, designs in report.joined_nets[:20]:
            print(f"  {name:<16} {len(designs)} instances")
        if len(report.joined_nets) > 20:
            print(f"  {d}... and {len(report.joined_nets) - 20} more{o}")

    before, after = report.before, report.after
    if before and after:
        print(f"\n{b}Board layout{o}")
        if report.outline:
            print(f"  outline    {report.outline[0]:.0f} x {report.outline[1]:.0f} mm")
        print(f"  {'':<12} {'size (mm)':>18} {'fill':>7} {'airwire (mm)':>14}")
        print(f"  {'start':<12} {before.width:8.1f} x {before.height:6.1f} "
              f"{before.utilization:6.0f}% {before.airwire:14.0f}")
        print(f"  {'chosen':<12} {after.width:8.1f} x {after.height:6.1f} "
              f"{after.utilization:6.0f}% {after.airwire:14.0f}")
        if after.iterations:
            air = before.airwire - after.airwire
            area = before.area - after.area
            print(f"  {d}{after.improved} improvement(s) over {after.iterations} tries; "
                  f"{_delta(air, 'airwire', 'mm')}, {_delta(area, 'board', 'mm2')}{o}")

    if report.placements:
        print(f"\n{b}Board placement{o}")
        for place in report.placements:
            print(f"  {place.design:<44} {place.width:7.2f} x {place.height:7.2f} mm  "
                  f"at {place.x:8.2f}, {place.y:8.2f}")

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
                         help=".sch files, shared stems, or a directory. "
                              "Append *N to place several copies, e.g. relay.sch*4")
        sub.add_argument("--count", action="append",
                         help="copies of each input, in order")
        sub.add_argument("--prefix", action="append",
                         help="reference-designator prefix per input, in order")

    def add_layout(sub):
        sub.add_argument("--layout", choices=layout.STYLES, default="pack",
                         help="how source boards are tiled (default: pack)")
        sub.add_argument("--optimize", choices=tuple(layout.WEIGHTS), default="balanced",
                         help="what the placement search minimises (default: balanced)")
        sub.add_argument("--outline", default=layout.DEFAULT_OUTLINE,
                         help=f"board outline in mm, default "
                              f"{layout.DEFAULT_OUTLINE}; "
                              "'keep' preserves each source board's own outline, "
                              "'none' draws none")
        sub.add_argument("--gap", type=float, default=5.0,
                         help="millimetres between tiled boards (default: 5)")
        sub.add_argument("--columns", type=int, default=0,
                         help="force a column count for the grid layout")
        sub.add_argument("--sheet-layout", choices=("per-design", "packed", "single"),
                         default="per-design",
                         help="one sheet per design (default), several per sheet, "
                              "or every design on one sheet")
        sub.add_argument("--sheets-per-page", type=int, default=1,
                         help="designs per sheet when --sheet-layout packed")

    def add_policy(sub):
        sub.add_argument("--default", choices=("join", "split"), default="split",
                         help="what to do with contested nets when not asking "
                              "(default: split, which keeps designs electrically apart)")
        sub.add_argument("--replicas", choices=("join", "split"), default="split",
                         help="whether nets shared between copies of one design are "
                              "common (default: split, one net per copy)")
        sub.add_argument("--link", action="append",
                         help="tie differently named nets together, e.g. SDA=I2C_DATA "
                              "or SDA=I2C_DATA:BUS_SDA to name the result")
        sub.add_argument("--connect", action="append",
                         help="wire named designs' nets together, e.g. "
                              "esp32:GPIO5=relay:SIGNAL")
        sub.add_argument("--drop", action="append",
                         help="leave parts out, e.g. --drop MOUNTINGHOLE or "
                              "--drop esp32:FID*")

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
    planner.add_argument("-i", "--interactive", action="store_true",
                         help="ask about copies, links and contested nets")
    planner.set_defaults(func=cmd_plan)

    merger = subparsers.add_parser("merge", help="write the merged .sch and .brd")
    merger.add_argument("inputs", nargs="*",
                        help=".sch files, stems, or a directory (omit when using --plan). "
                             "Append *N for several copies")
    merger.add_argument("--count", action="append", help="copies of each input, in order")
    merger.add_argument("--prefix", action="append",
                        help="reference-designator prefix per input, in order")
    add_layout(merger)
    add_policy(merger)
    merger.add_argument("-o", "--output", default="", help="stem for the merged files")
    merger.add_argument("--out-dir", default="out", help="where to write (default: out)")
    merger.add_argument("--title", default="")
    merger.add_argument("--plan", help="read decisions from a plan file")
    merger.add_argument("--save-plan", help="write the decisions actually used")
    merger.add_argument("--ask-counts", action="store_true",
                        help="ask how many copies of each design to place")
    merger.add_argument("--no-suggest", action="store_true",
                        help="skip the differently-named-net suggestions")
    merger.add_argument("--no-prune", action="store_true",
                        help="skip the question about parts to leave out")
    merger.add_argument("-y", "--yes", action="store_true",
                        help="never ask; take the defaults for everything contested")
    merger.set_defaults(func=cmd_merge)

    parts = subparsers.add_parser(
        "parts", help="list parts, grouped so they can be dropped by kind")
    add_inputs(parts)
    parts.add_argument("--all", action="store_true",
                       help="show every kind, not just the top 30")
    parts.set_defaults(func=cmd_parts)

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
