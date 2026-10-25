"""What an airport is doing right now, read off the live sky.

The airport card asks three things:

  - the weather: the latest METAR from the Aviation Weather Center
    (worldwide, keyless), fetched on a background thread and kept ten
    minutes — ``Metars``;
  - the board: arrivals and departures, from the routes the scope has
    looked up for traffic in view — ``board``;
  - the runways in use, inferred the way a spotter would: who is lined up
    on a final, descending toward a threshold, and who is climbing out
    along a runway's heading — ``runways_in_use``.  ADS-B carries no
    runway, so this is geometry, and the card says "seen" rather than
    pretending to know the ATIS.
"""

import math
import threading
import time

from blips import USER_AGENT
from blips._geo import bearing_to, haversine_nm
from blips._http import fetch_json
from blips._runtime import debug_log

METAR_URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=json"
METAR_TTL_S = 600.0
METAR_RETRY_S = 120.0


class Metars:
    """METARs by ICAO code, fetched in the background.  ``get`` never
    blocks: it answers what's on hand (or None) and asks for the rest;
    ``on_update`` is called when a report lands."""

    def __init__(self, on_update=None, fetch=fetch_json):
        self._on_update = on_update
        self._fetch = fetch
        self._have = {}       # ICAO → (stamp, report dict or None)
        self._asking = set()
        self._lock = threading.Lock()

    def get(self, icao):
        icao = (icao or "").upper()
        if not icao:
            return None
        now = time.time()
        with self._lock:
            hit = self._have.get(icao)
            fresh = hit is not None and now - hit[0] < (
                METAR_TTL_S if hit[1] else METAR_RETRY_S)
            if fresh or icao in self._asking:
                return hit[1] if hit else None
            self._asking.add(icao)
        threading.Thread(target=self._run, args=(icao,), daemon=True,
                         name="metar").start()
        return hit[1] if hit else None

    def _run(self, icao):
        report = None
        try:
            data = self._fetch(METAR_URL.format(ids=icao),
                               headers={"User-Agent": USER_AGENT}, timeout=8)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                report = data[0]
        except Exception as exc:
            debug_log(f"metar for {icao} failed: {exc}")
        with self._lock:
            self._asking.discard(icao)
            self._have[icao] = (time.time(), report)
        if self._on_update is not None:
            self._on_update()


def metar_summary(report):
    """A METAR dict → a short line: '270° 28G40 kt · 6+ mi · 14°C · VFR'."""
    if not report:
        return ""
    bits = []
    wdir, wspd, wgst = report.get("wdir"), report.get("wspd"), report.get("wgst")
    if isinstance(wspd, (int, float)):
        if wspd == 0:
            bits.append("calm")
        else:
            d = f"{wdir:03d}°" if isinstance(wdir, int) else "variable"
            g = f"G{wgst}" if isinstance(wgst, (int, float)) and wgst else ""
            bits.append(f"wind {d} {wspd}{g} kt")
    vis = report.get("visib")
    if vis not in (None, ""):
        bits.append(f"vis {vis} mi")
    clouds = report.get("clouds") or []
    if clouds:
        low = clouds[0]
        if isinstance(low, dict) and low.get("cover"):
            base = low.get("base")
            bits.append(f"{low['cover']}" + (f" {base:,} ft" if isinstance(base, int) else ""))
    temp = report.get("temp")
    if isinstance(temp, (int, float)):
        bits.append(f"{temp:.0f}°C")
    cat = report.get("fltCat")
    if cat:
        bits.append(cat)
    return " · ".join(bits)


def _codes(ap):
    return {c for c in (ap.get("icao"), ap.get("iata")) if c}


def _leg_codes(leg):
    return {c for c in (leg[1], getattr(leg, "icao", ""),
                        getattr(leg, "iata", "")) if c}


def board(ap, aircraft, routes, max_nm=400.0):
    """(arrivals, departures) at ``ap`` among ``aircraft``.

    Each entry is (ac, other_leg, dist_nm, eta_min or None): arrivals are
    bound here by their looked-up route and still airborne, nearest first
    with an ETA at their ground speed; departures left from here, are
    within ``max_nm``, and are on the ground or climbing away, nearest
    first.  ``routes`` maps callsign → route.
    """
    codes = _codes(ap)
    arrivals, departures = [], []
    for ac in aircraft:
        route = routes.get((ac.get("flight") or ac["callsign"]).upper())
        if not route:
            continue
        d = haversine_nm(ap["lat"], ap["lon"], ac["lat"], ac["lon"])
        inbound = bool(_leg_codes(route[-1]) & codes)
        outbound = bool(_leg_codes(route[0]) & codes)
        if inbound and outbound:
            # a round trip on file (a training flight, a diversion's
            # return): which way it's going is which way it's climbing
            inbound = (ac.get("vrate") or 0) < 300 and not ac.get("ground")
            outbound = not inbound
        if inbound and not ac.get("ground"):
            gs = ac.get("gs") or 0
            eta = d / gs * 60.0 if gs > 60 else None
            arrivals.append((ac, route[0], d, eta))
        elif outbound and d <= max_nm:
            departures.append((ac, route[-1], d, None))
    arrivals.sort(key=lambda e: e[2])
    departures.sort(key=lambda e: e[2])
    return arrivals, departures


def _cross_along(lat0, lon0, hdg, lat, lon):
    """(along, cross) nm of a point from a threshold along a heading —
    along positive ahead of the threshold, cross positive to the right."""
    d = haversine_nm(lat0, lon0, lat, lon)
    if d < 1e-6:
        return 0.0, 0.0
    rel = math.radians((bearing_to(lat0, lon0, lat, lon) - hdg + 540) % 360 - 180)
    return d * math.cos(rel), d * math.sin(rel)


def _angle(a, b):
    return abs((a - b + 180) % 360 - 180)


def runways_in_use(ap, aircraft):
    """({ident: landings}, {ident: departures}) seen in ``aircraft``.

    A landing is an aircraft within 12 nm of a threshold, near its
    extended centreline, below 4,500 ft above the field, tracking within
    20° of the runway's heading and not yet past it.  A departure is one
    past the far end of a runway, still low and climbing, tracking within
    20° of its heading and within 8 nm.  The centreline corridor narrows
    toward the runway (0.3 nm wide at the threshold, about a mile at ten
    out), and each aircraft counts for the best-aligned runway only, so
    close parallels — Heathrow's are three quarters of a mile apart —
    are told apart.
    """
    elev = ap.get("elev") or 0
    landing, departing = {}, {}
    ends = []
    for rwy in ap.get("rwys") or ():
        le, he = rwy.get("le"), rwy.get("he")
        if not (le and he) or None in (le[2], le[3], he[2], he[3]):
            continue
        # each end: (ident, heading, threshold lat/lon, far end lat/lon)
        ends.append((le[0], le[1], le[2], le[3], he[2], he[3]))
        ends.append((he[0], he[1], he[2], he[3], le[2], le[3]))
    for ac in aircraft:
        if ac.get("ground") or ac.get("track") is None or ac.get("alt") is None:
            continue
        agl = ac["alt"] - elev
        if agl > 6000:
            continue
        vr = ac.get("vrate") or 0
        best = None   # (|cross|, kind, ident)
        for ident, hdg, tlat, tlon, flat, flon in ends:
            if hdg is None or _angle(ac["track"], hdg) > 20:
                continue
            along, cross = _cross_along(tlat, tlon, hdg, ac["lat"], ac["lon"])
            if (-12.0 <= along <= 0.3 and agl < 4500 and vr <= 300
                    and abs(cross) <= 0.3 + 0.08 * abs(along)):
                cand = (abs(cross), "land", ident)
                best = cand if best is None or cand < best else best
                continue
            past, pcross = _cross_along(flat, flon, hdg, ac["lat"], ac["lon"])
            if (0.0 <= past <= 8.0 and vr > 300
                    and abs(pcross) <= 0.4 + 0.15 * past):
                cand = (abs(pcross), "dep", ident)
                best = cand if best is None or cand < best else best
        if best is not None:
            tally = landing if best[1] == "land" else departing
            tally[best[2]] = tally.get(best[2], 0) + 1
    return landing, departing
