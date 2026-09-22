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

from engine.fast import dispatch as dispatch_mod
from engine.fast import execute as execute_mod
from engine.fast import view
from engine.fast.bus import BUS, TOPICS
from engine.fast.execute import LEDGER
from engine.fast.watch import WATCHER

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
