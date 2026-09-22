"""One route, on its own page: what is wrong, and which vehicles are carrying it.

WHY THIS EXISTS
===============
The board answers "which lanes are in trouble". It cannot answer the next
question a planner actually asks, which is "so what is on that lane, and which
of it is hurt" — and that question is about VEHICLES, not about lanes.

A lane in trouble is an abstraction. Nine trucks, of which two are stuck behind
a closed motorway and one has reported damage, is the thing somebody can act
on. So this view draws the route as its legs, and each leg as the vehicles
moving along it, each one coloured by its own state.

STRICTLY A PROJECTION
---------------------
Every figure here already appears on the planner's board. Nothing is
recomputed, and nothing is rounded differently. Two screens that disagree
about the same number destroy trust in both, and the one a planner will
believe is whichever they saw last — which is not a property you want.

THE THREE COLOURS
-----------------
``affected``  this leg is hit AND the option has closed, or somebody on site
              has reported damage. Red: it has already happened to this one.
``at_risk``   this leg is hit and there is still time. Amber: a decision.
``ok``        this leg is not hit. Green: leave it alone.

The distinction that matters is the middle one. A board that paints everything
touched in red tells a planner to panic about nine vehicles when two need a
decision today and the rest are fine for a week.
"""

from __future__ import annotations

from engine.pipeline import RunContext


def _node_name(context: RunContext, node_id: str) -> str:
    node = context.config.nodes.get(node_id)
    return node.name if node else node_id


def _reports_for(shipment_id: str, context: RunContext) -> list[dict]:
    """Field reports on this consignment, as of the board's instant.

    The as-of filter is the point: a report that arrived after the instant
    this board describes must not appear on it, or the hindcast stops being
    reproducible the moment somebody files something.
    """
    from engine.ingest import reports as reports_mod  # noqa: PLC0415

    try:
        found = reports_mod.as_of(context.clock.as_of)
    except OSError:
        return []
    return [r.as_dict() for r in found if r.shipment_id == shipment_id]


def route_view(board: dict, context: RunContext, route_id: str) -> dict | None:
    """The whole page, in one call."""
    route = next((r for r in board["routes"] if r["route_id"] == route_id), None)
    if route is None:
        return None

    shipments = [s for s in context.shipments if s.lane_id == route_id]
    hits_by_shipment: dict[str, set[int]] = {}
    for hit in context.hits:
        if hit.shipment_id in {s.shipment_id for s in shipments}:
            hits_by_shipment.setdefault(hit.shipment_id, set()).add(hit.leg_index)

    # Per-shipment figures, taken from the board rather than recomputed.
    risk_by_shipment: dict[str, dict] = {}
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            current = risk_by_shipment.get(risk.shipment_id)
            lead = risk.lead_time_hours
            # Keep the SOONEST deadline across every event touching it: that
            # is the one that decides when somebody has to move.
            if lead is None:
                continue
            if current is None or lead < current["lead_time_hours"]:
                risk_by_shipment[risk.shipment_id] = {
                    "lead_time_hours": lead,
                    "actionability": getattr(risk, "actionability", None),
                    "expected_loss_chf": getattr(risk, "expected_loss_chf", 0.0),
                    "driving_event": assessment.event.title,
                    "driving_event_id": assessment.event.event_id,
                }

    reports_cache = {s.shipment_id: _reports_for(s.shipment_id, context) for s in shipments}

    # ---- the route drawn as legs, each carrying its vehicles -----------
    legs: list[dict] = []
    template = shipments[0].legs if shipments else []
    for index, leg in enumerate(template):
        vehicles = []
        for shipment in shipments:
            if index >= len(shipment.legs):
                continue        # a shipment routed short of the full lane
            own = shipment.legs[index]
            hit = index in hits_by_shipment.get(shipment.shipment_id, set())
            risk = risk_by_shipment.get(shipment.shipment_id)
            reports = reports_cache.get(shipment.shipment_id, [])
            damaged = any(r.get("load_state") == "damaged" for r in reports)
            stopped = any(r.get("status") in ("held", "stopped") for r in reports)

            if damaged or (hit and risk and risk["lead_time_hours"] <= 0):
                status = "affected"
            elif hit or stopped:
                status = "at_risk"
            else:
                status = "ok"

            vehicles.append({
                "shipment_id": shipment.shipment_id,
                "customer": shipment.customer,
                "carrier": own.carrier,
                "value_chf": shipment.value_chf,
                "status": status,
                "eta": shipment.eta.isoformat() if shipment.eta else None,
                "leg_arrives": own.planned_arrive.isoformat(),
                "lead_time_hours": (risk or {}).get("lead_time_hours"),
                "driving_event": (risk or {}).get("driving_event"),
                "expected_loss_chf": (risk or {}).get("expected_loss_chf", 0.0),
                "dangerous_goods": shipment.dangerous_goods,
                "temperature_controlled": shipment.temperature_controlled,
                "reports": reports,
            })

        counts = {
            "affected": sum(1 for v in vehicles if v["status"] == "affected"),
            "at_risk": sum(1 for v in vehicles if v["status"] == "at_risk"),
            "ok": sum(1 for v in vehicles if v["status"] == "ok"),
        }
        legs.append({
            "index": index,
            "from": leg.from_node,
            "to": leg.to_node,
            "from_name": _node_name(context, leg.from_node),
            "to_name": _node_name(context, leg.to_node),
            "mode": leg.mode.value,
            "vehicles": vehicles,
            "counts": counts,
        })

    events = route.get("events") or []
    driving = max(
        events,
        key=lambda e: (e.get("shipments_here") or 0, e.get("exposure_chf") or 0),
        default=None,
    )

    return {
        "route_id": route_id,
        "name": route["name"],
        "level": route["level"],
        "level_label": route.get("level_label", route["level"]),
        "directive": route.get("directive", ""),
        "reason": route.get("reason", ""),
        "lead_time_hours": route.get("lead_time_hours"),
        "exposure_chf": route.get("exposure_chf"),
        "shipments_total": len(shipments),
        "shipments_affected": sum(
            1 for s in shipments if hits_by_shipment.get(s.shipment_id)
        ),
        "radar": route.get("radar"),
        "matrix_grid": board.get("matrix_grid"),
        "events": events,
        "driving_event_id": (driving or {}).get("event_id"),
        "legs": legs,
        "totals": {
            "affected": sum(leg["counts"]["affected"] for leg in legs),
            "at_risk": sum(leg["counts"]["at_risk"] for leg in legs),
            "ok": sum(leg["counts"]["ok"] for leg in legs),
        },
    }
