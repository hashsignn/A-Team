"""How far along is it, and where is it actually.

TWO DIFFERENT CLAIMS, NEVER MERGED
==================================
``planned``   where the schedule says this vehicle should be right now,
              interpolated along the leg it should currently be on.
``observed``  where somebody with the freight said it was, with the instant
              they said it.

A dashboard that shows one dot has to pick, and whichever it picks it is
lying half the time: a planned dot is fiction the moment a truck stops, and
an observed dot is stale the moment it starts moving again.

So both are returned, and the GAP BETWEEN THEM is the interesting number. A
consignment 180 km behind where the plan says it should be is the single most
actionable fact on the page, and it exists only because the two are kept
apart.

WHY PLANNED POSITION IS NOT A LIE
---------------------------------
It is labelled. Interpolating along a great circle between two nodes is a
rough answer — freight follows roads and shipping lanes, not geodesics — and
it is stated as "where the plan puts it", never as a position. The distance
figures come from the same geometry, so they are consistent with each other
even where both are approximations of the real route.

NOTHING HERE READS THE WALL CLOCK.
Progress is computed against the board's as-of, like everything else, which
is what lets a hindcast replay a past day and get that day's positions.
"""

from __future__ import annotations

import math
from datetime import datetime

from engine.network.geo import Point, haversine_km


def interpolate(a: Point, b: Point, fraction: float) -> Point:
    """A point along the great circle from a to b.

    Spherical interpolation rather than a linear average of the coordinates:
    averaging longitudes puts a Rotterdam-to-Shanghai midpoint in Kazakhstan
    and, across the date line, on the wrong side of the planet.
    """
    f = max(0.0, min(1.0, fraction))
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)

    d = 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    if d == 0:
        return a

    x = math.sin((1 - f) * d) / math.sin(d)
    y = math.sin(f * d) / math.sin(d)
    px = x * math.cos(lat1) * math.cos(lon1) + y * math.cos(lat2) * math.cos(lon2)
    py = x * math.cos(lat1) * math.sin(lon1) + y * math.cos(lat2) * math.sin(lon2)
    pz = x * math.sin(lat1) + y * math.sin(lat2)
    return Point(
        lat=math.degrees(math.atan2(pz, math.sqrt(px * px + py * py))),
        lon=math.degrees(math.atan2(py, px)),
    )


def leg_fraction(leg, as_of: datetime) -> float:
    """How far through this leg the schedule says we are, in [0, 1]."""
    start, end = leg.planned_depart, leg.planned_arrive
    if as_of <= start:
        return 0.0
    if as_of >= end:
        return 1.0
    span = (end - start).total_seconds()
    return 0.0 if span <= 0 else (as_of - start).total_seconds() / span


def progress(shipment, nodes: dict, as_of: datetime) -> dict:
    """Distance done, distance left, and where the plan puts it now."""
    legs = []
    total = 0.0
    for leg in shipment.legs:
        a, b = nodes.get(leg.from_node), nodes.get(leg.to_node)
        km = (
            haversine_km(Point(a.lat, a.lon), Point(b.lat, b.lon))
            if a is not None and b is not None else 0.0
        )
        legs.append((leg, km, a, b))
        total += km

    done = 0.0
    current_index = None
    planned: Point | None = None

    for index, (leg, km, a, b) in enumerate(legs):
        fraction = leg_fraction(leg, as_of)
        done += km * fraction
        if 0.0 < fraction < 1.0 and current_index is None:
            current_index = index
            if a is not None and b is not None:
                planned = interpolate(Point(a.lat, a.lon), Point(b.lat, b.lon), fraction)

    # Between legs — sitting at a node — or not started, or finished.
    if planned is None and legs:
        if as_of <= legs[0][0].planned_depart:
            node = legs[0][2]
            current_index = 0
        elif as_of >= legs[-1][0].planned_arrive:
            node = legs[-1][3]
            current_index = len(legs) - 1
        else:
            # Waiting at the destination of the last leg that has finished.
            finished = [i for i, (lg, *_) in enumerate(legs)
                        if as_of >= lg.planned_arrive]
            current_index = finished[-1] if finished else 0
            node = legs[current_index][3]
        if node is not None:
            planned = Point(node.lat, node.lon)

    remaining = max(0.0, total - done)
    return {
        "total_km": round(total, 1),
        "travelled_km": round(done, 1),
        "remaining_km": round(remaining, 1),
        "percent": round(100.0 * done / total, 1) if total else 0.0,
        "current_leg": current_index,
        "planned_position": (
            {"lat": round(planned.lat, 4), "lon": round(planned.lon, 4)}
            if planned else None
        ),
        "leg_km": [round(km, 1) for _, km, _, _ in legs],
    }


def observed(reports: list[dict]) -> dict | None:
    """The most recent report that carried coordinates.

    Text positions are deliberately NOT geocoded into a pin. "Kaub, third in
    the queue" is worth more to a planner as those words than as a dot two
    kilometres from where the vehicle actually is, and a geocoder would give
    the dot the same visual authority as a GPS fix.
    """
    fixes = [r for r in reports if r.get("lat") is not None and r.get("lon") is not None]
    if not fixes:
        return None
    latest = max(fixes, key=lambda r: r["observed_at"])
    return {
        "lat": latest["lat"],
        "lon": latest["lon"],
        "observed_at": latest["observed_at"],
        "reported_by": latest.get("role_label"),
        "first_hand": latest.get("first_hand"),
        "accuracy_m": latest.get("accuracy_m"),
    }


def drift_km(planned: dict | None, seen: dict | None) -> float | None:
    """How far the freight is from where the plan puts it.

    The number this module exists for. It is only meaningful when both are
    known, and None the rest of the time rather than 0 — a zero would read as
    "exactly on plan", which is the opposite of "we have no idea".
    """
    if not planned or not seen:
        return None
    return round(haversine_km(
        Point(planned["lat"], planned["lon"]), Point(seen["lat"], seen["lon"])
    ), 1)
