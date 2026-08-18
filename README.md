<div align="center">

# blips

**Live aircraft and an approach-control game — drawn for the terminal.**

[![PyPI](https://img.shields.io/pypi/v/blips)](https://pypi.org/project/blips/)
[![Python](https://img.shields.io/pypi/pyversions/blips)](https://pypi.org/project/blips/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

blips turns community ADS-B and public weather data into a live,
mouse-friendly air-traffic scope. It is pure Python, has no dependencies for
its core experience, adapts to your terminal theme, and needs no account or
API key.

Geography sets the stage: a dim block-colour sea with coastlines, borders and
city labels picked out in braille. Live aircraft move over it as directional
blips coloured by altitude, each with an ATC-style data block, a one-minute
velocity leader and a fading trail. Between feed polls they glide on dead
reckoning, so the sky moves the way the sky moves. Weather radar settles in
underneath by default, showing the storms the aircraft are threading.

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
blips --location 51.47,-0.45 --zoom 1.5
blips --no-weather                 # leave out the weather-radar underlay
blips --print                      # one static frame for scripts or captures
```

Drag to pan, use `+` and `-` to zoom, and hover or click an aircraft for its
full data readout and route when one is known.

| Key | Action |
| --- | --- |
| `+` / `-` | Zoom in / out |
| drag | Pan the scope |
| hover / click | Inspect an aircraft |
| `t` | Toggle trails |
| `r` | Toggle range rings |
| `g` | Toggle ground traffic |
| `w` | Toggle weather radar |
| `space` | Pause the glide animation |
| `q` | Quit |

## Work the frequency

There is an approach-control sim hiding inside the scope. The live feed steps
aside and simulated flights become yours: vector and sequence them around a
real airport, clear approaches, hand departures to centre, and keep everyone
three miles and a thousand feet apart.

```sh
blips --game                     # the airport nearest you
blips --game tpa                 # Tampa approach
blips --game egll                # Heathrow director
blips --game billings            # somewhere the terrain pushes back
blips --game --calm              # a gentler opening shift
```

Everything is typed into the command bar. Instructions chain, and pilots read
the full clearance back:

```text
> rpa5655 l 230 c 40
  Brickyard 5655, turn left heading two three zero, climb and maintain 4,000.
```

The sector is built around real runways, navaids, terrain and weather. At
supported US airports its named SIDs and STARs come from the FAA's published
procedures and can be drawn, inspected and flown. Traffic thickens as the
shift goes on; separation losses, go-arounds, clean handoffs, prompt landings
and caught bad readbacks all reach the shift book.

Press `?` in a shift for a two-page command card, or read the full
[game manual](docs/GAME.md) for phraseology, procedures, scoring, voices and
everything the frequency eventually throws at you. The `--game` flag remains
absent from `blips --help`; now you know where it is.

### Pilot voices

`voice` or <kbd>Ctrl</kbd>+<kbd>V</kbd> makes the frequency audible, with one
voice kept for each flight. macOS uses the system `say` voices. On Linux,
install the optional local Piper backend:

```sh
uv tool install 'blips[voice]'
```

The first transmission then downloads two multi-speaker models (about 150 MB)
to `~/.cache/blips/voices`. Speech is synthesized locally on the CPU; turning
voice off returns to the text-only frequency.

## Data and privacy

With no `--location`, blips asks ipinfo.io for an approximate location from
your IP address. Pass a place or coordinates to avoid that lookup. Place-name
search uses Open-Meteo. Live mode necessarily sends the visible area's centre
and radius to the selected aircraft and weather providers; no blips account or
telemetry service is involved.

<details>
<summary><strong>Data sources and coverage</strong></summary>

- **Aircraft** — [adsb.lol](https://adsb.lol),
  [airplanes.live](https://airplanes.live) and
  [adsb.fi](https://adsb.fi), elected by coverage for the current view and
  polled once every five seconds. These community feeds depend on volunteer
  receivers, so coverage is thinner over oceans and sparsely populated land.
- **Routes** — demand-driven callsign lookups from
  [adsb.im](https://adsb.im), requested only for an aircraft being inspected.
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
The weather-radar underlay came home from the same project. blips takes that
shared visual language into the moving sky, then lets you work it.

## License

[MIT](LICENSE)
