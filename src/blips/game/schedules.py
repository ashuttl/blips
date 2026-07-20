"""Vendored per-airport schedule profiles: the real flights that serve a
field, so a shift anywhere spawns real airlines flying real metal on real
routes — Breeze A220s from Charleston, Endeavor CRJ9s from JFK — without a
live connection.

Built offline from the open Wikipedia "Airlines and destinations" tables
(tools/build_schedules.py → data/schedules.json.gz) and refreshed only
every few years, the same vendored pattern as the airport database.  Each
airport maps to a list of weighted route tuples, usable both ways round —
an arrival *from* the far end, a departure *to* it:

    "KPWM": {"routes": [["MXY", "A223", "CHS", 3],
                        ["RPA", "E175", "LGA", 4], ...]}

Route = [operating-carrier prefix, aircraft type (a PERF code), far end
(an IATA code when known, else a place name), weight].  The far end reads
back as a city on the check-in and the hover chip.
"""

import gzip
import json
import os

from blips._airports import _load as _load_airports

_DATA = None
_CITY = None


def _load():
    global _DATA
    if _DATA is None:
        # data lives in the package root (blips/data), one level up from game/
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data",
                            "schedules.json.gz")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                _DATA = json.load(fh)
        except FileNotFoundError:      # no vendored schedules — cold-start only
            _DATA = {}
    return _DATA


def schedule_for(icao):
    """The weighted route list for an airport, or [] when none is vendored."""
    rec = _load().get((icao or "").upper())
    return rec["routes"] if rec else []


def far_city(far):
    """A displayable place for a route's far end: an IATA code becomes its
    city ('CHS' → 'Charleston'); a free-text name passes through unchanged."""
    global _CITY
    if _CITY is None:
        _CITY = {a["iata"]: (a["city"] or a["name"])
                 for a in _load_airports() if a.get("iata")}
    return _CITY.get(far, far)
