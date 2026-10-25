"""Basemap geography: the lake carve and marine naming that treat inland
water like the sea. Guards against Natural Earth land (which has no lake
holes) filling the Great Lakes solid."""

import blips._basemap as basemap_mod
from blips._basemap import Basemap, _project, marine_region


class TestLakeCarve:
    """A lake ring wholly inside a land square must read as water, with its
    shoreline stroked in COAST — synthetic data, no vendored file needed."""

    BBOX = (-5.0, -5.0, 5.0, 5.0)
    GRAPH_W = 10
    HEIGHT_CELLS = 5

    def setup_method(self):
        self._original_data = basemap_mod._DATA
        land_ring = [(-2, -2), (2, -2), (2, 2), (-2, 2), (-2, -2)]
        lake_ring = [(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)]
        basemap_mod._DATA = {
            "land": [[land_ring]],
            "lakes": [[lake_ring]],
            "borders": [],
            "cities": [],
        }

    def teardown_method(self):
        basemap_mod._DATA = self._original_data

    def test_lake_interior_is_water(self):
        bm = Basemap(self.BBOX, self.GRAPH_W, self.HEIGHT_CELLS)
        # lon=0,lat=0 is dead centre of both squares -> cell (5, 2)
        col, row = (int(v) for v in _project(0, 0, self.BBOX,
                                             self.GRAPH_W, self.HEIGHT_CELLS))
        assert bm.sea[row][col] is True

    def test_land_around_lake_stays_land(self):
        bm = Basemap(self.BBOX, self.GRAPH_W, self.HEIGHT_CELLS)
        # lon=1.8,lat=0 is inside land but outside the lake
        col, row = (int(v) for v in _project(1.8, 0, self.BBOX,
                                             self.GRAPH_W, self.HEIGHT_CELLS))
        assert bm.sea[row][col] is False


class TestGreatLakesVendored:
    """End-to-end against the real vendored basemap data."""

    def test_great_lakes_are_named_water(self):
        assert marine_region(47.6, -87.5) == "Lake Superior"
        assert marine_region(44.0, -87.0) == "Lake Michigan"
        assert marine_region(42.2, -81.2) == "Lake Erie"

    def test_dry_land_is_not_water(self):
        assert marine_region(38.5, -98.0) is None  # Kansas


def _reference_fill(bm, poly_groups, value, land):
    """The row-by-row scanline the edge-bucket fill replaced: every row
    asks every edge.  Slow, obviously right, and the fill must match it."""
    for rings in poly_groups:
        if not bm._in_view([p for ring in rings for p in ring]):
            continue
        prings = [[_project(lon, lat, bm.bbox, bm.dw, bm.dh)
                   for lon, lat in ring] for ring in rings]
        ys = [p[1] for ring in prings for p in ring]
        for y in range(max(0, int(min(ys))), min(bm.dh - 1, int(max(ys)) + 1) + 1):
            yc = y + 0.5
            xs = []
            for ring in prings:
                for i in range(len(ring) - 1):
                    (ax, ay), (bx, by) = ring[i], ring[i + 1]
                    if (ay <= yc < by) or (by <= yc < ay):
                        xs.append(ax + (yc - ay) / (by - ay) * (bx - ax))
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                for x in range(max(0, int(xs[i] + 0.5)),
                               min(bm.dw, int(xs[i + 1] + 0.5))):
                    land[y][x] = value


class TestFillMatchesScanline:
    """The fast fill is dot-for-dot the plain scanline, on real data."""

    VIEWS = ((40.64, -73.78, 3.0), (51.47, -0.45, 0.6), (35.5, 139.8, 9.0),
             (64.0, -20.0, 1.5), (-41.0, 174.0, 4.0))

    def test_land_mask_matches_reference(self):
        from blips.scope import bbox_for
        data = basemap_mod._load_data()
        for lat, lon, zoom in self.VIEWS:
            bm = Basemap(bbox_for(lat, lon, zoom, 60, 20), 60, 20)
            fast = bm._sea_mask()
            ref = [bytearray(bm.dw) for _ in range(bm.dh)]
            _reference_fill(bm, data["land"], 1, ref)
            _reference_fill(bm, data["lakes"], 0, ref)
            assert fast == ref, (lat, lon, zoom)
