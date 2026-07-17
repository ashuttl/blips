"""The game's world: a TRACON sector full of simulated flights.

``Sim`` owns everything airborne.  It quacks like ``scope.Feed`` —
``snapshot()`` returns the same aircraft dicts the ADS-B poller produces,
with ``fix_time`` stamped to the present so the scope's dead-reckoning
glide becomes a no-op — which is how the whole rendering stack works on
simulated traffic untouched.

Flying is deliberately simple and deliberately honest: standard-rate
turns (3°/s — half a minute to come around 90°), type-plausible climb
and descent rates, speed changes that take time.  Sequencing feels like
a skill because a 737 turns like a 737.

The sector is a ring around one real airport.  Arrivals check in at
entry fixes wanting the runway; departures come off the runway wanting
their exit fix.  Separation is 3 nm / 1,000 ft; the monitor debounces so
one bust per pair is scored until they're apart again.
"""

import math
import random
import time

from blips._commands import (
    CommandError, parse, resolve_callsign, say_altitude, say_digits,
    telephony,
)
from blips._geo import (
    advance, bearing_to, cross_along_track, haversine_nm, turn_delta,
)

SECTOR_NM = 45.0          # boundary ring radius
DESPAWN_NM = 60.0         # grace past the farthest gate (arrivals spawn
                          # a few nm outside their fix; never despawn there)
TURN_RATE = 3.0           # deg/s, standard rate
ACCEL_KT_S = 1.2          # speed change rate
GS_FT_PER_NM = 318.0      # 3° glideslope
SEP_NM, SEP_FT = 3.0, 1000.0
# a VFR target isn't separation traffic — it's a hazard.  you owe them a
# traffic call, not three miles; only a genuine near-miss scores
NMAC_NM, NMAC_FT = 0.9, 500.0
SEP_FLOOR_AGL = 900.0     # ignore pairs in the runway environment
CA_LOOK_S = 45.0          # conflict alert looks this far down the track
TRAIL_MAX_FIXES = 120
TRAIL_MIN_GAP_S = 4.0
# radar echo (0..1) is scaled to intensity: light rain reads near 0, the
# heavy convective cores near 1 (see _wx_sampler).  Pilots avoid the cores,
# not the stratiform blue they'd fly through every day.
WX_DEVIATE = 0.5          # echo at/above this on the path → pilots avoid it
WX_CLEAR = 0.25           # echo below this → they call clear of weather
WX_WORSE = 0.1            # a vector only draws "unable" if this much worse

# type → (tracon cruise kt, min clean kt, approach kt, climb fpm, descend fpm)
PERF = {
    "B738": (280, 210, 140, 2300, 2100), "B739": (280, 215, 145, 2200, 2100),
    "A320": (280, 205, 138, 2200, 2000), "A321": (280, 210, 142, 2000, 2000),
    "A20N": (280, 205, 136, 2400, 2000), "B752": (280, 200, 135, 2600, 2200),
    "E175": (270, 195, 130, 2400, 2000), "E190": (270, 200, 132, 2300, 2000),
    "CRJ9": (270, 200, 135, 2200, 2100), "CRJ7": (270, 195, 132, 2200, 2100),
    "DH8D": (240, 170, 120, 1500, 1600), "AT76": (230, 165, 115, 1400, 1500),
    "B763": (280, 210, 145, 2200, 2000), "B77W": (290, 220, 150, 2000, 2000),
    "A388": (290, 225, 150, 1800, 1900), "B788": (290, 215, 145, 2300, 2000),
    "C56X": (270, 180, 115, 3000, 2500), "GLF5": (290, 190, 130, 3500, 2800),
}

# the sky that isn't yours: GA types that wander the sector VFR, squawking
# 1200 and talking to nobody — same tuple shape as PERF, kept separate so
# the live traffic pool never casts a Skyhawk as an IFR arrival
GA_PERF = {
    "C172": (105, 55, 65, 700, 500), "PA28": (110, 60, 70, 650, 500),
    "C182": (135, 60, 70, 900, 600), "SR22": (155, 70, 80, 1200, 700),
    "DA40": (125, 55, 70, 800, 550),
    "BALN": (0, 0, 0, 200, 200),     # a balloon: the wind does the flying
}


def _controlled(ac):
    """Is this target on your frequency — yours to work, yours to lose?"""
    return ac["plan"] in ("arrival", "departure")

# wake category by type — everything unlisted radar-separates at the
# standard 3 nm.  The B757 is its own famous case: "large" on paper,
# notorious enough on final to carry extra spacing in the real rules.
WAKE = {"A388": "super",
        "B77W": "heavy", "B763": "heavy", "B788": "heavy",
        "B752": "b757"}
WAKE_NM = {"super": 6.0, "heavy": 5.0, "b757": 4.0}   # in-trail behind one
_WAKE_WORD = {"super": "super", "heavy": "heavy", "b757": "seven five seven"}


def hail(ac):
    """Telephony plus the wake suffix the R/T actually carries: a 777 is
    'Speedbird 12 heavy' every time anyone says its name."""
    tag = {"super": " super", "heavy": " heavy"}.get(WAKE.get(ac["actype"]))
    return telephony(ac["callsign"]) + (tag or "")


# airline → plausible TRACON fleet (types must exist in PERF)
FLEETS = {
    "AAL": ("B738", "A321", "E175"), "DAL": ("B738", "A320", "A321", "B752"),
    "UAL": ("B738", "B739", "A320"), "SWA": ("B738",),
    "JBU": ("A320", "A321", "E190"), "ASA": ("B738", "E175"),
    "FFT": ("A20N", "A321"), "NKS": ("A20N", "A321"),
    "AAY": ("A320",), "SCX": ("B738",), "MXY": ("A20N", "E190"),
    "FDX": ("B763", "B752"), "UPS": ("B763",), "GTI": ("B763",),
    "SKW": ("E175", "CRJ9", "CRJ7"), "RPA": ("E175",), "EDV": ("CRJ9",),
    "ENY": ("E175", "CRJ7"), "PDT": ("E175",), "JIA": ("CRJ9", "CRJ7"),
    "AWI": ("CRJ7",), "EJA": ("C56X", "GLF5"), "LXJ": ("C56X", "GLF5"),
    "ACA": ("A320", "A321", "B738"), "WJA": ("B738",),
    "POE": ("E190", "DH8D"), "JZA": ("CRJ9", "DH8D"),
    "AMX": ("B738",), "VOI": ("A320", "A321"), "CMP": ("B738",),
    "AVA": ("A320",), "GLO": ("B738",),
    "BAW": ("A320", "A321", "B77W", "B788"), "VIR": ("B788", "A388"),
    "DLH": ("A320", "A321", "B788"), "AFR": ("A320", "A321"),
    "KLM": ("B738", "E190"), "RYR": ("B738",), "EZY": ("A320", "A20N"),
    "WZZ": ("A321", "A20N"), "IBE": ("A320", "A321"), "TAP": ("A320", "A321"),
    "SAS": ("A320", "A20N"), "FIN": ("A320", "E190"), "SWR": ("A320", "A321"),
    "AUA": ("A320",), "BEL": ("A320",), "EIN": ("A320", "A321"),
    "ICE": ("B738",), "THY": ("A321", "B77W"), "ELY": ("B738", "B788"),
    "UAE": ("B77W", "A388"), "QTR": ("B77W", "A320"), "ETD": ("B788",),
    "SVA": ("A320", "B77W"), "ANA": ("B788", "B77W", "A321"),
    "JAL": ("B788", "B738"), "KAL": ("B77W", "A321"), "AAR": ("A321",),
    "CPA": ("B77W", "A321"), "CCA": ("B77W", "A320"), "CES": ("A320", "B788"),
    "CSN": ("A320", "B788"), "SIA": ("B77W", "A388"), "MAS": ("B738",),
    "THA": ("B77W",), "EVA": ("B77W",), "CAL": ("B77W",),
    "AIC": ("B788", "A320"), "IGO": ("A320", "A321"),
    "QFA": ("B738", "A388"), "ANZ": ("A320", "B788"), "JST": ("A320",),
    "FJI": ("B738",),
}

# country → airlines likely on frequency there (fallback: a world mix)
POOLS = {
    "US": ("AAL", "DAL", "UAL", "SWA", "JBU", "ASA", "FFT", "NKS", "AAY",
           "SCX", "MXY", "FDX", "UPS", "SKW", "RPA", "EDV", "ENY", "PDT",
           "JIA", "AWI", "EJA", "LXJ", "ACA", "WJA", "AMX", "VOI", "CMP",
           "BAW", "DLH", "UAE"),
    "CA": ("ACA", "WJA", "POE", "JZA", "UAL", "DAL", "AAL", "BAW", "DLH"),
    "MX": ("AMX", "VOI", "AAL", "UAL", "DAL", "SWA"),
    "GB": ("BAW", "VIR", "EZY", "RYR", "DLH", "AFR", "KLM", "UAE", "QTR",
           "AAL", "DAL", "UAL", "EIN", "WZZ", "EJA"),
    "IE": ("EIN", "RYR", "BAW", "AAL", "UAL", "DAL"),
    "FR": ("AFR", "EZY", "RYR", "BAW", "DLH", "UAE", "EJA"),
    "DE": ("DLH", "EZY", "RYR", "SWR", "AUA", "THY", "UAE", "BAW"),
    "ES": ("IBE", "RYR", "EZY", "WZZ", "BAW", "AFR"),
    "IT": ("RYR", "EZY", "WZZ", "DLH", "BAW", "AFR"),
    "NL": ("KLM", "EZY", "RYR", "DLH", "BAW"),
    "PT": ("TAP", "RYR", "EZY", "BAW"),
    "CH": ("SWR", "EZY", "DLH", "BAW"),
    "AT": ("AUA", "RYR", "WZZ", "DLH"),
    "SE": ("SAS", "RYR", "FIN", "DLH"), "NO": ("SAS", "RYR", "DLH"),
    "DK": ("SAS", "RYR", "DLH"), "FI": ("FIN", "SAS", "DLH"),
    "IS": ("ICE", "SAS", "EZY"), "TR": ("THY", "DLH", "UAE"),
    "IL": ("ELY", "THY", "UAE"), "AE": ("UAE", "ETD", "QTR", "THY", "AIC"),
    "QA": ("QTR", "UAE", "THY"), "SA": ("SVA", "UAE", "QTR"),
    "JP": ("ANA", "JAL", "KAL", "CPA", "UAL", "DAL", "SIA"),
    "KR": ("KAL", "AAR", "ANA", "JAL", "CPA"),
    "HK": ("CPA", "CCA", "CES", "CSN", "SIA", "ANA"),
    "CN": ("CCA", "CES", "CSN", "CPA", "SIA"),
    "SG": ("SIA", "CPA", "MAS", "THA", "QFA", "ANA"),
    "MY": ("MAS", "SIA", "THA"), "TH": ("THA", "SIA", "MAS"),
    "TW": ("CAL", "EVA", "CPA", "ANA"), "IN": ("AIC", "IGO", "UAE", "SIA"),
    "AU": ("QFA", "JST", "ANZ", "SIA", "UAE"),
    "NZ": ("ANZ", "QFA", "JST", "SIA"), "FJ": ("FJI", "QFA", "ANZ"),
    "BR": ("GLO", "TAP", "AAL", "UAE"), "PA": ("CMP", "AAL", "UAL"),
    "CO": ("AVA", "CMP", "AAL"),
}
WORLD_POOL = ("BAW", "DLH", "AFR", "KLM", "UAE", "QTR", "SIA", "AAL",
              "DAL", "UAL", "THY", "CPA", "ANA")

_VOWELS = "AEIOU"
_CONSONANTS = "BCDFGHJKLMNPRSTVWZ"

_NATO = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
         "hotel", "india", "juliett", "kilo", "lima", "mike", "november",
         "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
         "victor", "whiskey", "xray", "yankee", "zulu")


def say_runway(rwy):
    """'01L' → 'one left' — digits spoken, side spelled out."""
    num = "".join(c for c in rwy if c.isdigit())
    side = {"L": " left", "R": " right", "C": " centre"}.get(
        rwy[-1] if rwy[-1].isalpha() else "", "")
    return f"{say_digits(num)}{side}"


def _fix_name(rng):
    """A pronounceable five-letter fix, the way real ones sound."""
    pattern = rng.choice(("CVCCV", "CVCVC", "VCCVC", "CVVCV"))
    return "".join(rng.choice(_VOWELS if c == "V" else _CONSONANTS)
                   for c in pattern)


GATE_BAND_NM = (22.0, 52.0)   # where sector gates live — wide enough to
                              # catch the real close-in VORs (PIE is 15 nm
                              # off TPA; a near gate is a fun corner)
_NAV_RANK = {"VORTAC": 0, "VOR-DME": 0, "VOR": 0, "TACAN": 1,
             "NDB-DME": 2, "NDB": 3, "DME": 4}


def build_sector(airport):
    """Fixes and active runway for an airport, seeded by its ICAO code.

    The corner posts are real: the best radio navaid in each 45° octant
    of the gate band, VORs first, the way TRACON gates always were.  A
    synthesized five-letter fix fills any octant the real world left
    empty.  Deterministic per airport either way — TPA's sector is
    always TPA's sector, so learning it means something.

    A real approach control rarely works one field: the nearest airport
    with a jet runway inside the sector becomes the satellite, and some
    of the traffic is theirs.
    """
    from blips._airports import airports_near, navaids_near
    rng = random.Random(airport["icao"])
    lat, lon = airport["lat"], airport["lon"]
    candidates = navaids_near(lat, lon, *GATE_BAND_NM)
    ideal = sum(GATE_BAND_NM) / 2.0

    fixes, names, entries, exits = {}, set(), [], []
    for base in (45, 135, 225, 315, 0, 90, 180, 270):  # diagonals first
        best = None
        for dist, brg, nav in candidates:
            if abs(((brg - base) + 180.0) % 360.0 - 180.0) > 22.5:
                continue
            if nav["id"] in names:
                continue
            score = (_NAV_RANK.get(nav["type"], 5), abs(dist - ideal))
            if best is None or score < best[0]:
                best = (score, nav)
        if best is not None:
            name = best[1]["id"]
            fixes[name] = (best[1]["lat"], best[1]["lon"])
        else:
            while True:                      # thin coverage: invent a gate
                name = _fix_name(rng)
                if name not in names:
                    break
            brg = base + rng.uniform(-18, 18)
            fixes[name] = advance(lat, lon, brg, SECTOR_NM)
        names.add(name)
        (entries if base % 90 else exits).append(name)

    runway = airport["rwys"][0]                    # longest, by build sort
    end = rng.choice(("le", "he"))                 # today's flow
    ident, course, thr = _end_geometry(airport, runway, end)

    sat_apt = None
    for _dist, ap in airports_near(lat, lon, 10.0, 34.0):
        if ap["icao"] != airport["icao"]:
            sat_apt = ap
            break
    return {
        "fixes": fixes, "entries": entries, "exits": exits,
        "rwy": ident, "course": course, "thr": thr, "end": end,
        "elev": airport["elev"], "sat_apt": sat_apt,
        "sat": _sat_end(sat_apt, course) if sat_apt else None,
    }


def _sat_end(sat_apt, main_course):
    """The satellite's runway end that flows the same way the main
    airport's does — one wind, one direction of traffic."""
    runway = sat_apt["rwys"][0]
    ident, course, thr = min(
        (_end_geometry(sat_apt, runway, e) for e in ("le", "he")),
        key=lambda geom: abs(turn_delta(geom[1], main_course)))
    return {"code": sat_apt["iata"] or sat_apt["icao"][-3:],
            "name": sat_apt["city"] or sat_apt["name"],
            "elev": sat_apt["elev"],
            "rwy": ident, "course": course, "thr": thr}


def _end_geometry(airport, runway, end):
    """(ident, course, (thr_lat, thr_lon)) for one end of a runway."""
    ident, course, thr_lat, thr_lon = runway[end]
    if thr_lat is None:                            # no threshold coords —
        thr_lat, thr_lon = advance(                # walk back from midpoint
            airport["lat"], airport["lon"], (course + 180.0) % 360.0,
            runway["len"] / 6076.0 / 2.0)
    return ident, course, (thr_lat, thr_lon)


def _runway_end(airport, ident):
    """(ident, course, thr_lat, thr_lon) for a named runway end, or None."""
    want = ident.upper().lstrip("0")
    for rwy in airport["rwys"]:
        for end in ("le", "he"):
            rid, course, tlat, tlon = rwy[end]
            if rid.upper().lstrip("0") == want:
                if tlat is None:
                    tlat, tlon = airport["lat"], airport["lon"]
                return rid, course, tlat, tlon
    return None


class Sim:
    """The sector, its traffic, and the frequency.  Feed-compatible."""

    def __init__(self, airport, seed=None, pool=None, terrain=None):
        self.airport = airport
        self.sector = build_sector(airport)
        self.rng = random.Random(seed)
        self.pool = pool         # live-sampled traffic, or None when offline
        self.terrain = terrain   # sector MVA grid, or None for a flat world
        self.wx_sample = None    # callable(lat, lon) → echo 0..1, or None
        self.sector_rev = 0      # bumps on a flow change so the UI redraws
        self.bell = False        # ring the terminal on the next frame
        self.go_arounds = 0
        self.diversions = 0
        self.aircraft = []
        self.trails = {}
        self.radio = []          # [(time, line, kind)] — newest last
        self.score = 0
        self.offered = 0         # points the concluded traffic was worth
        self.landed = 0
        self.departed = 0
        self.busts = 0
        self._delay_extra = 0.0  # arrival seconds beyond par, summed
        self._delay_n = 0
        self.start = time.time()
        self.updated = self.start
        self.error = None
        self.source = f"{airport['icao']} approach"
        self._bust_pairs = set()
        self._nmac_pairs = set()
        self.nmacs = 0           # near-misses with traffic nobody controls
        self._counter = 0
        self._next_arrival = 45.0
        self._next_departure = 30.0
        self._next_request = 150.0
        self._next_flow = self.rng.uniform(600.0, 1080.0)
        self._next_push = self.rng.uniform(420.0, 780.0)
        self._push_until = 0.0
        self._rwy_closed_until = 0.0
        self._next_vfr = self.rng.uniform(90.0, 240.0)
        self._next_over = self.rng.uniform(60.0, 180.0)
        self._next_sat_dep = (self.rng.uniform(240.0, 480.0)
                              if self.sector["sat"] else float("inf"))
        self._balloon_event = 0  # 0 not yet · 1 aloft · 2 done for the day
        self._center_until = 0.0
        self._center_events = 0
        self._emergencies = 0
        self._nordos = 0
        self._elapsed = 0.0
        self._last_tick = None
        self.hearback_p = 0.05   # odds per transmission of a bad readback
        self.hearbacks = 0       # instructions misheard this shift
        self.hearbacks_caught = 0  # ...and corrected before they stuck
        self.react_s = (1.5, 3.5)  # seconds between readback and hands
        self._atis_n = 0
        self.wind = (360.0, 0.0)
        self._set_wind()
        self._say_atis()         # the shift opens with the numbers
        self._prepopulate()      # a shift starts mid-shift, not empty

    def _set_wind(self):
        """A wind that favours today's runway, held for the whole flow."""
        course = self.sector["course"]
        direction = round((course + self.rng.uniform(-40.0, 40.0))
                          % 360.0 / 10.0) * 10 % 360
        self.wind = (float(direction or 360), float(self.rng.randint(6, 16)))

    def _say_atis(self, update=False):
        head = ("ATIS update, information" if update else "information")
        d, k = self.wind
        self.say(f"{head} {_NATO[self._atis_n % len(_NATO)]} — wind "
                 f"{int(d):03d} at {int(k)}, landing and departing runway "
                 f"{self.sector['rwy']}", "atis")

    def _prepopulate(self):
        """The sector you take over already has traffic in it."""
        for _ in range(6):
            if sum(a["plan"] == "arrival" for a in self.aircraft) >= 2:
                break
            self._spawn_arrival(allow_sat=False)
        arrivals = [a for a in self.aircraft if a["plan"] == "arrival"]
        if len(arrivals) > 1:
            # one of them is already partway in and part-descended — but
            # no closer than a fresh pair of hands can still work, and no
            # higher than a normal profile would have it at that range
            ac = arrivals[-1]
            here = haversine_nm(ac["lat"], ac["lon"],
                                self.airport["lat"], self.airport["lon"])
            ac["lat"], ac["lon"] = advance(
                ac["lat"], ac["lon"], ac["hdg"],
                max(0.0, min(18.0, here - 15.0)))
            dist = haversine_nm(ac["lat"], ac["lon"],
                                self.airport["lat"], self.airport["lon"])
            prof = 1000.0 * ((self.airport["elev"] + dist * 320.0) // 1000)
            ac["alt"] = ac["tgt_alt"] = min(ac["alt"], max(
                self.airport["elev"] + 5000.0, prof))
        self._spawn_departure()
        # the sky you take over was never empty either
        self._spawn_overflight()
        self._spawn_vfr()

    # -- Feed interface -----------------------------------------------------
    def snapshot(self):
        return (list(self.aircraft), dict(self.trails), self.updated,
                self.source, self.error)

    def set_view(self, *_a, **_k):
        pass

    def start_thread(self):
        pass
    start_polling = start_thread

    # -- radio --------------------------------------------------------------
    def say(self, line, kind="pilot"):
        self.radio.append((time.time(), line, kind))
        del self.radio[:-30]

    # -- spawning -----------------------------------------------------------
    def _new_callsign(self):
        pool = POOLS.get(self.airport["country"], WORLD_POOL)
        for _ in range(20):
            airline = self.rng.choice(pool)
            number = str(self.rng.randint(2, 9)) + "".join(
                str(self.rng.randint(0, 9))
                for _ in range(self.rng.randint(1, 3)))
            callsign = airline + number
            if not any(ac["callsign"] == callsign for ac in self.aircraft):
                return callsign, airline
        return f"SIM{self._counter}", "SIM"

    def _cast_flight(self, role):
        """(callsign, actype, far_city|None) — live-sampled when possible.

        The pool holds flights genuinely in the air near this airport
        right now, with their real routes; the synthesized country mix
        only plays when the pool is empty or offline.
        """
        if self.pool is not None:
            pick = self.pool.draw(role)
            if pick is not None and not any(
                    ac["callsign"] == pick[0] for ac in self.aircraft):
                return pick
        callsign, airline = self._new_callsign()
        actype = self.rng.choice(FLEETS.get(airline, ("A320",)))
        return callsign, actype, None

    def _base(self, callsign, actype, lat, lon, alt, hdg, ias):
        self._counter += 1
        return {
            # everything the scope's renderer reads —
            "hex": f"sim{self._counter:04d}", "callsign": callsign,
            "reg": "", "actype": actype, "lat": lat, "lon": lon,
            "alt": alt, "ground": False, "gs": 0.0, "track": hdg,
            "vrate": 0, "squawk": "%04d" % self.rng.choice(
                [n for n in range(1201, 6777)
                 if "8" not in str(n) and "9" not in str(n)]),
            "emergency": False, "fix_time": time.time(),
            # — and the sim's own flight state
            "ias": ias, "hdg": hdg, "tgt_hdg": hdg, "turn_dir": None,
            "tgt_alt": alt, "tgt_ias": ias,
            "perf": PERF.get(actype) or GA_PERF[actype],
            "phase": "cruise",   # cruise | cleared | established | handed
            "plan": "arrival", "fix": None, "rwy": None, "thr": None,
            "course": None, "delay": 0.0,
        }

    def _spawn_arrival(self, allow_sat=True):
        entry = self.rng.choice(self.sector["entries"])
        elat, elon = self.sector["fixes"][entry]
        lat, lon = advance(elat, elon,
                           bearing_to(self.airport["lat"],
                                      self.airport["lon"], elat, elon),
                           self.rng.uniform(0, 4))  # just outside the fix
        # each corner post owns an altitude band, staggered so unworked
        # streams don't conflict with each other — only with your plan
        base = 110 + 10 * self.sector["entries"].index(entry)
        floor = 100.0 * ((self.airport["elev"] + 6000) // 100 + 10)
        band = 100.0 * (base + self.rng.choice((0, 20)))
        # ...but a near gate can't work the full band: nobody checks in
        # higher than a ~3 nm-per-thousand-feet descent can get down from
        dist = haversine_nm(lat, lon, self.airport["lat"],
                            self.airport["lon"])
        cap = 1000.0 * ((self.airport["elev"] + dist * 350.0) // 1000)
        alt = max(floor, min(band, cap))
        # never spawn into an immediate conflict the player couldn't
        # prevent — checked before drawing a cast, so an aborted spawn
        # doesn't burn a real flight from the pool
        for other in self.aircraft:
            if (abs(other["alt"] - alt) < SEP_FT * 1.5
                    and haversine_nm(other["lat"], other["lon"],
                                     lat, lon) < SEP_NM * 3):
                self._next_arrival = 25.0   # try again shortly
                return
        callsign, actype, origin = self._cast_flight("arrival")
        hdg = bearing_to(lat, lon, self.airport["lat"], self.airport["lon"])
        ias = float(self.rng.choice((250, 270, 280)))
        ac = self._base(callsign, actype, lat, lon, alt, hdg, ias)
        # par: the straight-in distance at working speeds plus room for a
        # civilised pattern — beat it and nothing happens, dawdle past it
        # (laps, forgotten holds) and the landing pays less
        sat = (self.sector["sat"] if allow_sat and self.sector["sat"]
               and self.rng.random() < 0.18 else None)
        if sat is not None:
            sat_d = haversine_nm(lat, lon, sat["thr"][0], sat["thr"][1])
            ac.update(plan="arrival", fix=entry, sat=True, tag=sat["code"],
                      rwy=sat["rwy"], thr=sat["thr"], course=sat["course"],
                      felev=float(sat["elev"]), par=sat_d * 16.0 + 300.0)
        else:
            ac.update(plan="arrival", fix=entry, rwy=self.sector["rwy"],
                      thr=self.sector["thr"], course=self.sector["course"],
                      felev=float(self.airport["elev"]),
                      par=dist * 16.0 + 300.0)
        self.aircraft.append(ac)
        where = f" for {sat['name']}" if sat is not None else ""
        tail = f", from {origin}" if origin else ""
        self.say(f"{self.airport['city'] or 'Approach'} approach, "
                 f"{hail(ac)} with you, {say_altitude(alt)}, "
                 f"inbound {entry}{where}{tail}", "checkin")

    def _spawn_departure(self, sat=None):
        """A departure off the main runway — or, given ``sat``, off the
        satellite field, low in the middle of your airspace."""
        callsign, actype, dest = self._cast_flight("departure")
        src = sat or self.sector
        course = src["course"]
        thr = src["thr"]
        lat, lon = advance(thr[0], thr[1], course, 1.5)  # rolling, airborne
        elev = src["elev"]
        exit_fix = self.rng.choice(self.sector["exits"])
        initial = float(round((elev + 3000) / 1000) * 1000)
        ac = self._base(callsign, actype, lat, lon, elev + 1200.0,
                        course, 170.0)
        ac.update(plan="departure", fix=exit_fix, tgt_alt=initial,
                  tgt_ias=250.0, phase="cruise")
        if sat is not None:
            ac.update(sat=True, tag=sat["code"])
        # centre's letter of agreement: some departures carry a crossing
        # restriction, and centre won't take a handoff assigned below it
        note = ""
        if self.rng.random() < 0.35:
            ac["xr"] = 1000.0 * round(
                (self.airport["elev"]
                 + self.rng.choice((7000.0, 9000.0, 11000.0))) / 1000.0)
            note = f" — centre wants {say_altitude(ac['xr'])} crossing it"
        self.aircraft.append(ac)
        off = (f"off {sat['name']}, runway {say_runway(sat['rwy'])}"
               if sat is not None
               else f"off runway {say_runway(self.sector['rwy'])}")
        tail = f", for {dest}" if dest else ""
        self.say(f"{hail(ac)} {off}, "
                 f"passing {say_altitude(ac['alt'])} for "
                 f"{say_altitude(initial)}, requesting {exit_fix}{tail}"
                 f"{note}", "checkin")

    # -- the sky that isn't yours --------------------------------------------
    def _spawn_vfr(self):
        """Somebody's Saturday: a 1200 code wandering the practice area,
        talking to nobody.  Not separation traffic — a hazard you owe a
        traffic call, and the same design move as weather-as-terrain."""
        if sum(ac["plan"] == "vfr" for ac in self.aircraft) >= 3:
            return
        brg = self.rng.uniform(0.0, 360.0)
        lat, lon = advance(self.airport["lat"], self.airport["lon"], brg,
                           self.rng.uniform(12.0, 38.0))
        alt = self.airport["elev"] + self.rng.choice(
            (1700.0, 2200.0, 2700.0, 3500.0, 4500.0, 5500.0))
        # never materialise on top of somebody — the player gets a chance
        # to see every threat coming
        for other in self.aircraft:
            if (abs(other["alt"] - alt) < SEP_FT
                    and haversine_nm(other["lat"], other["lon"],
                                     lat, lon) < SEP_NM * 2):
                return
        tail = ("N" + str(self.rng.randint(1, 9))
                + "".join(str(self.rng.randint(0, 9))
                          for _ in range(self.rng.randint(1, 2)))
                + "".join(self.rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")
                          for _ in range(2)))
        actype = self.rng.choice(("C172", "PA28", "C182", "SR22", "DA40"))
        ac = self._base(tail, actype, lat, lon, alt,
                        self.rng.uniform(0.0, 360.0),
                        float(GA_PERF[actype][0]))
        ac.update(plan="vfr", squawk="1200", limited=True,
                  vfr_turn=self._elapsed + self.rng.uniform(20.0, 90.0),
                  vfr_leave=self._elapsed + self.rng.uniform(360.0, 900.0))
        self.aircraft.append(ac)

    def _spawn_overflight(self):
        """Centre's traffic, four miles above yours: a blip sliding across
        the top of the sector in a dim block.  Scenery — the way most of
        what a real scope shows is scenery."""
        if sum(ac["plan"] == "overflight" for ac in self.aircraft) >= 3:
            return
        callsign = actype = None
        if self.pool is not None:
            pick = self.pool.draw("overflight")
            if pick is not None and not any(
                    a["callsign"] == pick[0] for a in self.aircraft):
                callsign, actype = pick[0], pick[1]
        if callsign is None:
            callsign, airline = self._new_callsign()
            actype = self.rng.choice(FLEETS.get(airline, ("A320",)))
        brg = self.rng.uniform(0.0, 360.0)
        lat, lon = advance(self.airport["lat"], self.airport["lon"],
                           brg, DESPAWN_NM - 4.0)
        side = (brg + self.rng.choice((90.0, -90.0))) % 360.0
        aim = advance(self.airport["lat"], self.airport["lon"], side,
                      self.rng.uniform(0.0, 22.0))
        hdg = bearing_to(lat, lon, aim[0], aim[1])
        # hemispheric flight levels, because someone will check
        alt = 1000.0 * (2 * self.rng.randint(14, 19)
                        + (1 if hdg < 180.0 else 0))
        ac = self._base(callsign, actype, lat, lon, alt, hdg,
                        float(self.rng.randint(255, 290)))
        ac.update(plan="overflight", dim=True)
        self.aircraft.append(ac)

    def _spawn_balloons(self):
        """A calm morning's reward: hot air balloons, the one aircraft
        that renders the wind — near-stationary targets drifting at
        exactly the speed and direction the ATIS read out."""
        self._balloon_event = 1
        brg = self.rng.uniform(0.0, 360.0)
        base_d = self.rng.uniform(7.0, 16.0)
        top = 0.0
        for i in range(self.rng.randint(2, 3)):
            lat, lon = advance(self.airport["lat"], self.airport["lon"],
                               (brg + self.rng.uniform(-12.0, 12.0)) % 360.0,
                               base_d + self.rng.uniform(0.0, 3.0))
            alt = self.airport["elev"] + self.rng.uniform(1400.0, 3800.0)
            top = max(top, alt)
            ac = self._base(f"BLN{i + 1}", "BALN", lat, lon, alt,
                            (self.wind[0] + 180.0) % 360.0, 0.0)
            ac.update(plan="balloon", squawk="1200", limited=True,
                      glyph="○", balloon_down=self._elapsed
                      + self.rng.uniform(420.0, 780.0))
            self.aircraft.append(ac)
        octant = ("north", "northeast", "east", "southeast", "south",
                  "southwest", "west", "northwest")[round(brg / 45.0) % 8]
        self.say(f"caution — hot air balloons {octant} of the field, "
                 f"below {say_altitude(1000.0 * math.ceil(top / 1000.0))}, "
                 "drifting with the wind", "atis")

    def _ambient_tick(self, dt):
        self._next_vfr -= dt
        self._next_over -= dt
        if self._next_vfr <= 0:
            self._spawn_vfr()
            self._next_vfr = self.rng.uniform(150.0, 420.0)
        if self._next_over <= 0:
            self._spawn_overflight()
            self._next_over = self.rng.uniform(120.0, 300.0)
        if (self._balloon_event == 0 and self._elapsed > 240.0
                and self.wind[1] <= 10.0
                and self.rng.random() <= dt / 1200.0):
            self._spawn_balloons()
        for ac in self.aircraft:
            if ac["plan"] == "vfr":
                if self._elapsed >= ac["vfr_leave"]:
                    # done for the day: point away from the field, go home
                    ac["vfr_leave"] = ac["vfr_turn"] = float("inf")
                    ac["tgt_hdg"] = bearing_to(
                        self.airport["lat"], self.airport["lon"],
                        ac["lat"], ac["lon"])
                    ac["turn_dir"] = None
                elif self._elapsed >= ac["vfr_turn"]:
                    ac["vfr_turn"] = (self._elapsed
                                      + self.rng.uniform(25.0, 90.0))
                    ac["tgt_hdg"] = (ac["hdg"]
                                     + self.rng.uniform(-70.0, 70.0)) % 360.0
                    if self.rng.random() < 0.25:
                        ac["tgt_alt"] = max(
                            self.airport["elev"] + 1200.0,
                            min(self.airport["elev"] + 5800.0,
                                ac["alt"] + self.rng.choice((-500.0, 500.0))))
            elif (ac["plan"] == "balloon"
                    and self.rng.random() < dt / 25.0):
                ac["tgt_alt"] = max(
                    self.airport["elev"] + 800.0,
                    min(self.airport["elev"] + 4200.0,
                        ac["alt"] + self.rng.uniform(-400.0, 400.0)))

    def _rwy_closed(self):
        return self._elapsed < self._rwy_closed_until

    def _spawn_tick(self, dt):
        self._next_arrival -= dt
        self._next_departure -= dt
        # traffic breathes: quiet spells, then a bank comes off the
        # boundary all at once — the alternation of calm and slam is
        # what makes a shift feel like a shift
        if (self._elapsed >= self._next_push
                and self._push_until <= self._elapsed):
            self._push_until = self._elapsed + self.rng.uniform(180.0, 300.0)
            self._next_push = self._push_until + self.rng.uniform(420.0,
                                                                  900.0)
            self.say("centre calls the push — a bank of arrivals is "
                     "coming off the boundary", "atc")
        active = sum(1 for ac in self.aircraft
                     if _controlled(ac) and ac["phase"] != "handed")
        base = min(34.0, 16.0 + self._elapsed / 75.0)   # per hour, ramping
        rate = base * (2.4 if self._elapsed < self._push_until else 0.75)
        if self._next_arrival <= 0 and active < 16:
            self._spawn_arrival()
            self._next_arrival = max(
                35.0, self.rng.expovariate(rate / 3600.0))
        if self._rwy_closed():
            self._next_departure = max(self._next_departure, 15.0)
        if self._next_departure <= 0 and active < 16:
            # tower meters releases: nobody rolls while the previous
            # departure is still climbing out close-in on runway heading
            blocked = any(
                ac["plan"] == "departure" and ac["phase"] == "cruise"
                and haversine_nm(ac["lat"], ac["lon"], self.airport["lat"],
                                 self.airport["lon"]) < 7.0
                for ac in self.aircraft)
            if blocked:
                self._next_departure = 20.0
            else:
                self._spawn_departure()
                # departures follow the day, not the arrival push
                self._next_departure = max(
                    45.0, self.rng.expovariate(base * 0.7 / 3600.0))
        self._next_sat_dep -= dt
        if self._next_sat_dep <= 0 and active < 16:
            self._spawn_departure(sat=self.sector["sat"])
            self._next_sat_dep = self.rng.uniform(300.0, 600.0)

    # -- flying -------------------------------------------------------------
    def _stage(self, ac, due, **fields):
        """Readback now, hands on the bug a beat later: target changes
        wait out the pilot's reaction time before they start to fly."""
        if due is None or due <= self._elapsed:
            ac.update(fields)
            return
        pend = ac.get("pend") or {}
        pend["due"] = due
        pend.update(fields)
        ac["pend"] = pend

    def _flush_pend(self, ac):
        """Whatever was staged happens now — a new transmission means the
        pilot has certainly finished acting on the last one."""
        pend = ac.pop("pend", None)
        if pend:
            ac.update({k: v for k, v in pend.items() if k != "due"})

    def _fly(self, ac, dt):
        cruise, min_clean, app_kt, climb, descend = ac["perf"]

        pend = ac.get("pend")
        if pend is not None and self._elapsed >= pend["due"]:
            ac["pend"] = None
            ac.update({k: v for k, v in pend.items() if k != "due"})

        if ac["phase"] in ("cleared", "established"):
            self._fly_ils(ac)
        elif ac["phase"] == "hold":
            # a lazy right-hand orbit around the holding point
            hlat, hlon = ac["hold_at"]
            if haversine_nm(ac["lat"], ac["lon"], hlat, hlon) > 2.5:
                ac["tgt_hdg"] = bearing_to(ac["lat"], ac["lon"], hlat, hlon)
                ac["turn_dir"] = None
            else:
                ac["tgt_hdg"] = (ac["hdg"] + 45.0) % 360.0
                ac["turn_dir"] = "r"

        # heading: standard-rate toward target, honouring a forced direction
        delta = turn_delta(ac["hdg"], ac["tgt_hdg"], ac["turn_dir"])
        step = TURN_RATE * dt
        if abs(delta) <= step:
            ac["hdg"] = ac["tgt_hdg"]
            ac["turn_dir"] = None
        else:
            ac["hdg"] = (ac["hdg"] + math.copysign(step, delta)) % 360.0

        # altitude — descents respect the terrain under them (the ILS is a
        # surveyed path, so a coupled approach may go below the grid's MVA)
        tgt_alt = ac["tgt_alt"]
        if (self.terrain is not None and tgt_alt < ac["alt"]
                and _controlled(ac)
                and ac["phase"] not in ("cleared", "established")):
            floor = self.terrain.mva_at(ac["lat"], ac["lon"])
            if floor is not None and tgt_alt < floor:
                tgt_alt = floor
                if ac["alt"] <= floor + 300.0 and not ac.get("terrain_stop"):
                    ac["terrain_stop"] = True
                    self.say(f"{hail(ac)} leveling at "
                             f"{say_altitude(floor)}, terrain below us",
                             "request")
            elif ac.get("terrain_stop"):
                ac["terrain_stop"] = False   # clear of the high ground
        diff = tgt_alt - ac["alt"]
        rate = climb if diff > 0 else descend
        step_ft = rate * dt / 60.0
        if abs(diff) <= step_ft:
            ac["alt"] = tgt_alt
            ac["vrate"] = 0
        else:
            ac["alt"] += math.copysign(step_ft, diff)
            ac["vrate"] = int(math.copysign(rate, diff))

        # speed, and the ground speed it buys at altitude
        sdiff = ac["tgt_ias"] - ac["ias"]
        sstep = ACCEL_KT_S * dt
        ac["ias"] = (ac["tgt_ias"] if abs(sdiff) <= sstep
                     else ac["ias"] + math.copysign(sstep, sdiff))
        tas = ac["ias"] * (1.0 + ac["alt"] * 2e-5)

        # the wind triangle: they point where you said, the air moves —
        # heading and track diverge, and the scope draws the truth
        wdir, wkt = self.wind
        if wkt:
            rad_h = math.radians(ac["hdg"])
            rad_w = math.radians(wdir + 180.0)   # blowing toward
            vx = tas * math.sin(rad_h) + wkt * math.sin(rad_w)
            vy = tas * math.cos(rad_h) + wkt * math.cos(rad_w)
            ac["gs"] = math.hypot(vx, vy)
            ac["track"] = math.degrees(math.atan2(vx, vy)) % 360.0
        else:
            ac["gs"] = tas
            ac["track"] = ac["hdg"]

        ac["lat"], ac["lon"] = advance(ac["lat"], ac["lon"], ac["track"],
                                       ac["gs"] * dt / 3600.0)
        ac["fix_time"] = time.time()

    def _fly_ils(self, ac):
        """Capture and ride the localizer, then the glideslope, then land."""
        cross, along = cross_along_track(ac["lat"], ac["lon"],
                                         ac["thr"][0], ac["thr"][1],
                                         ac["course"])
        if ac["phase"] == "cleared":
            # lead the turn: capture when starting a standard-rate turn
            # *now* would roll out on the localizer — so a sane intercept
            # vector (anything under ~90° across) locks on without the
            # player having to thread a needle
            theta = abs(turn_delta(ac["hdg"], ac["course"]))
            turn_radius = ac["gs"] / 188.5           # nm, standard rate
            lead = turn_radius * (1.0 - math.cos(
                math.radians(min(theta, 90.0)))) + 0.2
            window = lead if theta < 90.0 else 0.45
            if abs(cross) < window and along > 0.5 and theta < 110.0:
                ac["phase"] = "established"
                ac["turn_dir"] = None
                self.say(f"{hail(ac)} established, "
                         f"runway {ac['rwy']}", "pilot")
            else:
                return
        # established: track the centreline with a proportional nudge…
        ac["tgt_hdg"] = (ac["course"]
                         + max(-30.0, min(30.0, -cross * 40.0))) % 360.0
        ac["turn_dir"] = None
        # …descend on a 3° slope once it comes down to meet you…
        elev = ac.get("felev", self.sector["elev"])
        gs_alt = elev + along * GS_FT_PER_NM
        if gs_alt < ac["alt"]:
            ac["tgt_alt"] = max(elev, gs_alt)
        # …and slow to approach speed inside six miles
        if along < 6.0:
            ac["tgt_ias"] = float(ac["perf"][2])
        elif ac["tgt_ias"] > 190.0:
            ac["tgt_ias"] = 180.0
        # a closed runway turns short final around — through no fault of
        # yours, so the go-around is free; the workload is the price
        # (the satellite field keeps its own counsel)
        if along < 2.0 and self._rwy_closed() and not ac.get("sat"):
            self._go_around(ac, "the runway's still occupied, keep us in "
                            "the pattern", cost=0)
            return
        if along < 5.5 and ac.get("tower_handoff") is None:
            ac["tower_handoff"] = True
            self.say(f"{hail(ac)}, contact tower. "
                     "Good day.", "atc")
        # an unstable approach goes around: still hot or high on short
        # final means the clearance came too late or too fast
        if along < 1.5 and (ac["ias"] > ac["perf"][2] + 25.0
                            or ac["alt"] > gs_alt + 500.0):
            why = ("too fast" if ac["ias"] > ac["perf"][2] + 25.0
                   else "too high")
            self._go_around(ac, f"{why}, give us vectors when you can")
            return
        if along < 0.35 or ac["alt"] <= elev + 30.0:
            ac["phase"] = "landed"

    def _go_around(self, ac, reason, cost=50):
        """Break an approach off: climb out on runway heading, yours again."""
        ac.update(phase="cruise", tower_handoff=None, pend=None,
                  tgt_hdg=ac["course"], turn_dir=None,
                  tgt_ias=max(ac["perf"][2] + 40.0, 180.0), wake_warned=False)
        ac["tgt_alt"] = float(
            round((ac.get("felev", self.sector["elev"]) + 3000.0)
                  / 1000.0) * 1000.0)
        self.score -= cost
        self.go_arounds += 1
        self.say(f"{hail(ac)} going around — {reason}", "alert")

    def _wake_final(self):
        """In-trail wake minima on final: three miles is legal behind a
        737 and dangerous behind a heavy.  The follower warns once inside
        a mile of the minimum; below it they protect themselves."""
        finals = {}
        for ac in self.aircraft:
            if ac["phase"] != "established":
                continue
            _cross, along = cross_along_track(ac["lat"], ac["lon"],
                                              ac["thr"][0], ac["thr"][1],
                                              ac["course"])
            key = (ac["rwy"], round(ac["course"]))
            finals.setdefault(key, []).append((along, ac))
        for stream in finals.values():
            stream.sort(key=lambda pair: pair[0])
            for (lead_at, leader), (foll_at, follower) in zip(stream,
                                                              stream[1:]):
                cat = WAKE.get(leader["actype"])
                need = WAKE_NM.get(cat)
                if need is None or follower["squawk"] == "7700":
                    continue     # standard 3 nm applies; the monitor has it
                gap = foll_at - lead_at
                if gap < need:
                    self._go_around(follower,
                                    f"we're inside {say_digits(int(need))} "
                                    f"miles of the {_WAKE_WORD[cat]} ahead")
                elif gap < need + 1.0 and not follower.get("wake_warned"):
                    follower["wake_warned"] = True
                    self.say(f"{hail(follower)}, we're closing on the "
                             f"{_WAKE_WORD[cat]} ahead — we can take a "
                             "little speed off", "request")

    # -- world tick ---------------------------------------------------------
    def tick(self, now=None):
        now = time.time() if now is None else now
        if self._last_tick is None:
            self._last_tick = now
            return
        dt = min(now - self._last_tick, 3.0)   # clamp a paused frame
        self._last_tick = now
        if dt <= 0:
            return
        self._elapsed += dt

        if self._rwy_closed_until and not self._rwy_closed():
            self._rwy_closed_until = 0.0
            self.say(f"runway {self.sector['rwy']} back open — "
                     "resume approaches", "atis")
        self._spawn_tick(dt)
        self._ambient_tick(dt)
        for ac in self.aircraft:
            self._fly(ac, dt)
            ac["delay"] += dt
        self._requests(dt)
        self._weather_tick(dt)
        self._flow_tick(dt)
        self._center_tick(dt)
        self._emergency_tick(dt)
        self._nordo_tick(dt)
        self._wake_final()
        self._separation()
        self._trails(now)
        self._retire()
        self.updated = now

    # -- weather ------------------------------------------------------------
    def _wx_ahead(self, ac, hdg, dist_nm=10.0, samples=5):
        """Worst radar echo (0..1) along a heading; 0.0 when unknown."""
        if self.wx_sample is None:
            return 0.0
        worst = 0.0
        for i in range(1, samples + 1):
            plat, plon = advance(ac["lat"], ac["lon"], hdg,
                                 dist_nm * i / samples)
            worst = max(worst, self.wx_sample(plat, plon) or 0.0)
        return worst

    def _wx_worst(self, lat, lon, hdg, near_nm, far_nm, step_nm=1.0):
        """Worst echo along a track segment [near, far] nm out from a point.

        Unlike _wx_ahead this starts from an arbitrary spot, not an
        aircraft — used to sniff the final approach path for a cell.
        """
        if self.wx_sample is None:
            return 0.0
        worst = 0.0
        d = near_nm
        while d <= far_nm + 1e-6:
            plat, plon = advance(lat, lon, hdg, d)
            worst = max(worst, self.wx_sample(plat, plon) or 0.0)
            d += step_nm
        return worst

    def _weather_tick(self, dt):
        """Pilots don't fly into cells: they ask, then they act."""
        if self.wx_sample is None:
            return
        for ac in self.aircraft:
            if (ac["phase"] not in ("cruise", "hold")
                    or not _controlled(ac)       # VFR dodges its own cells
                    or ac["squawk"] == "7700"
                    or ac.get("nordo_until")):   # nobody to ask with
                continue
            ahead = self._wx_ahead(ac, ac["hdg"], 6.0)
            if ac.get("wx_deviating"):
                if ahead < WX_CLEAR:
                    ac["wx_deviating"] = False
                    ac["wx_asked_t"] = None
                    self.say(f"{hail(ac)} clear of weather,"
                             " ready for a vector", "request")
                continue
            if ahead < WX_DEVIATE:
                ac["wx_asked_t"] = None
                continue
            side = ("left" if self._wx_ahead(ac, (ac["hdg"] - 30.0) % 360.0)
                    <= self._wx_ahead(ac, (ac["hdg"] + 30.0) % 360.0)
                    else "right")
            if ac.get("wx_asked_t") is None:
                ac["wx_asked_t"] = self._elapsed
                self.say(f"{hail(ac)} requesting 30 "
                         f"{side} for weather", "request")
            elif self._elapsed - ac["wx_asked_t"] > 20.0:
                # ignored long enough: they protect themselves — and
                # whatever you had staged goes out the cockpit window
                ac["wx_deviating"] = True
                ac["pend"] = None
                delta = -30.0 if side == "left" else 30.0
                ac["tgt_hdg"] = (ac["hdg"] + delta) % 360.0
                ac["turn_dir"] = side[0]
                if ac["phase"] == "hold":
                    ac["phase"] = "cruise"
                self.say(f"{hail(ac)} deviating {side}, "
                         "will advise clear", "request")

    # -- the day changes ------------------------------------------------------
    def _flow_tick(self, dt):
        """Eventually the wind comes around, and the airport turns with it."""
        self._next_flow -= dt
        if self._next_flow > 0:
            return
        self._next_flow = self.rng.uniform(900.0, 1500.0)
        runway = self.airport["rwys"][0]
        new_end = "he" if self.sector["end"] == "le" else "le"
        ident, course, thr = _end_geometry(self.airport, runway, new_end)
        self.sector.update(rwy=ident, course=course, thr=thr, end=new_end)
        if self.sector["sat_apt"] is not None:
            self.sector["sat"] = _sat_end(self.sector["sat_apt"], course)
        self.sector_rev += 1
        self._atis_n += 1
        self._set_wind()         # the wind that turned the airport
        self._say_atis(update=True)
        for ac in self.aircraft:
            if ac["plan"] == "arrival" and ac["phase"] == "cleared":
                # not yet established: their clearance dies with the flow
                ac["phase"] = "cruise"
                expect = (self.sector["sat"]["rwy"] if ac.get("sat")
                          else ident)
                self.say(f"{hail(ac)}, cancel approach "
                         f"clearance, fly present heading, expect runway "
                         f"{expect}", "atc")

    # -- centre next door -------------------------------------------------------
    def _center_closed(self):
        return self._elapsed < self._center_until

    def _center_tick(self, dt):
        """Centre is a character too, and sometimes their sector is full:
        for a couple of minutes nothing gets handed off, and the boundary
        you normally throw departures over becomes a wall."""
        if self._center_until and self._elapsed >= self._center_until:
            self._center_until = 0.0
            self.say("centre calls back — they'll take handoffs again",
                     "atc")
            return
        if (self._center_events >= 1 or self._center_until
                or self._elapsed < 600.0
                or self.rng.random() > dt / 1500.0):
            return
        self._center_events = 1
        self._center_until = self._elapsed + self.rng.uniform(70.0, 120.0)
        self.say("centre calls — their sector's full, no handoffs for the "
                 "next couple of minutes; keep your departures inside the "
                 "boundary", "atc")

    # -- emergencies ----------------------------------------------------------
    def _declare_emergency(self, ac):
        ac["squawk"] = "7700"    # the blip goes red and stays red
        ac["mayday_t"] = self._elapsed
        self._emergencies += 1
        self.bell = True
        self.say(f"MAYDAY, MAYDAY — {hail(ac)} declaring "
                 "a medical emergency, request priority to the field",
                 "alert")

    def _emergency_tick(self, dt):
        if self._emergencies >= 1 or self._elapsed < 600.0:
            return
        if self.rng.random() > dt / 1500.0:
            return
        candidates = [ac for ac in self.aircraft
                      if ac["plan"] == "arrival" and ac["phase"] == "cruise"
                      and not ac.get("sat")     # the equipment is here
                      and ac["alt"] > 6000.0]
        if candidates:
            self._declare_emergency(self.rng.choice(candidates))

    # -- lost comms -----------------------------------------------------------
    def _declare_nordo(self, ac):
        """Radios fail: squawk 7600, last clearance flown, nobody home."""
        ac["squawk"] = "7600"    # the blip goes red; the frequency goes quiet
        ac["nordo_until"] = self._elapsed + self.rng.uniform(150.0, 240.0)
        self._nordos += 1
        self.bell = True
        self.say(f"{ac['callsign']} squawking seven six zero zero — "
                 "radio failure, they'll fly their last clearance", "alert")

    def _nordo_tick(self, dt):
        for ac in self.aircraft:
            until = ac.get("nordo_until")
            if until is not None and self._elapsed >= until:
                ac["nordo_until"] = None
                ac["squawk"] = "%04d" % self.rng.choice(
                    [n for n in range(1201, 6777)
                     if "8" not in str(n) and "9" not in str(n)])
                self.say(f"{hail(ac)} back with you — sorry, we had a "
                         "radio failure. Say again anything we missed",
                         "checkin")
        if self._nordos >= 1 or self._elapsed < 900.0:
            return
        if self.rng.random() > dt / 2400.0:
            return
        candidates = [ac for ac in self.aircraft
                      if _controlled(ac)
                      and ac["phase"] in ("cruise", "hold")
                      and ac["squawk"] not in ("7600", "7700")
                      and not ac.get("mayday_t")]
        if len(candidates) >= 4:     # a quiet scope makes a dull failure
            self._declare_nordo(self.rng.choice(candidates))

    def _requests(self, dt):
        """Now and then somebody on frequency wants something."""
        self._next_request -= dt
        if self._next_request > 0:
            return
        self._next_request = 120.0 + self.rng.expovariate(1.0 / 120.0)
        wanting = []
        for ac in self.aircraft:
            if (ac.get("asked") or ac["phase"] != "cruise"
                    or ac.get("nordo_until")):
                continue
            if (ac["plan"] == "arrival" and ac["alt"] > 9000.0
                    and ac["tgt_alt"] >= ac["alt"]):
                wanting.append((ac, "requesting lower"))
            elif (ac["plan"] == "departure"
                  and ac["alt"] >= ac["tgt_alt"] - 200.0):
                verb = self.rng.choice(
                    ("requesting higher", f"requesting direct {ac['fix']}"))
                wanting.append((ac, verb))
        if wanting:
            ac, want = self.rng.choice(wanting)
            ac["asked"] = True
            self.say(f"{hail(ac)} {want}", "request")

    def _retire(self):
        keep = []
        had_balloons = any(ac["plan"] == "balloon" for ac in self.aircraft)
        for ac in self.aircraft:
            if not _controlled(ac):
                # nobody's traffic comes and goes on its own: no score,
                # no radio, no ledger — the sky just breathes
                down = ac.get("balloon_down")
                gone = (down is not None and self._elapsed >= down) or (
                    haversine_nm(ac["lat"], ac["lon"], self.airport["lat"],
                                 self.airport["lon"]) > DESPAWN_NM)
                if gone:
                    self.trails.pop(ac["hex"], None)
                else:
                    keep.append(ac)
                continue
            if ac["phase"] == "landed":
                self.landed += 1
                # a landing is worth 100 flown at par; every six seconds
                # spent over par shaves a point (down to 20 — a landing
                # is never worth nothing)
                par = ac.get("par")
                extra = max(0.0, ac["delay"] - par) if par else 0.0
                self.score += 100 - min(80, int(extra / 6.0))
                if par:
                    self.offered += 100
                    self._delay_extra += extra
                    self._delay_n += 1
                if ac.get("mayday_t") is not None:
                    quick = self._elapsed - ac["mayday_t"] < 720.0
                    self.score += 300 if quick else 100
                    self.say(f"{hail(ac)} — thanks for "
                             "the help, medics are meeting us", "checkin")
                    # the ambulance owns the runway for a few minutes:
                    # approaches wave off, departures wait, final backs
                    # up, and holding suddenly earns its keep
                    self._rwy_closed_until = (self._elapsed
                                              + self.rng.uniform(180.0,
                                                                 300.0))
                    self.bell = True
                    self.say(f"runway {self.sector['rwy']} closed — "
                             "equipment meeting the emergency, expect "
                             "delays", "atis")
                    for other in self.aircraft:
                        if other["phase"] == "cleared":
                            other["phase"] = "cruise"
                            self.say(f"{hail(other)}, cancel approach "
                                     "clearance, fly present heading — "
                                     "the runway is closed for the moment",
                                     "atc")
                self.trails.pop(ac["hex"], None)
                continue
            dist = haversine_nm(ac["lat"], ac["lon"],
                                self.airport["lat"], self.airport["lon"])
            if dist > DESPAWN_NM:
                if ac["phase"] == "handed":
                    self.departed += 1
                    self.offered += 50
                elif ac["plan"] == "arrival":
                    self.score -= 100
                    self.offered += 100
                    self.diversions += 1
                    self.say(f"{ac['callsign']} diverted — flew out of "
                             "your airspace unworked", "alert")
                else:
                    self.score -= 100
                    self.offered += 50
                    self.say(f"{ac['callsign']} left the sector "
                             "without a handoff", "alert")
                self.trails.pop(ac["hex"], None)
                continue
            keep.append(ac)
        if (had_balloons and self._balloon_event == 1
                and not any(ac["plan"] == "balloon" for ac in keep)):
            self._balloon_event = 2
            self.say("the balloons are down — caution cancelled", "atis")
        self.aircraft = keep

    def _separation(self):
        floor = self.airport["elev"] + SEP_FLOOR_AGL
        current = set()
        flying = [ac for ac in self.aircraft
                  if ac["phase"] != "handed" and ac["alt"] > floor]
        yours = [ac for ac in flying if _controlled(ac)]
        others = [ac for ac in flying if not _controlled(ac)]
        for ac in self.aircraft:
            ac["emergency"] = False
            ac["ca"] = False
        for i, a in enumerate(yours):
            for b in yours[i + 1:]:
                if abs(a["alt"] - b["alt"]) >= SEP_FT:
                    continue
                if haversine_nm(a["lat"], a["lon"],
                                b["lat"], b["lon"]) >= SEP_NM:
                    continue
                pair = tuple(sorted((a["hex"], b["hex"])))
                current.add(pair)
                a["emergency"] = b["emergency"] = True
                if pair not in self._bust_pairs:
                    self.busts += 1
                    self.score -= 500
                    self.bell = True
                    self.say(f"LOSS OF SEPARATION — {a['callsign']} and "
                             f"{b['callsign']}", "alert")
        self._bust_pairs = current
        # the uncontrolled sky: no three-mile rule against a 1200 code,
        # only the near-miss you should have called traffic on — and a
        # pilot holding the target in sight never has one
        nmacs = set()
        for a in yours:
            for t in others:
                if t["hex"] in a.get("visual", ()):
                    continue
                if abs(a["alt"] - t["alt"]) >= NMAC_FT:
                    continue
                if haversine_nm(a["lat"], a["lon"],
                                t["lat"], t["lon"]) >= NMAC_NM:
                    continue
                pair = tuple(sorted((a["hex"], t["hex"])))
                nmacs.add(pair)
                a["emergency"] = t["emergency"] = True
                if pair not in self._nmac_pairs:
                    self.nmacs += 1
                    self.score -= 200
                    self.bell = True
                    what = ("the balloon" if t["plan"] == "balloon"
                            else "the VFR traffic")
                    self.say(f"TRAFFIC ALERT — {a['callsign']} and {what}, "
                             f"{say_altitude(t['alt'])}", "alert")
        self._nmac_pairs = nmacs
        # conflict alert: straight-line projection, the way the real box
        # does it — both blips blink before the loss, while there's still
        # a turn that saves it.  Final is the wake monitor's business.
        for i, a in enumerate(yours):
            for b in yours[i + 1:]:
                if a["emergency"] and b["emergency"]:
                    continue     # already lost; solid red says so
                if (a["phase"] == "established"
                        and b["phase"] == "established"):
                    continue
                if abs(self._proj_alt(a) - self._proj_alt(b)) >= SEP_FT:
                    continue
                pa = advance(a["lat"], a["lon"], a["track"],
                             a["gs"] * CA_LOOK_S / 3600.0)
                pb = advance(b["lat"], b["lon"], b["track"],
                             b["gs"] * CA_LOOK_S / 3600.0)
                if haversine_nm(pa[0], pa[1], pb[0], pb[1]) < SEP_NM:
                    a["ca"] = b["ca"] = True
        # …and the same projection against the traffic nobody controls,
        # at hazard scale rather than separation scale
        for a in yours:
            for t in others:
                if t["hex"] in a.get("visual", ()):
                    continue
                if a["emergency"] and t["emergency"]:
                    continue
                if abs(self._proj_alt(a) - self._proj_alt(t)) >= NMAC_FT:
                    continue
                pa = advance(a["lat"], a["lon"], a["track"],
                             a["gs"] * CA_LOOK_S / 3600.0)
                pt = advance(t["lat"], t["lon"], t["track"],
                             t["gs"] * CA_LOOK_S / 3600.0)
                if haversine_nm(pa[0], pa[1], pt[0], pt[1]) < 1.5:
                    a["ca"] = t["ca"] = True

    @staticmethod
    def _proj_alt(ac):
        """Altitude CA_LOOK_S from now: the current rate, capped at the
        level-off — nobody blows through an assigned altitude."""
        if not ac["vrate"]:
            return ac["alt"]
        delta = ac["vrate"] * CA_LOOK_S / 60.0
        room = ac["tgt_alt"] - ac["alt"]
        return ac["alt"] + (max(delta, room) if delta < 0
                            else min(delta, room))

    def _trails(self, now):
        for ac in self.aircraft:
            trail = self.trails.setdefault(ac["hex"], [])
            if not trail or now - trail[-1][2] >= TRAIL_MIN_GAP_S:
                trail.append((ac["lat"], ac["lon"], now))
                del trail[:-TRAIL_MAX_FIXES]

    # -- the frequency ------------------------------------------------------
    def command(self, text):
        """One transmission: parse, validate, apply, and answer.

        Returns the response line for the radio log; every path speaks —
        errors come back as pilot (or facility) talk, not stack traces.

        Now and then a pilot mishears a number and reads back what they
        heard — the readback line is the only tell, and they will fly
        what they said unless the instruction is issued again.  Catching
        it is hearback, and it's why controllers listen to readbacks.
        """
        try:
            query, instructions = parse(text)
            roster = [a for a in self.aircraft if a["phase"] != "handed"]
            try:
                ac = resolve_callsign(query,
                                      [a for a in roster if _controlled(a)])
            except CommandError:
                # maybe they keyed a target that was never theirs to call
                try:
                    ghost = resolve_callsign(
                        query, [a for a in roster if not _controlled(a)])
                except CommandError:
                    raise   # the original "nobody answers" stands
                what = {"overflight": "they're with centre",
                        "balloon": "that's a balloon",
                        "vfr": "a VFR target squawking twelve hundred"
                        }[ghost["plan"]]
                raise CommandError(f"{ghost['callsign']} isn't on your "
                                   f"frequency — {what}")
            if ac.get("nordo_until"):
                raise CommandError(f"nothing heard back from "
                                   f"{ac['callsign']} — they're NORDO, "
                                   "squawking seven six zero zero")
            self._flush_pend(ac)
            due = self._elapsed + self.rng.uniform(*self.react_s)
            bad_idx = self._mishear_roll(ac, instructions)
            phrases = []
            for i, ins in enumerate(instructions):
                if (ac.get("misheard_kind") == ins["kind"]
                        and self._elapsed - ac.get("misheard_t", 0.0) < 45.0):
                    # the same kind of instruction, again, quickly: the
                    # controller caught the bad readback and fixed it
                    self.hearbacks_caught += 1
                    ac["misheard_kind"] = None
                if i == bad_idx:
                    heard = self._mishear(ins)
                    if heard is not None:
                        try:
                            phrases.append(self._apply(ac, heard, due))
                            self.hearbacks += 1
                            ac["misheard_kind"] = ins["kind"]
                            ac["misheard_t"] = self._elapsed
                            continue
                        except CommandError:
                            pass   # the mishearing was unflyable: heard right
                phrases.append(self._apply(ac, ins, due))
        except CommandError as exc:
            line = str(exc)
            self.say(line, "error")
            return line
        line = f"{hail(ac)}, {', '.join(phrases)}."
        self.say(line, "readback")
        return line

    def _mishear_roll(self, ac, instructions):
        """Index of the instruction to mishear this transmission, or None."""
        if (self.hearback_p <= 0.0 or self._elapsed < 180.0
                or ac["squawk"] == "7700"
                or self.rng.random() >= self.hearback_p):
            return None
        idxs = [i for i, ins in enumerate(instructions)
                if ins["kind"] in ("turn", "alt")
                or (ins["kind"] == "speed" and ins["kt"] is not None)]
        return self.rng.choice(idxs) if idxs else None

    def _mishear(self, ins):
        """A plausibly-wrong copy of an instruction — one value off, the
        way numbers actually get garbled on a scratchy frequency."""
        if ins["kind"] == "turn":
            hdg = (ins["hdg"] + self.rng.choice((-20, -10, 10, 20))) % 360
            return {**ins, "hdg": hdg or 360}
        if ins["kind"] == "alt":
            alt = ins["alt_ft"] + self.rng.choice((-1000, 1000))
            return {**ins, "alt_ft": alt} if 2000 <= alt <= 45000 else None
        if ins["kind"] == "speed":
            return {**ins, "kt": ins["kt"] + self.rng.choice((-10, 10))}
        return None

    def _wx_check(self, ac, new_hdg):
        """Refuse a vector into a cell the pilot can see on their radar.

        Only when the new heading is meaningfully worse than the current
        one — if they're already in the soup, any instruction that helps
        is welcome.  Emergencies take whatever gets them down fastest.
        """
        if ac["squawk"] == "7700":
            return
        new_wx = self._wx_ahead(ac, new_hdg)
        if (new_wx < WX_DEVIATE
                or new_wx <= self._wx_ahead(ac, ac["hdg"]) + WX_WORSE):
            return
        me = hail(ac)
        side = ("left" if self._wx_ahead(ac, (new_hdg - 40.0) % 360.0)
                <= self._wx_ahead(ac, (new_hdg + 40.0) % 360.0)
                else "right")
        raise CommandError(f"unable — that heading puts {me} into a cell, "
                           f"we could take further {side}")

    def _apply(self, ac, ins, due=None):
        """Apply one instruction; return its readback phrase.

        Direction verbs are checked against the aircraft's state — the
        game never quietly fixes a wrong one (holding the picture is the
        point), it hands the mic to a puzzled pilot instead.  Validation
        happens now; the hands move at ``due`` (see ``_stage``).
        """
        me = hail(ac)
        kind = ins["kind"]
        if kind == "turn":
            hdg = float(ins["hdg"] % 360 or 360)
            self._wx_check(ac, hdg)
            self._stage(ac, due, tgt_hdg=hdg, turn_dir=ins["dir"])
            ac["wx_deviating"] = False
            if ac["phase"] in ("cleared", "established", "hold"):
                ac["phase"] = "cruise"     # vectored off approach or hold
            word = "left" if ins["dir"] == "l" else "right"
            return f"turn {word} heading {say_digits(ins['hdg'], 3)}"
        if kind == "alt":
            up = ins["alt_ft"] > ac["alt"]
            if ins["verb"] == "c" and not up:
                raise CommandError(f"unable climb — {me} is at "
                                   f"{say_altitude(ac['alt'])}")
            if ins["verb"] == "d" and up:
                raise CommandError(f"unable descend — {me} is at "
                                   f"{say_altitude(ac['alt'])}")
            if not up and self.terrain is not None:
                floor = self.terrain.mva_at(ac["lat"], ac["lon"])
                if floor is not None and ins["alt_ft"] < floor:
                    raise CommandError(
                        f"unable {say_altitude(ins['alt_ft'])} — minimum "
                        f"vectoring altitude here is {say_altitude(floor)}")
            self._stage(ac, due, tgt_alt=float(ins["alt_ft"]))
            ac["terrain_stop"] = False
            verb = "climb" if up else "descend"
            return f"{verb} and maintain {say_altitude(ins['alt_ft'])}"
        if kind == "speed":
            if ins["kt"] is None:
                self._stage(ac, due, tgt_ias=float(ac["perf"][0]))
                return "resume normal speed"
            lo = ac["perf"][2] if ac["phase"] in ("cleared", "established") \
                else ac["perf"][1]
            if not (lo - 5 <= ins["kt"] <= ac["perf"][0] + 10):
                raise CommandError(f"unable {say_digits(ins['kt'])} knots — "
                                   f"{me} can do "
                                   f"{say_digits(lo)} to "
                                   f"{say_digits(ac['perf'][0])}")
            if ins["dir"] == "reduce" and ins["kt"] > ac["ias"] + 5:
                raise CommandError(f"unable reduce — {me} is doing "
                                   f"{say_digits(round(ac['ias']))} knots")
            if ins["dir"] == "increase" and ins["kt"] < ac["ias"] - 5:
                raise CommandError(f"unable increase — {me} is doing "
                                   f"{say_digits(round(ac['ias']))} knots")
            self._stage(ac, due, tgt_ias=float(ins["kt"]))
            return (f"{'reduce' if ins['dir'] == 'reduce' else 'increase'} "
                    f"speed {say_digits(ins['kt'])}")
        if kind == "direct":
            spot = self.sector["fixes"].get(ins["fix"])
            if spot is None:
                raise CommandError(f"unable — {me} is unfamiliar with "
                                   f"{ins['fix']}")
            hdg = bearing_to(ac["lat"], ac["lon"], spot[0], spot[1])
            self._wx_check(ac, hdg)
            self._stage(ac, due, tgt_hdg=hdg, turn_dir=None)
            ac["wx_deviating"] = False
            if ac["phase"] in ("cleared", "established", "hold"):
                ac["phase"] = "cruise"
            return f"direct {ins['fix']}"
        if kind == "traffic":
            # a traffic call on the nearest 1200 code: the pilot who gets
            # the target in sight keeps it there, and the near-miss that
            # was coming never happens
            best = None
            for t in self.aircraft:
                if _controlled(t) or t["hex"] in ac.get("visual", ()):
                    continue
                gap = haversine_nm(ac["lat"], ac["lon"], t["lat"], t["lon"])
                if gap > 8.0 or abs(t["alt"] - ac["alt"]) > 3500.0:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, t)
            if best is None:
                raise CommandError(f"no traffic to call for {me} — "
                                   "nothing within eight miles of them")
            gap, t = best
            rel = (bearing_to(ac["lat"], ac["lon"], t["lat"], t["lon"])
                   - ac["hdg"]) % 360.0
            clock = ("twelve", "one", "two", "three", "four", "five", "six",
                     "seven", "eight", "nine", "ten",
                     "eleven")[int(round(rel / 30.0)) % 12]
            miles = max(1, round(gap))
            what = ("a balloon" if t["plan"] == "balloon"
                    else "type unknown")
            self.say(f"{me}, traffic {clock} o'clock, {say_digits(miles)} "
                     f"mile{'s' if miles != 1 else ''}, {what}, altitude "
                     f"indicates {say_altitude(t['alt'])}", "atc")
            seen_p = (0.95 if t["plan"] == "balloon"
                      else 0.85 if gap < 3.0 else 0.65 if gap < 6.0
                      else 0.45)
            if self.rng.random() < seen_p:
                ac.setdefault("visual", set()).add(t["hex"])
                return (f"traffic in sight, {clock} o'clock — "
                        "we'll maintain visual")
            return (f"negative contact on the {clock} o'clock traffic, "
                    "looking")
        if kind == "hold":
            if ins["fix"] is not None:
                spot = self.sector["fixes"].get(ins["fix"])
                if spot is None:
                    raise CommandError(f"unable — {me} is unfamiliar with "
                                       f"{ins['fix']}")
                ac["hold_at"] = spot
                where = f"at {ins['fix']}"
            else:
                ac["hold_at"] = (ac["lat"], ac["lon"])
                where = "present position"
            ac["phase"] = "hold"
            return f"hold {where}, right turns"
        if kind == "ils":
            if ac["plan"] != "arrival":
                raise CommandError(f"unable — {me} is a departure")
            # a satellite arrival gets its own field's approach — the
            # scratchpad on the data block says whose traffic this is
            sat = self.sector["sat"] if ac.get("sat") else None
            if self._rwy_closed() and sat is None:
                raise CommandError("unable — the runway's closed while "
                                   f"they clear the emergency, {me} can "
                                   "take a hold or vectors")
            if sat is not None:
                rwy = sat["rwy"]
                course, thr = sat["course"], sat["thr"]
            else:
                rwy = self.sector["rwy"]
                course, thr = self.sector["course"], self.sector["thr"]
            if ins["rwy"]:
                apt = self.sector["sat_apt"] if sat is not None \
                    else self.airport
                end = _runway_end(apt, ins["rwy"])
                if end is None:
                    raise CommandError(f"unable — no runway {ins['rwy']} "
                                       f"at {apt['icao']}")
                rwy, course, tlat, tlon = end
                thr = (tlat, tlon)
            # hopeless geometry gets a puzzled pilot, not a wasted clearance
            cross, along = cross_along_track(ac["lat"], ac["lon"],
                                             thr[0], thr[1], course)
            theta = abs(turn_delta(ac["hdg"], course))
            if along < 1.0:
                raise CommandError(f"unable — {me} is inside the marker, "
                                   "vector us back around")
            if theta > 110.0:
                raise CommandError(f"unable — {me} is pointed away from "
                                   "the localizer, give us a vector first")
            # a cell parked on the final is a go-around waiting to happen —
            # no one accepts the approach into it (emergencies excepted)
            if ac["squawk"] != "7700":
                recip = (course + 180.0) % 360.0
                on_final = self._wx_worst(thr[0], thr[1], recip, 1.0, 12.0)
                if on_final >= WX_DEVIATE:
                    raise CommandError(
                        f"unable — there's a cell on the final for "
                        f"{say_runway(rwy)}, {me} needs vectors around it")
            ac.update(phase="cleared", rwy=rwy, course=course, thr=thr,
                      tower_handoff=None, wake_warned=False)
            return f"cleared ILS runway {say_runway(rwy)} approach"
        if kind == "handoff":
            if ac["plan"] != "departure":
                raise CommandError(f"unable — {me} is an arrival, "
                                   "they're yours to land")
            if self._center_closed():
                raise CommandError(f"centre is unable to take {me} — "
                                   "their sector's full, keep them inside "
                                   "the boundary")
            if ac.get("xr") and ac["tgt_alt"] < ac["xr"]:
                raise CommandError(f"centre won't take {me} assigned below "
                                   f"{say_altitude(ac['xr'])} — "
                                   "climb them first")
            spot = self.sector["fixes"][ac["fix"]]
            dist_fix = haversine_nm(ac["lat"], ac["lon"], spot[0], spot[1])
            dist_apt = haversine_nm(ac["lat"], ac["lon"],
                                    self.airport["lat"], self.airport["lon"])
            if dist_fix > 15.0 and dist_apt < SECTOR_NM - 10.0:
                raise CommandError(f"centre won't take {me} yet — "
                                   f"get them out toward {ac['fix']}")
            ac["phase"] = "handed"
            self.score += 50
            return "switching, good day"
        raise CommandError("say again?")
