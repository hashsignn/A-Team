"""The five-level indicator ladder, and route-level aggregation.

THE LADDER IS A TIME-TO-ACT SCALE, NOT A DAMAGE SCALE
=====================================================
Read the client's own definitions and this falls out:

    GREEN   Normal    no action is required
    WHITE   Bias      monitor closely, pay attention to changes
    BLUE    Watch     action should be DETERMINED within 3-7 days
    YELLOW  Alert     action should be TAKEN within 24-48 hours
    RED     Critical  action should be TAKEN within 6 hours

Every rung is phrased as a deadline. So the level is not "how bad is this" — it
is "how soon must somebody decide". That is exactly ``lead_time_hours``, which
the engine already computes from
``decision_deadline = impact_time − action_duration``.

No new machinery is needed. The ladder is a relabelling of a quantity that is
already there, which is why it can be wired straight through.

PROVISIONAL — THE CUTOFFS ARE THEIRS TO SET
-------------------------------------------
The thresholds below come from the client's own wording (6 h / 48 h / 7 days).
The two judgement calls that are NOT in their wording are marked ``ASSUMED``:

  * what counts as "something at stake" — the floor below which a touched route
    is White (monitor) rather than Blue (decide);
  * how routes are ordered *within* a level.

Both live in ``config.example/scoring.yaml`` under ``alert_levels``, and the
whole classification is this one function. When the real severity formula
arrives, it replaces ``classify()`` and nothing else moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from engine.config import Config
from engine.schemas import ShipmentRisk


class Level(str, Enum):
    """Ordered least to most urgent. The order is load-bearing — it is what
    ranks the board — so it is declared once, here."""

    GREEN = "green"
    WHITE = "white"
    BLUE = "blue"
    YELLOW = "yellow"
    RED = "red"


LEVEL_RANK: dict[Level, int] = {
    Level.GREEN: 0,
    Level.WHITE: 1,
    Level.BLUE: 2,
    Level.YELLOW: 3,
    Level.RED: 4,
}

LEVEL_LABEL: dict[Level, str] = {
    Level.GREEN: "Normal",
    Level.WHITE: "Bias",
    Level.BLUE: "Watch",
    Level.YELLOW: "Alert",
    Level.RED: "Critical",
}

LEVEL_DIRECTIVE: dict[Level, str] = {
    Level.GREEN: "No action required",
    Level.WHITE: "Monitor closely — watch for changes",
    Level.BLUE: "Determine action within 3–7 days",
    Level.YELLOW: "Take action within 24–48 hours",
    Level.RED: "Take action within 6 hours",
}


@dataclass(frozen=True)
class Verdict:
    """A level plus the sentence that justifies it.

    A level with no reason attached is an assertion a planner cannot argue
    with, so the reason travels with it everywhere — onto the globe tooltip,
    into the ranked table, into the export.
    """

    level: Level
    reason: str
    lead_time_hours: float | None
    exposure_chf: float
    recoverable_chf: float

    @property
    def rank(self) -> int:
        return LEVEL_RANK[self.level]

    @property
    def label(self) -> str:
        return LEVEL_LABEL[self.level]

    @property
    def directive(self) -> str:
        return LEVEL_DIRECTIVE[self.level]


def _thresholds(config: Config) -> dict:
    return config.scoring.get("alert_levels", {})


def classify(
    risks: list[ShipmentRisk],
    config: Config,
) -> Verdict:
    """Assign a level to a set of shipment risks (one route, or one event).

    THE SWAP POINT. When the real severity formula arrives it replaces the body
    of this function; callers, the API shape and the UI stay as they are.
    """
    spec = _thresholds(config)
    red_h = float(spec.get("red_hours", 6))
    yellow_h = float(spec.get("yellow_hours", 48))
    blue_h = float(spec.get("blue_hours", 168))
    material_floor = float(spec.get("material_chf", 1000))

    if not risks:
        return Verdict(
            level=Level.GREEN,
            reason="No external event currently intersects this route.",
            lead_time_hours=None,
            exposure_chf=0.0,
            recoverable_chf=0.0,
        )

    exposure = sum(r.do_nothing.expected_loss_chf for r in risks)
    recoverable = sum(r.value_of_acting_chf for r in risks if r.value_of_acting_chf > 0)

    # Nothing is genuinely at stake: the route is touched, but buffers absorb
    # it. That is a real answer and the reassuring one — BRIEF §5.7's green dot
    # that says "yes this touches you, but everything has four days of slack".
    if exposure < material_floor and recoverable <= 0:
        return Verdict(
            level=Level.WHITE,
            reason=(
                f"{len(risks)} shipment(s) intersect an event, but buffers absorb it "
                f"— exposure below CHF {material_floor:,.0f}. Monitor for change."
            ),
            lead_time_hours=None,
            exposure_chf=exposure,
            recoverable_chf=recoverable,
        )

    # Exposure is material but every option has already expired. You cannot
    # reroute, so the remaining action — tell the customer, re-agree the date —
    # is immediate. The client's ladder puts "act now" at Red, and that is
    # correct here even though nothing can be rescued.
    actionable = [
        r for r in risks
        if r.lead_time_hours is not None and r.value_of_acting_chf > 0
    ]
    if not actionable:
        return Verdict(
            level=Level.RED,
            reason=(
                f"CHF {exposure:,.0f} exposed and no mitigation option remains open. "
                "Notify the customer and re-agree the date now."
            ),
            lead_time_hours=None,
            exposure_chf=exposure,
            recoverable_chf=recoverable,
        )

    # The binding deadline is the EARLIEST one on the route: the first option to
    # expire sets the clock, not the average and not the most comfortable.
    soonest = min(r.lead_time_hours for r in actionable)  # type: ignore[type-var]

    if soonest <= red_h:
        level = Level.RED
    elif soonest <= yellow_h:
        level = Level.YELLOW
    elif soonest <= blue_h:
        level = Level.BLUE
    else:
        level = Level.WHITE

    when = (
        f"{soonest:.0f} h"
        if soonest < 48
        else f"{soonest / 24:.0f} days"
    )
    # Deliberately does NOT quote a recoverable CHF figure. That number comes
    # from our invented action costs and residual fractions — the least
    # defensible arithmetic in the system — so stating it here would lend it
    # the authority of the level itself. The deadline and the shipment count
    # are both directly observed.
    reason = (
        f"{len(actionable)} shipment(s) on this route still have an option "
        f"open; the first expires in {when}."
    )
    if level is Level.WHITE:
        reason += " No decision needed yet."

    return Verdict(
        level=level,
        reason=reason,
        lead_time_hours=soonest,
        exposure_chf=exposure,
        recoverable_chf=recoverable,
    )


def severity_score(verdict: Verdict, exposure_cap: float) -> float:
    """A 0–1 number for ranking, with no invented weights.

    Sorting is LEXICOGRAPHIC: level first, then CHF exposure within the level.
    That ordering needs no coefficients — it is the client's own ladder, with a
    tie-break everyone already agrees on.

    It is packed into one float only so the table can show a single column:

        score = (level_rank + exposure_fraction) / 5

    The integer part is the rung; the fraction never crosses a rung boundary, so
    a White route can never outrank a Blue one however much money is on it.
    Weighting the two together would allow exactly that, and would be the
    "multiply several [0,1] factors" mistake the brief warns against.
    """
    if exposure_cap <= 0:
        fraction = 0.0
    else:
        fraction = min(0.999, max(0.0, verdict.exposure_chf / exposure_cap))
    return round((verdict.rank + fraction) / len(LEVEL_RANK), 4)


def worst(verdicts: list[Verdict]) -> Verdict:
    """The most urgent of several. Used to colour a leg shared by many routes."""
    if not verdicts:
        return Verdict(Level.GREEN, "No events.", None, 0.0, 0.0)
    return max(verdicts, key=lambda v: (v.rank, v.exposure_chf))
