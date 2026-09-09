"""Asking the engineer about the nets the tool will not guess at.

Only genuinely contested names reach this point: role-named rails like VCC,
and ordinary signal names that several designs happen to share.  Everything
else was already settled by rule.
"""

from __future__ import annotations

import sys

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


def describe(group: NetGroup) -> str:
    where = ", ".join(group.designs)
    spellings = " / ".join(group.spellings)
    reason = {
        Kind.AMBIGUOUS: "names a role, not a voltage, so the designs may disagree",
        Kind.SIGNAL: "shared signal name",
    }.get(group.kind, group.kind.value)
    lines = [
        f"  net      {spellings}",
        f"  used by  {group.design_count} designs: {where}",
        f"  why ask  {reason}",
    ]
    return "\n".join(lines)


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
            try:
                raw = input(f"  join / split / rename? [{suggestion}] ").strip()
            except EOFError:
                raw = ""
            answer = (raw or suggestion).lower()

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
                new = input("  merged net name: ").strip()
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
