"""Every asset on the map, its live status, and the Action Hub behind a click.

THE THREE COLOURS, AND WHY THEY ARE NOT THE LADDER
==================================================
The ladder (engine/score/severity.py) answers *how soon must somebody decide*.
The map's three colours answer *how disrupted is this asset*:

  green   nominal — on schedule
  yellow  minor disruption — less than four hours late
  red     major disruption or stoppage

The input is the Monte Carlo's do-nothing expected delay from the worst event
touching the consignment — the float the board already has, not a new
estimate. A field report of a stoppage or damage makes it red regardless: a
driver looking at a closed lock outranks a forecast that it might close.

STRICTLY A PROJECTION
---------------------
Status, delay, P(late) and the bill if late come off the run the board was
built from. The position is the schedule's (labelled so) unless somebody on
site sent a fix, and both are kept — see engine/export/progress.py for why
the gap between them is the useful number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.export import progress as progress_mod
from engine.fleet import manifest
from engine.fleet.paths import as_latlon, bearing_deg, length_km, split_at
from engine.fleet.settings import settings as fleet_settings
from engine.network.geo import Point
from engine.pipeline import RunContext
from engine.schemas import Shipment, ShipmentRisk
from engine.score.matrix import UNSOURCED_BAND_ID

STATUS_LABEL = {
    "green": "Nominal",
    "yellow": "Minor disruption",
    "red": "Major disruption",
}
PHASE_LABEL = {
    "in_transit": "in transit",
    "at_node": "waiting at a node",
    "staging": "loading at origin",
    "booked": "booked, not yet departed",
}


# =====================================================================
# One pass over the run, shared by every view
# =====================================================================


@dataclass
class _Index:
    risks: dict[str, list[ShipmentRisk]] = field(default_factory=dict)
    hits: dict[str, list] = field(default_factory=dict)
    events: dict[str, object] = field(default_factory=dict)
    contributions: dict[str, dict[str, float]] = field(default_factory=dict)
    shipments: dict[str, Shipment] = field(default_factory=dict)


def index(context: RunContext) -> _Index:
    """Build once per run and keep it on the context.

    The context is already cached per (as_of, shipments) by the API, so the
    index lives exactly as long as the numbers it was built from.
    """
    cached = getattr(context, "_fleet_index", None)
    if cached is not None:
        return cached

    idx = _Index()
    idx.shipments = {s.shipment_id: s for s in context.shipments}
    for assessment in context.result.assessments:
        idx.events[assessment.event.event_id] = assessment.event
        idx.contributions[assessment.event.event_id] = dict(assessment.variable_contributions)
        for risk in assessment.shipment_risks:
            idx.risks.setdefault(risk.shipment_id, []).append(risk)
    for event in context.events:
        idx.events.setdefault(event.event_id, event)
    for hit in context.hits:
        idx.hits.setdefault(hit.shipment_id, []).append(hit)

    context._fleet_index = idx  # type: ignore[attr-defined]
    return idx


def field_reports(context: RunContext) -> dict[str, list[dict]]:
    """Field reports observed at or before the as-of, by shipment.

    Read from the log on every call, NOT cached with the run: the run is
    cached per as-of, and a driver who taps "stopped" must turn the dot red
    on the next load, not on the next restart. The as-of filter keeps a
    pinned board reproducible all the same.
    """
    out: dict[str, list[dict]] = {}
    try:
        from engine.ingest import reports as reports_mod  # noqa: PLC0415

        for report in reports_mod.as_of(context.clock.as_of):
            out.setdefault(report.shipment_id, []).append(report.as_dict())
    except OSError:
        pass
    return out


# =====================================================================
# Where it is
# =====================================================================


def locate(context: RunContext, shipment: Shipment) -> dict | None:
    """Phase, current leg, and the planned position ON the leg's geometry.

    None for freight already delivered — it is not an asset any more.
    """
    as_of = context.clock.as_of
    legs = shipment.legs
    if not legs or as_of >= legs[-1].planned_arrive:
        return None

    if as_of < legs[0].planned_depart:
        window = fleet_settings(context.config)["visibility"]["staging_window_hours"]
        phase = (
            "staging"
            if legs[0].planned_depart - as_of <= timedelta(hours=float(window))
            else "booked"
        )
        leg_index, fraction = 0, 0.0
    else:
        leg_index, fraction, phase = len(legs) - 1, 0.0, "at_node"
        for i, leg in enumerate(legs):
            if leg.planned_depart <= as_of < leg.planned_arrive:
                leg_index, phase = i, "in_transit"
                fraction = progress_mod.leg_fraction(leg, as_of)
                break
            if as_of < leg.planned_depart:
                leg_index, fraction, phase = i, 0.0, "at_node"
                break

    leg = legs[leg_index]
    geometry = context.network.geometry(leg.from_node, leg.to_node, leg.mode)
    point, travelled, remaining = split_at(geometry.path, fraction)
    ahead = remaining[1] if len(remaining) > 1 else geometry.path[-1]
    behind = travelled[-2] if len(travelled) > 1 else geometry.path[0]
    heading = bearing_deg(point, ahead) if ahead != point else bearing_deg(behind, point)

    return {
        "phase": phase,
        "leg_index": leg_index,
        "fraction": round(fraction, 4),
        "point": point,
        "leg_travelled_path": travelled,
        "leg_remaining_path": remaining,
        "leg_remaining_km": round(length_km(remaining), 1),
        "heading_deg": round(heading, 1),
    }


# =====================================================================
# How disrupted it is
# =====================================================================


def _worst(risks: list[ShipmentRisk]) -> ShipmentRisk | None:
    return max(risks, key=lambda r: r.do_nothing.expected_delay_days, default=None)


def status_of(context: RunContext, shipment: Shipment,
              reports: list[dict] | None = None) -> dict:
    """Green / yellow / red, the reason, and the delay behind it."""
    cfg = fleet_settings(context.config)["status"]
    idx = index(context)
    risks = idx.risks.get(shipment.shipment_id, [])
    worst = _worst(risks)
    delay_h = worst.do_nothing.expected_delay_days * 24.0 if worst else 0.0

    if reports is None:
        reports = field_reports(context).get(shipment.shipment_id, [])
    latest = max(reports, key=lambda r: r["observed_at"], default=None)
    stopped = latest is not None and latest.get("status") in cfg["stoppage_report_statuses"]
    damaged = any(r.get("load_state") in cfg["damage_load_states"] for r in reports)

    event = idx.events.get(worst.event_id) if worst else None
    title = getattr(event, "title", None)

    if damaged:
        level = "red"
        reason = "Damage reported from site — a field report outranks every forecast."
    elif stopped:
        level = "red"
        reason = (f"Reported {latest['status']} from site at "
                  f"{latest['observed_at'][:16].replace('T', ' ')} UTC.")
    elif delay_h >= float(cfg["minor_max_hours"]):
        level = "red"
        reason = f"{title}: expected +{_hours_text(delay_h)} at destination."
    elif delay_h > float(cfg["nominal_max_hours"]):
        level = "yellow"
        reason = f"{title}: expected +{_hours_text(delay_h)} at destination."
    else:
        level = "green"
        reason = (
            f"On schedule. {title} touches it, but the expected delay "
            f"({_hours_text(delay_h)}) is inside schedule noise."
            if worst else "On schedule. No event touches its remaining legs."
        )

    return {
        "level": level,
        "label": STATUS_LABEL[level],
        "colour": cfg["colours"][level],
        "reason": reason,
        "delay_hours": round(delay_h, 2),
        "p_late": round(worst.do_nothing.p_late, 4) if worst else 0.0,
        "driving_event_id": worst.event_id if worst else None,
        "driving_event": title,
        "field_stoppage": bool(stopped),
        "field_damage": bool(damaged),
    }


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _hours_text(h: float) -> str:
    if h < 1:
        return f"{round(h * 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


# =====================================================================
# The list the map draws
# =====================================================================


def fleet_assets(board: dict, context: RunContext) -> dict:
    """Every asset not yet delivered, with enough to draw and colour it."""
    cfg = fleet_settings(context.config)
    route_level = {r["route_id"]: r["level"] for r in board["routes"]}
    lanes = {lane["id"]: lane for lane in context.config.lanes}
    nodes = context.config.nodes

    reports = field_reports(context)
    assets = []
    for shipment in context.shipments:
        where = locate(context, shipment)
        if where is None:
            continue
        own = reports.get(shipment.shipment_id, [])
        status = status_of(context, shipment, own)
        leg = shipment.legs[where["leg_index"]]
        veh = manifest.vehicle(shipment, where["leg_index"])
        observed = progress_mod.observed(own)
        point = where["point"]
        position = (
            {"lat": observed["lat"], "lon": observed["lon"], "source": "field report"}
            if observed else
            {"lat": round(point.lat, 5), "lon": round(point.lon, 5), "source": "schedule"}
        )
        eta = shipment.eta
        assets.append({
            "id": shipment.shipment_id,
            "asset_id": veh["asset_id"],
            "name": veh["name"],
            "mode": leg.mode.value,
            "phase": where["phase"],
            "status": status["level"],
            "status_label": status["label"],
            "colour": status["colour"],
            "reason": status["reason"],
            "delay_hours": status["delay_hours"],
            "lat": position["lat"],
            "lon": position["lon"],
            "position_source": position["source"],
            "heading_deg": where["heading_deg"],
            "lane_id": shipment.lane_id,
            "lane_name": lanes.get(shipment.lane_id, {}).get("name", shipment.lane_id),
            "route_level": route_level.get(shipment.lane_id),
            "leg": f"{nodes[leg.from_node].name} → {nodes[leg.to_node].name}",
            "destination": nodes[shipment.destination_node].name,
            "customer": shipment.customer,
            "eta": eta.isoformat(),
            "eta_revised": (eta + timedelta(hours=status["delay_hours"])).isoformat(),
        })

    order = {"red": 0, "yellow": 1, "green": 2}
    assets.sort(key=lambda a: (order[a["status"]], -a["delay_hours"], a["id"]))
    counts = {k: sum(1 for a in assets if a["status"] == k) for k in ("green", "yellow", "red")}

    from engine.ingest.sources.fetch import network_allowed  # noqa: PLC0415

    return {
        "as_of": board["as_of"],
        "as_of_label": board["as_of_label"],
        "synthetic": all(s.synthetic for s in context.shipments),
        "counts": counts,
        "assets": assets,
        "lanes": [
            {
                "route_id": r["route_id"],
                "name": r["name"],
                "level": r["level"],
                "path": [[p[0], p[1]] for leg in r["legs"] for p in leg["path"]],
            }
            for r in board["routes"]
        ],
        "meta": {
            "status_colours": cfg["status"]["colours"],
            "status_labels": STATUS_LABEL,
            "thresholds": {
                "nominal_max_hours": cfg["status"]["nominal_max_hours"],
                "minor_max_hours": cfg["status"]["minor_max_hours"],
            },
            "staging_window_hours": cfg["visibility"]["staging_window_hours"],
            "weights": cfg["ranking"]["weights"],
            "badges": cfg["ranking"]["badges"],
            "basemap": cfg["basemap"],
            "network_allowed": network_allowed(),
            "phases": PHASE_LABEL,
        },
    }


# =====================================================================
# The Action Hub
# =====================================================================


def asset_detail(board: dict, context: RunContext, shipment_id: str) -> dict | None:
    """Everything the pop-up card shows for one asset."""
    idx = index(context)
    shipment = idx.shipments.get(shipment_id)
    if shipment is None:
        return None
    where = locate(context, shipment)
    if where is None:
        return None

    cfg = fleet_settings(context.config)
    nodes = context.config.nodes
    as_of = context.clock.as_of
    reports = sorted(field_reports(context).get(shipment_id, []),
                     key=lambda r: r["observed_at"])
    status = status_of(context, shipment, reports)
    leg_index = where["leg_index"]
    leg = shipment.legs[leg_index]
    veh = manifest.vehicle(shipment, leg_index)

    # Who is driving: a verified reporter is a real name; otherwise the
    # synthetic roster, and the card says which.
    verified = next((r for r in reversed(reports)
                     if r.get("authenticated") and r.get("reported_by")), None)
    crew = (
        {"name": verified["reported_by"], "role": veh["crew"]["role"], "verified": True}
        if verified else {**veh["crew"], "verified": False, "synthetic": True}
    )

    point = where["point"]
    observed = progress_mod.observed(reports)
    planned = {"lat": round(point.lat, 5), "lon": round(point.lon, 5)}
    if observed:
        position = {"lat": observed["lat"], "lon": observed["lon"], "source": "field report",
                    "accuracy_m": observed.get("accuracy_m")}
        last_sync, sync_source = observed["observed_at"], "field report"
    else:
        position = {**planned, "source": "schedule", "accuracy_m": None}
        last_sync, sync_source = as_of.isoformat(), "schedule at the board's as-of"

    boxes = manifest.containers(shipment)
    remaining_risks = _remaining(context, shipment, leg_index)
    derate = next(
        (idx.events[r.event_id].payload_fraction for r in remaining_risks
         if getattr(idx.events.get(r.event_id), "payload_fraction", None) is not None),
        None,
    )
    load = manifest.load(shipment, leg_index, boxes, payload_fraction=derate)

    eta = shipment.eta
    revised = eta + timedelta(hours=status["delay_hours"])
    committed = shipment.otif_committed_date

    return {
        "shipment_id": shipment_id,
        "synthetic": shipment.synthetic,
        "asset": {**veh, "crew": crew},
        "status": status,
        "phase": where["phase"],
        "phase_label": PHASE_LABEL[where["phase"]],
        "position": position,
        "planned_position": planned,
        "drift_km": progress_mod.drift_km(planned, observed),
        "heading_deg": where["heading_deg"],
        "last_sync": last_sync,
        "last_sync_source": sync_source,
        "leg": {
            "index": leg_index,
            "count": len(shipment.legs),
            "mode": leg.mode.value,
            "from": leg.from_node,
            "to": leg.to_node,
            "from_name": nodes[leg.from_node].name,
            "to_name": nodes[leg.to_node].name,
            "fraction": where["fraction"],
            "remaining_km": where["leg_remaining_km"],
        },
        "load": load,
        "cargo": {
            "type": manifest.PRODUCT_LABEL.get(shipment.product_family, shipment.product_family),
            "product_family": shipment.product_family,
            "dangerous_goods": shipment.dangerous_goods,
            "temperature_controlled": shipment.temperature_controlled,
            "value_chf": shipment.value_chf,
        },
        "containers": [
            {**b, "on_time_original": datetime.fromisoformat(b["deadline"]) >= revised}
            for b in boxes
        ],
        "logistics": {
            "lane_id": shipment.lane_id,
            "lane_name": next((lane["name"] for lane in context.config.lanes
                               if lane["id"] == shipment.lane_id), shipment.lane_id),
            "origin": nodes[shipment.origin_node].name,
            "destination": nodes[shipment.destination_node].name,
            "destination_id": shipment.destination_node,
            "customer": shipment.customer,
            "eta_original": eta.isoformat(),
            "eta_revised": revised.isoformat(),
            "delay_hours": status["delay_hours"],
            "committed": committed.isoformat(),
            "misses_commitment": revised > committed,
        },
        "logs": _logs(context, shipment, where, status, reports),
        "matrix": _matrix(context, shipment, remaining_risks, reports, cfg["matrix"]),
        "radar": _radar(context, remaining_risks, reports, cfg["radar"]),
    }


def _remaining(context: RunContext, shipment: Shipment, leg_index: int) -> list[ShipmentRisk]:
    """Risks whose hit is on a leg this asset has not finished yet."""
    idx = index(context)
    ahead = {
        h.event_id for h in idx.hits.get(shipment.shipment_id, [])
        if h.leg_index >= leg_index
    }
    return [r for r in idx.risks.get(shipment.shipment_id, []) if r.event_id in ahead]


def _logs(context: RunContext, shipment: Shipment, where: dict, status: dict,
          reports: list[dict]) -> list[dict]:
    """Why it is the colour it is, newest first.

    Three kinds, never blended: what a FEED said about the region, what
    somebody ON SITE said about this consignment, and what the SCHEDULE says
    — labelled as the schedule, because no GPS or AIS feed is connected and a
    planned position presented as telemetry would be a lie with a timestamp.
    """
    idx = index(context)
    nodes = context.config.nodes
    as_of = context.clock.as_of
    minor_max = float(fleet_settings(context.config)["status"]["minor_max_hours"])
    out: list[dict] = []

    hits_by_event: dict[str, list] = {}
    for hit in idx.hits.get(shipment.shipment_id, []):
        hits_by_event.setdefault(hit.event_id, []).append(hit)

    for risk in idx.risks.get(shipment.shipment_id, []):
        event = idx.events.get(risk.event_id)
        if event is None:
            continue
        legs = sorted({h.leg_index for h in hits_by_event.get(risk.event_id, [])})
        where_txt = "; ".join(
            f"leg {i + 1} {nodes[shipment.legs[i].from_node].name} → "
            f"{nodes[shipment.legs[i].to_node].name} ({shipment.legs[i].mode.value})"
            for i in legs
        )
        behind = legs and max(legs) < where["leg_index"]
        tier = event.provenance.source_tier
        effect = (
            f"Expected +{_hours_text(risk.do_nothing.expected_delay_days * 24)} at "
            f"destination; P(late) {risk.do_nothing.p_late:.0%}."
        )
        if event.payload_fraction is not None:
            effect += f" Loading restricted to {event.payload_fraction:.0%} of capacity."
        seen = event.provenance.retrieved_at
        out.append({
            "at": min(seen, as_of).isoformat() if seen else event.starts_at.isoformat(),
            "kind": "feed",
            "level": "red" if risk.do_nothing.expected_delay_days * 24 >= minor_max else "yellow",
            "title": event.title,
            "detail": f"Touches {where_txt}. {effect}"
                      + (" This leg is already behind the asset." if behind else ""),
            "source": f"{event.provenance.source} · tier {tier}"
                      + (" · uncorroborated" if tier >= 3 else ""),
        })

    for report in reports:
        note = f" “{report['note']}”" if report.get("note") else ""
        out.append({
            "at": report["observed_at"],
            "kind": "field report",
            "level": "red" if report.get("status") in ("held", "stopped")
                     or report.get("load_state") == "damaged" else "info",
            "title": f"{report.get('role_label') or report.get('role') or 'On site'}: "
                     f"{report.get('status')}, load {report.get('load_state')}",
            "detail": (report.get("position") or "") + note,
            "source": ("verified driver credential" if report.get("authenticated")
                       else "unverified field report"),
        })

    leg = shipment.legs[where["leg_index"]]
    phase = where["phase"]
    if phase == "in_transit":
        detail = (f"Leg {where['leg_index'] + 1} of {len(shipment.legs)}: "
                  f"{nodes[leg.from_node].name} → {nodes[leg.to_node].name} by {leg.mode.value}, "
                  f"{where['fraction']:.0%} through; {where['leg_remaining_km']:.0f} km to "
                  f"{nodes[leg.to_node].name}.")
    elif phase == "at_node":
        detail = (f"Between legs at {nodes[leg.from_node].name}; next departs "
                  f"{leg.planned_depart.strftime('%a %d %b %H:%M')} UTC by {leg.mode.value}.")
    else:
        detail = (f"At {nodes[leg.from_node].name}; departs "
                  f"{leg.planned_depart.strftime('%a %d %b %H:%M')} UTC by {leg.mode.value}.")
    out.append({
        "at": as_of.isoformat(),
        "kind": "schedule",
        "level": status["level"],
        "title": f"{status['label']} — {PHASE_LABEL[phase]}",
        "detail": detail + " No GPS/AIS feed is connected: this position is where the plan "
                           "puts it, not a fix.",
        "source": "schedule, at the board's as-of",
    })

    out.sort(key=lambda e: e["at"], reverse=True)
    return out


def _band(value: float, bands: list[dict], key: str) -> str:
    for band in bands:
        if value < band[key]:
            return band["id"]
    return bands[-1]["id"]


def _impact_band(chf: float, bands: list[dict]) -> str:
    for band in bands:                       # most severe first
        if chf >= band["min_chf"]:
            return band["id"]
    return bands[-1]["id"]


def _matrix(context: RunContext, shipment: Shipment, risks: list[ShipmentRisk],
            reports: list[dict], cfg: dict) -> dict:
    """5 x 5: P(this asset is late) against the bill IF it is late.

    The board's own two axes, banded finer, over the hazards on the legs this
    asset still has to travel. An event whose probability cannot be sourced
    goes in the gutter — never at a computed middle band, which is the bug
    this whole codebase is built to avoid.
    """
    idx = index(context)
    points = []
    for risk in risks:
        event = idx.events.get(risk.event_id)
        unsourced = risk.probability_band == UNSOURCED_BAND_ID
        points.append({
            "id": risk.event_id,
            "label": getattr(event, "title", risk.event_id),
            "p_late": None if unsourced else round(risk.do_nothing.p_late, 4),
            "conditional_loss_chf": round(risk.do_nothing.conditional_loss_chf, 2),
            "probability_band": None if unsourced
            else _band(risk.do_nothing.p_late, cfg["probability_bands"], "max_p"),
            "impact_band": _impact_band(risk.do_nothing.conditional_loss_chf, cfg["impact_bands"]),
            "source": "monte carlo",
        })
    if any(r.get("load_state") == "damaged" for r in reports):
        points.append({
            "id": "FIELD-DAMAGE",
            "label": "Load damaged (field report)",
            "p_late": 1.0,
            "conditional_loss_chf": shipment.value_chf,
            "probability_band": cfg["probability_bands"][-1]["id"],
            "impact_band": _impact_band(shipment.value_chf, cfg["impact_bands"]),
            "source": "observed",
        })
    return {
        "probability_bands": cfg["probability_bands"],
        "impact_bands": cfg["impact_bands"],
        "points": points,
        "unsourced": sum(1 for p in points if p["probability_band"] is None),
    }


def _radar(context: RunContext, risks: list[ShipmentRisk], reports: list[dict],
           cfg: dict) -> dict:
    """Five axes, 0-100, each backed by hours of expected delay.

    Each event's expected delay for THIS asset is shared across its active
    variables in proportion to what the ledger says each contributes, then
    summed by axis. The index is a saturating transform of hours so a
    three-week closure does not flatten every other spoke to zero — and the
    hours ride along, because an index nobody can convert back to time is an
    index nobody can argue with.
    """
    idx = index(context)
    variables = context.config.variables
    family_axis = {fam: axis["key"] for axis in cfg["axes"] for fam in axis["families"]}
    hours = {axis["key"]: 0.0 for axis in cfg["axes"]}
    drivers: dict[str, list[str]] = {axis["key"]: [] for axis in cfg["axes"]}

    for risk in risks:
        event = idx.events.get(risk.event_id)
        contrib = idx.contributions.get(risk.event_id) or {}
        total = sum(contrib.values())
        delay_h = risk.do_nothing.expected_delay_days * 24.0
        if not contrib or total <= 0:
            contrib = {v: 1.0 for v in getattr(event, "active_variables", [])}
            total = float(len(contrib)) or 1.0
        for var_id, share in contrib.items():
            var = variables.get(var_id)
            axis = family_axis.get(var.family) if var else None
            if axis is None:
                continue
            hours[axis] += delay_h * share / total
            name = f"{var.name} — {_short(getattr(event, 'title', risk.event_id), 56)}"
            if name not in drivers[axis]:
                drivers[axis].append(name)

    scale = float(cfg["scale_hours"]) or 24.0
    values = {k: 100.0 * (1.0 - math.exp(-h / scale)) for k, h in hours.items()}

    # Mechanical status has one source no feed can give: somebody looking at
    # the vehicle. A stoppage or damage sets a floor on that spoke.
    if "mechanical" in values:
        if any(r.get("load_state") == "damaged" for r in reports):
            values["mechanical"] = max(values["mechanical"], 90.0)
            drivers["mechanical"].append("Damage reported from site")
        elif any(r.get("status") in ("held", "stopped") for r in reports):
            values["mechanical"] = max(values["mechanical"], 60.0)
            drivers["mechanical"].append("Stoppage reported from site")

    keys = [axis["key"] for axis in cfg["axes"]]
    return {
        "axes": [axis["label"] for axis in cfg["axes"]],
        "keys": keys,
        "values": [round(values[k], 1) for k in keys],
        "hours": [round(hours[k], 2) for k in keys],
        "drivers": [drivers[k] for k in keys],
        "scale_hours": scale,
    }


def remaining_legs(context: RunContext, shipment: Shipment, where: dict) -> list[dict]:
    """The original route from the live location to the destination.

    The current leg is cut at the asset's position; the rest are the planned
    legs with their planned geometry — the same lines the board draws.
    """
    nodes = context.config.nodes
    out = []
    for i in range(where["leg_index"], len(shipment.legs)):
        leg = shipment.legs[i]
        if i == where["leg_index"]:
            path = where["leg_remaining_path"]
            start = {"id": None if where["phase"] == "in_transit" else leg.from_node,
                     "name": "Live location" if where["phase"] == "in_transit"
                     else nodes[leg.from_node].name,
                     "lat": path[0].lat, "lon": path[0].lon}
        else:
            path = context.network.geometry(leg.from_node, leg.to_node, leg.mode).path
            start = {"id": leg.from_node, "name": nodes[leg.from_node].name,
                     "lat": nodes[leg.from_node].lat, "lon": nodes[leg.from_node].lon}
        out.append({
            "index": i,
            "mode": leg.mode.value,
            "from": start,
            "to": {"id": leg.to_node, "name": nodes[leg.to_node].name,
                   "lat": nodes[leg.to_node].lat, "lon": nodes[leg.to_node].lon},
            "km": round(length_km(path), 1),
            "planned_hours": round(
                (leg.planned_arrive - max(leg.planned_depart, context.clock.as_of)
                 ).total_seconds() / 3600.0, 2),
            "path": path,
            "path_latlon": as_latlon(path),
        })
    return out


def point_of(entry: dict) -> Point:
    return Point(entry["lat"], entry["lon"])
