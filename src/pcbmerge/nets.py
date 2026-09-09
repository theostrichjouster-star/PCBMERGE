"""Deciding which nets from different designs are the same net.

When two boards both have a net called GND, they mean the same wire and should
become one.  When they both have N$1, they mean nothing in particular and must
stay apart.  Between those extremes sit names like VCC or SDA, where only the
engineer knows.

Replication adds a fourth case.  Four copies of one relay board all have a net
called SIGNAL, but they are four separate channels, so the copies are numbered
apart unless you say the signal is common to all of them.
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
    REPLICA = "replica"        # same name in several copies of one design


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


def classify(name: str, source_count: int, instance_count: int) -> Kind:
    """Bucket a net name.

    `source_count` counts distinct input designs; `instance_count` counts the
    copies those designs were expanded into.  The difference is what separates
    a genuine cross-design clash from replication of a single board.
    """
    if ANONYMOUS.match(name.strip()):
        return Kind.ANONYMOUS

    key = normalize(name)
    if instance_count > 1:
        # Rails are joined across copies as readily as across designs.
        if key == "GND":
            return Kind.GROUND
        if key in KNOWN_RAILS or EXPLICIT_RAIL.fullmatch(key.lstrip("+")):
            return Kind.RAIL

    if source_count > 1:
        if key in AMBIGUOUS_RAILS or name.strip().upper() in AMBIGUOUS_RAILS:
            return Kind.AMBIGUOUS
        return Kind.SIGNAL
    if instance_count > 1:
        return Kind.REPLICA
    return Kind.UNIQUE


@dataclass(frozen=True)
class NetRef:
    """One net, in one instance of one design."""

    design: str   # instance name, unique across the merge
    source: str   # the input design it was copied from
    raw: str      # the net name as written in that file


@dataclass
class NetGroup:
    """Every net across the inputs that shares one resolution key."""

    key: str
    display: str
    kind: Kind = Kind.UNIQUE
    refs: list[NetRef] = field(default_factory=list)
    action: Action = Action.SPLIT
    merged_name: str = ""
    decided_by: str = "auto"   # auto | plan | prompt | link

    @property
    def occurrences(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for ref in self.refs:
            names = out.setdefault(ref.design, [])
            if ref.raw not in names:
                names.append(ref.raw)
        return out

    @property
    def designs(self) -> list[str]:
        return list(self.occurrences)

    @property
    def sources(self) -> list[str]:
        out: list[str] = []
        for ref in self.refs:
            if ref.source not in out:
                out.append(ref.source)
        return out

    @property
    def design_count(self) -> int:
        return len(self.designs)

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def spellings(self) -> list[str]:
        out: list[str] = []
        for ref in self.refs:
            if ref.raw not in out:
                out.append(ref.raw)
        return out

    @property
    def needs_decision(self) -> bool:
        return self.kind in NEEDS_DECISION

    @property
    def is_replica_question(self) -> bool:
        return self.kind is Kind.REPLICA


class NetResolver:
    """Builds net groups from the loaded designs and applies a policy."""

    def __init__(self) -> None:
        self.refs: list[NetRef] = []
        self.groups: dict[str, NetGroup] = {}
        # Forced regrouping, from explicit links and accepted suggestions.
        self.key_alias: dict[str, str] = {}
        # A connection naming one design's copy of a net, rather than the name
        # everywhere: (design, raw name) -> the group it is pulled into.
        self.ref_alias: dict[tuple[str, str], str] = {}
        self.link_names: dict[str, str] = {}
        self.linked_keys: set[str] = set()

    # -- input --------------------------------------------------------------
    def add(self, design: str, net_names: list[str], source: str | None = None) -> None:
        source = source or design
        for raw in net_names:
            if raw:
                self.refs.append(NetRef(design=design, source=source, raw=raw))

    def link(self, keys: list[str], name: str = "") -> str:
        """Force several resolution keys to become one net.

        This is how a net called SDA on one board is tied to one called
        I2C_DATA on another, which no naming rule could have matched.
        """
        resolved = [self.key_alias.get(k, k) for k in keys if k]
        if not resolved:
            return ""
        target = resolved[0]
        absorbed = set(resolved[1:]) | set(keys)
        absorbed.discard(target)

        for key in absorbed:
            self.key_alias[key] = target
        # Anything that previously pointed at an absorbed key follows it over.
        for key, value in list(self.key_alias.items()):
            if value in absorbed:
                self.key_alias[key] = target
        self.key_alias.pop(target, None)

        self.linked_keys.add(target)
        self.linked_keys -= absorbed
        if name:
            self.link_names[target] = name
        return target

    def connect(self, members: list[tuple[str, str]], name: str = "") -> str:
        """Wire specific designs' nets together, leaving other copies alone.

        `link` works on a name everywhere it appears.  This works on one
        design's copy of a net, which is what you need to run a controller's
        GPIO to the first relay board and not to the other three.
        """
        members = [(d, r) for d, r in members if d and r]
        if len(members) < 2:
            return ""
        target = normalize(name) if name else normalize(members[0][1])
        target = self.key_alias.get(target, target)
        for design, raw in members:
            self.ref_alias[(design, raw)] = target
        self.linked_keys.add(target)
        self.link_names[target] = name or members[0][1]
        return target

    def key_for(self, ref: NetRef) -> str:
        """Which group a net belongs to, honouring links and connections."""
        target = self.ref_alias.get((ref.design, ref.raw))
        if target is None:
            target = normalize(ref.raw)
        return self.key_alias.get(target, target)

    # -- resolution ---------------------------------------------------------
    def finalize(self, default_action: Action = Action.SPLIT,
                 replica_action: Action = Action.SPLIT) -> None:
        """Rebuild every group from the refs and set automatic decisions."""
        self.groups = {}
        for ref in self.refs:
            key = self.key_for(ref)
            group = self.groups.get(key)
            if group is None:
                group = NetGroup(key=key, display=ref.raw)
                self.groups[key] = group
            group.refs.append(ref)

        for group in self.groups.values():
            group.display = _preferred_spelling(group)
            group.kind = classify(group.display, group.source_count, group.design_count)

            if group.key in self.linked_keys:
                # An explicit link is a decision already made.
                group.action = Action.JOIN
                group.merged_name = self.link_names.get(group.key) or group.display
                group.decided_by = "link"
            elif group.kind is Kind.UNIQUE:
                group.action = Action.JOIN
                group.merged_name = group.display
            elif group.kind in AUTO_JOIN:
                group.action = Action.JOIN
                group.merged_name = group.display
            elif group.kind in AUTO_SPLIT:
                group.action = Action.SPLIT
            elif group.kind is Kind.REPLICA:
                group.action = replica_action
                if replica_action is Action.JOIN:
                    group.merged_name = group.display
            else:
                group.action = default_action
                if group.action is Action.JOIN:
                    group.merged_name = group.display

    # -- queries -------------------------------------------------------------
    def open_questions(self) -> list[NetGroup]:
        return sorted(
            (g for g in self.groups.values() if g.needs_decision),
            key=lambda g: (-g.source_count, g.key),
        )

    def replica_questions(self) -> list[NetGroup]:
        return sorted(
            (g for g in self.groups.values() if g.is_replica_question),
            key=lambda g: (g.sources[0] if g.sources else "", g.key),
        )

    def joined(self) -> list[NetGroup]:
        return [g for g in self.groups.values()
                if g.action is Action.JOIN and g.design_count > 1]

    def all_groups(self) -> list[NetGroup]:
        return sorted(self.groups.values(), key=lambda g: (-g.design_count, g.key))

    def group_for(self, raw_name: str, design: str = "") -> NetGroup | None:
        """The group a net name lands in, for one design or in general.

        Pass the design when connections may have pulled one copy of a name
        somewhere its siblings did not follow.
        """
        if design:
            return self.groups.get(self.key_for(NetRef(design, design, raw_name)))
        key = normalize(raw_name)
        found = self.groups.get(self.key_alias.get(key, key))
        if found is not None:
            return found
        # A connection may have moved this name into a group of its own, under
        # a key the name alone does not reach.
        for (_, raw), target in self.ref_alias.items():
            if raw == raw_name:
                return self.groups.get(target)
        return None

    def nets_by_design(self) -> dict[str, list[str]]:
        """Every design's net names, in the order the files list them."""
        out: dict[str, list[str]] = {}
        for ref in self.refs:
            names = out.setdefault(ref.design, [])
            if ref.raw not in names:
                names.append(ref.raw)
        return out


def _preferred_spelling(group: NetGroup) -> str:
    """Pick the spelling the inputs use most; ties go to the first design.

    Deferring to input order means the merged file keeps the convention of the
    design the engineer listed first, which is the one they think in.
    """
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for ref in group.refs:
        counts[ref.raw] = counts.get(ref.raw, 0) + 1
        order.setdefault(ref.raw, len(order))
    return sorted(counts, key=lambda n: (-counts[n], order[n]))[0]


def plan_design_names(
    group: NetGroup, design: str, prefix: str, taken: set[str]
) -> dict[str, str]:
    """Map one design instance's raw net names in this group to merged names.

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
