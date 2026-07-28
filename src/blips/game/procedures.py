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

The raw CIFP record is not a drawable thing, though, and pretending it is
was what made a busy field unreadable.  A procedure lists every runway it
has ever served, transitions that fly headings rather than fixes, and
enroute entries that begin hundreds of miles away.  So the raw records are
compiled, once per (airport, runway), into a **plan** — the one shape that
procedure takes on today's flow:

    {"name", "kind",                 # CHINS5, STAR
     "spine": [Fix, ...],            # the trunk, ordered gate → field
     "branches": [(name, [Fix, ...])],   # entries that feed the trunk head
     "gate": Fix,                    # where the trunk meets the boundary
     "vectors": bool,                # ...then radar vectors (a real ending)
     "top", "bottom"}                # the published altitude window

A ``Fix`` is ``(lat, lon, ident, floor_ft, ceil_ft, speed_kt)``: a real
place with the crossing restriction that rides along with it.  Everything
the game draws, flies, labels or explains comes off a plan, so the picture,
the clearance and the hover chip can never disagree about what a procedure
is.
"""

import gzip
import json
import math
import os

from blips._airports import load_fixes, load_navaids
from blips._geo import bearing_to, haversine_nm

_DATA = None
_FIXC = None      # fix ident -> (lat, lon), unique
_NAVC = None      # navaid ident -> [(lat, lon), ...]  (idents repeat globally)
_PLANS = {}       # (icao, runway, radius) -> [plan, ...], compiled once

# ARINC path/terminators that end a procedure in radar vectors rather than at
# a fix.  "VM" flies a heading until the controller says otherwise; "FM" the
# same from a named fix.  Both are real endings — the plate says *expect
# vectors* — so they're carried as a flag instead of quietly truncating.
_VECTOR_END = frozenset(("VM", "FM"))

# ...and the ones that fly a heading with no fix at all.  These are the climb
# off the runway ("VA" to an altitude, "VI" to an intercept): undrawable in
# themselves, but they're the reason a SID begins at the threshold.
_HEADING_LEG = frozenset(("VA", "VI", "VD", "VR", "CA", "CI"))

SECTOR_EDGE_NM = 78.0     # how far out a plan is compiled and drawn.  It has
                          # to reach past the furthest corner post a sector
                          # can adopt (Boston's ORW is 66 nm out, Seattle's
                          # CHINS 67), or the trunk gets clipped short of its
                          # own gate and the name ends up labelling a bare
                          # point on the boundary instead of the fix.  The
                          # viewport throws away whatever is off-screen.


def _load():
    global _DATA
    if _DATA is None:
        # data lives in the package root (blips/data), one level up from game/
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data",
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
    return "".join(c for c in (rwy or "") if c.isdigit()).lstrip("0")


def _rwy_side(rwy):
    tail = (rwy or "").strip().upper().lstrip("0123456789")
    return tail[:1]


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
    return _rwy_digits(trans[2:]) == _rwy_digits(active)


def _rwy_rank(trans, active):
    """How well a runway transition matches the runway in use — lower is
    better, None when it serves a different runway entirely.

    The CIFP files one transition per parallel: ``RW34C``, ``RW34L`` and
    ``RW34R`` differ only in the first fix off the threshold, and matching on
    the digits alone (as this once did) made every SID draw three times over
    and fly the wrong parallel's initial fix.  Exact wins; ``B`` (both) and a
    bare number are the published stand-ins; another parallel is a last
    resort, taken only when this runway has no transition of its own.
    """
    if not trans or not trans.startswith("RW"):
        return None
    ident = trans[2:]
    if ident == "ALL":
        return 3
    if _rwy_digits(ident) != _rwy_digits(active):
        return None
    side, want = _rwy_side(ident), _rwy_side(active)
    if side == want:
        return 0
    if side in ("B", ""):                 # "34B" — both parallels, or plain
        return 1
    return 2                              # the other parallel's own version


def _threshold(airport, ident):
    """The departure threshold for a runway ident, or the field centre."""
    want = _rwy_digits(ident), _rwy_side(ident)
    for rwy in airport["rwys"]:
        for end in ("le", "he"):
            rid, course, tlat, tlon = rwy[end]
            if (_rwy_digits(rid), _rwy_side(rid)) == want:
                if tlat is None:
                    return (airport["lat"], airport["lon"])
                return (tlat, tlon)
    return (airport["lat"], airport["lon"])


def _legs_to_fixes(tr, airport):
    """A transition's legs as ``[(lat, lon, ident, lo, hi, spd), ...]``, plus
    whether it ends in radar vectors.

    Heading legs carry no fix and drop out — but a vector terminator is
    remembered rather than discarded, because "then vectors" is what the
    plate actually says, and a procedure that quietly stopped short read as
    missing data instead of as the real thing.
    """
    out, vectors = [], False
    for leg in (tr["legs"] if tr else ()):
        if leg["l"] in _VECTOR_END:
            vectors = True                 # ...and whatever fix it names is
            continue                       # the one we're already at
        if leg["l"] in _HEADING_LEG and not leg["f"]:
            continue
        p = _resolve(leg, airport)
        if p is None:
            continue
        lo, hi, spd = _restr(leg)
        if out and haversine_nm(out[-1][0], out[-1][1], p[0], p[1]) <= 0.2:
            # the same fix twice (a TF to it, then an FM from it): keep the
            # tighter restriction rather than a zero-length leg
            prev = out[-1]
            out[-1] = (prev[0], prev[1], prev[2] or leg["f"],
                       prev[3] if prev[3] is not None else lo,
                       prev[4] if prev[4] is not None else hi,
                       prev[5] if prev[5] is not None else spd)
            continue
        out.append((p[0], p[1], leg["f"], lo, hi, spd))
    return out, vectors


def _splice(*chains):
    """Join fix chains end to end, dropping the seam where one ends on the
    fix the next begins with."""
    merged = []
    for chain in chains:
        for p in chain:
            if merged and haversine_nm(merged[-1][0], merged[-1][1],
                                       p[0], p[1]) <= 0.2:
                continue
            merged.append(p)
    return merged


def _clip_runs(pts, alat, alon, radius_nm):
    """Split a fix chain into the runs of it that lie within ``radius_nm``.

    The old code filtered points and kept the survivors in one list, so a leg
    that left the ring and came back had its outside fixes deleted and the
    two neighbours joined by a straight line that no aircraft ever flies.
    Splitting into runs — and walking each one out to the boundary itself —
    keeps the picture honest: a flow that leaves the scope leaves it.
    """
    def inside(p):
        return haversine_nm(alat, alon, p[0], p[1]) <= radius_nm

    def edge(a, b):
        """Where segment a→b crosses the ring, as an unnamed fix.  Exactly one
        end is inside; bisect outward from whichever that is, so the crossing
        is found the same way whether the flow is entering or leaving."""
        near, far = (a, b) if inside(a) else (b, a)
        lo, hi = 0.0, 1.0                          # fraction near → far
        for _ in range(14):                        # ~4 m at 60 nm
            mid = (lo + hi) / 2.0
            plat = near[0] + (far[0] - near[0]) * mid
            plon = near[1] + (far[1] - near[1]) * mid
            if haversine_nm(alat, alon, plat, plon) <= radius_nm:
                lo = mid
            else:
                hi = mid
        return (near[0] + (far[0] - near[0]) * lo,
                near[1] + (far[1] - near[1]) * lo,
                None, near[3], near[4], near[5])

    runs, cur = [], []
    for i, p in enumerate(pts):
        if inside(p):
            if not cur and i > 0:                  # entering: start at the ring
                cur.append(edge(pts[i - 1], p))
            cur.append(p)
        else:
            if cur:
                cur.append(edge(cur[-1], p))       # leaving: stop at the ring
                runs.append(cur)
                cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2 and _length(r) >= 1.5]


def _length(pts):
    return sum(haversine_nm(a[0], a[1], b[0], b[1])
               for a, b in zip(pts, pts[1:]))


def _window(fixes):
    """The published altitude window over a chain: (top, bottom) in feet."""
    alts = [a for f in fixes for a in (f[3], f[4]) if a is not None]
    return (max(alts), min(alts)) if alts else (None, None)


GATE_BAND_NM = (10.0, 70.0)   # where a corner post can credibly sit


def _pick_gate(spine, name, alat, alon, star):
    """Which fix on a spine is the procedure's corner post.

    Almost always the one it is *named for*: the CAMRN FIVE crosses CAMRN,
    the ROBUC THREE crosses ROBUC, the OOSHN FIVE crosses OOSHN.  Taking the
    outermost fix instead — which is where this started — put Boston's
    OOSHN5 label under the EURRO gate and its WOONS2 label under ORW, both
    of them true statements about the trunk and neither of them the post the
    arrival is named after.  Reading the name off the plate and finding it on
    the spine gets the real answer nearly every time.

    Where the eponymous fix is missing or absurdly far out (Kennedy's PARCH
    is eighty miles away), fall back to the outermost fix still inside the
    band — the point where the flow crosses into the terminal area, which is
    what a corner post is for.
    """
    lo, hi = GATE_BAND_NM
    stem = name.rstrip("0123456789")
    in_band = [f for f in spine
               if f[2] and lo <= haversine_nm(alat, alon, f[0], f[1]) <= hi]
    for f in in_band:
        if f[2] == stem:
            return f
    if in_band:
        return max(in_band,
                   key=lambda f: haversine_nm(alat, alon, f[0], f[1]))
    named = [f for f in spine if f[2]]
    if not named:
        return spine[0] if star else spine[-1]
    return named[0] if star else named[-1]


def _reach(tr, airport, star):
    """How close a transition gets to the field at the end it flies toward —
    the test for which entry is really the trunk when none is published."""
    pts, _v = _legs_to_fixes(tr, airport)
    if not pts:
        return float("inf")
    end = pts[-1] if star else pts[0]
    return haversine_nm(airport["lat"], airport["lon"], end[0], end[1])


def plans_for(airport, active_rwy, radius_nm=SECTOR_EDGE_NM):
    """Every SID and STAR that serves the runway in use, compiled to plans.

    This is the one place the raw CIFP becomes a thing the game can draw and
    fly.  Per procedure it picks the single runway transition that matches
    today's runway (not every parallel's), splices it to the common route
    into one **spine** ordered the way an aeroplane flies it, anchors a
    departure's spine at the threshold it rolls from, hangs the enroute
    entries off the trunk head as **branches**, and remembers whether the
    whole thing ends in radar vectors.

    Cached per (airport, runway, radius): a flow change rebuilds it, a frame
    does not.
    """
    key = (airport["icao"], (active_rwy or "").upper(), radius_nm)
    if key in _PLANS:
        return _PLANS[key]
    alat, alon = airport["lat"], airport["lon"]
    out = []
    for proc in procedures_for(airport["icao"]):
        if proc["k"] not in ("SID", "STAR"):
            continue
        star = proc["k"] == "STAR"

        # one runway transition, the best match for the runway in use
        runway, best_rank = None, None
        common, enroute = None, []
        has_rwy = False
        for tr in proc["t"]:
            v = tr["v"] or ""
            if v.startswith("RW"):
                has_rwy = True
                rank = _rwy_rank(v, active_rwy)
                if rank is not None and (best_rank is None or rank < best_rank):
                    runway, best_rank = tr, rank
            elif v in ("", "ALL"):
                # ARINC files the common portion under a blank ident or the
                # literal "ALL" — never both.  Reading only the blank one lost
                # the trunk of half the dataset, which is why so many
                # procedures used to draw as a fan of unattached entries.
                common = tr
            else:
                enroute.append(tr)
        if has_rwy and runway is None:
            continue                            # serves only other runways

        rwy_pts, rwy_vec = _legs_to_fixes(runway, airport)
        com_pts, com_vec = _legs_to_fixes(common, airport)
        if not rwy_pts and not com_pts and enroute:
            # no trunk of its own: some procedures are published purely as
            # entries, each running the whole way in.  The one that reaches
            # furthest in becomes the trunk and the rest feed it.
            pick = min(enroute, key=lambda tr: _reach(tr, airport, star))
            enroute = [tr for tr in enroute if tr is not pick]
            com_pts, com_vec = _legs_to_fixes(pick, airport)
        if star:
            spine = _splice(com_pts, rwy_pts)
            vectors = rwy_vec or (com_vec and not rwy_pts)
        else:
            spine = _splice(rwy_pts, com_pts)
            vectors = com_vec or (rwy_vec and not com_pts)
            # a departure begins on the runway: the climb-out leg flies a
            # heading and carries no fix, so without this the stroke started
            # at the first named waypoint — thirteen miles off the field at
            # Portland — and the SID looked like it began in mid-air
            thr = _threshold(airport, active_rwy)
            if not spine or haversine_nm(thr[0], thr[1],
                                         spine[0][0], spine[0][1]) > 1.0:
                spine.insert(0, (thr[0], thr[1], active_rwy.upper(),
                                 None, None, None))
        # the field centre is where a STAR's "expect vectors" leg resolves;
        # the arrival really ends at its last named fix
        while star and len(spine) > 1 and haversine_nm(
                alat, alon, spine[-1][0], spine[-1][1]) < 0.6:
            spine.pop()
            vectors = True
        if len(spine) < 2:
            continue
        # An arrival that stops well short of the field is handing you the
        # aeroplane to vector, whether or not the CIFP bothered to file the
        # leg that says so — Boston's JFUND2 and OOSHN5 carry an explicit
        # terminator and ROBUC3 does not, though all three end on a downwind
        # fix a dozen miles out.  Without this the odd one out drew a stroke
        # that simply stopped dead, which reads as broken data rather than as
        # the most ordinary thing in approach control.
        if star and not vectors and haversine_nm(
                alat, alon, spine[-1][0], spine[-1][1]) > 5.0:
            vectors = True

        gate = _pick_gate(spine, proc["n"], alat, alon, star)
        trunk_head = spine[0] if star else spine[-1]
        branches = []
        for tr in enroute:
            pts, _v = _legs_to_fixes(tr, airport)
            if len(pts) < 2:
                continue
            if not star:
                pts = list(pts)
            # an entry only belongs to the picture if it actually meets the
            # trunk: off runway 34 Seattle's SUMMA2 is "NEZUG then vectors",
            # and its BKE and LKV entries hang off a fix this flow never
            # reaches.  Real, but not a line to draw.
            joint = pts[-1] if star else pts[0]
            if haversine_nm(joint[0], joint[1],
                            trunk_head[0], trunk_head[1]) > 1.0:
                continue
            branches.append((tr["v"], pts))

        top, bottom = _window(spine)
        out.append({
            "name": proc["n"], "kind": proc["k"], "rwy": (runway or {}).get("v"),
            "spine": spine, "branches": branches, "gate": gate,
            "vectors": bool(vectors), "top": top, "bottom": bottom,
            "dist_nm": haversine_nm(alat, alon, gate[0], gate[1]),
            "bearing": bearing_to(alat, alon, gate[0], gate[1]),
        })
    out.sort(key=lambda p: (p["kind"], p["name"]))
    if len(_PLANS) > 64:      # both runway ends of a field, its satellite
        _PLANS.clear()        # and its neighbours all compile their own

    _PLANS[key] = out
    return out


def fix_positions(airport, active_rwy):
    """Every named fix on today's procedures, as ``{ident: (lat, lon)}``.

    A controller can send an aeroplane direct to any fix on the plate, not
    just to the four corner posts — and once the radio starts suggesting
    "direct HUMPP and we can pick it up", ``dct HUMPP`` had better work.
    """
    out = {}
    for plan in plans_for(airport, active_rwy):
        chains = [plan["spine"]] + [pts for _v, pts in plan["branches"]]
        for chain in chains:
            for lat, lon, ident, _lo, _hi, _spd in chain:
                if ident and ident not in out and not ident.startswith("RW"):
                    out[ident] = (lat, lon)
    return out


def find_plan(airport, active_rwy, name):
    """The compiled plan for a named procedure on today's runway, or None."""
    want = (name or "").strip().upper()
    for plan in plans_for(airport, active_rwy):
        if plan["name"].upper() == want:
            return plan
    return None


def _octant(bearing):
    return int(((bearing + 22.5) % 360.0) // 45.0)


def flow_gates(airport, active_rwy):
    """The corner posts today's procedures actually use, as
    ``[{"name", "pos", "kind", "procs", "dist_nm", "bearing"}, ...]``.

    A TRACON's gates are not an abstract compass rose — they are the fixes
    the arrivals arrive over and the departures leave by, which is why a real
    controller can say "CHINS5" and "the CHINS gate" and mean one place.
    Deriving them from the plates instead of from the nearest navaid in each
    octant is what lets the label under a corner post name the procedure that
    feeds it, and what makes ``via`` agree with the picture.

    One gate per *fix*, not per octant.  Two arrivals can share a quadrant and
    still be separate corner posts — Boston's ROBUC and ORW sit eleven degrees
    apart — and thinning them here dropped a real gate on the floor and left
    the arrival that used it labelled in mid-air.  Spreading and capping is
    the sector's job, which knows how many posts it wants; this reports what
    the plates say.  Fields off the CIFP get nothing and fall back to the
    octant navaid search.
    """
    best = {}
    for plan in plans_for(airport, active_rwy):
        gate = plan["gate"]
        if not gate[2]:                            # a clipped edge, not a fix
            continue
        key = (plan["kind"], gate[2])
        if key in best:
            best[key]["procs"].append(plan["name"])
            continue
        best[key] = {
            "name": gate[2], "pos": (gate[0], gate[1]),
            "kind": plan["kind"], "procs": [plan["name"]],
            "dist_nm": plan["dist_nm"], "bearing": plan["bearing"],
        }
    return sorted(best.values(), key=lambda g: g["bearing"])


def _label_text(names, keep=2):
    """Name a stroke without letting a shared corner post run off the scope:
    two procedures spelled out, the rest counted."""
    names = sorted(names)
    if len(names) <= keep:
        return "/".join(names)
    return "/".join(names[:keep]) + f"+{len(names) - keep}"


def _dedupe(paths):
    """Collapse strokes that are already on the scope, remembering every
    procedure that claims each one.

    Two things overlap for real reasons, not from sloppiness.  Off Seattle's
    runway 34 the MONTN2 and SUMMA2 departures fly the same track to NEZUG
    before vectors take them different ways, and a fan of enroute entries
    that all leave the ring on the same radial is four copies of one line.
    Drawing either twice just thickens the ink; naming both on the one stroke
    is the truth.  Returns ``[(kind, [name, ...], pts), ...]``.
    """
    exact, order = {}, []
    for kind, name, pts in paths:
        key = (kind, tuple((round(p[0], 3), round(p[1], 3)) for p in pts))
        if key not in exact:
            exact[key] = [kind, [], pts]
            order.append(key)
        if name not in exact[key][1]:
            exact[key][1].append(name)

    # ...then drop any stroke that already lies on top of a longer one.  The
    # test is containment, not a shared first radial: thirteen Atlanta
    # departures all leave 27R over SLAWW and then go thirteen different
    # ways, and merging them on that first mile would erase twelve real
    # routes.  Only a stroke that never leaves another one is a duplicate.
    runs = sorted((exact[k] for k in order),
                  key=lambda r: -_length(r[2]))
    out = []
    for kind, names, pts in runs:
        for okind, onames, opts in out:
            if okind == kind and _covered_by(pts, opts):
                onames.extend(n for n in names if n not in onames)
                break
        else:
            out.append((kind, list(names), pts))
    return out


def _covered_by(pts, other, tol_nm=0.6):
    """Does every point of ``pts`` sit on the polyline ``other``?"""
    return all(_dist_to_path(p, other) <= tol_nm for p in pts)


def _dist_to_path(p, path):
    best = float("inf")
    for a, b in zip(path, path[1:]):
        best = min(best, _dist_to_seg(p, a, b))
    return best


def _dist_to_seg(p, a, b):
    """Great-circle-ish distance from a fix to a segment, in nautical miles.

    Equirectangular is plenty here: the segments are tens of miles, not
    thousands, and the answer only has to beat a sub-mile tolerance.
    """
    coslat = math.cos(math.radians(p[0]))
    px, py = (p[1] - a[1]) * coslat, p[0] - a[0]
    bx, by = (b[1] - a[1]) * coslat, b[0] - a[0]
    den = bx * bx + by * by
    t = 0.0 if den == 0.0 else max(0.0, min(1.0, (px * bx + py * by) / den))
    dx, dy = px - bx * t, py - by * t
    return math.hypot(dx, dy) * 60.0


def overlay_for(airport, active_rwy, entry_gates=None, exit_gates=None,
                radius_nm=SECTOR_EDGE_NM, kinds=("STAR", "SID"),
                declutter=True):
    """Drawable geometry for the procedures feeding today's runway:

        {"paths":  [(kind, name, [(lat, lon), ...]), ...],
         "labels": [(lat, lon, name, kind, vectors), ...],
         "plans":  [plan, ...]}          # what's actually shown, for chips

    ``kind`` is "STAR" or "SID" so the caller can paint arrivals and depar-
    tures apart, and ``name`` rides along so a stroke can answer for itself
    when the pointer lands on it.

    Only SIDs and STARs are drawn — the approach's final segment is already
    the localizer the scope paints.

    The declutter ties back to the sector's gates: a busy field lists dozens
    of procedures, but a controller only has up the ones on their own flows,
    so each entry gate keeps the STAR whose gate lies nearest it, each exit
    gate the nearest SID.  With ``declutter=False`` every serving procedure
    is drawn — the raw plate, for studying a field.

    Fixes past ``radius_nm`` are clipped to the terminal area the scope
    shows, as runs rather than as surviving points, so nothing is ever joined
    across a gap it doesn't fly.
    """
    alat, alon = airport["lat"], airport["lon"]
    plans = [p for p in plans_for(airport, active_rwy, radius_nm)
             if p["kind"] in kinds]

    if declutter and (entry_gates or exit_gates):
        chosen, picked = [], set()

        def take(gates, kind):
            # A gate keeps the procedure that actually crosses it.  Matching
            # on mere nearness let a corner post no procedure uses adopt the
            # nearest orphan and draw its name a hundred miles away, which is
            # how Boston ended up with ROBUC3 lettered in open water off the
            # LFV gate.  A gate with nothing of its own stays bare — an empty
            # corner post is honest, a mislabelled one is not.
            for glat, glon in gates or []:
                cands = [p for p in plans if p["kind"] == kind
                         and p["name"] not in picked
                         and haversine_nm(glat, glon, p["gate"][0],
                                          p["gate"][1]) <= 1.0]
                for plan in cands:
                    picked.add(plan["name"])
                    chosen.append(plan)

        take(entry_gates, "STAR")
        take(exit_gates, "SID")
        plans = chosen

    paths, anchors = [], {}
    for plan in plans:
        drawn = False
        for run in _clip_runs(plan["spine"], alat, alon, radius_nm):
            paths.append((plan["kind"], plan["name"], run))
            drawn = True
        if drawn:
            # the name belongs on the corner post the procedure is named for,
            # which is a fix on its own trunk — so a label always has its own
            # stroke running through it, instead of floating at a fan prong,
            # at a bare point on the clip boundary, or at a procedure that
            # drew no line at all
            anchors[plan["name"]] = (plan["gate"], plan)
        for _v, pts in plan["branches"]:
            for run in _clip_runs(pts, alat, alon, radius_nm):
                paths.append((plan["kind"], plan["name"], run))

    paths = _dedupe(paths)

    # one label per anchor: procedures sharing a corner post read as one name
    grouped = {}
    for name, (end, plan) in anchors.items():
        key = (plan["kind"], round(end[0], 2), round(end[1], 2))
        slot = grouped.setdefault(key, [end, plan["kind"], [], False])
        slot[2].append(name)
        slot[3] = slot[3] or plan["vectors"]
    labels = [(end[0], end[1], _label_text(names), kind, vec)
              for end, kind, names, vec in grouped.values()]

    shown = {n for _k, names, _p in paths for n in names}
    return {"paths": [(k, "/".join(names), [(p[0], p[1]) for p in pts])
                      for k, names, pts in paths],
            "labels": labels,
            "plans": [p for p in plans if p["name"] in shown]}


def flow_path(airport, active_rwy, kind, rng):
    """One stitched polyline for a random SID/STAR serving the runway — the
    fix sequence a flight actually flies, ordered gate→field for an arrival
    and field→gate for a departure — as ``(name, [(lat, lon, floor_ft,
    ceil_ft, speed_kt), ...])`` or None; the last three carry each fix's
    crossing restriction (any None) so the flight can descend/climb via it.

    Used to fly the uncontrolled traffic of neighbouring fields down their
    own procedures.
    """
    want = "STAR" if kind == "arrival" else "SID"
    plans = [p for p in plans_for(airport, active_rwy) if p["kind"] == want]
    if not plans:
        return None
    order = list(range(len(plans)))
    rng.shuffle(order)
    field = (airport["lat"], airport["lon"], None, None, None)
    for idx in order:
        plan = plans[idx]
        branch = ()
        if plan["branches"]:
            branch = plan["branches"][rng.randrange(len(plan["branches"]))][1]
        pts = (_splice(branch, plan["spine"]) if want == "STAR"
               else _splice(plan["spine"], branch))
        pts = [(p[0], p[1], p[3], p[4], p[5]) for p in pts]
        if want == "STAR":
            if haversine_nm(pts[-1][0], pts[-1][1],
                            airport["lat"], airport["lon"]) > 1.0:
                pts.append(field)
        elif haversine_nm(pts[0][0], pts[0][1],
                          airport["lat"], airport["lon"]) > 1.0:
            pts.insert(0, field)
        if len(pts) >= 2:
            return plan["name"], pts
    return None


def find_named(icao, name):
    """The raw procedure record at a field with this name, or None.

    Matching is forgiving about the revision number the way a controller is:
    a plate is reissued as CDOGG5 the month after you learned CDOGG4, and
    "via CDOGG" should still mean the arrival everyone calls CDOGG.
    """
    want = (name or "").strip().upper()
    if not want:
        return None
    procs = procedures_for(icao)
    for proc in procs:
        if proc["n"].upper() == want:
            return proc
    stem = want.rstrip("0123456789")
    if stem and stem != want:
        return None                    # a number was given; it has to be right
    matches = [p for p in procs
               if p["n"].upper().rstrip("0123456789") == want
               and p["k"] in ("SID", "STAR")]
    return matches[0] if len(matches) == 1 else None


def procedures_through(icao, fix):
    """Every SID/STAR at a field that flies over a named fix, as
    ``[(name, kind), ...]`` — what lets an unfamiliar name in a clearance be
    answered with the procedure it belongs to rather than a shrug."""
    want = (fix or "").strip().upper()
    out = []
    for proc in procedures_for(icao):
        if proc["k"] not in ("SID", "STAR"):
            continue
        for tr in proc["t"]:
            if any(leg["f"] == want for leg in tr["legs"]):
                out.append((proc["n"], proc["k"]))
                break
    return out


def join_plan(airport, active_rwy, proc, ref, hdg=None,
              max_join_nm=45.0):
    """Where an aircraft at ``ref`` would pick up a published procedure.

    Returns ``{"nav": [...], "join": ident, "reason": None}`` on success, or
    ``{"nav": [], "reason": ..., ...}`` with enough detail for the pilot to
    say something useful back.

    A controller does not spear an aeroplane into the middle of an arrival.
    They send it to a fix it can still make — one *ahead* of it — and clear
    it to descend via from there.  So the join is the **earliest fix on the
    procedure the aircraft has not already passed**, which means an aircraft
    that joins flies as much of the procedure as its position allows, and one
    that's already inside the last fix is told so by name instead of being
    quietly teleported back out.  Reasons:

      ``"runway"``   nothing of it serves the runway in use
      ``"far"``      the nearest joinable fix is too far off (``dist_nm``)
      ``"behind"``   none of it is ahead of them; ``nearest`` names the
                     closest fix and ``at_end`` says whether they have flown
                     past the end of it rather than never been lined up
    """
    if isinstance(proc, str):
        name = proc
    else:
        name = proc.get("n") or proc.get("name")
    plan = find_plan(airport, active_rwy, name)
    if plan is None:
        return {"nav": [], "reason": "runway"}
    star = plan["kind"] == "STAR"

    # the whole routing, outermost first for an arrival: the entry that comes
    # nearest the aeroplane, then the trunk
    best = None
    for _v, pts in (plan["branches"] or [(None, [])]):
        seq = (_splice(pts, plan["spine"]) if star
               else _splice(plan["spine"], pts))
        if not seq:
            continue
        near = min(haversine_nm(ref[0], ref[1], p[0], p[1]) for p in seq)
        if best is None or near < best[0]:
            best = (near, seq)
    if best is None:
        return {"nav": [], "reason": "runway"}
    seq = best[1]

    here = haversine_nm(ref[0], ref[1], airport["lat"], airport["lon"])

    def ahead(p):
        """Is this fix in front of the aeroplane, and does flying to it make
        progress along the procedure?

        Two separate questions, and both have to be yes.  The turn matters —
        you can't join what's behind you — but so does direction of travel:
        an arrival already over the field is pointed *at* its own STAR's
        entry fix forty miles out, and clearing it "direct RADDY, descend
        via" would fly it away from the airport down an arrival it has
        already finished.  A little backtrack to catch a gate is normal; a
        reversal is not.  Without a heading, treat anything as reachable —
        the caller does its own turn check.
        """
        if hdg is not None:
            rel = abs((bearing_to(ref[0], ref[1], p[0], p[1]) - hdg + 180.0)
                      % 360.0 - 180.0)
            if rel > 100.0:
                return False
        d = haversine_nm(airport["lat"], airport["lon"], p[0], p[1])
        return d <= here + 12.0 if star else d >= here - 12.0

    for i, p in enumerate(seq):
        if not ahead(p):
            continue
        d = haversine_nm(ref[0], ref[1], p[0], p[1])
        if d > max_join_nm:
            continue
        return {"nav": [(q[0], q[1], q[3], q[4], q[5]) for q in seq[i:]],
                "join": p[2], "reason": None,
                "dist_nm": d, "plan": plan}

    # nothing joinable — say which way it failed, and name the fix that would
    reachable = [p for p in seq if ahead(p)]
    if reachable:
        nearest = min(reachable,
                      key=lambda p: haversine_nm(ref[0], ref[1], p[0], p[1]))
        return {"nav": [], "reason": "far", "join": nearest[2], "plan": plan,
                "dist_nm": haversine_nm(ref[0], ref[1],
                                        nearest[0], nearest[1])}
    # pointed away from all of it.  Being *past* the end is a different
    # mistake from never having been lined up, and worth saying differently:
    # one is "we're inside AUBRN", the other "we're not positioned for it".
    idx = min(range(len(seq)),
              key=lambda i: haversine_nm(ref[0], ref[1], seq[i][0], seq[i][1]))
    return {"nav": [], "reason": "behind", "nearest": seq[idx][2],
            "at_end": idx >= len(seq) - 2, "plan": plan}



