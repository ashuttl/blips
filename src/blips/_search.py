"""Find a flight, a route, an airport or a place from a few typed letters.

The scope's ``/`` field asks ``Search.results()`` on every repaint, so the
answers follow both the typing and the sky.  What a query can mean:

  - a flight: a callsign (DAL1673), a flight number as printed (DL 1673,
    UA123), a registration (N123DL, G-EUPT) or a hex code (a1b2c3).
    Traffic on screen and traffic seen earlier this session answer at
    once; a moment after the typing stops, the same identity is asked of
    the whole world (``_adsb.lookup``), so a flight over another ocean is
    one Enter away.
  - a route: BOS-SFO, BOS SFO, BOS→SFO, "to SFO", "from BOS" — answered
    from the routes the scope has looked up for traffic it has seen.  No
    free source lists every BOS→SFO flight in the world, so this is the
    sky the scope knows, and the result says so.
  - an airport: ICAO or IATA code, or part of its name or city.
  - a filter: an airline's name (delta), a type (737, A388, dreamliner),
    military, emergency, above/below an altitude.
  - a place: whatever else, geocoded when chosen.

Results are ``Result`` objects; the app decides what choosing one does.
"""

import re
import threading
import time

from blips import _names
from blips._airports import _load as _load_airports
from blips._geo import haversine_nm

DEBOUNCE_S = 0.35     # stillness before the world is asked
LOOKUP_TTL_S = 30.0   # a worldwide answer is good this long
MAX_RESULTS = 9

_ROUTE = re.compile(
    r"^([A-Z0-9]{3,4})\s*(?:-+|–|—|>|→|\bTO\b|\s)\s*([A-Z0-9]{3,4})$")
_TO_FROM = re.compile(r"^(TO|FROM|INTO|OUT OF)\s+([A-Z0-9]{3,4})$")
_HEX = re.compile(r"^[0-9A-F]{6}$")
_CALLSIGN = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]{0,2}$")
_REG = re.compile(r"^(N[1-9][0-9A-Z]{0,4}|[A-Z0-9]{1,2}-[A-Z0-9]{2,5}|"
                  r"[A-Z]-[A-Z]{4})$")
_ALT = re.compile(r"^(ABOVE|BELOW|OVER|UNDER)\s+(?:FL\s*)?(\d{2,5})$")


class Filter:
    """A predicate over aircraft that the scope can hold on screen:
    ``test(ac, route)`` with the aircraft's route (or None)."""

    def __init__(self, key, label, test):
        self.key = key        # what makes two filters the same filter
        self.label = label    # how the header names it
        self.test = test

    def __eq__(self, other):
        return isinstance(other, Filter) and other.key == self.key

    def __hash__(self):
        return hash(self.key)


class Result:
    """One line of the result list."""

    def __init__(self, kind, label, detail="", *, ac=None, airport=None,
                 filter=None, query=None, where="here", count=None):
        self.kind = kind          # flight, airport, filter, place, note
        self.label = label
        self.detail = detail
        self.ac = ac
        self.airport = airport
        self.filter = filter
        self.query = query
        self.where = where        # flights: here, seen, world
        self.count = count

    def __repr__(self):
        return f"Result({self.kind}, {self.label!r}, {self.detail!r})"


def _norm_reg(reg):
    return (reg or "").replace("-", "").upper()


def _leg_codes(leg):
    return {c for c in (leg[1], getattr(leg, "icao", ""),
                        getattr(leg, "iata", "")) if c}


def route_filter(origin, dest):
    """Aircraft whose known route runs from ``origin`` to ``dest`` (codes,
    ICAO or IATA; either may be None for "to X" / "from X")."""
    o = (origin or "").upper()
    d = (dest or "").upper()

    def test(ac, route):
        if not route:
            return False
        if o and o not in _leg_codes(route[0]):
            return False
        if d and d not in _leg_codes(route[-1]):
            return False
        return True

    if o and d:
        label = f"{o}→{d}"
    elif d:
        label = f"to {d}"
    else:
        label = f"from {o}"
    return Filter(("route", o, d), label, test)


def airline_filter(icao):
    name = (_names.airline(icao) or (icao,))[0]
    return Filter(("airline", icao), name,
                  lambda ac, _r: (ac.get("flight") or ac["callsign"])
                  .upper().startswith(icao)
                  and (ac.get("flight") or ac["callsign"])[3:4].isdigit())


def type_filter(codes, label):
    codes = frozenset(codes)
    return Filter(("type", codes), label,
                  lambda ac, _r: (ac.get("actype") or "").upper() in codes)


def keyword_filter(q):
    """military / emergency / heavies / above FL300 / below 10000 → Filter."""
    word = q.strip().upper()
    if word in ("MIL", "MILITARY"):
        return Filter(("mil",), "military", lambda ac, _r: ac.get("mil"))
    if word in ("EMERGENCY", "EMERGENCIES", "7700", "SQUAWK 7700", "MAYDAY"):
        return Filter(("emergency",), "emergencies",
                      lambda ac, _r: ac.get("emergency")
                      or ac.get("squawk") in ("7500", "7600", "7700"))
    if word in ("HEAVY", "HEAVIES", "WIDEBODY", "WIDEBODIES"):
        return Filter(("heavy",), "heavies",
                      lambda ac, _r: ac.get("category") in ("A5", "A4"))
    if word in ("HELI", "HELICOPTER", "HELICOPTERS", "ROTORCRAFT"):
        return Filter(("rotor",), "helicopters",
                      lambda ac, _r: ac.get("category") == "A7")
    if word in ("GROUND", "ON THE GROUND"):
        return Filter(("ground",), "on the ground",
                      lambda ac, _r: ac.get("ground"))
    m = _ALT.match(word)
    if m:
        n = int(m.group(2))
        ft = n * 100 if (n < 1000 or "FL" in word) else n
        if m.group(1) in ("ABOVE", "OVER"):
            return Filter(("above", ft), f"above {ft:,} ft",
                          lambda ac, _r: (ac.get("alt") or 0) > ft)
        return Filter(("below", ft), f"below {ft:,} ft",
                      lambda ac, _r: ac.get("alt") is not None
                      and ac["alt"] < ft)
    return None


class Search:
    """The query, its results, and the worldwide lookups behind them.

    ``lookup(kind, value)`` is ``_adsb.lookup``'s shape; it runs on a
    background thread, and ``on_update()`` is called when an answer
    lands (the app passes the live loop's nudge).
    """

    def __init__(self, lookup=None, on_update=None, clock=time.monotonic):
        if lookup is None:
            from blips._adsb import lookup
        self._lookup = lookup
        self._on_update = on_update
        self._clock = clock
        self.query = ""
        self.cursor = 0
        self._changed = clock()
        self._world = {}      # (kind, value) → (stamp, [ac]) answered
        self._asking = set()  # (kind, value) in flight
        self._failed = set()
        self._lock = threading.Lock()
        self._airports = None

    # -- editing -------------------------------------------------------------
    def set_query(self, text):
        if text != self.query:
            self.query = text
            self.cursor = 0
            self._changed = self._clock()

    def type(self, ch):
        self.set_query(self.query + ch)

    def backspace(self):
        self.set_query(self.query[:-1])

    def kill_word(self):
        self.set_query(self.query.rstrip().rpartition(" ")[0]
                       + (" " if " " in self.query.rstrip() else ""))

    def move(self, step, n):
        if n:
            self.cursor = (self.cursor + step) % n

    # -- the world -------------------------------------------------------------
    def _ask_world(self, kind, value):
        """A worldwide answer for (kind, value): the list if it's in,
        None if it's on its way (asked once the typing settles)."""
        key = (kind, value)
        now = self._clock()
        with self._lock:
            hit = self._world.get(key)
            if hit is not None and now - hit[0] < LOOKUP_TTL_S:
                return hit[1]
            if key in self._asking or key in self._failed:
                return None
            if now - self._changed < DEBOUNCE_S:
                return None
            self._asking.add(key)
        threading.Thread(target=self._run_lookup, args=(key,), daemon=True,
                         name="search lookup").start()
        return None

    def _run_lookup(self, key):
        try:
            found, _src = self._lookup(*key)
        except Exception:
            found = None
        with self._lock:
            self._asking.discard(key)
            if found is None:
                self._failed.add(key)
            else:
                self._world[key] = (self._clock(), found)
        if self._on_update is not None:
            self._on_update()

    def waiting(self):
        """True while a worldwide lookup for the current query is out —
        or about to be, once the typing settles."""
        with self._lock:
            return bool(self._asking) or (
                self._clock() - self._changed < DEBOUNCE_S
                and bool(self.query.strip()))

    def due_in(self):
        """Seconds until the debounce lets the world be asked (0 if now)."""
        return max(0.0, DEBOUNCE_S - (self._clock() - self._changed))

    # -- airports --------------------------------------------------------------
    def _airport_rows(self):
        if self._airports is None:
            self._airports = [
                (ap["icao"], ap.get("iata") or "", ap["name"].lower(),
                 (ap.get("city") or "").lower(), ap)
                for ap in _load_airports()]
        return self._airports

    def airports(self, q, limit=4):
        Q, low = q.upper(), q.lower()
        exact, prefix, inside = [], [], []
        for icao, iata, name, city, ap in self._airport_rows():
            if Q == icao or (iata and Q == iata):
                exact.append(ap)
            elif len(low) >= 3 and (name.startswith(low) or city.startswith(low)):
                prefix.append(ap)
            elif len(low) >= 4 and (low in name or low in city):
                inside.append(ap)
        large = lambda aps: sorted(aps, key=lambda a: not a["large"])  # noqa: E731
        return (exact + large(prefix) + large(inside))[:limit]

    # -- results ---------------------------------------------------------------
    def results(self, aircraft, seen=(), routes=None, centre=None):
        """What the query means against this sky: a list of Result.

        ``aircraft`` is the traffic in the feed now; ``seen`` the records
        of traffic seen earlier this session (newest last); ``routes`` a
        {callsign: route} map; ``centre`` the (lat, lon) distances and
        world results are ordered from.
        """
        q = " ".join(self.query.split())
        if not q:
            return []
        Q = q.upper()
        compact = re.sub(r"[\s-]", "", Q)
        routes = routes or {}
        out = []
        have = set()          # hexes already listed

        def dist(ac):
            if centre is None or ac.get("lat") is None:
                return 0.0
            return haversine_nm(centre[0], centre[1], ac["lat"], ac["lon"])

        here = {ac["hex"]: ac for ac in aircraft}
        earlier = {ac["hex"]: ac for ac in seen if ac["hex"] not in here}

        def add_flight(ac, where):
            if ac["hex"] in have or len(out) >= MAX_RESULTS * 2:
                return
            have.add(ac["hex"])
            out.append(Result("flight", ac["callsign"], _flight_detail(
                ac, routes.get((ac.get("flight") or "").upper()), where,
                dist(ac)), ac=ac, where=where))

        # 1. identity: callsign, flight number, registration, hex
        flight_cs = set(_names.callsigns_for_flight(Q))
        if _CALLSIGN.match(compact):
            flight_cs.add(compact)
        wanted_reg = _norm_reg(compact)
        is_hex = bool(_HEX.match(compact))

        def identity(ac):
            cs = (ac.get("flight") or ac["callsign"]).upper()
            return (cs in flight_cs or cs == compact
                    or (ac.get("reg") and _norm_reg(ac["reg"]) == wanted_reg)
                    or (is_hex and ac["hex"].upper() == compact))

        for where, pool in (("here", here), ("seen", earlier)):
            for ac in sorted((a for a in pool.values() if identity(a)), key=dist):
                add_flight(ac, where)

        # 2. airports by code
        code_airports = [ap for ap in self.airports(q, limit=2)
                         if Q in (ap["icao"], ap.get("iata"))]
        for ap in code_airports:
            out.append(_airport_result(ap, centre))

        # 3. routes
        m = _ROUTE.match(Q) or _TO_FROM.match(Q)
        if m and not (len(m.group(1)) == 3 and m.group(2).isdigit()):
            if m.re is _TO_FROM:
                origin, dest = ((None, m.group(2)) if m.group(1) in ("TO", "INTO")
                                else (m.group(2), None))
            else:
                origin, dest = m.group(1), m.group(2)
            flt = route_filter(origin, dest)
            matched = [ac for ac in list(here.values()) + list(earlier.values())
                       if flt.test(ac, routes.get((ac.get("flight") or "").upper()))]
            n_here = sum(1 for ac in matched if ac["hex"] in here)
            out.append(Result("filter", f"Show only {flt.label}",
                              f"{n_here} here" + (f", {len(matched) - n_here} seen earlier"
                                                  if len(matched) > n_here else "")
                              + " · from routes looked up this session",
                              filter=flt, count=n_here))
            for ac in sorted(matched, key=dist):
                add_flight(ac, "here" if ac["hex"] in here else "seen")

        # 4. flights by prefix, on screen first
        if len(compact) >= 2 and not m:
            for where, pool in (("here", here), ("seen", earlier)):
                hits = [a for a in pool.values()
                        if (a.get("flight") or a["callsign"]).upper().startswith(compact)
                        or (len(compact) >= 3 and a.get("reg")
                            and _norm_reg(a["reg"]).startswith(wanted_reg))]
                for ac in sorted(hits, key=dist)[:6]:
                    add_flight(ac, where)

        # 5. the world: the same identity, asked everywhere — unless it's
        # already on screen, which is the answer
        found_here = any(r.kind == "flight" and r.where == "here"
                         and identity(r.ac) for r in out)
        world_keys = []
        for cs in sorted(flight_cs)[:2]:
            world_keys.append(("callsign", cs))
        if _REG.match(Q.replace(" ", "")):
            world_keys.append(("reg", Q.replace(" ", "")))
        if is_hex:
            world_keys.append(("hex", compact.lower()))
        answered = 0
        for key in ([] if found_here else world_keys):
            found = self._ask_world(*key)
            if found is not None:
                answered += 1
            for ac in sorted(found or (), key=dist):
                if ac["hex"] in here:
                    continue
                add_flight(ac, "world")
        if (world_keys and not found_here and answered == len(world_keys)
                and not any(r.kind == "flight" for r in out)):
            out.append(Result("note", f"{q.upper()} isn't flying where the "
                              "feeds can see", "not airborne, or no "
                              "receiver in range"))

        # 6. filters: airline, type, keywords
        kw = keyword_filter(q)
        if kw is not None:
            n = sum(1 for ac in here.values() if kw.test(ac, None))
            out.append(Result("filter", f"Show only {kw.label}", f"{n} here",
                              filter=kw, count=n))
        if len(q) >= 3 and not is_hex:
            for icao in _names.airlines_matching(q, limit=2):
                flt = airline_filter(icao)
                n = sum(1 for ac in here.values() if flt.test(ac, None))
                if n or Q in (icao, (_names.airline(icao) or ("", ""))[1]):
                    out.append(Result("filter", f"Show only {flt.label}",
                                      f"{n} here", filter=flt, count=n))
            codes = _names.types_matching(q)
            if codes:
                exact = len(codes) == 1 and codes[0] == Q
                label = (f"{Q} · {_names.type_name(Q)}" if exact
                         else _type_family(q, codes))
                flt = type_filter(codes, label)
                n = sum(1 for ac in here.values() if flt.test(ac, None))
                out.append(Result("filter", f"Show only {label}", f"{n} here",
                                  filter=flt, count=n))
                if exact:
                    found = self._ask_world("type", Q)
                    if found:
                        out.append(Result(
                            "note", f"{len(found)} {Q} flying worldwide",
                            "nearest listed below"))
                        for ac in sorted(found, key=dist)[:4]:
                            if ac["hex"] not in here:
                                add_flight(ac, "world")

        # 7. airports by name or city
        for ap in self.airports(q, limit=4):
            if ap not in code_airports:
                out.append(_airport_result(ap, centre))

        # 8. somewhere else entirely
        if len(q) >= 3:
            out.append(Result("place", f"Go to “{q}”", "a place, by name",
                              query=q))
        if self.waiting():
            out.append(Result("note", "searching the world…", ""))
        return _rank(out)[:MAX_RESULTS]


def _rank(results):
    """Flights here, then airports by code, routes, the world… — the order
    they were added, but with notes kept beside what they explain and the
    place fallback last."""
    places = [r for r in results if r.kind == "place"]
    waiting = [r for r in results if r.kind == "note"
               and r.label.startswith("searching")]
    rest = [r for r in results if r not in places and r not in waiting]
    return rest + waiting + places


def _type_family(q, codes):
    names = [_names.type_name(c) or c for c in codes]
    if len(codes) <= 3:
        return " / ".join(names)
    return f"{q.strip()} family · {len(codes)} types"


def _flight_detail(ac, route, where, dist_nm):
    bits = []
    fn = _names.flight_number(ac.get("flight") or ac["callsign"])
    if fn and fn != ac["callsign"]:
        bits.append(fn)
    if route:
        bits.append(f"{route[0][1]}→{route[-1][1]}")
    if ac.get("actype"):
        bits.append(ac["actype"])
    if ac.get("reg") and ac["reg"] != ac["callsign"]:
        bits.append(ac["reg"])
    if where == "here":
        bits.append(f"{dist_nm:.0f} nm")
    elif where == "seen":
        bits.append("seen earlier")
    else:
        bits.append(f"{dist_nm:,.0f} nm away" if dist_nm else "elsewhere")
    return " · ".join(bits)


def _airport_result(ap, centre):
    code = ap.get("iata") or ap["icao"]
    detail = f"{ap['icao']} · {ap.get('city') or ''}".strip(" ·")
    if centre is not None:
        d = haversine_nm(centre[0], centre[1], ap["lat"], ap["lon"])
        detail += f" · {d:,.0f} nm"
    return Result("airport", f"{code}  {ap['name']}", detail, airport=ap)
