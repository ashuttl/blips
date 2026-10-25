"""Source election: the aggregator that sees this sky best wins.

_fetch_source is faked throughout, so these tests exercise the voting
rules — empty-is-not-final, winner stickiness, re-votes — not the wire.
"""

import pytest

import blips._adsb as adsb
from blips._adsb import SourcePicker, fetch_point


def _install(monkeypatch, results, calls):
    """Fake the wire: results[name] is a list, or an Exception to raise.

    The table is a fake trio too, so the voting rules are tested with
    three voters whatever the real SOURCES holds this season."""

    def fake(source, lat, lon, radius_nm, timeout):
        name = source[0]
        calls.append(name)
        out = results[name]
        if isinstance(out, Exception):
            raise out
        return list(out)

    monkeypatch.setattr(adsb, "SOURCES", tuple(
        (name, "", "ac") for name in ("adsb.lol", "airplanes.live",
                                      "adsb.fi")))
    monkeypatch.setattr(adsb, "_fetch_source", fake)


def test_empty_first_source_is_not_the_final_word(monkeypatch):
    # the Lagos case: adsb.lol answers 200-with-nothing, another can see
    calls = []
    _install(monkeypatch, {"adsb.lol": [], "airplanes.live": ["a", "b"],
                           "adsb.fi": []}, calls)
    aircraft, source = fetch_point(6.577, 3.321, 250)
    assert source == "airplanes.live"
    assert len(aircraft) == 2
    assert set(calls) == {"adsb.lol", "airplanes.live", "adsb.fi"}


def test_all_empty_is_empty_sky_not_an_error(monkeypatch):
    _install(monkeypatch, {"adsb.lol": [], "airplanes.live": [],
                           "adsb.fi": []}, [])
    aircraft, source = fetch_point(0.0, -140.0, 250)
    assert aircraft == [] and source == "adsb.lol"


def test_all_failed_raises_the_last_error(monkeypatch):
    boom = RuntimeError("down")
    _install(monkeypatch, {"adsb.lol": boom, "airplanes.live": boom,
                           "adsb.fi": boom}, [])
    with pytest.raises(RuntimeError):
        fetch_point(51.47, -0.45, 250)


def test_winner_sticks_between_polls(monkeypatch):
    calls = []
    _install(monkeypatch, {"adsb.lol": ["a"], "airplanes.live": ["a", "b"],
                           "adsb.fi": []}, calls)
    picker = SourcePicker()
    assert picker.fetch(42.36, -71.01, 250)[1] == "airplanes.live"
    del calls[:]
    aircraft, source = picker.fetch(42.36, -71.01, 250)
    assert source == "airplanes.live"
    assert calls == ["airplanes.live"]   # no election, just the incumbent


def test_empty_incumbent_forces_a_revote_without_double_asking(monkeypatch):
    results = {"adsb.lol": [], "airplanes.live": ["a"], "adsb.fi": []}
    calls = []
    _install(monkeypatch, results, calls)
    picker = SourcePicker()
    picker.fetch(42.36, -71.01, 250)
    results["airplanes.live"], results["adsb.lol"] = [], ["a", "b"]
    del calls[:]
    aircraft, source = picker.fetch(42.36, -71.01, 250)
    assert source == "adsb.lol" and len(aircraft) == 2
    assert calls.count("airplanes.live") == 1   # polled once, not re-asked


def test_failing_incumbent_falls_back_same_poll(monkeypatch):
    results = {"adsb.lol": [], "airplanes.live": ["a"], "adsb.fi": []}
    calls = []
    _install(monkeypatch, results, calls)
    picker = SourcePicker()
    picker.fetch(42.36, -71.01, 250)
    results["airplanes.live"] = RuntimeError("429")
    results["adsb.fi"] = ["a", "b", "c"]
    aircraft, source = picker.fetch(42.36, -71.01, 250)
    assert source == "adsb.fi" and len(aircraft) == 3


def test_moving_the_view_revotes(monkeypatch):
    results = {"adsb.lol": ["a", "b"], "airplanes.live": ["a"],
               "adsb.fi": []}
    calls = []
    _install(monkeypatch, results, calls)
    picker = SourcePicker()
    picker.fetch(19.09, 72.87, 250)          # Mumbai: adsb.lol's turf
    results["adsb.lol"], results["airplanes.live"] = [], ["a", "b", "c"]
    del calls[:]
    aircraft, source = picker.fetch(6.577, 3.321, 250)   # Lagos
    assert source == "airplanes.live"
    assert set(calls) == {"adsb.lol", "airplanes.live", "adsb.fi"}


def test_small_pan_does_not_revote(monkeypatch):
    calls = []
    _install(monkeypatch, {"adsb.lol": ["a"], "airplanes.live": [],
                           "adsb.fi": []}, calls)
    picker = SourcePicker()
    picker.fetch(42.36, -71.01, 250)
    del calls[:]
    picker.fetch(42.50, -71.20, 250)         # a nudge, well inside range
    assert calls == ["adsb.lol"]


def test_stale_vote_ages_out(monkeypatch):
    calls = []
    _install(monkeypatch, {"adsb.lol": ["a"], "airplanes.live": [],
                           "adsb.fi": []}, calls)
    picker = SourcePicker()
    picker.fetch(42.36, -71.01, 250)
    picker._voted -= SourcePicker.REELECT_S + 1
    del calls[:]
    picker.fetch(42.36, -71.01, 250)
    assert set(calls) == {"adsb.lol", "airplanes.live", "adsb.fi"}


def test_election_asks_side_by_side(monkeypatch):
    # a slow host costs its own wait, not everyone's in turn
    import time as _time

    def slow(source, lat, lon, radius_nm, timeout):
        _time.sleep(0.3)
        return ["a"] if source[0] == "adsb.fi" else []

    monkeypatch.setattr(adsb, "SOURCES", tuple(
        (name, "", "ac") for name in ("adsb.lol", "airplanes.live",
                                      "adsb.fi")))
    monkeypatch.setattr(adsb, "_fetch_source", slow)
    started = _time.monotonic()
    aircraft, source = fetch_point(6.577, 3.321, 250)
    assert source == "adsb.fi"
    assert _time.monotonic() - started < 0.8


def test_normalize_keeps_what_the_card_shows():
    ac = adsb._normalize({
        "hex": "abc123", "flight": "DAL1673 ", "r": "N123DL", "t": "B739",
        "lat": 40.6, "lon": -73.8, "alt_baro": 31000, "gs": 450,
        "track": 200, "category": "A3", "nav_altitude_mcp": 35000,
        "dbFlags": 1, "type": "mlat", "wd": 270, "ws": 85, "oat": -52,
    }, 1000.0)
    assert ac["flight"] == "DAL1673" and ac["category"] == "A3"
    assert ac["sel_alt"] == 35000 and ac["mil"] and ac["mlat"]
    assert ac["wind"] == (270, 85) and ac["oat"] == -52


def test_lookup_falls_back_to_the_next_host(monkeypatch):
    seen = []

    def fake_fetch(url, headers=None, timeout=10):
        seen.append(url)
        if "adsb.lol" in url:
            raise RuntimeError("down")
        return {"aircraft": [{"hex": "a1", "flight": "SWA938", "lat": 1,
                              "lon": 2, "alt_baro": 5000}]}

    monkeypatch.setattr(adsb, "fetch_json", fake_fetch)
    found, source = adsb.lookup("callsign", "swa938")
    assert source == "adsb.fi" and found[0]["callsign"] == "SWA938"
    assert seen[0].endswith("/callsign/SWA938")
