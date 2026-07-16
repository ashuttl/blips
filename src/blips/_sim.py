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
SEP_FLOOR_AGL = 900.0     # ignore pairs in the runway environment
TRAIL_MAX_FIXES = 120
TRAIL_MIN_GAP_S = 4.0

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
    """
    from blips._airports import navaids_near
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
    return {
        "fixes": fixes, "entries": entries, "exits": exits,
        "rwy": ident, "course": course, "thr": thr, "end": end,
        "elev": airport["elev"],
    }


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
        self._counter = 0
        self._next_arrival = 45.0
        self._next_departure = 30.0
        self._next_request = 150.0
        self._next_flow = self.rng.uniform(600.0, 1080.0)
        self._emergencies = 0
        self._elapsed = 0.0
        self._last_tick = None
        self.hearback_p = 0.05   # odds per transmission of a bad readback
        self.hearbacks = 0       # instructions misheard this shift
        self.hearbacks_caught = 0  # ...and corrected before they stuck
        self._prepopulate()      # a shift starts mid-shift, not empty

    def _prepopulate(self):
        """The sector you take over already has traffic in it."""
        for _ in range(6):
            if sum(a["plan"] == "arrival" for a in self.aircraft) >= 2:
                break
            self._spawn_arrival()
        arrivals = [a for a in self.aircraft if a["plan"] == "arrival"]
        if len(arrivals) > 1:
            # one of them is already partway in and part-descended
            ac = arrivals[-1]
            ac["lat"], ac["lon"] = advance(ac["lat"], ac["lon"],
                                           ac["hdg"], 18.0)
            ac["alt"] = ac["tgt_alt"] = max(
                self.airport["elev"] + 5000.0, ac["alt"] - 4000.0)
        self._spawn_departure()

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
            "tgt_alt": alt, "tgt_ias": ias, "perf": PERF[actype],
            "phase": "cruise",   # cruise | cleared | established | handed
            "plan": "arrival", "fix": None, "rwy": None, "thr": None,
            "course": None, "delay": 0.0,
        }

    def _spawn_arrival(self):
        entry = self.rng.choice(self.sector["entries"])
        elat, elon = self.sector["fixes"][entry]
        lat, lon = advance(elat, elon,
                           bearing_to(self.airport["lat"],
                                      self.airport["lon"], elat, elon),
                           self.rng.uniform(0, 4))  # just outside the fix
        # each corner post owns an altitude band, staggered so unworked
        # streams don't conflict with each other — only with your plan
        base = 110 + 10 * self.sector["entries"].index(entry)
        alt = 100.0 * max(base + self.rng.choice((0, 20)),
                          (self.airport["elev"] + 6000) // 100 + 10)
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
        dist = haversine_nm(lat, lon, self.airport["lat"],
                            self.airport["lon"])
        ac.update(plan="arrival", fix=entry, rwy=self.sector["rwy"],
                  thr=self.sector["thr"], course=self.sector["course"],
                  par=dist * 16.0 + 300.0)
        self.aircraft.append(ac)
        tail = f", from {origin}" if origin else ""
        self.say(f"{self.airport['city'] or 'Approach'} approach, "
                 f"{hail(ac)} with you, {say_altitude(alt)}, "
                 f"inbound {entry}{tail}", "checkin")

    def _spawn_departure(self):
        callsign, actype, dest = self._cast_flight("departure")
        exit_fix = self.rng.choice(self.sector["exits"])
        course = self.sector["course"]
        thr = self.sector["thr"]
        lat, lon = advance(thr[0], thr[1], course, 1.5)  # rolling, airborne
        elev = self.airport["elev"]
        initial = float(round((elev + 3000) / 1000) * 1000)
        ac = self._base(callsign, actype, lat, lon, elev + 1200.0,
                        course, 170.0)
        ac.update(plan="departure", fix=exit_fix, tgt_alt=initial,
                  tgt_ias=250.0, phase="cruise")
        self.aircraft.append(ac)
        tail = f", for {dest}" if dest else ""
        self.say(f"{hail(ac)} off runway "
                 f"{say_runway(self.sector['rwy'])}, "
                 f"passing {say_altitude(ac['alt'])} for "
                 f"{say_altitude(initial)}, requesting {exit_fix}{tail}",
                 "checkin")

    def _spawn_tick(self, dt):
        self._next_arrival -= dt
        self._next_departure -= dt
        active = sum(1 for ac in self.aircraft if ac["phase"] != "handed")
        rate = min(40.0, 18.0 + self._elapsed / 60.0)   # per hour, ramping
        if self._next_arrival <= 0 and active < 16:
            self._spawn_arrival()
            self._next_arrival = max(
                35.0, self.rng.expovariate(rate / 3600.0))
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
                self._next_departure = max(
                    45.0, self.rng.expovariate(rate * 0.7 / 3600.0))

    # -- flying -------------------------------------------------------------
    def _fly(self, ac, dt):
        cruise, min_clean, app_kt, climb, descend = ac["perf"]

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
        ac["track"] = ac["hdg"]

        # altitude — descents respect the terrain under them (the ILS is a
        # surveyed path, so a coupled approach may go below the grid's MVA)
        tgt_alt = ac["tgt_alt"]
        if (self.terrain is not None and tgt_alt < ac["alt"]
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
        ac["gs"] = ac["ias"] * (1.0 + ac["alt"] * 2e-5)

        ac["lat"], ac["lon"] = advance(ac["lat"], ac["lon"], ac["hdg"],
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
        gs_alt = self.sector["elev"] + along * GS_FT_PER_NM
        if gs_alt < ac["alt"]:
            ac["tgt_alt"] = max(self.sector["elev"], gs_alt)
        # …and slow to approach speed inside six miles
        if along < 6.0:
            ac["tgt_ias"] = float(ac["perf"][2])
        elif ac["tgt_ias"] > 190.0:
            ac["tgt_ias"] = 180.0
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
        if along < 0.35 or ac["alt"] <= self.sector["elev"] + 30.0:
            ac["phase"] = "landed"

    def _go_around(self, ac, reason, cost=50):
        """Break an approach off: climb out on runway heading, yours again."""
        ac.update(phase="cruise", tower_handoff=None,
                  tgt_hdg=ac["course"], turn_dir=None,
                  tgt_ias=max(ac["perf"][2] + 40.0, 180.0), wake_warned=False)
        ac["tgt_alt"] = float(
            round((self.sector["elev"] + 3000.0) / 1000.0) * 1000.0)
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

        self._spawn_tick(dt)
        for ac in self.aircraft:
            self._fly(ac, dt)
            ac["delay"] += dt
        self._requests(dt)
        self._weather_tick(dt)
        self._flow_tick(dt)
        self._emergency_tick(dt)
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

    def _weather_tick(self, dt):
        """Pilots don't fly into cells: they ask, then they act."""
        if self.wx_sample is None:
            return
        for ac in self.aircraft:
            if (ac["phase"] not in ("cruise", "hold")
                    or ac["squawk"] == "7700"):
                continue
            ahead = self._wx_ahead(ac, ac["hdg"], 6.0)
            if ac.get("wx_deviating"):
                if ahead < 0.3:
                    ac["wx_deviating"] = False
                    ac["wx_asked_t"] = None
                    self.say(f"{hail(ac)} clear of weather,"
                             " ready for a vector", "request")
                continue
            if ahead < 0.65:
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
                # ignored long enough: they protect themselves
                ac["wx_deviating"] = True
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
        self.sector_rev += 1
        wind_dir = round((course + self.rng.uniform(-20.0, 20.0))
                         % 360.0 / 10.0) * 10 or 360
        self.say(f"ATIS update — wind {wind_dir:03d} at "
                 f"{self.rng.randint(8, 16)}, landing and departing "
                 f"runway {self.sector['rwy']}", "atis")
        for ac in self.aircraft:
            if ac["plan"] == "arrival" and ac["phase"] == "cleared":
                # not yet established: their clearance dies with the flow
                ac["phase"] = "cruise"
                self.say(f"{hail(ac)}, cancel approach "
                         f"clearance, fly present heading, expect runway "
                         f"{ident}", "atc")

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
                      and ac["alt"] > 6000.0]
        if candidates:
            self._declare_emergency(self.rng.choice(candidates))

    def _requests(self, dt):
        """Now and then somebody on frequency wants something."""
        self._next_request -= dt
        if self._next_request > 0:
            return
        self._next_request = 120.0 + self.rng.expovariate(1.0 / 120.0)
        wanting = []
        for ac in self.aircraft:
            if ac.get("asked") or ac["phase"] != "cruise":
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
        for ac in self.aircraft:
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
        self.aircraft = keep

    def _separation(self):
        floor = self.airport["elev"] + SEP_FLOOR_AGL
        current = set()
        flying = [ac for ac in self.aircraft
                  if ac["phase"] != "handed" and ac["alt"] > floor]
        for ac in self.aircraft:
            ac["emergency"] = False
        for i, a in enumerate(flying):
            for b in flying[i + 1:]:
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
            ac = resolve_callsign(query, [a for a in self.aircraft
                                          if a["phase"] != "handed"])
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
                            phrases.append(self._apply(ac, heard))
                            self.hearbacks += 1
                            ac["misheard_kind"] = ins["kind"]
                            ac["misheard_t"] = self._elapsed
                            continue
                        except CommandError:
                            pass   # the mishearing was unflyable: heard right
                phrases.append(self._apply(ac, ins))
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
        if new_wx < 0.65 or new_wx <= self._wx_ahead(ac, ac["hdg"]) + 0.1:
            return
        me = hail(ac)
        side = ("left" if self._wx_ahead(ac, (new_hdg - 40.0) % 360.0)
                <= self._wx_ahead(ac, (new_hdg + 40.0) % 360.0)
                else "right")
        raise CommandError(f"unable — that heading puts {me} into a cell, "
                           f"we could take further {side}")

    def _apply(self, ac, ins):
        """Apply one instruction; return its readback phrase.

        Direction verbs are checked against the aircraft's state — the
        game never quietly fixes a wrong one (holding the picture is the
        point), it hands the mic to a puzzled pilot instead.
        """
        me = hail(ac)
        kind = ins["kind"]
        if kind == "turn":
            hdg = float(ins["hdg"] % 360 or 360)
            self._wx_check(ac, hdg)
            ac["tgt_hdg"] = hdg
            ac["turn_dir"] = ins["dir"]
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
            ac["tgt_alt"] = float(ins["alt_ft"])
            ac["terrain_stop"] = False
            verb = "climb" if up else "descend"
            return f"{verb} and maintain {say_altitude(ins['alt_ft'])}"
        if kind == "speed":
            if ins["kt"] is None:
                ac["tgt_ias"] = float(ac["perf"][0])
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
            ac["tgt_ias"] = float(ins["kt"])
            return (f"{'reduce' if ins['dir'] == 'reduce' else 'increase'} "
                    f"speed {say_digits(ins['kt'])}")
        if kind == "direct":
            spot = self.sector["fixes"].get(ins["fix"])
            if spot is None:
                raise CommandError(f"unable — {me} is unfamiliar with "
                                   f"{ins['fix']}")
            hdg = bearing_to(ac["lat"], ac["lon"], spot[0], spot[1])
            self._wx_check(ac, hdg)
            ac["tgt_hdg"] = hdg
            ac["turn_dir"] = None
            ac["wx_deviating"] = False
            if ac["phase"] in ("cleared", "established", "hold"):
                ac["phase"] = "cruise"
            return f"direct {ins['fix']}"
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
            rwy = self.sector["rwy"]
            course, thr = self.sector["course"], self.sector["thr"]
            if ins["rwy"]:
                end = _runway_end(self.airport, ins["rwy"])
                if end is None:
                    raise CommandError(f"unable — no runway {ins['rwy']} "
                                       f"at {self.airport['icao']}")
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
            ac.update(phase="cleared", rwy=rwy, course=course, thr=thr,
                      tower_handoff=None, wake_warned=False)
            return f"cleared ILS runway {say_runway(rwy)} approach"
        if kind == "handoff":
            if ac["plan"] != "departure":
                raise CommandError(f"unable — {me} is an arrival, "
                                   "they're yours to land")
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
