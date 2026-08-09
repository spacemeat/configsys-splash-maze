'''Unit tests for the maze/centipede splash (configsys-splash-maze plugin).

Only the curses-free core (MazeSim: the path-first maze, the crawl route, playback) is tested here;
MazeSplash.render drives curses and is exercised by hand. Deterministic given its rng. Requires
`configsys` importable (maze.py imports configsys.plugins) — run from the configsys env.'''

import importlib.util
import pathlib
import random

_p = pathlib.Path(__file__).resolve().parent.parent / 'maze.py'
_spec = importlib.util.spec_from_file_location('maze', _p)
maze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maze)


def _sim(w=64, h=26, seed=0):
    return maze.MazeSim(w, h, random.Random(seed))


def _connected_from(sim, start):
    seen = {start}
    stack = [start]
    while stack:
        y, x = stack.pop()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < sim.H and 0 <= nx < sim.W and sim.open[ny][nx] and (ny, nx) not in seen:
                seen.add((ny, nx))
                stack.append((ny, nx))
    return seen


# -- generation -----------------------------------------------------------

def test_maze_is_one_connected_region():
    '''Path + offshoots form one connected maze — no walled-off pockets.'''
    for seed in range(6):
        sim = _sim(seed=seed)
        opencells = {(y, x) for y in range(sim.H) for x in range(sim.W) if sim.open[y][x]}
        assert _connected_from(sim, sim.route[0]) == opencells


def test_route_runs_from_a_top_edge_to_the_eggs():
    for seed in range(6):
        sim = _sim(seed=seed)
        assert sim.route[0][0] <= 2                       # starts up near the top edge
        end = sim.route[-1]
        assert sim.eggs and any(abs(end[0] - ey) + abs(end[1] - ex) <= 2 for ey, ex in sim.eggs)


def test_route_is_contiguous():
    '''Consecutive route cells are orthogonally adjacent, so the crawl is a smooth unbroken line.'''
    sim = _sim(seed=1)
    for (y0, x0), (y1, x1) in zip(sim.route, sim.route[1:]):
        assert abs(y0 - y1) + abs(x0 - x1) == 1


def test_path_is_a_modest_fraction_not_maze_filling():
    '''She wanders a bit then beelines to the nest — a modest route, not a maze-filling tour (so the
    crawl stays slow), even on a big maze.'''
    for seed in range(6):
        sim = _sim(seed=seed)
        assert 2 <= len(sim.path_rooms) < 0.35 * sim.gw * sim.gh
    big = maze.MazeSim(200, 50, random.Random(0))
    assert len(big.path_rooms) < 0.2 * big.gw * big.gh


def test_whole_body_ends_inside_the_chamber():
    '''The route serpentines the egg chamber, so its last body-length of cells all lie in the
    chamber box — her whole centipede coils onto the floor, not just the head.'''
    sim = _sim(seed=1)
    y0, y1, x0, x1 = sim.chamber_box
    for (y, x) in sim.route[-sim.body:]:
        assert y0 <= y <= y1 and x0 <= x <= x1


def test_chambers_sit_off_the_route():
    '''The open chambers are nooks the centipede never crawls through — off the route.'''
    sim = _sim(seed=3)
    route = set(sim.route)
    for ch in sim.chambers:
        for y in range(ch['y0'], ch['y1'] + 1):
            for x in range(ch['x0'], ch['x1'] + 1):
                assert (y, x) not in route


def test_fish_only_in_wide_enough_basins():
    '''Fish appear only where water can pool (a basin) AND the basin is 3-6 blocks wide.'''
    saw_fish = saw_dry = False
    for seed in range(8):
        sim = _sim(seed=seed)
        for ch in sim.chambers:
            if ch['fish']:
                assert ch['basin'] and ch['depth'] >= 1
                assert maze.FISH_MIN_W <= ch['width'] <= maze.FISH_MAX_W
                saw_fish = True
            if not ch['basin']:
                saw_dry = True
    assert saw_fish and saw_dry                            # both basins-with-fish and dry chambers occur


def test_determinism_by_seed():
    a, b = _sim(seed=7), _sim(seed=7)
    assert a.wall == b.wall
    assert a.route == b.route
    assert a.eggs == b.eggs
    assert _sim(seed=8).route != a.route


# -- playback -------------------------------------------------------------

def test_head_advances_with_progress():
    sim = _sim(seed=2)
    sim.set_progress(0.0)
    for _ in range(30):
        sim.step(1 / 30)
    early = sim.head_index()
    sim.set_progress(0.6)
    for _ in range(120):
        sim.step(1 / 30)
    assert sim.head_index() > early


def test_progress_is_monotonic_and_clamped():
    sim = _sim(seed=0)
    sim.set_progress(0.5)
    sim.set_progress(0.2)
    assert sim._p == 0.5                                   # never rewinds
    sim.set_progress(5)
    assert sim._p == 1.0                                   # clamped


def test_reaches_the_eggs_at_full_progress():
    sim = _sim(seed=4)
    sim.set_progress(1.0)
    for _ in range(400):
        sim.step(1 / 30)
        if sim.filled:
            break
    assert sim.filled and sim.arrived
    assert int(sim.head_index()) == len(sim.route) - 1     # head is home at the nest
