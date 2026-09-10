# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pcbmerge` combines several PCB designs into one schematic and one board. It reads
EAGLE (`.sch` / `.brd`) and KiCad (`.kicad_pcb`), and always writes EAGLE. It can
also search Adafruit, SparkFun and Seeed Studio on GitHub and download designs to
merge. The hard parts are naming (everything collides) and net resolution (deciding
which nets from different designs are the same wire).

## Commands

No dependencies beyond the standard library; `pytest` is the only dev extra.

```bash
pip install -e ".[dev]"     # editable install, puts `pcbmerge` on PATH
pytest                      # whole suite, ~330 tests, under 5 seconds
pytest tests/test_nets.py                                   # one file
pytest "tests/test_nets.py::test_normalize_folds_equivalent_spellings"   # one test
pytest -k kicad             # by name
```

`pytest` works from the repo root without installing: `pyproject.toml` sets
`pythonpath = ["src"]`.

Exercising the tool against the real designs in `examples/adafruit` (eight EAGLE,
one KiCad):

```bash
pcbmerge inspect examples/adafruit
pcbmerge merge examples/adafruit -o combo --out-dir out --yes
pcbmerge check out/combo
pcbmerge web examples/adafruit
```

The search reaches the network, so it is exercised by hand rather than in the suite:

```bash
pcbmerge search bme280
pcbmerge fetch adafruit/Adafruit-BME280-Breakout-PCB --all --dest downloads
```

`check` is the fast correctness gate: it verifies a `.sch`/`.brd` pair references
nothing that does not exist. On the samples it reports exactly three problems, all
orphan copper stubs inherited from `Adafruit MAX31850.brd`. **Three is the expected
baseline; more means something regressed.**

Reinstall fails with a permissions error while `pcbmerge web` is running, because the
server holds `pcbmerge.exe`. Stop the server first.

`web.py` is loaded into memory when the server starts, but `static/app.html` is read
from disk on every request. A server left running across a change therefore serves the
new page and answers with the old API, which looks like a feature silently not working
rather than an error. Restart the server after touching `web.py`, and when a view
reports no data, check the server's age before looking for a bug.

## Architecture

### The central invariant

EAGLE will not open a `.sch`/`.brd` pair whose parts, nets and library references
disagree. Everything else follows from keeping them in step:

**Every rename is computed before a single output element is written.** `Merger.prepare()`
builds four maps per design instance, then both output files are rebuilt from the same
maps. Nothing is decided during writing. Order matters and is load-bearing:

1. `_apply_drops` — decided first, so a removed part never claims a designator
2. `_merge_libraries` — packages, then symbols, then devicesets (a deviceset references both)
3. `_map_parts` — `R1` becomes `RELAY2_R1`, applied identically to schematic parts and board elements
4. `_map_classes` — net classes are per-file and all start at zero, so they are merged by content and renumbered
5. `_map_nets` — raw net name to merged name, applied to schematic nets and board signals alike

Net survival after pruning is decided **once**, from the schematic (`_nets_left_empty`),
and both builders consult that set. Letting each file decide for itself produces board
signals no schematic net matches, which EAGLE rejects.

### Module map

| Module | Role |
| --- | --- |
| `eagle.py` | Load/save EAGLE XML, coordinate translation, content hashing, name safety |
| `sexp.py` | Read the S-expressions KiCad writes |
| `sources.py` | Search the vendor GitHub accounts; download designs into a folder |
| `kicad.py` | Convert a KiCad design into an EAGLE pair |
| `libraries.py` | Merge library sets, renaming items that clash by name but differ in content |
| `nets.py` | Classify net names; decide join or split |
| `linking.py` | Propose connections between differently named nets |
| `pruning.py` | Catalogue parts; work out what a set of drop rules removes |
| `layout.py` | Measure boards, pack them, search for a cheaper arrangement, pin them |
| `plan.py` | Serialise every decision to JSON so a merge is replayable |
| `prompt.py` | The interactive questions |
| `merge.py` | Apply the maps, build the two documents |
| `cli.py` | `inspect`, `parts`, `plan`, `merge`, `check`, `web` |
| `web.py` + `static/app.html` | Local HTTP front end |

### Net resolution

`normalize()` folds spellings (`3.3V`, `+3V3`, `3V3` → `3V3`). `classify()` then buckets
by how self-describing a name is:

- **ground family and explicit-voltage rails** join automatically — the name states the node
- **anonymous names** (`N$1`) never join
- **role-named rails** (`VCC`, `VIN`) and **shared signals** (`SDA`) are asked about — joining a
  `VCC` that means 5 V on one board and 3.3 V on another is a real hazard
- **replica nets**, appearing in several copies of one design, get their own question type

`NetGroup` reports `design_count` (instances) and `source_count` (distinct input designs)
separately. That difference is what distinguishes a genuine cross-design clash from four
copies of one board sharing names by construction.

Two resolution paths force nets together at different granularities: `link()` aliases a
resolution *key* (a name everywhere), `connect()` aliases a *(design, net)* pair (one copy
only). `key_for()` resolves both in one place so `finalize()` stays a single pass.

**The intra-design guard:** two nets inside a single design are always distinct, even when
they normalize alike. `plan_design_names()` lets at most one raw name per instance inherit
a joined name. Without it a board carrying both `3.3V` and `+3V3` would have those separate
nodes shorted.

### Constraints learned the hard way

These were each found by opening output in EAGLE. Do not undo them.

- **Layer tables are per file kind.** A schematic marks copper layers `visible="no"
  active="no"`; a board needs them on. Building the board's table from schematic layers
  hides every footprint. `_merged_layers(kind)` takes one kind only.
- **One schematic sheet, always.** Multi-sheet output was built and withdrawn: its sheets
  carried an empty `<moduleinsts/>` that no hand-drawn file has, and EAGLE would not open
  them. `_sheet_body()` emits only `plain`, `instances`, `busses`, `nets`.
- **One `<net>` per name per sheet.** When designs share a sheet their joined nets meet;
  two elements named `GND` on one sheet is not a form EAGLE accepts. `_absorb_named()`
  folds segments into the first element of that name.
- **Never `Path.with_suffix` or `.stem` on a design path.** Both cut at the last dot, so
  `widget_V1.5.kicad_pcb` loses its `.5`. Use `design_stem()` and `with_ext()`.
- **Managed-library URNs are stripped.** EAGLE would otherwise try to re-sync a library
  whose items have been renamed.
- **Only translate board-level geometry** (`plain`, `elements`, `signals`). Library packages
  use local coordinates; shifting them deforms every footprint.

### KiCad conversion

Happens in `load_designs` and nowhere else, so the rest of the engine only ever sees
`EagleDoc`. The **board is the source of truth for nets** — it holds the netlist,
placement, copper and outline.

`kicad_sch.py` converts the `.kicad_sch` when one is there: symbols with their real
graphics, placements with rotation and mirroring, wires, junctions and labels, plus any
child sheets found beside it. Connectivity is worked out from the geometry, but every net
takes its **name from the board**, because the pair only opens if the schematic's nets and
the board's signals agree.

Three traps, each of which produces a file that looks nearly right:

- **A symbol is y-up, a sheet is y-down.** Symbol geometry crosses over untouched;
  everything sheet-level has its y negated. One flip too many draws every symbol upside
  down inside a correctly placed outline.
- **A placement angle belongs to the symbol's frame** and is carried across as-is. Settled
  against a real file, not reasoned about.
- **Unit 0 is what every unit shares, not a unit.** Most two-pin parts are drawn entirely
  in unit 0 and placed as unit 1. Treat it as its own unit and those parts get no symbol,
  no part, and pinrefs to a part that does not exist.

With no usable drawing it falls back to one box per part with one pin per pad, built from
the netlist, which keeps the pair consistent by construction. The merge report says which
route it took and why.

Footprint references and values are read from `property` **and** from `fp_text`. KiCad 8
moved them; reading only the newer form leaves every part on an older board called `U$n`,
which silently breaks the match between schematic and board.

Translated rather than copied: Y is negated (KiCad counts down), so rotations change sign and
back-side footprints mirror; arcs go from three points to an included angle; net names keep
only their leaf (`/Sheet/VCC_3V3` → `VCC_3V3`) or no rail would match, and `Net-(U1-Pad2)`
becomes `N$1` so the resolver treats it as anonymous.

### Hand placement

A `Spot` in the plan pins one design in one view (`board` or `sheet`). The two are
independent by design: a board sits where the copper has to go, a drawing sits
where it reads well.

- **`layout.pin` rewrites both halves of a `Placement`**, the translation and the
  resulting edges. Setting only one draws a board in one place and writes it out
  in another.
- **Pins are applied after the search, not as a constraint on it**, so the boards
  left to the packer are still arranged well among themselves. Because of that
  `report.after` must be recomputed from the final placements, or the cost shown
  describes an arrangement nobody gets.
- **A pin for a design that is not in the merge is ignored**, never an error.
  Changing a copy count must not invalidate everything else.
- **`preview()` and `sheet_preview()` are the only sources for the two views.**
  The page never computes a position itself; a picture that disagreed with the
  file would be worse than no picture.

### Vendor search

`sources.py` reaches GitHub with `urllib` and knows nothing about merging. What it
produces is a folder, which is already a valid input, so nothing downstream changed
to accommodate it.

- **The module is `sources`, not `catalog`.** `pruning.catalog` is exported from the
  package, and `from . import catalog` inside `cli.py` would resolve to that function
  rather than the submodule.
- **One search per vendor, interleaved.** Several `org:` qualifiers in one query let
  the ranking fill the page with a single account.
- **A name that answers the query is opened first**, then hardware above software.
  `rank()` puts the two together. An account holds the board, its library and its
  hookup guide, all mentioning the part, so without the name test the budget goes on
  the writing about the board rather than the board. Ranking only reorders; it never
  hides anything.
- **Design files are usually in a subfolder** (`Hardware/` is the common one). The
  whole tree is read, so nothing depends on where they are or what the folder is
  called.
- **A result is a design, not a repository.** Repository search matches names and
  descriptions, never file contents, so it both returns repositories with no hardware
  and misses hardware in a repository named after something else. Every candidate is
  opened and kept only if it holds a design.
- **Catalogue repositories are always opened**, named per vendor in `Source.catalogs`,
  and filtered by design name rather than repository name -- otherwise a catalogue
  answers every search. This is what makes Seeed's KiCad designs findable at all.
- **Opening costs a request.** Sixty an hour unauthenticated, so a search opens at most
  `BUDGET` repositories and puts the reason on `Found.stopped`. A refusal partway
  through returns what was found rather than raising. `GITHUB_TOKEN` raises the limit.
- **Candidates follow the budget, not the result limit.** Offering inspection only as
  many repositories as will be shown starves it: the first names a vendor returns are
  usually libraries, and a search able to open nothing else reports that the vendor has
  no hardware.
- **A design is keyed by tool as well as by name.** A vendor porting a board keeps the
  EAGLE pair and the KiCad project beside each other under one name; folded together
  they become one entry that pulls all four files down and hides whichever half was
  wanted.
- **A downloaded design keeps one stem for both halves**, or the merge cannot find the
  board. Nothing from the repository is used as a path: the name is sanitised, only a
  known design extension survives, and a repeat download gets its own stem.
- **An import lands in the open project folder**, which must already exist and have
  been scanned. `import_designs` refuses a missing or unknown folder rather than
  inventing one, and the page rescans with `absorb` rather than `adopt` so copy
  counts, net decisions and hand placements survive adding a design.

`SourceError` subclasses `EagleError` deliberately, so the CLI and the web front end
report a network failure through the paths they already have.

### Plan and web

`MergePlan` is the serialisation boundary — designs, counts, drops, net decisions, links,
connections, layout. A saved plan replays byte-identically. Bump `PLAN_VERSION` on format
changes and keep older plans loading; removed options are ignored, not fatal.

`web.py` owns no merge logic. Every request builds the same `Merger` the CLI builds. A second
implementation of the rules in JavaScript would drift within a week. `analyze` writes nothing
and is called after every edit, so it leans on the document cache and `Merger.preview()`,
which runs the real placement but stops before producing XML.

## Testing approach

`tests/conftest.py` builds small synthetic EAGLE designs that clash deliberately — same part
names, same library names with different pad geometry, a net set covering every bucket.
`tests/test_kicad.py` does the same for KiCad with an inline board. `tests/test_samples.py`
runs the pipeline over `examples/adafruit` and skips if absent.

Tests assert behaviour and invariants, not incidental values. Derive prefixes from
`collect_specs` rather than hardcoding them; they change when the naming heuristic does.
`tests/test_board_output.py` and `tests/test_single_sheet.py` exist specifically to pin the
things only EAGLE would otherwise catch.

`tests/test_sources.py` never touches the network. GitHub is replaced with canned
payloads keyed by URL fragment; the fake resolves the deepest fragment, because a tree
URL contains the repository URL. Do not add a test there that would make a real
request.
