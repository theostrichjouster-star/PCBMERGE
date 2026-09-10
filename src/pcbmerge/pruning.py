"""Dropping parts that only made sense on the original boards.

Eight breakout boards carry eight page borders, twenty mounting holes and
twelve fiducials.  On one merged board almost none of that is wanted: the
holes are at each sub-board's old position, the fiducials belong to panels
that no longer exist, and the borders overlap into noise.

This module catalogues what is there, groups it so a decision covers every
copy at once, and works out which designators a set of rules removes.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

# Devicesets that draw something rather than doing something.  Used only to
# sort the catalogue helpfully; nothing is dropped without being asked for.
MECHANICAL_HINTS = (
    "FRAME", "FIDUCIAL", "MOUNTINGHOLE", "MOUNTING_HOLE", "LOGO", "PLABEL",
    "TESTPOINT", "STANDOFF", "SPACER", "MARK", "ARROW", "DOCFIELD",
)


@dataclass
class PartRef:
    """One part or footprint, in one design instance."""

    design: str
    source: str
    name: str            # the designator as written in the source file
    library: str = ""
    deviceset: str = ""
    package: str = ""
    value: str = ""
    pins: int = 0        # how many net connections it has
    on_schematic: bool = False
    on_board: bool = False

    @property
    def mechanical(self) -> bool:
        """Draws something rather than doing something."""
        text = f"{self.deviceset} {self.package} {self.name}".upper()
        if any(hint in text for hint in MECHANICAL_HINTS):
            return True
        return self.pins == 0

    @property
    def label(self) -> str:
        return f"{self.design}:{self.name}"

    @property
    def kind(self) -> str:
        """The part's type, with any copy number or generated id folded away.

        PLABEL0 through PLABEL32 are one kind of thing to anyone deciding
        whether to keep them, so they are catalogued as one.  So are the
        silkscreen labels a KiCad plugin stamps as KIBUZZARD-6569BE4A,
        KIBUZZARD-6569BE57 and so on: the hex tail is an id, not a kind.
        """
        base = (self.deviceset or self.package or "?").upper()
        stem = re.fullmatch(r"([A-Z]{3,})(\d+)", base)
        if stem:
            return stem.group(1)
        tagged = re.fullmatch(r"(.+?)[-_][0-9A-F]{6,}", base)
        return tagged.group(1) if tagged else base

    def key(self) -> tuple[str, str]:
        """What makes two parts the same kind of thing.

        Value distinguishes a 5.1K resistor from a 10K one, but says nothing
        useful about a fiducial, so it is only part of the key when the part
        actually does something.
        """
        return (self.kind, "" if self.mechanical else self.value)


@dataclass
class PartGroup:
    """Every copy of one kind of part, across every design."""

    key: tuple[str, str]           # kind, value
    refs: list[PartRef] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.key[0]

    @property
    def value(self) -> str:
        return self.key[1]

    @property
    def label(self) -> str:
        return f"{self.kind} {self.value}".strip()

    @property
    def count(self) -> int:
        return len(self.refs)

    @property
    def designs(self) -> list[str]:
        out: list[str] = []
        for ref in self.refs:
            if ref.design not in out:
                out.append(ref.design)
        return out

    @property
    def pins(self) -> int:
        return sum(r.pins for r in self.refs)

    @property
    def mechanical(self) -> bool:
        return all(r.mechanical for r in self.refs)

    @property
    def note(self) -> str:
        if self.pins == 0:
            return "no connections"
        return f"{self.pins} connections"


def catalog(designs) -> list[PartGroup]:
    """Group every part in every design by what kind of thing it is.

    Sorted so the things most worth removing float to the top: many copies,
    nothing wired to them.
    """
    groups: dict[tuple[str, str], PartGroup] = {}
    for design in designs:
        for ref in _refs_for(design):
            group = groups.setdefault(ref.key(), PartGroup(key=ref.key()))
            group.refs.append(ref)
    ordered = sorted(groups.values(),
                     key=lambda g: (not g.mechanical, -g.count, g.label))
    return ordered


def _refs_for(design) -> list[PartRef]:
    """Catalogue one design's parts and its board-only footprints."""
    used: dict[str, int] = {}
    for pinref in design.sch.section.iterfind("sheets/sheet/nets/net//pinref"):
        name = pinref.get("part", "")
        used[name] = used.get(name, 0) + 1

    # What the copper reaches counts too.  A footprint the schematic never
    # mentions can still have tracks on its pads, and a part with tracks on
    # it is not decoration whatever the schematic says.
    wired: dict[str, int] = {}
    packages: dict[str, str] = {}
    values: dict[str, str] = {}
    if design.brd is not None:
        for element in design.brd.elements():
            packages[element.get("name", "")] = element.get("package", "")
            values[element.get("name", "")] = element.get("value", "") or ""
        for contact in design.brd.section.iterfind("signals/signal//contactref"):
            name = contact.get("element", "")
            wired[name] = wired.get(name, 0) + 1

    refs: list[PartRef] = []
    seen: set[str] = set()
    for part in design.sch.parts():
        name = part.get("name", "")
        seen.add(name)
        refs.append(PartRef(
            design=design.name, source=design.source, name=name,
            library=part.get("library", ""), deviceset=part.get("deviceset", ""),
            package=packages.get(name, ""), value=part.get("value", "") or "",
            pins=max(used.get(name, 0), wired.get(name, 0)),
            on_schematic=True, on_board=name in packages,
        ))

    for name, package in packages.items():
        if name in seen:
            continue
        # A footprint placed straight onto the board: silkscreen labels,
        # logos, the odd mounting hole; or, on a board whose schematic was
        # only partly published, a real part.
        refs.append(PartRef(
            design=design.name, source=design.source, name=name,
            package=package, value=values.get(name, ""), pins=wired.get(name, 0),
            on_schematic=False, on_board=True,
        ))
    return refs


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

@dataclass
class DropRule:
    """A pattern saying which parts to leave out."""

    pattern: str
    design: str = ""     # empty means every design

    def __str__(self) -> str:
        return f"{self.design}:{self.pattern}" if self.design else self.pattern


def parse_drop(text: str) -> DropRule:
    """Parse a --drop argument: `MOUNTINGHOLE`, `ESP32:FID*`, `*:FRAME1`."""
    design, sep, pattern = text.partition(":")
    if not sep:
        design, pattern = "", text
    pattern = pattern.strip()
    design = design.strip()
    if not pattern:
        raise ValueError(f"drop rule needs a pattern: {text!r}")
    if design in ("*", "all"):
        design = ""
    return DropRule(pattern=pattern, design=design)


def rule_matches(rule: DropRule, ref: PartRef) -> bool:
    """Does this rule name this part?

    A pattern is matched, case-insensitively, against the designator, the
    deviceset and the package, so `MOUNTINGHOLE` catches the part whatever it
    was called and `FID*` catches it by designator.
    """
    if rule.design and not _design_matches(rule.design, ref):
        return False
    pattern = rule.pattern.upper()
    if "*" not in pattern and "?" not in pattern:
        pattern = f"*{pattern}*"
    return any(fnmatch.fnmatchcase(field.upper(), pattern)
               for field in (ref.name, ref.deviceset, ref.package) if field)


def _design_matches(wanted: str, ref: PartRef) -> bool:
    pattern = wanted.upper()
    if "*" not in pattern and "?" not in pattern:
        pattern = f"*{pattern}*"
    return any(fnmatch.fnmatchcase(name.upper(), pattern)
               for name in (ref.design, ref.source))


def resolve(designs, rules: list[DropRule]) -> dict[str, set[str]]:
    """Work out which designators each rule removes, per design instance."""
    drops: dict[str, set[str]] = {}
    if not rules:
        return drops
    for design in designs:
        for ref in _refs_for(design):
            if any(rule_matches(rule, ref) for rule in rules):
                drops.setdefault(ref.design, set()).add(ref.name)
    return drops


def unmatched(designs, rules: list[DropRule]) -> list[DropRule]:
    """Rules that name nothing, so a typo is reported rather than ignored."""
    refs = [ref for design in designs for ref in _refs_for(design)]
    return [rule for rule in rules
            if not any(rule_matches(rule, ref) for ref in refs)]


def connections_lost(designs, drops: dict[str, set[str]]) -> list[tuple[str, str, int]]:
    """Parts being dropped that something is actually wired to.

    Removing a decoration is free.  Removing a part with connections changes
    the netlist, so it is worth saying out loud.
    """
    out: list[tuple[str, str, int]] = []
    for design in designs:
        names = drops.get(design.name, set())
        if not names:
            continue
        for ref in _refs_for(design):
            if ref.name in names and ref.pins:
                out.append((ref.design, ref.name, ref.pins))
    return sorted(out, key=lambda item: -item[2])
