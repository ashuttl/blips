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
    s._next_arrival = s._next_departure = 1e9
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
        assert 44.0 < d < 46.0


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
