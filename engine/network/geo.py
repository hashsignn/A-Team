"""Geometry for the spatial gate and the map.

WHY THERE IS NO OSMnx HERE
--------------------------
BRIEF §8.7 names OSMnx for road and rail routing. It is dropped, deliberately:

* it needs geopandas / shapely / rtree / GEOS, which is a slow and fragile
  install on a machine you do not control;
* it queries Overpass at runtime, so the demo acquires a network dependency at
  exactly the moment you least want one;
* for ten to fifteen named lanes it buys almost nothing. The spatial gate asks
  "is this event on the corridor this shipment uses", and a buffered
  great-circle corridor answers that as well as a road graph does at the
  resolution a planner cares about.

OSMnx ships as a visibly empty socket (BRIEF §8.2) rather than a silent
absence: the /inputs panel says road routing is running on corridor geometry,
and says what wiring the real thing would buy.

Sea routing uses ``searoute`` where it is available — it is offline and
bundles its own network — and falls back to chokepoint-waypoint corridors
otherwise. Naming the chokepoints explicitly is arguably better than a routing
library for this job: Suez and Malacca then exist as graph nodes the gate can
intersect, which is what lets "Panama cuts transits" find the shipments it
actually touches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)


def haversine_km(a: Point, b: Point) -> float:
    """Great-circle distance in kilometres."""
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def great_circle_points(a: Point, b: Point, segments: int = 24) -> list[Point]:
    """Interpolate a great-circle arc.

    Drawn on a map this is what stops a Rotterdam -> Shanghai lane rendering as
    a straight line through central Asia.
    """
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)

    d = 2 * math.asin(
        math.sqrt(
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        )
    )
    if d == 0:
        return [a, b]

    out: list[Point] = []
    for i in range(segments + 1):
        f = i / segments
        A = math.sin((1 - f) * d) / math.sin(d)
        B = math.sin(f * d) / math.sin(d)
        x = A * math.cos(lat1) * math.cos(lon1) + B * math.cos(lat2) * math.cos(lon2)
        y = A * math.cos(lat1) * math.sin(lon1) + B * math.cos(lat2) * math.sin(lon2)
        z = A * math.sin(lat1) + B * math.sin(lat2)
        out.append(
            Point(
                lat=math.degrees(math.atan2(z, math.sqrt(x * x + y * y))),
                lon=math.degrees(math.atan2(y, x)),
            )
        )
    return out


def cross_track_distance_km(point: Point, seg_a: Point, seg_b: Point) -> float:
    """Shortest distance from *point* to the great-circle segment a->b.

    This is the whole spatial gate. BRIEF §3.2 calls layer 1 the filter that
    does the most work and has zero parameters — either the event is near a
    corridor you use or it is not. The only parameter anywhere near it is the
    corridor width, and that is declared per mode rather than tuned.
    """
    d13 = haversine_km(seg_a, point) / EARTH_RADIUS_KM
    if d13 == 0.0:
        return 0.0

    d12 = haversine_km(seg_a, seg_b) / EARTH_RADIUS_KM
    if d12 == 0.0:  # degenerate segment
        return d13 * EARTH_RADIUS_KM

    delta_bearing = _bearing_rad(seg_a, point) - _bearing_rad(seg_a, seg_b)

    # Behind the start: the perpendicular foot is off the segment.
    if math.cos(delta_bearing) < 0.0:
        return haversine_km(point, seg_a)

    cross_track = math.asin(math.sin(d13) * math.sin(delta_bearing))
    along_track = math.acos(
        max(-1.0, min(1.0, math.cos(d13) / math.cos(cross_track)))
    )

    # Past the end: likewise off the segment.
    if along_track > d12:
        return haversine_km(point, seg_b)

    return abs(cross_track) * EARTH_RADIUS_KM


def _bearing_rad(a: Point, b: Point) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.atan2(y, x)


# ---------------------------------------------------------------------
# Corridor widths
# ---------------------------------------------------------------------
# BRIEF §13 lists corridor width as an open question. These are the declared
# answers. They are deliberately generous: a corridor that is too narrow drops
# real disruptions silently, while one that is too wide costs a row the planner
# dismisses in a second. Asymmetric error, asymmetric default.
CORRIDOR_WIDTH_KM: dict[str, float] = {
    "road": 60.0,
    "rail": 45.0,
    "barge": 25.0,  # a river is narrow; the gauge is the point
    "sea": 250.0,   # deep-sea lanes are not surveyed routes
}

# How far inland from a port an event still counts as touching that port.
PORT_CATCHMENT_KM = 40.0


def on_corridor(
    point: Point,
    path: list[Point],
    mode: str,
    width_km: float | None = None,
) -> tuple[bool, float]:
    """Is *point* within the corridor of *path*? Returns (hit, distance_km)."""
    width = width_km if width_km is not None else CORRIDOR_WIDTH_KM.get(mode, 60.0)
    if len(path) < 2:
        if not path:
            return (False, float("inf"))
        d = haversine_km(point, path[0])
        return (d <= width, d)

    best = float("inf")
    for a, b in zip(path, path[1:], strict=False):
        best = min(best, cross_track_distance_km(point, a, b))
    return (best <= width, best)


def bounding_box(points: list[Point], pad_deg: float = 1.0) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon), padded.

    Layer 1 of the funnel: a cheap rejection before any real geometry runs.
    """
    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    return (
        min(lats) - pad_deg,
        min(lons) - pad_deg,
        max(lats) + pad_deg,
        max(lons) + pad_deg,
    )


def in_bounding_box(point: Point, box: tuple[float, float, float, float]) -> bool:
    min_lat, min_lon, max_lat, max_lon = box
    return min_lat <= point.lat <= max_lat and min_lon <= point.lon <= max_lon
