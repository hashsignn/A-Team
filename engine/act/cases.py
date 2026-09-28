"""Closed cases: the risk ledger's history.

A route with something on it is an open case. When the planner has dealt
with it (rerouted, split, told the customer, or found it absorbed), they
close it, and it goes into the risk ledger's history: what it was, how bad,
what was done, who closed it and when.

WHAT A CLOSE COVERS
-------------------
The route at the level it was closed at. A close does not silence the route
for good: if it gets WORSE later (a higher rung on the ladder), it is an open
case again, because the planner closed what they saw, not what had not
happened yet. Event ids are not the test: a continuing condition is
re-observed every day under a new id (the Kaub gauge reading is dated), and
that is the same case, not a new one. The history keeps the earlier close
either way, and the events it was closed on.

AS-OF
-----
A close is a wall-clock action, recorded with the board instant it was made
on. Replaying the board at an instant before that shows the case open, as it
was then; nothing here reads the clock, the API passes the time in.

WHERE IT LIVES
--------------
``data/cases.jsonl``, append-only, one record per line: ``close`` records and
``reopen`` records. Nothing is rewritten, so the history is its own audit
trail. It is operational data (it names customers and people), so it is
gitignored like the field reports. ``RADAR_CASE_LOG`` points it elsewhere.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from engine.clock import ensure_utc, parse_instant

# The ladder, least to most urgent: a route closed at one rung reopens only
# if it climbs above it.
LEVEL_RANK = {"green": 0, "white": 1, "blue": 2, "yellow": 3, "red": 4}

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = ROOT / "data" / "cases.jsonl"

# How a case ended. Short, because they are buttons.
OUTCOMES = {
    "rerouted": "Rerouted",
    "split": "Split",
    "other_port": "Other port",
    "customer_told": "Customer informed",
    "absorbed": "No impact",
    "other": "Other",
}

MAX_NOTE = 500
MAX_ACTOR = 60


class CaseError(ValueError):
    """A close or reopen that cannot be recorded, with the reason."""


def _log_path() -> Path:
    override = os.environ.get("RADAR_CASE_LOG")
    return Path(override) if override else DEFAULT_LOG


def _append(record: dict, log: Path | None = None) -> dict:
    log = log or _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_all(log: Path | None = None) -> list[dict]:
    """Every record, oldest first. A damaged line is skipped, never fatal."""
    log = log or _log_path()
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("kind") in ("close", "reopen") and record.get("case_id"):
            out.append(record)
    return out


def _events(route: dict) -> list[dict]:
    return [{"event_id": e.get("event_id"), "title": e.get("title", "")}
            for e in route.get("events") or [] if e.get("event_id")]


def _site_name(route: dict) -> str | None:
    site = route.get("site")
    if isinstance(site, dict):
        return site.get("name")
    return site or None


def close(route: dict, *, outcome: str, note: str | None, actor: str | None,
          closed_at: datetime, as_of: datetime, log: Path | None = None) -> dict:
    """Close the case on ``route`` and write it to the history."""
    if outcome not in OUTCOMES:
        raise CaseError(f"outcome must be one of {', '.join(OUTCOMES)}")
    current = status_for(route, as_of, log)
    if current is not None:
        raise CaseError("this case is already closed")
    record = {
        "kind": "close",
        "case_id": uuid.uuid4().hex[:12],
        "route_id": route["route_id"],
        "route_name": route.get("name", route["route_id"]),
        "site": _site_name(route),
        "level": route.get("level"),
        "level_label": route.get("level_label"),
        "reason": route.get("reason", ""),
        "events": _events(route),
        "exposure_chf": route.get("exposure_chf"),
        "shipments": route.get("shipments"),
        "shipments_at_risk": route.get("shipments_at_risk"),
        "outcome": outcome,
        "outcome_label": OUTCOMES[outcome],
        "note": (note or "").strip()[:MAX_NOTE] or None,
        "closed_by": (actor or "").strip()[:MAX_ACTOR] or "planner",
        "closed_at": ensure_utc(closed_at).isoformat(),
        "as_of": ensure_utc(as_of).isoformat(),
    }
    return _append(record, log)


def reopen(case_id: str, *, actor: str | None, at: datetime,
           log: Path | None = None) -> dict:
    """Take a close back. The close stays in the history, marked reopened."""
    records = read_all(log)
    closes = {r["case_id"]: r for r in records if r["kind"] == "close"}
    if case_id not in closes:
        raise CaseError(f"no closed case {case_id!r}")
    if any(r["kind"] == "reopen" and r["case_id"] == case_id for r in records):
        raise CaseError("this case is already open again")
    return _append({
        "kind": "reopen",
        "case_id": case_id,
        "route_id": closes[case_id]["route_id"],
        "reopened_by": (actor or "").strip()[:MAX_ACTOR] or "planner",
        "reopened_at": ensure_utc(at).isoformat(),
    }, log)


def history(log: Path | None = None) -> list[dict]:
    """Every close, newest first, each saying whether it was reopened."""
    records = read_all(log)
    reopened = {r["case_id"]: r for r in records if r["kind"] == "reopen"}
    out = []
    for record in records:
        if record["kind"] != "close":
            continue
        again = reopened.get(record["case_id"])
        out.append(record | {
            "reopened": again is not None,
            "reopened_at": again["reopened_at"] if again else None,
            "reopened_by": again["reopened_by"] if again else None,
        })
    out.sort(key=lambda r: r["closed_at"], reverse=True)
    return out


def status_for(route: dict, as_of: datetime, log: Path | None = None,
               records: list[dict] | None = None) -> dict | None:
    """The close that covers this route at this instant, or None if open.

    Covered means: closed on a board at or before ``as_of``, not reopened,
    and the route no more urgent now than when it was closed.
    """
    records = read_all(log) if records is None else records
    cutoff = ensure_utc(as_of)
    reopened = {r["case_id"] for r in records if r["kind"] == "reopen"}
    now_rank = LEVEL_RANK.get(route.get("level"), 0)
    latest = None
    for record in records:
        if (record["kind"] != "close" or record["route_id"] != route["route_id"]
                or record["case_id"] in reopened):
            continue
        if parse_instant(record["as_of"]) > cutoff:
            continue
        if now_rank > LEVEL_RANK.get(record.get("level"), 0):
            continue
        if latest is None or record["closed_at"] > latest["closed_at"]:
            latest = record
    return latest


def annotate(board: dict, as_of: datetime, log: Path | None = None) -> dict:
    """The board with each route's case state, without touching the cached
    board: a shallow copy with copied route rows."""
    records = read_all(log)
    routes = []
    for route in board.get("routes") or []:
        closed = status_for(route, as_of, records=records)
        routes.append(route | {"case": {
            "status": "closed",
            "case_id": closed["case_id"],
            "outcome": closed["outcome"],
            "outcome_label": closed["outcome_label"],
            "closed_at": closed["closed_at"],
            "closed_by": closed["closed_by"],
        } if closed else {"status": "open"}})
    return board | {"routes": routes, "case_outcomes": OUTCOMES}
