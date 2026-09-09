"""Combining several EAGLE designs into one schematic and one board.

The merge is driven entirely by rename maps computed up front: one for library
items, one for reference designators, one for nets, one for net classes.  Both
output files are then rebuilt using the same maps, which is what keeps the
schematic and the board consistent enough for EAGLE to open them as a pair.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import layout
from .eagle import (
    EagleDoc, EagleError, clone, content_hash, strip_urns, tidy, translate,
    unique_name,
)
from .libraries import LibraryMerger, apply_to_element, apply_to_part
from .nets import Action, NetResolver, plan_design_names
from .plan import DesignSpec, MergePlan


@dataclass
class Design:
    """One loaded input design and every rename that applies to it."""

    spec: DesignSpec
    sch: EagleDoc
    brd: EagleDoc | None = None
    part_map: dict[str, str] = field(default_factory=dict)
    net_map: dict[str, str] = field(default_factory=dict)
    class_map: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def prefix(self) -> str:
        return self.spec.prefix


@dataclass
class MergeReport:
    designs: list[str] = field(default_factory=list)
    parts: int = 0
    elements: int = 0
    sheets: int = 0
    signals: int = 0
    joined_nets: list[tuple[str, list[str]]] = field(default_factory=list)
    renamed_parts: int = 0
    renamed_library_items: int = 0
    dropped_urns: int = 0
    placements: list[layout.Placement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sch_path: Path | None = None
    brd_path: Path | None = None


def load_designs(specs: list[DesignSpec]) -> list[Design]:
    designs: list[Design] = []
    for spec in specs:
        sch = EagleDoc.load(spec.sch_path)
        if sch.kind != "sch":
            raise EagleError(f"{spec.sch}: expected a schematic")
        brd = None
        if spec.brd_path is not None:
            brd = EagleDoc.load(spec.brd_path)
            if brd.kind != "brd":
                raise EagleError(f"{spec.brd}: expected a board")
        designs.append(Design(spec=spec, sch=sch, brd=brd))
    return designs


def build_resolver(designs: list[Design]) -> NetResolver:
    resolver = NetResolver()
    for design in designs:
        names = list(design.sch.net_names())
        if design.brd is not None:
            for name in design.brd.net_names():
                if name not in names:
                    names.append(name)
        resolver.add(design.name, names)
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
        self._merge_libraries()
        self._map_parts()
        self._map_classes()
        self._map_nets()

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
                if not original:
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
                mapping = plan_design_names(group, design.name, design.prefix, taken)
                design.net_map.update(mapping)
        for group in self.resolver.joined():
            if group.action is Action.JOIN:
                self.report.joined_nets.append(
                    (group.merged_name or group.display, group.designs)
                )

    # -- schematic ----------------------------------------------------------
    def build_schematic(self) -> EagleDoc:
        base = self.designs[0].sch
        root = ET.Element("eagle", {"version": base.version})
        root.text = "\n"
        drawing = ET.SubElement(root, "drawing")
        drawing.text = "\n"
        for tag in ("settings", "grid"):
            node = base.drawing.find(tag)
            if node is not None:
                drawing.append(clone(node))
        drawing.append(self._merged_layers())

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
        sheets = ET.SubElement(schematic, "sheets")
        sheets.text = "\n"

        for design in self.designs:
            renames = self.libs.renames_by_design[design.name]
            for part in design.sch.parts():
                node = clone(part)
                node.set("name", design.part_map.get(part.get("name", ""), part.get("name", "")))
                apply_to_part(node, renames)
                parts.append(node)
                self.report.parts += 1
            for index, sheet in enumerate(design.sch.sheets()):
                sheets.append(self._build_sheet(design, sheet, index))
                self.report.sheets += 1

        doc = EagleDoc(path=Path("merged.sch"), tree=ET.ElementTree(root), kind="sch")
        self.report.dropped_urns += strip_urns(root)
        tidy(root)
        return doc

    def _build_sheet(self, design: Design, sheet: ET.Element, index: int) -> ET.Element:
        node = ET.Element("sheet")
        node.text = "\n"
        node.tail = "\n"
        label = design.name if index == 0 else f"{design.name} (sheet {index + 1})"
        description = ET.SubElement(node, "description")
        description.text = label
        description.tail = "\n"

        for tag in ("plain", "moduleinsts", "instances", "busses"):
            source = sheet.find(tag)
            copy = clone(source) if source is not None else ET.Element(tag)
            if tag == "instances":
                for instance in copy:
                    if not isinstance(instance.tag, str):
                        continue
                    part = instance.get("part", "")
                    instance.set("part", design.part_map.get(part, part))
            node.append(copy)

        nets = ET.SubElement(node, "nets")
        nets.text = "\n"
        source_nets = sheet.find("nets")
        if source_nets is not None:
            for net in source_nets:
                if not isinstance(net.tag, str) or net.tag != "net":
                    continue
                copy = clone(net)
                original = net.get("name", "")
                copy.set("name", design.net_map.get(original, original))
                copy.set("class", design.class_map.get(net.get("class", "0"), "0"))
                self._rewrite_net_body(copy, design)
                nets.append(copy)
        return node

    def _rewrite_net_body(self, net: ET.Element, design: Design) -> None:
        """Point every pin reference inside a net at its renamed part.

        Net labels need no edit: EAGLE draws them from the net's own name.
        """
        for pinref in net.iterfind(".//pinref"):
            part = pinref.get("part", "")
            pinref.set("part", design.part_map.get(part, part))

    # -- board --------------------------------------------------------------
    def build_board(self) -> EagleDoc | None:
        boards = [d for d in self.designs if d.brd is not None]
        if not boards:
            return None
        base = boards[0].brd

        placements = layout.arrange(
            [(d.name, d.brd.section) for d in boards],
            style=self.plan.layout,
            gap=self.plan.gap,
            columns=self.plan.columns,
        )
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
        drawing.append(self._merged_layers())

        board = ET.SubElement(drawing, "board")
        board.text = "\n"

        plain = ET.SubElement(board, "plain")
        plain.text = "\n"
        for design in boards:
            source = design.brd.section.find("plain")
            if source is None:
                continue
            copy = clone(source)
            place = by_design[design.name]
            translate(copy, place.dx, place.dy)
            for node in list(copy):
                plain.append(node)

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
                node = clone(element)
                original = element.get("name", "")
                node.set("name", design.part_map.get(original, original))
                apply_to_element(node, renames)
                translate(node, place.dx, place.dy)
                elements.append(node)
                self.report.elements += 1

            for signal in design.brd.signals():
                original = signal.get("name", "")
                final = design.net_map.get(original, original)
                copy = clone(signal)
                copy.set("name", final)
                cls = signal.get("class")
                if cls is not None:
                    copy.set("class", design.class_map.get(cls, "0"))
                for contact in copy.iterfind(".//contactref"):
                    element_name = contact.get("element", "")
                    contact.set("element", design.part_map.get(element_name, element_name))
                translate(copy, place.dx, place.dy)

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

    # -- shared pieces ------------------------------------------------------
    def _merged_layers(self) -> ET.Element:
        """Union of every layer definition, keyed by layer number."""
        node = ET.Element("layers")
        node.text = "\n"
        seen: dict[int, ET.Element] = {}
        for design in self.designs:
            docs = [design.sch] + ([design.brd] if design.brd else [])
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
                        self.report.warnings.append(
                            f"global attribute {name} differs between designs; "
                            f"kept {seen[name]!r}, dropped {value!r}"
                        )
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
