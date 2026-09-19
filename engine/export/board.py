"""Assemble the pipeline's output into the payload the UI consumes.

BRIEF §9.1 rule 1: ``api/`` is thin and all logic lives in ``engine/``. So the
shaping happens here, not in the route handler — which also means the board can
be tested without starting a server, and exported to CSV from the same numbers
the globe draws.

The board is organised around ROUTES, not events. That is the change the client
asked for: rank the *lines affected* by the severity of the effect, because a
planner owns lanes, not headlines.
"""

from __future__ import annotations

from collections import defaultdict

from engine.act import contacts as contacts_mod
from engine.pipeline import RunContext
from engine.schemas import ShipmentRisk
from engine.score.severity import (
    LEVEL_DIRECTIVE,
    LEVEL_LABEL,
    Level,
    classify,
    severity_score,
)

# Altitude the route lines float above the globe surface. Flat on the sphere
# they are occluded by the horizon and unreadable; too high and they read as
# arcs through space rather than as freight moving over ground.
PATH_ALTITUDE = 0.012


def build_board(context: RunContext) -> dict:
    """The whole payload: nodes, routes, radar data, ranking, posture."""
    config = context.config
    result = context.result

    risks_by_route, events_by_route = _index_by_route(context)
    exposure_cap = _exposure_cap(risks_by_route)

    routes = [
        _build_route(lane, risks_by_route, events_by_route, context, exposure_cap)
        for lane in config.lanes
    ]
    routes.sort(key=lambda r: r["severity_score"], reverse=True)

    return {
        "as_of": result.as_of.isoformat(),
        "as_of_label": str(context.clock),
        "config_version": result.config_version,
        "shipments_total": result.shipments_total,
        "variables_total": len(config.variables),
        "posture": _posture(context),
        "levels": [
            {
                "level": lvl.value,
                "label": LEVEL_LABEL[lvl],
                "directive": LEVEL_DIRECTIVE[lvl],
                "count": sum(1 for r in routes if r["level"] == lvl.value),
            }
            for lvl in (Level.RED, Level.YELLOW, Level.BLUE, Level.WHITE, Level.GREEN)
        ],
        "nodes": _nodes(context),
        "routes": routes,
        "funnel": {
            "raw_observations": result.funnel.raw_observations,
            "after_geographic": result.funnel.after_geographic,
            "after_type": result.funnel.after_type,
            "after_temporal": result.funnel.after_temporal,
            "after_resolution": result.funnel.after_resolution,
            "reasoned": result.funnel.reasoned,
            "gated_hits": result.funnel.gated_hits,
            "shipments_touched": result.funnel.shipments_touched,
        },
    }


# ---------------------------------------------------------------------
def _index_by_route(context: RunContext) -> tuple[dict, dict]:
    """Map lane_id -> the risks and events that land on it.

    A shipment knows its lane; a risk knows its shipment. Walking that link
    once here keeps the per-route build O(1) instead of rescanning every
    assessment per lane.
    """
    lane_of = {s.shipment_id: s.lane_id for s in context.shipments}

    risks: dict[str, list[ShipmentRisk]] = defaultdict(list)
    events: dict[str, dict] = defaultdict(dict)

    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            lane_id = lane_of.get(risk.shipment_id)
            if lane_id is None:
                continue
            risks[lane_id].append(risk)
            events[lane_id][assessment.event.event_id] = assessment

    return risks, events


def _exposure_cap(risks_by_route: dict) -> float:
    """Largest route exposure, used to normalise the within-level tie-break."""
    totals = [
        sum(r.do_nothing.expected_loss_chf for r in risks)
        for risks in risks_by_route.values()
    ]
    return max(totals) if totals else 0.0


def _build_route(
    lane: dict,
    risks_by_route: dict,
    events_by_route: dict,
    context: RunContext,
    exposure_cap: float,
) -> dict:
    lane_id = lane["id"]
    risks = risks_by_route.get(lane_id, [])
    assessments = list(events_by_route.get(lane_id, {}).values())

    verdict = classify(risks, context.config)
    shipments = [s for s in context.shipments if s.lane_id == lane_id]

    return {
        "route_id": lane_id,
        "name": lane["name"],
        "focus": lane.get("focus", ""),
        "level": verdict.level.value,
        "level_label": verdict.label,
        "directive": verdict.directive,
        "reason": verdict.reason,
        "severity_score": severity_score(verdict, exposure_cap),
        "lead_time_hours": verdict.lead_time_hours,
        "exposure_chf": round(verdict.exposure_chf, 2),
        # NOT DISPLAYED. Kept because the convene rule is built on it, but it
        # is the least defensible number in the system: it comes from our
        # invented action costs and residual fractions, so presenting it as a
        # CHF figure claims a precision we do not have.
        "_recoverable_chf_internal": round(verdict.recoverable_chf, 2),
        "shipments": len(shipments),
        "shipments_at_risk": len({r.shipment_id for r in risks}),
        "value_chf": round(sum(s.value_chf for s in shipments), 2),
        "contracts": sorted({r.customer for r in risks}),
        "legs": _legs(lane, context),
        "events": _events(assessments, risks),
        "radar": _radar(assessments, lane, context),
        "actions": _actions(risks, context),
        "response": _response(lane, verdict, risks, context),
    }


def _response(lane: dict, verdict, risks: list[ShipmentRisk],
              context: RunContext) -> dict:
    """Who to call and what it needs approving, for this route at this level.

    Everything is filtered by the route: the vendors are the ones at nodes this
    lane passes through, the carriers run the modes it uses, the alternatives
    are the ones declared on its own nodes. A planner looking at a Rhine barge
    problem should not be handed the Singapore agency.
    """
    costs = [
        r.best_action.cost_chf
        for r in risks
        if r.best_action is not None and r.value_of_acting_chf > 0
    ]
    return contacts_mod.build(
        lane=lane,
        level=verdict.level,
        exposure_chf=verdict.exposure_chf,
        best_cost_chf=max(costs) if costs else 0.0,
        config=context.config,
        network=context.network,
    )


def _legs(lane: dict, context: RunContext) -> list[dict]:
    """Route geometry for the globe, as real paths rather than idealised arcs.

    The Rhine legs follow the river and the deep-sea legs run through their
    chokepoints, because those are the places an event actually intersects the
    route. A great-circle arc from Basel to Rotterdam passes nowhere near Kaub,
    and Kaub is the entire point.
    """
    from engine.schemas import Mode

    out = []
    for leg in lane["legs"]:
        geometry = context.network.geometry(
            leg["from"], leg["to"], Mode(leg["mode"])
        )
        out.append(
            {
                "from": leg["from"],
                "to": leg["to"],
                "mode": leg["mode"],
                "distance_km": round(geometry.distance_km, 1),
                "routed_by": geometry.routed_by,
                # [lat, lon, altitude] — globe.gl path format.
                "path": [[p.lat, p.lon, PATH_ALTITUDE] for p in geometry.path],
            }
        )
    return out


def _events(assessments: list, risks: list[ShipmentRisk]) -> list[dict]:
    by_event = defaultdict(list)
    for risk in risks:
        by_event[risk.event_id].append(risk)

    out = []
    for assessment in assessments:
        event = assessment.event
        local = by_event.get(event.event_id, [])
        out.append(
            {
                "event_id": event.event_id,
                "title": event.title,
                "severity": event.severity.value,
                "event_class": event.event_class,
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat() if event.ends_at else None,
                "lat": event.lat,
                "lon": event.lon,
                "realized": event.realized,
                # None, never 0.5. An unsourceable probability is rendered as
                # unsourced, not discounted by a number nobody can defend.
                "probability": event.probability,
                "probability_basis": event.probability_basis,
                "source": event.provenance.source,
                "source_tier": event.provenance.source_tier,
                "quote": event.provenance.verbatim_quote,
                "inferred": event.provenance.inferred,
                "active_variables": list(event.active_variables),
                "shipments_here": len(local),
                "exposure_chf": round(
                    sum(r.do_nothing.expected_loss_chf for r in local), 2
                ),
            }
        )
    out.sort(key=lambda e: e["exposure_chf"], reverse=True)
    return out


def _eligible_families(lane: dict, context: RunContext) -> list[str]:
    """Families that COULD touch this route, via the exposure mask.

    These become the radar's axes even when they contribute nothing, and that
    is the honest reading: the mask genuinely evaluated whether each family can
    reach this route's nodes and modes. A family sitting at zero says "checked,
    contributes nothing" — which is information a planner wants, and is what
    turns a one-spoke stub into a chart you can actually read.

    Families that cannot reach the route at all are still excluded. Drawing
    port congestion on a road-only inland lane would imply an assessment that
    never happened.
    """
    from engine.schemas import Mode
    from engine.variables.mask import mode_applies, node_exposed

    nodes = {leg["from"] for leg in lane["legs"]} | {leg["to"] for leg in lane["legs"]}
    modes = {Mode(leg["mode"]) for leg in lane["legs"]}

    families: list[str] = []
    for var in context.config.variables.values():
        if var.family in families:
            continue
        if not any(mode_applies(var, m).exposed for m in modes):
            continue
        if not any(
            node_exposed(context.network.node(n), var).exposed
            for n in nodes
            if n in context.network.nodes
        ):
            continue
        families.append(var.family)
    return families


def _radar(assessments: list, lane: dict, context: RunContext) -> dict:
    """Risk families as spokes, split into the three severity bands.

    Spokes are labelled in DAYS OF DELAY, not abstract weights, so a planner
    reads "water level 4 days, port congestion 1 day" instead of
    "variable 47: 0.63".

    The axes are every family the exposure mask says COULD reach this route —
    active or not. A family at zero means "checked, contributes nothing", which
    is a true statement and a useful one. Families that cannot reach the route
    at all are excluded, because drawing them would imply an assessment that
    never happened.

    Each spoke carries its sub-categories: the individual variables inside that
    family, so the planner can see that "weather" means high wind specifically.
    """
    config = context.config
    variables = config.variables

    # family -> severity band -> days
    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"severe": 0.0, "moderate": 0.0, "minor": 0.0}
    )
    # family -> variable id -> {days, severity}
    subs: dict[str, dict[str, dict]] = defaultdict(dict)

    for assessment in assessments:
        band = assessment.event.severity.value
        for var_id, days in assessment.variable_contributions.items():
            var = variables.get(var_id)
            if var is None:
                continue
            totals[var.family][band] += days
            entry = subs[var.family].setdefault(
                var_id,
                {"id": var_id, "name": var.name, "days": 0.0, "severity": band,
                 "description": var.description.strip()},
            )
            entry["days"] += days
            entry["severity"] = band

    eligible = _eligible_families(lane, context)
    for family in eligible:
        totals[family]  # touch the defaultdict so quiet families get an axis

    # Loudest first, so the chart's shape reads before its labels do.
    families = sorted(
        totals, key=lambda f: (sum(totals[f].values()), f), reverse=True
    )

    return {
        "axes": [f.replace("_", " ").title() for f in families],
        "axis_keys": families,
        "series": {
            band: [round(totals[f][band], 2) for f in families]
            for band in ("severe", "moderate", "minor")
        },
        "max": round(
            max((sum(totals[f].values()) for f in families), default=0.0), 2
        ),
        "subcategories": {
            f: sorted(
                (
                    {**entry, "days": round(entry["days"], 2)}
                    for entry in subs[f].values()
                ),
                key=lambda e: e["days"],
                reverse=True,
            )
            for f in families
        },
    }


def _actions(risks: list[ShipmentRisk], context: RunContext) -> list[dict]:
    """The options worth taking, most valuable first.

    BRIEF §5.4: recommend the action when value of acting > 0. A negative value
    means the cheapest option costs more than it saves, so it is not offered —
    advising a planner to lose money is worse than saying nothing.
    """
    from engine.score.impact import explain
    from engine.score.leadtime import deadline_text

    out = []
    for risk in sorted(risks, key=lambda r: r.value_of_acting_chf, reverse=True):
        if risk.best_action is None or risk.value_of_acting_chf <= 0:
            continue
        action = risk.best_action
        out.append(
            {
                "shipment_id": risk.shipment_id,
                "customer": risk.customer,
                "label": action.label,
                "owner": action.owner,
                "sentence": explain(
                    risk.do_nothing.expected_loss_chf,
                    risk.act_outcome.expected_loss_chf if risk.act_outcome else 0.0,
                    action.cost_chf,
                    action.label,
                    deadline_text(context.clock, risk.decision_deadline),
                ),
                "value_chf": round(risk.value_of_acting_chf, 2),
                "cost_chf": round(action.cost_chf, 2),
                "lead_time_hours": risk.lead_time_hours,
                "actionability": risk.actionability,
                "min_hours": action.min_hours,
                "contacts": action.contacts,
                "p_late": round(risk.do_nothing.p_late, 4),
                "expected_delay_days": round(risk.do_nothing.expected_delay_days, 2),
                "expected_loss_chf": round(risk.do_nothing.expected_loss_chf, 2),
            }
        )
    return out[:12]


def _nodes(context: RunContext) -> list[dict]:
    """Only nodes that carry freight in this book.

    Plotting every port on earth is precisely the noise the product exists to
    remove — a planner does not need Callao on screen to decide about
    Rotterdam.
    """
    counts: dict[str, int] = defaultdict(int)
    for shipment in context.shipments:
        for node_id in shipment.node_ids:
            counts[node_id] += 1

    out = []
    for node_id, count in counts.items():
        node = context.network.node(node_id)
        out.append(
            {
                "id": node.id,
                "name": node.name,
                "kind": node.kind.value,
                "country": node.country,
                "lat": node.lat,
                "lon": node.lon,
                "chokepoint": node.chokepoint,
                "on_river": node.on_river,
                "shipments": count,
            }
        )
    out.sort(key=lambda n: n["shipments"], reverse=True)
    return out


def _posture(context: RunContext) -> dict:
    verdict = context.result.convene
    return {
        "posture": verdict.posture.value,
        "headline": verdict.headline,
        "rule_agreed": verdict.rule_agreed,
        "triggers_fired": verdict.triggers_fired,
        "exposure_chf": verdict.exposure_chf,
        "contracts_exposed": verdict.contracts_exposed,
        "options_expiring": verdict.options_expiring,
        "next_meeting_at": (
            verdict.next_meeting_at.isoformat() if verdict.next_meeting_at else None
        ),
    }
