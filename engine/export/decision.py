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
                    "loss_chf": loss,
                    "p_late": risk.do_nothing.p_late,
                    # Late against the date PROMISED, not delay against the
                    # plan: the slack between the two is free.
                    "late_days": risk.do_nothing.expected_lateness_days,
                }
    return out


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
            "best": i == 0 and on_time,
        })
    return out


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
                            "decide_in_h": a.get("lead_time_hours")})
        g["cost_chf"] += float(a.get("cost_chf") or 0.0)
        g["saves_chf"] += float(a.get("value_chf") or 0.0)
        lead = a.get("lead_time_hours")
        if lead is not None and (g["decide_in_h"] is None or lead < g["decide_in_h"]):
            g["decide_in_h"] = lead
    out = []
    for g in groups.values():
        g["cost_chf"] = round(g["cost_chf"], 2)
        g["saves_chf"] = round(g["saves_chf"], 2)
        out.append(g)
    out.sort(key=lambda g: (g["decide_in_h"] if g["decide_in_h"] is not None else 1e9,
                            -g["saves_chf"]))
    return out


def build(context: RunContext, route: dict, detail: dict | None) -> dict:
    """The tree for one board route. ``detail`` is the delivery-first
    optimiser's view of it (engine/fast/view.route_detail), or None when that
    engine finds nothing to do on it."""
    risks = _risks_on(context, route["route_id"])
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
        "exposure_chf": round(sum(r["loss_chf"] for r in material.values()), 2),
        "baseline": True,
    }] + [{**o, "baseline": False} for o in keep_options] + [
        {**o, "orders": o["orders_n"], "baseline": False}
        for o in reduce_options if o.get("route")]

    return {
        "route_id": route["route_id"],
        "happening": [{"title": e["title"], "kind": e.get("kind_label") or e.get("kind") or "",
                       "meaning": e.get("kind_meaning") or "", "orders": e.get("shipments_here", 0)}
                      for e in (route.get("events") or [])[:3]],
        "hit": {"orders": len(risks), "of": route.get("shipments", 0),
                "absorbed": len(absorbed), "need": need,
                "exposure_chf": route.get("exposure_chf", 0.0)},
        "keep": {"answer": answer, "kept": kept, "options": keep_options,
                 "more": max(0, int((detail or {}).get("options_total") or 0) - len(keep_options))},
        "reduce": {"orders": reduced, "options": reduce_options},
        "tell": {"orders": [{"shipment_id": sid, "customer": material[sid]["customer"],
                             "late_days": round(material[sid]["late_days"], 1),
                             "p_late": round(material[sid]["p_late"], 2)} for sid in told]},
        "compare": compare,
        "escalation": (route.get("response") or {}).get("escalation") or {},
    }
