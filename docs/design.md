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
| `linking.py` | Propose connections between differently named nets. |
| `pruning.py` | Catalogue parts and work out which a set of rules removes. |
| `layout.py` | Measure boards, pack them, and search for a cheaper arrangement. |
| `plan.py` | Serialise every decision to JSON so a merge is replayable. |
| `prompt.py` | Ask about copies, replicas, contested nets and links. |
| `merge.py` | Apply the maps and build the two output documents. |
| `cli.py` | `inspect`, `plan`, `merge`, `check`. |

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

### Schematic sheets

Per-design sheets need no translation at all, which is why that is the default: the
pages keep their original coordinates and look exactly as drawn.

`packed` and `single` share one code path; `single` is just a page size equal to the
instance count. Each design's content is translated to a tile after measuring its
extent with page borders excluded, and each page is shelf-packed on its own so a big
drawing on page two costs page one nothing. Sheets use a wider aspect target than
boards, because a drawing is read on screen rather than cut from a panel.

Designs sharing a sheet have their frame parts dropped, since several overlapping A4
borders are only noise. Dropped parts are skipped in both the parts list and the
instance list. Each block gains a caption on layer 97, placed in a gap the tile
reserves above itself, so one crowded page stays navigable.

Sharing a sheet also forces net folding. EAGLE writes one `<net>` per name per sheet
with several `<segment>` children, so two elements named `GND` on one page is not a
form it accepts. `_absorb_named()` moves segments into the first element of that name
instead. Per-design sheets never hit this, because each sheet holds one design's copy
of a net; it only appears once designs meet on a page.

## Why joined nets stay unrouted

When two designs' `GND` signals merge, their copper is concatenated into one signal
but no wire is drawn between the two boards. EAGLE renders the gap as an airwire.
That is the intended output: the airwires are precisely the list of connections the
engineer still has to make, and inventing a route across a board boundary would be
worse than showing the work that remains.

## Testing

`tests/conftest.py` builds small synthetic EAGLE designs that clash deliberately:
same part names, same library names with different pad geometry, and a net set
covering every bucket. Those tests run in milliseconds and pin the behaviour.

`tests/test_board_output.py` pins the things EAGLE checks and Python cannot: that
the board's copper layers are switched on, that the two files do not share one
layer table, and that the outline is drawn once with the boards inside it.

`tests/test_layout.py` checks that no arrangement style ever overlaps two boards,
that packing beats uniform cells on mixed sizes, and that the optimizer shortens
airwires without producing overlaps. `tests/test_linking.py` pins the scorer against
both the pairs it must find and the ones it must not.

`tests/test_samples.py` runs the pipeline over the eight real Adafruit designs in
`examples/adafruit`, checking that every library reference resolves, every reference
designator is unique, no two boards overlap, and each source board survives as a
rigid translation. It skips if the samples are absent.
