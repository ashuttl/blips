# blips

A live air-traffic scope in your terminal.

Geography sets the stage — a dim block-colour sea with coastlines, borders
and city labels picked out in braille — and live aircraft are painted over it
the way a controller's scope does it: a directional blip coloured by altitude,
an ATC-style data block (callsign + flight level + climb/descent trend), a
velocity leader showing the next minute of travel, and a fading trail of
recent positions. Between feed polls the blips glide on dead reckoning, so the
sky moves the way the sky moves. Live weather radar settles in underneath the
traffic by default, so you can see the storms the aircraft are threading.

```
blips                      # scope centred on your location
blips --location jfk       # or any place name
blips --location 51.47,-0.45 --zoom 1.5
blips --no-weather         # turn the weather-radar underlay off
blips --print              # single static frame (for scripts/screenshots)
```

## Keys

| Key | Action |
| --- | --- |
| `+` / `-` | zoom in / out |
| drag | pan the scope |
| hover / click a blip | full data readout in the footer |
| `t` | toggle trails |
| `r` | toggle range rings |
| `g` | toggle ground traffic |
| `w` | toggle the weather-radar underlay |
| `space` | pause the glide animation |
| `q` | quit |

## Data

Live positions come from community ADS-B aggregators — [adsb.lol](https://adsb.lol)
first, [airplanes.live](https://airplanes.live) as fallback — fed by thousands
of volunteer receivers. Please be kind to them; blips polls once every five
seconds for the visible window only. Coverage is excellent over populated
land and thinner over oceans (no satellite pickup — if an airliner vanishes
mid-Atlantic, it didn't crash, it just flew out of range of the volunteers).

The basemap is [Natural Earth](https://www.naturalearthdata.com/) (public
domain), vendored at 1:50m. Geolocation is IP-based via ipinfo.io unless you
pass `--location`; place names geocode via Open-Meteo.

Weather radar (`w`, on by default) is only fetched while it's showing:
[LibreWXR](https://librewxr.net) worldwide, with NEXRAD via the
[Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/) over the
continental US and [RainViewer](https://www.rainviewer.com/) elsewhere as
fallbacks. Pick a colour scheme with `--wx-theme` (e.g. `dark-sky`, `nexrad`,
`rainbow`).

## Lineage

The rendering approach — braille geography under glyph overlays, terminal
theme adaptation, the live loop — began life in
[linecast](https://github.com/ashuttl/linecast)'s `radar` command, and the
weather-radar underlay comes home from that same project.

## Install

```
pip install blips
```

Requires Python 3.10+ and a terminal with decent Unicode; truecolor
recommended (falls back to 256/16 colours).
