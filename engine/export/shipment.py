"""The Action decision tree for ONE shipment.

Every shipment on a disrupted route has its own way out: a truck still at
the plant can switch to rail, a barge already on the Rhine cannot; a key
account's line stops if the order is late, a DIY chain takes a note. So the
tree opens on the route, and each order grows its own branch:

    What is happening      the events on the route
    Who is hit             the customers, key accounts first
    Which shipment         that customer's orders, the most at stake first
    Which way, best first  this order's recovery routes, ranked on time,
                           cost and risk, with arrival, CO2e and the change
                           against the plan; staying on the plan in the same
                           pool; another Sika site when one can serve it
    Who carries it         the partners along the way that can take it
    Sign off and book      the limit, then book, with undo

The ways are the fleet map's recovery routes (engine/fleet/reroute.py), the
same numbers the Action Hub shows under "Plan recovery", so the two never
disagree about a shipment. Nothing here decides: it arranges what the
engines already computed around one order.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine.pipeline import RunContext

TIER = {"A": "Key account", "B": "Standard", "C": "Flexible"}
IMPACT = {"line_down": "their line stops if late", "stock_out": "they run out if late",
          "inconvenience": "an inconvenience if late"}


def _parts(context: RunContext, shipment_id: str) -> dict[str, float]:
    """This order's expected loss by what drives it, over every event, the
    way the board totals a route."""
    parts = {"penalty": 0.0, "expediting": 0.0, "customer_impact": 0.0,
             "surcharge": 0.0, "penalty_if_counted": 0.0}
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            if risk.shipment_id == shipment_id:
                for k in parts:
                    parts[k] += float((risk.do_nothing.cost_parts or {}).get(k, 0.0))
    return {k: round(v, 2) for k, v in parts.items()}


def _modes(legs: list[dict]) -> list[dict]:
    """The legs as a short chain of modes, a changed leg marked: what a box
    can draw as three icons instead of a sentence."""
    out: list[dict] = []
    for leg in legs or []:
        mode, new = leg.get("mode"), bool(leg.get("new"))
        if out and out[-1]["mode"] == mode and out[-1]["new"] == new:
            out[-1]["km"] += float(leg.get("km") or 0.0)
            out[-1]["to"] = (leg.get("to") or {}).get("name")
            continue
        out.append({"mode": mode, "new": new, "km": float(leg.get("km") or 0.0),
                    "from": (leg.get("from") or {}).get("name"), "to": (leg.get("to") or {}).get("name")})
    for m in out:
        m["km"] = round(m["km"])
    return out


def _closes_at(now: datetime, committed: str | None, eta: str | None, on_time: bool) -> str | None:
    """A way that lands on time stays open for as long as its slack to the
    promised date: after that, starting it misses the date."""
    if not (on_time and committed and eta):
        return None
    try:
        slack = datetime.fromisoformat(committed) - datetime.fromisoformat(eta)
    except ValueError:
        return None
    return (now + max(timedelta(0), slack)).isoformat()


def late_days(eta: str | None, committed: str | None) -> float:
    """Days after the promised date a way lands, 0 when it is in time. Not the
    engine's delay_hours: that is the delay against the planned arrival, and
    a plan with slack can be delayed and still on time."""
    try:
        late = datetime.fromisoformat(eta) - datetime.fromisoformat(committed)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, late.total_seconds() / 86400.0), 1)


def _option(c: dict, now: datetime, committed: str | None, plan: dict | None) -> dict:
    delta = c.get("delta") or {}
    on_time = bool(c.get("meets_commitment"))
    if plan is not None and c.get("kind") != "original":
        days_saved = round(-float(delta.get("hours") or 0.0) / 24.0, 1)
    else:
        days_saved = 0.0
    return {
        "id": c["id"],
        "label": c.get("label") or c["id"],
        "kind": c.get("kind"),
        "plan": c.get("kind") == "original",
        "eta": c.get("eta"),
        "on_time": on_time,
        "late_days": late_days(c.get("eta"), committed),
        "cost_chf": round(float(c.get("cost_chf") or 0.0), 2),
        "extra_chf": round(float(delta.get("cost_chf") or 0.0), 2),
        "days_saved": days_saved,
        "co2e_t": round(float(c.get("co2e_kg") or 0.0) / 1000.0, 1),
        "co2e_pct": delta.get("co2e_pct"),
        "lowest_co2": bool(c.get("lowest_co2_on_time")),
        "risk": round(float(c.get("risk") or 0.0), 3),
        "risk_label": c.get("risk_label"),
        "risk_unsourced": bool(c.get("risk_unsourced")),
        "setup_h": round(float(c.get("setup_hours") or 0.0), 1),
        "km": round(float(c.get("km") or 0.0)),
        "hours": round(float(c.get("hours") or 0.0), 1),
        "transfers": c.get("transfers"),
        "owner": c.get("owner") or "us",
        "lever": c.get("lever"),
        "score": c.get("score"),
        "beats_plan": bool(c.get("beats_original")) if c.get("kind") != "original" else False,
        "chain": _modes(c.get("legs") or []),
        "path": [c["legs"][0]["from"]["name"], *[leg["to"]["name"] for leg in c["legs"]]] if c.get("legs") else [],
        "notes": list(c.get("notes") or []),
        "closes_at": _closes_at(now, committed, c.get("eta"), on_time) if c.get("kind") != "original" else None,
    }


def _partners(vendors: dict | None, cargo: dict) -> list[dict]:
    """Who along the way can take it, and for which of the ways."""
    out = []
    for v in (vendors or {}).get("vendors") or []:
        serves = [s["route_id"] for s in v.get("serviceable") or [] if s.get("ok")]
        reasons = {s["route_id"]: list(s.get("reasons") or []) for s in v.get("serviceable") or []}
        cap = v.get("capacity") or {}
        contact = v.get("contact") or {}
        out.append({
            "id": v["id"], "name": v["name"], "kind": v.get("kind"), "modes": list(v.get("modes") or []),
            "city": v.get("city"), "km": round(float(v.get("distance_km") or 0.0)),
            "capacity": f"{cap.get('available')} {cap.get('unit')}" if cap else None,
            "teu": (cap.get("available") or 0) * (cap.get("teu_per_unit") or 0) if cap else None,
            "adr": v.get("adr_certified"), "reefer": v.get("reefer"),
            "needs_adr": bool(cargo.get("dangerous_goods")),
            "channel": v.get("channel"), "email": contact.get("email"), "phone": contact.get("phone"),
            "portal": contact.get("portal"), "synthetic": bool(v.get("synthetic")),
            "checked_against": v.get("checked_against"), "note": v.get("note"),
            "serves": serves, "reasons": reasons,
        })
    # Real operators first (they exist), then those who can take the most.
    out.sort(key=lambda p: (not p["serves"], p["synthetic"], -(p["teu"] or 0), p["km"]))
    return out


def verdict(recovery: dict | None) -> dict:
    """A shipment's own ways in one line, for its box before it is opened:
    how many land it on time, and how late the plan is. From the same engine
    as its branch, so the box never promises what the branch does not show.
    Independent of the ranking weights: they reorder the ways, not this."""
    plan = (recovery or {}).get("original") or {}
    ways = list((recovery or {}).get("candidates") or [])
    status = (recovery or {}).get("status") or {}
    legs = plan.get("legs") or []
    return {
        # False when the map has no position for it: past its planned
        # arrival, so no route from "here" can be drawn.
        "located": recovery is not None,
        "ways": len(ways),
        "on_time": sum(1 for c in ways if c.get("meets_commitment")),
        "plan_on_time": bool(plan.get("meets_commitment")),
        "plan_eta": plan.get("eta"),
        "late_days": late_days(plan.get("eta"), recovery.get("committed")) if recovery else None,
        "level": status.get("level"),
        "label": status.get("label"),
        "mode": legs[0].get("mode") if legs else None,
    }


def build(context: RunContext, board: dict, route: dict, tree: dict, asset: dict | None,
          recovery: dict | None, vendors: dict | None, shipment_id: str) -> dict:
    """One shipment's branch of the tree. ``tree`` is the route's decision
    tree (engine/export/decision.py), for the order's branch, the sign-off
    limit, the contract clocks and another site; ``asset``, ``recovery`` and
    ``vendors`` are the fleet map's detail, recovery routes and partners."""
    now = context.clock.as_of
    row = next((o for o in tree.get("orders") or [] if o["shipment_id"] == shipment_id), {})
    logistics = (asset or {}).get("logistics") or {}
    cargo = (asset or {}).get("cargo") or {}
    customer_name = row.get("customer") or logistics.get("customer")
    customer = next((c for c in route.get("customers") or [] if c["name"] == customer_name), {})
    clause = next((c for c in route.get("clauses") or [] if c.get("customer") == customer_name), {})
    tier = customer.get("priority") or row.get("tier") or "B"
    status = (asset or {}).get("status") or (recovery or {}).get("status") or {}
    committed = (recovery or {}).get("committed") or logistics.get("committed") or row.get("due")

    plan = (recovery or {}).get("original")
    candidates = list((recovery or {}).get("candidates") or [])
    options = [_option(c, now, committed, plan) for c in candidates]
    stay = _option(plan, now, committed, None) if plan else None
    # One pool, ranked by the engine's own score (lower is better): the plan
    # is in it, so "stay" can be the answer, and is shown so when it is.
    pool = options + ([stay] if stay else [])
    pool.sort(key=lambda o: (o["score"] if o["score"] is not None else 9.0))
    for i, o in enumerate(pool):
        o["rank"] = i + 1
    best = pool[0] if pool else None
    for o in pool:
        o["best"] = o is best

    # Another Sika site, when the route's Procurement lever has one for this order.
    sources = [dict(s, on_time_here=shipment_id in (s.get("on_time_ids") or []))
               for s in tree.get("sources") or [] if shipment_id in (s.get("shipment_ids") or [])]

    driving = status.get("driving_event")
    clocks = next((e["clocks"] for e in tree.get("clocks") or [] if e.get("title") == driving),
                  (tree.get("clocks") or [{}])[0].get("clocks") if tree.get("clocks") else [])
    started = next((e["starts_at"] for e in tree.get("clocks") or [] if e.get("title") == driving), None)

    containers = (asset or {}).get("containers") or []
    load = (asset or {}).get("load") or {}
    leg = (asset or {}).get("leg") or {}
    vehicle = (asset or {}).get("asset") or {}
    others = [o for o in tree.get("orders") or [] if o.get("branch") != "absorbed"]

    return {
        "shipment_id": shipment_id,
        "route_id": route["route_id"],
        "as_of": now.isoformat(),
        "customer": {
            "name": customer_name, "tier": tier, "tier_label": TIER.get(tier, tier),
            "impact": customer.get("impact"), "impact_words": IMPACT.get(customer.get("impact") or "", ""),
            "orders": list(customer.get("orders") or []), "orders_at_risk": list(customer.get("orders_at_risk") or []),
            "loss_chf": customer.get("expected_loss_chf"),
            "clause": {k: clause.get(k) for k in ("label", "clause", "summary", "counted", "sources")} if clause else None,
        },
        "order": {
            "status": {k: status.get(k) for k in ("level", "label", "reason")},
            "phase": (asset or {}).get("phase_label"),
            "vehicle": {"name": vehicle.get("name"), "id": vehicle.get("asset_id"), "mode": vehicle.get("mode"),
                        "synthetic": bool(vehicle.get("synthetic"))},
            "leg": {"from": leg.get("from_name"), "to": leg.get("to_name"), "mode": leg.get("mode"),
                    "index": leg.get("index"), "count": leg.get("count")},
            "cargo": {"type": cargo.get("type"), "dangerous_goods": bool(cargo.get("dangerous_goods")),
                      "temperature_controlled": bool(cargo.get("temperature_controlled")),
                      "value_chf": cargo.get("value_chf")},
            "teu": (recovery or {}).get("teu") or load.get("loaded_teu"),
            "tonnes": (recovery or {}).get("tonnes"),
            "containers": len(containers),
            "vehicles": load.get("vehicles"),
            "committed": committed,
            "eta_original": logistics.get("eta_original"),
            "eta_revised": logistics.get("eta_revised"),
            "late_days": row.get("late_days", round(float(status.get("delay_hours") or 0.0) / 24.0, 1)),
            "p_late": row.get("p_late", status.get("p_late")),
            "loss_chf": row.get("loss_chf"),
            "branch": row.get("branch"),
            "from": logistics.get("origin"), "to": logistics.get("destination"),
        },
        "event": {"title": driving, "starts_at": started,
                  "at": ((recovery or {}).get("disruption") or {}).get("name")},
        "clocks": clocks or [],
        "cost": {"parts": _parts(context, shipment_id),
                 "penalties_counted": bool((board.get("penalties") or {}).get("enabled"))},
        "weights": (recovery or {}).get("weights"),
        "located": recovery is not None,
        "note": (recovery or {}).get("note") if recovery is not None else (
            "Past its planned arrival, so the map has no position for it and no "
            "recovery route can be drawn. What is left is telling the customer."),
        "options": [o for o in pool if not o["plan"]],
        "stay": stay,
        "best": best["id"] if best else None,
        "stay_best": bool(best and best["plan"]),
        "sources": sources,
        "partners": _partners(vendors, cargo),
        "approval": tree.get("approval") or {},
        "others": [{"shipment_id": o["shipment_id"], "customer": o["customer"], "tier": o.get("tier"),
                    "branch": o.get("branch"), "loss_chf": o.get("loss_chf")} for o in others],
    }
