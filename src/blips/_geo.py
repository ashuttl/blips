"""Shared geospatial helpers."""

import math


def haversine_nm(lat1, lon1, lat2, lon2):
    """Distance in nautical miles between two points."""
    earth_radius_nm = 3440.065
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return earth_radius_nm * 2 * math.asin(math.sqrt(a))


def advance(lat, lon, track_deg, dist_nm):
    """Move a point along a bearing (equirectangular; fine at scope ranges)."""
    rad = math.radians(track_deg)
    dlat = dist_nm * math.cos(rad) / 60.0
    dlon = (dist_nm * math.sin(rad)
            / (60.0 * max(0.2, math.cos(math.radians(lat)))))
    return lat + dlat, lon + dlon


def bearing_to(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, degrees true."""
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.cos(dlon))
    return math.degrees(math.atan2(y, x)) % 360.0


def turn_delta(hdg, tgt, direction=None):
    """Signed degrees from ``hdg`` to ``tgt`` turning ``direction``.

    'l' turns are negative, 'r' positive; None takes the shorter way
    (the sim uses that for its own flying — the player always says which).
    """
    if direction == "l":
        return -((hdg - tgt) % 360.0)
    if direction == "r":
        return (tgt - hdg) % 360.0
    return ((tgt - hdg + 180.0) % 360.0) - 180.0


def cross_along_track(lat, lon, thr_lat, thr_lon, course_deg):
    """(cross_nm, along_nm) of a point relative to a final-approach course.

    ``along`` is distance out from the threshold measured back along the
    approach course (positive = still inbound); ``cross`` is signed lateral
    offset from the extended centreline (positive = right of course, from
    the pilot's seat flying inbound).
    """
    dist = haversine_nm(lat, lon, thr_lat, thr_lon)
    brg = bearing_to(lat, lon, thr_lat, thr_lon)
    off = math.radians(brg - course_deg)
    return -dist * math.sin(off), dist * math.cos(off)
