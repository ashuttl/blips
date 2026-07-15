#!/usr/bin/env python3
"""Build src/blips/data/navaids.json.gz from the OurAirports CSV.

Source data is public domain: https://ourairports.com/data/ (navaids.csv
via https://davidmegginson.github.io/ourairports-data/).  Keeps every
radio navaid with an ident and a position — the game uses them as real
sector gates, preferring VORs the way TRACON corner posts always did.

Usage: python tools/build_navaids.py navaids.csv
"""

import csv
import gzip
import json
import sys


def main(navaids_csv):
    out = []
    with open(navaids_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ident = row["ident"].strip().upper()
            name = row["name"].strip()
            try:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            if not ident or not name:
                continue
            out.append({
                "id": ident,
                "name": name,
                "type": row["type"],
                "lat": round(lat, 5),
                "lon": round(lon, 5),
            })
    path = "src/blips/data/navaids.json.gz"
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps({"navaids": out}, separators=(",", ":")))
    kinds = {}
    for n in out:
        kinds[n["type"]] = kinds.get(n["type"], 0) + 1
    print(f"{len(out)} navaids → {path}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main(sys.argv[1])
