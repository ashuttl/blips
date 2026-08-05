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
back in your pattern. The header keeps the tally and the running
grade, and every scored event names its price up there as it lands.

Expedition counts too. Every arrival carries a **par time** (the
straight-in distance at working speeds, plus room for a civilised
pattern); a landing pays 100 flown at par and loses a point for every
six seconds past it. Slowing the whole sector to a crawl is safe and
it shows. Hover an arrival and the chip wears its par clock — time in
hand, then time owed, amber once the landing starts shrinking — along
with any assigned speed still coming. The shift is graded on what
you scored against what the concluded traffic was worth, so a clean
prompt hour is an A whether the sector gave you six aircraft or
sixty — and the game keeps a
**shift book**, one page per airport, whose personal best is the best
*rate*, not the biggest pile: a short brilliant shift outranks a long
mediocre one, and the card after every shift tells you the rate still
to beat.

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
| `via CDOGG4`        | join a named SID/STAR — it flies the fixes itself |
| `hold` / `hold LAL` | hold present position / at a fix, right turns  |
| `i` / `i 19L`       | cleared ILS approach (active runway, or say which) |
| `tfc`               | traffic call — points out the nearest VFR target   |
| `ho`                | handoff — near their exit fix, this banks a departure |

Direction is always yours to give — there is no "fly heading" shortcut
that picks the turn for you, and a climb instruction to an aircraft above
the altitude gets a puzzled "unable climb, we're at one three thousand"
rather than a silent fix. Speaking correctly requires holding the picture,
which is the actual skill.

Listening is the other half. A few transmissions per hour come back
**misheard** — one number wrong, a heading ten degrees off, an altitude
a thousand foot out — and the aircraft flies what the pilot *said*, not
what you meant. The readback line is the only tell. Saying it again is
the correction (and if they've already sunk through the altitude you
wanted, the fix takes the other verb). This is hearback, it's why real
controllers never stop listening to readbacks, and a catch pays +25
and goes on the shift card. Pilots also take a **beat between reading
back and acting** — the readback is the mic click, not the bank — so
lead your turns like you mean it.

The bar shows only the latest transmission by default. Type `log` (or
`r`) to hold the **tape** open while you work — the last nine calls,
oldest at the top, the same view a pause gives you. In the tape your own
keyed transmissions appear spelled out in full phraseology, one line
above each pilot's readback, so a misheard number is there to be *read*
off the two lines rather than held in your head. You never hear your own
side; the log is where you see it.

Callsigns abbreviate: any unique suffix works (`rpa5655`, `5655`, `55`),
a space is forgiven (`ual 71` ≡ `ual71`), and clicking a blip drops its
callsign into the bar. Ambiguity gets you "multiple aircraft match";
nonsense gets a pilot's "say again". Airline telephony comes from a
vendored table — `RPA` reads back as Brickyard, `BAW` as Speedbird.

Words the sim understands without a callsign: `pause`, `q`/`quit`,
`w`/`weather`, `log`/`r` (hold the tape open), `proc` (and `arr`/`star`,
`dep`/`sid`, `plate` — the procedure overlay), `voice` (speak the
frequency aloud — macOS only), `?` for help. `+`/`-` zoom while the bar
is empty; Esc clears it; up-arrow recalls history.

On macOS, `voice` turns the pilots' words into speech through the system
`say` command. It's a radio, so it stays half-duplex — one transmission
at a time, and a call that lands while another is playing gets stepped
on. Each flight keeps **one voice for its whole life** on your frequency,
cast by the airline's nationality wherever the machine has the accent for
it: Speedbird speaks in a London voice, Shamrock in a Dublin one, Air
India in Rishi, Qantas in Karen — everyone else in a neutral US voice,
and the ATIS in its own flat recording. Off it comes clean the way
the log always has; you never hear your own side. (More accents means
more voices — the range comes from whatever voices are installed under
System Settings → Accessibility → Spoken Content.)

## The sector

A ~45 nm ring around one real airport (vendored, trimmed
[OurAirports](https://ourairports.com/data/) data, public domain: 4,472
fields with real runways). The corner posts are **the fixes the field
actually uses**: where the FAA's
[CIFP](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/)
has published procedures, a gate *is* the fix a procedure is named for.
Seattle's inbounds cross CHINS, HAWKZ, MARNR and SKYKO because that is
precisely what the CHINS FIVE, HAWKZ EIGHT, MARNR EIGHT and SKYKO ONE
arrivals do; Boston's are JFUND, OOSHN, ROBUC and WOONS; JFK's are CAMRN
and PUCKY. So the name under a corner post and the name in a `via`
clearance are the same word, and a sector can be learned once. Where a
plate's eponymous fix is absurdly far out — Kennedy files PARCH eighty
miles away — the post falls back to where that flow crosses into the
terminal area instead. Gates come off both directions of the runway and
hold still for the shift: real corner posts don't move when the wind
turns, the procedures over them change. An *exit* gate additionally has to
be out near the boundary, since that's where you throw a departure over
it; a short SID that ends in vectors twelve miles off the field still
draws, it just isn't a gate.

Octants the plates leave empty top up the old way: the best **real radio
navaid** in the gate band, VORs first, then a real named waypoint. A field
off the CIFP entirely gets that search for all eight, so Heathrow's gates
are still Barkway, Mayfield, Southampton and Daventry, and only an octant
the real world left with neither falls back to a synthesized five-letter
fix. Deterministic per airport, always.

None of this pre-positions anybody. Inbounds still enter from the true
bearing of their origin, scattered wide, and check in with a rough
position off the nearest named point — never "ready for the SCOGS four".
Getting a flight onto a procedure is your work, not the spawner's.

The named procedures are real too. `^O` thumbs through the picture —
**arrivals**, then **departures**, then **both**, then off — and the top bar
says which you're looking at, so on a busy scope you can mute the flow
you're not working. Typed words go straight to a state (`star`/`arr`,
`sid`/`dep`, `proc`); `plate` swaps the decluttered picture for the whole
published set. Off by default; the localizer and the traffic always read
first.

What draws is one stroke per procedure, ordered the way an aeroplane flies
it — arrivals in cool teal, departures in warm amber, and the **name in its
own flow's colour** rather than the grey every other label wears, hung a row
under the corner post it leaves from, so a procedure and its gate read as
one thing. A departure's stroke starts **on the runway**, where the climb-out
does. An arrival that ends on a downwind fix and hands you the aeroplane to
vector says so, with a `⇣` on the name — "then vectors to final" is what
almost every STAR really does, and a stroke that just stopped dead read as
broken data instead. A departure that ends in vectors wears `⇡`.
A busy field would drown in plates (LAX publishes 36 that serve a given
runway), so the picture is held to the flows through your own gates: the
nearest arrival to each entry, the nearest departure to each exit. That's
nine procedures at LAX instead of thirty-six.

**Hover anything and it answers.** A procedure name gives you the plate: the
fix chain with the crossing restrictions that ride on it
(`HAWKZ 120+ 270kt → LIINE → FOOTT 120 250kt → …`), the runway, the entries
that feed it, and the clearance to type. A corner post tells you which way it
flows, how far out it is, and which procedure crosses it *today* — the thing
that makes `via` and the scope agree.

And you can fly them. `via CDOGG4` clears an arrival onto that arrival (or a
departure onto its SID) and it flies the fixes itself, descending or climbing
on the procedure while you work the rest of the room. It joins at the
**earliest fix it hasn't already passed**, and the readback names it —
*"direct RADDY, descend via the CHINS FIVE arrival"* — so you can hear
whether you caught the whole procedure or only its last few miles. Position
the aeroplane first and it flies more of the plate; that's the skill, and
it's what lets a mastered field carry more traffic.

**The refusals teach.** The name has to be real and the right kind (`via
HSKEL4` to an arrival gets *"HSKEL4 is a departure"*). Name a fix instead of
a procedure — the honest mistake, since a corner post is both — and you're
told which procedure owns it: *"RADDY is a fix on the CHINS five arrival, say
via CHINS5"*. Name nothing real and you get the list of what does serve the
runway. Ask for one you can't reach and the pilot says why *and* what would
work: *"unable CHINS5 — AUBRN is 49 miles from us, MARNR8 would work from
here"*. A revision number is forgiven, the way a controller forgives it —
`via CDOGG` finds CDOGG4.

`dct` reaches any fix on a procedure, not just the corner posts, so the
advice on the radio is advice you can take. The scratchpad on the data block
wears the procedure (`EJA925 093 CDOGG4`) so you can see who's flying
themselves. An altitude *amends* the clearance (`c 230` = "climb via the SID,
then maintain FL230") without dropping it; a heading or a `dct` is you taking
it back by hand, and cancels the procedure; the ILS clearance picks up
naturally where the arrival ends. A field off the CIFP has no procedures to
join, so there it's vectors as ever.

Arrivals check in at entry gates between 11,000 and 16,000 ft; your job
ends when they're established on the localizer — cleared, coupled, the
sim flies the approach and the landing. Departures appear off the runway
passing ~1,500 ft (the tower meters releases so they don't stack up);
climb them, point them at their exit fix, hand them off at the edge.

Range rings stay pinned to your airport when you pan, with compass
headings marked on the second ring — "turn left heading two three zero"
has somewhere to point.

**A real approach control rarely works one field.** The nearest airport
with a jet runway inside the sector is the satellite — Portland has
Brunswick, Heathrow has Farnborough — drawn with its own runway and
localizer,
and some of the traffic is theirs: an arrival checks in "inbound ENE
for Brunswick" and wears the destination on its data block the way a
STARS scratchpad would, because the wrong airport should never sneak up
on you. And it's *their* traffic in the honest sense: a satellite casts
its own flights, not repainted main-field ones. A field with airline
service flies its real schedule; one without is read from its vendored
signals — the name, the runway, and the keywords column, which
remembers former names — so Brunswick Executive (né NAS Brunswick)
fills with NetJets Citations, November-something Cirruses and King
Airs, and the odd Reach or Convoy mission heavy off the 8,000-foot
runway the Navy left behind. Registrations read back the way a pilot
would say them — "November four two three tango bravo, descend and
maintain four thousand" — and any unique suffix still keys them up. Clear them for "the ILS" and they fly *their* field's approach;
their final is wherever their runway points, and so is the sequencing
problem. Satellite departures pop up low in the middle of your airspace
wanting an exit fix like anybody else — levelling a thousand feet
under the main field's climb-outs until you say otherwise, because
the letter of agreement keeps the two flows apart until they're on
your frequency. When the flow turns, the
satellite turns with it — one wind, one direction of traffic.

## The cast

At the start of a shift, one background fetch samples the **real traffic
currently within 250 nm** of your airport (same community ADS-B feed the
scope watches) and their real routes fill in from the route API. The
spawner draws from that pool: a session at TPA is Southwest 737s and
Breeze E-Jets, arrivals check in "with you, one three thousand, inbound
LAL, **from Baltimore**", departures want their gate "**for Denver**".
Flights whose real route runs the other way never spawn in the wrong
direction. Offline it degrades to a country-plausible airline mix — and
a field with no airline service at all (play Brunswick itself, or any
executive field) degrades instead to its own **traffic profile**:
bizjets, IFR general aviation and military metal in the proportions
its vendored signals call for, never a phantom airline.

## How they fly

Simple physics, honest feel: standard-rate turns (3°/s — a jet takes a
full minute to come around 180°), type-plausible climbs and speeds, speed
changes that take time. Pilots say "unable" to what they can't do, ask
for things now and then ("requesting lower"), and read back what they
will. And the speed limit isn't yours to waive: **250 below one zero
thousand** — ask for more down low and you get "unable — two five zero
below one zero thousand". A departure holds 250 through the floor and
opens up on its own; an arrival given 280 up high keeps it, then bleeds
it off through ten the way real crews do. The props never reach it, and
an emergency takes whatever it needs.

**The wind is real.** The shift opens with an ATIS ("information alpha —
wind 190 at 12, landing and departing runway 19"), and that wind is the
wind: pilots point where you say and the air carries them somewhere
slightly else, so heading and track diverge, the downwind gets pushed,
and every intercept is a little different. The header carries the wind
readout a controller glances at before every clearance.

And it doesn't stop at the surface. Aloft the air moves harder and from
somewhere else — by the mid-teens two to three times the ATIS number,
veered a few tens of degrees (backed, south of the equator) — easing
down through the friction layer the way the real atmosphere does. The
ATIS reads the surface; the groundspeeds tell the rest. An arrival
riding a 40-knot push on the downwind at 11,000 descends into slower
air, the trail spacing you built up high concertinas on the way down,
and **compression on final** — the defining tax of approach control —
is yours to pay for. Watch the overflights hustle across the top of the
scope; that's the same wind your next arrival is about to descend out
of.

**Heavies are heavy.** The 777s, 767s and 787s carry the suffix on
every call — "Speedbird 12 heavy" — and on final it matters, by the
pair, the way the book has it: behind a heavy it's four miles for
another heavy, five for a 737, six for a Cape Air 402; behind the A380
six, seven and eight; four behind the 757, five if you're small — and
even a small behind an ordinary jet owes four. Close inside a mile
of the minimum and the follower offers to take speed off; below it
they go around and tell you why. Sequencing becomes an ordering
puzzle: slot the E175 ahead of the 777, or pay the miles behind it.
The tower pays too: a release behind a heavy departure waits out the
two-minute wake gap unless the climb-out ahead has turned away or
climbed clear.

**The ILS** captures the way you'd hope: clear them on a sane intercept
(30–40° works; up to ~90° will lock on, leading the turn) and they take
it from there — localizer, glideslope, slowing to approach speed,
switched to tower at five miles. A speed you assign on the approach is
theirs to keep — "one niner zero to the marker" is the real spacing
tool — flown to five miles from the threshold, where the pilot says
"slowing to final approach speed" once and takes the schedule from
there; ask for more than 190 on final and you're offered exactly that
instead. Clear them pointed away and you'll hear
about it. Blow the energy management — still hot or high on short final —
and they **go around**, back into your pattern for another try.

**The scope helps the way STARS does.** While an aircraft is off its
assigned altitude the data block shows both — `DAL204 110↓080` is
eleven thousand descending eight — because on a busy scope the
assignment you can't remember is the one that bites. And a conflict
alert projects every pair forty-five seconds down the track: when it
goes bad, both blips **blink** red before the loss, while there's
still a turn that saves it. Solid red means it's already happened.

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

**The day changes.** Ten-odd minutes in, the ATIS updates (the letter
advances — information bravo). Usually the wind has come around, and
the airport turns with it — new landing runway (the reciprocal end),
new localizer on the map, departures rolling the other way. Anyone
established rides their approach in; anyone merely cleared hears
"cancel approach clearance, expect the new runway" and is yours to
re-sequence. This is the hardest moment in real approach control, on
purpose. But not every update is the turn: sometimes the wind only
shifts in place — new numbers, same runway — and the next one comes
when it comes, so a fresh letter is a thing to read, not a fire alarm
you can set your watch by.

**The traffic comes in banks.** Quiet spells, then centre calls the
push and a bank of arrivals comes off the boundary at two and a half
times the pace for a few minutes. The alternation of calm and slam is
what makes a shift feel like a shift — and the push doesn't care what
else is going on.

**Most of what a real scope shows is scenery.** The sky around your
sector was never empty: centre's overflights slide across the top at
FL350 in dim data blocks (drawn from the same live pool — flights whose
real route passes your airport by are exactly what belongs overhead),
and 1200-code VFR targets wander the practice areas below 6,000 ft in
limited blocks — altitude readout only, because nobody's tagged them
up, their blips washed toward grey so the altitude hue still reads but
the saturation doesn't lie. Full colour on this scope means exactly one
thing: a flight on your frequency. A departure you've handed off greys
out the same way centre's traffic does — still climbing across your
airspace, no longer your problem, drawn accordingly. None of the grey
sky is on your frequency; key one up and you'll be told whose they are. They cost you nothing to carry and everything to
ignore: a Skyhawk has every right to cross your final at 2,500 ft, and
vectoring the RJ around traffic that won't move for you is half the
job. There's no three-mile rule against an aircraft nobody controls —
you owe them a **traffic call**, not separation. `tfc` points out the
nearest target the way a controller would ("traffic ten o'clock, three
miles, type unknown, altitude indicates two thousand five hundred");
a pilot who answers "traffic in sight" maintains visual and that
near-miss never happens. One who answers "negative contact" is still
counting on you. An actual near-miss — inside a mile and 500 ft of an
unsighted target — is a **traffic alert**: both blips flash, the bell
rings, and it costs 200 points. The conflict alert projects against
the uncontrolled sky too, at hazard scale, so the blink comes while
the turn still saves it.

**A metroplex has neighbours.** Work JFK and LaGuardia and Newark are
right there; SFO has Oakland and San José, Chicago O'Hare has Midway.
Their traffic isn't yours — a real TRACON works one position beside
others — so a nearby major field puts up its own arrivals and departures
flying its own real SIDs and STARs, dim and tagged with that field's code
(`112 EWR` descending toward Newark), never on your frequency. You don't
work them, but you sequence *around* them: they're traffic-as-terrain
like the VFR sky, owed a turn and not a word, and the same traffic alert
bites if you vector into one. Their procedures are laid out so the normal
flows clear each other — the pressure is the picture, not a gauntlet.
Fields with no major neighbour (Portland, most of the world) simply run
quieter.

**On a calm morning, balloons.** Wind under ten knots and somebody's
aloft: two or three hot-air balloons drift near the field for a few
minutes, near-stationary ○ targets moving at exactly the speed and
heading of the wind at their height — a touch brisker and turned from
the ATIS number, because the wind aloft is its own weather — the one
aircraft that renders it.
The frequency gets a caution; the balloons get wherever the wind is
going; pilots spot them easily when you call them. Then they're down
in a field and the caution is cancelled.

**Centre is a character too.** Departures sometimes check in carrying a
crossing restriction from the letter of agreement — "centre wants one
one thousand crossing it" — and a handoff assigned below that altitude
comes straight back: *climb them first*. And a departure you switch
doesn't freeze on its last clearance: a beat after the handoff, centre
turns it loose — own navigation to the fix, climbing away, back up to
speed — silent on your frequency but right there on the scope, the
system carrying on without you. Hand one off out of a hold and you'll
see it finish the turn, swing outbound, and go. And once in a while centre's
own sector fills: for a couple of minutes nothing gets handed off, the
boundary you normally throw departures over becomes a wall, and `hold`
earns its keep at the exit fixes until they call back.

**Sometimes it's not routine.** Now and then — one crisis at a time,
with a real breather between them, so a shift feels unlucky rather
than scripted — an arrival declares a medical emergency: squawk 7700,
the blip goes red and stays red, and they want the field *now*. Get
them down inside twelve minutes for a
+300 bonus; everyone else can wait. Then the equipment meets them **on
the runway**: closed for three to five minutes, approaches refused,
clearances cancelled, short final waving off free of charge, departures
holding on the ground — and `hold` finally earns its keep while final
backs up behind the ambulance. Or the mayday is a departure you just
launched — a sick passenger, an engine shutting down — and it flips to
an arrival on the spot: low and fast in the middle of the room, wanting
the runway it left, ahead of everything you'd sequenced. Or an arrival
calls **minimum fuel** — no red blip, not an emergency, just a crew
telling you their pattern allowance is gone: the par clock on the hover
chip reads what's left, and one still airborne six minutes on stops
advising and declares emergency fuel. Rarely, on a busy scope, somebody's
radios die instead: **squawk 7600**, the blip goes red, they fly their
last clearance and answer nobody for a few minutes — the traffic you
can't talk to is yours to vector everyone else around. Your terminal
bell rings for maydays, failures, closures and separation losses,
because you're heads-down typing when it matters.

**Pause reviews the tape.** The sim stops and the footer opens up to
the last nine transmissions in order — the busy moment you were
heads-down for, back on the record.

## Under the hood

The renderer never knows the difference. `Sim` produces the same aircraft
dicts the ADS-B poller does and answers the same `snapshot()` call, so
every layer of the scope — basemap, blips, data blocks, trails, leaders,
weather, drag, zoom — works untouched. The sim ticks on the live loop's
animation cadence; `fix_time` is stamped to the present so the scope's
dead-reckoning glide becomes a no-op.

The game-only modules live in the `blips.game` subpackage; the rest is
shared with the live scope (`blips`), which reuses the same renderer.

| module                | job                                                       |
| --------------------- | --------------------------------------------------------- |
| `game/app.py`         | command bar, radio log, tape, HUD, `--game` wiring        |
| `game/sim.py`         | kinematics, wind, ILS, wake, sector, satellite, spawner, ambient sky, separation, conflict alert, hearback, scoring |
| `game/procedures.py`  | vendored SIDs/STARs compiled per runway: spines, gates, joins, the decluttered overlay |
| `game/schedules.py`   | vendored per-airport arrival/departure schedules          |
| `game/fleet.py`       | live-sampled traffic pool with real routes                |
| `game/voice.py`       | macOS `say` speech: one voice per flight, accent by airline |
| `game/records.py`     | the shift book (`~/.cache/blips/records.json`)            |
| `_airports.py`        | vendored airports/runways/navaids/fixes (`data/*.json.gz`) *(shared)* |
| `_commands.py`        | parser, callsign matching, phraseology readback *(shared)* |
| `_terrain.py`         | real-elevation MVA grid *(shared)*                        |

Scoring: +100 landing less a point per six seconds over par (floored at
20), +50 handoff, +25 a hearback caught before it stuck, −500
separation bust (debounced), −200 traffic alert
(a near-miss with an unsighted VFR target), −50 go-around (free when a
closed runway waves them off), −100 leaving the sector unworked. The
rating is score against what the concluded traffic was worth; three
busts is an F, whatever the score. Arrival rate breathes between quiet
spells and pushes; active count capped so the scope stays readable —
and ambient traffic never counts against the cap, never scores, and
never talks: it's there to make the sky honest, not to make you busier.
