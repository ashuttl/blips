#!/usr/bin/env python3
"""Build src/blips/data/types.json.gz: what an ICAO type designator is.

The feed says "B38M"; the flight card should say "Boeing 737 MAX 8", and
search should find every 737 when you type "737".  Airliners come from
OpenFlights' planes.dat (Open Database License); it stops short of the
business jets, light aircraft, helicopters and military lifters a live
scope is also full of, so EXTRA below adds the common ones.

    {"types": {"B738": "Boeing 737-800", ...}}

Usage: python tools/build_types.py [planes.dat]
       (downloads planes.dat from OpenFlights when no path is given)
"""

import csv
import gzip
import io
import json
import os
import sys
import urllib.request

OPENFLIGHTS_URL = ("https://raw.githubusercontent.com/jpatokal/openflights/"
                   "master/data/planes.dat")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "blips", "data",
                   "types.json.gz")

EXTRA = {
    # business jets
    "C25A": "Cessna Citation CJ2", "C25B": "Cessna Citation CJ3",
    "C25C": "Cessna Citation CJ4", "C525": "Cessna CitationJet",
    "C510": "Cessna Citation Mustang", "C550": "Cessna Citation II",
    "C560": "Cessna Citation V", "C56X": "Cessna Citation Excel",
    "C680": "Cessna Citation Sovereign", "C68A": "Cessna Citation Latitude",
    "C700": "Cessna Citation Longitude", "C750": "Cessna Citation X",
    "E50P": "Embraer Phenom 100", "E55P": "Embraer Phenom 300",
    "E545": "Embraer Legacy 450", "E550": "Embraer Praetor 600",
    "E35L": "Embraer Legacy 600",
    "CL30": "Bombardier Challenger 300", "CL35": "Bombardier Challenger 350",
    "CL60": "Bombardier Challenger 600", "GLEX": "Bombardier Global Express",
    "GL5T": "Bombardier Global 5000", "GL7T": "Bombardier Global 7500",
    "GLF4": "Gulfstream IV", "GLF5": "Gulfstream V", "GLF6": "Gulfstream G650",
    "GA5C": "Gulfstream G500", "GA6C": "Gulfstream G600",
    "G280": "Gulfstream G280",
    "LJ35": "Learjet 35", "LJ45": "Learjet 45", "LJ60": "Learjet 60",
    "LJ75": "Learjet 75", "H25B": "Hawker 800",
    "FA50": "Dassault Falcon 50", "F900": "Dassault Falcon 900",
    "F2TH": "Dassault Falcon 2000", "FA7X": "Dassault Falcon 7X",
    "FA8X": "Dassault Falcon 8X", "PC24": "Pilatus PC-24",
    "HDJT": "Honda HondaJet", "SF50": "Cirrus Vision Jet",
    # turboprops
    "PC12": "Pilatus PC-12", "C208": "Cessna 208 Caravan",
    "BE20": "Beechcraft King Air 200", "B350": "Beechcraft King Air 350",
    "BE9L": "Beechcraft King Air 90", "TBM7": "Daher TBM 700",
    "TBM8": "Daher TBM 850", "TBM9": "Daher TBM 900",
    "AT43": "ATR 42-300", "AT45": "ATR 42-500", "AT72": "ATR 72",
    "AT75": "ATR 72-500", "AT76": "ATR 72-600",
    "DH8A": "De Havilland Canada Dash 8-100",
    "DH8B": "De Havilland Canada Dash 8-200",
    "DH8C": "De Havilland Canada Dash 8-300",
    "DH8D": "De Havilland Canada Dash 8-400",
    "SW4": "Fairchild Metro", "B190": "Beechcraft 1900",
    # light aircraft
    "C150": "Cessna 150", "C152": "Cessna 152", "C172": "Cessna 172",
    "C182": "Cessna 182", "C206": "Cessna 206", "C210": "Cessna 210",
    "C310": "Cessna 310", "P28A": "Piper Cherokee", "P28R": "Piper Arrow",
    "PA32": "Piper Saratoga", "PA34": "Piper Seneca", "PA44": "Piper Seminole",
    "PA46": "Piper Malibu", "SR20": "Cirrus SR20", "SR22": "Cirrus SR22",
    "S22T": "Cirrus SR22T", "DA40": "Diamond DA40", "DA42": "Diamond DA42",
    "DA62": "Diamond DA62", "BE36": "Beechcraft Bonanza",
    "BE58": "Beechcraft Baron", "M20P": "Mooney M20",
    # helicopters
    "R22": "Robinson R22", "R44": "Robinson R44", "R66": "Robinson R66",
    "B06": "Bell 206 JetRanger", "B407": "Bell 407", "B429": "Bell 429",
    "EC30": "Airbus H130", "EC35": "Airbus H135", "EC45": "Airbus H145",
    "AS50": "Airbus AS350 Écureuil", "A139": "Leonardo AW139",
    "A109": "Leonardo AW109", "S76": "Sikorsky S-76",
    "H60": "Sikorsky UH-60 Black Hawk",
    # military and government lifters
    "C130": "Lockheed C-130 Hercules", "C30J": "Lockheed C-130J Super Hercules",
    "C17": "Boeing C-17 Globemaster III", "C5M": "Lockheed C-5M Super Galaxy",
    "K35R": "Boeing KC-135 Stratotanker", "KC46": "Boeing KC-46 Pegasus",
    "P8": "Boeing P-8 Poseidon", "E3TF": "Boeing E-3 Sentry",
    "A400": "Airbus A400M Atlas", "B52": "Boeing B-52 Stratofortress",
    "F16": "General Dynamics F-16", "F35": "Lockheed Martin F-35",
    "C12": "Beechcraft C-12 Huron", "V22": "Bell Boeing V-22 Osprey",
}


def load(path=None):
    if path:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        print("fetching OpenFlights planes.dat ...", file=sys.stderr)
        req = urllib.request.Request(OPENFLIGHTS_URL,
                                     headers={"User-Agent": "blips-build"})
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def build(rows):
    out = {}
    for row in rows:
        if len(row) < 3:
            continue
        name, icao = row[0].strip(), row[2].strip()
        if name and icao and icao != r"\N" and icao not in out:
            out[icao] = name
    out.update(EXTRA)
    return dict(sorted(out.items()))


def main():
    types = build(load(sys.argv[1] if len(sys.argv) > 1 else None))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump({"types": types}, fh, ensure_ascii=False,
                  separators=(",", ":"))
    print(f"wrote {len(types)} types → {os.path.normpath(OUT)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
