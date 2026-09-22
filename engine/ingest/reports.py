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

# WHO IS REPORTING, and why it changes the tier.
#
# The app is for anyone on site with the freight — the driver in the cab, the
# agent at the quay, a forwarder's person at the terminal gate. They are all
# tier 1, because they are all LOOKING AT IT. That is the whole reason a field
# report beats every feed in this system: every other input describes a region
# and infers your consignment; this one observes it.
#
# But "on site" and "relaying what somebody told me" are different evidence,
# and the difference is exactly the one that matters when a report is about to
# unlock a reroute. A planner reading "the lock is shut" needs to know whether
# the person typing it can see the lock. So a relayed report is accepted, kept
# and shown — it is still worth having — at tier 2, alongside trade press,
# rather than at tier 1 alongside a gauge reading.
#
# This is not a permissions model. Nobody is stopped from reporting. It only
# records what kind of knowledge the report is, which the planner could not
# otherwise recover from the text.
ROLES: dict[str, dict] = {
    "driver":     {"label": "Driver / on board",     "tier": 1, "on_site": True},
    "site_agent": {"label": "On site with the load", "tier": 1, "on_site": True},
    "terminal":   {"label": "Terminal / depot staff", "tier": 1, "on_site": True},
    "relayed":    {"label": "Passing on what I was told", "tier": 2, "on_site": False},
    "other":      {"label": "Other",                 "tier": 1, "on_site": True},
}
DEFAULT_ROLE = "driver"

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
    role: str = DEFAULT_ROLE

    # A real fix from the phone, kept as numbers rather than folded into the
    # position text. The app already asked the device for coordinates and
    # then wrote them into a free-text field, where nothing could use them:
    # a string that happens to read "47.1234, 7.5678" is not a position, and
    # "Kaub, third in the queue" — which is the more useful answer — has no
    # numbers in it at all. Both are kept, because they say different things.
    lat: float | None = None
    lon: float | None = None
    accuracy_m: float | None = None

    # Photo ids, not photo bytes. An append-only JSONL with base64 images in
    # it stops being a file anyone can open, and the log is evidence: it has
    # to stay readable with `cat` in five years.
    photos: tuple[str, ...] = ()

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
            "role": self.role,
            "lat": self.lat,
            "lon": self.lon,
            "accuracy_m": self.accuracy_m,
            "photos": list(self.photos),
            "role_label": ROLES[self.role]["label"],
            # Derived from the role, never hardcoded: a relayed account is
            # tier 2 however confidently it is worded.
            "source_tier": self.source_tier,
            "first_hand": self.first_hand,
        }

    @property
    def source_tier(self) -> int:
        return int(ROLES[self.role]["tier"])

    @property
    def first_hand(self) -> bool:
        """Could this person SEE the thing they are describing?"""
        return bool(ROLES[self.role]["on_site"])


class ReportError(ValueError):
    """A report that cannot be trusted enough to store."""


def _clean(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


MAX_PHOTOS = 6
_PHOTO_RE = re.compile(r"^[a-f0-9]{16,64}$")


def _coords(payload: dict) -> tuple[float | None, float | None]:
    """Latitude and longitude, or neither.

    Refused as a PAIR. A report carrying only a latitude is not half a
    position, it is a bug somewhere upstream, and storing it would put a
    vehicle on the prime meridian.
    """
    raw_lat, raw_lon = payload.get("lat"), payload.get("lon")
    if raw_lat is None and raw_lon is None:
        return None, None
    if raw_lat is None or raw_lon is None:
        raise ReportError("lat and lon must be given together or not at all")
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except (TypeError, ValueError) as exc:
        raise ReportError(f"lat/lon are not numbers: {exc}") from exc
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ReportError(f"lat/lon out of range: {lat}, {lon}")
    return lat, lon


def _positive(value, limit: float) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= limit else None


def _photo_ids(value) -> tuple[str, ...]:
    """Ids of photos already uploaded, bounded and shape-checked.

    Bounded because this arrives from a phone over an endpoint that is
    unauthenticated in the prototype, and an unbounded list is a way to make
    one line of the log arbitrarily long.
    """
    if not value:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ReportError("photos must be a list of ids")
    ids = [str(v).strip().lower() for v in value if str(v).strip()]
    if len(ids) > MAX_PHOTOS:
        raise ReportError(f"at most {MAX_PHOTOS} photos per report")
    bad = [i for i in ids if not _PHOTO_RE.match(i)]
    if bad:
        raise ReportError(f"not photo ids: {', '.join(bad[:3])}")
    return tuple(ids)


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

    # An unknown role is refused rather than defaulted. Defaulting to 'driver'
    # would silently promote a relayed account to tier 1 on a typo, which is
    # the exact mistake the field exists to prevent.
    role = str(payload.get("role", DEFAULT_ROLE)).strip().lower() or DEFAULT_ROLE
    if role not in ROLES:
        raise ReportError(f"role must be one of {', '.join(ROLES)}")

    lat, lon = _coords(payload)
    accuracy = _positive(payload.get("accuracy_m"), limit=100_000)
    photos = _photo_ids(payload.get("photos"))

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
        role=role,
        lat=lat,
        lon=lon,
        accuracy_m=accuracy,
        photos=photos,
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
                # Reports written before the role existed are 'driver', which
                # is what they were: the app had no other kind of user.
                role=raw.get("role") or DEFAULT_ROLE,
                lat=raw.get("lat"),
                lon=raw.get("lon"),
                accuracy_m=raw.get("accuracy_m"),
                photos=tuple(raw.get("photos") or ()),
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
