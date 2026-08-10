"""Metre distances for the pure core.

`location_data.krovak` already carries this formula, but importing it would pull `pyproj`
into the resolver's import graph for one 6-line function, and the resolver core must import
and run with nothing but the stdlib. The Earth radius is the same constant; the DB-side
jobs use `::geography` casts, which agree with this to well under a metre at CZ latitudes
— far inside every threshold this module feeds (300 m registry↔pin, 250 m sliver).
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def distance_between(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    if a is None or b is None:
        return None
    return haversine_m(a[0], a[1], b[0], b[1])
