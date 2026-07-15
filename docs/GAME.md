# blips --game

An approach-control sim hiding inside the scope. The live ADS-B feed steps
aside; simulated flights take its place, and they're *yours*. You work a
TRACON-scale sector around a real airport — real runways, real coastline,
real weather overhead — and you control everything the way real controllers
wish they could: by typing.

```
blips --game            # approach control at the airport nearest you
blips --game tpa        # Tampa approach
blips --game egll       # Heathrow director
blips --game haneda     # place names geocode like they do for the scope
```

## The loop

Arrivals check in at the sector boundary, descending, wanting a runway.
Departures come off the runway climbing, wanting an exit fix. You vector,
climb, descend, and sequence them; the sim flies whatever you say. Traffic
starts polite and thickens the longer you last. The shift never ends — you
do, when the picture finally gets away from you.

Score comes from working aircraft: a landing, a clean handoff at the
boundary. Losing separation — two aircraft closer than **3 nm laterally and
1,000 ft vertically** — costs you heavily, flashes the pair red, and goes on
your record. The header keeps the tally: time on shift, aircraft worked,
busts, score.

## Talking to aircraft

Everything is typed into the command bar. The grammar is terse going in and
full phraseology coming back — the readback line answers the way a pilot
would, so playing it teaches you to sound like a controller.

```
> rpa5655 l 230 c 40
  Brickyard 5655: left heading 230, climb and maintain 4,000.
```

A command is a callsign followed by one or more instructions, chained:

| you type            | meaning                                        |
| ------------------- | ---------------------------------------------- |
| `l 230` / `r 230`   | turn left/right heading 230                    |
| `h 230`             | fly heading 230 (shortest turn)                |
| `c 110` / `d 110`   | climb/descend and maintain (hundreds of feet, like the data blocks: `110` = 11,000 ft, `240` = FL240; the wrong verb is quietly corrected) |
| `s 210`             | speed 210 knots; bare `s` resumes normal speed |
| `dct LOOSE`         | proceed direct a fix                           |
| `i` / `i 19L`       | cleared ILS approach (assigned runway, or say which) |
| `ho`                | handoff — at the boundary near their exit fix, this banks the flight |

Callsigns abbreviate: any unique suffix works, so `rpa5655`, `5655`, and
`55` all reach Brickyard 5655 (ambiguity gets you "multiple aircraft match
55"; nonsense gets you a pilot's "say again"). Clicking a blip drops its
callsign into the bar. Airline telephony comes from a vendored table —
`RPA` reads back as Brickyard, `SWA` as Southwest, `BAW` as Speedbird.

Words the sim understands without a callsign: `pause`, `q`/`quit`, `?` for
help. `+`/`-` still zoom while the bar is empty; Esc clears it.

## The sector

A ~45 nm ring around one real airport. Airports and runways come from a
vendored, trimmed [OurAirports](https://ourairports.com/data/) dataset
(public domain): real identifiers, real headings, real lengths. Entry and
exit fixes are synthesized — five-letter, pronounceable, deterministically
seeded by the airport code, so TPA's corner posts are always in the same
places and learning the sector means something.

Arrivals spawn at entry fixes between 11,000 and 15,000 ft; your job ends
when they're established on the localizer — cleared, coupled, and handed to
tower, the sim flies the approach and the landing. Departures appear off
the runway passing ~1,500 ft; climb them, point them at their exit fix, and
hand them off at the edge.

## How they fly

Simple physics, honest feel: standard-rate turns (3°/s — a jet takes a full
minute to come around 180°), climbs and descents at type-plausible rates,
speed changes that take time. Types are drawn from a small performance
table (a 737 is not a Dash 8), matched loosely to airline fleets. Pilots
say "unable" to speeds outside their envelope.

Weather is the house specialty: the same live radar underlay the scope
renders is terrain the sim's pilots respect. Vector someone into a heavy
echo and they'll refuse and ask for a deviation — the real weather over
your real airport becomes the game's terrain, different every session.
(Ships after the core loop; the seam is designed in from the start.)

## Under the hood

The renderer never knows the difference. `SimFeed` produces the same
aircraft dicts the ADS-B poller does and answers the same `snapshot()`
call, so every layer of the scope — basemap, blips, data blocks, trails,
leaders, weather, drag, zoom — works untouched. The sim ticks on the live
loop's existing animation cadence; each frame advances the world by
wall-clock dt and stamps `fix_time = now`, so the scope's dead-reckoning
glide becomes a no-op rather than a double integration.

New pieces, following the private-module convention:

| module          | job                                                       |
| --------------- | --------------------------------------------------------- |
| `_airports.py`  | vendored airport/runway lookup (`data/airports.json.gz`)  |
| `_sim.py`       | kinematics, flight plans, spawner, separation monitor     |
| `_commands.py`  | parser, callsign matching, phraseology readback           |
| `_game.py`      | `SimFeed`, command bar, HUD, `--game` wiring              |

Scoring v1: +100 landing, +50 handoff, −500 separation bust (debounced —
one bust per pair until separation is restored), small bonus for low
average delay. Difficulty: arrival rate ramps from ~8/hr, active-count
capped so the scope stays readable.
