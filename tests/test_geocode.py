"""Place names the way people type them: "portland maine", "london on"."""

import blips._location as loc


def _fake(monkeypatch, table):
    asked = []

    def fake_search(name, count=10):
        asked.append(name)
        return table.get(name.lower(), [])

    monkeypatch.setattr(loc, "_search", fake_search)
    return asked


PORTLANDS = [
    {"name": "Portland", "admin1": "Oregon", "country_code": "US",
     "latitude": 45.5, "longitude": -122.7, "population": 650000},
    {"name": "Portland", "admin1": "Maine", "country_code": "US",
     "latitude": 43.66, "longitude": -70.26, "population": 66000},
]


def test_name_and_state(monkeypatch):
    _fake(monkeypatch, {"portland": PORTLANDS})
    for q in ("portland maine", "portland me", "Portland, ME"):
        lat, lon, label = loc.geocode_place(q)
        assert (lat, lon) == (43.66, -70.26), q
        assert label == "Portland, Maine"


def test_whole_query_first(monkeypatch):
    asked = _fake(monkeypatch, {"portland": PORTLANDS})
    lat, _lon, _label = loc.geocode_place("portland")
    assert lat == 45.5 and asked == ["portland"]   # population decides


def test_airport_fallback(monkeypatch):
    _fake(monkeypatch, {})
    lat, lon, name = loc.geocode_place("kpwm")
    assert "Portland" in name
