"""Per-lane consignment views: the cargo page, and the execute view.

WHY THESE EXIST
===============
From the team, on the map:

    "one disruption on a route may only affect some of the vessels using that
     route. And the effects will not be the same for all vessels along the
     same route."

That is already true in the engine and has never been visible. The gate is
per (event, shipment, leg) and the Monte Carlo is per shipment, so on a
thirteen-consignment lane there are routinely twelve distinct deadlines. The
board showed one colour for the lane and hid all of it.

So: ``lane_view`` lists every consignment on a lane with its OWN answer, and
says plainly which ones the event never reached.

AND THE SIMPLIFIED VIEW IS A FILTER, NOT A SECOND APP
-----------------------------------------------------
Also from the team:

    "The calculations cannot be simplified."

Correct, and it is the constraint that decides the architecture. ``execute_view``
returns a SUBSET of numbers already computed for the planner's board — it
recomputes nothing. The moment a driver's screen works out its own ETA it
will disagree with the planner's, and a planner who has been contradicted by
the tool once stops using it.

What the transport manager gets is therefore not a smaller model. It is the
same model, answering only the questions someone executing needs: where is it
going next, what is the constraint, who do I call. The decision-making — the
ladder, the matrix, the convene rule — is absent because it is not theirs to
make, not because it was too complicated to show.

WHAT THEY CAN GIVE BACK
-----------------------
A driver or an on-site agent knows things no feed here carries: the actual
queue at the gate, whether the crane turned up, whether the load shifted.
``report_template`` is that channel, and it would be the first tier-1
OBSERVED source in the system — better than anything currently connected.
It is a socket: the app composes the report, it does not submit it, because
no store is wired and faking one would be worse than the gap.
"""

from __future__ import annotations

from engine.pipeline import RunContext


def lane_view(board: dict, context: RunContext, route_id: str) -> dict:
    """Every consignment on one lane, each with its own answer."""
    route = next(
        (r for r in board["routes"] if r["route_id"] == route_id), None
    )
    if route is None:
        return {"error": f"unknown route {route_id!r}"}

    shipments = [s for s in context.shipments if s.lane_id == route_id]

    # Per-shipment risk, keyed so untouched consignments stay visible as
    # untouched rather than silently dropping off the page. "This event does
    # not reach eleven of your twenty-two" is an answer a planner wants.
    risk_by_shipment: dict[str, list] = {}
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            risk_by_shipment.setdefault(risk.shipment_id, []).append(
                (assessment.event, risk)
            )

    consignments = []
    for shipment in shipments:
        entries = risk_by_shipment.get(shipment.shipment_id, [])
        touched = bool(entries)

        binding = None
        if entries:
            actionable = [
                (e, r) for e, r in entries if r.lead_time_hours is not None
            ]
            if actionable:
                binding = min(actionable, key=lambda pair: pair[1].lead_time_hours)
            else:
                binding = entries[0]

        event, risk = binding if binding else (None, None)
        consignments.append({
            "shipment_id": shipment.shipment_id,
            "customer": shipment.customer,
            "origin": shipment.origin_node,
            "destination": shipment.destination_node,
            "mode": shipment.mode,
            "carrier": shipment.carrier,
            "value_chf": shipment.value_chf,
            "eta": shipment.eta.isoformat(),
            "committed": shipment.otif_committed_date.isoformat(),
            "dangerous_goods": shipment.dangerous_goods,
            "temperature_controlled": shipment.temperature_controlled,
            "touched": touched,
            # None for an untouched consignment. Not zero — zero would read as
            # "assessed and found harmless", which is a different claim.
            "lead_time_hours": risk.lead_time_hours if risk else None,
            "actionability": risk.actionability if risk else None,
            "expected_loss_chf": (
                round(risk.do_nothing.expected_loss_chf, 2) if risk else None
            ),
            "conditional_loss_chf": (
                round(risk.do_nothing.conditional_loss_chf, 2) if risk else None
            ),
            "p_late": round(risk.do_nothing.p_late, 3) if risk else None,
            "driving_event": event.title if event else None,
            "driving_event_id": event.event_id if event else None,
            "events_touching": len(entries),
        })

    # Soonest deadline first; untouched last. Ordering by urgency rather than
    # by id, because the page's job is to say which of these to look at.
    consignments.sort(
        key=lambda c: (
            not c["touched"],
            c["lead_time_hours"] if c["lead_time_hours"] is not None else 1e9,
        )
    )

    touched = [c for c in consignments if c["touched"]]
    deadlines = {
        round(c["lead_time_hours"], 1) for c in touched
        if c["lead_time_hours"] is not None
    }

    return {
        "route_id": route_id,
        "name": route["name"],
        "level": route["level"],
        "level_label": route["level_label"],
        "directive": route["directive"],
        "reason": route["reason"],
        "consignments": consignments,
        "summary": {
            "total": len(consignments),
            "touched": len(touched),
            "untouched": len(consignments) - len(touched),
            # The number that justifies this page existing.
            "distinct_deadlines": len(deadlines),
            "exposure_chf": round(
                sum(c["expected_loss_chf"] or 0.0 for c in consignments), 2
            ),
        },
    }


def execute_view(board: dict, context: RunContext, shipment_id: str) -> dict:
    """One consignment, for whoever is moving it.

    Strictly a projection. Every figure here appears on the planner's board;
    nothing is recomputed. See the module docstring for why that is the
    binding constraint rather than a style preference.
    """
    shipment = next(
        (s for s in context.shipments if s.shipment_id == shipment_id), None
    )
    if shipment is None:
        return {"error": f"unknown shipment {shipment_id!r}"}

    route = next(
        (r for r in board["routes"] if r["route_id"] == shipment.lane_id), None
    )

    entries = [
        (a.event, r)
        for a in context.result.assessments
        for r in a.shipment_risks
        if r.shipment_id == shipment_id
    ]
    actionable = [(e, r) for e, r in entries if r.lead_time_hours is not None]
    event, risk = (
        min(actionable, key=lambda pair: pair[1].lead_time_hours)
        if actionable else (entries[0] if entries else (None, None))
    )

    # Which leg is next, and where it is going. The one question someone
    # executing asks first and the board never answered.
    legs = [
        {
            "index": i,
            "from": leg.from_node,
            "to": leg.to_node,
            "from_name": _node_name(context, leg.from_node),
            "to_name": _node_name(context, leg.to_node),
            "mode": leg.mode.value,
            "carrier": leg.carrier,
            "planned_depart": leg.planned_depart.isoformat(),
            "planned_arrive": leg.planned_arrive.isoformat(),
            "buffer_hours": leg.buffer_hours,
            "affected": any(
                h.shipment_id == shipment_id and h.leg_index == i
                for h in context.hits
            ),
        }
        for i, leg in enumerate(shipment.legs)
    ]
    affected_legs = [leg for leg in legs if leg["affected"]]

    response = (route or {}).get("response") or {}

    return {
        "shipment_id": shipment_id,
        "customer": shipment.customer,
        "lane": (route or {}).get("name", shipment.lane_id),
        "route_id": shipment.lane_id,
        # The planner's level, shown as CONTEXT rather than as something to
        # act on. The transport manager is not being asked to decide.
        "status": {
            "level": (route or {}).get("level"),
            "level_label": (route or {}).get("level_label"),
            "what_it_means": (route or {}).get("directive"),
        },
        "shipment": {
            "origin": _node_name(context, shipment.origin_node),
            "destination": _node_name(context, shipment.destination_node),
            "mode": shipment.mode,
            "carrier": shipment.carrier,
            "planned_eta": shipment.eta.isoformat(),
            "committed_date": shipment.otif_committed_date.isoformat(),
            "dangerous_goods": shipment.dangerous_goods,
            "temperature_controlled": shipment.temperature_controlled,
        },
        "legs": legs,
        "affected_legs": [leg["index"] for leg in affected_legs],
        "what_is_happening": (
            {
                "title": event.title,
                "where": ", ".join(
                    _node_name(context, n) for n in event.node_ids
                ) or "on this corridor",
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat() if event.ends_at else None,
                "source": event.provenance.source,
                "source_tier": event.provenance.source_tier,
            }
            if event else None
        ),
        "your_deadline": (
            {
                "hours": risk.lead_time_hours,
                "state": risk.actionability,
                "note": (
                    "This is when a decision has to be made by, not when the "
                    "freight is due."
                ),
            }
            if risk and risk.lead_time_hours is not None else None
        ),
        "coordinate_with": {
            "route_manager": response.get("route_manager"),
            "carrier": shipment.carrier,
        },
        "report_back": report_template(shipment_id),
    }


def report_template(shipment_id: str) -> dict:
    """What the person on the ground can tell us that no feed carries.

    A socket. The app composes; it does not submit, because no store is
    wired and pretending otherwise would put a confirmation on screen for
    something that went nowhere.
    """
    return {
        "shipment_id": shipment_id,
        "fields": [
            {"id": "position", "label": "Where is it now?",
             "type": "text", "hint": "Port, terminal, motorway junction"},
            {"id": "status", "label": "What is actually happening?",
             "type": "choice",
             "options": ["moving", "queued", "held", "stopped", "delivered"]},
            {"id": "revised_eta", "label": "Revised arrival", "type": "datetime"},
            {"id": "cargo_ok", "label": "Is the load intact?",
             "type": "choice", "options": ["intact", "damaged", "unknown"]},
            {"id": "note", "label": "Anything the tool would not know",
             "type": "text",
             "hint": "Gate queue, missing crane, weather on site"},
        ],
        "socket": (
            "Not connected. Submitting this would be the first TIER-1 "
            "OBSERVED source in the system — better than anything currently "
            "wired, because it is someone looking at the freight rather than "
            "a feed describing the region. It needs a store and an auth "
            "decision, both out of scope for the prototype."
        ),
    }


def _node_name(context: RunContext, node_id: str) -> str:
    node = context.config.nodes.get(node_id)
    return node.name if node else node_id
