"""The operations console: every step does the thing it names.

The playbook this replaces was a to-do list. "Read the event and what it
touches" was a checkbox, and ticking it did not show you the event — you
ticked a box asserting you had read something the page never offered to open.
Everything after it had the same shape: a line of text, an owner, and a
square. That is a printed procedure with a cursor, and it is exactly the
"tedious, multi-step checklist" the rebuild exists to remove.

So a step here is not a claim. It is a control.

    STEP        what has to be true
    EVIDENCE    what already makes it true, found rather than asserted
    TOOLS       the controls that make it true if nothing has yet

WHERE THE TICKS COME FROM
-------------------------
Three places, and only one of them is a person:

    automatic   the engine already knows. Two tier-1 sources agree, a driver
                filed a position, a revised ETA arrived. These land as
                evidence and the step goes AMBER — satisfied, awaiting a
                look. Not green: somebody should see what was decided for
                them, and an amber marker is how they find it.
    executed    a tool ran. The reroute was dispatched, the customer was
                notified, the call was logged. These go green, because the
                system watched it happen.
    reviewed    the planner acknowledged an amber step. One click, and it is
                the only click the flow asks for.

WHY THE GATE STAYED
-------------------
It did not. Rerouting is no longer locked behind Confirm. What survives is a
WARNING with the evidence attached, so a planner rerouting on an unconfirmed
report is told what they are acting on rather than stopped — the same trade
``engine/fast/execute.py`` makes, for the same reason. The undo window is the
safety net, not the checklist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.act import flow as flow_mod
from engine.act.flow import STAGE_ORDER, STAGE_TITLE, Stage

# How a step reads on screen.
OPEN = "open"            # nothing satisfies it yet
EVIDENCE = "evidence"    # satisfied by something that arrived — amber, review it
DONE = "done"            # a tool ran, or the planner acknowledged the evidence

# What a tool does when you press it, which is what decides how it is drawn.
REVEAL = "reveal"    # opens data this page already has — no round trip
RUN = "run"          # asks the server to do something and returns a result
LOG = "log"          # records a fact the planner supplies
LINK = "link"        # leaves for another page


@dataclass(frozen=True)
class Tool:
    """One control on one step."""

    tool_id: str
    label: str
    kind: str
    hint: str = ""
    primary: bool = False
    data_key: str | None = None   # for REVEAL: which key of the step's data
    fields: tuple[dict, ...] = ()  # for LOG: what the planner has to supply
    href: str | None = None        # for LINK

    def as_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "label": self.label,
            "kind": self.kind,
            "hint": self.hint,
            "primary": self.primary,
            "data_key": self.data_key,
            "fields": [dict(f) for f in self.fields],
            "href": self.href,
        }


@dataclass
class Step:
    step_id: str
    stage: Stage
    label: str
    note: str
    owner: str
    owner_contact: str | None
    sla_hours: float | None
    required: bool
    state: str = OPEN
    evidence: list[dict] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "stage": self.stage.value,
            "label": self.label,
            "note": self.note,
            "owner": self.owner,
            "owner_contact": self.owner_contact,
            "sla_hours": self.sla_hours,
            "required": self.required,
            "state": self.state,
            "evidence": self.evidence,
            "tools": [t.as_dict() for t in self.tools],
            "data": self.data,
        }


# ---------------------------------------------------------------------
def _event_rows(route: dict) -> list[dict]:
    """What "read the event" actually opens.

    Provenance first, because the question a planner has about an event they
    did not see happen is "who says so" — and an event with one tier-3 source
    is a different object from one two agencies agree on.
    """
    rows = []
    for event in route.get("events", []):
        rows.append({
            "event_id": event.get("event_id"),
            "title": event.get("title"),
            "severity": event.get("severity"),
            "starts_at": event.get("starts_at"),
            "ends_at": event.get("ends_at"),
            "source": event.get("source"),
            "source_tier": event.get("source_tier"),
            "quote": event.get("quote"),
            "inferred": event.get("inferred"),
            "probability": event.get("probability"),
            "probability_basis": event.get("probability_basis"),
            "shipments_here": event.get("shipments_here"),
            "exposure_chf": event.get("exposure_chf"),
            "realized": event.get("realized"),
        })
    return rows


def _at_risk_ids(context, route_id: str) -> set[str]:
    """Which consignments the gate actually touched on this lane.

    NOT derived from ``route["actions"]``. That list is capped at twelve and
    only carries options worth taking, so a lane with fifteen affected
    consignments and three hopeless ones reports nine — and the page then
    said "9 of 28" two lines under a header saying "11 / 28". Two numbers for
    one fact, a hand-span apart, is how a planner stops believing either.
    """
    on_lane = {s.shipment_id for s in context.shipments if s.lane_id == route_id}
    return {
        risk.shipment_id
        for assessment in context.result.assessments
        for risk in assessment.shipment_risks
        if risk.shipment_id in on_lane
    }


def _consignment_rows(route: dict, context) -> list[dict]:
    """Every consignment on the lane, with its OWN deadline.

    The old step said "9 of the shipments on this lane are in scope — they do
    not all have the same deadline" and then showed none of them, which is a
    sentence describing a table instead of a table.
    """
    best = {a["shipment_id"]: a for a in route.get("actions", [])}
    at_risk = _at_risk_ids(context, route["route_id"])
    rows = []
    for shipment in context.shipments:
        if shipment.lane_id != route["route_id"]:
            continue
        hit = best.get(shipment.shipment_id)
        touched = shipment.shipment_id in at_risk
        rows.append({
            "shipment_id": shipment.shipment_id,
            "customer": shipment.customer,
            "value_chf": round(shipment.value_chf, 2),
            "eta": shipment.eta.isoformat(),
            "committed": shipment.otif_committed_date.isoformat(),
            "at_risk": touched,
            "lead_time_hours": hit.get("lead_time_hours") if hit else None,
            # None when the consignment is hit but nothing is worth doing —
            # which is a different answer from "on plan" and has to read that
            # way on screen.
            "best_action": hit.get("label") if hit else None,
        })
    rows.sort(key=lambda r: (not r["at_risk"], r["lead_time_hours"] is None,
                             r["lead_time_hours"] or 0))
    return rows


def _position_rows(reports: list) -> list[dict]:
    """The last thing anybody actually saw, per consignment."""
    latest: dict[str, Any] = {}
    for report in sorted(reports, key=lambda r: r.observed_at):
        latest[report.shipment_id] = report
    rows = []
    for shipment_id, report in sorted(latest.items()):
        rows.append({
            "shipment_id": shipment_id,
            "observed_at": report.observed_at.isoformat(),
            "status": report.status,
            "position": report.position,
            "lat": report.lat,
            "lon": report.lon,
            "load_state": report.load_state,
            "revised_eta": report.revised_eta.isoformat() if report.revised_eta else None,
            "reported_by": report.reported_by,
            "role": report.role,
            "first_hand": report.first_hand,
            "authenticated": report.authenticated,
            "photos": list(report.photos),
            "note": report.note,
        })
    return rows


def _corroboration_evidence(route: dict) -> list[dict]:
    """Sources that already agree, which is what "confirm" was asking for."""
    tiers: dict[int, list[str]] = {}
    for event in route.get("events", []):
        tier = event.get("source_tier")
        if tier is None:
            continue
        tiers.setdefault(int(tier), []).append(
            f"{event.get('source', 'unnamed source')} — {event.get('title', '')}"
        )
    out = []
    for tier in sorted(tiers):
        for line in tiers[tier]:
            out.append({
                "kind": "source",
                "tier": tier,
                "text": line,
                "weight": "official" if tier <= 1 else "secondary",
            })
    return out


def _report_evidence(reports: list, predicate, label: str) -> list[dict]:
    out = []
    for report in sorted(reports, key=lambda r: r.observed_at, reverse=True):
        if not predicate(report):
            continue
        who = report.reported_by or "somebody with the consignment"
        out.append({
            "kind": "report",
            "text": f"{label}: {who} on {report.shipment_id}",
            "detail": report.note or report.position or "",
            "at": report.observed_at.isoformat(),
            "weight": "first hand" if report.first_hand else "relayed",
            "authenticated": report.authenticated,
            "report_id": report.report_id,
        })
    return out[:4]


# ---------------------------------------------------------------------
def build(
    context,
    route: dict,
    reports: list,
    executions: list | None = None,
    reviewed: set[str] | None = None,
    logged: dict[str, dict] | None = None,
) -> dict:
    """The whole console for one lane."""
    reviewed = reviewed or set()
    logged = logged or {}
    executions = executions or []

    tiers = [
        e["source_tier"] for e in route.get("events", [])
        if e.get("source_tier") is not None
    ]
    tasks = {t.id: t for t in flow_mod.build(context.config, route, tiers)}
    route_id = route["route_id"]

    events = _event_rows(route)
    consignments = _consignment_rows(route, context)
    positions = _position_rows(reports)
    corroboration = _corroboration_evidence(route)

    def base(step_id: str) -> Step:
        task = tasks[step_id]
        return Step(
            step_id=step_id, stage=task.stage, label=task.label, note=task.note,
            owner=task.owner, owner_contact=task.owner_contact,
            sla_hours=task.sla_hours, required=task.required,
        )

    steps: list[Step] = []

    # ---- DETECT -----------------------------------------------------
    read = base("detect.read")
    read.data = {"events": events}
    read.evidence = [
        {"kind": "engine", "text": f"{len(events)} event(s) gated onto this lane",
         "weight": "computed"}
    ] if events else []
    read.tools = [
        Tool("show.event", "Open the event", REVEAL,
             "Provenance, window, and what it touches", primary=True,
             data_key="events"),
        Tool("ask.event", "Ask about it", LINK,
             "Put a question to the assistant, grounded in this board",
             href=f"/?ask={route_id}"),
    ]
    read.state = EVIDENCE if events else OPEN
    steps.append(read)

    scope = base("detect.scope")
    at_risk = [c for c in consignments if c["at_risk"]]
    scope.data = {"consignments": consignments}
    scope.evidence = [
        {"kind": "engine",
         "text": f"{len(at_risk)} of {len(consignments)} consignments in scope, "
                 f"each with its own deadline",
         "weight": "computed"}
    ]
    scope.tools = [
        Tool("show.consignments", "List them", REVEAL,
             "Every consignment, its customer, its deadline", primary=True,
             data_key="consignments"),
        Tool("open.route", "Open the lane page", LINK,
             "Matrix, charts and the per-consignment view",
             href=f"/route/{route_id}"),
    ]
    scope.state = EVIDENCE
    steps.append(scope)

    # ---- CONFIRM ----------------------------------------------------
    # This is the stage the complaint was really about. None of it should be
    # a person ticking a box: the sources are already in, the driver already
    # filed, and the engine already knows whether they agree.
    carrier = base("confirm.carrier")
    carrier_reports = _report_evidence(
        reports, lambda r: r.confirms_disruption and r.first_hand,
        "confirmed from the road")
    carrier.evidence = corroboration + carrier_reports
    carrier.data = {"sources": corroboration, "reports": carrier_reports}
    carrier.tools = [
        Tool("show.sources", "Show what agrees", REVEAL,
             "Every source, with its tier", primary=True, data_key="sources"),
        Tool("log.call", "Log a call", LOG,
             "Record that you spoke to the carrier",
             fields=({"name": "who", "label": "Spoke to", "type": "text"},
                     {"name": "outcome", "label": "What they said", "type": "text"})),
        Tool("request.confirmation", "Ask the road", RUN,
             "Push a request to whoever is with the freight"),
    ]
    official = [e for e in corroboration if e["weight"] == "official"]
    carrier.state = (
        DONE if "confirm.carrier" in logged
        else EVIDENCE if (len(official) >= 2 or carrier_reports)
        else OPEN
    )
    steps.append(carrier)

    position = base("confirm.position")
    with_fix = [p for p in positions if p["lat"] is not None]
    position.data = {"positions": positions}
    position.evidence = _report_evidence(
        reports, lambda r: r.lat is not None, "position filed")
    position.tools = [
        Tool("show.positions", "Show last seen", REVEAL,
             "Where each consignment was, and who said so", primary=True,
             data_key="positions"),
        Tool("request.position", "Request a position", RUN,
             "Dispatch a position request to the vehicles on this lane"),
    ]
    position.state = (
        DONE if "confirm.position" in logged
        else EVIDENCE if with_fix else OPEN
    )
    steps.append(position)

    eta = base("confirm.eta")
    revised = [p for p in positions if p["revised_eta"]]
    eta.data = {"revised": revised}
    eta.evidence = [
        {"kind": "report",
         "text": f"{p['shipment_id']} revised to {p['revised_eta']}",
         "weight": "first hand" if True else "relayed", "at": p["observed_at"]}
        for p in revised[:4]
    ]
    eta.tools = [
        Tool("show.eta", "Show revised arrivals", REVEAL,
             "What the road has already told us", primary=True, data_key="revised"),
        Tool("log.eta", "Enter a revised ETA", LOG,
             "For a consignment nobody has filed for",
             fields=({"name": "shipment_id", "label": "Consignment", "type": "text"},
                     {"name": "eta", "label": "New arrival (UTC)", "type": "datetime-local"})),
    ]
    eta.state = DONE if "confirm.eta" in logged else EVIDENCE if revised else OPEN
    steps.append(eta)

    # ---- ACT --------------------------------------------------------
    # A toolbox, not a tick. "Take the mitigation worth more than it costs"
    # was a box next to a sentence; the options were somewhere else on the
    # page and nothing connected the two.
    choose = base("act.choose")
    ran = [e for e in executions if not e.get("undone")]
    choose.data = {"options": []}   # filled by the API from the fast optimiser
    choose.evidence = [
        {"kind": "executed", "text": f"{e['label']} — {e['shipment_id']}",
         "at": e["executed_at"], "weight": "executed"}
        for e in ran[:5]
    ]
    choose.tools = [
        Tool("find.alternates", "Find alternate routes", RUN,
             "Search the network around the failed node, ranked by speed",
             primary=True),
        Tool("show.options", "Show current options", REVEAL,
             "Ranked fastest first, loss-making ones removed",
             data_key="options"),
    ]
    choose.state = DONE if ran else OPEN
    steps.append(choose)

    capacity = base("act.capacity")
    capacity.data = {"carriers": (route.get("response") or {}).get("carriers", []),
                     "vendors": (route.get("response") or {}).get("local_vendors", [])}
    capacity.tools = [
        Tool("show.capacity", "Who can take it", REVEAL,
             "Carriers on these modes and vendors at these nodes",
             primary=True, data_key="carriers"),
        Tool("find.vendors", "Find local vendors", RUN,
             "3PLs near the freight who can take over today"),
        Tool("log.booking", "Record a booking", LOG,
             "An agreed reroute with no booked slot is not a mitigation",
             fields=({"name": "carrier", "label": "Booked with", "type": "text"},
                     {"name": "reference", "label": "Reference", "type": "text"})),
    ]
    capacity.state = DONE if "act.capacity" in logged else OPEN
    steps.append(capacity)

    customer = base("act.customer")
    customer.data = {"contacts": (route.get("response") or {}).get("internal", [])}
    customer.tools = [
        Tool("compose.customer", "Compose the notice", RUN,
             "Draft it from the board's own numbers", primary=True),
        Tool("show.contacts", "Who to tell", REVEAL,
             "The teams on this route", data_key="contacts"),
    ]
    customer.state = DONE if "act.customer" in logged else OPEN
    steps.append(customer)

    # ---- CLOSE ------------------------------------------------------
    # Written by the system, not typed by the planner. Everything in it
    # already happened somewhere this page can see.
    record = base("close.record")
    record.data = {"record": None}
    record.evidence = [
        {"kind": "engine",
         "text": f"{len(ran)} executed action(s) and {len(reports)} field "
                 f"report(s) available to write up",
         "weight": "computed"}
    ]
    record.tools = [
        Tool("build.record", "Write the record", RUN,
             "Assembled from what actually ran — not typed twice",
             primary=True),
        Tool("export.record", "Download it", LINK,
             "The same pack the escalation sends",
             href=f"/api/report/{route_id}.pdf"),
    ]
    record.state = DONE if "close.record" in logged else OPEN
    steps.append(record)

    review = base("close.review")
    review.data = {}
    review.tools = [
        Tool("log.miss", "Log what it got wrong", LOG,
             "A missed event or a false alarm is worth more than a correct one",
             primary=True,
             fields=({"name": "kind", "label": "What kind",
                      "type": "select",
                      "options": ["missed event", "false alarm",
                                  "wrong deadline", "wrong option"]},
                     {"name": "detail", "label": "What happened", "type": "text"})),
    ]
    review.state = DONE if "close.review" in logged else OPEN
    steps.append(review)

    # A reviewed amber step is a done step.
    for step in steps:
        if step.state == EVIDENCE and step.step_id in reviewed:
            step.state = DONE

    return _assemble(steps, route)


def _assemble(steps: list[Step], route: dict) -> dict:
    """Stage rollup and the one number the rail across the top draws."""
    by_stage: dict[Stage, list[Step]] = {}
    for step in steps:
        by_stage.setdefault(step.stage, []).append(step)

    stages = []
    for stage in STAGE_ORDER:
        rows = by_stage.get(stage, [])
        done = sum(1 for s in rows if s.state == DONE)
        amber = sum(1 for s in rows if s.state == EVIDENCE)
        stages.append({
            "stage": stage.value,
            "title": STAGE_TITLE[stage],
            "steps": len(rows),
            "done": done,
            "evidence": amber,
            "progress": round(done / len(rows), 3) if rows else 0.0,
            "settled": bool(rows) and done == len(rows),
        })

    total = len(steps)
    done = sum(1 for s in steps if s.state == DONE)
    amber = sum(1 for s in steps if s.state == EVIDENCE)

    # Where the planner is: the first stage that is not settled.
    current = next(
        (s["stage"] for s in stages if not s["settled"]), STAGE_ORDER[-1].value
    )

    return {
        "route_id": route["route_id"],
        "route_name": route["name"],
        "level": route["level"],
        "level_label": route["level_label"],
        "directive": route["directive"],
        "reason": route["reason"],
        "lead_time_hours": route.get("lead_time_hours"),
        "exposure_chf": route.get("exposure_chf"),
        "shipments_at_risk": route.get("shipments_at_risk"),
        "shipments": route.get("shipments"),
        "stages": stages,
        "current_stage": current,
        "progress": round(done / total, 3) if total else 0.0,
        "counts": {"total": total, "done": done, "evidence": amber,
                   "open": total - done - amber},
        "steps": [s.as_dict() for s in steps],
        # No lock. A warning with the evidence attached, so a planner acting
        # on thin ground is told what they are acting on rather than stopped.
        "caution": _caution(steps),
    }


def _caution(steps: list[Step]) -> dict | None:
    confirm = [s for s in steps if s.stage is Stage.CONFIRM]
    unsettled = [s for s in confirm if s.state == OPEN]
    if not unsettled:
        return None
    return {
        "level": "warn",
        "text": (
            "Nothing has confirmed this yet from "
            + ", ".join(s.label.lower() for s in unsettled)
            + ". You can still act — the undo window is the safety net — but "
            "you are acting on a single unconfirmed signal."
        ),
    }
