"""Callsign → route (origin/destination) lookup.

ADS-B broadcasts carry no flight-plan data, so routes come from adsbdb.com,
a community database keyed by callsign:

    GET /v0/callsign/{callsign}  →  {"response": {"flightroute": {...}}}

Coverage is good for scheduled airline/cargo callsigns and empty for most GA
(a 404 means "no route on file", not an error).  Lookups are demand-driven —
the scope asks only for the aircraft under the pointer — so a background
worker fetches one callsign at a time and results are cached for the session
(routes are static per callsign).  Misses are cached too; transient network
failures back off before retrying.
"""

import threading
import time

from blips import USER_AGENT
from blips._http import fetch_json
from blips._runtime import debug_log

URL = "https://api.adsbdb.com/v0/callsign/{cs}"
RETRY_S = 30  # back-off before re-asking after a network failure


def _endpoint(ap):
    """One adsbdb airport record → (municipality, short code) or None."""
    if not isinstance(ap, dict):
        return None
    code = ap.get("iata_code") or ap.get("icao_code") or ""
    place = ap.get("municipality") or ap.get("name") or ""
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
        self._cache = {}     # CALLSIGN → (origin, dest) tuple or None (miss)
        self._failed = {}    # CALLSIGN → wall time of last network failure
        self._queue = []
        self._queued = set()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._nudge = nudge

    def get(self, callsign):
        """Route for a callsign: ((place, code), (place, code)) or None.

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
                self._queue.append(cs)
                if not self._started:
                    self._started = True
                    threading.Thread(target=self._run, daemon=True).start()
                self._wake.set()
        return None

    def _fetch(self, cs):
        try:
            data = fetch_json(URL.format(cs=cs),
                              headers={"User-Agent": USER_AGENT}, timeout=8)
        except Exception as exc:
            # adsbdb answers 404 for callsigns with no route on file — that's
            # a definitive miss, worth caching; anything else may be transient
            if getattr(exc, "code", None) == 404:
                return None, True
            debug_log(f"route lookup failed for {cs}: {exc}")
            return None, False
        fr = (data.get("response") or {}).get("flightroute") or {}
        origin = _endpoint(fr.get("origin"))
        dest = _endpoint(fr.get("destination"))
        route = (origin, dest) if (origin and dest) else None
        return route, True

    def _run(self):
        while True:
            with self._lock:
                cs = self._queue.pop(0) if self._queue else None
            if cs is None:
                self._wake.wait()
                self._wake.clear()
                continue
            route, definitive = self._fetch(cs)
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
