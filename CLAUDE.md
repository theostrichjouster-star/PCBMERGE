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
`EagleDoc`. The **board is the source of truth** — it holds the netlist, placement, copper
and outline. The schematic is rebuilt from that netlist as one box per part with one pin per
pad, which keeps the pair consistent by construction. It is deliberately not a conversion of
the `.kicad_sch` drawing; the merge reports that it did this.

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
- **Hardware is ranked above software.** Searching a part number finds the driver
  library long before the board. `hardware_rank` reorders each account's results; it
  never hides anything.
- **Contents are listed lazily.** Sixty unauthenticated requests an hour, ten searches
  a minute. Answers are cached for ten minutes and the page looks inside only the
  first few results, stopping at the first refusal. `GITHUB_TOKEN` raises the limit.
- **A downloaded design keeps one stem for both halves**, or the merge cannot find the
  board. Nothing from the repository is used as a path: the name is sanitised, only a
  known design extension survives, and a repeat download gets its own stem.

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
