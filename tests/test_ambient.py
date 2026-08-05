"""The sky that isn't yours: VFR intruders, overflights, balloons,
the satellite field, and centre's moods.

The design rule under test throughout: ambient traffic adds richness to
the *picture* without adding anything to the roster — it never scores,
never talks, and never counts against the player except as the near-miss
they should have called traffic on.
"""

import pytest

from blips._airports import find_airport
from blips._geo import advance, haversine_nm
from blips.game.sim import DESPAWN_NM, GA_PERF, Sim, build_sector
from blips.scope import data_block


@pytest.fixture()
def sim():
    """A quiet TPA sector, every spawner parked — tests own the sky."""
    s = Sim(find_airport("tpa"), seed=1)
    s._next_arrival = s._next_departure = s._next_request = 1e9
    s._next_vfr = s._next_over = s._next_sat_dep = 1e9
    s._balloon_event = 2
    s._next_abnormal = 1e9      # nothing goes wrong unless a test says
    s.flow_hold_p = 0.0         # a forced flow change always turns the field
    s.hearback_p = 0.0
    s.wind = (360.0, 0.0)
    s._aloft = (0.0, 1.0)       # one wind at every altitude unless layered
    s.react_s = (0.0, 0.0)
    s.aircraft.clear()
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
              course=sim.sector["course"], felev=float(ap["elev"]))
    sim.aircraft.append(ac)
    return ac


def _vfr(sim, callsign="N42XY", lat=None, lon=None, alt=2500.0, hdg=90.0):
    ap = sim.airport
    ac = sim._base(callsign, "C172",
                   ap["lat"] + 0.3 if lat is None else lat,
                   ap["lon"] if lon is None else lon, alt, hdg,
                   float(GA_PERF["C172"][0]))
    ac.update(plan="vfr", squawk="1200", limited=True,
              vfr_turn=1e9, vfr_leave=1e9)
    sim.aircraft.append(ac)
    return ac


def _run(sim, seconds, step=1.0):
    t = sim._last_tick if sim._last_tick is not None else sim.start
    sim._last_tick = t
    for _ in range(int(seconds / step)):
        t += step
        sim.tick(t)
    return t


# -- VFR intruders --------------------------------------------------------

def test_vfr_spawns_low_slow_and_silent(sim):
    sim.radio.clear()
    sim._spawn_vfr()
    ac = sim.aircraft[-1]
    assert ac["plan"] == "vfr"
    assert ac["squawk"] == "1200"
    assert ac["limited"]
    assert ac["callsign"].startswith("N")
    assert ac["alt"] <= sim.airport["elev"] + 5500.0
    assert not sim.radio                 # nobody checked in

def test_vfr_is_not_on_your_frequency(sim):
    ac = _vfr(sim)
    line = sim.command(f"{ac['callsign']} d 20")
    assert "isn't on your frequency" in line
    assert "twelve hundred" in line

def test_vfr_wanders_but_flies(sim):
    ac = _vfr(sim)
    start = (ac["lat"], ac["lon"])
    _run(sim, 60)
    assert haversine_nm(*start, ac["lat"], ac["lon"]) > 1.0

def test_vfr_never_costs_three_miles(sim):
    a = _arrival(sim, alt=2500.0)
    _vfr(sim, lat=a["lat"] + 2.0 / 60.0, lon=a["lon"], alt=2500.0)
    sim._separation()
    assert sim.busts == 0                # 2 nm from a 1200 code is legal
    assert sim.score == 0

def test_nmac_scores_once_and_rings(sim):
    a = _arrival(sim, alt=2500.0)
    _vfr(sim, lat=a["lat"] + 0.5 / 60.0, lon=a["lon"], alt=2400.0)
    sim._separation()
    assert sim.nmacs == 1
    assert sim.score == -200
    assert a["emergency"]
    assert "TRAFFIC ALERT" in sim.radio[-1][1]
    assert sim.ledger == ["traffic alert · −200"]
    sim._separation()                    # debounced while still close
    assert sim.nmacs == 1
    assert sim.score == -200

def test_traffic_in_sight_prevents_the_deal(sim):
    a = _arrival(sim, alt=2500.0)
    t = _vfr(sim, lat=a["lat"] + 0.5 / 60.0, lon=a["lon"], alt=2400.0)
    a["visual"] = {t["hex"]}
    sim._separation()
    assert sim.nmacs == 0
    assert sim.score == 0
    assert not a["ca"]


# -- the traffic call -----------------------------------------------------

def test_tfc_calls_the_target_and_pilot_sees_it(sim):
    a = _arrival(sim, alt=2500.0, hdg=360.0)
    t = _vfr(sim, lat=a["lat"] + 3.0 / 60.0, lon=a["lon"], alt=2400.0)
    sim.rng.random = lambda: 0.0         # eyes like a hawk today
    line = sim.command("100 tfc")
    assert "traffic in sight" in line.lower()
    assert t["hex"] in a["visual"]
    assert any("twelve o'clock" in said and kind == "atc"
               for _t, said, kind in sim.radio)

def test_tfc_negative_contact_grants_nothing(sim):
    a = _arrival(sim, alt=2500.0, hdg=360.0)
    _vfr(sim, lat=a["lat"] + 7.0 / 60.0, lon=a["lon"], alt=2400.0)
    sim.rng.random = lambda: 0.99        # staring into the haze
    line = sim.command("100 tfc")
    assert "negative contact" in line.lower()
    assert not a.get("visual")

def test_tfc_with_an_empty_sky(sim):
    _arrival(sim)
    assert "no traffic to call" in sim.command("100 tfc")


# -- overflights ----------------------------------------------------------

def test_overflight_is_high_dim_scenery(sim):
    sim._spawn_overflight()
    ac = sim.aircraft[-1]
    assert ac["plan"] == "overflight"
    assert ac["alt"] >= 28000.0
    assert ac["dim"]
    line = sim.command(f"{ac['callsign']} d 100")
    assert "they're with centre" in line

def test_overflight_leaves_without_a_ledger_entry(sim):
    sim._spawn_overflight()
    ac = sim.aircraft[-1]
    ac["lat"], ac["lon"] = advance(sim.airport["lat"], sim.airport["lon"],
                                   45.0, DESPAWN_NM + 5.0)
    sim.radio.clear()
    _run(sim, 2)
    assert ac not in sim.aircraft
    assert sim.score == 0
    assert not any(kind == "alert" for _t, _l, kind in sim.radio)


# -- balloons ---------------------------------------------------------------

def test_balloons_render_the_wind(sim):
    sim.wind = (90.0, 10.0)              # easterly, 10 kt
    sim._spawn_balloons()
    balloons = [ac for ac in sim.aircraft if ac["plan"] == "balloon"]
    assert 2 <= len(balloons) <= 3
    assert any("hot air balloons" in line and kind == "atis"
               for _t, line, kind in sim.radio)
    _run(sim, 10)
    b = balloons[0]
    assert abs(b["gs"] - 10.0) < 0.5     # exactly the ATIS wind
    assert abs(b["track"] - 270.0) < 5.0  # drifting downwind
    assert b["limited"] and b["glyph"] == "○"

def test_balloons_go_down_and_say_so(sim):
    sim.wind = (90.0, 8.0)
    sim._spawn_balloons()
    for ac in sim.aircraft:
        if ac["plan"] == "balloon":
            ac["balloon_down"] = 0.0
    _run(sim, 2)
    assert not any(ac["plan"] == "balloon" for ac in sim.aircraft)
    assert any("balloons are down" in line for _t, line, _k in sim.radio)


# -- the satellite field ----------------------------------------------------

def test_tpa_sector_has_a_satellite():
    sector = build_sector(find_airport("tpa"))
    assert sector["sat"] is not None
    assert sector["sat"]["rwy"]
    assert sector["sat"]["code"]

def test_satellite_arrival_lands_at_the_satellite(sim):
    sat = sim.sector["sat"]
    assert sat is not None
    final = advance(sat["thr"][0], sat["thr"][1],
                    (sat["course"] + 180.0) % 360.0, 8.0)
    ac = _arrival(sim, lat=final[0], lon=final[1],
                  alt=sat["elev"] + 2600.0, hdg=sat["course"], ias=180.0)
    ac.update(sat=True, tag=sat["code"], felev=float(sat["elev"]),
              rwy=sat["rwy"], thr=sat["thr"], course=sat["course"])
    line = sim.command("100 i")
    assert "cleared ils" in line.lower()
    _run(sim, 420)
    assert sim.landed == 1

def test_satellite_departure_wears_the_scratchpad(sim):
    sat = sim.sector["sat"]
    sim._spawn_departure(sat=sat)
    ac = sim.aircraft[-1]
    assert ac["sat"] and ac["tag"] == sat["code"]
    assert haversine_nm(ac["lat"], ac["lon"], sat["thr"][0],
                        sat["thr"][1]) < 3.0
    assert any("off" in line and kind == "checkin"
               for _t, line, kind in sim.radio)

def test_flow_change_turns_the_satellite_too(sim):
    before = sim.sector["sat"]["rwy"]
    sim._next_flow = 0.0
    _run(sim, 2)
    assert sim.sector["sat"]["rwy"] != before


# -- centre is a character ---------------------------------------------------

def _departure_at_fix(sim):
    sim._spawn_departure()
    dep = sim.aircraft[-1]
    dep.pop("xr", None)
    dep["lat"], dep["lon"] = sim.sector["fixes"][dep["fix"]]
    return dep

def test_saturated_center_refuses_handoffs(sim):
    dep = _departure_at_fix(sim)
    sim._center_until = sim._elapsed + 100.0
    line = sim.command(f"{dep['callsign']} ho")
    assert "sector's full" in line
    assert dep["phase"] != "handed"
    sim._center_until = 0.0
    assert "switching" in sim.command(f"{dep['callsign']} ho").lower()

def test_crossing_restriction_blocks_a_low_handoff(sim):
    dep = _departure_at_fix(sim)
    dep["xr"] = 11000.0
    line = sim.command(f"{dep['callsign']} ho")
    assert "climb them first" in line
    sim.command(f"{dep['callsign']} c 110")
    assert "switching" in sim.command(f"{dep['callsign']} ho").lower()


# -- the scope's new vocabulary ----------------------------------------------

def test_limited_data_block_is_altitude_only():
    ac = {"ground": False, "alt": 2500.0, "callsign": "N42XY",
          "limited": True, "vrate": 0}
    assert data_block(ac) == "025"

def test_scratchpad_tag_rides_the_block():
    ac = {"ground": False, "alt": 11000.0, "tgt_alt": 11000.0,
          "callsign": "JIA72", "vrate": 0, "tag": "PIE"}
    assert data_block(ac) == "JIA72 110 PIE"

def test_saturation_means_on_your_frequency():
    """Full colour is reserved for traffic you're talking to: a 1200
    target washes toward grey (the altitude hue survives, faded), and a
    handed-off strip greys all the way to centre's shade."""
    from blips.scope import DIM, blip_color
    base = {"emergency": False, "squawk": "2345", "ground": False,
            "alt": 1700.0, "track": 90.0}
    own = blip_color(dict(base))
    vfr = blip_color(dict(base, squawk="1200", limited=True))
    ceded = blip_color(dict(base, dim=True))
    assert ceded == DIM
    assert vfr != own and vfr != DIM
    # washed means between the two: dimmer than yours, warmer than grey
    assert sum(vfr) < sum(own)
    assert all(min(o, d) - 1 <= v <= max(o, d) + 1
               for v, o, d in zip(vfr, own, DIM))

def test_handoff_greys_the_strip(sim):
    dep = _departure_at_fix(sim)
    assert not dep.get("dim")
    assert "switching" in sim.command(f"{dep['callsign']} ho").lower()
    assert dep["dim"]      # centre's traffic now, drawn like centre's
