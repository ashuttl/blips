"""Callsign → route (origin/destination) lookup.

ADS-B broadcasts carry no flight-plan data, so routes come from the adsb.im
route API (the same vrs-standing-data service tar1090 queries):

    POST /api/0/routeset  {"planes": [{"callsign", "lat", "lng"}]}

Routes are keyed by callsign, and airlines reassign flight numbers to new
city pairs, so a bare callsign match can be stale.  Given the aircraft's
position the API cross-checks it against the route's track and flags the
match ``plausible`` or not; implausible routes are dropped here — a blank
footer beats a confidently wrong one.  Coverage is good for scheduled
airline/cargo callsigns and empty for most GA (``"unknown"`` means "no route
on file", not an error).  Lookups are demand-driven — the scope asks only
for the aircraft under the pointer — so a background worker fetches one
callsign at a time and results are cached for the session.  Misses are
cached too; transient network failures back off before retrying.
"""

import threading
import time

from blips import USER_AGENT
from blips._http import fetch_json
from blips._runtime import debug_log

URL = "https://adsb.im/api/0/routeset"
RETRY_S = 30  # back-off before re-asking after a network failure


def _endpoint(ap):
    """One route-API airport record → (municipality, short code) or None."""
    if not isinstance(ap, dict):
        return None
    code = ap.get("iata") or ap.get("icao") or ""
    place = ap.get("location") or ap.get("name") or ""
    if not (code or place):
        return None
    return (place, code)


class RouteLookup:
    """Session cache of callsign routes, filled by a background worker.

    get() never blocks: it returns the cached route (or None) immediately and
    queues unseen callsigns for the worker.  A repaint nudge (SIGWINCH, same
    trick as Feed) fires when an answer lands so the footer updates mid-hover.
    """

    def __init__(self, nudge=False):
        self._cache = {}     # CALLSIGN → tuple of route legs or None (miss)
        self._failed = {}    # CALLSIGN → wall time of last network failure
        self._queue = []
        self._queued = set()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._nudge = nudge

    def get(self, callsign, lat=None, lon=None):
        """Route for a callsign: ((place, code), (place, code), ...) or None.

        Legs run origin → ... → destination (usually just the pair).  Pass
        the aircraft's position so stale callsign matches can be rejected.
        None means unknown — either no route on file or not fetched yet;
        asking queues the fetch, so keep asking while the hover lasts.
        """
        cs = (callsign or "").strip().upper()
        if not (2 <= len(cs) <= 8) or not cs.isalnum():
            return None
        with self._lock:
            if cs in self._cache:
                return self._cache[cs]
            failed_at = self._failed.get(cs)
            if failed_at is not None and time.time() - failed_at < RETRY_S:
                return None
            if cs not in self._queued:
                self._queued.add(cs)
                self._queue.append((cs, lat, lon))
                if not self._started:
                    self._started = True
                    threading.Thread(target=self._run, daemon=True).start()
                self._wake.set()
        return None

    def _fetch(self, cs, lat, lon):
        planes = [{"callsign": cs,
                   "lat": lat if lat is not None else 0.0,
                   "lng": lon if lon is not None else 0.0}]
        try:
            data = fetch_json(URL, payload={"planes": planes},
                              headers={"User-Agent": USER_AGENT}, timeout=8)
        except Exception as exc:
            debug_log(f"route lookup failed for {cs}: {exc}")
            return None, False
        entry = data[0] if isinstance(data, list) and data else None
        if not isinstance(entry, dict):
            return None, False
        if entry.get("airport_codes", "unknown") == "unknown":
            return None, True  # no route on file — definitive miss
        if lat is not None and not entry.get("plausible", True):
            # the route on file doesn't pass anywhere near the aircraft:
            # the callsign has been reassigned to a different city pair
            debug_log(f"route for {cs} implausible here: "
                      f"{entry.get('airport_codes')}")
            return None, True
        legs = tuple(_endpoint(ap) for ap in entry.get("_airports") or [])
        route = legs if len(legs) >= 2 and all(legs) else None
        return route, True

    def _run(self):
        while True:
            with self._lock:
                cs, lat, lon = (self._queue.pop(0) if self._queue
                                else (None, None, None))
            if cs is None:
                self._wake.wait()
                self._wake.clear()
                continue
            route, definitive = self._fetch(cs, lat, lon)
            with self._lock:
                self._queued.discard(cs)
                if definitive:
                    self._cache[cs] = route
                    self._failed.pop(cs, None)
                else:
                    self._failed[cs] = time.time()
            if definitive and route is not None and self._nudge:
                try:
                    import os, signal
                    os.kill(os.getpid(), signal.SIGWINCH)
                except Exception:
                    pass
            time.sleep(0.3)  # be a polite adsbdb citizen
