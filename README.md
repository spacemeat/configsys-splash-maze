# configsys-splash-maze

A `maze` startup splash for [configsys](https://github.com/spacemeat/configsys) — shipped as a
trusted **code plugin**, exporting a `Splash` provider (see `configsys/splashes.py` for the ABI).

## What you see

A **centipede** crawls through a **brick maze** toward a chamber of **quivering eggs**. She winds
along corridors — twists, turns, dead ends to either side — her legs a-wiggle and a ripple running
down her body. In nooks she never visits sit little **standing pools** with a **fish** paddling
back and forth. When she reaches the nest, a small **happy ending** plays: the eggs quiver harder
and a few hearts drift up.

## How it fits a startup's timing (the trick)

We know the step budget up front, so instead of fighting a simulation to land on time, we **build
the path first**:

1. **Path first** — a winding, self-avoiding route from a top edge down to the chamber, kept out of
   the chamber until it has wandered enough, and length-tuned (a good fraction of the maze).
2. **Maze around it** — a spanning tree is then grown into every *other* room, hanging off the
   path, so the route is indistinguishable from the offshoots and dead ends. She just looks like
   she's solving a maze.
3. **The crawl is pure playback** — her head position is `progress` mapped along the route (each
   cell ≈ one component checked); the last sliver of the run is reserved for the happy ending. Legs,
   body ripple, egg quiver and fish are procedural from elapsed time.

There's no physics and effectively no precompute — generation is a couple of cheap graph walks
(≤ ~60ms even on a huge terminal), and it always lands exactly on time.

## Install

```
# in ~/.config/configsys/configsys.hu
plugins: [ { source: "github:spacemeat/configsys-splash-maze"  ref: v0.1.0 } ]
```

```
configsys plugin sync
configsys plugin trust configsys-splash-maze     # it ships code
```

Then select it (a machine setting) — `splash: maze` — or pick it from the Config screen in the TUI.

## Tests

The curses-free core (`MazeSim`: path-first generation, the crawl route, playback) is unit-tested
and deterministic given its seed; the curses renderer (`MazeSplash`) is exercised by hand:

```
PYTHONPATH=/path/to/configsys python -m pytest test -q
```

Tuning knobs at the top of `maze.py`: `PATH_LO`/`PATH_HI` (route length), `BODY_MIN`/`BODY_MAX`
(centipede length), `ENDING_FRAC` (how much of the run is the happy ending), `PLAY_EASE`.

## License

Same as configsys.
