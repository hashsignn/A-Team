"""Splitting a load: some boxes take the fast route, the rest stay put.

WHY THIS IS THE RHINE ANSWER
============================
Low water at Kaub is a payload derate, not a stoppage (thresholds.yaml says
so at length). The barge keeps sailing, lighter and later. What a planner
actually does that week is not "move the barge" — it is "take the three boxes
feeding a customer's line on Thursday off at Mainz and put them on trucks,
and let the replenishment stock ride the river". That is a split, and it is
cheaper than moving everything because road is priced per truck.

WHAT IS EVALUATED
=================
An allocation maps each container to a route: ``ORIGINAL`` or a candidate id
from engine/fleet/reroute.py. Each route that receives boxes becomes a
branch with its own TEU, its own rate-card cost, its own ETA — and each box is
checked against ITS OWN deadline, because the point of a split is that the
boxes do not all have the same one.

The suggestion moves exactly the boxes that miss their deadline on the
original asset AND make it on the target route. Nothing that would arrive in
time anyway is moved: that would be paying for speed nobody needs.

A PLAN, NOT AN ORDER
--------------------
Nothing here books anything or writes anything. The playbook gate still
decides whether a reroute may be taken (engine/act/flow.py) — confirming the
disruption comes first. This computes what a split would look like.
"""

from __future__ import annotations

from datetime import datetime

from engine.fleet import manifest
from engine.fleet.assets import index
from engine.fleet.reroute import price, recovery
from engine.fleet.settings import settings as fleet_settings
from engine.pipeline import RunContext

ORIGINAL = "ORIGINAL"


class SplitError(ValueError):
    """An allocation that cannot be evaluated. The message says why."""


def evaluate(board: dict, context: RunContext, shipment_id: str,
             allocation: dict | None = None, target: str | None = None,
             weights: dict | None = None) -> dict | None:
    """Branches, costs and deadlines for an allocation — or the suggested one."""
    shipment = index(context).shipments.get(shipment_id)
    if shipment is None:
        return None
    routes = recovery(board, context, shipment_id, weights=weights, force=True)
    if routes is None:
        return None

    boxes = manifest.containers(shipment)
    by_id = {b["container_id"]: b for b in boxes}
    options = {ORIGINAL: routes["original"], **{c["id"]: c for c in routes["candidates"]}}

    suggested = allocation is None
    reason = None
    if suggested:
        allocation, target, reason = _suggest(boxes, routes, target)
    else:
        problems = []
        for box_id, route_id in allocation.items():
            if box_id not in by_id:
                problems.append(f"{box_id} is not on {shipment_id}")
            if route_id not in options:
                problems.append(f"{route_id} is not a route for {shipment_id}")
        if problems:
            raise SplitError("; ".join(problems))

    assigned = {b["container_id"]: allocation.get(b["container_id"], ORIGINAL) for b in boxes}
    cfg = fleet_settings(context.config)
    total_teu = manifest.teu_of(boxes)

    branches = []
    for route_id in [ORIGINAL, *[c["id"] for c in routes["candidates"]]]:
        members = [by_id[k] for k, v in assigned.items() if v == route_id]
        if not members:
            continue
        option = options[route_id]
        teu = manifest.teu_of(members)
        eta = datetime.fromisoformat(option["eta"])
        legs = [(leg["mode"], leg["km"]) for leg in option["legs"]]
        cost = price(cfg, legs, teu, option["transfers"])
        on_time = [m["container_id"] for m in members
                   if datetime.fromisoformat(m["deadline"]) >= eta]
        branches.append({
            "route_id": route_id,
            "label": option["label"],
            "badge": option.get("badge"),
            "rank": option.get("rank"),
            "teu": teu,
            "containers": [m["container_id"] for m in members],
            "eta": option["eta"],
            "cost_chf": round(cost, 2),
            "on_time": on_time,
            "late": [m["container_id"] for m in members if m["container_id"] not in on_time],
            "vehicles": _vehicles(cfg, option, teu),
            "path": option["path"],
        })

    before = _baseline(cfg, routes["original"], boxes, total_teu)
    after_cost = sum(b["cost_chf"] for b in branches)
    after_on_time = sum(len(b["on_time"]) for b in branches)
    moved = [k for k, v in assigned.items() if v != ORIGINAL]

    return {
        "shipment_id": shipment_id,
        "suggested": suggested,
        "suggestion_reason": reason,
        "target": target,
        "allocation": assigned,
        "containers": [
            {**b, "route_id": assigned[b["container_id"]]} for b in boxes
        ],
        "branches": branches,
        "summary": {
            "containers": len(boxes),
            "moved": len(moved),
            "teu_moved": manifest.teu_of([by_id[k] for k in moved]),
            "teu_total": total_teu,
            "on_time_before": before["on_time"],
            "on_time_after": after_on_time,
            "cost_before_chf": round(before["cost"], 2),
            "cost_after_chf": round(after_cost, 2),
            "extra_cost_chf": round(after_cost - before["cost"], 2),
            "branches": len(branches),
        },
        "routes": [
            {"id": o["id"], "label": o["label"], "badge": o.get("badge"), "eta": o["eta"],
             "rank": o.get("rank")}
            for o in [routes["original"], *routes["candidates"]]
        ],
        "synthetic": True,
    }


def _baseline(cfg: dict, original: dict, boxes: list[dict], teu: int) -> dict:
    eta = datetime.fromisoformat(original["eta"])
    legs = [(leg["mode"], leg["km"]) for leg in original["legs"]]
    return {
        "cost": price(cfg, legs, teu, original["transfers"]),
        "on_time": sum(1 for b in boxes if datetime.fromisoformat(b["deadline"]) >= eta),
    }


def _vehicles(cfg: dict, option: dict, teu: int) -> dict | None:
    """Trucks needed on the new road legs — what a vendor is asked for."""
    road = [leg for leg in option["legs"] if leg["new"] and leg["mode"] == "road"]
    if not road:
        return None
    per = float(cfg["modes"]["road"].get("teu_per_vehicle", 2))
    return {"mode": "road", "count": max(1, -(-teu // int(per))), "unit": "trucks"}


def _suggest(boxes: list[dict], routes: dict, target: str | None
             ) -> tuple[dict, str | None, str]:
    """Move only the boxes the original asset now makes late, and only onto a
    route that gets them there in time. Returns (allocation, target, why)."""
    original_eta = datetime.fromisoformat(routes["original"]["eta"])
    missing = [b for b in boxes if datetime.fromisoformat(b["deadline"]) < original_eta]
    candidates = routes["candidates"]
    if target is not None:
        candidates = [c for c in candidates if c["id"] == target]
        if not candidates:
            raise SplitError(f"{target} is not a route for this shipment")
    if not missing:
        return {}, None, ("Every container makes its own deadline on the original asset. "
                          "No split needed.")
    if not candidates:
        return {}, None, "No alternative route exists to move the late containers onto."

    best = None
    for option in candidates:              # already in rank order
        eta = datetime.fromisoformat(option["eta"])
        saved = [b for b in missing if datetime.fromisoformat(b["deadline"]) >= eta]
        if saved and (best is None or len(saved) > len(best[1])):
            best = (option, saved)
    if best is None:
        return {}, None, ("No alternative gets the late containers there by their "
                          "deadlines either. Splitting would add cost and save nothing.")

    option, saved = best
    allocation = {b["container_id"]: option["id"] for b in saved}
    fine = len(boxes) - len(missing)
    lost = len(missing) - len(saved)
    why = (f"{len(saved)} container(s) miss their deadline on the original asset and make "
           f"it on {option['badge'] or option['label']}.")
    if fine:
        why += (f" {fine} stay on board: they arrive in time as planned, and moving them "
                "would buy speed nobody needs.")
    if lost:
        why += f" {lost} miss on every route, so moving them would add cost and save nothing."
    return allocation, option["id"], why
