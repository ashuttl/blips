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
DESPAWN_NM = 52.0         # a little grace past the edge
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


def build_sector(airport):
    """Fixes and active runway for an airport, seeded by its ICAO code.

    The corner posts never move: TPA's sector is always TPA's sector,
    so learning it means something.  Entries take the diagonals, exits
    the cardinals, all jittered enough to feel found rather than drawn.
    """
    rng = random.Random(airport["icao"])
    lat, lon = airport["lat"], airport["lon"]
    fixes, entries, exits = {}, [], []
    names = set()

    def fresh_name():
        while True:
            name = _fix_name(rng)
            if name not in names:
                names.add(name)
                return name

    for base in (45, 135, 225, 315):
        name = fresh_name()
        brg = base + rng.uniform(-18, 18)
        fixes[name] = advance(lat, lon, brg, SECTOR_NM)
        entries.append(name)
    for base in (0, 90, 180, 270):
        name = fresh_name()
        brg = base + rng.uniform(-18, 18)
        fixes[name] = advance(lat, lon, brg, SECTOR_NM)
        exits.append(name)

    runway = airport["rwys"][0]                    # longest, by build sort
    end = rng.choice(("le", "he"))                 # today's flow
    ident, course, thr_lat, thr_lon = runway[end]
    if thr_lat is None:                            # no threshold coords —
        back = runway["he" if end == "le" else "le"]  # walk back from midpoint
        thr_lat, thr_lon = advance(
            lat, lon, (course + 180.0) % 360.0,
            runway["len"] / 6076.0 / 2.0)
    return {
        "fixes": fixes, "entries": entries, "exits": exits,
        "rwy": ident, "course": course,
        "thr": (thr_lat, thr_lon), "elev": airport["elev"],
    }


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

    def __init__(self, airport, seed=None):
        self.airport = airport
        self.sector = build_sector(airport)
        self.rng = random.Random(seed)
        self.aircraft = []
        self.trails = {}
        self.radio = []          # [(time, line, kind)] — newest last
        self.score = 0
        self.landed = 0
        self.departed = 0
        self.busts = 0
        self.start = time.time()
        self.updated = self.start
        self.error = None
        self.source = f"{airport['icao']} approach"
        self._bust_pairs = set()
        self._counter = 0
        self._next_arrival = 8.0     # first one checks in quickly
        self._next_departure = 25.0
        self._elapsed = 0.0
        self._last_tick = None

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
        callsign, airline = self._new_callsign()
        actype = self.rng.choice(FLEETS.get(airline, ("A320",)))
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
        # never spawn into an immediate conflict the player couldn't prevent
        for other in self.aircraft:
            if (abs(other["alt"] - alt) < SEP_FT * 1.5
                    and haversine_nm(other["lat"], other["lon"],
                                     lat, lon) < SEP_NM * 3):
                self._next_arrival = 25.0   # try again shortly
                return
        hdg = bearing_to(lat, lon, self.airport["lat"], self.airport["lon"])
        ias = float(self.rng.choice((250, 270, 280)))
        ac = self._base(callsign, actype, lat, lon, alt, hdg, ias)
        ac.update(plan="arrival", fix=entry, rwy=self.sector["rwy"],
                  thr=self.sector["thr"], course=self.sector["course"])
        self.aircraft.append(ac)
        self.say(f"{self.airport['city'] or 'Approach'} approach, "
                 f"{telephony(callsign)} with you, {say_altitude(alt)}, "
                 f"inbound {entry}", "checkin")

    def _spawn_departure(self):
        callsign, airline = self._new_callsign()
        actype = self.rng.choice(FLEETS.get(airline, ("A320",)))
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
        self.say(f"{telephony(callsign)} off runway "
                 f"{say_runway(self.sector['rwy'])}, "
                 f"passing {say_altitude(ac['alt'])} for "
                 f"{say_altitude(initial)}, requesting {exit_fix}", "checkin")

    def _spawn_tick(self, dt):
        self._next_arrival -= dt
        self._next_departure -= dt
        active = sum(1 for ac in self.aircraft if ac["phase"] != "handed")
        rate = min(30.0, 9.0 + self._elapsed / 60.0 * 0.4)   # per hour
        if self._next_arrival <= 0 and active < 14:
            self._spawn_arrival()
            self._next_arrival = max(
                40.0, self.rng.expovariate(rate / 3600.0))
        if self._next_departure <= 0 and active < 14:
            self._spawn_departure()
            self._next_departure = max(
                50.0, self.rng.expovariate(rate * 0.6 / 3600.0))

    # -- flying -------------------------------------------------------------
    def _fly(self, ac, dt):
        cruise, min_clean, app_kt, climb, descend = ac["perf"]

        if ac["phase"] in ("cleared", "established"):
            self._fly_ils(ac)

        # heading: standard-rate toward target, honouring a forced direction
        delta = turn_delta(ac["hdg"], ac["tgt_hdg"], ac["turn_dir"])
        step = TURN_RATE * dt
        if abs(delta) <= step:
            ac["hdg"] = ac["tgt_hdg"]
            ac["turn_dir"] = None
        else:
            ac["hdg"] = (ac["hdg"] + math.copysign(step, delta)) % 360.0
        ac["track"] = ac["hdg"]

        # altitude
        diff = ac["tgt_alt"] - ac["alt"]
        rate = climb if diff > 0 else descend
        step_ft = rate * dt / 60.0
        if abs(diff) <= step_ft:
            ac["alt"] = ac["tgt_alt"]
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
            closing = abs(turn_delta(ac["hdg"], ac["course"])) < 100.0
            if abs(cross) < 0.45 and along > 0.3 and closing:
                ac["phase"] = "established"
                ac["turn_dir"] = None
                self.say(f"{telephony(ac['callsign'])} established, "
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
            self.say(f"{telephony(ac['callsign'])}, contact tower. "
                     "Good day.", "atc")
        if along < 0.35 or ac["alt"] <= self.sector["elev"] + 30.0:
            ac["phase"] = "landed"

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
        self._separation()
        self._trails(now)
        self._retire()
        self.updated = now

    def _retire(self):
        keep = []
        for ac in self.aircraft:
            if ac["phase"] == "landed":
                self.landed += 1
                self.score += 100
                self.trails.pop(ac["hex"], None)
                continue
            dist = haversine_nm(ac["lat"], ac["lon"],
                                self.airport["lat"], self.airport["lon"])
            if dist > DESPAWN_NM:
                if ac["phase"] == "handed":
                    self.departed += 1
                elif ac["plan"] == "arrival":
                    self.score -= 100
                    self.say(f"{ac['callsign']} diverted — flew out of "
                             "your airspace unworked", "alert")
                else:
                    self.score -= 100
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
        """
        try:
            query, instructions = parse(text)
            ac = resolve_callsign(query, [a for a in self.aircraft
                                          if a["phase"] != "handed"])
            phrases = [self._apply(ac, ins) for ins in instructions]
        except CommandError as exc:
            line = str(exc)
            self.say(line, "error")
            return line
        line = f"{telephony(ac['callsign'])}, {', '.join(phrases)}."
        self.say(line, "readback")
        return line

    def _apply(self, ac, ins):
        """Apply one instruction; return its readback phrase.

        Direction verbs are checked against the aircraft's state — the
        game never quietly fixes a wrong one (holding the picture is the
        point), it hands the mic to a puzzled pilot instead.
        """
        me = telephony(ac["callsign"])
        kind = ins["kind"]
        if kind == "turn":
            ac["tgt_hdg"] = float(ins["hdg"] % 360 or 360)
            ac["turn_dir"] = ins["dir"]
            if ac["phase"] in ("cleared", "established"):
                ac["phase"] = "cruise"     # vectored off the approach
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
            ac["tgt_alt"] = float(ins["alt_ft"])
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
            ac["tgt_hdg"] = bearing_to(ac["lat"], ac["lon"], spot[0], spot[1])
            ac["turn_dir"] = None
            if ac["phase"] in ("cleared", "established"):
                ac["phase"] = "cruise"
            return f"direct {ins['fix']}"
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
            ac.update(phase="cleared", rwy=rwy, course=course, thr=thr,
                      tower_handoff=None)
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
