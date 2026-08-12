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
from blips._geo import bearing_to, haversine_nm, turn_delta
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
        self._entries = []       # [{cs, actype, lat, lon, alt, gs, track, vrate}]
        self._used = set()
        self._lock = threading.Lock()
        self.sampled = threading.Event()   # set once the fetch has resolved,
                                           # success or not — the shift's
                                           # opening curtain waits on it

    def start(self):
        threading.Thread(target=self._sample, daemon=True).start()

    def _sample(self):
        try:
            aircraft, source = fetch_point(
                self.airport["lat"], self.airport["lon"], 250)
        except Exception as exc:
            debug_log(f"traffic sample failed: {exc}")
            self.sampled.set()
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
                            "lat": ac["lat"], "lon": ac["lon"],
                            "alt": ac.get("alt"), "gs": ac.get("gs"),
                            "track": ac.get("track"),
                            "vrate": ac.get("vrate")})
        self._rng.shuffle(entries)
        with self._lock:
            self._entries = entries
        debug_log(f"traffic pool: {len(entries)} flights via {source}")
        self.sampled.set()
        # warm the route cache nearest-first: close-in flights are the
        # likeliest to be this airport's own traffic, so their origins
        # and destinations are on the radio soonest
        by_distance = sorted(entries, key=lambda e: (
            (e["lat"] - self.airport["lat"]) ** 2
            + (e["lon"] - self.airport["lon"]) ** 2))
        for e in by_distance:
            self.routes.get(e["cs"], e["lat"], e["lon"])

    def opening(self, sector_nm, elev):
        """The real picture at sample time, classified for a shift's open:
        ``{"arrivals", "handins", "departures"}``, each nearest-first.

        ``arrivals`` are genuinely inbound and already inside the ring —
        the sector you inherit; ``handins`` are the real arrival stream
        still outside the boundary, so centre can work them across on
        their own true ETA; ``departures`` are climb-outs caught
        mid-departure.  Each entry is the sampled dict plus ``dist`` and,
        when its route has already resolved, ``far`` — the origin leg for
        an inbound, the destination leg for an outbound.

        Geometry does the classifying (the route cache is seconds old at
        curtain time); a route that has resolved confirms an inbound the
        track alone wouldn't, and vetoes a passer-by that happens to
        point here.  Nothing is marked used — the sim ``take``s exactly
        what it admits, and everything else stays castable."""
        home = (self.airport["lat"], self.airport["lon"])
        out = {"arrivals": [], "handins": [], "departures": []}
        with self._lock:
            entries = [dict(e) for e in self._entries
                       if e["cs"] not in self._used]
        for e in entries:
            alt, gs, track = e.get("alt"), e.get("gs"), e.get("track")
            if alt is None or gs is None or track is None or gs < 90.0:
                continue     # on the ground, or too partial to trust
            d = haversine_nm(e["lat"], e["lon"], home[0], home[1])
            to_field = bearing_to(e["lat"], e["lon"], home[0], home[1])
            closing = abs(turn_delta(track, to_field)) <= 60.0
            leaving = abs(turn_delta(track,
                                     (to_field + 180.0) % 360.0)) <= 60.0
            vrate = e.get("vrate") or 0.0
            origin, dest = self._route_ends(e)
            here = lambda leg: (leg[0] in self._codes
                                or leg[1] in self._codes)
            inbound = (here(dest) if origin is not None
                       else closing and alt <= elev + 26000.0
                       and vrate < 500.0)
            # beyond the ring a route-unknown flight has to be pointed
            # nearly straight here: a 60° cone at 100 nm sweeps up real
            # traffic bound for the airport down the road
            tight = abs(turn_delta(track, to_field)) <= 35.0
            outbound = (here(origin) if origin is not None
                        else leaving and vrate > 300.0
                        and alt <= elev + 18000.0)
            e["dist"] = d
            # the closest handful is the outgoing controller's traffic —
            # already sequenced, some of it nearly on final — so the shift
            # inherits the 12 nm-to-boundary band, not the short final
            if (inbound and 12.0 <= d <= sector_nm - 2.0
                    and elev + 2500.0 <= alt <= elev + 17000.0):
                if origin is not None:
                    e["far"] = origin
                out["arrivals"].append(e)
            elif (inbound and (origin is not None or tight)
                    and sector_nm - 2.0 < d <= 110.0
                    and alt <= elev + 26000.0):
                if origin is not None:
                    e["far"] = origin
                out["handins"].append(e)
            elif (outbound and 3.0 <= d <= sector_nm * 0.7
                    and elev + 1200.0 <= alt <= elev + 20000.0):
                if origin is not None:
                    e["far"] = dest
                out["departures"].append(e)
        for flights in out.values():
            flights.sort(key=lambda e: e["dist"])
        return out

    def take(self, cs):
        """Mark a flight the opening admitted, so the cast never re-draws
        it — the opening's counterpart to ``draw`` marking its own."""
        with self._lock:
            self._used.add(cs)

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
