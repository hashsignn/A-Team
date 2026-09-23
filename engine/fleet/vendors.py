"""Who nearby can carry it: partners in the radius of the disruption.

WHERE THE LIST COMES FROM
=========================
Two sources, never blended into one confidence:

* ``fleet.yaml`` partners — synthetic, with a capacity snapshot, a service
  radius and what they are certified for. The socket a partner capacity API
  would fill.
* ``contacts.yaml`` local vendors — the "who do I ring at that port" list the
  board already uses, placed at their node. They carry no capacity figure, so
  capacity is UNKNOWN and says so. An absence never becomes a value; "3 trucks
  available" invented for a vendor nobody asked would be the worst kind of
  number on this page, because it is the one a planner would act on.

SERVICEABLE, LEGALLY AND PHYSICALLY
===================================
For each recovery route generated for this asset, each partner is checked:

  physical  it runs a mode one of the route's NEW legs uses, and that leg
            starts or ends inside its service radius
  legal     dangerous goods need an ADR-certified carrier on road and rail;
            temperature-controlled cargo needs reefer equipment
  capacity  enough units for the TEU being moved, or partial, or unknown

A partner that fails is still listed with the reason. "Mainz Haulage is ten
kilometres away and cannot take ADR" is information a planner wants before
they ring them, not after.
"""

from __future__ import annotations

import math

from engine.fleet import manifest
from engine.fleet.assets import index, locate
from engine.fleet.reroute import recovery
from engine.fleet.settings import settings as fleet_settings
from engine.network.geo import Point, haversine_km
from engine.pipeline import RunContext

# contacts.yaml `service` -> (kind, modes)
LOCAL_SERVICE = {
    "customs_and_haulage": ("forwarder", ["road"]),
    "storage": ("warehouse", ["road"]),
    "barge_terminal": ("terminal", ["barge", "road"]),
    "barge_rail_transfer": ("terminal", ["barge", "rail"]),
    "agency": ("forwarder", ["sea"]),
}
TRANSFER_KINDS = {"warehouse", "terminal"}


def partners(context: RunContext) -> list[dict]:
    """Every partner the deployment knows about, from both sources."""
    cfg = fleet_settings(context.config)["vendors"]
    out = []
    for p in cfg.get("partners") or []:
        out.append({
            "id": p["id"],
            "name": p["name"],
            "kind": p.get("kind", "forwarder"),
            "channel": p.get("channel", "phone"),
            "city": p.get("city"),
            "lat": float(p["lat"]),
            "lon": float(p["lon"]),
            "modes": list(p.get("modes") or []),
            "capacity": p.get("capacity"),
            "service_radius_km": float(p.get("service_radius_km", 100)),
            "adr_certified": p.get("adr_certified"),
            "reefer": p.get("reefer"),
            "contact": {"phone": p.get("phone"), "email": p.get("email"),
                        "portal": p.get("portal")},
            "source": "fleet.yaml",
            "synthetic": "synthetic" in p["name"].lower(),
        })

    nodes = context.config.nodes
    local = context.config.contacts.get("local_vendors", {}) or {}
    for node_id, entries in local.items():
        node = nodes.get(node_id)
        if node is None:
            continue
        for i, v in enumerate(entries):
            kind, modes = LOCAL_SERVICE.get(v.get("service"), ("forwarder", ["road"]))
            out.append({
                "id": f"LV_{node_id}_{i}",
                "name": v["name"],
                "kind": kind,
                "channel": "phone",
                "city": node.name,
                "lat": node.lat,
                "lon": node.lon,
                "modes": modes,
                "capacity": None,
                "service_radius_km": 80.0,
                "adr_certified": None,
                "reefer": None,
                "contact": {"phone": v.get("phone"), "email": v.get("email"), "portal": None},
                "source": "contacts.yaml",
                "synthetic": "synthetic" in v["name"].lower(),
            })
    return out


def nearby(board: dict, context: RunContext, shipment_id: str,
           radius_km: float | None = None, weights: dict | None = None,
           teu: int | None = None) -> dict | None:
    """Partners around the asset, and which recovery routes each could cover."""
    idx = index(context)
    shipment = idx.shipments.get(shipment_id)
    if shipment is None:
        return None
    where = locate(context, shipment)
    if where is None:
        return None

    cfg = fleet_settings(context.config)["vendors"]
    mode = shipment.legs[where["leg_index"]].mode.value
    radius = float(radius_km if radius_km is not None else cfg["radius_km"].get(mode, 150))
    center = where["point"]

    routes = recovery(board, context, shipment_id, weights=weights)
    candidates = (routes or {}).get("candidates") or []
    moving_teu = teu if teu is not None else manifest.teu_of(manifest.containers(shipment))

    listed = []
    for p in partners(context):
        d = haversine_km(center, Point(p["lat"], p["lon"]))
        listed.append({**p, "distance_km": round(d, 1), "within_radius": d <= radius})

    inside = sorted((p for p in listed if p["within_radius"]), key=lambda p: p["distance_km"])
    fallback = False
    if not inside:
        n = int(cfg.get("fallback_nearest", 3))
        inside = sorted(listed, key=lambda p: p["distance_km"])[:n]
        fallback = True

    out = []
    for p in inside:
        checks = [_serviceable(p, c, shipment, moving_teu) for c in candidates]
        out.append({**p, "serviceable": checks, "covers_any": any(c["ok"] for c in checks)})

    return {
        "shipment_id": shipment_id,
        "center": {"lat": round(center.lat, 5), "lon": round(center.lon, 5)},
        "radius_km": radius,
        "mode": mode,
        "fallback": fallback,
        "note": (
            f"No partner within {radius:.0f} km. The nearest {len(out)} are shown, "
            "flagged as outside the radius."
            if fallback else None
        ),
        "teu": moving_teu,
        "vendors": out,
    }


def _serviceable(p: dict, route: dict, shipment, teu: int) -> dict:
    reasons: list[str] = []
    new_legs = [leg for leg in route["legs"] if leg["new"]]
    here = Point(p["lat"], p["lon"])

    def within(end: dict) -> bool:
        return haversine_km(here, Point(end["lat"], end["lon"])) <= p["service_radius_km"]

    covered = [
        leg for leg in new_legs
        if leg["mode"] in p["modes"] and (within(leg["from"]) or within(leg["to"]))
    ]
    physical = bool(covered)
    if not physical and p["kind"] in TRANSFER_KINDS and any(within(leg["from"]) for leg in new_legs):
        physical = True
        reasons.append("cross-dock / hold at the transfer point")
    if not physical:
        modes = sorted({leg["mode"] for leg in new_legs})
        if not set(modes) & set(p["modes"]):
            reasons.append(f"runs {', '.join(p['modes'])}; this route needs {', '.join(modes)}")
        else:
            reasons.append(f"outside its {p['service_radius_km']:.0f} km service radius")

    legal = True
    needs = {leg["mode"] for leg in covered}
    if shipment.dangerous_goods and needs & {"road", "rail"}:
        if p["adr_certified"] is False:
            legal = False
            reasons.append("not ADR-certified — cannot carry this dangerous-goods load")
        elif p["adr_certified"] is None:
            reasons.append("ADR certification unknown — confirm before booking")
    if shipment.temperature_controlled:
        if p["reefer"] is False:
            legal = False
            reasons.append("no reefer equipment for temperature-controlled cargo")
        elif p["reefer"] is None:
            reasons.append("reefer capability unknown — confirm before booking")

    capacity = "unknown"
    cap = p.get("capacity")
    if cap and cap.get("teu_per_unit"):
        needed = max(1, math.ceil(teu / float(cap["teu_per_unit"])))
        available = int(cap.get("available", 0))
        capacity = "full" if available >= needed else ("partial" if available else "none")
        if capacity != "full" and physical:
            reasons.append(f"{available} available, {needed} {cap.get('unit', 'units')} needed")
    elif physical:
        reasons.append("capacity not on file — call to confirm")

    return {
        "route_id": route["id"],
        "rank": route.get("rank"),
        "badge": route.get("badge"),
        "label": route["label"],
        "ok": physical and legal and capacity != "none",
        "physical": physical,
        "legal": legal,
        "capacity": capacity,
        "reasons": reasons,
    }
