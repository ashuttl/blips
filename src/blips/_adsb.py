"""ADS-B feed client.

Fetches live aircraft near a point from community aggregators — adsb.lol,
airplanes.live, adsb.fi.  All serve the same readsb per-aircraft schema
(URL shape and list key differ; smoothed over in SOURCES):

    GET /v2/point/{lat}/{lon}/{radius_nm}  →  {"ac": [...], ...}

The aggregators drink from overlapping but distinct slices of the same
volunteer receiver network, so coverage is regional, not ranked: one has
the best ears over Mumbai while another is the only one that can see
Lagos at all — and a coverage hole comes back as HTTP 200 with an empty
list, indistinguishable from genuinely empty sky.  So instead of a fixed
pecking order there's an election: ask everyone once, keep whoever sees
the most traffic *here*, and poll only the winner until the view moves,
the winner errors or goes empty, or the vote ages out.

Field quirks handled here so the renderer never sees them:
  - alt_baro is the string "ground" for surface traffic (alt_geom may still
    be numeric); we expose a clean `ground` flag and numeric `alt` or None.
  - `flight` (callsign) is sometimes absent/blank → registration → hex.
  - `track` is usually absent on the ground → None (no heading known).
  - vertical rate arrives as baro_rate or geom_rate depending on equipage.

Every aggregator asks for at most 1 request/second; our poll cadence is far
below that, and an election is one request to each of three different
hosts.  Radius is capped at 250 nm, the aggregators' documented max.
"""

import time

from blips import USER_AGENT
from blips._geo import haversine_nm
from blips._http import fetch_json
from blips._runtime import debug_log

# name, URL template, key holding the aircraft list
SOURCES = (
    ("adsb.lol",
     "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{r:.0f}", "ac"),
    ("airplanes.live",
     "https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{r:.0f}",
     "ac"),
    ("adsb.fi",
     "https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}"
     "/dist/{r:.0f}", "aircraft"),
)
MAX_RADIUS_NM = 250


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _normalize(raw, fetched_at):
    """One aggregator aircraft record → the dict the scope renders.

    Returns None for records without a position.
    """
    lat, lon = _num(raw.get("lat")), _num(raw.get("lon"))
    if lat is None or lon is None:
        return None
    alt_baro = raw.get("alt_baro")
    ground = alt_baro == "ground"
    alt = _num(alt_baro)
    if alt is None:
        alt = _num(raw.get("alt_geom"))
    callsign = (raw.get("flight") or "").strip()
    reg = (raw.get("r") or "").strip()
    hexid = (raw.get("hex") or "").strip()
    # seen_pos is how stale this position already was at fetch time
    fix_time = fetched_at - min(_num(raw.get("seen_pos")) or 0.0, 60.0)
    return {
        "hex": hexid,
        "callsign": callsign or reg or hexid.upper(),
        "reg": reg,
        "actype": (raw.get("t") or "").strip(),
        "lat": lat,
        "lon": lon,
        "alt": None if ground else alt,
        "ground": ground,
        "gs": _num(raw.get("gs")),
        "track": _num(raw.get("track")),
        "vrate": _num(raw.get("baro_rate")) or _num(raw.get("geom_rate")),
        "squawk": raw.get("squawk") or "",
        "emergency": raw.get("emergency") not in (None, "", "none"),
        "fix_time": fix_time,
    }


def _fetch_source(source, lat, lon, radius_nm, timeout):
    """One aggregator, one request → normalized aircraft (may be empty)."""
    _name, tmpl, key = source
    r = max(1, min(MAX_RADIUS_NM, radius_nm))
    data = fetch_json(tmpl.format(lat=lat, lon=lon, r=r),
                      headers={"User-Agent": USER_AGENT}, timeout=timeout)
    fetched_at = time.time()
    aircraft = []
    for raw in data.get(key) or []:
        ac = _normalize(raw, fetched_at)
        if ac is not None:
            aircraft.append(ac)
    return aircraft


def _election(lat, lon, radius_nm, timeout, on_status, have=None):
    """Ask every aggregator once → ({name: aircraft|None}, last_exc).

    ``have`` carries this round's already-fetched results so nobody is
    asked twice: a name mapped to a list was polled moments ago, a name
    mapped to None just errored and doesn't get an instant retry.
    """
    have = dict(have or {})
    last_exc = None
    for source in SOURCES:
        name = source[0]
        if name in have:
            continue
        if on_status is not None:
            on_status(name)
        try:
            have[name] = _fetch_source(source, lat, lon, radius_nm, timeout)
        except Exception as exc:
            debug_log(f"{name} fetch failed: {exc}")
            have[name] = None
            last_exc = exc
    return have, last_exc


def _winner(returns):
    """The aggregator that saw the most traffic; ties keep SOURCES order."""
    best = None
    for source in SOURCES:
        aircraft = returns.get(source[0])
        if aircraft is None:
            continue
        if best is None or len(aircraft) > len(returns[best]):
            best = source[0]
    return best


def fetch_point(lat, lon, radius_nm, timeout=8, on_status=None):
    """Aircraft near a point, one-shot: (normalized list, source name).

    Holds a fresh election every call — right for a single sample like
    the game's traffic pool; a steady poller should keep a SourcePicker
    so the winner is remembered between polls.  ``on_status(name)`` is
    called before each attempt so a UI can show which aggregator is
    being waited on.  Raises only if every aggregator failed.
    """
    returns, last_exc = _election(lat, lon, radius_nm, timeout, on_status)
    best = _winner(returns)
    if best is None:
        raise last_exc if last_exc else RuntimeError("no ADS-B sources")
    return returns[best], best


class SourcePicker:
    """Sticky aggregator choice for a polling feed.

    "Best" is a property of the view, not the provider, so the picker
    holds an election on the first poll and then polls only the winner.
    It re-votes when the view moves meaningfully, when the winner errors
    or comes back empty (an empty list is a coverage hole until every
    aggregator agrees the sky is empty), or REELECT_S after the last
    vote — feeders come and go.
    """

    REELECT_S = 900.0

    def __init__(self):
        self._view = None       # (lat, lon, radius_nm) at the last vote
        self._name = None
        self._voted = 0.0

    def _view_changed(self, lat, lon, radius_nm):
        if self._view is None:
            return True
        plat, plon, pr = self._view
        return (abs(radius_nm - pr) > pr * 0.3
                or haversine_nm(lat, lon, plat, plon) > pr / 3.0)

    def fetch(self, lat, lon, radius_nm, timeout=8, on_status=None):
        """(normalized aircraft, source name) for the current view."""
        have = {}
        if (self._name is not None
                and not self._view_changed(lat, lon, radius_nm)
                and time.time() - self._voted < self.REELECT_S):
            source = next(s for s in SOURCES if s[0] == self._name)
            if on_status is not None:
                on_status(self._name)
            try:
                aircraft = _fetch_source(source, lat, lon, radius_nm,
                                         timeout)
            except Exception as exc:
                debug_log(f"{self._name} fetch failed: {exc}")
                aircraft = None
            if aircraft:
                return aircraft, self._name
            have[self._name] = aircraft   # [] or None: the seat is contested
        returns, last_exc = _election(lat, lon, radius_nm, timeout,
                                      on_status, have=have)
        best = _winner(returns)
        if best is None:
            raise last_exc if last_exc else RuntimeError("no ADS-B sources")
        if best != self._name:
            debug_log(f"source election: {best} "
                      f"({len(returns[best])} aircraft) takes the view")
        self._view = (lat, lon, radius_nm)
        self._name = best
        self._voted = time.time()
        return returns[best], best
