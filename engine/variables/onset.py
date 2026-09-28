"""What kind of event this is — which decides what is uncertain about it.

Disaster science separates SUDDEN-ONSET hazards (an earthquake, a fire) from
SLOW-ONSET ones (a drought, a falling river); that is the UN's own
terminology (UNDRR). Freight adds two cases of its own: disruptions that are
announced with their dates, and signs of a stoppage that are not a stoppage.
Each kind leaves a different question open, so each is judged differently:

    kind        open question              judged by
    sudden      how long                   Time-to-Recover vs Time-to-Survive
    scheduled   overrun                    the stated window
    building    whether and when it bites  the measurement or forecast
    warning     whether, when, how long    the cost-loss ratio, and lead time

A warning is the case the first real recording got wrong: "Iran threatens to
close the Strait of Hormuz" went on the board as the closure. See
engine/variables/modality.py for how the words are read, and
docs/RISK_METHOD.md for the whole method.

The kind is decided from evidence first and the ledger second. The ledger's
``onset`` says how a risk USUALLY arrives: a road closure is usually sudden,
but the Autobahn feed lists roadworks with their dates, which are scheduled.
"""

from __future__ import annotations

from engine.schemas import RiskVariable
from engine.variables.modality import Reading

LABELS = {
    "sudden": "Sudden",
    "scheduled": "Planned",
    "building": "Developing",
    "warning": "Early warning",
}

# What each kind means, for the tooltip beside the label.
MEANING = {
    "sudden": "Happened without warning: a closure, a strike, an accident.",
    "scheduled": "Announced in advance: roadworks, a planned closure.",
    "building": "Builds up over days: a falling river, a storm on its way.",
    "warning": "A sign it may happen: carriers moving orders, a strike ballot.",
}


def classify(
    variables: list[RiskVariable],
    *,
    measured: bool,
    realized: bool,
    probability: float | None,
    stated_window: bool,
    reading: Reading | None = None,
) -> tuple[str, str]:
    """(kind, why) for one event. ``variables`` in the router's order: the
    first is the one that named the event."""
    primary = variables[0] if variables else None
    name = primary.name if primary else "this"

    if primary is not None and primary.onset == "precursor":
        return "warning", f"{name}: a sign of a stoppage, never a stoppage itself"

    if reading is not None and reading.hypothetical:
        # A warning about something an instrument measures is a forecast —
        # "gale warning", "congestion expected to worsen" — and its odds can
        # come from the forecaster. Anything else is a sign.
        forecastable = (primary is not None and primary.probability_sourceable
                        and primary.onset in ("sudden", "slow"))
        if forecastable:
            return "building", f"forecast, not yet happened: the report says ‘{reading.cue}’"
        return "warning", f"not reported as happening: the report says ‘{reading.cue}’"

    if measured and not realized:
        return "building", "a forecast: it has not happened yet"
    if not realized and probability is None and not measured:
        return "warning", "not reported as having happened, and no odds are stated"
    if stated_window:
        return "scheduled", "the source states when it starts and ends"
    if primary is not None and primary.onset == "scheduled":
        return "scheduled", f"{name} is announced with its dates"
    if primary is not None and primary.onset == "slow":
        what = "measured" if measured else "reported"
        return "building", f"{name} builds over days or weeks ({what})"
    what = "measured" if measured else "reported as happening"
    return "sudden", f"{what}; {name.lower()} comes without warning, so what matters is how long it lasts"
