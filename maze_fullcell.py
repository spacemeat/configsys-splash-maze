'''maze_fullcell.py — EXPERIMENTAL full-cell physics for the maze splash. NOT loaded by the plugin
(plugin.hu points at maze.py, the fast shippable version). See HANDOFF.md for why this isn't shipped
and where to take it. This file is the genuine per-cell finite-water attempt — physically it's the
real thing (cascades, basins at different levels, U-tube), it's just too slow to precompute in pure
Python for a startup splash. Kept as a scaffold to develop a faster algorithm on.

A 2-D brick maze fills the screen: walls are courses of bricks (each brick is the two side-by-side
block glyphs 🬗🬤, brick-red glyph on cement-grey mortar). Somewhere in the lower half is a small
CHAMBER — an open room that's just part of the maze, with a gap in its ceiling so water can pour
into it from above — holding a fish flopping on dry stone. Liquid (a random colour each run) pours
in through a gap at the top and floods the maze; as the water reaches the chamber it fills, and the
fish, now afloat, stops flopping and swims. The simulation is DONE when the chamber is full.

The pour is a genuine FULL-CELL simulation (finite-water cellular automaton): every open cell holds
a water mass, and each step a constant inflow is injected at the inlet and the field is relaxed —
water settles DOWN, equalises LEFT/RIGHT to a common level, and the pressurised excess pushes UP
(what lets a pool rise and connected water climb a passage, U-tube). Mass only ever transfers
between neighbours (plus the inlet), so volume is conserved. One frame is recorded per step, so the
timeline IS the flow: the stream descends from empty, pools rise and cascade, and the chamber fills
because water reaches it — nothing special-cased. Playback maps the timeline onto inspection
progress (eased, rate-capped) so the chamber tops off about when inspection completes.

Only the moving water is worked each step (a settled pool leaves the active frontier), so the cost
tracks the flow, not the grid. Constant inflow -> constant on-screen flow rate. INJECT (flow rate),
PASSES (relaxation per frame) and MAX_COMPRESS (how fast pools rise) are the tuning knobs.

Ships as a configsys splash provider (see configsys/splashes.py for the ABI): the HOST
(configsys.tui.splash.run_splash) owns the frame loop and calls render(frame); MazeSim is the
curses-free, deterministic sim (unit-tested); MazeSplash the curses renderer. `SPLASHES` at the
foot exports it so the trusted loader registers `splash: maze`.
'''

import colorsys
import curses
import math

from configsys.plugins import Splash

# -- glyphs -------------------------------------------------------------------
BRICK_L, BRICK_R = '🬗', '🬤'        # the two halves of one brick, drawn side by side
EIGHTHS = ' ▁▂▃▄▅▆▇█'              # water height in a cell: index 0..8 (lower blocks)
EDGE_RIGHT, EDGE_LEFT = '▕', '▏'    # a thin waterfall hugging a wall on its right / left
SHIMMER = ('🮕', '🮖')               # turbulent water at the inlet, alternated per frame
STREAM = '▓'                       # a falling stream where no wall is alongside
FULL = '█'

# Fish (small, ocean-style): a body between a taper and a head triangle. Swimming poses point the
# way it moves; flopping poses are the fish out of water on the chamber floor, tilting side to side.
FISH_RIGHT = ('◄▪►', '◄▪▪►')
FISH_LEFT = ('◄▪►', '◄▪▪►')
FLOP = ('◟▪◞', '◜▪◝', '◠▪◠')

# -- maze geometry (chars) ----------------------------------------------------
CELL_W, CELL_H = 4, 2               # open interior of one maze cell
WALL_T = 2                          # wall thickness — 2 keeps bricks a running bond in both axes

# brick masonry: a fixed fg-on-bg — brick-red glyph, cement-grey mortar behind it
BRICK_FG = (150, 54, 40)
BRICK_BG = (108, 104, 98)

# -- tuning (all visual; safe to tweak) ---------------------------------------
FPS = 30.0
MIN_DURATION = 2.6                  # play at least this long so the pour is enjoyable on fast boxes
PLAY_EASE = 3.6                     # cursor eases toward progress at this rate (per second)
MAX_FRAMES = 300                    # timeline is sampled to at most this many frames for playback
# Finite-water automaton: constant inflow; every other move a symmetric neighbour transfer, so
# volume is conserved. Stepped forward in time (one frame per step) — the stream, pools and chamber
# fill are all emergent, none special-cased.
INJECT = 3.0                        # water injected at the inlet each step (the flow rate)
SOURCE_CAP = 3.0                    # don't inject while the inlet is this backed up (no pile-up)
PASSES = 4                          # relaxation passes per recorded frame (more = settles, less pile)
MAX_COMPRESS = 0.05                 # extra a full cell holds under pressure (small = near-incompressible,
                                    # so excess rises promptly instead of packing into a column)
MAX_SPEED = 4.0                     # most mass a cell may pass vertically in one pass
MIN_MASS = 1e-4                     # below this a cell is treated as dry
FLOW_EPS = 4e-3                     # a cell whose flow drops below this has settled -> leaves the frontier
MAX_STEPS = 1500                    # reliably fills, but SLOW — see HANDOFF.md (pure-Python wall)
SETTLE_TAIL = 8                     # a few calm passes after the chamber fills, so the end reads still


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _stable_b(total):
    '''Gallant's stable-state: of `total` mass shared by a cell and the one directly below it, how
    much the BOTTOM holds at rest. <=1 all sinks; a little compression lets a full cell hold slightly
    over 1 under pressure — which is what makes water climb a connected up-passage (the U-tube).'''
    if total <= 1.0:
        return total
    if total < 2.0 + MAX_COMPRESS:
        return (1.0 + total * MAX_COMPRESS) / (1.0 + MAX_COMPRESS)
    return (total + MAX_COMPRESS) / 2.0


def random_liquid(rng):
    '''A fresh liquid look each run: a deep (dark) -> surface (bright) value ramp on a random hue,
    plus a contrasting fish tint. Returns (deep, surface, fish) rgb tuples.'''
    h = rng.random()
    deep = _hsv(h, 0.85, 0.30)
    surface = _hsv((h + rng.uniform(-0.04, 0.04)) % 1.0, 0.60, 0.97)
    fish = _hsv((h + rng.uniform(0.4, 0.6)) % 1.0, 0.65, 0.95)
    return deep, surface, fish


class MazeSim:
    '''Curses-free maze + full-cell pour. Construction generates a brick maze whose lower half holds
    an open, ceiling-gapped chamber, then simulates the finite-water flood into a list of `frames`
    (each a per-cell water level 0..8), ending when the chamber fills. At runtime feed progress via
    set_progress(frac), advance the cursor with step(dt), read frame()/chamber_depth() to draw.
    Deterministic given `rng`.'''

    def __init__(self, w, h, rng):
        self.W = max(16, int(w))
        self.H = max(12, int(h))
        self.rng = rng
        self._dims()
        self._generate()
        self.frames = self._simulate()             # full-cell pour -> one frame per step
        self.n = len(self.frames)
        self._p = 0.0                              # progress target (monotonic)
        self._cursor = 0.0                         # eased playback position in [0, n-1]

    # -- geometry ---------------------------------------------------------

    def _dims(self):
        self.mc = max(3, (self.W - WALL_T) // (CELL_W + WALL_T))
        self.mr = max(3, (self.H - WALL_T) // (CELL_H + WALL_T))

    def _cell_origin(self, cr, cc):
        return (WALL_T + cr * (CELL_H + WALL_T), WALL_T + cc * (CELL_W + WALL_T))

    def _cell_interior(self, cr, cc):
        y, x = self._cell_origin(cr, cc)
        return [(yy, xx) for yy in range(y, y + CELL_H) for xx in range(x, x + CELL_W)]

    def _open_right(self, cr, cc):        # passage to (cr, cc+1)
        y, x = self._cell_origin(cr, cc)
        for yy in range(y, y + CELL_H):
            for xx in range(x + CELL_W, x + CELL_W + WALL_T):
                self.wall[yy][xx] = False

    def _open_down(self, cr, cc):         # passage to (cr+1, cc)
        y, x = self._cell_origin(cr, cc)
        for yy in range(y + CELL_H, y + CELL_H + WALL_T):
            for xx in range(x, x + CELL_W):
                self.wall[yy][xx] = False

    def _carve(self, r, c, nr, nc):
        if nr == r and nc == c + 1:
            self._open_right(r, c)
        elif nr == r and nc == c - 1:
            self._open_right(r, c - 1)
        elif nr == r + 1 and nc == c:
            self._open_down(r, c)
        elif nr == r - 1 and nc == c:
            self._open_down(r - 1, c)

    # -- generation -------------------------------------------------------

    def _generate(self):
        self.wall = [[True] * self.W for _ in range(self.H)]   # solid, then carve
        rng = self.rng
        for r in range(self.mr):                               # every cell's interior is open floor
            for c in range(self.mc):
                for (yy, xx) in self._cell_interior(r, c):
                    self.wall[yy][xx] = False

        # chamber: a rectangle of cells at the BOTTOM of the maze. It's ordinary maze (open, normally
        # connected), but being the low point it reliably HOLDS water — a basin — so it fills instead
        # of draining through. Water still enters it from above through the ceiling gap below.
        ch = max(2, self.mr // 4)
        cw = max(2, self.mc // 4)
        cr0 = self.mr - ch
        cc0 = rng.randint(1, max(1, self.mc - cw - 1))
        chamber = {(r, c) for r in range(cr0, cr0 + ch) for c in range(cc0, cc0 + cw)}
        self.chamber_rc = (cr0, cr0 + ch - 1, cc0, cc0 + cw - 1)

        # a perfect maze over ALL the cells (the chamber included — it's not an island)
        start = (rng.randrange(self.mr), rng.randrange(self.mc))
        seen = {start}
        stack = [start]
        while stack:
            r, c = stack[-1]
            nb = [(nr, nc) for (nr, nc) in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                  if 0 <= nr < self.mr and 0 <= nc < self.mc and (nr, nc) not in seen]
            if not nb:
                stack.pop()
                continue
            nr, nc = nb[rng.randrange(len(nb))]
            self._carve(r, c, nr, nc)
            seen.add((nr, nc))
            stack.append((nr, nc))

        # knock the chamber's internal walls out so it's one open room
        for (r, c) in chamber:
            if (r, c + 1) in chamber:
                self._open_right(r, c)
            if (r + 1, c) in chamber:
                self._open_down(r, c)
        # guarantee a CEILING gap over the chamber, so water can pour into it from above
        gap_c = cc0 + cw // 2
        self._open_down(cr0 - 1, gap_c)

        # the top inlet sits ABOVE that ceiling gap: water pours down the maze and into the chamber
        # from above (rather than crossing the whole maze first), so the fill is physical AND quick.
        ic = gap_c
        y, x = self._cell_origin(0, ic)
        for yy in range(0, y):
            for xx in range(x, x + CELL_W):
                self.wall[yy][xx] = False
        self.source = (0, x + CELL_W // 2)

        self._finish()

    def _finish(self):
        self.open = [[not self.wall[y][x] for x in range(self.W)] for y in range(self.H)]
        self.open_cells = [(y, x) for y in range(self.H) for x in range(self.W) if self.open[y][x]]
        cr0, cr1, cc0, cc1 = self.chamber_rc
        y0, x0 = self._cell_origin(cr0, cc0)
        y1, x1 = self._cell_origin(cr1, cc1)
        self.chamber_box = (y0, y1 + CELL_H - 1, x0, x1 + CELL_W - 1)
        self.chamber_cells = [(y, x) for y in range(y0, y1 + CELL_H) for x in range(x0, x1 + CELL_W)
                              if self.open[y][x]]
        self.chamber_set = set(self.chamber_cells)
        self._open_set = set(self.open_cells)

    # -- the full-cell pour -----------------------------------------------

    def _ca_pass(self, mass, active):
        '''One finite-water relaxation pass over the ACTIVE frontier only (a settled pool interior
        drops out, so cost tracks the moving water). Reads the old grid, accumulates symmetric
        transfers as deltas, applies them, returns the next frontier. Per cell: settle DOWN, equalise
        LEFT/RIGHT, then push the pressurised excess UP (the rise / U-tube climb).'''
        H, W, op = self.H, self.W, self.open
        delta = {}
        for (y, x) in active:
            m = mass[y][x]
            if m <= MIN_MASS:
                continue
            rem = m
            if y + 1 < H and op[y + 1][x]:                             # DOWN
                f = _stable_b(rem + mass[y + 1][x]) - mass[y + 1][x]
                f = MAX_SPEED if f > MAX_SPEED else (rem if f > rem else f)
                if f > 0:
                    delta[(y, x)] = delta.get((y, x), 0.0) - f
                    delta[(y + 1, x)] = delta.get((y + 1, x), 0.0) + f
                    rem -= f
            for nx in (x - 1, x + 1):                                  # LEFT / RIGHT (equalise)
                if rem > 0 and 0 <= nx < W and op[y][nx]:
                    f = (rem - mass[y][nx]) / 4.0
                    f = rem if f > rem else f
                    if f > 0:
                        delta[(y, x)] = delta.get((y, x), 0.0) - f
                        delta[(y, nx)] = delta.get((y, nx), 0.0) + f
                        rem -= f
            if rem > 0 and y - 1 >= 0 and op[y - 1][x]:                # UP (pressurised excess)
                f = rem - _stable_b(rem + mass[y - 1][x])
                f = MAX_SPEED if f > MAX_SPEED else (rem if f > rem else f)
                if f > 0:
                    delta[(y, x)] = delta.get((y, x), 0.0) - f
                    delta[(y - 1, x)] = delta.get((y - 1, x), 0.0) + f
                    rem -= f
        nxt = set()
        for c, d in delta.items():
            mass[c[0]][c[1]] += d
        for c, d in delta.items():
            if d > FLOW_EPS or d < -FLOW_EPS:                          # still moving -> stay active
                y, x = c
                nxt.add(c)
                if y - 1 >= 0 and op[y - 1][x]:
                    nxt.add((y - 1, x))
                if y + 1 < H and op[y + 1][x]:
                    nxt.add((y + 1, x))
                if x - 1 >= 0 and op[y][x - 1]:
                    nxt.add((y, x - 1))
                if x + 1 < W and op[y][x + 1]:
                    nxt.add((y, x + 1))
        return nxt

    def _levels(self, mass):
        '''Per-cell water level 0..8 for rendering (falling vs. pooled is derived at draw time).'''
        grid = [[0] * self.W for _ in range(self.H)]
        for (y, x) in self.open_cells:
            m = mass[y][x]
            if m <= MIN_MASS:
                continue
            lv = int(m * 8 + 0.5)
            grid[y][x] = 8 if lv > 8 else (1 if lv < 1 else lv)
        return grid

    def _chamber_full(self, mass):
        return all(mass[y][x] >= 0.9 for (y, x) in self.chamber_cells)

    def _simulate(self):
        '''Pour forward in time from EMPTY: each step inject a constant flow at the inlet, relax the
        water a few passes, record a frame. The stream descends, pools rise and cascade, and the
        chamber fills as water reaches it — all emergent. Ends when the chamber brims; subsampled to
        MAX_FRAMES so playback maps onto progress.'''
        mass = [[0.0] * self.W for _ in range(self.H)]
        sy, sx = self.source
        active = set()
        frames = [self._levels(mass)]                    # frame 0: bone dry, t=0
        for _ in range(MAX_STEPS):
            if mass[sy][sx] < SOURCE_CAP:                 # hold off while the inlet is backed up
                mass[sy][sx] += INJECT
            active.add((sy, sx))
            for _ in range(PASSES):
                active = self._ca_pass(mass, active)
                active.add((sy, sx))
            frames.append(self._levels(mass))
            if self._chamber_full(mass):
                break
        for _ in range(SETTLE_TAIL):                     # let it settle so the end reads calm
            for _ in range(PASSES):
                active = self._ca_pass(mass, active)
            frames.append(self._levels(mass))
        if len(frames) > MAX_FRAMES:
            frames = [frames[round(i * (len(frames) - 1) / (MAX_FRAMES - 1))] for i in range(MAX_FRAMES)]
        return frames

    # -- runtime ----------------------------------------------------------

    def set_progress(self, frac):
        '''Monotonic 0..1 playback target (never rewinds).'''
        frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
        self._p = max(self._p, frac)

    def step(self, dt):
        '''Ease the playback cursor toward progress*(n-1) — a stepwise jump in progress ramps over
        ~0.3s rather than teleporting the whole flood in one frame.'''
        if dt <= 0:
            return
        target = self._p * (self.n - 1)
        rate = PLAY_EASE * (2.2 if self._p >= 0.999 else 1.0)
        self._cursor += (target - self._cursor) * min(1.0, dt * rate)

    @property
    def filled(self):
        return self._cursor >= (self.n - 1) - 0.5

    def frame(self):
        i = int(self._cursor + 0.5)
        return self.frames[0 if i < 0 else self.n - 1 if i >= self.n else i]

    def chamber_depth(self, grid):
        '''Whole cells of water standing in the chamber (fully-submerged rows from the floor up):
        >=1 means the fish is afloat and swims; 0 means it flops on dry stone.'''
        y0, y1, x0, x1 = self.chamber_box
        rows = 0
        for y in range(y1, y0 - 1, -1):
            cells = [x for x in range(x0, x1 + 1) if (y, x) in self._open_set]
            if cells and all(grid[y][x] >= 8 for x in cells):
                rows += 1
            else:
                break
        return rows


class MazeSplash(Splash):
    '''The `maze` splash: a curses driver around a MazeSim. Bakes the random liquid ramp + brick
    palette once, then render(frame) advances the playback cursor toward frame.progress and paints
    the bricks, the water (eighth blocks, waterfall edges, the inlet shimmer), and the fish
    (flopping, then swimming once the chamber holds water). Returns True once the chamber is full.'''

    name = 'maze'
    fps = FPS
    min_duration = MIN_DURATION

    def __init__(self, scr, pal, size, seed=None):
        super().__init__(scr, pal, size, seed)
        self.sim = MazeSim(self.w, self.h, self.rng)
        deep, surface, fish = random_liquid(self.rng)
        self.nbands = max(4, min(20, self.h))
        ramp = [_lerp(deep, surface, k / (self.nbands - 1)) for k in range(self.nbands)]
        self._water = [pal.rgb_attr(c) for c in ramp]
        self._surface = pal.rgb_attr(surface) | curses.A_BOLD
        self._shimmer = pal.rgb_attr(_lerp(surface, (255, 255, 255), 0.35)) | curses.A_BOLD
        self._fish = [pal.rgb_pair(fish, c) | curses.A_BOLD for c in ramp]
        self._fish_dry = pal.rgb_attr(fish) | curses.A_BOLD
        self._brick_attr = pal.rgb_pair(BRICK_FG, BRICK_BG)            # brick-red on cement-grey
        self._label_attr = pal.get('title') | curses.A_BOLD
        self._bake_bricks()

    def _bake_bricks(self):
        '''Precompute each wall char's brick glyph — a running-bond masonry (bricks 2 chars wide,
        offset one half each course). Colour is uniform: brick-red foreground on a cement-grey
        background (the glyph carves the brick, the background shows as mortar).'''
        sim = self.sim
        self._wg = [[None] * sim.W for _ in range(sim.H)]
        for y in range(sim.H):
            shift = y % 2
            for x in range(sim.W):
                if sim.wall[y][x]:
                    self._wg[y][x] = BRICK_L if (x + shift) % 2 == 0 else BRICK_R

    def _band(self, height_from_bottom):
        ratio = 0.0 if self.h <= 1 else height_from_bottom / self.h
        idx = int(ratio * (self.nbands - 1) + 0.5)
        return self._water[0 if idx < 0 else self.nbands - 1 if idx >= self.nbands else idx]

    def render(self, frame):
        self.sim.set_progress(frame.progress)
        self.sim.step(frame.dt)
        sim, scr = self.sim, self.scr
        grid = sim.frame()
        scr.erase()
        brick = self._brick_attr
        for y in range(sim.H):                               # bricks (static masonry)
            wg = self._wg[y]
            for x in range(sim.W):
                if wg[x] is not None:
                    self._add(y, x, wg[x], brick)
        H, op = sim.H, sim.open
        for (y, x) in sim.open_cells:
            lvl = grid[y][x]
            if lvl == 0:
                continue
            if lvl >= 8:
                self._add(y, x, FULL, self._band(H - 1 - y))
            elif y + 1 < H and op[y + 1][x] and grid[y + 1][x] < 8:
                self._draw_stream(y, x, frame)               # water still draining down here
            else:
                self._add(y, x, EIGHTHS[lvl], self._surface)  # a pooled surface
        self._draw_fish(frame, grid)
        if frame.label:
            self._draw_label(frame)
        return sim.filled

    def _draw_stream(self, y, x, frame):
        '''Falling water: the inlet's turbulent shimmer near the very top, a wall-hugging edge glyph
        where a brick wall stands to one side (▕ against a wall on the right, ▏ on the left), else a
        slim mid-stream body.'''
        sim = self.sim
        if y <= sim.source[0] + 1 and x == sim.source[1]:
            self._add(y, x, SHIMMER[int(frame.elapsed * 6) % 2], self._shimmer)
            return
        wall_left = x - 1 < 0 or not sim.open[y][x - 1]
        wall_right = x + 1 >= sim.W or not sim.open[y][x + 1]
        if wall_right and not wall_left:
            self._add(y, x, EDGE_RIGHT, self._surface)
        elif wall_left and not wall_right:
            self._add(y, x, EDGE_LEFT, self._surface)
        else:
            self._add(y, x, STREAM, self._surface)

    def _draw_fish(self, frame, grid):
        sim = self.sim
        y0, y1, x0, x1 = sim.chamber_box
        depth = sim.chamber_depth(grid)
        cx = (x0 + x1) / 2.0
        span = max(1, (x1 - x0) - 3)
        if depth >= 1:                                       # afloat: swim near the pond surface
            phase = frame.elapsed * 1.6
            fx = int(cx + math.sin(phase) * (span / 2.0))
            right = math.cos(phase) >= 0
            fy = max(y0, y1 - depth + 1)
            glyph = (FISH_RIGHT if right else FISH_LEFT)[int(frame.elapsed * 2) % 2]
            band = min(self.nbands - 1, (sim.H - 1 - fy) * self.nbands // max(1, sim.H))
            attr = self._fish[band]
        else:                                                # dry: flop on the chamber floor
            fy = y1 - (int(frame.elapsed * 6) % 2)
            fx = int(cx)
            glyph = FLOP[int(frame.elapsed * 5) % len(FLOP)]
            attr = self._fish_dry
        start = fx - len(glyph) // 2
        for i, ch in enumerate(glyph):
            gx = start + i
            if x0 <= gx <= x1 and y0 <= fy <= y1 and sim.open[fy][gx]:
                self._add(fy, gx, ch, attr)

    def _label_text(self, counts, label):
        i, total = counts
        if not total:
            return f'{label}…'
        return f'{label}:   {i}/{total} ({int(i / total * 100)}%)'

    def _draw_label(self, frame):
        text = ' ' + self._label_text(frame.counts, frame.label) + ' '
        y = max(0, self.h - 1)
        x = max(0, (self.w - len(text)) // 2)
        for k, ch in enumerate(text):
            if x + k < self.w:
                self._add(y, x + k, ch, self._label_attr)

    def _add(self, y, x, s, attr):
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:                                 # the last cell always throws — a curses fact
            pass


SPLASHES = [MazeSplash]
