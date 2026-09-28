"""Who has confirmed the all-hands.

The all-hands brings Supply Chain, Procurement, Manufacturing and Controlling
(desk.yaml → all_hands.functions) into one room, and as a crisis looms it
meets daily. A meeting nobody confirmed is a meeting half the room misses, so
the board shows, for the NEXT sitting, which departments have confirmed and
which have not replied, and the ones that have not can be contacted from
there.

A reply is for one sitting, identified by its start time: the next sitting
starts with nobody confirmed. Replies are working state, not engine input:
they never change a level or a number.

``data/rsvp.jsonl``, append-only; the latest record per department and
sitting wins, so an undo is one more line. ``RADAR_RSVP_LOG`` points it
elsewhere.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from engine.clock import ensure_utc

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "data" / "rsvp.jsonl"

STATUSES = ("confirmed", "pending")


class RsvpError(ValueError):
    """A reply that cannot be recorded, with the reason."""


def _log_path() -> Path:
    override = os.environ.get("RADAR_RSVP_LOG")
    return Path(override) if override else DEFAULT_LOG


def read_all(log: Path | None = None) -> list[dict]:
    log = log or _log_path()
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and {"meeting_at", "function_id", "status"} <= set(record):
            out.append(record)
    return out


def record(meeting: dict, function_id: str, status: str, *, by: str | None,
           at: datetime, log: Path | None = None) -> dict:
    """Record one department's reply to the next sitting of ``meeting``."""
    if status not in STATUSES:
        raise RsvpError(f"status must be one of {', '.join(STATUSES)}")
    if not meeting.get("next_at"):
        raise RsvpError("no sitting is scheduled")
    known = {a["id"] for a in meeting.get("attendees") or []}
    if function_id not in known:
        raise RsvpError(f"{function_id!r} is not in the room")
    entry = {
        "meeting_at": meeting["next_at"],
        "function_id": function_id,
        "status": status,
        "by": (by or "").strip()[:60] or "planner",
        "at": ensure_utc(at).isoformat(),
    }
    log = log or _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def replies(meeting: dict, log: Path | None = None) -> dict:
    """Per department in the room: its reply to the next sitting."""
    at = meeting.get("next_at")
    latest: dict[str, dict] = {}
    for entry in read_all(log):
        if entry["meeting_at"] == at:
            latest[entry["function_id"]] = entry
    out = {}
    for attendee in meeting.get("attendees") or []:
        entry = latest.get(attendee["id"])
        out[attendee["id"]] = {
            "status": entry["status"] if entry else "pending",
            "at": entry["at"] if entry else None,
            "by": entry["by"] if entry else None,
        }
    return out


def annotate(board: dict, log: Path | None = None) -> dict:
    """The board with the replies on its all-hands, without touching the
    cached board."""
    meeting = board.get("all_hands")
    if not meeting:
        return board
    got = replies(meeting, log)
    confirmed = sum(1 for r in got.values() if r["status"] == "confirmed")
    return board | {"all_hands": meeting | {
        "rsvp": got, "confirmed": confirmed, "invited": len(got)}}
