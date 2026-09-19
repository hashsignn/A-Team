"""The option-decay curve and the convene rule.

WHY THIS MODULE EXISTS
======================
Sika, answering Q6:

    "In crisis, established teams that meet on a weekly schedule (e.g.
     Procurement, Manufacturing, Supply Chain, Controlling) increase meeting
     frequency e.g. from weekly to every day. They have full authority to
     decide on mitigation. **The real problem is that we declare a crisis too
     late and lose on available options.**"

Authority is not the bottleneck. Analysis is not the bottleneck. The decision
to convene is. Everything the brief specifies is per-shipment and per-event;
the decision that is actually failing is portfolio-level and organisational.

THIS IS NOT A NEW MODEL
-----------------------
The brief already computes ``decision_deadline = impact_time − action_duration``.
That number exists only because options EXPIRE. So the real decision, even for
a single shipment, is never "act or don't" — it is **act now vs. wait**:

    Value(act now) = S − C
    Value(wait)    = P(still actionable at t+Δ) × E[S − C | info at t+Δ]

Once t crosses the deadline, Value(wait) = 0 for that action. Plot it over time
and you get a step function that drops each time an action class expires. The
portfolio version is just the sum:

    R(t) = Σ over at-risk shipments  max over still-feasible actions
           Value_of_acting(shipment, action, t)

Zero new parameters. The cliff edges are the ``min_action_hours`` table the
brief already mandates. We are drawing an aggregate of numbers we already have.

THE PART THAT ACTUALLY CHANGES BEHAVIOUR IS NOT TECHNICAL
---------------------------------------------------------
Teams do not declare late because they lack a number. They declare late because
declaring is socially expensive — somebody has to stick their neck out and risk
crying wolf. So the tool must not argue for a crisis. It reports that a
threshold **the group pre-agreed in calm conditions** has tripped:

    "Convene rule (agreed 12 Mar): recoverable value at risk > CHF 150k, or
     >4 contracts exposed. Both tripped 06:14 today."

That moves the decision from a judgement one person owns to a rule the group
already owns. It costs about forty lines of YAML and it is the whole mechanism.

RELATION TO BRIEF §12 ("do not show a global risk matrix")
----------------------------------------------------------
Honoured. There is no global P×I matrix here and the per-event matrix remains
the distinctive idea. A one-line portfolio *state* strip is not a matrix, and
§12 was written before Sika's answer to Q6 arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from engine.clock import Clock
from engine.config import Config
from engine.schemas import (
    ConveneVerdict,
    DecayPoint,
    EventAssessment,
    Posture,
)


@dataclass
class _Option:
    """One still-open mitigation, flattened for the sweep."""

    shipment_id: str
    event_id: str
    customer: str
    label: str
    value_chf: float
    expires_at: datetime
    loss_draws: np.ndarray | None


def build_curve(
    assessments: list[EventAssessment],
    config: Config,
    clock: Clock,
) -> list[DecayPoint]:
    """Sweep forward, recomputing recoverable value at each step.

    R(t) is monotonically non-increasing: options only expire, never reappear.
    The steps are where the cliff edges are, and those are the moments worth
    convening before.
    """
    spec = config.scoring["decay_curve"]
    horizon_days = float(spec["horizon_days"])
    step_hours = float(spec["step_hours"])
    cap = int(spec["concurrent_action_cap"])

    options = _flatten(assessments)
    if not options:
        return []

    points: list[DecayPoint] = []
    steps = int((horizon_days * 24) / step_hours) + 1

    for i in range(steps):
        at = clock.as_of + timedelta(hours=i * step_hours)
        live = [o for o in options if o.expires_at > at]

        # Best option per shipment — you take one action per shipment, not all
        # of them.
        best: dict[str, _Option] = {}
        for option in live:
            current = best.get(option.shipment_id)
            if current is None or option.value_chf > current.value_chf:
                best[option.shipment_id] = option

        ranked = sorted(best.values(), key=lambda o: o.value_chf, reverse=True)

        # CEILING, NOT FORECAST. Summing value-of-acting across shipments
        # assumes every action could be taken at once, which ignores contention
        # for the same alternate capacity. The cap makes that assumption
        # explicit and bounded rather than silent.
        counted = ranked[:cap]
        recoverable = sum(o.value_chf for o in counted)

        p10, p90 = _band(counted)

        # What falls off next — the thing worth naming in the headline.
        next_at = at + timedelta(hours=step_hours)
        expiring = [
            f"{o.label} ({o.shipment_id})"
            for o in counted
            if o.expires_at <= next_at
        ]

        points.append(
            DecayPoint(
                at=at,
                hours_from_now=i * step_hours,
                recoverable_chf=round(recoverable, 2),
                recoverable_p10_chf=round(p10, 2),
                recoverable_p90_chf=round(p90, 2),
                actions_still_open=len(live),
                shipments_still_actionable=len(best),
                expiring_next=expiring[:5],
            )
        )

    return points


def _flatten(assessments: list[EventAssessment]) -> list[_Option]:
    out: list[_Option] = []
    for assessment in assessments:
        for risk in assessment.shipment_risks:
            if risk.best_action is None or risk.decision_deadline is None:
                continue
            if risk.value_of_acting_chf <= 0:
                continue
            out.append(
                _Option(
                    shipment_id=risk.shipment_id,
                    event_id=risk.event_id,
                    customer=risk.customer,
                    label=risk.best_action.label,
                    value_chf=risk.value_of_acting_chf,
                    expires_at=risk.decision_deadline,
                    loss_draws=None,
                )
            )
    return out


def _band(options: list[_Option]) -> tuple[float, float]:
    """A spread around the recoverable total.

    Each value-of-acting is already a Monte Carlo expectation, so summing forty
    of them and printing one number invites false precision. Where per-draw
    arrays are carried through, this becomes the true correlated percentile;
    until then it is a declared ±30% band, which is honest about being a band.
    """
    total = sum(o.value_chf for o in options)
    return total * 0.70, total * 1.30


def cost_of_waiting(
    curve: list[DecayPoint],
    clock: Clock,
    until: datetime,
) -> float:
    """CHF of mitigation value that expires between now and *until*.

    This is the sentence the whole reframe produces:

        "If you wait for Tuesday's meeting, CHF 180,000 of mitigation options
         expire. Convene tomorrow and you keep CHF 210,000 of them."

    Sika's colleagues come from economics and logistics (Q3). Option decay is
    their native vocabulary.
    """
    if not curve:
        return 0.0
    now_value = curve[0].recoverable_chf
    later = [p for p in curve if p.at <= until]
    later_value = later[-1].recoverable_chf if later else curve[-1].recoverable_chf
    return max(0.0, now_value - later_value)


def next_meeting(config: Config, clock: Clock) -> datetime | None:
    """When the standing meeting next sits.

    Modelled as a cadence rather than a calendar because we do not have their
    calendar. Swapping in the real one is a config change.
    """
    cadence = config.scoring["convene_rule"].get("meeting_cadence_days")
    if not cadence:
        return None
    # Next Tuesday 09:00 UTC as the stand-in for a weekly slot.
    days_ahead = (1 - clock.as_of.weekday()) % 7 or 7
    candidate = (clock.as_of + timedelta(days=days_ahead)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    return candidate


def evaluate(
    assessments: list[EventAssessment],
    curve: list[DecayPoint],
    config: Config,
    clock: Clock,
) -> ConveneVerdict:
    """Has the pre-agreed rule tripped?

    Every trigger is a quantity a planner can see on the board and check:
    expected loss, a count of contracts, a count of options. Nothing here
    depends on the summed value of acting, which is built on our invented
    action costs and is not a number anyone can stand behind.
    """
    rule = config.scoring["convene_rule"]
    thresholds = rule["thresholds"]
    agreed = rule.get("agreed_on") is not None

    exposure = sum(a.total_value_at_risk_chf for a in assessments)

    contracts: set[str] = set()
    for assessment in assessments:
        for risk in assessment.shipment_risks:
            if risk.do_nothing.expected_loss_chf > 0:
                contracts.add(risk.customer)

    meeting = next_meeting(config, clock)
    expiring = options_expiring_before(curve, meeting) if meeting else 0

    fired: list[str] = []
    if exposure >= thresholds["exposure_chf"]:
        fired.append(
            f"CHF {exposure:,.0f} of expected loss across the book "
            f"≥ CHF {thresholds['exposure_chf']:,.0f}"
        )
    if len(contracts) >= thresholds["contracts_exposed"]:
        fired.append(
            f"{len(contracts)} customer contracts exposed "
            f"≥ {thresholds['contracts_exposed']}"
        )
    if expiring >= thresholds["options_expiring_before_meeting"]:
        fired.append(
            f"{expiring} mitigation options expire before the next meeting "
            f"≥ {thresholds['options_expiring_before_meeting']}"
        )

    watch_fraction = float(rule.get("watch_fraction", 0.5))
    near = (
        exposure >= thresholds["exposure_chf"] * watch_fraction
        or len(contracts) >= max(1, int(thresholds["contracts_exposed"] * watch_fraction))
        or expiring >= max(1, int(
            thresholds["options_expiring_before_meeting"] * watch_fraction))
    )

    if fired:
        posture = Posture.CONVENE
    elif near:
        posture = Posture.WATCH
    else:
        posture = Posture.NORMAL

    headline = _headline(posture, fired, exposure, expiring, meeting, agreed)

    return ConveneVerdict(
        posture=posture,
        rule_agreed=agreed,
        triggers_fired=fired,
        exposure_chf=round(exposure, 2),
        contracts_exposed=len(contracts),
        options_expiring=expiring,
        next_meeting_at=meeting,
        headline=headline,
    )


def options_expiring_before(curve: list[DecayPoint], until: datetime) -> int:
    """How many mitigation options lapse between now and *until*.

    A count, not a price. Which options exist and when each expires comes
    straight from the min_action_hours table; what one is worth does not.
    """
    if not curve:
        return 0
    now_open = curve[0].actions_still_open
    later = [p for p in curve if p.at <= until]
    still_open = later[-1].actions_still_open if later else curve[-1].actions_still_open
    return max(0, now_open - still_open)


def _headline(
    posture: Posture,
    fired: list[str],
    exposure: float,
    expiring: int,
    meeting: datetime | None,
    agreed: bool,
) -> str:
    when = f"{meeting:%A %d %b}" if meeting else "the next meeting"

    if posture is Posture.CONVENE:
        prefix = (
            "Convene rule tripped"
            if agreed
            else "Proposed convene rule would trip (not yet agreed with the team)"
        )
        return (
            f"{prefix}: {fired[0]}. "
            f"{expiring} mitigation option(s) lapse before {when}."
        )

    if posture is Posture.WATCH:
        return (
            f"Watch. CHF {exposure:,.0f} of expected loss across the book; "
            f"{expiring} mitigation option(s) lapse before {when}. "
            "Below the convene threshold."
        )

    return (
        "Normal. Nothing on your lanes needs a decision today — "
        f"no mitigation option lapses before {when}."
    )
