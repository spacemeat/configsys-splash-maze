'''maze.py — the "maze" startup splash for configsys, as a code plugin.

A centipede crawls through a brick maze toward a chamber of quivering eggs. The trick that makes it
fit a startup's timing: the build is DEFERRED to the first render, when the component count is known,
and the PATH is built first — sized to that count (she wanders ~components/COMPONENTS_PER_WANDER_ROOM
rooms, then beelines to the nest) — and only THEN the maze (walls, offshoots, dead ends) is grown
AROUND that path, so she just looks like she's solving it. Her head is progress mapped along the
path; the route's tail serpentines the egg chamber so her WHOLE body coils onto the floor, and a
little of the run is reserved for the happy ending. Off the path, in wide water-holding basins, swim
ocean-style fish.

Coupling the path to the load means she paces to the real work: a bigger load = a windier, longer
trip at the same crawl speed. There's no physics and no heavy precompute — generation is a couple of
cheap graph walks; the animation is procedural from `progress` (the crawl) and elapsed time (legs,
quiver, fish).

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

# Fish (from configsys-splash-ocean): [tail bracket][taper tri][1-3 body][head tri]; the head points
# the swim direction. Two sizes (big ■/◀▶, small ▪/◄►) and assorted ornamental tail brackets.
_TAILS = ('❨❩', '❪❫', '❬❭', '❮❯', '❰❱', '❲❳', '❴❵')
_FISH_SIZES = (('■', '◀', '▶'), ('▪', '◄', '►'))
FISH_RIGHT, FISH_LEFT = [], []
for _b, _lt, _rt in _FISH_SIZES:
    for _n in (1, 2, 3):
        for _ob, _cb in _TAILS:
            FISH_RIGHT.append(_cb + _lt + _b * _n + _rt)   # ❩◀■▶  tail, taper, body, head
            FISH_LEFT.append(_lt + _b * _n + _rt + _ob)    # ◄▪►❰
FISH_RIGHT, FISH_LEFT = tuple(FISH_RIGHT), tuple(FISH_LEFT)

# -- geometry -----------------------------------------------------------------
CW = 2                             # a maze cell is CW chars wide x 1 tall (≈ square on a terminal)

# -- tuning -------------------------------------------------------------------
FPS = 30.0
MIN_DURATION = 2.8
PLAY_EASE = 3.2                    # the crawl eases toward progress at this rate (per second)
BODY_MIN, BODY_MAX = 8, 18         # centipede body length (segments), scaled to the maze
ENDING_FRAC = 0.12                 # last fraction of the run: she's coiled on the floor, hearts play
# Route length sets the crawl SPEED (she covers it over the run, so shorter = slower per block). A
# fraction of the rooms, floored and CAPPED so she doesn't blur through a huge terminal — tuned for
# ~half the earlier pace (roughly a block per two components). The route then SERPENTINES the egg
# chamber, so its last body-length lies inside the chamber and her whole body coils onto the floor.
# Her WANDER length — the rooms she loops through before beelining to the nest — scales with the
# number of components to load (known once inspection starts; the splash defers its build until then).
# So she paces to the real work: a bigger load = a windier, longer trip at the SAME crawl speed.
#   wander ≈ components / COMPONENTS_PER_WANDER_ROOM   (floored, and capped to a fraction of the maze)
# Lower COMPONENTS_PER_WANDER_ROOM ⇒ a WINDIER, slightly faster crawl (more looping per component).
COMPONENTS_PER_WANDER_ROOM = 3.0
WANDER_MIN, WANDER_MAX_FRAC = 6, 0.8
BEELINE_CHOICES = 4                            # after wandering, approach the nest directly (1); the
                                               # WANDER carries the windiness. Raise for a meandering
                                               # (windier but longer, less predictable) approach.
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

    def __init__(self, w, h, rng, wander=None):
        self.W = max(20, int(w))
        self.H = max(12, int(h))
        self.rng = rng
        self._wander = wander                  # rooms to loop before beelining (None => a default)
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
        entrance = (cr0, cc0)                      # the room the path arrives at (a top corner)

        # 1) plan the bigger open CHAMBERS up front (rooms reserved, each with ONE chosen opening —
        # high => a water-holding basin, low => it just drains), so the path + maze go around them.
        self._plan_chambers()
        avoid = self.chamber_rooms | self.decor_rooms

        # 2) THE PATH: a winding self-avoiding route from a top edge to the chamber entrance, kept out
        # of the chambers until it's wandered enough, tuned to a good (now shorter, slower) length.
        rooms = self.gw * self.gh
        wander = self._wander if self._wander is not None else max(WANDER_MIN, rooms // 12)
        target = max(WANDER_MIN, min(int(rooms * WANDER_MAX_FRAC), wander))
        starts = [(0, c) for c in range(self.gw) if (0, c) not in avoid]
        self.path_rooms = None
        for _ in range(GEN_TRIES):                             # wander ~target rooms, then beeline in
            p = self._walk(starts[rng.randrange(len(starts))], entrance, target, avoid)
            if p:
                self.path_rooms = p
                break
        if self.path_rooms is None:
            self.path_rooms = self._walk(starts[0], entrance, 0, avoid) or [entrance]
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
        # body: scaled to the route, but never longer than the coil-region so it all fits on the floor
        self.body = max(BODY_MIN, min(BODY_MAX, len(self.route) // 4, self._cham_route))

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
            if len(stack) >= want_min:                          # wandered enough -> head for the
                nb.sort(key=lambda n: abs(n[0] - goal[0]) + abs(n[1] - goal[1]))         # nest, but
                nxt = nb[rng.randrange(min(BEELINE_CHOICES, len(nb)))]                   # meander a bit
            else:
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
        cells.append(self._room_cell(*self.path_rooms[-1]))      # the entrance-corner cell
        path_chars = self._trace(cells)
        # into the chamber: descend the corner column, then SERPENTINE only the bottom coil-region
        # (just enough rows for her body), ending on the FLOOR — so she reaches the bottom and her
        # whole body coils there, without a long chamber detour that would speed the crawl back up.
        y0, y1 = 2 * cr0 + 1, 2 * (cr0 + ch - 1) + 1
        x0, x1 = (2 * cc0 + 1) * CW, (2 * (cc0 + cw - 1) + 1) * CW + CW - 1
        cwid = min(x1 - x0 + 1, 12)                              # coil this wide (a coil, not a sweep)
        xr = x0 + cwid - 1
        need = BODY_MAX + 6
        kr = min(y1 - y0 + 1, max(2, (need + cwid - 1) // cwid))  # rows of coil needed
        top_coil = y1 - kr + 1
        cham = [(y, x0) for y in range(y0 + 1, top_coil) if self.open[y][x0]]  # descend the corner
        for ri, y in enumerate(range(top_coil, y1 + 1)):         # boustrophedon down to the floor
            cols = range(x0, xr + 1) if ri % 2 == 0 else range(xr, x0 - 1, -1)
            for x in cols:
                if self.open[y][x]:
                    cham.append((y, x))
        self.route = path_chars + cham
        self._cham_route = len(cham)
        self.egg_end = self.route[-1]

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
        ey, ex = self.egg_end                         # the nest sits where the crawl ends
        self.eggs = []
        for dx in (0, -1, 1, -2, 2, -3, 3):
            x = ex + dx
            if x0 <= x <= x1 and not self.wall[ey][x]:
                self.eggs.append((ey, x))
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
            ch['fishes'] = []
            if ch['fish']:
                fits = [i for i in range(len(FISH_RIGHT)) if len(FISH_RIGHT[i]) <= x1 - x0]
                for k in range(max(1, ch['width'] // 2)):
                    i = (fits or range(len(FISH_RIGHT)))[self.rng.randrange(len(fits) or len(FISH_RIGHT))]
                    ch['fishes'].append({'r': FISH_RIGHT[i], 'l': FISH_LEFT[i], 'row': k,
                                         'phase': self.rng.uniform(0, 6.28)})

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
        '''Her head has reached the eggs (the tail may still be coiling in — hearts start now).'''
        return self.head_index() >= len(self.route) - 1 - 1e-6

    @property
    def filled(self):
        return self._cursor >= 0.999

    def head_index(self):
        '''Float head index along self.route (0 .. len-1). The head reaches the eggs a touch before
        the very end (all but the last ENDING_FRAC of progress); since the route's tail serpentines
        the chamber, her whole body is on the floor by then, and the hearts play out the rest.'''
        t = min(1.0, self._cursor / max(1e-6, 1.0 - ENDING_FRAC))
        return t * (len(self.route) - 1)


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
        # DEFER the build: the maze/path is sized to the component count, which we only learn from
        # frame.counts at render time — so build lazily on the first render (see _build).
        self.sim = None

    def _build(self, total):
        pal, rng = self.pal, self.rng
        # size the wander to the load, then build the maze around a path of that length
        gw = max(3, (self.w // CW - 1) // 2)
        gh = max(3, (self.h - 1) // 2)
        wander = None
        if total and total > 0:
            wander = max(WANDER_MIN, min(int(gw * gh * WANDER_MAX_FRAC),
                                         round(total / COMPONENTS_PER_WANDER_ROOM)))
        self.sim = MazeSim(self.w, self.h, rng, wander=wander)
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
        if self.sim is None:                                 # first frame: now we know the load size
            self._build(frame.counts[1] if frame.counts else 0)
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
            span = ch['x1'] - ch['x0'] + 1
            depth = max(1, y1 - surf + 1)
            for fk in ch['fishes']:                          # ocean-style fish paddling about
                t = frame.elapsed * 0.9 + fk['phase']
                glyph = fk['r'] if math.cos(t) >= 0 else fk['l']
                fy = surf + (fk['row'] % depth)
                fx = int(ch['x0'] + (0.5 + 0.5 * math.sin(t)) * max(0, span - len(glyph)))
                for i, gch in enumerate(glyph):
                    gx = fx + i
                    if 0 <= fy < sim.H and ch['x0'] <= gx <= ch['x1'] and sim.open[fy][gx]:
                        self._add(fy, gx, gch, self._fishc)

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
        if arrived:                                          # a happy flourish above her, over the
            hy, _ = sim.route[min(len(sim.route) - 1, int(sim.head_index()))]   # nest — centred in the
            y0, y1, x0, x1 = sim.chamber_box                                    # chamber so it never
            cx = (x0 + x1) // 2                                                 # clips the walls
            for k in range(3):
                fy = hy - 1 - (int(frame.elapsed * 4 + k) % 2)   # bob a row or two over her head
                fx = cx + (k - 1) * 2                            # spread a little around centre
                if fy >= 0 and x0 <= fx <= x1 and sim.open[fy][fx]:
                    self._add(fy, fx, HEART if k % 2 else SPARKLE[int(frame.elapsed * 5 + k) % 3],
                              self._heart)

    def _draw_centipede(self, frame):
        sim = self.sim
        route = sim.route
        n = len(route)
        body = sim.body
        hi = int(sim.head_index())
        wig = frame.elapsed * 9.0
        for s in range(body):
            idx = hi - s
            if idx < 0:
                continue                 # this segment hasn't emerged from the top edge yet
            idx = min(n - 1, idx)        # past the route end: the body coils onto the eggs
            y, x = route[idx]
            if s == 0:
                self._add(y, x, CENT_HEAD, self._cent[2])
                # antennae wiggle
                ay = y - 1
                if ay >= 0:
                    self._add(ay, x, LEGS[int(wig) % 2], self._cent[1])
            elif s == body - 1:
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
