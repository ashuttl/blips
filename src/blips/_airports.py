"""Vendored airport/runway lookup for the game.

Data is a trimmed OurAirports extract (public domain, see
tools/build_airports.py → data/airports.json.gz): medium and large
airports with at least one open jet-length runway.  Each airport dict:

    {"icao": "KTPA", "iata": "TPA", "name": ..., "city": ..., "country": ...,
     "lat": ..., "lon": ..., "elev": ft, "large": bool,
     "rwys": [{"len": ft, "le": [ident, true_hdg, lat, lon],
                          "he": [ident, true_hdg, lat, lon]}, ...]}

Runway ends carry their own threshold coordinates when OurAirports has
them; a missing threshold falls back to the field reference point.
"""

import gzip
import json
import math
import os

_DATA = None


def _load():
    global _DATA
    if _DATA is None:
        path = os.path.join(os.path.dirname(__file__), "data",
                            "airports.json.gz")
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            _DATA = json.load(fh)["airports"]
    return _DATA


def find_airport(query):
    """Airport dict for an ICAO/IATA code or name fragment, or None.

    Exact code matches win; then name/city substring, preferring large
    airports so "tokyo" lands you at Haneda, not a commuter field.
    """
    q = query.strip().upper()
    if not q:
        return None
    airports = _load()
    for ap in airports:
        if ap["icao"] == q or (ap["iata"] and ap["iata"] == q):
            return ap
    ql = query.strip().lower()
    best = None
    for ap in airports:  # sorted large-first at build time
        hay = f"{ap['name']} {ap['city']}".lower()
        if ql in hay:
            if ap["large"]:
                return ap
            if best is None:
                best = ap
    return best


def nearest_airport(lat, lon, large_only=True):
    """The closest airport to a point (large by default — it's the game)."""
    best, best_d2 = None, None
    coslat = math.cos(math.radians(lat))
    for ap in _load():
        if large_only and not ap["large"]:
            continue
        d2 = ((ap["lat"] - lat) ** 2
              + ((ap["lon"] - lon) * coslat) ** 2)
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = ap, d2
    return best
