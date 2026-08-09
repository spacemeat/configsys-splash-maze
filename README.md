# configsys-splash-maze

A `maze` startup splash for [configsys](https://github.com/spacemeat/configsys) — shipped as a
trusted **code plugin**, exactly like a driver plugin, exporting a `Splash` provider (see
`configsys/splashes.py` for the ABI).

## What you see

A 2-D **brick maze** fills the screen — walls are courses of bricks (each brick is the two
side-by-side block glyphs `🬗🬤`, in brick-red and grey, laid in a running bond). Somewhere in the
lower half is a small sealed **chamber** with a single doorway high in one side wall, holding a
little fish flopping on dry stone.

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

The fill must be exact and land on time, so the whole constant-inflow pour is **solved once up
front** (`MazeSim` precomputes a timeline of frames ending the instant the chamber tops off), then
played back against configsys's inspection **progress** — eased and rate-capped — so the chamber
fills just as inspection completes: no added latency, no dead air. Frames are spaced by equal
**volume**, so the on-screen flow rate is constant; volume is conserved by construction.

The pour is a *level* model: for a surface height `L`, a breadth-first flood from the inlet lets
water fall and cascade freely and pool only through submerged cells, so it finds a common level,
overflows the lowest lip, and a side-doored pocket floods only once `L` reaches its door. The
chamber then fills as an explicit second phase (outside held, chamber rising floor→ceiling), which
is what makes its fill gradual and last.

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
