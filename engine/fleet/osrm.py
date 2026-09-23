"""Road geometry from OSRM — optional, and only with the network switched on.

The rest of the engine routes road legs on a buffered great-circle corridor
(engine/network/geo.py explains why OSMnx was dropped). That is fine for "is
this event on the corridor" and poor for drawing a detour a planner will
actually read: a truck avoiding a closed A5 does not drive a geodesic.

So recovery routes ask OSRM for the road, when they are allowed to ask:

* nothing is called unless RADAR_ALLOW_NETWORK=1 — the same switch every
  other outbound request in the system obeys;
* the URL is config (fleet.yaml `routing.osrm_url`), so a self-hosted OSRM
  is a one-line change. The public demo server is a courtesy with a
  fair-use policy, not a service to build on;
* any failure — timeout, 4xx, a route OSRM cannot find — returns None and the
  caller falls back to the corridor estimate, labelled as such. Never raises.

Results are cached per coordinate tuple for the life of the process: the same
detour is asked for on every click of the same asset.
"""

from __future__ import annotations

import json
import urllib.request

from engine.network.geo import Point

_CACHE: dict[tuple, tuple[list[Point], float] | None] = {}


def road(points: list[Point], base_url: str, timeout_s: float = 4.0
         ) -> tuple[list[Point], float] | None:
    """(path, km) along roads through *points*, or None.

    Distance only — time is the rate card's, so a road leg found by OSRM and a
    road leg estimated on the corridor are timed on the same basis. OSRM's own
    duration assumes one driver who never stops.
    """
    from engine.ingest.sources.fetch import network_allowed  # noqa: PLC0415

    if not network_allowed() or len(points) < 2:
        return None
    key = (base_url, tuple((round(p.lat, 4), round(p.lon, 4)) for p in points))
    if key in _CACHE:
        return _CACHE[key]

    coords = ";".join(f"{p.lon:.5f},{p.lat:.5f}" for p in points)
    url = (f"{base_url.rstrip('/')}/route/v1/driving/{coords}"
           "?overview=simplified&geometries=geojson&alternatives=false&steps=false")
    result: tuple[list[Point], float] | None = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "sika-risk-radar/0.1"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            blob = json.loads(response.read().decode("utf-8"))
        route = (blob.get("routes") or [None])[0]
        if blob.get("code") == "Ok" and route:
            line = route["geometry"]["coordinates"]
            path = [Point(lat=lat, lon=lon) for lon, lat in line]
            if len(path) >= 2:
                result = (path, float(route["distance"]) / 1000.0)
    except Exception:  # noqa: BLE001 — any failure means "use the corridor"
        result = None
    _CACHE[key] = result
    return result
