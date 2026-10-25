#!/usr/bin/env python3
"""Build src/blips/data/airlines.json.gz: who an ICAO airline code is.

Search needs to read a flight number the way it's printed on a boarding
pass ("UA 123") and find the callsign it flies under ("UAL123"), and the
flight card wants the company's name ("Republic Airways") rather than the
radio telephony the game speaks ("Brickyard").  Both come from
OpenFlights' airlines.dat (Open Database License), active carriers only:

    {"airlines": {"DAL": ["Delta Air Lines", "DL"], ...}}

OpenFlights has gone stale in places — a code reassigned, a carrier long
gone still marked active — so CORRECTIONS below overrides it for carriers
a scope in 2026 actually sees.  An entry there wins outright.

Usage: python tools/build_airlines.py [airlines.dat]
       (downloads airlines.dat from OpenFlights when no path is given)
"""

import csv
import gzip
import io
import json
import os
import sys
import urllib.request

OPENFLIGHTS_URL = ("https://raw.githubusercontent.com/jpatokal/openflights/"
                   "master/data/airlines.dat")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "blips", "data",
                   "airlines.json.gz")

# ICAO → (name, IATA): newer carriers, reassigned codes, and names
# OpenFlights mangles.  IATA None means the carrier keeps its name but
# gives up any two-letter code OpenFlights still credits it with.
CORRECTIONS = {
    "MXY": ("Breeze Airways", "MX"),
    "MXA": ("Mexicana de Aviación", None),
    "VXP": ("Avelo Airlines", "XP"),
    "XPT": (None, None),
    "ITY": ("ITA Airways", "AZ"),
    "AZA": ("Alitalia", None),
    "IGO": ("IndiGo", "6E"),
    "AKJ": ("Akasa Air", "QP"),
    "ENY": ("Envoy Air", "MQ"),
    "RPA": ("Republic Airways", "YX"),
    "SKW": ("SkyWest Airlines", "OO"),
    "EDV": ("Endeavor Air", "9E"),
    "ASH": ("Mesa Airlines", "YV"),
    "GJS": ("GoJet Airlines", "G7"),
    "PDT": ("Piedmont Airlines", "PT"),
    "JIA": ("PSA Airlines", "OH"),
    "QXE": ("Horizon Air", "QX"),
    "FLE": ("Flair Airlines", "F8"),
    "POE": ("Porter Airlines", "PD"),
    "JSX": ("JSX", "XE"),
    "SCX": ("Sun Country Airlines", "SY"),
    "AAY": ("Allegiant Air", "G4"),
    "NKS": ("Spirit Airlines", "NK"),
    "FFT": ("Frontier Airlines", "F9"),
    "ROU": ("Air Canada Rouge", "RV"),
    "JZA": ("Jazz", "QK"),
    "TSC": ("Air Transat", "TS"),
    "VOI": ("Volaris", "Y4"),
    "VIV": ("Viva Aerobus", "VB"),
    "AMX": ("Aeroméxico", "AM"),
    "GLO": ("Gol", "G3"),
    "AZU": ("Azul", "AD"),
    "CMP": ("Copa Airlines", "CM"),
    "AVA": ("Avianca", "AV"),
    "VJC": ("VietJet Air", "VJ"),
    "WZZ": ("Wizz Air", "W6"),
    "EZY": ("easyJet", "U2"),
    "EWG": ("Eurowings", "EW"),
    "CFG": ("Condor", "DE"),
    "BTI": ("airBaltic", "BT"),
    "FDX": ("FedEx", "FX"),
    "UPS": ("UPS Airlines", "5X"),
    "GTI": ("Atlas Air", "5Y"),
    "EJA": ("NetJets", None),
    "LXJ": ("Flexjet", None),
    "RCH": ("US Air Force Air Mobility Command", None),
}


def load(path=None):
    if path:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        print("fetching OpenFlights airlines.dat ...", file=sys.stderr)
        req = urllib.request.Request(OPENFLIGHTS_URL,
                                     headers={"User-Agent": "blips-build"})
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def build(rows):
    out = {}
    for row in rows:
        if len(row) < 8 or row[7] != "Y":
            continue
        name, iata, icao = row[1].strip(), row[3].strip(), row[4].strip()
        if not (len(icao) == 3 and icao.isalpha() and icao.isupper()):
            continue
        if not (len(iata) == 2 and iata.isalnum()):
            iata = ""
        if name and icao not in out:
            out[icao] = [name, iata]
    # an IATA code claimed by a corrected carrier is that carrier's alone
    claimed = {iata for _n, iata in CORRECTIONS.values() if iata}
    for icao, entry in out.items():
        if entry[1] in claimed and icao not in CORRECTIONS:
            entry[1] = ""
    for icao, (name, iata) in CORRECTIONS.items():
        if name is None:
            out.pop(icao, None)
            continue
        out[icao] = [name, iata or ""]
    return dict(sorted(out.items()))


def main():
    rows = load(sys.argv[1] if len(sys.argv) > 1 else None)
    airlines = build(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump({"airlines": airlines}, fh, ensure_ascii=False,
                  separators=(",", ":"))
    with_iata = sum(1 for _n, i in airlines.values() if i)
    print(f"wrote {len(airlines)} airlines ({with_iata} with an IATA code) "
          f"→ {os.path.normpath(OUT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
