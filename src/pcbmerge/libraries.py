"""Merging EAGLE libraries from several designs into one library set.

Boards drawn years apart carry libraries with the same name but different
content -- the sample set has eight distinct `microbuilder` libraries.  So a
merge cannot just take the first copy or the last one.  Each package, symbol
and deviceset is compared by content and either reused or renamed, and every
reference to a renamed item is rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import xml.etree.ElementTree as ET

from .eagle import child, clone, content_hash, iter_named, unique_name

# Sections of a library, in dependency order: devicesets reference symbols and
# packages, so those must be resolved first.
SECTIONS = (
    ("packages", "package"),
    ("packages3d", "package3d"),
    ("symbols", "symbol"),
    ("devicesets", "deviceset"),
)


@dataclass
class LibraryRenames:
    """What one design's library references must be rewritten to."""

    packages: dict[tuple[str, str], str] = field(default_factory=dict)
    symbols: dict[tuple[str, str], str] = field(default_factory=dict)
    devicesets: dict[tuple[str, str], str] = field(default_factory=dict)

    def package(self, lib: str, name: str) -> str:
        return self.packages.get((lib, name), name)

    def symbol(self, lib: str, name: str) -> str:
        return self.symbols.get((lib, name), name)

    def deviceset(self, lib: str, name: str) -> str:
        return self.devicesets.get((lib, name), name)

    @property
    def count(self) -> int:
        return len(self.packages) + len(self.symbols) + len(self.devicesets)


class LibraryMerger:
    """Accumulates libraries from every design into one merged set."""

    def __init__(self) -> None:
        self.libraries: dict[str, ET.Element] = {}
        # (library, section, name) -> content hash of the item we kept
        self._hashes: dict[tuple[str, str, str], str] = {}
        # (library, section, hash) -> name it was stored under
        self._by_hash: dict[tuple[str, str, str], str] = {}
        self.renames_by_design: dict[str, LibraryRenames] = {}

    def add_design(self, design: str, libs: list[ET.Element]) -> LibraryRenames:
        renames = LibraryRenames()
        self.renames_by_design[design] = renames
        for lib in libs:
            self._add_library(lib, renames)
        return renames

    # -- internals ----------------------------------------------------------
    def _add_library(self, lib: ET.Element, renames: LibraryRenames) -> None:
        lib_name = lib.get("name", "unnamed")
        target = self.libraries.get(lib_name)
        if target is None:
            target = ET.Element("library", {"name": lib_name})
            target.text = "\n"
            target.tail = "\n"
            desc = lib.find("description")
            if desc is not None:
                target.append(clone(desc))
            self.libraries[lib_name] = target

        for section, item_tag in SECTIONS:
            source = lib.find(section)
            if source is None:
                continue
            dest = child(target, section)
            existing = {node.get("name") for node in iter_named(dest)}
            for item in iter_named(source):
                if item.tag != item_tag:
                    continue
                self._add_item(lib_name, section, item, dest, existing, renames)

    def _add_item(
        self,
        lib_name: str,
        section: str,
        item: ET.Element,
        dest: ET.Element,
        existing: set[str],
        renames: LibraryRenames,
    ) -> None:
        original = item.get("name", "")
        candidate = clone(item)
        # Rewrite inner references first so the hash reflects what this copy
        # will actually mean once merged.
        _rewrite_item_refs(candidate, lib_name, renames)
        digest = content_hash(candidate, ignore=("urn", "library_version"))

        # Already stored an identical item, possibly under another name.
        twin = self._by_hash.get((lib_name, section, digest))
        if twin is not None:
            if twin != original:
                _record(renames, section, lib_name, original, twin)
            return

        kept = self._hashes.get((lib_name, section, original))
        if kept is None:
            name = original
        else:
            # Same name, different content: keep both under distinct names.
            name = unique_name(original, existing)
            candidate.set("name", name)
            _record(renames, section, lib_name, original, name)

        dest.append(candidate)
        existing.add(name)
        self._hashes[(lib_name, section, name)] = digest
        self._by_hash[(lib_name, section, digest)] = name

    def build(self) -> ET.Element:
        node = ET.Element("libraries")
        node.text = "\n"
        node.tail = "\n"
        for name in sorted(self.libraries):
            node.append(self.libraries[name])
        return node

    def stats(self) -> dict[str, int]:
        counts = {"libraries": len(self.libraries)}
        for section, _ in SECTIONS:
            total = sum(len(lib.findall(f"{section}/*")) for lib in self.libraries.values())
            if total:
                counts[section] = total
        return counts


def _record(renames: LibraryRenames, section: str, lib: str, old: str, new: str) -> None:
    if section == "packages":
        renames.packages[(lib, old)] = new
    elif section == "symbols":
        renames.symbols[(lib, old)] = new
    elif section == "devicesets":
        renames.devicesets[(lib, old)] = new


def _rewrite_item_refs(item: ET.Element, lib: str, renames: LibraryRenames) -> None:
    """Point a deviceset's gates and devices at any renamed symbols/packages."""
    for gate in item.iterfind("gates/gate"):
        symbol = gate.get("symbol")
        if symbol:
            gate.set("symbol", renames.symbol(lib, symbol))
    for device in item.iterfind("devices/device"):
        package = device.get("package")
        if package:
            device.set("package", renames.package(lib, package))
    for pkg3d in item.iterfind("packageinstances/packageinstance"):
        name = pkg3d.get("name")
        if name:
            pkg3d.set("name", renames.package(lib, name))


def apply_to_part(part: ET.Element, renames: LibraryRenames) -> None:
    """Rewrite a schematic <part>'s library references in place."""
    lib = part.get("library", "")
    deviceset = part.get("deviceset")
    if deviceset:
        part.set("deviceset", renames.deviceset(lib, deviceset))


def apply_to_element(element: ET.Element, renames: LibraryRenames) -> None:
    """Rewrite a board <element>'s package reference in place."""
    lib = element.get("library", "")
    package = element.get("package")
    if package:
        element.set("package", renames.package(lib, package))
