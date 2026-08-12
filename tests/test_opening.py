"""The live opening: the picture you inherit is the real one.

The pool's sample and the route lookup are faked throughout — no ADS-B,
no route API — so these tests exercise the classifier (who is genuinely
inbound, outbound, or just passing), the curtain (the open waits for the
sample, briefly), and the real-state spawns (true position and altitude,
centre's dim strips for the stream, mutual separation on admission).
"""

import threading

from blips._airports import find_airport
from blips._geo import advance, bearing_to, haversine_nm
from blips.game.fleet import TrafficPool
from blips.game.sim import SECTOR_NM, Sim

_AIRPORT = {"iata": "TPA", "icao": "KTPA", "lat": 27.98, "lon": -82.53}
_PERF = {"B738", "A320", "E175"}

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


def _entry(cs, dist_nm, brg_from_field, alt, track, gs=350.0, vrate=-1000.0,
           actype="B738"):
    """A sampled flight placed ``dist_nm`` out on ``brg_from_field``."""
    lat, lon = advance(_AIRPORT["lat"], _AIRPORT["lon"],
                       brg_from_field, dist_nm)
    return {"cs": cs, "actype": actype, "lat": lat, "lon": lon,
            "alt": alt, "gs": gs, "track": track, "vrate": vrate}


def _pool(entries, routes=None):
    pool = TrafficPool(_AIRPORT, _PERF)
    pool._entries = entries
    pool.routes = _Routes(routes or {})
    pool.sampled.set()
    return pool


# -- the classifier -----------------------------------------------------------

def test_a_descending_closer_inside_the_ring_opens_as_an_arrival():
    # 30 nm north of the field, tracking south at 11,000 and coming down
    pool = _pool([_entry("DAL100", 30.0, 360.0, alt=11000.0, track=180.0)])
    picture = pool.opening(SECTOR_NM, 26.0)
    assert [e["cs"] for e in picture["arrivals"]] == ["DAL100"]
    assert picture["handins"] == [] and picture["departures"] == []


def test_the_stream_outside_the_ring_classifies_as_handins_nearest_first():
    pool = _pool([_entry("DAL200", 80.0, 90.0, alt=22000.0, track=270.0),
                  _entry("DAL100", 60.0, 90.0, alt=18000.0, track=270.0)])
    picture = pool.opening(SECTOR_NM, 26.0)
    assert [e["cs"] for e in picture["handins"]] == ["DAL100", "DAL200"]


def test_a_climber_tracking_away_near_the_field_is_a_departure():
    pool = _pool([_entry("SWA300", 12.0, 45.0, alt=6000.0, track=45.0,
                         gs=280.0, vrate=2500.0)])
    picture = pool.opening(SECTOR_NM, 26.0)
    assert [e["cs"] for e in picture["departures"]] == ["SWA300"]


def test_a_resolved_route_elsewhere_vetoes_a_flight_that_points_here():
    # closing geometry, but its route says Atlanta to Miami: a passer-by
    pool = _pool([_entry("DAL400", 30.0, 360.0, alt=12000.0, track=180.0)],
                 {"DAL400": (_ATL, _MIA)})
    picture = pool.opening(SECTOR_NM, 26.0)
    assert picture["arrivals"] == [] and picture["handins"] == []


def test_a_resolved_route_here_confirms_a_dogleg_the_track_would_miss():
    # tracking east — a downwind dogleg — but the route ends at Tampa
    pool = _pool([_entry("JBU500", 35.0, 360.0, alt=12000.0, track=90.0)],
                 {"JBU500": (_BWI, _TPA)})
    picture = pool.opening(SECTOR_NM, 26.0)
    assert [e["cs"] for e in picture["arrivals"]] == ["JBU500"]
    assert picture["arrivals"][0]["far"] == _BWI


def test_the_short_final_stays_with_the_outgoing_controller():
    # 8 nm out and pointed at the field: already sequenced, not yours
    pool = _pool([_entry("DAL800", 8.0, 360.0, alt=4000.0, track=180.0)])
    picture = pool.opening(SECTOR_NM, 26.0)
    assert all(not v for v in picture.values())


def test_ground_targets_and_high_cruisers_never_open_the_shift():
    grounded = _entry("AAL600", 2.0, 0.0, alt=None, track=180.0, gs=15.0)
    cruiser = _entry("UAL700", 90.0, 360.0, alt=35000.0, track=180.0,
                     vrate=0.0)
    pool = _pool([grounded, cruiser])
    picture = pool.opening(SECTOR_NM, 26.0)
    assert all(not v for v in picture.values())


def test_a_taken_flight_never_comes_back_out_of_the_pool():
    pool = _pool([_entry("DAL100", 30.0, 360.0, alt=11000.0, track=180.0)],
                 {"DAL100": (_BWI, _TPA)})
    pool.take("DAL100")
    assert pool.draw("arrival") is None
    assert pool.opening(SECTOR_NM, 26.0)["arrivals"] == []


# -- the curtain and the real spawns ------------------------------------------

class _StubPool:
    """A pool whose sample already landed, holding a fixed picture."""

    def __init__(self, picture, sampled=True):
        self.sampled = threading.Event()
        if sampled:
            self.sampled.set()
        self._picture = picture
        self.taken = []
        self.routes = _Routes({})

    def opening(self, sector_nm, elev):
        return {"arrivals": [], "handins": [], "departures": [],
                **self._picture}

    def take(self, cs):
        self.taken.append(cs)

    def draw(self, role, confirmed_only=False):
        return None

    def spent(self):
        return False


def _tick(sim, seconds=1.0, step=1.0):
    t = sim._last_tick if sim._last_tick is not None else sim.start
    sim._last_tick = t
    while seconds > 0:
        t += step
        sim.tick(t)
        seconds -= step
    return t


def _opened(picture, **kw):
    tpa = find_airport("tpa")
    sim = Sim(tpa, seed=1, pool=_StubPool(picture, **kw))
    assert sim.aircraft == []          # the curtain holds the open
    _tick(sim)
    return sim


def test_the_open_waits_for_the_sample_then_spawns_the_real_picture():
    e = _entry("DAL100", 30.0, 360.0, alt=11400.0, track=180.0)
    e.update(dist=30.0, far=_BWI)
    sim = _opened({"arrivals": [e]})
    real = [ac for ac in sim.aircraft if ac.get("real")]
    assert [ac["callsign"] for ac in real] == ["DAL100"]
    ac = real[0]
    assert ac["alt"] == 11400.0 and ac["from"] == "Baltimore"
    assert haversine_nm(ac["lat"], ac["lon"], e["lat"], e["lon"]) < 0.1
    assert not ac.get("pre_ho")
    # the check-in speaks telephony, so look for the origin, not the code
    assert any(kind == "checkin" and "with you" in line
               and "Baltimore" in line for _, line, kind in sim.radio)
    assert any("the real one" in line for _, line, _ in sim.radio)
    assert "DAL100" in sim.pool.taken


def test_the_stream_rides_the_handin_machinery_to_its_true_eta():
    e = _entry("DAL200", 55.0, 360.0, alt=16000.0, track=180.0, gs=420.0)
    e["dist"] = 55.0
    sim = _opened({"handins": [e]})
    ac = next(a for a in sim.aircraft if a["callsign"] == "DAL200")
    assert ac.get("pre_ho") and ac.get("dim")
    assert ac["tgt_alt"] < ac["alt"]   # centre descends it toward the band
    checkins = sum(kind == "checkin" for _, _, kind in sim.radio)
    # fly it across the boundary: it checks in the moment it crosses
    _tick(sim, seconds=(55.0 - SECTOR_NM) / 420.0 * 3600.0 + 120.0,
          step=2.0)
    assert not ac.get("pre_ho")
    assert sum(kind == "checkin" for _, _, kind in sim.radio) > checkins


def test_a_real_departure_wants_the_exit_its_climb_points_at():
    e = _entry("SWA300", 12.0, 45.0, alt=5600.0, track=45.0,
               gs=280.0, vrate=2500.0)
    e["dist"] = 12.0
    sim = _opened({"departures": [e]})
    ac = next(a for a in sim.aircraft if a["callsign"] == "SWA300")
    # one tick of climb has already flown by the time we look
    assert ac["plan"] == "departure" and abs(ac["alt"] - 5600.0) < 100.0
    assert ac["fix"] in sim.sector["exits"]
    assert ac["tgt_alt"] >= ac["alt"]
    # its exit is the gate its own track points at
    want = bearing_to(sim.airport["lat"], sim.airport["lon"],
                      *sim.sector["fixes"][ac["fix"]])
    assert abs((want - 45.0 + 180.0) % 360.0 - 180.0) <= 90.0


def test_the_opening_admits_only_a_mutually_separated_set():
    a = _entry("DAL100", 30.0, 360.0, alt=11000.0, track=180.0)
    b = _entry("DAL110", 31.5, 360.0, alt=11200.0, track=180.0)  # on top
    a["dist"], b["dist"] = 30.0, 31.5
    sim = _opened({"arrivals": [a, b]})
    real = [ac for ac in sim.aircraft if ac.get("real")]
    assert [ac["callsign"] for ac in real] == ["DAL100"]
    assert "DAL110" not in sim.pool.taken   # still castable later


def test_reality_understudied_the_vendored_open_tops_up():
    e = _entry("DAL100", 30.0, 360.0, alt=11000.0, track=180.0)
    e["dist"] = 30.0
    sim = _opened({"arrivals": [e]})
    yours = [ac for ac in sim.aircraft
             if ac["plan"] == "arrival" and not ac.get("pre_ho")]
    assert 3 <= len(yours) <= 4        # the want the open always had
    assert any(ac["plan"] == "departure" for ac in sim.aircraft)


def test_an_empty_sample_opens_the_shift_the_old_way():
    sim = _opened({})
    arrivals = [ac for ac in sim.aircraft if ac["plan"] == "arrival"]
    assert 3 <= len(arrivals) <= 4
    assert not any(ac.get("real") for ac in sim.aircraft)


def test_the_curtain_times_out_when_the_sample_never_lands():
    tpa = find_airport("tpa")
    sim = Sim(tpa, seed=1, pool=_StubPool({}, sampled=False))
    assert sim.aircraft == []
    _tick(sim, seconds=5.0)
    assert sim.aircraft == []          # still holding
    _tick(sim, seconds=2.0)
    assert any(ac["plan"] == "arrival" for ac in sim.aircraft)


def test_an_offline_or_seeded_shift_never_holds_a_curtain():
    sim = Sim(find_airport("tpa"), seed=1)     # pool=None, as seeded shifts
    assert sim._curtain is None
    assert any(ac["plan"] == "arrival" for ac in sim.aircraft)
