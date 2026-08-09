# maze splash — where the water sim stands (handoff)

Two implementations live here:

- **`maze.py`** — the **shipped** version (what `plugin.hu` loads). Fast (~0.1–0.4s precompute),
  reliable, but it's a *reduction*, not true per-cell physics: the outside pool is one
  volume↔level reservoir (a fast BFS "level-flood"), and the chamber is a second reservoir joined
  by a head-driven flux. It works, but it has the artifacts you called out — the outside can *pin*
  and look static, and the chamber fill is a bit of a special case.
- **`maze_fullcell.py`** — the **experimental** genuine full-cell simulation you asked me to try.
  Every open cell holds a water mass; each step injects a constant inflow and relaxes the field
  (settle down → equalise sideways → push the pressurised excess up). The stream, the cascades,
  the pools finding their level, separate basins at different heights, the chamber filling — all
  emergent, nothing special-cased, no "door". **It looks genuinely good and physical.** It is
  *not* loaded, because of the wall below.

## The wall: pure-Python per-cell is too slow for a startup precompute

The cost is `steps × passes × active_cells × per-cell-Python-cost`, and to actually *fill* a maze
the water has to physically move through the passages, which takes many steps. Measured
(`maze_fullcell.py`, filling until the chamber brims):

| terminal | precompute to a *complete* fill |
|----------|--------------------------------|
| 44×22    | ~0.2–5.6s (seed-dependent)      |
| 80×24    | ~0.5–13s                       |
| 120×40   | ~4–56s                         |

Capping the step count makes it fast (~0.5s) but then the chamber **doesn't finish filling** (the
fish never swims). It's genuinely one or the other. I swept every lever — `MAX_COMPRESS` (had it
backwards at first: *low* compression rises faster, since near-incompressible water pushes its
excess up instead of packing a column), `MAX_SPEED` (inert — flows are gated by the stable-state
term, not it), `PASSES`, `INJECT`, an injection cap to kill the unphysical pile-up, and a
coarse-sim-plus-interpolate idea. None cross the wall; it's the pure-Python cell loop.

Two real design facts fell out, worth keeping whichever way you go:

1. **A cell that's "just part of the maze" doesn't reliably fill** — water pours in the top and
   drains out its other openings without ever pooling, *unless it's a basin*. In `maze_fullcell.py`
   the chamber is anchored to the maze floor (the low point) so it reliably holds water. That's the
   honest way to have "no door" and still get a fish pond.
2. **Where the inlet is dominates the runtime.** Inlet far from the chamber ⇒ water must cross the
   whole maze first (slow, 1000+ steps). Inlet *above* the chamber's ceiling gap ⇒ water pours
   straight in (fast, ~100 steps) and still "flows in from above". `maze_fullcell.py` uses the
   latter.

## Paths forward (for your vision)

- **Vectorise with numpy.** The CA is a stencil — `numpy` roll/where turns each pass into a handful
  of array ops in C. That's the straightforward 50–100× and would make the *real* full-cell sim
  ship. Only question is whether `numpy` is acceptable as a plugin dependency.
- **Basin/watershed graph.** Precompute the maze's pools and their spill saddles once, then
  time-step *fluxes between basins* (the chamber included) instead of between cells — O(basins) per
  step, keeps true multi-basin physics, no per-cell loop. This is the "physical but cheap" middle
  path.
- **Coarser sim grid + upsample.** Simulate at, say, one node per maze *cell* (tens of nodes, not
  thousands of chars) and render the char-level water by interpolation.
- **Accept the reduction** (`maze.py`) as-is and just refine its look.

To *see* the full-cell version run:

```
PYTHONPATH=/path/to/configsys python -c "
import importlib.util, pathlib, random
s=importlib.util.spec_from_file_location('m', pathlib.Path('maze_fullcell.py'))
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
sim=m.MazeSim(60,22,random.Random(3))   # ~a few seconds; MAX_STEPS caps it
# dump sim.frames[k] (0..8 per cell) to eyeball the flow
"
```

Tuning knobs at the top of `maze_fullcell.py`: `INJECT`, `PASSES`, `MAX_COMPRESS`, `SOURCE_CAP`,
`MAX_STEPS`.
