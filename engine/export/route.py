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

from engine.export import progress as progress_mod
from engine.network.geo import Point, haversine_km
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
    from engine.ingest import photos as photos_mod  # noqa: PLC0415

    out = []
    for report in found:
        if report.shipment_id != shipment_id:
            continue
        blob = report.as_dict()
        # The log stores ids; the rotation lives beside the file. Decorating
        # here keeps the append-only record free of anything derived — a log
        # that carries computed fields is a log that can disagree with the
        # thing it describes.
        blob["photos"] = [
            {"id": pid, "orientation": photos_mod.orientation_for(pid)}
            for pid in blob.get("photos", [])
        ]
        out.append(blob)
    return out


def leg_status(hit: bool, lead_time_hours: float | None, reports: list[dict]) -> str:
    """The three colours: one rule, for the route page and a shipment's card."""
    damaged = any(r.get("load_state") == "damaged" for r in reports)
    stopped = any(r.get("status") in ("held", "stopped") for r in reports)
    if damaged or (hit and lead_time_hours is not None and lead_time_hours <= 0):
        return "affected"
    if hit or stopped:
        return "at_risk"
    return "ok"


def _soonest_risks(context: RunContext) -> dict[str, dict]:
    """Per shipment, the SOONEST deadline across every event touching it:
    that is the one that decides when somebody has to move."""
    out: dict[str, dict] = {}
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            current = out.get(risk.shipment_id)
            lead = risk.lead_time_hours
            if lead is None:
                continue
            if current is None or lead < current["lead_time_hours"]:
                out[risk.shipment_id] = {
                    "lead_time_hours": lead,
                    "actionability": getattr(risk, "actionability", None),
                    "expected_loss_chf": getattr(risk, "expected_loss_chf", 0.0),
                    "driving_event": assessment.event.title,
                    "driving_event_id": assessment.event.event_id,
                }
    return out


def _leg_km(context: RunContext, leg) -> float:
    a_node = context.config.nodes.get(leg.from_node)
    b_node = context.config.nodes.get(leg.to_node)
    if a_node is None or b_node is None:
        return 0.0
    return round(haversine_km(Point(a_node.lat, a_node.lon), Point(b_node.lat, b_node.lon)), 1)


def shipment_journey(context: RunContext, shipment_id: str) -> dict | None:
    """One shipment's row of the route page: its legs, each in the colour the
    route page gives it, and how far along it is."""
    shipment = next((s for s in context.shipments if s.shipment_id == shipment_id), None)
    if shipment is None:
        return None
    hit_legs = {h.leg_index for h in context.hits if h.shipment_id == shipment_id}
    risk = _soonest_risks(context).get(shipment_id) or {}
    reports = _reports_for(shipment_id, context)
    moved = progress_mod.progress(shipment, context.config.nodes, context.clock.as_of)
    return {
        "legs": [{
            "index": index,
            "from": _node_name(context, own.from_node),
            "to": _node_name(context, own.to_node),
            "mode": own.mode.value,
            "km": _leg_km(context, own),
            "carrier": own.carrier,
            "arrives": own.planned_arrive.isoformat(),
            "status": leg_status(index in hit_legs, risk.get("lead_time_hours"), reports),
        } for index, own in enumerate(shipment.legs)],
        "progress": {k: moved.get(k) for k in ("percent", "travelled_km", "remaining_km", "total_km")},
        "driving_event": risk.get("driving_event"),
        "lead_time_hours": risk.get("lead_time_hours"),
        "reports": len(reports),
    }


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
    risk_by_shipment = _soonest_risks(context)

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
            status = leg_status(hit, (risk or {}).get("lead_time_hours"), reports)

            moved = progress_mod.progress(shipment, context.config.nodes,
                                          context.clock.as_of)
            seen = progress_mod.observed(reports)
            vehicles.append({
                "progress": moved,
                "observed_position": seen,
                # How far the freight is from where the plan puts it. None,
                # not zero, when nothing has been observed — a zero would
                # read as "exactly on plan", which is the opposite of "we
                # have no idea where this is".
                "drift_km": progress_mod.drift_km(moved["planned_position"], seen),
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
            "km": _leg_km(context, leg),
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
        # Both cuts, because the page draws both. Carried through rather than
        # recomputed: the board already did the split, and a second copy of
        # that arithmetic is a second thing to keep in step.
        "radar_measured": route.get("radar_measured"),
        "radar_reported": route.get("radar_reported"),
        "matrix_grid": board.get("matrix_grid"),
        "events": events,
        "driving_event_id": (driving or {}).get("event_id"),
        # Carried through from the board: what on this route is real, and the
        # recorded conditions at each of its places.
        "real_data": route.get("real_data") or {"focus": False},
        # Sika's week-ahead sign: the current burst of small orders on this
        # flow, and the earlier ones with what followed them.
        "early_warning": route.get("early_warning"),
        "burst_history": route.get("burst_history") or [],
        "conditions": route.get("conditions") or [],
        "legs": legs,
        "totals": {
            "affected": sum(leg["counts"]["affected"] for leg in legs),
            "at_risk": sum(leg["counts"]["at_risk"] for leg in legs),
            "ok": sum(leg["counts"]["ok"] for leg in legs),
        },
    }


# =====================================================================
# One shipment, on its own page
# =====================================================================
def shipment_view(board: dict, context: RunContext, shipment_id: str) -> dict | None:
    """The route page for ONE shipment: the vehicle on each of its legs, its
    field reports, where it is, and its risk: one matrix point per event
    that touches it, and the radars cut to those events. Every figure is the
    board's; nothing is recomputed."""
    from engine.export import board as board_mod  # noqa: PLC0415
    from engine.fleet import assets as assets_mod  # noqa: PLC0415
    from engine.fleet import manifest  # noqa: PLC0415

    shipment = next((s for s in context.shipments if s.shipment_id == shipment_id), None)
    if shipment is None:
        return None
    route = next((r for r in board["routes"] if r["route_id"] == shipment.lane_id), None)
    lane = next((ln for ln in context.config.lanes if ln["id"] == shipment.lane_id), None)
    if route is None or lane is None:
        return None
    as_of = context.clock.as_of
    journey = shipment_journey(context, shipment_id) or {"legs": []}
    reports = _reports_for(shipment_id, context)
    moved = progress_mod.progress(shipment, context.config.nodes, as_of)
    seen = progress_mod.observed(reports)
    where = assets_mod.locate(context, shipment)
    now = where["leg_index"] if where else None

    # The journey as stretches, one per run of a mode (the road to Basel, the
    # Rhine to Rotterdam, the sea to Shanghai), each with every vehicle on it.
    worse = {"ok": 0, "at_risk": 1, "affected": 2}
    touching = [a for a in context.result.assessments
                if any(r.shipment_id == shipment_id for r in a.shipment_risks)]
    derate = min((a.event.payload_fraction for a in touching
                  if getattr(a.event, "payload_fraction", None) is not None), default=None)
    boxes = manifest.containers(shipment)
    stretches: list[dict] = []
    for leg in journey["legs"]:
        own = shipment.legs[leg["index"]]
        if stretches and stretches[-1]["mode"] == leg["mode"]:
            s = stretches[-1]
            s["legs"].append(leg)
            s["to"], s["arrives"], s["last"] = leg["to"], leg["arrives"], leg["index"]
            s["km"] = round(s["km"] + leg["km"], 1)
            if worse[leg["status"]] > worse[s["status"]]:
                s["status"] = leg["status"]
            continue
        stretches.append({"mode": leg["mode"], "from": leg["from"], "to": leg["to"], "km": leg["km"],
                          "departs": own.planned_depart.isoformat(), "arrives": leg["arrives"],
                          "status": leg["status"], "legs": [leg], "first": leg["index"],
                          "last": leg["index"]})

    phase_now = where["phase"] if where else None
    for s in stretches:
        # Where the shipment is against this stretch: past it, on it, or not
        # there yet, in which case its vehicles wait (grey on the page).
        if where is None or s["last"] < now:
            s["state"], s["state_word"] = "done", "done"
        elif s["first"] > now:
            s["state"], s["state_word"] = "waiting", f"waits at {_short_place(s['from'])}"
        elif phase_now == "in_transit":
            s["state"], s["state_word"] = "moving", "en route"
        elif phase_now in ("staging", "booked"):
            s["state"] = "loading"
            s["state_word"] = (f"loading at {_short_place(s['from'])}"
                               if phase_now == "staging" else "booked")
        else:
            here = journey["legs"][now]["from"]
            s["state"], s["state_word"] = "at", f"at {_short_place(here)}"
        load = manifest.load(shipment, s["first"], boxes,
                             payload_fraction=derate if s["mode"] == "barge" else None)
        units = []
        for unit in manifest.convoy(shipment, s["first"], boxes):
            carried = [b for b in boxes if b["container_id"] in unit["containers"]]
            units.append({
                "asset_id": unit["asset_id"], "name": unit["name"], "mode": unit["mode"],
                "carrier": unit["carrier"], "crew": dict(unit["crew"]),
                "capacity_teu": unit["capacity_teu"],
                # A truck carries only ours; a barge or a ship others' freight too.
                "loaded_teu": unit["teu"] if unit["mode"] == "road" else load["loaded_teu"],
                "usable_teu": None if unit["mode"] == "road" else load["usable_teu"],
                "ours_teu": unit["teu"],
                "boxes": [{k: b[k] for k in ("container_id", "size_ft", "priority", "deadline",
                                             "gross_t", "content")} for b in carried],
                "reports": [], "position": None,
            })
        s["units"] = units

    # Each report on the vehicle it names, or else the one carrying the
    # freight when it was seen.
    for r in reports:
        unit = next((u for s in stretches for u in s["units"]
                     if u["asset_id"] == r.get("vehicle_id")), None)
        if unit is None:
            seen_at = r.get("observed_at") or ""
            stretch = (next((s for s in stretches if s["departs"] <= seen_at <= s["arrives"]), None)
                       or next((s for s in stretches if s["state"] not in ("done", "waiting")), None)
                       or (stretches[0] if stretches else None))
            unit = stretch["units"][0] if stretch else None
            r = r | {"placed_by_time": True}
        if unit is not None:
            unit["reports"].append(r)
    for s in stretches:
        for u in s["units"]:
            u["reports"].sort(key=lambda r: r.get("observed_at") or "", reverse=True)
            verified = next((r for r in u["reports"]
                             if r.get("authenticated") and r.get("reported_by")), None)
            u["crew"]["verified"] = bool(verified)
            if verified:
                u["crew"]["name"] = verified["reported_by"]
            if s["state"] not in ("done", "waiting"):
                mine = progress_mod.observed(u["reports"])
                u["position"] = {"planned": moved.get("planned_position"), "seen": mine,
                                 "drift_km": progress_mod.drift_km(moved.get("planned_position"), mine)}

    # This shipment in each event touching it: the event's matrix point for
    # it, as the route page draws them, one dot per event here.
    events = []
    for e in route.get("events") or []:
        point = next((p for p in (e.get("matrix") or {}).get("points") or []
                      if p["shipment_id"] == shipment_id), None)
        if point is None:
            continue
        events.append({k: e.get(k) for k in (
            "event_id", "title", "kind", "kind_label", "delay_days", "capped_at",
            "break_even_probability", "break_even_words", "severity", "starts_at")}
            | {"point": point})
    worst = max(events, key=lambda e: e["point"]["expected_loss_chf"], default=None)
    risk = _soonest_risks(context).get(shipment_id) or {}

    return {
        "shipment_id": shipment_id,
        "route_id": route["route_id"],
        "route_name": route["name"],
        "level": route["level"],
        "level_label": route.get("level_label", route["level"]),
        "customer": shipment.customer,
        "tier": _tier(shipment.customer, context),
        "value_chf": shipment.value_chf,
        "cargo": {"type": shipment.product_family, "dangerous_goods": shipment.dangerous_goods,
                  "temperature_controlled": shipment.temperature_controlled},
        "committed": shipment.otif_committed_date.isoformat(),
        "eta": shipment.eta.isoformat() if shipment.eta else None,
        "stats": {
            "lead_time_hours": risk.get("lead_time_hours"),
            "loss_chf": worst["point"]["expected_loss_chf"] if worst else 0.0,
            "p_late": worst["point"]["p_late"] if worst else None,
            "survive_days": worst["point"]["time_to_survive_days"] if worst else None,
            "events": len(events),
            "reports": len(reports),
        },
        "stretches": stretches,
        "vehicles": sum(len(s["units"]) for s in stretches),
        "progress": {k: moved.get(k) for k in ("percent", "travelled_km", "remaining_km", "total_km")},
        "position": {
            "planned": moved.get("planned_position"),
            "seen": seen,
            "drift_km": progress_mod.drift_km(moved.get("planned_position"), seen),
        },
        "reports": reports,
        "events": events,
        "radar_measured": board_mod._radar(touching, lane, context,
                                           keep=lambda v: v.probability_sourceable),
        "radar_reported": board_mod._radar(touching, lane, context,
                                           keep=lambda v: not v.probability_sourceable),
        "matrix_grid": board.get("matrix_grid"),
    }


def _short_place(name: str) -> str:
    """'Kaub (Rhine gauge, governing shallow point)' reads as 'Kaub'."""
    return name.split(" (")[0]


def _tier(customer: str, context: RunContext) -> str:
    from engine.desk import priority_of  # noqa: PLC0415
    return priority_of(customer, context.config)
