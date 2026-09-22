"""Low-latency identification: an incident the moment something says so.

The batch pipeline answers "what does the book look like as of 06:00". This
module answers "what just happened", and it answers it per item, as each item
lands, with no window to wait for.

THREE WAYS IN, ONE WAY OUT
--------------------------
    a field report     a driver marks Held or Stopped, or files a revised ETA
                       — this is the fastest signal in the system, because the
                       person is standing next to the problem
    an inbound hook    a carrier, a TMS or a port system POSTs to us
    a polled source    the existing catalogue, but published per item as it
                       arrives rather than collected into a batch

All three become an ``Incident`` and go onto the bus. Whatever is listening —
the dashboard over SSE, the dispatcher, a webhook — hears about it in the same
breath.

WHAT COUNTS AS AN INCIDENT
--------------------------
Something that changes when freight arrives. A driver reporting "moving" is
not an incident and must not raise one: a stream that fires on every heartbeat
trains the planner to ignore it, which costs more than the latency it saved.
So the classifier is deliberately narrow, deterministic, and states its reason.

NO WALL CLOCK
-------------
Every entry point takes the instant as an argument. The API passes the real
time; a test or a replay passes a pinned one, and the same inputs produce the
same incidents either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from engine.fast.bus import BUS, TOPIC_DISRUPTION, TOPIC_FIELD, TOPIC_SIGNAL, Bus

# Field statuses that mean the freight is not moving. `queued` is not here:
# a queue is normal at a port and raising an incident for it is the noise that
# gets a live feed muted.
STOPPED_STATUSES = frozenset({"held", "stopped"})

# Load states that mean the consignment itself is in trouble, whatever the
# vehicle is doing.
DAMAGED_STATES = frozenset({"damaged"})

# How late a revised ETA has to be before it is an incident rather than a
# routine correction. Under this, the buffer absorbs it.
ETA_SLIP_HOURS = 6.0

KIND_LABEL = {
    "vehicle_stopped": "Vehicle stopped",
    "load_damaged": "Load damaged",
    "eta_slip": "Arrival slipping",
    "external": "External disruption",
}


@dataclass(frozen=True)
class Incident:
    """One thing that needs a decision, with where it came from."""

    incident_id: str
    kind: str
    shipment_id: str | None
    headline: str
    detail: str
    at: str
    source: str            # "field" | "hook" | "source"
    first_hand: bool
    confidence: str        # "observed" | "reported" | "relayed"
    node_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    photos: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind.replace("_", " ").capitalize())

    def as_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "label": self.label,
            "shipment_id": self.shipment_id,
            "headline": self.headline,
            "detail": self.detail,
            "at": self.at,
            "source": self.source,
            "first_hand": self.first_hand,
            "confidence": self.confidence,
            "node_id": self.node_id,
            "lat": self.lat,
            "lon": self.lon,
            "photos": list(self.photos),
        }


def _confidence(report: dict) -> str:
    """How much the report is worth, derived from the role, not the wording.

    A relayed account is second-hand however confidently it is phrased, and
    the planner needs to see that before they spend money on it.
    """
    if report.get("authenticated") and report.get("first_hand"):
        return "observed"
    if report.get("first_hand"):
        return "reported"
    return "relayed"


def from_field_report(report: dict, now: datetime) -> Incident | None:
    """Classify one report. Returns None when nothing has gone wrong.

    None is the common case and the important one: most reports are a vehicle
    saying it is fine.
    """
    shipment_id = report.get("shipment_id")
    status = (report.get("status") or "").lower()
    load_state = (report.get("load_state") or "").lower()
    where = report.get("position") or "an unstated position"
    confidence = _confidence(report)

    kind: str | None = None
    headline = ""
    detail = ""

    if load_state in DAMAGED_STATES:
        kind = "load_damaged"
        headline = f"{shipment_id} reports damage"
        detail = report.get("note") or f"Damage reported at {where}."
    elif status in STOPPED_STATUSES:
        kind = "vehicle_stopped"
        headline = f"{shipment_id} is {status}"
        detail = report.get("note") or f"Reported {status} at {where}."
    else:
        revised = report.get("revised_eta")
        if revised:
            try:
                slip_hours = (
                    datetime.fromisoformat(revised) - now
                ).total_seconds() / 3600.0
            except (TypeError, ValueError):
                slip_hours = None
            if slip_hours is not None and slip_hours < -ETA_SLIP_HOURS:
                kind = "eta_slip"
                headline = f"{shipment_id} is running late"
                detail = (
                    f"Revised arrival is {abs(slip_hours):.0f} h behind the "
                    "time on the plan."
                )

    if kind is None:
        return None

    return Incident(
        incident_id=f"INC:{report.get('report_id', shipment_id)}",
        kind=kind,
        shipment_id=shipment_id,
        headline=headline,
        detail=detail,
        at=report.get("observed_at") or now.isoformat(),
        source="field",
        first_hand=bool(report.get("first_hand")),
        confidence=confidence,
        lat=report.get("lat"),
        lon=report.get("lon"),
        photos=tuple(report.get("photos") or ()),
        raw=report,
    )


def from_hook(payload: dict, now: datetime) -> Incident:
    """An incident pushed in by a carrier, a TMS or a port system.

    Trusted no further than a relayed report: the caller asserts what happened
    and we record that they asserted it. A machine saying a thing confidently
    is still a machine saying a thing.
    """
    shipment_id = payload.get("shipment_id")
    headline = payload.get("headline") or "External system reported a disruption"
    return Incident(
        incident_id=f"INC:hook:{payload.get('reference', headline)[:48]}",
        kind=str(payload.get("kind", "external")),
        shipment_id=shipment_id,
        headline=headline,
        detail=payload.get("detail") or "",
        at=payload.get("at") or now.isoformat(),
        source="hook",
        first_hand=False,
        confidence="relayed",
        node_id=payload.get("node_id"),
        lat=payload.get("lat"),
        lon=payload.get("lon"),
        raw=payload,
    )


class Watcher:
    """Holds the live incident list and publishes as things arrive.

    Deliberately not a queue consumer with its own thread: it is driven by
    whoever has the news. The API calls ``saw_report`` on POST, the hook
    handler calls ``saw_hook``, and a poller calls ``saw_signal`` per item as
    each one comes back. Nothing sleeps.
    """

    def __init__(self, bus: Bus | None = None, limit: int = 200) -> None:
        self._bus = bus or BUS
        self._incidents: list[Incident] = []
        self._limit = limit
        self._seen: set[str] = set()

    # ------------------------------------------------------------- inbound
    def saw_report(self, report: dict, now: datetime) -> Incident | None:
        """A field report landed. Publish it, and raise an incident if it is one."""
        self._bus.publish(TOPIC_FIELD, {"report": report}, at=now.isoformat())
        incident = from_field_report(report, now)
        if incident is not None:
            self._record(incident)
        return incident

    def saw_hook(self, payload: dict, now: datetime) -> Incident:
        incident = from_hook(payload, now)
        self._record(incident)
        return incident

    def saw_signal(self, item: dict, now: datetime, touches_freight: bool) -> None:
        """One item from a source, published the moment it arrives.

        Published even when it touches nothing: the funnel's own numbers are
        how a planner tells "quiet morning" from "the feed is dead", and that
        distinction is invisible if only the survivors are announced.
        """
        self._bus.publish(
            TOPIC_SIGNAL,
            {"item": item, "touches_freight": touches_freight},
            at=now.isoformat(),
        )

    # -------------------------------------------------------------- record
    def _record(self, incident: Incident) -> None:
        if incident.incident_id in self._seen:
            return
        self._seen.add(incident.incident_id)
        self._incidents.append(incident)
        if len(self._incidents) > self._limit:
            dropped = self._incidents[: -self._limit]
            self._incidents = self._incidents[-self._limit:]
            for old in dropped:
                self._seen.discard(old.incident_id)
        self._bus.publish(
            TOPIC_DISRUPTION, {"incident": incident.as_dict()}, at=incident.at
        )

    # --------------------------------------------------------------- read
    def live(self, limit: int = 20) -> list[Incident]:
        """Newest first. What the dashboard puts at the top."""
        return list(reversed(self._incidents))[:limit]

    def for_shipment(self, shipment_id: str) -> list[Incident]:
        return [i for i in self._incidents if i.shipment_id == shipment_id]

    def clear(self) -> None:
        self._incidents.clear()
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._incidents)


WATCHER = Watcher()
