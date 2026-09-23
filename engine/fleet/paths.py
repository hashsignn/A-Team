"""Polyline arithmetic: where along a path, and the path from there on.

progress.py interpolates along the great circle between two nodes, which is
right for "how far through the leg" and wrong for drawing: a barge placed on
the geodesic from Basel to Kaub sits in a field, not on the Rhine. The map
puts the asset on the leg's actual geometry — the river, the sea lane — at
the same fraction of distance, so the dot is on the line it is travelling.
"""

from __future__ import annotations

import math

from engine.network.geo import Point, haversine_km


def length_km(path: list[Point]) -> float:
    return sum(haversine_km(a, b) for a, b in zip(path, path[1:], strict=False))


def split_at(path: list[Point], fraction: float) -> tuple[Point, list[Point], list[Point]]:
    """The point at *fraction* of the path's length, and the two halves.

    Returns (point, travelled, remaining). Both halves include the point, so
    drawing them end to end reproduces the path with no gap.
    """
    if not path:
        raise ValueError("empty path")
    if len(path) == 1:
        return path[0], [path[0]], [path[0]]

    f = max(0.0, min(1.0, fraction))
    total = length_km(path)
    if total == 0.0 or f == 0.0:
        return path[0], [path[0]], list(path)
    if f == 1.0:
        return path[-1], list(path), [path[-1]]

    target = total * f
    walked = 0.0
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False)):
        seg = haversine_km(a, b)
        if walked + seg >= target and seg > 0:
            t = (target - walked) / seg
            at = Point(lat=a.lat + (b.lat - a.lat) * t, lon=a.lon + (b.lon - a.lon) * t)
            return at, [*path[: i + 1], at], [at, *path[i + 1:]]
        walked += seg
    return path[-1], list(path), [path[-1]]


def bearing_deg(a: Point, b: Point) -> float:
    """Initial bearing from a to b, degrees clockwise from north."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def as_latlon(path: list[Point], digits: int = 4) -> list[list[float]]:
    """[[lat, lon], …] — the wire format, rounded so payloads stay small."""
    return [[round(p.lat, digits), round(p.lon, digits)] for p in path]


def midpoint(path: list[Point]) -> Point:
    return split_at(path, 0.5)[0]
