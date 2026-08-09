'''Unit tests for the maze splash simulation (configsys-splash-maze plugin).

Only the curses-free core (MazeSim: generation, the finite-water pour, playback) is tested here;
MazeSplash.render drives curses and is exercised by hand. The sim is deterministic given its rng.
Requires `configsys` importable (maze.py imports configsys.plugins) — run from the configsys env.'''

import importlib.util
import pathlib
import random
import types

_p = pathlib.Path(__file__).resolve().parent.parent / 'maze.py'
_spec = importlib.util.spec_from_file_location('maze', _p)
maze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maze)


def _sim(w=44, h=22, seed=0):
    return maze.MazeSim(w, h, random.Random(seed))


def _reachable(sim, start):
    seen = {start}
    stack = [start]
    while stack:
        y, x = stack.pop()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if (0 <= ny < sim.H and 0 <= nx < sim.W and sim.open[ny][nx]
                    and (ny, nx) not in seen):
                seen.add((ny, nx))
                stack.append((ny, nx))
    return seen


# -- generation -----------------------------------------------------------

def test_open_space_is_connected_source_reaches_chamber():
    '''A perfect maze + a single chamber door means every open cell — the inlet source and the whole
    chamber included — is one connected region, so the flood can reach the fish.'''
    for seed in range(6):
        sim = _sim(seed=seed)
        reach = _reachable(sim, sim.source)
        assert set(sim.open_cells) == reach                  # nothing walled off from the source
        for cell in sim.chamber_cells:
            assert cell in reach                             # the chamber is reachable


def test_chamber_has_a_single_doorway():
    '''The chamber's open interior touches the outside maze through exactly one passage (its ceiling
    door) — that lone entrance is what makes it flood late and cleanly.'''
    for seed in range(6):
        sim = _sim(seed=seed)
        cham = set(sim.chamber_cells)
        border = 0
        for (y, x) in cham:
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if (0 <= ny < sim.H and 0 <= nx < sim.W and sim.open[ny][nx]
                        and (ny, nx) not in cham):
                    border += 1
        # one WALL_T-wide door: at most WALL_T boundary openings (allow the door's width)
        assert 1 <= border <= maze.CELL_W                    # a single doorway, not a sieve


# -- the level-flood physics (isolated) -----------------------------------

def _phys(open_grid, source):
    '''A bare object carrying just what MazeSim._flood reads, for pure-physics tests.'''
    H = len(open_grid)
    W = len(open_grid[0])
    return types.SimpleNamespace(
        H=H, W=W, open=open_grid, source=source,
        open_cells=[(y, x) for y in range(H) for x in range(W) if open_grid[y][x]])


def test_u_tube_water_finds_a_common_level():
    '''The hydrostatic crux: at surface level L, both arms of a U are wet to the SAME level (water
    climbs the far shaft). Two vertical shafts joined only along the bottom; inlet atop the left.'''
    H, W = 9, 5
    grid = [[False] * W for _ in range(H)]
    for y in range(0, H):                                    # left shaft x=1 (inlet), right shaft x=3
        grid[y][1] = True
    for y in range(1, H):
        grid[y][3] = True
    for x in range(1, 4):                                    # joined along the bottom row
        grid[H - 1][x] = True
    o = _phys(grid, source=(0, 1))
    reach = maze.MazeSim._flood(o, 3.0, block=set())         # surface 3 rows up from the bottom
    for e in (0, 1, 2):                                      # both arms submerged to the SAME level
        y = H - 1 - e
        assert reach[y][1] and reach[y][3]
    assert not reach[H - 1 - 3][3]                           # nothing pooled above the common level


def test_side_doored_pocket_gates_on_the_rising_level():
    '''Air-gating: a pocket whose only opening is up in a SIDE wall takes no water until the outside
    surface rises to that door — then it floods. This is what lets the chamber fill late.'''
    H, W = 10, 6
    grid = [[False] * W for _ in range(H)]
    for y in range(0, H):                                    # inlet shaft on the right, x=4
        grid[y][4] = True
    for y in range(3, H):                                    # a sealed box at x=1, rows 3..9
        grid[y][1] = True
    door_row = 3                                             # its ONLY opening: a side door at row 3
    grid[door_row][2] = grid[door_row][3] = True            # connecting pocket (x=1) to shaft (x=4)
    o = _phys(grid, source=(0, 4))
    door_e = H - 1 - door_row
    low = maze.MazeSim._flood(o, door_e - 0.5, block=set())  # surface just below the door
    assert not any(low[y][1] for y in range(4, H))           # pocket interior still bone dry
    high = maze.MazeSim._flood(o, door_e + 0.5, block=set())  # surface risen past the door
    assert high[H - 1][1]                                    # now the pocket floods


def test_flood_volume_is_monotone_in_level():
    '''More surface height never means less water — the profile the forward sim inverts is monotone.'''
    H, W = 10, 4
    grid = [[y > 0 and x == 1 for x in range(W)] for y in range(H)]   # one open shaft, x=1
    o = _phys(grid, source=(0, 1))
    vols = [maze.MazeSim._volume(o, L, maze.MazeSim._flood(o, L, block=set()))
            for L in (0.0, 1.0, 2.0, 4.0, 8.0)]
    for a, b in zip(vols, vols[1:]):
        assert b >= a


# -- the pour timeline ----------------------------------------------------

def test_pour_ends_with_a_full_chamber():
    for seed in range(4):
        sim = _sim(seed=seed)
        last = sim.frames[-1]
        assert all(last[y][x] >= 7 for (y, x) in sim.chamber_cells)   # brimming at the end


def test_timeline_is_monotone_nondecreasing_volume():
    '''Constant inflow -> the pooled water in each recorded frame only grows (never dips). Stream
    cells (-1) carry no volume; count the pooled eighths.'''
    sim = _sim(seed=2)
    vols = [sum(max(0, f[y][x]) for (y, x) in sim.open_cells) for f in sim.frames]
    for a, b in zip(vols, vols[1:]):
        assert b >= a - 8                                    # allow tiny quantisation wobble


def test_frame_count_is_bounded():
    sim = _sim(seed=1)
    assert 2 <= sim.n <= maze.MAX_FRAMES


# -- playback -------------------------------------------------------------

def test_progress_is_monotonic_and_clamped():
    sim = _sim(seed=0)
    sim.set_progress(0.5)
    assert sim._p == 0.5
    sim.set_progress(0.2)
    assert sim._p == 0.5                                     # never rewinds
    sim.set_progress(5)
    assert sim._p == 1.0                                     # clamped


def test_playback_reaches_filled_at_full_progress():
    sim = _sim(seed=3)
    sim.set_progress(1.0)
    for _ in range(600):
        sim.step(1 / 30)
        if sim.filled:
            break
    assert sim.filled
    grid = sim.frame()
    assert sim.chamber_depth(grid) >= 1                      # the fish is afloat by the end


def test_determinism_by_seed():
    a, b = _sim(seed=7), _sim(seed=7)
    assert a.wall == b.wall
    assert a.frames == b.frames
    c = _sim(seed=8)
    assert c.wall != a.wall


def test_chamber_depth_zero_when_dry():
    sim = _sim(seed=0)
    assert sim.chamber_depth(sim.frames[0]) == 0            # first frame: bone dry, fish flopping
