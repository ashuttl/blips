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

from blips._geo import bearing_to, haversine_nm

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


_NAVAIDS = None


def load_navaids():
    """The vendored radio-navaid list (see tools/build_navaids.py)."""
    global _NAVAIDS
    if _NAVAIDS is None:
        path = os.path.join(os.path.dirname(__file__), "data",
                            "navaids.json.gz")
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            _NAVAIDS = json.load(fh)["navaids"]
    return _NAVAIDS


def navaids_near(lat, lon, min_nm, max_nm):
    """Navaids in a distance band around a point: (dist_nm, bearing, navaid).

    Cheap equirectangular prefilter, exact haversine on the survivors.
    """
    out = []
    coslat = max(0.2, math.cos(math.radians(lat)))
    max_deg = max_nm / 60.0 + 0.5
    for nav in load_navaids():
        if (abs(nav["lat"] - lat) > max_deg
                or abs(nav["lon"] - lon) * coslat > max_deg):
            continue
        d = haversine_nm(lat, lon, nav["lat"], nav["lon"])
        if min_nm <= d <= max_nm:
            out.append((d, bearing_to(lat, lon, nav["lat"], nav["lon"]), nav))
    return out


_FIXES = None


def load_fixes():
    """The vendored named-waypoint list (see tools/build_fixes.py)."""
    global _FIXES
    if _FIXES is None:
        path = os.path.join(os.path.dirname(__file__), "data",
                            "fixes.json.gz")
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            _FIXES = json.load(fh)["fixes"]
    return _FIXES


def fixes_near(lat, lon, min_nm, max_nm):
    """Named waypoints in a distance band: (dist_nm, bearing, fix).

    Same cheap prefilter as navaids_near.  Fixes carry no radio type, so
    they tag along as ``type: "WPT"`` — good enough to name a gate the real
    world left without a navaid, but ranked behind every real navaid.
    """
    out = []
    coslat = max(0.2, math.cos(math.radians(lat)))
    max_deg = max_nm / 60.0 + 0.5
    for fix in load_fixes():
        if (abs(fix["lat"] - lat) > max_deg
                or abs(fix["lon"] - lon) * coslat > max_deg):
            continue
        d = haversine_nm(lat, lon, fix["lat"], fix["lon"])
        if min_nm <= d <= max_nm:
            nav = {"id": fix["id"], "name": fix["id"], "type": "WPT",
                   "lat": fix["lat"], "lon": fix["lon"]}
            out.append((d, bearing_to(lat, lon, fix["lat"], fix["lon"]), nav))
    return out


def airports_near(lat, lon, min_nm, max_nm):
    """Airports in a distance band around a point: (dist_nm, airport),
    nearest first.  Same cheap prefilter as navaids_near."""
    out = []
    coslat = max(0.2, math.cos(math.radians(lat)))
    max_deg = max_nm / 60.0 + 0.5
    for ap in _load():
        if (abs(ap["lat"] - lat) > max_deg
                or abs(ap["lon"] - lon) * coslat > max_deg):
            continue
        d = haversine_nm(lat, lon, ap["lat"], ap["lon"])
        if min_nm <= d <= max_nm:
            out.append((d, ap))
    out.sort(key=lambda pair: pair[0])
    return out


def airports_in_bbox(bbox, large_only=False):
    """Airports inside a lon/lat bbox, most prominent first.

    Prominence is large-airport status, then longest runway — so wide
    views label the majors and close views fill in the fields.
    """
    minlon, minlat, maxlon, maxlat = bbox
    hits = [ap for ap in _load()
            if minlat <= ap["lat"] <= maxlat
            and minlon <= ap["lon"] <= maxlon
            and (ap["large"] or not large_only)]
    hits.sort(key=lambda a: (not a["large"], -a["rwys"][0]["len"]))
    return hits


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
