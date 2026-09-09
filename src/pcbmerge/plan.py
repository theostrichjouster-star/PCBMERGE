"""The merge plan: every decision, written down and replayable.

Answering the same net questions on every run would be miserable, so the tool
separates deciding from doing.  A plan is JSON: which designs go in, how many
copies of each, what prefix they get, which nets join, and which differently
named nets were tied together by hand.  Edit it, commit it, feed it back.

Plans written before version 4 may name a sheet layout.  That option is gone and
is ignored on load; everything goes on one sheet now.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .eagle import sanitize_name
from .layout import DEFAULT_OUTLINE
from .nets import Action, Kind, NetResolver

PLAN_VERSION = 4


@dataclass
class DesignSpec:
    """One input design: a schematic, usually its board, and a copy count."""

    name: str
    prefix: str
    sch: str
    brd: str | None = None
    count: int = 1

    @property
    def sch_path(self) -> Path:
        return Path(self.sch)

    @property
    def brd_path(self) -> Path | None:
        return Path(self.brd) if self.brd else None


@dataclass
class InstanceSpec:
    """One copy of a design, after replication has been expanded."""

    name: str      # unique instance name
    source: str    # the DesignSpec it came from
    prefix: str
    sch: str
    brd: str | None = None
    index: int = 1
    count: int = 1

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
class LinkDecision:
    """Two or more differently named nets tied into one."""

    keys: list[str]
    name: str = ""
    note: str = ""


@dataclass
class ConnectDecision:
    """A wire between named designs' copies of a net.

    Unlike a link, which acts on a name wherever it appears, this names the
    exact instances, so a controller can reach one relay board and not its
    three siblings.
    """

    members: list[list[str]]      # [design, net] pairs
    name: str = ""
    note: str = ""


@dataclass
class MergePlan:
    output: str = "merged"
    title: str = "merged"
    designs: list[DesignSpec] = field(default_factory=list)
    nets: list[NetDecision] = field(default_factory=list)
    links: list[LinkDecision] = field(default_factory=list)
    connections: list[ConnectDecision] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    layout: str = "pack"
    optimize: str = "balanced"
    outline: str = DEFAULT_OUTLINE
    gap: float = 5.0
    columns: int = 0
    version: int = PLAN_VERSION

    # -- serialisation ------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "MergePlan":
        data = json.loads(text)
        return cls(
            output=data.get("output", "merged"),
            title=data.get("title", "merged"),
            designs=[DesignSpec(**d) for d in data.get("designs", [])],
            nets=[NetDecision(**n) for n in data.get("nets", [])],
            links=[LinkDecision(**l) for l in data.get("links", [])],
            connections=[ConnectDecision(**c) for c in data.get("connections", [])],
            drops=list(data.get("drops", [])),
            layout=data.get("layout", "pack"),
            optimize=data.get("optimize", "balanced"),
            outline=data.get("outline", DEFAULT_OUTLINE),
            gap=float(data.get("gap", 5.0)),
            columns=int(data.get("columns", 0)),
            version=int(data.get("version", PLAN_VERSION)),
        )

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

    @property
    def instances(self) -> list[InstanceSpec]:
        return expand(self.designs)


def expand(specs: list[DesignSpec]) -> list[InstanceSpec]:
    """Turn copy counts into individual instances with numbered prefixes.

    The copy number lives in the prefix, so every reference designator and
    every design-local net name increments together and stays consistent
    between the schematic and the board.
    """
    instances: list[InstanceSpec] = []
    for spec in specs:
        count = max(1, int(spec.count or 1))
        base = spec.prefix.rstrip("_")
        for index in range(1, count + 1):
            if count == 1:
                prefix = f"{base}_"
                name = spec.name
            else:
                prefix = f"{base}{index}_"
                name = f"{spec.name} #{index}"
            instances.append(InstanceSpec(
                name=name, source=spec.name, prefix=prefix,
                sch=spec.sch, brd=spec.brd, index=index, count=count,
            ))
    return instances


def design_name(path: Path) -> str:
    """A short, readable identity for a design, taken from its filename.

    The path given is already stripped of its extension, so `.stem` must not be
    used here: it would cut again at the last dot and turn `board_V1.5` into
    `board_V1`.
    """
    return sanitize_name(Path(path).name)


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
    if len(words) == 1:
        # A single word has no initials to take, so keep it nearly whole.
        candidates.append(words[0].upper()[:8])
    elif words:
        candidates.append("".join(w[:4].upper() for w in words[:2]))
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
    drops: list[str] | None = None,
    layout: str = "pack",
    optimize: str = "balanced",
    outline: str = DEFAULT_OUTLINE,
    gap: float = 5.0,
    columns: int = 0,
) -> MergePlan:
    """Snapshot the resolver's current decisions as a plan."""
    nets: list[NetDecision] = []
    for group in resolver.all_groups():
        if group.kind is Kind.UNIQUE:
            continue  # nothing to decide, nothing to record
        nets.append(NetDecision(
            key=group.key,
            name=group.merged_name or group.display,
            action=group.action.value,
            kind=group.kind.value,
            designs=group.designs,
            spellings=group.spellings,
            decided_by=group.decided_by,
            note=_note_for(group),
        ))

    links: list[LinkDecision] = []
    connections: list[ConnectDecision] = []
    for target in sorted(resolver.linked_keys):
        members = sorted({k for k, v in resolver.key_alias.items() if v == target} | {target})
        refs = sorted([design, raw] for (design, raw), key
                      in resolver.ref_alias.items() if key == target)
        if refs:
            connections.append(ConnectDecision(
                members=refs,
                name=resolver.link_names.get(target, target),
                note="specific designs wired together by hand",
            ))
        if len(members) > 1:
            links.append(LinkDecision(
                keys=members,
                name=resolver.link_names.get(target, target),
                note="tied together by hand; no naming rule would match these",
            ))

    return MergePlan(
        output=output, title=title, designs=designs, nets=nets, links=links,
        connections=connections, drops=list(drops or []),
        layout=layout, optimize=optimize, outline=outline, gap=gap, columns=columns,
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
    if group.kind is Kind.REPLICA:
        return f"one net per copy unless it is common to all {group.design_count}"
    return "same name in several designs; join only if they are one node"


def apply_plan(resolver: NetResolver, plan: MergePlan,
               default_action: Action = Action.SPLIT,
               replica_action: Action = Action.SPLIT) -> list[str]:
    """Push a plan's decisions into a resolver. Returns keys not found.

    Links are applied first and force a regroup, because they change which
    nets are in which group before any join or split decision can apply.
    """
    missing: list[str] = []

    if plan.links or plan.connections:
        from .linking import apply_links

        missing.extend(apply_links(resolver, [(l.keys, l.name) for l in plan.links]))
        for connection in plan.connections:
            resolver.connect([(m[0], m[1]) for m in connection.members if len(m) == 2],
                             connection.name)
        resolver.finalize(default_action=default_action, replica_action=replica_action)

    for decision in plan.nets:
        group = resolver.groups.get(decision.key)
        if group is None:
            missing.append(decision.key)
            continue
        group.action = Action(decision.action)
        group.decided_by = decision.decided_by if decision.decided_by != "auto" else "plan"
        if group.action is Action.JOIN:
            group.merged_name = decision.name or group.display
    return missing
