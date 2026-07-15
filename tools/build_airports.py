#!/usr/bin/env python3
"""Build src/blips/data/airports.json.gz from OurAirports CSVs.

Source data is public domain: https://ourairports.com/data/
    airports.csv, runways.csv
      via https://davidmegginson.github.io/ourairports-data/

Keeps medium and large airports that have at least one open, reasonably
long runway, with per-end idents and true headings (taken from the data
when present, computed from the threshold coordinates when not, and
falling back to ident-number × 10 as a last resort — close enough for
the game's geometry).

Usage: python tools/build_airports.py airports.csv runways.csv
"""

import csv
import gzip
import json
import math
import sys
from collections import defaultdict

MIN_RUNWAY_FT = 4500  # long enough for the jets the sim flies


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(dlon))
    return math.degrees(math.atan2(y, x)) % 360.0


def _ident_heading(ident):
    """'19R' → 190.0, or None if the ident isn't runway-number shaped."""
    digits = "".join(c for c in ident if c.isdigit())
    if not digits:
        return None
    n = int(digits)
    return float(n % 36 or 36) * 10.0


def _end(ident, hdg, lat, lon, other_lat, other_lon):
    """One runway end: [ident, true heading, threshold lat, lon]."""
    if hdg is None and None not in (lat, lon, other_lat, other_lon):
        hdg = _bearing(lat, lon, other_lat, other_lon)
    if hdg is None:
        hdg = _ident_heading(ident)
    if hdg is None:
        return None
    return [ident, round(hdg, 1),
            None if lat is None else round(lat, 5),
            None if lon is None else round(lon, 5)]


def main(airports_csv, runways_csv):
    runways = defaultdict(list)
    with open(runways_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["closed"] == "1":
                continue
            length = _f(row["length_ft"])
            le, he = row["le_ident"].strip(), row["he_ident"].strip()
            if not length or length < MIN_RUNWAY_FT or not le or not he:
                continue
            if le.startswith("H") or "W" in (le + he):  # helipads, water
                continue
            le_lat, le_lon = _f(row["le_latitude_deg"]), _f(row["le_longitude_deg"])
            he_lat, he_lon = _f(row["he_latitude_deg"]), _f(row["he_longitude_deg"])
            a = _end(le, _f(row["le_heading_degT"]), le_lat, le_lon, he_lat, he_lon)
            b = _end(he, _f(row["he_heading_degT"]), he_lat, he_lon, le_lat, le_lon)
            if a is None or b is None:
                continue
            runways[row["airport_ident"]].append(
                {"len": int(length), "le": a, "he": b})

    airports = []
    with open(airports_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["type"] not in ("large_airport", "medium_airport"):
                continue
            rwys = runways.get(row["ident"])
            if not rwys:
                continue
            lat, lon = _f(row["latitude_deg"]), _f(row["longitude_deg"])
            if lat is None or lon is None:
                continue
            airports.append({
                "icao": row["ident"],
                "iata": row["iata_code"] or "",
                "name": row["name"],
                "city": row["municipality"] or "",
                "country": row["iso_country"],
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "elev": int(_f(row["elevation_ft"]) or 0),
                "large": row["type"] == "large_airport",
                "rwys": sorted(rwys, key=lambda r: -r["len"]),
            })

    airports.sort(key=lambda a: (not a["large"], a["icao"]))
    out = "src/blips/data/airports.json.gz"
    payload = json.dumps({"airports": airports}, separators=(",", ":"))
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(payload)
    large = sum(1 for a in airports if a["large"])
    print(f"{len(airports)} airports ({large} large), "
          f"{sum(len(a['rwys']) for a in airports)} runways → {out}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
