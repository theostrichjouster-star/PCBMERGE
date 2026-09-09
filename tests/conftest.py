"""Small synthetic EAGLE designs, so tests do not depend on the samples."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

HEADER = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE eagle SYSTEM "eagle.dtd">\n'


def _sch(parts: str, nets: str, library: str) -> str:
    return HEADER + textwrap.dedent(f"""\
        <eagle version="9.6.2">
        <drawing>
        <settings/>
        <grid distance="0.1" unit="inch"/>
        <layers>
        <layer number="91" name="Nets" color="2" fill="1" visible="yes" active="yes"/>
        <layer number="94" name="Symbols" color="4" fill="1" visible="yes" active="yes"/>
        </layers>
        <schematic xreflabel="%F%N/%S.%C%R" xrefpart="/%S.%C%R">
        <libraries>
        {library}
        </libraries>
        <attributes/>
        <variantdefs/>
        <classes>
        <class number="0" name="default" width="0" drill="0"/>
        </classes>
        <parts>
        {parts}
        </parts>
        <sheets>
        <sheet>
        <plain/>
        <instances>
        <instance part="R1" gate="G$1" x="10" y="10"/>
        <instance part="C1" gate="G$1" x="20" y="10"/>
        </instances>
        <busses/>
        <nets>
        {nets}
        </nets>
        </sheet>
        </sheets>
        </schematic>
        </drawing>
        </eagle>
        """)


def _brd(elements: str, signals: str, library: str) -> str:
    return HEADER + textwrap.dedent(f"""\
        <eagle version="9.6.2">
        <drawing>
        <settings/>
        <grid distance="0.05" unit="inch"/>
        <layers>
        <layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes"/>
        <layer number="20" name="Dimension" color="15" fill="1" visible="yes" active="yes"/>
        </layers>
        <board>
        <plain>
        <wire x1="0" y1="0" x2="30" y2="0" width="0.05" layer="20"/>
        <wire x1="30" y1="0" x2="30" y2="20" width="0.05" layer="20"/>
        <wire x1="30" y1="20" x2="0" y2="20" width="0.05" layer="20"/>
        <wire x1="0" y1="20" x2="0" y2="0" width="0.05" layer="20"/>
        </plain>
        <libraries>
        {library}
        </libraries>
        <attributes/>
        <variantdefs/>
        <classes>
        <class number="0" name="default" width="0" drill="0"/>
        </classes>
        <designrules name="default"/>
        <autorouter/>
        <elements>
        {elements}
        </elements>
        <signals>
        {signals}
        </signals>
        </board>
        </drawing>
        </eagle>
        """)


def library(package_pad: str = "1.0", symbol_extra: str = "") -> str:
    """A one-library fixture whose content can be varied to force a clash."""
    return textwrap.dedent(f"""\
        <library name="parts">
        <packages>
        <package name="0603">
        <smd name="1" x="-0.8" y="0" dx="{package_pad}" dy="0.8" layer="1"/>
        <smd name="2" x="0.8" y="0" dx="{package_pad}" dy="0.8" layer="1"/>
        </package>
        </packages>
        <symbols>
        <symbol name="R">
        <wire x1="-2" y1="0" x2="2" y2="0" width="0.2" layer="94"/>{symbol_extra}
        <pin name="1" x="-5" y="0" length="middle"/>
        <pin name="2" x="5" y="0" length="middle"/>
        </symbol>
        </symbols>
        <devicesets>
        <deviceset name="RES" prefix="R">
        <gates>
        <gate name="G$1" symbol="R" x="0" y="0"/>
        </gates>
        <devices>
        <device name="_0603" package="0603">
        <connects>
        <connect gate="G$1" pin="1" pad="1"/>
        <connect gate="G$1" pin="2" pad="2"/>
        </connects>
        </device>
        </devices>
        </deviceset>
        </devicesets>
        </library>
        """)


PARTS = textwrap.dedent("""\
    <part name="R1" library="parts" deviceset="RES" device="_0603" value="10k"/>
    <part name="C1" library="parts" deviceset="RES" device="_0603" value="100n"/>
    """)

ELEMENTS = textwrap.dedent("""\
    <element name="R1" library="parts" package="0603" value="10k" x="5" y="5"/>
    <element name="C1" library="parts" package="0603" value="100n" x="15" y="5"/>
    """)


def nets(names: list[str]) -> str:
    out = []
    for index, name in enumerate(names):
        out.append(
            f'<net name="{name}" class="0">\n'
            f'<segment>\n'
            f'<pinref part="R1" gate="G$1" pin="{index % 2 + 1}"/>\n'
            f'<pinref part="C1" gate="G$1" pin="{index % 2 + 1}"/>\n'
            f'</segment>\n'
            f'</net>'
        )
    return "\n".join(out)


def signals(names: list[str]) -> str:
    out = []
    for index, name in enumerate(names):
        out.append(
            f'<signal name="{name}">\n'
            f'<contactref element="R1" pad="{index % 2 + 1}"/>\n'
            f'<contactref element="C1" pad="{index % 2 + 1}"/>\n'
            f'<wire x1="5" y1="5" x2="15" y2="5" width="0.2" layer="1"/>\n'
            f'</signal>'
        )
    return "\n".join(out)


def write_design(directory: Path, name: str, net_names: list[str],
                 pad: str = "1.0", symbol_extra: str = "", board: bool = True) -> Path:
    lib = library(pad, symbol_extra)
    stem = directory / name
    stem.with_suffix(".sch").write_text(_sch(PARTS, nets(net_names), lib), encoding="utf-8")
    if board:
        stem.with_suffix(".brd").write_text(_brd(ELEMENTS, signals(net_names), lib), encoding="utf-8")
    return stem


@pytest.fixture
def design_writer():
    """Lets a test build extra synthetic designs of its own."""
    return write_design


@pytest.fixture
def designs(tmp_path: Path) -> Path:
    """Two designs that clash on names, library content and nets."""
    write_design(tmp_path, "alpha board", ["GND", "3.3V", "N$1", "SDA", "VCC"])
    write_design(tmp_path, "beta board", ["GND", "+3V3", "N$1", "SDA", "VCC"],
                 pad="1.2", symbol_extra='<wire x1="0" y1="-2" x2="0" y2="2" width="0.2" layer="94"/>')
    return tmp_path


@pytest.fixture(scope="session")
def samples() -> Path:
    """The real Adafruit designs, when they are present in the repo."""
    path = Path(__file__).resolve().parent.parent / "examples" / "adafruit"
    if not path.is_dir() or not list(path.glob("*.sch")):
        pytest.skip("sample designs not available")
    return path
