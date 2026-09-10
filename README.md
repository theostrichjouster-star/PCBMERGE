# pcbmerge

Combine several PCB designs into a single schematic and board, resolving net names
automatically where the answer is certain and asking where it is not. Reads EAGLE
(`.sch` / `.brd`) and KiCad (`.kicad_pcb`), and writes EAGLE.

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

## The visual front end

The command line makes you decide before you can see anything. `pcbmerge web`
serves the same engine over HTTP so you can decide against a picture:

```bash
pcbmerge web examples/adafruit
```

That opens a browser on `127.0.0.1:8765` showing the merged board as it would be
built: the outline, every source board packed inside it, and a copper line for
each net that still needs routing. Change anything on the left and the picture
redraws.

**Choose folder** opens your operating system's own folder dialog. A browser will
not tell a page where a chosen folder really lives, so the server opens the dialog
instead, in a separate process: a modal window on a request thread would hold the
server for as long as you left it open. You can still paste a path into the box
beside it, which is the only way in on a machine with no display.

- **Designs** tick designs in or out and set how many copies of each
- **Board** outline, gap, tiling and what the placement search optimises for
- **Parts to leave out** every kind of part with its copy count, the ones nothing
  is wired to starred, and one button to drop all of them
- **Nets** what joined automatically, what needs a decision with a join/split
  toggle, and the pairs that look like the same wire under different names
- **Connections** wire one design's net to another's
- **Write files** name, folder, and the Merge button

Drag the divider between the panel and the canvas to give either one more room;
the width is remembered. Arrow keys move it too, and double-clicking it goes back
to the middle.

The server reads the page from disk on every request but answers from the code it
started with, so after an upgrade a running server can serve a page it is too old
to answer. Stop it and start it again.

Hovering a net highlights its airwires on the board; hovering a board names it.
The numbers across the top are live, so the cost of a choice is visible before
you commit to it. Joining the I2C bus on the eight sample designs takes the
airwire total from 425 mm to 556 mm, which is the sort of thing worth seeing
while you decide rather than afterwards.

Nothing is written until you press Merge. Everything else only reads.

The server binds to the loopback address and reads and writes files as you, which
is right for a tool you start yourself and wrong for anything exposed to a
network. It needs no internet connection and loads nothing from a CDN.

## Placing designs yourself

The packer arranges everything by default. When you want a particular board in a
particular corner, put it there: drag it, or focus it and use the arrow keys.
Everything you have not touched is still packed around what you have.

The front end draws two views of the same merge, and the toggle above the canvas
switches between them.

- **Board** is the arrangement inside the outline, with the airwires that will
  still need routing.
- **Schematic** is the shared sheet, one block per design, which is what the
  single output sheet will look like.

A design is placed separately in each. Pinning a board does not move its drawing,
because the two have nothing to do with each other: a board sits where the copper
has to go, a drawing sits where it reads well.

Dragging snaps to the millimetre on the board and to a tenth of an inch on the
sheet, which is the grid EAGLE draws schematics on. Hold Shift to move freely.
Arrow keys nudge by one snap and Shift with an arrow by ten, so the whole
arrangement is reachable without a mouse. Delete hands a block back to the packer,
and **Auto-place** hands back everything in the current view.

A block placed by hand is outlined in orange with a dot in its corner, and the
count is reported under the canvas. The cost shown above it is measured from where
things actually ended up, not from the arrangement the search settled on before
your placements were applied.

Hand placements are part of the plan, so a merge that used them replays exactly:

```json
"positions": [
  { "design": "wio_terminal", "view": "board", "x": 32.0, "y": 45.5 }
]
```

## Finding designs to merge

Adafruit, SparkFun and Seeed Studio publish their hardware on GitHub as the same
EAGLE and KiCad files this tool reads. `search` looks through all three accounts
at once, `fetch` brings a design down into a folder, and that folder is then an
ordinary input.

```bash
pcbmerge search bme280
pcbmerge fetch adafruit/Adafruit-BME280-Breakout-PCB --all --dest downloads
pcbmerge merge downloads -o combo --out-dir out --yes
```

```
6 repositories

  adafruit/Adafruit-BME280-Breakout-PCB  Adafruit, 11 star(s), updated 2019-06-21
    PCB files for the Adafruit BME280 Breakout

  sparkfun/Qwiic_Atmospheric_Sensor_Breakout_BME280  SparkFun, 2 star(s), updated 2024-07-23
    A basic Qwiic board to provide atmospheric data from the BME280.
```

Results come back a row at a time from each account, so one vendor cannot crowd
out the others, and hardware is pulled above software before they are shown.
Searching a part number otherwise returns the driver library long before the board
it drives, because that is what people star and link to.

`--vendor` narrows the search to one account, and `--designs` looks inside each
result rather than only naming it:

```bash
pcbmerge search qwiic --vendor sparkfun --designs
```

`fetch` on its own lists what a repository holds and writes nothing:

```
adafruit/Adafruit-BME280-Breakout-PCB  Adafruit, branch master
    Adafruit BME280                            eagle, .brd, .sch
```

Add `--all` to take every complete pair, or `--design` with a name or wildcard to
take one. Both halves of a design are written under a single stem, because the
merge finds the board by the schematic's name. Nothing from the repository is used
as a path: only the extension survives, and a second copy of the same design gets
its own stem rather than overwriting the first.

The front end has the same thing as a panel. Tick the vendors, type a search, open a
result to see its designs, and importing downloads them and opens the folder ready
to merge, without leaving the page.

### Rate limits

GitHub allows about sixty unauthenticated requests an hour, and searches are counted
separately at ten a minute. A search costs one request per vendor, and looking inside
a repository costs one more. Answers are cached for ten minutes, and the front end
only looks inside the first few results, leaving the rest until they are opened.

Setting `GITHUB_TOKEN` (or `GH_TOKEN`) to a personal access token raises the limit
considerably. No scopes are needed for public repositories.

## KiCad designs

A `.kicad_pcb` can go into a merge beside EAGLE files, with no flag and nothing to
convert by hand. Point the tool at a folder holding both and it works out which is
which.

```bash
pcbmerge merge examples/adafruit -o combo --out-dir out --yes
```

The board is the source of truth. A KiCad board carries the whole netlist, every
footprint with its pads and their nets, the copper and the outline, which is
everything a merge needs. Footprints become packages, nets become signals, tracks
and vias and pours come across, and the outline lands on the Dimension layer.

Two conventions differ and both are handled. KiCad measures Y downwards where EAGLE
measures it up, so every Y is negated and rotations change sign with it. Footprints
on the back come across mirrored.

Net names are normalised so they can match. A hierarchical KiCad name like
`/Sheet One/VCC_3V3` becomes `VCC_3V3`, or no rail would ever line up with an EAGLE
design's. Names KiCad invented, the `Net-(U1-Pad2)` form, become `N$1` and so are
kept apart exactly as EAGLE's own anonymous nets are.

**The schematic is drawn from the netlist**, not from the `.kicad_sch`. Each part
becomes a box with one pin per pad, and connections are carried on net labels. It is
not the drawing the engineer made and is not meant to be: it is a faithful, openable
statement of the same connections, consistent with the board by construction. The
merge says so in its report rather than leaving you to notice.

Converting a KiCad schematic drawing faithfully is a separate and much larger job:
symbols, wires, buses, hierarchical labels and sheet pins all have to be redrawn in
EAGLE's model. The netlist route gives a correct merge today.

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
- **Two tools, two file formats.** KiCad stores S-expressions and measures Y the
  other way up.

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

## Leaving parts out

Eight breakout boards bring eight page borders, twenty-two mounting holes,
sixteen fiducials and ninety-four silkscreen pin labels. On one merged board
almost none of that is wanted: the holes sit at each sub-board's old position and
the fiducials belong to panels that no longer exist.

See what is there, grouped by kind:

```bash
pcbmerge parts examples/adafruit
```

```
461 parts in 8 design instances
  161 of them are decoration: borders, holes, fiducials, silkscreen labels

 kind                          value        copies  designs   note
*PLABEL                                         94        4   no connections
*MOUNTINGHOLE                                   22        8   no connections
*FIDUCIAL                                       16        8   no connections
*FRAME_A4                                        4        4   no connections
```

Then leave kinds out with `--drop`, which matches the designator, the deviceset
or the package, case-insensitively:

```bash
pcbmerge merge examples/adafruit --drop MOUNTINGHOLE --drop FIDUCIAL --drop PLABEL
```

Restrict a rule to one design with `esp32:FID*`. A rule matching nothing is an
error, not a silent no-op, so a typo cannot quietly keep parts you meant to remove.

Dropping is decided before anything is renamed, so a removed part never claims a
designator a survivor could have had. It reaches the schematic and the board
together, pins are pulled out of the nets they were on, and a net whose every pin
belonged to removed parts goes too, on both sides at once. Removing something that
was actually wired to a net is reported, since that changes the netlist:

```
Warnings
  dropped Adafruit_MAX31850:R1, which had 4 connection(s)
```

Copper belonging to a signal that survives is kept even when one of its parts
went away. It is real routing, and deleting it silently would be worse than
leaving a stub you can see.

## Connecting specific designs

`--link` acts on a name wherever it appears. That is right for a bus, and wrong
when a controller drives one board out of four copies. `--connect` names the
instances:

```bash
pcbmerge merge controller.sch relay.sch*3 \
  --connect 1:A0=2:SIGNAL --connect 1:A1=3:SIGNAL
```

Designs can be named by their listing number or by any unambiguous part of their
name. The result is exactly what you asked for and nothing more:

| net | reaches |
| --- | --- |
| `A0` | controller, relay copy 1 |
| `A1` | controller, relay copy 2 |
| `SIGNAL` | relay copy 3, on its own |

Connecting two nets that live on the same design is refused, and so is naming a
net no design has.

Interactively, `merge` asks for these after the copy counts:

```
Connect nets between designs?  Enter to skip.
Write them as  design:net = design:net,  for example
  1:GPIO5 = 2:SIGNAL

   1  Adafruit_ESP32-S3_8MB_No_PSRAM
   2  Adafruit_Non-Latching_Relay_Breakout #1

  connection (Enter when done):
```

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

## The board outline

The merged board gets one plain rectangle on the Dimension layer, 150 by 100 mm
by default, and the sub-boards are packed to fit inside it. Each source board's
own outline is discarded, because carrying eight of them over leaves a pile of
overlapping rectangles rather than a board shape.

```bash
pcbmerge merge examples/adafruit --outline 80x100
```

| Value | Result |
| --- | --- |
| `150x100` | one rectangle that size (default) |
| any `WxH` | one rectangle of your dimensions |
| `keep` | every source board's outline, carried over in place |
| `none` | outlines removed, nothing drawn |

If the sub-boards do not fit, they are still placed and the overflow is reported
with the size they actually need:

```
the sub-boards need 55 x 333 mm and overflow the 150 x 100 mm outline;
give --outline a bigger size or move them by hand
```

## The schematic

Every design lands on one sheet. Each drawing is packed into rows sized to their
tallest member, so a page of one large and three small drawings does not pay for
the large one four times, and each block is captioned with its design name on
layer 97 (Info) so a crowded page stays navigable.

Page borders are dropped, since eight overlapping A4 frames are only noise. A
single design merged on its own keeps both its border and its original
coordinates.

Nets of the same name fold into one element carrying several segments. EAGLE
writes one `<net>` per name per sheet, and two elements named `GND` on one sheet
is not a form it accepts. On the eight samples, `GND` becomes one net with 88
segments reaching all eight designs.

Multi-sheet output was built and withdrawn. It emitted an empty `<moduleinsts/>`
container that no hand-drawn file carries, and EAGLE 9.6.2 would not reliably open
the result. One sheet is what works, so one sheet is what there is.

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
- `--link A=B:NAME` tie differently named nets together everywhere
- `--connect 1:A0=2:SIGNAL` wire named designs' nets together
- `--drop MOUNTINGHOLE` leave parts out; `--no-prune` skips the question
- `--no-suggest` skip the differently-named-net questions
- `--layout pack|grid|row|column` and `--optimize balanced|airwire|area|none`
- `--outline 150x100`, or `keep` / `none`
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
  "connections": [{"members": [["esp32", "A0"], ["relay #1", "SIGNAL"]], "name": "A0"}],
  "drops": ["MOUNTINGHOLE", "FIDUCIAL"],
  "layout": "pack",
  "optimize": "balanced",
  "outline": "150x100"
}
```

### web

Open the visual front end described above.

```bash
pcbmerge web [folder] [--port 8765] [--no-browser]
```

### search

```bash
pcbmerge search [terms ...] [--vendor ORG] [--limit N] [--designs]
```

Finds repositories in the vendor accounts. With no terms it lists the most popular
in each. `--vendor` is repeatable and takes either form of a name, `sparkfun` or
`SparkFun`.

### fetch

```bash
pcbmerge fetch OWNER/NAME [--design NAME] [--all] [--dest DIR]
```

Lists the designs in a repository, or downloads them. A `github.com` URL works in
place of `owner/name`. Without `--design` or `--all` nothing is written.

### parts

List every part, grouped by kind so a decision covers all its copies at once.
Kinds nothing is wired to are starred.

```bash
pcbmerge parts examples/adafruit --all
```

### check

Verify a `.sch`/`.brd` pair references nothing that does not exist. Works on any
EAGLE pair, not just merged output, which is how you tell an inherited problem from
one the merge introduced.

```bash
pcbmerge check out/combo
```

## What the merged files look like

**Schematic.** One sheet holding every design, each captioned, with same-named
nets folded into a single element.

**Board.** Source boards are tiled into a packed arrangement inside a single
outline, each moved as a rigid body so relative placement, rotation and routing
survive intact. Joined nets appear as airwires spanning the sub-boards, which is
the list of connections you still have to route.

Each file keeps the layer table of its own kind. A schematic marks the copper
layers hidden because it has no use for them, and a board needs exactly those
layers switched on, so the two tables are not interchangeable.

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
3. Adjust the arrangement if you want, and resize the outline to suit.

## Install

Python 3.10 or newer, no dependencies.

```bash
pip install -e .
```

That puts a `pcbmerge` command on your PATH. Check it with:

```bash
pcbmerge --version
```

If you would rather click than type, `pcbmerge web` opens the visual front end.

Run the tests with `pip install -e ".[dev]"` then `pytest`.

## A first run

Start with `inspect`, which writes nothing and tells you what a merge would do:

```bash
pcbmerge inspect examples/adafruit
```

Then merge. Without `--yes` it asks about parts to drop, connections to make and
contested nets; with it, everything takes the documented default:

```bash
pcbmerge merge examples/adafruit -o combo --out-dir out --yes
```

You get `out/combo.sch` and `out/combo.brd`. Open the board in EAGLE, run DRC, and
the airwires you see are the joined nets waiting to be routed.

To check the result without opening EAGLE:

```bash
pcbmerge check out/combo
```

## Limitations

- Writes EAGLE only. Reads EAGLE and KiCad; Altium is not supported.
- Search covers the three vendor accounts only, and reads public repositories.
- Designs are placed by hand as whole blocks. Moving one part within a
  design is a job for EAGLE, on the merged file.
- A KiCad schematic's drawing is not converted. The schematic is rebuilt from the
  board netlist as boxes with one pin per pad.
- KiCad copper pours come across as their outline polygons, not as the filled shape
  KiCad computed.
- Design rules, autorouter settings and global attributes come from the first
  design; conflicts elsewhere are reported as warnings, not merged.
- Buses are copied per sheet but never joined across designs.
- Placement search permutes which board goes where. It does not rotate boards or
  attempt non-rectangular nesting.
- Copper is never re-routed. Joined nets are left as airwires on purpose.
