"""Reading an airport off the live sky: the board and the runways in use."""

from blips._airport import board, metar_summary, runways_in_use
from blips._geo import advance
from blips._routes import Leg

AP = {"icao": "KTST", "iata": "TST", "lat": 40.0, "lon": -75.0, "elev": 100,
      "rwys": [{"len": 10000,
                "le": ["09", 90.0, 40.0, -75.02],
                "he": ["27", 270.0, 40.0, -74.98]}]}


def _ac(hex_, lat, lon, alt, track, vrate=0, gs=150, callsign="TST1",
        ground=False):
    return {"hex": hex_, "callsign": callsign, "flight": callsign,
            "lat": lat, "lon": lon, "alt": alt, "track": track,
            "vrate": vrate, "gs": gs, "ground": ground}


def test_landing_on_final_is_seen():
    # 5 nm west of the 09 threshold, inbound on 090, descending
    lat, lon = advance(40.0, -75.02, 270.0, 5.0)
    landing, departing = runways_in_use(AP, [_ac("a", lat, lon, 1700, 90,
                                                 -700)])
    assert landing == {"09": 1} and departing == {}


def test_departure_climbing_off_the_far_end():
    lat, lon = advance(40.0, -74.98, 90.0, 3.0)
    landing, departing = runways_in_use(AP, [_ac("a", lat, lon, 2500, 90,
                                                 2000)])
    assert departing == {"09": 1} and landing == {}


def test_overflight_counts_for_nothing():
    lat, lon = advance(40.0, -75.02, 270.0, 5.0)
    assert runways_in_use(AP, [_ac("a", lat, lon, 30000, 90)]) == ({}, {})


def test_board_sorts_and_classifies():
    here = Leg("Test", "TST", icao="KTST", iata="TST")
    far = Leg("Far", "FAR", icao="KFAR", iata="FAR")
    sky = [_ac("a", 40.5, -75.0, 9000, 180, -1000, callsign="AAA1"),
           _ac("b", 40.1, -75.0, 3000, 180, -800, callsign="BBB2"),
           _ac("c", 40.0, -75.0, None, 0, ground=True, callsign="CCC3"),
           _ac("d", 40.2, -75.0, 5000, 0, 2500, callsign="DDD4")]
    routes = {"AAA1": (far, here), "BBB2": (far, here),
              "CCC3": (here, far), "DDD4": (here, here)}
    arrivals, departures = board(AP, sky, routes)
    assert [e[0]["callsign"] for e in arrivals] == ["BBB2", "AAA1"]
    assert {e[0]["callsign"] for e in departures} == {"CCC3", "DDD4"}


def test_metar_summary():
    assert metar_summary({"wdir": 270, "wspd": 28, "wgst": 40,
                          "visib": "6+", "temp": 14, "fltCat": "VFR"}) \
        == "wind 270° 28G40 kt · vis 6+ mi · 14°C · VFR"
    assert metar_summary({"wspd": 0}) == "calm"
