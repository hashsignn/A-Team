"""FastAPI app — thin. All logic lives in engine/ (BRIEF §9.1 rule 1).

This module does three things and nothing else: run the pipeline, hand the
board back as JSON, and serve the static files. Every number on screen was
computed in ``engine/`` and can be reproduced from the command line without a
server running.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine.clock import Clock
from engine.config import load_config
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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "cached_runs": len(_CACHE)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
