"""Read, write and transform EAGLE XML documents (.sch / .brd).

The whole tool rests on this module: EAGLE files are plain XML, so a merge is
really a tree surgery problem plus a naming problem.  Everything here is about
keeping that surgery lossless -- comments, attribute order and text nodes are
preserved so a merged file diffs cleanly against its sources.
"""

from __future__ import annotations

import copy
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

XML_HEADER = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'

# Tags whose children are structural containers; a newline tail on these keeps
# generated files line-per-element the way EAGLE writes them.
STRUCTURAL = {
    "eagle", "drawing", "schematic", "board", "libraries", "library", "packages",
    "package", "symbols", "symbol", "devicesets", "deviceset", "gates", "devices",
    "device", "connects", "technologies", "technology", "parts", "part", "sheets",
    "sheet", "instances", "instance", "busses", "bus", "nets", "net", "segment",
    "signals", "signal", "elements", "element", "classes", "class", "plain",
    "attributes", "variantdefs", "designrules", "autorouter", "errors",
    "mfgpreviewcolors", "layers", "settings",
}

# Attribute pairs that carry drawing coordinates.  Anything not listed here
# (rot, curve, width, drill, ...) is invariant under translation.
POINT_ATTRS = (("x", "y"), ("x1", "y1"), ("x2", "y2"), ("x3", "y3"))


class EagleError(Exception):
    """Raised when a file is not a usable EAGLE drawing."""


def _parser() -> ET.XMLParser:
    return ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))


@dataclass
class EagleDoc:
    """One EAGLE drawing: either a schematic or a board."""

    path: Path
    tree: ET.ElementTree
    kind: str  # "sch" or "brd"

    @classmethod
    def load(cls, path: str | Path) -> "EagleDoc":
        path = Path(path)
        try:
            tree = ET.parse(path, parser=_parser())
        except ET.ParseError as exc:  # pragma: no cover - depends on bad input
            raise EagleError(f"{path.name}: not valid XML ({exc})") from exc
        root = tree.getroot()
        if root.tag != "eagle":
            raise EagleError(f"{path.name}: root element is <{root.tag}>, expected <eagle>")
        drawing = root.find("drawing")
        if drawing is None:
            raise EagleError(f"{path.name}: no <drawing> element")
        if drawing.find("schematic") is not None:
            kind = "sch"
        elif drawing.find("board") is not None:
            kind = "brd"
        else:
            raise EagleError(f"{path.name}: drawing holds neither <schematic> nor <board>")
        return cls(path=path, tree=tree, kind=kind)

    # -- navigation ---------------------------------------------------------
    @property
    def root(self) -> ET.Element:
        return self.tree.getroot()

    @property
    def version(self) -> str:
        return self.root.get("version", "")

    @property
    def drawing(self) -> ET.Element:
        return self.root.find("drawing")

    @property
    def section(self) -> ET.Element:
        """The <schematic> or <board> element."""
        node = self.drawing.find("schematic")
        return node if node is not None else self.drawing.find("board")

    def libraries(self) -> list[ET.Element]:
        node = self.section.find("libraries")
        return list(node) if node is not None else []

    def parts(self) -> list[ET.Element]:
        return list(self.section.iterfind("parts/part"))

    def elements(self) -> list[ET.Element]:
        return list(self.section.iterfind("elements/element"))

    def sheets(self) -> list[ET.Element]:
        return list(self.section.iterfind("sheets/sheet"))

    def nets(self) -> list[ET.Element]:
        return list(self.section.iterfind("sheets/sheet/nets/net"))

    def signals(self) -> list[ET.Element]:
        return list(self.section.iterfind("signals/signal"))

    def net_names(self) -> list[str]:
        seen: dict[str, None] = {}
        source = self.nets() if self.kind == "sch" else self.signals()
        for node in source:
            seen.setdefault(node.get("name", ""), None)
        return list(seen)

    def layers(self) -> list[ET.Element]:
        node = self.drawing.find("layers")
        return list(node) if node is not None else []

    # -- output -------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = ET.tostring(self.root, encoding="unicode")
        path.write_text(XML_HEADER + body + "\n", encoding="utf-8")
        return path


# --------------------------------------------------------------------------
# tree helpers
# --------------------------------------------------------------------------

def child(parent: ET.Element, tag: str) -> ET.Element:
    """Return parent's child `tag`, creating it if absent."""
    node = parent.find(tag)
    if node is None:
        node = ET.SubElement(parent, tag)
        node.text = "\n"
        node.tail = "\n"
    return node


def tidy(elem: ET.Element) -> None:
    """Give structural elements newline tails so output stays readable."""
    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag in STRUCTURAL:
            if not node.tail:
                node.tail = "\n"
            if len(node) and not node.text:
                node.text = "\n"
        elif node.tag in ("part", "element", "instance", "pinref", "contactref", "wire", "label"):
            if not node.tail:
                node.tail = "\n"


def translate(elem: ET.Element, dx: float, dy: float) -> None:
    """Shift every coordinate in a subtree by (dx, dy).

    Only ever call this on board-level geometry (plain / elements / signals).
    Library packages use local coordinates and must stay untouched.
    """
    if dx == 0 and dy == 0:
        return
    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        for xa, ya in POINT_ATTRS:
            xv, yv = node.get(xa), node.get(ya)
            if xv is not None and yv is not None:
                try:
                    node.set(xa, fmt(float(xv) + dx))
                    node.set(ya, fmt(float(yv) + dy))
                except ValueError:
                    continue


def fmt(value: float) -> str:
    """Format a coordinate the way EAGLE does: shortest exact decimal."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def bbox(elem: ET.Element) -> tuple[float, float, float, float] | None:
    """Axis-aligned bounds of every coordinate in a subtree."""
    xs: list[float] = []
    ys: list[float] = []
    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        for xa, ya in POINT_ATTRS:
            xv, yv = node.get(xa), node.get(ya)
            if xv is not None and yv is not None:
                try:
                    xs.append(float(xv))
                    ys.append(float(yv))
                except ValueError:
                    continue
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def content_hash(elem: ET.Element, ignore: Iterable[str] = ()) -> str:
    """Stable digest of an element's meaning, ignoring the given attributes.

    Used to decide whether two same-named library items are actually the same
    thing.  Attribute order is normalised so files written by different EAGLE
    versions still compare equal.
    """
    ignored = set(ignore)

    def walk(node: ET.Element, out: list[str]) -> None:
        if not isinstance(node.tag, str):  # comments carry no meaning here
            return
        attrs = sorted((k, v) for k, v in node.attrib.items() if k not in ignored)
        out.append(node.tag + "|" + "|".join(f"{k}={v}" for k, v in attrs))
        text = (node.text or "").strip()
        if text:
            out.append("#" + text)
        for kid in node:
            walk(kid, out)
        out.append("/")

    acc: list[str] = []
    walk(elem, acc)
    return hashlib.sha1("\n".join(acc).encode("utf-8")).hexdigest()[:16]


def strip_urns(elem: ET.Element) -> int:
    """Drop managed-library URNs, converting cloud references to local copies.

    A merged design mixes libraries that EAGLE would otherwise try to re-sync
    against their originals, which fails once we rename colliding items.
    """
    removed = 0
    attrs = ("urn", "library_urn", "library_version", "deviceset_urn",
             "package_urn", "symbol_urn", "package3d_urn")
    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        for attr in attrs:
            if attr in node.attrib:
                del node.attrib[attr]
                removed += 1
    return removed


def clone(elem: ET.Element) -> ET.Element:
    return copy.deepcopy(elem)


_UNSAFE = re.compile(r"[^A-Za-z0-9_$\-.+]")


def sanitize_name(name: str) -> str:
    """Coerce arbitrary text into something EAGLE accepts as a name."""
    cleaned = _UNSAFE.sub("_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "X"


def unique_name(base: str, taken: set[str], sep: str = "$") -> str:
    """First free name in the base, base$2, base$3 ... sequence."""
    if base not in taken:
        return base
    n = 2
    while f"{base}{sep}{n}" in taken:
        n += 1
    return f"{base}{sep}{n}"


# Every extension the tool treats as one half of a design.  Longest first, so
# `.kicad_sch` is recognised before anything shorter could match inside it.
DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb", ".sch", ".brd")


def design_stem(path: str | Path) -> Path:
    """A design's path with its extension removed.

    `Path.with_suffix("")` cannot be used for this: it strips from the last dot,
    so a board called `XIAO ESP32S3_V1.5.kicad_pcb` would lose the `.5` and every
    file beside it would then be looked for under the wrong name.
    """
    path = Path(path)
    name = path.name
    for suffix in DESIGN_SUFFIXES:
        if name.lower().endswith(suffix):
            return path.with_name(name[: -len(suffix)])
    return path


def with_ext(stem: str | Path, extension: str) -> Path:
    """The path of one half of a design, named by adding an extension.

    Appends rather than replaces, for the same reason `design_stem` exists.
    """
    return Path(f"{stem}{extension}")


def iter_named(container: ET.Element | None) -> Iterator[ET.Element]:
    if container is None:
        return iter(())
    return (node for node in container if isinstance(node.tag, str) and node.get("name") is not None)
