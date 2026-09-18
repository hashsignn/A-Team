"""Monte Carlo delay propagation (BRIEF §5.2, §5.3).

TWO DELIBERATE DEPARTURES FROM THE BRIEF, BOTH LOAD-BEARING
===========================================================

1. EVENT DRAWS ARE SHARED ACROSS SHIPMENTS
------------------------------------------
The brief says "sample 10,000 times", per shipment. Done literally, each
shipment draws its own duration for the same Antwerp strike — so a portfolio of
forty shipments calling at Antwerp experiences forty *independent* strikes.
That is physically impossible, and it flattens the tail: independent draws
average out, correlated ones do not.

The tail is not a detail here. It is the entire quantity the convene decision
depends on. So durations are drawn ONCE PER ITERATION into an
``(n_draws, n_events)`` matrix, and that same draw is applied to every shipment
the event gates onto. Summing across shipments *within a draw* then gives the
true portfolio distribution, correlation included, for free.

Retrofitting this later means rewriting simulate/ and score/ together. It costs
nothing now.

2. THE BASELINE DOES NOT TAKE THE BEST ALTERNATE
------------------------------------------------
BRIEF §5.2 says to compute delay on each feasible route and take the minimum.
BRIEF §5.4 then wants ``E[loss|nothing] − E[loss|act]``. Those two cannot both
hold: if the baseline already routes around the disruption, the baseline has
already acted, and the value of acting collapses to ~zero on exactly the
shipments that have alternatives — the interesting ones.

So the baseline is the PLANNED route, unswitched. Alternates live inside the
act scenario, priced with their cost. The minimum is taken there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from engine.config import Config
from engine.schemas import Event, GateHit, Shipment


@dataclass
class DrawMatrix:
    """Shared event draws: the object that makes correlation possible."""

    event_ids: list[str]
    delay_days: np.ndarray  # (n_draws, n_events)
    occurs: np.ndarray      # (n_draws, n_events) bool
    probability_known: dict[str, bool]
    index: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.index = {eid: i for i, eid in enumerate(self.event_ids)}

    @property
    def n_draws(self) -> int:
        return int(self.delay_days.shape[0])

    def column(self, event_id: str) -> np.ndarray:
        """Effective delay contributed by one event, per draw."""
        i = self.index[event_id]
        return self.delay_days[:, i] * self.occurs[:, i]


def build_draw_matrix(
    events: list[Event],
    config: Config,
    rng: np.random.Generator | None = None,
) -> DrawMatrix:
    """Draw every event's duration and occurrence, once."""
    sim = config.scoring["simulation"]
    n_draws = int(sim["draws"])
    rng = rng or np.random.default_rng(int(sim["seed"]))

    n_events = len(events)
    delay = np.zeros((n_draws, n_events), dtype=float)
    occurs = np.ones((n_draws, n_events), dtype=bool)
    p_known: dict[str, bool] = {}

    for j, event in enumerate(events):
        # An event may activate several variables. The delay it contributes is
        # the worst of them, not the sum: a strike that closes the terminal
        # does not also cost you the crane outage on top.
        columns = []
        for vid in event.active_variables:
            try:
                triple = config.delay_triple(vid, event.severity.value)
            except KeyError:
                continue
            columns.append(_triangular(rng, triple, n_draws))

        if columns:
            delay[:, j] = np.max(np.column_stack(columns), axis=1)

        # Occurrence. A realized event has already happened — P is 1 and the
        # remaining uncertainty is duration, which is exactly the case a
        # compound-probability formula cannot express (BRIEF §5.3).
        p_known[event.event_id] = event.probability_known
        if event.realized:
            occurs[:, j] = True
        elif event.probability is not None:
            occurs[:, j] = rng.random(n_draws) < event.probability
        else:
            # Probability unsourceable. We do NOT invent one. The draw is
            # conditional on the event happening, and every number derived from
            # it is labelled "if this occurs" rather than being silently
            # discounted by a number nobody can defend.
            occurs[:, j] = True

    return DrawMatrix(
        event_ids=[e.event_id for e in events],
        delay_days=delay,
        occurs=occurs,
        probability_known=p_known,
    )


def _triangular(
    rng: np.random.Generator, triple, n: int
) -> np.ndarray:
    """numpy.random.triangular, guarding the degenerate case.

    A zero-width triple is legitimate — a union ballot contributes no delay of
    its own, only lead time — and numpy rejects left == right.
    """
    left, mode, right = triple.optimistic, triple.likely, triple.pessimistic
    if right <= left:
        return np.full(n, float(mode))
    return rng.triangular(left, min(max(mode, left), right), right, size=n)


# =====================================================================
# Propagation
# =====================================================================


@dataclass
class ShipmentDraws:
    """Per-draw arrival outcome for one shipment under one scenario."""

    shipment_id: str
    total_delay_days: np.ndarray   # (n_draws,) post-buffer delay vs plan
    lateness_days: np.ndarray      # (n_draws,) days past the COMMITTED date
    driving_event_ids: list[str]

    @property
    def p_late(self) -> float:
        return float(np.mean(self.lateness_days > 0.0))

    @property
    def expected_delay(self) -> float:
        return float(np.mean(self.total_delay_days))

    @property
    def p90_delay(self) -> float:
        return float(np.percentile(self.total_delay_days, 90))

    @property
    def expected_lateness(self) -> float:
        return float(np.mean(self.lateness_days))


def propagate(
    shipment: Shipment,
    hits: list[GateHit],
    draws: DrawMatrix,
    residual_by_event: dict[str, float] | None = None,
) -> ShipmentDraws:
    """Walk the legs, accumulating delay and letting buffer absorb it.

        delay = 0
        for leg in route:
            delay += sampled_delay_at(leg)
            delay  = max(0, delay - buffer(leg))
        arrival = planned_eta + delay

    Additive, not multiplicative-decay. A three-day delay at Rotterdam is still
    about three days at the customer; a per-hop decay coefficient would make
    delay shrink as it travels, which is physically backwards and cannot be
    calibrated anyway (BRIEF §5.2).

    ``residual_by_event`` scales an event's contribution — that is how an
    action is modelled. 0.0 means the action removes the exposure entirely,
    1.0 means it does nothing. Absent means no action.
    """
    residual = residual_by_event or {}
    n = draws.n_draws

    by_leg: dict[int, list[str]] = {}
    for hit in hits:
        if hit.shipment_id != shipment.shipment_id:
            continue
        by_leg.setdefault(hit.leg_index, []).append(hit.event_id)

    running = np.zeros(n, dtype=float)
    driving: list[str] = []

    for index, leg in enumerate(shipment.legs):
        leg_events = by_leg.get(index, [])
        if leg_events:
            stacked = []
            for event_id in leg_events:
                if event_id not in draws.index:
                    continue
                contribution = draws.column(event_id) * residual.get(event_id, 1.0)
                stacked.append(contribution)
                if event_id not in driving:
                    driving.append(event_id)
            if stacked:
                # Concurrent events on the same leg overlap in time rather than
                # queueing end to end, so the leg's delay is the worst of them.
                running = running + np.max(np.column_stack(stacked), axis=1)

        buffer_days = leg.buffer_hours / 24.0
        running = np.maximum(0.0, running - buffer_days)

    # LATENESS IS NOT DELAY. Delay is measured against the planned ETA;
    # penalties accrue against the date promised to the customer. The gap
    # between them is real commitment slack, and BRIEF §5.4's formula charges
    # penalty across it. max(0, ·) is convex, so it is evaluated here, inside
    # the expectation, never applied to the mean afterwards.
    slack_days = shipment.commitment_slack_hours / 24.0
    lateness = np.maximum(0.0, running - slack_days)

    return ShipmentDraws(
        shipment_id=shipment.shipment_id,
        total_delay_days=running,
        lateness_days=lateness,
        driving_event_ids=driving,
    )


def variable_contributions(
    event: Event,
    config: Config,
) -> dict[str, float]:
    """Expected days of delay per active variable, for the web chart.

    BRIEF §7 pane 3: spokes labelled in DAYS OF DELAY, not abstract weights, so
    a planner reads "water level 4 days, lock closure 2 days, port congestion
    1 day" instead of "variable 47: 0.63".
    """
    out: dict[str, float] = {}
    for vid in event.active_variables:
        try:
            triple = config.delay_triple(vid, event.severity.value)
        except KeyError:
            continue
        # Mean of a triangular distribution.
        out[vid] = (triple.optimistic + triple.likely + triple.pessimistic) / 3.0
    return out
