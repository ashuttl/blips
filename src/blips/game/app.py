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
from blips._framebuffer import get_terminal_size
from blips._geo import advance, bearing_to, haversine_nm
from blips._live import live_loop
from blips._location import get_location
from blips._radar_sources import theme_id
from blips._runtime import resolve_live
from blips._commands import CommandError, airline_name, resolve_callsign
from blips.game.sim import SECTOR_NM, Sim, _commandable, _controlled
from blips.game.procedures import overlay_for
from blips._theme import ensure_contrast
from blips.game.voice import Speaker
from blips.scope import (
    ALERT, CHIP_BG, COMPASS, DIM, MARKER, MUTED, RING, WeatherFeed,
    _route_leg, hit_test, render_scope,
)

TEXT = (214, 219, 233)
GOOD = (140, 210, 110)
WARN = (240, 190, 80)
TX = (120, 175, 225)      # your own key: the controller's side of the exchange
STAR_COLOR = (72, 122, 134)   # arrivals: a cool teal, dim under the localizer
SID_COLOR = (150, 108, 66)    # departures: a warm amber, the other flow
STAR_LABEL = (112, 176, 190)  # ...and the names, which have to be read, not
SID_LABEL = (198, 152, 98)    # merely noticed — the strokes stay quiet
GAME_ZOOM = 1.9           # degrees of latitude: the sector ring plus margin

RADIO_COLORS = {
    "checkin": GOOD, "readback": MUTED, "atc": DIM, "tx": TX,
    "alert": ALERT, "error": WARN, "help": DIM, "request": WARN,
    "atis": WARN,
}
TERRAIN_TINT = (126, 96, 58)   # high ground, as a dim warm wash

# the bar carries only the radio language — what you say to an airplane after
# a callsign.  Nothing here touches the app itself: pause, voice, the log, the
# weather layer are desk controls, reached by a key, never keyed like a mic.
RADIO_HINT = ("callsign then  l/r hdg · c/d alt · rs/is spd · s resume · "
              "dct FIX · via PROC · hold [FIX] · i [rwy] · tfc · ho handoff")
# `?` reveals the desk: control keys, shown as keys so they read apart from the
# radio verbs above.  These run the station, not the airplanes.
MORE_HINT = ("desk:  +/− zoom · ^L log · ^O procs (arr/dep/both) · ^P pause · "
             "^W weather · ^B labels · ^V voice · ^C quit")


def _strip_card(ac):
    """Hover chip for a sim aircraft: who they are and what they hold.

    Chip lines — identity, the job (an overflight's real route when the
    live cast knows it), the state — coloured against the chip background;
    None for a target the sim didn't cast (the scope's stock card covers
    those).  Lines never RESET, so the chip bg survives.
    """
    if "plan" not in ac:
        return None
    plan = ac["plan"]
    route = None
    if plan == "vfr":
        job = "VFR — not on your frequency"
    elif plan == "overflight":
        job = "overflight · with centre"
        if ac.get("route"):
            origin, dest = ac["route"]
            route = f"{_route_leg(origin)} → {_route_leg(dest)}"
    elif plan == "balloon":
        job = "hot air balloon — going where the wind goes"
    elif plan == "arrival":
        where = f"{ac['tag']} " if ac.get("tag") else ""
        job = f"arrival → {where}rwy {ac['rwy']}"
        if ac.get("pre_ho"):
            job += " · with centre"
        if ac.get("from"):
            route = f"from {ac['from']}"
    else:
        off = f"off {ac['tag']} " if ac.get("tag") else ""
        cross = (f" · cross at {ac['xr']:,.0f}+ ft" if ac.get("xr") else "")
        job = f"departure {off}→ {ac['fix']}{cross}"
        if ac.get("to"):
            route = f"to {ac['to']}"
    if plan in ("vfr", "overflight", "balloon"):
        state = f"{ac['alt']:,.0f} ft · {ac['gs']:.0f} kt"
    else:
        state = (f"{ac['alt']:,.0f}→{ac['tgt_alt']:,.0f} ft · "
                 f"hdg {ac['hdg']:03.0f}→{ac['tgt_hdg']:03.0f} · "
                 f"{ac['ias']:.0f} kt")
    phase = {"cleared": " · cleared ILS", "established": " · on the ILS",
             "handed": " · handed off"}.get(ac["phase"], "")
    mut = fg(*ensure_contrast(MUTED, CHIP_BG, 3.0))
    ident = " · ".join(p for p in (airline_name(ac["callsign"]),
                                   ac["actype"]) if p)
    lines = [f"{fg(*ensure_contrast(MARKER, CHIP_BG, 3.0))}{ac['callsign']}"
             f" {mut}{ident}",
             f"{mut}{job}"]
    if route:
        lines.append(f"{mut}{route}")
    lines.append(f"{mut}{state}{phase}")
    return lines


def _hundreds(ft):
    """3,000 ft → '030', the way every data block on this scope writes it."""
    return f"{ft / 100:03.0f}"


def _proc_card(plan, rwy, spoken):
    """Chip lines for a procedure the pointer is resting on: what it is, the
    fixes it strings together with the restrictions that ride on them, and
    the clearance that puts an aeroplane on it.

    This is the answer to "what does that label refer to".  A name on a scope
    is only worth having if it can be asked, and the plate is the one thing a
    controller reads that the radio can't tell them.
    """
    star = plan["kind"] == "STAR"
    accent = STAR_LABEL if star else SID_LABEL
    mut = fg(*ensure_contrast(MUTED, CHIP_BG, 3.0))
    dim = fg(*ensure_contrast(DIM, CHIP_BG, 2.0))
    noun = "arrival" if star else "departure"
    head = (f"{fg(*ensure_contrast(accent, CHIP_BG, 3.0))}{plan['name']}"
            f" {mut}{spoken(plan['name'])} {noun}")
    lines = [head]

    # the fix chain, each with whatever it has to be crossed at
    legs = []
    for _lat, _lon, ident, lo, hi, spd in plan["spine"]:
        if not ident:
            continue
        bits = ""
        if lo is not None and lo == hi:
            bits = f" {_hundreds(lo)}"
        elif lo is not None:
            bits = f" {_hundreds(lo)}+"
        elif hi is not None:
            bits = f" {_hundreds(hi)}−"
        if spd:
            bits += f" {spd}kt"
        legs.append(ident + (f"{dim}{bits}{mut}" if bits else ""))
    if plan["vectors"]:
        legs.append(f"{dim}vectors{mut}")
    if legs:
        lines.append(mut + f" {dim}→{mut} ".join(legs))

    state = [f"rwy {rwy}"]
    if plan["branches"]:
        entries = ", ".join(v for v, _p in plan["branches"][:4] if v)
        if entries:
            state.append(f"from {entries}"
                         + ("…" if len(plan["branches"]) > 4 else ""))
    lines.append(mut + " · ".join(state))
    lines.append(f"{dim}type {mut}‹callsign› via {plan['name']}")
    return lines


def _gate_card(sim, name, plans):
    """Chip lines for a corner post: which way it flows, how far out it is,
    and — the thing that makes the sector learnable — which published
    procedure crosses it, so ``via`` and the scope agree."""
    from blips.game.procedures import plans_for, procedures_through
    sector = sim.sector
    lat, lon = sector["fixes"][name]
    entry = name in sector["entries"]
    accent = STAR_LABEL if entry else SID_LABEL
    mut = fg(*ensure_contrast(MUTED, CHIP_BG, 3.0))
    dim = fg(*ensure_contrast(DIM, CHIP_BG, 2.0))
    kind = "entry gate — arrivals" if entry else "exit gate — departures"
    d = haversine_nm(sim.airport["lat"], sim.airport["lon"], lat, lon)
    brg = bearing_to(sim.airport["lat"], sim.airport["lon"], lat, lon)
    lines = [f"{fg(*ensure_contrast(accent, CHIP_BG, 3.0))}{name} "
             f"{mut}{kind}",
             f"{mut}{d:.0f} nm {COMPASS[round(brg / 45) % 8]} of the field"]
    # what actually crosses here on today's runway, not merely on the plate:
    # a corner post the reciprocal flow uses is real but not yours right now,
    # and saying "via" for it would be a clearance nobody could fly
    want = "STAR" if entry else "SID"
    today = [p["name"] for p in plans_for(sim.airport, sector["rwy"])
             if p["kind"] == want
             and any(f[2] == name for f in p["spine"])]
    if today:
        lines.append(f"{mut}on the {', '.join(today[:3])}")
        lines.append(f"{dim}type {mut}‹callsign› via {today[0]}")
        return lines
    other = [n for n, k in procedures_through(sim.airport["icao"], name)
             if k == want]
    if other:
        lines.append(f"{mut}on the {', '.join(other[:3])} "
                     f"{dim}— not in today's flow")
    else:
        lines.append(f"{dim}no published procedure — vectors")
    return lines


TAPE_LINES = 9                # transmissions shown while paused
DESK_HOLD = 4.0               # seconds a desk-status note lingers in the top bar


class _Console:
    """The command bar and radio log: keystrokes in, transmissions out."""

    def __init__(self, sim, meta):
        self.sim = sim
        self.meta = meta          # callable(word) → True if a control word
        self.buffer = ""
        self.history = []
        self.hist_idx = None
        self.last_mouse = None
        self.tape = False         # paused: the footer replays the frequency
        self.log_open = True      # `log`: keep the tape up while you work

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
        ctrl = {"key:ctrl-l": "log", "key:ctrl-p": "pause",
                "key:ctrl-o": "proc", "key:ctrl-v": "voice",
                "key:ctrl-w": "weather", "key:ctrl-b": "labels"}.get(action)
        if ctrl is not None:
            return self.meta(ctrl)
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
        """A click on a blip addresses it.  An empty bar takes the callsign;
        a bar that already opens with a callsign gets that callsign swapped
        for the new one, so misclicking a plane is one click to fix and any
        command you'd started keying after it survives the retarget."""
        if self.last_mouse is None:
            return False
        callsign = hit_test(*self.last_mouse)
        if not callsign:
            return False
        if not self.buffer:
            self.buffer = callsign.lower() + " "
            return True
        # only retarget when the head is genuinely a callsign on frequency —
        # otherwise the bar holds something else and we leave it be
        head, _, rest = self.buffer.partition(" ")
        on_freq = [a for a in self.sim.aircraft
                   if a["phase"] != "handed" and _commandable(a)]
        try:
            resolve_callsign(head, on_freq)
        except CommandError:
            return False
        self.buffer = callsign.lower() + " " + rest
        return True

    def _age_alpha(self, t):
        """How opaque a call is by age: a spell at full strength, then it
        dissolves into the scope, so a frequency quiet for a couple of minutes
        clears the log back to bare map. Frozen while paused — the replay you
        paused to read stays fully legible however long you sit on it.
        """
        if self.tape:
            return 1.0
        age = time.time() - t
        hold, gone = 15.0, 180.0     # full for 15s, gone by three minutes
        if age <= hold:
            return 1.0
        if age >= gone:
            return 0.0
        return 1.0 - (age - hold) / (gone - hold)

    # -- the bottom lines ----------------------------------------------------
    def footer(self, focused):
        # the log floats over the map now: the renderer composites these lines
        # into the bottom rows, so each is (segments, alpha) — segments carry
        # colour as data, (text, rgb[, bold]), not baked ANSI, and alpha lets a
        # call fade with age; the renderer knocks the braille out under the
        # glyphs and shows terrain in the gaps. hover detail floats on the scope
        # as a chip (hover_card), so the bar keeps its own jobs: radio + command
        if self.sim.radio:
            t, line, kind = self.sim.radio[-1]
            top = ([(line, RADIO_COLORS.get(kind, MUTED))], self._age_alpha(t))
        else:
            top = ([], 1.0)
        prompt = ("▸ ", MARKER, True)
        if self.buffer:
            bar = [prompt, (self.buffer, TEXT), ("▌", MARKER)]
        else:
            # radio language first, then the desk-controls cluster: keys in a
            # brighter tone so they read as pressable, labels kept quiet
            bar = [prompt, (RADIO_HINT + "      ", DIM),
                   ("^L", MUTED), (" log   ", DIM),
                   ("?", MUTED), (" help", DIM)]
        bar = (bar, 1.0)              # the command line never ages out
        if self.tape or self.log_open:
            # the tape: every call in order, oldest at the top — up while
            # paused (the busy moment you missed) or held open with `log`,
            # where your own keyed transmissions show above each readback so
            # a misheard number is there to be read, not just remembered
            tape = [([(line, RADIO_COLORS.get(kind, MUTED))], self._age_alpha(t))
                    for t, line, kind in self.sim.radio[-TAPE_LINES:]]
            tape = [([], 1.0) for _ in range(TAPE_LINES - len(tape))] + tape
            return tape + [bar]
        return [top, bar]


def _sector_pins(sim, airport):
    """Geo-anchored decorations: fixes, the field, runway and localizer.

    A corner post wears the direction it works: ``∇`` for a gate traffic
    comes down through, ``∆`` for one it climbs out over, tinted with the
    same cool-teal and warm-amber the procedure strokes use — so which way a
    gate flows is legible before you read its name.
    """
    sector = sim.sector
    pins = []
    for name in sector["entries"]:
        lat, lon = sector["fixes"][name]
        pins.append((lat, lon, "∇", STAR_COLOR, name, {"key": ("gate", name)}))
    for name in sector["exits"]:
        lat, lon = sector["fixes"][name]
        pins.append((lat, lon, "∆", SID_COLOR, name, {"key": ("gate", name)}))
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
    sat = sector.get("sat")
    if sat is not None:
        apt = sector["sat_apt"]
        pins.append((apt["lat"], apt["lon"], "⊕", MUTED, sat["code"]))
        sthr = sat["thr"]
        srwy_nm = apt["rwys"][0]["len"] / 6076.0
        sfar = advance(sthr[0], sthr[1], sat["course"], srwy_nm)
        sloc = advance(sthr[0], sthr[1], (sat["course"] + 180.0) % 360.0,
                       8.0)
        lines.append((sthr[0], sthr[1], sfar[0], sfar[1], MUTED))
        lines.append((sthr[0], sthr[1], sloc[0], sloc[1], RING))
    for nb in sector.get("neighbors", ()):
        ap = nb["apt"]
        pins.append((ap["lat"], ap["lon"], "⊕", DIM,
                     nb["end"]["code"]))          # a major you don't work
    return pins, lines


PROC_MODES = ("off", "arr", "dep", "both")
PROC_KINDS = {"arr": ("STAR",), "dep": ("SID",), "both": ("STAR", "SID")}
PROC_SAID = {"off": "procedures hidden", "arr": "procedures: arrivals",
             "dep": "procedures: departures", "both": "procedures: both"}


def _procedure_overlay(sim, airport, kinds, declutter):
    """The named SIDs and STARs feeding today's runway, as dotted strokes
    with a name at each outer end — drawn dim, under the localizer, so the
    approach picture still reads first.  It rebuilds on a flow change, so a
    new runway redraws the flows that feed it.  Empty where no procedures
    are vendored (outside the CIFP's coverage).

    The name is painted in its own flow's colour rather than the grey every
    other label wears, and it hangs a row *under* the corner post it leaves
    from, so a procedure and the gate it uses read as one thing.  ``⇣``/``⇡``
    lead the name where a flow ends in radar vectors rather than at a fix,
    because "then vectors" is the plate talking, not missing data.

    Returns ``(pins, lines, plans)``; ``plans`` is what the pointer asks.
    """
    sector = sim.sector
    gates = sector["fixes"]
    ov = overlay_for(
        airport, sector["rwy"], kinds=kinds, declutter=declutter,
        entry_gates=[gates[n] for n in sector["entries"]],
        exit_gates=[gates[n] for n in sector["exits"]])
    lines = []
    for kind, _name, pts in ov["paths"]:
        color = STAR_COLOR if kind == "STAR" else SID_COLOR
        for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
            lines.append((la1, lo1, la2, lo2, color))
    pins = []
    for lat, lon, name, kind, vectors in ov["labels"]:
        star = kind == "STAR"
        tail = ("⇣" if star else "⇡") if vectors else ""
        pins.append((lat, lon, "", MUTED, f"{name}{tail}",
                     {"label_color": STAR_LABEL if star else SID_LABEL,
                      "row": 1, "key": ("proc", name)}))
    return pins, lines, {p["name"]: p for p in ov["plans"]}


def _wx_sampler(rgba, pw, ph, fbbox):
    """Point-sample a radar frame: (lat, lon) → echo 0..1, None off-frame.

    Bound to the frame's own bbox, so it stays honest even if the view
    has panned away since the frame was fetched.

    Echo is intensity, not mere coverage.  The pixel alpha alone can't tell
    a wall of light stratiform blue from a convective core — both come back
    near opaque — so a controller keyed on alpha would watch pilots shy from
    rain they'd fly through without a thought.  The default palette ramps its
    hue with reflectivity (bright blue → purple → magenta → red as the cells
    get heavy), so redness (r − b) tracks intensity where alpha can't: light
    rain reads ~0, the heavy cores climb toward 1.  A faint pixel is clear air.
    """
    minlon, minlat, maxlon, maxlat = fbbox

    def sample(lat, lon):
        if not (minlat <= lat <= maxlat and minlon <= lon <= maxlon):
            return None
        px = int((lon - minlon) / (maxlon - minlon) * (pw - 1))
        py = int((maxlat - lat) / (maxlat - minlat) * (ph - 1))
        i = (py * pw + px) * 4
        if rgba[i + 3] < 40:               # too faint to be a real echo
            return 0.0
        redness = rgba[i] - rgba[i + 2]    # r − b, the palette's heat axis
        return max(0.0, min(1.0, (redness + 60) / 240.0))

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


def _shift_card(sim, airport, seed, live_cast, entry=None, prev=None):
    minutes = int(sim._elapsed) // 60
    code = (airport["iata"] or airport["icao"]).lower()
    cast_note = " (synthetic cast)" if live_cast else ""
    rating = _rating(sim)
    book = ""
    if entry is not None:
        if rating != "—" and prev is not None and sim.score > prev["score"]:
            note = (f"new personal best — previous "
                    f"{prev['score']:,} ({prev['rating']})")
        elif prev is not None:
            note = f"personal best here {prev['score']:,} ({prev['rating']})"
        elif rating != "—":
            note = "first rated shift — your record now"
        elif entry["shifts"] == 1:
            note = "first shift in the book here"
        else:
            note = "no rated shift yet"
        book = (f"\n  {note} · {entry['shifts']} shifts, "
                f"{entry['landed']} landings all-time")
    lines = [
        f"{BOLD}shift summary{RESET} — {airport['icao']} · {minutes} min",
        f"  landed {sim.landed} · handed off {sim.departed} · "
        f"go-arounds {sim.go_arounds} · diversions {sim.diversions} · "
        f"busts {sim.busts}"
        + (f" · traffic alerts {sim.nmacs}" if sim.nmacs else ""),
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
        f"  score {sim.score:,} · rating {BOLD}{rating}{RESET}{book}",
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
        from blips.game.sim import PERF
        terrain.start()   # real elevation → MVAs; flat until it lands
        if args.seed is None:
            # live cast breaks determinism, so seeded shifts skip it
            from blips.game.fleet import TrafficPool
            pool = TrafficPool(airport, PERF)
            pool.start()  # the real traffic near this airport, filling in
    else:
        terrain._fetch(retries=1)  # a screenshot is worth a short wait
    # vendored real routes for this field — deterministic, so seeded shifts
    # keep them too; the check-in and hover chip get a true origin/dest
    from blips.game.schedules import schedule_for
    sim = Sim(airport, seed=seed, pool=pool, terrain=terrain,
              schedule=schedule_for(airport["icao"]))
    center = [airport["lat"], airport["lon"]]
    zoom = [GAME_ZOOM]
    scenery = {"rev": -1, "pins": None, "lines": None,
               "key": None, "proc": None}
    _ground_cache = {}

    def sector_scenery():
        """Pins and strokes for the current sector, rebuilt on flow change.
        The procedure overlay rides along, shown only when toggled on, and
        recompiled when the toggle changes what it's asking for."""
        if scenery["rev"] != sim.sector_rev:
            scenery["pins"], scenery["lines"] = _sector_pins(sim, airport)
            scenery["rev"] = sim.sector_rev
            scenery["key"] = None
        pins, lines = scenery["pins"], scenery["lines"]
        mode = state["procs"]
        if mode == "off":
            return pins, lines
        key = (sim.sector_rev, mode, state["plate"])
        if scenery["key"] != key:
            scenery["proc"] = _procedure_overlay(
                sim, airport, PROC_KINDS[mode], not state["plate"])
            scenery["key"] = key
        ppins, plines, _plans = scenery["proc"]
        return pins + ppins, lines + plines

    def shown_plans():
        """The compiled procedures currently on the scope, for the chip."""
        if state["procs"] == "off" or not scenery["proc"]:
            return {}
        return scenery["proc"][2]

    def pin_card(key):
        """What the scenery under the pointer has to say for itself."""
        from blips.game.sim import say_proc
        what, name = key
        if what == "gate":
            return _gate_card(sim, name, shown_plans())
        plan = shown_plans().get(name)
        return (_proc_card(plan, sim.sector["rwy"], say_proc)
                if plan else None)

    def ground(bbox, gw, hc, sea=None):
        """Terrain as a per-cell underlay tint: MVA above the field glows.

        Clipped to the basemap's land/sea mask (when given) so the terrain
        footprint follows the coastline instead of spilling square-edged
        into open water, and interpolated between samples so it reads as a
        smooth relief rather than the raw fetch grid.
        """
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
            srow = sea[cy] if sea is not None else None
            row = []
            for cx in range(gw):
                if srow is not None and srow[cx]:
                    row.append(None)      # terrain lives on land only
                    continue
                clon = minlon + (cx + 0.5) * (maxlon - minlon) / gw
                mva = terrain.mva_smooth(clat, clon)
                if mva is None or mva <= base:
                    row.append(None)
                else:
                    # low floor so valleys fade almost to nothing and the
                    # relief climbs across the range — ridges read, not a
                    # flat wash of brown over everything above the field
                    w = min(0.42, 0.06 + (mva - base) / 6500.0 * 0.36)
                    row.append((*TERRAIN_TINT, w))
            grid.append(row)
        if len(_ground_cache) > 4:
            _ground_cache.clear()
        _ground_cache[key] = grid
        return grid
    weather = WeatherFeed(airport["lat"], airport["lon"],
                          theme=theme_id(args.wx_theme), nudge=live)
    state = {"paused": False, "weather": bool(args.weather), "procs": "off",
             "plate": False, "labels": True, "desk": None}

    def clock():
        m, s = divmod(int(sim._elapsed), 60)
        return f"{m:02d}:{s:02d}"

    def desk(msg):
        """A brief station-status note for the top bar.  Desk actions live
        here, beside PAUSED — never on the radio tape, so a toggle can never
        crowd a real transmission out of a tape slot."""
        state["desk"] = (msg, time.time() + DESK_HOLD)

    def hud():
        wd, wk = sim.wind
        note = (f"{airport['icao']} approach · rwy {sim.sector['rwy']} · "
                f"wind {int(wd):03d}/{int(wk):02d} · "
                f"score {sim.score:,} · busts {sim.busts} · {clock()}")
        if state["paused"]:
            note += " · PAUSED"
        toast = state["desk"]
        if toast is not None:
            msg, expiry = toast
            if time.time() < expiry:
                note += "   " + msg
            else:
                state["desk"] = None
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
            sim.say(MORE_HINT, "help")
            return True
        if word in ("w", "wx", "weather"):
            state["weather"] = not state["weather"]
            weather.set_enabled(state["weather"])
            _sync_weather()
            return True
        if word in ("log", "r", "radio", "tape"):
            console.log_open = not console.log_open
            return True
        if word in ("labels", "b", "blocks", "tags"):
            state["labels"] = not state["labels"]
            desk("data blocks shown" if state["labels"]
                 else "data blocks hidden")
            return True
        if word in ("proc", "procs", "procedures", "sid", "sids", "star",
                    "stars", "arr", "arrivals", "dep", "deps", "departures",
                    "plate"):
            # ^O thumbs through the states; a word goes straight to one, so
            # "star" mutes the departures without cycling past them
            if word in ("star", "stars", "arr", "arrivals"):
                state["procs"] = "arr"
            elif word in ("sid", "sids", "dep", "deps", "departures"):
                state["procs"] = "dep"
            elif word == "plate":
                state["plate"] = not state["plate"]
                if state["procs"] == "off":
                    state["procs"] = "both"
            else:
                state["procs"] = PROC_MODES[
                    (PROC_MODES.index(state["procs"]) + 1) % len(PROC_MODES)]
            note = PROC_SAID[state["procs"]]
            if state["procs"] != "off" and state["plate"]:
                note += " · full plate"
            desk(note)
            return True
        if word in ("voice", "voices", "tts", "sound"):
            if sim.speaker is not None:
                sim.speaker.close()
                sim.speaker = None
                desk("voices off")
            elif not Speaker.available():
                desk("voices need macOS ‘say’")
            else:
                sim.speaker = Speaker()
                desk("voices on")
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
        console.tape = state["paused"]
        pins, lines_geo = sector_scenery()
        frame = render_scope(
            center, zoom[0], sim, playing=not state["paused"],
            mouse_pos=mouse_pos, show_ground=False, weather=weather,
            show_weather=state["weather"], show_labels=state["labels"],
            drag_offset=drag_preview[0],
            pins=pins, lines_geo=lines_geo, ground=ground,
            game_footer=((TAPE_LINES + 1)
                         if (state["paused"] or console.log_open) else 2,
                         console.footer),
            header_note=hud(), rings_at=(airport["lat"], airport["lon"]),
            hover_card=_strip_card, pin_card=pin_card)
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
            "? for the desk controls", "atc")
    live_loop(render, interval=0.5, mouse=True, auto_play=True,
              play_interval=0.5, on_action=on_action, on_drag=on_drag,
              intercept=console.intercept, raw_keys=True)
    if sim.speaker is not None:
        sim.speaker.close()
    # back on the normal screen: how the shift went, and how it compares
    from blips.game.records import record_shift
    entry, prev = record_shift(
        airport["icao"], score=sim.score, rating=_rating(sim),
        minutes=int(sim._elapsed) // 60, landed=sim.landed,
        handed=sim.departed, busts=sim.busts)
    print(_shift_card(sim, airport, seed, live_cast=pool is not None,
                      entry=entry, prev=prev))
