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

import functools
import math
import os
import sys
import time

from blips._commands import airline_name
from blips._basemap import (
    Basemap, DotLayer, SEA_FILL, marine_region, nearest_city, _project,
)
from blips._color import BG_PRIMARY, BOLD, RESET, bg, fg, interp_stops, lerp
from blips._framebuffer import get_terminal_size, visible_len
from blips._geo import advance, bearing_to, haversine_nm
from blips._camera import bbox_for  # noqa: F401 — the game imports it here
from blips._feeds import (  # noqa: F401 — re-exported for the game
    REFRESH_S, TRAIL_MAX_AGE, TRAIL_MAX_FIXES, WEATHER_REFRESH_S, Feed,
    WeatherFeed, _view_key,
)
from blips._location import geocode_place, get_location
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

WX_DIM = 0.6          # weather is an ambient underlay; keep traffic readable on top
MAX_EXTRAP_S = 20.0   # cap dead reckoning so a stale fix doesn't fly away
ARROWS = "↑↗→↘↓↙←↖"
HEAVY_ARROWS = "⇑⇗⇒⇘⇓⇙⇐⇖"
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # header pulse while the first fetch is in flight

_basemap_cache = {}


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
    color = interp_stops(list(ALT_STOPS), ac["alt"])
    if ac.get("limited"):
        # an uncorrelated 1200 target: the altitude hue survives — a
        # hazard is still judged by colour — but washed toward grey, so
        # full saturation means on your frequency and nothing else does
        return lerp(DIM, color, 0.4)
    return color


def blip_glyph(ac):
    """The blip: an arrow along the track, doubled for a heavy (ADS-B
    emitter category A5) so the widebodies stand out of a crowd, and a
    rotor star for a helicopter (A7), whose track says little."""
    if ac.get("glyph"):
        return ac["glyph"]
    if ac["ground"]:
        return "·"
    category = ac.get("category")
    if category == "A7":
        return "✱"
    if ac["track"] is None:
        return "•"
    arrows = HEAVY_ARROWS if category == "A5" else ARROWS
    return arrows[round(ac["track"] / 45.0) % 8]


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


SELECT_BG = lerp(BG_PRIMARY, MARKER, 0.28)   # under the selected blip


def _draw_geo_path(fx, basemap, points, color, dotted=False):
    """A (lat, lon) polyline in braille — the selected flight's route.

    Longitudes are unwrapped against the view's centre, so a route that
    crosses the antimeridian draws as one stroke.  ``dotted`` draws every
    third dot: the part of a route still to fly, against the solid part
    already flown.  The ink fades toward what it's drawn over, as trails
    do, so a route reads as a guide rather than as traffic.
    """
    minlon, minlat, maxlon, maxlat = fx.bbox
    mid = (minlon + maxlon) / 2
    lon_span, lat_span = maxlon - minlon, maxlat - minlat
    prev = None
    count = 0
    for lat, lon in points:
        lon = mid + (lon - mid + 180.0) % 360.0 - 180.0
        x = (lon - minlon) / lon_span * fx.dw
        y = (maxlat - lat) / lat_span * fx.dh
        if prev is not None:
            px, py = prev
            if not ((px < -2 and x < -2) or (px > fx.dw + 2 and x > fx.dw + 2)
                    or (py < -2 and y < -2)
                    or (py > fx.dh + 2 and y > fx.dh + 2)):
                steps = int(max(abs(x - px), abs(y - py))) + 1
                for i in range(steps):
                    count += 1
                    if dotted and count % 3:
                        continue
                    dx = int(px + (x - px) * i / steps)
                    dy = int(py + (y - py) * i / steps)
                    if 0 <= dx < fx.dw and 0 <= dy < fx.dh:
                        fx._set_dot(dx, dy, lerp(_fade_bg(basemap, dx, dy),
                                                 color, 0.7))
        prev = (x, y)


def _airport_focus(basemap, icao, overlays, hits):
    """Overlay cells that light up the airport on the card: its label in
    the marker colour, drawn even where the label budget left it out."""
    from blips._airports import find_airport
    ap = find_airport(icao)
    if ap is None:
        return {}
    out = {}
    for col, row, width, code in hits:
        if code == icao:
            for c in range(col, col + width + 1):
                cell = overlays.get((c, row))
                if cell is not None:
                    out[(c, row)] = (cell[0], MARKER, None, True)
            return out
    x, y = _project(ap["lon"], ap["lat"], basemap.bbox, basemap.graph_w,
                    basemap.height_cells)
    col, row = int(x), int(y)
    if not (0 <= col < basemap.graph_w and 0 <= row < basemap.height_cells):
        return out
    label = "◉" + (ap.get("iata") or ap["icao"])
    for i, ch in enumerate(label):
        if col + i < basemap.graph_w:
            out[(col + i, row)] = (ch, MARKER, None, True)
    hits.append((col, row, len(label) - 1, icao))
    return out


_TRAIL_STEPS = 24      # fade levels a trail is drawn in
_trail_ramps = {}      # (background, colour) → that pair's fade levels


def _trail_ramp(base, color):
    """The trail's fade levels from ``color`` toward ``base``: the weights
    a dot of any age can take, blended once per pair instead of per dot."""
    key = (base, color)
    ramp = _trail_ramps.get(key)
    if ramp is None:
        if len(_trail_ramps) > 4096:
            _trail_ramps.clear()
        lo, hi = 0.15, 0.55
        ramp = _trail_ramps[key] = [
            lerp(base, color, lo + (hi - lo) * i / (_TRAIL_STEPS - 1))
            for i in range(_TRAIL_STEPS)]
    return ramp


def _draw_trail(fx, basemap, trail, color, now, bright=False):
    """A fading dotted history: newest fixes at 0.55 toward the blip's
    colour, dissolving to 0.15 over TRAIL_MAX_AGE (and staying there
    for the oldest, so a long trail never vanishes mid-stroke).
    ``bright`` draws the selected flight's trail in its own colour, solid
    and joined, so the path it has flown stands out of the crowd."""
    if bright:
        pts = [(tlat, tlon) for tlat, tlon, _t in trail]
        _draw_geo_path(fx, basemap, pts, color, dotted=False)
        return
    minlon, minlat, maxlon, maxlat = fx.bbox
    lon_span, lat_span = maxlon - minlon, maxlat - minlat
    dw, dh = fx.dw, fx.dh
    sea, gw, hc = basemap.sea, basemap.graph_w, basemap.height_cells
    sea_ramp = land_ramp = None
    top = _TRAIL_STEPS - 1
    for tlat, tlon, tt in trail:
        age = now - tt
        if age > TRAIL_MAX_AGE:
            continue
        dx = int((tlon - minlon) / lon_span * dw)
        dy = int((maxlat - tlat) / lat_span * dh)
        if not (0 <= dx < dw and 0 <= dy < dh):
            continue
        weight = max(0.15, 0.55 * (1.0 - age / TRAIL_MAX_AGE))
        step = int((weight - 0.15) / 0.40 * top + 0.5)
        col, row = dx >> 1, dy >> 2
        if row < hc and col < gw and sea[row][col]:
            if sea_ramp is None:
                sea_ramp = _trail_ramp(SEA_FILL, color)
            fx._set_dot(dx, dy, sea_ramp[step])
        else:
            if land_ramp is None:
                land_ramp = _trail_ramp(BG_PRIMARY, color)
            fx._set_dot(dx, dy, land_ramp[step])


_trail_cache = {}   # the last trail layer, keyed by what it was drawn from


def _trail_layer(basemap, placed, trails, updated, now):
    """Every visible trail on a fresh DotLayer, ready for leaders on top.

    Trails change only when a poll lands (``updated``) or a fade step
    passes, so the layer is drawn once and copied per frame — a busy sky
    is tens of thousands of trail dots, too many to redraw at the glide
    rate.  Returns a copy the caller may draw over.
    """
    key = (basemap.bbox, basemap.graph_w, basemap.height_cells, updated,
           int(now // (TRAIL_MAX_AGE / _TRAIL_STEPS)),
           frozenset(p[1]["hex"] for p in placed))
    layer = _trail_cache.get(key)
    if layer is None:
        layer = DotLayer(basemap.bbox, basemap.graph_w, basemap.height_cells)
        for p in placed:
            ac = p[1]
            _draw_trail(layer, basemap, trails.get(ac["hex"], ()),
                        blip_color(ac), now)
        _trail_cache.clear()
        _trail_cache[key] = layer
    fx = DotLayer(basemap.bbox, basemap.graph_w, basemap.height_cells)
    fx.dots = [row[:] for row in layer.dots]
    fx.color = [row[:] for row in layer.color]
    return fx


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


_echo_cache = {}


def _echo_for(frame, bbox, graph_w, height_cells):
    """Resample a radar frame into the window on screen: a per-cell
    (r, g, b, weight) grid, None where there's no echo.

    The frame covers its own (padded) bbox at its own resolution, both
    plain lat/lon, so each cell's two sub-pixel rows look up the frame
    pixel under their centres — a pan or a zoom step paints the weather
    it already has instead of a blank until the next fetch.  The two
    samples' colours are alpha-blended and the coverage scaled by WX_DIM,
    so the weather reads as a dim underlay the traffic sits on top of.
    Returns fresh rows each call (the game tints its terrain into them).
    """
    rgba, pw, ph, fbbox = frame
    key = (id(rgba), fbbox, bbox, graph_w, height_cells)
    hit = _echo_cache.get(key)
    if hit is None:
        fminlon, fminlat, fmaxlon, fmaxlat = fbbox
        minlon, minlat, maxlon, maxlat = bbox
        fw, fh = fmaxlon - fminlon, fmaxlat - fminlat
        cols = []
        for cx in range(graph_w):
            lon = minlon + (cx + 0.5) / graph_w * (maxlon - minlon)
            px = int((lon - fminlon) / fw * pw)
            cols.append(px if 0 <= px < pw else -1)
        hit = []
        for cy in range(height_cells):
            ys = []
            for sub in (0.25, 0.75):
                lat = maxlat - (cy + sub) / height_cells * (maxlat - minlat)
                py = int((fmaxlat - lat) / fh * ph)
                ys.append(py * pw if 0 <= py < ph else -1)
            r0, r1 = ys
            erow = [None] * graph_w
            for cx, px in enumerate(cols):
                if px < 0:
                    continue
                if r0 >= 0:
                    i0 = (r0 + px) * 4
                    a0 = rgba[i0 + 3]
                else:
                    i0, a0 = 0, 0
                if r1 >= 0:
                    i1 = (r1 + px) * 4
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
            hit.append(erow)
        _echo_cache.clear()
        _echo_cache[key] = hit
    return [row[:] for row in hit]


def compose(basemap, fx, overlays, graph_w, height_cells, echo=None):
    """Composite geography + weather + braille fx + text overlays into a grid.

    The sea, land and (when present) weather echo all resolve to a per-cell
    background colour; braille strokes and text glyphs are drawn on top of it.
    Returns the grid as rows of ``(char, fg_rgb, bg_rgb, bold)`` cells — data,
    not escape codes, so a live drag can shift the frame whole, a reticle or
    the game's floating text can be stamped over a cell's own background, and
    ``_render_lines`` can emit a colour only where it changes.  A trailing
    column of a double-width glyph is ``("", None, bg, False)``.
    """
    sea = basemap.sea
    grid = []
    base_dots, base_color = basemap.dots, basemap.color
    fx_dots, fx_color = fx.dots, fx.color
    for cy in range(height_cells):
        srow = sea[cy]
        erow = echo[cy] if echo is not None else None
        fdots, fcols = fx_dots[cy], fx_color[cy]
        bdots, bcols = base_dots[cy], base_color[cy]
        row = []
        for cx in range(graph_w):
            bg_rgb = SEA_FILL if srow[cx] else BG_PRIMARY
            if erow is not None:
                e = erow[cx]
                if e is not None:
                    bg_rgb = lerp(bg_rgb, (e[0], e[1], e[2]), e[3])
            ov = overlays.get((cx, cy))
            if ov is not None:
                if len(ov) > 2:   # (ch, fg, bg or None, bold)
                    row.append((ov[0], ov[1], ov[2] or bg_rgb, ov[3]))
                else:
                    row.append((ov[0], ov[1], bg_rgb, False))
                continue
            mask = fdots[cx]
            if mask:
                row.append((chr(0x2800 + mask), fcols[cx] or DIM, bg_rgb,
                            False))
                continue
            mask = bdots[cx]
            if mask:
                row.append((chr(0x2800 + mask), bcols[cx] or DIM, bg_rgb,
                            False))
            else:
                row.append((" ", None, bg_rgb, False))
        grid.append(row)
    return grid


def _footer_alpha(i, n):
    """How opaque log line ``i`` (0 = top) is, of ``n`` lines.

    The command bar and the calls nearest it sit at full strength; lines
    ramp down with height *above the bar*, so the oldest tape lines dissolve
    into the radar. Keyed on distance from the bottom, not the top, so a
    footer of any height (the bar hint wraps on a narrow terminal) keeps
    its working lines legible.
    """
    j = n - 1 - i                 # rows above the bottom line
    if j < 5:
        return 1.0
    return max(0.0, min(1.0, (n - j) / 6))


def _line_overlay(overlay, segs, cy, graph_w, alpha):
    """Add one footer/header line of coloured segments to ``overlay`` at ``cy``.

    ``segs`` is a list of ``(text, rgb[, bold])``. Every glyph and every
    interior space knocks the map out (a clean band, so text never reads as
    speckled with radar or garbled by a blip label the full-height map drew
    underneath); only the gap past the last glyph is left to the map, so the
    line still floats over the scope on the right. The whole line carries
    ``alpha`` — ``_render_lines`` fades its text toward the map background.
    """
    if cy < 0:
        return
    # lay the whole line out first, so we know where the text ends and can
    # clear the interior spaces without touching the trailing gap
    cells = []            # (cx, char, rgb, bold, wide)
    cx = 0
    for seg in segs:
        text, rgb = seg[0], seg[1]
        bold = seg[2] if len(seg) > 2 else False
        for ch in text:
            if cx >= graph_w:
                break
            w = visible_len(ch)
            cells.append((cx, ch, rgb, bold, w == 2))
            cx += w
        if cx >= graph_w:
            break
    last = max((k for k, c in enumerate(cells) if c[1] != " "), default=-1)
    for k, (cxx, ch, rgb, bold, wide) in enumerate(cells):
        if ch == " " and k > last:
            continue  # trailing gap past the text: keep the map here
        overlay[(cxx, cy)] = (ch, rgb, bold, alpha)
        if wide and cxx + 1 < graph_w:
            overlay[(cxx + 1, cy)] = ("", rgb, bold, alpha)


def _footer_overlay(lines, graph_w, height_cells):
    """Lay the game's log over the bottom map rows as a per-cell overlay.

    Each line is ``(segments, alpha)`` (or a bare segment list, alpha 1.0),
    anchored to the bottom of the map. Its opacity is that line alpha — the
    game fades a call by age there — times ``_footer_alpha`` for its distance
    from the bottom, so old and scrolled-away calls both dissolve into the
    radar. A line alpha of ``None`` pins the line at full strength wherever
    it sits — the help card reads top to bottom, not oldest to newest.
    Returns {(cx, cy): (char, rgb, bold, alpha)}.
    """
    n = len(lines)
    y0 = height_cells - n
    overlay = {}
    for i, line in enumerate(lines):
        segs, line_alpha = line if isinstance(line, tuple) else (line, 1.0)
        alpha = (1.0 if line_alpha is None
                 else _footer_alpha(i, n) * line_alpha)
        if alpha < 0.08:
            # faded to nothing (an old call aged out): drop the line entirely
            # so the scope shows through where it was, braille and all — a knocked
            # -out band of near-invisible text would just be a hole in the map
            continue
        _line_overlay(overlay, segs, y0 + i, graph_w, alpha)
    return overlay


_BOLD_OFF = "\033[22m"


@functools.lru_cache(maxsize=8192)
def _bg_esc(rgb):
    return bg(*rgb)


@functools.lru_cache(maxsize=8192)
def _fg_esc(rgb):
    return fg(*rgb)


def _emit_row(cells):
    """One row of ``(char, fg, bg, bold)`` cells as ANSI, naming a colour
    only where it changes — most of a scope is one sea or one land
    background, so a row is mostly bare glyphs.  Compared as escape codes,
    so two colours a 256-colour terminal draws alike don't repeat."""
    parts = []
    cur_bg = cur_fg = None
    cur_bold = False
    for ch, col, bgc, bold in cells:
        if not ch:
            continue  # trailing column of a double-width glyph
        if ch != " " and bold != cur_bold:
            parts.append(BOLD if bold else _BOLD_OFF)
            cur_bold = bold
        e = _bg_esc(bgc)
        if e != cur_bg:
            parts.append(e)
            cur_bg = e
        if ch != " " and col is not None:
            e = _fg_esc(col)
            if e != cur_fg:
                parts.append(e)
                cur_fg = e
        parts.append(ch)
    parts.append(RESET)
    return "".join(parts)


def _render_lines(grid, reticle=None, text=None):
    """Flatten a per-cell grid into ANSI lines, stamping overlays on top.

    ``reticle`` is {(cx, cy): (char, color)} for the range rings and crosshair.
    Each glyph is drawn over its cell's existing background, so the reticle
    stays fixed at the view centre while the map (the grid) shifts beneath it
    during a live drag — panning slews the scope head, it doesn't drag it off.

    ``text`` (the game's floating top bar and log) is
    {(cx, cy): (char, rgb, bold, alpha)}: each glyph is drawn over its cell's
    own map background — knocking the braille out from under the glyphs
    while the gaps keep the terrain — fading toward that background by its
    alpha, and it outranks the reticle so a ring dot never eats a letter.
    """
    lines = []
    for cy, row in enumerate(grid):
        if reticle or text:
            row = list(row)
            for cx, cell in enumerate(row):
                fo = text.get((cx, cy)) if text else None
                if fo is not None:
                    ch, rgb, bold, alpha = fo
                    base = cell[2]
                    col = rgb if alpha >= 1.0 else lerp(base, rgb, alpha)
                    row[cx] = (ch, col, base, bold)
                    continue
                rc = reticle.get((cx, cy)) if reticle else None
                if rc is not None:
                    row[cx] = (rc[0], rc[1], cell[2], False)
        lines.append(_emit_row(row))
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
                 show_labels=True,
                 weather=None, show_weather=False, drag_offset=None,
                 routes=None, pins=None, lines_geo=None, game_footer=None,
                 header_note=None, rings_at=None, ground=None,
                 hover_card=None, pin_card=None, selected=None,
                 route_geo=None, aircraft_filter=None, label_mode=None,
                 airport_focus=None, header_extra=None, footer_hint=None,
                 blocked=None, **_):
    """Render one frame of the scope.

    Hovering a blip floats a chip of flight detail beside the pointer
    (drawn over the map via live_loop's ``\\x00`` overlay channel);
    ``pin_card`` is an optional callable(key) → chip lines for the scenery
    under the pointer when no aircraft is (a fix, a procedure name).
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

    The live scope's hooks: ``selected`` is the hex of the flight on the
    card (always labelled, highlighted, its trail drawn bright);
    ``route_geo`` is a list of ``(points, colour, dotted)`` paths in
    (lat, lon) — the selected flight's route; ``aircraft_filter(ac)``
    hides what it rejects (the selected flight excepted); ``label_mode``
    is "full", "callsign" or "off"; ``airport_focus`` is the ICAO code of
    the airport on the card; ``header_extra`` is a list of (text, rgb)
    appended to the header; ``footer_hint`` replaces the key hint;
    ``blocked`` is a list of 1-based (col0, row0, col1, row1) screen
    rectangles (panels) where the pointer focuses nothing.
    """
    cols, rows = get_terminal_size()
    graph_w = max(20, cols)
    # in the game the top bar and the log both float over the map (their glyphs
    # knock the braille out from under them, terrain shows through the gaps), so
    # the map fills the whole screen and they're composited into its top and
    # bottom rows; the live feed keeps a header row and one footer row of its own
    height_cells = max(8, rows if game_footer is not None else rows - 2)

    # live drag: slide the last map under the cursor now; the true re-render
    # (fresh geography, blips) lands on release. The reticle (rings+crosshair)
    # is re-stamped at centre so it stays put while the map moves. Skip if the
    # terminal resized under us — a stale grid wouldn't line up — and fall thru.
    if (drag_offset is not None and _last_frame.get("gw") == graph_w
            and _last_frame.get("hc") == height_cells):
        dcol, drow = drag_offset
        blank = (" ", None, BG_PRIMARY, False)
        shifted = _shift_grid(_last_frame["grid"], dcol, drow, blank)
        reticle_prev = _last_frame["reticle"]
        if _last_frame.get("geo_reticle"):
            # rings pinned to a place, not the screen: they ride the drag
            reticle_prev = {(cx + dcol, cy + drow): v
                            for (cx, cy), v in reticle_prev.items()}
        # the top bar and log stay put while the map slides beneath them
        lines = _render_lines(shifted, reticle_prev,
                              text=_last_frame.get("text_overlay"))
        head, foot = _last_frame.get("header"), _last_frame.get("footer")
        parts = ([head] if head is not None else []) + list(lines)
        if foot is not None:
            parts.append(foot)
        return "\n".join(parts)

    now = time.time()
    lat, lon = center
    bbox = bbox_for(lat, lon, zoom, graph_w, height_cells)
    basemap = _get_basemap(bbox, graph_w, height_cells)
    aircraft, trails, updated, source, error = feed.snapshot()
    if not show_ground:
        aircraft = [ac for ac in aircraft
                    if not ac["ground"] or ac["hex"] == selected]
    total = len(aircraft)
    if aircraft_filter is not None:
        aircraft = [ac for ac in aircraft
                    if ac["hex"] == selected or aircraft_filter(ac)]

    # weather underlay: keep the poller aimed at the live view, but only paint
    # a frame that was fetched for exactly this window (else it'd be misaligned)
    echo = None
    wx_label = wx_error = None
    if show_weather and weather is not None:
        weather.set_view(bbox, graph_w, height_cells)
        snap = weather.snapshot()
        wx_label, wx_error = snap[4], snap[7]
        frame = weather.frame()
        if frame is not None:
            echo = _echo_for(frame, bbox, graph_w, height_cells)

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
    mid = (minlon + maxlon) / 2
    placed = []
    for ac in aircraft:
        alat, alon = extrapolate(ac, now)
        if not minlon <= alon <= maxlon:
            # a view across the antimeridian runs past ±180: use the copy
            # of the longitude that falls on screen
            alon = mid + (alon - mid + 180.0) % 360.0 - 180.0
        if not (minlon <= alon <= maxlon and minlat <= alat <= maxlat):
            continue
        col = int((alon - minlon) / (maxlon - minlon) * graph_w)
        row = int((maxlat - alat) / (maxlat - minlat) * height_cells)
        d2 = (col - graph_w / 2) ** 2 + ((row - height_cells / 2) * 2) ** 2
        placed.append((d2, ac, alat, alon, col, row))
    # centre-out, so central traffic gets labels first — the selected
    # flight before all of it
    placed.sort(key=lambda p: (p[1]["hex"] != selected, p[0]))

    blip_cells = {(p[4], p[5]) for p in placed
                  if 0 <= p[4] < graph_w and 0 <= p[5] < height_cells}
    if show_trails:
        if lines_geo:
            # the game: its strokes are already on fx, and its traffic is
            # re-simulated every tick, so there is nothing to keep
            for p in placed:
                _draw_trail(fx, basemap, trails.get(p[1]["hex"], ()),
                            blip_color(p[1]), now)
        else:
            fx = _trail_layer(basemap, placed, trails, updated, now)
    if route_geo:
        for points, rcolor, dotted in route_geo:
            _draw_geo_path(fx, basemap, points, rcolor, dotted)
    sel = next((p for p in placed if p[1]["hex"] == selected), None)
    if sel is not None:
        _draw_trail(fx, basemap, trails.get(selected, ()), blip_color(sel[1]),
                    now, bright=True)
    mode = label_mode or ("full" if show_labels else "off")
    # auto: as many full data blocks as the screen has room to read —
    # about one per ninety cells — handed out centre-out, so a busy
    # metro keeps a legible middle and thins to bare blips at the edges
    budget = (graph_w * height_cells) // 90 if mode == "auto" else None
    labelled = 0
    air = {}
    for _d2, ac, alat, alon, col, row in placed:
        color = blip_color(ac)
        _draw_leader(fx, basemap, ac, alat, alon, color)
        is_sel = ac["hex"] == selected
        if ac["ground"] and not is_sel:
            continue  # ground targets: dim blip only, no data block
        if mode == "off" and not is_sel:
            continue  # labels off: bare glyphs, the scope stripped to blips
        if budget is not None and labelled >= budget and not is_sel:
            continue
        # declutter: a data block draws only where it fits whole — try the
        # right of the blip, then flip left, then the rows above and below
        # (the way a STARS leader swings through the quadrants), so a knot
        # of traffic sheds labels outward instead of just going mute
        label = (ac["callsign"] if mode == "callsign" and not is_sel
                 else data_block(ac))
        for c0, r0 in ((col + 1, row), (col - len(label), row),
                       (col + 1, row - 1), (col - len(label), row - 1),
                       (col + 1, row + 1), (col - len(label), row + 1)):
            cells = [(c0 + i, r0) for i in range(len(label))]
            # a block keeps a column of air either side of it, so two
            # neighbours never run together into one long word
            if all(0 <= c < graph_w and 0 <= r < height_cells
                   and (c, r) not in air and (c, r) not in blip_cells
                   for c, r in cells) and \
                    (c0 - 1, r0) not in air and (c0 + len(label), r0) not in air:
                labelled += 1
                for i, (c, r) in enumerate(cells):
                    head = i < len(ac["callsign"])
                    if is_sel:
                        air[(c, r)] = (label[i], color if head else MUTED,
                                       None, head)
                    else:
                        air[(c, r)] = (label[i], color if head else MUTED)
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
            if ac["hex"] == selected:
                air[(col, row)] = (blip_glyph(ac), blip_color(ac), SELECT_BG,
                                   True)
            else:
                air[(col, row)] = (blip_glyph(ac), blip_color(ac))

    overlays = dict(basemap.airport_overlays())
    airport_hits = basemap.airport_hits()
    if airport_focus:
        overlays.update(_airport_focus(basemap, airport_focus, overlays,
                                       airport_hits))
        airport_hits = basemap.airport_hits() + airport_hits
    # game pins (sector fixes, procedure names): glyph + as much of the label
    # as fits, under the traffic — a data block always outranks a fix name.
    # A pin may carry ``meta``: a colour for the label text (so an arrival
    # and a departure read apart at a glance instead of both going grey), a
    # row offset (so a procedure can sit under the corner post it feeds), and
    # a key the pointer can ask about.
    pin_hits = []
    if pins:
        for pin in pins:
            plat, plon, glyph, pcolor, plabel = pin[:5]
            meta = pin[5] if len(pin) > 5 else None
            if not (minlon <= plon <= maxlon and minlat <= plat <= maxlat):
                continue
            pcol = int((plon - minlon) / (maxlon - minlon) * graph_w)
            prow = int((maxlat - plat) / (maxlat - minlat) * height_cells)
            prow += (meta or {}).get("row", 0)
            if not (0 <= pcol < graph_w and 0 <= prow < height_cells):
                continue
            lcolor = (meta or {}).get("label_color") or DIM
            key = (meta or {}).get("key")
            if glyph:
                overlays[(pcol, prow)] = (glyph, pcolor)
                pin_hits.append((pcol, prow, key))
            for i, ch in enumerate(plabel or ""):
                cell = (pcol + (2 if glyph else 0) + i, prow)
                if (cell[0] >= graph_w or cell in overlays
                        or cell in blip_cells):
                    break
                overlays[cell] = (ch, lcolor)
                pin_hits.append((cell[0], cell[1], key))
    overlays.update(air)

    # reticle: range rings + crosshair, stamped over the map last. Anchored
    # at the view centre by default (a drag slews the scope head under them);
    # with rings_at they anchor to that fixed point instead and ride the map.
    # Rings yield to text (blips/labels); the crosshair beats scenery but
    # yields to traffic — a data block passing over the anchor stays whole.
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
                pad = [(col - 1, row), (col + 2, row)]
                if (all(0 <= c < graph_w and 0 <= r < height_cells
                        and (c, r) not in overlays for c, r in cells)
                        and all(p not in overlays for p in pad)):
                    for (c, r), ch in zip(cells, label):
                        reticle[(c, r)] = (ch, RING)
            # range: each ring wears its radius in miles, a dim scale marching
            # down the south spoke, so after a zoom the picture still says how
            # far apart things are — three miles has to mean something on sight.
            # The number sits just outside its ring and yields to traffic.
            spacing = _ring_spacing_nm(zoom)
            for k in range(1, 8):
                r_nm = spacing * k
                if r_nm > zoom * 45:
                    break
                dlat, dlon = advance(rlat, rlon, 180.0, r_nm)
                dx, dy = _project(dlon, dlat, bbox, graph_w, height_cells)
                label = f"{int(round(r_nm))}"
                col, row = int(dx) - len(label) // 2, int(dy) + 1
                cells = [(col + i, row) for i in range(len(label))]
                # a scale number is the quietest thing on the scope: it
                # yields not just to text on its own cells but to text one
                # cell away in any direction, so it never brushes a fix name
                pad = ([(col - 1, row), (col + len(label), row)]
                       + [(c, row - 1) for c, _r in cells]
                       + [(c, row + 1) for c, _r in cells])
                if (all(0 <= c < graph_w and 0 <= row < height_cells
                        and (c, row) not in overlays
                        and (c, row) not in reticle for c, _r in cells)
                        and all(p not in overlays for p in pad)):
                    for (c, _r), ch in zip(cells, label):
                        reticle[(c, row)] = (ch, RING)
    if (0 <= hcol < graph_w and 0 <= hrow < height_cells
            and (hcol, hrow) not in air):
        reticle[(hcol, hrow)] = ("+", MARKER)

    # pointer focus: nearest blip within a few cells of the mouse
    focused = focused_pin = None
    if mouse_pos is not None and any(
            c0 <= mouse_pos[0] <= c1 and r0 <= mouse_pos[1] <= r1
            for c0, r0, c1, r1 in blocked or ()):
        mouse_pos = None      # over a panel: the panel has the pointer
    if mouse_pos is not None:
        mcol, mrow = mouse_pos[0] - 1, mouse_pos[1] - 2  # screen → cell
        if placed:
            best = min(placed, key=lambda p: (p[4] - mcol) ** 2
                       + ((p[5] - mrow) * 2) ** 2)
            if abs(best[4] - mcol) <= 4 and abs(best[5] - mrow) <= 2:
                focused = best[1]
        if focused is None:
            # nothing under the pointer but scenery: a fix or a procedure
            # name will answer for itself.  Traffic always outranks it.
            for hcol_, hrow_, key in pin_hits:
                if key is not None and hrow_ == mrow and abs(hcol_ - mcol) <= 1:
                    focused_pin = key
                    break

    grid = compose(basemap, fx, overlays, graph_w, height_cells, echo)

    airborne = sum(1 for ac in aircraft if not ac["ground"])
    ground = len(aircraft) - airborne
    counts = f"✈ {airborne}" if updated else "✈ …"
    if updated and aircraft_filter is not None:
        counts = f"✈ {len(aircraft)} of {total}"
    elif updated and ground and show_ground:
        counts += f" · {ground} gnd"
    sweep = getattr(feed, "sweep", None)
    if sweep:
        counts += f" · sweeping {sweep[0]}/{sweep[1]}"
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
        elif (fetching and fetch_started and now - fetch_started > 5
              and not sweep):
            status += f" · still fetching… {now - fetch_started:.0f}s"

    place = header_note if header_note else _place(lat, lon)
    if game_footer is not None:
        # the top bar floats over the map too: build it as coloured segments
        # and knock it into the top row alongside the log, so the scope shows
        # edge to edge and only the gap past the text keeps its braille
        header = None
        text_overlay = _footer_overlay(game_footer[1](focused),
                                       graph_w, height_cells)
        _line_overlay(text_overlay,
                      [("⬤ blips", MARKER, True),
                       (f"  {place}", MUTED),
                       (f"  {counts} · {status}", DIM)],
                      0, graph_w, 1.0)
    else:
        text_overlay = None

        extra = "".join(f"  {fg(*rgb)}{text}{RESET}"
                        for text, rgb in header_extra or ())

        def _header(place_str):
            return (f"{fg(*MARKER)}{BOLD}⬤ blips{RESET}  {fg(*MUTED)}"
                    f"{place_str}{RESET}  {fg(*DIM)}{counts} · {status}{RESET}"
                    f"{extra}")

        header = _header(place)
        over = visible_len(header) - cols
        if over > 0 and len(place) > over + 1:
            header = _header(place[:len(place) - over - 1] + "…")
        header += " " * max(0, cols - visible_len(header))

    # the game floats its top bar and log over the map (text_overlay), so it
    # has no header or footer row of its own — only the live feed builds those
    foot = None
    if game_footer is None:
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
        ring_note = f"rings {_ring_spacing_nm(zoom)} nm · " if show_rings else ""
        hint_text = footer_hint if footer_hint is not None else (
            "+/- zoom · drag pan · t trails · r rings · g ground · "
            "w weather · q quit")
        hint = (f"{fg(*DIM)}{ring_note}{hint_text}{RESET}"
                if sys.stdout.isatty() else "")
        for foot in (f"{left}  {hint}", f"{left}", ""):
            if visible_len(foot) <= cols:
                break
    if game_footer is None:
        foot += " " * max(0, cols - visible_len(foot))

    map_lines = _render_lines(grid, reticle, text=text_overlay)

    # cache this clean frame so a live drag can shift the map whole and
    # re-stamp the (fixed) reticle over it (see the drag fast-path up top);
    # blip screen positions ride along so a click can name its aircraft
    _last_frame.clear()
    _last_frame.update(grid=grid, reticle=reticle, header=header, footer=foot,
                       text_overlay=text_overlay,
                       gw=graph_w, hc=height_cells,
                       geo_reticle=rings_at is not None,
                       hits=[(p[4], p[5], p[1]["callsign"], p[1]["hex"])
                             for p in placed],
                       airports=airport_hits, bbox=bbox,
                       header_rows=0 if game_footer is not None else 1)

    parts = ([header] if header is not None else []) + list(map_lines)
    if foot is not None:
        parts.append(foot)
    out = "\n".join(parts)
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
    elif focused_pin is not None and pin_card is not None:
        card = pin_card(focused_pin)
        if card:
            out += "\x00" + _chip_overlay(card, mouse_pos, cols, rows)
    return out


def _hit(screen_col, screen_row):
    """The nearest blip record (col, row, callsign, hex) to a screen cell,
    or None — same tolerance as hover focus (a couple of rows, a few
    columns: cells are tall)."""
    hits = _last_frame.get("hits")
    if not hits:
        return None
    mcol, mrow = screen_col - 1, screen_row - 2  # screen → cell coords
    best = min(hits, key=lambda h: (h[0] - mcol) ** 2
               + ((h[1] - mrow) * 2) ** 2)
    if abs(best[0] - mcol) <= 4 and abs(best[1] - mrow) <= 2:
        return best
    return None


def hit_test(screen_col, screen_row):
    """Callsign of the blip near a screen cell, or None (game click-to-hail)."""
    best = _hit(screen_col, screen_row)
    return best[2] if best else None


def hit_aircraft(screen_col, screen_row):
    """Hex of the blip near a screen cell, or None."""
    best = _hit(screen_col, screen_row)
    return best[3] if best else None


def hit_airport(screen_col, screen_row):
    """ICAO code of the airport label under a screen cell, or None."""
    mcol = screen_col - 1
    mrow = screen_row - 1 - _last_frame.get("header_rows", 1)
    for col, row, width, icao in _last_frame.get("airports") or ():
        if row == mrow and col - 1 <= mcol <= col + width:
            return icao
    return None


def last_view():
    """(bbox, graph_w, height_cells, header_rows) of the last full frame."""
    f = _last_frame
    if "bbox" not in f:
        return None
    return f["bbox"], f["gw"], f["hc"], f.get("header_rows", 1)


def main():
    args = blips_parser().parse_args()
    if args.debug:
        from blips._runtime import set_debug
        set_debug(True)

    if args.game is not None:
        from blips.game.app import main as game_main
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
    if live:
        from blips._app import main as app_main
        app_main(args, lat, lon)
        return

    zoom = max(0.08, min(60.0, args.zoom))
    feed = Feed()
    cols, rows = get_terminal_size()
    view = bbox_for(lat, lon, zoom, max(20, cols), max(8, rows - 2))
    feed.set_view(lat, lon, view_radius_nm(view, lat, lon), view)
    weather = WeatherFeed(lat, lon, theme=_wx_theme(args))
    try:
        feed.poll_once()
    except Exception as exc:
        feed.error = str(exc)
    if args.weather:
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 2)
        weather.set_enabled(True)
        weather.set_view(bbox_for(lat, lon, zoom, gw, hc), gw, hc)
        try:
            weather.poll_once()
        except Exception as exc:
            weather.error = str(exc)
    print(render_scope([lat, lon], zoom, feed, playing=False,
                       weather=weather, show_weather=bool(args.weather)))


def _wx_theme(args):
    from blips._radar_sources import theme_id
    return theme_id(args.wx_theme)


if __name__ == "__main__":
    main()
