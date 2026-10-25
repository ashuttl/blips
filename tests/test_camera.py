"""The camera: zooms keep the ground under the pointer, long jumps fly,
and every motion ends exactly where it was asked to."""

from blips._camera import (
    Camera, Flight, anchored_centre, bbox_for, geo_screen, screen_geo,
)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_anchored_zoom_keeps_the_pointer_on_its_ground():
    gw, hc = 120, 34
    clock = Clock()
    cam = Camera(43.6, -70.3, 3.0, clock=clock)
    bbox = bbox_for(cam.lat, cam.lon, cam.zoom, gw, hc)
    col, row = 30, 8
    lat0, lon0 = screen_geo(bbox, gw, hc, col, row)
    cam.zoom_by(1 / 1.5, at=(col, row), gw=gw, hc=hc)
    for t in (0.05, 0.1, 0.15, 0.3):
        clock.t = t
        cam.step()
        bbox = bbox_for(cam.lat, cam.lon, cam.zoom, gw, hc)
        assert geo_screen(bbox, gw, hc, lat0, lon0) == (col, row)
    assert abs(cam.zoom - 2.0) < 1e-9 and not cam.moving


def test_wheel_spin_compounds_on_the_target():
    clock = Clock()
    cam = Camera(0, 0, 4.0, clock=clock)
    for _ in range(3):
        cam.zoom_by(0.5)
    assert abs(cam.target[2] - 0.5) < 1e-9
    clock.t = 1.0
    cam.step()
    assert abs(cam.zoom - 0.5) < 1e-9


def test_flight_lands_exactly_on_its_target():
    f = Flight(43.6, -70.3, 3.0, 29.2, 47.9, 2.0)
    assert f.at(0.0)[:2] == (43.6, -70.3)
    assert f.at(f.duration + 1) == (29.2, 47.9, 2.0)
    # it climbs on the way: somewhere mid-flight the view is wider than
    # either end, so both ends are in frame
    assert max(f.at(f.duration * k / 10)[2] for k in range(11)) > 3.0


def test_pan_moves_a_quarter_screen():
    clock = Clock()
    cam = Camera(40.0, -74.0, 2.0, clock=clock)
    cam.pan_by(0, -0.25, 100, 30)        # up
    clock.t = 1.0
    cam.step()
    assert abs(cam.lat - 40.5) < 1e-9


def test_geo_screen_across_the_antimeridian():
    bbox = bbox_for(0.0, 179.0, 4.0, 80, 20)    # runs past +180
    col, _row = geo_screen(bbox, 80, 20, 0.0, -179.5)
    assert 0 <= col < 80


def test_anchored_centre_round_trip():
    lat, lon = anchored_centre(10.0, 20.0, 0.5, 0.5, 2.0, 80, 20)
    assert abs(lat - 10.0) < 1e-9 and abs(lon - 20.0) < 1e-9
