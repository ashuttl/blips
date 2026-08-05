# blips --game — working plan

A synthesis of four audits (gameplay, discoverability/UI, realism &
local uniqueness, engineering) against the goals: **(a) fun,
(b) discoverability & UI, (c) realism, (d) local airport specificity,
(e) technical underpinnings.** Ordered by leverage. Items marked ✅
are done; 🚧 are in flight this session.

## Done / in flight

- ✅ **Departure release metering** *(fun, realism)* — tower no longer
  rolls a departure into the climb-out ahead: gap measured from the
  release point, waived only by divergence (20°+) or altitude (800 ft+),
  slow leaders get more room, the second (satellite) departure timer
  obeys the same rules, and satellite departures level 1,000 ft under
  the main flow per the LOA. Verified: 0 departure-pair busts in 9
  unattended shift-hours (previously routine).
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
- 🚧 **Scoring made felt** *(fun, discoverability)* — every score event
  says its number (landing with par verdict, +50 handoff, −500/−200/−50
  named); caught hearbacks pay +25; par pressure and assigned speed on
  the hover chip; live rating letter, ATIS letter and aircraft count in
  the HUD; shift-book personal best keyed on rating ratio, not raw
  score (a 3-hour C shift no longer beats a 50-minute A+).
- 🚧 **Engineering floor** *(underpinnings)* — GitHub Actions CI;
  unit tests for the untested offline-degradation cores (`_terrain`
  0% → covered, `fleet` draw semantics); the one unseeded RNG
  (fleet shuffle) made injectable.

## Next — highest leverage remaining

1. **Help panel + un-truncated hints** *(discoverability — the #1 UI
   gap)*. The radio grammar hint needs 141 columns; on an 80-col
   terminal players never see `i` (the win condition), `ho`, `tfc`, or
   `? help` itself. `?` should pause and draw a real card: verbs with a
   worked example, the hundreds rule, desk keys, mouse affordances, a
   short glossary page (established, NORDO, par, gates). Wrap or rotate
   the bar hints to the measured terminal width. Also reconcile
   `log_open = True` default with GAME.md's claim that the tape starts
   closed.
2. **Let the shift outgrow you** *(fun)*. Arrival rate caps at 34/hr by
   minute 20 and the active cap pins at 16 — measured: every shift
   plateaus; a player who can hold 16 has beaten the game permanently.
   Let the cap breathe upward with survival (e.g. +1 per 20 min) and
   let pushes intensify slightly. Pair with: **one early bust
   mathematically locks out an A** — grade cleanliness by cap (1 bust
   caps B+, 2 caps C, 3 = F) instead of raw subtraction, so playing on
   after a bust stays rational.
3. **Event cooldowns instead of once-per-shift-forever** *(fun)*.
   Emergency/NORDO/centre-closure are hard-capped at 1 per shift; after
   minute 40 only pushes and flow changes remain. Cooldowns (~20–25 min)
   at the same low rates, plus one or two cheap new events reusing
   existing machinery: a departure that declares and returns; a
   minimum-fuel arrival (no red blip, zero par slack). De-metronome the
   flow change (measured: exactly 3/hr at ~12/~33/~52 every shift) —
   sometimes the wind holds and only the ATIS letter advances.
4. **Parallel-runway operations** *(uniqueness — the single biggest
   differentiator)*. `build_sector` uses only `rwys[0]`; TPA, SEA, EGLL
   all run parallels. Phase 1 is segregated mode (land the longer,
   depart the other — EGLL's actual operation); the `i 19L` grammar and
   per-runway wake keys already exist. Phase 2: dual arrival streams
   with the independent-approach separation exemption (centerlines >
   4,300 ft).
5. **Visual approaches** *(realism + fun)*. `v 19L` with a
   field-in-sight roll keyed on range/weather; "follow the traffic
   ahead" reuses the existing `visual` sighting set so in-trail inside
   3 nm is legal while wake still bites. VMC majors clear mostly
   visuals in reality; also the hook for charted ones (river/bay
   visuals) later.
6. **First-shift calm ramp** *(discoverability)*. When the shift book
   is empty: doubled spawn intervals, hearback/emergency/flow-change
   off for ~8 minutes, and 3–4 one-time coach lines at the moment they
   apply ("type dal204 d 60 to start them down"). Also fixes the cold
   open generally — measured up to 7 minutes of dead air in the first
   10; prepopulate 3–4 arrivals with one mid-descent and floor the
   early rate.

## Later — worth doing, not first

- **2.5 nm same-runway final separation inside 10 nm** (7110.65
  5-5-4-j) — rewards precise tight-packing; small change in
  `_separation` keyed on both established same-course.
- **Tight-spacing bonus** — pay +20 for a landing gap within ~25% of
  the wake minimum; the skill the doc calls the defining tax currently
  scores nothing.
- **Optional `--shift 45`** — "relief is on the line, clean up the
  board": arrivals stop, card presents itself on a clean board; gives
  a session a final act.
- **Local flow rules table** — preferred calm-wind runways (EGLL
  westerlies), one-way fields; a dozen hand-curated majors is enough.
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
- **sim.py trim** — move the ~800 lines of casting tables and the
  sector builder to `game/casting.py` / `game/sector.py`, re-exported;
  do NOT split the radio layer (its coupling is the design).
- **Data provenance** — `_meta` key (source, CIFP cycle, build date) in
  the vendored `.gz` files + a refresh runbook in tools/.
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
