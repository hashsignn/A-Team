"""The Action tab as a decision tree: one flow, per order, no order twice.

Two engines answer "what do we do" on this board, and on screen they used to
disagree. The playbook (the route panel's option cards) values an action by
the expected loss it avoids; the delivery-first optimiser (Act fast) keeps an
option only if it lands the order on its promised date and still leaves
margin. Both are right about different questions, and side by side they read
as a contradiction: "switch the barge leg to rail, it saves CHF 44,895" above
a page that rules the same switch out.

So the tab asks the questions in the order a planner does, and each engine
answers the one it is for:

    What is happening?            the events on the route
    Who is hit?                   orders touched; those the buffers absorb
    Can we still keep the dates?  YES: the route alternatives that land on
                                  time (delivery-first optimiser), compared
                                  NO:  what reduces the damage (playbook), and
                                  for the rest, tell the customer
    Who needs to know?            the escalation step and the summary

Every order at risk lands in exactly one branch: kept on time, damage
reduced, customer told, or absorbed. Nothing here is decided: it is the same
advice as before, arranged so it can be followed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine.network.geo import Point, haversine_km
from engine.pipeline import RunContext

# Below this expected loss an order hit by an event arrives on time anyway
# or near enough: the buffers absorb it. The same floor the ladder uses for
# "nothing at stake" would hide single orders, so it is per order and small.
ABSORBED_CHF = 50.0


def _risks_on(context: RunContext, route_id: str) -> dict[str, dict]:
    """Per order on this route: its worst risk, as the tree needs it."""
    lane = {s.shipment_id for s in context.shipments if s.lane_id == route_id}
    out: dict[str, dict] = {}
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            if risk.shipment_id not in lane:
                continue
            row = out.get(risk.shipment_id)
            loss = risk.do_nothing.expected_loss_chf
            if row is None or loss > row["loss_chf"]:
                out[risk.shipment_id] = {
                    "shipment_id": risk.shipment_id,
                    "customer": risk.customer,
                    "event_id": risk.event_id,
                    "cost_parts": dict(risk.do_nothing.cost_parts or {}),
                    "loss_chf": loss,
                    "p_late": risk.do_nothing.p_late,
                    # Late against the date PROMISED, not delay against the
                    # plan: the slack between the two is free.
                    "late_days": risk.do_nothing.expected_lateness_days,
                }
    return out


def _parts_on(context: RunContext, route_id: str) -> dict[str, float]:
    """The expected loss on this route by what drives it, over every event
    the way the board totals the route."""
    lane = {s.shipment_id for s in context.shipments if s.lane_id == route_id}
    parts = {"penalty": 0.0, "expediting": 0.0, "customer_impact": 0.0,
             "surcharge": 0.0, "penalty_if_counted": 0.0}
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            if risk.shipment_id in lane:
                for k in parts:
                    parts[k] += float((risk.do_nothing.cost_parts or {}).get(k, 0.0))
    return {k: round(v, 2) for k, v in parts.items()}


def _place(context: RunContext, node_id: str) -> dict:
    try:
        node = context.network.node(node_id)
    except KeyError:
        return {"id": node_id, "name": node_id, "lat": None, "lon": None, "kind": ""}
    return {"id": node.id, "name": node.name, "lat": node.lat, "lon": node.lon,
            "kind": node.kind.value}


_WATER = {"seaport", "chokepoint"}


def _line(context: RunContext, node_ids: list[str], modes: list[str]) -> list[list[float]]:
    """The path of a way to move the freight, as [lon, lat] points for the
    map: each leg by the same geometry the board draws lanes with (sea legs
    through their chokepoints), its mode read from what it joins."""
    from engine.schemas import Mode  # noqa: PLC0415

    land = [m for m in ("rail", "road", "barge") if m in (modes or [])] or ["road"]
    out: list[list[float]] = []
    for a, b in zip(node_ids, node_ids[1:], strict=False):
        try:
            na, nb = context.network.node(a), context.network.node(b)
        except KeyError:
            continue
        mode = "sea" if na.kind.value in _WATER and nb.kind.value in _WATER else land[0]
        try:
            geometry = context.network.geometry(a, b, Mode(mode))
        except (KeyError, ValueError):
            continue
        points = [[round(p.lon, 4), round(p.lat, 4)] for p in geometry.path]
        out.extend(points[1:] if out else points)
    return out


def _route_options(detail: dict | None, context: RunContext, on_time: bool) -> list[dict]:
    """The optimiser's ways to move the freight: those that land every order
    they carry on time (on_time=True), or those that are faster but still
    late (on_time=False)."""
    if not detail:
        return []
    out = []
    for i, o in enumerate(detail.get("options") or []):
        if not (o.get("restores_delivery") and o.get("executable")):
            continue
        if bool(o.get("on_time")) is not on_time:
            continue
        out.append({
            "id": o["option_id"],
            "label": o["label"],
            "kind": o["kind"],
            "path": [_place(context, n) for n in o.get("route") or []],
            "line": _line(context, list(o.get("route") or []), list(o.get("modes") or [])),
            "modes": list(o.get("modes") or []),
            "shipment_ids": list(o.get("shipment_ids") or []),
            "orders": int(o.get("shipments") or 0),
            "on_time": int(o.get("on_time_shipments") or 0),
            "cost_chf": round(float(o.get("cost_chf") or 0.0), 2),
            "starts_in_h": o.get("hours_to_start"),
            "done_in_h": o.get("hours_to_resolve"),
            "late_after_days": o.get("days_late_after"),
            "owner": o.get("owner"),
            "detail": o.get("detail", ""),
            "capacity_note": o.get("capacity_note"),
            "window_h": o.get("window_hours"),
            "best": i == 0 and on_time,
        })
    return out


def _closes_at(now: datetime, window_h: float | None) -> str | None:
    """The moment a way stops working: after it, starting it misses the
    promised date. The optimiser's window, as a time on the clock."""
    if window_h is None:
        return None
    return (now + timedelta(hours=float(window_h))).isoformat()


def _carriers(context: RunContext, partners: list[dict], path: list[dict], modes: list[str]) -> list[dict]:
    """Who can carry a way: partners and real operators within their
    service radius of a place on it, running one of its modes."""
    points = [Point(p["lat"], p["lon"]) for p in path if p.get("lat") is not None]
    if not points:
        return []
    out = []
    for p in partners:
        if not set(p.get("modes") or []) & set(modes or []):
            continue
        here = Point(p["lat"], p["lon"])
        km = min(haversine_km(here, q) for q in points)
        if km > float(p.get("service_radius_km") or 100.0):
            continue
        cap = p.get("capacity") or {}
        teu = (cap.get("available") or 0) * (cap.get("teu_per_unit") or 0) if cap else None
        contact = p.get("contact") or {}
        out.append({
            "name": p["name"], "kind": p.get("kind"), "modes": list(p.get("modes") or []),
            "city": p.get("city"), "km": round(km),
            "capacity": (f"{cap.get('available')} {cap.get('unit')}" if cap else None),
            "teu": teu, "channel": p.get("channel"),
            "phone": contact.get("phone"), "email": contact.get("email"), "portal": contact.get("portal"),
            "synthetic": bool(p.get("synthetic")), "note": p.get("note"),
        })
    # Per mode, so the truck leg's hauliers are not crowded out by the sea
    # lines: real operators first (they exist), then capacity, then nearest.
    out.sort(key=lambda c: (c["synthetic"], -(c["teu"] or 0), c["km"]))
    picked: list[dict] = []
    for mode in modes or []:
        picked += [c for c in out if mode in c["modes"] and c not in picked][:3]
    return picked


def _event_clocks(context: RunContext, route: dict) -> list[dict]:
    """Each event on the route with the contract clocks its timestamp starts."""
    spec = context.config.scoring.get("contract_clocks") or {}
    out = []
    for e in (route.get("events") or [])[:3]:
        try:
            start = datetime.fromisoformat(e["starts_at"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "title": e["title"], "starts_at": e["starts_at"],
            "clocks": [{"id": c["id"], "label": c["label"], "party": c.get("party"),
                        "due_at": (start + timedelta(hours=float(c["hours"]))).isoformat(),
                        "hours": c["hours"], "basis": c.get("basis", "")}
                       for c in spec.get("from_event") or []],
        })
    return out


def _sources(board: dict | None, orders: set[str], now: datetime) -> list[dict]:
    """Another Sika site that can serve these orders on time (the all-hands
    Procurement lever), cut to this route's orders."""
    if not board:
        return []
    levers = (board.get("all_hands") or {}).get("levers") or []
    items = next((lv.get("items") or [] for lv in levers if lv.get("id") == "FN_PROCUREMENT"), [])
    out = []
    for item in items:
        mine = sorted(set(item.get("orders") or []) & orders)
        if not mine:
            continue
        window = item.get("window_h")
        out.append({"id": f"src:{item['text']}", "label": item["text"].split(" · ")[0],
                    "orders": len(mine), "shipment_ids": mine, "detail": item.get("detail", ""),
                    "on_time": len(set(item.get("in_time_orders") or []) & set(mine)),
                    "via": item.get("via"), "via_route": item.get("route_id"),
                    "hours": item.get("hours"), "value_chf": item.get("value_chf"),
                    "closes_at": (now + timedelta(hours=float(window))).isoformat()
                    if window is not None else None})
    return out


def _lever(board: dict | None, lever_id: str) -> dict:
    for lv in ((board or {}).get("all_hands") or {}).get("levers") or []:
        if lv.get("id") == lever_id:
            return lv
    return {}


def _approval(board: dict | None, route: dict) -> dict:
    """Who signs off what: the delegated limit, raised in a crisis meeting,
    and the people to ask above it."""
    lever = _lever(board, "FN_CONTROLLING")
    crisis = ((board or {}).get("all_hands") or {}).get("posture") == "CONVENE"
    limit = lever.get("crisis_limit_chf") if crisis and lever.get("crisis_limit_chf") else lever.get("limit_chf")
    teams = {t.get("name"): t for t in (route.get("response") or {}).get("standing_teams") or []}

    def person(p: dict | None) -> dict | None:
        if not p:
            return None
        return {k: p.get(k) for k in ("name", "role", "email", "phone")}

    return {"limit_chf": limit, "base_limit_chf": lever.get("limit_chf"),
            "crisis_limit_chf": lever.get("crisis_limit_chf"), "crisis": crisis,
            "controlling": person(teams.get("Controlling")),
            "procurement": person(teams.get("Procurement")),
            "route_manager": person((route.get("response") or {}).get("route_manager"))}


def _group_actions(actions: list[dict], keep: set[str]) -> list[dict]:
    """The playbook's actions for orders no alternative keeps on time,
    grouped by what they are, soonest deadline first."""
    groups: dict[str, dict] = {}
    for a in actions:
        if a["shipment_id"] in keep:
            continue
        g = groups.setdefault(a["label"], {
            "label": a["label"],
            "action_type": a.get("action_type"),
            "owner": a.get("owner"),
            "orders": [],
            "cost_chf": 0.0,
            "saves_chf": 0.0,
            "takes_h": a.get("min_hours"),
            "decide_in_h": None,
        })
        g["orders"].append({"shipment_id": a["shipment_id"], "customer": a["customer"],
                            "priority": a.get("customer_priority", "B"),
                            "decide_in_h": a.get("lead_time_hours"),
                            "clock_h": a.get("clock_hours")})
        g["cost_chf"] += float(a.get("cost_chf") or 0.0)
        g["saves_chf"] += float(a.get("value_chf") or 0.0)
        lead = a.get("lead_time_hours")
        if lead is not None and (g["decide_in_h"] is None or lead < g["decide_in_h"]):
            g["decide_in_h"] = lead
    out = []
    for g in groups.values():
        g["cost_chf"] = round(g["cost_chf"], 2)
        g["saves_chf"] = round(g["saves_chf"], 2)
        clocks = [o["clock_h"] for o in g["orders"] if o.get("clock_h") is not None]
        g["closes_in_h"] = min(clocks) if clocks else None
        out.append(g)
    out.sort(key=lambda g: (g["decide_in_h"] if g["decide_in_h"] is not None else 1e9,
                            -g["saves_chf"]))
    return out


def build(context: RunContext, route: dict, detail: dict | None,
          board: dict | None = None) -> dict:
    """The tree for one board route. ``detail`` is the delivery-first
    optimiser's view of it (engine/fast/view.route_detail), or None when that
    engine finds nothing to do on it. ``board`` supplies the other-site
    sources (the all-hands Procurement lever)."""
    risks = _risks_on(context, route["route_id"])
    all_parts = _parts_on(context, route["route_id"])
    absorbed = sorted(sid for sid, r in risks.items() if r["loss_chf"] < ABSORBED_CHF)
    material = {sid: r for sid, r in risks.items() if r["loss_chf"] >= ABSORBED_CHF}

    keep_options = _route_options(detail, context, on_time=True)
    kept = sorted({sid for o in keep_options for sid in o["shipment_ids"]} & set(material))
    rest = set(material) - set(kept)

    # For the orders no alternative keeps on time: the playbook's actions,
    # and the optimiser's ways that are faster but still late.
    reduce_options = _group_actions(route.get("actions") or [], set(kept))
    for o in _route_options(detail, context, on_time=False):
        ids = sorted(set(o["shipment_ids"]) & rest)
        if ids:
            reduce_options.append({**o, "shipment_ids": ids, "orders_n": len(ids), "route": True})
    reduced = sorted(({o["shipment_id"] for g in reduce_options if not g.get("route")
                       for o in g["orders"]}
                      | {sid for g in reduce_options if g.get("route") for sid in g["shipment_ids"]})
                     & rest)
    told = sorted(rest - set(reduced))

    need = len(material)
    answer = ("none" if not need else "yes" if len(kept) == need
              else "partly" if kept else "no")

    # Clocks: when each way stops working. A way that keeps the date closes
    # when waiting longer would miss it; a faster-but-late way never closes,
    # it only gets later; a playbook action at its own decision deadline.
    now = context.clock.as_of
    for o in keep_options + [g for g in reduce_options if g.get("route")]:
        o["closes_at"] = _closes_at(now, o.get("window_h"))
    for g in reduce_options:
        if not g.get("route"):
            h = g.get("closes_in_h")
            g["closes_at"] = (now + timedelta(hours=h)).isoformat() if h is not None else None
    clock_h = route.get("clock_hours")
    decide_at = (now + timedelta(hours=clock_h)).isoformat() if clock_h is not None else None

    # Who can carry each way: partners and operators along its path.
    from engine.fleet import vendors  # noqa: PLC0415
    partners = vendors.partners(context)
    for o in keep_options + [g for g in reduce_options if g.get("route")]:
        o["carriers"] = _carriers(context, partners, o["path"], o["modes"])

    # What it costs: the expected loss by driver, the penalty clauses
    # included when they are counted, and what they would add when not.
    # Summed the way the board sums the route, so the two totals agree.
    parts = all_parts

    # Staying can be the best way: when every order is more likely than not
    # on time anyway, and what lateness would cost, weighted by its chance,
    # is less than the cheapest way costs for certain. Paying CHF 5,000 to
    # protect CHF 800 of expected loss is not a recommendation.
    stay_loss = round(sum(v for k, v in parts.items() if k != "penalty_if_counted"), 2)
    stay_best = bool(need and keep_options
                     and all(r["p_late"] < 0.5 for r in material.values())
                     and stay_loss < min(o["cost_chf"] for o in keep_options))
    if stay_best:
        for o in keep_options:
            o["best"] = False

    # The comparison: staying as planned against each way to move the
    # freight, on the numbers a planner compares routes by. "On time" for
    # staying is the orders more likely than not to make their date anyway.
    worst_late = max((r["late_days"] for r in material.values()), default=0.0)
    compare = [{
        "id": "as_planned",
        "label": "Stay as planned",
        "path": [_place(context, n) for n in route.get("node_ids") or []],
        "orders": need,
        "on_time": sum(1 for r in material.values() if r["p_late"] < 0.5),
        "cost_chf": 0.0,
        "starts_in_h": None,
        "late_after_days": round(worst_late, 2),
        "exposure_chf": stay_loss,
        "baseline": True,
        "best": stay_best,
    }] + [{**o, "baseline": False} for o in keep_options] + [
        {**o, "orders": o["orders_n"], "baseline": False}
        for o in reduce_options if o.get("route")]

    # Per order, the numbers the branches are made of.
    ships = {sh.shipment_id: sh for sh in context.shipments}
    branch = ({sid: "kept" for sid in kept} | {sid: "reduced" for sid in reduced}
              | {sid: "told" for sid in told} | {sid: "absorbed" for sid in absorbed})
    from engine.desk import priority_of  # noqa: PLC0415
    orders = []
    for sid, r in sorted(risks.items(), key=lambda kv: -kv[1]["loss_chf"]):
        sh = ships.get(sid)
        orders.append({
            "shipment_id": sid, "customer": r["customer"],
            "tier": priority_of(r["customer"], context.config),
            "loss_chf": round(r["loss_chf"], 2), "p_late": round(r["p_late"], 2),
            "late_days": round(r["late_days"], 1), "branch": branch.get(sid, "absorbed"),
            "due": sh.otif_committed_date.isoformat() if sh else None,
            "value_chf": round(sh.value_chf, 2) if sh else None,
        })

    return {
        "route_id": route["route_id"],
        "route": {"name": route.get("name"), "level": route.get("level"),
                  "level_label": route.get("level_label"),
                  "site": (route.get("site") or {}).get("name"),
                  "shipments": route.get("shipments", 0)},
        "orders": orders,
        "approval": _approval(board, route),
        # The other routes with something to decide, for the page's picker.
        "others": [{"route_id": r["route_id"], "name": r.get("name"), "level": r.get("level"),
                    "level_label": r.get("level_label"), "at_risk": r.get("shipments_at_risk", 0)}
                   for r in (board or {}).get("routes") or [] if r.get("shipments_at_risk")],
        "as_of": now.isoformat(),
        "decide_at": decide_at,
        "clocks": _event_clocks(context, route),
        "after_delivery": list((context.config.scoring.get("contract_clocks") or {}).get("after_delivery") or []),
        "cost": {"parts": parts, "penalties_counted": bool((board or {}).get("penalties", {}).get("enabled")),
                 "total_chf": round(parts["penalty"] + parts["expediting"] + parts["customer_impact"]
                                    + parts["surcharge"], 2)},
        "sources": _sources(board, set(material), now),
        "happening": [{"title": e["title"], "kind": e.get("kind_label") or e.get("kind") or "",
                       "meaning": e.get("kind_meaning") or "", "orders": e.get("shipments_here", 0)}
                      for e in (route.get("events") or [])[:3]],
        "hit": {"orders": len(risks), "of": route.get("shipments", 0),
                "absorbed": len(absorbed), "need": need,
                "exposure_chf": route.get("exposure_chf", 0.0)},
        "keep": {"answer": answer, "kept": kept, "options": keep_options, "stay_best": stay_best,
                 "more": max(0, int((detail or {}).get("options_total") or 0) - len(keep_options))},
        "reduce": {"orders": reduced, "options": reduce_options},
        "tell": {"orders": [{"shipment_id": sid, "customer": material[sid]["customer"],
                             "late_days": round(material[sid]["late_days"], 1),
                             "p_late": round(material[sid]["p_late"], 2)} for sid in told]},
        "compare": compare,
        "escalation": (route.get("response") or {}).get("escalation") or {},
    }
