"""NEXRAD radar frames from the Iowa State Mesonet (IEM) WMS.

Uses IEM's WMS to fetch the latest NEXRAD composite for the continental US.
Frames are fetched at exactly the requested sub-pixel dimensions (server-side
resampling) and the raw PNG bytes are cached briefly on disk.

Data: NWS NEXRAD Level III base reflectivity (n0q), composited by IEM.
Attribution: Iowa Environmental Mesonet, Iowa State University.
"""

import datetime
import urllib.request

from blips import USER_AGENT
from blips._cache import CACHE_ROOT
from blips._runtime import debug_log

_WMS = "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi"
_LAYER = "nexrad-n0q-wmst"
FRAME_STEP = 5 * 60          # radar composite cadence, seconds
_LATENCY = 5 * 60            # newest frame lags real time by ~one step
_RADAR_CACHE = CACHE_ROOT / "radar"


def _floor_step(dt):
    """Floor a UTC datetime to the nearest 5-minute frame boundary."""
    m = dt.minute - (dt.minute % 5)
    return dt.replace(minute=m, second=0, microsecond=0)


def latest_frame_time(now_utc=None):
    """Newest frame timestamp likely to have data, given radar latency."""
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
    return _floor_step(now_utc - datetime.timedelta(seconds=_LATENCY))


def _url(bbox, w, h, when):
    minlon, minlat, maxlon, maxlat = bbox
    return (
        f"{_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={_LAYER}"
        f"&STYLES=&FORMAT=image/png&TRANSPARENT=true&SRS=EPSG:4326"
        f"&BBOX={minlon},{minlat},{maxlon},{maxlat}&WIDTH={w}&HEIGHT={h}"
        f"&TIME={when.strftime('%Y-%m-%dT%H:%M:00Z')}"
    )


def _cache_path(bbox, w, h, when):
    key = f"{bbox[0]:.3f}_{bbox[1]:.3f}_{bbox[2]:.3f}_{bbox[3]:.3f}_{w}x{h}"
    stamp = when.strftime("%Y%m%dT%H%M")
    return _RADAR_CACHE / f"{key}_{stamp}.png"


def fetch_frame(bbox, w, h, when=None, timeout=15):
    """Fetch the latest radar frame as PNG bytes for `bbox` at `w`×`h` pixels.

    The newest frame is cached only until the next 5-minute boundary. Falls
    back to stale cache on error.
    """
    when = _floor_step(when) if when else latest_frame_time()
    path = _cache_path(bbox, w, h, when)

    if path.exists() and _time_since(path) < FRAME_STEP:
        debug_log(f"radar cache hit: {path.name}")
        return path.read_bytes()

    url = _url(bbox, w, h, when)
    try:
        debug_log(f"radar fetch {url}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        data = urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as exc:
        debug_log(f"radar fetch failed: {exc}")
        if path.exists():
            return path.read_bytes()
        raise

    _RADAR_CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _time_since(path):
    import time
    return time.time() - path.stat().st_mtime
