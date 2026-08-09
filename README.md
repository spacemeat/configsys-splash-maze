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

The build is **deferred to the first render**, when the component count is known, and the path is
built to match it:

1. **Path first, sized to the load** — she wanders `components / COMPONENTS_PER_WANDER_ROOM` rooms
   (a winding, self-avoiding walk), then beelines to the chamber. So a bigger load ⇒ a windier,
   longer trip *at the same crawl speed*.
2. **Maze around it** — a spanning tree is grown into every *other* room, hanging off the path, so
   the route is indistinguishable from the offshoots and dead ends.
3. **Crawl is pure playback** — her head is `progress` mapped along the route; the route's tail
   serpentines the egg chamber so her **whole body coils onto the floor**, then the happy ending
   plays. Legs, body ripple, egg quiver and the fish are procedural from elapsed time.

There's no physics and no heavy precompute — generation is a couple of cheap graph walks (built once
on the first frame), and it always lands on time.

**Tuning** (top of `maze.py`): `COMPONENTS_PER_WANDER_ROOM` — *lower = windier & a bit faster*;
`BEELINE_CHOICES` — raise for a meandering (windier, less predictable) final approach;
`BODY_MIN/MAX`, `ENDING_FRAC`, `FISH_MIN_W/MAX_W`.

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
