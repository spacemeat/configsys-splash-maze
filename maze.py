'''maze.py — the "maze" startup splash for configsys, as a code plugin.

A centipede crawls through a brick maze toward a chamber of quivering eggs. The trick that makes it
fit a startup's timing perfectly: we know the step budget up front, so we build the PATH first — a
winding, twisting route from an edge to the chamber, adjusted to a good length — and only THEN grow
the maze (walls, offshoots, dead ends) AROUND that path, so the centipede just looks like it's
solving a maze. Her head position is simply progress mapped along the path (each cell ~ one component
checked); a little tail of the run is reserved for the happy ending as she reaches her eggs. Off the
path, in maze nooks she never visits, sit little standing pools with fish.

There's no physics and no precompute to speak of — generation is a couple of cheap graph walks; the
animation is procedural from `progress` (the crawl) and elapsed time (legs, quiver, fish). So it's
instant to build and always lands on time.

Ships as a configsys splash provider (see configsys/splashes.py for the ABI): the HOST
(configsys.tui.splash.run_splash) owns the frame loop and calls render(frame); MazeSim is the
curses-free, deterministic sim (unit-tested); MazeSplash the curses renderer. `SPLASHES` at the foot
exports it so the trusted loader registers `splash: maze`.
'''

import colorsys
import curses
import math

from configsys.plugins import Splash

# -- glyphs -------------------------------------------------------------------
BRICK_L, BRICK_R = '🬗', '🬤'        # the two halves of one brick, drawn side by side
CENT_HEAD = '◉'                    # the centipede's head
CENT_BODY = '●'                    # a body segment
CENT_TAIL = '◗'                    # the tail tip
LEGS = ('╱', '╲')                  # little legs, alternated for a crawl
EGG = '○'
EGG_JIGGLE = ('○', '◌', '◯')       # egg quiver frames
HEART = '♥'
SPARKLE = ('✦', '✧', '⋆')
EIGHTHS = ' ▁▂▃▄▅▆▇█'              # pool water height
FISH_R = ('◄▪►', '◄▪▪►')
FISH_L = ('◄▪►', '◄▪▪►')

# -- geometry -----------------------------------------------------------------
CW = 2                             # a maze cell is CW chars wide x 1 tall (≈ square on a terminal)

# -- tuning -------------------------------------------------------------------
FPS = 30.0
MIN_DURATION = 2.8
PLAY_EASE = 3.2                    # the crawl eases toward progress at this rate (per second)
BODY_MIN, BODY_MAX = 8, 16         # centipede body length (segments), scaled to the maze
ENDING_FRAC = 0.13                 # last fraction of the run: she's home on the floor, hearts play
PATH_LO, PATH_HI = 0.17, 0.31      # target path length (fraction of rooms) — halved so she crawls
                                   # slower: each block spans ~2 components now, not one
GEN_TRIES = 60                     # path attempts; keep the best-length winding route
FISH_MIN_W, FISH_MAX_W = 3, 6      # only basins this many blocks wide get fish


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


class MazeSim:
    '''Curses-free maze + centipede route. Construction builds a winding PATH from an edge to a
    lower chamber (length-tuned), grows the rest of the maze around it (offshoots, dead ends), places
    the eggs and a few fish pools, and exposes the crawl as a function of progress. Deterministic
    given `rng`.'''

    def __init__(self, w, h, rng):
        self.W = max(20, int(w))
        self.H = max(12, int(h))
        self.rng = rng
        self._dims()
        self._generate()
        self._p = 0.0
        self._cursor = 0.0

    # -- geometry ---------------------------------------------------------

    def _dims(self):
        cols = self.W // CW
        rows = self.H
        self.gw = max(3, (cols - 1) // 2)          # rooms across
        self.gh = max(3, (rows - 1) // 2)          # rooms down

    def _room_cell(self, r, c):                    # room (r,c) -> cell (cr, cc)
        return (2 * r + 1, 2 * c + 1)

    def _cell_chars(self, cr, cc):                 # cell -> its char positions (CW wide, 1 tall)
        y = cr
        x0 = cc * CW
        return y, x0

    def _open_cell(self, cr, cc):
        y, x0 = self._cell_chars(cr, cc)
        if 0 <= y < self.H:
            for x in range(x0, min(x0 + CW, self.W)):
                self.wall[y][x] = False

    def _room_neighbors(self, r, c):
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < self.gh and 0 <= nc < self.gw:
                yield (nr, nc)

    def _carve(self, a, b):                        # open two rooms + the passage cell between them
        (r1, c1), (r2, c2) = a, b
        self._open_cell(*self._room_cell(r1, c1))
        self._open_cell(*self._room_cell(r2, c2))
        self._open_cell(r1 + r2 + 1, c1 + c2 + 1)  # midpoint cell
        self.graph.setdefault(a, set()).add(b)
        self.graph.setdefault(b, set()).add(a)

    # -- generation -------------------------------------------------------

    def _generate(self):
        self.wall = [[True] * self.W for _ in range(self.H)]
        self.graph = {}
        rng = self.rng
        # the EGG chamber: a roomy block low and central — the destination she walks down into
        cw = min(self.gw - 2, max(4, self.gw // 4))
        ch = max(3, self.gh // 4)
        cc0 = (self.gw - cw) // 2
        cr0 = self.gh - ch
        self.chamber_rooms = {(r, c) for r in range(cr0, cr0 + ch) for c in range(cc0, cc0 + cw)}
        entrance = (cr0, cc0 + cw // 2)            # the room the path arrives at (top-centre of chamber)

        # 1) plan the bigger open CHAMBERS up front (rooms reserved, each with ONE chosen opening —
        # high => a water-holding basin, low => it just drains), so the path + maze go around them.
        self._plan_chambers()
        avoid = self.chamber_rooms | self.decor_rooms

        # 2) THE PATH: a winding self-avoiding route from a top edge to the chamber entrance, kept out
        # of the chambers until it's wandered enough, tuned to a good (now shorter, slower) length.
        rooms = self.gw * self.gh
        lo, hi = int(rooms * PATH_LO), int(rooms * PATH_HI)
        starts = [(0, c) for c in range(self.gw) if (0, c) not in avoid]
        best = None
        for _ in range(GEN_TRIES):
            start = starts[rng.randrange(len(starts))]
            p = self._walk(start, entrance, lo, avoid)
            if p and (best is None or _closer(len(p), lo, hi, len(best))):
                best = p
                if lo <= len(p) <= hi:
                    break
        self.path_rooms = best or self._walk(starts[0], entrance, 0, avoid) or [entrance]
        for a, b in zip(self.path_rooms, self.path_rooms[1:]):
            self._carve(a, b)

        # 3) THE MAZE AROUND IT: a spanning tree into every other room (not the reserved chambers),
        # hanging off the path — so the route is indistinguishable from the offshoots and dead ends.
        visited = set(self.path_rooms) | avoid
        stack = [rng.choice(self.path_rooms)]
        while stack:
            cur = stack[-1]
            unv = [n for n in self._room_neighbors(*cur) if n not in visited]
            if unv:
                nxt = unv[rng.randrange(len(unv))]
                self._carve(cur, nxt)
                visited.add(nxt)
                stack.append(nxt)
            else:
                stack.pop()
            if not stack:                          # unvisited rooms left? attach one to the tree + restart
                # anchor to a visited MAZE room (not a reserved chamber — those aren't in the tree
                # yet), or the region hangs off a chamber as its own disconnected component.
                rem = [(r, c) for r in range(self.gh) for c in range(self.gw)
                       if (r, c) not in visited
                       and any(n in visited and n not in avoid for n in self._room_neighbors(r, c))]
                if rem:
                    room = rem[rng.randrange(len(rem))]
                    anchor = next(n for n in self._room_neighbors(*room) if n in visited and n not in avoid)
                    self._carve(room, anchor)
                    visited.add(room)
                    stack = [room]

        # open the egg chamber into one clean room, and each decorative chamber + its single opening
        self._open_block(cr0, cc0, ch, cw)
        for cd in self.chambers:
            r, c, bh, bw = cd['r'], cd['c'], cd['bh'], cd['bw']
            self._open_block(r, c, bh, bw)
            self._carve(cd['edge'], cd['adj'])     # the one opening (top = basin, bottom = drains)
        self.open = [[not self.wall[y][x] for x in range(self.W)] for y in range(self.H)]

        # 4) the crawl route (down into the egg chamber to the floor), the eggs, and which chambers
        # are water-holding basins wide enough for fish.
        self._build_route(entrance, cr0, cc0, cw, ch)
        self._place_eggs(cr0, cc0, cw, ch)
        self._compute_basins()

    def _open_block(self, r, c, bh, bw):
        for cr in range(2 * r + 1, 2 * (r + bh - 1) + 2):
            for cc in range(2 * c + 1, 2 * (c + bw - 1) + 2):
                self._open_cell(cr, cc)

    def _walk(self, start, goal, want_min, avoid):
        '''A random self-avoiding DFS from start to goal (its stack IS the path). It won't step onto
        the goal until it has wandered `want_min` rooms, which biases a long, twisty route; it steers
        clear of the reserved chamber rooms in `avoid`.'''
        rng = self.rng
        stack = [start]
        seen = {start}
        guard = 0
        cap = self.gw * self.gh * 4
        while stack:
            guard += 1
            if guard > cap:
                return None
            cur = stack[-1]
            if cur == goal:
                return list(stack)
            nb = [n for n in self._room_neighbors(*cur)
                  if n not in seen and n not in avoid]
            if goal in self._room_neighbors(*cur) and len(stack) >= want_min:
                nb.append(goal)
            if not nb:
                stack.pop()
                continue
            nxt = nb[rng.randrange(len(nb))]
            stack.append(nxt)
            seen.add(nxt)
        return None

    def _build_route(self, entrance, cr0, cc0, cw, ch):
        cells = []
        for a, b in zip(self.path_rooms, self.path_rooms[1:]):
            cells.append(self._room_cell(*a))
            (r1, c1), (r2, c2) = a, b
            cells.append((r1 + r2 + 1, c1 + c2 + 1))
        cells.append(self._room_cell(*self.path_rooms[-1]))
        # continue down into the open chamber to the egg cell
        er, ec = self._room_cell(cr0 + ch - 1, cc0 + cw // 2)     # egg room cell
        cy, cx = cells[-1]
        while cy < er:
            cy += 1
            cells.append((cy, cx))
        self.egg_cell = (er, ec)
        self.route = self._trace(cells)

    def _trace(self, cells):
        '''Cell list -> per-char centreline (insert a midpoint on 2-wide horizontal steps) for a
        smooth crawl. Uses the left char of each cell; the other half of the corridor is left for legs.'''
        chars = []
        for cr, cc in cells:
            y, x = cr, cc * CW
            if chars:
                py, px = chars[-1]
                if y == py and abs(x - px) == CW:
                    chars.append((y, (x + px) // 2))
                elif y == py and abs(x - px) > CW:      # (shouldn't happen) fill straight
                    step = 1 if x > px else -1
                    for xx in range(px + step, x, step):
                        chars.append((y, xx))
            chars.append((y, x))
        # de-dup consecutive
        out = [chars[0]]
        for p in chars[1:]:
            if p != out[-1]:
                out.append(p)
        return out

    def _place_eggs(self, cr0, cc0, cw, ch):
        y0 = 2 * cr0 + 1                              # char rows == cell rows
        y1 = 2 * (cr0 + ch - 1) + 1
        x0 = (2 * cc0 + 1) * CW                       # char cols == cell col * CW
        x1 = (2 * (cc0 + cw - 1) + 1) * CW + CW - 1
        floor = y1                                    # bottom row of the chamber
        mid = (x0 + x1) // 2
        self.eggs = []
        for dx in (-3, -1, 1, 3, 0, -2, 2):
            x = mid + dx
            if 0 <= x < self.W and not self.wall[floor][x]:
                self.eggs.append((floor, x))
        self.chamber_box = (y0, y1, x0, x1)

    def _plan_chambers(self):
        '''Reserve several bigger open room-blocks off the egg chamber, kept apart by a 1-room margin,
        each with exactly ONE opening chosen up front: mostly a HIGH opening (so it holds water — a
        basin) but sometimes a LOW one (so it just drains, dry). Actual carving happens in _generate;
        which basins get fish is decided by width in _compute_basins.'''
        rng = self.rng
        taken = set(self.chamber_rooms) | {n for rm in self.chamber_rooms
                                           for n in self._room_neighbors(*rm)}
        self.chambers = []
        self.decor_rooms = set()
        want = max(2, (self.gw * self.gh) // 16)
        for _ in range(300):
            if len(self.chambers) >= want:
                break
            bw = rng.randint(2, 4)
            bh = rng.randint(2, 3)
            if bw >= self.gw or bh >= self.gh:
                continue
            r = rng.randint(1, self.gh - bh - 1)                 # leave room for a top/bottom opening
            c = rng.randint(0, self.gw - bw)
            block = {(rr, cc) for rr in range(r, r + bh) for cc in range(c, c + bw)}
            margin = block | {n for rm in block for n in self._room_neighbors(*rm)}
            if margin & taken:
                continue
            mid = c + bw // 2
            top, bot = (r - 1, mid), (r + bh, mid)               # rooms just above / below the block
            if rng.random() < 0.68 and top not in self.chamber_rooms:
                edge, adj = (r, mid), top                        # high opening -> basin
            elif r + bh < self.gh and bot not in self.chamber_rooms:
                edge, adj = (r + bh - 1, mid), bot               # low opening -> drains
            elif top not in self.chamber_rooms:
                edge, adj = (r, mid), top
            else:
                continue
            taken |= margin
            self.decor_rooms |= block
            self.chambers.append({'r': r, 'c': c, 'bh': bh, 'bw': bw, 'edge': edge, 'adj': adj,
                                  'y0': 2 * r + 1, 'y1': 2 * (r + bh - 1) + 1,
                                  'x0': (2 * c + 1) * CW, 'x1': (2 * (c + bw - 1) + 1) * CW + CW - 1})

    def _compute_basins(self):
        '''For each chamber decide if it's a BASIN — water poured in would pool — by finding its
        lowest opening (where water would drain). Water sits from the floor up to just below that
        rim; a chamber that's wide enough (FISH_MIN_W..FISH_MAX_W blocks) and holds real depth gets
        fish. A chamber whose lowest opening is at its floor just drains (dry).'''
        for ch in self.chambers:
            y0, y1, x0, x1 = ch['y0'], ch['y1'], ch['x0'], ch['x1']
            lowest_opening = y0 - 1                           # nothing found yet
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    if not self.open[y][x]:
                        continue
                    for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                        outside = not (y0 <= ny <= y1 and x0 <= nx <= x1)
                        if outside and 0 <= ny < self.H and 0 <= nx < self.W and self.open[ny][nx]:
                            lowest_opening = max(lowest_opening, y)
            ch['width'] = (x1 - x0 + 1) // CW
            ch['surface'] = lowest_opening + 1                # water top; below the rim
            ch['depth'] = y1 - lowest_opening
            ch['basin'] = ch['depth'] >= 1
            ch['fish'] = ch['basin'] and FISH_MIN_W <= ch['width'] <= FISH_MAX_W
            ch['phase'] = self.rng.uniform(0, 6.28)

    # -- runtime ----------------------------------------------------------

    def set_progress(self, frac):
        self._p = max(self._p, 0.0 if frac < 0 else 1.0 if frac > 1 else frac)

    def step(self, dt):
        if dt <= 0:
            return
        rate = PLAY_EASE * (1.8 if self._p >= 0.999 else 1.0)
        self._cursor += (self._p - self._cursor) * min(1.0, dt * rate)

    @property
    def arrived(self):
        return self._cursor >= (1.0 - ENDING_FRAC) - 1e-3

    @property
    def filled(self):
        return self._cursor >= 0.999

    def head_index(self):
        '''Float index of the head along self.route (0..len-1). The crawl uses all but the last
        ENDING_FRAC of progress; the tail of the run is the happy ending at the eggs.'''
        t = min(1.0, self._cursor / max(1e-6, 1.0 - ENDING_FRAC))
        return t * (len(self.route) - 1)


def _closer(n, lo, hi, cur):
    '''Is length n a better fit for [lo,hi] than cur? Prefer in-range, else nearer the band.'''
    def score(x):
        if lo <= x <= hi:
            return 0
        return min(abs(x - lo), abs(x - hi))
    return score(n) < score(cur)


class MazeSplash(Splash):
    '''The `maze` splash: a curses driver around a MazeSim. Bakes the brick + creature palette once,
    then render(frame) crawls the centipede along the route toward frame.progress and paints the
    bricks, the fish pools, the centipede (legs a-wiggle), and the egg chamber (quivering, then a
    happy flourish when she arrives).'''

    name = 'maze'
    fps = FPS
    min_duration = MIN_DURATION

    def __init__(self, scr, pal, size, seed=None):
        super().__init__(scr, pal, size, seed)
        self.sim = MazeSim(self.w, self.h, self.rng)
        rng = self.rng
        self._brick = pal.rgb_pair((150, 54, 40), (108, 104, 98))
        self._brick_grey = pal.rgb_pair((120, 120, 126), (96, 96, 100))
        hue = rng.random()
        self._cent = [pal.rgb_attr(_hsv(hue, 0.75, v)) | curses.A_BOLD for v in (0.55, 0.75, 0.95)]
        self._legs = pal.rgb_attr(_hsv(hue, 0.5, 0.7))
        self._egg = pal.rgb_attr((238, 232, 210)) | curses.A_BOLD
        self._egg_warm = pal.rgb_attr((250, 214, 160)) | curses.A_BOLD
        self._heart = pal.rgb_attr((240, 120, 140)) | curses.A_BOLD
        wr, wg, wb = _hsv(rng.uniform(0.5, 0.62), 0.7, 0.9)
        self._water = pal.rgb_attr((wr, wg, wb))
        self._fishc = pal.rgb_pair(_hsv(rng.uniform(0.05, 0.15), 0.7, 0.95), (wr // 3, wg // 3, wb // 3)) | curses.A_BOLD
        self._label_attr = pal.get('title') | curses.A_BOLD
        self._bake_walls()

    def _bake_walls(self):
        sim = self.sim
        self._wg = [[None] * sim.W for _ in range(sim.H)]
        for y in range(sim.H):
            shift = y % 2
            for x in range(sim.W):
                if sim.wall[y][x]:
                    glyph = BRICK_L if (x + shift) % 2 == 0 else BRICK_R
                    grey = (y * 131 + (x + shift) // 2 * 17) % 6 == 0
                    self._wg[y][x] = (glyph, self._brick_grey if grey else self._brick)

    def render(self, frame):
        self.sim.set_progress(frame.progress)
        self.sim.step(frame.dt)
        sim, scr = self.sim, self.scr
        scr.erase()
        for y in range(sim.H):                               # bricks
            wg = self._wg[y]
            for x in range(sim.W):
                if wg[x] is not None:
                    self._add(y, x, wg[x][0], wg[x][1])
        self._draw_basins(frame)
        self._draw_eggs(frame)
        self._draw_centipede(frame)
        if frame.label:
            self._draw_label(frame)
        return sim.filled

    def _draw_basins(self, frame):
        '''Standing water in the chambers that are basins; fish only in the wide-enough ones.'''
        sim = self.sim
        for ch in sim.chambers:
            if not ch['basin']:
                continue
            surf, y1 = ch['surface'], ch['y1']
            for y in range(surf, y1 + 1):
                for x in range(ch['x0'], ch['x1'] + 1):
                    if sim.open[y][x]:
                        self._add(y, x, '█' if y > surf else '▆', self._water)
            if not ch['fish']:
                continue
            span = max(1, ch['x1'] - ch['x0'] - 1)
            depth = max(1, y1 - surf + 1)
            for k in range(max(1, ch['width'] // 2)):        # a fish or three, by width
                t = frame.elapsed * 1.1 + ch['phase'] + k * 2.1
                fx = int(ch['x0'] + 1 + (0.5 + 0.5 * math.sin(t)) * (span - 1))
                fy = surf + (k % depth)
                if 0 <= fy < sim.H and 0 <= fx < sim.W and sim.open[fy][fx]:
                    self._add(fy, fx, '►' if math.cos(t) >= 0 else '◄', self._fishc)

    def _draw_eggs(self, frame):
        sim = self.sim
        arrived = sim.arrived
        base = frame.elapsed * (7.0 if arrived else 3.0)
        for i, (y, x) in enumerate(sim.eggs):
            jig = EGG_JIGGLE[int(base + i) % len(EGG_JIGGLE)]
            dx = 0
            if arrived and int(base * 1.7 + i) % 2:
                dx = 1 if i % 2 else -1
            self._add(y, x + dx, jig, self._egg_warm if arrived else self._egg)
        if arrived:                                          # a happy flourish over the nest
            y0 = sim.chamber_box[0]
            for k, (y, x) in enumerate(sim.eggs[:3]):
                fy = y0 - 1 - (int(frame.elapsed * 4 + k) % 2)
                self._add(fy, x, HEART if k % 2 else SPARKLE[int(frame.elapsed * 5 + k) % 3], self._heart)

    def _draw_centipede(self, frame):
        sim = self.sim
        route = sim.route
        head = sim.head_index()
        n = len(route)
        body = max(BODY_MIN, min(BODY_MAX, n // 4))
        hi = int(head)
        # segments from head back along the route
        wig = frame.elapsed * 9.0
        for s in range(body):
            idx = hi - s
            if idx < 0:
                break
            y, x = route[idx]
            if s == 0:
                self._add(y, x, CENT_HEAD, self._cent[2])
                # antennae wiggle
                ay = y - 1
                if ay >= 0:
                    self._add(ay, x, LEGS[int(wig) % 2], self._cent[1])
            elif s == body - 1 or idx == 0:
                self._add(y, x, CENT_TAIL, self._cent[1])
            else:
                shade = self._cent[1 + (int(wig + s) % 2)]   # a peristalsis ripple down the body
                self._add(y, x, CENT_BODY, shade)
                # legs on the free half of the corridor, alternating
                lx = x + 1 if (x + 1 < sim.W and sim.open[y][x + 1]) else x - 1
                if 0 <= lx < sim.W and sim.open[y][lx]:
                    self._add(y, lx, LEGS[(s + int(wig)) % 2], self._legs)

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
        except curses.error:
            pass


SPLASHES = [MazeSplash]
