#!/usr/bin/env python3
"""blips — live air traffic on a braille basemap.

An ambient terminal scope: geography (a block-colour sea, with coastlines and
borders in braille) is drawn first, and live aircraft from community ADS-B
aggregators are painted over it — a directional blip with an ATC-style data block
(callsign + flight level), a velocity leader showing the next minute of
travel, and a fading braille trail of recent positions.  Between feed polls
the blips glide on dead reckoning, so the scope moves the way the sky does.

Layer priority per terminal cell, top to bottom:

  1. aircraft blip           → arrow glyph in altitude colour
  2. data block / city label → text glyph
  3. leaders, trails, rings  → braille strokes
  4. coast / border          → braille dots
  5. sea / bare land         → block-colour background

Usage: blips [--location LAT,LNG | PLACE] [--zoom DEG] [--print]
"""

import math
import os
import signal
import sys
import threading
import time

from blips._adsb import SourcePicker
from blips._commands import airline_name
from blips._routes import RouteLookup
from blips._basemap import (
    Basemap, DotLayer, SEA_FILL, marine_region, nearest_city, _project,
)
from blips._color import BG_PRIMARY, BOLD, RESET, bg, fg, interp_stops, lerp
from blips._framebuffer import get_terminal_size, visible_len
from blips._geo import advance, bearing_to, haversine_nm
from blips._live import live_loop
from blips._location import geocode_place, get_location
from blips._radar_sources import get_source
from blips._runtime import blips_parser, resolve_live
from blips._theme import darken, ensure_contrast, is_light_theme, surface_bg

MUTED = (150, 155, 170)
DIM = (110, 114, 130)
MARKER = (255, 240, 120)
GROUND = (125, 129, 145)
RING = (62, 76, 108)
ALERT = (235, 80, 70)
# hover chip: the floating readout beside the pointer (same recipe as the
# linecast radar's warning tooltip, so the two scopes feel like siblings)
CHIP_BG = darken(surface_bg(0.10), 0.45 if not is_light_theme() else 0.10)

# altitude (ft) → blip colour, low-and-warm to high-and-cool
ALT_STOPS = (
    (0, (240, 190, 80)),
    (10000, (140, 210, 110)),
    (20000, (80, 185, 220)),
    (30000, (115, 140, 240)),
    (45000, (190, 120, 240)),
)

REFRESH_S = 5.0       # feed poll cadence (aggregators ask ≤1 req/s; we're 5x under)
WEATHER_REFRESH_S = 120.0  # radar composites update ~every 5 min; poll gently
WX_DIM = 0.6          # weather is an ambient underlay; keep traffic readable on top
MAX_EXTRAP_S = 20.0   # cap dead reckoning so a stale fix doesn't fly away
TRAIL_MAX_AGE = 480.0
TRAIL_MAX_FIXES = 120
ARROWS = "↑↗→↘↓↙←↖"
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # header pulse while the first fetch is in flight

_basemap_cache = {}


def bbox_for(lat, lon, zoom, graph_w, height_cells):
    """Geographic window so map sub-cells render ~square on screen.

    `zoom` is the degrees of latitude shown top-to-bottom.
    """
    spy_h = height_cells * 2
    half_lat = zoom / 2
    lon_span = zoom * (graph_w / spy_h) / max(0.2, math.cos(math.radians(lat)))
    return (lon - lon_span / 2, lat - half_lat, lon + lon_span / 2,
            lat + half_lat)


def view_radius_nm(bbox, lat, lon):
    """Feed radius covering the visible window (plus margin for trails)."""
    corner = haversine_nm(lat, lon, bbox[3], bbox[0])
    return max(10, min(250, corner * 1.15))


def _get_basemap(bbox, graph_w, height_cells):
    key = (tuple(round(v, 3) for v in bbox), graph_w, height_cells)
    bm = _basemap_cache.get(key)
    if bm is None:
        bm = Basemap(bbox, graph_w, height_cells)
        _basemap_cache.clear()  # only need the current view
        _basemap_cache[key] = bm
    return bm


def extrapolate(ac, now):
    """Dead-reckoned (lat, lon) — the blip glides between feed polls."""
    dt = now - ac["fix_time"]
    if (ac["ground"] or not ac["gs"] or ac["track"] is None or dt <= 0):
        return ac["lat"], ac["lon"]
    return advance(ac["lat"], ac["lon"], ac["track"],
                   ac["gs"] * min(dt, MAX_EXTRAP_S) / 3600.0)


def blip_color(ac):
    if ac["emergency"] or ac["squawk"] in ("7500", "7600", "7700"):
        return ALERT
    # conflict alert (game): blink before the loss, solid only after
    if ac.get("ca") and int(time.time() * 2) % 2:
        return ALERT
    if ac.get("dim"):
        return DIM      # somebody else's traffic — centre's, not yours
    if ac["ground"]:
        return GROUND
    if ac["alt"] is None:
        return MUTED
    return interp_stops(list(ALT_STOPS), ac["alt"])


def blip_glyph(ac):
    if ac.get("glyph"):
        return ac["glyph"]
    if ac["ground"]:
        return "·"
    if ac["track"] is None:
        return "•"
    return ARROWS[round(ac["track"] / 45.0) % 8]


def trend_arrow(ac):
    vr = ac["vrate"] or 0
    return "↑" if vr > 400 else "↓" if vr < -400 else ""


def data_block(ac):
    """ATC-style label: callsign + flight level (hundreds of feet) + trend.

    Game aircraft carry an assigned altitude; while they're off it the
    block shows both, STARS-interim style — 110↓080 is eleven thousand
    descending to eight — because on a busy scope the assignment you
    can't remember is the one that bites.
    """
    if ac["ground"]:
        return ac["callsign"]
    if ac["alt"] is None:
        return ac["callsign"]
    if ac.get("limited"):
        # an uncorrelated 1200 target the way STARS shows one: altitude
        # readout only — nobody's tagged them up, because they're nobody's.
        # A neighbouring field's traffic wears that field's code, so its
        # stream reads as bound somewhere else, not as loose VFR.
        alt = f"{round(ac['alt'] / 100):03d}"
        tag = ac.get("tag")
        return f"{alt} {tag}" if tag else alt
    tgt = ac.get("tgt_alt")
    if tgt is not None and abs(tgt - ac["alt"]) > 300.0:
        arrow = "↑" if tgt > ac["alt"] else "↓"
        block = (f"{ac['callsign']} {round(ac['alt'] / 100):03d}"
                 f"{arrow}{round(tgt / 100):03d}")
    else:
        block = (f"{ac['callsign']} {round(ac['alt'] / 100):03d}"
                 f"{trend_arrow(ac)}")
    # the scratchpad: a satellite-field arrival wears its destination, and
    # one flying a named procedure wears the procedure — the way a real
    # STARS track does, so what a plane is doing never sneaks up on you
    tag = ac.get("via_name") or ac.get("tag")
    return f"{block} {tag}" if tag else block


class Feed:
    """Background poller: keeps fresh aircraft + position trails per view."""

    def __init__(self, nudge=False):
        self.aircraft = []
        self.trails = {}     # hex → [(lat, lon, fix_time), ...]
        self.updated = None  # wall time of last successful poll
        self.source = ""
        self.error = None
        self._fetching = False      # a poll is in flight right now
        self._fetch_started = None  # wall time the in-flight poll began
        self._trying = ""           # aggregator currently being waited on
        self._view = None    # (lat, lon, radius_nm)
        self._picker = SourcePicker()  # sticky best-aggregator-for-here
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._nudge = nudge  # SIGWINCH-poke the live loop after each poll

    def set_view(self, lat, lon, radius_nm):
        view = (round(lat, 4), round(lon, 4), round(radius_nm))
        if view != self._view:
            self._view = view
            self._wake.set()  # poll the new window immediately

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def poll_once(self):
        lat, lon, radius = self._view

        def _trying(name):
            with self._lock:
                self._trying = name

        with self._lock:
            self._fetching = True
            self._fetch_started = time.time()
        try:
            aircraft, source = self._picker.fetch(lat, lon, radius,
                                                  on_status=_trying)
        finally:
            with self._lock:
                self._fetching = False
                self._trying = ""
        with self._lock:
            self.aircraft = aircraft
            self.source = source
            self.updated = time.time()
            self.error = None
            self._update_trails(aircraft)

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
                # repaint now that data landed (rides the live loop's
                # existing SIGWINCH self-pipe wakeup; harmless if coalesced)
                try:
                    os.kill(os.getpid(), signal.SIGWINCH)
                except Exception:
                    pass
            self._wake.wait(REFRESH_S)
            self._wake.clear()


def _view_key(bbox, gw, hc):
    return (tuple(round(v, 3) for v in bbox), gw, hc)


class WeatherFeed:
    """Background poller for the latest weather-radar frame over the view.

    Mirrors ``Feed``: a daemon thread fetches the newest radar composite for
    the currently-rendered window and the renderer picks it up on the next
    repaint.  It only touches the network while weather is toggled on, and a
    frame is only rendered when it was fetched for the exact view on screen
    (radar is a raster tied to a bbox, so a stale frame from a different pan/
    zoom would be misaligned — better to show nothing until the new one lands).
    """

    def __init__(self, lat, lon, theme=None, nudge=False):
        self._home = (lat, lon)
        self._theme = theme
        self._source = None
        self.rgba = None
        self.pw = self.ph = 0
        self.frame_view = None   # _view_key the current frame was fetched for
        self.label = ""
        self.attribution = ""
        self.updated = None
        self.error = None
        self._view = None        # (bbox, gw, hc) requested by the renderer
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
                self.frame_view = None
        self._wake.set()

    def set_view(self, bbox, gw, hc):
        key = _view_key(bbox, gw, hc)
        with self._lock:
            if key == self._view_key:
                return
            self._view_key = key
            self._view = (bbox, gw, hc)
            wake = self._enabled
        if wake:
            self._wake.set()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

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
                self.frame_view = None
            else:
                self.pw, self.ph, self.rgba = result
                self.frame_view = key
            self.label = self._source.label
            self.attribution = self._source.attribution
            self.updated = time.time()
            self.error = None

    def snapshot(self):
        with self._lock:
            return (self.rgba, self.pw, self.ph, self.frame_view,
                    self.label, self.attribution, self.updated, self.error)

    def _run(self):
        while True:
            if self._enabled:
                try:
                    self.poll_once()
                except Exception as exc:
                    with self._lock:
                        self.error = str(exc)
                if self._nudge:
                    try:
                        os.kill(os.getpid(), signal.SIGWINCH)
                    except Exception:
                        pass
                self._wake.wait(WEATHER_REFRESH_S)
            else:
                self._wake.wait()  # idle until toggled on
            self._wake.clear()


_place_cache = {}


def _place(lat, lon):
    """Friendly name for the scope centre, from the offline basemap data."""
    key = (round(lat, 3), round(lon, 3))
    hit = _place_cache.get(key)
    if hit is not None:
        return hit

    def phrase(name, km, bearing):
        mi = km * 0.621371
        if mi < 3:
            return name
        return f"{mi:.0f} mi {COMPASS[round(bearing / 45) % 8]} of {name}"

    city = nearest_city(lat, lon)
    if city and city[1] < 160:
        place = phrase(*city)
    else:
        water = marine_region(lat, lon)
        if water:
            place = water
        elif city and city[1] <= 1000:
            place = phrase(*city)
        else:
            place = f"{lat:.2f}, {lon:.2f}"

    if len(_place_cache) > 64:
        _place_cache.clear()
    _place_cache[key] = place
    return place


def _ring_spacing_nm(zoom):
    half_span = zoom * 30  # nm from centre to the top edge
    for spacing in (5, 10, 25, 50, 100, 200):
        if half_span / spacing <= 3.5:
            return spacing
    return 400


def _draw_rings(fx, home_lat, home_lon, zoom):
    """Dotted range rings around the scope centre, ATC-scope style."""
    spacing = _ring_spacing_nm(zoom)
    for k in range(1, 8):
        r_nm = spacing * k
        if r_nm > zoom * 45:  # comfortably past every screen corner
            break
        steps = max(120, int(r_nm / zoom * 40))
        for i in range(steps):
            if i % 3:
                continue  # dotted, not solid
            theta = 360.0 * i / steps
            plat, plon = advance(home_lat, home_lon, theta, r_nm)
            x, y = _project(plon, plat, fx.bbox, fx.dw, fx.dh)
            fx._set_dot(int(x), int(y), RING)


def _fade_bg(basemap, dx, dy):
    """Background a braille dot at dot-coords (dx, dy) will sit on.

    Trails and leaders fade toward whatever they're drawn over, so an old
    trail dissolves into the sea fill instead of leaving a dark speckle.
    """
    col, row = dx // 2, dy // 4
    if (0 <= row < basemap.height_cells and 0 <= col < basemap.graph_w
            and basemap.sea[row][col]):
        return SEA_FILL
    return BG_PRIMARY


def _draw_trail(fx, basemap, trail, color, now):
    for tlat, tlon, tt in trail:
        age = now - tt
        if age > TRAIL_MAX_AGE:
            continue
        weight = max(0.15, 0.55 * (1.0 - age / TRAIL_MAX_AGE))
        x, y = _project(tlon, tlat, fx.bbox, fx.dw, fx.dh)
        dx, dy = int(x), int(y)
        fx._set_dot(dx, dy, lerp(_fade_bg(basemap, dx, dy), color, weight))


def _draw_leader(fx, basemap, ac, lat, lon, color):
    """Velocity leader: where the aircraft will be in one minute."""
    if ac["ground"] or not ac["gs"] or ac["track"] is None:
        return
    tip_lat, tip_lon = advance(lat, lon, ac["track"], ac["gs"] / 60.0)
    x0, y0 = _project(lon, lat, fx.bbox, fx.dw, fx.dh)
    x1, y1 = _project(tip_lon, tip_lat, fx.bbox, fx.dw, fx.dh)
    fx._dot_line(x0, y0, x1, y1,
                 lerp(_fade_bg(basemap, int(x0), int(y0)), color, 0.55))


def _cpa_nm(a, alat, alon, b, blat, blon):
    """Predicted closest separation (nm) for two aircraft holding their
    current tracks — the straight-line miss distance from here on.  None
    when a target has no vector to project.  Positions come in already
    dead-reckoned so the number matches the line drawn between the blips.
    """
    if a["track"] is None or b["track"] is None or not a["gs"] or not b["gs"]:
        return None
    latm = math.radians((alat + blat) / 2)
    rx = (alon - blon) * 60.0 * math.cos(latm)   # a relative to b, in nm
    ry = (alat - blat) * 60.0
    ta, tb = math.radians(a["track"]), math.radians(b["track"])
    vx = a["gs"] * math.sin(ta) - b["gs"] * math.sin(tb)   # closure, kt
    vy = a["gs"] * math.cos(ta) - b["gs"] * math.cos(tb)
    vv = vx * vx + vy * vy
    if vv < 1e-6:
        return math.hypot(rx, ry)                # parallel: the gap holds
    t = max(0.0, -(rx * vx + ry * vy) / vv)      # hours until closest
    return math.hypot(rx + vx * t, ry + vy * t)


def _build_echo(rgba, pw, ph, graph_w, height_cells):
    """Reduce a sub-pixel radar frame to a per-cell (r, g, b, weight) grid.

    Each cell spans two sub-pixel rows; their echo colours are alpha-blended
    and the coverage is scaled by WX_DIM so the weather reads as a dim underlay
    the traffic sits on top of.  Cells with no echo stay None.
    """
    echo = [[None] * graph_w for _ in range(height_cells)]
    cols = min(pw, graph_w)
    for cy in range(height_cells):
        y0 = cy * 2
        if y0 >= ph:
            break
        r0 = y0 * pw
        has_bot = (y0 + 1) < ph
        r1 = (y0 + 1) * pw if has_bot else r0
        erow = echo[cy]
        for cx in range(cols):
            i0 = (r0 + cx) * 4
            a0 = rgba[i0 + 3]
            if has_bot:
                i1 = (r1 + cx) * 4
                a1 = rgba[i1 + 3]
            else:
                i1, a1 = i0, 0
            tw = a0 + a1
            if not tw:
                continue
            r = (rgba[i0] * a0 + rgba[i1] * a1) // tw
            g = (rgba[i0 + 1] * a0 + rgba[i1 + 1] * a1) // tw
            b = (rgba[i0 + 2] * a0 + rgba[i1 + 2] * a1) // tw
            erow[cx] = (r, g, b, (max(a0, a1) / 255.0) * WX_DIM)
    return echo


def compose(basemap, fx, overlays, graph_w, height_cells, echo=None):
    """Composite geography + weather + braille fx + text overlays into a grid.

    The sea, land and (when present) weather echo all resolve to a per-cell
    background colour; braille strokes and text glyphs are drawn on top of it.
    Returns a per-cell grid (one self-contained ANSI snippet per cell) so a
    live drag can shift the frame whole; ``_render_lines`` turns it into lines.
    """
    base_bg = bg(*BG_PRIMARY)
    sea_bg = bg(*SEA_FILL)
    sea = basemap.sea
    grid = []
    for cy in range(height_cells):
        srow = sea[cy]
        erow = echo[cy] if echo is not None else None
        row = []
        for cx in range(graph_w):
            e = erow[cx] if erow is not None else None
            if e is not None:
                base_rgb = SEA_FILL if srow[cx] else BG_PRIMARY
                cell_bg = bg(*lerp(base_rgb, (e[0], e[1], e[2]), e[3]))
            else:
                cell_bg = sea_bg if srow[cx] else base_bg
            ov = overlays.get((cx, cy))
            if ov is not None:
                ch, color = ov
                if ch == "":
                    row.append("")  # trailing column of a double-width glyph
                    continue
                row.append(f"{cell_bg}{fg(*color)}{ch}")
                continue
            mask = fx.dots[cy][cx]
            layer = fx if mask else basemap
            if not mask:
                mask = basemap.dots[cy][cx]
            if mask:
                color = layer.color[cy][cx] or DIM
                row.append(f"{cell_bg}{fg(*color)}{chr(0x2800 + mask)}")
            else:
                row.append(f"{cell_bg} ")
        grid.append(row)
    return grid


def _bg_prefix(cell):
    """The leading background-colour SGR of a composed cell (up to its 'm').

    Composed cells begin with ``\\033[48;2;r;g;bm``, so a reticle glyph can be
    stamped onto a cell while keeping whatever background it was sitting on.
    """
    i = cell.find("m")
    return cell[:i + 1] if i != -1 else ""


def _render_lines(grid, reticle=None):
    """Flatten a per-cell grid into ANSI lines, stamping the reticle on top.

    ``reticle`` is {(cx, cy): (char, color)} for the range rings and crosshair.
    Each glyph is drawn over its cell's existing background, so the reticle
    stays fixed at the view centre while the map (the grid) shifts beneath it
    during a live drag — panning slews the scope head, it doesn't drag it off.
    """
    lines = []
    for cy, row in enumerate(grid):
        parts = []
        for cx, cell in enumerate(row):
            rc = reticle.get((cx, cy)) if reticle else None
            if rc is not None:
                ch, color = rc
                parts.append(f"{_bg_prefix(cell)}{fg(*color)}{ch}")
            else:
                parts.append(cell)
        parts.append(RESET)
        lines.append("".join(parts))
    return lines


def _shift_grid(grid, dcol, drow, blank):
    """The grid moved dcol cells right and drow cells down for a drag preview.

    Cells dragged in from beyond the old frame are unknown, so they're filled
    with ``blank`` (a neutral "not drawn yet" background) rather than guessed.
    """
    h = len(grid)
    w = len(grid[0]) if h else 0
    out = []
    for y in range(h):
        sy = y - drow
        if 0 <= sy < h:
            srow = grid[sy]
            out.append([srow[x - dcol] if 0 <= x - dcol < w else blank
                        for x in range(w)])
        else:
            out.append([blank] * w)
    return out


def _clip_ansi(s, width):
    """Truncate a one-line ANSI string to ``width`` visible columns.

    Walks the string keeping escape sequences intact; anything cut loses a
    column to an ellipsis so the clip reads as deliberate.
    """
    if visible_len(s) <= width:
        return s
    out, seen = [], 0
    i = 0
    while i < len(s) and seen < width - 1:
        if s[i] == "\033":
            m = i + 1
            while m < len(s) and s[m] not in "m\\":
                m += 1
            out.append(s[i:m + 1])
            i = m + 1
            continue
        out.append(s[i])
        seen += 1
        i += 1
    return "".join(out) + f"{RESET}{fg(*DIM)}…{RESET}"


def _route_leg(leg):
    place, code = leg
    return f"{place} {code}".strip() if place else code


def _hover_card(ac, home, route=None):
    """Chip lines for the aircraft under the pointer: identity, route, state.

    Lines carry their own fg colours but never RESET — the chip's background
    has to survive to the padded right edge (see ``_chip_overlay``).
    """
    mut = fg(*ensure_contrast(MUTED, CHIP_BG, 3.0))
    sep = f"{fg(*ensure_contrast(DIM, CHIP_BG, 2.0))} · {mut}"
    head = f"{fg(*ensure_contrast(blip_color(ac), CHIP_BG, 3.0))}{ac['callsign']}"
    craft = " ".join(p for p in (ac["actype"], ac["reg"]) if p)
    ident = " · ".join(p for p in (airline_name(ac["callsign"]), craft) if p)
    if ident:
        head += f" {mut}{ident}"
    lines = [head]
    if route is not None:
        lines.append(mut + " → ".join(_route_leg(leg) for leg in route))
    state = []
    if ac["ground"]:
        state.append("on the ground")
    elif ac["alt"] is not None:
        vr = ac["vrate"] or 0
        trend = (f" {trend_arrow(ac)}{abs(vr):,.0f} fpm"
                 if abs(vr) > 400 else "")
        state.append(f"{ac['alt']:,.0f} ft{trend}")
    if ac["gs"]:
        state.append(f"{ac['gs']:.0f} kt")
    if ac["squawk"]:
        state.append(f"squawk {ac['squawk']}")
    dist = haversine_nm(home[0], home[1], ac["lat"], ac["lon"])
    brg = bearing_to(home[0], home[1], ac["lat"], ac["lon"])
    state.append(f"{dist:.0f} nm {COMPASS[round(brg / 45) % 8]}")
    lines.append(mut + sep.join(state))
    return lines


def _chip_overlay(lines, mouse_pos, cols, rows):
    """Float chip lines beside the pointer, over whatever the map drew.

    Returns cursor-positioned ANSI for live_loop's ``\\x00`` overlay channel:
    anchored below-right of the pointer, pulled inward at the screen edges —
    the same floating chip as the linecast radar's warning tooltip.
    """
    boxed = [f"{bg(*CHIP_BG)} {ln} " for ln in lines]
    width = max(visible_len(ln) for ln in boxed)
    padded = [f"{ln}{' ' * (width - visible_len(ln))}{RESET}" for ln in boxed]
    mcol, mrow = mouse_pos
    col, row = mcol + 1, mrow + 1
    if col + width - 1 > cols:
        col = mcol - width
    if row + len(padded) - 1 > rows:
        row = mrow - len(padded)
    col = max(1, min(col, cols - width + 1))
    row = max(1, row)
    return "".join(f"\033[{row + i};{col}H{ln}"
                   for i, ln in enumerate(padded))


_last_frame = {}  # cached clean render, shifted for live drag-pan preview


def render_scope(center, zoom, feed, playing=True, mouse_pos=None,
                 show_trails=True, show_rings=True, show_ground=True,
                 weather=None, show_weather=False, drag_offset=None,
                 routes=None, pins=None, lines_geo=None, game_footer=None,
                 header_note=None, rings_at=None, ground=None,
                 hover_card=None, **_):
    """Render one frame of the scope.

    Hovering a blip floats a chip of flight detail beside the pointer
    (drawn over the map via live_loop's ``\\x00`` overlay channel);
    ``hover_card`` is an optional callable(ac) → chip lines that replaces
    the stock card — the game uses it to serve a flight strip — falling
    back to the stock card when it returns None.

    The remaining parameters are the game's hooks, inert otherwise:
    ``pins`` are geo-anchored glyphs with labels (sector fixes), ``lines_geo``
    are geo-anchored braille strokes (runway, localizer), ``game_footer`` is
    ``(n_lines, builder)`` where ``builder(focused)`` supplies the bottom
    lines in place of the standard footer, ``header_note`` replaces the
    place name in the header (facility, score, shift clock), and
    ``rings_at`` pins the range rings, crosshair and compass to a fixed
    point (the airport you're working) instead of the view centre — they
    then ride the map through a pan rather than following the scope head.
    ``ground`` is a callable(bbox, graph_w, height_cells) → echo grid (or
    None): a second underlay tint below the weather — terrain — filling
    only the cells the weather leaves empty.
    """
    cols, rows = get_terminal_size()
    graph_w = max(20, cols)
    height_cells = max(8, rows - 1 - (game_footer[0] if game_footer else 1))

    # live drag: slide the last map under the cursor now; the true re-render
    # (fresh geography, blips) lands on release. The reticle (rings+crosshair)
    # is re-stamped at centre so it stays put while the map moves. Skip if the
    # terminal resized under us — a stale grid wouldn't line up — and fall thru.
    if (drag_offset is not None and _last_frame.get("gw") == graph_w
            and _last_frame.get("hc") == height_cells):
        dcol, drow = drag_offset
        blank = f"{bg(*BG_PRIMARY)} "
        shifted = _shift_grid(_last_frame["grid"], dcol, drow, blank)
        reticle_prev = _last_frame["reticle"]
        if _last_frame.get("geo_reticle"):
            # rings pinned to a place, not the screen: they ride the drag
            reticle_prev = {(cx + dcol, cy + drow): v
                            for (cx, cy), v in reticle_prev.items()}
        return "\n".join([
            _last_frame["header"],
            *_render_lines(shifted, reticle_prev),
            _last_frame["footer"]])

    now = time.time()
    lat, lon = center
    bbox = bbox_for(lat, lon, zoom, graph_w, height_cells)
    basemap = _get_basemap(bbox, graph_w, height_cells)
    aircraft, trails, updated, source, error = feed.snapshot()
    if not show_ground:
        aircraft = [ac for ac in aircraft if not ac["ground"]]

    # weather underlay: keep the poller aimed at the live view, but only paint
    # a frame that was fetched for exactly this window (else it'd be misaligned)
    echo = None
    wx_label = wx_error = None
    if show_weather and weather is not None:
        weather.set_view(bbox, graph_w, height_cells)
        rgba, pw, ph, frame_view, wx_label, _attr, _wxup, wx_error = \
            weather.snapshot()
        if rgba is not None and frame_view == _view_key(bbox, graph_w,
                                                        height_cells):
            echo = _build_echo(rgba, pw, ph, graph_w, height_cells)

    # terrain (or any ground tint) sits under the weather: it only fills
    # cells the precipitation leaves empty, so storms always read on top
    if ground is not None:
        gecho = ground(bbox, graph_w, height_cells, basemap.sea)
        if gecho is not None:
            if echo is None:
                echo = gecho
            else:
                for gy in range(height_cells):
                    erow, grow = echo[gy], gecho[gy]
                    for gx in range(graph_w):
                        if erow[gx] is None:
                            erow[gx] = grow[gx]

    # braille fx layer: trails under velocity leaders (range rings are a fixed
    # reticle stamped later, so they don't ride the map during a drag)
    fx = DotLayer(bbox, graph_w, height_cells)

    # game strokes: runway and localizer, drawn first so traffic covers them
    if lines_geo:
        for glat1, glon1, glat2, glon2, gcolor in lines_geo:
            gx0, gy0 = _project(glon1, glat1, fx.bbox, fx.dw, fx.dh)
            gx1, gy1 = _project(glon2, glat2, fx.bbox, fx.dw, fx.dh)
            fx._dot_line(gx0, gy0, gx1, gy1, gcolor)

    # aircraft positions, centre-out so central traffic gets labels first
    minlon, minlat, maxlon, maxlat = bbox
    placed = []
    for ac in aircraft:
        alat, alon = extrapolate(ac, now)
        if not (minlon <= alon <= maxlon and minlat <= alat <= maxlat):
            continue
        col = int((alon - minlon) / (maxlon - minlon) * graph_w)
        row = int((maxlat - alat) / (maxlat - minlat) * height_cells)
        d2 = (col - graph_w / 2) ** 2 + ((row - height_cells / 2) * 2) ** 2
        placed.append((d2, ac, alat, alon, col, row))
    placed.sort(key=lambda p: p[0])

    blip_cells = {(p[4], p[5]) for p in placed
                  if 0 <= p[4] < graph_w and 0 <= p[5] < height_cells}
    air = {}
    for _d2, ac, alat, alon, col, row in placed:
        color = blip_color(ac)
        if show_trails:
            _draw_trail(fx, basemap, trails.get(ac["hex"], ()), color, now)
        _draw_leader(fx, basemap, ac, alat, alon, color)
        if ac["ground"]:
            continue  # ground targets: dim blip only, no data block
        # declutter: a data block draws only where it fits whole — try the
        # right of the blip, then flip left; otherwise the blip stands alone
        label = data_block(ac)
        for cells in ([(col + 1 + i, row) for i in range(len(label))],
                      [(col - len(label) + i, row) for i in range(len(label))]):
            if all(0 <= c < graph_w and (c, row) not in air
                   and (c, row) not in blip_cells for c, _r in cells):
                for i, (c, _r) in enumerate(cells):
                    air[(c, row)] = (label[i],
                                     color if i < len(ac["callsign"])
                                     else MUTED)
                break

    # conflict pairs: a line tying the two blips the box is alarming about,
    # so on a crowded scope you see which two at a glance — and, at the
    # midpoint, the miles they're set to pass within.  It pulses with the
    # blips before the loss and goes solid once the three miles are gone.
    by = {ac["hex"]: (ac, alat, alon, col, row)
          for _d2, ac, alat, alon, col, row in placed}
    blink = int(now * 2) % 2
    seen_pairs = set()
    for ha, hb, sev in getattr(feed, "conflicts", ()):
        if ha not in by or hb not in by or (ha, hb) in seen_pairs:
            continue
        seen_pairs.add((ha, hb))
        if sev == "alert" and not blink:
            continue                     # in step with the alerting blips
        a, ala, alo, aco, aro = by[ha]
        b, bla, blo, bco, bro = by[hb]
        x0, y0 = _project(alo, ala, fx.bbox, fx.dw, fx.dh)
        x1, y1 = _project(blo, bla, fx.bbox, fx.dw, fx.dh)
        fx._dot_line(x0, y0, x1, y1,
                     lerp(_fade_bg(basemap, int(x0), int(y0)), ALERT, 0.5))
        gap = _cpa_nm(a, ala, alo, b, bla, blo)
        if gap is None:
            continue
        text = f"{gap:.1f}"
        mc = (aco + bco) // 2 - len(text) // 2   # centred on the midpoint
        mrow = (aro + bro) // 2
        # a same-altitude pair shares a row with its data blocks, so drop the
        # readout to a neighbouring row when the midpoint itself is taken
        for mr in (mrow, mrow - 1, mrow + 1, mrow - 2, mrow + 2):
            span = [(mc + i, mr) for i in range(len(text))]
            if all(0 <= c < graph_w and 0 <= mr < height_cells
                   and (c, mr) not in air and (c, mr) not in blip_cells
                   for c, _r in span):
                for i, (c, _r) in enumerate(span):
                    air[(c, mr)] = (text[i], ALERT)
                break

    # blips last: never covered, even by another aircraft's data block
    for _d2, ac, _alat, _alon, col, row in placed:
        if 0 <= col < graph_w and 0 <= row < height_cells:
            air[(col, row)] = (blip_glyph(ac), blip_color(ac))

    overlays = dict(basemap.airport_overlays())
    # game pins (sector fixes): glyph + as much of the label as fits, under
    # the traffic — a data block always outranks a fix name
    if pins:
        for plat, plon, glyph, pcolor, plabel in pins:
            if not (minlon <= plon <= maxlon and minlat <= plat <= maxlat):
                continue
            pcol = int((plon - minlon) / (maxlon - minlon) * graph_w)
            prow = int((maxlat - plat) / (maxlat - minlat) * height_cells)
            if not (0 <= pcol < graph_w and 0 <= prow < height_cells):
                continue
            overlays[(pcol, prow)] = (glyph, pcolor)
            for i, ch in enumerate(plabel or ""):
                cell = (pcol + 2 + i, prow)
                if (cell[0] >= graph_w or cell in overlays
                        or cell in blip_cells):
                    break
                overlays[cell] = (ch, DIM)
    overlays.update(air)

    # reticle: range rings + crosshair, stamped over the map last. Anchored
    # at the view centre by default (a drag slews the scope head under them);
    # with rings_at they anchor to that fixed point instead and ride the map.
    # Rings yield to text (blips/labels); the crosshair always wins.
    rlat, rlon = rings_at if rings_at is not None else (lat, lon)
    reticle = {}
    hcol = int((rlon - minlon) / (maxlon - minlon) * graph_w)
    hrow = int((maxlat - rlat) / (maxlat - minlat) * height_cells)
    if show_rings:
        ring_layer = DotLayer(bbox, graph_w, height_cells)
        _draw_rings(ring_layer, rlat, rlon, zoom)
        for ry in range(height_cells):
            mrow, crow = ring_layer.dots[ry], ring_layer.color[ry]
            for rx in range(graph_w):
                mask = mrow[rx]
                if mask and (rx, ry) not in overlays:
                    reticle[(rx, ry)] = (chr(0x2800 + mask), crow[rx] or RING)
        if game_footer is not None:
            # compass: dim two-digit headings every 30° on the second ring,
            # so "turn left heading two three zero" has somewhere to point
            r_nm = _ring_spacing_nm(zoom) * 2
            for deg in range(0, 360, 30):
                clat, clon = advance(rlat, rlon, deg, r_nm)
                cx, cy = _project(clon, clat, bbox, graph_w, height_cells)
                col, row = int(cx), int(cy)
                label = f"{(deg or 360) // 10:02d}"
                cells = [(col, row), (col + 1, row)]
                if all(0 <= c < graph_w and 0 <= r < height_cells
                       and (c, r) not in overlays for c, r in cells):
                    for (c, r), ch in zip(cells, label):
                        reticle[(c, r)] = (ch, RING)
    if 0 <= hcol < graph_w and 0 <= hrow < height_cells:
        reticle[(hcol, hrow)] = ("+", MARKER)

    # pointer focus: nearest blip within a few cells of the mouse
    focused = None
    if mouse_pos is not None and placed:
        mcol, mrow = mouse_pos[0] - 1, mouse_pos[1] - 2  # screen → cell
        best = min(placed, key=lambda p: (p[4] - mcol) ** 2
                   + ((p[5] - mrow) * 2) ** 2)
        if abs(best[4] - mcol) <= 4 and abs(best[5] - mrow) <= 2:
            focused = best[1]

    grid = compose(basemap, fx, overlays, graph_w, height_cells, echo)
    map_lines = _render_lines(grid, reticle)

    airborne = sum(1 for ac in aircraft if not ac["ground"])
    ground = len(aircraft) - airborne
    counts = f"✈ {airborne}" if updated else "✈ …"
    if updated and ground and show_ground:
        counts += f" · {ground} gnd"
    fetching, fetch_started, trying = (
        feed.fetch_status() if hasattr(feed, "fetch_status")
        else (False, None, ""))
    if game_footer is not None:
        status = "▶" if playing else "⏸"   # no feed to be stale in the game
    elif updated is None and not error:
        # first fetch still in flight: show it's alive (and, once it drags
        # on, that it's working through slow aggregators — not empty skies)
        spin = SPINNER[int(now * 8) % len(SPINNER)]
        status = f"{spin} fetching {trying or 'traffic'}…"
        wait = (now - fetch_started) if fetch_started else 0.0
        if wait > 4:
            status += f" {wait:.0f}s"
    else:
        age = f"↺ {now - updated:.0f}s" if updated else "…"
        status = f"{'▶' if playing else '⏸'} {age}"
        if error:
            status += " · feed unavailable, retrying"
        elif fetching and fetch_started and now - fetch_started > 5:
            status += f" · still fetching… {now - fetch_started:.0f}s"

    def _header(place_str):
        return (f"{fg(*MARKER)}{BOLD}⬤ blips{RESET}  {fg(*MUTED)}{place_str}"
                f"{RESET}  {fg(*DIM)}{counts} · {status}{RESET}")

    place = header_note if header_note else _place(lat, lon)
    header = _header(place)
    over = visible_len(header) - cols
    if over > 0 and len(place) > over + 1:
        header = _header(place[:len(place) - over - 1] + "…")
    header += " " * max(0, cols - visible_len(header))

    if game_footer is not None:
        # the game supplies the bottom lines (radio log + command bar);
        # clip before padding — a wrapped footer line would shove the whole
        # frame down a row on every repaint
        foot = "\n".join(
            (lambda ln: ln + " " * max(0, cols - visible_len(ln)))(
                _clip_ansi(line, cols))
            for line in game_footer[1](focused))
    else:
        if updated is None:
            src_note = f"Contacting {trying or 'adsb.lol'}…"
        else:
            src_note = f"Live ADS-B by {source}"
        if show_weather:
            if echo is not None:
                src_note += f" · wx {wx_label}"
            elif wx_error:
                src_note += " · wx unavailable"
            else:
                src_note += " · wx …"
        left = f"{fg(*DIM)}{src_note}{RESET}"
        ring_note = f"rings {_ring_spacing_nm(zoom)} nm" if show_rings else ""
        hint = (f"{fg(*DIM)}{ring_note} · +/- zoom · drag pan · t trails "
                f"· r rings · g ground · w weather · q quit{RESET}"
                if sys.stdout.isatty() else "")
        for foot in (f"{left}  {hint}", f"{left}", ""):
            if visible_len(foot) <= cols:
                break
    if game_footer is None:
        foot += " " * max(0, cols - visible_len(foot))

    # cache this clean frame so a live drag can shift the map whole and
    # re-stamp the (fixed) reticle over it (see the drag fast-path up top);
    # blip screen positions ride along so a click can name its aircraft
    _last_frame.clear()
    _last_frame.update(grid=grid, reticle=reticle, header=header, footer=foot,
                       gw=graph_w, hc=height_cells,
                       geo_reticle=rings_at is not None,
                       hits=[(p[4], p[5], p[1]["callsign"]) for p in placed])

    out = "\n".join([header, *map_lines, foot])
    # hover chip: flight detail floats beside the pointer instead of living
    # in the footer, so the eye never leaves the blip it's asking about
    if focused is not None and mouse_pos is not None:
        card = hover_card(focused) if hover_card is not None else None
        if card is None:
            # route lookup is async: None now, filled in (with a repaint
            # nudge) once adsbdb answers — keep asking while the hover lasts
            route = (routes.get(focused["callsign"], focused["lat"],
                                focused["lon"]) if routes else None)
            card = _hover_card(focused, center, route)
        out += "\x00" + _chip_overlay(card, mouse_pos, cols, rows)
    return out


def hit_test(screen_col, screen_row):
    """Callsign of the blip near a screen cell, or None (game click-to-hail).

    Uses the last clean frame's blip positions; same tolerance as hover
    focus (a couple of rows, a few columns — cells are tall).
    """
    hits = _last_frame.get("hits")
    if not hits:
        return None
    mcol, mrow = screen_col - 1, screen_row - 2  # screen → cell coords
    best = min(hits, key=lambda h: (h[0] - mcol) ** 2
               + ((h[1] - mrow) * 2) ** 2)
    if abs(best[0] - mcol) <= 4 and abs(best[1] - mrow) <= 2:
        return best[2]
    return None


def main():
    args = blips_parser().parse_args()
    if args.debug:
        from blips._runtime import set_debug
        set_debug(True)

    if args.game is not None:
        from blips._game import main as game_main
        game_main(args)
        return

    override = args.location or os.environ.get("BLIPS_LOCATION", "").strip()
    if override:
        try:
            parts = override.split(",")
            lat, lon = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            hit = geocode_place(override)
            if hit is None:
                print(f'No locations matching "{override}".', file=sys.stderr)
                sys.exit(1)
            lat, lon = hit[0], hit[1]
    else:
        lat, lon = get_location()
        if lat is None:
            print("Could not determine location; try --location.",
                  file=sys.stderr)
            sys.exit(1)

    live = resolve_live(args)
    zoom = [max(0.4, min(24.0, args.zoom))]
    center = [lat, lon]
    feed = Feed(nudge=live)
    routes = RouteLookup(nudge=live)
    feed.set_view(lat, lon, view_radius_nm(
        bbox_for(lat, lon, zoom[0], 80, 40), lat, lon))

    from blips._radar_sources import theme_id
    wx_theme = theme_id(args.wx_theme)
    weather = WeatherFeed(lat, lon, theme=wx_theme, nudge=live)

    if not live:
        try:
            feed.poll_once()
        except Exception as exc:
            feed.error = str(exc)
        if args.weather:
            cols, rows = get_terminal_size()
            gw, hc = max(20, cols), max(8, rows - 2)
            weather.set_enabled(True)
            weather.set_view(bbox_for(lat, lon, zoom[0], gw, hc), gw, hc)
            try:
                weather.poll_once()
            except Exception as exc:
                weather.error = str(exc)
        print(render_scope(center, zoom[0], feed, playing=False,
                           weather=weather, show_weather=bool(args.weather)))
        return

    toggles = {"t": [True], "r": [True], "g": [True], "w": [bool(args.weather)]}

    def _sync_feed():
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 2)
        bbox = bbox_for(center[0], center[1], zoom[0], gw, hc)
        feed.set_view(center[0], center[1],
                      view_radius_nm(bbox, center[0], center[1]))

    def _sync_weather():
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 2)
        weather.set_view(bbox_for(center[0], center[1], zoom[0], gw, hc),
                         gw, hc)

    def on_action(key):
        if key == "+":
            zoom[0] = max(0.4, zoom[0] / 1.5)
        elif key == "-":
            zoom[0] = min(24.0, zoom[0] * 1.5)
        elif key in toggles:
            toggles[key][0] = not toggles[key][0]
            if key == "w":
                weather.set_enabled(toggles["w"][0])
                _sync_weather()
            return True
        else:
            return False
        _sync_feed()
        return True

    drag_preview = [None]  # (dcol, drow) while a drag is mid-flight, else None

    def on_drag(dcol, drow, done):
        if not done:
            # live feedback: shift the last frame so the map tracks the cursor
            # immediately; the real re-render happens once the button releases
            drag_preview[0] = (dcol, drow) if (dcol or drow) else None
            return bool(dcol or drow)
        drag_preview[0] = None
        if not (dcol or drow):
            return False
        # dragging pulls the map, so the view centre moves the opposite way
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 2)
        lon_span = (zoom[0] * (gw / (hc * 2))
                    / max(0.2, math.cos(math.radians(center[0]))))
        center[0] = max(-80.0, min(80.0, center[0] + drow * zoom[0] / hc))
        center[1] += -dcol * lon_span / gw
        if center[1] > 180.0:
            center[1] -= 360.0
        elif center[1] < -180.0:
            center[1] += 360.0
        _sync_feed()
        return True

    feed.start()
    weather.start()
    if toggles["w"][0]:
        weather.set_enabled(True)
        _sync_weather()
    live_loop(
        lambda playing=True, mouse_pos=None, **_: render_scope(
            center, zoom[0], feed, playing=playing,
            mouse_pos=mouse_pos, show_trails=toggles["t"][0],
            show_rings=toggles["r"][0], show_ground=toggles["g"][0],
            weather=weather, show_weather=toggles["w"][0],
            drag_offset=drag_preview[0], routes=routes),
        interval=REFRESH_S,
        mouse=True,
        auto_play=True,
        play_interval=0.5,  # dead-reckoning glide rate between polls
        on_action=on_action,
        on_drag=on_drag,
    )


if __name__ == "__main__":
    main()
