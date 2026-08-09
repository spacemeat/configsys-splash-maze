'''maze.py — the "maze" startup splash for configsys, as a code plugin.

A 2-D brick maze fills the screen: walls are courses of bricks (each brick is the two side-by-side
block glyphs 🬗🬤, brick-red glyph on cement-grey mortar). Somewhere in the lower half sits a small
sealed CHAMBER holding a fish flopping on dry stone — the chamber has a single doorway high in one
side wall. Liquid (a random colour each run) pours in through a gap at the top and floods the maze:
it falls where it can, pools, and — because connected water shares a common level — climbs adjacent
passages (U-tube) and overflows the low lips. The chamber's lone side door means it stays dry until
the surrounding water rises to it; then it fills, and the fish, now afloat, stops flopping and
swims. The simulation is DONE when the chamber is full.

The pour is stepped FORWARD IN TIME (MazeSim precomputes one frame per step) so it reads as real
flow: it starts bone dry, the stream is revealed as its front DESCENDS from the inlet, and pools
rise. When the rising outside water actually reaches the chamber's door, it SPILLS THROUGH into the
chamber — drained from the outside pool (conserved) at a head-driven rate, a visible waterfall down
the chamber wall — so the chamber fills because the maze's own water got there, ONE coupled flow,
not a private tap. The outside then holds at the door sill, spilling its inflow in, until the chamber
brims. The timeline is played back against inspection progress (eased, rate-capped) so the chamber
tops off just as inspection completes — no added latency, no dead air.

Pooling itself is a fast level-flood (so surfaces are instantly flat, no slow relaxation): for a
surface height L, a breadth-first flood from the inlet lets water FALL for free (the visible
streams) and POOL only through submerged cells (floor below L), finding one common level (U-tube),
overflowing the lowest lip to cascade on. The outside is one such reservoir (volume<->level via the
flood profile); the chamber is a second, joined by the door as a finite orifice — a head-driven flux
between them, conserved. Constant inflow -> constant flow rate; volume conserved by construction.

Ships as a configsys splash provider (see configsys/splashes.py for the ABI): the HOST
(configsys.tui.splash.run_splash) owns the frame loop and calls render(frame); MazeSim is the
curses-free, deterministic sim (unit-tested); MazeSplash the curses renderer. `SPLASHES` at the
foot exports it so the trusted loader registers `splash: maze`.
'''

import colorsys
import curses
import math
from collections import deque

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
MAX_FRAMES = 260                    # the timeline is sampled to at most this many frames
# The pour is stepped FORWARD IN TIME (one recorded frame per step) so the stream visibly descends
# from empty and the chamber fills only once the flow reaches its door — the timeline IS the flow,
# not a static fill paced by pooled volume. Pooling itself is a fast level-flood (instant flat
# surfaces); the forward loop just advances the volume and the reveal front, and rate-limits the
# chamber. Constant inflow -> constant flow rate; volume is conserved by construction.
INJECT = 2.0                        # volume poured in each step (the flow rate)
DESCENT_FRACTION = 0.12             # reveal the stream's front over roughly the first 12% of the pour
DOOR_C = 10.0                       # door conductance: chamber intake per unit of head over its sill
                                    # (high -> the outside holds at the sill and spills its inflow in)
MAX_STEPS = 40000                   # safety cap (the volume timeline is cheap, so this is generous)
SETTLE_TAIL = 6                     # hold the brimming end a moment so it reads still


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def random_liquid(rng):
    '''A fresh liquid look each run: a deep (dark) -> surface (bright) value ramp on a random hue,
    plus a contrasting fish tint. Returns (deep, surface, fish) rgb tuples.'''
    h = rng.random()
    deep = _hsv(h, 0.85, 0.30)
    surface = _hsv((h + rng.uniform(-0.04, 0.04)) % 1.0, 0.60, 0.97)
    fish = _hsv((h + rng.uniform(0.4, 0.6)) % 1.0, 0.65, 0.95)
    return deep, surface, fish


class MazeSim:
    '''Curses-free maze + pour. Construction generates a brick maze with a sealed lower chamber and
    a top inlet, then PRECOMPUTES the constant-inflow flood as a list of `frames` (each a per-cell
    water level: 0 dry, 1..8 pooled eighths, -1 a falling stream) ending when the chamber fills. At
    runtime feed progress via set_progress(frac), advance the playback cursor with step(dt), and
    read frame()/chamber_depth() to draw. Deterministic given `rng`.'''

    def __init__(self, w, h, rng):
        self.W = max(16, int(w))
        self.H = max(12, int(h))
        self.rng = rng
        self._dims()
        self._generate()
        self.frames = self._simulate()             # forward-time pour -> one frame per step
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
        # chamber: a rectangle of cells in the lower half, with a maze rim around it
        ch = max(2, self.mr // 4)
        cw = max(2, self.mc // 4)
        cr0 = max(self.mr // 2, self.mr - ch - 1)
        cc0 = rng.randint(1, max(1, self.mc - cw - 1))
        chamber = {(r, c) for r in range(cr0, cr0 + ch) for c in range(cc0, cc0 + cw)}
        self.chamber_rc = (cr0, cr0 + ch - 1, cc0, cc0 + cw - 1)

        # perfect maze over the NON-chamber cells (the chamber is an island joined by one door)
        outside = [(r, c) for r in range(self.mr) for c in range(self.mc) if (r, c) not in chamber]
        outset = set(outside)
        start = outside[rng.randrange(len(outside))]
        seen = {start}
        stack = [start]
        while stack:
            r, c = stack[-1]
            nb = [(nr, nc) for (nr, nc) in
                  ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                  if (nr, nc) in outset and (nr, nc) not in seen]
            if not nb:
                stack.pop()
                continue
            nr, nc = nb[rng.randrange(len(nb))]
            self._carve(r, c, nr, nc)
            seen.add((nr, nc))
            stack.append((nr, nc))

        # open the chamber interior (all cells + the walls between adjacent chamber cells)
        for (r, c) in chamber:
            for (yy, xx) in self._cell_interior(r, c):
                self.wall[yy][xx] = False
            if (r, c + 1) in chamber:
                self._open_right(r, c)
            if (r + 1, c) in chamber:
                self._open_down(r, c)

        # the single chamber door: a gap high in a SIDE wall — the chamber's top row opens to the
        # adjacent maze cell at the SAME height, so water spills in once the outside rises to it (and
        # the chamber then backfills gradually) rather than a ceiling door that pours it full at once.
        if cc0 - 1 >= 0:
            self._carve(cr0, cc0, cr0, cc0 - 1)            # door on the left of the top row
            self._door = (cr0, cc0, 'left')
        else:
            self._carve(cr0, cc0 + cw - 1, cr0, cc0 + cw)  # ...or the right, at a screen edge
            self._door = (cr0, cc0 + cw - 1, 'right')

        # the top inlet: open the ceiling above a top-row maze cell, biased to the FAR side from the
        # chamber so water has to flood and traverse the maze before it reaches the door (a fuller,
        # more consistent pour) rather than dropping straight onto the door mouth.
        inlet_cols = [c for c in range(self.mc) if (0, c) not in chamber]
        cham_center = cc0 + cw / 2.0
        if cham_center < self.mc / 2.0:                        # chamber on the left -> inlet right
            far = [c for c in inlet_cols if c > self.mc * 0.55]
        else:                                                 # chamber on the right -> inlet left
            far = [c for c in inlet_cols if c < self.mc * 0.45]
        pool = far or inlet_cols
        ic = pool[rng.randrange(len(pool))]
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
        self.chamber_top_e = max(self.H - 1 - y for (y, x) in self.chamber_cells)
        self.chamber_floor_e = min(self.H - 1 - y for (y, x) in self.chamber_cells)
        self._open_set = set(self.open_cells)
        # the door's char column + top row inside the chamber, for drawing the pour-through waterfall
        dr, dc, side = self._door
        oy, ox = self._cell_origin(dr, dc)
        self.door_y = oy
        self.door_x = ox if side == 'left' else ox + CELL_W - 1
        self.door_sill = self.chamber_top_e - 0.5    # provisional; _simulate resets it to L_door
        # the OUTSIDE cells at the chamber's walls — water reaching one of these (submerged) is what
        # actually opens the door, so timing tracks where the flood really is, not a global level.
        mouth = set()
        for (y, x) in self.chamber_cells:
            for ny, nx in ((y, x - 1), (y, x + 1), (y - 1, x), (y + 1, x)):
                if (0 <= ny < self.H and 0 <= nx < self.W and self.open[ny][nx]
                        and (ny, nx) not in self.chamber_set):
                    mouth.add((ny, nx))
        self.door_mouth = mouth

    # -- the forward-time pour --------------------------------------------

    def _flood(self, L, block):
        '''Cells the water occupies at outside surface level L: streams FALL for free, and a
        submerged cell (floor below L) spreads to submerged horizontal/upper neighbours (the pool
        finding one level — U-tube) or, if it can't fall, sheets across a ledge to the next drop.
        `block` (the chamber) is sealed. Fast — one BFS. Returns the wet grid.'''
        H, W, op = self.H, self.W, self.open
        reach = [[False] * W for _ in range(H)]
        sy, sx = self.source
        reach[sy][sx] = True
        dq = deque([(sy, sx)])
        while dq:
            y, x = dq.popleft()
            can_fall = y + 1 < H and op[y + 1][x] and (y + 1, x) not in block
            if can_fall and not reach[y + 1][x]:
                reach[y + 1][x] = True
                dq.append((y + 1, x))
            for ny, nx in ((y, x - 1), (y, x + 1)):
                if (0 <= nx < W and op[ny][nx] and not reach[ny][nx] and (ny, nx) not in block
                        and ((H - 1 - ny) < L or not can_fall)):
                    reach[ny][nx] = True
                    dq.append((ny, nx))
            ny = y - 1
            if (ny >= 0 and op[ny][x] and not reach[ny][x] and (ny, x) not in block
                    and (H - 1 - ny) < L):
                reach[ny][x] = True
                dq.append((ny, x))
        return reach

    def _volume(self, L, reach):
        H = self.H
        return sum(min(1.0, L - (H - 1 - y)) for (y, x) in self.open_cells
                   if reach[y][x] and L > (H - 1 - y))

    def _arrival(self, block):
        '''BFS step at which the flow FRONT first reaches each outside cell, spreading only DOWN and
        SIDEWAYS from the inlet (never up) — the order the pouring water physically arrives, used to
        reveal the descending stream over time instead of all at once.'''
        H, W, op = self.H, self.W, self.open
        arr = [[10 ** 9] * W for _ in range(H)]
        sy, sx = self.source
        arr[sy][sx] = 0
        dq = deque([(sy, sx)])
        while dq:
            y, x = dq.popleft()
            d = arr[y][x] + 1
            for ny, nx in ((y + 1, x), (y, x - 1), (y, x + 1)):
                if (0 <= ny < H and 0 <= nx < W and op[ny][nx] and (ny, nx) not in block
                        and arr[ny][nx] > d):
                    arr[ny][nx] = d
                    dq.append((ny, nx))
        return arr

    @staticmethod
    def _invert(table, v):
        '''Level L whose volume is v, by linear interp over the monotone (L, volume) profile.'''
        if v <= table[0][1]:
            return table[0][0]
        for (l0, v0), (l1, v1) in zip(table, table[1:]):
            if v <= v1:
                return l0 if v1 == v0 else l0 + (l1 - l0) * (v - v0) / (v1 - v0)
        return table[-1][0]

    def _chamber_level(self, v):
        '''Local surface level (floor->ceiling) at which the chamber holds volume v — bisected.'''
        H = self.H
        lo, hi = float(self.chamber_floor_e), self.chamber_top_e + 1.2
        if v <= 0:
            return lo

        def vol(cl):
            return sum(min(1.0, max(0.0, cl - (H - 1 - y))) for (y, x) in self.chamber_cells)

        for _ in range(28):
            mid = (lo + hi) / 2
            if vol(mid) < v:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def _build(self, L, arrival, front, v_cham, block):
        '''One frame: the outside pool at level L (stream cells revealed only once the front has
        reached them), plus the chamber filled to volume v_cham. Levels 0..8 (falling vs. pooled is
        decided at draw time from the cell below).'''
        H = self.H
        reach = self._flood(L, block)
        grid = [[0] * self.W for _ in range(H)]
        for (y, x) in self.open_cells:
            if (y, x) in block or not reach[y][x]:
                continue
            d = L - (H - 1 - y)
            if d >= 1:
                grid[y][x] = 8                               # submerged pool
            elif d > 0:
                grid[y][x] = max(1, min(7, int(d * 8 + 0.5)))  # pool surface
            elif arrival[y][x] <= front:
                grid[y][x] = 4                               # a falling stream the front has reached
        if v_cham > 0:
            cl = self._chamber_level(v_cham)
            for (y, x) in self.chamber_cells:
                d = cl - (H - 1 - y)
                grid[y][x] = 8 if d >= 0.92 else (max(1, min(7, int(d * 8 + 0.5))) if d > 0 else 0)
        if v_cham < len(self.chamber_cells) and L >= self.door_sill:   # water pouring THROUGH the door
            x = self.door_x
            for y in range(self.door_y, self.chamber_box[1] + 1):
                if (y, x) not in block:
                    break
                if grid[y][x] != 0:
                    break                                    # reached the chamber's water surface
                grid[y][x] = 4                               # a falling stream down the chamber wall
        return grid

    def _simulate(self):
        '''Pour forward in time from EMPTY: each step injects a constant flow, the outside surface
        rises off that volume, and the stream is revealed as its front descends. Once the surface
        reaches the chamber's side door, part of the inflow is diverted into the chamber (rate-
        limited, so it fills gradually floor->ceiling) while the outside keeps rising on the rest —
        so the chamber fills visibly WITH the ongoing flow, not in a frozen phase. One frame per
        step (the timeline IS the flow); ends when the chamber brims.'''
        block = self.chamber_set
        arrival = self._arrival(block)
        far = max((arrival[y][x] for (y, x) in self.open_cells if arrival[y][x] < 10 ** 9), default=1)
        # profile outside volume vs surface level AND find the level at which the flood first reaches
        # the chamber's door mouth submerged — the moment water genuinely arrives at the door.
        profile = []                                         # (L, outside volume) up the whole maze
        door_sill = vol_door = None
        L = 0.0
        while L <= self.H + 1:
            reach = self._flood(L, block)
            vol = self._volume(L, reach)
            profile.append((L, vol))
            if door_sill is None and any(reach[y][x] and (self.H - 1 - y) < L for (y, x) in self.door_mouth):
                door_sill, vol_door = L, vol                 # water has reached the door here
            L += 0.34
        if door_sill is None:                                # mouth never wetted (shouldn't happen)
            door_sill, vol_door = profile[-1]
        self.door_sill = door_sill                           # so _build draws the pour at the right time
        cham_cap = float(len(self.chamber_cells))
        # 1) the cheap part: step the (outside, chamber) VOLUME timeline forward — scalar updates
        # only, no flood — so it can run at a fine step for a smooth pour.
        timeline = [(0.0, 0.0)]
        v_out = v_cham = 0.0
        while len(timeline) <= MAX_STEPS:
            v_out += INJECT                                  # the source pours into the maze
            head = self._invert(profile, v_out) - door_sill  # depth of outside water over the door
            if head > 0 and v_cham < cham_cap:
                # water spills THROUGH the door into the chamber, DRAINED from the outside pool
                # (conserved) at a head-driven rate — a finite opening, not a private tap. Capped so
                # it can't pull the outside below the sill: the excess just keeps pouring in.
                flux = min(DOOR_C * head, cham_cap - v_cham, max(0.0, v_out - vol_door))
                v_out -= flux
                v_cham += flux
            timeline.append((v_out, v_cham))
            if v_cham >= cham_cap - 1e-6:
                break
        # 2) the only real cost is the per-frame flood, so render at most MAX_FRAMES of the timeline
        # (evenly sampled) — bounds precompute regardless of how many steps the pour took.
        n = len(timeline)
        front_speed = far / max(1.0, (n - 1) * DESCENT_FRACTION)   # descent over ~DESCENT_FRACTION of the run
        idxs = range(n) if n <= MAX_FRAMES else (round(i * (n - 1) / (MAX_FRAMES - 1))
                                                 for i in range(MAX_FRAMES))
        frames = [self._build(self._invert(profile, timeline[i][0]), arrival, i * front_speed,
                              timeline[i][1], block) for i in idxs]
        frames += [frames[-1]] * SETTLE_TAIL                 # hold the brimming end a moment
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
