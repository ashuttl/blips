"""blips --game: the scope stops watching and starts working.

Wires a ``Sim`` (which quacks like the ADS-B ``Feed``) into the existing
live loop and renderer.  The bottom of the screen becomes a frequency: a
radio-log line and a command bar, fed by the live loop's raw-key mode.
Everything else — basemap, weather, drag, zoom, hover — is the scope,
unchanged, with the sector's fixes and runway pinned onto the map.

    blips --game            # approach control at the nearest large airport
    blips --game tpa        # or name one, ICAO/IATA/city
"""

import math
import random
import sys
import time

from blips._airports import find_airport, nearest_airport
from blips._color import BOLD, RESET, fg
from blips._framebuffer import get_terminal_size, visible_len
from blips._geo import advance
from blips._live import live_loop
from blips._location import get_location
from blips._radar_sources import theme_id
from blips._runtime import resolve_live
from blips._sim import SECTOR_NM, Sim
from blips.scope import (
    ALERT, DIM, MARKER, MUTED, RING, WeatherFeed, hit_test, render_scope,
)

TEXT = (214, 219, 233)
GOOD = (140, 210, 110)
WARN = (240, 190, 80)
GAME_ZOOM = 1.9           # degrees of latitude: the sector ring plus margin

RADIO_COLORS = {
    "checkin": GOOD, "readback": MUTED, "atc": DIM,
    "alert": ALERT, "error": WARN, "help": DIM, "request": WARN,
    "atis": WARN,
}
TERRAIN_TINT = (126, 96, 58)   # high ground, as a dim warm wash

HINT = ("callsign then:  l/r hdg · c/d alt · rs/is spd · s resume · "
        "dct FIX · hold [FIX] · i [rwy] · ho  —  ? help · pause · quit")


def _strip_line(ac):
    """Hover readout for a sim aircraft: who they are and what they hold."""
    if ac["plan"] == "arrival":
        job = f"arrival via {ac['fix']} → rwy {ac['rwy']}"
    else:
        job = f"departure → {ac['fix']}"
    state = (f"{ac['alt']:,.0f}→{ac['tgt_alt']:,.0f} ft · "
             f"hdg {ac['hdg']:03.0f}→{ac['tgt_hdg']:03.0f} · "
             f"{ac['ias']:.0f} kt")
    phase = {"cleared": " · cleared ILS", "established": " · on the ILS",
             "handed": " · handed off"}.get(ac["phase"], "")
    return (f"{fg(*MARKER)}{BOLD}{ac['callsign']}{RESET} "
            f"{fg(*MUTED)}{ac['actype']} · {job} · {state}{phase}{RESET}")


class _Console:
    """The command bar and radio log: keystrokes in, transmissions out."""

    def __init__(self, sim, meta):
        self.sim = sim
        self.meta = meta          # callable(word) → True if a control word
        self.buffer = ""
        self.history = []
        self.hist_idx = None
        self.last_mouse = None

    # -- keyboard (live_loop raw mode: every printable is ours) -------------
    def intercept(self, action):
        if action == "key:enter":
            return self._submit()
        if action == "key:backspace":
            self.buffer = self.buffer[:-1]
            return True
        if action == "escape":
            self.buffer = ""
            return True
        if action in ("back", "fwd"):
            return self._history(-1 if action == "back" else 1)
        if action.startswith("key:"):
            ch = action[4:]
            if ch in "+-=" and not self.buffer:
                return False              # empty bar: keep the zoom keys
            if ch == "?" and not self.buffer:
                return self.meta("?")
            if len(self.buffer) < 80:
                self.buffer += ch
            return True
        return False

    def _submit(self):
        text, self.buffer = self.buffer.strip(), ""
        self.hist_idx = None
        if not text:
            return True
        self.history.append(text)
        if not self.meta(text.lower()):
            self.sim.command(text)
        return True

    def _history(self, step):
        if not self.history:
            return True
        if self.hist_idx is None:
            self.hist_idx = len(self.history)
        self.hist_idx = max(0, min(len(self.history), self.hist_idx + step))
        self.buffer = ("" if self.hist_idx == len(self.history)
                       else self.history[self.hist_idx])
        return True

    def click_hail(self):
        """A click on a blip drops its callsign into an empty bar."""
        if self.buffer or self.last_mouse is None:
            return False
        callsign = hit_test(*self.last_mouse)
        if callsign:
            self.buffer = callsign.lower() + " "
            return True
        return False

    # -- the two bottom lines ------------------------------------------------
    def footer(self, focused):
        # the radio line is never covered: hovering an aircraft borrows
        # the (idle) command bar for its strip instead
        if self.sim.radio:
            _t, line, kind = self.sim.radio[-1]
            top = f"{fg(*RADIO_COLORS.get(kind, MUTED))}{line}{RESET}"
        else:
            top = ""
        prompt = f"{fg(*MARKER)}{BOLD}▸{RESET} "
        strip = (_strip_line(focused)
                 if focused is not None and "plan" in focused else None)
        if self.buffer:
            bar = (f"{prompt}{fg(*TEXT)}{self.buffer}{RESET}"
                   f"{fg(*MARKER)}▌{RESET}")
            if strip is not None:
                # the strip rides the right side of the bar: a controller
                # gets to read a flight strip mid-transmission
                cols = get_terminal_size()[0]
                pad = cols - visible_len(bar) - visible_len(strip) - 1
                if pad >= 4:
                    bar += " " * pad + strip
        elif strip is not None:
            bar = f"{prompt}{strip}"
        else:
            bar = f"{prompt}{fg(*DIM)}{HINT}{RESET}"
        return [top, bar]


def _sector_pins(sim, airport):
    """Geo-anchored decorations: fixes, the field, runway and localizer."""
    sector = sim.sector
    pins = []
    for name in sector["entries"]:
        lat, lon = sector["fixes"][name]
        pins.append((lat, lon, "∆", MUTED, name))
    for name in sector["exits"]:
        lat, lon = sector["fixes"][name]
        pins.append((lat, lon, "∇", MUTED, name))
    pins.append((airport["lat"], airport["lon"], "⊕", MARKER,
                 airport["iata"] or airport["icao"]))

    thr = sector["thr"]
    rwy_nm = airport["rwys"][0]["len"] / 6076.0
    far = advance(thr[0], thr[1], sector["course"], rwy_nm)
    loc = advance(thr[0], thr[1], (sector["course"] + 180.0) % 360.0, 11.0)
    lines = [
        (thr[0], thr[1], far[0], far[1], MARKER),          # the runway
        (thr[0], thr[1], loc[0], loc[1], RING),            # the localizer
    ]
    return pins, lines


def _wx_sampler(rgba, pw, ph, fbbox):
    """Point-sample a radar frame: (lat, lon) → echo 0..1, None off-frame.

    Bound to the frame's own bbox, so it stays honest even if the view
    has panned away since the frame was fetched.
    """
    minlon, minlat, maxlon, maxlat = fbbox

    def sample(lat, lon):
        if not (minlat <= lat <= maxlat and minlon <= lon <= maxlon):
            return None
        px = int((lon - minlon) / (maxlon - minlon) * (pw - 1))
        py = int((maxlat - lat) / (maxlat - minlat) * (ph - 1))
        return rgba[(py * pw + px) * 4 + 3] / 255.0

    return sample


def _rating(sim):
    """A letter for the shift: what you scored against what the traffic
    was worth.  Fair at any shift length and any hour of the ramp —
    working everything cleanly and promptly is an A whether the sector
    gave you six aircraft or sixty.  Busts are what they are."""
    if sim._elapsed < 120.0 or sim.offered < 150:
        return "—"
    if sim.busts >= 3 or sim.score < 0:
        return "F"
    ratio = sim.score / sim.offered
    for grade, floor in (("A+", 0.96), ("A", 0.88), ("B+", 0.78),
                         ("B", 0.65), ("C", 0.45), ("D", 0.20)):
        if ratio >= floor:
            return grade
    return "F"


def _shift_card(sim, airport, seed, live_cast):
    minutes = int(sim._elapsed) // 60
    code = (airport["iata"] or airport["icao"]).lower()
    cast_note = " (synthetic cast)" if live_cast else ""
    lines = [
        f"{BOLD}shift summary{RESET} — {airport['icao']} · {minutes} min",
        f"  landed {sim.landed} · handed off {sim.departed} · "
        f"go-arounds {sim.go_arounds} · diversions {sim.diversions} · "
        f"busts {sim.busts}",
    ]
    if sim.hearbacks:
        lines.append(f"  readbacks misheard {sim.hearbacks} · "
                     f"caught {sim.hearbacks_caught}")
    if sim._delay_n:
        avg = sim._delay_extra / sim._delay_n
        m, s = divmod(int(avg), 60)
        note = "right at par" if avg < 30.0 else f"+{m}:{s:02d} over par"
        lines.append(f"  arrivals averaged {note}")
    return "\n".join(lines + [
        f"  score {sim.score:,} · rating {BOLD}{_rating(sim)}{RESET}",
        f"  {fg(*DIM)}replay this traffic: blips --game {code} "
        f"--seed {seed}{cast_note}{RESET}",
    ])


def _resolve_airport(query):
    if query:
        ap = find_airport(query)
        if ap is None:
            print(f'No airport matching "{query}".', file=sys.stderr)
            sys.exit(1)
        return ap
    lat, lon = get_location()
    if lat is None:
        print("Could not determine location; name an airport: "
              "blips --game tpa", file=sys.stderr)
        sys.exit(1)
    ap = nearest_airport(lat, lon)
    if ap is None:
        print("No airport found nearby; name one: blips --game tpa",
              file=sys.stderr)
        sys.exit(1)
    return ap


def main(args):
    airport = _resolve_airport((args.game or "").strip())
    live = resolve_live(args)
    seed = args.seed if args.seed is not None else random.randint(10000,
                                                                  99999)
    pool = terrain = None
    from blips._terrain import Terrain
    terrain = Terrain(airport["lat"], airport["lon"])
    if live:
        from blips._sim import PERF
        terrain.start()   # real elevation → MVAs; flat until it lands
        if args.seed is None:
            # live cast breaks determinism, so seeded shifts skip it
            from blips._fleet import TrafficPool
            pool = TrafficPool(airport, PERF)
            pool.start()  # the real traffic near this airport, filling in
    else:
        terrain._fetch(retries=1)  # a screenshot is worth a short wait
    sim = Sim(airport, seed=seed, pool=pool, terrain=terrain)
    center = [airport["lat"], airport["lon"]]
    zoom = [GAME_ZOOM]
    scenery = {"rev": -1, "pins": None, "lines": None}
    _ground_cache = {}

    def sector_scenery():
        """Pins and strokes for the current sector, rebuilt on flow change."""
        if scenery["rev"] != sim.sector_rev:
            scenery["pins"], scenery["lines"] = _sector_pins(sim, airport)
            scenery["rev"] = sim.sector_rev
        return scenery["pins"], scenery["lines"]

    def ground(bbox, gw, hc):
        """Terrain as a per-cell underlay tint: MVA above the field glows."""
        if terrain is None:
            return None
        key = (tuple(round(v, 3) for v in bbox), gw, hc)
        if key in _ground_cache:
            return _ground_cache[key]
        if terrain.mva_at(airport["lat"], airport["lon"]) is None:
            return None      # grid not in yet; try again next frame
        base = airport["elev"] + 2500.0
        minlon, minlat, maxlon, maxlat = bbox
        grid = []
        for cy in range(hc):
            clat = maxlat - (cy + 0.5) * (maxlat - minlat) / hc
            row = []
            for cx in range(gw):
                clon = minlon + (cx + 0.5) * (maxlon - minlon) / gw
                mva = terrain.mva_at(clat, clon)
                if mva is None or mva <= base:
                    row.append(None)
                else:
                    w = min(0.42, 0.14 + (mva - base) / 8000.0 * 0.30)
                    row.append((*TERRAIN_TINT, w))
            grid.append(row)
        if len(_ground_cache) > 4:
            _ground_cache.clear()
        _ground_cache[key] = grid
        return grid
    weather = WeatherFeed(airport["lat"], airport["lon"],
                          theme=theme_id(args.wx_theme), nudge=live)
    state = {"paused": False, "weather": bool(args.weather)}

    def clock():
        m, s = divmod(int(sim._elapsed), 60)
        return f"{m:02d}:{s:02d}"

    def hud():
        wd, wk = sim.wind
        note = (f"{airport['icao']} approach · rwy {sim.sector['rwy']} · "
                f"wind {int(wd):03d}/{int(wk):02d} · "
                f"score {sim.score:,} · busts {sim.busts} · {clock()}")
        if state["paused"]:
            note += " · PAUSED"
        return note

    def _sync_weather():
        from blips.scope import bbox_for
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 3)
        weather.set_view(bbox_for(center[0], center[1], zoom[0], gw, hc),
                         gw, hc)

    def meta(word):
        """Bare control words typed into the bar (no callsign)."""
        if word in ("q", "quit", "exit"):
            raise SystemExit
        if word in ("p", "pause"):
            state["paused"] = not state["paused"]
            if not state["paused"]:
                sim._last_tick = None    # resume without a time jump
            return True
        if word in ("?", "help", "h"):
            sim.say(HINT, "help")
            return True
        if word in ("w", "wx", "weather"):
            state["weather"] = not state["weather"]
            weather.set_enabled(state["weather"])
            _sync_weather()
            return True
        return False

    console = _Console(sim, meta)

    def render(playing=True, mouse_pos=None, **_):
        console.last_mouse = mouse_pos
        # say once whether the ground is in play — nobody should have to
        # wonder whether the sector is genuinely flat or just not loaded
        if terrain is not None and not state.get("terrain_told"):
            if terrain.status == "ready":
                state["terrain_told"] = True
                if terrain.max_mva > airport["elev"] + 3500.0:
                    sim.say(f"high terrain in sector — MVA up to "
                            f"{terrain.max_mva:,.0f} ft, shaded on the "
                            "scope", "help")
            elif terrain.status == "failed":
                state["terrain_told"] = True
                sim.say("terrain data unavailable this shift — "
                        "flat-world rules", "help")
        # the sim's pilots see whatever radar frame the scope is showing
        rgba, pw, ph, frame_view, *_rest = weather.snapshot()
        sim.wx_sample = (_wx_sampler(rgba, pw, ph, frame_view[0])
                         if rgba is not None and state["weather"] else None)
        if not state["paused"]:
            sim.tick()
        pins, lines_geo = sector_scenery()
        frame = render_scope(
            center, zoom[0], sim, playing=not state["paused"],
            mouse_pos=mouse_pos, show_ground=False, weather=weather,
            show_weather=state["weather"], drag_offset=drag_preview[0],
            pins=pins, lines_geo=lines_geo, ground=ground,
            game_footer=(2, console.footer), header_note=hud(),
            rings_at=(airport["lat"], airport["lon"]))
        if sim.bell:
            sim.bell = False
            frame = "\a" + frame   # something on frequency needs you
        return frame

    if not live:
        # a static frame for --print: run the shift forward so there's
        # traffic in the picture, then draw once
        t = sim.start
        for _ in range(480):
            t += 1.0
            sim.tick(t)
        if args.weather:
            weather.set_enabled(True)
            _sync_weather()
            try:
                weather.poll_once()
            except Exception as exc:
                weather.error = str(exc)
        drag_preview = [None]
        print(render(playing=False))
        return

    drag_preview = [None]

    def on_action(key):
        if key in ("+", "="):
            zoom[0] = max(0.4, zoom[0] / 1.5)
        elif key in ("-", "_"):
            zoom[0] = min(24.0, zoom[0] * 1.5)
        else:
            return False
        _sync_weather()
        return True

    def on_drag(dcol, drow, done):
        if not done:
            drag_preview[0] = (dcol, drow) if (dcol or drow) else None
            return bool(dcol or drow)
        drag_preview[0] = None
        if not (dcol or drow):
            return console.click_hail()
        cols, rows = get_terminal_size()
        gw, hc = max(20, cols), max(8, rows - 3)
        lon_span = (zoom[0] * (gw / (hc * 2))
                    / max(0.2, math.cos(math.radians(center[0]))))
        center[0] = max(-80.0, min(80.0, center[0] + drow * zoom[0] / hc))
        center[1] += -dcol * lon_span / gw
        _sync_weather()
        return True

    if state["weather"]:
        weather.set_enabled(True)
        _sync_weather()
    weather.start()
    sim.say(f"you have the {airport['name']} sector — traffic inbound. "
            "? for the phrase book", "atc")
    live_loop(render, interval=0.5, mouse=True, auto_play=True,
              play_interval=0.5, on_action=on_action, on_drag=on_drag,
              intercept=console.intercept, raw_keys=True)
    # back on the normal screen: how the shift went
    print(_shift_card(sim, airport, seed, live_cast=pool is not None))
