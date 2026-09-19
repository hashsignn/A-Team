"""The convene rule: has a pre-agreed threshold been crossed?

WHY THIS MODULE EXISTS
======================
Sika, answering Q6:

    "In crisis, established teams that meet on a weekly schedule (e.g.
     Procurement, Manufacturing, Supply Chain, Controlling) increase meeting
     frequency e.g. from weekly to every day. They have full authority to
     decide on mitigation. **The real problem is that we declare a crisis too
     late and lose on available options.**"

Authority is not the bottleneck. Analysis is not the bottleneck. The decision
to convene is.

THE PART THAT CHANGES BEHAVIOUR IS NOT TECHNICAL
------------------------------------------------
Teams do not declare late because they lack a number. They declare late
because declaring is socially expensive — somebody has to stick their neck out
and risk crying wolf. So the tool must not argue for a crisis. It reports that
a threshold **the group pre-agreed in calm conditions** has been crossed:

    "Convene rule (agreed 12 Mar): expected loss above CHF 150k, and more than
     four customer contracts exposed. Both crossed at 06:14 today."

That moves the decision from a judgement one person owns to a rule the group
already owns. It costs about forty lines of YAML and it is the whole mechanism.

EVERY TRIGGER IS SOMETHING A PLANNER CAN CHECK
----------------------------------------------
Expected loss out of the Monte Carlo, a count of customer contracts, a count
of shipments against the clock. Nothing here rests on the summed value of
acting, which is built on our invented action costs and is not a number anyone
can stand behind.

WHAT WAS REMOVED, AND WHY
-------------------------
There was briefly a fourth quantity: the CHF value of mitigations lapsing
before the next meeting, plotted as a decay curve. It is gone.

The framing was borrowed from finance and does not belong in a freight
planner's hands. A planner has a cutoff to get a container to the terminal, or
a last train path to book — not an option with a strike price and a decay
rate. Dressing a booking deadline up as optionality makes the tool sound like
it is trading the freight rather than moving it.

The timing dimension it was reaching for is already carried, properly, by the
five-level ladder: every rung IS a deadline. Nothing was lost but the jargon.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine.clock import Clock
from engine.config import Config
from engine.schemas import ConveneVerdict, EventAssessment, Posture

# Shipments inside this many hours of their decision deadline count as
# pressing. It is the Alert rung of the client's own ladder, reused rather
# than invented: "action should be taken within 24-48 hours".
PRESSING_HOURS = 48.0


def next_meeting(config: Config, clock: Clock) -> datetime | None:
    """When the standing meeting next sits.

    Modelled as a cadence because we do not have their calendar. Swapping in
    the real one is a config change.
    """
    cadence = config.scoring["convene_rule"].get("meeting_cadence_days")
    if not cadence:
        return None
    # Next Tuesday 09:00 UTC as the stand-in for a weekly slot.
    days_ahead = (1 - clock.as_of.weekday()) % 7 or 7
    return (clock.as_of + timedelta(days=days_ahead)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


def evaluate(
    assessments: list[EventAssessment],
    config: Config,
    clock: Clock,
) -> ConveneVerdict:
    """Has the pre-agreed rule been crossed?"""
    rule = config.scoring["convene_rule"]
    thresholds = rule["thresholds"]
    agreed = rule.get("agreed_on") is not None

    exposure = sum(a.total_value_at_risk_chf for a in assessments)

    contracts: set[str] = set()
    pressing: set[str] = set()
    for assessment in assessments:
        for risk in assessment.shipment_risks:
            if risk.do_nothing.expected_loss_chf > 0:
                contracts.add(risk.customer)
            # A shipment whose decision deadline falls inside the Alert window.
            # Plain supply chain: a cutoff to switch to rail, not an option
            # expiring.
            if risk.lead_time_hours is not None and risk.lead_time_hours <= PRESSING_HOURS:
                pressing.add(risk.shipment_id)

    meeting = next_meeting(config, clock)

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
    if len(pressing) >= thresholds["shipments_needing_decision"]:
        fired.append(
            f"{len(pressing)} shipments need a decision within 48 hours "
            f"≥ {thresholds['shipments_needing_decision']}"
        )

    watch_fraction = float(rule.get("watch_fraction", 0.5))
    near = (
        exposure >= thresholds["exposure_chf"] * watch_fraction
        or len(contracts) >= max(1, int(thresholds["contracts_exposed"] * watch_fraction))
        or len(pressing) >= max(
            1, int(thresholds["shipments_needing_decision"] * watch_fraction)
        )
    )

    if fired:
        posture = Posture.CONVENE
    elif near:
        posture = Posture.WATCH
    else:
        posture = Posture.NORMAL

    return ConveneVerdict(
        posture=posture,
        rule_agreed=agreed,
        triggers_fired=fired,
        exposure_chf=round(exposure, 2),
        contracts_exposed=len(contracts),
        shipments_needing_decision=len(pressing),
        next_meeting_at=meeting,
        headline=_headline(posture, fired, exposure, len(pressing), meeting, agreed),
    )


def _headline(
    posture: Posture,
    fired: list[str],
    exposure: float,
    pressing: int,
    meeting: datetime | None,
    agreed: bool,
) -> str:
    when = f"{meeting:%A %d %b}" if meeting else "the next meeting"

    if posture is Posture.CONVENE:
        prefix = (
            "Convene rule crossed"
            if agreed
            else "Proposed convene rule would be crossed (not yet agreed with the team)"
        )
        return (
            f"{prefix}: {fired[0]}. "
            f"{pressing} shipment(s) need a decision within 48 hours, and the "
            f"team does not sit again until {when}."
        )

    if posture is Posture.WATCH:
        return (
            f"Watch. CHF {exposure:,.0f} of expected loss across the book; "
            f"{pressing} shipment(s) need a decision within 48 hours. "
            "Below the convene threshold."
        )

    return (
        "Normal. Nothing on your lanes needs a decision today, and nothing "
        f"falls due before {when}."
    )
