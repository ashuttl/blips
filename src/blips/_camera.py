"""The scope's camera: where it looks, how far, and how it gets there.

The view is a centre and a zoom (degrees of latitude top to bottom, as
everywhere in blips).  Every change of view is a motion with a start and
an end rather than a jump, so the eye keeps its place:

- a zoom step (a key, a wheel notch) eases over a fifth of a second and
  keeps the ground under the pointer where it was, the way a map in a
  browser does;
- an arrow-key pan eases a quarter of the screen across;
- a jump to somewhere else — a search result on another continent — flies
  the van Wijk & Nuij path (ported from linecast's maps): up until both
  ends are in frame, across, and down, at a steady apparent speed.

Nothing here reads the clock except through ``clock``, so a test can step
a motion by hand.  ``screen_geo`` and ``geo_screen`` convert between
terminal cells and the ground for a given window.
"""

import math
import time

MIN_ZOOM = 0.08   # ~5 nm top to bottom: a field and its final
MAX_ZOOM = 60.0   # a hemisphere
STEP_S = 0.2      # a zoom step or keyboard pan
RHO = 1.42        # van Wijk's trade-off between zooming and panning
SPEED = 3.0       # path units per second
FLIGHT_MIN = 0.45
FLIGHT_MAX = 2.6


def ease_in_out(s):
    """Smoothstep: 0 at 0, 1 at 1, and flat at both ends."""
    s = max(0.0, min(1.0, s))
    return s * s * (3.0 - 2.0 * s)


def wrap_lon(lon):
    return (lon + 180.0) % 360.0 - 180.0


def angle_delta(a, b):
    """Shortest signed longitude step from a to b."""
    return (b - a + 180.0) % 360.0 - 180.0


def clamp_lat(lat):
    return max(-80.0, min(80.0, lat))


def bbox_for(lat, lon, zoom, graph_w, height_cells):
    """Geographic window so map sub-cells render ~square on screen."""
    spy_h = height_cells * 2
    half_lat = zoom / 2
    lon_span = zoom * (graph_w / spy_h) / max(0.2, math.cos(math.radians(lat)))
    return (lon - lon_span / 2, lat - half_lat, lon + lon_span / 2,
            lat + half_lat)


def screen_geo(bbox, gw, hc, col, row):
    """Ground (lat, lon) at the middle of 0-based cell (col, row)."""
    minlon, minlat, maxlon, maxlat = bbox
    lon = minlon + (col + 0.5) / gw * (maxlon - minlon)
    lat = maxlat - (row + 0.5) / hc * (maxlat - minlat)
    return lat, lon


def geo_screen(bbox, gw, hc, lat, lon):
    """0-based (col, row) of the cell over (lat, lon) — may be off screen."""
    minlon, minlat, maxlon, maxlat = bbox
    mid = (minlon + maxlon) / 2
    lon = mid + angle_delta(mid, lon)   # the copy of lon nearest the view
    col = int((lon - minlon) / (maxlon - minlon) * gw)
    row = int((maxlat - lat) / (maxlat - minlat) * hc)
    return col, row


def anchored_centre(anchor_lat, anchor_lon, fx, fy, zoom, gw, hc):
    """The centre that puts (anchor_lat, anchor_lon) at fraction (fx, fy)
    of the window at this zoom — a zoom about the pointer.  The window's
    width depends on its centre latitude, so the latitude is settled
    first, then the longitude."""
    lat = clamp_lat(anchor_lat + (fy - 0.5) * zoom)
    minlon, _s, maxlon, _n = bbox_for(lat, 0.0, zoom, gw, hc)
    lon = wrap_lon(anchor_lon - (fx - 0.5) * (maxlon - minlon))
    return lat, lon


class Flight:
    """A smooth flight from one view to another (van Wijk & Nuij 2003):
    the camera rises just high enough to see both ends, crosses, and
    descends, along the path on which the picture appears to move at a
    constant speed.  ``w`` is the zoom, ``u`` the ground distance in the
    same degrees."""

    def __init__(self, lat0, lon0, w0, lat1, lon1, w1, speed=SPEED):
        self.lat0, self.lon0, self.w0 = lat0, lon0, w0
        self.lat1, self.lon1, self.w1 = lat1, lon1, w1
        dlat = lat1 - lat0
        dlon = angle_delta(lon0, lon1) * math.cos(math.radians((lat0 + lat1) / 2))
        self.u1 = math.hypot(dlat, dlon)
        rho = RHO
        if self.u1 < 1e-6:
            self.k = 1.0 if w1 > w0 else -1.0
            self.r0 = None
            self.S = abs(math.log(w1 / w0)) / rho if w1 != w0 else 0.0
        else:
            u1 = self.u1
            b0 = (w1 * w1 - w0 * w0 + rho ** 4 * u1 * u1) / (2 * w0 * rho * rho * u1)
            b1 = (w1 * w1 - w0 * w0 - rho ** 4 * u1 * u1) / (2 * w1 * rho * rho * u1)
            r0 = -math.asinh(b0)
            r1 = -math.asinh(b1)
            self.r0 = r0
            self.k = None
            self.S = (r1 - r0) / rho
        self.duration = max(FLIGHT_MIN, min(FLIGHT_MAX, self.S / speed))

    def at(self, t):
        """(lat, lon, zoom) ``t`` seconds in; exactly the end past it."""
        if t >= self.duration:
            return self.lat1, self.lon1, self.w1
        s = ease_in_out(max(0.0, t / self.duration)) * self.S
        rho = RHO
        if self.r0 is None:
            frac = s / self.S if self.S else 1.0
            w = self.w0 * math.exp(self.k * rho * s)
        else:
            r0 = self.r0
            u = (self.w0 / (rho * rho) * math.sinh(rho * s)
                 / math.cosh(rho * s + r0))
            w = self.w0 * math.cosh(r0) / math.cosh(rho * s + r0)
            frac = max(0.0, min(1.0, u / self.u1))
        lat = self.lat0 + (self.lat1 - self.lat0) * frac
        lon = self.lon0 + angle_delta(self.lon0, self.lon1) * frac
        return lat, wrap_lon(lon), w


class Camera:
    """The view and whatever motion it is part-way through.

    ``lat``, ``lon`` and ``zoom`` are where it looks this instant; call
    ``step()`` once per frame to advance a motion, and ``moving`` says
    whether one is under way (the app paints faster meanwhile).
    """

    def __init__(self, lat, lon, zoom, clock=time.monotonic):
        self.lat, self.lon = clamp_lat(lat), wrap_lon(lon)
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self.clock = clock
        self._zoom = None    # (from, to, anchor, started) — anchor (lat, lon, fx, fy)
        self._pan = None     # (lat0, lon0, lat1, lon1, started)
        self._flight = None  # (Flight, started)

    @property
    def moving(self):
        return bool(self._zoom or self._pan or self._flight)

    @property
    def target(self):
        """(lat, lon, zoom) the view will settle on."""
        lat, lon, zoom = self.lat, self.lon, self.zoom
        if self._flight:
            f = self._flight[0]
            return f.lat1, f.lon1, f.w1
        if self._pan:
            lat, lon = self._pan[2], self._pan[3]
        if self._zoom:
            zoom = self._zoom[1]
        return lat, lon, zoom

    def stop(self):
        self._zoom = self._pan = self._flight = None

    def jump(self, lat, lon, zoom=None):
        """Be there now, no motion (a drag commit, a follow step)."""
        self.stop()
        self.lat, self.lon = clamp_lat(lat), wrap_lon(lon)
        if zoom is not None:
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))

    def zoom_by(self, factor, at=None, gw=None, hc=None):
        """Ease the zoom by ``factor`` (<1 zooms in).  ``at`` is a 0-based
        (col, row) the ground under which should stay put; repeated calls
        mid-motion compound on the target, so a fast wheel spin zooms far
        in one smooth move."""
        _lat, _lon, target = self.target
        new = max(MIN_ZOOM, min(MAX_ZOOM, target * factor))
        if abs(new - target) < 1e-9:
            return False
        anchor = None
        if at is not None and gw and hc:
            if self._zoom and self._zoom[2] is not None and \
                    self._zoom[2][2:] == ((at[0] + 0.5) / gw, (at[1] + 0.5) / hc):
                anchor = self._zoom[2]       # same pointer: same ground
            else:
                bbox = bbox_for(self.lat, self.lon, self.zoom, gw, hc)
                alat, alon = screen_geo(bbox, gw, hc, at[0], at[1])
                anchor = (alat, alon, (at[0] + 0.5) / gw, (at[1] + 0.5) / hc)
        self._flight = None
        self._zoom = (self.zoom, new, anchor, self.clock(), gw, hc)
        return True

    def pan_by(self, fx, fy, gw, hc):
        """Ease the view by a fraction of the window (fx right, fy down)."""
        lat, lon, zoom = self.target
        bbox = bbox_for(lat, lon, zoom, gw, hc)
        lat1 = clamp_lat(lat - fy * zoom)
        lon1 = wrap_lon(lon + fx * (bbox[2] - bbox[0]))
        self._flight = None
        if self._zoom and self._zoom[2] is not None:
            # a pan supersedes the zoom's anchor: finish the zoom in place
            f, t, _a, s, g, h = self._zoom
            self._zoom = (f, t, None, s, g, h)
        self._pan = (self.lat, self.lon, lat1, lon1, self.clock())
        return True

    def fly_to(self, lat, lon, zoom=None):
        """The long way: up, across, and down to (lat, lon, zoom)."""
        zoom = self.zoom if zoom is None else max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self._zoom = self._pan = None
        self._flight = (Flight(self.lat, self.lon, self.zoom,
                               clamp_lat(lat), wrap_lon(lon), zoom),
                        self.clock())

    def step(self):
        """Advance whatever motion is under way to now; True while moving."""
        now = self.clock()
        if self._flight:
            flight, started = self._flight
            t = now - started
            self.lat, self.lon, self.zoom = flight.at(t)
            if t >= flight.duration:
                self._flight = None
            return self.moving
        if self._zoom:
            z0, z1, anchor, started, gw, hc = self._zoom
            s = ease_in_out((now - started) / STEP_S)
            # geometric, so each notch of a spin feels the same size
            self.zoom = z0 * (z1 / z0) ** s
            if anchor is not None:
                self.lat, self.lon = anchored_centre(
                    anchor[0], anchor[1], anchor[2], anchor[3],
                    self.zoom, gw, hc)
            if s >= 1.0:
                self.zoom = z1
                self._zoom = None
        if self._pan:
            lat0, lon0, lat1, lon1, started = self._pan
            s = ease_in_out((now - started) / STEP_S)
            self.lat = lat0 + (lat1 - lat0) * s
            self.lon = wrap_lon(lon0 + angle_delta(lon0, lon1) * s)
            if s >= 1.0:
                self._pan = None
        return self.moving
