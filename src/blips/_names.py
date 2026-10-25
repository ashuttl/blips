"""Who an airline code is and what a type designator means.

Vendored tables (tools/build_airlines.py, tools/build_types.py, both from
OpenFlights with corrections) answer the questions a flight card and a
search box ask:

    airline("DAL")            → ("Delta Air Lines", "DL")
    callsigns_for_flight("DL 1673") → ["DAL1673"]
    flight_number("DAL1673")  → "DL1673"
    type_name("B38M")         → "Boeing 737 MAX 8"
    types_matching("dreamliner") → ["B788", "B789", "B78X"]
"""

import gzip
import json
import os
import re

_AIRLINES = None
_BY_IATA = None
_TYPES = None


def _data(name, key):
    path = os.path.join(os.path.dirname(__file__), "data", name)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)[key]
    except (OSError, ValueError, KeyError):
        return {}


def _airlines():
    global _AIRLINES, _BY_IATA
    if _AIRLINES is None:
        _AIRLINES = _data("airlines.json.gz", "airlines")
        by_iata = {}
        for icao, (_name, iata) in _AIRLINES.items():
            if iata:
                by_iata.setdefault(iata, []).append(icao)
        _BY_IATA = by_iata
    return _AIRLINES


def _types():
    global _TYPES
    if _TYPES is None:
        _TYPES = _data("types.json.gz", "types")
    return _TYPES


def airline(icao):
    """(name, iata) for a three-letter ICAO airline code, or None."""
    hit = _airlines().get((icao or "").upper())
    return (hit[0], hit[1]) if hit else None


def airline_of(callsign):
    """(icao, name, iata) for an airline-shaped callsign — three letters
    then a digit, 'DAL1673' — or None for registrations and the rest."""
    cs = (callsign or "").upper()
    if len(cs) > 3 and cs[:3].isalpha() and cs[3].isdigit():
        hit = airline(cs[:3])
        if hit:
            return cs[:3], hit[0], hit[1]
    return None


def flight_number(callsign):
    """'DAL1673' → 'DL1673', the number as a ticket prints it; None when
    the airline has no IATA code on file or the callsign isn't an
    airline's."""
    hit = airline_of(callsign)
    if hit and hit[2]:
        return hit[2] + callsign[3:].upper()
    return None


_NUMBER = re.compile(r"^\d{1,4}[A-Z]?$")


def callsigns_for_flight(text):
    """The ICAO callsigns a typed flight number could be flying under.

    'UA 123' and 'ua123' → ['UAL123']; 'UAL123' → ['UAL123']; a code
    two airlines share gives both.  Leading zeros go, as they do on the
    air ('DL 0042' flies as DAL42).  IATA codes may hold a digit (B6,
    9E), so both a two- and a three-character airline code are tried.
    """
    q = re.sub(r"[\s-]", "", (text or "").upper())
    _airlines()
    out = []
    for n in (3, 2):
        code, number = q[:n], q[n:]
        if not _NUMBER.match(number):
            continue
        digits = number.lstrip("0") or "0"
        if n == 3 and code.isalpha() and code in _AIRLINES:
            out.append(code + digits)
        elif n == 2 and code.isalnum():
            out.extend(icao + digits for icao in _BY_IATA.get(code, ()))
    return list(dict.fromkeys(out))


def airlines_matching(text, limit=5):
    """ICAO codes of airlines whose name contains ``text`` (or whose code
    is it), best first: exact code, then name prefix, then anywhere."""
    q = (text or "").strip().lower()
    if len(q) < 2:
        return []
    exact, prefix, inside = [], [], []
    for icao, (name, iata) in _airlines().items():
        low = name.lower()
        if q in (icao.lower(), iata.lower()):
            exact.append(icao)
        elif low.startswith(q):
            prefix.append(icao)
        elif len(q) >= 4 and q in low:
            inside.append(icao)
    return (exact + prefix + inside)[:limit]


def type_name(designator):
    """'B38M' → 'Boeing 737 MAX 8', or None for a designator not on file."""
    return _types().get((designator or "").upper())


# what people call a type, to what its name contains
_NICKNAMES = {"dreamliner": "787", "jumbo": "747", "queen of the skies": "747",
              "superjumbo": "a380", "max": "max", "neo": "neo",
              "herc": "c-130", "hercules": "c-130", "osprey": "v-22",
              "caravan": "caravan", "king air": "king air"}


def types_matching(text):
    """Designators whose code is ``text`` or whose name contains it, so
    '737' finds the whole family and 'a380' finds A388."""
    q = (text or "").strip().lower()
    q = _NICKNAMES.get(q, q)
    if len(q) < 3:
        return []
    types = _types()
    if q.upper() in types:
        return [q.upper()]
    out = []
    for code, name in types.items():
        low = name.lower()
        if q in low or q.replace(" ", "") in low.replace(" ", "").replace("-", ""):
            out.append(code)
    return out
