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
from blips._geo import advance
from blips._http import fetch_json
from blips._runtime import debug_log

URL = "https://api.open-meteo.com/v1/elevation"
GRID_N = 18               # cells per side
SPAN_NM = 130.0           # grid coverage, comfortably past the gates
BUFFER_FT = 2000.0        # obstacle clearance over the highest terrain
BATCH = 100               # points per API call (the documented max)


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

    def start(self):
        threading.Thread(target=self._fetch, daemon=True).start()

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

    def _fetch(self, retries=3):
        """Fetch the grid, retrying with backoff — a transient rate-limit
        shouldn't quietly flatten the world for a whole shift."""
        for attempt in range(retries):
            if attempt:
                time.sleep(20.0 * attempt)
            if self._try_fetch():
                self.status = "ready"
                return
        self.status = "failed"

    def _try_fetch(self):
        lats, lons = self._points()
        elev_m = []
        try:
            for i in range(0, len(lats), BATCH):
                if i:
                    time.sleep(0.5)  # a polite gap between grid batches
                data = fetch_json(
                    URL + "?latitude=" + ",".join(
                        f"{v:.4f}" for v in lats[i:i + BATCH])
                    + "&longitude=" + ",".join(
                        f"{v:.4f}" for v in lons[i:i + BATCH]),
                    headers={"User-Agent": USER_AGENT}, timeout=10)
                elev_m.extend(data["elevation"])
        except Exception as exc:
            debug_log(f"terrain fetch failed: {exc}")
            return False
        if len(elev_m) != GRID_N * GRID_N:
            debug_log("terrain fetch came back short; staying flat")
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
        debug_log(f"terrain grid ready: MVA up to {self.max_mva:,.0f} ft")
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
