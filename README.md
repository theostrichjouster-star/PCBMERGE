# pcbmerge

Combine several Autodesk EAGLE designs into a single schematic and board, resolving
net names automatically where the answer is certain and asking where it is not.

Point it at a folder of `.sch`/`.brd` pairs and it produces one merged pair that
EAGLE will open: every part renamed apart, every library conflict preserved, the
source boards tiled side by side, and the power rails already tied together.

```bash
pcbmerge merge examples/adafruit -o combo --out-dir out
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

Every net name is sorted into one of three buckets.

| Bucket | Examples | What happens |
| --- | --- | --- |
| Joined automatically | `GND`, `VSS`, `0V`, `3.3V`, `+3V3`, `5V`, `VBUS` | One net across all designs |
| Kept separate always | `N$1`, `N$7` | Renamed per design, never fused |
| Asked about | `VCC`, `VDD`, `VIN`, `AGND`, `SDA`, `SCL`, `D+` | You decide |

The rule behind the split is whether the name states its own meaning. `GND` and
`3.3V` do, so matching names are safe to join. `VCC` names a role instead of a
voltage, so two boards can disagree about it, and joining them could put five volts
onto a three-volt part. Those always come to you.

Spelling differences are folded before comparison, so `3.3V`, `+3V3` and `3V3` are
recognised as one rail. The merged file uses whichever spelling your inputs use
most, with ties going to the design you listed first.

Two nets inside a *single* design are never fused, even when they normalize alike.
A board carrying both `3.3V` and `+3V3` has two nodes, and it keeps two.

## Commands

### inspect

See what would happen before anything is written.

```bash
pcbmerge inspect examples/adafruit
```

Reports each design's part and net counts, the library items that will need
renaming, the nets that will join automatically, and the ones needing a decision.

### merge

Do the work. Without `--yes` it asks about contested nets one at a time.

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

- `--yes` never ask, take `--default` for everything contested
- `--default join|split` what "everything contested" means, default `split`
- `--layout grid|row|column`, `--gap 5`, `--columns 3` how source boards are tiled
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

### check

Verify a `.sch`/`.brd` pair references nothing that does not exist. Works on any
EAGLE pair, not just merged output, which is how you tell an inherited problem from
one the merge introduced.

```bash
pcbmerge check out/combo
```

## What the merged files look like

**Schematic.** Each input becomes its own sheet, named after the design it came
from. Sheet coordinates are untouched, so every page looks exactly as it did.
Because EAGLE treats one net name as one net across all sheets, joining a rail
needs no wires drawn between pages.

**Board.** Source boards are tiled into a grid with a configurable gap, each moved
as a rigid body so relative placement, rotation and routing survive intact. Joined
nets appear as airwires spanning the sub-boards, which is the list of connections
you still have to route.

**Names.** Every part gets a short prefix from its design, so `R1` becomes
`ESP3S3_R1`. Library items that clash by name but differ in content are kept side
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
3. Move the sub-boards into the arrangement you actually want, then draw a single
   outline on the Dimension layer and delete the inherited ones.

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
- The board outline is not recomputed. Each source outline is carried over in
  place, so you get several rectangles rather than one board shape.
- Copper is never re-routed. Joined nets are left as airwires on purpose.
