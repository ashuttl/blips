"""TrafficPool.draw: the right real flight for the role, once each.

The pool and route lookup are faked throughout — no ADS-B sample, no
route API — so these tests exercise the draw semantics: role matching,
used-once, the anonymous fallback, and the empty-pool degradation.
"""

import random

import blips.game.fleet as fleet
from blips.game.fleet import TrafficPool

_AIRPORT = {"iata": "TPA", "icao": "KTPA", "lat": 27.98, "lon": -82.53}
_PERF = {"B738", "A320", "C56X"}

_BWI = ("Baltimore", "BWI")
_TPA = ("Tampa", "TPA")
_ATL = ("Atlanta", "ATL")
_MIA = ("Miami", "MIA")


class _Routes:
    """A route lookup that already knows everything it will ever know."""

    def __init__(self, routes):
        self._routes = routes

    def get(self, cs, lat=None, lon=None):
        return self._routes.get(cs)


def _pool(entries, routes):
    pool = TrafficPool(_AIRPORT, _PERF)
    pool._entries = [{"cs": cs, "actype": actype, "lat": 28.0, "lon": -82.0}
                     for cs, actype in entries]
    pool.routes = _Routes(routes)
    return pool


def test_an_inbound_flight_is_drawn_as_an_arrival_with_its_origin():
    pool = _pool([("SWA123", "B738")], {"SWA123": (_BWI, _TPA)})
    assert pool.draw("arrival") == ("SWA123", "B738", _BWI)


def test_a_wrong_direction_flight_never_spawns():
    # its real route leaves Tampa, so it can only ever be a departure
    pool = _pool([("SWA123", "B738")], {"SWA123": (_TPA, _MIA)})
    assert pool.draw("arrival") is None
    assert pool.draw("departure") == ("SWA123", "B738", _MIA)


def test_overflights_are_the_flights_that_pass_this_airport_by():
    pool = _pool([("SWA123", "B738"), ("DAL456", "A320")],
                 {"SWA123": (_BWI, _TPA), "DAL456": (_ATL, _MIA)})
    assert pool.draw("overflight") == ("DAL456", "A320", (_ATL, _MIA))


def test_each_entry_spawns_at_most_once():
    pool = _pool([("SWA123", "B738")], {"SWA123": (_BWI, _TPA)})
    assert pool.draw("arrival") is not None
    assert pool.draw("arrival") is None


def test_an_empty_pool_degrades_to_none():
    assert _pool([], {}).draw("arrival") is None


def test_route_unknown_entries_fill_any_role_anonymously():
    pool = _pool([("SWA123", "B738")], {})
    assert pool.draw("departure") == ("SWA123", "B738", None)


def test_anonymous_prefers_an_airline_over_the_bizjet_soup():
    pool = _pool([("ZZZ123", "C56X"), ("DAL456", "A320")], {})
    assert pool.draw("arrival")[0] == "DAL456"


def test_confirmed_only_never_hands_out_the_anonymous_fill():
    # confirmed_only is the leading draw: a route-unknown entry stays in
    # the pool for the anonymous phase rather than jumping the schedule
    pool = _pool([("SWA123", "B738")], {})
    assert pool.draw("arrival", confirmed_only=True) is None
    assert pool.draw("arrival") == ("SWA123", "B738", None)


def test_spent_when_nothing_could_still_confirm():
    pool = _pool([("SWA123", "B738"), ("DAL456", "A320"), ("AAL789", "A320")],
                 {"SWA123": (_BWI, _TPA), "DAL456": (_ATL, _MIA)})
    assert not pool.spent()      # SWA123 could still lead an arrival
    pool.draw("arrival")
    assert not pool.spent()      # AAL789's route may yet fill in
    pool.draw("departure")       # ...but it flies anonymously instead
    assert pool.spent()          # DAL456 only ever passes overhead


def test_sample_filters_the_pool_and_shuffles_with_its_own_rng(monkeypatch):
    raw = [
        {"callsign": "SWA123", "actype": "B38M", "lat": 28.0, "lon": -82.0},
        {"callsign": "SWA123", "actype": "B738", "lat": 28.1, "lon": -82.1},
        {"callsign": "N123AB", "actype": "B738", "lat": 28.0, "lon": -82.0},
        {"callsign": "DAL456", "actype": "ZZZZ", "lat": 28.0, "lon": -82.0},
        {"callsign": "AAL789", "actype": "A320", "lat": 28.0, "lon": -82.0},
    ]
    monkeypatch.setattr(fleet, "fetch_point",
                        lambda lat, lon, r: (list(raw), "faked"))
    pools = []
    for _ in range(2):
        pool = TrafficPool(_AIRPORT, _PERF, rng=random.Random(42))
        pool.routes = _Routes({})    # keep the warm-up off the network
        pool._sample()
        pools.append(pool)
    # dupes, GA tails and unknown types are gone; B38M aliased to B738
    kept = {(e["cs"], e["actype"]) for e in pools[0]._entries}
    assert kept == {("SWA123", "B738"), ("AAL789", "A320")}
    # a seeded rng makes the shuffle reproducible run to run
    assert pools[0]._entries == pools[1]._entries
