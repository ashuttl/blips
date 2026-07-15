#!/usr/bin/env python3
"""blips — live air traffic on a braille basemap.

An ambient terminal scope: geography (sea stipple, coastlines, borders) is
drawn in braille, and live aircraft from community ADS-B aggregators are
painted over it — a directional blip with an ATC-style data block
(callsign + flight level), a velocity leader showing the next minute of
travel, and a fading braille trail of recent positions.  Between feed polls
the blips glide on dead reckoning, so the scope moves the way the sky does.

Layer priority per terminal cell, top to bottom:

  1. aircraft blip           → arrow glyph in altitude colour
  2. data block / city label → text glyph
  3. leaders, trails, rings  → braille strokes
  4. braille geography       → sea stipple / coast / border dots
  5. bare land               → background

Usage: blips [--location LAT,LNG | PLACE] [--zoom DEG] [--print]
"""

import math
import os
import signal
import sys
import threading
import time

from blips._adsb import fetch_point
from blips._basemap import (
    Basemap, DotLayer, marine_region, nearest_city, _project,
)
from blips._color import BG_PRIMARY, BOLD, RESET, bg, fg, interp_stops, lerp
from blips._framebuffer import get_terminal_size, visible_len
from blips._geo import haversine_nm
from blips._live import live_loop
from blips._location import geocode_place, get_location
from blips._runtime import blips_parser, resolve_live

MUTED = (150, 155, 170)
DIM = (110, 114, 130)
MARKER = (255, 240, 120)
GROUND = (125, 129, 145)
RING = (62, 76, 108)
ALERT = (235, 80, 70)

# altitude (ft) → blip colour, low-and-warm to high-and-cool
ALT_STOPS = (
    (0, (240, 190, 80)),
    (10000, (140, 210, 110)),
    (20000, (80, 185, 220)),
    (30000, (115, 140, 240)),
    (45000, (190, 120, 240)),
)

REFRESH_S = 5.0       # feed poll cadence (aggregators ask ≤1 req/s; we're 5x under)
MAX_EXTRAP_S = 20.0   # cap dead reckoning so a stale fix doesn't fly away
TRAIL_MAX_AGE = 480.0
TRAIL_MAX_FIXES = 120
ARROWS = "↑↗→↘↓↙←↖"
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

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


def advance(lat, lon, track_deg, dist_nm):
    """Move a point along a bearing (equirectangular; fine at scope ranges)."""
    rad = math.radians(track_deg)
    dlat = dist_nm * math.cos(rad) / 60.0
    dlon = (dist_nm * math.sin(rad)
            / (60.0 * max(0.2, math.cos(math.radians(lat)))))
    return lat + dlat, lon + dlon


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
    if ac["ground"]:
        return GROUND
    if ac["alt"] is None:
        return MUTED
    return interp_stops(list(ALT_STOPS), ac["alt"])


def blip_glyph(ac):
    if ac["ground"]:
        return "·"
    if ac["track"] is None:
        return "•"
    return ARROWS[round(ac["track"] / 45.0) % 8]


def trend_arrow(ac):
    vr = ac["vrate"] or 0
    return "↑" if vr > 400 else "↓" if vr < -400 else ""


def data_block(ac):
    """ATC-style label: callsign + flight level (hundreds of feet) + trend."""
    if ac["ground"]:
        return ac["callsign"]
    if ac["alt"] is None:
        return ac["callsign"]
    return f"{ac['callsign']} {round(ac['alt'] / 100):03d}{trend_arrow(ac)}"


class Feed:
    """Background poller: keeps fresh aircraft + position trails per view."""

    def __init__(self, nudge=False):
        self.aircraft = []
        self.trails = {}     # hex → [(lat, lon, fix_time), ...]
        self.updated = None  # wall time of last successful poll
        self.source = ""
        self.error = None
        self._view = None    # (lat, lon, radius_nm)
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
        aircraft, source = fetch_point(lat, lon, radius)
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


def _bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(dlon))
    return math.degrees(math.atan2(y, x)) % 360.0


def _ring_spacing_nm(zoom):
    half_span = zoom * 30  # nm from centre to the top edge
    for spacing in (5, 10, 25, 50, 100, 200):
        if half_span / spacing <= 3.5:
            return spacing
    return 400


def _draw_rings(fx, home_lat, home_lon, zoom):
    """Dotted range rings around the home marker, ATC-scope style."""
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


def _draw_trail(fx, trail, color, now):
    for tlat, tlon, tt in trail:
        age = now - tt
        if age > TRAIL_MAX_AGE:
            continue
        weight = max(0.15, 0.55 * (1.0 - age / TRAIL_MAX_AGE))
        x, y = _project(tlon, tlat, fx.bbox, fx.dw, fx.dh)
        fx._set_dot(int(x), int(y), lerp(BG_PRIMARY, color, weight))


def _draw_leader(fx, ac, lat, lon, color):
    """Velocity leader: where the aircraft will be in one minute."""
    if ac["ground"] or not ac["gs"] or ac["track"] is None:
        return
    tip_lat, tip_lon = advance(lat, lon, ac["track"], ac["gs"] / 60.0)
    x0, y0 = _project(lon, lat, fx.bbox, fx.dw, fx.dh)
    x1, y1 = _project(tip_lon, tip_lat, fx.bbox, fx.dw, fx.dh)
    fx._dot_line(x0, y0, x1, y1, lerp(BG_PRIMARY, color, 0.55))


def compose(basemap, fx, overlays, graph_w, height_cells):
    """Composite geography + braille fx + text overlays into ANSI lines."""
    base_bg = bg(*BG_PRIMARY)
    lines = []
    for cy in range(height_cells):
        parts = []
        for cx in range(graph_w):
            ov = overlays.get((cx, cy))
            if ov is not None:
                ch, color = ov
                if ch == "":
                    continue  # trailing column of a double-width glyph
                parts.append(f"{base_bg}{fg(*color)}{ch}")
                continue
            mask = fx.dots[cy][cx]
            layer = fx if mask else basemap
            if not mask:
                mask = basemap.dots[cy][cx]
            if mask:
                color = layer.color[cy][cx] or DIM
                parts.append(f"{base_bg}{fg(*color)}{chr(0x2800 + mask)}")
            else:
                parts.append(f"{base_bg} ")
        parts.append(RESET)
        lines.append("".join(parts))
    return lines


def _focused_line(ac, home):
    """Footer detail line for the aircraft nearest the pointer."""
    pieces = [f"{fg(*blip_color(ac))}{BOLD}{ac['callsign']}{RESET}"]
    ident = " ".join(p for p in (ac["actype"], ac["reg"]) if p)
    if ident:
        pieces.append(ident)
    if ac["ground"]:
        pieces.append("on the ground")
    elif ac["alt"] is not None:
        vr = ac["vrate"] or 0
        trend = (f" {trend_arrow(ac)}{abs(vr):,.0f} fpm"
                 if abs(vr) > 400 else "")
        pieces.append(f"{ac['alt']:,.0f} ft{trend}")
    if ac["gs"]:
        pieces.append(f"{ac['gs']:.0f} kt")
    if ac["squawk"]:
        pieces.append(f"squawk {ac['squawk']}")
    dist = haversine_nm(home[0], home[1], ac["lat"], ac["lon"])
    brg = _bearing(home[0], home[1], ac["lat"], ac["lon"])
    pieces.append(f"{dist:.0f} nm {COMPASS[round(brg / 45) % 8]}")
    sep = f"{fg(*DIM)} · {RESET}"
    return sep.join(p if "\033" in p else f"{fg(*MUTED)}{p}{RESET}"
                    for p in pieces)


def render_scope(center, zoom, feed, home, playing=True, mouse_pos=None,
                 show_trails=True, show_rings=True, show_ground=True, **_):
    now = time.time()
    cols, rows = get_terminal_size()
    graph_w = max(20, cols)
    height_cells = max(8, rows - 2)

    lat, lon = center
    bbox = bbox_for(lat, lon, zoom, graph_w, height_cells)
    basemap = _get_basemap(bbox, graph_w, height_cells)
    aircraft, trails, updated, source, error = feed.snapshot()
    if not show_ground:
        aircraft = [ac for ac in aircraft if not ac["ground"]]

    # braille fx layer: rings under trails under velocity leaders
    fx = DotLayer(bbox, graph_w, height_cells)
    if show_rings:
        _draw_rings(fx, home[0], home[1], zoom)

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
            _draw_trail(fx, trails.get(ac["hex"], ()), color, now)
        _draw_leader(fx, ac, alat, alon, color)
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

    # blips last: never covered, even by another aircraft's data block
    for _d2, ac, _alat, _alon, col, row in placed:
        if 0 <= col < graph_w and 0 <= row < height_cells:
            air[(col, row)] = (blip_glyph(ac), blip_color(ac))

    overlays = dict(basemap.city_overlays())
    overlays.update(air)

    # home marker, pinned geographically (panning can move it off-centre)
    hcol = int((home[1] - minlon) / (maxlon - minlon) * graph_w)
    hrow = int((maxlat - home[0]) / (maxlat - minlat) * height_cells)
    if 0 <= hcol < graph_w and 0 <= hrow < height_cells:
        overlays[(hcol, hrow)] = ("+", MARKER)

    # pointer focus: nearest blip within a few cells of the mouse
    focused = None
    if mouse_pos is not None and placed:
        mcol, mrow = mouse_pos[0] - 1, mouse_pos[1] - 2  # screen → cell
        best = min(placed, key=lambda p: (p[4] - mcol) ** 2
                   + ((p[5] - mrow) * 2) ** 2)
        if abs(best[4] - mcol) <= 4 and abs(best[5] - mrow) <= 2:
            focused = best[1]

    map_lines = compose(basemap, fx, overlays, graph_w, height_cells)

    airborne = sum(1 for ac in aircraft if not ac["ground"])
    ground = len(aircraft) - airborne
    counts = f"✈ {airborne}"
    if ground and show_ground:
        counts += f" · {ground} gnd"
    age = f"↺ {now - updated:.0f}s" if updated else "…"
    status = f"{'▶' if playing else '⏸'} {age}"
    if error:
        status += " · feed unavailable, retrying"

    def _header(place_str):
        return (f"{fg(*MARKER)}{BOLD}⬤ blips{RESET}  {fg(*MUTED)}{place_str}"
                f"{RESET}  {fg(*DIM)}{counts} · {status}{RESET}")

    place = _place(lat, lon)
    header = _header(place)
    over = visible_len(header) - cols
    if over > 0 and len(place) > over + 1:
        header = _header(place[:len(place) - over - 1] + "…")
    header += " " * max(0, cols - visible_len(header))

    if focused is not None:
        foot = _focused_line(focused, home)
        if visible_len(foot) > cols:
            foot = f"{fg(*MUTED)}{focused['callsign']}{RESET}"
    else:
        left = f"{fg(*DIM)}Live ADS-B by {source or 'adsb.lol'}{RESET}"
        ring_note = f"rings {_ring_spacing_nm(zoom)} nm" if show_rings else ""
        hint = (f"{fg(*DIM)}{ring_note} · +/- zoom · drag pan · t trails "
                f"· r rings · g ground · q quit{RESET}"
                if sys.stdout.isatty() else "")
        for foot in (f"{left}  {hint}", f"{left}", ""):
            if visible_len(foot) <= cols:
                break
    foot += " " * max(0, cols - visible_len(foot))

    return "\n".join([header, *map_lines, foot])


def main():
    args = blips_parser().parse_args()
    if args.debug:
        from blips._runtime import set_debug
        set_debug(True)

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
    home = (lat, lon)
    zoom = [max(0.4, min(24.0, args.zoom))]
    center = [lat, lon]
    feed = Feed(nudge=live)
    feed.set_view(lat, lon, view_radius_nm(
        bbox_for(lat, lon, zoom[0], 80, 40), lat, lon))

    if not live:
        try:
            feed.poll_once()
        except Exception as exc:
            feed.error = str(exc)
        print(render_scope(center, zoom[0], feed, home, playing=False))
        return

    toggles = {"t": [True], "r": [True], "g": [True]}

    def _sync_feed():
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 2)
        bbox = bbox_for(center[0], center[1], zoom[0], gw, hc)
        feed.set_view(center[0], center[1],
                      view_radius_nm(bbox, center[0], center[1]))

    def on_action(key):
        if key == "+":
            zoom[0] = max(0.4, zoom[0] / 1.5)
        elif key == "-":
            zoom[0] = min(24.0, zoom[0] * 1.5)
        elif key in toggles:
            toggles[key][0] = not toggles[key][0]
            return True
        else:
            return False
        _sync_feed()
        return True

    def on_drag(dcol, drow, done):
        if not done or not (dcol or drow):
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
    live_loop(
        lambda playing=True, mouse_pos=None, **_: render_scope(
            center, zoom[0], feed, home, playing=playing,
            mouse_pos=mouse_pos, show_trails=toggles["t"][0],
            show_rings=toggles["r"][0], show_ground=toggles["g"][0]),
        interval=REFRESH_S,
        mouse=True,
        auto_play=True,
        play_interval=0.5,  # dead-reckoning glide rate between polls
        on_action=on_action,
        on_drag=on_drag,
    )


if __name__ == "__main__":
    main()
