"""Vendored per-airport instrument procedures: the named SIDs, STARs and
approaches that string real fixes into the arrival and departure flows a
controller actually works.

Built offline from the FAA CIFP (tools/build_procedures.py →
data/procedures.json.gz) and refreshed only every few years, the same
vendored pattern as the schedule and airport data.  US-first, because the
CIFP is; an airport with no vendored procedures simply has none, and the
game falls back to plain vectoring the way it always did.

Each airport maps to a list of procedures; a procedure is a name, a kind
(SID / STAR / APPCH), and its transitions; a transition is an ordered list
of legs.  A leg names the fix it flies to and the ARINC path/terminator that
gets there — see build_procedures.py for the record shape.  Fix coordinates
are not stored: they resolve here against the vendored fixes and navaids, so
one waypoint has one position across the whole game.
"""

import gzip
import json
import math
import os

from blips._airports import load_fixes, load_navaids
from blips._geo import haversine_nm

_DATA = None
_FIXC = None      # fix ident -> (lat, lon), unique
_NAVC = None      # navaid ident -> [(lat, lon), ...]  (idents repeat globally)


def _load():
    global _DATA
    if _DATA is None:
        path = os.path.join(os.path.dirname(__file__), "data",
                            "procedures.json.gz")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                _DATA = json.load(fh)
        except FileNotFoundError:      # no vendored procedures — vectors only
            _DATA = {}
    return _DATA


def procedures_for(icao):
    """The procedure list for an airport, or [] when none is vendored."""
    rec = _load().get((icao or "").upper())
    return rec["procs"] if rec else []


def _coords():
    global _FIXC, _NAVC
    if _FIXC is None:
        _FIXC = {f["id"]: (f["lat"], f["lon"]) for f in load_fixes()}
        _NAVC = {}
        for n in load_navaids():
            _NAVC.setdefault(n["id"], []).append((n["lat"], n["lon"]))
    return _FIXC, _NAVC


def _resolve(leg, airport):
    """A leg's fix as (lat, lon), or None when it flies a heading, not a fix.

    Runway and airport references collapse to the field; a navaid or
    waypoint resolves from its dataset, and a duplicated navaid ident picks
    the copy nearest the field — the one the procedure means.
    """
    fix, section = leg["f"], leg["s"]
    if not fix:
        return None
    if fix.startswith("RW") or section in ("PG", "PA") or fix == airport["icao"]:
        return (airport["lat"], airport["lon"])
    fixc, navc = _coords()
    if section in ("D", "DB", "PN"):
        cands = navc.get(fix, [])
    else:
        c = fixc.get(fix)
        cands = [c] if c else []
    if not cands:                                  # section lied — try both
        c = fixc.get(fix)
        cands = [c] if c else list(navc.get(fix, []))
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    coslat = math.cos(math.radians(airport["lat"]))
    return min(cands, key=lambda p: (p[0] - airport["lat"]) ** 2
               + ((p[1] - airport["lon"]) * coslat) ** 2)


def _rwy_digits(rwy):
    return "".join(c for c in (rwy or "") if c.isdigit())


def _restr(leg):
    """A leg's crossing restriction as ``(floor_ft, ceil_ft, speed_kt)``, any
    of them None.  The altitude carries an ARINC descriptor: ``+`` is at-or-
    above (a floor), ``-`` at-or-below (a ceiling), a bare number a mandatory
    crossing (both), and ``FLxxx`` is a flight level.  The speed limit reads
    as a maximum to cross at."""
    lo = hi = spd = None
    a = leg.get("a")
    if a:
        digits = "".join(c for c in a if c.isdigit())
        if digits:
            ft = float(digits) * (100.0 if "FL" in a else 1.0)
            if a[0] == "+":
                lo = ft
            elif a[0] == "-":
                hi = ft
            else:                       # bare 'at', or a block we keep as 'at'
                lo = hi = ft
    s = leg.get("spd")
    if s:
        try:
            spd = int(s)
        except ValueError:
            spd = None
    return lo, hi, spd


def _serves(trans, active):
    """Does a transition belong to today's flow?  Enroute and common-route
    transitions always do; a runway transition only when it's this runway."""
    if not trans or trans == "RWALL" or not trans.startswith("RW"):
        return True
    return _rwy_digits(trans[2:]) == active


def _serving(airport, active, radius_nm):
    """Every SID/STAR that serves the active runway, as
    (kind, name, paths, outer_point) — legs clipped to the terminal area."""
    alat, alon = airport["lat"], airport["lon"]
    out = []
    for proc in procedures_for(airport["icao"]):
        if proc["k"] not in ("SID", "STAR"):
            continue
        rw_trans = [t for t in proc["t"] if (t["v"] or "").startswith("RW")]
        if rw_trans and not any(_serves(t["v"], active) for t in rw_trans):
            continue                               # only serves other runways
        proc_paths, farthest = [], None
        for tr in proc["t"]:
            if not _serves(tr["v"], active):
                continue
            pts = [p for p in (_resolve(leg, airport) for leg in tr["legs"])
                   if p is not None
                   and haversine_nm(alat, alon, p[0], p[1]) <= radius_nm]
            if len(pts) >= 2:
                proc_paths.append(pts)
            for p in pts:
                d = haversine_nm(alat, alon, p[0], p[1])
                if farthest is None or d > farthest[0]:
                    farthest = (d, p)
        if farthest is not None:
            out.append((proc["k"], proc["n"], proc_paths, farthest[1]))
    return out


def overlay_for(airport, active_rwy, entry_gates=None, exit_gates=None,
                radius_nm=60.0):
    """Drawable geometry for the procedures feeding today's runway:

        {"paths": [(kind, [(lat, lon), ...]), ...],   # dotted polylines
         "labels": [(lat, lon, name, kind), ...]}      # one per procedure

    ``kind`` is "STAR" or "SID" so the caller can paint arrivals and depar-
    tures apart.

    Only SIDs and STARs are drawn — the approach's final segment is already
    the localizer the scope paints.  A procedure is skipped unless it serves
    the active runway (or is runway-agnostic).

    The declutter ties back to the sector's gates: a busy field lists dozens
    of procedures, but a controller only has up the ones on their own flows,
    so each entry gate keeps the STAR whose outer end lies nearest it, each
    exit gate the nearest SID.  That bounds the picture to roughly one path
    per gate, spread around the compass, and never empties a real field the
    way a hard distance cutoff would.  With no gates given, every serving
    procedure is drawn (the raw plate).

    Fixes past ``radius_nm`` are clipped to the terminal area the scope shows.
    """
    active = _rwy_digits(active_rwy)
    serving = _serving(airport, active, radius_nm)

    if entry_gates is None and exit_gates is None:
        chosen = serving
    else:
        chosen, picked = [], set()

        def take(gates, kind):
            for glat, glon in gates or []:
                cands = [s for s in serving if s[0] == kind]
                if not cands:
                    continue
                best = min(cands, key=lambda s: haversine_nm(
                    glat, glon, s[3][0], s[3][1]))
                if best[1] not in picked:
                    picked.add(best[1])
                    chosen.append(best)

        take(entry_gates, "STAR")
        take(exit_gates, "SID")

    paths, labels = [], []
    for kind, name, proc_paths, outer in chosen:
        for pts in proc_paths:
            paths.append((kind, pts))
        labels.append((outer[0], outer[1], name, kind))
    return {"paths": paths, "labels": labels}


def flow_path(airport, active_rwy, kind, rng):
    """One stitched polyline for a random SID/STAR serving the runway — the
    fix sequence a flight actually flies, ordered gate→field for an arrival
    and field→gate for a departure — as ``(name, [(lat, lon, floor_ft,
    ceil_ft, speed_kt), ...])`` or None; the last three carry each fix's
    crossing restriction (any None) so the flight can descend/climb via it.

    A transition is classed by its identifier: ``RW…`` is the runway leg,
    empty is the common route, anything else is a named entry/exit.  An
    arrival stitches entry → common → runway → field; a departure the reverse.
    Used to fly the uncontrolled traffic of neighbouring fields down their
    own procedures.
    """
    want = "STAR" if kind == "arrival" else "SID"
    procs = [p for p in procedures_for(airport["icao"]) if p["k"] == want]
    order = list(range(len(procs)))
    rng.shuffle(order)
    field = (airport["lat"], airport["lon"])
    active = _rwy_digits(active_rwy)
    for idx in order:
        proc = procs[idx]
        enroute, common, runway = [], None, None
        for tr in proc["t"]:
            v = tr["v"] or ""
            if v.startswith("RW"):
                if _serves(v, active):
                    runway = tr
            elif v == "":
                common = tr
            else:
                enroute.append(tr)
        if any((t["v"] or "").startswith("RW") for t in proc["t"]) \
                and runway is None:
            continue                              # serves only other runways
        entry = enroute[rng.randrange(len(enroute))] if enroute else None
        chain = ([entry, common, runway] if want == "STAR"
                 else [runway, common, entry])
        pts = []
        for tr in chain:
            if tr is None:
                continue
            for leg in tr["legs"]:
                p = _resolve(leg, airport)
                if p is not None and (not pts or haversine_nm(
                        pts[-1][0], pts[-1][1], p[0], p[1]) > 0.2):
                    lo, hi, spd = _restr(leg)
                    pts.append((p[0], p[1], lo, hi, spd))
        field_pt = (field[0], field[1], None, None, None)
        if want == "STAR":
            if not pts or haversine_nm(pts[-1][0], pts[-1][1], *field) > 1.0:
                pts.append(field_pt)
        else:
            if not pts or haversine_nm(pts[0][0], pts[0][1], *field) > 1.0:
                pts.insert(0, field_pt)
        if len(pts) >= 2:
            return proc["n"], pts
    return None


def find_named(icao, name):
    """The procedure at a field with this name (case-insensitive), or None."""
    want = (name or "").strip().upper()
    for proc in procedures_for(icao):
        if proc["n"].upper() == want:
            return proc
    return None


def build_join(airport, active_rwy, proc, ref):
    """The fix sequence to fly for a named procedure, from the point nearest
    ``ref`` onward — how a controller joins a plane onto a published routing.

    For a STAR the sequence runs entry → common → runway (stopping at the
    last real fix, short of the field, where the approach takes over); for a
    SID it runs runway → common → exit, out of the field.  The entry (or
    exit) transition is the one whose join fix sits nearest ``ref``, so the
    plane joins where it already is.  Each point is ``(lat, lon, floor_ft,
    ceil_ft, speed_kt)`` — the crossing restriction rides along so the plane
    can fly the published 'descend via' profile.  Returns [] when nothing
    resolves.
    """
    active = _rwy_digits(active_rwy)
    field = (airport["lat"], airport["lon"])
    enroute, common, runway = [], None, None
    for tr in proc["t"]:
        v = tr["v"] or ""
        if v.startswith("RW"):
            if _serves(v, active):
                runway = tr
        elif v == "":
            common = tr
        else:
            enroute.append(tr)
    star = proc["k"] == "STAR"

    def pts_of(tr):
        out = []
        for leg in (tr["legs"] if tr else ()):
            p = _resolve(leg, airport)
            if p is None or haversine_nm(field[0], field[1], *p) < 0.6:
                continue                          # drop the field-centre legs
            if not out or haversine_nm(out[-1][0], out[-1][1], *p) > 0.2:
                lo, hi, spd = _restr(leg)
                out.append((p[0], p[1], lo, hi, spd))
        return out

    common_pts, runway_pts = pts_of(common), pts_of(runway)
    best = None
    for entry in (enroute or [None]):
        e_pts = pts_of(entry)
        seq = (e_pts + common_pts + runway_pts if star
               else runway_pts + common_pts + e_pts)
        merged = []
        for p in seq:
            if not merged or haversine_nm(merged[-1][0], merged[-1][1],
                                          p[0], p[1]) > 0.2:
                merged.append(p)
        if not merged:
            continue
        ji = min(range(len(merged)),
                 key=lambda i: haversine_nm(ref[0], ref[1],
                                            merged[i][0], merged[i][1]))
        nav = merged[ji:]
        d = haversine_nm(ref[0], ref[1], nav[0][0], nav[0][1])
        if best is None or d < best[0]:
            best = (d, nav)
    return best[1] if best else []
