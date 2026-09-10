"""Combining several EAGLE designs into one schematic and one board.

The merge is driven entirely by rename maps computed up front: one for library
items, one for reference designators, one for nets, one for net classes.  Both
output files are then rebuilt using the same maps, which is what keeps the
schematic and the board consistent enough for EAGLE to open them as a pair.

A design may be instantiated more than once.  Each copy is an independent
instance with its own numbered prefix, so parts and design-local nets increment
together while shared rails still collapse into one net.

Every design lands on one schematic sheet.  Multi-sheet output was tried and
withdrawn: EAGLE 9.6.2 would not reliably open it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import kicad, layout, pruning
from .eagle import (
    EagleDoc, EagleError, bbox, clone, content_hash, fmt, rotate, strip_urns,
    tidy, translate, unique_name,
)
from .libraries import LibraryMerger, apply_to_element, apply_to_part
from .nets import Action, NetResolver, plan_design_names
from .plan import InstanceSpec, MergePlan

# Devicesets that only draw a page border.  Packing several designs onto one
# sheet has to drop them or the borders overlap into noise.
FRAME_HINT = "FRAME"

# EAGLE layer 97 is Info: annotation that is not part of the netlist.  The
# captions naming each block on a shared sheet belong there.
INFO_LAYER = "97"
CAPTION_SIZE = "5.08"
CAPTION_GAP = 5.0

# Past roughly a metre and a half a single sheet stops being something anyone
# can navigate or print, so the merge says so rather than silently producing it.
UNWIELDY_SHEET = 1500.0

# Margin between a fixed board outline and the sub-boards placed inside it.
OUTLINE_MARGIN = 2.0
OUTLINE_WIDTH = "0"

# How much of a board the preview describes.  Enough to draw any outline
# someone would recognise and to dot every part on a dense board, and a cap
# so a pathological file does not turn every click into a megabyte.
PREVIEW_SHAPES = 400
PREVIEW_PARTS = 600


@dataclass
class Design:
    """One loaded design instance and every rename that applies to it."""

    spec: InstanceSpec
    sch: EagleDoc
    brd: EagleDoc | None = None
    part_map: dict[str, str] = field(default_factory=dict)
    net_map: dict[str, str] = field(default_factory=dict)
    class_map: dict[str, str] = field(default_factory=dict)
    dropped_parts: set[str] = field(default_factory=set)
    dropped_nets: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)   # e.g. how it was converted

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def source(self) -> str:
        return self.spec.source

    @property
    def prefix(self) -> str:
        return self.spec.prefix


@dataclass
class MergeReport:
    designs: list[str] = field(default_factory=list)
    copies: dict[str, int] = field(default_factory=dict)
    parts: int = 0
    elements: int = 0
    sheets: int = 0
    signals: int = 0
    joined_nets: list[tuple[str, list[str]]] = field(default_factory=list)
    linked_nets: list[tuple[str, list[str]]] = field(default_factory=list)
    renamed_parts: int = 0
    renamed_library_items: int = 0
    dropped_urns: int = 0
    dropped_frames: int = 0
    dropped_parts: int = 0
    converted: list[str] = field(default_factory=list)
    sheet_extent: tuple[float, float, float, float] | None = None
    placed_by_hand: list[str] = field(default_factory=list)
    outline: tuple[float, float] | None = None
    placements: list[layout.Placement] = field(default_factory=list)
    before: layout.LayoutStats | None = None
    after: layout.LayoutStats | None = None
    warnings: list[str] = field(default_factory=list)
    sch_path: Path | None = None
    brd_path: Path | None = None


def load_designs(instances: list[InstanceSpec],
                 cache: dict[str, EagleDoc] | None = None,
                 notes: list[str] | None = None) -> list[Design]:
    """Load each instance, parsing every source file only once.

    KiCad designs are converted here rather than anywhere later, so the rest of
    the engine only ever sees EAGLE documents.

    Pass a `cache` to keep parsed documents between calls.  A long-running
    caller re-analysing the same designs after every edit would otherwise
    re-read several megabytes each time.
    """
    if cache is None:
        cache = {}

    def stamp(path: Path) -> str:
        return f"{path}|{path.stat().st_mtime_ns}"

    def get(path: Path, kind: str) -> EagleDoc:
        key = stamp(path)
        doc = cache.get(key)
        if doc is None:
            doc = EagleDoc.load(path)
            if doc.kind != kind:
                raise EagleError(f"{path.name}: expected a {kind} file")
            cache[key] = doc
        return doc

    def converted(spec: InstanceSpec, told: list[str]) -> tuple[EagleDoc, EagleDoc]:
        board = spec.brd_path if spec.brd_path is not None else spec.sch_path
        stem = kicad.design_stem(board)
        key = f"kicad|{stamp(board)}"
        pair = cache.get(key)
        if pair is None:
            result = kicad.convert(stem)
            pair = (result.schematic, result.board, result.notes)
            cache[key] = pair
        told.extend(pair[2])
        if notes is not None:
            notes.extend(n for n in pair[2] if n not in notes)
        return pair[0], pair[1]

    designs: list[Design] = []
    for spec in instances:
        told: list[str] = []
        if kicad.is_kicad(spec.sch_path) or (
                spec.brd_path is not None and kicad.is_kicad(spec.brd_path)):
            sch, brd = converted(spec, told)
        else:
            sch = get(spec.sch_path, "sch")
            brd = get(spec.brd_path, "brd") if spec.brd_path is not None else None
        designs.append(Design(spec=spec, sch=sch, brd=brd, notes=told))
    return designs


def build_resolver(designs: list[Design]) -> NetResolver:
    resolver = NetResolver()
    for design in designs:
        names = list(design.sch.net_names())
        if design.brd is not None:
            for name in design.brd.net_names():
                if name not in names:
                    names.append(name)
        resolver.add(design.name, names, source=design.source)
    return resolver


class Merger:
    """Runs a merge from loaded designs plus a finalized net resolver."""

    def __init__(self, designs: list[Design], resolver: NetResolver, plan: MergePlan):
        self.designs = designs
        self.resolver = resolver
        self.plan = plan
        self.libs = LibraryMerger()
        self.report = MergeReport(designs=[d.name for d in designs])
        self._classes: list[ET.Element] = []
        self._class_index: dict[str, str] = {}  # content hash -> class number

    # -- planning -----------------------------------------------------------
    def prepare(self) -> None:
        for design in self.designs:
            self.report.copies[design.source] = self.report.copies.get(design.source, 0) + 1
            for note in design.notes:
                if note not in self.report.converted:
                    self.report.converted.append(note)
        self._apply_drops()
        self._merge_libraries()
        self._map_parts()
        self._map_classes()
        self._map_nets()

    def _apply_drops(self) -> None:
        """Decide what is left out, before anything is given a new name.

        Dropping happens first so removed parts never claim a designator, and
        so the same decision reaches the schematic and the board together.
        """
        rules = [pruning.parse_drop(text) for text in self.plan.drops]
        for design, names in pruning.resolve(self.designs, rules).items():
            by_name = {d.name: d for d in self.designs}
            if design in by_name:
                by_name[design].dropped_parts |= names

        if len(self.designs) > 1:
            for design in self.designs:
                frames = self._frame_parts(design)
                design.dropped_parts |= frames
                self.report.dropped_frames += len(frames)

        for design in self.designs:
            design.dropped_nets = self._nets_left_empty(design)

        for design, name, pins in pruning.connections_lost(self.designs, self._drops()):
            self.report.warnings.append(
                f"dropped {design}:{name}, which had {pins} connection(s)")
        self.report.dropped_parts = sum(len(d.dropped_parts) for d in self.designs)

    def _drops(self) -> dict[str, set[str]]:
        return {d.name: d.dropped_parts for d in self.designs if d.dropped_parts}

    def _nets_left_empty(self, design: Design) -> set[str]:
        """Nets whose every pin belonged to a part being dropped.

        Decided once, from the schematic, and then applied to the board as
        well.  Letting each file work it out separately is how you end up with
        a board signal that no schematic net matches, which EAGLE rejects.
        A net that never had pins is left alone: it came that way.
        """
        if not design.dropped_parts:
            return set()
        pins: dict[str, list[str]] = {}
        for net in design.sch.nets():
            name = net.get("name", "")
            for pinref in net.iterfind(".//pinref"):
                pins.setdefault(name, []).append(pinref.get("part", ""))
        return {name for name, parts in pins.items()
                if parts and all(p in design.dropped_parts for p in parts)}

    def _merge_libraries(self) -> None:
        for design in self.designs:
            docs = [design.sch] + ([design.brd] if design.brd else [])
            libs: list[ET.Element] = []
            for doc in docs:
                libs.extend(doc.libraries())
            renames = self.libs.add_design(design.name, libs)
            self.report.renamed_library_items += renames.count

    def _map_parts(self) -> None:
        """Give every part and element a design-prefixed reference designator."""
        taken: set[str] = set()
        for design in self.designs:
            names: list[str] = [p.get("name", "") for p in design.sch.parts()]
            if design.brd is not None:
                for element in design.brd.elements():
                    name = element.get("name", "")
                    if name not in names:
                        names.append(name)
            for original in names:
                if not original or original in design.dropped_parts:
                    continue
                base = f"{design.prefix}{original}" if design.prefix else original
                final = unique_name(base, taken)
                taken.add(final)
                design.part_map[original] = final
                if final != original:
                    self.report.renamed_parts += 1

    def _map_classes(self) -> None:
        """Merge net classes by content, remapping each design's numbers."""
        for design in self.designs:
            docs = [design.sch] + ([design.brd] if design.brd else [])
            for doc in docs:
                container = doc.section.find("classes")
                if container is None:
                    continue
                for cls in container:
                    if not isinstance(cls.tag, str) or cls.tag != "class":
                        continue
                    number = cls.get("number", "0")
                    if number in design.class_map:
                        continue
                    digest = content_hash(cls, ignore=("number",))
                    known = self._class_index.get(digest)
                    if known is None:
                        known = str(len(self._classes))
                        copy = clone(cls)
                        copy.set("number", known)
                        self._classes.append(copy)
                        self._class_index[digest] = known
                    design.class_map[number] = known

    def _map_nets(self) -> None:
        taken: set[str] = set()
        for design in self.designs:
            for group in self.resolver.all_groups():
                if design.name not in group.occurrences:
                    continue
                design.net_map.update(
                    plan_design_names(group, design.name, design.prefix, taken))

        for group in self.resolver.joined():
            entry = (group.merged_name or group.display, group.designs)
            if group.decided_by == "link":
                self.report.linked_nets.append((entry[0], group.spellings))
            self.report.joined_nets.append(entry)

    # -- schematic ----------------------------------------------------------
    def build_schematic(self) -> EagleDoc:
        base = self.designs[0].sch
        shared = len(self.designs) > 1
        tiles = self._sheet_tiles() if shared else {}

        root = ET.Element("eagle", {"version": base.version})
        root.text = "\n"
        drawing = ET.SubElement(root, "drawing")
        drawing.text = "\n"
        for tag in ("settings", "grid"):
            node = base.drawing.find(tag)
            if node is not None:
                drawing.append(clone(node))
        drawing.append(self._merged_layers("sch"))

        schematic = ET.SubElement(drawing, "schematic")
        schematic.text = "\n"
        schematic.set("xreflabel", base.section.get("xreflabel", "%F%N/%S.%C%R"))
        schematic.set("xrefpart", base.section.get("xrefpart", "/%S.%C%R"))

        schematic.append(self.libs.build())
        schematic.append(self._merged_attributes([d.sch for d in self.designs]))
        schematic.append(ET.Element("variantdefs"))
        schematic.append(self._classes_element())

        parts = ET.SubElement(schematic, "parts")
        parts.text = "\n"
        sheets_node = ET.SubElement(schematic, "sheets")
        sheets_node.text = "\n"

        for design in self.designs:
            renames = self.libs.renames_by_design[design.name]
            for part in design.sch.parts():
                original = part.get("name", "")
                if original in design.dropped_parts:
                    continue
                node = clone(part)
                node.set("name", design.part_map.get(original, original))
                apply_to_part(node, renames)
                parts.append(node)
                self.report.parts += 1

        sheets_node.append(self._one_sheet(tiles))
        self.report.sheets = 1

        doc = EagleDoc(path=Path("merged.sch"), tree=ET.ElementTree(root), kind="sch")
        self.report.dropped_urns += strip_urns(root)
        tidy(root)
        return doc

    def _sheet_body(self, design: Design, sheet: ET.Element,
                    offset: tuple[float, float]) -> list[ET.Element]:
        """The plain / instances / busses / nets of one design's sheet.

        Only the containers EAGLE expects on a sheet, and only ones the source
        actually has.  An empty <moduleinsts/> here, which no hand-drawn file
        carries, was enough to stop EAGLE opening the result.
        """
        out: list[ET.Element] = []
        for tag in ("plain", "instances", "busses"):
            source = sheet.find(tag)
            copy = clone(source) if source is not None else ET.Element(tag)
            if tag == "instances":
                for instance in list(copy):
                    if not isinstance(instance.tag, str):
                        continue
                    part = instance.get("part", "")
                    if part in design.dropped_parts:
                        copy.remove(instance)
                        continue
                    instance.set("part", design.part_map.get(part, part))
            out.append(copy)

        nets = ET.Element("nets")
        nets.text = "\n"
        source_nets = sheet.find("nets")
        if source_nets is not None:
            for net in source_nets:
                if not isinstance(net.tag, str) or net.tag != "net":
                    continue
                copy = clone(net)
                original = net.get("name", "")
                copy.set("name", design.net_map.get(original, original))
                if original in design.dropped_nets:
                    continue
                copy.set("class", design.class_map.get(net.get("class", "0"), "0"))
                self._rewrite_net_body(copy, design)
                nets.append(copy)
        out.append(nets)

        if offset != (0.0, 0.0):
            for node in out:
                translate(node, offset[0], offset[1])
        return out

    def _rewrite_net_body(self, net: ET.Element, design: Design) -> None:
        """Point pin references at renamed parts, dropping any that are gone.

        Net labels need no edit: EAGLE draws them from the net's own name.
        """
        for segment in net:
            if not isinstance(segment.tag, str):
                continue
            for pinref in list(segment.iterfind("pinref")):
                part = pinref.get("part", "")
                if part in design.dropped_parts:
                    segment.remove(pinref)
                    continue
                pinref.set("part", design.part_map.get(part, part))

    # -- schematic packing --------------------------------------------------
    def _sheet_tiles(self) -> dict[str, tuple[float, float]]:
        """Where each design's drawing goes on the shared sheet.

        Anything placed by hand keeps the corner it was dropped at; the rest
        is packed around it as before.
        """
        sizes = self._sheet_sizes()
        tiles = layout.sheet_tiles(sizes)
        known = {d.name for d in self.designs}
        # A drawing never turns, so only the corner of a sheet spot is used.
        tiles.update({name: (spot[0], spot[1])
                      for name, spot in self.plan.spots("sheet").items()
                      if name in known})
        return tiles

    def _sheet_sizes(self) -> list[tuple[str, float, float]]:
        """Each design's drawing, with room above it for its caption."""
        sizes: list[tuple[str, float, float]] = []
        for design in self.designs:
            box = self._sheet_extent(design)
            sizes.append((design.name, box[2] - box[0],
                          box[3] - box[1] + CAPTION_GAP + float(CAPTION_SIZE)))
        return sizes

    def _sheet_extent(self, design: Design) -> tuple[float, float, float, float]:
        """Bounds of a design's schematic, ignoring its page border."""
        probe = ET.Element("probe")
        frames = self._frame_parts(design)
        for sheet in design.sch.sheets():
            instances = sheet.find("instances")
            if instances is not None:
                for instance in instances:
                    if isinstance(instance.tag, str) and instance.get("part") not in frames:
                        probe.append(instance)
            nets = sheet.find("nets")
            if nets is not None:
                probe.append(nets)
        box = bbox(probe)
        return box or (0.0, 0.0, 0.0, 0.0)

    def _frame_parts(self, design: Design) -> set[str]:
        """Parts whose deviceset only draws a page border."""
        out: set[str] = set()
        for part in design.sch.parts():
            if FRAME_HINT in (part.get("deviceset", "") or "").upper():
                out.add(part.get("name", ""))
        return out

    def _one_sheet(self, tiles: dict[str, tuple[float, float]]) -> ET.Element:
        """Build the single sheet that carries every design.

        Each design's drawing is translated to its tile and its blocks are
        captioned, so one page stays navigable.  Nets of the same name fold
        together, because two elements named GND on one sheet is not a form
        EAGLE accepts.
        """
        page = ET.Element("sheet")
        page.text = "\n"
        page.tail = "\n"
        description = ET.SubElement(page, "description")
        description.tail = "\n"
        for tag in ("plain", "instances", "busses", "nets"):
            node = ET.SubElement(page, tag)
            node.text = "\n"
            node.tail = "\n"

        labels: list[str] = []
        named: dict[tuple[str, str], ET.Element] = {}

        for design in self.designs:
            box = self._sheet_extent(design)
            if tiles:
                tile_x, tile_y = tiles.get(design.name, (0.0, 0.0))
                offset = (tile_x - box[0], tile_y - box[1])
            else:
                # A lone design has nothing to make room for, so it keeps the
                # coordinates it was drawn at and looks exactly as it did.
                tile_x, tile_y = box[0], box[1]
                offset = (0.0, 0.0)
            labels.append(design.name)
            if tiles:
                page.find("plain").append(self._caption(
                    design.name, tile_x, tile_y + (box[3] - box[1]) + CAPTION_GAP))

            for source_sheet in design.sch.sheets():
                for part in self._sheet_body(design, source_sheet, offset):
                    target = page.find(part.tag)
                    if target is None:
                        continue
                    if part.tag in ("nets", "busses"):
                        _absorb_named(target, part, named)
                    else:
                        for node in list(part):
                            target.append(node)

        description.text = ", ".join(labels)
        extent = bbox(page) or (0.0, 0.0, 0.0, 0.0)
        self.report.sheet_extent = extent
        width, height = extent[2] - extent[0], extent[3] - extent[1]
        if max(width, height) > UNWIELDY_SHEET:
            self.report.warnings.append(
                f"the schematic sheet is {width:.0f} x {height:.0f} mm, which is "
                f"awkward to navigate and to print; merge fewer designs at once")
        return page

    def _caption(self, text: str, x: float, y: float) -> ET.Element:
        """A name above a block, so one crowded sheet stays navigable."""
        node = ET.Element("text", {
            "x": fmt(x), "y": fmt(y), "size": CAPTION_SIZE,
            "layer": INFO_LAYER, "ratio": "12", "align": "bottom-left",
        })
        node.text = text
        node.tail = "\n"
        return node

    # -- preview ------------------------------------------------------------
    def sheet_preview(self) -> list[dict]:
        """Where each drawing would sit on the shared sheet.

        The same tiling the schematic build uses, reported rather than drawn,
        so the page can show the sheet and let someone rearrange it.  A lone
        design is not tiled at all -- it keeps the coordinates it was drawn
        at -- so the preview says the same.
        """
        sizes = self._sheet_sizes()
        tiles = self._sheet_tiles() if len(self.designs) > 1 else {}
        out: list[dict] = []
        for design, (_, width, height) in zip(self.designs, sizes):
            box = self._sheet_extent(design)
            x, y = tiles.get(design.name, (box[0], box[1]))
            symbols, more = self._symbol_spots(design, (x - box[0], y - box[1]))
            out.append({"design": design.name, "x": x, "y": y,
                        "width": width, "height": height,
                        "symbols": symbols, "more": more})
        return out

    def _symbol_spots(self, design: Design,
                      offset: tuple[float, float]) -> tuple[list[dict], bool]:
        """Where each symbol sits on the shared sheet, so a block has a face.

        The same offset the sheet build applies, so a dot in the preview is
        where the symbol will be drawn.  Page borders are left out, as they
        are from the sheet itself.
        """
        frames = self._frame_parts(design)
        out: list[dict] = []
        for sheet in design.sch.sheets():
            for instance in sheet.iterfind("instances/instance"):
                part = instance.get("part", "")
                if part in frames or part in design.dropped_parts:
                    continue
                try:
                    x = float(instance.get("x", "0")) + offset[0]
                    y = float(instance.get("y", "0")) + offset[1]
                except ValueError:
                    continue
                out.append({"name": part, "x": x, "y": y})
        return out[:PREVIEW_PARTS], len(out) > PREVIEW_PARTS

    def _outline_shapes(self, design: Design, place: layout.Placement) -> list[dict]:
        """The board's own outline, where its placement puts it.

        Wires, with their arcs, circles and polygons on the Dimension layer,
        each point put through the placement the way the copper will be.  The
        preview draws these inside the block so a board is recognisable by
        its shape and not only by its name.
        """
        plain = design.brd.section.find("plain") if design.brd is not None else None
        out: list[dict] = []
        if plain is None:
            return out
        for node in plain:
            if not isinstance(node.tag, str) or node.get("layer") != layout.DIMENSION_LAYER:
                continue
            try:
                if node.tag == "wire":
                    x1, y1 = place.transform(float(node.get("x1")), float(node.get("y1")))
                    x2, y2 = place.transform(float(node.get("x2")), float(node.get("y2")))
                    shape = {"t": "w", "x1": x1, "y1": y1, "x2": x2, "y2": y2}
                    if node.get("curve"):
                        shape["curve"] = float(node.get("curve"))
                    out.append(shape)
                elif node.tag == "circle":
                    x, y = place.transform(float(node.get("x")), float(node.get("y")))
                    out.append({"t": "c", "x": x, "y": y, "r": float(node.get("radius"))})
                elif node.tag == "polygon":
                    points = [place.transform(float(v.get("x")), float(v.get("y")))
                              for v in node.iterfind("vertex")]
                    if len(points) > 2:
                        out.append({"t": "p", "points": [{"x": x, "y": y} for x, y in points]})
            except (TypeError, ValueError):
                continue
            if len(out) >= PREVIEW_SHAPES:
                break
        return out

    def _part_spots(self, design: Design,
                    place: layout.Placement) -> tuple[list[dict], bool]:
        """Where each footprint's origin lands, by the part's own name."""
        out: list[dict] = []
        for element in design.brd.elements():
            name = element.get("name", "")
            if name in design.dropped_parts:
                continue
            try:
                x, y = place.transform(float(element.get("x", "0")), float(element.get("y", "0")))
            except ValueError:
                continue
            out.append({"name": name, "x": x, "y": y})
        return out[:PREVIEW_PARTS], len(out) > PREVIEW_PARTS

    def preview(self) -> dict:
        """Where the boards would land, and what would still need routing.

        Runs the same placement the board build runs, but stops before any
        XML is produced, so a caller can show the arrangement and let someone
        change their mind cheaply.
        """
        boards = [d for d in self.designs if d.brd is not None]
        self.report.outline = layout.parse_outline(self.plan.outline)
        if not boards:
            return {"outline": self.report.outline, "placements": [],
                    "airwires": [], "sheet": self.sheet_preview()}

        placements = self._place(boards)
        offsets = {p.design: p for p in placements}

        points: dict[str, list[dict]] = {}
        centroids: dict[str, dict[str, tuple[float, float]]] = {}
        for design in boards:
            place = offsets[design.name]
            centroids[design.name] = {}
            for net, (x, y) in self._net_centroids(design).items():
                x, y = place.transform(x, y)
                centroids[design.name][net] = (x, y)
                points.setdefault(net, []).append(
                    {"design": design.name, "x": x, "y": y})

        airwires = [{"net": net, "points": spots}
                    for net, spots in points.items() if len(spots) > 1]
        airwires.sort(key=lambda a: -len(a["points"]))

        # Where each undecided net sits on every board it touches.  A split
        # net has no airwire to point at, so this is what lets someone see
        # what a join would connect before choosing it.
        by_name = {d.name: d for d in boards}
        marks: dict[str, list[dict]] = {}
        for group in self.resolver.open_questions() + self.resolver.replica_questions():
            for ref in group.refs:
                design = by_name.get(ref.design)
                if design is None:
                    continue
                final = design.net_map.get(ref.raw, ref.raw)
                spot = centroids[design.name].get(final)
                if spot is not None:
                    marks.setdefault(group.key, []).append(
                        {"design": design.name, "x": spot[0], "y": spot[1]})

        faces: dict[str, dict] = {}
        for design in boards:
            place = offsets[design.name]
            parts, more = self._part_spots(design, place)
            faces[design.name] = {"outline": self._outline_shapes(design, place),
                                  "parts": parts, "more": more}
        return {
            "outline": self.report.outline,
            "placements": placements,
            "faces": faces,
            "airwires": airwires,
            "marks": marks,
            "sheet": self.sheet_preview(),
        }

    # -- board --------------------------------------------------------------
    def build_board(self) -> EagleDoc | None:
        boards = [d for d in self.designs if d.brd is not None]
        if not boards:
            return None
        base = boards[0].brd

        self.report.outline = layout.parse_outline(self.plan.outline)
        keep_outlines = layout.keeps_source_outlines(self.plan.outline)
        placements = self._place(boards)
        by_design = {p.design: p for p in placements}
        self.report.placements = placements

        root = ET.Element("eagle", {"version": base.version})
        root.text = "\n"
        drawing = ET.SubElement(root, "drawing")
        drawing.text = "\n"
        for tag in ("settings", "grid"):
            node = base.drawing.find(tag)
            if node is not None:
                drawing.append(clone(node))
        drawing.append(self._merged_layers("brd"))

        board = ET.SubElement(drawing, "board")
        board.text = "\n"

        plain = ET.SubElement(board, "plain")
        plain.text = "\n"
        outline = self.report.outline
        for design in boards:
            source = design.brd.section.find("plain")
            if source is None:
                continue
            copy = clone(source)
            place = by_design[design.name]
            _move(copy, place)
            for node in list(copy):
                if not keep_outlines and _is_outline(node):
                    # The merged board gets one outline of its own; carrying
                    # over each sub-board's would leave a pile of rectangles.
                    continue
                plain.append(node)
        if outline is not None:
            for wire in _outline_wires(*outline):
                plain.append(wire)

        board.append(self.libs.build())
        board.append(self._merged_attributes([d.brd for d in boards]))
        board.append(ET.Element("variantdefs"))
        board.append(self._classes_element())

        for tag in ("designrules", "autorouter"):
            node = base.section.find(tag)
            if node is not None:
                board.append(clone(node))

        elements = ET.SubElement(board, "elements")
        elements.text = "\n"
        signals = ET.SubElement(board, "signals")
        signals.text = "\n"

        merged_signals: dict[str, ET.Element] = {}
        for design in boards:
            place = by_design[design.name]
            renames = self.libs.renames_by_design[design.name]
            for element in design.brd.elements():
                original = element.get("name", "")
                if original in design.dropped_parts:
                    continue
                node = clone(element)
                node.set("name", design.part_map.get(original, original))
                apply_to_element(node, renames)
                _move(node, place)
                elements.append(node)
                self.report.elements += 1

            for signal in design.brd.signals():
                original = signal.get("name", "")
                if original in design.dropped_nets:
                    continue
                final = design.net_map.get(original, original)
                copy = clone(signal)
                copy.set("name", final)
                cls = signal.get("class")
                if cls is not None:
                    copy.set("class", design.class_map.get(cls, "0"))
                _rewrite_signal_body(copy, design)
                _move(copy, place)

                if final in merged_signals:
                    # A joined net: fold this design's copper into the existing
                    # signal.  The pieces stay unrouted between boards, which is
                    # exactly the airwire the engineer needs to see.
                    target = merged_signals[final]
                    for node in list(copy):
                        target.append(node)
                else:
                    merged_signals[final] = copy
                    signals.append(copy)
                    self.report.signals += 1

        source = base.section.find("mfgpreviewcolors")
        if source is not None:
            board.append(clone(source))

        doc = EagleDoc(path=Path("merged.brd"), tree=ET.ElementTree(root), kind="brd")
        self.report.dropped_urns += strip_urns(root)
        tidy(root)
        return doc

    def _place(self, boards: list[Design]) -> list[layout.Placement]:
        """Choose where each board goes, optimising if asked to.

        With a fixed outline the sub-boards are packed to its width and laid
        out from its top-left corner, so they land inside the board you are
        actually going to have made.
        """
        measured = layout.measure([(d.name, d.brd.section) for d in boards])
        centroids = {d.name: self._net_centroids(d) for d in boards}

        outline = self.report.outline
        if outline is not None:
            origin = (OUTLINE_MARGIN, outline[1] - OUTLINE_MARGIN)
            max_width = outline[0] - 2 * OUTLINE_MARGIN
        else:
            origin, max_width = (0.0, 0.0), 0.0

        placements, before, after = layout.optimize(
            measured, centroids,
            style=self.plan.layout, gap=self.plan.gap, columns=self.plan.columns,
            goal=self.plan.optimize, origin=origin, max_width=max_width,
        )

        # Hand placements are applied after the search rather than constraining
        # it, so the boards left to the packer are still arranged well among
        # themselves.  The reported cost is then measured from what actually
        # results, not from the arrangement the search settled on.
        spots = self.plan.spots("board")
        if spots:
            placements = layout.pin(placements, measured, spots)
            after = layout.stats(placements, measured, centroids)
            self.report.placed_by_hand = sorted(
                spots.keys() & {p.design for p in placements})

        self.report.before = before
        self.report.after = after
        self._check_fit(placements)
        return placements

    def _check_fit(self, placements: list[layout.Placement]) -> None:
        """Say so when the sub-boards do not fit the outline they were given."""
        outline = self.report.outline
        if outline is None or not placements:
            return
        right = max(p.x + p.width for p in placements)
        bottom = min(p.y for p in placements)
        over_x = right - (outline[0] - OUTLINE_MARGIN)
        over_y = OUTLINE_MARGIN - bottom
        if over_x > 0.01 or over_y > 0.01:
            self.report.warnings.append(
                f"the sub-boards need {right + OUTLINE_MARGIN:.0f} x "
                f"{outline[1] - bottom + OUTLINE_MARGIN:.0f} mm and overflow the "
                f"{outline[0]:.0f} x {outline[1]:.0f} mm outline; give --outline a "
                f"bigger size or move them by hand")

    def _net_centroids(self, design: Design) -> dict[str, tuple[float, float]]:
        """Where each merged net sits on this board, in its own coordinates.

        Element origins stand in for pad positions.  That is accurate enough
        to rank arrangements, and avoids resolving every package's pad
        geometry through its rotation.
        """
        origins: dict[str, tuple[float, float]] = {}
        for element in design.brd.elements():
            try:
                origins[element.get("name", "")] = (
                    float(element.get("x", "0")), float(element.get("y", "0")))
            except ValueError:
                continue

        out: dict[str, tuple[float, float]] = {}
        for signal in design.brd.signals():
            points = [origins[c.get("element", "")]
                      for c in signal.iterfind(".//contactref")
                      if c.get("element", "") in origins]
            if not points:
                continue
            name = design.net_map.get(signal.get("name", ""), signal.get("name", ""))
            out[name] = (sum(p[0] for p in points) / len(points),
                         sum(p[1] for p in points) / len(points))
        return out

    # -- shared pieces ------------------------------------------------------
    def _merged_layers(self, kind: str) -> ET.Element:
        """Union of the layer definitions of one kind of drawing.

        Layer tables are not interchangeable.  A schematic marks the copper
        layers `visible="no" active="no"` because it has no use for them; a
        board needs exactly those layers switched on.  Taking the schematic's
        table for the board hides every footprint.
        """
        node = ET.Element("layers")
        node.text = "\n"
        seen: dict[int, ET.Element] = {}
        for design in self.designs:
            docs = [design.sch] if kind == "sch" else (
                [design.brd] if design.brd is not None else [])
            for doc in docs:
                for layer in doc.layers():
                    if not isinstance(layer.tag, str):
                        continue
                    try:
                        number = int(layer.get("number", "0"))
                    except ValueError:
                        continue
                    if number not in seen:
                        seen[number] = clone(layer)
        for number in sorted(seen):
            node.append(seen[number])
        return node

    def _merged_attributes(self, docs: list[EagleDoc]) -> ET.Element:
        """Global attributes, first definition wins; clashes are reported."""
        node = ET.Element("attributes")
        node.text = "\n"
        seen: dict[str, str] = {}
        for doc in docs:
            container = doc.section.find("attributes")
            if container is None:
                continue
            for attribute in container:
                if not isinstance(attribute.tag, str):
                    continue
                name = attribute.get("name", "")
                value = attribute.get("value", "")
                if name in seen:
                    if seen[name] != value:
                        message = (f"global attribute {name} differs between designs; "
                                   f"kept {seen[name]!r}, dropped {value!r}")
                        if message not in self.report.warnings:
                            self.report.warnings.append(message)
                    continue
                seen[name] = value
                node.append(clone(attribute))
        return node

    def _classes_element(self) -> ET.Element:
        node = ET.Element("classes")
        node.text = "\n"
        if not self._classes:
            default = ET.SubElement(node, "class",
                                    {"number": "0", "name": "default", "width": "0", "drill": "0"})
            default.tail = "\n"
            return node
        for cls in self._classes:
            node.append(clone(cls))
        return node


def _move(node: ET.Element, place: layout.Placement) -> None:
    """Put a piece of board geometry where its placement says.

    The turn comes first, about the corner the board was drawn with, and the
    shift after; `Placement.transform` does the same to a single point, so a
    centroid and the copper it stands for always land together.
    """
    rotate(node, place.rotation, place.pivot_x, place.pivot_y)
    translate(node, place.dx, place.dy)


def _is_outline(node: ET.Element) -> bool:
    """Board-shape geometry, which the merged board replaces with its own."""
    return (isinstance(node.tag, str)
            and node.tag in ("wire", "circle", "rectangle", "polygon")
            and node.get("layer") == layout.DIMENSION_LAYER)


def _outline_wires(width: float, height: float) -> list[ET.Element]:
    """A plain rectangle on the Dimension layer, starting at the origin."""
    corners = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height), (0.0, 0.0)]
    wires: list[ET.Element] = []
    for (x1, y1), (x2, y2) in zip(corners, corners[1:]):
        wire = ET.Element("wire", {
            "x1": fmt(x1), "y1": fmt(y1), "x2": fmt(x2), "y2": fmt(y2),
            "width": OUTLINE_WIDTH, "layer": layout.DIMENSION_LAYER,
        })
        wire.tail = "\n"
        wires.append(wire)
    return wires


def _rewrite_signal_body(signal: ET.Element, design: Design) -> None:
    """Point contacts at renamed elements, dropping any that are gone.

    Copper belonging to a signal that survives is kept even when one of its
    parts went away, because it is real routing the engineer can see and
    delete.  Whether the signal itself survives was already settled on the
    schematic side, so the two files cannot disagree.
    """
    for contact in list(signal.iterfind("contactref")):
        element = contact.get("element", "")
        if element in design.dropped_parts:
            signal.remove(contact)
            continue
        contact.set("element", design.part_map.get(element, element))


def _absorb_named(target: ET.Element, source: ET.Element,
                  seen: dict[tuple[str, str], ET.Element]) -> None:
    """Append nets or busses to a sheet, folding same-named ones together.

    EAGLE writes one <net> per name per sheet, carrying several <segment>
    children.  When designs share a page their joined nets meet, and two
    elements with the same name on one sheet is not a form EAGLE accepts --
    the segments have to go into a single net instead.
    """
    for node in list(source):
        if not isinstance(node.tag, str):
            continue
        key = (node.tag, node.get("name", ""))
        existing = seen.get(key)
        if existing is None:
            seen[key] = node
            target.append(node)
            continue
        for segment in list(node):
            existing.append(segment)


def merge(designs: list[Design], resolver: NetResolver, plan: MergePlan,
          out_dir: Path, stem: str) -> MergeReport:
    """Run the merge and write both output files."""
    merger = Merger(designs, resolver, plan)
    merger.prepare()

    schematic = merger.build_schematic()
    merger.report.sch_path = schematic.save(out_dir / f"{stem}.sch")

    board = merger.build_board()
    if board is not None:
        merger.report.brd_path = board.save(out_dir / f"{stem}.brd")
    else:
        merger.report.warnings.append("no board files supplied; wrote a schematic only")

    _sanity_check(merger)
    return merger.report


def _sanity_check(merger: Merger) -> None:
    """Catch the mistakes that make EAGLE refuse to open a file pair."""
    report = merger.report
    part_names: set[str] = set()
    for design in merger.designs:
        for final in design.part_map.values():
            if final in part_names:
                report.warnings.append(f"duplicate reference designator {final}")
            part_names.add(final)
