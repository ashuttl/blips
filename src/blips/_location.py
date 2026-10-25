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


_REGIONS = {
    # US states and Canadian provinces by postal abbreviation, so
    # "portland me" and "london on" read the way people type them
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "ON": "Ontario", "PE": "Prince Edward Island",
    "QC": "Quebec", "SK": "Saskatchewan",
}


def _search(name, count=10):
    url = ("https://geocoding-api.open-meteo.com/v1/search"
           f"?name={quote(name)}&count={count}&format=json")
    try:
        data = fetch_json(url, headers={"User-Agent": USER_AGENT}, timeout=6)
    except Exception as exc:
        debug_log(f"geocoding failed: {exc}")
        return None
    return data.get("results") or []


def _best(hits):
    # this is an air-traffic scope: an airport beats a namesake town
    # ("heathrow" must not mean Heathrow, Florida), then population decides
    return min(enumerate(hits),
               key=lambda ih: (ih[1].get("feature_code") != "AIRP",
                               -(ih[1].get("population") or 0), ih[0]))[1]


def _qualifies(hit, qualifier):
    """Whether a geocoder hit is in the region a typed qualifier names:
    a state, province or country, by name or code ("maine", "me", "uk")."""
    q = qualifier.strip().lower().rstrip(".")
    region = _REGIONS.get(q.upper(), "").lower()
    fields = [str(hit.get(k) or "").lower()
              for k in ("admin1", "admin2", "country", "country_code")]
    if q in ("uk", "britain", "great britain", "england"):
        fields.append("gb" if hit.get("country_code") == "GB" else "")
        q = "gb"
    return any(f and (f == q or f == region or f.startswith(q))
               for f in fields)


def geocode_place(query):
    """(lat, lng, display_name) for a place name via Open-Meteo, or None.

    Open-Meteo matches a bare place name, so "portland maine" or
    "portland, me" finds nothing as typed: when the whole query misses,
    it is split into a name and a qualifier (the words after a comma, or
    the last word or two) and the name's hits are kept to the region the
    qualifier names.  Last, the vendored airports answer for an airport's
    own name or code ("logan", "kpwm").
    """
    hits = _search(query, count=5)
    if hits:
        hit = _best(hits)
        return hit["latitude"], hit["longitude"], hit.get("name", query)
    words = query.replace(",", " , ").split()
    splits = []
    if "," in words:
        i = words.index(",")
        splits.append((" ".join(words[:i]), " ".join(w for w in words[i + 1:]
                                                     if w != ",")))
    for k in (1, 2):
        if len(words) > k and "," not in words[-k:]:
            splits.append((" ".join(w for w in words[:-k] if w != ","),
                           " ".join(words[-k:])))
    for name, qualifier in splits:
        if not name or not qualifier:
            continue
        found = [h for h in (_search(name, count=20) or ())
                 if _qualifies(h, qualifier)]
        if found:
            hit = _best(found)
            where = hit.get("admin1") or hit.get("country") or ""
            label = f"{hit.get('name', name)}, {where}".strip(", ")
            return hit["latitude"], hit["longitude"], label
    from blips._airports import find_airport
    ap = find_airport(query)
    if ap is not None:
        return ap["lat"], ap["lon"], ap["name"]
    return None
