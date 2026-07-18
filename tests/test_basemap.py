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
