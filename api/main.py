"""FastAPI app — thin. All logic lives in engine/ (BRIEF §9.1 rule 1).

This module does three things and nothing else: run the pipeline, hand the
board back as JSON, and serve the static files. Every number on screen was
computed in ``engine/`` and can be reproduced from the command line without a
server running.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from engine.clock import Clock
from engine.config import load_config
from engine.export import report as report_mod
from engine.export.board import build_board
from engine.pipeline import RunOptions, run

STATIC = Path(__file__).resolve().parent / "static"

# The demo runs at a pinned instant so what you rehearse is what happens on
# stage. Nothing in engine/ reads the wall clock; the as-of is always explicit.
DEFAULT_AS_OF = "2026-09-18T06:00:00+00:00"

app = FastAPI(title="Supply Chain Risk Radar", docs_url="/api/docs")

# A full pipeline run is a 10k-draw Monte Carlo over the whole book. The board
# is a pure function of (as_of, shipment_count), so it is cached on that key —
# without this, every page load re-runs the simulation.
_CACHE: dict[tuple[str, int], dict] = {}


def _board(as_of: str, shipments: int) -> dict:
    key = (as_of, shipments)
    if key not in _CACHE:
        try:
            clock = Clock.at(as_of)
        except ValueError as exc:
            raise HTTPException(400, f"bad as_of: {exc}") from exc
        context = run(
            clock=clock,
            config=load_config(),
            options=RunOptions(shipment_count=shipments),
        )
        _CACHE[key] = build_board(context)
    return _CACHE[key]


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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "cached_runs": len(_CACHE)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
