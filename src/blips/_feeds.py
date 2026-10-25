"""The scope's two background pollers: aircraft and weather radar.

``Feed`` keeps fresh aircraft and their trails for the view.  A view no
wider than the aggregators' 250 nm radius is one request per poll; a wider
one is swept in tiles, a second apart, nearest the centre first, so a
continent fills in over a few seconds instead of showing only its middle.
Aircraft the scope has pinned — the selected flight — are looked up by hex
when they're outside what the view's request returned, so following a
flight never loses it at the edge.

``WeatherFeed`` keeps the newest radar composite for an area a little
larger than the view, so a pan, a zoom step or a followed flight keeps
its weather: the renderer resamples the frame into whatever window is on
screen, and only a view that leaves the frame (or outgrows its
resolution) asks for a new one.

Both run on daemon threads, take the newest view from the renderer, and
wake the live loop (``nudge``) when something lands.
"""

import math
import threading
import time

from blips import _adsb
from blips._adsb import MAX_RADIUS_NM, SourcePicker, _fetch_source, lookup
from blips._geo import haversine_nm
from blips._live import nudge
from blips._radar_sources import get_source
from blips._runtime import debug_log

REFRESH_S = 5.0       # feed poll cadence (aggregators ask ≤1 req/s; we're 5x under)
WEATHER_REFRESH_S = 120.0  # radar composites update ~every 5 min; poll gently
TRAIL_MAX_AGE = 480.0
TRAIL_MAX_FIXES = 120
TILE_GAP_S = 1.1      # between the requests of a wide sweep: ≤1 req/s
MAX_TILES = 16        # a sweep's ceiling; the edges of a continent wait
SWEEP_FROM_NM = 300   # one request up to here, tiles beyond
WX_PAD = 0.25         # radar fetched this far past each edge of the view


def _view_key(bbox, gw, hc):
    return (tuple(round(v, 3) for v in bbox), gw, hc)


def sweep_tiles(lat, lon, bbox, radius_nm=MAX_RADIUS_NM, cap=MAX_TILES):
    """Centres of the point queries that cover ``bbox``, nearest first.

    Circles of ``radius_nm`` on a square grid ~1.4 radii apart leave no
    gaps; the grid is anchored on the view's centre so the first request
    is the middle of the screen.
    """
    step_nm = radius_nm * 1.38
    dlat = step_nm / 60.0
    minlon, minlat, maxlon, maxlat = bbox
    out = []
    ny = int(math.ceil((maxlat - minlat) / 2 / dlat)) + 1
    for iy in range(-ny, ny + 1):
        tlat = lat + iy * dlat
        if tlat < minlat - dlat / 2 or tlat > maxlat + dlat / 2:
            continue
        if abs(tlat) > 85:
            continue
        dlon = dlat / max(0.15, math.cos(math.radians(tlat)))
        nx = int(math.ceil((maxlon - minlon) / 2 / dlon)) + 1
        for ix in range(-nx, nx + 1):
            tlon = lon + ix * dlon
            if tlon < minlon - dlon / 2 or tlon > maxlon + dlon / 2:
                continue
            out.append((haversine_nm(lat, lon, tlat, tlon), tlat,
                        (tlon + 180.0) % 360.0 - 180.0))
    out.sort()
    return [(t[1], t[2]) for t in out[:cap]]


class Feed:
    """Background poller: keeps fresh aircraft + position trails per view."""

    def __init__(self, nudge=False):
        self.aircraft = []
        self.trails = {}     # hex → [(lat, lon, fix_time), ...]
        self.updated = None  # wall time of last successful poll
        self.source = ""
        self.error = None
        self.sweep = None    # (done, total) while a wide view fills in
        self._fetching = False      # a poll is in flight right now
        self._fetch_started = None  # wall time the in-flight poll began
        self._trying = ""           # aggregator currently being waited on
        self._view = None    # (lat, lon, radius_nm)
        self._bbox = None
        self._pins = set()   # hexes to keep however far they stray
        self._picker = SourcePicker()  # sticky best-aggregator-for-here
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._nudge = nudge  # wake the live loop after each poll

    def set_view(self, lat, lon, radius_nm, bbox=None):
        view = (round(lat, 4), round(lon, 4), round(radius_nm))
        self._bbox = bbox
        if view != self._view:
            self._view = view
            self._wake.set()  # poll the new window immediately

    def set_pins(self, hexes):
        """Aircraft to fetch by hex when the view's request misses them."""
        pins = {h for h in hexes if h}
        if pins != self._pins:
            fresh = pins - self._pins
            self._pins = pins
            if fresh and not any(ac["hex"] in fresh for ac in self.aircraft):
                self._wake.set()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="feed").start()

    def poll_once(self):
        lat, lon, radius = self._view

        def _trying(name):
            with self._lock:
                self._trying = name

        with self._lock:
            self._fetching = True
            self._fetch_started = time.time()
        try:
            if radius > SWEEP_FROM_NM and self._bbox is not None:
                aircraft, source = self._sweep(lat, lon, _trying)
            else:
                # a view whose corners are a little past the aggregators'
                # reach asks once and lets the far corners go quiet
                aircraft, source = self._picker.fetch(
                    lat, lon, min(radius, MAX_RADIUS_NM), on_status=_trying)
                with self._lock:
                    self.sweep = None
            aircraft = self._with_pins(aircraft)
        finally:
            with self._lock:
                self._fetching = False
                self._trying = ""
        self._publish(aircraft, source)

    def _publish(self, aircraft, source):
        with self._lock:
            self.aircraft = aircraft
            self.source = source
            self.updated = time.time()
            self.error = None
            self._update_trails(aircraft)

    def _with_pins(self, aircraft):
        """The poll's aircraft plus any pinned ones it didn't include."""
        have = {ac["hex"] for ac in aircraft}
        missing = [h for h in self._pins if h not in have]
        for hexid in missing[:2]:
            try:
                found, _src = lookup("hex", hexid)
            except Exception as exc:
                debug_log(f"pinned {hexid} lookup failed: {exc}")
                continue
            for ac in found:
                if ac["hex"] == hexid:
                    aircraft = aircraft + [ac]
                    break
        return aircraft

    def _sweep(self, lat, lon, on_status):
        """A wide view in tiles: the centre tile votes for the source, the
        rest ask the winner, a polite second apart.  Each tile is published
        as it lands (the previous sweep's aircraft stand in for tiles not
        yet re-asked), so the scope fills in rather than waiting."""
        view = self._view
        tiles = sweep_tiles(lat, lon, self._bbox)
        merged = {ac["hex"]: ac for ac in self.aircraft}
        fresh = {}
        source = self.source
        for i, (tlat, tlon) in enumerate(tiles):
            if self._view != view:
                break               # the view moved: start over there
            if i == 0:
                got, source = self._picker.fetch(tlat, tlon, MAX_RADIUS_NM,
                                                 on_status=on_status)
            else:
                time.sleep(TILE_GAP_S)
                src = next((s for s in _adsb.SOURCES if s[0] == source),
                           None)
                if src is None:
                    break
                on_status(source)
                try:
                    got = _fetch_source(src, tlat, tlon, MAX_RADIUS_NM, 8)
                except Exception as exc:
                    debug_log(f"sweep tile {i} failed: {exc}")
                    continue
            for ac in got:
                fresh[ac["hex"]] = ac
            merged.update(fresh)
            with self._lock:
                self.sweep = (i + 1, len(tiles))
            if i < len(tiles) - 1:
                self._publish(list(merged.values()), source)
                if self._nudge:
                    nudge()
        with self._lock:
            self.sweep = None
        # once the sweep is whole, what it didn't see this time is gone
        return list(fresh.values()) or list(merged.values()), source

    def snapshot(self):
        with self._lock:
            return (list(self.aircraft), dict(self.trails), self.updated,
                    self.source, self.error)

    def fetch_status(self):
        """(fetching, started, trying): is a poll in flight, since when,
        and which aggregator it's waiting on."""
        with self._lock:
            return self._fetching, self._fetch_started, self._trying

    def _update_trails(self, aircraft):
        now = time.time()
        seen = set()
        for ac in aircraft:
            if ac["ground"]:
                continue  # taxiing scribbles aren't worth the ink
            seen.add(ac["hex"])
            trail = self.trails.setdefault(ac["hex"], [])
            if not trail or (ac["fix_time"] > trail[-1][2] + 1.0):
                trail.append((ac["lat"], ac["lon"], ac["fix_time"]))
                del trail[:-TRAIL_MAX_FIXES]
        for hexid in list(self.trails):
            trail = self.trails[hexid]
            while trail and now - trail[0][2] > TRAIL_MAX_AGE:
                trail.pop(0)
            if not trail or (hexid not in seen
                             and now - trail[-1][2] > 120.0):
                del self.trails[hexid]

    def _run(self):
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                with self._lock:
                    self.error = str(exc)
            if self._nudge:
                nudge()  # repaint now that data landed
            self._wake.wait(REFRESH_S)
            self._wake.clear()


def _padded(bbox, pad):
    minlon, minlat, maxlon, maxlat = bbox
    w, h = maxlon - minlon, maxlat - minlat
    return (minlon - w * pad, max(-89.0, minlat - h * pad),
            maxlon + w * pad, min(89.0, maxlat + h * pad))


def _covers(frame, bbox, gw, fgw):
    """Whether a frame fetched for ``frame`` (``fgw`` columns wide) can
    still paint ``bbox`` at ``gw`` columns: the view sits inside it, and
    its pixels aren't much coarser or finer than the screen's cells."""
    fminlon, fminlat, fmaxlon, fmaxlat = frame
    minlon, minlat, maxlon, maxlat = bbox
    if not (fminlon <= minlon and maxlon <= fmaxlon
            and fminlat <= minlat and maxlat <= fmaxlat):
        return False
    view_px = (maxlon - minlon) / max(1, gw)
    frame_px = (fmaxlon - fminlon) / max(1, fgw)
    return 0.6 <= view_px / frame_px <= 1.7


class WeatherFeed:
    """Background poller for the latest weather-radar frame over the view.

    Mirrors ``Feed``: a daemon thread fetches the newest radar composite
    for an area a quarter wider than the view on every side, and the
    renderer resamples it into the window on screen (``echo_for``).  It
    only touches the network while weather is toggled on.
    """

    def __init__(self, lat, lon, theme=None, nudge=False):
        self._home = (lat, lon)
        self._theme = theme
        self._source = None
        self.rgba = None
        self.pw = self.ph = 0
        self.frame_view = None   # _view_key of the area the frame covers
        self.frame_bbox = None
        self.label = ""
        self.attribution = ""
        self.updated = None
        self.error = None
        self._view = None        # (bbox, gw, hc) asked of the source
        self._view_key = None
        self._enabled = False
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._nudge = nudge

    def set_enabled(self, on):
        with self._lock:
            if on == self._enabled:
                return
            self._enabled = on
            if not on:  # drop the frame so a re-enable can't flash stale data
                self.rgba = None
                self.frame_view = self.frame_bbox = None
        self._wake.set()

    def set_view(self, bbox, gw, hc):
        """Aim at the window on screen; a new frame is asked for only when
        the one asked for last can't paint it."""
        with self._lock:
            if self._view is not None and _covers(self._view[0], bbox, gw,
                                                  self._view[1]):
                return
            pbbox = _padded(bbox, WX_PAD)
            pgw = int(round(gw * (1 + 2 * WX_PAD)))
            phc = int(round(hc * (1 + 2 * WX_PAD)))
            key = _view_key(pbbox, pgw, phc)
            if key == self._view_key:
                return
            self._view_key = key
            self._view = (pbbox, pgw, phc)
            wake = self._enabled
        if wake:
            self._wake.set()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="weather").start()

    def poll_once(self):
        with self._lock:
            if not self._enabled or self._view is None:
                return
            bbox, gw, hc = self._view
            key = self._view_key
        if self._source is None:
            self._source = get_source(self._home[0], self._home[1], self._theme)
        result = self._source.latest_rgba(bbox, gw, hc)
        with self._lock:
            if result is None:
                self.rgba = None
                self.frame_view = self.frame_bbox = None
            else:
                self.pw, self.ph, self.rgba = result
                self.frame_view = key
                self.frame_bbox = bbox
            self.label = self._source.label
            self.attribution = self._source.attribution
            self.updated = time.time()
            self.error = None

    def snapshot(self):
        with self._lock:
            return (self.rgba, self.pw, self.ph, self.frame_view,
                    self.label, self.attribution, self.updated, self.error)

    def frame(self):
        """(rgba, pw, ph, bbox) of the frame on hand, or None."""
        with self._lock:
            if self.rgba is None or self.frame_bbox is None:
                return None
            return self.rgba, self.pw, self.ph, self.frame_bbox

    def _run(self):
        while True:
            if self._enabled:
                try:
                    self.poll_once()
                except Exception as exc:
                    with self._lock:
                        self.error = str(exc)
                if self._nudge:
                    nudge()
                self._wake.wait(WEATHER_REFRESH_S)
            else:
                self._wake.wait()  # idle until toggled on
            self._wake.clear()
