"""The merge plan: every decision, written down and replayable.

Answering the same net questions on every run would be miserable, so the tool
separates deciding from doing.  A plan is JSON: which designs go in, what
prefix each gets, and what happens to every contested net.  Edit it, commit it,
feed it back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .eagle import sanitize_name
from .nets import Action, Kind, NetResolver

PLAN_VERSION = 1


@dataclass
class DesignSpec:
    """One input design: a schematic and, usually, its board."""

    name: str
    prefix: str
    sch: str
    brd: str | None = None

    @property
    def sch_path(self) -> Path:
        return Path(self.sch)

    @property
    def brd_path(self) -> Path | None:
        return Path(self.brd) if self.brd else None


@dataclass
class NetDecision:
    """What to do with one normalized net name."""

    key: str
    name: str
    action: str
    kind: str
    designs: list[str] = field(default_factory=list)
    spellings: list[str] = field(default_factory=list)
    decided_by: str = "auto"
    note: str = ""


@dataclass
class MergePlan:
    output: str = "merged"
    title: str = "merged"
    designs: list[DesignSpec] = field(default_factory=list)
    nets: list[NetDecision] = field(default_factory=list)
    layout: str = "grid"
    gap: float = 5.0
    columns: int = 0
    version: int = PLAN_VERSION

    # -- serialisation ------------------------------------------------------
    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "MergePlan":
        data = json.loads(text)
        designs = [DesignSpec(**d) for d in data.get("designs", [])]
        nets = [NetDecision(**n) for n in data.get("nets", [])]
        plan = cls(
            output=data.get("output", "merged"),
            title=data.get("title", "merged"),
            designs=designs,
            nets=nets,
            layout=data.get("layout", "grid"),
            gap=float(data.get("gap", 5.0)),
            columns=int(data.get("columns", 0)),
            version=int(data.get("version", PLAN_VERSION)),
        )
        return plan

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "MergePlan":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    # -- lookup -------------------------------------------------------------
    def decision_for(self, key: str) -> NetDecision | None:
        for net in self.nets:
            if net.key == key:
                return net
        return None


def design_name(path: Path) -> str:
    """A short, readable identity for a design, taken from its filename."""
    return sanitize_name(path.stem)


def default_prefix(name: str, taken: set[str]) -> str:
    """Build a short reference-designator prefix like `ESP32S3_`.

    Prefers the initials of a multi-word name, falls back to the leading
    alphanumerics, and always ends up unique across the merge.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    # Drop a leading vendor word shared by everything (Adafruit, Sparkfun...).
    if len(words) > 1 and words[0].lower() in {"adafruit", "sparkfun", "seeed", "pimoroni"}:
        words = words[1:]
    candidates = []
    if words:
        joined = "".join(w[:4].upper() for w in words[:2])
        candidates.append(joined)
        candidates.append("".join(w[0].upper() for w in words if w)[:6])
        candidates.append(words[0].upper()[:8])
    candidates.append(sanitize_name(name).upper()[:8])
    for base in candidates:
        base = sanitize_name(base).strip("_")
        if base and f"{base}_" not in taken:
            return f"{base}_"
    base = sanitize_name(candidates[0] or "D").strip("_") or "D"
    n = 2
    while f"{base}{n}_" in taken:
        n += 1
    return f"{base}{n}_"


def plan_from_resolver(
    resolver: NetResolver,
    designs: list[DesignSpec],
    output: str,
    title: str,
    layout: str = "grid",
    gap: float = 5.0,
    columns: int = 0,
) -> MergePlan:
    """Snapshot the resolver's current decisions as a plan."""
    nets: list[NetDecision] = []
    for group in resolver.all_groups():
        if group.kind is Kind.UNIQUE:
            continue  # nothing to decide, nothing to record
        nets.append(
            NetDecision(
                key=group.key,
                name=group.merged_name or group.display,
                action=group.action.value,
                kind=group.kind.value,
                designs=group.designs,
                spellings=group.spellings,
                decided_by=group.decided_by,
                note=_note_for(group),
            )
        )
    return MergePlan(
        output=output, title=title, designs=designs, nets=nets,
        layout=layout, gap=gap, columns=columns,
    )


def _note_for(group) -> str:
    if group.kind is Kind.GROUND:
        return "ground family, joined automatically"
    if group.kind is Kind.RAIL:
        return "voltage stated in the name, joined automatically"
    if group.kind is Kind.ANONYMOUS:
        return "auto-generated name, always kept separate"
    if group.kind is Kind.AMBIGUOUS:
        return "role-named rail; voltage differs between designs unless you say otherwise"
    return "same name in several designs; join only if they are one node"


def apply_plan(resolver: NetResolver, plan: MergePlan) -> list[str]:
    """Push a plan's decisions into a resolver. Returns keys not found."""
    missing: list[str] = []
    for decision in plan.nets:
        group = resolver.groups.get(decision.key)
        if group is None:
            missing.append(decision.key)
            continue
        group.action = Action(decision.action)
        group.decided_by = "plan"
        if group.action is Action.JOIN:
            group.merged_name = decision.name or group.display
    return missing
