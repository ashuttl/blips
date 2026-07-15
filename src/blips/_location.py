"""IP geolocation (cached) and place-name geocoding."""

from urllib.parse import quote

from blips import USER_AGENT
from blips._cache import CACHE_ROOT, read_cache, write_cache
from blips._http import fetch_json
from blips._runtime import debug_log

_CACHE_FILE = CACHE_ROOT / "location.json"
_MAX_AGE = 3600  # 1 hour; implicit IP geolocation should refresh as users move


def get_location():
    """(lat, lng) from cache or IP geolocation; (None, None) on failure."""
    cached = read_cache(_CACHE_FILE, _MAX_AGE)
    if cached is not None:
        try:
            return cached["lat"], cached["lng"]
        except KeyError:
            pass

    try:
        data = fetch_json(
            "https://ipinfo.io/json",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=3,
        )
        parts = data.get("loc", "").split(",")
        if len(parts) == 2:
            lat, lng = float(parts[0]), float(parts[1])
            write_cache(_CACHE_FILE, {"lat": lat, "lng": lng})
            return lat, lng
    except Exception as exc:
        debug_log(f"geolocation failed: {exc}")

    return None, None


def geocode_place(query):
    """(lat, lng, display_name) for a place name via Open-Meteo, or None."""
    url = ("https://geocoding-api.open-meteo.com/v1/search"
           f"?name={quote(query)}&count=5&format=json")
    try:
        data = fetch_json(url, headers={"User-Agent": USER_AGENT}, timeout=6)
    except Exception as exc:
        debug_log(f"geocoding failed: {exc}")
        return None
    hits = data.get("results") or []
    if not hits:
        return None
    # this is an air-traffic scope: an airport beats a namesake town
    # ("heathrow" must not mean Heathrow, Florida), then population decides
    hit = min(enumerate(hits),
              key=lambda ih: (ih[1].get("feature_code") != "AIRP",
                              -(ih[1].get("population") or 0), ih[0]))[1]
    return hit["latitude"], hit["longitude"], hit.get("name", query)
