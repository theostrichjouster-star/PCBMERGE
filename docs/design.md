# How the merge works

The whole tool is one idea: compute every rename first, then rebuild both output
files using the same maps. Nothing is decided while writing. That is what keeps the
schematic and the board agreeing with each other, which EAGLE checks before it lets
you route.

## Modules

| Module | Responsibility |
| --- | --- |
| `sources.py` | Search the vendor accounts on GitHub and download designs. |
| `sexp.py` | Read the S-expressions KiCad writes. |
| `kicad.py` | Convert a KiCad board into an EAGLE pair on the way in. |
| `kicad_sch.py` | Convert the KiCad schematic drawing that goes with it. |
| `eagle.py` | Load, save and transform EAGLE XML. Coordinate translation, content hashing, name sanitising. |
| `libraries.py` | Merge library sets, renaming items that clash by name but differ in content. |
| `nets.py` | Classify net names and decide join or split. |
| `linking.py` | Propose connections between differently named nets. |
| `pruning.py` | Catalogue parts and work out which a set of rules removes. |
| `layout.py` | Measure boards, pack them, and search for a cheaper arrangement. |
| `plan.py` | Serialise every decision to JSON so a merge is replayable. |
| `prompt.py` | Ask about copies, replicas, contested nets and links. |
| `merge.py` | Apply the maps and build the two output documents. |
| `cli.py` | `inspect`, `parts`, `plan`, `merge`, `check`, `web`. |
| `web.py` | A loopback HTTP server exposing the engine to the browser. |
| `static/app.html` | The whole front end: one file, no dependencies. |

## Where designs come from

Merging vendor boards means first having them, and that used to mean finding the
repository by hand. `sources.py` searches Adafruit, SparkFun and Seeed Studio on
GitHub and downloads what is chosen. It has no merge logic and nothing else
depends on it: what it produces is a folder, which is already a valid input.

Three decisions shape it.

**One search per account, then interleaved.** GitHub supports several `org:`
qualifiers in one query, but the ranking would then be free to fill the page with
whichever vendor happens to rank well for those words. Searching each account
separately and taking a row at a time from each guarantees all three are
represented.

**Hardware is ranked above software.** A search for a part number finds the driver
library long before the board, because the library is what people star and link
to. `hardware_rank` scores a repository on words like *pcb*, *breakout* and
*shield* against *library*, *driver* and *firmware*, and reorders each account's
results. It only reorders: nothing a search returned is hidden.

**A result is a design, not a repository.** GitHub's repository search matches a
name and a description and never the files inside, which fails in both directions:
it returns libraries and example code that hold no hardware, and it misses hardware
whose repository is named after something else. Every candidate is therefore opened
and kept only if a design is in it.

**Catalogue repositories are always opened.** Some vendors keep many designs in one
repository whose name answers no part query at all; seven XIAO designs sit in
`OPL_Kicad_Library`. Those are named per vendor in `Source.catalogs`, always looked
in, and filtered by the names of the designs rather than the repository, or a
catalogue would answer every search.

**Opening costs a request**, against sixty an hour unauthenticated, so a search
opens at most `BUDGET` repositories and reports on `Found.stopped` when it stopped
short. A refusal partway through returns what was found rather than raising, since
half a page of results beats none. Every answer is cached for ten minutes.

Downloading pairs files the same way the rest of the tool does, by extension after
a shared stem, and writes both halves of a design under one name. Renaming one
half without the other would hide the board from the merge. Nothing a repository
supplies is used as a path: the name is sanitised, only a known design extension
survives, and the destination is decided locally, so a crafted filename cannot
write outside the folder or choose its own extension.

## Reading KiCad

Conversion happens in `load_designs` and nowhere else, so the whole engine only ever
sees `EagleDoc` objects. Nothing downstream branches on which tool drew a design,
which is what keeps one format from leaking into the merge logic.

The board is the source. A `.kicad_pcb` holds the netlist, the placement, the copper
and the outline; the schematic is rebuilt from it as one box per part with one pin
per pad, connections carried on labels. That guarantees the pair is consistent,
which converting two files independently would not.

Three things have to be translated rather than copied:

- **The Y axis.** KiCad counts down, EAGLE counts up, so every Y is negated.
  Rotations therefore change sign, and a footprint on the back is mirrored.
- **Arcs.** KiCad stores three points, EAGLE stores an included angle, so the angle
  is computed from the inscribed angle at the middle point.
- **Net names.** `/Sheet/VCC_3V3` keeps only its leaf or no rail would match an
  EAGLE design's; `Net-(U1-Pad2)` becomes `N$1` so the resolver keeps it apart the
  same way it keeps EAGLE's anonymous nets apart.

### The drawing

`kicad_sch.py` reads the `.kicad_sch` and produces symbols, placements, wires and
nets; `kicad.py` wraps them in a document and falls back to the netlist boxes when
there is nothing to draw. The split keeps the board conversion, which is the part
everything else depends on, free of the drawing's complications.

Three things decide whether the result looks right.

**Two coordinate systems.** A symbol is stored y-up and a sheet y-down, so symbol
geometry crosses over untouched and everything sheet-level has its y negated. Doing
one flip too many draws each symbol upside down inside a correctly placed outline,
which is subtle enough to ship by accident.

**Rotation is the symbol's.** A placement angle is applied in the symbol's y-up
frame and carried across unchanged. That was settled against a real file rather than
reasoned about: on a Seeed board of 157 symbols, taking the angle as given lands
pins on wire ends everywhere that negating it does, and in the rotated cases where
negating it does not.

**Unit 0 is not a unit.** KiCad puts what every unit shares in unit 0, and most
two-pin parts are drawn entirely there while still being placed as unit 1. Treating
unit 0 as a unit of its own leaves those parts with no symbol, no part, and pinrefs
pointing at a part that was never made, which EAGLE refuses to open.

Connectivity is union-find over the points the drawing actually contains. Wires join
at shared ends; anything sitting on the middle of a wire joins it, which is how a T
and a pin landing mid-span connect; two wires merely crossing do not, which is why
intersections are never computed. Each group then takes its name from the board.

### Filenames with dots

`Path.with_suffix` and `Path.stem` both cut at the last dot, so a board called
`XIAO ESP32S3_V1.5.kicad_pcb` would be looked for as `XIAO ESP32S3_V1.kicad_pcb`
and its design would be named `XIAO_ESP32S3_V1`. `design_stem()` strips only a
known extension and `with_ext()` appends rather than replaces. This was already
wrong for EAGLE files with a version in the name; KiCad's example is simply what
exposed it.

## Designs and instances

A `DesignSpec` is an input file with a copy count. `expand()` turns it into
`InstanceSpec` objects, one per copy, each carrying a numbered prefix and a `source`
naming the design it came from.

That `source` field does real work. It is what separates a genuine cross-design
clash from replication: four copies of one board share every net name by
construction, which is not the same situation as two different boards happening to
both use `SDA`. `NetGroup` therefore reports `design_count` and `source_count`
separately, and `classify()` takes both.

Instances of the same file share one parsed document. Every consumer clones before
mutating, so the sharing is invisible and eight copies cost one parse.

## The four maps

Every design instance carries four rename maps, all built before a single output
element is written.

1. **Library renames.** Per instance, keyed by `(library, item name)`. Built by
   `LibraryMerger` in dependency order: packages, then symbols, then devicesets,
   because a deviceset references both.
2. **Reference designators.** `R1` to `RELAY2_R1`, applied identically to schematic
   parts and board elements so the two files keep matching.
3. **Nets.** Raw net name to merged net name, applied to schematic nets and board
   signals alike.
4. **Net classes.** Class numbers are per-file and start at zero in every design, so
   they are merged by content and renumbered.

Because the copy number lives in the prefix, parts and design-local nets increment
together automatically. There is no separate numbering scheme to keep in sync.

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

- **Ground family** and **explicit-voltage rails** join automatically, across copies
  as readily as across designs. The name states the node, so a match is a real match.
- **Anonymous names** (`N$1`) never join. EAGLE generates them per file and they
  carry no meaning.
- **Role-named rails** (`VCC`, `VIN`, `AGND`) and **ordinary shared signals**
  (`SDA`, `D+`) from two or more source designs are asked about. Joining a `VCC`
  that means 5 V on one board and 3.3 V on another is a real hazard.
- **Replica nets**, appearing in several copies of one design, get their own
  question type. They are asked once per design with all candidates listed, rather
  than once per net per copy.
- A name only one instance uses is not a conflict and passes through untouched.

### The intra-design guard

Normalisation groups names across designs, but two nets inside one design are always
distinct, even when they normalize alike. `plan_design_names()` lets at most one raw
name per instance inherit a joined name; the rest are localised under the prefix.
Without this, a board carrying both `3.3V` and `+3V3` would have those two separate
nodes shorted together.

## Linking differently named nets

`linking.py` proposes connections that no naming rule could find. Net names are
tokenised, folded through a synonym table so `MOSI`, `SDI` and `DATA` all become
`SDA`, and compared as token sets.

The scorer is tuned for precision over recall. A missed connection remains visible
as an unrouted net; a wrong one has to be noticed and undone, which is more
expensive. So:

- Connector pin labels (`A0`, `D13`) are rejected outright. The digit is the
  identity, not a channel marker, and two of them say nothing about being one wire.
- A trailing index is stripped only from tokens of three characters or more, so
  `SDA1` folds to `SDA` while `A1` stays `A1`.
- Containment is tested on token sets, not raw strings. Substring matching on short
  names produced nonsense like `A1` inside `ADDR0`.
- Camel-case splitting only runs on names that actually contain lowercase, or `I2C`
  would tear into `I2` and `C`.

A suggestion is only offered when accepting it would bridge designs that this net
leaves otherwise unconnected.

Accepted links become key aliases in the resolver, which regroups on the next
`finalize()`. Links apply before any join or split decision, because they change
which nets are in which group.

## Two kinds of connection

`link` and `connect` both force nets into one group, at different granularities.

`link` aliases one resolution *key* to another, so `SDA` and `I2C_DATA` become one
net wherever either name appears. That is what you want for a bus.

`connect` aliases one *(design, net name)* pair, so a controller's `A0` can reach
the first of three relay copies while the other two keep their own `SIGNAL`. Both
end up in `linked_keys`, which makes the group a join.

`key_for()` resolves a net's group in one place, checking the per-reference alias
first and the key alias second, so `finalize()` stays a single pass. `group_for()`
takes an optional design for the same reason: once a connection has moved one
copy of a name, the name alone no longer identifies a group.

## Dropping parts

`pruning.py` catalogues every part and board-only footprint, grouping by kind and
folding copy numbers away, so `PLABEL0` through `PLABEL32` are one decision rather
than thirty-three. Value is part of the key only for parts that do something: a
5.1K resistor differs from a 10K one, but a fiducial's value says nothing.

Drops are applied first, in `prepare()`, before any renaming. That ordering is
what stops a removed part from reserving a designator, and it is what lets one
decision reach both files.

Net survival is the subtle part. A net whose every pin belonged to removed parts
has to disappear from the schematic *and* the board, and the two must agree.
Letting each side decide for itself produces board signals that no schematic net
matches, which EAGLE rejects; an early version of this did exactly that and a test
caught it. So `_nets_left_empty()` decides once, from the schematic, and both
builders consult the same set.

## Geometry

Board translation walks the subtree and shifts any element carrying an `x`/`y`,
`x1`/`y1`, `x2`/`y2` or `x3`/`y3` pair. Rotations, widths and curves are untouched,
so each board moves as a rigid body.

Translation is applied only to board-level geometry: `plain`, `elements` and
`signals`. Library packages use local coordinates relative to their own origin and
must never be shifted, or every footprint in the file would deform.

### Layer tables

Each output file takes its layers from documents of its own kind. This is not a
detail: a schematic writes the copper layers as `visible="no" active="no"` because
it never draws on them, so a board built from the schematic's table opens with
every footprint invisible and every layer locked. An early version pooled both and
produced exactly that.

### The outline

The merged board draws one rectangle on layer 20 and discards the sub-boards' own
dimension geometry, since eight overlapping outlines are not a board shape.
Placement then runs with the outline's width as a packing constraint and its
top-left as the origin, so the boards land inside the shape that will be made.
A board too wide for the outline is still placed rather than dropped, and the
overflow is reported with the size actually needed.

`keep` is the only value that preserves the source outlines. A size replaces them,
and `none` removes them without drawing a replacement.

The default lives in `layout.DEFAULT_OUTLINE` and nowhere else; `plan.py` imports
it rather than repeating the literal, so the two cannot drift apart.

### Hand placement

A `Spot` in the plan pins one design in one view. `layout.pin` applies them, and
it rewrites both halves of a `Placement`: the translation applied to the board's
geometry and the edges that translation produces. Setting one without the other
would draw a board in one place and write it out in another.

Pins are applied **after** the search rather than constraining it. Constraining
the search would be defensible, but applying afterwards means the boards left to
the packer are still arranged well among themselves, which is what someone
pinning one board actually wants. The consequence is that the search's own cost
figures no longer describe the result, so `report.after` is recomputed from the
final placements.

The two views are pinned independently. A board sits where the copper has to go
and a drawing sits where it reads well, so nothing is gained by tying them
together. `MergePlan.spots(view)` returns one view's pins and the sheet tiler and
the board placer each ask for their own.

A pin naming a design that is not in the merge is ignored rather than being an
error: changing the copy count or unticking a design should not invalidate the
positions of everything else.

### Packing and search

`_shelf()` packs boards into rows sized to their tallest member, targeting a roughly
square result. Uniform cells, still available as `--layout grid`, pay for the largest
board on every slot.

`optimize()` hill-climbs over board orderings. Each candidate ordering is re-packed
and scored, so the search changes both which board sits where and the shelf geometry
that follows from it. Cost is a weighted sum of airwire length and bounding area,
each normalised against the starting arrangement so two quantities in different
units can be added meaningfully.

Airwire length is a minimum spanning tree, per net, over the board-local centroids of
its pads, translated by each board's placement. Element origins substitute for exact
pad positions: enough to rank arrangements, and it avoids resolving package pad
geometry through rotation.

### The schematic sheet

Everything goes on one sheet. Each design's content is translated to a tile after
measuring its extent with page borders excluded, and the sheet is shelf-packed
with a wider aspect target than the board uses, because a drawing is read on
screen rather than cut from a panel.

Designs sharing a sheet have their frame parts dropped, since several overlapping
A4 borders are only noise, and each gains a caption on layer 97 placed in a gap
the tile reserves above itself. A design merged on its own is not translated at
all, so it keeps the coordinates it was drawn at.

Sharing a sheet forces net folding. EAGLE writes one `<net>` per name per sheet
with several `<segment>` children, so two elements named `GND` on one page is not
a form it accepts. `_absorb_named()` moves segments into the first element of that
name instead.

### Why there is only one sheet

A per-design sheet mode existed and was removed. Its sheets carried an empty
`<moduleinsts/>` container, produced because the builder created every child tag
whether or not the source had one. No hand-drawn EAGLE file carries that element,
and 9.6.2 would not reliably open the result. The single-sheet builder never hit
it, because it only copied across the tags its page already had.

`_sheet_body()` now emits only `plain`, `instances`, `busses` and `nets`, and a
test asserts a merged sheet's children match what a drawn sheet contains. The
lesson generalises: writing a structurally valid element that real files never
contain is still a way to produce a file the tool will not open.

## Two views of one merge

`Merger.preview()` reports the board arrangement and `Merger.sheet_preview()`
reports the sheet, both running the same code the builders run and stopping
before any XML is produced. The front end draws whichever the toggle selects.
Neither is a second implementation of the placement: a picture that disagreed
with the file would be worse than no picture.

The sheet preview reports what `_one_sheet` would do, including the special case
of a lone design, which is not tiled at all and keeps the coordinates it was drawn
at. The page reads that and stops offering to move it, rather than accepting a
drag the builder would ignore.

## Why joined nets stay unrouted

When two designs' `GND` signals merge, their copper is concatenated into one signal
but no wire is drawn between the two boards. EAGLE renders the gap as an airwire.
That is the intended output: the airwires are precisely the list of connections the
engineer still has to make, and inventing a route across a board boundary would be
worse than showing the work that remains.

## The front end

`web.py` is a thin layer. It owns no merge logic of its own: every request builds
the same `Merger` the command line builds, and the page is redrawn from whatever
that produces. The alternative, a second implementation of the rules in
JavaScript, would drift from the engine within a week.

Four endpoints do the work. `browse` opens a folder dialog. `scan` lists the
designs in a folder. `analyze` rebuilds everything from the decisions the page is
holding and returns what to draw. `merge` is the only one that touches the disk.

The picker is the operating system's, not the browser's, because a page is never
told where a chosen folder actually lives. It runs as a subprocess rather than in
the handler: Tk dislikes worker threads, and a modal dialog on a request thread
would hold the server for as long as someone left it open. Cancelling and timing
out both come back as `{"cancelled": true}` rather than an error, since neither is
a failure. `can_browse()` reports whether tkinter exists at all, and the page hides
the button when it does not.

`analyze` is called after every edit, so it has to be cheap. Two things make it
so. Parsed documents live in a module-level cache keyed by path and mtime, since
re-reading seven megabytes of XML per keystroke would make the page feel broken.
And `Merger.preview()` runs the real placement but stops before any XML is
produced, so looking costs a fraction of committing.

The page holds all the state and sends it whole with each request, which keeps
the server stateless and means a reload cannot leave the two disagreeing. Requests
carry a sequence number and stale replies are dropped, so a slow analyse cannot
overwrite a newer one.

The board picture is plain SVG built in JavaScript. Millimetres are y-up and
screens are y-down, so points are transformed in code rather than with an SVG
flip, which would mirror every label. Boards too small to hold their name are
drawn without one: labelling regardless turns a crowded arrangement into a pile
of overlapping text.

## Testing

`tests/conftest.py` builds small synthetic EAGLE designs that clash deliberately:
same part names, same library names with different pad geometry, and a net set
covering every bucket. Those tests run in milliseconds and pin the behaviour.

`tests/test_kicad_sch.py` pairs an inline drawing with the inline board from
`tests/test_kicad.py`, so the two halves can be checked against each other: the
same part names, the same net names, and no pinref to a part that was never made.

`tests/test_placement.py` covers hand placement at every level it passes
through: the geometry in `layout.pin`, the plan round trip, the merger honouring
a pin in each view, and the API the page talks to. It also checks that a pinned
drawing really moves on the written sheet, because a preview that disagreed with
the file is the failure that matters.

`tests/test_sources.py` never touches the network: GitHub is replaced with canned
payloads, which is the only way to pin behaviour that would otherwise depend on
what Adafruit published this week. It covers the interleaving, the pairing rules,
the sanitising of names, and the failures GitHub actually produces.

`tests/test_kicad.py` builds a small KiCad board inline rather than leaning on the
sample, so the parser, the axis flip, the arc maths and the net renaming are each
pinned on input small enough to read. It finishes by merging that board with two
EAGLE designs, which is the thing the feature exists for.

`tests/test_web.py` drives the API directly rather than through HTTP, which keeps
it fast and keeps the assertions about behaviour rather than transport. It pins
that analysing writes nothing, that the cache survives repeated calls but notices
an edited file, and that what the page previews is what the merge writes.

`tests/test_board_output.py` pins the things EAGLE checks and Python cannot: that
the board's copper layers are switched on, that the two files do not share one
layer table, and that the outline is drawn once with the boards inside it.

`tests/test_layout.py` checks that no arrangement style ever overlaps two boards,
that packing beats uniform cells on mixed sizes, and that the optimizer shortens
airwires without producing overlaps. `tests/test_linking.py` pins the scorer against
both the pairs it must find and the ones it must not.

`tests/test_samples.py` runs the pipeline over the eight real Adafruit designs in
`examples/basic`, checking that every library reference resolves, every reference
designator is unique, no two boards overlap, and each source board survives as a
rigid translation. It skips if the samples are absent.
