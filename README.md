<div align="center">

# blips

**Live air traffic — drawn for the terminal.**

[![PyPI](https://img.shields.io/pypi/v/blips)](https://pypi.org/project/blips/)
[![Python](https://img.shields.io/pypi/pyversions/blips)](https://pypi.org/project/blips/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

blips turns community ADS-B and public weather data into a live,
mouse-friendly air-traffic scope. It is pure Python, has no dependencies for
its core experience, adapts to your terminal theme, and needs no account or
API key.

The basemap is a dim block-colour sea with coastlines, borders and city
labels drawn in braille. Aircraft appear as directional blips coloured by
altitude, each with an ATC-style data block, a one-minute velocity leader and
a fading trail, and move smoothly on dead reckoning between feed polls. A
weather-radar underlay is on by default.

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install blips
```

`pipx install blips` and `pip install blips` work too. blips requires Python
3.10+ on macOS or Linux, and a terminal with good Unicode support. Truecolor
is recommended; 256- and 16-colour terminals are supported.

To try blips once without keeping it installed:

```sh
uvx blips --location jfk
```

## Watch the sky

```sh
blips                              # centre on your approximate location
blips --location jfk               # or any place name
blips --location "portland maine"  # a place with its state or country
blips --location 51.47,-0.45 --zoom 1.5
blips --find "DL 1673"             # open with a search
blips --no-weather                 # leave out the weather-radar underlay
blips --print                      # one static frame for scripts or captures
```

Scroll to zoom about the pointer, drag or use the arrow keys to pan, and
hover over an aircraft for a quick readout. Click it to pin its card: the
airline and aircraft type, its route with the distance and time to go,
altitude and the altitude the crew has selected, speed, and the wind and
temperature where it's flying. Its route is drawn as great-circle arcs,
and `f` follows it across the map. Click an airport's label, or press
`a`, for that airport's weather, the runways seen in use and its
arrivals and departures.

## Find a flight

Press `/` and type. Results follow your typing:

- a flight by callsign (`DAL1673`), flight number (`DL 1673`, `UA123`),
  registration (`N812DN`, `G-EUPT`) or hex code. Anything on screen
  answers at once. A moment after you stop typing, the rest of the
  world is asked too, so Enter flies you to a flight on the other side
  of the planet and follows it.
- a route: `BOS-SFO`, `to LHR`, `from JFK`, matched against the routes
  blips has looked up for the traffic it has seen.
- an airport by code or name: `PWM`, `Heathrow`.
- a filter: an airline (`delta`), a type (`737`, `A388`, `dreamliner`),
  `military`, `emergency`, `heavies`, `above FL300`. Filters are named
  in the header, and `x` clears them.
- anything else is taken as a place to go.

| Key | Action |
| --- | --- |
| `/` | Search |
| scroll · `+` / `-` | Zoom |
| drag · arrows | Pan |
| click | Select a flight (click again to follow) or an airport |
| `Tab` | Select the next flight out from the centre |
| `f` | Follow the selected flight |
| `c` / `h` | Centre on the selection / fly home |
| `a` | Card for the airport nearest the centre |
| `x` | Clear filters |
| `l` | Labels: auto, all, callsign only, off |
| `t` `r` `g` `w` | Trails, range rings, ground traffic, weather |
| `space` | Pause the glide |
| `esc` | Close the card, then clear filters |
| `?` | Help |
| `q` | Quit |

## There's also a game

An undocumented `--game` flag turns the scope into an approach-control sim:
the live feed steps aside and you vector and sequence simulated flights
around a real airport, built from real runways, navaids, terrain and weather
— with the FAA's published SIDs and STARs at supported US airports.

```sh
blips --game                     # the airport nearest you
blips --game tpa                 # Tampa approach
blips --game --calm              # a gentler opening shift
```

Instructions are typed into the command bar and pilots read them back. Press
`?` in a shift for a command card, and type `voice` to hear the frequency
(on Linux, `uv tool install 'blips[voice]'` adds the local speech backend).
The [game manual](docs/GAME.md) covers phraseology, procedures, scoring and
voices in full.

## Data and privacy

With no `--location`, blips asks ipinfo.io for an approximate location from
your IP address. Pass a place or coordinates to avoid that lookup. Place-name
search uses Open-Meteo. Live mode necessarily sends the visible area's centre
and radius to the selected aircraft and weather providers, the callsigns and
positions of traffic in view to the route service, and your search terms to
the aircraft providers; no blips account or telemetry service is involved.

<details>
<summary><strong>Data sources and coverage</strong></summary>

- **Aircraft** — [adsb.lol](https://adsb.lol) and
  [adsb.fi](https://adsb.fi), elected by coverage for the current view and
  polled once every five seconds; a view wider than their 250 nm reach is
  swept in tiles, a second apart. Search looks flights up worldwide on the
  same services. These community feeds depend on volunteer receivers, so
  coverage is thinner over oceans and sparsely populated land.
- **Routes** — callsign lookups from [adsb.im](https://adsb.im), in
  batches for the airline traffic in view, kept on disk for a few hours.
- **Airport weather** — METARs from the
  [Aviation Weather Center](https://aviationweather.gov/).
- **Airlines and aircraft types** — distilled from
  [OpenFlights](https://openflights.org/data.php), with corrections.
- **Weather radar** — [LibreWXR](https://librewxr.net) worldwide, with NEXRAD
  from the [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/)
  over the continental US and [RainViewer](https://www.rainviewer.com/) as a
  fallback elsewhere. Choose a colour scheme with `--wx-theme`, for example
  `dark-sky`, `nexrad` or `rainbow`.
- **Basemap** — [Natural Earth](https://www.naturalearthdata.com/) geography,
  vendored and simplified from its public-domain datasets.
- **Airports and navaids** — trimmed public-domain
  [OurAirports](https://ourairports.com/data/) data.
- **US procedures and fixes** — the FAA's public
  [Coded Instrument Flight Procedures](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/)
  data. Outside its coverage, the game still uses real airports, runways and
  navaids but cannot offer the same published SID/STAR detail.
- **Game schedules** — distilled at build time from open Wikipedia airline and
  destination tables, with operator identifiers supplemented by
  [OpenFlights](https://openflights.org/data.php).
- **Game terrain** — sampled when a shift starts from Open-Meteo's elevation
  API and cached locally.

</details>

## Lineage

The braille geography, terminal-theme adaptation and live rendering loop began
as the `radar` command in [linecast](https://github.com/ashuttl/linecast), a
sibling collection of terminal weather, sunlight, tide, radar and map tools.
The weather-radar underlay comes from the same project. blips applies that
shared visual language to live aircraft.

## License

[MIT](LICENSE)
