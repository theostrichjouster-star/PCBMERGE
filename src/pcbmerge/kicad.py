"""Reading KiCad designs and presenting them as EAGLE drawings.

The merge engine works on EAGLE documents, so a KiCad design is converted on
the way in rather than handled as a second kind of input everywhere.  What
comes out is an ordinary `EagleDoc` pair that the rest of the tool cannot tell
from a file EAGLE wrote.

The board is the source of truth.  A `.kicad_pcb` carries the whole netlist,
every footprint with its pads and their net assignments, the copper and the
outline, which is everything a merge needs.  The schematic is drawn from that
netlist: one box symbol per part, one pin per pad, net names on labels.  It is
not the drawing the engineer made, and it is not meant to be -- it is a
faithful, openable statement of the same connections.

Two conventions differ and both are handled here.  KiCad measures Y downwards
and EAGLE upwards, so every Y is negated; rotations therefore change sign too.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import kicad_sch, sexp
from .eagle import (
    EagleDoc, EagleError, design_stem, fmt, parse_rot, sanitize_name, unique_name,
    with_ext,
)

SCH_SUFFIX = ".kicad_sch"
PCB_SUFFIX = ".kicad_pcb"
SUFFIXES = (SCH_SUFFIX, PCB_SUFFIX)

# KiCad layer names against EAGLE layer numbers.  Anything not listed is
# carried nowhere: it is documentation that would only clutter a merged board.
LAYERS = {
    "F.Cu": 1, "In1.Cu": 2, "In2.Cu": 3, "In3.Cu": 4, "In4.Cu": 5,
    "In5.Cu": 6, "In6.Cu": 7, "B.Cu": 16,
    "Edge.Cuts": 20,
    "F.SilkS": 21, "B.SilkS": 22,
    "F.Mask": 29, "B.Mask": 30,
    "F.Paste": 31, "B.Paste": 32,
    "F.CrtYd": 39, "B.CrtYd": 40,
    "F.Fab": 51, "B.Fab": 52,
    "Cmts.User": 41, "Dwgs.User": 48,
}
COPPER = {"F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "In5.Cu", "In6.Cu"}

# KiCad invents these for nets nobody named, exactly as EAGLE invents N$1.
AUTO_NET = re.compile(r"^(?:Net-\(.*\)|unconnected-.*)$")

DEFAULT_WIDTH = "0.1524"


class KiCadError(EagleError):
    """Raised when a KiCad file cannot be converted."""


def is_kicad(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUFFIXES


def design_stem(path: str | Path) -> Path:
    """Re-exported so callers converting a design need only this module."""
    from .eagle import design_stem as strip

    return strip(path)


def find_stems(folder: Path) -> list[Path]:
    """KiCad designs in a folder, named by their shared stem."""
    return sorted({design_stem(p) for p in folder.glob(f"*{PCB_SUFFIX}")})


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def _xy(node, name: str = "at") -> tuple[float, float]:
    """A KiCad point, with Y turned the way EAGLE measures it."""
    found = sexp.first(node, name)
    if found is None or len(found) < 3:
        return 0.0, 0.0
    return sexp.as_float(found[1]), -sexp.as_float(found[2])


def _angle(node, name: str = "at") -> float:
    """A KiCad rotation, which is already EAGLE's direction.

    Reasoning said the sign should change with the Y flip.  Counting, on a
    real board, which convention lands a rotated footprint's pads on the
    tracks KiCad drew to them said otherwise: every pad of every footprint
    turned a quarter landed with the angle carried across as it is, and none
    landed with it negated.  KiCad's stored angles are, in effect, turns in
    the y-up frame.  The angle of a pad or a label is stored absolute, so a
    package takes the difference from its footprint's.
    """
    found = sexp.first(node, name)
    if found is None or len(found) < 4:
        return 0.0
    return sexp.as_float(found[3]) % 360.0


def _rot(angle: float, mirrored: bool = False) -> str | None:
    """EAGLE's rotation attribute for a footprint at `angle`.

    A footprint on the back is stored by KiCad as the image seen from the
    top, which is the library footprint mirrored top-to-bottom.  EAGLE puts a
    package on the back by mirroring it left-to-right, and the two mirrors
    differ by a half turn, so a back-side element turns a further 180.  The
    package itself is built from the library image (see `_unmirror`).
    """
    if mirrored:
        return f"MR{fmt((angle + 180.0) % 360.0)}"
    return f"R{fmt(angle)}" if angle else None


def _arc_curve(start: tuple[float, float], mid: tuple[float, float],
               end: tuple[float, float]) -> float:
    """The included angle of the arc through three points, in degrees.

    EAGLE draws an arc as a wire with a `curve`, so the three points KiCad
    stores have to become one angle.  Positive is counterclockwise.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    area = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
    if abs(area) < 1e-12:
        return 0.0                      # three points in a line is a wire
    a = math.dist(start, mid)
    b = math.dist(mid, end)
    c = math.dist(start, end)
    if a * b == 0:
        return 0.0
    cosine = max(-1.0, min(1.0, (a * a + b * b - c * c) / (2 * a * b)))
    # The inscribed angle at the middle point subtends the rest of the circle,
    # so the arc from start to end sweeps twice its supplement.
    swept = 2.0 * (180.0 - math.degrees(math.acos(cosine)))
    return swept if area > 0 else -swept


# --------------------------------------------------------------------------
# the board
# --------------------------------------------------------------------------

@dataclass
class Converted:
    """One KiCad design, expressed as an EAGLE pair."""

    name: str
    schematic: EagleDoc
    board: EagleDoc
    notes: list[str] = field(default_factory=list)


@dataclass
class _Part:
    """A footprint instance, on its way to becoming an element and a part."""

    ref: str
    value: str
    library: str
    package: str
    x: float
    y: float
    angle: float
    mirrored: bool
    pads: list[tuple[str, str]] = field(default_factory=list)   # (pad, net)
    labels: dict[str, kicad_sch.Label] = field(default_factory=dict)


def convert(stem: Path) -> Converted:
    """Read a KiCad design and hand back an EAGLE schematic and board."""
    stem = Path(stem)
    pcb = with_ext(stem, PCB_SUFFIX)
    if not pcb.exists():
        raise KiCadError(f"{pcb.name}: not found; a KiCad design needs its board")

    try:
        tree = sexp.parse(pcb.read_text(encoding="utf-8", errors="replace"))
    except sexp.SexpError as exc:
        raise KiCadError(f"{pcb.name}: {exc}") from exc
    if sexp.tag(tree) != "kicad_pcb":
        raise KiCadError(f"{pcb.name}: not a KiCad board")

    name = sanitize_name(stem.name)
    notes: list[str] = []
    nets = _net_names(tree)
    packages, parts = _footprints(tree, name, nets, notes)

    board = _build_board(tree, name, packages, parts, nets, notes)
    schematic = _drawn_schematic(stem, name, packages, parts, notes)
    if schematic is None:
        schematic = _build_schematic(name, packages, parts, notes)
    return Converted(name=name, schematic=schematic, board=board, notes=notes)


def _drawn_schematic(stem: Path, design: str, packages: dict[str, ET.Element],
                     parts: list[_Part], notes: list[str]) -> EagleDoc | None:
    """The schematic as it was drawn, when the drawing is there to read.

    Returns None when there is nothing worth drawing, which leaves the caller
    to fall back to the netlist.  The commonest reason is a root sheet whose
    real content lives in child files that were not published with it.
    """
    source = with_ext(stem, SCH_SUFFIX)
    if not source.exists():
        notes.append(f"{stem.name}: no {SCH_SUFFIX} beside the board, so the "
                     f"schematic is drawn from the board netlist")
        return None

    try:
        drawing = kicad_sch.read(source)
    except (OSError, ValueError) as exc:
        notes.append(f"{source.name}: could not be read ({exc}); the schematic is "
                     f"drawn from the board netlist instead")
        return None

    if not drawing.usable:
        _note_nothing_drawn(source, drawing, notes)
        return None

    pads = {name: _pads_of(node) for name, node in packages.items()}
    package_of = {part.ref: part.package for part in parts}
    board_nets = {(part.ref, pad): net for part in parts for pad, net in part.pads}
    built = kicad_sch.build(drawing, pads, package_of, board_nets)
    boxed = _box_the_undrawn(parts, pads, built)

    doc = _assemble_schematic(design, packages, built)
    _note_drawn(source, drawing, built, boxed, notes)
    return doc


def _box_the_undrawn(parts: list[_Part], pad_names: dict[str, list[str]],
                     built) -> list[str]:
    """Give every footprint the drawing left out a box on the sheet.

    A root sheet whose child sheets were not published draws some of the
    board and none of the rest.  Writing only what was drawn leaves the
    board full of elements the schematic has never heard of: the pair opens,
    but nothing about those parts is decided on the schematic, and the
    catalogue reads them as decoration because no pin reaches them.  So each
    footprint with pads that has no drawn symbol gets the same box the
    netlist route draws, placed beside the drawing, wired to its nets by
    label.  A footprint with no pads at all is silkscreen, and stays where
    silkscreen belongs: on the board only.
    """
    drawn = {row["name"] for row in built.parts}
    undrawn = [p for p in parts if p.ref not in drawn and pad_names.get(p.package)]
    if not undrawn:
        return []

    taken = set(built.symbols) | set(built.devicesets)
    boxes: dict[str, str] = {}          # package -> the box symbol drawn for it
    for part in undrawn:
        if part.package in boxes:
            continue
        name = unique_name(part.package, taken)
        taken.add(name)
        boxes[part.package] = name
        built.symbols[name] = _symbol(name, pad_names[part.package])
        built.devicesets[name] = _deviceset(name, pad_names[part.package], part.package)
    for part in undrawn:
        built.parts.append({"name": part.ref, "deviceset": boxes[part.package],
                            "device": "", "value": part.value})

    # To the right of whatever was drawn, from its top edge down.
    right, top = _drawn_extent(built)
    holder = ET.Element("instances")
    placed = _place_instances(holder, undrawn, pad_names, origin=(right, top))
    built.instances.extend(list(holder))

    # Into the nets the drawing already has, where the names meet: one net
    # element per name per sheet is the only form EAGLE accepts.
    nets = {net.get("name", ""): net for net in built.nets}
    _wire_nets(nets, undrawn, pad_names, placed)
    built.nets = list(nets.values())
    built.unplaced = [name for name in built.unplaced if name not in nets]
    return [part.ref for part in undrawn]


def _drawn_extent(built) -> tuple[float, float]:
    """Where the drawing ends on the right, and where it starts at the top."""
    xs: list[float] = []
    ys: list[float] = []
    for node in [*built.instances, *built.nets, *built.plain]:
        for item in node.iter():
            for xa, ya in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
                if item.get(xa) is not None and item.get(ya) is not None:
                    try:
                        xs.append(float(item.get(xa)))
                        ys.append(float(item.get(ya)))
                    except ValueError:
                        continue
    if not xs:
        return 0.0, 0.0
    return max(xs) + COLUMN_WIDTH, max(ys)


def _note_nothing_drawn(source: Path, drawing, notes: list[str]) -> None:
    if drawing.missing:
        missing = ", ".join(sorted(set(drawing.missing))[:4])
        notes.append(
            f"{source.name}: the drawing is in child sheets that are not here "
            f"({missing}); the schematic is drawn from the board netlist instead")
    else:
        notes.append(
            f"{source.name}: holds no wired-up drawing, so the schematic is drawn "
            f"from the board netlist")


def _note_drawn(source: Path, drawing, built, boxed: list[str],
                notes: list[str]) -> None:
    notes.append(
        f"{source.name}: schematic converted as drawn -- "
        f"{len(built.instances) - len(boxed)} symbols, {len(drawing.wires)} wires, "
        f"{len(built.nets)} nets")
    if drawing.missing:
        missing = ", ".join(sorted(set(drawing.missing))[:4])
        notes.append(f"{source.name}: child sheet(s) not found and left out "
                     f"({missing})")
    if boxed:
        notes.append(
            f"{source.name}: {len(boxed)} footprint(s) the drawing does not show "
            f"were added beside it as boxes, one pin per pad")
    if built.unplaced:
        notes.append(
            f"{source.name}: {len(built.unplaced)} board net(s) are not drawn on "
            f"the schematic and were added as labelled stubs")


def _assemble_schematic(design: str, packages: dict[str, ET.Element],
                        built) -> EagleDoc:
    """Wrap the converted drawing in the document EAGLE expects."""
    root, schematic = _shell("schematic", design, _SCH_LAYERS,
                             packages, built.symbols, built.devicesets)
    schematic.append(_libraries(design, packages, built.symbols, built.devicesets))
    for tag_name in ("attributes", "variantdefs"):
        node = ET.SubElement(schematic, tag_name)
        node.tail = "\n"
    schematic.append(_classes())

    parts_node = ET.SubElement(schematic, "parts")
    parts_node.text = "\n"
    parts_node.tail = "\n"
    for row in built.parts:
        node = ET.SubElement(parts_node, "part", {
            "name": row["name"], "library": design,
            "deviceset": row["deviceset"], "device": row["device"]})
        if row["value"]:
            node.set("value", row["value"])
        node.tail = "\n"

    sheets = ET.SubElement(schematic, "sheets")
    sheets.text = "\n"
    sheets.tail = "\n"
    sheet = ET.SubElement(sheets, "sheet")
    sheet.text = "\n"
    sheet.tail = "\n"
    for tag_name in ("plain", "instances", "busses", "nets"):
        holder = ET.SubElement(sheet, tag_name)
        holder.text = "\n"
        holder.tail = "\n"

    for node in built.plain:
        sheet.find("plain").append(node)
    for node in built.instances:
        sheet.find("instances").append(node)
    for node in built.nets:
        sheet.find("nets").append(node)
    _stub_nets(sheet.find("nets"), built.unplaced)

    return EagleDoc(path=Path(f"{design}.sch"),
                    tree=ET.ElementTree(root), kind="sch")


def _stub_nets(holder: ET.Element, names: list[str]) -> None:
    """Give a board net the drawing never reached somewhere to exist.

    EAGLE will not open a pair whose board carries a signal the schematic has
    never heard of, so each one gets a short labelled wire off to the side.
    They are the copper the drawing does not account for, and showing them is
    better than a file that will not open.
    """
    for index, name in enumerate(names):
        y = -index * 5.08
        net = ET.SubElement(holder, "net", {"name": name, "class": "0"})
        net.text = "\n"
        net.tail = "\n"
        segment = ET.SubElement(net, "segment")
        segment.text = "\n"
        segment.tail = "\n"
        wire = ET.SubElement(segment, "wire", {
            "x1": "-50.8", "y1": f"{y:.4g}", "x2": "-38.1", "y2": f"{y:.4g}",
            "width": "0.1524", "layer": "91"})
        wire.tail = "\n"
        label = ET.SubElement(segment, "label", {
            "x": "-50.8", "y": f"{y:.4g}", "size": "1.778", "layer": "95"})
        label.tail = "\n"


def _net_names(tree) -> dict[str, str]:
    """KiCad net numbers to the names EAGLE should use.

    A hierarchical name like `/Sheet/VCC_3V3` becomes `VCC_3V3`, so the rails
    line up with the ones EAGLE designs use.  Names KiCad invented become the
    `N$1` form the resolver already knows to keep apart.
    """
    out: dict[str, str] = {}
    taken: set[str] = set()
    auto = 0
    for node in sexp.children(tree, "net"):
        if len(node) < 3:
            continue
        number, raw = str(node[1]), str(node[2])
        if not raw:
            continue
        if AUTO_NET.match(raw):
            auto += 1
            name = f"N${auto}"
        else:
            name = sanitize_name(raw.rsplit("/", 1)[-1]) or f"N${number}"
        name = unique_name(name, taken)
        taken.add(name)
        out[number] = name
    return out


def _footprints(tree, design: str, nets: dict[str, str],
                notes: list[str]) -> tuple[dict[str, ET.Element], list[_Part]]:
    """Turn every footprint into one shared package and one placed part."""
    packages: dict[str, ET.Element] = {}
    parts: list[_Part] = []
    used_refs: set[str] = set()
    anonymous = 0

    for node in sexp.children(tree, "footprint"):
        identifier = node[1] if len(node) > 1 and isinstance(node[1], str) else "?"
        _, _, bare = identifier.partition(":")
        package = sanitize_name(bare or identifier)
        if not package:
            continue

        ref = _property(node, "Reference") or ""
        if not ref:
            anonymous += 1
            ref = f"U${anonymous}"
        ref = unique_name(sanitize_name(ref), used_refs)
        used_refs.add(ref)

        layer = sexp.value(node, "layer", default="F.Cu")
        mirrored = str(layer).startswith("B.")
        x, y = _xy(node)
        angle = _angle(node)

        if package not in packages:
            packages[package] = _package(node, package, angle, mirrored)

        pads: list[tuple[str, str]] = []
        for pad in sexp.children(node, "pad"):
            pad_name = str(pad[1]) if len(pad) > 1 else ""
            net = sexp.first(pad, "net")
            if not pad_name or net is None or len(net) < 2:
                continue
            merged = nets.get(str(net[1]))
            if merged:
                pads.append((pad_name, merged))

        parts.append(_Part(
            ref=ref, value=_property(node, "Value") or "",
            library=design, package=package,
            x=x, y=y, angle=angle, mirrored=mirrored, pads=pads,
            labels=kicad_sch.read_labels(node, flip_y=True),
        ))

    if anonymous:
        notes.append(f"{anonymous} footprint(s) had no reference and were named U$n")
    return packages, parts


# KiCad 8 moved a footprint's reference and value into `property`; before that
# they were `fp_text` with a role.  Boards in the wild are both, and reading
# only the newer form leaves every part on an older board unnamed.
FP_TEXT_ROLES = {"Reference": "reference", "Value": "value"}


def _property(node, name: str) -> str:
    for prop in sexp.children(node, "property"):
        if len(prop) > 2 and prop[1] == name:
            return str(prop[2])
    role = FP_TEXT_ROLES.get(name)
    if role:
        for text in sexp.children(node, "fp_text"):
            if len(text) > 2 and text[1] == role:
                return str(text[2])
    return ""


# --------------------------------------------------------------------------
# packages
# --------------------------------------------------------------------------

def _package(node, name: str, angle: float = 0.0, back: bool = False) -> ET.Element:
    """One footprint's geometry, as an EAGLE package.

    Built from whichever placement of the footprint comes first.  Its pads
    and labels carry absolute angles, so the footprint's own is taken off;
    and if that placement is on the back, KiCad has stored the mirror image,
    which is turned back into the library footprint so a front placement of
    the same package is right too.
    """
    package = ET.Element("package", {"name": name})
    package.text = "\n"
    package.tail = "\n"

    for pad in sexp.children(node, "pad"):
        shape = _pad(pad, angle)
        if shape is not None:
            package.append(shape)
    for line in sexp.children(node, "fp_line"):
        wire = _line(line)
        if wire is not None:
            package.append(wire)
    for arc in sexp.children(node, "fp_arc"):
        wire = _arc(arc)
        if wire is not None:
            package.append(wire)
    for circle in sexp.children(node, "fp_circle"):
        shape = _circle(circle)
        if shape is not None:
            package.append(shape)
    for poly in sexp.children(node, "fp_poly"):
        shape = _polygon(poly)
        if shape is not None:
            package.append(shape)

    if not len(package):
        # A package EAGLE can place, even when nothing was convertible.
        marker = ET.SubElement(package, "wire", {
            "x1": "0", "y1": "0", "x2": "0", "y2": "0",
            "width": DEFAULT_WIDTH, "layer": "21"})
        marker.tail = "\n"

    # The name and value where the footprint draws them, relative to it.
    for kind, layer in (("NAME", "25"), ("VALUE", "27")):
        label = kicad_sch.read_labels(node, flip_y=True).get(kind)
        if label is None:
            continue
        relative = (label.angle - angle) % 360.0
        rot = f"R{fmt(relative)}" if relative else ""
        text = kicad_sch.label_text(label, label.x, label.y, rot, layer)
        text.tail = "\n"
        package.append(text)

    if back:
        _unmirror(package)
    return package


def _unmirror(package: ET.Element) -> None:
    """Turn the image KiCad stores for a back-side footprint into the library one.

    KiCad flips a footprint top-to-bottom, so what it stores for one on the
    back is the library footprint with y negated.  Undoing that negates every
    y, every turn and every arc, and leaves the package as the library drew
    it, which is what EAGLE mirrors for itself.
    """
    for node in package.iter():
        if not isinstance(node.tag, str):
            continue
        for key in ("y", "y1", "y2"):
            value = node.get(key)
            if value is not None:
                node.set(key, fmt(-float(value)))
        curve = node.get("curve")
        if curve is not None:
            node.set("curve", fmt(-float(curve)))
        rot = node.get("rot")
        if rot is not None:
            flags, degrees = parse_rot(rot)
            node.set("rot", f"{flags}R{fmt((-degrees) % 360.0)}")


def _pad(pad, base: float = 0.0) -> ET.Element | None:
    name = str(pad[1]) if len(pad) > 1 else ""
    if not name:
        return None
    kind = str(pad[2]) if len(pad) > 2 else "smd"
    shape = str(pad[3]) if len(pad) > 3 else "rect"
    x, y = _xy(pad)
    size = sexp.first(pad, "size")
    width = sexp.as_float(size[1]) if size and len(size) > 1 else 0.5
    height = sexp.as_float(size[2]) if size and len(size) > 2 else 0.5
    # A pad's angle is stored absolute, so a package keeps only what the
    # pad adds to its footprint's own turn.
    angle = (_angle(pad) - base) % 360.0

    if kind == "smd":
        node = ET.Element("smd", {
            "name": name, "x": fmt(x), "y": fmt(y),
            "dx": fmt(width), "dy": fmt(height),
            "layer": "16" if "B.Cu" in _pad_layers(pad) else "1",
        })
        if shape in ("roundrect", "oval", "circle"):
            node.set("roundness", "50" if shape != "roundrect" else "25")
    else:
        drill = sexp.first(pad, "drill")
        hole = sexp.as_float(drill[1]) if drill and len(drill) > 1 else 0.3
        node = ET.Element("pad", {
            "name": name, "x": fmt(x), "y": fmt(y),
            "drill": fmt(hole), "diameter": fmt(max(width, height)),
        })
        if shape in ("rect", "roundrect"):
            node.set("shape", "square")
        elif shape == "oval":
            node.set("shape", "long")
    if angle:
        node.set("rot", f"R{fmt(angle)}")
    node.tail = "\n"
    return node


def _pad_layers(pad) -> list[str]:
    found = sexp.first(pad, "layers")
    return [str(item) for item in found[1:]] if found else []


def _layer_of(node) -> int | None:
    raw = sexp.value(node, "layer")
    return LAYERS.get(str(raw)) if raw is not None else None


def _stroke_width(node) -> str:
    stroke = sexp.first(node, "stroke")
    if stroke is not None:
        width = sexp.number(stroke, "width", default=0.0)
        if width > 0:
            return fmt(width)
    width = sexp.number(node, "width", default=0.0)
    return fmt(width) if width > 0 else DEFAULT_WIDTH


def _line(node) -> ET.Element | None:
    layer = _layer_of(node)
    if layer is None:
        return None
    x1, y1 = _xy(node, "start")
    x2, y2 = _xy(node, "end")
    wire = ET.Element("wire", {
        "x1": fmt(x1), "y1": fmt(y1), "x2": fmt(x2), "y2": fmt(y2),
        "width": _stroke_width(node), "layer": str(layer)})
    wire.tail = "\n"
    return wire


def _arc(node) -> ET.Element | None:
    layer = _layer_of(node)
    if layer is None:
        return None
    start, mid, end = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
    wire = ET.Element("wire", {
        "x1": fmt(start[0]), "y1": fmt(start[1]),
        "x2": fmt(end[0]), "y2": fmt(end[1]),
        "width": _stroke_width(node), "layer": str(layer)})
    curve = _arc_curve(start, mid, end)
    if abs(curve) > 0.01:
        wire.set("curve", fmt(curve))
    wire.tail = "\n"
    return wire


def _circle(node) -> ET.Element | None:
    layer = _layer_of(node)
    if layer is None:
        return None
    cx, cy = _xy(node, "center")
    ex, ey = _xy(node, "end")
    node_out = ET.Element("circle", {
        "x": fmt(cx), "y": fmt(cy),
        "radius": fmt(math.dist((cx, cy), (ex, ey))),
        "width": _stroke_width(node), "layer": str(layer)})
    node_out.tail = "\n"
    return node_out


def _polygon(node, layer: int | None = None, width: str | None = None) -> ET.Element | None:
    layer = layer if layer is not None else _layer_of(node)
    if layer is None:
        return None
    points = sexp.first(node, "pts")
    if points is None:
        return None
    poly = ET.Element("polygon", {
        "width": width or _stroke_width(node), "layer": str(layer)})
    poly.text = "\n"
    poly.tail = "\n"
    for item in sexp.children(points, "xy"):
        if len(item) < 3:
            continue
        vertex = ET.SubElement(poly, "vertex", {
            "x": fmt(sexp.as_float(item[1])), "y": fmt(-sexp.as_float(item[2]))})
        vertex.tail = "\n"
    return poly if len(poly) >= 3 else None


# --------------------------------------------------------------------------
# board document
# --------------------------------------------------------------------------

def _drawing(version: str = "9.6.2") -> tuple[ET.Element, ET.Element]:
    root = ET.Element("eagle", {"version": version})
    root.text = "\n"
    drawing = ET.SubElement(root, "drawing")
    drawing.text = "\n"
    settings = ET.SubElement(drawing, "settings")
    settings.tail = "\n"
    grid = ET.SubElement(drawing, "grid", {
        "distance": "0.05", "unitdist": "inch", "unit": "inch",
        "style": "lines", "multiple": "1", "display": "no",
        "altdistance": "0.01", "altunitdist": "inch", "altunit": "inch"})
    grid.tail = "\n"
    return root, drawing


# EAGLE's own layer table, trimmed to what a converted design uses.
_BOARD_LAYERS = [
    (1, "Top", 4, 1, "yes"), (2, "Route2", 1, 3, "no"), (3, "Route3", 4, 3, "no"),
    (4, "Route4", 1, 4, "no"), (5, "Route5", 4, 4, "no"), (6, "Route6", 1, 8, "no"),
    (7, "Route7", 4, 8, "no"), (16, "Bottom", 1, 1, "yes"),
    (17, "Pads", 2, 1, "yes"), (18, "Vias", 2, 1, "yes"), (19, "Unrouted", 6, 1, "yes"),
    (20, "Dimension", 15, 1, "yes"), (21, "tPlace", 7, 1, "yes"),
    (22, "bPlace", 7, 1, "yes"), (23, "tOrigins", 15, 1, "yes"),
    (24, "bOrigins", 15, 1, "yes"), (25, "tNames", 7, 1, "yes"),
    (26, "bNames", 7, 1, "yes"), (27, "tValues", 7, 1, "no"),
    (28, "bValues", 7, 1, "no"), (29, "tStop", 7, 3, "no"), (30, "bStop", 7, 6, "no"),
    (31, "tCream", 7, 4, "no"), (32, "bCream", 7, 5, "no"),
    (39, "tKeepout", 4, 11, "yes"), (40, "bKeepout", 1, 11, "yes"),
    (41, "tRestrict", 4, 10, "yes"), (42, "bRestrict", 1, 10, "yes"),
    (43, "vRestrict", 6, 10, "yes"), (44, "Drills", 7, 1, "no"),
    (45, "Holes", 7, 1, "no"), (46, "Milling", 3, 1, "no"), (48, "Document", 7, 1, "yes"),
    (49, "Reference", 7, 1, "yes"), (51, "tDocu", 7, 1, "yes"), (52, "bDocu", 7, 1, "yes"),
]
_SCH_LAYERS = [
    (91, "Nets", 2, 1, "yes"), (92, "Busses", 1, 1, "yes"), (93, "Pins", 2, 1, "no"),
    (94, "Symbols", 4, 1, "yes"), (95, "Names", 7, 1, "yes"),
    (96, "Values", 7, 1, "yes"), (97, "Info", 7, 1, "yes"), (98, "Guide", 6, 1, "yes"),
]


def _layers(rows) -> ET.Element:
    node = ET.Element("layers")
    node.text = "\n"
    for number, name, colour, fill, visible in rows:
        layer = ET.SubElement(node, "layer", {
            "number": str(number), "name": name, "color": str(colour),
            "fill": str(fill), "visible": visible, "active": "yes"})
        layer.tail = "\n"
    node.tail = "\n"
    return node


def _libraries(design: str, packages: dict[str, ET.Element],
               symbols: dict[str, ET.Element] | None = None,
               devicesets: dict[str, ET.Element] | None = None) -> ET.Element:
    node = ET.Element("libraries")
    node.text = "\n"
    library = ET.SubElement(node, "library", {"name": design})
    library.text = "\n"
    library.tail = "\n"

    holder = ET.SubElement(library, "packages")
    holder.text = "\n"
    holder.tail = "\n"
    for name in sorted(packages):
        holder.append(packages[name])
    if symbols is not None:
        holder = ET.SubElement(library, "symbols")
        holder.text = "\n"
        holder.tail = "\n"
        for name in sorted(symbols):
            holder.append(symbols[name])
    if devicesets is not None:
        holder = ET.SubElement(library, "devicesets")
        holder.text = "\n"
        holder.tail = "\n"
        for name in sorted(devicesets):
            holder.append(devicesets[name])
    node.tail = "\n"
    return node


def _shell(section_tag: str, design: str, layers, packages, symbols=None,
           devicesets=None) -> tuple[ET.Element, ET.Element]:
    root, drawing = _drawing()
    drawing.append(_layers(layers))
    section = ET.SubElement(drawing, section_tag)
    section.text = "\n"
    if section_tag == "schematic":
        section.set("xreflabel", "%F%N/%S.%C%R")
        section.set("xrefpart", "/%S.%C%R")
    return root, section


def _classes() -> ET.Element:
    node = ET.Element("classes")
    node.text = "\n"
    default = ET.SubElement(node, "class", {
        "number": "0", "name": "default", "width": "0", "drill": "0"})
    default.tail = "\n"
    node.tail = "\n"
    return node


def _build_board(tree, design: str, packages: dict[str, ET.Element],
                 parts: list[_Part], nets: dict[str, str],
                 notes: list[str]) -> EagleDoc:
    root, board = _shell("board", design, _BOARD_LAYERS, packages)

    plain = ET.SubElement(board, "plain")
    plain.text = "\n"
    plain.tail = "\n"
    for tag_name, builder in (("gr_line", _line), ("gr_arc", _arc),
                              ("gr_circle", _circle)):
        for node in sexp.children(tree, tag_name):
            shape = builder(node)
            if shape is not None and shape.get("layer") == "20":
                plain.append(shape)
    for node in sexp.children(tree, "gr_rect"):
        rect = _rect_wires(node)
        for wire in rect:
            plain.append(wire)

    board.append(_libraries(design, packages))
    attributes = ET.SubElement(board, "attributes")
    attributes.tail = "\n"
    variants = ET.SubElement(board, "variantdefs")
    variants.tail = "\n"
    board.append(_classes())
    rules = ET.SubElement(board, "designrules", {"name": "default"})
    rules.tail = "\n"
    router = ET.SubElement(board, "autorouter")
    router.tail = "\n"

    elements = ET.SubElement(board, "elements")
    elements.text = "\n"
    elements.tail = "\n"
    for part in parts:
        element = ET.SubElement(elements, "element", {
            "name": part.ref, "library": design, "package": part.package,
            "value": part.value, "x": fmt(part.x), "y": fmt(part.y),
            "smashed": "yes" if part.labels else "no"})
        rotation = _rot(part.angle, part.mirrored)
        if rotation:
            element.set("rot", rotation)
        element.tail = "\n"
        _label_attributes(element, part)

    signals = ET.SubElement(board, "signals")
    signals.text = "\n"
    signals.tail = "\n"
    holders: dict[str, ET.Element] = {}
    for name in dict.fromkeys(nets.values()):
        signal = ET.SubElement(signals, "signal", {"name": name})
        signal.text = "\n"
        signal.tail = "\n"
        holders[name] = signal

    for part in parts:
        for pad, net in part.pads:
            signal = holders.get(net)
            if signal is None:
                continue
            contact = ET.SubElement(signal, "contactref",
                                    {"element": part.ref, "pad": pad})
            contact.tail = "\n"

    _copper(tree, nets, holders, notes)

    for name, signal in list(holders.items()):
        if not len(signal):
            signals.remove(signal)      # a net nothing is on is not a net

    doc = EagleDoc(path=Path(f"{design}.brd"), tree=ET.ElementTree(root), kind="brd")
    return doc


def _label_attributes(element: ET.Element, part: _Part) -> None:
    """The part's own label positions, as a smashed element carries them.

    KiCad places every footprint's labels individually; EAGLE's word for
    that is "smashed", with an attribute per label in board coordinates.
    The label's position is relative to the footprint as stored, and what
    is stored for a footprint on the back is already the image seen from
    the top, so for either side the board position is the footprint's plus
    the offset turned by the footprint's angle.
    """
    if not part.labels:
        return
    element.text = "\n"
    turn = math.radians(part.angle)
    for kind, (front, back) in (("NAME", ("25", "26")), ("VALUE", ("27", "28"))):
        label = part.labels.get(kind)
        if label is None:
            continue
        x = part.x + label.x * math.cos(turn) - label.y * math.sin(turn)
        y = part.y + label.x * math.sin(turn) + label.y * math.cos(turn)
        prefix = "M" if part.mirrored else ""
        rot = f"{prefix}R{fmt(label.angle)}" if (label.angle or prefix) else ""
        attribute = kicad_sch.label_attribute(
            label, x, y, rot, back if part.mirrored else front)
        attribute.tail = "\n"
        element.append(attribute)


def _rect_wires(node) -> list[ET.Element]:
    layer = _layer_of(node)
    if layer != 20:
        return []
    x1, y1 = _xy(node, "start")
    x2, y2 = _xy(node, "end")
    width = _stroke_width(node)
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    out = []
    for (ax, ay), (bx, by) in zip(corners, corners[1:]):
        wire = ET.Element("wire", {
            "x1": fmt(ax), "y1": fmt(ay), "x2": fmt(bx), "y2": fmt(by),
            "width": width, "layer": "20"})
        wire.tail = "\n"
        out.append(wire)
    return out


def _copper(tree, nets: dict[str, str], holders: dict[str, ET.Element],
            notes: list[str]) -> None:
    """Tracks, vias and pours, filed under the signals they belong to."""
    dropped = 0
    for node in sexp.children(tree, "segment"):
        name = nets.get(str(sexp.value(node, "net", default="")))
        layer = _layer_of(node)
        if name is None or layer is None:
            dropped += 1
            continue
        x1, y1 = _xy(node, "start")
        x2, y2 = _xy(node, "end")
        wire = ET.SubElement(holders[name], "wire", {
            "x1": fmt(x1), "y1": fmt(y1), "x2": fmt(x2), "y2": fmt(y2),
            "width": fmt(sexp.number(node, "width", default=0.2)),
            "layer": str(layer)})
        wire.tail = "\n"

    for node in sexp.children(tree, "via"):
        name = nets.get(str(sexp.value(node, "net", default="")))
        if name is None:
            continue
        x, y = _xy(node)
        via = ET.SubElement(holders[name], "via", {
            "x": fmt(x), "y": fmt(y),
            "extent": "1-16",
            "drill": fmt(sexp.number(node, "drill", default=0.3)),
            "diameter": fmt(sexp.number(node, "size", default=0.6))})
        via.tail = "\n"

    pours = 0
    for node in sexp.children(tree, "zone"):
        name = nets.get(str(sexp.value(node, "net", default="")))
        if name is None:
            continue
        layers = sexp.first(node, "layers") or sexp.first(node, "layer")
        target = None
        for raw in (layers[1:] if layers else []):
            if str(raw) in COPPER:
                target = LAYERS[str(raw)]
                break
        if target is None:
            continue
        outline = sexp.first(node, "polygon")
        poly = _polygon(outline, layer=target, width="0.2") if outline else None
        if poly is not None:
            holders[name].append(poly)
            pours += 1

    if dropped:
        notes.append(f"{dropped} copper segment(s) on layers with no EAGLE equivalent")
    if pours:
        notes.append(f"{pours} copper pour(s) carried over as polygons")


# --------------------------------------------------------------------------
# schematic drawn from the netlist
# --------------------------------------------------------------------------

PIN_PITCH = 2.54
PIN_X = -7.62
BLOCK_GAP = 12.7
COLUMN_WIDTH = 63.5


def _build_schematic(design: str, packages: dict[str, ET.Element],
                     parts: list[_Part], notes: list[str]) -> EagleDoc:
    """One box per part, one pin per pad, connections carried on labels."""
    pad_names = {name: _pads_of(node) for name, node in packages.items()}
    symbols = {name: _symbol(name, pads) for name, pads in pad_names.items()}
    devicesets = {name: _deviceset(name, pads) for name, pads in pad_names.items()}

    root, schematic = _shell("schematic", design, _SCH_LAYERS,
                             packages, symbols, devicesets)
    schematic.append(_libraries(design, packages, symbols, devicesets))
    attributes = ET.SubElement(schematic, "attributes")
    attributes.tail = "\n"
    variants = ET.SubElement(schematic, "variantdefs")
    variants.tail = "\n"
    schematic.append(_classes())

    parts_node = ET.SubElement(schematic, "parts")
    parts_node.text = "\n"
    parts_node.tail = "\n"
    for part in parts:
        node = ET.SubElement(parts_node, "part", {
            "name": part.ref, "library": design,
            "deviceset": part.package, "device": ""})
        if part.value:
            node.set("value", part.value)
        node.tail = "\n"

    sheets = ET.SubElement(schematic, "sheets")
    sheets.text = "\n"
    sheets.tail = "\n"
    sheet = ET.SubElement(sheets, "sheet")
    sheet.text = "\n"
    sheet.tail = "\n"
    for tag_name in ("plain", "instances", "busses", "nets"):
        holder = ET.SubElement(sheet, tag_name)
        holder.text = "\n"
        holder.tail = "\n"

    placed = _place_instances(sheet.find("instances"), parts, pad_names)
    nets: dict[str, ET.Element] = {}
    _wire_nets(nets, parts, pad_names, placed)
    for name in sorted(nets):
        sheet.find("nets").append(nets[name])

    doc = EagleDoc(path=Path(f"{design}.sch"), tree=ET.ElementTree(root), kind="sch")
    return doc


def _pads_of(package: ET.Element) -> list[str]:
    out: list[str] = []
    for node in package:
        if node.tag in ("smd", "pad"):
            name = node.get("name")
            if name and name not in out:
                out.append(name)
    return out


def _symbol(name: str, pads: list[str]) -> ET.Element:
    """A plain box with the pads down its left side."""
    symbol = ET.Element("symbol", {"name": name})
    symbol.text = "\n"
    symbol.tail = "\n"
    height = max(PIN_PITCH, PIN_PITCH * (len(pads) + 1))
    corners = [(-2.54, PIN_PITCH), (17.78, PIN_PITCH),
               (17.78, PIN_PITCH - height), (-2.54, PIN_PITCH - height),
               (-2.54, PIN_PITCH)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:]):
        wire = ET.SubElement(symbol, "wire", {
            "x1": fmt(x1), "y1": fmt(y1), "x2": fmt(x2), "y2": fmt(y2),
            "width": "0.254", "layer": "94"})
        wire.tail = "\n"
    for label, attrs in ((">NAME", {"y": fmt(PIN_PITCH + 0.5), "layer": "95"}),
                         (">VALUE", {"y": fmt(PIN_PITCH - height - 2.5), "layer": "96"})):
        text = ET.SubElement(symbol, "text", dict(
            {"x": "-2.54", "size": "1.778"}, **attrs))
        text.text = label
        text.tail = "\n"
    for index, pad in enumerate(pads):
        pin = ET.SubElement(symbol, "pin", {
            "name": pad, "x": fmt(PIN_X), "y": fmt(-PIN_PITCH * index),
            "length": "middle", "direction": "pas"})
        pin.tail = "\n"
    return symbol


def _deviceset(name: str, pads: list[str], package: str | None = None) -> ET.Element:
    """A deviceset with one gate on the box symbol and one device on the package.

    The box is usually named after the package.  When that name is taken on
    the sheet by a drawn symbol, the box gets another name and the device
    still has to point at the real package.
    """
    package = package or name
    deviceset = ET.Element("deviceset", {"name": name, "uservalue": "yes"})
    deviceset.text = "\n"
    deviceset.tail = "\n"
    gates = ET.SubElement(deviceset, "gates")
    gates.text = "\n"
    gates.tail = "\n"
    gate = ET.SubElement(gates, "gate",
                         {"name": "G$1", "symbol": name, "x": "0", "y": "0"})
    gate.tail = "\n"
    devices = ET.SubElement(deviceset, "devices")
    devices.text = "\n"
    devices.tail = "\n"
    device = ET.SubElement(devices, "device", {"name": "", "package": package})
    device.text = "\n"
    device.tail = "\n"
    connects = ET.SubElement(device, "connects")
    connects.text = "\n"
    connects.tail = "\n"
    for pad in pads:
        connect = ET.SubElement(connects, "connect",
                                {"gate": "G$1", "pin": pad, "pad": pad})
        connect.tail = "\n"
    technologies = ET.SubElement(device, "technologies")
    technologies.text = "\n"
    technologies.tail = "\n"
    technology = ET.SubElement(technologies, "technology", {"name": ""})
    technology.tail = "\n"
    return deviceset


def _place_instances(holder: ET.Element, parts: list[_Part],
                     pad_names: dict[str, list[str]],
                     origin: tuple[float, float] = (0.0, 0.0)) -> dict[str, tuple[float, float]]:
    """Lay the boxes out in columns, tallest-aware, and remember where they went."""
    placed: dict[str, tuple[float, float]] = {}
    x = origin[0]
    y = origin[1]
    column_bottom = y
    per_column = 0

    for part in parts:
        pads = pad_names.get(part.package, [])
        height = PIN_PITCH * (len(pads) + 2) + BLOCK_GAP
        if per_column and y - height < origin[1] - 254.0:
            x += COLUMN_WIDTH
            y = origin[1]
            per_column = 0
        instance = ET.SubElement(holder, "instance", {
            "part": part.ref, "gate": "G$1", "x": fmt(x), "y": fmt(y)})
        instance.tail = "\n"
        placed[part.ref] = (x, y)
        y -= height
        per_column += 1
        column_bottom = min(column_bottom, y)
    return placed


def _wire_nets(nets: dict[str, ET.Element], parts: list[_Part],
               pad_names: dict[str, list[str]],
               placed: dict[str, tuple[float, float]]) -> None:
    """Attach a labelled stub to every pin, which is how EAGLE joins by name.

    Segments go into the net of that name in `nets`, made if it is not there
    yet, so a box wired beside a drawing joins the drawing's own net rather
    than standing up a second one of the same name.
    """
    by_net: dict[str, list[tuple[str, str]]] = {}
    for part in parts:
        for pad, net in part.pads:
            by_net.setdefault(net, []).append((part.ref, pad))

    for net_name in sorted(by_net):
        net = nets.get(net_name)
        if net is None:
            net = ET.Element("net", {"name": net_name, "class": "0"})
            net.text = "\n"
            net.tail = "\n"
            nets[net_name] = net
        for ref, pad in by_net[net_name]:
            spot = placed.get(ref)
            part = next((p for p in parts if p.ref == ref), None)
            if spot is None or part is None:
                continue
            pads = pad_names.get(part.package, [])
            if pad not in pads:
                continue
            index = pads.index(pad)
            px = spot[0] + PIN_X
            py = spot[1] - PIN_PITCH * index

            segment = ET.SubElement(net, "segment")
            segment.text = "\n"
            segment.tail = "\n"
            wire = ET.SubElement(segment, "wire", {
                "x1": fmt(px - 2.54), "y1": fmt(py), "x2": fmt(px), "y2": fmt(py),
                "width": "0.1524", "layer": "91"})
            wire.tail = "\n"
            label = ET.SubElement(segment, "label", {
                "x": fmt(px - 2.54), "y": fmt(py + 0.5),
                "size": "1.27", "layer": "95"})
            label.tail = "\n"
            pinref = ET.SubElement(segment, "pinref",
                                   {"part": ref, "gate": "G$1", "pin": pad})
            pinref.tail = "\n"
