"""Vendored procedures: real fixes resolve, the overlay stays bounded and
tied to the sector gates, and fields off the CIFP simply have none."""

import random

from blips._airports import find_airport
from blips._geo import haversine_nm
from blips._procedures import flow_path, overlay_for, procedures_for


def _gates(icao):
    from blips._sim import build_sector
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
    assert ov == {"paths": [], "labels": []}


def test_overlay_resolves_real_fix_coordinates():
    ap, s, eg, xg = _gates("KPWM")
    ov = overlay_for(ap, s["rwy"], entry_gates=eg, exit_gates=xg)
    assert ov["paths"] and ov["labels"]
    assert all(kind in ("STAR", "SID") for kind, _pts in ov["paths"])
    # every drawn point is a real place near the field, not a stray global dup
    for _kind, pts in ov["paths"]:
        for lat, lon in pts:
            assert haversine_nm(ap["lat"], ap["lon"], lat, lon) <= 61.0


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
    assert haversine_nm(ap["lat"], ap["lon"], *pts[0]) < 1.5   # off the field


def test_flow_path_stitches_an_arrival_to_the_field():
    ap = find_airport("KMDW")
    res = flow_path(ap, ap["rwys"][0]["le"][0], "arrival", random.Random(5))
    assert res is not None
    _name, pts = res
    assert haversine_nm(ap["lat"], ap["lon"], *pts[-1]) < 1.5  # ends at field
