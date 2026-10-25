"""The live scope's app: keys, clicks and search against a fake sky.

No network: the feed is filled by hand, worldwide lookups and METARs are
faked, and the terminal is a fixed 120 x 40.
"""

import os
import re
import time

import pytest

import blips._app as app_mod
import blips._routes as routes_mod
import blips.scope as scope


def _plain(s):
    return re.sub(r"\033\[[0-9;?]*[a-zA-Z]", "", s)


@pytest.fixture
def app(monkeypatch, tmp_path):
    size = os.terminal_size((120, 40))
    monkeypatch.setattr(scope, "get_terminal_size", lambda *a: size)
    monkeypatch.setattr(app_mod, "get_terminal_size", lambda *a: size)
    monkeypatch.setattr(routes_mod, "DISK", tmp_path / "routes.json")
    monkeypatch.setattr(routes_mod.RouteLookup, "_start", lambda self: None)
    monkeypatch.setattr("blips._adsb.lookup", lambda kind, value: ([], "x"))
    monkeypatch.setattr(app_mod.Metars, "get", lambda self, icao: None)
    a = app_mod.ScopeApp(40.64, -73.78, 2.0, weather_on=False)
    a.search._lookup = lambda kind, value: ([], "fake")
    now = time.time()
    a.feed.aircraft = [
        {"hex": "aaa111", "callsign": "DAL1673", "flight": "DAL1673",
         "reg": "N812DN", "actype": "B739", "lat": 40.64, "lon": -73.60,
         "alt": 9000, "ground": False, "gs": 280, "track": 90,
         "vrate": 1500, "squawk": "1234", "emergency": False,
         "fix_time": now, "category": "A3", "mil": False},
        {"hex": "bbb222", "callsign": "JBU109", "flight": "JBU109",
         "reg": "N123JB", "actype": "A320", "lat": 40.80, "lon": -73.95,
         "alt": 5000, "ground": False, "gs": 230, "track": 45,
         "vrate": 0, "squawk": "", "emergency": False,
         "fix_time": now, "category": "A3", "mil": False},
    ]
    a.feed.updated = now
    a.feed.source = "fake"
    return a


def _render(app):
    return app.render(mouse_pos=None, playing=True)


def _settle(app):
    for _ in range(40):
        app.camera.step()
        if not app.camera.moving:
            break
        time.sleep(0.02)


def test_keys_zoom_and_pan(app):
    app.on_action("+")
    assert app.camera.target[2] < 2.0
    lat0 = app.camera.target[0]
    app.on_action("up")
    _settle(app)
    assert app.camera.lat > lat0


def test_click_selects_then_follows_then_escape(app):
    _render(app)
    hit = next(h for h in scope._last_frame["hits"] if h[3] == "aaa111")
    col, row = hit[0] + 1, hit[1] + 2           # cell → 1-based screen
    assert app.on_click(col, row)
    assert app.selected == "aaa111" and not app.follow
    frame = _plain(_render(app))
    assert "Delta Air Lines" in frame and "Boeing 737-900" in frame
    assert app.on_click(col, row)               # again: follow
    assert app.follow
    assert app.on_action("escape")
    assert app.selected is None and not app.follow


def test_tab_cycles_from_the_centre(app):
    _render(app)
    assert app.on_action("tab")
    first = app.selected
    assert app.on_action("tab")
    assert app.selected != first


def test_search_typing_and_choosing_an_airport(app):
    assert app.on_action("/")
    assert app.text_mode()
    for ch in "jfk":
        assert app.intercept(f"char:{ch}")
    frame = _plain(_render(app))
    assert "John F. Kennedy International" in frame
    assert app.intercept("key:enter")
    assert not app.search_open
    assert app.airport and app.airport["icao"] == "KJFK"


def test_search_finds_a_flight_on_screen(app):
    app.on_action("/")
    for ch in "dl1673":
        app.intercept(f"char:{ch}")
    _render(app)
    assert app.results[0].kind == "flight"
    app.intercept("key:enter")
    assert app.selected == "aaa111" and app.follow


def test_filter_from_search_and_clear(app):
    app.on_action("/")
    for ch in "jetblue":
        app.intercept(f"char:{ch}")
    _render(app)
    i = next(i for i, r in enumerate(app.results) if r.kind == "filter")
    app.search.cursor = i
    app.intercept("key:enter")
    frame = _plain(_render(app))
    assert "✈ 1 of 2" in frame and "JetBlue" in frame
    app.on_action("x")
    assert not app.filters


def test_help_opens_and_any_key_closes(app):
    app.on_action("?")
    assert "keys" in _plain(_render(app))
    assert app.intercept("escape")
    assert not app.help


def test_wheel_zooms_about_the_pointer(app):
    _render(app)
    assert app.on_wheel(1, 20, 10)
    assert app.camera.target[2] < 2.0
