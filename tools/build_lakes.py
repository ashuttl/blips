"""Rebuild the basemap's lake layer from Natural Earth 1:10m.

The vendored basemap ships 1:50m lakes, which is too coarse for a region
like Maine — Sebago and Moosehead vanish while Lake Winnipesaukee next door
survives.  This lifts just the lakes to 1:10m (public domain, nvkelso's
GeoJSON mirror), keeps every lake above an area threshold so the bump is a
consistent worldwide gain rather than a regional special-case, simplifies
the rings to the basemap's density, and writes them back into
data/basemap.json.gz in place.  Land/coast/borders/cities are untouched.

    PYTHONPATH=src uv run python tools/build_lakes.py
"""

import gzip
import json
import math
import os
import urllib.request

SRC = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
       "master/geojson/ne_10m_lakes.geojson")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "blips", "data",
                   "basemap.json.gz")
MIN_KM2 = 40.0        # keep lakes at least this big (Sebago is ~117)
EPS_DEG = 0.008       # ~0.9 km simplification tolerance, matches basemap
UA = {"User-Agent": "blips-basemap/0.1 (github.com/ashuttl/blips)"}


def ring_area_km2(ring):
    """Approximate area of a lon/lat ring, latitude-corrected."""
    lat = sum(p[1] for p in ring) / len(ring)
    a = 0.0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(a) / 2 * (111.32 ** 2) * math.cos(math.radians(lat))


def _dp(points, eps):
    """Douglas-Peucker line simplification in degrees."""
    if len(points) < 3:
        return points
    ax, ay = points[0]
    bx, by = points[-1]
    dx, dy = bx - ax, by - ay
    norm = math.hypot(dx, dy) or 1e-9
    far_i, far_d = 0, -1.0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        d = abs((px - ax) * dy - (py - ay) * dx) / norm
        if d > far_d:
            far_i, far_d = i, d
    if far_d <= eps:
        return [points[0], points[-1]]
    left = _dp(points[:far_i + 1], eps)
    right = _dp(points[far_i:], eps)
    return left[:-1] + right


def simplify_ring(ring):
    # a closed ring degenerates under Douglas-Peucker (its baseline is a
    # zero-length start==end segment), so split it at the vertex farthest
    # from the start and simplify the two open chains, then reclose.
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    if len(pts) < 3:
        return None
    ax, ay = pts[0]
    far = max(range(len(pts)),
              key=lambda i: (pts[i][0] - ax) ** 2 + (pts[i][1] - ay) ** 2)
    chain = _dp(pts[:far + 1], EPS_DEG)[:-1] + _dp(pts[far:] + [pts[0]], EPS_DEG)
    out = [[round(x, 3), round(y, 3)] for x, y in chain]
    if out[0] != out[-1]:
        out.append([out[0][0], out[0][1]])
    return out if len(out) >= 4 else None


def polygons(geom):
    """Each polygon (a list of rings) in a Polygon/MultiPolygon geometry."""
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def main():
    print(f"fetching {SRC.rsplit('/', 1)[1]} ...")
    req = urllib.request.Request(SRC, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as fh:
        gj = json.load(fh)
    lakes = []
    for feat in gj["features"]:
        for poly in polygons(feat["geometry"]):
            if not poly or ring_area_km2(poly[0]) < MIN_KM2:
                continue
            rings = [r for r in (simplify_ring(ring) for ring in poly) if r]
            if rings:
                lakes.append(rings)
    lakes.sort(key=lambda rings: (rings[0][0][0], rings[0][0][1]))

    with gzip.open(OUT, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    before = len(data.get("lakes", []))
    data["lakes"] = lakes
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))

    size = os.path.getsize(OUT)
    print(f"lakes {before} -> {len(lakes)}  (>= {MIN_KM2:.0f} km2)")
    print(f"wrote {OUT}  ({size / 1e6:.2f} MB)")
    # confirm the pine tree state made it
    want = {"Sebago": (-70.55, 43.87), "Moosehead": (-69.65, 45.69),
            "Winnipesaukee": (-71.34, 43.63), "Chesuncook": (-69.34, 46.0)}
    for name, (wx, wy) in want.items():
        hit = any(abs(sum(p[0] for p in rings[0]) / len(rings[0]) - wx) < 0.3
                  and abs(sum(p[1] for p in rings[0]) / len(rings[0]) - wy) < 0.3
                  for rings in lakes)
        print(f"  {'OK ' if hit else 'MISS'} {name}")


if __name__ == "__main__":
    main()
