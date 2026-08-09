# configsys-splash-maze

A `maze` startup splash for [configsys](https://github.com/spacemeat/configsys) — shipped as a
trusted **code plugin**, exactly like a driver plugin, exporting a `Splash` provider (see
`configsys/splashes.py` for the ABI).

## What you see

A 2-D **brick maze** fills the screen — walls are courses of bricks (each brick is the two
side-by-side block glyphs `🬗🬤`, a brick-red glyph on cement-grey mortar, laid in a running bond).
Somewhere in the lower half is a small sealed **chamber** with a single doorway high in one side
wall, holding a little fish flopping on dry stone.

A random-coloured **liquid** pours in through a gap at the top and floods the maze under
hydrostatic physics:

- streams **fall** for free and **sheet across ledges** to find the next drop (a cascade), pooling
  only where they must — `▓` mid-stream, `▏`/`▕` hugging a wall, `🮕`/`🮖` shimmering at the inlet;
- a body of water finds **one common level** and can climb an adjacent passage (**U-tube**);
- the chamber, sealed but for its one side door, stays dry until the surrounding water rises to the
  door — then it **backfills** floor-to-ceiling (`▁▂▃▄▅▆▇█`), and the fish, now afloat, stops
  flopping and **swims**.

The simulation is **done when the chamber is full**.

## How it's timed

The pour is stepped **forward in time** — `MazeSim` precomputes one frame per step, so the timeline
*is* the flow: it starts bone dry, the stream is revealed as its front **descends** from the inlet,
and pools rise. When the outside water actually reaches the chamber's door it **spills through**
into the chamber — drained from the outside pool (conserved) at a **head-driven rate**, a visible
waterfall down the chamber wall — so the chamber fills *because the maze's own water got there*, one
coupled flow, not a private tap. The outside then holds at the sill, spilling its inflow in, until
the chamber brims. That timeline is played back against configsys's inspection **progress** — eased
and rate-capped — so the chamber tops off just as inspection completes.

Pooling is a fast *level-flood* (surfaces are instantly flat, no slow relaxation): for a surface
height `L`, a breadth-first flood from the inlet lets water fall and cascade freely and pool only
through submerged cells, finding one common level (U-tube) and overflowing the lowest lip. The
outside is one reservoir (volume↔level via the flood profile); the chamber is a second, joined by
the door as a **finite orifice** — a head-driven flux between them. The door conductance (`DOOR_C`)
and flow rate (`INJECT`) are the tuning knobs. Constant inflow → constant flow rate; volume
conserved by construction.

## Install

```
# in your configsys user config (~/.config/configsys/configsys.hu)
plugins: [ { source: "github:spacemeat/configsys-splash-maze"  ref: v0.1.0 } ]
```

```
configsys plugin sync
configsys plugin trust configsys-splash-maze     # it ships code
```

Then select it as the startup splash (a machine setting):

```
splash: maze
```

or pick it from the Config screen in the TUI.

## Tests

The curses-free core (`MazeSim`: generation, the level-flood pour, playback) is unit-tested and
deterministic given its seed; the curses renderer (`MazeSplash`) is exercised by hand:

```
PYTHONPATH=/path/to/configsys python -m pytest test -q
```

## License

Same as configsys.
