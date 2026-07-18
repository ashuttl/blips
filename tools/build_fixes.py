#!/usr/bin/env python3
"""Build src/blips/data/fixes.json.gz from the FAA CIFP (ARINC 424).

Source data is public domain: the FAA's Coded Instrument Flight Procedures
package, published on a 28-day cycle at
https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/
(the ``FAACIFP18`` file inside ``CIFP_YYMMDD.zip``).  Refresh every few
years — same vendoring pattern as airports.json.gz / navaids.json.gz.

Keeps every named waypoint with a position: the enroute waypoints (section
``EA``) that STARs and airways feed through, and the airport terminal
waypoints (subsection ``PC``) that hang off approaches and departures.
The game uses them as real sector gates — a real fix name in a thin octant
beats an invented one — and, later, to resolve the fixes a named procedure
strings together.  US-first, the way the source is: coverage is the FAA's
(CONUS, Alaska, Pacific, and the bordering regions the CIFP carries).

ARINC 424-18 is fixed width (132 columns).  For both record kinds the
five-character ident sits at columns 14-18 and the geographic position at
columns 33-52, encoded ``Hdddmmssss`` — hemisphere, then degrees / minutes
/ seconds / hundredths of a second (latitude uses two degree digits, longi-
tude three).

Usage: python tools/build_fixes.py FAACIFP18
"""

import gzip
import json
import sys


def _dms(field, deg_digits):
    """ARINC packed coordinate → signed decimal degrees, or None."""
    hemi = field[0]
    body = field[1:]
    try:
        deg = int(body[:deg_digits])
        rest = body[deg_digits:]
        mn, sec, hun = int(rest[0:2]), int(rest[2:4]), int(rest[4:6])
    except ValueError:
        return None
    val = deg + mn / 60.0 + (sec + hun / 100.0) / 3600.0
    return -val if hemi in "SW" else val


def main(cifp_file):
    seen = set()
    out = []
    with open(cifp_file, encoding="latin-1") as fh:
        for line in fh:
            section, subsection = line[4:6], line[12:13]
            if section == "EA":                     # enroute waypoint
                pass
            elif line[4] == "P" and subsection == "C":   # terminal waypoint
                pass
            else:
                continue
            ident = line[13:18].strip().upper()
            if not ident or ident in seen:
                continue
            lat = _dms(line[32:41], 2)
            lon = _dms(line[41:51], 3)
            if lat is None or lon is None:
                continue
            seen.add(ident)
            out.append({"id": ident,
                        "lat": round(lat, 5), "lon": round(lon, 5)})
    out.sort(key=lambda f: f["id"])
    path = "src/blips/data/fixes.json.gz"
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps({"fixes": out}, separators=(",", ":")))
    print(f"{len(out)} fixes → {path}")


if __name__ == "__main__":
    main(sys.argv[1])
