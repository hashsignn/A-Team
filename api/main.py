"""FastAPI app — thin. All logic lives in engine/ (BRIEF §9.1 rule 1).

This module does three things and nothing else: run the pipeline, hand the
board back as JSON, and serve the static files. Every number on screen was
computed in ``engine/`` and can be reproduced from the command line without a
server running.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from engine.clock import Clock
from engine.config import load_config
from engine.export import profile as profile_mod
from engine.export import report as report_mod
from engine.export.board import build_board
from engine.pipeline import RunContext, RunOptions, run
from engine.reason import ask as ask_mod
from engine.reason import llm as llm_mod

STATIC = Path(__file__).resolve().parent / "static"

# The demo runs at a pinned instant so what you rehearse is what happens on
# stage. Nothing in engine/ reads the wall clock; the as-of is always explicit.
DEFAULT_AS_OF = "2026-09-18T06:00:00+00:00"

app = FastAPI(title="Supply Chain Risk Radar", docs_url="/api/docs")

# A full pipeline run is a 10k-draw Monte Carlo over the whole book. Both the
# board and the profile are pure functions of (as_of, shipment_count, config),
# so they are cached on that key — without this, every page load re-runs the
# simulation. Editing the profile changes the config, so it clears both.
_RUNS: dict[tuple[str, int], RunContext] = {}
_BOARDS: dict[tuple[str, int], dict] = {}


def _context(as_of: str, shipments: int) -> RunContext:
    key = (as_of, shipments)
    if key not in _RUNS:
        try:
            clock = Clock.at(as_of)
        except ValueError as exc:
            raise HTTPException(400, f"bad as_of: {exc}") from exc
        _RUNS[key] = run(
            clock=clock,
            config=load_config(),
            options=RunOptions(shipment_count=shipments),
        )
    return _RUNS[key]


def _board(as_of: str, shipments: int) -> dict:
    key = (as_of, shipments)
    if key not in _BOARDS:
        _BOARDS[key] = build_board(_context(as_of, shipments))
    return _BOARDS[key]


def _invalidate() -> None:
    """Everything downstream of the config is now stale.

    Called on every write to the profile. The alternative — recomputing only
    what changed — would mean tracking which cached numbers depend on which
    setting, and getting that wrong shows a planner a board that predates their
    own edit.
    """
    _RUNS.clear()
    _BOARDS.clear()


@app.get("/api/board")
def board(
    as_of: str = Query(DEFAULT_AS_OF),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """Everything the UI draws: nodes, routes, radar data, ranking, posture."""
    return JSONResponse(_board(as_of, shipments))


def _route(board: dict, route_id: str) -> dict:
    for route in board["routes"]:
        if route["route_id"] == route_id:
            return route
    raise HTTPException(404, f"unknown route {route_id!r}")


@app.get("/api/report/{route_id}.pdf")
def report_pdf(
    route_id: str,
    as_of: str = Query(DEFAULT_AS_OF),
    shipments: int = Query(150, ge=20, le=400),
) -> Response:
    """The convening pack for one route.

    Not a customer handout: it is the evidence that justifies pulling the
    standing teams out of their weekly cycle, so it leads with the deadline
    and who is being asked to convene.
    """
    board = _board(as_of, shipments)
    route = _route(board, route_id)
    data = report_mod.build_pdf(route, board["as_of_label"], board["posture"])
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="{report_mod.filename(route, as_of)}"'
        },
    )


@app.get("/api/report/{route_id}.txt")
def report_summary(
    route_id: str,
    as_of: str = Query(DEFAULT_AS_OF),
    shipments: int = Query(150, ge=20, le=400),
) -> Response:
    """Plain-text summary, for pasting into mail or chat.

    The app composes; it does not send. Sending is an outward-facing action
    and no mail path is wired, so the planner sends — see the socket note in
    engine/export/report.py.
    """
    board = _board(as_of, shipments)
    route = _route(board, route_id)
    text = report_mod.build_summary(route, board["as_of_label"], board["posture"])
    return Response(content=text, media_type="text/plain; charset=utf-8")


# =====================================================================
# The planner's risk profile
# =====================================================================
# Not a separate store: the profile IS the config, rendered readable. Saving
# writes an overlay into config/ (gitignored); config.example/ stays pristine.


@app.get("/api/profile")
def profile(
    as_of: str = Query(DEFAULT_AS_OF),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """Desk, network, risk ledger, appetite, response and sources."""
    return JSONResponse(profile_mod.build_profile(_context(as_of, shipments)))


@app.post("/api/profile")
def profile_save(edits: Annotated[dict, Body()]) -> JSONResponse:
    """Apply allow-listed edits to the appetite settings.

    Returns what was applied, what was rejected and why. A 422 means nothing
    was written — the settings would have made a rung of the ladder
    unreachable, and saving them silently is the failure mode worth avoiding.
    """
    outcome = profile_mod.apply_edits(load_config(), edits)
    if outcome.get("problems"):
        return JSONResponse(outcome, status_code=422)
    if outcome["applied"]:
        _invalidate()
    return JSONResponse(outcome)


@app.delete("/api/profile")
def profile_reset() -> JSONResponse:
    """Drop the customer overlay and fall back to the committed stand-in."""
    outcome = profile_mod.clear_overlay()
    if outcome["removed"]:
        _invalidate()
    return JSONResponse(outcome)


# =====================================================================
# The assistant
# =====================================================================
# Grounded in the board and nothing else. With no model reachable this
# returns answered=false plus what connecting one would unlock — the same
# socket shape every other absent input uses. It is never an error: the whole
# board is computed without a model and is unaffected by its absence.


@app.post("/api/ask")
def ask(
    payload: Annotated[dict, Body()],
    as_of: str = Query(DEFAULT_AS_OF),
    shipments: int = Query(150, ge=20, le=400),
) -> JSONResponse:
    """Ask about one event, or about the board."""
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question is required")
    if len(question) > 2000:
        raise HTTPException(400, "question is too long")

    board = _board(as_of, shipments)
    event_id = payload.get("event_id")
    if event_id:
        return JSONResponse(ask_mod.event_question(board, str(event_id), question))
    return JSONResponse(
        ask_mod.board_question(board, question, payload.get("route_id"))
    )


@app.get("/api/model")
def model_status() -> JSONResponse:
    """What is running, so the UI can say so rather than fail silently."""
    return JSONResponse(llm_mod.report())


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "cached_runs": len(_BOARDS)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/profile")
def profile_page() -> FileResponse:
    return FileResponse(STATIC / "profile.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
