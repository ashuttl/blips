"""The pollers' geometry: a wide view's tiles cover it, nearest first, and
a radar frame keeps painting until the view leaves it."""

from blips._camera import bbox_for
from blips._feeds import WeatherFeed, _covers, _padded, sweep_tiles
from blips._geo import haversine_nm


def test_sweep_covers_the_view_centre_first():
    lat, lon = 40.0, -95.0
    bbox = bbox_for(lat, lon, 14.0, 160, 40)
    tiles = sweep_tiles(lat, lon, bbox, cap=64)
    assert tiles[0] == (lat, lon)
    # every point of a coarse grid over the view is within a tile's reach
    for i in range(11):
        for j in range(11):
            p_lat = bbox[1] + (bbox[3] - bbox[1]) * i / 10
            p_lon = bbox[0] + (bbox[2] - bbox[0]) * j / 10
            assert min(haversine_nm(p_lat, p_lon, t[0], t[1])
                       for t in tiles) <= 250, (p_lat, p_lon)


def test_sweep_is_capped():
    bbox = bbox_for(40.0, -95.0, 50.0, 200, 50)
    assert len(sweep_tiles(40.0, -95.0, bbox, cap=16)) == 16


def test_padded_frame_covers_small_moves_and_zoom_steps():
    view = bbox_for(43.6, -70.3, 3.0, 120, 34)
    frame = _padded(view, 0.25)
    fgw = int(120 * 1.5)
    assert _covers(frame, view, 120, fgw)
    nudged = bbox_for(43.9, -70.0, 3.0, 120, 34)
    assert _covers(frame, nudged, 120, fgw)
    zoomed_in = bbox_for(43.6, -70.3, 1.0, 120, 34)
    assert not _covers(frame, zoomed_in, 120, fgw)   # too coarse now
    far = bbox_for(40.6, -73.8, 3.0, 120, 34)
    assert not _covers(frame, far, 120, fgw)


def test_weather_asks_again_only_when_the_frame_cannot_paint():
    wx = WeatherFeed(43.6, -70.3)
    wx.set_view(bbox_for(43.6, -70.3, 3.0, 120, 34), 120, 34)
    first = wx._view_key
    wx.set_view(bbox_for(43.7, -70.2, 3.0, 120, 34), 120, 34)
    assert wx._view_key == first
    wx.set_view(bbox_for(51.5, -0.4, 3.0, 120, 34), 120, 34)
    assert wx._view_key != first
