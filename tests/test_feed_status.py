"""Feed fetch-status plumbing and the header states it drives."""

import re
import time

from blips.scope import Feed, render_scope


def _plain(ansi):
    return re.sub(r"\033\[[0-9;]*m", "", ansi)


def _header(feed):
    frame = render_scope([51.47, -0.45], 4.0, feed, playing=True)
    return _plain(frame.split("\n")[0])


def _footer(feed):
    frame = render_scope([51.47, -0.45], 4.0, feed, playing=True)
    return _plain(frame.split("\n")[-1])


def test_first_fetch_shows_progress_not_zero_flights():
    feed = Feed()
    feed._fetching = True
    feed._fetch_started = time.time() - 9.0
    feed._trying = "adsb.lol"
    header = _header(feed)
    assert "fetching adsb.lol" in header
    assert "9s" in header
    assert "✈ …" in header
    assert "✈ 0" not in header
    assert "Contacting adsb.lol" in _footer(feed)


def test_stalled_poll_flagged_once_data_exists():
    feed = Feed()
    feed.updated = time.time() - 12.0
    feed.source = "adsb.lol"
    feed._fetching = True
    feed._fetch_started = time.time() - 8.0
    feed._trying = "airplanes.live"
    assert "still fetching" in _header(feed)


def test_healthy_feed_keeps_plain_age_status():
    feed = Feed()
    feed.updated = time.time() - 3.0
    feed.source = "adsb.lol"
    header = _header(feed)
    assert "↺ 3s" in header
    assert "fetching" not in header
    assert "Live ADS-B by adsb.lol" in _footer(feed)


def test_fetch_status_resets_after_poll(monkeypatch):
    seen = {}

    def fake_fetch_point(lat, lon, radius, on_status=None):
        on_status("adsb.lol")
        seen["mid"] = feed.fetch_status()
        return [], "adsb.lol"

    monkeypatch.setattr("blips.scope.fetch_point", fake_fetch_point)
    feed = Feed()
    feed.set_view(51.47, -0.45, 100)
    feed.poll_once()
    fetching, started, trying = seen["mid"]
    assert fetching and started is not None and trying == "adsb.lol"
    fetching, _started, trying = feed.fetch_status()
    assert not fetching and trying == ""
    assert feed.updated is not None
