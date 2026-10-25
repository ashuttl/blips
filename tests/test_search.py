"""Search: what a few typed letters mean against the sky on screen.

The worldwide lookup is faked, and the clock is a counter, so these test
the meaning of a query, the order of the answers, and the debounce —
not the network.
"""

from blips._search import Search, keyword_filter, route_filter
from blips._routes import Leg


def _ac(hex_, callsign, lat=40.6, lon=-73.8, **kw):
    ac = {"hex": hex_, "callsign": callsign, "flight": callsign, "reg": "",
          "actype": "", "lat": lat, "lon": lon, "alt": 30000, "ground": False,
          "category": "", "mil": False, "emergency": False, "squawk": ""}
    ac.update(kw)
    return ac


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _search(world=None):
    clock = Clock()
    asked = []

    def lookup(kind, value):
        asked.append((kind, value))
        return (world or {}).get((kind, value), []), "fake"

    s = Search(lookup=lookup, clock=clock)
    return s, clock, asked


def _settle(s, clock, *args, **kw):
    """Run results once past the debounce, then wait for lookups."""
    clock.t += 1.0
    s.results(*args, **kw)
    import time
    for _ in range(50):
        if not s._asking:
            break
        time.sleep(0.01)
    return s.results(*args, **kw)


def test_flight_number_finds_the_callsign_on_screen():
    s, clock, asked = _search()
    sky = [_ac("a1", "UAL123"), _ac("a2", "DAL42")]
    s.set_query("UA 123")
    res = _settle(s, clock, sky)
    assert res[0].kind == "flight" and res[0].ac["callsign"] == "UAL123"
    assert "UA123" not in [r.label for r in res[1:] if r.kind == "flight"]
    assert asked == []          # on screen already: the world isn't asked


def test_flight_elsewhere_is_asked_of_the_world_after_typing_stops():
    far = _ac("b2", "QFA1", lat=-33.9, lon=151.2)
    s, clock, asked = _search({("callsign", "QFA1"): [far]})
    s.set_query("QF1")
    first = s.results([], centre=(40.6, -73.8))
    assert not any(r.kind == "flight" for r in first)
    assert asked == []          # still typing
    res = _settle(s, clock, [], centre=(40.6, -73.8))
    flights = [r for r in res if r.kind == "flight"]
    assert flights and flights[0].where == "world"
    assert ("callsign", "QFA1") in asked


def test_registration_and_hex():
    s, clock, _ = _search()
    sky = [_ac("abc123", "N55294", reg="N55294"), _ac("a1b2c3", "DAL1")]
    s.set_query("n55294")
    assert _settle(s, clock, sky)[0].ac["hex"] == "abc123"
    s.set_query("a1b2c3")
    assert _settle(s, clock, sky)[0].ac["callsign"] == "DAL1"


def test_airport_by_code_and_by_name():
    s, clock, _ = _search()
    s.set_query("jfk")
    res = _settle(s, clock, [])
    assert res[0].kind == "airport" and res[0].airport["icao"] == "KJFK"
    s.set_query("heathrow")
    res = _settle(s, clock, [])
    assert any(r.kind == "airport" and r.airport["icao"] == "EGLL"
               for r in res)


def test_route_search_uses_known_routes():
    s, clock, _ = _search()
    sky = [_ac("a1", "DAL1673"), _ac("a2", "JBU109"), _ac("a3", "AAL1")]
    routes = {"DAL1673": (Leg("Newark", "EWR", icao="KEWR", iata="EWR"),
                          Leg("Atlanta", "ATL", icao="KATL", iata="ATL")),
              "JBU109": (Leg("New York", "JFK", icao="KJFK", iata="JFK"),
                         Leg("Boston", "BOS", icao="KBOS", iata="BOS"))}
    for q in ("EWR-ATL", "ewr atl", "EWR→KATL", "to atl"):
        s.set_query(q)
        res = _settle(s, clock, sky, routes=routes)
        assert res[0].kind == "filter", q
        flights = [r.ac["callsign"] for r in res if r.kind == "flight"]
        assert flights == ["DAL1673"], q


def test_dal_123_is_a_flight_not_a_route():
    s, clock, _ = _search()
    s.set_query("dal 123")
    res = _settle(s, clock, [_ac("a1", "DAL123")])
    assert res[0].kind == "flight"


def test_airline_and_type_filters():
    s, clock, _ = _search()
    sky = [_ac("a1", "DAL1", actype="B738"), _ac("a2", "DAL2", actype="A321"),
           _ac("a3", "UAL3", actype="B738")]
    s.set_query("delta")
    res = _settle(s, clock, sky)
    flt = next(r for r in res if r.kind == "filter")
    assert flt.count == 2
    assert sorted(a["callsign"] for a in sky if flt.filter.test(a, None)) \
        == ["DAL1", "DAL2"]
    s.set_query("737")
    res = _settle(s, clock, sky)
    flt = next(r for r in res if r.kind == "filter")
    assert flt.count == 2


def test_keyword_filters():
    assert keyword_filter("military").test({"mil": True}, None)
    above = keyword_filter("above FL300")
    assert above.test({"alt": 35000}, None) and not above.test({"alt": 20000}, None)
    below = keyword_filter("below 10000")
    assert below.test({"alt": 5000}, None) and not below.test({"alt": None}, None)
    assert keyword_filter("pizza") is None


def test_route_filter_matches_either_code():
    leg = lambda code, icao: Leg(code, code, icao=icao, iata=code)  # noqa: E731
    route = (leg("BOS", "KBOS"), leg("SFO", "KSFO"))
    assert route_filter("KBOS", "SFO").test({}, route)
    assert route_filter(None, "KSFO").test({}, route)
    assert not route_filter("SFO", None).test({}, route)


def test_a_place_is_always_offered_last():
    s, clock, _ = _search()
    s.set_query("portland maine")
    res = _settle(s, clock, [])
    assert res[-1].kind == "place" and res[-1].query == "portland maine"
