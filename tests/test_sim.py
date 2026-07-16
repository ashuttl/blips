"""The sim: honest kinematics, ILS geometry, separation, sector rules."""

import pytest

from blips._airports import find_airport
from blips._commands import CommandError
from blips._geo import advance, cross_along_track, haversine_nm, turn_delta
from blips._sim import PERF, Sim, build_sector


@pytest.fixture()
def sim():
    """A quiet TPA sector: the spawner is parked so tests own the traffic."""
    s = Sim(find_airport("tpa"), seed=1)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s.hearback_p = 0.0          # pilots hear perfectly unless a test says
    s.wind = (360.0, 0.0)       # calm air unless a test brings weather
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
    assert "reduce speed two one zero" in ok


def test_unknown_fix_and_wrong_plan(sim):
    _arrival(sim)
    assert "unfamiliar with ZZZZZ" in sim.command("100 dct zzzzz")
    assert "arrival" in sim.command("100 ho")       # arrivals aren't handed off


def test_readback_uses_telephony(sim):
    _arrival(sim, callsign="RPA5655")
    line = sim.command("5655 r 270")
    assert line.startswith("Brickyard 5655, turn right heading two seven zero")


# -- the approach -------------------------------------------------------------

def test_ils_capture_glideslope_landing(sim):
    thr = sim.sector["thr"]
    course = sim.sector["course"]
    # 12 nm out on the extended centreline, 3,000 ft, pointed at the runway
    lat, lon = advance(*thr, (course + 180.0) % 360.0, 12.0)
    ac = _arrival(sim, lat=lat, lon=lon, alt=3000.0, hdg=course, ias=200.0)
    line = sim.command("100 i")
    assert "cleared ILS runway" in line
    _run(sim, 60)
    assert ac["phase"] == "established"
    _run(sim, 600)
    assert ac not in sim.aircraft       # flown down the slope and landed
    assert sim.landed == 1
    assert sim.score == 100


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
    assert ac in sim.aircraft           # back with you for another try
    assert ac["phase"] == "cruise"


def test_hold_orbits_and_vectors_cancel_it(sim):
    ac = _arrival(sim, alt=8000.0)
    line = sim.command("100 hold")
    assert "hold present position, right turns" in line
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
    assert f"hold at {fix}" in line
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

def test_sector_is_deterministic_per_airport():
    ap = find_airport("tpa")
    s1, s2 = build_sector(ap), build_sector(ap)
    assert s1["fixes"] == s2["fixes"]   # TPA's corner posts never move
    assert s1["rwy"] == s2["rwy"]
    assert len(s1["fixes"]) == 8
    for lat, lon in s1["fixes"].values():
        d = haversine_nm(lat, lon, ap["lat"], ap["lon"])
        assert 21.0 < d < 53.0          # the real-navaid gate band


def test_sector_gates_are_real_navaids_where_possible():
    # London is ringed with famous VORs; all eight gates should be real
    s = build_sector(find_airport("egll"))
    real = [n for n in s["fixes"] if len(n) <= 3]
    assert len(real) == 8
    assert "BKY" in s["fixes"]          # Barkway, the classic north gate
    # Tampa has thinner coverage: real where possible, synthesized elsewhere
    s = build_sector(find_airport("tpa"))
    assert "LAL" in s["fixes"] and "SRQ" in s["fixes"]


def test_shift_starts_populated():
    s = Sim(find_airport("tpa"), seed=1)
    arrivals = [a for a in s.aircraft if a["plan"] == "arrival"]
    departures = [a for a in s.aircraft if a["plan"] == "departure"]
    assert len(arrivals) >= 2 and len(departures) >= 1
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


def test_departure_handoff_rules(sim):
    sim._next_departure = 0.0
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    suffix = dep["callsign"][3:]
    assert "won't take" in sim.command(f"{suffix} ho")   # still on the field
    # teleport them out to their exit fix: now centre wants them
    dep["lat"], dep["lon"] = sim.sector["fixes"][dep["fix"]]
    line = sim.command(f"{suffix} ho")
    assert "switching" in line
    assert sim.score == 50


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
    from blips._sim import FLEETS
    for airline, types in FLEETS.items():
        for t in types:
            assert t in PERF, f"{airline} flies {t} but PERF doesn't know it"


def test_type_aliases_land_in_perf():
    from blips._fleet import TYPE_ALIAS
    for real, alias in TYPE_ALIAS.items():
        assert alias in PERF, f"{real} → {alias} but PERF doesn't know it"


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
    assert "turn left heading" in sim.command("100 l 270")  # away: fine


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
    assert "turn right heading" in sim.command("100 r 90")


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
    assert "turn left heading" in sim.command("100 l 360")


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
    assert f"runway {s.sector['rwy']}" in first[1]
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
    assert line.startswith("Speedbird 12 heavy,")
    _arrival(sim, callsign="DAL200", actype="B738")
    assert sim.command("200 l 270").startswith("Delta 200,")


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


def test_spawned_arrivals_carry_a_reachable_par(sim):
    sim._spawn_arrival()
    ac = sim.aircraft[-1]
    # par ≈ 16 s/nm plus five minutes of slack: a straight-in at working
    # speeds beats it comfortably, a couple of laps does not
    dist = haversine_nm(ac["lat"], ac["lon"],
                        sim.airport["lat"], sim.airport["lon"])
    assert dist * 14.0 < ac["par"] < dist * 16.0 + 320.0


def test_rating_is_score_against_offered(sim):
    from blips._game import _rating
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


# -- hearback -------------------------------------------------------------------

def test_misheard_readback_is_flown_until_corrected(sim):
    ac = _arrival(sim, alt=13000.0)
    sim.hearback_p = 1.0
    sim._elapsed = 300.0                # past the settling-in grace
    line = sim.command("100 d 70")
    assert "descend and maintain" in line
    assert ac["tgt_alt"] != 7000.0      # they heard a different number
    assert abs(ac["tgt_alt"] - 7000.0) == 1000.0
    assert "seven thousand" not in line  # and the readback says so
    assert sim.hearbacks == 1
    sim.hearback_p = 0.0                # the correction gets through
    line = sim.command("100 d 70")
    assert "seven thousand" in line
    assert ac["tgt_alt"] == 7000.0
    assert sim.hearbacks_caught == 1


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
    assert "descend and maintain" in hilly.command("200 d 40")


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
