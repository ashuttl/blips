"""Vendored procedures: the raw CIFP compiles to one honest picture per
runway, the overlay stays bounded and tied to the sector gates, and fields
off the CIFP simply have none."""

import random

from blips._airports import find_airport
from blips._geo import haversine_nm
from blips.game.procedures import (
    approach_ends, approach_to, find_named, flow_path, join_plan,
    overlay_for, plans_for, procedures_for, procedures_through,
)


def _gates(icao):
    from blips.game.sim import build_sector
    ap = find_airport(icao)
    s = build_sector(ap)
    return (ap, s, [s["fixes"][n] for n in s["entries"]],
            [s["fixes"][n] for n in s["exits"]])


def test_us_field_has_named_procedures():
    procs = procedures_for("KPWM")
    names = {p["n"] for p in procs}
    assert "CDOGG4" in names                 # a real KPWM arrival
    kinds = {p["k"] for p in procs}
    assert {"SID", "STAR", "APPCH"} <= kinds


def test_ex_cifp_field_has_no_procedures():
    # Heathrow is off the FAA CIFP — no procedures, empty overlay, no crash.
    assert procedures_for("EGLL") == []
    ap = find_airport("egll")
    ov = overlay_for(ap, "27", entry_gates=[(ap["lat"], ap["lon"])])
    assert ov["paths"] == [] and ov["labels"] == [] and ov["plans"] == []


def test_overlay_resolves_real_fix_coordinates():
    ap, s, eg, xg = _gates("KPWM")
    ov = overlay_for(ap, s["rwy"], entry_gates=eg, exit_gates=xg)
    assert ov["paths"] and ov["labels"]
    assert all(kind in ("STAR", "SID") for kind, _name, _pts in ov["paths"])
    # every drawn point is a real place near the field, not a stray global dup
    for _kind, _name, pts in ov["paths"]:
        for lat, lon in pts:
            assert haversine_nm(ap["lat"], ap["lon"], lat, lon) <= 79.0


def test_overlay_stays_bounded_at_a_busy_field():
    # LAX lists dozens of procedures; the gate declutter holds the drawn set
    # to about one per gate, so the scope never turns to spaghetti.
    ap, s, eg, xg = _gates("KLAX")
    assert len(procedures_for("KLAX")) > 40
    ov = overlay_for(ap, s["rwy"], entry_gates=eg, exit_gates=xg)
    assert len(ov["labels"]) <= len(eg) + len(xg)


def test_gates_declutter_never_empties_a_real_field():
    # A hard distance cutoff once zeroed out ORD; nearest-per-gate must not.
    ap, s, eg, xg = _gates("KORD")
    ov = overlay_for(ap, s["rwy"], entry_gates=eg, exit_gates=xg)
    assert ov["labels"]


def test_flow_path_stitches_a_departure_from_the_field():
    ap = find_airport("KEWR")
    res = flow_path(ap, "22R", "departure", random.Random(3))
    assert res is not None
    _name, pts = res
    assert len(pts) >= 2
    assert haversine_nm(ap["lat"], ap["lon"],
                        pts[0][0], pts[0][1]) < 1.5   # off the field


def test_flow_path_stitches_an_arrival_to_the_field():
    ap = find_airport("KMDW")
    res = flow_path(ap, ap["rwys"][0]["le"][0], "arrival", random.Random(5))
    assert res is not None
    _name, pts = res
    assert haversine_nm(ap["lat"], ap["lon"],
                        pts[-1][0], pts[-1][1]) < 1.5  # ends at field


# -- what the compiled plan fixed -------------------------------------------

def test_a_departure_starts_on_the_runway():
    # The climb-out leg flies a heading and names no fix, so the stroke used
    # to begin at the first named waypoint — GORHM, thirteen miles off the
    # field — and the SID looked like it started in mid-air.
    ap = find_airport("KPWM")
    sid = next(p for p in plans_for(ap, "29")
               if p["kind"] == "SID" and p["name"] == "RBELA1")
    head = sid["spine"][0]
    assert haversine_nm(ap["lat"], ap["lon"], head[0], head[1]) < 2.0


def test_only_the_runway_in_use_draws():
    # RW34C/L/R differ only in the first fix off the threshold; matching on
    # the digits drew every SID three times and flew the wrong parallel.
    ap = find_airport("KSEA")
    for plan in plans_for(ap, "34R"):
        if plan["rwy"] and plan["rwy"].startswith("RW34") \
                and plan["rwy"] not in ("RW34B", "RWALL"):
            assert plan["rwy"] == "RW34R", plan["name"]
    idents = {f[2] for p in plans_for(ap, "34R") if p["kind"] == "SID"
              for f in p["spine"]}
    assert "CUSBU" not in idents and "NESOE" not in idents   # 34L / 34C


def test_every_label_has_a_stroke_under_it():
    # A procedure whose transitions all reduced to one point used to emit a
    # label and no line — a name floating in space, attached to nothing.
    for code, rwy in (("KSEA", "34R"), ("KLAX", "25R"), ("KDEN", "16R"),
                      ("KATL", "27R"), ("KPWM", "29")):
        ov = overlay_for(find_airport(code), rwy, declutter=False)
        named = set()
        for _kind, name, _pts in ov["paths"]:
            named.update(name.split("/"))
        for _la, _lo, label, _k, _v in ov["labels"]:
            for part in label.split("+")[0].split("/"):
                assert part in named, f"{code}: {part} labelled but not drawn"


def test_clipping_never_invents_a_leg():
    # Filtering out-of-range points and keeping the survivors in one list
    # joined two fixes with a line no aircraft flies — a 100 nm stroke where
    # the real clipped leg was 22.  Runs are split instead.
    ap = find_airport("KPWM")
    ov = overlay_for(ap, "29", declutter=False)
    for _kind, name, pts in ov["paths"]:
        for a, b in zip(pts, pts[1:]):
            assert haversine_nm(*a, *b) < 65.0, name


def test_the_common_route_filed_as_ALL_is_still_a_trunk():
    # ARINC files the common portion under a blank ident or the literal
    # "ALL"; reading only the blank one lost the trunk of half the dataset.
    ap = find_airport("KEWR")
    brand = next(p for p in plans_for(ap, "22R") if p["name"] == "BRAND1")
    idents = [f[2] for f in brand["spine"]]
    assert "RUUTH" in idents and "BRAND" in idents


def test_a_procedure_that_ends_in_vectors_says_so():
    # "NEZUG then radar vectors" is a real ending, not missing data.
    ap = find_airport("KSEA")
    montn = next(p for p in plans_for(ap, "34R") if p["name"] == "MONTN2")
    assert montn["vectors"]
    assert [f[2] for f in montn["spine"]][-1] == "NEZUG"


def test_shared_strokes_name_every_procedure_on_them():
    # MONTN2 and SUMMA2 fly the same track off 34 before vectors split them.
    ov = overlay_for(find_airport("KSEA"), "34R", declutter=False)
    labels = {lab for _la, _lo, lab, _k, _v in ov["labels"]}
    assert any("MONTN2" in x and "SUMMA2" in x for x in labels)


def test_distinct_routes_are_never_merged():
    # Thirteen Atlanta departures leave 27R over SLAWW and then go thirteen
    # different ways; collapsing them on that shared first mile would erase
    # twelve real routes.
    ov = overlay_for(find_airport("KATL"), "27R", declutter=False)
    labels = [lab for _la, _lo, lab, _k, _v in ov["labels"]]
    assert len(labels) >= 20
    assert all("+" not in lab for lab in labels)


# -- joining one ------------------------------------------------------------

def test_a_procedure_is_gated_at_the_fix_it_is_named_for():
    # The CAMRN FIVE crosses CAMRN; the OOSHN FIVE crosses OOSHN.  Taking the
    # outermost fix of the trunk instead put Boston's OOSHN5 label under the
    # EURRO gate and WOONS2's under ORW, sixty-six miles out — both true of
    # the trunk, neither the post the arrival is named after.
    for code, rwy, want in (("KBOS", "33L", {"JFUND2": "JFUND",
                                             "OOSHN5": "OOSHN",
                                             "ROBUC3": "ROBUC",
                                             "WOONS2": "WOONS"}),
                            ("KJFK", "13R", {"CAMRN5": "CAMRN",
                                             "PUCKY1": "PUCKY"}),
                            ("KPWM", "29", {"CDOGG4": "CDOGG",
                                            "SCOGS4": "SCOGS"})):
        got = {p["name"]: p["gate"][2] for p in plans_for(find_airport(code),
                                                          rwy)}
        for name, gate in want.items():
            assert got.get(name) == gate, (code, name, got.get(name))


def test_a_corner_post_stays_inside_the_terminal_area():
    # Kennedy's PARCH is eighty miles out — real, but not a corner post.
    for code, rwy in (("KJFK", "13R"), ("KBOS", "33L"), ("KLAX", "25R"),
                      ("KSEA", "34R"), ("KATL", "27R")):
        for plan in plans_for(find_airport(code), rwy):
            assert plan["dist_nm"] <= 71.0, (code, plan["name"])


def test_an_arrival_that_stops_short_expects_vectors():
    # Boston's JFUND2 and OOSHN5 file an explicit terminator and ROBUC3 does
    # not, though all three end on a downwind fix a dozen miles out.  The odd
    # one out used to draw a stroke that simply stopped dead.
    plans = {p["name"]: p for p in plans_for(find_airport("KBOS"), "33L")}
    for name in ("JFUND2", "OOSHN5", "ROBUC3", "WOONS2"):
        assert plans[name]["vectors"], name


def test_every_drawn_label_sits_on_a_sector_gate():
    # A name floating with no corner post under it is the whole complaint.
    from blips.game.sim import Sim
    for code in ("KBOS", "KSEA", "KJFK", "KLAX", "KATL", "KTPA", "KPWM"):
        ap = find_airport(code)
        sim = Sim(ap, seed=3)
        gates = sim.sector["fixes"]
        ov = overlay_for(
            ap, sim.sector["rwy"],
            entry_gates=[gates[n] for n in sim.sector["entries"]],
            exit_gates=[gates[n] for n in sim.sector["exits"]])
        for lat, lon, label, _kind, _vec in ov["labels"]:
            assert any(haversine_nm(lat, lon, *pos) < 1.0
                       for pos in gates.values()), (code, label)


def test_join_takes_the_earliest_fix_still_ahead():
    ap = find_airport("KSEA")
    chins = find_named("KSEA", "CHINS5")
    # sitting on the gate, pointed at the field: joins at the top
    res = join_plan(ap, "34R", chins, (47.07, -121.0), hdg=290.0)
    assert res["nav"] and res["join"] in ("CHINS", "RADDY")


def test_join_refuses_when_pointed_away():
    ap = find_airport("KSEA")
    chins = find_named("KSEA", "CHINS5")   # everything on it lies SE
    res = join_plan(ap, "34R", chins, (47.95, -122.31), hdg=360.0)
    assert not res["nav"] and res["reason"] in ("behind", "far")


def test_join_never_flies_an_arrival_backwards():
    # Over the field, a STAR's entry fix forty miles out is still "ahead" of
    # a southbound aeroplane — but clearing it there would fly it away from
    # the airport down an arrival it has already finished.
    ap = find_airport("KSEA")
    chins = find_named("KSEA", "CHINS5")
    res = join_plan(ap, "34R", chins, (ap["lat"], ap["lon"]), hdg=160.0)
    assert res["join"] != "CHINS" and res["join"] != "RADDY"


def test_a_revision_number_is_forgiven():
    # A plate reissued as CDOGG5 shouldn't make "via CDOGG" a mystery.
    assert find_named("KPWM", "CDOGG")["n"] == "CDOGG4"
    assert find_named("KPWM", "cdogg4")["n"] == "CDOGG4"
    assert find_named("KPWM", "CDOGG9") is None    # a number given must fit


def test_a_gate_knows_which_procedure_owns_it():
    # RADDY is a Seattle corner post and a fix on the CHINS FIVE arrival —
    # "via RADDY" should be answerable, not a shrug.
    owners = dict(procedures_through("KSEA", "RADDY"))
    assert owners.get("CHINS5") == "STAR"


# -- approach availability: what the APPCH records actually say ---------------

def test_approach_ends_read_the_vendored_appch_records():
    ends = approach_ends("KTPA")
    assert "I" in ends["19R"] and "I" in ends["1L"]    # the parallels' ILSes
    assert approach_to("KTPA", "01L") == "ILS"         # leading zero forgiven
    assert approach_to("KTPA", "28") == "RNAV"         # the crossing runway


def test_an_rnav_only_field_says_so():
    # Palm Springs really has no ILS in any direction
    assert approach_to("KPSP", "31L") == "RNAV"
    assert approach_to("KPSP", "13R") == "RNAV"
    ends = approach_ends("KPSP")
    assert not any(kinds & {"I", "L"} for kinds in ends.values())


def test_fields_the_data_cannot_speak_for_stay_unknown():
    assert approach_ends("EGLL") is None       # off the CIFP entirely
    assert approach_to("EGLL", "27L") is None
    assert approach_ends("KADW") is None       # military: DoD FLIP, not CIFP
    assert approach_ends("KASE") is None       # Aspen only circles in
    # ...and an end that simply has nothing straight-in reads as empty,
    # which is knowledge, not ignorance
    assert approach_to("KTEB", "1") == ""
