"""The sim: honest kinematics, ILS geometry, separation, sector rules."""

import math
import re

import pytest

from blips._airports import find_airport
from blips._geo import (
    advance, bearing_to, cross_along_track, haversine_nm, turn_delta,
)
from blips.game.sim import (
    PERF, SECTOR_NM, SEP_FT, WX_CLEAR, WX_DEVIATE, Sim, _commandable,
    _controlled, _flow_ends, build_sector, say_runway,
)


@pytest.fixture()
def sim():
    """A quiet TPA sector: the spawner is parked so tests own the traffic."""
    s = Sim(find_airport("tpa"), seed=1)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = 1e9
    s._balloon_event = 2        # the ambient sky stays parked too
    s._next_abnormal = 1e9      # nothing goes wrong unless a test says
    s.flow_hold_p = 0.0         # a forced flow change always turns the field
    s.hearback_p = 0.0          # pilots hear perfectly unless a test says
    s.wind = (360.0, 0.0)       # calm air unless a test brings weather
    s._aloft = (0.0, 1.0)       # one wind at every altitude unless layered
    s.react_s = (0.0, 0.0)      # and their hands are instant
    s.aircraft.clear()          # drop the pre-populated shift traffic
    s.trails.clear()
    s.radio.clear()
    return s


def _arrival(sim, callsign="DAL100", lat=None, lon=None, alt=12000.0,
             hdg=180.0, ias=250.0, actype="B738"):
    ap = sim.airport
    ac = sim._base(callsign, actype,
                   ap["lat"] + 0.5 if lat is None else lat,
                   ap["lon"] if lon is None else lon, alt, hdg, ias)
    ac.update(plan="arrival", fix=sim.sector["entries"][0],
              rwy=sim.sector["rwy"], thr=sim.sector["thr"],
              course=sim.sector["course"])
    sim.aircraft.append(ac)
    return ac


def _run(sim, seconds, step=1.0):
    t = sim._last_tick if sim._last_tick is not None else sim.start
    sim._last_tick = t
    for _ in range(int(seconds / step)):
        t += step
        sim.tick(t)
    return t


# -- geometry -----------------------------------------------------------------

def test_turn_delta_directions():
    assert turn_delta(360, 90, "r") == 90
    assert turn_delta(360, 90, "l") == -270      # the long way round, as told
    assert turn_delta(10, 350, None) == -20      # sim's own flying: shortest


def test_cross_along_track():
    # 10 nm due south of a threshold, course 360: on centreline, 10 out
    thr = (28.0, -82.0)
    pt = advance(*thr, 180.0, 10.0)
    cross, along = cross_along_track(pt[0], pt[1], *thr, 360.0)
    assert abs(cross) < 0.05
    assert 9.9 < along < 10.1


# -- kinematics ---------------------------------------------------------------

def test_standard_rate_turn_takes_real_time(sim):
    ac = _arrival(sim, hdg=360.0)
    sim.command("100 r 90")
    _run(sim, 10)
    assert 25 < ac["hdg"] < 35          # 3°/s: ~30° after 10 s
    _run(sim, 25)
    assert ac["hdg"] == 90.0            # arrived and captured


def test_forced_left_turn_goes_the_long_way(sim):
    ac = _arrival(sim, hdg=360.0)
    sim.command("100 l 90")             # left to 090: 270° of turning
    _run(sim, 45)
    assert 220 < ac["hdg"] < 250        # mid-turn, heading down through west
    _run(sim, 60)
    assert ac["hdg"] == 90.0


def test_climb_descend_capture(sim):
    ac = _arrival(sim, alt=12000.0)
    sim.command("100 d 60")
    _run(sim, 60)
    assert ac["vrate"] < 0
    assert 9000 < ac["alt"] < 11500     # ~2,100 fpm on a B738
    _run(sim, 240)
    assert ac["alt"] == 6000.0
    assert ac["vrate"] == 0


def test_speed_changes_take_time(sim):
    ac = _arrival(sim, ias=280.0)
    sim.command("100 rs 210")
    _run(sim, 20)
    assert 240 < ac["ias"] < 265        # 1.2 kt/s
    _run(sim, 60)
    assert ac["ias"] == 210.0
    assert ac["gs"] > ac["ias"]         # TAS buys ground speed at altitude


# -- the frequency ------------------------------------------------------------

def test_wrong_verbs_get_a_puzzled_pilot(sim):
    _arrival(sim, alt=12000.0, ias=250.0)
    assert "unable climb" in sim.command("100 c 100")
    assert "unable descend" in sim.command("100 d 140")
    assert "unable reduce" in sim.command("100 rs 280")
    assert "unable increase" in sim.command("100 is 210")


def test_speed_envelope(sim):
    _arrival(sim)
    assert "unable" in sim.command("100 rs 120")   # below clean minimum
    ok = sim.command("100 rs 210")
    assert "reduce speed two one zero" in ok.lower()


def test_unknown_fix_and_wrong_plan(sim):
    _arrival(sim)
    assert "unfamiliar with ZZZZZ" in sim.command("100 dct zzzzz")
    assert "arrival" in sim.command("100 ho")       # arrivals aren't handed off


def test_unable_declines_a_standing_request(sim):
    """`unable` answers a pilot's ask: you key the decline spelled out, they
    roger and hold what they've got.  Direct and altitude asks read back
    differently — an altitude decline restates the level they'll keep."""
    ac = _arrival(sim, callsign="FFT2615", alt=24000.0)
    ac["phase"] = "cruise"
    # a direct request, declined
    ac["req"] = {"what": "direct", "fix": "EFLOW"}
    line = sim.command("2615 unable")
    assert line == "Roger, Frontier Flight 2615."
    assert sim.radio[-2][1] == "Frontier Flight 2615, unable direct EFLOW."
    assert ac.get("req") is None                 # the ask is settled
    # a lower request, declined: they read back the level they'll maintain
    ac["req"] = {"what": "lower"}
    line = sim.command("2615 unable")
    assert line == "Roger, maintaining flight level two four zero, Frontier Flight 2615."
    assert sim.radio[-2][1] == "Frontier Flight 2615, unable lower."


def test_unable_with_no_request_is_puzzled(sim):
    ac = _arrival(sim, callsign="DAL100")
    ac["phase"] = "cruise"
    assert "hasn't asked for anything" in sim.command("100 unable")


def test_granting_clears_the_standing_request(sim):
    """Sending them where they asked settles the ask, so a later `unable`
    has nothing to decline."""
    ac = _arrival(sim, callsign="DAL100", alt=24000.0)
    ac["phase"] = "cruise"
    ac["req"] = {"what": "lower"}
    sim.command("100 d 100")
    assert ac.get("req") is None
    assert "hasn't asked for anything" in sim.command("100 unable")


def test_readback_uses_telephony(sim):
    _arrival(sim, callsign="RPA5655")
    line = sim.command("5655 r 270")
    assert line == "Turn right heading two seven zero, Brickyard 5655."


def test_command_echoes_your_own_transmission(sim):
    """The log carries your keyed transmission (kind 'tx') just above the
    pilot's readback, spelled out the same way — the readable half of the
    exchange."""
    _arrival(sim, callsign="DAL100")
    sim.command("100 d 60 rs 210")
    kinds = [k for _t, _l, k in sim.radio]
    assert kinds[-2:] == ["tx", "readback"]
    tx = sim.radio[-2][1]
    assert tx == ("Delta 100, descend and maintain six thousand, "
                  "reduce speed two one zero.")
    # controller leads with the callsign, the pilot trails it — a clean copy
    # carries the same instruction body both ways, read down the two
    assert sim.radio[-1][1] == ("Descend and maintain six thousand, "
                                "reduce speed two one zero, Delta 100.")


def test_echo_reveals_a_mishear(sim):
    """When the pilot mishears, your echo and their readback diverge by the
    one garbled number — the whole point of showing your own side."""
    _arrival(sim, callsign="DAL100")
    sim.hearback_p = 1.0            # this transmission will be misheard
    sim._elapsed = 300.0           # (mishears only start after the warm-up)
    sim.command("100 l 180")
    tx, readback = sim.radio[-2][1], sim.radio[-1][1]
    assert "one eight zero" in tx           # what you said
    assert "one eight zero" not in readback  # what they flew
    assert sim.hearbacks == 1


def test_pilot_lines_carry_a_voice(sim):
    """Pilot transmissions hand the speaker a callsign; the controller's own
    'tx' echo never does, so it's never spoken aloud."""
    spoken = []

    class _Spy:
        def speak(self, line, key):
            spoken.append((key, line))

    sim.speaker = _Spy()
    _arrival(sim, callsign="DAL100")
    sim.command("100 r 270")
    # exactly one call spoke — the pilot's readback, in that flight's voice.
    # The 'tx' echo above it (radio[-2]) went to the log only.
    assert spoken == [("DAL100", sim.radio[-1][1])]
    assert sim.radio[-2][2] == "tx"


# -- the approach -------------------------------------------------------------

def test_ils_capture_glideslope_landing(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    # 12 nm out on the extended centreline, 3,000 ft, pointed at the runway
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    line = sim.command("100 i")
    assert "cleared ils runway" in line.lower()
    _run(sim, 60)
    assert ac["phase"] == "established"
    _run(sim, 600)
    assert ac not in sim.aircraft       # flown down the slope and landed
    assert sim.landed == 1
    assert sim.score == 100
    assert sim.ledger[-1] == "DAL100 down · +100"   # quietly, in the ledger


def test_vector_off_the_approach_cancels_it(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course)
    sim.command("100 i")
    _run(sim, 60)
    assert ac["phase"] == "established"
    sim.command("100 l 270")            # break them off
    assert ac["phase"] == "cruise"


def test_ils_capture_from_a_thirty_degree_intercept(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    # abeam the localizer at 10 nm, 2 nm right of centreline, cutting
    # across at 30° — the everyday intercept must capture without fuss
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 10.0)
    lat, lon = advance(lat, lon, (course + 90.0) % 360.0, 2.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0,
                  hdg=(course - 30.0) % 360.0, ias=180.0)
    sim.command("100 i")
    _run(sim, 180)
    assert ac["phase"] in ("established", "landed") or ac not in sim.aircraft


def test_hopeless_ils_geometry_is_refused(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 10.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0,
                  hdg=(course + 180.0) % 360.0)   # flying away down final
    assert "pointed away" in sim.command("100 i")
    ac["lat"], ac["lon"] = advance(*thr, course, 0.2)  # past the threshold
    ac["hdg"] = ac["tgt_hdg"] = course
    assert "inside the marker" in sim.command("100 i")


def test_unstable_approach_goes_around(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    # cleared close-in, way too high: they'll capture, then bail
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 4.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=5000.0, hdg=course, ias=170.0)
    sim.command("100 i")
    _run(sim, 240)
    assert sim.busts == 0
    assert any("going around" in line for _t, line, _k in sim.radio)
    assert "go-around · −50" in sim.ledger
    assert ac in sim.aircraft           # back with you for another try
    assert ac["phase"] == "cruise"


def test_assigned_speed_rides_to_the_marker(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 14.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=4000.0, hdg=course, ias=190.0)
    sim.command("100 i")
    _run(sim, 5)
    assert ac["phase"] == "established"
    sim.command("100 rs 190")
    for _ in range(400):                # walk down final to short of five
        _run(sim, 1)
        _cross, along = cross_along_track(ac["lat"], ac["lon"], *thr, course)
        if along < 5.5:
            break
    assert ac["tgt_ias"] == 190.0       # theirs to keep, not force-slowed at six
    assert not any("slowing to final approach speed" in line
                   for _t, line, _k in sim.radio)
    _run(sim, 30)                       # through five miles: announced once
    assert any("slowing to final approach speed" in line
               for _t, line, _k in sim.radio)
    assert ac["tgt_ias"] == float(ac["perf"][2])
    _run(sim, 300)
    assert sim.landed == 1              # and the landing still works out


def test_fast_on_final_is_refused_with_the_real_tool(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3500.0, hdg=course, ias=190.0)
    sim.command("100 i")
    _run(sim, 5)
    assert ac["phase"] == "established"
    line = sim.command("100 is 220")
    assert "unable two two zero on final" in line
    assert "one niner zero to the marker" in line
    assert ac["tgt_ias"] <= 190.0       # nothing quietly took the number
    assert "one niner zero" in sim.command("100 rs 190")   # the offered tool


def test_hold_orbits_and_vectors_cancel_it(sim):
    ac = _arrival(sim, alt=8000.0)
    line = sim.command("100 hold")
    assert "hold present position, right turns" in line.lower()
    start = (ac["lat"], ac["lon"])
    _run(sim, 300)
    assert ac["phase"] == "hold"
    # still near where they started after five minutes of orbiting
    assert haversine_nm(ac["lat"], ac["lon"], *start) < 6.0
    sim.command("100 r 90")
    assert ac["phase"] == "cruise"


def test_hold_at_a_fix(sim):
    fix = sim.sector["entries"][0]
    spot = sim.sector["fixes"][fix]
    ac = _arrival(sim, alt=9000.0)
    line = sim.command(f"100 hold {fix.lower()}")
    assert f"hold at {fix}".lower() in line.lower()
    _run(sim, 900)
    assert haversine_nm(ac["lat"], ac["lon"], *spot) < 6.0


def test_uncleaered_arrival_blows_through_final(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    # crossing the localizer at 90°, never cleared: should sail through
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 10.0)
    lat, lon = advance(lat, lon, (course + 270.0) % 360.0, 3.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=4000.0,
                  hdg=(course + 90.0) % 360.0)
    _run(sim, 120)
    assert ac["phase"] == "cruise"      # nobody flies the ILS uninvited
    cross, _ = cross_along_track(ac["lat"], ac["lon"], *thr, course)
    assert abs(cross) > 1.0             # out the other side


# -- separation ---------------------------------------------------------------

def test_separation_bust_scored_once_until_resolved(sim):
    a = _arrival(sim, callsign="DAL100", alt=5000.0, hdg=90.0)
    b = _arrival(sim, callsign="SWA200", alt=5400.0, hdg=90.0,
                 lat=a["lat"], lon=a["lon"] + 0.02)
    _run(sim, 3)
    assert sim.busts == 1
    assert a["emergency"] and b["emergency"]       # both blips flash red
    assert sim.ledger.count("loss of separation · −500") == 1
    _run(sim, 30)
    assert sim.busts == 1               # same conflict, still one bust
    sim.command("200 c 100")            # climb SWA200 away
    _run(sim, 240)
    assert not a["emergency"]
    before = sim.busts
    sim.command("200 d 54")             # and shove them back together
    _run(sim, 240)
    assert sim.busts == before + 1      # a fresh bust after resolution


def test_vertical_separation_is_enough(sim):
    a = _arrival(sim, callsign="DAL100", alt=5000.0)
    _arrival(sim, callsign="SWA200", alt=6100.0,
             lat=a["lat"], lon=a["lon"])
    _run(sim, 3)
    assert sim.busts == 0


# -- the sector ---------------------------------------------------------------

def test_sector_is_deterministic_per_airport(monkeypatch):
    ap = find_airport("tpa")
    s1, s2 = build_sector(ap), build_sector(ap)
    assert s1["fixes"] == s2["fixes"]   # TPA's corner posts never move
    assert s1["rwy"] == s2["rwy"]
    assert 6 <= len(s1["fixes"]) <= 10
    assert 3 <= len(s1["entries"]) and 3 <= len(s1["exits"])
    for lat, lon in s1["fixes"].values():
        d = haversine_nm(lat, lon, ap["lat"], ap["lon"])
        assert 11.0 < d < 76.0          # searched band, or a published post
    # and an unprofiled field can't tell the profile table exists: with
    # the table emptied outright, its sector and its opening wind come
    # out identical — the feature is invisible everywhere it isn't asked for
    from blips.game import profiles
    sea = find_airport("ksea")
    with_table = build_sector(sea)
    wind_with = Sim(sea, seed=3).wind
    monkeypatch.setattr(profiles, "PROFILES", {})
    assert build_sector(sea) == with_table
    assert Sim(sea, seed=3).wind == wind_with


def test_sector_gates_are_real_navaids_where_possible():
    # London is off the CIFP, so it still gates the old way: eight famous
    # VORs picked one per octant, no procedures involved.
    s = build_sector(find_airport("egll"))
    real = [n for n in s["fixes"] if len(n) <= 3]
    assert len(real) == 8
    assert "BKY" in s["fixes"]          # Barkway, the classic north gate


def test_us_gates_come_from_the_published_procedures():
    # A gate should be the fix the procedures actually use, so the name under
    # a corner post and the name in a `via` clearance are the same word.
    from blips.game.procedures import plans_for
    for code, want in (("kpwm", {"CDOGG", "SCOGS", "RBELA", "HSKEL"}),
                       ("ksea", {"CHINS", "HAWKZ", "MARNR"}),
                       ("kjfk", {"PUCKY"})):
        s = build_sector(find_airport(code))
        assert want <= set(s["fixes"]), f"{code}: {sorted(s['fixes'])}"
    # ...and every entry gate is the outer end of some real arrival
    ap = find_airport("kpwm")
    s = build_sector(ap)
    heads = {p["gate"][2] for p in plans_for(ap, s["rwy"])}
    assert heads & set(s["entries"])


def test_gates_survive_a_flow_change():
    # Real corner posts don't move when the wind turns; the procedures over
    # them change.  Anything else would strand a hold or a departure's exit.
    sim = Sim(find_airport("ksea"), seed=4)
    before = dict(sim.sector["fixes"])
    sim._next_flow = 0.0
    sim._flow_tick(1.0)
    assert sim.sector["fixes"] == before


def test_us_gates_are_all_real_fixes_or_navaids():
    # The CIFP covers the US, so a US field invents no gates at all — every
    # corner post is a real navaid or a real named waypoint (no more ICTEB).
    from blips._airports import load_fixes, load_navaids
    real = ({n["id"] for n in load_navaids()} | {f["id"] for f in load_fixes()})
    for code in ("kpwm", "ktpa", "kjfk", "klax"):
        s = build_sector(find_airport(code))
        invented = [g for g in s["fixes"] if g not in real]
        assert not invented, f"{code} invented gates: {invented}"


def test_ex_cifp_field_still_degrades_to_synthesized_gates():
    # Sydney is off the CIFP's coverage: navaids fill what octants they can,
    # the rest fall back to synthesized fixes rather than leaving a hole.
    from blips._airports import load_fixes, load_navaids
    real = ({n["id"] for n in load_navaids()} | {f["id"] for f in load_fixes()})
    s = build_sector(find_airport("yssy"))
    assert len(s["fixes"]) == 8
    assert any(g not in real for g in s["fixes"])   # some are synthesized


# -- parallel runways: segregated mode ----------------------------------------

def test_parallel_pair_lands_the_longer_and_departs_the_other():
    # TPA: 19R/01L (11,002 ft) lands, 19L/01R (8,300 ft) departs; the
    # crossing 10/28 never qualifies.  Detected from the data, not a table.
    ap = find_airport("tpa")
    s = build_sector(ap)
    assert s["parallel"]
    arr_ids = {ap["rwys"][0]["le"][0], ap["rwys"][0]["he"][0]}
    dep_ids = {ap["rwys"][1]["le"][0], ap["rwys"][1]["he"][0]}
    assert s["rwy"] in arr_ids                  # the longer parallel lands
    assert s["dep_rwy"] in dep_ids              # the shorter departs
    assert abs(turn_delta(s["course"], s["dep_course"])) < 10.0  # one flow
    # SEA runs three parallels: 16L (11,901 ft) lands, 16C is next longest
    sea = build_sector(find_airport("ksea"))
    assert sea["parallel"]
    assert sea["rwy"] in ("16L", "34R")
    assert sea["dep_rwy"] in ("16C", "34C")
    # EGLL — the operation this mode is named for
    assert build_sector(find_airport("egll"))["parallel"]


def test_single_and_crossing_runway_fields_are_unchanged():
    # PWM's 11/29 and 18/36 cross; BIL's 10L and 07 diverge 28° — neither
    # is a pair, so both ends read the same runway, as they always did.
    for code in ("kpwm", "kbil"):
        s = build_sector(find_airport(code))
        assert not s["parallel"], code
        assert s["dep_rwy"] == s["rwy"]
        assert s["dep_thr"] == s["thr"]
        assert s["dep_course"] == s["course"]


# -- airport operations profiles ----------------------------------------------

class _Gale:
    """A scriptable stand-in for the sim's rng: a wind speed you dial,
    a queue of random() rolls, everything else down the middle."""
    def __init__(self, kt, rolls=()):
        self.kt, self.rolls = kt, list(rolls)

    def randint(self, a, b):
        return self.kt

    def uniform(self, a, b):
        return (a + b) / 2.0

    def random(self):
        return self.rolls.pop(0) if self.rolls else 1.0

    def choice(self, seq):
        return seq[0]


def test_calm_wind_takes_a_profiled_field_to_its_preferred_end():
    # Heathrow's westerly preference: light air doesn't get to turn the
    # field easterly — and a wind with an opinion still does.
    ap = find_airport("egll")
    s = Sim(ap, seed=1)
    assert s._prefer == "he"                        # the 27s
    s.sector.update(_flow_ends(ap, "le"))           # parked easterly
    s.rng = _Gale(6)                                # under the calm line
    s._set_wind(turn_home=True)
    assert s.sector["rwy"] == "27R" and s.sector["dep_rwy"] == "27L"
    s.sector.update(_flow_ends(ap, "le"))
    s.rng = _Gale(15)                               # a real easterly
    s._set_wind(turn_home=True)
    assert s.sector["rwy"] == "09L"                 # preference ignored


def test_flow_change_rolls_lean_back_toward_the_preference():
    # most winds that would turn Heathrow easterly just shift in place:
    # the letter advances, the 27s keep the flow, nobody is re-vectored
    s = Sim(find_airport("egll"), seed=1)
    s.sector.update(_flow_ends(s.airport, "he"))    # on the westerlies
    rev = s.sector_rev
    s.rng = _Gale(15, rolls=(0.9, 0.5))   # no hold; the habit wins the lean
    s._next_flow = 0.0
    s._flow_tick(1.0)
    assert s.sector["rwy"] == "27R" and s.sector_rev == rev
    s.rng = _Gale(15, rolls=(0.9, 0.9))   # no hold; the habit loses
    s._next_flow = 0.0
    s._flow_tick(1.0)
    assert s.sector["rwy"] == "09L" and s.sector_rev == rev + 1


def test_profile_lands_tampas_shorter_parallel_in_south_flow():
    # Tampa's runway-use program, not the tape measure: south flow rolls
    # its turbojet departures down the long 19R and lands 19L — the
    # 8,300-footer — which longest-lands would get exactly backwards.
    ap = find_airport("ktpa")
    south = _flow_ends(ap, "le")
    assert south["rwy"] == "19L" and south["dep_rwy"] == "19R"
    assert south["dep_len"] == 11002        # the long one departs
    assert south["parallel"]
    north = _flow_ends(ap, "he")            # ...and north flow lands it
    assert north["rwy"] == "01L" and north["dep_rwy"] == "01R"


def test_profiled_initial_rides_the_loa_invariant():
    # Heathrow SIDs cap the climb at 6,000 whatever the elevation says,
    # and Farnborough's number is derived from that same figure — the
    # thousand-foot split survives the override by construction.
    s = Sim(find_airport("egll"), seed=1)
    assert s._initial_alt() == 6000.0
    assert abs(s._initial_alt() - s._initial_alt(s.sector["sat"])) >= 1000.0


def test_pinned_satellites_land_where_the_profile_says():
    # PWM's pin agrees with the search — which is the point: it's now
    # documentation — and EGLL's names Farnborough outright.
    assert build_sector(find_airport("kpwm"))["sat_apt"]["icao"] == "KBXM"
    assert build_sector(find_airport("egll"))["sat_apt"]["icao"] == "EGLF"


def test_profiled_xr_menu_replaces_the_generic_pick():
    # centre's numbers at a curated field are the profile's, not the
    # seven/nine/eleven-above-the-field default
    s = Sim(find_airport("egll"), seed=2)
    s.aircraft.clear()
    for _ in range(40):
        s._spawn_departure()
    xrs = {a["xr"] for a in s.aircraft if "xr" in a}
    assert xrs and xrs <= {10000.0, 12000.0}


def test_departures_roll_the_departure_parallel(sim):
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    d_dep = haversine_nm(dep["lat"], dep["lon"], *sim.sector["dep_thr"])
    d_arr = haversine_nm(dep["lat"], dep["lon"], *sim.sector["thr"])
    assert d_dep < d_arr                        # off their own pavement
    assert abs(turn_delta(dep["hdg"], sim.sector["dep_course"])) < 0.5
    # and the check-in names the runway they actually rolled from
    assert any(f"off runway {say_runway(sim.sector['dep_rwy'])}" in line
               for _t, line, _k in sim.radio)


def test_ils_to_the_departure_parallel_teaches(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=180.0)
    line = sim.command(f"100 i {sim.sector['dep_rwy']}")
    assert "departing traffic today" in line
    assert f"landing {say_runway(sim.sector['rwy'])}" in line
    assert ac["phase"] == "cruise"              # no clearance issued
    # bare `i` is the arrival end, as ever
    line = sim.command("100 i")
    assert "cleared ils" in line.lower()
    assert ac["rwy"] == sim.sector["rwy"]


def test_flow_change_flips_both_parallel_ends_together(sim):
    before = (sim.sector["rwy"], sim.sector["dep_rwy"])
    sim._next_flow = 0.0
    sim._flow_tick(1.0)
    after = (sim.sector["rwy"], sim.sector["dep_rwy"])
    assert after[0] != before[0] and after[1] != before[1]
    assert after[0] != after[1]                 # still segregated
    assert abs(turn_delta(sim.sector["course"],
                          sim.sector["dep_course"])) < 10.0


def test_procedures_serve_their_own_parallel(sim):
    assert sim.sector["parallel"]
    assert sim._rwy_for("STAR") == sim.sector["rwy"]
    assert sim._rwy_for("SID") == sim.sector["dep_rwy"]


def test_closure_holds_approaches_but_not_departures_at_a_parallel_field(sim):
    sim._rwy_closed_until = sim._elapsed + 300.0
    _arrival(sim)
    assert "runway's closed" in sim.command("100 i")   # approaches refused
    sim._next_departure = 0.0                   # tower has one ready
    sim._spawn_tick(1.0)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 1


def test_closure_still_holds_departures_at_a_single_runway_field():
    s = Sim(find_airport("kpwm"), seed=4)
    s._next_arrival = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = 1e9
    s.aircraft.clear()
    s._rwy_closed_until = s._elapsed + 300.0
    s._next_departure = 0.0
    s._spawn_tick(1.0)
    assert not any(a["plan"] == "departure" for a in s.aircraft)


# -- honest approaches: the ILS is equipment, not pavement ---------------------

def _quiet(icao, seed=4):
    s = Sim(find_airport(icao), seed=seed)
    s.hearback_p = 0.0
    s.react_s = (0.0, 0.0)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = s._next_neighbor = 1e9
    s._balloon_event = 2
    s._next_abnormal = 1e9
    s.wind = (360.0, 0.0)
    s._aloft = (0.0, 1.0)
    s.aircraft.clear()
    s.radio.clear()
    return s


def test_ils_to_an_end_without_one_teaches_the_parallel_that_has_it():
    # Boston's 4L is RNAV-only; the ILS next door on 4R is the real answer
    s = _quiet("kbos")
    ac = _arrival(s)
    line = s.command("100 i 4L")
    assert "no ils to runway four left" in line.lower()
    assert "rnav approach" in line.lower()
    assert "the ils serves runway four right" in line.lower()
    assert ac["phase"] == "cruise"              # no clearance issued


def test_an_end_with_no_approach_at_all_is_refused():
    # Teterboro publishes nothing straight-in to runway 1 — and the sector
    # opens on 19, the only flow its plates serve
    s = _quiet("kteb")
    assert s.sector["rwy"] == "19"
    _arrival(s)
    line = s.command("100 i 1")
    assert "no instrument approach to runway one" in line.lower()
    assert "the ils serves runway" in line.lower()


def test_an_rnav_only_field_clears_the_rnav_by_name():
    # Palm Springs has no ILS anywhere: `i` clears the approach that
    # actually exists, by its own name, and it flies the same final
    s = _quiet("kpsp")
    thr, course = s.sector["thr"], s.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(s, lat=lat, lon=lon, alt=s.sector["elev"] + 3500.0,
                  hdg=course, ias=180.0)
    line = s.command("100 i")
    assert "cleared rnav runway" in line.lower()
    assert "ils" not in line.lower()
    assert ac["phase"] == "cleared"


def test_arrivals_land_the_parallel_with_the_ils():
    # Kennedy's longer 13R has no ILS of its own; the data lands arrivals
    # on 13L and rolls departures off 13R.  San Jose's ILSes live on the
    # shorter 12R/30L in both directions.
    s = build_sector(find_airport("kjfk"))
    assert (s["rwy"], s["dep_rwy"]) in (("13L", "13R"), ("31L", "31R"))
    sj = build_sector(find_airport("ksjc"))
    assert sj["rwy"] in ("12R", "30L")
    assert sj["dep_rwy"] in ("12L", "30R")


def test_flow_holds_at_a_field_with_one_ils():
    # Brunswick's only ILS points down 01R: the sector opens on it, and
    # when the wind turns the field doesn't — the broadcast just advances
    s = _quiet("kbxm")
    s.flow_hold_p = 0.0
    assert s.sector["rwy"] == "01R"
    letter = s._atis_n
    s._next_flow = 0.0
    s._flow_tick(1.0)
    assert s.sector["rwy"] == "01R"
    assert s._atis_n == letter + 1


# -- visual approaches: the field in sight, then the flying is theirs ----------

def _on_final(sim, callsign, actype, nm, established=True):
    """One arrival on the final approach course, established unless a
    test wants it fresh and still to be cleared."""
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, nm)
    ac = _arrival(sim, callsign=callsign, lat=lat, lon=lon,
                  alt=sim.airport["elev"] + nm * 300.0, hdg=course,
                  ias=170.0, actype=actype)
    if established:
        ac["phase"] = "established"
    return ac


def test_visual_sighted_cleared_lands_and_scores_like_an_ils(sim):
    # weather off is VMC everywhere: the field is sighted, the clearance
    # reads back in full, and the landing pays exactly what the ILS pays
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    line = sim.command("100 v")
    assert "field in sight" in line.lower()
    assert "cleared visual approach runway" in line.lower()
    _run(sim, 60)
    assert ac["phase"] == "established"
    assert any("turning final" in line for _t, line, _k in sim.radio)
    _run(sim, 600)
    assert ac not in sim.aircraft
    assert sim.landed == 1
    assert sim.score == 100


def test_visual_needs_the_field_in_sight(sim):
    # a heavy cell between them and the field is an honest negative
    # contact; when the weather moves off, the re-ask finds the field
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    sim.wx_sample = lambda lat, lon: 0.9
    line = sim.command("100 v")
    assert "not in sight" in line and "looking" in line
    assert ac["phase"] == "cruise"              # no clearance issued
    sim.wx_sample = None                        # the cell moves off: VMC
    line = sim.command("100 v")
    assert "field in sight" in line.lower()
    assert ac["phase"] == "cleared"


def test_a_crew_thats_looking_gets_a_better_second_look(sim):
    # "we're looking" is remembered: the same murk, the same roll, and
    # the re-ask succeeds because they've had windshield time on it
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    sim.wx_sample = lambda lat, lon: 0.4        # murk, not a wall
    sim.rng.random = lambda: 0.45               # a roll the first look misses
    assert "looking" in sim.command("100 v")
    line = sim.command("100 v")                 # still looking: better odds
    assert "field in sight" in line.lower()
    assert ac["phase"] == "cleared"


def test_visual_follows_the_traffic_and_the_gap_is_the_pilots(sim):
    # a Southwest 737 on final at 5 nm, the follower cleared 2.5 behind:
    # the readback names the traffic, and in-trail inside 3 nm is legal —
    # sighted traffic is visual separation, the pilot's to keep
    leader = _on_final(sim, "SWA1234", "B738", 5.0)
    follower = _on_final(sim, "DAL100", "B738", 7.5, established=False)
    line = sim.command("100 v")
    assert "following the southwest seven thirty-seven" in line.lower()
    assert leader["hex"] in follower["visual"]
    _run(sim, 30)
    assert follower["phase"] == "established"
    assert sim.busts == 0                       # 2.5 nm in trail, and legal


def test_visual_behind_a_heavy_still_pays_the_wake_miles(sim):
    # visual separation waives the 3 nm rule, never the wake matrix:
    # four miles behind the triple seven is still a go-around
    _on_final(sim, "BAW12", "B77W", 5.0)
    follower = _on_final(sim, "DAL100", "B738", 9.0, established=False)
    line = sim.command("100 v")
    assert "following the speedbird triple seven" in line.lower()
    _run(sim, 30)
    assert follower["phase"] == "cruise"        # waved off by the wake
    assert sim.go_arounds == 1
    assert sim.busts == 0


def test_visual_to_the_departure_parallel_teaches(sim):
    ac = _arrival(sim)
    line = sim.command(f"100 v {sim.sector['dep_rwy']}")
    assert "departing traffic today" in line
    assert ac["phase"] == "cruise"              # no clearance issued


def test_visual_flies_the_base_the_ils_calls_hopeless(sim):
    # abeam the threshold on a four-mile base — "inside the marker" to the
    # ILS — a visual pilot just turns a tight final and lands
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 0.5)
    lat, lon = advance(lat, lon, (course + 90.0) % 360.0, 4.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=2000.0,
                  hdg=(course - 60.0) % 360.0, ias=180.0)
    assert "inside the marker" in sim.command("100 i")
    line = sim.command("100 v")
    assert "cleared visual approach" in line.lower()
    _run(sim, 420)
    assert sim.go_arounds == 0
    assert ac not in sim.aircraft               # around the corner and down
    assert sim.landed == 1


def test_visual_needs_no_plates():
    # Teterboro's runway 1 has nothing straight-in published, so `i 1`
    # refuses honestly — but a visual needs eyes, not plates: it's the
    # one clearance that's always real
    s = _quiet("kteb")
    ap = s.airport
    lat, lon = advance(ap["lat"], ap["lon"], 180.0, 10.0)
    ac = _arrival(s, lat=lat, lon=lon, alt=3000.0, hdg=360.0, ias=200.0)
    assert "no instrument approach" in s.command("100 i 1").lower()
    line = s.command("100 v 1")
    assert "cleared visual approach runway one" in line.lower()
    assert ac["phase"] == "cleared"


def test_metroplex_has_uncontrolled_neighbors():
    # New York is the textbook case: JFK works beside LGA and EWR.
    s = build_sector(find_airport("kjfk"))
    codes = {nb["end"]["code"] for nb in s["neighbors"]}
    assert {"LGA", "EWR"} <= codes
    # Portland is no metroplex — a satellite, but no neighbouring major.
    assert build_sector(find_airport("kpwm"))["neighbors"] == []


def test_satellite_is_never_a_major():
    # a nearby major is a neighbour you don't work, not a satellite you do
    for code in ("kjfk", "ksfo", "kord"):
        s = build_sector(find_airport(code))
        if s["sat_apt"] is not None:
            assert not s["sat_apt"]["large"]


def test_neighbor_traffic_is_uncontrolled_and_navigates_a_procedure():
    sim = Sim(find_airport("kjfk"), seed=7)
    sim.hearback_p = 0.0
    for _ in range(8):
        sim._spawn_neighbor()
    nb = [a for a in sim.aircraft if a["plan"] == "neighbor"]
    assert nb                                   # the metroplex populated
    for a in nb:
        assert not _controlled(a)               # never on your frequency
        assert a["dim"] and a["nav"]            # someone else's, flying fixes


def test_neighbor_arrival_descends_toward_its_own_field():
    sim = Sim(find_airport("kjfk"), seed=7)
    sim.hearback_p = 0.0
    for _ in range(8):
        sim._spawn_neighbor()
    arr = next(a for a in sim.aircraft
               if a["plan"] == "neighbor" and a["nav_kind"] == "arrival")
    start_alt = arr["alt"]
    start_d = haversine_nm(arr["lat"], arr["lon"], *arr["nav_field"])
    for _ in range(120):
        sim._fly(arr, 3.0)
        if not arr.get("nav"):
            break
    assert arr["alt"] < start_alt               # came down
    assert haversine_nm(arr["lat"], arr["lon"],
                        *arr["nav_field"]) < start_d   # and closer in


def _pwm():
    s = Sim(find_airport("kpwm"), seed=4)
    s.hearback_p = 0.0
    s.react_s = (0.0, 0.0)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = s._next_neighbor = 1e9
    s._balloon_event = 2
    s.aircraft.clear()
    return s


def test_clearing_an_arrival_onto_a_star():
    s = _pwm()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    line = s.command(f"{a['callsign']} via CDOGG4")     # a real KPWM STAR
    assert "descend via the cdogg four arrival" in line.lower()
    assert a["phase"] == "nav" and a["nav"] and a["via_name"] == "CDOGG4"
    assert _controlled(a)                               # still yours to work
    start_alt = a["alt"]
    t = 0.0
    s.tick(t)
    for _ in range(120):
        t += 3.0
        s.tick(t)
    assert a["alt"] < start_alt                         # it descends the STAR


def _sea():
    s = Sim(find_airport("ksea"), seed=99)
    s.hearback_p = 0.0
    s.react_s = (0.0, 0.0)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = s._next_neighbor = 1e9
    s._balloon_event = 2
    s.aircraft.clear()
    return s


def test_a_gate_name_in_a_via_clearance_names_its_procedure():
    """RADDY is a Seattle corner post *and* a fix on the CHINS FIVE arrival,
    so "via RADDY" is the first thing anyone tries.  "Unfamiliar with RADDY"
    taught nothing about why; naming the procedure teaches the field."""
    s = _sea()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    msg = s.command(f"{a['callsign']} via RADDY")
    assert "RADDY is a fix on the CHINS five arrival" in msg
    assert "via CHINS5" in msg


def test_an_unknown_procedure_lists_the_real_ones():
    s = _sea()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    msg = s.command(f"{a['callsign']} via NOTAFIX")
    assert "CHINS5" in msg and "HAWKZ8" in msg


def test_a_refusal_names_a_procedure_that_would_work():
    # "Unable" teaches nothing; "HAWKZ8 would work from here" teaches the
    # shape of the sector, which is the thing worth learning.
    s = _sea()
    for _ in range(8):
        s._spawn_arrival(allow_sat=False, handin=False)
    seen = [s.command(f"{a['callsign']} via {name}")
            for a in list(s.aircraft)
            for name in ("CHINS5", "SKYKO1", "MARNR8", "OLM2", "HAWKZ8")]
    assert any("would work from here" in m for m in seen), seen[:4]


def test_a_direct_can_name_any_fix_on_a_procedure():
    # The radio now suggests "direct HUMPP and we can pick it up", so `dct`
    # has to accept more than the four corner posts.
    s = _sea()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    assert "HUMPP" in s.command(f"{a['callsign']} dct HUMPP")


def test_clearing_an_arrival_names_the_fix_it_joins_at():
    s = _sea()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    for name in ("SKYKO1", "MARNR8", "HAWKZ8", "OLM2", "CHINS5"):
        line = s.command(f"{a['callsign']} via {name}")
        if line.startswith("unable"):
            continue
        assert "descend via the" in line.lower()
        assert line.lower().startswith("direct ")   # names the join fix
        assert a["via_name"] == name
        return
    pytest.fail("no arrival was joinable from a spawn inside the sector")


def test_arrivals_check_in_from_centre_at_the_boundary():
    """An inbound spawns out past your ring on centre's frequency — grey,
    and deaf to you — then checks in and turns controllable the instant it
    crosses the boundary."""
    s = _pwm()
    s._spawn_arrival(allow_sat=False)          # a normal hand-in arrival
    a = s.aircraft[-1]
    ap = s.airport
    # spawned beyond the ring, still with centre, not yet yours
    assert a["pre_ho"] and a["dim"]
    assert haversine_nm(a["lat"], a["lon"], ap["lat"], ap["lon"]) > SECTOR_NM
    assert not _commandable(a)
    assert "still with centre" in s.command(f"{a['callsign']} l 90")
    # walk it in across the boundary
    t = 0.0
    s.tick(t)
    for _ in range(600):
        t += 3.0
        s.tick(t)
        if not a.get("pre_ho"):
            break
    assert not a["pre_ho"] and not a["dim"]     # centre handed it over
    assert _commandable(a)                       # now yours to work
    assert any("with you" in line for _, line, *_ in s.radio)


def test_arrivals_enter_from_their_origin_direction():
    """A flight comes in from the general bearing of its origin, scattered
    but never from the wrong side of the field — a westerly origin enters
    from the west, not snapped to a corner post."""
    from blips._airports import find_airport
    ap = find_airport("kpwm")
    ord_ = find_airport("ord")
    want = bearing_to(ap["lat"], ap["lon"], ord_["lat"], ord_["lon"])
    s = Sim(ap, seed=3, schedule=[["UAL", "B738", "ORD", 30]])
    s.aircraft.clear()
    for _ in range(20):
        s._spawn_arrival(allow_sat=False)
    arrs = [a for a in s.aircraft if a["plan"] == "arrival" and not a.get("sat")]
    assert arrs
    for a in arrs:
        got = bearing_to(ap["lat"], ap["lon"], a["lat"], a["lon"])
        # within the scatter of the true origin bearing, and not on a gate
        assert abs(turn_delta(got, want)) <= 45.0
    spread = {round(bearing_to(ap["lat"], ap["lon"], a["lat"], a["lon"]))
              for a in arrs}
    assert len(spread) > 1                        # not all on one radial


def test_check_in_gives_a_position_off_a_named_point():
    """The first call carries a rough position read off the nearest thing the
    controller can see named — a gate, the field, a neighbour — so the blip is
    easy to find: 'over SZO' on top of one, else 'N miles <dir> of X'."""
    s = _pwm()
    ap = s.airport
    # right on top of a named gate → "over <gate>"
    name, (glat, glon) = next(iter(s.sector["fixes"].items()))
    assert s._position_phrase(glat, glon) == f"over {name}"
    # well away from everything → "<n> miles <compass> of <named point>"
    phrase = s._position_phrase(ap["lat"] + 0.6, ap["lon"])
    assert re.match(r"\d+ miles (north|south|east|west|northeast|northwest"
                    r"|southeast|southwest) of \S+", phrase), phrase
    # and it actually reaches the radio on check-in
    s.aircraft.clear(); s.radio.clear()
    s._spawn_arrival(allow_sat=False, handin=False)   # checks in at once
    call = [line for _, line, *_ in s.radio if "with you" in line][-1]
    assert (" of " in call) or (" over " in call)


def test_wrong_procedure_kind_and_unknown_are_refused():
    s = _pwm()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    assert "is a departure" in s.command(f"{a['callsign']} via HSKEL4")
    assert "unfamiliar" in s.command(f"{a['callsign']} via NOPE9")


def test_a_vector_cancels_the_procedure():
    s = _pwm()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    s.command(f"{a['callsign']} via CDOGG4")
    assert a["phase"] == "nav"
    s.command(f"{a['callsign']} l 200")                 # taking it back by hand
    assert a["phase"] == "cruise" and not a["nav"] and not a["via_name"]


def test_an_altitude_amends_a_procedure_without_cancelling():
    s = _pwm()
    s._spawn_departure()
    d = s.aircraft[-1]
    s.command(f"{d['callsign']} via HSKEL4")            # a real KPWM SID
    assert d["phase"] == "nav"
    s.command(f"{d['callsign']} c 230")
    assert d["phase"] == "nav" and d["nav"]             # still on the SID
    assert d["cruise_alt"] == 23000.0                   # ceiling amended


def test_handoff_takes_a_sid_flier_off_the_procedure():
    """Centre doesn't fly your SID: a departure handed off mid-procedure
    sheds the nav and its gates, turns loose toward its exit fix, and the
    climb-via becomes a climb — no more levelling at a chart altitude
    while droning around a dogleg that stopped being anyone's problem."""
    s = _pwm()
    s._spawn_departure()
    d = s.aircraft[-1]
    d.pop("xr", None)
    s.command(f"{d['callsign']} via HSKEL4")
    assert d["phase"] == "nav" and d["nav"]
    spot = s.sector["fixes"][d["fix"]]
    d["lat"], d["lon"] = spot            # out at the boundary, still on it
    assert "switching" in s.command(f"{d['callsign']} ho").lower()
    s._flush_pend(d)                     # the beat passes; centre has them
    assert not d["nav"] and not d["via_name"]
    out = bearing_to(s.airport["lat"], s.airport["lon"], *spot)
    assert abs(turn_delta(d["tgt_hdg"], out, None)) < 0.5
    want = 23000.0 if d["perf"][0] >= 230 else 12000.0
    assert d["tgt_alt"] >= want          # gates cancelled, climbing away


def test_ex_cifp_field_declines_a_procedure():
    s = Sim(find_airport("egll"), seed=1)
    s.hearback_p = 0.0
    s._next_arrival = s._next_vfr = s._next_over = 1e9
    s._next_sat_dep = s._next_neighbor = s._next_departure = 1e9
    s.aircraft.clear()
    s._spawn_arrival(allow_sat=False, handin=False)
    a = s.aircraft[-1]
    assert "unfamiliar" in s.command(f"{a['callsign']} via CDOGG4")


def test_shift_starts_populated():
    s = Sim(find_airport("tpa"), seed=1)
    arrivals = [a for a in s.aircraft if a["plan"] == "arrival"]
    departures = [a for a in s.aircraft if a["plan"] == "departure"]
    assert 3 <= len(arrivals) <= 4 and len(departures) >= 1
    # one is already partway in: well inside its own entry gate (measured
    # against the gate, not the other arrivals — TPA has a near corner)
    inside = []
    for a in arrivals:
        gate = haversine_nm(*s.sector["fixes"][a["fix"]],
                            s.airport["lat"], s.airport["lon"])
        here = haversine_nm(a["lat"], a["lon"],
                            s.airport["lat"], s.airport["lon"])
        inside.append(gate - here)
    assert max(inside) > 12.0


def test_shift_opens_workable():
    # nobody on frequency at 0:00 is unmanageably close or high — every
    # arrival, including the one already partway in, can still make the
    # field on a sane descent profile (~3 nm per thousand feet)
    for icao in ("tpa", "egll", "kden"):
        apt = find_airport(icao)
        for seed in range(5):
            s = Sim(apt, seed=seed)
            for a in s.aircraft:
                if a["plan"] != "arrival":
                    continue
                dist = haversine_nm(a["lat"], a["lon"],
                                    apt["lat"], apt["lon"])
                assert dist > 14.0, (icao, seed, a["callsign"])
                assert a["alt"] - apt["elev"] <= dist * 350.0 + 1000.0, (
                    icao, seed, a["callsign"], a["alt"], dist)


def test_warm_open_inbound_is_mid_descent():
    # one prepopulated arrival is already partway in and part-descended,
    # pointed at the field — the open has something to work immediately
    for seed in range(3):
        s = Sim(find_airport("tpa"), seed=seed)
        arrivals = [a for a in s.aircraft if a["plan"] == "arrival"]
        near = min(arrivals, key=lambda a: haversine_nm(
            a["lat"], a["lon"], s.airport["lat"], s.airport["lon"]))
        dist = haversine_nm(near["lat"], near["lon"],
                            s.airport["lat"], s.airport["lon"])
        assert dist <= 26.0, (seed, dist)
        assert near["alt"] <= s.airport["elev"] + dist * 350.0
        brg = bearing_to(near["lat"], near["lon"],
                         s.airport["lat"], s.airport["lon"])
        assert abs(turn_delta(near["hdg"], brg, None)) < 30.0


def test_no_lull_before_the_first_push(sim):
    # the 0.75 quiet-spell multiplier waits for the first push: before
    # the first bank a lull isn't a breather, it's dead air
    base, rate = sim._spawn_rate()
    assert rate == base
    sim._pushes = 1                # the first bank has come and gone
    base, rate = sim._spawn_rate()
    assert rate == pytest.approx(base * 0.75)


# -- the first shift opens gently ----------------------------------------------

def _calm_sim():
    """A calm-shift TPA sector, parked the same way as the sim fixture."""
    s = Sim(find_airport("tpa"), seed=1, calm=True)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = 1e9
    s._balloon_event = 2
    s.flow_hold_p = 0.0
    s.hearback_p = 0.0
    s.wind = (360.0, 0.0)
    s._aloft = (0.0, 1.0)
    s.react_s = (0.0, 0.0)
    s.aircraft.clear()
    s.trails.clear()
    s.radio.clear()
    return s


def test_calm_shift_parks_the_events_past_the_window():
    s = Sim(find_airport("tpa"), seed=1, calm=True)
    assert s.calm_until == 480.0
    assert s._next_push >= s.calm_until + 60.0
    assert s._next_flow >= s.calm_until + 120.0
    assert s._next_abnormal >= s.calm_until
    # an ordinary shift has no window at all
    assert Sim(find_airport("tpa"), seed=1).calm_until == 0.0


def test_calm_window_doubles_the_spawn_gaps():
    s = _calm_sim()
    s.rng.expovariate = lambda lam: 100.0   # a fixed draw isolates the ×2
    s._next_arrival = 0.0
    s._spawn_tick(1.0)
    assert s._next_arrival == 200.0         # doubled inside the window
    s.aircraft.clear()
    s._elapsed = s.calm_until               # the window has lifted
    s._next_arrival = 0.0
    s._spawn_tick(1.0)
    assert s._next_arrival == 100.0         # back to the normal draw


def test_calm_window_hears_perfectly():
    s = _calm_sim()
    s.hearback_p = 1.0                      # every copy would go wrong…
    s._elapsed = 300.0                      # …past the 3-minute grace
    _arrival(s)
    s.command("100 r 270")
    assert s.hearbacks == 0                 # but the window holds
    s._elapsed = s.calm_until + 1.0
    s.command("100 r 90")
    assert s.hearbacks == 1                 # lifted: hearback ramps in


def test_calm_shift_opens_with_coach_lines_once_each():
    s = Sim(find_airport("tpa"), seed=1, calm=True)
    helps = [line for _, line, kind in s.radio if kind == "help"]
    assert len(helps) == 2                  # first check-in, first departure
    assert any("to start them down" in line for line in helps)
    assert any("ho at the edge" in line for line in helps)
    # the whispers use a real callsign off the scope
    assert any(a["callsign"].lower() in line
               for line in helps for a in s.aircraft)
    # park the shift and run: neither line ever repeats
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = 1e9
    s._balloon_event = 2
    s._next_abnormal = 1e9
    _run(s, 30)
    helps = [line for _, line, kind in s.radio if kind == "help"]
    assert len([l for l in helps if "start them down" in l]) == 1
    assert len([l for l in helps if "ho at the edge" in l]) == 1


def test_approach_coach_waits_for_a_close_uncleared_arrival():
    s = _calm_sim()
    ac = _arrival(s, lat=s.airport["lat"] + 0.5)   # ~30 nm out: too far
    _run(s, 2)
    assert not [1 for _, line, kind in s.radio if kind == "help"]
    ac["lat"] = s.airport["lat"] + 0.2             # ~12 nm, still uncleared
    _run(s, 2)
    helps = [line for _, line, kind in s.radio if kind == "help"]
    assert len(helps) == 1
    assert "clears the approach" in helps[0]
    assert ac["callsign"].lower() in helps[0]
    _run(s, 10)                                     # once means once
    assert len([1 for _, line, kind in s.radio if kind == "help"]) == 1


def test_no_coach_lines_off_the_calm_shift(sim):
    # the ordinary fixture is seeded and not calm: even a close,
    # uncleared arrival earns no whisper — determinism keeps its promise
    assert not [1 for _, line, kind in sim.radio if kind == "help"]
    _arrival(sim, lat=sim.airport["lat"] + 0.2)
    _run(sim, 5)
    assert not [1 for _, line, kind in sim.radio if kind == "help"]


def test_departure_handoff_rules(sim):
    sim._next_departure = 0.0
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    dep.pop("xr", None)         # crossing restrictions have their own test
    suffix = dep["callsign"][3:]
    assert "won't take" in sim.command(f"{suffix} ho")   # still on the field
    # teleport them out to their exit fix: now centre wants them
    dep["lat"], dep["lon"] = sim.sector["fixes"][dep["fix"]]
    line = sim.command(f"{suffix} ho")
    assert "switching" in line.lower()
    assert sim.score == 50
    assert sim.ledger[-1] == f"{dep['callsign']} handed off · +50"


def test_traffic_profile_reads_the_signals():
    """The vendored signals shape a field's traffic: scheduled service
    casts airlines, 'Executive' plus a keywords column that remembers the
    Navy casts bizjets, GA and the odd Reach mission."""
    from blips.game.sim import traffic_profile
    pwm = traffic_profile(find_airport("kpwm"))
    assert pwm == {"airline": 1.0}                 # scheduled service
    bxm = traffic_profile(find_airport("kbxm"))    # Brunswick Executive
    assert "airline" not in bxm                    # never an airline arrival
    assert bxm["bizjet"] >= 0.4                    # the Executive part
    assert bxm["mil"] >= 0.2                       # the NAS the keywords keep
    assert bxm["ga"] >= 0.2
    base = traffic_profile({"icao": "XXXX", "name": "Whiteman Air Force Base",
                            "svc": False, "kw": "", "country": "US",
                            "rwys": [{"len": 12000}]})
    assert base["mil"] >= 0.7                      # an active base
    teb = traffic_profile(find_airport("kteb"))
    assert teb["bizjet"] >= 0.7                    # the curated overrides


def test_satellite_casts_as_itself():
    """A satellite with no schedule casts from its own profile — bizjets,
    GA and military metal with reg or wing callsigns — never a repainted
    main-field airline flight."""
    s = Sim(find_airport("kpwm"), seed=5)
    sat = s.sector["sat_apt"]
    assert sat["icao"] == "KBXM"                   # Brunswick, as in life
    allowed = {"C56X", "GLF5", "PC12", "B350", "SR22", "C182",
               "C130", "C17", "K35R"}
    kinds = set()
    for _ in range(40):
        callsign, actype, origin = s._cast_flight("arrival", field=sat)
        assert actype in allowed, (callsign, actype)
        assert origin is None                      # no borrowed schedule
        prefix = callsign[:3]
        if prefix in ("EJA", "LXJ", "VJT"):
            kinds.add("bizjet")
        elif prefix in ("RCH", "CNV"):
            kinds.add("mil")
        elif callsign[0] == "N":
            kinds.add("reg")
    assert kinds >= {"bizjet", "mil", "reg"}       # the whole cast shows up


def test_slow_types_spawn_at_their_own_speed():
    """A nine-seater never spawns doing 250 knots, and its par time is
    paced by its own cruise, not a jet's."""
    sched = [["KAP", "C402", "BOS", 1]]
    s = Sim(find_airport("kpwm"), seed=2, schedule=sched)
    s.aircraft.clear()
    s._spawn_arrival(allow_sat=False)
    ac = s.aircraft[-1]
    assert ac["actype"] == "C402"
    assert ac["ias"] <= 175.0
    dist = haversine_nm(ac["lat"], ac["lon"],
                        s.airport["lat"], s.airport["lon"])
    assert ac["par"] == pytest.approx(dist * (4500.0 / 175.0) + 300.0)


def test_handoff_resumes_own_navigation(sim):
    """A departure handed off out of a hold doesn't roll out on a random
    tangent of the orbit — centre silently turns it loose on the outbound
    radial and climbs it, and the scope watches it happen."""
    sim._next_departure = 0.0
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    dep.pop("xr", None)
    spot = sim.sector["fixes"][dep["fix"]]
    dep["lat"], dep["lon"] = spot        # out at the exit fix
    suffix = dep["callsign"][3:]
    sim.command(f"{suffix} hold")
    _run(sim, 90)                        # well into the orbit
    assert dep["phase"] == "hold"
    sim.command(f"{suffix} ho")
    assert dep["phase"] == "handed"      # the strip is centre's now
    _run(sim, 5)
    out = bearing_to(sim.airport["lat"], sim.airport["lon"], *spot)
    assert abs(turn_delta(dep["tgt_hdg"], out, None)) < 0.5
    want = 23000.0 if dep["perf"][0] >= 230 else 12000.0
    assert dep["tgt_alt"] >= want        # centre climbs them away


def test_handoff_own_nav_waits_out_the_beat(sim):
    """Centre's 'resume own navigation' takes effect a pilot beat after
    the switch, not on the keystroke."""
    sim._next_departure = 0.0
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    dep.pop("xr", None)
    spot = sim.sector["fixes"][dep["fix"]]
    dep["lat"], dep["lon"] = spot
    suffix = dep["callsign"][3:]
    sim.command(f"{suffix} hold")
    _run(sim, 90)
    sim.react_s = (4.0, 4.0)             # pilots take their time again
    before = dep["tgt_alt"]
    sim.command(f"{suffix} ho")
    _run(sim, 1)
    assert dep["tgt_alt"] == before      # still flying the old clearance
    _run(sim, 6)                         # ...until the beat passes
    out = bearing_to(sim.airport["lat"], sim.airport["lon"], *spot)
    assert abs(turn_delta(dep["tgt_hdg"], out, None)) < 0.5
    assert dep["tgt_alt"] > before


def test_feed_compatible_snapshot(sim):
    _arrival(sim)
    _run(sim, 5)
    aircraft, trails, updated, source, error = sim.snapshot()
    ac = aircraft[0]
    for key in ("hex", "callsign", "reg", "actype", "lat", "lon", "alt",
                "ground", "gs", "track", "vrate", "squawk", "emergency",
                "fix_time"):
        assert key in ac                # everything render_scope reads
    assert trails[ac["hex"]]            # trails accumulate
    assert error is None


def test_all_fleet_types_have_performance():
    from blips.game.sim import FLEETS
    for airline, types in FLEETS.items():
        for t in types:
            assert t in PERF, f"{airline} flies {t} but PERF doesn't know it"


def test_type_aliases_land_in_perf():
    from blips.game.fleet import TYPE_ALIAS
    for real, alias in TYPE_ALIAS.items():
        assert alias in PERF, f"{real} → {alias} but PERF doesn't know it"


def test_runway_gate_keeps_widebodies_off_short_fields():
    """A 7,200 ft field (PWM) never casts a widebody arrival or departure;
    a long field (JFK) can take anything.  The gate reads each airport's
    own runways, so it's airport-agnostic, not a KPWM special case."""
    from blips.game.sim import Sim
    wide = {"B763", "B788", "B77W", "A388", "A359", "A339"}
    pwm = Sim(find_airport("kpwm"), seed=3)
    assert not any(pwm._runway_ok(t) for t in wide)   # 7,200 ft can't take one
    assert pwm._runway_ok("B738") and pwm._runway_ok("A223")
    for _ in range(300):
        assert pwm._cast_flight("arrival")[1] not in wide
        assert pwm._cast_flight("departure")[1] not in wide
    jfk = Sim(find_airport("kjfk"), seed=3)
    assert all(jfk._runway_ok(t) for t in wide)


def _fake_pool(airport, entries, routes):
    """A TrafficPool that already sampled: injected entries, canned routes —
    the same faking test_fleet.py uses, wearing a Sim."""
    from blips.game.fleet import TrafficPool

    class _Routes:
        def __init__(self, r):
            self._r = r

        def get(self, cs, lat=None, lon=None):
            return self._r.get(cs)

    pool = TrafficPool(airport, set(PERF))
    pool._entries = [{"cs": cs, "actype": t,
                      "lat": airport["lat"], "lon": airport["lon"]}
                     for cs, t in entries]
    pool.routes = _Routes(routes)
    return pool


def test_route_confirmed_pool_traffic_leads_the_cast():
    """A live flight really inbound here spawns before anything vendored —
    the schedule is the fallback, not the headliner — and when the pool
    runs dry mid-shift the schedule takes over silently."""
    ap = find_airport("ktpa")
    s = Sim(ap, seed=1, schedule=[["DAL", "B738", "ATL", 5]])
    s.pool = _fake_pool(ap, [("SWA123", "B738")],
                        {"SWA123": (("Baltimore", "BWI"), ("Tampa", "TPA"))})
    cs, actype, far = s._cast_flight("arrival")
    assert (cs, actype) == ("SWA123", "B738")
    assert far[0] == "Baltimore" and far[1] is not None
    assert s.cast_sources["pool"] == 1
    before = s.cast_sources["schedule"]
    cs2, _t, _f = s._cast_flight("arrival")     # the pool is used once
    assert cs2.startswith("DAL")
    assert s.cast_sources["schedule"] == before + 1
    assert s.pool_dry                           # it led, then ran out


def test_wrong_direction_pool_flight_never_casts_the_wrong_role():
    """A live flight whose real route leaves Tampa can only ever be the
    departure; the arrival cast passes it over for the schedule."""
    ap = find_airport("ktpa")
    s = Sim(ap, seed=1, schedule=[["DAL", "B738", "ATL", 5]])
    s.pool = _fake_pool(ap, [("SWA123", "B738")],
                        {"SWA123": (("Tampa", "TPA"), ("Miami", "MIA"))})
    cs, _t, _f = s._cast_flight("arrival")
    assert cs.startswith("DAL")                 # SWA123 left untouched
    cs, _t, far = s._cast_flight("departure")
    assert cs == "SWA123" and far[0] == "Miami"


def test_route_unknown_pool_traffic_waits_behind_the_schedule():
    """A sampled flight with no route on file never outranks the vendored
    schedule; it fills in only where there is no schedule at all."""
    ap = find_airport("ktpa")
    s = Sim(ap, seed=1, schedule=[["DAL", "B738", "ATL", 5]])
    s.pool = _fake_pool(ap, [("AAL789", "A320")], {})
    assert s._cast_flight("arrival")[0].startswith("DAL")
    bare = Sim(ap, seed=1)                      # no schedule vendored in
    bare.pool = _fake_pool(ap, [("AAL789", "A320")], {})
    cs, _t, far = bare._cast_flight("arrival")
    assert cs == "AAL789" and far is None       # real metal beats the mix


def test_runway_gate_holds_on_the_pool_path():
    """A widebody sampled inbound to a 7,200 ft field keeps its real
    callsign but flies metal the runway takes; a carrier with nothing
    fitting is passed over for the fallback entirely."""
    from blips.game.sim import MIN_RWY
    ap = find_airport("kpwm")
    s = Sim(ap, seed=3)
    s.pool = _fake_pool(
        ap, [("DAL999", "B77W")],
        {"DAL999": (("Atlanta", "ATL"), ("Portland", "PWM"))})
    cs, actype, _far = s._cast_flight("arrival")
    assert cs == "DAL999" and actype != "B77W"
    assert MIN_RWY.get(actype, 0) <= 7200
    s.pool = _fake_pool(
        ap, [("ZZZ999", "B77W")],               # no fleet to substitute from
        {"ZZZ999": (("Atlanta", "ATL"), ("Portland", "PWM"))})
    assert s._cast_flight("arrival")[0] != "ZZZ999"


def test_seeded_shifts_never_touch_the_live_pool():
    """--seed promises a replay; the live sample would break it, so the
    wiring skips the pool entirely on a seeded shift."""
    from blips.game.app import _live_pool
    from blips.game.fleet import TrafficPool
    ap = find_airport("ktpa")
    assert _live_pool(ap, 12345) is None
    assert isinstance(_live_pool(ap, None), TrafficPool)


def test_hover_chip_shows_origin_and_destination():
    """Arrivals carry 'from <city>' in the hover chip and departures 'to
    <city>' — the game's parity with the live scope's route line."""
    from blips.game.app import _strip_card

    def plain(lines):
        return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)

    arr = {"plan": "arrival", "callsign": "MXY4964", "actype": "A223",
           "fix": "ICTEB", "rwy": "11", "from": "Charleston", "alt": 6000,
           "tgt_alt": 3000, "hdg": 110, "tgt_hdg": 110, "ias": 250,
           "tgt_ias": 250, "phase": "cruise"}
    dep = {"plan": "departure", "callsign": "RPA606", "actype": "CRJ7",
           "fix": "AUG", "to": "Newark", "alt": 2000, "tgt_alt": 11000,
           "hdg": 290, "tgt_hdg": 290, "ias": 210, "tgt_ias": 210,
           "phase": "cruise"}
    assert "from Charleston" in plain(_strip_card(arr))
    assert "to Newark" in plain(_strip_card(dep))
    # a flight whose far city is unknown simply omits the line
    del arr["from"]
    assert "from" not in plain(_strip_card(arr))


def test_hover_chip_shows_assigned_speed_and_par():
    """The chip carries what the assignment book knows: a speed still owed
    shows as ias→target, and an arrival wears its par clock — time in hand
    while under, time owed once past it."""
    from blips.game.app import _strip_card

    def plain(lines):
        return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines)

    arr = {"plan": "arrival", "callsign": "DAL204", "actype": "B738",
           "fix": "ICTEB", "rwy": "11", "alt": 6000, "tgt_alt": 3000,
           "hdg": 110, "tgt_hdg": 110, "ias": 250, "tgt_ias": 210,
           "par": 600.0, "delay": 410.0, "phase": "cruise"}
    text = plain(_strip_card(arr))
    assert "250→210 kt" in text
    assert "par −3:10" in text          # 3:10 in hand
    arr["delay"] = 680.0
    assert "par +1:20" in plain(_strip_card(arr))   # 1:20 past it
    arr["tgt_ias"] = 250.0              # at assigned speed: no arrow
    assert "250 kt" in plain(_strip_card(arr))


def test_schedule_drives_real_routes_with_origin():
    """Given a vendored schedule, arrivals come from it — the real carrier
    flying real metal, with a true origin stored for the check-in and chip.
    An IATA far end resolves to its city ('CHS' → 'Charleston')."""
    sched = [["MXY", "A223", "CHS", 3], ["RPA", "E175", "LGA", 4]]
    s = Sim(find_airport("kpwm"), seed=7, schedule=sched)
    for _ in range(12):
        s._spawn_arrival()
    # satellite arrivals cast as the satellite's own traffic, not the
    # main schedule's — so only the main field's arrivals are checked
    arrs = [ac for ac in s.aircraft
            if ac.get("plan") == "arrival" and not ac.get("sat")]
    assert arrs
    for ac in arrs:
        assert ac["callsign"][:3] in ("MXY", "RPA")
        assert ac["actype"] in ("A223", "E175")
        assert ac.get("from") in ("Charleston", "New York")


# -- weather ------------------------------------------------------------------

def _wall_east_of(lon0):
    """Fake radar: solid heavy echo east of a longitude, clear west."""
    return lambda lat, lon: 1.0 if lon > lon0 else 0.0


def test_vector_into_a_cell_is_refused(sim):
    ap = sim.airport
    ac = _arrival(sim, lat=ap["lat"] + 0.4, lon=ap["lon"], alt=10000.0,
                  hdg=360.0)
    sim.wx_sample = _wall_east_of(ac["lon"] + 0.05)
    line = sim.command("100 r 90")      # straight into the wall
    assert "into a cell" in line
    assert ac["tgt_hdg"] == 360.0       # instruction not applied
    assert "turn left heading" in sim.command("100 l 270").lower()  # away: fine


def test_pilots_deviate_when_ignored(sim):
    ap = sim.airport
    ac = _arrival(sim, lat=ap["lat"] + 0.4, lon=ap["lon"], alt=10000.0,
                  hdg=90.0)             # flying at the wall
    sim.wx_sample = _wall_east_of(ac["lon"] + 0.03)
    _run(sim, 10)
    assert any("requesting 30" in line for _t, line, _k in sim.radio)
    assert not ac.get("wx_deviating")
    _run(sim, 30)                       # ignored: they act
    assert ac["wx_deviating"]
    assert any("deviating" in line for _t, line, _k in sim.radio)
    sim.wx_sample = lambda lat, lon: 0.0    # the cell clears
    _run(sim, 10)
    assert not ac["wx_deviating"]
    assert any("clear of weather" in line for _t, line, _k in sim.radio)


def test_emergency_exempt_from_weather(sim):
    ap = sim.airport
    ac = _arrival(sim, lat=ap["lat"] + 0.4, lon=ap["lon"], alt=10000.0,
                  hdg=360.0)
    sim.wx_sample = _wall_east_of(ac["lon"] + 0.05)
    sim._declare_emergency(ac)
    assert "turn right heading" in sim.command("100 r 90").lower()


def test_wx_sampler_reads_intensity_not_coverage():
    from blips.game.app import _wx_sampler
    # a 2×1 frame: opaque light-blue echo | opaque heavy-red core
    blue = [0, 94, 182, 255]      # bright blue — light stratiform rain
    red = [252, 83, 112, 255]     # salmon-red — a convective core
    rgba = bytearray(blue + red)
    sample = _wx_sampler(rgba, 2, 1, (0.0, 0.0, 2.0, 1.0))
    assert sample(0.5, 0.0) < WX_CLEAR        # blue reads as good as clear
    assert sample(0.5, 2.0) >= WX_DEVIATE      # the red core reads heavy
    assert sample(0.5, 5.0) is None            # off-frame stays None
    # a barely-there echo (near-transparent) is clear air, whatever its hue
    faint = _wx_sampler(bytearray([252, 83, 112, 20]), 1, 1,
                        (0.0, 0.0, 1.0, 1.0))
    assert faint(0.5, 0.5) == 0.0


def test_ils_refused_with_a_cell_on_the_final(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    # lined up 12 nm out, ready to be cleared
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=180.0)
    # heavy echo sitting 6 nm down the final, on the centreline
    cell = advance(*thr, (course + 180.0) % 360.0, 6.0)
    sim.wx_sample = (lambda la, lo: 1.0
                     if abs(la - cell[0]) < 0.05 and abs(lo - cell[1]) < 0.05
                     else 0.0)
    line = sim.command("100 i")
    assert "cell on the final" in line
    assert ac["phase"] != "cleared"          # clearance withheld
    sim.wx_sample = lambda la, lo: 0.0        # the cell moves off
    assert "cleared ils runway" in sim.command("100 i").lower()


def test_emergency_takes_the_ils_through_weather(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=180.0)
    sim.wx_sample = lambda la, lo: 1.0        # wall-to-wall weather
    sim._declare_emergency(ac)
    assert "cleared ils runway" in sim.command("100 i").lower()


# -- the day changes ------------------------------------------------------------

def test_flow_change_flips_the_runway(sim):
    old_rwy, old_end = sim.sector["rwy"], sim.sector["end"]
    # one arrival merely cleared, one established: only the first loses it
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    est = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=180.0)
    sim.command("100 i")
    _run(sim, 45)
    assert est["phase"] == "established"
    far = _arrival(sim, callsign="DAL200", alt=9000.0)
    far["phase"] = "cleared"            # pretend they were cleared far out
    sim._next_flow = 0.0
    _run(sim, 2)
    assert sim.sector["end"] != old_end
    assert sim.sector["rwy"] != old_rwy
    assert sim.sector_rev == 1
    assert any("ATIS update" in line for _t, line, _k in sim.radio)
    assert far["phase"] == "cruise"     # clearance canceled
    assert est["phase"] in ("established", "landed")   # grandfathered


def test_the_wind_can_hold_through_an_atis_update(sim):
    # not every new letter turns the airport: sometimes the wind only
    # shifts in place — new numbers, same runway, nothing cancelled
    before = (sim.sector["rwy"], sim.sector["end"], sim.sector_rev)
    letter = sim.atis
    cleared = _arrival(sim, alt=9000.0)
    cleared["phase"] = "cleared"
    sim.flow_hold_p = 1.0           # this update, the wind holds
    sim._next_flow = 0.0
    _run(sim, 2)
    assert sim.atis != letter                       # the letter advances
    assert any("ATIS update" in line for _t, line, _k in sim.radio)
    assert (sim.sector["rwy"], sim.sector["end"]) == before[:2]
    assert sim.sector_rev == before[2]              # the scope never redraws
    assert cleared["phase"] == "cleared"            # the clearance survives


def test_flow_change_holds_departures_for_the_old_final(sim):
    # an arrival established when the airport turns lands out the old way —
    # tower must not release a new-end departure into that head-on final
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 8.0)
    est = _arrival(sim, lat=lat, lon=lon, alt=2500.0, hdg=course, ias=180.0)
    sim.command("100 i")
    _run(sim, 30)
    assert est["phase"] == "established"
    sim._next_flow = 0.0
    _run(sim, 2)
    assert est["phase"] in ("established", "landed")   # grandfathered
    sim._next_departure = 0.0           # tower has one ready to roll
    _run(sim, 2)
    assert not any(ac["plan"] == "departure" for ac in sim.aircraft)
    sim._next_departure = 1e9           # park it while the arrival lands out
    _run(sim, 480)
    assert sim.landed == 1
    sim._next_departure = 0.0           # now the release is clean
    _run(sim, 2)
    assert any(ac["plan"] == "departure" for ac in sim.aircraft)


def _departure(sim, gap, alt=3000.0, hdg=None, ias=250.0,
               callsign="EJA100", actype="B738"):
    """A climb-out planted ``gap`` nm ahead of where the next release
    would appear, on runway heading unless told otherwise."""
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, course, 1.5 + gap)
    ac = sim._base(callsign, actype, lat, lon, alt,
                   course if hdg is None else hdg, ias)
    ac.update(plan="departure", fix=sim.sector["exits"][0],
              tgt_alt=alt, tgt_ias=ias)
    sim.aircraft.append(ac)
    return ac


def test_tower_holds_the_release_behind_a_climbout(sim):
    # same track, same altitude, nothing between them — nobody rolls,
    # even though the leader is past the old 7 nm-from-the-field line
    lead = _departure(sim, gap=7.0)
    sim._next_departure = 0.0
    _run(sim, 2)
    assert [a for a in sim.aircraft if a["plan"] == "departure"] == [lead]
    # climbed away, and the runway frees up
    lead["alt"] = lead["tgt_alt"] = 4200.0
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2


def test_tower_does_not_treat_800_feet_as_vertical_separation(sim):
    # The separation monitor requires 1,000 ft.  The release meter must use
    # that same number or this follower climbs into an automatic bust about
    # 40 seconds after tower puts it on the runway.
    initial = float(round((sim.sector["elev"] + 3000) / 1000) * 1000)
    lead = _departure(sim, gap=2.0, alt=initial + SEP_FT - 200.0)
    sim._next_departure = 0.0
    _run(sim, 60)
    assert [a for a in sim.aircraft if a["plan"] == "departure"] == [lead]
    assert sim.busts == 0


def test_tower_holds_for_a_high_departure_assigned_back_into_the_flow(sim):
    # Being high at this instant is not protection if the aircraft is level
    # below the minimum, descending there, or has yet to act on that descent.
    initial = float(round((sim.sector["elev"] + 3000) / 1000) * 1000)
    lead = _departure(sim, gap=2.0, alt=initial + 3000.0)
    lead["pend"] = {"due": sim._elapsed + 10.0, "tgt_alt": initial}
    sim._next_departure = 0.0
    _run(sim, 2)
    assert [a for a in sim.aircraft if a["plan"] == "departure"] == [lead]


def test_a_diverged_climbout_frees_the_release(sim):
    course = sim.sector["course"]
    _departure(sim, gap=7.0, hdg=(course + 40.0) % 360.0)
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2


def test_a_slow_climbout_needs_more_room(sim):
    # a jet released 250 kt behind a King Air's 200 runs it down on the
    # same track — the slow leader holds the release further out
    lead = _departure(sim, gap=12.0, ias=180.0)
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 1
    lead["lat"], lead["lon"] = advance(*sim.sector["thr"],
                                       sim.sector["course"], 1.5 + 16.0)
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2


def test_a_heavy_departure_holds_the_next_release_longer(sim):
    # the two-minute wake rule, in trail: eleven miles frees a 737 ahead,
    # not a heavy — the roll waits until the wake has somewhere to be
    thr, course = sim.sector["thr"], sim.sector["course"]
    lead = _departure(sim, gap=11.0, actype="B77W")
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 1
    lead["lat"], lead["lon"] = advance(*thr, course, 1.5 + 15.0)
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2


def test_a_heavy_climbout_turned_or_above_still_frees_the_release(sim):
    course = sim.sector["course"]
    lead = _departure(sim, gap=11.0, actype="B77W")
    lead["alt"] = lead["tgt_alt"] = 4200.0   # 800 up on the next one's initial
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2
    sim.aircraft.clear()
    lead = _departure(sim, gap=11.0, actype="B77W",
                      hdg=(course + 40.0) % 360.0)
    lead["tgt_hdg"] = lead["hdg"]            # turned away, holding it
    sim._next_departure = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2


def test_the_extra_departure_timer_owes_the_same_metering(sim):
    # at a field with no satellite the sat timer rolls a main-runway
    # departure — it must not roll one into a fresh climb-out
    sim.sector["sat"] = None
    sim.sector["sat_apt"] = None
    lead = _departure(sim, gap=2.0, alt=2000.0)
    sim._next_sat_dep = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 1
    lead["alt"] = lead["tgt_alt"] = 5200.0     # climbed clear overhead
    sim._next_sat_dep = 0.0
    _run(sim, 2)
    assert sum(a["plan"] == "departure" for a in sim.aircraft) == 2


def test_satellite_departures_level_below_the_main_flow(sim):
    # the LOA's split: a satellite climb-out stops a thousand feet clear
    # of the main field's, so two untouched departures never meet level
    sat = sim.sector["sat"]
    if sat is None:
        pytest.skip("no satellite at this field")
    sim._spawn_departure(sat=sat)
    dep = next(a for a in sim.aircraft if a["plan"] == "departure")
    assert dep["tgt_alt"] == sim._initial_alt(sat)
    assert abs(dep["tgt_alt"] - sim._initial_alt()) >= 1000.0


def test_loa_split_is_an_invariant_of_the_derivation(sim):
    # derived as a pair: a thousand under, or — where the satellite sits
    # too high above the main field for 'under' to clear its pattern —
    # a thousand over.  Never level with the main flow.
    main = sim._initial_alt()
    for sat_elev in range(0, 9000, 250):
        got = sim._initial_alt({"elev": float(sat_elev)})
        assert abs(got - main) >= 1000.0
        assert got >= sat_elev + 1200.0   # still above the spawn altitude


def test_flow_change_go_around_gets_the_new_runway(sim):
    # a grandfathered arrival that balks its approach comes back for the
    # runway everyone else is using now, not yesterday's
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 8.0)
    est = _arrival(sim, lat=lat, lon=lon, alt=2500.0, hdg=course, ias=180.0)
    sim.command("100 i")
    _run(sim, 30)
    assert est["phase"] == "established"
    sim._next_flow = 0.0
    _run(sim, 2)
    sim._go_around(est, "test")
    assert est["rwy"] == sim.sector["rwy"]
    assert est["course"] == sim.sector["course"]
    assert est["thr"] == sim.sector["thr"]


def test_emergency_priority_bonus(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 10.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=180.0)
    sim._declare_emergency(ac)
    assert ac["squawk"] == "7700"
    assert sim.bell
    sim.bell = False
    sim.command("100 i")
    _run(sim, 600)
    assert sim.landed == 1
    assert sim.score == 100 + 300       # landing + quick-priority bonus
    assert any("medics" in line for _t, line, _k in sim.radio)


def test_abnormals_cool_down_instead_of_capping(sim):
    # once-per-shift-forever is gone: a concluded crisis re-arms after a
    # breather, and only one runs at a time on the shared clock
    sim._next_abnormal = 0.0
    sim._elapsed = 1000.0               # past every event's opening floor
    sim._center_until = float("inf")    # park centre; this test works the clock
    first = _arrival(sim, alt=12000.0)
    assert sim._abnormal_ok()
    sim.rng.random = lambda: 0.0        # every hazard roll fires
    _run(sim, 3)
    assert first["squawk"] == "7700"    # the tick declared them unprompted
    assert not sim._abnormal_ok()       # one crisis at a time
    first["phase"] = "landed"
    _run(sim, 2)
    assert sim._abnormal_active == 0    # concluded on the deck...
    assert not sim._abnormal_ok()       # ...but the breather holds
    assert 1150.0 < sim._next_abnormal - sim._elapsed < 1550.0
    second = _arrival(sim, callsign="DAL200", alt=12000.0)
    _run(sim, 30)
    assert second["squawk"] != "7700"   # still cooling down
    sim._next_abnormal = sim._elapsed   # the breather expires
    _run(sim, 3)
    assert second["squawk"] == "7700"   # eligible again — no lifetime cap


def test_a_departure_declares_and_comes_back(sim):
    sim._next_abnormal = 0.0
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    dep.pop("xr", None)
    sim._declare_return(dep)
    assert dep["squawk"] == "7700"
    assert dep["plan"] == "arrival"             # the plan flips
    assert dep["rwy"] == sim.sector["rwy"]      # with a runway...
    assert dep["thr"] == sim.sector["thr"]
    assert dep["par"] and dep["mayday_t"] is not None   # ...a par, a clock
    assert sim.bell
    assert any("return to the field" in line for _t, line, _k in sim.radio)
    # no longer centre's to take — they're yours to land
    assert "yours to land" in sim.command(f"{dep['callsign']} ho")
    # bring them around: park them on a long final, the ILS just works
    thr, course = sim.sector["thr"], sim.sector["course"]
    dep["lat"], dep["lon"] = advance(*thr, (course + 180.0) % 360.0, 10.0)
    dep.update(alt=3000.0, tgt_alt=3000.0, hdg=course, tgt_hdg=course,
               ias=180.0, tgt_ias=180.0)
    assert "cleared ils" in sim.command(f"{dep['callsign']} i").lower()
    _run(sim, 400)
    assert sim.landed == 1                      # scores as an arrival...
    assert sim.score == 100 + 300               # ...with the priority bonus
    assert sim._rwy_closed()                    # equipment meets them
    assert any("closed — equipment" in line for _t, line, _k in sim.radio)
    assert sim._abnormal_active == 0            # concluded — cooldown running
    assert sim._next_abnormal > sim._elapsed


def test_minimum_fuel_zeroes_the_pattern_allowance(sim):
    ac = _arrival(sim, alt=9000.0)
    ac["par"] = 4000.0                          # plenty of slack this morning
    sim._declare_minfuel(ac)
    dist = haversine_nm(ac["lat"], ac["lon"], *sim.sector["thr"])
    straight = ac["delay"] + dist * 4500.0 / float(ac["perf"][0])
    assert abs(ac["par"] - straight) < 1.0      # par is now the straight-in
    assert ac["squawk"] != "7700"               # advisory, not an emergency
    assert not sim.bell                         # no red blip, no bell
    assert any("minimum fuel" in line for _t, line, _k in sim.radio)
    assert not sim._abnormal_ok()               # it holds the shared clock
    # the hover chip carries the tell, and its par clock reads what's left
    from blips.game.app import _strip_card
    lines = "\n".join(_strip_card(ac))
    assert "minimum fuel" in lines
    assert "par −" in lines                     # time in hand, honestly read


def test_minimum_fuel_escalates_to_emergency_fuel(sim):
    ac = _arrival(sim, alt=11000.0, hdg=90.0)   # dawdling across, not inbound
    ac["par"] = 4000.0
    sim._declare_minfuel(ac)
    _run(sim, 300)
    assert ac["squawk"] != "7700"               # six minutes of patience
    _run(sim, 90)
    assert ac["squawk"] == "7700"               # emergency fuel — a real 7700
    assert ac["mayday_t"] is not None
    assert sim.bell
    assert any("emergency fuel" in line for _t, line, _k in sim.radio)


def test_seeded_shifts_reproduce():
    ap = find_airport("tpa")
    radios = []
    for _ in range(2):
        s = Sim(ap, seed=4471)
        t = s.start
        for i in range(300):
            t += 1.0
            s.tick(t)
        radios.append([line for _t, line, _k in s.radio])
    assert radios[0] == radios[1]       # same script, same shift


# -- lost comms -------------------------------------------------------------------

def test_nordo_flies_the_last_clearance_and_wont_answer(sim):
    ac = _arrival(sim, alt=12000.0, hdg=90.0)
    sim.command("100 d 80")
    sim._declare_nordo(ac)
    assert ac["squawk"] == "7600"       # the blip is already alert-red
    assert sim.bell
    line = sim.command("100 l 360")
    assert "NORDO" in line
    assert ac["tgt_hdg"] == 90.0        # nobody home to turn
    _run(sim, 60)
    assert ac["tgt_alt"] == 8000.0      # the last clearance still flies
    assert 9000.0 < ac["alt"] < 12000.0


def test_radios_come_back(sim):
    ac = _arrival(sim, alt=12000.0)
    sim._declare_nordo(ac)
    ac["nordo_until"] = sim._elapsed + 5.0
    _run(sim, 10)
    assert ac["nordo_until"] is None
    assert ac["squawk"] != "7600"
    assert any("back with you" in line for _t, line, _k in sim.radio)
    assert "turn left heading" in sim.command("100 l 360").lower()


# -- reaction time ----------------------------------------------------------------

def test_instructions_take_a_beat_to_bite(sim):
    sim.react_s = (3.0, 3.0)
    ac = _arrival(sim, hdg=360.0)
    sim.command("100 r 90")
    assert ac["tgt_hdg"] == 360.0       # read back, not yet flown
    _run(sim, 2)
    assert ac["hdg"] == 360.0           # still straight and level
    _run(sim, 8)
    assert 12.0 < ac["hdg"] < 27.0      # ~7-8 s of turning, not 10


def test_new_transmission_flushes_the_staged_one(sim):
    sim.react_s = (5.0, 5.0)
    ac = _arrival(sim, alt=12000.0)
    sim.command("100 d 60")
    assert ac["tgt_alt"] == 12000.0     # staged, hands not moved yet
    sim.command("100 rs 210")           # keying again: the last one's done
    assert ac["tgt_alt"] == 6000.0
    assert ac["tgt_ias"] == 250.0       # the new one waits its turn
    _run(sim, 8)
    assert ac["tgt_ias"] == 210.0


def test_go_around_forgets_whatever_was_staged(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 4.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=5000.0, hdg=course, ias=170.0)
    ac["phase"] = "established"
    sim.react_s = (30.0, 30.0)
    sim.command("100 rs 140")           # staged far in the future
    _run(sim, 120)                      # too high on short final: go-around
    assert sim.go_arounds == 1
    assert ac["pend"] is None           # the pilot's hands went elsewhere


# -- conflict alert ---------------------------------------------------------------

def test_conflict_alert_blinks_before_the_bust(sim):
    # head-on at the same altitude, eight miles apart: legal now, a loss
    # in well under a minute — both blips must already be talking to you
    a = _arrival(sim, callsign="DAL100", alt=8000.0, hdg=180.0)
    b = _arrival(sim, callsign="SWA200", alt=8000.0, hdg=360.0,
                 lat=a["lat"] - 8.0 / 60.0, lon=a["lon"])
    _run(sim, 2)
    assert a["ca"] and b["ca"]
    assert sim.busts == 0
    assert not a["emergency"]           # an alert is not yet a loss


def test_conflict_alert_is_exposed_as_a_pair(sim):
    # the scope draws the tie-line between the two blips it's alarming about,
    # so the pair the box flags has to reach the renderer, not just the flags
    a = _arrival(sim, callsign="DAL100", alt=8000.0, hdg=180.0)
    b = _arrival(sim, callsign="SWA200", alt=8000.0, hdg=360.0,
                 lat=a["lat"] - 8.0 / 60.0, lon=a["lon"])
    _run(sim, 2)
    pairs = {(frozenset((h1, h2)), sev) for h1, h2, sev in sim.conflicts}
    assert (frozenset((a["hex"], b["hex"])), "alert") in pairs


def test_separation_loss_is_exposed_as_a_pair(sim):
    a = _arrival(sim, callsign="DAL100", alt=5000.0, hdg=90.0)
    b = _arrival(sim, callsign="SWA200", alt=5400.0, hdg=90.0,
                 lat=a["lat"], lon=a["lon"] + 0.02)
    _run(sim, 3)
    assert (a["hex"], b["hex"], "loss") in sim.conflicts \
        or (b["hex"], a["hex"], "loss") in sim.conflicts


def test_cpa_reads_the_miss_distance():
    from blips.scope import _cpa_nm
    # ten miles apart on one line, closing head-on → they pass through ~0
    lat, lon = 43.0, -70.0
    east = lon + 10.0 / (60.0 * math.cos(math.radians(lat)))
    a = {"track": 90.0, "gs": 300}       # west aircraft, flying east
    b = {"track": 270.0, "gs": 300}      # east aircraft, flying west
    assert _cpa_nm(a, lat, lon, b, lat, east) < 0.1
    # parallel, same track, four miles abeam → the gap simply holds
    c = {"track": 360.0, "gs": 250}
    d = {"track": 360.0, "gs": 250}
    gap = _cpa_nm(c, lat, lon, d, lat + 4.0 / 60.0, lon)
    assert abs(gap - 4.0) < 0.05
    # a target with no vector can't be projected
    assert _cpa_nm({"track": None, "gs": 0}, lat, lon, b, lat, east) is None


def test_no_conflict_alert_when_diverging(sim):
    a = _arrival(sim, callsign="DAL100", alt=8000.0, hdg=360.0)
    b = _arrival(sim, callsign="SWA200", alt=8000.0, hdg=180.0,
                 lat=a["lat"] - 8.0 / 60.0, lon=a["lon"])
    _run(sim, 2)
    assert not a["ca"] and not b["ca"]


def test_level_off_clears_the_projection(sim):
    # one descending toward the other, but assigned a thousand feet above:
    # the projection respects the level-off and stays quiet
    a = _arrival(sim, callsign="DAL100", alt=8000.0, hdg=90.0)
    b = _arrival(sim, callsign="SWA200", alt=12000.0, hdg=90.0,
                 lat=a["lat"], lon=a["lon"] + 0.02)
    sim.command("200 d 90")             # stops 1,000 above DAL100
    _run(sim, 30)
    assert not a["ca"] and not b["ca"]


def test_assigned_altitude_rides_the_data_block(sim):
    from blips.scope import data_block
    ac = _arrival(sim, callsign="DAL100", alt=11000.0)
    sim.command("100 d 80")
    _run(sim, 10)
    assert data_block(ac) == f"DAL100 {round(ac['alt'] / 100):03d}↓080"
    _run(sim, 300)                      # level at eight: back to plain
    assert data_block(ac) == "DAL100 080"
    live = {"ground": False, "alt": 31000, "callsign": "BAW42", "vrate": 0}
    assert data_block(live) == "BAW42 310"   # the live scope is untouched


# -- pushes and the closed runway ------------------------------------------------

def test_the_push_is_announced_and_timed(sim):
    sim._next_push = 0.0
    _run(sim, 3)
    assert any("bank of arrivals" in line for _t, line, _k in sim.radio)
    assert sim._push_until > sim._elapsed          # a few busy minutes
    assert sim._next_push > sim._push_until        # then a breather


def test_emergency_landing_closes_the_runway(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 10.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=180.0)
    sim._declare_emergency(ac)
    sim.command("100 i")
    # somebody else is merely cleared, far out: the closure strips it
    far = _arrival(sim, callsign="DAL200", alt=9000.0)
    far["phase"] = "cleared"
    for _ in range(120):                           # run until they're down
        _run(sim, 5)
        if sim.landed:
            break
    assert sim.landed == 1
    assert sim._rwy_closed()
    assert any("closed — equipment" in line for _t, line, _k in sim.radio)
    assert far["phase"] == "cruise"                # clearance died with it
    assert "runway's closed" in sim.command("200 i")


def test_short_final_waves_off_a_closed_runway_for_free(sim):
    thr, course = sim.sector["thr"], sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 4.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=1300.0, hdg=course, ias=140.0)
    ac["phase"] = "established"
    sim._rwy_closed_until = sim._elapsed + 300.0
    before = sim.score
    _run(sim, 120)
    assert ac["phase"] == "cruise"
    assert sim.go_arounds == 1
    assert sim.score == before                     # not your fault, no charge
    assert any("still occupied" in line for _t, line, _k in sim.radio)


def test_the_runway_reopens_on_the_radio(sim):
    sim._rwy_closed_until = 5.0
    _run(sim, 10)
    assert not sim._rwy_closed()
    assert any("back open" in line for _t, line, _k in sim.radio)


# -- wind ---------------------------------------------------------------------

def test_crosswind_drifts_the_track(sim):
    sim.wind = (270.0, 20.0)            # from the west
    ac = _arrival(sim, hdg=360.0)
    _run(sim, 10)
    drift = turn_delta(360.0, ac["track"])
    assert 2.0 < drift < 10.0           # pushed east of the nose
    assert ac["hdg"] == 360.0           # the pilot never moved


def test_headwind_costs_groundspeed(sim):
    sim.wind = (360.0, 20.0)            # right on the nose
    ac = _arrival(sim, hdg=360.0, alt=10000.0, ias=250.0)
    _run(sim, 5)
    tas = ac["ias"] * (1.0 + ac["alt"] * 2e-5)
    assert abs(ac["gs"] - (tas - 20.0)) < 1.0
    assert ac["track"] == 360.0         # straight into it: no drift


def test_wind_aloft_strengthens_and_turns(sim):
    sim.wind = (270.0, 10.0)
    sim._aloft = (30.0, 3.0)            # veers 30°, triples, by the top
    elev = sim.airport["elev"]
    assert sim.wind_at(elev) == (270.0, 10.0)      # the ATIS number
    d_top, k_top = sim.wind_at(elev + 18000.0)
    assert abs(d_top - 300.0) < 0.1 and abs(k_top - 30.0) < 0.1
    d_mid, k_mid = sim.wind_at(elev + 4000.0)
    assert 270.0 < d_mid < d_top        # turning on the way up...
    assert 10.0 < k_mid < k_top         # ...and building the whole way


def test_calm_day_is_calm_all_the_way_up(sim):
    sim._aloft = (30.0, 3.5)            # a gradient with nothing to grade
    assert sim.wind_at(sim.airport["elev"] + 18000.0)[1] == 0.0


def test_wind_aloft_backs_south_of_the_equator():
    assert Sim(find_airport("tpa"), seed=3)._aloft[0] > 0.0    # veers
    assert Sim(find_airport("syd"), seed=3)._aloft[0] < 0.0    # backs


def test_headwind_bites_harder_at_altitude(sim):
    sim.wind = (360.0, 20.0)            # 20 on the nose at the surface...
    sim._aloft = (0.0, 3.0)             # ...and 60 at the top of the climb
    low = _arrival(sim, "DAL1", hdg=360.0, alt=2000.0, ias=250.0)
    high = _arrival(sim, "DAL2", lat=sim.airport["lat"] + 1.0,
                    hdg=360.0, alt=14000.0, ias=250.0)
    _run(sim, 5)
    deficit = lambda ac: ac["ias"] * (1.0 + ac["alt"] * 2e-5) - ac["gs"]
    assert deficit(high) > deficit(low) + 15.0


def test_ils_lands_in_a_crosswind(sim):
    course = sim.sector["course"]
    sim.wind = ((course + 90.0) % 360.0 or 360.0, 14.0)
    thr = sim.sector["thr"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    sim.command("100 i")
    _run(sim, 900)
    assert sim.landed == 1              # crabbed down the localizer


def test_atis_opens_the_shift_and_the_letter_advances():
    s = Sim(find_airport("tpa"), seed=7)
    first = s.radio[0]
    assert first[2] == "atis"
    assert "information alpha" in first[1]
    # TPA runs segregated parallels, so the broadcast names both ends
    assert f"landing runway {say_runway(s.sector['rwy'])}" in first[1]
    assert (f"departing runway {say_runway(s.sector['dep_rwy'])}"
            in first[1])
    s._next_flow = 0.0
    t = s.start
    s._last_tick = t
    s.tick(t + 1.0)
    assert any("information bravo" in line for _t, line, _k in s.radio)


# -- wake turbulence --------------------------------------------------------------

def _final_pair(sim, lead_type, lead_nm, foll_type, foll_nm):
    """Two arrivals established on final, leader closer in."""
    thr, course = sim.sector["thr"], sim.sector["course"]
    out = []
    for i, (actype, nm) in enumerate(((lead_type, lead_nm),
                                      (foll_type, foll_nm))):
        lat, lon = advance(*thr, (course + 180.0) % 360.0, nm)
        ac = _arrival(sim, callsign=f"DAL10{i}", lat=lat, lon=lon,
                      alt=sim.airport["elev"] + nm * 300.0, hdg=course,
                      ias=170.0, actype=actype)
        ac["phase"] = "established"
        out.append(ac)
    return out


def test_heavies_carry_the_suffix_on_frequency(sim):
    _arrival(sim, callsign="BAW12", actype="B77W")
    line = sim.command("12 l 270")
    assert line.endswith(", Speedbird 12 heavy.")
    _arrival(sim, callsign="DAL200", actype="B738")
    assert sim.command("200 l 270").endswith(", Delta 200.")


def test_three_miles_behind_a_heavy_goes_around(sim):
    _lead, follower = _final_pair(sim, "B77W", 6.0, "B738", 9.5)
    _run(sim, 2)
    assert follower["phase"] == "cruise"
    assert sim.go_arounds == 1
    assert any("heavy ahead" in line for _t, line, _k in sim.radio)


def test_three_miles_behind_a_737_is_legal(sim):
    _lead, follower = _final_pair(sim, "B738", 6.0, "B738", 9.5)
    _run(sim, 2)
    assert follower["phase"] == "established"
    assert sim.go_arounds == 0


def test_closing_on_a_heavy_warns_before_it_bites(sim):
    _lead, follower = _final_pair(sim, "B77W", 6.0, "B738", 11.5)
    _run(sim, 2)
    assert follower["phase"] == "established"   # 5.5 nm: legal, but tight
    assert any("closing on the heavy" in line
               for _t, line, _k in sim.radio)
    warnings = sum("closing on the heavy" in line
                   for _t, line, _k in sim.radio)
    _run(sim, 4)
    assert sum("closing on the heavy" in line
               for _t, line, _k in sim.radio) == warnings  # said once


def test_the_wake_minimum_reads_the_follower(sim):
    # four and a half behind a heavy is legal for another heavy...
    _lead, follower = _final_pair(sim, "B77W", 6.0, "B77W", 10.5)
    _run(sim, 2)
    assert follower["phase"] == "established"
    assert sim.go_arounds == 0
    sim.aircraft.clear()
    # ...and a go-around for a 737, which owes five
    _lead, follower = _final_pair(sim, "B77W", 6.0, "B738", 10.5)
    _run(sim, 2)
    assert follower["phase"] == "cruise"
    assert sim.go_arounds == 1


def test_a_small_pays_miles_even_behind_a_737(sim):
    # 3.5 nm is an everyday gap for a jet pair; a nine-seater owes four
    _lead, follower = _final_pair(sim, "B738", 6.0, "C402", 9.5)
    _run(sim, 2)
    assert follower["phase"] == "cruise"
    assert sim.go_arounds == 1
    assert any("traffic ahead" in line for _t, line, _k in sim.radio)


def test_the_757_earns_its_own_rows(sim):
    _lead, follower = _final_pair(sim, "B752", 6.0, "B738", 9.5)
    _run(sim, 2)
    assert follower["phase"] == "cruise"        # 3.5 nm: four behind a 757
    assert any("seven five seven ahead" in line
               for _t, line, _k in sim.radio)
    sim.aircraft.clear()
    _lead, follower = _final_pair(sim, "B752", 6.0, "C402", 10.5)
    _run(sim, 2)
    assert follower["phase"] == "cruise"        # 4.5 nm: a small owes five
    assert sim.go_arounds == 2


# -- 250 below ten thousand -----------------------------------------------------

def test_the_clamp_bleeds_speed_off_below_ten_thousand(sim):
    # checked in fast and low: the crew slows themselves, no word needed
    ac = _arrival(sim, alt=8000.0, ias=280.0)
    _run(sim, 60)
    assert ac["ias"] == 250.0
    # ...and 280 given up high is kept until the descent crosses ten
    hi = _arrival(sim, callsign="SWA200", alt=14000.0, ias=280.0)
    sim.command("200 d 60")
    _run(sim, 60)
    assert hi["alt"] > 10000.0 and hi["ias"] == 280.0
    _run(sim, 300)
    assert hi["alt"] < 10000.0 and hi["ias"] == 250.0


def test_the_speed_limit_is_refused_below_ten_thousand(sim):
    ac = _arrival(sim, alt=8000.0, ias=250.0)
    line = sim.command("100 is 280")
    assert "two five zero below one zero thousand" in line
    assert ac["tgt_ias"] == 250.0
    # one still descending back under it gets the same answer...
    mid = _arrival(sim, callsign="SWA200", alt=12000.0, ias=250.0)
    sim.command("200 d 80")
    assert "below one zero thousand" in sim.command("200 is 280")
    # ...and one staying above may have it
    _arrival(sim, callsign="UAL300", alt=12000.0, ias=250.0)
    assert "two eight zero" in sim.command("300 is 280")
    assert mid["tgt_ias"] == 250.0


def test_a_departure_holds_250_through_ten_thousand(sim):
    # the spawner bugs cruise from the start; the clamp does the law, so
    # the acceleration through ten happens on its own, on the scope
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    assert dep["tgt_ias"] == float(dep["perf"][0])
    sim.aircraft.clear()
    dep = _departure(sim, gap=0.0, alt=3000.0, ias=250.0)
    dep.update(tgt_alt=23000.0, tgt_ias=280.0)
    _run(sim, 120)
    assert dep["alt"] < 10000.0 and dep["ias"] == 250.0
    _run(sim, 180)
    assert dep["alt"] > 10000.0 and dep["ias"] > 260.0


def test_an_emergency_takes_what_it_needs_below_ten(sim):
    ac = _arrival(sim, alt=8000.0, ias=250.0)
    sim._declare_emergency(ac)
    assert "two eight zero" in sim.command("100 is 280")
    _run(sim, 60)
    assert ac["ias"] == 280.0


# -- delay and the rating -------------------------------------------------------

def test_dawdling_landing_pays_a_delay_penalty(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    ac["par"] = 1.0                     # pretend they've been vectored ages
    ac["delay"] = 600.0                 # ...ten minutes of laps already flown
    sim.command("100 i")
    _run(sim, 900)
    assert sim.landed == 1
    assert sim.score == 20              # 100 minus the capped penalty
    assert sim.offered == 100
    assert sim.ledger[-1].startswith("DAL100 down · +20 · ")
    assert sim.ledger[-1].endswith(" over par")


def test_prompt_landing_keeps_the_hundred(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    ac["par"] = 3600.0                  # miles of room
    sim.command("100 i")
    _run(sim, 900)
    assert sim.score == 100
    assert sim.offered == 100
    assert sim.ledger[-1] == "DAL100 down · +100 · under par"


def test_spawned_arrivals_carry_a_reachable_par(sim):
    sim._spawn_arrival(allow_sat=False)    # the main-field par formula
    ac = sim.aircraft[-1]
    # par is the straight-in at the type's own working speed plus five
    # minutes of slack: flown direct it's beaten comfortably, a couple
    # of laps is not — whatever the cast happened to send
    dist = haversine_nm(ac["lat"], ac["lon"],
                        sim.airport["lat"], sim.airport["lon"])
    straight_in = dist * 4500.0 / float(ac["perf"][0])
    assert 240.0 < ac["par"] - straight_in < 360.0


def test_rating_is_score_against_offered(sim):
    from blips.game.app import _rating
    sim._elapsed = 900.0
    sim.offered, sim.score = 1000, 990
    assert _rating(sim) == "A+"
    sim.score = 900
    assert _rating(sim) == "A"
    sim.score = 500                     # a bust in an otherwise fine hour
    assert _rating(sim) == "C"
    sim.busts = 3
    assert _rating(sim) == "F"          # three strikes, whatever the score
    sim.busts, sim.offered = 0, 100
    assert _rating(sim) == "—"          # too little concluded to judge


def test_grade_is_pure_and_liveable():
    # the header calls this every frame, mid-shift — no sim required
    from blips.game.app import _grade
    assert _grade(990, 1000, 0, 900.0) == "A+"
    assert _grade(990, 1000, 0, 60.0) == "—"     # too young to judge
    assert _grade(100, 100, 0, 900.0) == "—"     # too little concluded
    assert _grade(2000, 2000, 3, 900.0) == "F"   # three strikes


def test_busts_cap_the_grade_instead_of_poisoning_the_ratio():
    from blips.game.app import _grade
    # the −500 still prices safety into the score; the cap is the ceiling
    assert _grade(990, 1000, 1, 900.0) == "B+"   # perfect ratio, one bust
    assert _grade(700, 1000, 1, 900.0) == "B"    # ratio worse than the cap
    assert _grade(990, 1000, 2, 900.0) == "C"    # two busts hold C
    assert _grade(300, 1000, 2, 900.0) == "D"    # the cap never lifts a letter
    # an early bust is no longer a standing F: score < 0 reads F off the
    # ratio while it lasts, and recovers as the hour is worked
    assert _grade(-400, 200, 1, 900.0) == "F"
    assert _grade(2500, 3000, 1, 3600.0) == "B+"  # played on, earned it back


def test_active_cap_loosens_with_survival(sim):
    """The room grows with the shift: sixteen strips at the top of the
    hour, one more every twenty minutes — the spawner that used to stop
    at 16 keeps dealing once you've survived long enough to earn it."""
    sim._next_push = 1e9                # no push: the cap alone decides
    for i in range(16):
        _arrival(sim, callsign=f"DAL{100 + i}",
                 lat=sim.airport["lat"] + 0.2 + i * 0.03)
    sim._next_arrival = 0.0
    sim._spawn_tick(0.0)
    assert len(sim.aircraft) == 16      # minute zero: the old wall holds
    sim._elapsed = 75 * 60.0            # minute 75: three notches looser
    for _ in range(12):
        sim._next_arrival = 0.0
        sim._spawn_tick(0.0)
    assert len(sim.aircraft) == 19      # 16 + 75//20, and not one more


def test_shift_card_reads_the_rate_based_best(sim):
    from blips.game.app import _shift_card
    sim._elapsed = 900.0
    sim.offered, sim.score = 1000, 900
    best = {"score": 700, "rating": "A", "ratio": 0.93,
            "minutes": 15, "when": 0}
    entry = {"shifts": 3, "landed": 12, "handed": 8, "busts": 0,
             "best": best}
    card = _shift_card(sim, sim.airport, 1, live_cast=False,
                       entry=entry, prev=best)     # best unmoved: not beaten
    assert "personal best here A (93% in 15 min)" in card


def test_shift_card_names_the_record_it_broke(sim):
    from blips.game.app import _shift_card
    sim._elapsed = 900.0
    sim.offered, sim.score = 1000, 900
    prev = {"score": 3000, "rating": "B+", "ratio": 0.85,
            "minutes": 40, "when": 0}
    entry = {"shifts": 3, "landed": 12, "handed": 8, "busts": 0,
             "best": {"score": 900, "rating": "A", "ratio": 0.9,
                      "minutes": 15, "when": 1}}   # this shift took it
    card = _shift_card(sim, sim.airport, 1, live_cast=False,
                       entry=entry, prev=prev)
    assert "new personal best — previous B+ (85% in 40 min)" in card


# -- hearback -------------------------------------------------------------------

def test_misheard_readback_is_flown_until_corrected(sim):
    ac = _arrival(sim, alt=13000.0)
    sim.hearback_p = 1.0
    sim._elapsed = 300.0                # past the settling-in grace
    line = sim.command("100 d 70")
    assert "descend and maintain" in line.lower()
    assert ac["tgt_alt"] != 7000.0      # they heard a different number
    assert abs(ac["tgt_alt"] - 7000.0) == 1000.0
    assert "seven thousand" not in line  # and the readback says so
    assert sim.hearbacks == 1
    sim.hearback_p = 0.0                # the correction gets through
    line = sim.command("100 d 70")
    assert "seven thousand" in line
    assert ac["tgt_alt"] == 7000.0
    assert sim.hearbacks_caught == 1
    assert sim.score == 25              # a catch is worth something
    assert sim.ledger[-1] == "hearback caught · +25"


def test_misheard_heading_is_one_value_off(sim):
    ac = _arrival(sim, hdg=360.0)
    sim.hearback_p = 1.0
    sim._elapsed = 300.0
    sim.command("100 r 90")
    assert ac["tgt_hdg"] != 90.0
    assert abs(ac["tgt_hdg"] - 90.0) in (10.0, 20.0)
    assert ac["turn_dir"] == "r"        # the turn direction survived


def test_no_mishears_while_settling_in(sim):
    ac = _arrival(sim, alt=13000.0)
    sim.hearback_p = 1.0                # even at certainty...
    assert sim._elapsed < 180.0         # ...the first minutes are clean
    sim.command("100 d 70")
    assert ac["tgt_alt"] == 7000.0
    assert sim.hearbacks == 0


def test_unflyable_mishearing_is_heard_right(sim):
    # a B738's clean minimum is 210: "rs 210" misheard as 200 falls off
    # the envelope, the pilot can't fly it, and the real instruction is
    # what quietly sticks — the player never eats an error they didn't
    # cause (misheard as 220 it stays flyable, hence the loop)
    ac = _arrival(sim, ias=215.0)
    sim.hearback_p = 1.0
    sim._elapsed = 300.0
    fell_back = False
    for _ in range(12):
        ac["tgt_ias"] = ac["ias"] = 215.0
        line = sim.command("100 rs 210")
        assert "unable" not in line     # the player never eats the error
        if ac["tgt_ias"] == 210.0:
            fell_back = True            # rs 220 was unflyable: heard right
    assert fell_back


# -- terrain ------------------------------------------------------------------

class _Hills:
    """A fake MVA grid: 7,300 ft east of the field, 2,100 ft west."""

    def __init__(self, split_lon):
        self.split = split_lon

    def mva_at(self, lat, lon):
        return 7300.0 if lon > self.split else 2100.0


@pytest.fixture()
def hilly(sim):
    sim.terrain = _Hills(sim.airport["lon"])
    return sim


def test_descent_below_mva_is_refused(hilly):
    ap = hilly.airport
    _arrival(hilly, lat=ap["lat"] + 0.3, lon=ap["lon"] + 0.5, alt=12000.0)
    line = hilly.command("100 d 40")
    assert "minimum vectoring altitude" in line
    assert "seven thousand three hundred" in line
    # same descent over the flat side is fine
    _arrival(hilly, callsign="DAL200",
             lat=ap["lat"] + 0.3, lon=ap["lon"] - 0.5, alt=12000.0)
    assert "descend and maintain" in hilly.command("200 d 40").lower()


def test_descending_into_rising_terrain_levels_off(hilly):
    ap = hilly.airport
    # cleared down over the flats, drifting east into the hills
    ac = _arrival(hilly, lat=ap["lat"] + 0.3, lon=ap["lon"] - 0.05,
                  alt=9000.0, hdg=90.0)
    hilly.command("100 d 40")
    _run(hilly, 600)
    assert ac["alt"] == 7300.0          # held at the MVA, not the target
    assert any("terrain below us" in line for _t, line, _k in hilly.radio)


def test_ils_descends_below_mva(hilly):
    # the approach is a surveyed path: the glideslope may go below the
    # grid's MVA even when the field sits in a high-MVA cell
    hilly.terrain = _Hills(hilly.airport["lon"] - 10.0)  # 7,300 everywhere
    thr = hilly.sector["thr"]
    course = hilly.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(hilly, lat=lat, lon=lon, alt=8000.0, hdg=course, ias=200.0)
    hilly.command("100 d 30")           # refused: below MVA
    assert ac["tgt_alt"] == 8000.0
    hilly.command("100 i")
    _run(hilly, 900)
    assert hilly.landed == 1            # rode the slope down regardless


def test_a_landed_flight_never_calls_terrain(hilly):
    # the tick that touches down used to fall through to the terrain guard —
    # a flight already off the scope announcing it was leveling at the MVA
    hilly.terrain = _Hills(hilly.airport["lon"] - 10.0)  # 7,300 everywhere
    thr = hilly.sector["thr"]
    course = hilly.sector["course"]
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    _arrival(hilly, lat=lat, lon=lon, alt=8000.0, hdg=course, ias=200.0)
    hilly.command("100 i")
    _run(hilly, 900)
    assert hilly.landed == 1
    assert not any("terrain below us" in line
                   for _t, line, _k in hilly.radio)
