"""Asking the engineer the things the tool will not guess at.

Every question here exists because guessing would be worse than asking:

- how many copies of a design to place;
- which parts are worth carrying over at all;
- which of one design's nets should reach another's;
- which nets are common across copies of one design;
- whether two same-named nets from different designs are one node;
- whether two differently-named nets are secretly the same wire.
"""

from __future__ import annotations

import sys

from .linking import Suggestion
from .nets import Action, Kind, NetGroup, NetResolver

HELP = """
  j  join    - one net across all these designs (they are the same node)
  s  split   - keep them separate, renamed per design
  r  rename  - join, but under a name you choose
  a  all     - apply this answer to every remaining question
  ?  help
"""


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(text: str, default: str = "") -> str:
    try:
        raw = input(text).strip()
    except EOFError:
        raw = ""
    return raw or default


# --------------------------------------------------------------------------
# copy counts
# --------------------------------------------------------------------------

def ask_counts(specs, default: int = 1) -> int:
    """Ask how many copies of each design to place. Returns how many changed."""
    print("\nHow many copies of each design?  Enter for one.\n")
    changed = 0
    for spec in specs:
        while True:
            raw = _ask(f"  {spec.name}  [{spec.count or default}] ")
            if not raw:
                break
            if raw.isdigit() and int(raw) >= 1:
                if int(raw) != spec.count:
                    spec.count = int(raw)
                    changed += 1
                break
            print("    give a whole number, 1 or more")
    print()
    return changed


# --------------------------------------------------------------------------
# nets shared between copies of one design
# --------------------------------------------------------------------------

def ask_replicas(resolver: NetResolver) -> int:
    """For each replicated design, ask which of its nets are common.

    Four copies of a relay board have four separate control signals but very
    likely one shared I2C bus.  Only the engineer knows which is which, so the
    question is asked once per design with every candidate listed.
    """
    questions = resolver.replica_questions()
    if not questions:
        return 0

    by_source: dict[str, list[NetGroup]] = {}
    for group in questions:
        source = group.sources[0] if group.sources else ""
        by_source.setdefault(source, []).append(group)

    answered = 0
    for source, groups in by_source.items():
        copies = max(g.design_count for g in groups)
        print(f"\n{source}: {copies} copies.")
        print("Which of these signals are common to all copies?")
        print("Anything you do not pick becomes one net per copy.\n")
        for index, group in enumerate(groups, 1):
            print(f"  {index:>2}  {group.display}")
        print()
        raw = _ask("  numbers, 'a' for all, Enter for none: ").lower()

        chosen: set[int] = set()
        if raw in ("a", "all"):
            chosen = set(range(1, len(groups) + 1))
        elif raw:
            for piece in raw.replace(",", " ").split():
                if piece.isdigit():
                    chosen.add(int(piece))

        for index, group in enumerate(groups, 1):
            group.decided_by = "prompt"
            if index in chosen:
                group.action = Action.JOIN
                group.merged_name = group.display
            else:
                group.action = Action.SPLIT
            answered += 1
    print()
    return answered


# --------------------------------------------------------------------------
# same name, different designs
# --------------------------------------------------------------------------

def describe(group: NetGroup) -> str:
    where = ", ".join(group.sources)
    spellings = " / ".join(group.spellings)
    reason = {
        Kind.AMBIGUOUS: "names a role, not a voltage, so the designs may disagree",
        Kind.SIGNAL: "shared signal name",
    }.get(group.kind, group.kind.value)
    return "\n".join([
        f"  net      {spellings}",
        f"  used by  {group.source_count} designs: {where}",
        f"  why ask  {reason}",
    ])


def ask_all(resolver: NetResolver, default: Action = Action.SPLIT) -> int:
    """Walk the open questions. Returns how many the user answered."""
    questions = resolver.open_questions()
    if not questions:
        return 0

    print(f"\n{len(questions)} net name(s) need a decision.\n")
    answered = 0
    blanket: Action | None = None

    for index, group in enumerate(questions, 1):
        if blanket is not None:
            group.action = blanket
            group.decided_by = "prompt"
            if blanket is Action.JOIN:
                group.merged_name = group.display
            answered += 1
            continue

        print(f"[{index}/{len(questions)}]")
        print(describe(group))
        suggestion = "j" if default is Action.JOIN else "s"
        while True:
            answer = _ask(f"  join / split / rename? [{suggestion}] ", suggestion).lower()

            if answer in ("?", "h", "help"):
                print(HELP)
                continue
            if answer in ("a", "all"):
                blanket = group.action = default
                group.decided_by = "prompt"
                if default is Action.JOIN:
                    group.merged_name = group.display
                break
            if answer in ("j", "join"):
                group.action = Action.JOIN
                group.merged_name = group.display
                group.decided_by = "prompt"
                break
            if answer in ("s", "split"):
                group.action = Action.SPLIT
                group.decided_by = "prompt"
                break
            if answer in ("r", "rename"):
                new = _ask("  merged net name: ")
                if not new:
                    print("  need a name")
                    continue
                group.action = Action.JOIN
                group.merged_name = new
                group.decided_by = "prompt"
                break
            print("  answer j, s, r, a or ?")
        answered += 1
        print()
    return answered


# --------------------------------------------------------------------------
# different names, possibly the same wire
# --------------------------------------------------------------------------

def ask_links(resolver: NetResolver, suggestions: list[Suggestion]) -> int:
    """Offer connections between differently named nets. Returns links made."""
    if not suggestions:
        return 0

    print(f"\n{len(suggestions)} net(s) look like they might be the same wire.\n")
    made = 0
    for index, suggestion in enumerate(suggestions, 1):
        left = ", ".join(suggestion.left_designs)
        right = ", ".join(suggestion.right_designs)
        print(f"[{index}/{len(suggestions)}]  confidence {suggestion.score:.0%}")
        print(f"  {suggestion.left_name:<20} {left}")
        print(f"  {suggestion.right_name:<20} {right}")
        print(f"  why      {suggestion.reason}")

        while True:
            answer = _ask("  connect these? [n] y/n/rename ", "n").lower()
            if answer in ("n", "no", ""):
                break
            if answer in ("y", "yes"):
                resolver.link([suggestion.left, suggestion.right], suggestion.default_name)
                made += 1
                break
            if answer in ("r", "rename"):
                name = _ask("  merged net name: ", suggestion.default_name)
                resolver.link([suggestion.left, suggestion.right], name)
                made += 1
                break
            print("  answer y, n or rename")
        print()
    return made


# --------------------------------------------------------------------------
# parts to leave out
# --------------------------------------------------------------------------

def _picked(raw: str, count: int) -> set[int]:
    """Read a numbers-or-all answer into a set of one-based indices."""
    answer = raw.strip().lower()
    if answer in ("a", "all"):
        return set(range(1, count + 1))
    chosen: set[int] = set()
    for piece in answer.replace(",", " ").split():
        if "-" in piece[1:]:
            start, _, end = piece.partition("-")
            if start.isdigit() and end.isdigit():
                chosen.update(range(int(start), int(end) + 1))
        elif piece.isdigit():
            chosen.add(int(piece))
    return {n for n in chosen if 1 <= n <= count}


def ask_drops(groups, limit: int = 25) -> list[str]:
    """Offer to leave parts out. Returns drop patterns the user chose.

    Grouped by kind, because deciding about twenty mounting holes one at a
    time is not a decision, it is data entry.
    """
    offered = [g for g in groups if g.count > 1 or g.mechanical][:limit]
    if not offered:
        return []

    mechanical = [g for g in offered if g.mechanical]
    print("\nParts you may not want on the merged board.")
    print("Mounting holes and fiducials sit at each sub-board's old position,")
    print("and page borders overlap into noise.\n")
    for index, group in enumerate(offered, 1):
        mark = "*" if group.mechanical else " "
        print(f"  {index:>2}{mark} {group.label:<30} {group.count:>3} copies "
              f"in {len(group.designs)} designs   {group.note}")
    print("\n  * nothing is wired to these")

    total = sum(g.count for g in mechanical)
    hint = f", 'm' for the {len(mechanical)} starred ({total} parts)" if mechanical else ""
    raw = _ask(f"\n  numbers to drop{hint}, Enter to keep everything: ")

    if raw.strip().lower() in ("m", "mech", "mechanical"):
        chosen = {i for i, g in enumerate(offered, 1) if g.mechanical}
    else:
        chosen = _picked(raw, len(offered))

    patterns = [offered[i - 1].kind for i in sorted(chosen)]
    if patterns:
        print(f"  dropping {len(patterns)} kind(s): {', '.join(patterns)}")
    print()
    return patterns


# --------------------------------------------------------------------------
# wiring specific designs together
# --------------------------------------------------------------------------

def ask_connections(resolver, designs) -> int:
    """Let the user wire one design's net to another's, by hand.

    This is the case no rule and no scorer reaches: you know the controller's
    GPIO5 drives the first relay board, and nothing in either file says so.
    """
    names = [d.name for d in designs]
    if len(names) < 2:
        return 0

    print("\nConnect nets between designs?  Enter to skip.")
    print("Write them as  design:net = design:net,  for example")
    print(f"  1:GPIO5 = 2:SIGNAL\n")
    for index, name in enumerate(names, 1):
        print(f"  {index:>2}  {name}")

    made = 0
    while True:
        raw = _ask("\n  connection (Enter when done): ")
        if not raw:
            break
        members = parse_connection(raw, names)
        if members is None:
            print("  write it as  design:net = design:net")
            continue
        if len({design for design, _ in members}) < 2:
            print("  those are both on the same design; nothing to connect")
            continue
        unknown = [f"{d}:{n}" for d, n in members
                   if n not in resolver.nets_by_design().get(d, [])]
        if unknown:
            print(f"  no such net: {', '.join(unknown)}")
            continue
        name = _ask("  name for the merged net [%s]: " % members[0][1], members[0][1])
        resolver.connect(members, name)
        print(f"  connected {' = '.join(f'{d}:{n}' for d, n in members)} as {name}")
        made += 1
    return made


def parse_connection(text: str, designs: list[str]) -> list[tuple[str, str]] | None:
    """Read `1:GPIO5 = 2:SIGNAL`, accepting an index or a name for the design."""
    parts = [p.strip() for p in text.split("=") if p.strip()]
    if len(parts) < 2:
        return None
    members: list[tuple[str, str]] = []
    for part in parts:
        design, sep, net = part.rpartition(":")
        if not sep or not net.strip():
            return None
        design, net = design.strip(), net.strip()
        if design.isdigit():
            index = int(design)
            if not 1 <= index <= len(designs):
                return None
            design = designs[index - 1]
        elif design not in designs:
            matches = [d for d in designs if design.lower() in d.lower()]
            if len(matches) != 1:
                return None
            design = matches[0]
        members.append((design, net))
    return members
