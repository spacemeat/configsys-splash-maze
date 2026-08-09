'''maze.py — the "maze" startup splash for configsys, as a code plugin.

A 2-D brick maze fills the screen: walls are courses of bricks (each brick is the two side-by-side
block glyphs 🬗🬤, in brick-red and grey). Somewhere in the lower half sits a small sealed CHAMBER
holding a fish flopping on dry stone — the chamber has a single doorway in its ceiling. Liquid (a
random colour each run) pours in through a gap at the top of the screen and floods the maze under
hydrostatic physics: it falls where it can, pools, and — because connected water shares a common
level — climbs adjacent passages (U-tube) and overflows the low lips. The chamber's lone ceiling
door means it stays dry until the surrounding water has risen to it; then it pours in and fills, and
the fish, now afloat, stops flopping and swims. The simulation is DONE when the chamber is full.

Because the fill must be exact and properly timed, the whole constant-inflow pour is SOLVED ONCE up
front (MazeSim precomputes a timeline of frames ending the instant the chamber tops off); the
animation then plays that timeline back against inspection progress (eased, rate-capped) so the
chamber fills just as inspection completes — no added latency, no dead air.

The fill is a level model: for a surface height L, a breadth-first flood from the inlet lets water
FALL for free (the visible streams) and POOL only through submerged cells (those whose floor is
below L), so a body of water finds one common level (U-tube), overflows the lowest lip to cascade
on, and a top-doored pocket like the chamber floods only once L reaches its door. Frames are spaced
by equal VOLUME, so the on-screen flow rate is constant. Volume is conserved by construction.

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

# -- tuning (all visual; safe to tweak) ---------------------------------------
FPS = 30.0
MIN_DURATION = 2.6                  # play at least this long so the pour is enjoyable on fast boxes
PLAY_EASE = 3.0                     # cursor eases toward progress at this rate (per second)
MAX_FRAMES = 240                    # timeline is sampled to at most this many equal-volume frames
GEN_TRIES = 4                       # generate this many mazes; keep the one that fills the chamber latest
L_STEP = 0.25                       # level granularity when profiling volume vs. surface height


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
        best = None
        for _ in range(GEN_TRIES):
            self._generate()
            frames = self._precompute()
            score = self._wetness(frames[-1])      # more of the maze wet at chamber-full = nicer
            if best is None or score > best[0]:
                best = (score, frames, self._snapshot())
        _, self.frames, snap = best
        self._restore(snap)
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
        else:
            self._carve(cr0, cc0 + cw - 1, cr0, cc0 + cw)  # ...or the right, at a screen edge

        # the top inlet: open the ceiling above a random top-row maze cell up to the screen top
        inlet_cols = [c for c in range(self.mc) if (0, c) not in chamber]
        ic = inlet_cols[rng.randrange(len(inlet_cols))]
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
        # the outside cells just past the chamber's walls — water reaching one of these (submerged)
        # is what opens the door and starts the chamber backfilling.
        doors = set()
        for (y, x) in self.chamber_cells:
            for ny, nx in ((y, x - 1), (y, x + 1), (y - 1, x), (y + 1, x)):
                if (0 <= ny < self.H and 0 <= nx < self.W and self.open[ny][nx]
                        and (ny, nx) not in self.chamber_set):
                    doors.add((ny, nx))
        self.outside_door_cells = sorted(doors)
        self._open_set = set(self.open_cells)

    def _snapshot(self):
        return ([row[:] for row in self.wall], self.source, self.chamber_rc)

    def _restore(self, snap):
        self.wall, self.source, self.chamber_rc = snap
        self._finish()

    # -- the level-flood pour ---------------------------------------------

    def _flood(self, L, block=None):
        '''Water reachable from the inlet at surface level L. Streams FALL for free; a submerged
        cell (floor elevation < L) also spreads to submerged horizontal/upper neighbours (the pool
        finding its level). `block` (a cell set) is treated as sealed — used to hold the chamber back
        during phase A. Returns a `reach` grid — the wet cells this frame.'''
        H, W, op = self.H, self.W, self.open
        blocked = block or ()
        reach = [[False] * W for _ in range(H)]
        sy, sx = self.source
        reach[sy][sx] = True
        dq = deque([(sy, sx)])
        while dq:
            y, x = dq.popleft()
            can_fall = y + 1 < H and op[y + 1][x] and (y + 1, x) not in blocked
            if can_fall and not reach[y + 1][x]:                       # FALL (free)
                reach[y + 1][x] = True
                dq.append((y + 1, x))
            # spread sideways to a submerged neighbour (the pool finding its level) OR, if this cell
            # can't fall, across the ledge as a sheet seeking the next drop (a cascade)
            for ny, nx in ((y, x - 1), (y, x + 1)):
                if (0 <= nx < W and op[ny][nx] and not reach[ny][nx] and (ny, nx) not in blocked
                        and ((H - 1 - ny) < L or not can_fall)):
                    reach[ny][nx] = True
                    dq.append((ny, nx))
            ny = y - 1                                                 # POOL rising: up into submerged
            if (ny >= 0 and op[ny][x] and not reach[ny][x] and (ny, x) not in blocked
                    and (H - 1 - ny) < L):
                reach[ny][x] = True
                dq.append((ny, x))
        return reach

    def _volume(self, L, reach):
        v = 0.0
        H = self.H
        for (y, x) in self.open_cells:
            if reach[y][x]:
                d = L - (H - 1 - y)
                if d > 0:
                    v += 1.0 if d >= 1 else d
        return v

    def _grid_at(self, L, reach):
        '''Per-cell render level: 8 fully submerged, 1..7 a pooled surface eighth, -1 a falling
        stream (wet but above the surface), 0 dry.'''
        H, W = self.H, self.W
        grid = [[0] * W for _ in range(H)]
        for (y, x) in self.open_cells:
            if not reach[y][x]:
                continue
            d = L - (H - 1 - y)
            if d <= 0:
                grid[y][x] = -1                      # reached by falling, still above the surface
            elif d >= 1:
                grid[y][x] = 8
            else:
                lvl = int(d * 8 + 0.5)
                grid[y][x] = 8 if lvl > 8 else (1 if lvl < 1 else lvl)
        return grid

    def _chamber_backfill_vol(self, cl):
        '''Chamber water volume when its own surface sits at local level `cl` (floor -> ceiling).'''
        H = self.H
        return sum(min(1.0, max(0.0, cl - (H - 1 - y))) for (y, x) in self.chamber_cells)

    def _precompute(self):
        '''Two phases, then sampled at EQUAL VOLUME (constant flow) across both:
          A) the OUTSIDE floods (chamber sealed) until water reaches the chamber door; then
          B) the outside holds and the CHAMBER backfills floor -> ceiling, ending when it's full.
        Phase A gives the rising maze; phase B the gradual chamber fill + the fish going afloat.'''
        H, cham = self.H, self.chamber_set
        Lc = self.chamber_top_e + 0.5                      # the side door opens as the outside rises
        profile = []                                       # (L, outside-volume) for phase A         # to the chamber's top: only the lower maze floods, the upper stays dry
        L = 0.0
        while L < Lc:
            profile.append((L, self._volume(L, self._flood(L, block=cham))))
            L += L_STEP
        profile.append((Lc, self._volume(Lc, self._flood(Lc, block=cham))))
        v_a = profile[-1][1]                               # outside volume when the door opens
        reach_c = self._flood(Lc, block=cham)              # frozen outside state for phase B
        c_top = self.chamber_top_e + 1.0
        v_b = self._chamber_backfill_vol(c_top)
        v_tot = v_a + v_b
        nf = min(MAX_FRAMES, max(24, int(v_tot / 3) + 8))
        frames = []
        for k in range(nf):
            vk = v_tot * k / (nf - 1)
            if vk <= v_a:                                  # phase A: outside rising
                lk = self._invert(profile, vk)
                frames.append(self._grid_at(lk, self._flood(lk, block=cham)))
            else:                                          # phase B: chamber backfilling
                cl = self._invert_fn(self._chamber_backfill_vol, self.chamber_floor_e, c_top, vk - v_a)
                frames.append(self._render_backfill(Lc, reach_c, cl))
        return frames

    def _render_backfill(self, Lc, reach_c, cl):
        '''Outside frozen at level Lc; the chamber filled to its own local level cl.'''
        grid = self._grid_at(Lc, reach_c)
        H = self.H
        for (y, x) in self.chamber_cells:
            d = cl - (H - 1 - y)
            grid[y][x] = 0 if d <= 0 else 8 if d >= 0.92 else max(1, min(7, int(d * 8 + 0.5)))
        return grid

    @staticmethod
    def _invert(table, v):
        '''Surface level L whose flood volume is v, by linear interp over the monotone profile.'''
        if v <= table[0][1]:
            return table[0][0]
        for (l0, v0), (l1, v1) in zip(table, table[1:]):
            if v <= v1:
                t = 0.0 if v1 == v0 else (v - v0) / (v1 - v0)
                return l0 + (l1 - l0) * t
        return table[-1][0]

    @staticmethod
    def _invert_fn(f, lo, hi, v):
        '''Level in [lo, hi] whose monotone volume f() equals v, by bisection.'''
        if v <= 0:
            return lo
        for _ in range(32):
            mid = (lo + hi) / 2
            if f(mid) < v:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def _wetness(self, grid):
        wet = sum(1 for (y, x) in self.open_cells if grid[y][x] != 0)
        return wet / max(1, len(self.open_cells))

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
        self._brick = pal.rgb_attr((150, 54, 40)) | curses.A_BOLD      # brick-red
        self._brick2 = pal.rgb_attr((96, 46, 40))                      # a darker course
        self._grey = pal.rgb_attr((120, 120, 128))                     # the odd grey brick
        self._label_attr = pal.get('title') | curses.A_BOLD
        self._bake_bricks()

    def _bake_bricks(self):
        '''Precompute each wall char's glyph + colour: a running-bond masonry (bricks 2 chars wide,
        offset one half each course) in brick-red with scattered grey bricks and a darker alternate
        course, so a wall reads as stone rather than a flat slab.'''
        sim = self.sim
        self._wg = [[None] * sim.W for _ in range(sim.H)]
        for y in range(sim.H):
            shift = y % 2
            for x in range(sim.W):
                if not sim.wall[y][x]:
                    continue
                bx = x + shift
                glyph = BRICK_L if bx % 2 == 0 else BRICK_R
                if (y * 131 + (bx // 2) * 17) % 7 == 0:
                    attr = self._grey
                else:
                    attr = self._brick if y % 2 == 0 else self._brick2
                self._wg[y][x] = (glyph, attr)

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
        for y in range(sim.H):                               # bricks (static masonry)
            wg = self._wg[y]
            for x in range(sim.W):
                if wg[x] is not None:
                    self._add(y, x, wg[x][0], wg[x][1])
        H = sim.H
        for (y, x) in sim.open_cells:
            lvl = grid[y][x]
            if lvl == 0:
                continue
            if lvl == -1:
                self._draw_stream(y, x, frame)               # falling water
            elif lvl >= 8:
                self._add(y, x, FULL, self._band(H - 1 - y))
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
