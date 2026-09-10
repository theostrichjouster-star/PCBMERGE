"""Convert a KiCad schematic drawing into an EAGLE one.

The board alone is enough to merge with, and the netlist route this replaces
proved that, but the drawing it produced was one box per part and nobody reads
a schematic that way.  This reads the `.kicad_sch` instead: the symbols as they
were drawn, where they were placed, the wires between them, and the labels.

Two coordinate systems meet here and confusing them is the way to get a drawing
that looks almost right.  A **symbol** is stored y-up, exactly as EAGLE stores
one, so symbol geometry crosses over untouched.  A **sheet** is stored y-down,
so every placement, wire and label has its y negated on the way out.  Rotation
belongs to the symbol's frame and is carried across unchanged, which the real
files confirm: on a Seeed board of 157 symbols, taking the angle as given lands
pins on wire ends everywhere that negating it does and in the rotated cases
where negating it does not.

Nets are still the board's.  Connectivity here is worked out from the geometry
the way KiCad works it out, but the *name* of each net comes from the board
wherever a pin can be matched to a pad, because the merged pair is only openable
if the schematic's nets and the board's signals agree.  A drawing that named
them itself would disagree on every unnamed net.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import sexp
from .eagle import EagleDoc, sanitize_name

SYMBOL_LAYER = "94"
NET_LAYER = "91"
LABEL_LAYER = "95"

# EAGLE has four pin lengths, in tenths of an inch.  A KiCad pin is any length
# it likes, so it takes the nearest one and the connection point stays put.
LENGTHS = ((0.0, "point"), (2.54, "short"), (5.08, "middle"), (7.62, "long"))

DIRECTIONS = {
    "input": "in", "output": "out", "bidirectional": "io", "tri_state": "hiz",
    "passive": "pas", "free": "pas", "unspecified": "pas",
    "power_in": "pwr", "power_out": "sup", "open_collector": "oc",
    "open_emitter": "oc", "no_connect": "nc",
}

# Points closer together than this are the same point.  KiCad works on a
# 1.27 mm grid and writes six decimals, so this is far tighter than any real
# gap and far looser than any rounding.
TOUCH = 0.01

# Room left between one sheet's drawing and the next when a hierarchy is
# flattened onto EAGLE's single sheet.
SHEET_GAP = 25.4


@dataclass
class Pin:
    """One pin of a library symbol, in the symbol's own coordinates."""

    number: str
    name: str
    x: float
    y: float
    angle: float
    length: float
    kind: str
    unit: int


@dataclass
class Symbol:
    """A library symbol, its graphics already converted."""

    lib_id: str
    name: str
    units: dict[int, list[ET.Element]] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)
    power: bool = False

    def pins_of(self, unit: int) -> list[Pin]:
        """Pins of one unit, plus the ones drawn in every unit (unit 0)."""
        return [p for p in self.pins if p.unit in (unit, 0)]

    def shapes_of(self, unit: int) -> list[ET.Element]:
        """Graphics of one unit, on top of the ones common to all of them."""
        return list(self.units.get(0, [])) + list(self.units.get(unit, []))

    def real_units(self) -> list[int]:
        """The units a placement can actually name.

        KiCad reserves unit 0 for what every unit shares, so a symbol drawn
        entirely in unit 0 -- which is most two-pin parts -- is still placed as
        unit 1 and has to be built as one.
        """
        numbered = sorted(u for u in self.units if u > 0)
        return numbered or [1]


@dataclass
class Placed:
    """A symbol put on a sheet."""

    ref: str
    value: str
    symbol: Symbol
    unit: int
    x: float
    y: float
    angle: float
    mirror: str
    on_board: bool = False


@dataclass
class Drawing:
    """Everything read out of one hierarchy of sheets."""

    placed: list[Placed] = field(default_factory=list)
    symbols: dict[str, Symbol] = field(default_factory=dict)
    wires: list[tuple[float, float, float, float]] = field(default_factory=list)
    junctions: list[tuple[float, float]] = field(default_factory=list)
    labels: list[tuple[str, float, float]] = field(default_factory=list)
    plain: list[ET.Element] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether there is a drawing here at all.

        A root sheet that only points at children which are not present has
        symbols for its title block and nothing else; drawing that would be
        worse than falling back to the netlist.
        """
        return bool(self.wires) and len(self.placed) > 1


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def read(path: Path) -> Drawing:
    """Read a sheet and every child sheet that is present beside it."""
    drawing = Drawing()
    _read_sheet(path, drawing, offset=(0.0, 0.0), seen=set())
    return drawing


def _read_sheet(path: Path, drawing: Drawing, offset: tuple[float, float],
                seen: set[str]) -> tuple[float, float]:
    """Read one file into `drawing`, offset so sheets do not overlap.

    Returns the width and height used, so the caller can put the next sheet
    somewhere else.
    """
    key = str(path.resolve()).lower()
    if key in seen or not path.is_file():
        return 0.0, 0.0
    seen.add(key)

    try:
        tree = sexp.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (sexp.SexpError, OSError):
        drawing.missing.append(path.name)
        return 0.0, 0.0
    if sexp.tag(tree) != "kicad_sch":
        drawing.missing.append(path.name)
        return 0.0, 0.0

    _read_symbols(tree, drawing)
    used = _read_body(tree, drawing, offset)

    # Children are laid out below this sheet, one after another.
    below = offset[1] + used[1] + SHEET_GAP
    for sheet in sexp.children(tree, "sheet"):
        props = {p[1]: p[2] for p in sexp.children(sheet, "property") if len(p) > 2}
        name = props.get("Sheetfile") or props.get("Sheet file") or ""
        if not name:
            continue
        child = path.parent / name
        if not child.is_file():
            drawing.missing.append(name)
            continue
        _, height = _read_sheet(child, drawing, (offset[0], below), seen)
        below += height + SHEET_GAP
    return used[0], below - offset[1]


def _read_symbols(tree, drawing: Drawing) -> None:
    for node in sexp.children(sexp.first(tree, "lib_symbols") or [], "symbol"):
        lib_id = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        if not lib_id or lib_id in drawing.symbols:
            continue
        drawing.symbols[lib_id] = _symbol(lib_id, node)


def _symbol(lib_id: str, node) -> Symbol:
    name = sanitize_name(lib_id.split(":")[-1]) or "SYMBOL"
    symbol = Symbol(lib_id=lib_id, name=name,
                    power=sexp.first(node, "power") is not None)
    for sub in sexp.children(node, "symbol"):
        unit = _unit_of(sub[1] if len(sub) > 1 else "", lib_id)
        shapes = symbol.units.setdefault(unit, [])
        for item in sub[1:]:
            if not isinstance(item, list):
                continue
            tag = sexp.tag(item)
            if tag == "pin":
                pin = _pin(item, unit)
                if pin is not None:
                    symbol.pins.append(pin)
            else:
                shapes.extend(_shape(item, SYMBOL_LAYER))
    if not symbol.units:
        symbol.units[1] = []
    return symbol


def _unit_of(sub_name: str, lib_id: str) -> int:
    """A sub-symbol is called `<name>_<unit>_<style>`."""
    parts = sub_name.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit():
        return int(parts[1])
    return 1


def _pin(node, unit: int) -> Pin | None:
    at = sexp.first(node, "at")
    if at is None or len(at) < 3:
        return None
    name = (sexp.first(node, "name") or [None, "~"])[1]
    number = (sexp.first(node, "number") or [None, ""])[1]
    kind = node[1] if len(node) > 1 and isinstance(node[1], str) else "passive"
    return Pin(
        number=str(number), name=str(name),
        x=sexp.as_float(at[1]), y=sexp.as_float(at[2]),
        angle=sexp.as_float(at[3]) if len(at) > 3 else 0.0,
        length=sexp.number(node, "length", default=2.54),
        kind=kind, unit=0 if unit == 0 else unit,
    )


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def _shape(node, layer: str) -> list[ET.Element]:
    """One KiCad graphic as EAGLE elements, in the same coordinate frame."""
    tag = sexp.tag(node)
    width = _width(node)
    if tag == "rectangle":
        start, end = sexp.first(node, "start"), sexp.first(node, "end")
        if start is None or end is None:
            return []
        x1, y1 = sexp.as_float(start[1]), sexp.as_float(start[2])
        x2, y2 = sexp.as_float(end[1]), sexp.as_float(end[2])
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
        return [_wire(corners[i], corners[i + 1], width, layer) for i in range(4)]
    if tag == "polyline":
        points = _points(node)
        return [_wire(points[i], points[i + 1], width, layer)
                for i in range(len(points) - 1)]
    if tag == "circle":
        centre = sexp.first(node, "center")
        if centre is None:
            return []
        return [_element("circle", {
            "x": _fmt(sexp.as_float(centre[1])), "y": _fmt(sexp.as_float(centre[2])),
            "radius": _fmt(sexp.number(node, "radius")), "width": width,
            "layer": layer})]
    if tag == "arc":
        return _arc(node, width, layer)
    if tag == "text":
        at = sexp.first(node, "at")
        body = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        if at is None or not body.strip():
            return []
        item = _element("text", {
            "x": _fmt(sexp.as_float(at[1])), "y": _fmt(sexp.as_float(at[2])),
            "size": _fmt(_text_size(node)), "layer": layer})
        item.text = body
        return [item]
    return []


def _arc(node, width: str, layer: str) -> list[ET.Element]:
    start, mid, end = (sexp.first(node, n) for n in ("start", "mid", "end"))
    if start is None or mid is None or end is None:
        return []
    a = (sexp.as_float(start[1]), sexp.as_float(start[2]))
    m = (sexp.as_float(mid[1]), sexp.as_float(mid[2]))
    b = (sexp.as_float(end[1]), sexp.as_float(end[2]))
    curve = _curve(a, m, b)
    wire = _wire(a, b, width, layer)
    if curve:
        wire.set("curve", _fmt(curve))
    return [wire]


def _curve(start, mid, end) -> float:
    """EAGLE states an arc as its included angle; KiCad states three points."""
    ax, ay = start[0] - mid[0], start[1] - mid[1]
    bx, by = end[0] - mid[0], end[1] - mid[1]
    la = math.hypot(ax, ay)
    lb = math.hypot(bx, by)
    if la < 1e-9 or lb < 1e-9:
        return 0.0
    cosine = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
    swept = 2.0 * (180.0 - math.degrees(math.acos(cosine)))
    area = (mid[0] - start[0]) * (end[1] - start[1]) - \
           (mid[1] - start[1]) * (end[0] - start[0])
    return swept if area > 0 else -swept


def _points(node) -> list[tuple[float, float]]:
    pts = sexp.first(node, "pts")
    if pts is None:
        return []
    return [(sexp.as_float(p[1]), sexp.as_float(p[2]))
            for p in sexp.children(pts, "xy") if len(p) > 2]


def _width(node) -> str:
    stroke = sexp.first(node, "stroke")
    value = sexp.number(stroke, "width", default=0.0) if stroke is not None else 0.0
    return _fmt(value if value > 0 else 0.254)


def _text_size(node) -> float:
    effects = sexp.first(node, "effects")
    font = sexp.first(effects, "font") if effects is not None else None
    size = sexp.first(font, "size") if font is not None else None
    return sexp.as_float(size[2]) if size is not None and len(size) > 2 else 1.778


def _wire(a, b, width: str, layer: str) -> ET.Element:
    return _element("wire", {
        "x1": _fmt(a[0]), "y1": _fmt(a[1]), "x2": _fmt(b[0]), "y2": _fmt(b[1]),
        "width": width, "layer": layer})


def _element(tag: str, attrs: dict[str, str]) -> ET.Element:
    node = ET.Element(tag, attrs)
    node.tail = "\n"
    return node


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


# --------------------------------------------------------------------------
# the sheet body
# --------------------------------------------------------------------------

def _read_body(tree, drawing: Drawing,
               offset: tuple[float, float]) -> tuple[float, float]:
    """Placements, wires and labels of one sheet, moved by `offset`.

    Sheet coordinates are y-down here and stay that way until the whole
    drawing is written out, so one negation happens in one place.
    """
    dx, dy = offset
    left = right = top = bottom = None

    def seen(x: float, y: float) -> None:
        nonlocal left, right, top, bottom
        left = x if left is None else min(left, x)
        right = x if right is None else max(right, x)
        top = y if top is None else min(top, y)
        bottom = y if bottom is None else max(bottom, y)

    for node in sexp.children(tree, "symbol"):
        place = _placement(node, drawing, dx, dy)
        if place is not None:
            drawing.placed.append(place)
            seen(place.x, place.y)

    for node in sexp.children(tree, "wire"):
        points = _points(node)
        for index in range(len(points) - 1):
            (x1, y1), (x2, y2) = points[index], points[index + 1]
            drawing.wires.append((x1 + dx, y1 + dy, x2 + dx, y2 + dy))
            seen(x1 + dx, y1 + dy)
            seen(x2 + dx, y2 + dy)

    for node in sexp.children(tree, "junction"):
        at = sexp.first(node, "at")
        if at is not None and len(at) > 2:
            drawing.junctions.append(
                (sexp.as_float(at[1]) + dx, sexp.as_float(at[2]) + dy))

    for tag in ("label", "global_label", "hierarchical_label"):
        for node in sexp.children(tree, tag):
            at = sexp.first(node, "at")
            text = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
            if at is None or not text:
                continue
            drawing.labels.append(
                (text, sexp.as_float(at[1]) + dx, sexp.as_float(at[2]) + dy))

    for tag in ("polyline", "text", "rectangle", "circle", "arc"):
        for node in sexp.children(tree, tag):
            for item in _shape(node, SYMBOL_LAYER):
                drawing.plain.append(_shift(item, dx, dy))

    if left is None:
        return 0.0, 0.0
    return right - left, bottom - top


def _shift(node: ET.Element, dx: float, dy: float) -> ET.Element:
    for xs, ys in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
        if node.get(xs) is not None:
            node.set(xs, _fmt(float(node.get(xs)) + dx))
            node.set(ys, _fmt(float(node.get(ys)) + dy))
    return node


def _placement(node, drawing: Drawing, dx: float, dy: float) -> Placed | None:
    lib_id = sexp.value(node, "lib_id", default="")
    symbol = drawing.symbols.get(lib_id)
    at = sexp.first(node, "at")
    if symbol is None or at is None or len(at) < 3:
        return None
    props = {p[1]: p[2] for p in sexp.children(node, "property")
             if len(p) > 2 and isinstance(p[1], str)}
    ref = str(props.get("Reference", "")).strip()
    if not ref or ref.startswith("#"):
        # KiCad hides power symbols and other drawing-only parts behind a `#`.
        ref = ""
    mirror = sexp.first(node, "mirror")
    return Placed(
        ref=ref, value=str(props.get("Value", "")), symbol=symbol,
        unit=int(sexp.as_float(sexp.value(node, "unit", default=1)) or 1),
        x=sexp.as_float(at[1]) + dx, y=sexp.as_float(at[2]) + dy,
        angle=sexp.as_float(at[3]) if len(at) > 3 else 0.0,
        mirror=(mirror[1] if mirror and len(mirror) > 1 else ""),
    )


def pin_at(place: Placed, pin: Pin) -> tuple[float, float]:
    """Where a pin's connection point lands on the sheet, still y-down.

    The symbol's frame is y-up and the sheet's is y-down, so the rotation is
    done in the symbol's frame and only the result is flipped into the sheet.
    """
    angle = math.radians(place.angle)
    x = pin.x * math.cos(angle) - pin.y * math.sin(angle)
    y = pin.x * math.sin(angle) + pin.y * math.cos(angle)
    if place.mirror == "y":
        x = -x
    elif place.mirror == "x":
        y = -y
    return place.x + x, place.y - y


# --------------------------------------------------------------------------
# the library
# --------------------------------------------------------------------------

@dataclass
class Built:
    """A drawing turned into the pieces an EAGLE schematic is made of."""

    symbols: dict[str, ET.Element] = field(default_factory=dict)
    devicesets: dict[str, ET.Element] = field(default_factory=dict)
    parts: list[dict] = field(default_factory=list)      # name, deviceset, device, value
    plain: list[ET.Element] = field(default_factory=list)
    instances: list[ET.Element] = field(default_factory=list)
    nets: list[ET.Element] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)    # board nets nothing drew


def build(drawing: Drawing, pads_of: dict[str, list[str]],
          packages: dict[str, str], board_nets: dict[tuple[str, str], str]) -> Built:
    """Turn a drawing into symbols, devicesets, parts, instances and nets.

    `packages` maps a reference designator to the package its footprint uses,
    and `board_nets` maps a (reference, pad) pair to the net the board puts it
    on.  Both come from the board, which stays the authority on what is
    connected to what.
    """
    out = Built()
    names = _EagleNames()
    shapes: dict[tuple[str, int], str] = {}     # (lib_id, unit) -> symbol name
    pin_names: dict[tuple[str, int], dict[str, str]] = {}

    for lib_id, symbol in sorted(drawing.symbols.items()):
        units = symbol.real_units()
        for unit in units:
            pins = symbol.pins_of(unit)
            shapes_here = symbol.shapes_of(unit)
            if not pins and not shapes_here:
                continue
            name = names.symbol(symbol.name, unit, len(units) > 1)
            shapes[(lib_id, unit)] = name
            mapping = _pin_names(pins)
            pin_names[(lib_id, unit)] = mapping
            out.symbols[name] = _eagle_symbol(name, shapes_here, pins, mapping)

    for place in drawing.placed:
        key = (place.symbol.lib_id, place.unit)
        if key not in shapes:
            key = (place.symbol.lib_id, 1)
        if key not in shapes:
            # No symbol means no part, and a pinref to a part that is not
            # there is a file EAGLE refuses; say so by clearing the name.
            place.ref = ""
            continue
        shape = shapes[key]
        package = packages.get(place.ref, "")
        device = sanitize_name(package) if package else ""
        deviceset = names.deviceset(shape)
        node = out.devicesets.get(deviceset)
        if node is None:
            node = _eagle_deviceset(deviceset, shape)
            out.devicesets[deviceset] = node
        _add_device(node, device, package, shape,
                    pin_names.get(key, {}), pads_of.get(package, []))

        part = names.part(place.ref or place.symbol.name)
        place.on_board = bool(package)
        out.parts.append({"name": part, "deviceset": deviceset,
                          "device": device, "value": place.value})
        out.instances.append(_element("instance", {
            "part": part, "gate": "G$1",
            "x": _fmt(place.x), "y": _fmt(-place.y),
            **_rotation(place)}))
        place.ref = part                       # what the pinrefs must now say

    out.plain = [_shift_y(node) for node in drawing.plain]
    out.nets, out.unplaced = _nets(drawing, pin_names, shapes, board_nets)
    return out


class _EagleNames:
    """Names EAGLE will accept, kept unique without losing the original."""

    def __init__(self) -> None:
        self._taken: set[str] = set()
        self._parts: set[str] = set()

    def symbol(self, base: str, unit: int, many: bool) -> str:
        wanted = f"{base}_{unit}" if many else base
        return self._unique(sanitize_name(wanted) or "SYMBOL")

    def deviceset(self, symbol_name: str) -> str:
        return symbol_name

    def part(self, base: str) -> str:
        wanted = sanitize_name(base) or "P"
        name, count = wanted, 1
        while name in self._parts:
            count += 1
            name = f"{wanted}{count}"
        self._parts.add(name)
        return name

    def _unique(self, wanted: str) -> str:
        name, count = wanted, 1
        while name in self._taken:
            count += 1
            name = f"{wanted}_{count}"
        self._taken.add(name)
        return name


def _pin_names(pins: list[Pin]) -> dict[str, str]:
    """A unique EAGLE pin name per pad number.

    KiCad lets a symbol repeat a pin name -- eight pins called GND is ordinary
    -- and EAGLE does not, so a repeat is qualified by its number.
    """
    counts: dict[str, int] = {}
    for pin in pins:
        base = sanitize_name(pin.name) if pin.name not in ("", "~") else ""
        counts[base] = counts.get(base, 0) + 1

    out: dict[str, str] = {}
    used: set[str] = set()
    for pin in pins:
        base = sanitize_name(pin.name) if pin.name not in ("", "~") else ""
        name = base if base and counts[base] == 1 else f"{base}{pin.number}" if base \
            else f"P{pin.number}"
        name = sanitize_name(name) or f"P{pin.number}"
        while name in used:
            name = f"{name}_"
        used.add(name)
        out[pin.number] = name
    return out


def _eagle_symbol(name: str, shapes: list[ET.Element], pins: list[Pin],
                  mapping: dict[str, str]) -> ET.Element:
    node = ET.Element("symbol", {"name": name})
    node.text = "\n"
    node.tail = "\n"
    for shape in shapes:
        node.append(shape)
    for pin in pins:
        node.append(_eagle_pin(pin, mapping.get(pin.number, f"P{pin.number}")))
    return node


def _eagle_pin(pin: Pin, name: str) -> ET.Element:
    length = min(LENGTHS, key=lambda pair: abs(pair[0] - pin.length))[1]
    attrs = {"name": name, "x": _fmt(pin.x), "y": _fmt(pin.y), "length": length}
    rot = int(round(pin.angle / 90.0)) % 4 * 90
    if rot:
        attrs["rot"] = f"R{rot}"
    direction = DIRECTIONS.get(pin.kind)
    if direction and direction != "io":
        attrs["direction"] = direction
    if pin.name in ("", "~"):
        attrs["visible"] = "pad"
    return _element("pin", attrs)


def _eagle_deviceset(name: str, symbol: str) -> ET.Element:
    node = ET.Element("deviceset", {"name": name})
    node.text = "\n"
    node.tail = "\n"
    gates = ET.SubElement(node, "gates")
    gates.text = "\n"
    gates.tail = "\n"
    gate = ET.SubElement(gates, "gate",
                         {"name": "G$1", "symbol": symbol, "x": "0", "y": "0"})
    gate.tail = "\n"
    devices = ET.SubElement(node, "devices")
    devices.text = "\n"
    devices.tail = "\n"
    return node


def _add_device(deviceset: ET.Element, name: str, package: str, symbol: str,
                mapping: dict[str, str], pads: list[str]) -> None:
    devices = deviceset.find("devices")
    for existing in devices:
        if existing.get("name") == name:
            return
    attrs = {"name": name}
    if package and pads:
        attrs["package"] = package
    device = ET.SubElement(devices, "device", attrs)
    device.text = "\n"
    device.tail = "\n"
    if not (package and pads):
        return
    connects = ET.SubElement(device, "connects")
    connects.text = "\n"
    connects.tail = "\n"
    for number, pin_name in mapping.items():
        if number in pads:
            row = ET.SubElement(connects, "connect",
                                {"gate": "G$1", "pin": pin_name, "pad": number})
            row.tail = "\n"


def _rotation(place: Placed) -> dict[str, str]:
    """EAGLE states rotation and mirroring in one attribute."""
    rot = int(round(place.angle / 90.0)) % 4 * 90
    mirrored = place.mirror in ("x", "y")
    if place.mirror == "x":
        # Mirroring about the sheet's x axis is a y mirror plus half a turn.
        rot = (rot + 180) % 360
    if not rot and not mirrored:
        return {}
    prefix = "M" if mirrored else ""
    return {"rot": f"{prefix}R{rot}"}


def _shift_y(node: ET.Element) -> ET.Element:
    """Flip a sheet-space element into EAGLE's y-up world."""
    for xs, ys in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
        if node.get(ys) is not None:
            node.set(ys, _fmt(-float(node.get(ys))))
    return node


# --------------------------------------------------------------------------
# connectivity
# --------------------------------------------------------------------------

class _Groups:
    """Union-find over sheet points, which is what a net is before it has a name."""

    def __init__(self) -> None:
        self._parent: dict[tuple[int, int], tuple[int, int]] = {}

    @staticmethod
    def key(x: float, y: float) -> tuple[int, int]:
        return (round(x / TOUCH), round(y / TOUCH))

    def find(self, point: tuple[int, int]) -> tuple[int, int]:
        root = self._parent.setdefault(point, point)
        while root != self._parent[root]:
            root = self._parent[root]
        while self._parent[point] != root:      # flatten as we go
            self._parent[point], point = root, self._parent[point]
        return root

    def join(self, first, second) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self._parent[a] = b

    def at(self, x: float, y: float) -> tuple[int, int]:
        return self.find(self.key(x, y))


def _on_segment(px: float, py: float, x1: float, y1: float,
                x2: float, y2: float) -> bool:
    """Whether a point lies on a wire, ends included.

    A pin or a wire end touching the middle of another wire is connected in
    KiCad; two wires merely crossing are not, which is why only points that
    exist are tested rather than every intersection.
    """
    dx, dy = x2 - x1, y2 - y1
    span = math.hypot(dx, dy)
    if span < TOUCH:
        return math.hypot(px - x1, py - y1) <= TOUCH
    # distance from the line, then whether it falls between the ends
    away = abs(dy * (px - x1) - dx * (py - y1)) / span
    if away > TOUCH:
        return False
    along = ((px - x1) * dx + (py - y1) * dy) / (span * span)
    return -TOUCH <= along * span <= span + TOUCH


def _nets(drawing: Drawing, pin_names: dict, shapes: dict,
          board_nets: dict[tuple[str, str], str]):
    """Group the drawing into nets and name them from the board.

    Returns the EAGLE `<net>` elements and the names of any board nets nothing
    in the drawing reached, which the caller has to account for: a board signal
    with no schematic net is a pair EAGLE will not open.
    """
    groups = _Groups()
    for x1, y1, x2, y2 in drawing.wires:
        groups.join(groups.key(x1, y1), groups.key(x2, y2))

    points: list[tuple[float, float]] = []
    for x1, y1, x2, y2 in drawing.wires:
        points.append((x1, y1))
        points.append((x2, y2))
    points.extend(drawing.junctions)

    # Anything sitting on the middle of a wire joins that wire.
    for px, py in list(points):
        for x1, y1, x2, y2 in drawing.wires:
            if _on_segment(px, py, x1, y1, x2, y2):
                groups.join(groups.key(px, py), groups.key(x1, y1))

    pins: dict[tuple[int, int], list[tuple[str, str, str]]] = {}
    for place in drawing.placed:
        if not place.ref:
            continue
        key = (place.symbol.lib_id, place.unit)
        if key not in shapes:
            key = (place.symbol.lib_id, 1)
        mapping = pin_names.get(key, {})
        for pin in place.symbol.pins_of(place.unit):
            px, py = pin_at(place, pin)
            spot = groups.key(px, py)
            for x1, y1, x2, y2 in drawing.wires:
                if _on_segment(px, py, x1, y1, x2, y2):
                    groups.join(spot, groups.key(x1, y1))
                    break
            pins.setdefault(groups.find(spot), []).append(
                (place.ref, mapping.get(pin.number, f"P{pin.number}"), pin.number))

    labels: dict[tuple[int, int], list[tuple[str, float, float]]] = {}
    for text, x, y in drawing.labels:
        labels.setdefault(groups.at(x, y), []).append((text, x, y))

    wires: dict[tuple[int, int], list] = {}
    for x1, y1, x2, y2 in drawing.wires:
        wires.setdefault(groups.at(x1, y1), []).append((x1, y1, x2, y2))
    junctions: dict[tuple[int, int], list] = {}
    for x, y in drawing.junctions:
        junctions.setdefault(groups.at(x, y), []).append((x, y))

    return _emit(drawing, pins, labels, wires, junctions, board_nets)


def _emit(drawing, pins, labels, wires, junctions, board_nets):
    """Write each group out as a segment, folded into one net per name."""
    nets: dict[str, ET.Element] = {}
    reached: set[str] = set()
    anonymous = 0
    order = sorted(set(pins) | set(labels) | set(wires) | set(junctions),
                   key=lambda k: (k[1], k[0]))

    for group in order:
        members = pins.get(group, [])
        name = _name_for(members, labels.get(group, []), board_nets)
        if not name:
            anonymous += 1
            name = f"N${anonymous}"
        reached.add(name)

        net = nets.get(name)
        if net is None:
            net = ET.Element("net", {"name": name, "class": "0"})
            net.text = "\n"
            net.tail = "\n"
            nets[name] = net
        segment = ET.SubElement(net, "segment")
        segment.text = "\n"
        segment.tail = "\n"

        for x1, y1, x2, y2 in wires.get(group, []):
            segment.append(_wire((x1, -y1), (x2, -y2), "0.1524", NET_LAYER))
        for x, y in junctions.get(group, []):
            segment.append(_element("junction", {"x": _fmt(x), "y": _fmt(-y)}))
        for ref, pin_name, _number in members:
            segment.append(_element(
                "pinref", {"part": ref, "gate": "G$1", "pin": pin_name}))
        for text, x, y in labels.get(group, [])[:1]:
            segment.append(_element("label", {
                "x": _fmt(x), "y": _fmt(-y), "size": "1.778",
                "layer": LABEL_LAYER}))

    unplaced = sorted(set(board_nets.values()) - reached)
    return list(nets.values()), unplaced


def _name_for(members, found_labels, board_nets) -> str:
    """The board's name for this group, or the label on it, or nothing.

    The board wins because the merged pair has to agree, and the board is
    where the netlist actually lives.  A label only names a group the board
    says nothing about, which is a net with no pads on it.
    """
    for ref, _pin_name, number in members:
        name = board_nets.get((ref, number))
        if name:
            return name
    for text, _x, _y in found_labels:
        cleaned = sanitize_name(text)
        if cleaned:
            return cleaned
    return ""
