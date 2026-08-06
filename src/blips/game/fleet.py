"""Live-sampled traffic for the game: the flights that actually fly here.

At the start of a shift, one background fetch pulls the real aircraft
currently within 250 nm of the airport (same ADS-B client the scope
uses) and keeps the airline callsigns and types.  Their real routes fill
in asynchronously via the route API the scope already speaks.  The
spawner draws from this pool, so a session at TPA is Southwest 737s and
Breeze A220s with "from Baltimore" on check-in, not a country-level
guess — and it degrades to the country pools when offline.

Each pool entry is spawned at most once per session; a callsign in the
pool that's known to *arrive* here is only ever spawned as an arrival,
and vice versa.  Entries with no route on file can play either role,
silently.
"""

import random
import re
import threading

from blips._adsb import fetch_point
from blips._commands import TELEPHONY
from blips._routes import RouteLookup
from blips._runtime import debug_log

_CALLSIGN = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]{0,2}$")
_BIZJETS = {"C56X", "GLF5"}

# common real-world types → the PERF type that flies most like them
TYPE_ALIAS = {
    "B737": "B738", "B38M": "B738", "B39M": "B738", "B3XM": "B739",
    "B734": "B738", "B735": "B738", "B736": "B738",
    "A318": "A320", "A319": "A320", "A19N": "A320", "A21N": "A321",
    "B753": "B752", "B762": "B763", "B764": "B763", "MD11": "B763",
    "A306": "B763", "A310": "B763", "A332": "B763", "A333": "B763",
    "A338": "A339", "A346": "B77W", "A343": "B77W",
    "B772": "B77W", "B773": "B77W", "B77L": "B77W", "B744": "B77W",
    "B748": "B77W", "B779": "B77W", "A35K": "A359",
    "B787": "B788", "B789": "B788", "B78X": "B788",
    "E170": "E175", "E75L": "E175", "E75S": "E175", "E195": "E190",
    "E294": "E290", "E295": "E290", "BCS1": "A223", "BCS3": "A223",
    "E145": "CRJ7", "E45X": "CRJ7", "E135": "CRJ7",
    "CRJ1": "CRJ7", "CRJ2": "CRJ7", "CRJX": "CRJ9",
    "AT43": "AT76", "AT45": "AT76", "AT72": "AT76", "AT75": "AT76",
    "DH8A": "DH8D", "DH8B": "DH8D", "DH8C": "DH8D", "SF34": "DH8D",
    "SB20": "DH8D", "E120": "DH8D",
    "C25A": "C56X", "C25B": "C56X", "C25C": "C56X", "C25M": "C56X",
    "C510": "C56X", "C525": "C56X", "C550": "C56X", "C560": "C56X",
    "C650": "C56X", "C680": "C56X", "C68A": "C56X", "C700": "GLF5",
    "CL30": "C56X", "CL35": "C56X", "CL60": "GLF5",
    "E50P": "C56X", "E55P": "C56X", "E545": "C56X", "E550": "C56X",
    "PC24": "C56X", "H25B": "C56X", "HDJT": "C56X", "LJ35": "C56X",
    "LJ45": "C56X", "LJ60": "C56X", "LJ75": "C56X",
    "GLF4": "GLF5", "GLF6": "GLF5", "GL5T": "GLF5", "GL7T": "GLF5",
    "GLEX": "GLF5", "F2TH": "GLF5", "FA7X": "GLF5", "FA8X": "GLF5",
    "F900": "GLF5",
}


class TrafficPool:
    """The real traffic near an airport, waiting to be spawned."""

    def __init__(self, airport, perf_types, rng=None):
        self.airport = airport
        self._rng = rng or random.Random()
        self._perf = perf_types
        self._codes = {c for c in (airport["iata"], airport["icao"]) if c}
        self.routes = RouteLookup()
        self._entries = []       # [{cs, actype, lat, lon}]
        self._used = set()
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._sample, daemon=True).start()

    def _sample(self):
        try:
            aircraft, source = fetch_point(
                self.airport["lat"], self.airport["lon"], 250)
        except Exception as exc:
            debug_log(f"traffic sample failed: {exc}")
            return
        seen = set()
        entries = []
        for ac in aircraft:
            cs = ac["callsign"]
            actype = TYPE_ALIAS.get(ac["actype"], ac["actype"])
            if (cs in seen or actype not in self._perf
                    or not _CALLSIGN.match(cs)):
                continue
            seen.add(cs)
            entries.append({"cs": cs, "actype": actype,
                            "lat": ac["lat"], "lon": ac["lon"]})
        self._rng.shuffle(entries)
        with self._lock:
            self._entries = entries
        debug_log(f"traffic pool: {len(entries)} flights via {source}")
        # warm the route cache nearest-first: close-in flights are the
        # likeliest to be this airport's own traffic, so their origins
        # and destinations are on the radio soonest
        by_distance = sorted(entries, key=lambda e: (
            (e["lat"] - self.airport["lat"]) ** 2
            + (e["lon"] - self.airport["lon"]) ** 2))
        for e in by_distance:
            self.routes.get(e["cs"], e["lat"], e["lon"])

    def _route_ends(self, entry):
        """(origin_leg, dest_leg) or (None, None) while unknown."""
        route = self.routes.get(entry["cs"], entry["lat"], entry["lon"])
        if not route:
            return None, None
        return route[0], route[-1]

    def draw(self, role, confirmed_only=False):
        """A real (callsign, actype, extra) for a spawn, or None.

        ``role`` is "arrival", "departure" or "overflight".  Entries whose
        real route involves this airport are matched to the right role and
        carry the far end for the check-in (``extra`` is its ``(place,
        code)`` pair, so the spawner can both read it back and place it on
        the map); a flight whose known route passes this airport by is
        exactly what belongs overhead at FL350 — for those ``extra`` is the
        route's ``(origin_leg, dest_leg)``, each a ``(place, code)`` pair, so
        the scope's hover chip can say where they're going over you to;
        route-unknown entries fill in for any role anonymously (extra None)
        unless ``confirmed_only`` says a known route is the whole point.
        Wrong-direction entries never spawn.
        """
        with self._lock:
            entries = [e for e in self._entries if e["cs"] not in self._used]
        anonymous = []
        for e in entries:
            origin, dest = self._route_ends(e)
            if origin is None:
                anonymous.append(e)
                continue
            if role == "overflight":
                if not any(leg[0] in self._codes or leg[1] in self._codes
                           for leg in (origin, dest)):
                    with self._lock:
                        self._used.add(e["cs"])
                    return e["cs"], e["actype"], (origin, dest)
                continue
            here_end = dest if role == "arrival" else origin
            far_end = origin if role == "arrival" else dest
            if here_end[1] in self._codes or here_end[0] in self._codes:
                with self._lock:
                    self._used.add(e["cs"])
                return e["cs"], e["actype"], far_end
        if anonymous and not confirmed_only:
            # no route-confirmed flight to hand out: prefer a recognisable
            # airline over the bizjet soup that fills any 250 nm circle
            anonymous.sort(key=lambda e: (e["cs"][:3] not in TELEPHONY,
                                          e["actype"] in _BIZJETS))
            pick = anonymous[0]
            with self._lock:
                self._used.add(pick["cs"])
            return pick["cs"], pick["actype"], None
        return None

    def spent(self):
        """True when nothing left could still lead an arrival or departure:
        every unused entry's route is known and known not to touch this
        field.  Route-unknown entries keep this False — their route may yet
        fill in and confirm.  The pool may still hold overflights."""
        with self._lock:
            entries = [e for e in self._entries if e["cs"] not in self._used]
        for e in entries:
            origin, dest = self._route_ends(e)
            if origin is None:
                return False
            if any(leg[0] in self._codes or leg[1] in self._codes
                   for leg in (origin, dest)):
                return False
        return True
