"""Callsign → route (origin/destination) lookup.

ADS-B broadcasts carry no flight-plan data, so routes come from the adsb.im
route API (the same vrs-standing-data service tar1090 queries):

    POST /api/0/routeset  {"planes": [{"callsign", "lat", "lng"}, ...]}

Routes are keyed by callsign, and airlines reassign flight numbers to new
city pairs, so a bare callsign match can be stale.  Given the aircraft's
position the API cross-checks it against the route's track and flags the
match ``plausible`` or not; implausible routes are dropped here — a blank
card beats a confidently wrong one.  Coverage is good for scheduled
airline/cargo callsigns and empty for most GA (``"unknown"`` means "no route
on file", not an error).

Two kinds of asking share one worker.  ``get`` is the pointer's: the
aircraft under the mouse or on the card jumps the queue.  ``want`` is the
scope's: every airline callsign in view, so search can find "BOS-SFO" and
an airport can list its arrivals.  The worker sends them in batches, as
tar1090 does, a second apart.  Answers (misses too) are kept for the
session and, with ``persist``, on disk for a few hours, so a scope opened
again this afternoon knows its routes at once.
"""

import json
import re
import threading
import time

from blips import USER_AGENT
from blips._cache import CACHE_ROOT
from blips._http import fetch_json
from blips._live import nudge
from blips._runtime import debug_log

URL = "https://adsb.im/api/0/routeset"
RETRY_S = 30          # back-off before re-asking after a network failure
BATCH = 40            # callsigns per request
GAP_S = 1.0           # between requests
HIT_TTL_S = 6 * 3600  # how long a route on disk is believed
MISS_TTL_S = 2 * 3600
DISK = CACHE_ROOT / "routes.json"
_AIRLINE_CS = re.compile(r"^[A-Z]{3}\d[A-Z0-9]{0,4}$")


class Leg(tuple):
    """One end (or stop) of a route: a ``(place, code)`` pair — what the
    game reads back — carrying the airport's ``icao``, ``iata``, ``name``,
    ``lat`` and ``lon`` as attributes for the scope's card and arcs."""

    def __new__(cls, place, code, **info):
        leg = super().__new__(cls, (place, code))
        leg.__dict__.update(info)
        return leg

    def __getattr__(self, name):
        if name in ("icao", "iata", "name"):
            return ""
        if name in ("lat", "lon"):
            return None
        raise AttributeError(name)

    def __reduce__(self):
        return (_leg, (tuple(self), dict(self.__dict__)))

    def to_json(self):
        return [self[0], self[1], self.__dict__]


def _leg(pair, info):
    return Leg(pair[0], pair[1], **info)


def _endpoint(ap):
    """One route-API airport record → Leg, or None."""
    if not isinstance(ap, dict):
        return None
    code = ap.get("iata") or ap.get("icao") or ""
    place = ap.get("location") or ap.get("name") or ""
    if not (code or place):
        return None
    lat, lon = ap.get("lat"), ap.get("lon")
    return Leg(place, code, icao=ap.get("icao") or "", iata=ap.get("iata") or "",
               name=ap.get("name") or "",
               lat=lat if isinstance(lat, (int, float)) else None,
               lon=lon if isinstance(lon, (int, float)) else None)


def _parse(entry, positioned):
    """One routeset answer → (route or None, definitive)."""
    if not isinstance(entry, dict):
        return None, False
    if entry.get("airport_codes", "unknown") == "unknown":
        return None, True  # no route on file — definitive miss
    if positioned and not entry.get("plausible", True):
        # the route on file doesn't pass anywhere near the aircraft:
        # the callsign has been reassigned to a different city pair
        debug_log(f"route for {entry.get('callsign')} implausible here: "
                  f"{entry.get('airport_codes')}")
        return None, True
    legs = tuple(_endpoint(ap) for ap in entry.get("_airports") or [])
    route = legs if len(legs) >= 2 and all(legs) else None
    return route, True


class RouteLookup:
    """Session cache of callsign routes, filled by a background worker.

    get() never blocks: it returns the cached route (or None) immediately and
    queues unseen callsigns for the worker.  A repaint nudge fires when an
    answer lands so the card updates mid-hover.
    """

    def __init__(self, nudge=False, persist=False):
        self._cache = {}     # CALLSIGN → tuple of Legs, or None (miss)
        self._stamp = {}     # CALLSIGN → wall time the answer came
        self._failed = {}    # CALLSIGN → wall time of last network failure
        self._urgent = []    # (cs, lat, lon) the pointer is waiting on
        self._queue = []     # (cs, lat, lon) the scope would like to know
        self._queued = set()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._nudge = nudge
        self._persist = persist
        self._dirty = False
        if persist:
            self._load()

    # -- asking ------------------------------------------------------------
    def get(self, callsign, lat=None, lon=None):
        """Route for a callsign: (Leg, Leg, ...) or None.

        Legs run origin → ... → destination (usually just the pair).  Pass
        the aircraft's position so stale callsign matches can be rejected.
        None means unknown — either no route on file or not fetched yet;
        asking queues the fetch at the front, so keep asking while the
        hover lasts.
        """
        cs = (callsign or "").strip().upper()
        if not (2 <= len(cs) <= 8) or not cs.isalnum():
            return None
        with self._lock:
            if cs in self._cache:
                return self._cache[cs]
            if self._backing_off(cs):
                return None
            if cs in self._queued:
                # already waiting in the background line: move it up
                for i, item in enumerate(self._queue):
                    if item[0] == cs:
                        self._urgent.append(self._queue.pop(i))
                        break
            else:
                self._queued.add(cs)
                self._urgent.append((cs, lat, lon))
            self._start()
        return None

    def want(self, aircraft):
        """Queue every airline-shaped callsign among ``aircraft`` (dicts
        with callsign/lat/lon) that isn't known or on its way — behind
        anything the pointer has asked for."""
        with self._lock:
            added = False
            for ac in aircraft:
                cs = (ac.get("flight") or ac.get("callsign") or "").upper()
                if (not _AIRLINE_CS.match(cs) or cs in self._cache
                        or cs in self._queued or self._backing_off(cs)):
                    continue
                self._queued.add(cs)
                self._queue.append((cs, ac.get("lat"), ac.get("lon")))
                added = True
            if added:
                self._start()

    def peek(self, callsign):
        """The cached route without queuing anything (None if unknown)."""
        with self._lock:
            return self._cache.get((callsign or "").strip().upper())

    def known(self):
        """{callsign: route} for every route on hand (misses left out)."""
        with self._lock:
            return {cs: r for cs, r in self._cache.items() if r}

    def pending(self):
        """How many callsigns are still waiting for an answer."""
        with self._lock:
            return len(self._queued)

    def _backing_off(self, cs):
        failed_at = self._failed.get(cs)
        return failed_at is not None and time.time() - failed_at < RETRY_S

    def _start(self):
        if not self._started:
            self._started = True
            threading.Thread(target=self._run, daemon=True,
                             name="route lookups").start()
        self._wake.set()

    # -- the worker --------------------------------------------------------
    def _fetch(self, batch):
        """POST one batch → {cs: (route, definitive)}."""
        planes = [{"callsign": cs,
                   "lat": lat if lat is not None else 0.0,
                   "lng": lon if lon is not None else 0.0}
                  for cs, lat, lon in batch]
        try:
            data = fetch_json(URL, payload={"planes": planes},
                              headers={"User-Agent": USER_AGENT}, timeout=10)
        except Exception as exc:
            debug_log(f"route lookup failed for {len(batch)} callsigns: {exc}")
            return {cs: (None, False) for cs, _la, _lo in batch}
        answers = {}
        positioned = {cs: lat is not None for cs, lat, _lo in batch}
        for entry in data if isinstance(data, list) else ():
            cs = (entry.get("callsign") or "").upper() \
                if isinstance(entry, dict) else ""
            if cs in positioned:
                answers[cs] = _parse(entry, positioned[cs])
        for cs, _la, _lo in batch:
            answers.setdefault(cs, (None, False))
        return answers

    def _take(self):
        with self._lock:
            batch = self._urgent[:BATCH]
            del self._urgent[:BATCH]
            room = BATCH - len(batch)
            if room > 0:
                batch += self._queue[:room]
                del self._queue[:room]
            return batch

    def _run(self):
        while True:
            batch = self._take()
            if not batch:
                self._save()
                self._wake.wait(30.0)
                self._wake.clear()
                continue
            answers = self._fetch(batch)
            landed = False
            now = time.time()
            with self._lock:
                for cs, (route, definitive) in answers.items():
                    self._queued.discard(cs)
                    if definitive:
                        self._cache[cs] = route
                        self._stamp[cs] = now
                        self._failed.pop(cs, None)
                        self._dirty = True
                        landed = landed or route is not None
                    else:
                        self._failed[cs] = now
            if landed and self._nudge:
                nudge()
            time.sleep(GAP_S)  # be a polite adsb.im citizen

    # -- on disk -----------------------------------------------------------
    def _load(self):
        try:
            raw = json.loads(DISK.read_text())
        except (OSError, ValueError):
            return
        now = time.time()
        for cs, (stamp, legs) in raw.items() if isinstance(raw, dict) else ():
            try:
                ttl = HIT_TTL_S if legs else MISS_TTL_S
                if now - stamp > ttl:
                    continue
                route = (tuple(Leg(p, c, **info) for p, c, info in legs)
                         if legs else None)
            except (TypeError, ValueError):
                continue
            self._cache[cs] = route
            self._stamp[cs] = stamp

    def _save(self):
        if not (self._persist and self._dirty):
            return
        with self._lock:
            self._dirty = False
            now = time.time()
            out = {}
            for cs, route in self._cache.items():
                stamp = self._stamp.get(cs, now)
                if now - stamp > (HIT_TTL_S if route else MISS_TTL_S):
                    continue
                out[cs] = [stamp, [leg.to_json() for leg in route]
                           if route else None]
        try:
            DISK.parent.mkdir(parents=True, exist_ok=True)
            tmp = DISK.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, separators=(",", ":")))
            tmp.replace(DISK)
        except OSError:
            pass
    save = _save
