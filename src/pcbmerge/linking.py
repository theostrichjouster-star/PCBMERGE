"""Guessing which differently-named nets are meant to connect.

Name matching only ever finds the easy cases.  A sensor board calling its bus
`I2C_DATA` and a controller calling it `SDA` describe the same wire, and no
normalisation rule will discover that.  This module proposes such pairs and
scores them, so the engineer confirms or rejects rather than hunting.

Nothing here connects anything on its own.  Every suggestion is a question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .nets import Action, Kind, NetGroup, NetResolver

# Words that mean the same signal in different house styles.  The first entry
# of each list is the representative every other spelling folds to, so it is
# chosen to be the clearest name rather than the shortest.
SYNONYMS: list[list[str]] = [
    ["SDA", "DATA", "DAT", "SDI", "MOSI", "DIN"],
    ["SCL", "SCK", "CLK", "CLOCK", "SCLK"],
    ["MISO", "SDO", "DOUT"],
    ["CS", "SS", "NSS", "CHIPSELECT", "SEL"],
    ["RX", "RXD", "RECEIVE"],
    ["TX", "TXD", "TRANSMIT"],
    ["RESET", "RST", "NRST", "MR"],
    ["ENABLE", "EN", "CE", "SHDN"],
    ["INT", "IRQ", "INTERRUPT", "ALERT"],
    ["DP", "D+", "USBDP", "DPLUS"],
    ["DM", "D-", "USBDM", "DMINUS"],
]

# Tokens that say nothing about which signal this is.  Deliberately short:
# a loose noise list turns every net into a match for every other one.
NOISE = {"NET", "LINE", "PIN", "PAD", "BUS", "PORT", "CONN", "THE"}

_SPLIT = re.compile(r"[^A-Za-z0-9+\-]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Header pin labels: A0, D13, IO2.  These name a position on a connector, not
# a signal, so two of them are never evidence of the same wire.
PIN_LABEL = re.compile(r"^[A-Z]{1,2}\d{1,3}$")

MIN_SCORE = 0.6


@dataclass
class Suggestion:
    """A proposed connection between two resolution keys."""

    left: str            # group key
    right: str           # group key
    left_name: str
    right_name: str
    score: float
    reason: str
    left_designs: list[str]
    right_designs: list[str]

    @property
    def default_name(self) -> str:
        """The shorter, more conventional of the two names."""
        a, b = self.left_name, self.right_name
        return a if (len(a), a) <= (len(b), b) else b


def tokens(name: str) -> list[str]:
    """Break a net name into comparable, synonym-folded tokens."""
    # Only split on camel-case boundaries when the name really is camel-case.
    # An all-caps name like I2C_DATA has no boundaries, and splitting on the
    # digit would tear I2C into I2 and C.
    expanded = _CAMEL.sub("_", name) if any(c.islower() for c in name) else name
    raw = [t for t in _SPLIT.split(expanded.upper()) if t]
    out: list[str] = []
    for token in raw:
        # A trailing index on a real word (SDA1, CHAN2) is a channel marker
        # rather than identity.  On a short label (A1, D7) the digit *is* the
        # identity, so it stays.
        stem = re.sub(r"(?<=[A-Z])(\d+)$", "", token)
        if len(stem) >= 3:
            token = stem
        if token in NOISE:
            continue
        out.append(canonical(token))
    return out or [name.upper()]


def _is_pin_label(name: str) -> bool:
    return bool(PIN_LABEL.fullmatch(name.strip().upper()))


def _substantial(words: set[str]) -> bool:
    """Enough signal in these tokens to draw a conclusion from.

    Two characters is the floor: CS and EN are real signal names, while a
    bare letter is noise.  Connector pin labels are rejected before this.
    """
    return bool(words) and max(len(w) for w in words) >= 2


def canonical(token: str) -> str:
    for group in SYNONYMS:
        if token in group:
            return group[0]
    return token


def similarity(left: str, right: str) -> tuple[float, str]:
    """Score how likely two net names are to be the same wire.

    Deliberately conservative.  A wrong suggestion costs the engineer more
    attention than a missed one, because a missed connection is still visible
    as an unrouted net while a wrong one has to be spotted and undone.
    """
    if left.strip().upper() == right.strip().upper():
        return 0.0, ""

    # Two connector pin labels say nothing about being the same signal.
    if _is_pin_label(left) or _is_pin_label(right):
        return 0.0, ""

    set_a, set_b = set(tokens(left)), set(tokens(right))
    if not _substantial(set_a) or not _substantial(set_b):
        return 0.0, ""

    if set_a == set_b:
        return 0.95, "same signal words in a different style"

    # One name's words are all present in the other: SDA against I2C_SDA.
    if set_a < set_b or set_b < set_a:
        smaller = set_a if len(set_a) < len(set_b) else set_b
        if _substantial(smaller):
            extra = ", ".join(sorted(set_a ^ set_b))
            return 0.78, f"same words plus {extra}"

    overlap = set_a & set_b
    if overlap and _substantial(overlap):
        jaccard = len(overlap) / len(set_a | set_b)
        if jaccard >= 0.5:
            return 0.6 + 0.3 * jaccard, f"shares {', '.join(sorted(overlap))}"

    plain_a, plain_b = "".join(sorted(set_a)), "".join(sorted(set_b))
    if min(len(plain_a), len(plain_b)) >= 4:
        ratio = SequenceMatcher(None, plain_a, plain_b).ratio()
        if ratio >= 0.85:
            return ratio * 0.8, "names are nearly identical"
    return 0.0, ""


def suggest(resolver: NetResolver, limit: int = 25) -> list[Suggestion]:
    """Find likely connections the naming rules could not see.

    Only nets from different source designs are considered, and only ones not
    already joined -- a suggestion is for the cases that would otherwise be
    silently left unconnected.
    """
    candidates = [
        g for g in resolver.groups.values()
        if g.kind not in (Kind.ANONYMOUS, Kind.GROUND, Kind.RAIL)
        and g.key not in resolver.linked_keys
    ]

    found: list[Suggestion] = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            if left.key == right.key:
                continue
            # Joining is only worth proposing when it would bridge designs
            # that are otherwise unconnected on this net.
            left_only = set(left.sources) - set(right.sources)
            right_only = set(right.sources) - set(left.sources)
            if not left_only or not right_only:
                continue
            score, reason = similarity(left.display, right.display)
            if score < MIN_SCORE:
                continue
            found.append(Suggestion(
                left=left.key, right=right.key,
                left_name=left.display, right_name=right.display,
                score=score, reason=reason,
                left_designs=left.designs, right_designs=right.designs,
            ))

    found.sort(key=lambda s: (-s.score, s.left_name, s.right_name))
    return found[:limit]


def apply_links(resolver: NetResolver, links: list[tuple[list[str], str]]) -> list[str]:
    """Apply saved links to a resolver. Returns keys that no design uses."""
    missing: list[str] = []
    for keys, name in links:
        known = [k for k in keys if k in resolver.groups or k in resolver.key_alias]
        missing.extend(k for k in keys if k not in known)
        if len(known) > 1:
            resolver.link(known, name)
    return missing


def parse_link(text: str) -> tuple[list[str], str]:
    """Parse a --link argument: `SDA=I2C_DATA` or `SDA=I2C_DATA:BUS_SDA`."""
    from .nets import normalize

    body, _, target = text.partition(":")
    keys = [normalize(part) for part in body.split("=") if part.strip()]
    if len(keys) < 2:
        raise ValueError(f"link needs at least two net names: {text!r}")
    return keys, target.strip()
