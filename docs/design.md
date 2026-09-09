# How the merge works

The whole tool is one idea: compute every rename first, then rebuild both output
files using the same maps. Nothing is decided while writing. That is what keeps the
schematic and the board agreeing with each other, which EAGLE checks before it lets
you route.

## Modules

| Module | Responsibility |
| --- | --- |
| `eagle.py` | Load, save and transform EAGLE XML. Coordinate translation, content hashing, name sanitising. |
| `libraries.py` | Merge library sets, renaming items that clash by name but differ in content. |
| `nets.py` | Classify net names and decide join or split. |
| `layout.py` | Measure source boards and tile them without overlap. |
| `plan.py` | Serialise every decision to JSON so a merge is replayable. |
| `prompt.py` | Ask about the nets that rules cannot settle. |
| `merge.py` | Apply the maps and build the two output documents. |
| `cli.py` | `inspect`, `plan`, `merge`, `check`. |

## The four maps

Every design carries four rename maps, all built before a single output element is
written.

1. **Library renames.** Per design, keyed by `(library, item name)`. Built by
   `LibraryMerger` in dependency order: packages, then symbols, then devicesets,
   because a deviceset references both.
2. **Reference designators.** `R1` to `ESP3S3_R1`, applied identically to schematic
   parts and board elements so the two files keep matching.
3. **Nets.** Raw net name to merged net name, applied to schematic nets and board
   signals alike.
4. **Net classes.** Class numbers are per-file and start at zero in every design, so
   they are merged by content and renumbered.

## Library merging

Same-named libraries are not assumed identical. Each package, symbol and deviceset
is hashed after its own inner references have been rewritten, so the hash reflects
what the item will actually mean once merged. Then:

- Hash already seen: reuse it, record a rename if the name differs.
- Name free: store it as is.
- Name taken by different content: store under `NAME$2` and record the rename.

The inner-references-first ordering matters. Two devicesets can have byte-identical
XML while referring to symbols that are not the same symbol. Hashing the rewritten
copy catches that; hashing the raw copy would silently merge them.

## Net classification

`normalize()` folds spelling variants into a single key, so `3.3V`, `+3V3` and `3V3`
all become `3V3`, and `GND`, `VSS`, `0V` and `GROUND` all become `GND`.

`classify()` then buckets the key by how self-describing it is:

- **Ground family** and **explicit-voltage rails** join automatically. The name
  states the node, so a match is a real match.
- **Anonymous names** (`N$1`) never join. EAGLE generates them per file and they
  carry no meaning.
- **Role-named rails** (`VCC`, `VIN`, `AGND`) and **ordinary shared signals**
  (`SDA`, `D+`) are asked about. Joining a `VCC` that means 5 V on one board and
  3.3 V on another is a real hazard, so the tool refuses to guess.
- A name only one design uses is not a conflict and passes through untouched.

### The intra-design guard

Normalisation groups names across designs, but two nets inside one design are always
distinct, even when they normalize alike. `plan_design_names()` lets at most one raw
name per design inherit a joined name; the rest are localised under the design
prefix. Without this, a board carrying both `3.3V` and `+3V3` would have those two
separate nodes shorted together.

## Geometry

Board translation walks the subtree and shifts any element carrying an `x`/`y`,
`x1`/`y1`, `x2`/`y2` or `x3`/`y3` pair. Rotations, widths and curves are untouched,
so each board moves as a rigid body.

Translation is applied only to board-level geometry: `plain`, `elements` and
`signals`. Library packages use local coordinates relative to their own origin and
must never be shifted, or every footprint in the file would deform.

Schematic sheets are not translated at all. Each design gets its own sheet, so the
pages keep their original coordinates and look exactly as drawn.

## Why joined nets stay unrouted

When two designs' `GND` signals merge, their copper is concatenated into one signal
but no wire is drawn between the two boards. EAGLE renders the gap as an airwire.
That is the intended output: the airwires are precisely the list of connections the
engineer still has to make, and inventing a route across a board boundary would be
worse than showing the work that remains.

## Testing

`tests/conftest.py` builds small synthetic EAGLE designs that clash deliberately:
same part names, same library names with different pad geometry, and a net set
covering all three buckets. Those tests run in milliseconds and pin the behaviour.

`tests/test_samples.py` runs the same pipeline over the eight real Adafruit designs
in `examples/adafruit`, checking that every library reference resolves, every
reference designator is unique, no two boards overlap, and each source board
survives as a rigid translation. It skips if the samples are absent.
