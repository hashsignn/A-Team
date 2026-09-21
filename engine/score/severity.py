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

import math
from dataclasses import dataclass, field
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
    # How the clock was compressed, and by what. Carried so a planner can ask
    # "why is this Watch and not Bias" and get the arithmetic back rather
    # than an assertion.
    urgency: dict = field(default_factory=dict)

    @property
    def rank(self) -> int:
        return LEVEL_RANK[self.level]

    @property
    def label(self) -> str:
        return LEVEL_LABEL[self.level]

    @property
    def directive(self) -> str:
        return LEVEL_DIRECTIVE[self.level]


# =====================================================================
# URGENCY COMPRESSION
# =====================================================================


def magnitude_term(exposure_chf: float, config: Config) -> float:
    """CHF exposure on [0, 1], log-scaled.

    Log because money here spans four orders of magnitude — CHF 900 to CHF
    400,000 on the same board — and a linear term saturates at the first big
    number, after which every large route looks identical.

    Anchored at the two figures already in this config: the material floor
    (below which nothing is at stake) and the convene threshold (big enough
    to pull the team out of their weekly cycle). No new parameter.
    """
    spec = config.scoring.get("urgency", {})
    floor = float(_thresholds(config).get("material_chf", 1000.0))
    reference = float(spec.get("magnitude_reference_chf", 150_000.0))
    if reference <= floor or exposure_chf <= floor:
        return 0.0
    span = math.log10(reference / floor)
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(exposure_chf / floor) / span))


def urgency_multiplier(
    exposure_chf: float,
    p_late: float,
    irreversible: bool,
    config: Config,
) -> tuple[float, dict]:
    """U >= 1. Returns the multiplier and the working, for the audit trail.

    ADDITIVE on purpose. A product of [0, 1] factors drives toward zero as
    dimensions are added, so the model would get quieter the more it was
    taught — backwards. A bounded sum cannot, and each term stays separately
    inspectable.

    U is never below 1: compression may only make a deadline sooner, never
    later. A term that could push a deadline further out would let a large
    exposure HIDE a real clock, which is the opposite of the failure this is
    for.
    """
    spec = config.scoring.get("urgency", {})
    weights = spec.get("weights", {})
    w_m = float(weights.get("magnitude", 0.0))
    w_l = float(weights.get("likelihood", 0.0))
    w_d = float(weights.get("irreversible", 0.0))

    m = magnitude_term(exposure_chf, config)
    likely = max(0.0, min(1.0, float(p_late)))
    damage = 1.0 if irreversible else 0.0

    multiplier = 1.0 + w_m * m + w_l * likely + w_d * damage
    return multiplier, {
        "multiplier": round(multiplier, 3),
        "magnitude": round(m, 3),
        "likelihood": round(likely, 3),
        "irreversible": bool(irreversible),
        "weights": {"magnitude": w_m, "likelihood": w_l, "irreversible": w_d},
    }


def max_multiplier(config: Config) -> float:
    weights = config.scoring.get("urgency", {}).get("weights", {})
    return 1.0 + sum(float(v) for v in weights.values())


def smallest_threshold_ratio(config: Config) -> float:
    """The tightest gap between adjacent rungs.

    U_max must stay below this, and that is what makes a single compression
    unable to advance more than one rung. With the client's own 6/48/168 the
    ratios are 3.5 and 8.0, so anything under 3.5 is safe.
    """
    spec = _thresholds(config)
    red = float(spec.get("red_hours", 6))
    yellow = float(spec.get("yellow_hours", 48))
    blue = float(spec.get("blue_hours", 168))
    ratios = [r for r in (yellow / red, blue / yellow) if r > 0]
    return min(ratios) if ratios else float("inf")


def _level_for_hours(hours: float, config: Config) -> Level:
    spec = _thresholds(config)
    if hours <= float(spec.get("red_hours", 6)):
        return Level.RED
    if hours <= float(spec.get("yellow_hours", 48)):
        return Level.YELLOW
    if hours <= float(spec.get("blue_hours", 168)):
        return Level.BLUE
    return Level.WHITE


def _apply_dead_band(
    raw_hours: float, effective_hours: float, config: Config
) -> tuple[Level, bool]:
    """Re-level only if the compressed clock clears the boundary decisively.

    Near a threshold ANY continuous modifier tips: 176 h with CHF 2,000 still
    crosses 168. Without a dead-band a route churns between rungs on
    successive runs as the exposure wobbles, and a level that flickers is a
    level nobody believes.
    """
    raw_level = _level_for_hours(raw_hours, config)
    new_level = _level_for_hours(effective_hours, config)
    if new_level is raw_level:
        return raw_level, False

    band = float(config.scoring.get("urgency", {}).get("dead_band", 0.0))
    if band <= 0.0:
        return new_level, True

    spec = _thresholds(config)
    boundary = {
        Level.RED: float(spec.get("red_hours", 6)),
        Level.YELLOW: float(spec.get("yellow_hours", 48)),
        Level.BLUE: float(spec.get("blue_hours", 168)),
        Level.WHITE: float("inf"),
    }[new_level]
    if boundary == float("inf"):
        return new_level, True
    # Must be inside the new rung by the band, not merely across the line.
    if effective_hours <= boundary * (1.0 - band):
        return new_level, True
    return raw_level, False


# =====================================================================
# CORROBORATION
# =====================================================================


def corroboration_cap(
    source_tiers: list[int] | None, config: Config
) -> tuple[Level | None, str | None]:
    """The highest rung an uncorroborated low-tier report may reach.

    Sika, answering Q5: factor in social media, in line with the
    corroboration threshold. Social media is not a BETTER signal, it is an
    EARLIER one — its whole value is arriving while there is still time to
    act. So it may raise a flag and may not on its own move a delivery date.

    Returns ``(None, None)`` when nothing is capped, which is the common case.
    """
    if not source_tiers:
        return None, None
    spec = config.scoring.get("corroboration", {})
    authoritative_at = int(spec.get("authoritative_tier_at_or_below", 2))
    needed = int(spec.get("tier3_sources_for_corroboration", 2))

    if any(t <= authoritative_at for t in source_tiers):
        return None, None
    low = [t for t in source_tiers if t > authoritative_at]
    if len(low) >= needed:
        return None, None

    cap_name = str(spec.get("uncorroborated_tier3_cap", "blue")).lower()
    try:
        cap = Level(cap_name)
    except ValueError:
        return None, None
    return cap, (
        f"Capped at {LEVEL_LABEL[cap]}: the only source is tier "
        f"{min(low)} and nothing corroborates it. It can raise a flag; it "
        f"cannot move a delivery date on its own."
    )


def _thresholds(config: Config) -> dict:
    return config.scoring.get("alert_levels", {})


def classify(
    risks: list[ShipmentRisk],
    config: Config,
    source_tiers: list[int] | None = None,
    irreversible_damage: bool = False,
) -> Verdict:
    """Assign a level to a set of shipment risks (one route, or one event).

    THE SEVERITY FORMULA.

    The ladder is a TIME-TO-ACT scale, so severity here is "how soon must
    somebody decide", never "how bad is this". The raw answer is the clock:

        tau = decision_deadline - as_of

    On its own that under-serves the question — two routes both a week out
    score identically whether CHF 900 or CHF 225,000 sits on them, which is
    the exact failure Sika named in Q6: a large, slow-building exposure stays
    in "monitor" until suddenly it is not, and by then the options are gone.

    So magnitude COMPRESSES the clock rather than replacing it:

        tau_effective = tau_binding / U
        U = 1 + 0.8*magnitude + 0.4*P(late) + 0.8*irreversible

    and tau_effective is read against the CLIENT'S OWN 6/48/168. Their
    thresholds are never touched: re-tuning them would make the ladder ours
    instead of theirs.

    U <= 3 is a load-bearing bound, not a preference. The adjacent threshold
    ratios are 168/48 = 3.5 and 48/6 = 8, so a U below 3.5 cannot cross two
    boundaries — money may make you decide sooner, it can never manufacture a
    six-hour emergency out of a week of slack. ``test_severity.py`` asserts
    that against the live config, so changing either side fails loudly.

    ``source_tiers`` and ``irreversible_damage`` are optional and default to
    the uninformed case, so every existing caller keeps working unchanged.
    """
    spec = _thresholds(config)
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
    binding = min(actionable, key=lambda r: r.lead_time_hours)  # type: ignore[arg-type]
    soonest = float(binding.lead_time_hours)  # type: ignore[arg-type]

    # ---- compress the clock -------------------------------------------
    enabled = bool(config.scoring.get("urgency", {}).get("enabled", False))
    if enabled:
        multiplier, working = urgency_multiplier(
            exposure, binding.do_nothing.p_late, irreversible_damage, config
        )
        effective = soonest / multiplier if multiplier > 0 else soonest
        level, moved = _apply_dead_band(soonest, effective, config)
        working.update({
            "raw_hours": round(soonest, 2),
            "effective_hours": round(effective, 2),
            "re_levelled": moved,
        })
    else:
        multiplier, effective = 1.0, soonest
        level = _level_for_hours(soonest, config)
        working = {"multiplier": 1.0, "raw_hours": round(soonest, 2),
                   "effective_hours": round(soonest, 2), "re_levelled": False}

    # ---- corroboration cap --------------------------------------------
    cap, cap_reason = corroboration_cap(source_tiers, config)
    capped = False
    if cap is not None and LEVEL_RANK[level] > LEVEL_RANK[cap]:
        working["capped_from"] = level.value
        level = cap
        capped = True
    working["capped"] = capped

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
    # Say WHY the clock was compressed, in the same sentence as the clock.
    # A level that moved for a reason the planner cannot see is a level they
    # will argue with, and they would be right to.
    if working.get("re_levelled"):
        drivers = []
        if working["magnitude"] >= 0.25:
            drivers.append(f"CHF {exposure:,.0f} at stake")
        if working["likelihood"] >= 0.5:
            drivers.append(f"{working['likelihood']:.0%} chance of lateness")
        if working["irreversible"]:
            drivers.append("cargo that cannot be saved by arriving later")
        if drivers:
            reason += (
                f" Treated as {working['effective_hours']:.0f} h rather than "
                f"{working['raw_hours']:.0f} h — {' and '.join(drivers)}."
            )
    if capped and cap_reason:
        reason += " " + cap_reason
    if level is Level.WHITE:
        reason += " No decision needed yet."

    return Verdict(
        level=level,
        reason=reason,
        lead_time_hours=soonest,
        exposure_chf=exposure,
        recoverable_chf=recoverable,
        urgency=working,
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
