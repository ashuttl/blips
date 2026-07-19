"""Terrain for the game: minimum vectoring altitudes from real elevation.

A coarse elevation grid over the sector, fetched once per shift from the
Open-Meteo elevation API (the same free service the scope's geocoder
uses) on a background thread.  Each cell's MVA is its highest terrain
plus a 2,000 ft buffer (the real-world obstacle-clearance rule of thumb
in mountainous areas), rounded up to the next hundred feet.

Around Tampa this whole grid is ~2,100 ft and you'll never hear about
it.  Work Billings or LAX and the sim's pilots start refusing descents
into the rocks — "unable four thousand, minimum vectoring altitude here
is seven thousand three hundred."  Offline, the world stays flat and
nothing changes.
"""

import math
import threading
import time

from blips import USER_AGENT
from blips._cache import CACHE_ROOT, location_cache_key, read_stale, write_cache
from blips._geo import advance
from blips._http import fetch_json
from blips._runtime import debug_log

URL = "https://api.open-meteo.com/v1/elevation"
GRID_N = 48               # cells per side (~2.7 nm/cell over the span)
SPAN_NM = 130.0           # grid coverage, comfortably past the gates
BUFFER_FT = 2000.0        # obstacle clearance over the highest terrain
BATCH = 100               # points per API call (the documented max)
# GRID_N=48 → 2,304 samples → 24 batches, ~12 s on the background thread
# once per shift.  Fine enough to separate ridgelines from valleys instead
# of smearing a whole island into one dome.


class Terrain:
    """Sector MVA grid.  mva_at() answers None until the fetch lands."""

    def __init__(self, lat, lon):
        self._home = (lat, lon)
        self._mva = None      # GRID_N × GRID_N of ft, row 0 = north edge
        self.status = "loading"   # → "ready" | "failed"
        self.max_mva = 0.0
        self._lock = threading.Lock()
        # grid corners: SPAN_NM square centred on the airport
        half = SPAN_NM / 2.0
        self._north = advance(lat, lon, 0.0, half)[0]
        self._south = advance(lat, lon, 180.0, half)[0]
        self._west = advance(lat, lon, 270.0, half)[1]
        self._east = advance(lat, lon, 90.0, half)[1]
        # elevation never changes, so cache the raw grid forever, keyed by
        # location and sample layout (MVA is recomputed on load, so tuning
        # BUFFER_FT doesn't invalidate the cache)
        self._cache = (CACHE_ROOT
                       / f"terrain_{location_cache_key(lat, lon)}"
                         f"_{GRID_N}_{int(SPAN_NM)}.json")

    def start(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _points(self):
        lats, lons = [], []
        for row in range(GRID_N):
            plat = (self._north
                    - (row + 0.5) * (self._north - self._south) / GRID_N)
            for col in range(GRID_N):
                plon = (self._west
                        + (col + 0.5) * (self._east - self._west) / GRID_N)
                lats.append(plat)
                lons.append(plon)
        return lats, lons

    def _load(self):
        """Cache first (elevation is static, so a hit is good forever),
        then the network."""
        cached = read_stale(self._cache)
        if cached is not None and self._install(cached.get("elev", [])):
            self.status = "ready"
            debug_log(f"terrain from cache: MVA up to {self.max_mva:,.0f} ft")
            return
        self._fetch()

    def _fetch(self, retries=2):
        """Network fetch with a modest whole-grid retry.  Batches retry on
        their own (see _fetch_grid), so this outer loop is only a last resort
        when a batch exhausts its own attempts."""
        for attempt in range(retries):
            if attempt:
                time.sleep(5.0 * attempt)
            elev_m = self._fetch_grid()
            if elev_m is not None and self._install(elev_m):
                write_cache(self._cache, {"elev": elev_m})
                self.status = "ready"
                debug_log(f"terrain grid ready: MVA up to {self.max_mva:,.0f} ft")
                return
        self.status = "failed"

    def _fetch_grid(self):
        """Every elevation sample, batched.  Each batch retries independently
        and completed batches are kept, so one flaky request no longer
        discards the whole grid and forces a restart."""
        lats, lons = self._points()
        elev_m = []
        for i in range(0, len(lats), BATCH):
            if i:
                time.sleep(0.3)  # a polite gap between grid batches
            batch = self._fetch_batch(lats[i:i + BATCH], lons[i:i + BATCH])
            if batch is None:
                return None
            elev_m.extend(batch)
        return elev_m

    def _fetch_batch(self, lats, lons, tries=4):
        """One batch of ≤BATCH points, retried with exponential backoff
        (1, 2, 4 s) to ride out a transient timeout or rate-limit."""
        for attempt in range(tries):
            if attempt:
                time.sleep(min(8.0, 2.0 ** (attempt - 1)))
            try:
                data = fetch_json(
                    URL + "?latitude=" + ",".join(f"{v:.4f}" for v in lats)
                    + "&longitude=" + ",".join(f"{v:.4f}" for v in lons),
                    headers={"User-Agent": USER_AGENT}, timeout=15)
                elev = data["elevation"]
                if len(elev) == len(lats):
                    return elev
                debug_log("terrain batch came back short; retrying")
            except Exception as exc:
                debug_log(f"terrain batch failed (try {attempt + 1}): {exc}")
        return None

    def _install(self, elev_m):
        """Build and publish the MVA grid from a flat GRID_N×GRID_N elevation
        list (from the network or the cache).  Returns False on a short list."""
        if len(elev_m) != GRID_N * GRID_N:
            return False
        mva = []
        for row in range(GRID_N):
            vals = elev_m[row * GRID_N:(row + 1) * GRID_N]
            mva.append([
                math.ceil((max(0.0, v) * 3.28084 + BUFFER_FT) / 100.0) * 100.0
                for v in vals])
        with self._lock:
            self._mva = mva
            self.max_mva = max(max(r) for r in mva)
        return True

    def mva_at(self, lat, lon):
        """Minimum vectoring altitude (ft) here, or None while unknown.

        Points off the grid answer None too — beyond the gates it's the
        next facility's problem.
        """
        with self._lock:
            mva = self._mva
        if mva is None:
            return None
        row = int((self._north - lat) / (self._north - self._south) * GRID_N)
        col = int((lon - self._west) / (self._east - self._west) * GRID_N)
        if 0 <= row < GRID_N and 0 <= col < GRID_N:
            return mva[row][col]
        return None

    def mva_smooth(self, lat, lon):
        """Bilinearly interpolated MVA (ft) for shading — display only.

        mva_at() stays nearest-cell: the safety floor a pilot quotes is the
        conservative per-sector maximum and must not soften near a peak.
        This is only the underlay tint, so the terrain reads as a smooth
        relief instead of the raw 18×18 fetch grid.
        """
        with self._lock:
            mva = self._mva
        if mva is None:
            return None
        # cell centres sit at row+0.5 / col+0.5, so shift by half a cell to
        # get fractional indices into the sample grid
        fr = (self._north - lat) / (self._north - self._south) * GRID_N - 0.5
        fc = (lon - self._west) / (self._east - self._west) * GRID_N - 0.5
        if fr < -0.5 or fr > GRID_N - 0.5 or fc < -0.5 or fc > GRID_N - 0.5:
            return None      # off the grid — next facility's problem
        r0 = max(0, min(GRID_N - 1, int(math.floor(fr))))
        c0 = max(0, min(GRID_N - 1, int(math.floor(fc))))
        r1, c1 = min(GRID_N - 1, r0 + 1), min(GRID_N - 1, c0 + 1)
        tr = min(1.0, max(0.0, fr - r0))
        tc = min(1.0, max(0.0, fc - c0))
        top = mva[r0][c0] * (1.0 - tc) + mva[r0][c1] * tc
        bot = mva[r1][c0] * (1.0 - tc) + mva[r1][c1] * tc
        return top * (1.0 - tr) + bot * tr
