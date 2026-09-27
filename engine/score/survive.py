"""Time-to-Recover against Time-to-Survive, and when a warning is worth acting on.

TIME-TO-SURVIVE
===============
Simchi-Levi, Schmidt and Wei ("From Superstorms to Factory Fires", Harvard
Business Review, 2014; the risk exposure work done with Ford) set aside the
probability of a rare disruption, which nobody can estimate, and ask two
questions instead: how long until it is fixed (Time-to-Recover, TTR), and how
long can we keep our promises without it (Time-to-Survive, TTS). Where TTR
exceeds TTS there is exposure, whatever the odds. ISO 22301 asks the same
question as the "maximum tolerable period of disruption".

For one shipment and one event, TTS is the most delay the event can add
before the shipment misses the date promised to its customer. It is computed
here with exactly the walk simulate/draws.py::propagate makes — the delay is
added at every leg the event hits, each leg's buffer absorbs what it can, and
what is left is measured against the committed date — so the break-even and
the simulation can never disagree about the same shipment.

TTR is the event's three judged points (delay_model.yaml: best, likely,
worst). The verdict compares them:

    late even in the best case     best  > TTS
    late in the likely case        likely > TTS >= best
    late only in the worst case    worst > TTS >= likely
    on time even in the worst case TTS >= worst

A planner can check that sentence against what they know, and argue with one
number — "that closure will not take three days" — instead of with a
percentage the triangle produced.

THE COST-LOSS RATIO
===================
For a warning nobody can price the odds of, the decision rule meteorological
services use for acting on a warning (Thompson 1952; Murphy 1977): take the
protective action when P(it happens) > C / L — the cost of acting over the
loss it avoids. The break-even probability is shown, not a probability: the
planner judges whether the warning is likelier than that, which is a question
they can answer, and nothing is invented.

The break-even is put in the words of ICD 203, the US intelligence
community's analytic standard for stating likelihood, so "12%" also reads as
"very unlikely" — a judgement somebody can make about a strike ballot.
"""

from __future__ import annotations

from engine.config import Config
from engine.schemas import Event, GateHit, Shipment

# Beyond a year of delay nothing about a shipment is still being planned.
HORIZON_DAYS = 365.0

LABELS = {
    "late_already": "late even without this",
    "late_best": "late even in the best case",
    "late_likely": "late in the likely case",
    "late_worst": "late only in the worst case",
    "on_time": "on time even in the worst case",
}

# ICD 203 (2015), "Expressions of likelihood", upper bound of each band.
ICD_203 = (
    (0.05, "almost no chance"),
    (0.20, "very unlikely"),
    (0.45, "unlikely"),
    (0.55, "roughly even chance"),
    (0.80, "likely"),
    (0.95, "very likely"),
    (1.00, "almost certain"),
)


def delay_points(event: Event, config: Config) -> tuple[float, float, float] | None:
    """(best, likely, worst) days of delay if it hits — the worst of the
    event's variables at each point, as the simulation takes the worst of
    them in each draw. None when no variable has a delay model."""
    triples = []
    for vid in event.active_variables:
        try:
            triples.append(config.delay_triple(vid, event.severity.value))
        except KeyError:
            continue
    if not triples:
        return None
    return (
        max(t.optimistic for t in triples),
        max(t.likely for t in triples),
        max(t.pessimistic for t in triples),
    )


# A stoppage of the WAY — a closed motorway, a line possession, a shut lock —
# delays a shipment at most until it reopens: the truck waits it out, or more
# often goes round. So when the source states the end, the judged points are
# capped at the time left in the window when the shipment gets there. A strike
# is not capped: a port that stops for two days takes longer than two days to
# clear its backlog.
WAIT_IT_OUT_FAMILIES = {"infrastructure"}
WAIT_IT_OUT_VARIABLES = {"WAT_LOCK_CLOSURE"}


def waits_out(event: Event) -> bool:
    """A scheduled closure of the way itself, with a stated end."""
    if event.kind != "scheduled" or event.ends_at is None:
        return False
    primary = event.active_variables[0] if event.active_variables else ""
    return event.event_class in WAIT_IT_OUT_FAMILIES or primary in WAIT_IT_OUT_VARIABLES


def window_cap(event: Event, impact_at) -> float | None:
    """Days left in a scheduled closure's stated window when the shipment
    reaches it — the most it can be delayed — or None where no cap applies.

    Without this a two-night A61 closure carried the family's judged worst
    case of five days, and put a route on red: "notify the customer now".
    """
    if impact_at is None or not waits_out(event):
        return None
    return max(0.0, (event.ends_at - impact_at).total_seconds() / 86400.0)


def capped(points: tuple[float, float, float] | None,
           cap: float | None) -> tuple[float, float, float] | None:
    if points is None or cap is None:
        return points
    return tuple(min(p, cap) for p in points)  # type: ignore[return-value]


def lateness_if(shipment: Shipment, hits: list[GateHit], event_id: str,
                delay_days: float) -> float:
    """Days past the committed date if the event adds ``delay_days`` — the
    propagate() walk with one number instead of a draw."""
    legs_hit = {h.leg_index for h in hits
                if h.shipment_id == shipment.shipment_id and h.event_id == event_id}
    running = 0.0
    for index, leg in enumerate(shipment.legs):
        if index in legs_hit:
            running += delay_days
        running = max(0.0, running - leg.buffer_hours / 24.0)
    return max(0.0, running - shipment.commitment_slack_hours / 24.0)


def time_to_survive(shipment: Shipment, hits: list[GateHit], event_id: str) -> float | None:
    """The most delay this event can add before the shipment is late.

    0.0 if it is late without the event; None if a year of delay would not
    make it late (it is not on a hit leg, or the slack is enormous). Lateness
    rises with delay, so the break-even is found by bisection: 32 halvings of
    a year is better than a second.
    """
    if lateness_if(shipment, hits, event_id, 0.0) > 0.0:
        return 0.0
    if lateness_if(shipment, hits, event_id, HORIZON_DAYS) <= 0.0:
        return None
    low, high = 0.0, HORIZON_DAYS
    for _ in range(32):
        mid = (low + high) / 2.0
        if lateness_if(shipment, hits, event_id, mid) > 0.0:
            high = mid
        else:
            low = mid
    return round(low, 3)


def survival(tts: float | None, points: tuple[float, float, float] | None) -> str:
    """The verdict against best / likely / worst. Late means MORE delay than
    the shipment survives: exactly its TTS still arrives on the promised
    date."""
    if points is None:
        return ""
    if tts is None:
        return "on_time"
    if tts <= 0.0:
        return "late_already"
    best, likely, worst = points
    if best > tts:
        return "late_best"
    if likely > tts:
        return "late_likely"
    if worst > tts:
        return "late_worst"
    return "on_time"


def break_even_probability(loss_do_nothing: float, loss_after_acting: float,
                           cost_of_acting: float) -> float | None:
    """C / L: act if P(it happens) is above this. The losses are the ones
    the simulation reports for an event whose odds are unknown — both IF it
    happens. None when acting would not pay even if it were certain."""
    avoided = loss_do_nothing - loss_after_acting
    if avoided <= 0.0:
        return None
    threshold = max(0.0, cost_of_acting) / avoided
    return round(threshold, 4) if threshold < 1.0 else None


def likelihood_words(p: float) -> str:
    """The ICD 203 expression for a probability."""
    for upper, words in ICD_203:
        if p <= upper:
            return words
    return ICD_203[-1][1]
