"""The live scope as an app: a camera, a selection, search, cards, filters.

``ScopeApp`` is the ``LiveApp`` that ``blips`` runs.  The renderer
(``scope.render_scope``) draws a frame from what this holds:

  - the camera (``_camera.Camera``): the wheel zooms about the pointer,
    + and - zoom about the centre, the arrows pan, a drag pulls the map,
    and every move eases instead of jumping;
  - the selection: click a blip (or Tab through them) and its card pins to
    the corner — airline, type, route with progress, altitude, speed, the
    winds it's flying through — its route is drawn as great-circle arcs,
    and ``f`` follows it.  Click an airport's label for its card instead:
    its weather, the runways seen in use, and its arrivals and departures;
  - search (``/``): a field over the map, results as you type
    (``_search.Search``), Enter to go — a flight anywhere in the world
    flies the camera there and follows it;
  - filters: chosen from search ("Show only Delta Air Lines"), named in
    the header, cleared with ``x``;
  - help (``?``).

Background work — the feed, routes, weather, METARs, worldwide lookups,
a geocode — never blocks a frame; it lands and nudges the loop.
"""

import math
import threading
import time

from blips import _names
from blips._airport import Metars, board, metar_summary, runways_in_use
from blips._airports import find_airport, nearest_airport
from blips._camera import Camera, bbox_for
from blips._feeds import Feed, WeatherFeed
from blips._framebuffer import get_terminal_size, visible_len
from blips._geo import bearing_to, great_circle, haversine_nm
from blips._live import LiveApp, nudge, overlay
from blips._panels import bar, fit_width, panel
from blips._routes import RouteLookup
from blips._search import Search
from blips._theme import ensure_contrast
from blips import scope
from blips.scope import (
    ALERT, CHIP_BG, COMPASS, DIM, MARKER, MUTED, blip_color, extrapolate,
    render_scope, trend_arrow,
)

TEXT = (214, 219, 233)
GOOD = (140, 210, 110)
ROUTE_FLOWN = (120, 150, 190)
ROUTE_AHEAD = (190, 200, 225)
SEEN_KEEP_S = 1800.0     # how long a flight seen earlier stays searchable
LOST_S = 60.0            # a selected flight unseen this long: "signal lost"
ZOOM_STEP = 1.5          # + and -
WHEEL_STEP = 1.25        # a wheel notch
LABEL_MODES = ("auto", "full", "callsign", "off")
STAR_INK = (72, 150, 160)     # arrivals, as the game paints them: teal
SID_INK = (170, 140, 90)      # departures: a dim amber
PROC_ZOOM = 4.0               # wider than this, a field's procedures are a knot


def _geometry():
    cols, rows = get_terminal_size()
    gw, hc = max(20, cols), max(8, rows - 2)
    return cols, rows, gw, hc


def view_radius_nm(bbox, lat, lon):
    """The feed radius that covers the window, uncapped: past the
    aggregators' 250 nm the feed sweeps in tiles."""
    corner = haversine_nm(lat, lon, bbox[3], bbox[0])
    return max(10.0, corner * 1.1)


class ScopeApp(LiveApp):
    interval = 5.0
    play_interval = 0.5
    hover_idle = 7.0

    def __init__(self, lat, lon, zoom, wx_theme=None, weather_on=True):
        self.home = (lat, lon, zoom)
        self.camera = Camera(lat, lon, zoom)
        self.feed = Feed(nudge=True)
        self.routes = RouteLookup(nudge=True, persist=True)
        self.weather = WeatherFeed(lat, lon, theme=wx_theme, nudge=True)
        self.metars = Metars(on_update=nudge)
        self.search = Search(on_update=nudge)
        self.search_open = False
        self.results = []
        self.selected = None      # hex of the flight on the card
        self.sel_ac = None        # its last record, wherever it came from
        self.sel_seen = 0.0       # wall time it was last in the feed
        self.follow = False
        self.airport = None       # airport dict on the card
        self.filters = []
        self.toggles = {"t": True, "r": True, "g": True, "w": weather_on}
        self.label_mode = "auto"
        self.help = False
        self.toast = None         # (text, until)
        self.seen = {}            # hex → record, for search
        self.drag_preview = None
        self.mouse_pos = None
        self.playing = True
        self._feed_aim = None     # (lat, lon, zoom, gw, hc) last aimed
        self._last_update = None
        self._card_rows = {}      # screen row → hex, on the airport card
        self._panel_rects = []
        self._search_rect = None
        self._result_rows = {}    # screen row → result index
        self._procs = None        # (key, overlay) for the airport on the card
        self._alerted = set()     # hexes whose emergency has been announced
        self._lock = threading.Lock()

    # -- running ---------------------------------------------------------------
    def run(self):
        self.feed.start()
        self.weather.start()
        if self.toggles["w"]:
            self.weather.set_enabled(True)
        self._aim_feed(force=True)
        try:
            super().run()
        finally:
            self.routes.save()

    def frame_interval(self):
        if self.camera.moving:
            return 1 / 30
        waits = [self.interval]
        if self.search_open and self.search.waiting():
            waits.append(max(0.05, self.search.due_in() + 0.02))
        if self.toast is not None:
            waits.append(max(0.05, self.toast[1] - time.time()))
        return min(waits)

    def glide_interval(self):
        """Fast enough that the quickest jet moves about a braille dot per
        frame at this zoom, and no faster: 1 s wide, 0.1 s on a final."""
        if self.camera.moving:
            return 1 / 30
        _c, _r, _gw, hc = _geometry()
        dot_nm = self.camera.zoom * 60.0 / (hc * 4)
        return max(0.1, min(1.0, dot_nm / 0.14, self.frame_interval()))

    # -- the view ----------------------------------------------------------------
    def _bbox(self):
        _c, _r, gw, hc = _geometry()
        return bbox_for(self.camera.lat, self.camera.lon, self.camera.zoom,
                        gw, hc), gw, hc

    def _aim_feed(self, force=False):
        """Point the feed at the view — when it has moved a quarter of its
        radius or zoomed a quarter, not on every frame of a glide."""
        if self.camera.moving and not force:
            return
        bbox, gw, hc = self._bbox()
        lat, lon, zoom = self.camera.lat, self.camera.lon, self.camera.zoom
        aim = self._feed_aim
        radius = view_radius_nm(bbox, lat, lon)
        if (not force and aim is not None and (gw, hc) == aim[3:]
                and abs(math.log(zoom / aim[2])) < 0.22
                and haversine_nm(lat, lon, aim[0], aim[1]) < radius * 0.25):
            return
        self._feed_aim = (lat, lon, zoom, gw, hc)
        self.feed.set_view(lat, lon, radius, bbox)

    def _follow_step(self):
        """Keep the followed flight under the crosshair, moving the map a
        whole cell at a time so the geography doesn't shimmer."""
        if not (self.follow and self.sel_ac) or self.camera.moving:
            return
        lat, lon = extrapolate(self.sel_ac, time.time())
        _c, _r, gw, hc = _geometry()
        bbox = bbox_for(self.camera.lat, self.camera.lon, self.camera.zoom,
                        gw, hc)
        cell_lat = (bbox[3] - bbox[1]) / hc
        cell_lon = (bbox[2] - bbox[0]) / gw
        dlat = round((lat - self.camera.lat) / cell_lat) * cell_lat
        dlon = round(((lon - self.camera.lon + 180) % 360 - 180) / cell_lon) \
            * cell_lon
        if dlat or dlon:
            self.camera.jump(self.camera.lat + dlat, self.camera.lon + dlon)

    # -- data from the feed --------------------------------------------------------
    def _on_feed(self, aircraft, updated):
        """Once per poll: remember what was seen, queue its routes, and
        refresh the selected flight's record."""
        if updated == self._last_update:
            return
        self._last_update = updated
        now = time.time()
        for ac in aircraft:
            self.seen[ac["hex"]] = ac
        if len(self.seen) > 6000 or now % 60 < 6:
            self.seen = {h: a for h, a in self.seen.items()
                         if now - a["fix_time"] < SEEN_KEEP_S}
        self.routes.want(a for a in aircraft if not a["ground"])
        self._announce(aircraft)
        if self.selected:
            hit = next((a for a in aircraft if a["hex"] == self.selected), None)
            if hit is not None:
                self.sel_ac = hit
                self.sel_seen = now

    SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "emergency"}

    def _announce(self, aircraft):
        """A toast the first time a flight in the feed declares: squawk
        7500, 7600 or 7700, or the emergency field set."""
        for ac in aircraft:
            if ac["ground"] or ac["hex"] in self._alerted:
                continue
            sq = ac.get("squawk")
            if sq in self.SQUAWKS or ac.get("emergency"):
                self._alerted.add(ac["hex"])
                what = self.SQUAWKS.get(sq, "emergency")
                code = f"squawking {sq}" if sq in self.SQUAWKS else "declaring"
                self.flash(f"⚠ {ac['callsign']} {code} — {what} · / and type "
                           f"{ac['callsign']} to find it", 10.0)

    def _known_routes(self):
        return self.routes.known()

    # -- selection ------------------------------------------------------------------
    def select(self, ac, follow=False, fly=False):
        self.selected = ac["hex"]
        self.sel_ac = ac
        self.sel_seen = time.time()
        self.airport = None
        self.follow = follow
        self.feed.set_pins({ac["hex"]})
        if ac.get("flight") or ac.get("callsign"):
            self.routes.get(ac.get("flight") or ac["callsign"], ac["lat"],
                            ac["lon"])
        if fly:
            zoom = min(self.camera.target[2], 2.0)
            self.camera.fly_to(ac["lat"], ac["lon"], zoom)

    def deselect(self):
        self.selected = self.sel_ac = None
        self.follow = False
        self.feed.set_pins(())

    def open_airport(self, ap, fly=False):
        self.deselect()
        self.airport = ap
        self.metars.get(ap["icao"])
        if fly:
            self.camera.fly_to(ap["lat"], ap["lon"],
                               min(self.camera.target[2], 1.2))

    def _cycle(self, step):
        """Tab: select the next flight out from the centre."""
        aircraft = self._visible()
        if not aircraft:
            return False
        lat, lon = self.camera.lat, self.camera.lon
        # airborne first: Tab at an airport shouldn't start on the gates
        aircraft.sort(key=lambda a: (a["ground"],
                                     haversine_nm(lat, lon, a["lat"], a["lon"])))
        hexes = [a["hex"] for a in aircraft]
        i = hexes.index(self.selected) + step if self.selected in hexes else 0
        self.select(aircraft[i % len(aircraft)])
        return True

    def _visible(self):
        aircraft = self.feed.snapshot()[0]
        bbox, _gw, _hc = self._bbox()
        test = self._filter_fn()
        return [a for a in aircraft
                if bbox[0] <= a["lon"] <= bbox[2] and bbox[1] <= a["lat"] <= bbox[3]
                and (self.toggles["g"] or not a["ground"])
                and (test is None or test(a))]

    def _filter_fn(self):
        if not self.filters:
            return None
        routes = self._known_routes()

        def test(ac):
            route = routes.get((ac.get("flight") or ac["callsign"]).upper())
            return all(f.test(ac, route) for f in self.filters)
        return test

    # -- input ------------------------------------------------------------------------
    def text_mode(self):
        return self.search_open

    def intercept(self, action):
        if self.help:
            # any key closes help; the closing keys stop there, the rest
            # go on to do what they do
            self.help = False
            return action in ("escape", "key:?", "reset", "key:enter")
        if not self.search_open:
            return False
        if action.startswith("char:"):
            self.search.type(action[5:])
        elif action == "key:backspace":
            if not self.search.query:
                self.search_open = False
            self.search.backspace()
        elif action == "key:kill":
            self.search.set_query("")
        elif action == "key:ctrl-w":
            self.search.kill_word()
        elif action in ("down", "tab"):
            self.search.move(1, len(self.results))
        elif action in ("up", "shift-tab"):
            self.search.move(-1, len(self.results))
        elif action == "key:enter":
            if self.results:
                self.choose(self.results[min(self.search.cursor,
                                             len(self.results) - 1)])
        elif action == "escape":
            self.search_open = False
        elif action == "quit":
            return False
        return True

    def choose(self, result):
        """Do what a search result means."""
        self.search_open = False
        if result.kind == "flight":
            self.select(result.ac, follow=True, fly=True)
        elif result.kind == "airport":
            self.open_airport(result.airport, fly=True)
        elif result.kind == "filter":
            self.filters = [f for f in self.filters if f != result.filter]
            self.filters.append(result.filter)
            self.flash(f"showing only {result.filter.label} · x clears")
        elif result.kind == "place":
            self._geocode(result.query)
        else:
            self.search_open = True     # a note: nothing to do, keep looking

    def _geocode(self, query):
        self.flash(f"finding {query}…", 8.0)

        def run():
            from blips._location import geocode_place
            hit = geocode_place(query)
            if hit is None:
                self.flash(f"no place called {query}")
            else:
                self.camera.fly_to(hit[0], hit[1], min(self.camera.zoom, 2.0))
                self.flash(hit[2])
            nudge()

        threading.Thread(target=run, daemon=True, name="geocode").start()

    def flash(self, text, seconds=3.0):
        self.toast = (text, time.time() + seconds)

    def on_action(self, key):
        _c, _r, gw, hc = _geometry()
        if key in ("+", "="):
            return self.camera.zoom_by(1 / ZOOM_STEP)
        if key in ("-", "_"):
            return self.camera.zoom_by(ZOOM_STEP)
        if key in ("up", "down", "left", "right"):
            self.follow = False
            fx, fy = {"up": (0, -0.25), "down": (0, 0.25),
                      "left": (-0.25, 0), "right": (0.25, 0)}[key]
            return self.camera.pan_by(fx, fy, gw, hc)
        if key == "/":
            self.search_open = True
            self.search.set_query("")
            return True
        if key == "?":
            self.help = True
            return True
        if key == "escape":
            if self.selected:
                self.deselect()
            elif self.airport:
                self.airport = None
            elif self.filters:
                self.filters = []
            else:
                return False
            return True
        if key in ("tab", "shift-tab"):
            return self._cycle(1 if key == "tab" else -1)
        k = key.lower() if len(key) == 1 else key
        if k == "f":
            if not self.selected:
                self.flash("select a flight first: click one, or Tab")
                return True
            self.follow = not self.follow
            return True
        if k == "c":
            if self.sel_ac:
                self.camera.fly_to(*extrapolate(self.sel_ac, time.time()))
            elif self.airport:
                self.camera.fly_to(self.airport["lat"], self.airport["lon"])
            else:
                self.camera.fly_to(*self.home)
            return True
        if k == "h":
            self.follow = False
            self.camera.fly_to(*self.home)
            return True
        if k == "a":
            ap = self._nearest_airport()
            if ap is not None:
                self.open_airport(ap)
            return True
        if k == "x":
            if self.filters:
                self.filters = []
                self.flash("filters cleared")
            return True
        if k == "l":
            i = LABEL_MODES.index(self.label_mode)
            self.label_mode = LABEL_MODES[(i + 1) % len(LABEL_MODES)]
            self.flash({"auto": "labels: as many as stay readable",
                        "full": "labels: every flight",
                        "callsign": "labels: callsign only",
                        "off": "labels off"}[self.label_mode])
            return True
        if k in self.toggles:
            self.toggles[k] = not self.toggles[k]
            if k == "w":
                self.weather.set_enabled(self.toggles["w"])
            return True
        return False

    def _nearest_airport(self):
        lat, lon = self.camera.lat, self.camera.lon
        ap = nearest_airport(lat, lon, large_only=True)
        near = nearest_airport(lat, lon, large_only=False)
        if near and ap and haversine_nm(lat, lon, near["lat"], near["lon"]) \
                < 0.3 * haversine_nm(lat, lon, ap["lat"], ap["lon"]):
            return near
        return ap

    def on_wheel(self, direction, col, row):
        _c, _r, gw, hc = _geometry()
        factor = 1 / WHEEL_STEP if direction > 0 else WHEEL_STEP
        if self.search_open and self._in(self._search_rect, col, row):
            return bool(self.intercept("up" if direction > 0 else "down"))
        if self.follow:
            return self.camera.zoom_by(factor)     # about the flight
        return self.camera.zoom_by(factor, at=(col - 1, row - 2), gw=gw, hc=hc)

    def on_drag(self, dcol, drow, done):
        if not done:
            self.drag_preview = (dcol, drow) if (dcol or drow) else None
            return bool(dcol or drow)
        self.drag_preview = None
        if not (dcol or drow):
            return False
        _c, _r, gw, hc = _geometry()
        bbox, gw, hc = self._bbox()
        lon_span = bbox[2] - bbox[0]
        self.follow = False
        self.camera.jump(self.camera.lat + drow * self.camera.zoom / hc,
                         self.camera.lon - dcol * lon_span / gw)
        return True

    @staticmethod
    def _in(rect, col, row):
        return rect is not None and rect[0] <= col <= rect[2] \
            and rect[1] <= row <= rect[3]

    def on_click(self, col, row):
        if self.help:
            self.help = False
            return True
        if self.search_open:
            if col and row in self._result_rows and \
                    self._in(self._search_rect, col, row):
                self.choose(self.results[self._result_rows[row]])
                return True
            if not self._in(self._search_rect, col, row):
                self.search_open = False
                return True
            return False
        hexid = self._card_rows.get(row)
        if hexid is not None and any(self._in(r, col, row)
                                     for r in self._panel_rects):
            ac = next((a for a in self.feed.snapshot()[0] if a["hex"] == hexid),
                      None) or self.seen.get(hexid)
            if ac:
                self.select(ac, fly=True)
                return True
        if any(self._in(r, col, row) for r in self._panel_rects):
            return False
        hexid = scope.hit_aircraft(col, row)
        if hexid:
            ac = next((a for a in self.feed.snapshot()[0] if a["hex"] == hexid),
                      None)
            if ac is not None:
                if hexid == self.selected:
                    self.follow = not self.follow
                else:
                    self.select(ac)
                return True
        icao = scope.hit_airport(col, row)
        if icao:
            ap = find_airport(icao)
            if ap is not None:
                self.open_airport(ap)
                return True
        if self.selected or self.airport:
            self.deselect()
            self.airport = None
            return True
        return False

    # -- drawing ------------------------------------------------------------------------
    def render(self, mouse_pos=None, playing=True, **_):
        self.mouse_pos = mouse_pos
        self.playing = playing
        self.camera.step()
        aircraft, _trails, updated, _src, _err = self.feed.snapshot()
        self._on_feed(aircraft, updated)
        self._follow_step()
        self._aim_feed()
        if self.toast is not None and time.time() > self.toast[1]:
            self.toast = None
        cols, rows, gw, hc = _geometry()
        routes = self._known_routes()
        if self.search_open:
            self.results = self.search.results(
                aircraft, list(self.seen.values()), routes,
                centre=(self.camera.lat, self.camera.lon))
            self.search.cursor = min(self.search.cursor,
                                     max(0, len(self.results) - 1))

        procs = self._procedures(aircraft)
        proc_paths, proc_pins = [], []
        if procs and self.camera.zoom <= PROC_ZOOM:
            for kind, _name, pts in procs["paths"]:
                proc_paths.append((pts, STAR_INK if kind == "STAR" else SID_INK,
                                   True))
            for lat, lon, name, kind, _vec in procs["labels"]:
                ink = STAR_INK if kind == "STAR" else SID_INK
                proc_pins.append((lat, lon, "", ink, name,
                                  {"label_color": ink, "key": ("proc", name)}))
        floating, rects = self._panels(cols, rows, aircraft, routes)
        self._panel_rects = rects
        frame = render_scope(
            (self.camera.lat, self.camera.lon), self.camera.zoom, self.feed,
            playing=playing, mouse_pos=mouse_pos,
            show_trails=self.toggles["t"], show_rings=self.toggles["r"],
            show_ground=self.toggles["g"], weather=self.weather,
            show_weather=self.toggles["w"], drag_offset=self.drag_preview,
            routes=self.routes, hover_card=self._hover_lines,
            selected=self.selected,
            route_geo=(self._route_geo() or []) + proc_paths,
            pins=proc_pins, pin_card=self._pin_card,
            aircraft_filter=self._filter_fn(), label_mode=self.label_mode,
            airport_focus=self.airport["icao"] if self.airport else None,
            header_extra=self._header_extra(),
            footer_hint=("/ search · click select · f follow · scroll zoom · "
                         "l labels · ? help · q quit",
                         "/ search · ? help · q quit"),
            blocked=rects)
        body, _, chip = frame.partition("\x00")
        return overlay(body, chip + floating,
                       motion=False if self.search_open else None)

    def _runways(self, aircraft):
        """(landing, departing) runway idents at the card's airport: the
        ones traffic is seen using, else the end facing into the METAR's
        wind for both, else (None, None)."""
        ap = self.airport
        near = [a for a in aircraft
                if haversine_nm(ap["lat"], ap["lon"], a["lat"], a["lon"]) < 15]
        landing, departing = runways_in_use(ap, near)
        land = max(landing, key=landing.get) if landing else None
        dep = max(departing, key=departing.get) if departing else None
        if land and dep:
            return land, dep
        report = self.metars.get(ap["icao"])
        wdir = report.get("wdir") if report else None
        if isinstance(wdir, (int, float)):
            ends = [(abs((e[1] - wdir + 180) % 360 - 180), e[0])
                    for r in ap.get("rwys") or () for e in (r.get("le"), r.get("he"))
                    if e and e[1] is not None]
            if ends:
                into = min(ends)[1]
                return land or into, dep or into
        return land or dep, dep or land

    def _procedures(self, aircraft):
        """The SIDs and STARs serving the card airport's runways in use,
        from the game's vendored FAA procedures — or None (no airport on
        the card, no procedures on file, no runway known)."""
        ap = self.airport
        if not ap:
            return None
        try:
            from blips.game.procedures import overlay_for, procedures_for
        except Exception:
            return None
        if not procedures_for(ap["icao"]):
            return None
        land, dep = self._runways(aircraft)
        if not (land or dep):
            return None
        key = (ap["icao"], land, dep)
        if self._procs is None or self._procs[0] != key:
            out = {"paths": [], "labels": [], "plans": []}
            for rwy, kinds in ((land, ("STAR",)), (dep, ("SID",))):
                if not rwy:
                    continue
                ov = overlay_for(ap, rwy, kinds=kinds, declutter=False,
                                 radius_nm=60.0)
                for k in out:
                    out[k] += ov[k]
            out["runways"] = (land, dep)
            self._procs = (key, out)
        return self._procs[1]

    def _star_for(self, ac):
        """The STAR an arrival is flying, by which one's track it sits on
        (within 2 nm), or None."""
        if not self._procs:
            return None
        from blips.game.procedures import _dist_to_path
        best = None
        for plan in self._procs[1]["plans"]:
            if plan["kind"] != "STAR":
                continue
            paths = [plan["spine"]] + [pts for _n, pts in plan["branches"]]
            for pts in paths:
                if len(pts) < 2:
                    continue
                d = _dist_to_path((ac["lat"], ac["lon"]),
                                  [(f[0], f[1]) for f in pts])
                if d <= 2.0 and (best is None or d < best[0]):
                    best = (d, plan["name"])
        return best[1] if best else None

    def _pin_card(self, key):
        """Chip lines for a procedure label under the pointer."""
        from blips._color import fg
        if not (isinstance(key, tuple) and key[0] == "proc" and self._procs):
            return None
        names = key[1].split("/")
        mut = fg(*ensure_contrast(MUTED, CHIP_BG, 3.0))
        lines = []
        for plan in self._procs[1]["plans"]:
            if plan["name"] not in names:
                continue
            ink = STAR_INK if plan["kind"] == "STAR" else SID_INK
            land, dep = self._procs[1]["runways"]
            rwy = land if plan["kind"] == "STAR" else dep
            what = "arrival" if plan["kind"] == "STAR" else "departure"
            lines.append(f"{fg(*ensure_contrast(ink, CHIP_BG, 3.0))}{plan['name']}"
                         f" {mut}{what} · runway {rwy}")
            fixes = " ".join(f[2] for f in plan["spine"] if f[2])
            if fixes:
                lines.append(mut + fixes)
            top, bottom = plan.get("top"), plan.get("bottom")
            if top or bottom:
                window = " – ".join(f"{v:,} ft" for v in (bottom, top) if v)
                lines.append(mut + "published " + window)
        return lines or None

    def _header_extra(self):
        out = []
        if self.follow and self.sel_ac:
            out.append((f"◎ following {self.sel_ac['callsign']}", MARKER))
        for f in self.filters:
            out.append((f"▾ {f.label}", MARKER))
        if self.filters:
            out.append(("x clears", DIM))
        return out

    def _route_geo(self):
        """The selected flight's route as arcs: flown (solid, dim) from the
        origin to where it is, still to fly (dotted, bright) on to the
        destination."""
        ac = self.sel_ac
        if not ac:
            return None
        route = self.routes.peek(ac.get("flight") or ac["callsign"])
        if not route or route[0].lat is None or route[-1].lat is None:
            return None
        lat, lon = extrapolate(ac, time.time())
        paths = []
        legs = list(route)
        # stops before the current position are flown; find where we are
        # by the leg whose far end is nearest ahead — usually just two legs
        o, d = legs[0], legs[-1]
        paths.append((great_circle(o.lat, o.lon, lat, lon), ROUTE_FLOWN, False))
        paths.append((great_circle(lat, lon, d.lat, d.lon), ROUTE_AHEAD, True))
        return paths

    # -- cards and panels ------------------------------------------------------------------
    def _panels(self, cols, rows, aircraft, routes):
        """(cursor-addressed ANSI, [rects]) for everything floating."""
        parts, rects = [], []
        self._card_rows = {}
        self._result_rows = {}
        self._search_rect = None
        card = None
        if self.sel_ac:
            card = self._flight_card(routes)
        elif self.airport:
            card = self._airport_card(aircraft, routes)
        if card:
            width = fit_width(card, 34, min(52, cols - 2))
            height = min(len(card), rows - 3)
            parts.append(panel(card[:height], 2, 1, width, CHIP_BG))
            rects.append((1, 2, width, 1 + height))
        if self.search_open:
            s, rect = self._search_panel(cols, rows)
            parts.append(s)
            rects.append(rect)
            self._search_rect = rect
        if self.help:
            s, rect = self._help_panel(cols, rows)
            parts.append(s)
            rects.append(rect)
        if self.toast is not None:
            text = f" {self.toast[0]} "
            w = min(cols - 2, visible_len(text) + 2)
            parts.append(panel([[(self.toast[0], TEXT)]], rows - 2,
                               max(1, (cols - w) // 2 + 1), w, CHIP_BG))
        return "".join(parts), rects

    def _flight_card(self, routes):
        ac = self.sel_ac
        now = time.time()
        mut = MUTED
        cs = ac.get("flight") or ac["callsign"]
        head = [(ac["callsign"], blip_color(ac), True)]
        fn = _names.flight_number(cs)
        if fn and fn != ac["callsign"]:
            head.append((f"  {fn}", mut))
        if ac.get("mil"):
            head.append(("  military", ALERT))
        lines = [head]
        who = _names.airline_of(cs)
        if who:
            lines.append([(who[1], TEXT)])
        elif ac.get("operator"):
            lines.append([(ac["operator"], TEXT)])
        craft = []
        tname = _names.type_name(ac.get("actype")) or ac.get("desc")
        if tname:
            craft.append(tname)
        if ac.get("reg") and ac["reg"] != ac["callsign"]:
            craft.append(ac["reg"])
        if ac.get("actype") and tname:
            craft.append(ac["actype"])
        elif ac.get("actype"):
            craft.append(ac["actype"])
        if craft:
            lines.append([(" · ".join(craft), mut)])

        route = self.routes.peek(cs)
        if route:
            o, d = route[0], route[-1]
            lines.append([(o[1], TEXT, True), (f" {o[0]}", mut), ("  →  ", DIM),
                          (d[1], TEXT, True), (f" {d[0]}", mut)])
            if o.lat is not None and d.lat is not None and not ac["ground"]:
                lat, lon = extrapolate(ac, now)
                flown = haversine_nm(o.lat, o.lon, lat, lon)
                left = haversine_nm(lat, lon, d.lat, d.lon)
                frac = flown / max(1.0, flown + left)
                eta = ""
                if ac.get("gs") and ac["gs"] > 60:
                    mins = left / ac["gs"] * 60
                    eta = f" · {int(mins // 60)}h {int(mins % 60):02d}m" \
                        if mins >= 60 else f" · {mins:.0f} min"
                lines.append(bar(frac, 14, ROUTE_AHEAD, DIM)
                             + [(f"  {left:,.0f} nm to go{eta}", mut)])
        elif cs and len(cs) > 3:
            pending = self.routes.pending()
            lines.append([("looking up the route…" if pending else
                           "no route on file", DIM)])

        if ac["ground"]:
            lines.append([("on the ground", TEXT)])
        elif ac.get("alt") is not None:
            alt = ac["alt"]
            alt_s = f"FL{alt / 100:03.0f}" if alt >= 18000 else f"{alt:,.0f} ft"
            row = [(alt_s, TEXT, True)]
            vr = ac.get("vrate") or 0
            if abs(vr) > 200:
                row.append((f" {trend_arrow(ac) or ('↑' if vr > 0 else '↓')}"
                            f"{abs(vr):,.0f} fpm", mut))
            sel = ac.get("sel_alt")
            if sel and abs(sel - alt) > 250:
                # ADS-B carries the dialled altitude in 16 ft steps: read
                # it back to the hundred, as the autopilot shows it
                sel = round(sel / 100) * 100
                sel_s = f"FL{sel / 100:03.0f}" if sel >= 18000 else f"{sel:,.0f}"
                row.append((f" · selected {sel_s}", mut))
            lines.append(row)
        motion = []
        if ac.get("gs"):
            motion.append(f"{ac['gs']:.0f} kt")
        if ac.get("track") is not None:
            motion.append(f"track {ac['track']:03.0f}°")
        if ac.get("mach"):
            motion.append(f"M{ac['mach']:.2f}")
        if motion:
            lines.append([(" · ".join(motion), mut)])
        air = []
        if ac.get("wind") and ac["wind"][0] is not None:
            wd, ws = ac["wind"]
            air.append(f"wind {wd:03.0f}° {ws:.0f} kt")
        if ac.get("oat") is not None:
            air.append(f"{ac['oat']:.0f}°C")
        if air:
            lines.append([(" · ".join(air), mut)])
        tail = []
        if ac.get("squawk"):
            tail.append(f"squawk {ac['squawk']}")
        tail.append("MLAT" if ac.get("mlat") else "ADS-B")
        age = now - self.sel_seen if self.sel_seen else 0
        if age > LOST_S:
            tail.append(f"signal lost {age / 60:.0f} min ago")
        else:
            d = haversine_nm(self.camera.lat, self.camera.lon,
                             ac["lat"], ac["lon"])
            tail.append(f"{d:,.0f} nm from centre")
        lines.append([(" · ".join(tail), DIM)])
        keys = "f unfollow" if self.follow else "f follow"
        lines.append([(f"{keys} · c centre · esc close", DIM)])
        return lines

    def _airport_card(self, aircraft, routes):
        ap = self.airport
        mut = MUTED
        code = ap.get("iata") or ap["icao"]
        lines = [[(code, MARKER, True), (f"  {ap['icao']}" if ap.get("iata") else "", mut)],
                 [(ap["name"], TEXT)]]
        where = [p for p in (ap.get("city"), f"{ap.get('elev', 0):,} ft") if p]
        n_rwy = len(ap.get("rwys") or ())
        if n_rwy:
            where.append(f"{n_rwy} runway{'s' if n_rwy != 1 else ''}")
        lines.append([(" · ".join(where), mut)])
        report = self.metars.get(ap["icao"])
        if report:
            lines.append([(metar_summary(report), TEXT)])
            raw = report.get("rawOb") or ""
            if raw:
                lines.append([(raw, DIM)])
        else:
            lines.append([("fetching the weather…", DIM)])
        near = [a for a in aircraft
                if haversine_nm(ap["lat"], ap["lon"], a["lat"], a["lon"]) < 15]
        landing, departing = runways_in_use(ap, near)
        if landing or departing:
            bits = []
            if landing:
                bits.append("landing " + " ".join(
                    sorted(landing, key=lambda k: -landing[k])))
            if departing:
                bits.append("departing " + " ".join(
                    sorted(departing, key=lambda k: -departing[k])))
            lines.append([("in use (seen) ", DIM), (" · ".join(bits), GOOD)])
        if self._procs and self._procs[0][0] == ap["icao"]:
            stars = sorted({p["name"] for p in self._procs[1]["plans"]
                            if p["kind"] == "STAR"})
            sids = sorted({p["name"] for p in self._procs[1]["plans"]
                           if p["kind"] == "SID"})
            if stars:
                lines.append([("arrivals ", DIM), (" ".join(stars), STAR_INK)])
            if sids:
                lines.append([("departures ", DIM), (" ".join(sids), SID_INK)])
        arrivals, departures = board(ap, aircraft, routes)
        row = 2 + len(lines)

        def entry(e, verb):
            ac, other, dist, eta = e
            when = f"{eta:.0f} min" if eta is not None and eta < 600 else ""
            alt = (f"FL{ac['alt'] / 100:03.0f}" if (ac.get("alt") or 0) >= 18000
                   else "ground" if ac.get("ground")
                   else f"{(ac.get('alt') or 0):,.0f}")
            row_ = [(f"{ac['callsign']:<9}", blip_color(ac)),
                    (f"{verb} {other[1]:<4}", mut),
                    (f" {dist:>4.0f} nm", DIM), (f"  {alt:>6}", DIM),
                    (f"  {when}", mut)]
            star = self._star_for(ac) if verb == "from" and dist < 80 else None
            if star:
                row_.append((f"  {star}", STAR_INK))
            return row_

        for title, items, verb in (("Arrivals", arrivals, "from"),
                                   ("Departures", departures, "to")):
            lines.append([(f"{title} ({len(items)})", TEXT, True)])
            row += 1
            for e in items[:6]:
                lines.append(entry(e, verb))
                self._card_rows[row] = e[0]["hex"]
                row += 1
            if not items:
                lines.append([("none seen yet — routes fill in as they're "
                               "looked up", DIM)])
                row += 1
        lines.append([("click a flight to select it · esc close", DIM)])
        return lines

    def _search_panel(self, cols, rows):
        width = min(70, cols - 4)
        col = max(1, (cols - width) // 2 + 1)
        top = 2
        cursor = "▏"
        query = self.search.query
        lines = [[("⌕ ", MARKER, True), (query, TEXT, True), (cursor, MARKER)]]
        if not query:
            for hint in ("a flight: DL1673 · UA 123 · N123AB · a1b2c3",
                         "a route: BOS-SFO · to LHR · from JFK",
                         "an airport: JFK · Heathrow · Portland",
                         "a filter: delta · 737 · A388 · military · above FL300",
                         "or any place by name"):
                lines.append([(hint, DIM)])
        icons = {"flight": "✈", "airport": "◉", "filter": "▾", "place": "⌖",
                 "note": "·"}
        for i, r in enumerate(self.results):
            sel = i == self.search.cursor
            mark = ("›", MARKER) if sel else (" ", DIM)
            color = TEXT if sel else MUTED
            if r.kind == "flight":
                color = blip_color(r.ac) if sel else ensure_contrast(
                    blip_color(r.ac), CHIP_BG, 3.0)
            lines.append([mark, (f" {icons.get(r.kind, '·')} ", DIM),
                          (r.label, color, sel), (f"  {r.detail}", DIM)])
            self._result_rows[top + len(lines) - 1] = i
        lines.append([("↑↓ choose · enter go · esc close", DIM)])
        height = len(lines)
        return (panel(lines, top, col, width, CHIP_BG),
                (col, top, col + width - 1, top + height - 1))

    def _help_panel(self, cols, rows):
        keys = (
            ("/", "search: a flight, route, airport, airline, type or place"),
            ("click", "select a flight (again to follow) or an airport"),
            ("Tab", "select the next flight out from the centre"),
            ("f", "follow the selected flight"),
            ("c / h", "centre on the selection / fly home"),
            ("a", "the airport card for the field nearest the centre"),
            ("scroll", "zoom about the pointer · + - zoom · arrows pan"),
            ("drag", "pan"),
            ("x", "clear filters"),
            ("l", "labels: auto · all · callsign only · off"),
            ("t r g w", "trails · range rings · ground traffic · weather"),
            ("space", "pause the glide"),
            ("esc", "close the card, then clear filters"),
            ("q", "quit"),
        )
        lines = [[("blips", MARKER, True), ("  keys", MUTED)], []]
        for k, what in keys:
            lines.append([(f"{k:>8}  ", MARKER), (what, TEXT)])
        lines.append([])
        lines.append([("data: adsb.lol · adsb.fi · routes adsb.im · "
                       "METAR aviationweather.gov", DIM)])
        lines.append([("any key closes", DIM)])
        width = fit_width(lines, 40, min(78, cols - 2))
        height = len(lines)
        col = max(1, (cols - width) // 2 + 1)
        top = max(2, (rows - height) // 2)
        return (panel(lines, top, col, width, CHIP_BG),
                (col, top, col + width - 1, top + height - 1))

    def _hover_lines(self, ac):
        """The chip beside the pointer: who, what, where to, how high."""
        from blips._color import fg
        mut = fg(*ensure_contrast(MUTED, CHIP_BG, 3.0))
        dim = fg(*ensure_contrast(DIM, CHIP_BG, 2.0))
        cs = ac.get("flight") or ac["callsign"]
        head = f"{fg(*ensure_contrast(blip_color(ac), CHIP_BG, 3.0))}{ac['callsign']}"
        who = _names.airline_of(cs)
        tname = _names.type_name(ac.get("actype"))
        ident = " · ".join(p for p in (who[1] if who else ac.get("operator"),
                                       tname or ac.get("actype")) if p)
        if ident:
            head += f" {mut}{ident}"
        lines = [head]
        route = self.routes.get(cs, ac["lat"], ac["lon"]) if cs else None
        if route:
            lines.append(f"{mut}{route[0][1]} {route[0][0]} {dim}→ {mut}"
                         f"{route[-1][1]} {route[-1][0]}")
        state = []
        if ac["ground"]:
            state.append("on the ground")
        elif ac.get("alt") is not None:
            vr = ac.get("vrate") or 0
            trend = (f" {trend_arrow(ac)}{abs(vr):,.0f} fpm"
                     if abs(vr) > 400 else "")
            state.append(f"{ac['alt']:,.0f} ft{trend}")
        if ac.get("gs"):
            state.append(f"{ac['gs']:.0f} kt")
        d = haversine_nm(self.camera.lat, self.camera.lon, ac["lat"], ac["lon"])
        brg = bearing_to(self.camera.lat, self.camera.lon, ac["lat"], ac["lon"])
        state.append(f"{d:.0f} nm {COMPASS[round(brg / 45) % 8]}")
        lines.append(mut + f"{dim} · {mut}".join(state))
        if ac["hex"] != self.selected:
            lines.append(f"{dim}click to select")
        return lines


def main(args, lat, lon):
    from blips._radar_sources import theme_id
    from blips._basemap import warm
    warm()
    zoom = max(0.08, min(60.0, args.zoom))
    app = ScopeApp(lat, lon, zoom, wx_theme=theme_id(args.wx_theme),
                   weather_on=bool(args.weather))
    if getattr(args, "find", None):
        app.search_open = True
        app.search.set_query(args.find)
    app.run()
