#!/usr/bin/env python3
"""Build src/blips/data/procedures.json.gz from the FAA CIFP (ARINC 424).

Same public-domain source and 28-day cycle as tools/build_fixes.py (the
``FAACIFP18`` file inside ``CIFP_YYMMDD.zip``).  Where build_fixes harvests
the waypoints, this harvests the named procedures that string them together
— the SIDs (subsection ``D``), STARs (``E``) and approaches (``F``) — into a
per-airport enrichment blob keyed by ICAO, kept only for airports the game
actually flies (the vendored airport DB).  US-first, the way the CIFP is.

Each procedure is a name, a kind, and its transitions; each transition is an
ordered list of legs.  A leg carries the fix it flies to (with the section
that says which dataset resolves it — ``D``/``DB`` navaid, ``PC``/``EA``
waypoint, ``PG`` runway), the ARINC path/terminator (``IF``/``TF``/``CF``…),
and any crossing constraint.  Legs that fly a heading or course rather than
to a fix (``VI``/``CA``/``VA``…) keep an empty fix — real, undrawable, and
there for the day the sim flies the procedure itself.

    "KPWM": {"procs": [
        {"n": "CDOGG4", "k": "STAR", "t": [
            {"v": "CAM", "r": "4", "legs": [
                {"f": "CAM", "s": "D", "l": "IF"},
                {"f": "CDOGG", "s": "PC", "l": "TF", "a": "11000"}, ...]}, ...]}]}

Fix coordinates are NOT baked in — they resolve at runtime against the
already-vendored fixes/navaids, so this stays small and never drifts from
them.

The APPCH records carry more than legs: the procedure ident's first letter
is the approach type (``I06`` is the ILS to runway 6, ``R31`` the RNAV,
``V-A`` a VOR that only circles), and the game reads that spelling to know
which runway ends really have a localizer (see procedures.approach_ends).
A rebuild against a fresh CIFP cycle refreshes availability for free.

ARINC 424-18, fixed width 132: proc ident cols 14-19, route type 20,
transition ident 21-25, sequence 27-29, fix ident 30-34, fix section 37-38,
path/terminator 48-49, altitude description 83, altitude 85-89, speed
limit 100-102.

Usage: python tools/build_procedures.py FAACIFP18
"""

import gzip
import json
import os
import sys

_KIND = {"D": "SID", "E": "STAR", "F": "APPCH"}


def main(cifp_file):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with gzip.open(os.path.join(here, "src", "blips", "data",
                                "airports.json.gz"), "rt",
                   encoding="utf-8") as fh:
        keep = {a["icao"] for a in json.load(fh)["airports"]}

    # airport -> proc name -> {"k": kind, "t": {trans_key: {...}}}
    airports = {}
    with open(cifp_file, encoding="latin-1") as fh:
        for line in fh:
            if line[4] != "P":
                continue
            kind = _KIND.get(line[12])
            if kind is None:
                continue
            icao = line[6:10].strip().upper()
            if icao not in keep:
                continue
            proc = line[13:19].strip()
            route_type = line[19]
            trans = line[20:25].strip()
            seq = line[26:29].strip()
            fix = line[29:34].strip()
            section = line[36:38].strip()
            leg_type = line[47:49].strip()
            alt_desc = line[82]
            alt = line[84:89].strip()
            speed = line[99:102].strip()

            procs = airports.setdefault(icao, {})
            rec = procs.setdefault(proc, {"k": kind, "t": {}})
            tkey = (route_type, trans)
            tr = rec["t"].setdefault(tkey, {"v": trans, "r": route_type,
                                            "legs": []})
            leg = {"f": fix, "s": section, "l": leg_type}
            if alt:
                leg["a"] = (alt_desc.strip() + alt).strip()
            if speed:
                leg["spd"] = speed
            tr["legs"].append((seq, leg))

    out = {}
    for icao, procs in airports.items():
        proclist = []
        for name, rec in procs.items():
            transitions = []
            for k in sorted(rec["t"]):
                tr = rec["t"][k]
                tr["legs"] = [leg for _seq, leg in
                              sorted(tr["legs"], key=lambda sl: sl[0])]
                transitions.append(tr)
            proclist.append({"n": name, "k": rec["k"], "t": transitions})
        proclist.sort(key=lambda p: (p["k"], p["n"]))
        out[icao] = {"procs": proclist}

    path = os.path.join(here, "src", "blips", "data", "procedures.json.gz")
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps(out, separators=(",", ":"), sort_keys=True))
    n_proc = sum(len(v["procs"]) for v in out.values())
    print(f"{len(out)} airports, {n_proc} procedures → {path}")


if __name__ == "__main__":
    main(sys.argv[1])
