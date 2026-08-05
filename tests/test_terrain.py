"""The MVA grid from synthetic elevation: no network, no real mountains.

fetch_json and the cache are faked throughout, so these tests exercise
the pure core — _install's metres→MVA arithmetic, the two lookups, and
the degradation paths (offline stays flat, a bad fetch leaves the grid
absent rather than corrupt).
"""

import pytest

import blips._terrain as _terrain
from blips._terrain import BUFFER_FT, GRID_N, Terrain


def _terrain_at_tampa():
    return Terrain(27.98, -82.53)


def _flat(elev=0.0):
    return [elev] * (GRID_N * GRID_N)


def _cell_centre(t, row, col):
    """(lat, lon) of a grid cell's sample point, straight from _points."""
    lats, lons = t._points()
    i = row * GRID_N + col
    return lats[i], lons[i]


def test_offline_world_stays_flat():
    # before any fetch lands, both lookups answer None everywhere
    t = _terrain_at_tampa()
    assert t.status == "loading"
    assert t.mva_at(27.98, -82.53) is None
    assert t.mva_smooth(27.98, -82.53) is None


def test_install_derives_mva_from_the_grid():
    t = _terrain_at_tampa()
    elev = _flat(-5.0)                # shallow gulf: clamped to sea level
    elev[7 * GRID_N + 11] = 1000.0    # one synthetic peak
    assert t._install(elev)
    # sea level → just the buffer; 1000 m → 3280.84 ft + 2000, up to 5300
    assert t.mva_at(*_cell_centre(t, 0, 0)) == BUFFER_FT
    assert t.mva_at(*_cell_centre(t, 7, 11)) == 5300.0
    assert t.max_mva == 5300.0


def test_off_grid_is_the_next_facilitys_problem():
    t = _terrain_at_tampa()
    assert t._install(_flat())
    assert t.mva_at(t._north + 1.0, -82.53) is None
    assert t.mva_smooth(t._north + 1.0, -82.53) is None


def test_smooth_interpolates_but_matches_at_cell_centres():
    t = _terrain_at_tampa()
    elev = _flat()
    elev[5 * GRID_N + 5] = 1000.0
    assert t._install(elev)
    lat, lon = _cell_centre(t, 5, 5)
    assert t.mva_at(lat, lon) == 5300.0
    assert t.mva_smooth(lat, lon) == pytest.approx(5300.0)
    # halfway to the flat neighbour, the tint is the average of the two
    _, lon6 = _cell_centre(t, 5, 6)
    mid = t.mva_smooth(lat, (lon + lon6) / 2.0)
    assert mid == pytest.approx((5300.0 + BUFFER_FT) / 2.0)


def test_a_short_list_leaves_the_grid_absent_not_corrupt():
    t = _terrain_at_tampa()
    assert not t._install([0.0] * 10)
    assert t.mva_at(27.98, -82.53) is None


def test_cache_hit_needs_no_network(monkeypatch):
    elev = _flat()
    elev[0] = 500.0
    monkeypatch.setattr(_terrain, "read_stale", lambda path: {"elev": elev})
    monkeypatch.setattr(_terrain, "fetch_json",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("hit the network on a cache hit")))
    t = _terrain_at_tampa()
    t._load()
    assert t.status == "ready"
    # 500 m → 1640.42 ft + 2000, up to 3700
    assert t.mva_at(*_cell_centre(t, 0, 0)) == 3700.0


def test_failed_fetch_marks_failed_and_keeps_the_world_flat(monkeypatch):
    monkeypatch.setattr(_terrain, "read_stale", lambda path: None)
    monkeypatch.setattr(_terrain, "fetch_json",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            OSError("no route to host")))
    monkeypatch.setattr(_terrain.time, "sleep", lambda s: None)
    t = _terrain_at_tampa()
    t._load()
    assert t.status == "failed"
    assert t.mva_at(27.98, -82.53) is None


def test_a_short_response_is_retried_not_installed(monkeypatch):
    # the API answering with fewer points than asked must never publish
    monkeypatch.setattr(_terrain, "read_stale", lambda path: None)
    monkeypatch.setattr(_terrain, "fetch_json",
                        lambda *a, **kw: {"elevation": [12.0]})
    monkeypatch.setattr(_terrain.time, "sleep", lambda s: None)
    t = _terrain_at_tampa()
    t._load()
    assert t.status == "failed"
    assert t.mva_at(27.98, -82.53) is None
