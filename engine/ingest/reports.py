"""Field reports: what the person with the freight can tell us.

THE ONLY TIER-1 OBSERVED SOURCE IN THE SYSTEM
=============================================
Every other input here describes a REGION. Pegelonline says the Rhine is at
78 cm; trade press says Antwerp dockers have voted; a forecast says it will
blow. All of it is inference about whether your freight is affected.

A driver looking at their own trailer is not inference. "I am third in a
queue of forty at Kaub and the lock is shut" is an observation of the actual
consignment, and it beats every feed in this system — including the ones that
are not connected yet.

That is why this is tier 1 and why it is worth building before the paid APIs.

APPEND-ONLY, ALWAYS
-------------------
A corrected report is a NEW report, never an edit. Three reasons and all of
them bite:

* a driver who said "held" at 09:00 and "moving" at 11:00 has told us
  something a single mutable row would erase — the duration of the hold;
* an audit asking "what did we know at 10:00" needs the log as it stood at
  10:00, which an overwritten row cannot answer;
* the hindcast replays the log. An edited history makes every past board
  irreproducible, which would quietly destroy the one honest metric the
  project has.

THE AS-OF DISCIPLINE SURVIVES
-----------------------------
Nothing in ``engine/`` reads the wall clock, and a live feed is exactly where
that usually breaks. It does not break here: each report carries the instant
it was OBSERVED, and a board built at as-of T sees only reports observed at
or before T. Replaying yesterday's board gives yesterday's answer even though
the log has grown since.

The wall clock is read once, at the API boundary, when a report arrives —
which is the same single sanctioned call site the rest of the system uses.

STORAGE IS A FILE, DELIBERATELY
-------------------------------
JSON Lines under ``data/``. No database, because the base install is seven
packages and a dependency here would be a dependency a planner has to
install before the demo runs. Append-only text is also the format an auditor
can read without our help, and a report that a lawyer can read in Notepad is
worth more than one behind an ORM.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from engine.clock import ensure_utc, parse_instant

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = ROOT / "data" / "reports.jsonl"

# A field report is the highest-quality source here: somebody looking at the
# freight rather than a feed describing the region around it.
SOURCE_TIER = 1

STATUSES = ("moving", "queued", "held", "stopped", "delivered")
LOAD_STATES = ("intact", "damaged", "unknown")

# Reports arrive from a phone over an unauthenticated endpoint in this
# prototype, so everything is bounded. An unbounded note field is a
# denial-of-service and a log nobody can open.
MAX_NOTE = 500
MAX_TEXT = 120
_SHIPMENT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass(frozen=True)
class FieldReport:
    report_id: str
    shipment_id: str
    observed_at: datetime
    received_at: datetime
    status: str
    position: str | None
    revised_eta: datetime | None
    load_state: str
    note: str | None
    reported_by: str | None
    confirms_disruption: bool

    def as_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "shipment_id": self.shipment_id,
            "observed_at": self.observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "status": self.status,
            "position": self.position,
            "revised_eta": self.revised_eta.isoformat() if self.revised_eta else None,
            "load_state": self.load_state,
            "note": self.note,
            "reported_by": self.reported_by,
            "confirms_disruption": self.confirms_disruption,
            "source_tier": SOURCE_TIER,
        }


class ReportError(ValueError):
    """A report that cannot be trusted enough to store."""


def _clean(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def validate(payload: dict, received_at: datetime) -> FieldReport:
    """Turn a submitted payload into a report, or refuse it.

    Refusing loudly matters more here than anywhere else in the system: this
    is the one source that can CONFIRM a disruption and unlock a reroute, so
    a malformed report that is quietly coerced into a valid-looking one is a
    reroute taken on a misunderstanding.
    """
    shipment_id = _clean(payload.get("shipment_id"), 64)
    if not shipment_id or not _SHIPMENT_RE.match(shipment_id):
        raise ReportError("shipment_id is required and must be a plain identifier")

    status = str(payload.get("status", "")).strip().lower()
    if status not in STATUSES:
        raise ReportError(f"status must be one of {', '.join(STATUSES)}")

    load_state = str(payload.get("load_state", "unknown")).strip().lower()
    if load_state not in LOAD_STATES:
        raise ReportError(f"load_state must be one of {', '.join(LOAD_STATES)}")

    # The moment the driver SAW it, which is not the moment it reached us —
    # a report queued in a tunnel can arrive an hour late and must not claim
    # to be an hour-old observation.
    observed_raw = payload.get("observed_at")
    try:
        observed_at = parse_instant(observed_raw) if observed_raw else received_at
    except (ValueError, TypeError) as exc:
        raise ReportError(f"observed_at is not a valid instant: {exc}") from exc

    # A future observation is a clock problem on the phone, not a prophecy.
    if observed_at > received_at:
        observed_at = received_at

    revised_eta = None
    if payload.get("revised_eta"):
        try:
            revised_eta = parse_instant(payload["revised_eta"])
        except (ValueError, TypeError) as exc:
            raise ReportError(f"revised_eta is not a valid instant: {exc}") from exc

    return FieldReport(
        report_id=uuid.uuid4().hex[:12],
        shipment_id=shipment_id,
        observed_at=ensure_utc(observed_at),
        received_at=ensure_utc(received_at),
        status=status,
        position=_clean(payload.get("position"), MAX_TEXT),
        revised_eta=revised_eta,
        load_state=load_state,
        note=_clean(payload.get("note"), MAX_NOTE),
        reported_by=_clean(payload.get("reported_by"), MAX_TEXT),
        confirms_disruption=bool(payload.get("confirms_disruption")),
    )


def append(report: FieldReport, log: Path | None = None) -> FieldReport:
    """Write one report. Never rewrites, never reorders."""
    log = log or _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report.as_dict(), ensure_ascii=False) + "\n")
    return report


def read_all(log: Path | None = None) -> list[FieldReport]:
    """Every report ever filed, oldest first.

    A malformed line is SKIPPED rather than raising. The log is append-only
    and may be truncated mid-write by a crash; one damaged trailing line must
    not make the whole history unreadable.
    """
    log = log or _log_path()
    if not log.exists():
        return []

    out: list[FieldReport] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            out.append(FieldReport(
                report_id=raw["report_id"],
                shipment_id=raw["shipment_id"],
                observed_at=parse_instant(raw["observed_at"]),
                received_at=parse_instant(raw["received_at"]),
                status=raw["status"],
                position=raw.get("position"),
                revised_eta=(
                    parse_instant(raw["revised_eta"])
                    if raw.get("revised_eta") else None
                ),
                load_state=raw.get("load_state", "unknown"),
                note=raw.get("note"),
                reported_by=raw.get("reported_by"),
                confirms_disruption=bool(raw.get("confirms_disruption")),
            ))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return out


def as_of(instant: datetime, log: Path | None = None) -> list[FieldReport]:
    """Reports OBSERVED at or before ``instant``.

    This is what keeps a live feed compatible with a pinned board. Replaying
    yesterday's as-of gives yesterday's answer even though the log has grown
    since, which is the property the whole hindcast rests on.
    """
    cutoff = ensure_utc(instant)
    return [r for r in read_all(log) if r.observed_at <= cutoff]


def latest_for(
    shipment_id: str, instant: datetime, log: Path | None = None
) -> FieldReport | None:
    """The most recent report for one consignment as of an instant."""
    mine = [r for r in as_of(instant, log) if r.shipment_id == shipment_id]
    return max(mine, key=lambda r: r.observed_at) if mine else None


def summarise(instant: datetime, log: Path | None = None) -> dict:
    """What the field has told us, for the inputs panel."""
    reports = as_of(instant, log)
    shipments = {r.shipment_id for r in reports}
    return {
        "reports": len(reports),
        "shipments_reporting": len(shipments),
        "confirmations": sum(1 for r in reports if r.confirms_disruption),
        "damaged": sum(1 for r in reports if r.load_state == "damaged"),
        "latest_observed_at": (
            max(r.observed_at for r in reports).isoformat() if reports else None
        ),
    }


def _log_path() -> Path:
    override = os.environ.get("RADAR_REPORT_LOG")
    return Path(override) if override else DEFAULT_LOG
