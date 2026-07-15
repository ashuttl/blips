"""ADS-B feed client.

Fetches live aircraft near a point from community aggregators — adsb.lol
first, airplanes.live as fallback.  Both serve the same readsb JSON schema:

    GET /v2/point/{lat}/{lon}/{radius_nm}  →  {"ac": [...], "now": ms, ...}

Field quirks handled here so the renderer never sees them:
  - alt_baro is the string "ground" for surface traffic (alt_geom may still
    be numeric); we expose a clean `ground` flag and numeric `alt` or None.
  - `flight` (callsign) is sometimes absent/blank → registration → hex.
  - `track` is usually absent on the ground → None (no heading known).
  - vertical rate arrives as baro_rate or geom_rate depending on equipage.

airplanes.live asks for at most 1 request/second; our poll cadence is far
below that.  Radius is capped at 250 nm, the aggregators' documented max.
"""

import time

from blips import USER_AGENT
from blips._http import fetch_json
from blips._runtime import debug_log

SOURCES = (
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{r:.0f}"),
    ("airplanes.live",
     "https://api.airplanes.live/v2/point/{lat:.4f}/{lon:.4f}/{r:.0f}"),
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


def fetch_point(lat, lon, radius_nm, timeout=8):
    """Aircraft near a point: (list of normalized dicts, source name).

    Tries each aggregator in order; raises the last error if all fail.
    """
    r = max(1, min(MAX_RADIUS_NM, radius_nm))
    last_exc = None
    for name, tmpl in SOURCES:
        url = tmpl.format(lat=lat, lon=lon, r=r)
        try:
            data = fetch_json(url, headers={"User-Agent": USER_AGENT},
                              timeout=timeout)
        except Exception as exc:
            debug_log(f"{name} fetch failed: {exc}")
            last_exc = exc
            continue
        fetched_at = time.time()
        aircraft = []
        for raw in data.get("ac") or []:
            ac = _normalize(raw, fetched_at)
            if ac is not None:
                aircraft.append(ac)
        return aircraft, name
    raise last_exc if last_exc else RuntimeError("no ADS-B sources")
