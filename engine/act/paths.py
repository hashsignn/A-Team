"""Action & Response: a decision flow that branches.

WHAT THE CONSOLE DID NOT DO
===========================
``console.py`` turned a checklist into controls, and every lane got the same
four stages in the same order. That is still a single fixed procedure. A
planner facing a closed port does not walk the same steps as one facing a
slow barge — they choose a RESPONSE, and the response decides the steps.

So this is a graph:

    confirm the situation on site
        │
        ├── Reroute the shipment        inventory → routes → Logistics &
        │                               Procurement → approval → execute
        ├── Use an alternative port     ports → port agent → rebook →
        │                               approval → execute
        ├── Split the shipment          urgent boxes → fast route →
        │                               approval → execute
        └── Hold and monitor            tell the customer → checkpoint → close

Horizon RECOMMENDS one path from the engine's own numbers — the recovery
routes, the split suggestion, the declared port alternatives — and activates
its first step once the situation is confirmed. The planner is never held to
it: choosing another path at any point opens that one instead, and the trail
records the switch.

WHAT EACH BLOCK CARRIES
-----------------------
Everything needed to do the step without leaving the page: the named contact
and their number, the consignment and the vehicle carrying it, and the
response time — tied to the lane's rung on the ladder, because the rung IS a
deadline. A block you have to go and research is a block nobody finishes.

WHO HOLDS THE STATE
-------------------
The engine builds the graph and evaluates it. It never stores the ticks —
which steps are done and which path was chosen is per-planner working state,
held at the API boundary like the console's (api/fast_routes.py), and passed
back in. ``evaluate`` is pure: same graph, same ticks, same answer. The trail
is what makes it traceable: every step completed and every path chosen, by
whom and when, in order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ROOT = "confirm.site"

# How long a step has, by the lane's rung. The rung is a deadline, so the
# response time is read off it rather than invented per step.
RESPONSE_HOURS = {
    "red": 1.0, "yellow": 4.0, "blue": 24.0, "white": 48.0, "green": 72.0,
}

# Step-state words, as the screen shows them.
DONE = "done"          # green: completed, by whom and when is in the trail
ACTIVE = "active"      # the next thing to do on the chosen path
NEXT = "next"          # later on the chosen path — activates in turn
IDLE = "idle"          # on a path nobody has chosen


@dataclass
class Block:
    """One action block."""

    step_id: str
    path_id: str | None            # None for the shared root
    label: str
    why: str
    contact: dict
    response_hours: float | None
    info: list[dict] = field(default_factory=list)   # [{label, value}]

    def as_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "path_id": self.path_id,
            "label": self.label,
            "why": self.why,
            "contact": self.contact,
            "response_hours": self.response_hours,
            "info": self.info,
        }


@dataclass
class Path:
    path_id: str
    label: str
    summary: str
    steps: list[Block]
    available: bool = True
    unavailable_reason: str | None = None
    basis: str = ""                 # what the engine computed that supports it

    def as_dict(self) -> dict:
        return {
            "path_id": self.path_id,
            "label": self.label,
            "summary": self.summary,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "basis": self.basis,
            "steps": [s.as_dict() for s in self.steps],
        }


# =====================================================================
# Building the graph for one lane
# =====================================================================


def _team(config, team_id: str) -> dict:
    for team in config.contacts.get("internal", []):
        if team["id"] == team_id:
            return {
                "name": team.get("function", team_id),
                "role": team.get("role", ""),
                "phone": team.get("phone"),
                "email": team.get("contact"),
                "sla_hours": team.get("response_sla_hours"),
            }
    return {"name": team_id, "role": "", "phone": None, "email": None, "sla_hours": None}


def _person(entry: dict | None, fallback: dict) -> dict:
    if not entry or not entry.get("name"):
        return fallback
    return {
        "name": entry.get("name"),
        "role": entry.get("role", ""),
        "phone": entry.get("phone"),
        "email": entry.get("email"),
        "sla_hours": (entry.get("meta") or {}).get("response_sla_hours"),
    }


def _chf(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "−" if value < 0 else ""
    return f"{sign}CHF {abs(round(value)):,}".replace(",", "'")


def _when(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M UTC")


def lead_consignment(board: dict, context, route_id: str) -> dict | None:
    """The consignment the flow is written around.

    One lane carries many consignments; a block that says "call the site"
    needs to say about WHICH vehicle. The worst-off one on the lane: the
    most delayed of those still undelivered, preferring ones already moving.
    """
    from engine.fleet import assets as assets_mod  # noqa: PLC0415

    fleet = assets_mod.fleet_assets(board, context)
    mine = [a for a in fleet["assets"] if a["lane_id"] == route_id]
    if not mine:
        return None
    phase_rank = {"in_transit": 0, "at_node": 1, "staging": 2, "booked": 3}
    return min(mine, key=lambda a: (-a["delay_hours"], phase_rank.get(a["phase"], 9), a["id"]))


def build(board: dict, context, route: dict) -> dict:
    """The decision graph for one lane: the root, the paths, the recommendation."""
    from engine.fleet import assets as assets_mod  # noqa: PLC0415
    from engine.fleet import reroute as reroute_mod  # noqa: PLC0415
    from engine.fleet import split as split_mod  # noqa: PLC0415

    config = context.config
    route_id = route["route_id"]
    level = route["level"]
    respond = RESPONSE_HOURS.get(level, 24.0)
    response = route.get("response") or {}

    supply = _team(config, "FN_SUPPLY_CHAIN")
    procurement = _team(config, "FN_PROCUREMENT")
    manufacturing = _team(config, "FN_MANUFACTURING")
    controlling = _team(
        config, (config.contacts.get("approval") or {}).get("approver", "FN_CONTROLLING"))
    customer_service = _team(config, "FN_CUSTOMER_SERVICE")
    manager = _person(response.get("route_manager"), supply)

    lead = lead_consignment(board, context, route_id)
    detail = assets_mod.asset_detail(board, context, lead["id"]) if lead else None
    routes = (reroute_mod.recovery(board, context, lead["id"], force=True)
              if lead else None)
    candidates = (routes or {}).get("candidates") or []
    disruption = (routes or {}).get("disruption") or {}

    # --- what the blocks say about the freight ----------------------------
    vehicle: list[dict] = []
    if detail:
        a = detail["asset"]
        vehicle = [
            {"label": "Consignment", "value": f"{detail['shipment_id']} · {detail['logistics']['customer']}"},
            {"label": a["mode"].title(), "value": f"{a['name']} ({a['asset_id']})"},
            {"label": a["crew"]["role"], "value": a["crew"]["name"]
                + ("" if a["crew"].get("verified") else " (synthetic roster)")},
            {"label": "Position", "value": f"{detail['position']['lat']:.3f}, "
                f"{detail['position']['lon']:.3f} · {detail['position']['source']}"},
            {"label": "Leg", "value": f"{detail['leg']['from_name']} → {detail['leg']['to_name']}"},
            {"label": "Due", "value": _when(detail["logistics"]["committed"])},
        ]

    # --- the root: confirm on site ---------------------------------------
    site_contact = _site_contact(response, disruption, manager)
    root = Block(
        ROOT, None, "Confirm the situation on site",
        "Before anything moves, somebody who can see the freight confirms "
        "what the feeds say. A field report from /driver answers it too.",
        site_contact, respond,
        info=[
            {"label": "Event", "value": disruption.get("title") or route.get("reason", "")},
            {"label": "Where", "value": disruption.get("name") or "on the lane"},
            *vehicle,
        ],
    )

    # --- the paths --------------------------------------------------------
    approver, approval_note = _approver(config, candidates, controlling, manager)
    top = candidates[:3]
    route_lines = [
        {"label": c.get("badge") or f"#{c['rank']}",
         "value": f"{c['label']}: {c['delta']['text']}"
                  + (f", CO₂e {c['delta']['co2e_pct']:+.0f}%"
                     if c["delta"].get("co2e_pct") is not None else "")}
        for c in top
    ]
    best = candidates[0] if candidates else None

    reroute = Path(
        "reroute", "Reroute the shipment",
        "Move the freight onto a different route (sea, rail or road) "
        "around the disruption.",
        [
            Block("reroute.inventory", "reroute", "Check inventory and stock cover",
                  "Whether the customer can wait decides how hard to push. "
                  "Stock at the destination buys days; none makes it urgent.",
                  manufacturing, respond,
                  info=_inventory_info(detail)),
            Block("reroute.routes", "reroute",
                  "Evaluate alternative sea, rail and road routes",
                  "Ranked on time, cost and risk; CO₂e alongside, never inside the score.",
                  manager, respond, info=route_lines or [
                      {"label": "Routes", "value": "No alternative route found for this disruption."}]),
            Block("reroute.coordinate", "reroute",
                  "Coordinate with Logistics and Procurement",
                  "An agreed route with no booked capacity is not a mitigation.",
                  procurement, procurement.get("sla_hours") or respond,
                  info=[{"label": "Logistics", "value": f"{supply['name']} · {supply.get('phone') or '—'}"},
                        *_carrier_info(response)]),
            Block("reroute.approve", "reroute", "Obtain approval",
                  approval_note, approver, approver.get("sla_hours") or respond,
                  info=[{"label": "Extra cost",
                         "value": _chf(best["delta"]["cost_chf"]) if best else "—"}]),
            Block("reroute.execute", "reroute", "Execute the reroute",
                  "Book it and tell the carrier; the undo window is the safety net.",
                  manager, respond,
                  info=[{"label": "Route", "value": best["label"] if best else "—"}]),
        ],
        available=bool(candidates),
        unavailable_reason=None if candidates else (
            (routes or {}).get("note") or "No alternative route was found."),
        basis=(f"{len(candidates)} alternative route(s); #1 is "
               f"{best['label'].lower()} ({best['delta']['text']})") if best else "",
    )

    ports = _port_options(context, route, candidates, disruption)
    alt_port = Path(
        "alt_port", "Use an alternative port",
        "Load or discharge somewhere else, and close the gap over land.",
        [
            Block("port.evaluate", "alt_port", "Evaluate alternative ports",
                  "Declared alternatives first; a land bridge where no sea route is left.",
                  manager, respond, info=ports["info"]),
            Block("port.agent", "alt_port", "Confirm berth and handling with the port agent",
                  "A port that cannot take the boxes this week is not an alternative.",
                  ports["agent"] or manager, respond,
                  info=[{"label": "Port", "value": ports["name"] or "—"}]),
            Block("port.rebook", "alt_port", "Rebook with the carrier",
                  "The sailing, or the drayage from the new port.",
                  procurement, procurement.get("sla_hours") or respond,
                  info=_carrier_info(response)),
            Block("port.approve", "alt_port", "Obtain approval", approval_note,
                  approver, approver.get("sla_hours") or respond, info=[]),
            Block("port.execute", "alt_port", "Execute the port change",
                  "Confirm the booking and update the customer's delivery address.",
                  manager, respond, info=[{"label": "Port", "value": ports["name"] or "—"}]),
        ],
        available=bool(ports["name"]),
        unavailable_reason=None if ports["name"] else ports["why_not"],
        basis=ports["basis"],
    )

    split_plan = None
    if lead and len((detail or {}).get("containers") or []) >= 2 and candidates:
        try:
            split_plan = split_mod.evaluate(board, context, lead["id"])
        except split_mod.SplitError:
            split_plan = None
    moved = (split_plan or {}).get("summary", {}).get("moved", 0)
    split = Path(
        "split", "Split the shipment",
        "Urgent containers take a faster route; the rest stay on the original asset.",
        [
            Block("split.identify", "split", "Identify the urgent containers",
                  "Only the boxes that miss their own deadline on the original asset.",
                  customer_service, customer_service.get("sla_hours") or respond,
                  info=[{"label": "Suggestion",
                         "value": (split_plan or {}).get("suggestion_reason") or "—"}]),
            Block("split.book", "split", "Book the fast route for them",
                  "Road is priced per truck. That is what makes a partial move pay.",
                  procurement, procurement.get("sla_hours") or respond,
                  info=_split_info(split_plan)),
            Block("split.approve", "split", "Obtain approval", approval_note,
                  approver, approver.get("sla_hours") or respond, info=[]),
            Block("split.execute", "split", "Execute the split",
                  "Two tracking lines from here on: one per branch.",
                  manager, respond, info=[]),
        ],
        available=moved > 0,
        unavailable_reason=None if moved > 0 else (
            (split_plan or {}).get("suggestion_reason")
            or "Nothing to split: one container, or no faster route."),
        basis=(split_plan or {}).get("suggestion_reason") or "",
    )

    hold = Path(
        "hold", "Hold and monitor",
        "Keep the plan, tell the customer early, and look again at a set time.",
        [
            Block("hold.notify", "hold", "Tell the customer before they ask",
                  "Always allowed, whatever else is decided. It costs nothing.",
                  customer_service, customer_service.get("sla_hours") or respond,
                  info=[{"label": "Revised ETA",
                         "value": _when((detail or {}).get("logistics", {}).get("eta_revised"))}]),
            Block("hold.checkpoint", "hold", "Set a review checkpoint",
                  "Holding is a decision with a deadline, not the absence of one.",
                  supply, respond,
                  info=[{"label": "Look again within", "value": f"{max(1, round(respond))} h"}]),
            Block("hold.close", "hold", "Record the decision and why",
                  "So the hindcast can tell whether waiting was right.",
                  supply, None, info=[]),
        ],
        basis="Always open. The right answer when no alternative beats the plan.",
    )

    paths = [reroute, alt_port, split, hold]
    recommended, why = _recommend(paths, candidates, split_plan, ports, level)
    return {
        "route_id": route_id,
        "level": level,
        "level_label": route.get("level_label", level),
        "response_hours": respond,
        "lead": lead and {"id": lead["id"], "name": lead["name"], "mode": lead["mode"]},
        "root": root.as_dict(),
        "paths": [p.as_dict() for p in paths],
        "recommended": recommended,
        "recommended_why": why,
    }


def _site_contact(response: dict, disruption: dict, manager: dict) -> dict:
    """Who can see it: a local partner at the disrupted node, else on the lane.

    A gauge or a strait has no partner standing on it; the nearest partner
    on this route is the next best pair of eyes, and the route manager the
    last resort.
    """
    vendors = response.get("vendors") or []
    node_id = disruption.get("node_id")
    at_node = [v for v in vendors if (v.get("meta") or {}).get("node") == node_id]
    if at_node:
        return _person(at_node[0], manager)
    return _person(vendors[0], manager) if vendors else manager


def _carrier_info(response: dict) -> list[dict]:
    return [
        {"label": "Carrier", "value": f"{c['name']} · {c.get('phone') or c.get('email') or '—'}"}
        for c in (response.get("carriers") or [])[:2]
    ]


def _inventory_info(detail: dict | None) -> list[dict]:
    if not detail:
        return []
    boxes = detail.get("containers") or []
    late = [b for b in boxes if not b.get("on_time_original")]
    critical = [b for b in boxes if b.get("priority") == "critical"]
    return [
        {"label": "Customer", "value": detail["logistics"]["customer"]},
        {"label": "Promised", "value": _when(detail["logistics"]["committed"])},
        {"label": "Revised ETA", "value": _when(detail["logistics"]["eta_revised"])},
        {"label": "Containers", "value": f"{len(boxes)} · {len(critical)} critical · {len(late)} late on the plan"},
    ]


def _split_info(plan: dict | None) -> list[dict]:
    if not plan:
        return []
    fast = [b for b in plan["branches"] if b["route_id"] != "ORIGINAL"]
    s = plan["summary"]
    rows = [{"label": "On time", "value": f"{s['on_time_before']} → {s['on_time_after']} of {s['containers']}"},
            {"label": "Extra cost", "value": _chf(s["extra_cost_chf"])}]
    if fast:
        rows.insert(0, {"label": "Fast route", "value": f"{fast[0]['label']} · {fast[0]['teu']} TEU"})
    return rows


def _approver(config, candidates: list[dict], controlling: dict, manager: dict
              ) -> tuple[dict, str]:
    """Controlling above the delegated limit, the lane owner below it."""
    limit = (config.contacts.get("approval") or {}).get("delegated_limit_chf")
    extra = candidates[0]["delta"]["cost_chf"] if candidates else 0.0
    if limit is not None and extra > float(limit):
        return controlling, (
            f"The extra cost exceeds the delegated limit of {_chf(float(limit))}, "
            "so Controlling signs it off.")
    return manager, (
        "Within the delegated limit"
        + (f" of {_chf(float(limit))}" if limit is not None else "")
        + ". The lane owner approves.")


def _port_options(context, route: dict, candidates: list[dict], disruption: dict) -> dict:
    nodes = context.config.nodes
    swaps = [c for c in candidates if c["kind"] == "port_swap"]
    local = context.config.contacts.get("local_vendors", {}) or {}
    name = None
    agent = None
    info: list[dict] = []
    if swaps:
        best = swaps[0]
        alt_id = next((leg["to"]["id"] for leg in best["legs"]
                       if leg["new"] and leg["mode"] == "sea"), None) or \
            next((leg["to"]["id"] for leg in best["legs"] if leg["new"]), None)
        name = nodes[alt_id].name if alt_id in nodes else best["label"]
        info = [{"label": c.get("badge") or f"#{c['rank']}", "value": f"{c['label']}: {c['delta']['text']}"}
                for c in swaps[:3]]
        if alt_id in local and local[alt_id]:
            v = local[alt_id][0]
            agent = {"name": v["name"], "role": v.get("service", "").replace("_", " "),
                     "phone": v.get("phone"), "email": v.get("email"), "sla_hours": None}
    else:
        node_id = disruption.get("node_id")
        node = nodes.get(node_id) if node_id else None
        alts = [nodes[a] for a in (node.alternatives if node else []) if a in nodes]
        seaports = [a for a in alts if a.kind.value == "seaport"]
        if node and node.kind.value == "seaport" and seaports:
            name = seaports[0].name
            info = [{"label": "Declared", "value": ", ".join(a.name for a in seaports)}]
            first = local.get(seaports[0].id) or []
            if first:
                agent = {"name": first[0]["name"], "role": first[0].get("service", ""),
                         "phone": first[0].get("phone"), "email": None, "sla_hours": None}
    why_not = (
        "The disruption is not at a port, or the port has no alternative configured."
        if not name else None)
    basis = f"{name} is the nearest workable alternative." if name else ""
    return {"name": name, "agent": agent, "info": info, "why_not": why_not, "basis": basis}


def _recommend(paths: list[Path], candidates: list[dict], split_plan: dict | None,
               ports: dict, level: str = "white") -> tuple[str, str]:
    """Which path Horizon suggests, from what the engine computed.

    In order: a split when it saves boxes without moving the whole load;
    a port swap when that is the best alternative; a reroute when an
    alternative beats staying; on a red lane, a declared alternative port
    rather than waiting; otherwise hold.
    """
    by_id = {p.path_id: p for p in paths}
    s = (split_plan or {}).get("summary") or {}
    if by_id["split"].available and 0 < s.get("moved", 0) < s.get("containers", 0) \
            and s.get("on_time_after", 0) > s.get("on_time_before", 0):
        return "split", (f"Moving {s['moved']} of {s['containers']} containers gets "
                         f"{s['on_time_after']} on time instead of {s['on_time_before']}, "
                         "and the rest can stay on the original asset.")
    best = candidates[0] if candidates else None
    if best and best.get("beats_original"):
        if best["kind"] == "port_swap" and by_id["alt_port"].available:
            return "alt_port", f"The best alternative is a port change: {best['label']}."
        return "reroute", f"{best['label']} scores better than staying ({best['delta']['text']})."
    if level == "red" and by_id["alt_port"].available:
        return "alt_port", (f"The lane is red and no route scores better than staying; "
                            f"{ports['name']} is a declared alternative worth checking now.")
    return "hold", ("No alternative scores better than the plan at the current weights, "
                    "so holding and telling the customer is the recommendation.")


# =====================================================================
# Evaluating it — pure
# =====================================================================


def evaluate(graph: dict, completed: set[str] | None = None,
             chosen: str | None = None, trail: list[dict] | None = None) -> dict:
    """Where the flow stands for these ticks and this choice.

    * The root is ACTIVE until done.
    * The path in force is the one chosen, or — until the planner chooses —
      the recommended one. Its first open step is ACTIVE once the root is
      done, or immediately if the planner chose a path before confirming
      (they are allowed to: the flow warns, it does not stop them).
    * Steps on the path in force after the active one are NEXT; steps on
      other paths are IDLE, keeping any that were done on them — switching
      path does not erase what was already done, and the trail shows it.
    """
    completed = set(completed or ())
    trail = list(trail or [])
    paths = {p["path_id"]: p for p in graph["paths"]}
    if chosen not in paths:
        chosen = None
    in_force = chosen or graph["recommended"]
    root_done = ROOT in completed

    root = dict(graph["root"], state=DONE if root_done else ACTIVE)

    out_paths = []
    for p in graph["paths"]:
        steps = []
        active_given = False
        live = p["path_id"] == in_force and (root_done or chosen is not None)
        for step in p["steps"]:
            if step["step_id"] in completed:
                state = DONE
            elif live and not active_given:
                state, active_given = ACTIVE, True
            elif p["path_id"] == in_force:
                state = NEXT
            else:
                state = IDLE
            steps.append(dict(step, state=state))
        done = sum(1 for s in steps if s["state"] == DONE)
        out_paths.append(dict(
            p, steps=steps, done=done, total=len(steps),
            in_force=p["path_id"] == in_force,
            chosen=p["path_id"] == chosen,
            recommended=p["path_id"] == graph["recommended"],
            complete=done == len(steps),
        ))

    current = next((p for p in out_paths if p["in_force"]), None)
    active = root if not root_done and chosen is None else next(
        (s for s in (current or {}).get("steps", []) if s["state"] == ACTIVE), None)
    caution = None
    if chosen and not root_done:
        caution = ("The situation on site has not been confirmed yet. You can act, "
                   "but you are acting on the feeds alone.")

    return {
        **{k: v for k, v in graph.items() if k not in ("root", "paths")},
        "root": root,
        "paths": out_paths,
        "in_force": in_force,
        "chosen": chosen,
        "active_step": active["step_id"] if active else None,
        "complete": bool(current and current["complete"] and root_done),
        "caution": caution,
        "trail": trail,
    }


def known_steps(graph: dict) -> set[str]:
    return {ROOT} | {s["step_id"] for p in graph["paths"] for s in p["steps"]}


def step_label(graph: dict, step_id: str) -> str:
    if step_id == ROOT:
        return graph["root"]["label"]
    for p in graph["paths"]:
        for s in p["steps"]:
            if s["step_id"] == step_id:
                return s["label"]
    return step_id


def path_label(graph: dict, path_id: str) -> str:
    return next((p["label"] for p in graph["paths"] if p["path_id"] == path_id), path_id)


__all__ = ["build", "evaluate", "known_steps", "step_label", "path_label", "ROOT",
           "DONE", "ACTIVE", "NEXT", "IDLE"]
