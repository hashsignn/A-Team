"""Solution A endpoints: one screen's worth of state, and one click to act.

Kept out of ``main.py`` because it is a different product surface with a
different contract, not a few more routes on the old one. The v1 endpoints
still serve the analytical board; nothing here changes them.

THE CONTRACT
------------
    GET  /api/v2/now             what needs a decision, headline first
    GET  /api/v2/route/{id}      one lane: delay now, and the options
    POST /api/v2/act             execute, immediately, no checklist
    POST /api/v2/undo            pull it back inside the window
    GET  /api/v2/stream          server-sent events off the bus
    POST /api/v2/hooks/incident  a carrier, TMS or port system pushes to us
    GET  /api/v2/outbox          what dispatch would have sent but could not

WHAT THE HOOK MAY AND MAY NOT DO
--------------------------------
An inbound hook can RAISE an incident and have the contingency options
computed against it in the same request. It cannot execute anything. That line
is where the safety of instant execution actually lives: the click is
instant, but the click is a person's. An endpoint that both accepts an
unverified assertion and spends money on it is a different product, and not
one anybody should ship.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from engine.act import console as console_mod
from engine.fast import dispatch as dispatch_mod
from engine.fast import execute as execute_mod
from engine.fast import view
from engine.fast.bus import BUS, TOPICS
from engine.fast.execute import LEDGER
from engine.fast.watch import WATCHER
from engine.ingest import reports as reports_mod

# Set this to require a token on the inbound hook. Unset, the hook still
# works — it is the only way to demo an integration offline — but everything
# it raises is marked unauthenticated, and the planner sees that on the card.
HOOK_TOKEN_ENV = "RADAR_HOOK_TOKEN"

router = APIRouter(prefix="/api/v2", tags=["fast"])

# Injected by main.py so this module does not own the run cache. Two caches of
# the same pipeline would drift, and the one that drifted would be the one on
# screen.
_context_for = None


def bind(context_getter) -> None:
    global _context_for
    _context_for = context_getter


def _ctx(as_of: str, shipments: int):
    if _context_for is None:  # pragma: no cover - wiring error, not a runtime path
        raise HTTPException(500, "fast routes are not bound to a run context")
    return _context_for(as_of, shipments)


def _now() -> datetime:
    """Real time, for things that happen now rather than as-of.

    Executions, dispatch receipts and undo windows are wall-clock events: they
    happen when the planner clicks, not at the pinned as-of the analysis runs
    at. Keeping the two apart is why ``engine/`` can stay reproducible while
    the action layer is live.
    """
    return datetime.now(UTC)


# ------------------------------------------------------------------ read
@router.get("/now")
def now(
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    context = _ctx(as_of, shipments)
    payload = view.build(
        context,
        incidents=[i.as_dict() for i in WATCHER.live()],
        ledger=LEDGER,
    )
    payload["server_time"] = _now().isoformat()
    payload["bus_seq"] = BUS.sequence
    return JSONResponse(payload)


@router.get("/route/{route_id}")
def route(
    route_id: str,
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    context = _ctx(as_of, shipments)
    detail = view.route_detail(context, route_id, ledger=LEDGER)
    if detail is None:
        raise HTTPException(404, f"no affected route {route_id!r}")
    detail["incidents"] = [
        i.as_dict() for i in WATCHER.live(50)
        if i.shipment_id in {
            s.shipment_id for s in context.shipments if s.lane_id == route_id
        }
    ]
    detail["server_time"] = _now().isoformat()
    return JSONResponse(detail)


@router.get("/incidents")
def incidents(limit: int = Query(20, ge=1, le=200)) -> JSONResponse:
    return JSONResponse({"incidents": [i.as_dict() for i in WATCHER.live(limit)]})


@router.get("/outbox")
def outbox() -> JSONResponse:
    """What dispatch recorded because it could not send.

    Exposed rather than buried: a demo where nothing is wired should be able to
    show exactly what would have gone out, and to whom.
    """
    return JSONResponse({"messages": dispatch_mod.OUTBOX.messages[-50:]})


# ------------------------------------------------------------------ act
@router.post("/act")
def act(
    payload: Annotated[dict, Body()],
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """Execute a grouped option across every consignment it applies to.

    No confirmation step, by design. The group is re-derived from the engine
    rather than taken from the request body: a client that sent an edited list
    of consignment ids would otherwise be executing against freight the engine
    never offered.
    """
    route_id = str(payload.get("route_id", "")).strip()
    option_id = str(payload.get("option_id", "")).strip()
    if not route_id or not option_id:
        raise HTTPException(400, "route_id and option_id are required")

    context = _ctx(as_of, shipments)
    options = view.option_by_id(context, route_id, option_id)
    if not options:
        raise HTTPException(
            404,
            f"option {option_id!r} is no longer on {route_id!r} — it has "
            "expired or the board has moved on",
        )

    moment = _now()
    confidence = str(payload.get("confidence") or "reported")
    trigger = str(payload.get("trigger") or "manual")
    by = str(payload.get("by") or "planner")

    done: list[dict] = []
    refused: list[dict] = []
    for option in options:
        result = execute_mod.execute(
            option, context.config, moment,
            by=by, trigger=trigger, confidence=confidence, ledger=LEDGER,
        )
        if isinstance(result, execute_mod.Refusal):
            refused.append({**result.as_dict(), "shipment_id": option.shipment_id})
        else:
            done.append(result.as_dict(moment))

    return JSONResponse(
        {
            "ok": bool(done),
            "executed": done,
            "refused": refused,
            "sentence": _act_sentence(done, refused),
            "server_time": moment.isoformat(),
        },
        status_code=200 if done else 409,
    )


def _act_sentence(done: list[dict], refused: list[dict]) -> str:
    if done and not refused:
        label = done[0]["label"]
        return f"{label} — running on {len(done)} consignment(s)."
    if done and refused:
        return (
            f"Running on {len(done)}; {len(refused)} refused "
            f"({refused[0].get('refused', 'see detail')})."
        )
    if refused:
        return f"Nothing ran: {refused[0].get('detail', 'refused')}."
    return "Nothing to do."


@router.post("/undo")
def undo(
    payload: Annotated[dict, Body()],
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    execution_ids = payload.get("execution_ids") or (
        [payload["execution_id"]] if payload.get("execution_id") else []
    )
    if not execution_ids:
        raise HTTPException(400, "execution_id or execution_ids is required")

    context = _ctx(as_of, shipments)
    moment = _now()
    undone: list[dict] = []
    refused: list[dict] = []

    for execution_id in execution_ids:
        result = execute_mod.undo(
            str(execution_id), context.config, moment, ledger=LEDGER
        )
        if isinstance(result, execute_mod.Refusal):
            refused.append({**result.as_dict(), "execution_id": execution_id})
        else:
            undone.append(result.as_dict(moment))

    return JSONResponse(
        {
            "ok": bool(undone),
            "undone": undone,
            "refused": refused,
            "server_time": moment.isoformat(),
        },
        status_code=200 if undone else 409,
    )


# ----------------------------------------------------------------- hooks
@router.post("/hooks/incident")
def hook_incident(
    payload: Annotated[dict, Body()],
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """A carrier, TMS or port system tells us something went wrong.

    The incident is raised and the contingency options for the affected lane
    are computed in the same request, so the planner's screen has the answer
    before they have finished reading the alert. Nothing is executed.
    """
    expected = os.environ.get(HOOK_TOKEN_ENV, "").strip()
    authenticated = False
    if expected:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        # Constant-time: a token check that returns early leaks the token one
        # character at a time to anyone willing to measure.
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "bad or missing hook token")
        authenticated = True

    moment = _now()
    incident = WATCHER.saw_hook(payload, moment)

    context = _ctx(as_of, shipments)
    lane_id = payload.get("route_id")
    if not lane_id and incident.shipment_id:
        lane_id = next(
            (s.lane_id for s in context.shipments
             if s.shipment_id == incident.shipment_id),
            None,
        )

    options: list[dict] = []
    if lane_id:
        detail = view.route_detail(context, str(lane_id), ledger=LEDGER)
        if detail:
            options = detail["options"]

    return JSONResponse(
        {
            "ok": True,
            "authenticated": authenticated,
            "incident": incident.as_dict(),
            "route_id": lane_id,
            "options": options,
            # Said out loud rather than implied: the caller asserted this and
            # we recorded that they asserted it.
            "note": (
                "Incident raised and options computed. Nothing has been "
                "executed — a person decides that."
            ),
        }
    )


# --------------------------------------------------------------- signals
@router.get("/signals")
def signals(
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """What came in, what the filter did with it, and what is reading it.

    The funnel was one line of small print under a table. Its whole argument —
    that a deterministic gate does the bulk of the work and a model only reads
    the survivors — was invisible, which meant the cost claim behind it was
    unauditable from the screen. This is that layer, made lookable-at.
    """
    from engine.ingest.sources import catalog as catalog_mod
    from engine.reason import cache as cache_mod
    from engine.reason import funnel as funnel_mod
    from engine.reason import llm as llm_mod

    context = _ctx(as_of, shipments)
    result = context.result
    f = result.funnel
    status = llm_mod.detect()

    # The funnel as it actually ran, each stage saying what it removed and by
    # what rule. A count with no rule beside it is a number nobody can argue
    # with, which is the same as a number nobody believes.
    stages = [
        {"key": "raw", "label": "Arrived", "count": f.raw_observations,
         "note": "Everything every connected source returned this run."},
        {"key": "geographic", "label": "Near our freight",
         "count": f.after_geographic,
         "removed": f.raw_observations - f.after_geographic,
         "note": "Outside the bounding box of every node we touch. Geometry, "
                 "not judgement."},
        {"key": "type", "label": "A kind that can hurt us",
         "count": f.after_type,
         "removed": f.after_geographic - f.after_type,
         "note": "No risk vocabulary matched. A named variable family or it "
                 "does not pass."},
        {"key": "temporal", "label": "While we are there",
         "count": f.after_temporal,
         "removed": f.after_type - f.after_temporal,
         "note": "The window does not overlap any leg's transit through the "
                 "node."},
        {"key": "resolution", "label": "Distinct events",
         "count": f.after_resolution,
         "removed": f.after_temporal - f.after_resolution,
         "note": "Many reports, one event. Clustered by place, kind and "
                 "window."},
        {"key": "reasoned", "label": "Read by a model", "count": f.reasoned,
         "note": ("Only these cost anything. The three filters above are "
                  "arithmetic and run whether or not a model is installed.")},
    ]

    rows: list[dict] = []
    for event in context.events:
        rows.append({
            "id": event.event_id,
            "title": event.title,
            "state": "event",
            "source": event.provenance.source,
            "tier": event.provenance.source_tier,
            "at": event.starts_at.isoformat(),
            "why": f"{event.event_class} · severity {event.severity.value}",
            "inferred": event.provenance.inferred,
        })
    for item_id, why in list(context.unpromoted.items())[:20]:
        rows.append({"id": item_id, "title": item_id, "state": "unpromoted",
                     "source": None, "tier": 3, "at": None, "why": why})
    for item_id, why in list(context.router_notes.items())[:20]:
        rows.append({"id": item_id, "title": item_id, "state": "dropped",
                     "source": None, "tier": None, "at": None, "why": why})

    sources = []
    for spec in catalog_mod.CATALOG:
        sources.append({
            "key": spec.key, "label": spec.label,
            "nature": spec.nature.value, "tier": spec.source_tier,
            "cost": spec.cost.value, "enabled": spec.enabled,
        })

    return JSONResponse({
        "as_of": result.as_of.isoformat(),
        "stages": stages,
        "signals": rows,
        "sources": sources,
        "model": {
            **llm_mod.report(status),
            "triage": llm_mod.TRIAGE_MODEL if status.available else None,
            "extract": llm_mod.EXTRACT_MODEL if status.available else None,
            "funnel_note": funnel_mod.report(
                funnel_mod.FunnelCost(), status)["note"],
            # A recording is a third state between "a model is reading this"
            # and "nothing is". It has to be visible as its own thing, or a
            # replayed board looks like a live one.
            "recording": cache_mod.report(),
        },
        "counts": {
            "events": len(context.events),
            "unpromoted": len(context.unpromoted),
            "dropped": len(context.router_notes),
            "shipments_touched": f.shipments_touched,
            "gated_hits": f.gated_hits,
        },
    })


# --------------------------------------------------------------- console
# Per-planner working state. "Has Maria acknowledged the amber on this step"
# is not a fact about the world, so it never reaches the engine and never
# changes what another planner's board says.
_REVIEWED: dict[str, set[str]] = {}
_LOGGED: dict[str, dict[str, dict]] = {}


def _lane_reports(context, route_id: str) -> list:
    """Field reports for this lane, filtered to the board's as-of.

    The as-of filter is what keeps a live feed compatible with a pinned
    board: replaying an earlier day gives that day's answer even though the
    log has grown since.
    """
    on_lane = {s.shipment_id for s in context.shipments if s.lane_id == route_id}
    return [
        r for r in reports_mod.as_of(context.clock.as_of)
        if r.shipment_id in on_lane
    ]


def _route_of(context, route_id: str) -> dict:
    from engine.export.board import build_board

    board = build_board(context)
    for row in board["routes"]:
        if row["route_id"] == route_id:
            return row
    raise HTTPException(404, f"no route {route_id!r}")


def _console(context, route_id: str) -> dict:
    route = _route_of(context, route_id)
    lane_shipments = {
        s.shipment_id for s in context.shipments if s.lane_id == route_id
    }
    executions = [
        e.as_dict(_now()) for e in LEDGER.recent(100)
        if e.shipment_id in lane_shipments
    ]
    payload = console_mod.build(
        context,
        route,
        reports=_lane_reports(context, route_id),
        executions=executions,
        reviewed=_REVIEWED.get(route_id, set()),
        logged=_LOGGED.get(route_id, {}),
    )
    payload["executions"] = executions
    payload["server_time"] = _now().isoformat()
    return payload


@router.get("/console/{route_id}")
def console(
    route_id: str,
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """One lane's operations console: stages, steps, evidence and tools."""
    return JSONResponse(_console(_ctx(as_of, shipments), route_id))


@router.post("/console/{route_id}/tool")
def run_tool(
    route_id: str,
    payload: Annotated[dict, Body()],
    as_of: str = Query(...),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """Press a control. Every branch returns a RESULT, never just an ack.

    A tool that answers "ok" has done the same thing a checkbox did. Each one
    here comes back with the thing it produced — the options it found, the
    vendors it reached, the record it wrote — so the step fills in rather
    than ticking.
    """
    tool_id = str(payload.get("tool_id", "")).strip()
    step_id = str(payload.get("step_id", "")).strip()
    context = _ctx(as_of, shipments)
    moment = _now()

    reviewed = _REVIEWED.setdefault(route_id, set())
    logged = _LOGGED.setdefault(route_id, {})

    if tool_id == "review":
        # Acknowledging an amber step. The one click the flow asks for.
        reviewed.add(step_id)
        return JSONResponse({"ok": True, "kind": "review", "step_id": step_id,
                             "console": _console(context, route_id)})

    if tool_id.startswith("log."):
        entry = {"at": moment.isoformat(), "by": payload.get("by", "planner"),
                 "fields": payload.get("fields", {})}
        logged[step_id] = entry
        return JSONResponse({"ok": True, "kind": "log", "step_id": step_id,
                             "entry": entry,
                             "console": _console(context, route_id)})

    if tool_id == "find.alternates":
        detail = view.route_detail(context, route_id, ledger=LEDGER)
        options = detail["options"] if detail else []
        return JSONResponse({
            "ok": True, "kind": "options", "step_id": step_id,
            "options": options,
            "vetoed": detail["vetoed"] if detail else [],
            "sentence": (
                f"{len(options)} option(s) hold the date and pay for themselves, "
                "fastest first."
                if options else
                "Nothing on this lane both holds the date and pays for itself."
            ),
        })

    if tool_id == "show.ruled_out":
        detail = view.route_detail(context, route_id, ledger=LEDGER)
        vetoed = detail["vetoed"] if detail else []
        return JSONResponse({
            "ok": True, "kind": "options", "step_id": step_id,
            "options": [], "vetoed": vetoed,
            "sentence": (
                f"{len(vetoed)} option(s) were discarded for losing money."
                if vetoed else
                "Nothing was discarded on cost — every option found pays for "
                "itself."
            ),
        })

    if tool_id == "find.vendors":
        return JSONResponse({
            "ok": True, "kind": "vendors", "step_id": step_id,
            "vendors": _vendors_near(context, route_id),
        })

    if tool_id in ("request.position", "request.confirmation"):
        what = ("a position" if tool_id == "request.position"
                else "confirmation of the disruption")
        body = {
            "type": "request.field",
            "at": moment.isoformat(),
            "route_id": route_id,
            "asking_for": what,
            "shipments": sorted(
                s.shipment_id for s in context.shipments if s.lane_id == route_id
            )[:40],
        }
        receipts = dispatch_mod.fan_out(
            context.config, body, audiences={"driver", "site_agent", "ground_ops"}
        )
        return JSONResponse({
            "ok": True, "kind": "dispatch", "step_id": step_id,
            "dispatch": dispatch_mod.summarise(receipts),
        })

    # ---- scoped to ONE consignment ---------------------------------
    # "What if I want to escalate just one issue?" The lane-level answer is
    # the right one for a lane and the wrong one for a customer on the phone.
    shipment_id = str(payload.get("shipment_id", "")).strip()

    if tool_id == "options.one":
        result = view.options_for_shipment(context, shipment_id)
        if result is None:
            raise HTTPException(404, f"no consignment {shipment_id!r}")
        return JSONResponse({"ok": True, "kind": "options", "step_id": step_id,
                             "scope": shipment_id, **result})

    if tool_id == "escalate.one":
        return JSONResponse({
            "ok": True, "kind": "draft", "step_id": step_id,
            "scope": shipment_id,
            "draft": _consignment_draft(context, route_id, shipment_id),
        })

    if tool_id == "locate.one":
        body = {
            "type": "request.field",
            "at": moment.isoformat(),
            "route_id": route_id,
            "asking_for": "a position and status",
            "shipments": [shipment_id],
        }
        receipts = dispatch_mod.fan_out(
            context.config, body, audiences={"driver", "site_agent"}
        )
        return JSONResponse({
            "ok": True, "kind": "dispatch", "step_id": step_id,
            "scope": shipment_id,
            "dispatch": dispatch_mod.summarise(receipts),
        })

    if tool_id == "compose.customer":
        return JSONResponse({
            "ok": True, "kind": "draft", "step_id": step_id,
            "draft": _customer_draft(context, route_id),
        })

    if tool_id == "build.record":
        record = _record(context, route_id, logged, moment)
        logged["close.record"] = {"at": moment.isoformat(), "fields": {}}
        return JSONResponse({"ok": True, "kind": "record", "step_id": step_id,
                             "record": record,
                             "console": _console(context, route_id)})

    raise HTTPException(400, f"unknown tool {tool_id!r}")


def _vendors_near(context, route_id: str) -> list[dict]:
    """3PLs within reach of the freight on this lane."""
    from engine.fast import contingency

    route = _route_of(context, route_id)
    seen: dict[str, dict] = {}
    for node_id in route.get("node_ids", []):
        if node_id not in context.network.nodes:
            continue
        value = max(
            (s.value_chf for s in context.shipments if s.lane_id == route_id),
            default=0.0,
        )
        for vendor in contingency.local_options(
            context.config, context.network, context.network.point(node_id), value
        ):
            key = f"{vendor.vendor}@{vendor.at_node}"
            seen.setdefault(key, {
                "vendor": vendor.vendor,
                "service": vendor.service,
                "phone": vendor.phone,
                "at": vendor.at_node_name,
                "distance_km": vendor.distance_km,
                "ready_in_hours": vendor.handover_hours,
                "cost_chf": vendor.cost_chf,
            })
    return sorted(seen.values(), key=lambda v: v["distance_km"])


def _customer_draft(context, route_id: str) -> dict:
    """A notice written from the board's own numbers, not from a template."""
    route = _route_of(context, route_id)
    detail = view.route_detail(context, route_id, ledger=LEDGER)
    best = detail["best"] if detail else None
    customers = sorted({
        a["customer"] for a in route.get("actions", []) if a.get("customer")
    })

    if best and best.get("on_time"):
        outcome = (
            f"We are moving it: {best['label']}. On the current plan the "
            "delivery date still holds."
        )
    elif best:
        outcome = (
            f"The fastest option left is {best['label']}, which still lands "
            f"{best['days_late_after']:.1f} day(s) late. We would like to "
            "re-agree the date."
        )
    else:
        outcome = (
            "No reroute on this lane both holds the date and pays for itself, "
            "so we would like to re-agree the date."
        )

    return {
        "to": customers,
        "subject": f"{route['level_label']} — {route['name']}",
        "body": (
            f"{route['reason']}\n\n{outcome}\n\n"
            f"Affected: {route.get('shipments_at_risk')} of "
            f"{route.get('shipments')} consignments on this lane.\n"
            "We will come back to you as soon as anything changes."
        ),
    }


def _consignment_draft(context, route_id: str, shipment_id: str) -> dict:
    """An escalation for ONE consignment, with its own numbers.

    Not the lane notice with a shipment id appended: this customer's value,
    this consignment's committed date, and the option that is actually open
    to it — which is frequently not the one the lane as a whole is taking.
    """
    shipment = next(
        (s for s in context.shipments if s.shipment_id == shipment_id), None
    )
    if shipment is None:
        raise HTTPException(404, f"no consignment {shipment_id!r}")

    route = _route_of(context, route_id)
    ranked = view.options_for_shipment(context, shipment_id) or {}
    best = (ranked.get("options") or [None])[0]

    if best and best.get("on_time"):
        outcome = (
            f"We are moving it: {best['label']}, set running within "
            f"{best['hours_to_resolve']:.0f} h. The agreed date still holds."
        )
    elif best:
        outcome = (
            f"The fastest option left is {best['label']}, which still lands "
            f"{best['days_late_after']:.1f} day(s) after the agreed date. "
            "We would like to re-agree it."
        )
    else:
        outcome = (
            "No option on this consignment both holds the date and pays for "
            "itself. We would like to re-agree the date."
        )

    committed = shipment.otif_committed_date.strftime("%d %b %Y")
    return {
        "to": [shipment.customer],
        "subject": (
            f"{shipment.shipment_id} — {route['level_label']} on "
            f"{route['name']}"
        ),
        "body": (
            f"{route['reason']}\n\n"
            f"Consignment {shipment.shipment_id}, value CHF "
            f"{shipment.value_chf:,.0f}, committed {committed}.\n\n"
            f"{outcome}\n\n"
            "We will come back to you as soon as anything changes."
        ),
        "scope": shipment.shipment_id,
    }


def _record(context, route_id: str, logged: dict, moment) -> dict:
    """The close-out, assembled rather than typed.

    Everything in it already happened somewhere this server can see: what ran
    is in the ledger, what was seen is in the report log, what was decided by
    hand is in the log entries. Asking a planner to retype any of it is how a
    record ends up written a week later from memory, or not at all.
    """
    route = _route_of(context, route_id)
    lane_shipments = {
        s.shipment_id for s in context.shipments if s.lane_id == route_id
    }
    executed = [
        e.as_dict(moment) for e in LEDGER.recent(200)
        if e.shipment_id in lane_shipments
    ]
    live = [e for e in executed if not e["undone"]]
    spend = round(sum(e["cost_chf"] for e in live), 2)
    reports = _lane_reports(context, route_id)

    return {
        "written_at": moment.isoformat(),
        "route_id": route_id,
        "route_name": route["name"],
        "level": route["level_label"],
        "cause": route["reason"],
        "actions_taken": [
            {"action": e["label"], "shipment_id": e["shipment_id"],
             "cost_chf": e["cost_chf"], "at": e["executed_at"],
             "confidence": e["confidence"]}
            for e in live
        ],
        "actions_pulled_back": [
            {"action": e["label"], "shipment_id": e["shipment_id"],
             "at": e["undone_at"], "reason": e["undo_reason"]}
            for e in executed if e["undone"]
        ],
        "total_spend_chf": spend,
        "evidence_from_the_road": [
            {"shipment_id": r.shipment_id, "at": r.observed_at.isoformat(),
             "status": r.status, "by": r.reported_by,
             "first_hand": r.first_hand, "authenticated": r.authenticated}
            for r in sorted(reports, key=lambda r: r.observed_at)[-10:]
        ],
        "decisions_logged_by_hand": [
            {"step": step_id, **entry} for step_id, entry in logged.items()
        ],
        "consignments_at_risk": route.get("shipments_at_risk"),
        "exposure_chf": route.get("exposure_chf"),
        "note": (
            "Assembled from the execution ledger, the field-report log and "
            "the decisions logged on this page. Nothing here was retyped."
        ),
    }


# ---------------------------------------------------------------- stream
@router.get("/stream")
async def stream(request: Request, topics: str = Query("")) -> StreamingResponse:
    """Server-sent events off the bus.

    SSE rather than websockets, for the same reason the v1 report stream uses
    it: it is one-directional, it reconnects by itself, and it costs no new
    dependency. Commands go back over POST, which is what they are.
    """
    wanted = [t for t in topics.split(",") if t in TOPICS] or list(TOPICS)

    async def events():
        loop = asyncio.get_running_loop()
        sub = BUS.stream(wanted, loop=loop)
        ready = {"topics": wanted, "seq": BUS.sequence}
        yield f"event: ready\ndata: {json.dumps(ready)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await BUS.drain(sub, timeout=1.0)
                if message is None:
                    # Keeps proxies from closing an idle stream, and tells the
                    # client the connection is alive rather than stalled.
                    yield ": keep-alive\n\n"
                    continue
                yield (
                    f"event: {message.topic}\n"
                    f"data: {json.dumps(message.as_dict())}\n\n"
                )
        finally:
            BUS.release(sub)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
