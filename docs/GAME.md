# blips --game

An approach-control sim hiding inside the scope. The live ADS-B feed steps
aside; simulated flights take its place, and they're *yours*. You work a
TRACON-scale sector around a real airport — real runways, real navaids,
real terrain, real weather overhead — and you control everything the way
real controllers wish they could: by typing.

```
blips --game            # approach control at the airport nearest you
blips --game tpa        # Tampa approach
blips --game egll       # Heathrow director
blips --game billings   # somewhere the terrain pushes back
```

The flag is deliberately missing from `--help`. You found it; it's yours.

## The loop

You take over a sector that's already busy — a couple of arrivals inbound
from the gates, a departure climbing out. Arrivals want a runway;
departures want their exit fix. You vector, climb, descend, and sequence
them; the sim flies whatever you say. Traffic thickens the longer you
last. The shift never ends — you do, when the picture finally gets away
from you.

Score comes from working aircraft: a landing, a clean handoff at the
boundary. Losing separation — two aircraft closer than **3 nm laterally
and 1,000 ft vertically** — costs you heavily, flashes the pair red, and
goes on your record. A go-around costs a little and puts the arrival
back in your pattern. The header keeps the tally.

## Talking to aircraft

Everything is typed into the command bar. The grammar is terse going in
and full phraseology coming back — the readback line answers the way a
pilot would, so playing it teaches you to sound like a controller.

```
> rpa5655 l 230 c 40
  Brickyard 5655, turn left heading two three zero, climb and maintain 4,000.
```

A command is a callsign followed by one or more instructions, chained:

| you type            | meaning                                        |
| ------------------- | ---------------------------------------------- |
| `l 230` / `r 230`   | turn left/right heading 230                    |
| `c 110` / `d 110`   | climb/descend and maintain (hundreds of feet, like the data blocks: `110` = 11,000 ft, `240` = FL240) |
| `rs 180` / `is 250` | reduce/increase speed (knots); bare `s` resumes normal speed |
| `dct LAL`           | proceed direct a fix                           |
| `hold` / `hold LAL` | hold present position / at a fix, right turns  |
| `i` / `i 19L`       | cleared ILS approach (active runway, or say which) |
| `ho`                | handoff — near their exit fix, this banks a departure |

Direction is always yours to give — there is no "fly heading" shortcut
that picks the turn for you, and a climb instruction to an aircraft above
the altitude gets a puzzled "unable climb, we're at one three thousand"
rather than a silent fix. Speaking correctly requires holding the picture,
which is the actual skill.

Callsigns abbreviate: any unique suffix works (`rpa5655`, `5655`, `55`),
a space is forgiven (`ual 71` ≡ `ual71`), and clicking a blip drops its
callsign into the bar. Ambiguity gets you "multiple aircraft match";
nonsense gets a pilot's "say again". Airline telephony comes from a
vendored table — `RPA` reads back as Brickyard, `BAW` as Speedbird.

Words the sim understands without a callsign: `pause`, `q`/`quit`,
`w`/`weather`, `?` for help. `+`/`-` zoom while the bar is empty; Esc
clears it; up-arrow recalls history.

## The sector

A ~45 nm ring around one real airport (vendored, trimmed
[OurAirports](https://ourairports.com/data/) data, public domain: 4,472
fields with real runways). The corner posts are **real radio navaids** —
the best VOR/NDB in each 45° octant of the gate band, from the same
public-domain dataset — so Heathrow's gates are Barkway, Mayfield,
Southampton and Daventry, and learning a sector means learning its actual
geography. A synthesized five-letter fix fills any octant the real world
left empty. Deterministic per airport, always.

Arrivals check in at entry gates between 11,000 and 16,000 ft; your job
ends when they're established on the localizer — cleared, coupled, the
sim flies the approach and the landing. Departures appear off the runway
passing ~1,500 ft (the tower meters releases so they don't stack up);
climb them, point them at their exit fix, hand them off at the edge.

Range rings stay pinned to your airport when you pan, with compass
headings marked on the second ring — "turn left heading two three zero"
has somewhere to point.

## The cast

At the start of a shift, one background fetch samples the **real traffic
currently within 250 nm** of your airport (same community ADS-B feed the
scope watches) and their real routes fill in from the route API. The
spawner draws from that pool: a session at TPA is Southwest 737s and
Breeze E-Jets, arrivals check in "with you, one three thousand, inbound
LAL, **from Baltimore**", departures want their gate "**for Denver**".
Flights whose real route runs the other way never spawn in the wrong
direction. Offline it degrades to a country-plausible airline mix.

## How they fly

Simple physics, honest feel: standard-rate turns (3°/s — a jet takes a
full minute to come around 180°), type-plausible climbs and speeds, speed
changes that take time. Pilots say "unable" to what they can't do, ask
for things now and then ("requesting lower"), and read back what they
will.

**The ILS** captures the way you'd hope: clear them on a sane intercept
(30–40° works; up to ~90° will lock on, leading the turn) and they take
it from there — localizer, glideslope, slowing to approach speed,
switched to tower at five miles. Clear them pointed away and you'll hear
about it. Blow the energy management — still hot or high on short final —
and they **go around**, back into your pattern for another try.

**Terrain is real.** A background fetch builds a minimum-vectoring-
altitude grid from real elevation (Open-Meteo, one grid per shift).
Around Tampa you'll never hear about it; at Billings the pilots refuse
descents into the Beartooths — "unable four thousand, minimum vectoring
altitude here is seven thousand three hundred" — and a descent drifting
into rising ground levels off with a complaint. The ILS, a surveyed path,
is exempt. Offline the world is flat.

**Weather is gameplay.** The same live radar frame the scope renders is
what the sim's pilots see out the windscreen. Vector someone into a heavy
cell and they refuse, with a suggestion ("unable — that heading puts us
into a cell, we could take further left"). Fly them at one and they ask
for a deviation; ignore them for twenty seconds and they take it
themselves, then call clear of weather and wait for your vector. The real
storms over your real airport are the game's terrain, different every
session. Turn weather off (`w`) and the sky is honest: nobody complains.

**The day changes.** Ten-odd minutes in, the ATIS updates: the wind has
come around, and the airport turns with it — new landing runway (the
reciprocal end), new localizer on the map, departures rolling the other
way. Anyone established rides their approach in; anyone merely cleared
hears "cancel approach clearance, expect the new runway" and is yours to
re-sequence. This is the hardest moment in real approach control, on
purpose.

**Sometimes it's not routine.** At most once a shift, an arrival declares
a medical emergency — squawk 7700, the blip goes red and stays red, and
they want the field *now*. Get them down inside twelve minutes for a
+300 bonus; everyone else can wait. Your terminal bell rings for maydays
and separation losses, because you're heads-down typing when it matters.

## Under the hood

The renderer never knows the difference. `Sim` produces the same aircraft
dicts the ADS-B poller does and answers the same `snapshot()` call, so
every layer of the scope — basemap, blips, data blocks, trails, leaders,
weather, drag, zoom — works untouched. The sim ticks on the live loop's
animation cadence; `fix_time` is stamped to the present so the scope's
dead-reckoning glide becomes a no-op.

| module          | job                                                       |
| --------------- | --------------------------------------------------------- |
| `_airports.py`  | vendored airports/runways/navaids (`data/*.json.gz`)      |
| `_sim.py`       | kinematics, ILS, sector, spawner, separation, scoring     |
| `_commands.py`  | parser, callsign matching, phraseology readback           |
| `_fleet.py`     | live-sampled traffic pool with real routes                |
| `_terrain.py`   | real-elevation MVA grid                                   |
| `_game.py`      | command bar, radio log, HUD, `--game` wiring              |

Scoring: +100 landing, +50 handoff, −500 separation bust (debounced),
−50 go-around, −100 leaving the sector unworked. Arrival rate ramps from
~18/hr toward ~40/hr; active count capped so the scope stays readable.
