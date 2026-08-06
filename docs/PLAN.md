# blips --game — working plan

A synthesis of four audits (gameplay, discoverability/UI, realism &
local uniqueness, engineering) against the goals: **(a) fun,
(b) discoverability & UI, (c) realism, (d) local airport specificity,
(e) technical underpinnings.** Ordered by leverage. Items marked ✅
are done.

## Done / in flight

- ✅ **Departure release metering** *(fun, realism)* — tower no longer
  rolls a departure into the climb-out ahead: gap measured from the
  release point, waived only by divergence (20°+) or preserved vertical
  separation (1,000 ft+ across both current and assigned altitude),
  slow leaders get more room, and the second (satellite) departure timer
  obeys the same release rules. Verified: 0 same-runway departure-pair
  busts in 300 unattended shift-hours (previously routine).
- ✅ **Teaching parse errors** *(discoverability)* — `d 4000` teaches the
  hundreds convention, `s 250` points at `rs`/`is`, `h/fh/t 230` teaches
  `l`/`r`, `l230` is forgiven, nonsense points at `?`.
- ✅ **Speed realism bundle** *(realism, fun)* — 250 kt below 10,000 MSL
  (clamp + pilot refusal); assigned speed rides to ~5 nm from the
  threshold ("190 to the marker" becomes a real tool, and the silent
  180-clip — the sim's only quiet input-fix — becomes an honest
  refusal); follower-aware wake matrix (7110.65 pairs: small behind
  large 4, behind heavy 4/5/6, behind super 6/7/8, B757 rules) plus a
  longer release hold behind a heavy departure. Par verified headless
  under the clamp: prompt landings keep 20–170 s of the 300 s pattern
  allowance, no retune needed.
- ✅ **Scoring made felt** *(fun, discoverability)* — every score event
  says its number (landing with par verdict, +50 handoff, −500/−200/−50
  named); caught hearbacks pay +25; par pressure and assigned speed on
  the hover chip; live rating letter, ATIS letter and aircraft count in
  the HUD; shift-book personal best keyed on rating ratio, not raw
  score (a 3-hour C shift no longer beats a 50-minute A+).
- ✅ **The shift outgrows you + fair bust grading** *(fun)* — the active
  cap breathes upward with survival (16 +1 per 20 min, ceiling 21, all
  three spawn gates), the arrival ramp keeps climbing gently past 34/hr
  toward ~40/hr by the second hour, and each push survived leans a
  little harder (×2.4 creeping to ×2.8); the ambient sky stays capped
  as before. Busts keep their −500 but now cap the letter (1 → B+,
  2 → C, 3 = F) instead of the score-below-zero standing F, so playing
  on after an early bust beats quit-restart. Verified unattended:
  active traffic peaks at 17–20 in minutes 60–80 across three seeds
  (previously pinned at 16).
- ✅ **Engineering floor** *(underpinnings)* — GitHub Actions CI;
  unit tests for the untested offline-degradation cores (`_terrain`
  0% → covered, `fleet` draw semantics); the one unseeded RNG
  (fleet shuffle) made injectable.
- ✅ **Help panel + un-truncated hints** *(discoverability — was the #1
  UI gap)* — `?` now pauses the sim and draws a real card over the
  footer: every radio verb one per line with a worked example off a
  live callsign, the hundreds rule, the desk keys and the mouse, and a
  second glossary page (`?` turns the page, Esc closes); the empty-bar
  hint greedy-wraps to the measured terminal width so `i`, `hold`,
  `tfc`, `ho` and `? help` itself survive an 80-column terminal; the
  first `^O` points at the hover cards, once; GAME.md and the code now
  agree the tape starts open (the open tape teaches, so `log_open =
  True` stayed and the doc moved).
- ✅ **Event cooldowns & variety** *(fun)* — emergency/NORDO/centre-
  closure share one abnormal clock: one crisis at a time, eligible
  again ~20–25 min after the last concludes, same low hazard rates
  (balloons stay a once-a-shift calm-morning special). Two new events
  off existing machinery: a climbing departure that declares and
  returns (7700, arrival priority, bonus clock, closure aftermath) and
  a minimum-fuel arrival (no red blip, par cut to the straight-in,
  hover-chip tell, escalates to emergency fuel after 6 min airborne).
  Flow change de-metronomed: 600–2,400 s reschedule, ~30% of updates
  hold the wind — the letter advances, the runway stays.
- ✅ **First-shift calm ramp + warm open** *(discoverability, fun)* —
  every shift now opens warm: 3–4 prepopulated arrivals (one already
  mid-descent, pointed at the field) and no 0.75 off-push lull until
  the first push has fired. A truly first shift (no rated page in the
  shift book anywhere, or explicit `--calm`) opens gently: ~8 minutes
  of doubled spawn gaps, zero hearback, push/flow-change/abnormals
  parked past the window, and three one-shot coach lines in the game's
  voice at the moments they matter (first check-in, first close-in
  uncleared arrival, first departure), each off a real callsign.
  Seeded shifts are never calm-adjusted unless `--calm` is explicit —
  determinism first.
- ✅ **Parallel-runway operations, phase 1: segregated mode**
  *(uniqueness — the single biggest differentiator)* — a parallel pair
  (courses within ~10°, same number, L/C/R suffixes, detected from the
  vendored data) runs the way EGLL actually does: arrivals land the
  longer parallel, departures roll the other, and both ends flip
  together on a flow change. ATIS and check-ins name both runways
  ("landing runway one niner right, departing runway one niner left"),
  the scope draws both with the localizer only on the landing one,
  STARs compile to the arrival end and SIDs to the departure end,
  `i <departure end>` gets a teaching refusal, and a medical closure
  holds approaches while departures keep rolling (single-runway fields
  still hold everything). Verified parallel: TPA, SEA (16L lands, 16C
  departs), EGLL; unchanged single/crossing: PWM, BIL. Phase 2 moved
  to Later.
- ✅ **Live traffic leads the cast** *(locality)* — `_cast_flight` now
  draws route-confirmed pool traffic first (right direction, runway-gated,
  used once), degrades silently to the vendored schedule, then anonymous
  pool metal, then the country mix; `cast_sources` tallies who led and the
  radio notes, once, when the live sample leads or runs dry. Seeded shifts
  still skip the pool (`_live_pool`, regression-tested).
- ✅ **Airport operations profiles** *(uniqueness)* — an optional,
  hand-curated table (`game/profiles.py`, every key optional) for the
  habits the data can't derive: calm-wind flow preference with
  flow-change rolls that lean back home (EGLL holds the 27s — the
  westerly preference is policy), explicit arrival/departure parallels
  (KTPA lands the shorter 19L in south flow per the airport's Informal
  Runway Use Program), pinned satellites (Farnborough, Brunswick — the
  latter agreeing with the search, so the pin is documentation), a
  curated initial level-off routed through `_initial_alt` so the
  satellite's 1,000-ft LOA split holds by construction (EGLL's 6,000
  SID cap), and the centre crossing-restriction menu. Unprofiled
  fields verified byte-identical to the generated sector; growth
  beyond the three showcases stays in Later ("Expand airport
  operations profiles").
- ✅ **Make approach claims true** *(realism, locality)* — the vendored
  APPCH records (5,116 approaches at 733 US fields, shipped all along
  in procedures.json.gz but ignored by the game) now say which runway
  ends really have a localizer. `i` to an ILS-less end teaches ("unable
  — no ILS to runway four left, that's an RNAV approach; the ILS serves
  runway four right"); an end with nothing straight-in refuses
  outright; a field with no ILS anywhere (Palm Springs, Lihue) clears
  its real approach by name — "cleared RNAV runway three one right
  approach" — down the same final course and slope the sim already
  flies, so RNAV-only fields play true instead of fictional. The data
  steers the sector too: segregated arrivals prefer the ILS-equipped
  parallel (Kennedy lands 13L and rolls departures off the longer 13R),
  the shift opens on the flow the instruments serve, and a one-ILS
  field holds its flow when the wind turns — the broadcast advances,
  the runway stays. Fields the data can't speak for — military
  (approaches live in the DoD FLIP, off the public CIFP), foreign, or
  circling-only (Aspen) — keep the old every-end-an-ILS assumption, so
  nothing changes across the rest of the 4,472. Approach final-fix and
  step-down geometry, and published missed approaches, wait (Later).

## Safety follow-up

- ✅ **Make the satellite LOA altitude an invariant.** The pair is now
  derived together in `_initial_alt` (used by spawning and release
  checks): the satellite levels a thousand under the main flow, or a
  thousand over where it sits too high for "under" to clear its own
  pattern — never level with it, by construction, verified across the
  elevation space and by unattended mixed-field soak.

## Next — highest leverage remaining

1. **Visual approaches** *(realism + fun)*. `v 19L` with a
   field-in-sight roll keyed on range/weather; "follow the traffic
   ahead" reuses the existing `visual` sighting set so in-trail inside
   3 nm is legal while wake still bites. VMC majors clear mostly
   visuals in reality; also the hook for charted ones (river/bay
   visuals) later.

## Later — worth doing, not first

- **Published missed approaches and approach step-downs** — the APPCH
  legs are already vendored (final fixes, crossing altitudes, and the
  climb-out after the runway fix); fly the published miss on a
  go-around instead of the synthesized straight-ahead climb, and honor
  the step-downs before the FAF. Availability is honest now; the
  geometry is the part still waiting.
- **Parallel operations, phase 2 — dual arrival streams** with the
  independent-approach separation exemption (centerlines > 4,300 ft
  run their own finals); segregated mode is the phase-1 floor it
  builds on.
- **2.5 nm same-runway final separation inside 10 nm** (7110.65
  5-5-4-j) — rewards precise tight-packing; small change in
  `_separation` keyed on both established same-course.
- **Tight-spacing bonus** — pay +20 for a landing gap within ~25% of
  the wake minimum; the skill the doc calls the defining tax currently
  scores nothing.
- **Optional `--shift 45`** — "relief is on the line, clean up the
  board": arrivals stop, card presents itself on a clean board; gives
  a session a final act.
- **Expand airport operations profiles** — after the three showcase fields
  prove the shape, add preferred flows and one-way/noise rules for a dozen
  hand-curated majors rather than trying to curate all 4,472 airports.
- **MacDill and friends** — the satellite search floor is 10 nm, which
  drops KMCF (7.6 nm from TPA, military profile already castable);
  widen to ~6 nm or adopt military fields inside 10 as an extra
  satellite.
- **Class B/C shelf for the VFR sky** — synthesized two-ring wedding
  cake at `large` airports so Skyhawks stop wandering surface Class B;
  concentrates VFR threats where they really live (under the shelf, at
  its edges).
- **International sound** — per-country transition altitude (EGLL says
  "flight level" far lower), QNH/altimeter in the ATIS, "with
  information alpha" on check-in.
- **Time-of-day and frequency weights for the cast** — the live pool and
  schedule now lead in the right order; the richer step is weighting the
  vendored routes by real frequency and hour (no red-eye regionals, banks
  at the hubs) rather than the flat per-route weights.
- **Published holds** — racetrack legs, left-turn option, EFC times;
  upgrades the two scripted crises (centre wall, runway closure) to
  play like the procedures they imitate.
- **Tab completion** in the bar (callsigns, fixes after `dct`, today's
  procedures after `via` — which quietly teaches the sector).
- **Keyboard route to the hover cards** — a bare fix/procedure/callsign
  typed alone prints its card into the log.
- **Tail 3 of the aircraft-dict registry** — 65 keys, only 31 born in
  `_base`; initialize all keys there with one comment each, fix the
  stale phase-enum comment, add a registry-conformance soak test.
- **Scripted-shift regression harness** — (airport, seed, command
  transcript) → golden ledger; then a **fixed internal timestep** so
  `--seed` means the same shift at any frame rate (measured: currently
  cadence-dependent).
- **Horizon-capped closest-approach alerts** — `_separation` currently checks
  only the projected positions at exactly 45 seconds, so a head-on pair that
  passes and separates inside that window can miss the advance warning. Share
  one CPA helper with the scope and test an early crossing conflict.
- **sim.py trim** — move the ~800 lines of casting tables and the
  sector builder to `game/casting.py` / `game/sector.py`, re-exported;
  do NOT split the radio layer (its coupling is the design).
- **Data provenance** — `_meta` key (source, CIFP cycle, build date) in
  the vendored `.gz` files + a refresh runbook in tools/.
- **Release artifact smoke test** — CI should build and install the wheel,
  verify the six vendored datasets, exercise the Python 3.10 floor, and run
  `blips --help` plus offline game construction from the installed artifact.
- **Name the terrain layer honestly** — the current 24×24 terrain-plus-
  2,000-ft grid is a useful safety floor, not a published MVA. Label it that
  way until a real MVA source exists.
- **VFR reporting-point landmarks** ("traffic over the Gandy Bridge") —
  new NASR checkpoint pipeline; cosmetic, last.

## Explicitly not doing

- Optimizing `_separation` — measured 0.3 ms/tick at double the
  gameplay cap; the O(n²) loops are the right tool.
- Splitting the phraseology/radio layer out of `Sim` — it touches
  weather, terrain, ILS and hearback state because that's the design.
- Enterprise ceremony (lint matrices, type-checking the dict model
  into dataclasses — `snapshot()` must keep quacking like the ADS-B
  feed).
