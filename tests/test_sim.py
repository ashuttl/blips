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
    dists = sorted(haversine_nm(a["lat"], a["lon"],
                                s.airport["lat"], s.airport["lon"])
                   for a in arrivals)
    assert dists[0] < dists[-1] - 10    # one is already partway in


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
