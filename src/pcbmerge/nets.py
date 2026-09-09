"""Deciding which nets from different designs are the same net.

When two boards both have a net called GND, they mean the same wire and should
become one.  When they both have N$1, they mean nothing in particular and must
stay apart.  Between those extremes sit names like VCC or SDA, where only the
engineer knows.  This module sorts every shared name into one of those three
buckets and leaves the middle one for a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# Anonymous nets EAGLE invents for unlabelled wires.  Never meaningful.
ANONYMOUS = re.compile(r"^(?:N\$\d+|U\$\d+)$", re.IGNORECASE)

# Ground spellings that are universally the same node.
GROUND_ALIASES = {"GND", "VSS", "0V", "GROUND"}

# Rails whose name states the voltage, so a same-name match is a real match.
EXPLICIT_RAIL = re.compile(r"^[+-]?(\d+)(?:[.,V](\d+))?V?$")

# Rails that name a role, not a voltage.  Two boards can disagree about what
# VCC means, so these always go to the human.
AMBIGUOUS_RAILS = {
    "VCC", "VDD", "VIN", "VBAT", "VSYS", "VEE", "VREF", "VPP", "VMOTOR", "VM",
    "AVDD", "DVDD", "AVCC", "DVCC", "VDDA", "VDDIO", "VSERVO", "VMAIN", "VOUT",
    "AGND", "DGND", "PGND", "EARTH", "CHASSIS", "VREF+", "VREF-",
}

# Well-defined named rails that are safe to join automatically.
KNOWN_RAILS = {"VBUS"}


class Kind(str, Enum):
    """Why a net got the treatment it did."""

    UNIQUE = "unique"          # only one design uses this name
    ANONYMOUS = "anonymous"    # auto-generated, always kept separate
    GROUND = "ground"          # ground family, always joined
    RAIL = "rail"              # explicit-voltage or known rail, joined
    AMBIGUOUS = "ambiguous"    # role-named power, needs a decision
    SIGNAL = "signal"          # ordinary shared signal name, needs a decision


class Action(str, Enum):
    JOIN = "join"    # one net across all designs that use the name
    SPLIT = "split"  # per-design nets, renamed apart


AUTO_JOIN = {Kind.GROUND, Kind.RAIL}
AUTO_SPLIT = {Kind.ANONYMOUS}
NEEDS_DECISION = {Kind.AMBIGUOUS, Kind.SIGNAL}


def normalize(name: str) -> str:
    """Canonical key for a net name, so 3.3V, +3V3 and 3V3 land together."""
    key = name.strip().upper().replace(" ", "")
    if key in GROUND_ALIASES:
        return "GND"
    stripped = key.lstrip("+")
    match = EXPLICIT_RAIL.fullmatch(stripped)
    if match:
        whole, frac = match.group(1), match.group(2) or "0"
        sign = "-" if key.startswith("-") else ""
        return f"{sign}{whole}V{frac}"
    return key


def classify(name: str, design_count: int) -> Kind:
    """Bucket a net name given how many designs use it."""
    if ANONYMOUS.match(name.strip()):
        return Kind.ANONYMOUS
    if design_count < 2:
        return Kind.UNIQUE
    key = normalize(name)
    if key == "GND":
        return Kind.GROUND
    if key in KNOWN_RAILS:
        return Kind.RAIL
    if EXPLICIT_RAIL.fullmatch(key.lstrip("+")) or re.fullmatch(r"-?\d+V\d+", key):
        return Kind.RAIL
    if key in AMBIGUOUS_RAILS or name.strip().upper() in AMBIGUOUS_RAILS:
        return Kind.AMBIGUOUS
    return Kind.SIGNAL


@dataclass
class NetGroup:
    """Every net across the inputs that shares one normalized name."""

    key: str                                  # normalized name
    display: str                              # preferred spelling
    kind: Kind
    occurrences: dict[str, list[str]] = field(default_factory=dict)  # design -> raw names
    action: Action = Action.SPLIT
    merged_name: str = ""
    decided_by: str = "auto"                  # auto | plan | prompt | flag

    @property
    def designs(self) -> list[str]:
        return list(self.occurrences)

    @property
    def design_count(self) -> int:
        return len(self.occurrences)

    @property
    def spellings(self) -> list[str]:
        out: list[str] = []
        for names in self.occurrences.values():
            for n in names:
                if n not in out:
                    out.append(n)
        return out

    @property
    def needs_decision(self) -> bool:
        return self.kind in NEEDS_DECISION


class NetResolver:
    """Builds net groups from the loaded designs and applies a policy."""

    def __init__(self) -> None:
        self.groups: dict[str, NetGroup] = {}

    def add(self, design: str, net_names: list[str]) -> None:
        for raw in net_names:
            if not raw:
                continue
            key = normalize(raw)
            group = self.groups.get(key)
            if group is None:
                group = NetGroup(key=key, display=raw, kind=Kind.UNIQUE)
                self.groups[key] = group
            group.occurrences.setdefault(design, [])
            if raw not in group.occurrences[design]:
                group.occurrences[design].append(raw)

    def finalize(self, default_action: Action = Action.SPLIT) -> None:
        """Classify every group and set the automatic decisions."""
        for group in self.groups.values():
            group.kind = classify(group.display, group.design_count)
            group.display = _preferred_spelling(group)
            if group.kind is Kind.UNIQUE:
                # A name only one design uses carries over untouched.
                group.action = Action.JOIN
                group.merged_name = group.display
            elif group.kind in AUTO_JOIN:
                group.action = Action.JOIN
                group.merged_name = group.display
            elif group.kind in AUTO_SPLIT:
                group.action = Action.SPLIT
            else:
                group.action = default_action
                if group.action is Action.JOIN:
                    group.merged_name = group.display

    # -- queries -------------------------------------------------------------
    def open_questions(self) -> list[NetGroup]:
        return sorted(
            (g for g in self.groups.values() if g.needs_decision),
            key=lambda g: (-g.design_count, g.key),
        )

    def joined(self) -> list[NetGroup]:
        return [g for g in self.groups.values() if g.action is Action.JOIN and g.design_count > 1]

    def all_groups(self) -> list[NetGroup]:
        return sorted(self.groups.values(), key=lambda g: (-g.design_count, g.key))

    def group_for(self, raw_name: str) -> NetGroup | None:
        return self.groups.get(normalize(raw_name))


def _preferred_spelling(group: NetGroup) -> str:
    """Pick the spelling the inputs use most; ties go to the first design.

    Deferring to input order means the merged file keeps the convention of the
    design the engineer listed first, which is the one they think in.
    """
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for names in group.occurrences.values():
        for n in names:
            counts[n] = counts.get(n, 0) + 1
            order.setdefault(n, len(order))
    return sorted(counts, key=lambda n: (-counts[n], order[n]))[0]


def plan_design_names(
    group: NetGroup, design: str, prefix: str, taken: set[str]
) -> dict[str, str]:
    """Map one design's raw net names in this group to their merged names.

    Two nets inside a single design are always distinct, even when they
    normalize alike -- a board carrying both `3.3V` and `+3V3` means two
    separate nodes.  Only one of them can inherit a joined name; the rest are
    kept apart under the design prefix.
    """
    from .eagle import unique_name  # local import keeps module import order simple

    raws = group.occurrences.get(design, [])
    mapping: dict[str, str] = {}
    winner: str | None = None
    if group.action is Action.JOIN:
        winner = group.display if group.display in raws else (raws[0] if raws else None)
    for raw in raws:
        if raw == winner:
            name = group.merged_name or group.display
        else:
            base = f"{prefix}{raw}" if prefix else raw
            name = unique_name(base, taken)
        mapping[raw] = name
        taken.add(name)
    return mapping
