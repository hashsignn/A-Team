"""The TMS alert payload: what leaves this system when a RED fires.

WHO CONSUMES THIS
=================
SAP TM, Blue Yonder, or whatever sits between the radar and a dispatcher.
The contract is POST /api/v1/shipment-alerts and the receiving system routes
on `alert.score` and `alert.band`.

THREE RULES THIS PAYLOAD FOLLOWS, AND WHY
-----------------------------------------
**No secrets, ever.** Not the TMS endpoint, not a token, not an internal
hostname. Everything here is freight data. Credentials live in the environment
of the process that POSTs it — see SECURITY.md — and the payload is designed
so that a copy of it pasted into a ticket leaks nothing but a shipment number.

**Every number carries its provenance.** A TMS field called `risk_score: 82`
is unarguable in the worst way. Each score here travels with the rung it came
from, the clashes that were resolved to get it, and the sentence a planner
would use. A dispatcher who disagrees can see exactly where to push.

**Recommended actions are filtered by what is legal.** `suppressed_actions`
is populated, not omitted. A TMS that offers air freight for a Class 3 solvent
has actively wasted the hours a planner had left, so the payload says which
mitigations were removed and under which rule.

NULL MEANS NULL. `probability: null` is an event whose odds cannot be sourced.
Consumers must not coerce it to 0.5. It is called out in the payload's own
`data_quality` block so an integrator reads it before their parser does.
"""

from __future__ import annotations

from typing import Any

from engine.score import alert as alert_mod

SCHEMA_VERSION = "1.0.0"
ENDPOINT = "POST /api/v1/shipment-alerts"


def build_alert(
    *,
    board: dict,
    route: dict,
    event: dict,
    resolution: dict,
    shipment: dict,
    run_id: str,
) -> dict:
    """One RED alert, ready to POST.

    Built from data already on the board. Nothing here is recomputed, so the
    TMS and the screen cannot disagree.
    """
    alert = resolution["alert"]
    pathway = resolution["pathway"]
    cost = resolution["channel_cost"]

    return {
        "schema_version": SCHEMA_VERSION,
        "message_type": "shipment_risk_alert",
        # Idempotency: the same event against the same shipment at the same
        # as-of must produce the same key, or a TMS will raise duplicate
        # exceptions every time the board is rebuilt.
        "idempotency_key": (
            f"{event['event_id']}:{shipment['shipment_id']}:{board['as_of']}"
        ),
        "emitted_at": board["as_of"],
        "as_of": board["as_of"],
        "run_id": run_id,
        "config_version": board["config_version"],

        "alert": {
            "band": alert["band"],
            "score": alert["score"],
            "level": alert["level"],
            "level_label": alert["level_label"],
            "directive": route["directive"],
            "rationale": alert["rationale"],
            "escalated": alert["escalated"],
            "escalation_reason": alert["escalation_reason"],
            "scale": {
                "note": (
                    "The score is a projection of the five-level ladder, not "
                    "a second model. 20 points per rung; the fraction inside "
                    "a rung is relative consequence within today's book and "
                    "never crosses a rung boundary."
                ),
                "bands": alert_mod.bands(),
            },
        },

        "shipment": {
            "shipment_id": shipment["shipment_id"],
            "lane_id": route["route_id"],
            "lane_name": route["name"],
            "customer": shipment.get("customer"),
            "origin": shipment.get("origin_node"),
            "destination": shipment.get("destination_node"),
            "planned_eta": shipment.get("eta"),
            "committed_date": shipment.get("otif_committed_date"),
            "value_chf": shipment.get("value_chf"),
            "carrier": shipment.get("carrier"),
        },

        "taxonomy": {
            "layer1_event": {
                "variable_id": (event.get("active_variables") or ["unmapped"])[0],
                "family": event.get("event_class"),
                "severity": event["severity"],
                "pathway": (
                    "both" if pathway["delay"]["open"] and pathway["damage"]["open"]
                    else "damage" if pathway["damage"]["open"]
                    else "delay" if pathway["delay"]["open"]
                    else "none"
                ),
                "probability": event["probability"],
                "probability_basis": event["probability_basis"],
                "starts_at": event["starts_at"],
                "ends_at": event["ends_at"],
            },
            "layer2_asset": {
                "asset_id": shipment.get("asset_id", "unmapped"),
                "mode": shipment.get("mode"),
            },
            "layer3_channel": {
                "channel_id": cost["channel"],
                "free_hours": cost["free_hours"],
                "penalty_shape": cost["shape"],
            },
            "layer4_cargo": {
                "cargo_class": shipment.get("cargo_class", "unmapped"),
                "adr_class": shipment.get("adr_class"),
                "irreversible_if_damaged": pathway["damage"]["irreversible"],
                "scrap_fraction_if_damaged": pathway["damage"]["scrap_fraction"],
            },
        },

        "assessment": {
            "pathway_headline": pathway["headline"],
            "delay": pathway["delay"],
            "damage": pathway["damage"],
            "notes": pathway["notes"],
            "chargeable_hours": cost["chargeable_hours"],
            "exposure_chf": cost["cost_chf"],
            "exposure_explanation": cost["explanation"],
            "clashes_resolved": resolution["clashes_resolved"],
        },

        "root_cause": {
            "event_id": event["event_id"],
            "title": event["title"],
            "geo": {"lat": event.get("lat"), "lon": event.get("lon")},
            "source": event["source"],
            "source_tier": event["source_tier"],
            # The quote is the audit trail. `inferred: true` with a null
            # quote is a legitimate state and is reported as one, never
            # dressed up as a citation.
            "verbatim_quote": event.get("quote"),
            "inferred": event.get("inferred", False),
        },

        "recommended_actions": [
            {
                "action": a["label"],
                "cost_chf": a.get("cost_chf"),
                "avoids_chf": a.get("avoids_chf"),
                "decide_by": a.get("deadline_text"),
                "owner": a.get("owner"),
            }
            for a in (route.get("actions") or [])
        ],
        # Populated, never omitted. A mitigation that cannot legally be taken
        # costs a planner the hours they had left.
        "suppressed_actions": resolution["actions_suppressed"],

        "contacts": {
            "route_manager": (route.get("response") or {}).get("route_manager"),
            "escalation": (route.get("response") or {}).get("escalation"),
        },

        "data_quality": {
            "probability_is_null_means_unsourced": True,
            "note": (
                "A null probability is an event whose odds cannot be sourced "
                "from anything defensible. Do NOT coerce it to 0.5 or to any "
                "other default; render it as unsourced. Every CHF figure is "
                "modelled, not invoiced."
            ),
            "shipment_book_is_synthetic": board.get("shipments_synthetic", True),
            "config_source": board.get("config_source", "config.example"),
        },

        "delivery": {
            "endpoint": ENDPOINT,
            "auth": (
                "Bearer token from the environment of the POSTing process. "
                "No credential appears in this payload by design."
            ),
            "retry": "exponential backoff; the idempotency_key makes replay safe",
        },
    }


def audit_for_secrets(payload: dict) -> list[str]:
    """Fail loudly if anything credential-shaped got into a payload.

    Cheap, deterministic, and runs in the test suite. The failure it guards
    against — a token pasted into a config and serialised into every alert —
    is the kind that is obvious afterwards and invisible before.
    """
    suspicious_keys = {
        "password", "passwd", "secret", "token", "api_key", "apikey",
        "authorization", "auth_token", "private_key", "client_secret",
        "access_key", "credential", "credentials", "bearer",
    }
    prefixes = ("sk-", "sk_live", "ghp_", "gho_", "xoxb-", "AKIA", "AIza", "eyJ")
    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                if key.lower() in suspicious_keys and isinstance(value, str) and value:
                    # `auth` describing WHERE the credential comes from is
                    # fine; a value that looks like one is not.
                    if any(value.startswith(p) for p in prefixes) or len(value) > 60:
                        problems.append(f"{here}: credential-shaped value")
                walk(value, here)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif isinstance(node, str):
            for prefix in prefixes:
                if node.startswith(prefix) and len(node) > 12:
                    problems.append(f"{path}: value begins with {prefix!r}")
    walk(payload, "")
    return problems


def alerts_for_board(board: dict, context, band: str = "red") -> list[dict]:
    """Every alert the board would emit at or above ``band``.

    Walks the ranked routes, resolves each event against the shipments it
    actually gated, and builds one payload per (event, shipment) encounter
    that clears the filter.
    """
    from engine.score.severity import Level
    from engine.taxonomy.resolve import Encounter
    from engine.taxonomy.resolve import resolve as resolve_encounter

    order = {"green": 0, "amber": 1, "red": 2}
    floor = order.get(band, 2) if band != "all" else -1

    shipments = {s.shipment_id: s for s in context.shipments}
    config = context.config
    run_id = board.get("run_id", "")

    cap = max(
        (r.get("exposure_chf", 0.0) for r in board["routes"]), default=1.0
    ) or 1.0

    out: list[dict] = []
    for route in board["routes"]:
        level = Level(route["level"])
        for event in route.get("events", []):
            for point in event.get("matrix", {}).get("points", []):
                shipment = shipments.get(point["shipment_id"])
                if shipment is None:
                    continue

                leg_mode = _leg_mode_for(
                    context, shipment.shipment_id, event["event_id"]
                )
                asset_id = _asset_of(shipment, leg_mode)
                encounter = Encounter(
                    variable_id=(event.get("active_variables") or [""])[0],
                    cargo_class=_cargo_class_of(shipment),
                    asset_id=asset_id,
                    channel_id=_channel_of(shipment),
                    late_hours=_late_hours(point),
                    observation={},
                    consequence_cap=cap,
                    base_level=level,
                )
                resolution = resolve_encounter(config, encounter).as_dict()
                if order[resolution["alert"]["band"]] < floor:
                    continue

                out.append(build_alert(
                    board=board,
                    route=route,
                    event=event,
                    resolution=resolution,
                    shipment={
                        "shipment_id": shipment.shipment_id,
                        "customer": shipment.customer,
                        "origin_node": shipment.origin_node,
                        "destination_node": shipment.destination_node,
                        "eta": shipment.eta.isoformat(),
                        "otif_committed_date": shipment.otif_committed_date.isoformat(),
                        "value_chf": shipment.value_chf,
                        "carrier": shipment.carrier,
                        "mode": leg_mode or shipment.mode,
                        "asset_id": asset_id,
                        "cargo_class": _cargo_class_of(shipment),
                        "adr_class": _adr_class_of(shipment),
                    },
                    run_id=run_id,
                ))
    return out


# ---------------------------------------------------------------------
# Bridging the current Shipment model onto Layers 2 and 4
# ---------------------------------------------------------------------
# The shipment generator still carries `dangerous_goods` and
# `temperature_controlled` booleans. These map them onto the taxonomy so the
# TMS contract is real today, and they are the ONLY place that mapping lives
# — when the order book grows proper taxonomy fields, three functions change
# and nothing else does.


def _asset_of(shipment, leg_mode: str | None = None) -> str:
    """The equipment under the freight ON THE LEG THE EVENT HIT.

    Layer 2 is per-leg, not per-shipment, and collapsing a multimodal
    shipment to a single asset gets every such shipment wrong: a Rhine
    low-water event gates the BARGE leg of a Düdingen-Basel-Rotterdam move,
    and asking whether it reaches "the shipment's asset" — a road tractor —
    correctly answers no, for the wrong reason, and drops a real exposure.

    ``leg_mode`` comes from the gate hit. Without one, the shipment's own
    mode is used, which is right for single-mode moves and is the only case
    where it is right.
    """
    mode = leg_mode or shipment.mode
    if shipment.dangerous_goods and mode == "road":
        return "adr_tanker"
    if shipment.temperature_controlled and mode == "road":
        return "reefer"
    return {
        "road": "road_ftl",
        "rail": "rail_combined",
        "barge": "barge",
        "sea": "deep_sea",
        "multimodal": "rail_combined",
    }.get(mode, "road_ftl")


def _leg_mode_for(context, shipment_id: str, event_id: str) -> str | None:
    """Which leg's mode the event actually gated.

    The gate already computed this — a GateHit carries the leg index — so
    reading it back is exact rather than inferred.
    """
    for hit in context.hits:
        if hit.shipment_id == shipment_id and hit.event_id == event_id:
            shipment = next(
                (s for s in context.shipments if s.shipment_id == shipment_id), None
            )
            if shipment and 0 <= hit.leg_index < len(shipment.legs):
                return shipment.legs[hit.leg_index].mode.value
    return None


def _cargo_class_of(shipment) -> str:
    if shipment.dangerous_goods:
        return "adr_class_3"
    if shipment.temperature_controlled:
        return "freeze_critical"
    return "none"


def _adr_class_of(shipment) -> str | None:
    return "3" if shipment.dangerous_goods else None


def _channel_of(shipment) -> str:
    return {
        "line_down": "jit_jis",
        "stock_out": "b2b_distributor",
        "inconvenience": "internal_replenishment",
    }.get(shipment.customer_impact_tier.value, "b2b_distributor")


def _late_hours(point: dict) -> float:
    """Lateness implied by this matrix point. Uses the lead time when there
    is one; a passed deadline is reported as the hours it has passed by."""
    lead = point.get("lead_time_hours")
    if lead is None:
        return 0.0
    return max(0.0, -float(lead)) if lead < 0 else 0.0
