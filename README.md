# pcbmerge

Combine several PCB designs into one schematic and one board.

Point it at a folder of designs and it produces a single pair that EAGLE will
open: every part renamed apart, every clashing library item kept side by side,
the source boards packed into a compact arrangement, and the power rails already
tied together. Reads EAGLE (`.sch` / `.brd`) and KiCad (`.kicad_pcb` /
`.kicad_sch`); writes EAGLE.

The hard parts are naming, because everything collides, and net resolution,
because deciding which `VCC` is the same wire as which other `VCC` is a judgement
call. Names that state the node, like `GND` and `3V3`, are joined without asking.
Names that only state a role, like `VCC` and `VIN`, are put to you, since joining
a 5 V rail to a 3.3 V one because both are called `VCC` would be a bad afternoon.

## Using it

```bash
pcbmerge web
```

That opens a page on `127.0.0.1:8765`. Nothing is written until you press Merge.

**Open a folder.** Choose folder opens your operating system's own dialog, or
paste a path. Every design in it is listed, and you set how many copies of each
you want.

**Watch the picture.** The canvas draws the merge as it would be built. Board
view shows the source boards packed inside the outline, with a line for every net
that still needs routing. Schematic view shows the shared sheet. Change anything
on the left and both redraw.

**Move things yourself.** Drag a block, or focus it and use the arrow keys, and
the packer fills in around whatever you have placed. Delete hands one back to it;
Auto-place hands back everything in that view.

**Answer the net questions.** What joined automatically is listed, what needs a
decision has a join or split toggle, and pairs that look like the same wire under
different names are offered as suggestions.

**Leave parts out.** Mounting holes, fiducials and silkscreen labels are grouped
by kind, with the ones nothing is wired to marked, so a crowded merge can be
thinned in a couple of clicks.

**Find designs online.** Adafruit, SparkFun and Seeed Studio publish their
hardware on GitHub. Search from the panel and whatever you pick is downloaded
into the project folder you have open, ready to merge with what is already there.

A GitHub token is worth adding in that panel. Without one you get a few searches
an hour and no file search, which is where most KiCad hardware is found. It needs
no permissions for public designs, is checked before it is saved, and every later
run picks it up.

**Then press Merge.** You get a `.sch` and a `.brd`. Open the board in EAGLE and
run DRC; the airwires are the joined nets waiting to be routed.

Everything the page does is also on the command line. Run `pcbmerge --help`.

## Install

Python 3.10 or newer. No dependencies.

```bash
pip install -e .
```

That puts `pcbmerge` on your PATH:

```bash
pcbmerge --version
```

Reinstalling fails while `pcbmerge web` is running, because the server holds the
executable. Stop it first.

## What it will not do

- Writes EAGLE only. Altium is not supported.
- Never re-routes copper. Joined nets are left as airwires on purpose.
- Places designs as whole blocks. Moving one part within a design is a job for
  EAGLE, on the merged file.
- Takes design rules and global attributes from the first design. Conflicts
  elsewhere are reported, not merged.

## Licence

MIT, in `LICENSE`.

The designs in `examples/basic` are not covered by it. They are hardware
published by Adafruit and SparkFun, included so the tool can be tried against
real files rather than something written to suit it, and they carry their own
terms. `examples/basic/README.md` says where each came from.
