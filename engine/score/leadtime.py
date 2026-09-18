"""Lead time and the decision deadline (BRIEF §5.5).

The brief asks directly: *"What becomes possible with 24/48 hours of extra lead
time?"* That is the client naming their own metric, so the headline number is
**hours of warning gained**, not a risk score.

    decision_deadline = event_impact_time − action_duration(mode, action)
    lead_time         = decision_deadline − now

A risk you learn about two hours out is worthless. At 72 hours it is the
product.

    lead > 2× duration  → comfortable
    1–2×                → tightening
    < 1×                → too late: stop proposing reroutes, switch to
                          notification

These bands are also the cliff edges of the option-decay curve in
engine/portfolio/decay.py. The same ``min_action_hours`` table drives both,
which is why the curve needs no parameters of its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine.clock import Clock
from engine.config import Config
from engine.schemas import ActionOption, GateHit


def impact_time(
    event_starts_at: datetime,
    hits: list[GateHit],
    shipment_id: str,
    event_id: str,
) -> datetime | None:
    """When this event first bites *this* shipment.

    Not when the event starts — when the shipment reaches it. A strike starting
    Monday does not bite a vessel that arrives Thursday until Thursday, and
    that difference is the lead time.
    """
    relevant = [
        h for h in hits
        if h.shipment_id == shipment_id and h.event_id == event_id
    ]
    if not relevant:
        return None
    first_contact = min(h.leg_enters_at for h in relevant)
    return max(first_contact, event_starts_at)


def decision_deadline(
    impact_at: datetime,
    action: ActionOption,
) -> datetime:
    return impact_at - timedelta(hours=action.min_hours)


def lead_time_hours(
    clock: Clock,
    impact_at: datetime,
    action: ActionOption,
) -> float:
    return clock.hours_until(decision_deadline(impact_at, action))


def actionability(
    lead_hours: float | None,
    action_min_hours: float | None,
    config: Config,
) -> str:
    """Which band this sits in.

    ``None`` propagates as ``no_action`` rather than collapsing to ``too_late``:
    "we have no option configured" and "the option has expired" are different
    statements and a planner needs to tell them apart.
    """
    if lead_hours is None or action_min_hours is None:
        return "no_action"
    if action_min_hours <= 0:
        return "no_action"

    bands = config.scoring["actionability_bands"]
    ratio = lead_hours / action_min_hours
    if ratio >= bands["comfortable"]:
        return "comfortable"
    if ratio >= bands["tightening"]:
        return "tightening"
    return "too_late"


def hours_until_impact(clock: Clock, impact_at: datetime | None) -> float:
    """Hours from as-of until the event bites. Negative once it has."""
    if impact_at is None:
        return float("-inf")
    return clock.hours_until(impact_at)


def warning_gained(
    clock: Clock,
    first_detected_at: datetime,
    impact_at: datetime | None,
) -> float | None:
    """Hours of warning this alert produced.

    The honest baseline is "when the carrier called", and we do not have carrier
    call timestamps. The hindcast harness supplies a usable proxy — first
    official public notice versus our first actionable alert — and until it
    runs, this number is reported as what it is: hours between detection and
    impact, not a comparison against anything.
    """
    if impact_at is None:
        return None
    return (impact_at - first_detected_at).total_seconds() / 3600.0


def deadline_text(clock: Clock, deadline: datetime | None) -> str | None:
    """The deadline as a planner would say it."""
    if deadline is None:
        return None
    hours = clock.hours_until(deadline)
    if hours < 0:
        return f"{deadline:%a %d %b %H:%M} UTC (passed)"
    if hours < 48:
        return f"{deadline:%a %H:%M} UTC ({hours:.0f} h)"
    return f"{deadline:%a %d %b %H:%M} UTC ({hours / 24:.0f} days)"
