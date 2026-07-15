"""Pluggable radar sources behind one small contract.

A source exposes:
  .label / .attribution              — footer text
  .latest_rgba(bbox, gw, hc)         — (pw, ph, rgba) at gw × hc*2, EPSG:4326,
                                        for the newest available frame, or None

Region routing: LibreWXR is primary everywhere — real radar composites for
North America / Europe / East Asia, model precipitation elsewhere, selectable
colour themes.  On failure, the continental US falls back to IEM/NEXRAD
(native projection) and the rest of the world to RainViewer.

blips shows only the latest frame as an ambient underlay; the scrubbable
storm-rewind timeline lives in the sibling project (linecast).
"""

from blips._png import decode_rgba
from blips._radar_source import fetch_frame, latest_frame_time
from blips import _radar_tiles as tiles

# rough lower-48 bounding box; IEM/NEXRAD coverage
_CONUS = (-127.0, 23.0, -65.0, 50.0)

# LibreWXR server-rendered colour schemes (name → tile-path colour id).
THEMES = {
    "dark-sky": 8,
    "universal-blue": 2,
    "rainbow": 7,
    "nexrad": 6,
    "original": 1,
    "titan": 3,
    "twc": 4,
    "meteored": 5,
    "datameteo": 9,
    "viper": 10,
    "mrms": 11,
    "max-storm": 12,
    "black-white": 0,
}
DEFAULT_THEME = "dark-sky"


def theme_id(value):
    """Resolve a theme name or bare numeric id to a colour id, or None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in THEMES:
        return THEMES[text]
    try:
        num = int(text)
    except ValueError:
        return None
    return num if num in THEMES.values() else None


def _in_conus(lat, lon):
    w, s, e, n = _CONUS
    return w <= lon <= e and s <= lat <= n


class IEMSource:
    label = "NEXRAD · IEM"
    attribution = "NEXRAD · IEM"

    def latest_rgba(self, bbox, gw, hc):
        png = fetch_frame(bbox, gw, hc * 2, when=latest_frame_time())
        return decode_rgba(png)


class _TileSource:
    """Shared body for sources speaking the RainViewer v2 tile protocol."""

    def __init__(self, provider):
        self.provider = provider
        self.host = None
        self._newest = None   # (path, is_future) of the freshest frame
        self._refresh()

    def _refresh(self):
        idx = tiles.fetch_index(self.provider)
        self.host = idx["host"]
        radar = idx.get("radar", {})
        past = radar.get("past") or []
        nowcast = radar.get("nowcast") or []
        # prefer the newest *observed* frame; fall back to a nowcast frame
        if past:
            self._newest = (past[-1]["path"], False)
        elif nowcast:
            self._newest = (nowcast[0]["path"], True)
        else:
            self._newest = None

    def latest_rgba(self, bbox, gw, hc):
        self._refresh()
        if self._newest is None:
            return None
        path, future = self._newest
        return tiles.reproject(self.provider, self.host, path,
                               bbox, gw, hc * 2, mutable=future)


class RainViewerSource(_TileSource):
    label = "RainViewer"
    attribution = "Weather data by RainViewer"

    def __init__(self):
        super().__init__(tiles.rainviewer_provider())


class LibreWXRSource(_TileSource):
    label = "LibreWXR"
    attribution = "Weather data by LibreWXR · CC BY 4.0"

    def __init__(self, theme=THEMES[DEFAULT_THEME]):
        self.theme = theme
        super().__init__(tiles.librewxr_provider(theme))


def get_source(lat, lon, theme=None):
    """Pick the best radar source for a location, falling back on failure."""
    if theme is None:
        theme = THEMES[DEFAULT_THEME]
    try:
        return LibreWXRSource(theme)
    except Exception:
        pass
    if not _in_conus(lat, lon):
        try:
            return RainViewerSource()
        except Exception:
            pass
    return IEMSource()
