# pcbmerge

Combine several Autodesk EAGLE designs into a single schematic and board, resolving
net names automatically where the answer is certain and asking where it is not.

Point it at a folder of `.sch`/`.brd` pairs and it produces one merged pair that
EAGLE will open: every part renamed apart, every library conflict preserved, the
source boards packed into a compact arrangement, and the power rails already tied
together.

```bash
pcbmerge merge examples/adafruit -o combo --out-dir out
```

Place several copies of a design by appending `*N`:

```bash
pcbmerge merge controller.sch relay.sch*4 -o farm --out-dir out
```

## The problem it solves

Dropping two EAGLE designs into one file breaks in four separate ways at once.

- **Reference designators collide.** Nearly every breakout board has an `R1` and a
  `U$1`. Across the eight sample designs, `U$2` appears eight times.
- **Libraries collide without being equal.** The samples carry eight different
  libraries all called `microbuilder`, with genuinely different footprints inside.
  Taking the first copy silently changes other boards' pad geometry.
- **Net names mean different things.** `GND` in two designs is one node. `N$1` in
  two designs is two unrelated nodes. `VCC` might be either, and only you know.
- **Boards sit on top of each other.** Every design is drawn near its own origin.

pcbmerge handles all four, and keeps the schematic and the board consistent with
each other, which is what EAGLE requires before it will let you route anything.

## How nets are resolved

Every net name is sorted into one of four buckets.

| Bucket | Examples | What happens |
| --- | --- | --- |
| Joined automatically | `GND`, `VSS`, `0V`, `3.3V`, `+3V3`, `5V`, `VBUS` | One net across all designs |
| Kept separate always | `N$1`, `N$7` | Renamed per design, never fused |
| Asked about | `VCC`, `VDD`, `VIN`, `AGND`, `SDA`, `SCL`, `D+` | You decide |
| Asked about, per copy | any net in a replicated design | One per copy, or common to all |

The rule behind the split is whether the name states its own meaning. `GND` and
`3.3V` do, so matching names are safe to join. `VCC` names a role instead of a
voltage, so two boards can disagree about it, and joining them could put five volts
onto a three-volt part. Those always come to you.

Spelling differences are folded before comparison, so `3.3V`, `+3V3` and `3V3` are
recognised as one rail. The merged file uses whichever spelling your inputs use
most, with ties going to the design you listed first.

Two nets inside a *single* design are never fused, even when they normalize alike.
A board carrying both `3.3V` and `+3V3` has two nodes, and it keeps two.

## Placing copies of a design

Four relay boards on one panel is four instances of one file. Each copy gets a
numbered prefix, so `R1` becomes `RELAY1_R1` through `RELAY4_R1`, and the same
number carries onto the nets: `SIGNAL` becomes `RELAY1_SIGNAL` and so on. Parts,
nets and footprints all increment together, which is what keeps the schematic and
the board consistent.

Rails are the exception. `GND` and `3.3V` stay a single net across every copy,
because a name that states its own voltage means the same node wherever it appears.

Everything else is a question, asked once per design rather than once per copy:

```
Adafruit_INA3221_Breakout: 3 copies.
Which of these signals are common to all copies?
Anything you do not pick becomes one net per copy.

   1  ALERT
   2  SCL
   3  SDA
   4  WARNING

  numbers, 'a' for all, Enter for none: 2 3
```

Three copies of a current sensor share one I2C bus but have three separate alert
lines. Three copies of a relay board share nothing but power. No rule can tell those
apart, so the tool lists the candidates and lets you pick.

Use `--replicas join` to make every replicated net common without being asked, or
`--replicas split`, the default, to keep them all separate.

## Connecting nets that are not spelled alike

Name matching only finds the easy cases. A sensor board calling its bus `I2C_DATA`
and a controller calling it `SDA` describe the same wire, and no normalisation rule
will discover that. pcbmerge scores likely pairs and offers them:

```
[1/2]  confidence 78%
  SDA                  Adafruit_INA3221_Breakout
  I2C_DATA             Adafruit_ESP32-S3_8MB_No_PSRAM
  why      same words plus I2C
  connect these? [n] y/n/rename
```

The scorer is deliberately conservative, because a wrong suggestion costs more
attention than a missed one. A missed connection stays visible as an unrouted net,
while a wrong one has to be spotted and undone. Connector pin labels like `A0` and
`D13` are never matched against each other, since they name a position rather than a
signal. On the eight sample designs it proposes two pairs, not dozens.

Answers default to no. To skip these questions entirely, pass `--no-suggest`.

You can also state connections outright, which is what a plan records:

```bash
pcbmerge merge examples/adafruit --link SDA=I2C_DATA:BUS_SDA --link SCL=I2C_CLK
```

A link naming a net no design has is an error rather than a silent no-op, because
linking a real net to a typo would quietly join the real one everywhere.

## Board placement

Source boards are packed rather than dropped into uniform cells, then the
arrangement is searched for one that is both compact and short on airwires. Boards
sharing many nets end up adjacent.

```
Board layout
                 size (mm)    fill   airwire (mm)
  start       76.1 x  112.8     57%            549
  chosen      64.7 x  112.8     67%            420
  11 improvement(s) over 4000 tries; airwire 129 mm shorter, board 1289 mm2 smaller
```

Airwire length is measured as a minimum spanning tree over each net's pad positions,
which is the cheapest set of hops a router could use. Board element origins stand in
for exact pad locations. That is accurate enough to rank arrangements, and much
cheaper than resolving every footprint's pad geometry through its rotation.

Control it with `--optimize`:

| Value | Minimises |
| --- | --- |
| `balanced` | both, weighted toward airwire (default) |
| `airwire` | connection length only |
| `area` | board area only |
| `none` | nothing, keeps input order |

Pick the tiling with `--layout pack` (default), `row`, `column`, or `grid` for
uniform cells.

## Schematic layout

Three options, set with `--sheet-layout`:

| Value | Result |
| --- | --- |
| `per-design` | one sheet per design instance (default) |
| `packed` | several designs per sheet, count set by `--sheets-per-page` |
| `single` | every design on one sheet |

The default leaves each design's coordinates untouched, so every page looks exactly
as it was drawn. Because EAGLE treats one net name as one net across all sheets,
joining a rail needs no wires drawn between pages.

To put everything on one sheet:

```bash
pcbmerge merge examples/adafruit -o combo --out-dir out --sheet-layout single
```

The eight sample designs become a single 705 by 576 mm sheet. Each design is packed
into rows sized to their tallest member rather than into uniform cells, so a page of
one large and three small drawings does not pay for the large one four times.

Two things change when designs share a sheet. Page borders are dropped, since eight
overlapping A4 frames are only noise. And each block gets a caption naming its design,
drawn on layer 97 (Info) so it never affects connectivity.

Nets that were joined also fold into a single element per name. EAGLE writes one
`<net>` per name per sheet carrying several segments, and two same-named nets on one
sheet is not a form it accepts. On the samples, `GND` becomes one net with 88
segments reaching all eight designs.

One sheet stops being practical at some size. Past about a metre and a half the merge
says so and suggests `--sheet-layout packed` with a page count instead.

## Commands

### inspect

See what would happen before anything is written.

```bash
pcbmerge inspect examples/adafruit
```

Reports each design's copy count and prefix, the library items that will need
renaming, the nets that will join automatically, the ones needing a decision, and
the differently named nets that might belong together.

### merge

Do the work. Without `--yes` it asks about links, copies and contested nets.

```bash
pcbmerge merge examples/adafruit -o combo --out-dir out
```

```
[1/16]
  net      SDA
  used by  2 designs: Adafruit_ESP32-S3_8MB_No_PSRAM, Adafruit_INA3221_Breakout
  why ask  shared signal name
  join / split / rename? [s]
```

Answer `j` to join, `s` to keep apart, `r` to join under a name you choose, or `a`
to apply the default to everything remaining.

Useful flags:

- `--yes` never ask, take the defaults for everything contested
- `--default join|split` what "everything contested" means, default `split`
- `--replicas join|split` whether nets are common across copies, default `split`
- `--ask-counts` ask how many copies of each design to place
- `--link A=B:NAME` tie differently named nets together
- `--no-suggest` skip the differently-named-net questions
- `--layout pack|grid|row|column` and `--optimize balanced|airwire|area|none`
- `--sheet-layout per-design|packed|single` and `--sheets-per-page 4`
- `--gap 5` and `--columns 3`
- `--prefix LEFT --prefix RIGHT` choose reference-designator prefixes yourself
- `--save-plan used.json` record the answers you gave

### plan

Separate deciding from doing. Writes a JSON file of every decision, which you can
edit by hand, commit, and replay.

```bash
pcbmerge plan examples/adafruit -o merge-plan.json
$EDITOR merge-plan.json
pcbmerge merge --plan merge-plan.json --out-dir out
```

Each entry says what it is and why:

```json
{
  "key": "VIN",
  "name": "VIN",
  "action": "split",
  "kind": "ambiguous",
  "designs": ["Adafruit_MAX31850", "Adafruit_Non-Latching_Relay_Breakout"],
  "note": "role-named rail; voltage differs between designs unless you say otherwise"
}
```

Change `action` to `join` and set `name` to whatever the merged net should be
called. Re-running with the same plan gives the same output every time.

A plan also carries copy counts, hand-made links, and the layout settings:

```json
{
  "designs": [{"name": "relay", "prefix": "RELAY_", "sch": "relay.sch", "count": 4}],
  "links": [{"keys": ["SDA", "I2C_DATA"], "name": "BUS_SDA"}],
  "layout": "pack",
  "optimize": "balanced",
  "sheet_layout": "per-design"
}
```

### check

Verify a `.sch`/`.brd` pair references nothing that does not exist. Works on any
EAGLE pair, not just merged output, which is how you tell an inherited problem from
one the merge introduced.

```bash
pcbmerge check out/combo
```

## What the merged files look like

**Schematic.** Each design instance becomes its own sheet, named after the design it
came from, unless you pack several per page or ask for a single sheet.

**Board.** Source boards are tiled into a packed arrangement, each moved as a rigid
body so relative placement, rotation and routing survive intact. Joined nets appear
as airwires spanning the sub-boards, which is the list of connections you still have
to route.

**Names.** Every part gets a short prefix from its design, so `R1` becomes
`ESP32S3_R1`. Library items that clash by name but differ in content are kept side
by side as `0603` and `0603$2`, with every reference rewritten to point at the copy
its own design was drawn with.

**Managed libraries.** Cloud library URNs are dropped, converting those parts to
local copies. EAGLE would otherwise try to re-sync a library whose items have been
renamed, and fail.

## Working with the output

The merged board is a starting point, not a finished layout. After opening it:

1. Run ERC on the schematic and DRC on the board.
2. Look at the airwires. Those are the joined nets, currently unrouted between
   sub-boards.
3. Adjust the arrangement if you want, then draw a single outline on the Dimension
   layer and delete the inherited ones.

## Install

Python 3.10 or newer, no dependencies.

```bash
pip install -e .
```

Run the tests with `pip install -e ".[dev]"` then `pytest`.

## Limitations

- EAGLE XML only (`.sch` / `.brd`). KiCad and Altium are not supported.
- Design rules, autorouter settings and global attributes come from the first
  design; conflicts elsewhere are reported as warnings, not merged.
- Buses are copied per sheet but never joined across designs.
- Placement search permutes which board goes where. It does not rotate boards or
  attempt non-rectangular nesting.
- The board outline is not recomputed. Each source outline is carried over in
  place, so you get several rectangles rather than one board shape.
- Copper is never re-routed. Joined nets are left as airwires on purpose.
