"""The 0-100 Alert Score, and why it is derived rather than computed.

WHY A SCALAR AT ALL
===================
A TMS wants a number. SAP TM and Blue Yonder both route on thresholds, and
"Yellow, act within 24-48 hours" does not fit in an integer field. So the
board emits a 0-100 score alongside the ladder.

WHY IT IS NOT ITS OWN MODEL
---------------------------
The obvious construction is the one the spec asks for:

    score = f(severity) x overlap x vulnerability x criticality

and it has three problems that show up immediately in use.

**It collapses.** Four factors at 0.7 each give 0.24, which lands in Green.
Nothing about that shipment is green. Multiplying independent [0,1] terms
drives everything toward zero as you add dimensions, so the model gets
quieter the more you teach it — exactly backwards.

**It cannot be calibrated.** There is no observation that tells you whether
vulnerability ought to be 0.6 or 0.8. The product of four such numbers is
four unfalsifiable judgements wearing the costume of arithmetic.

**It can disagree with the ladder.** Two scales over the same shipment WILL
drift apart, and then the board says Yellow while the TMS says 38/Green, and
whichever a planner saw last is the one they act on. That is worse than
having no score.

SO THE SCORE IS A PROJECTION OF THE LADDER
------------------------------------------
One quantity, two renderings. The rung sets the decade, the consequence sets
the position inside it:

    score = 20 x rung_rank + 20 x consequence_fraction

    Normal    0-20  |  Bias  20-40  |  Watch  40-60
    Alert    60-80  |  Critical 80-100

which lands the requested bands on the client's own rungs without either
having to move:

    Green  0-39   Normal + Bias        informational, monitor
    Amber  40-69  Watch + low Alert    evaluate a reroute
    Red    70-100 high Alert + Critical act now

The fraction never crosses a decade boundary, so a Bias shipment with huge
exposure can never outscore a Watch one — the same lexicographic discipline
the ranked table already uses. Disagreement between the score and the ladder
is structurally impossible rather than merely unlikely, and there is a test
that says so.

WHERE THE FOUR FACTORS WENT
---------------------------
They are all still here; they enter where each can be sourced.

    severity      -> the delay distribution -> lead time -> the rung
    overlap       -> the gate, which is binary and already applied
    vulnerability -> the damage gate, which ESCALATES the rung
    criticality   -> the channel's penalty schedule -> the fraction

Nothing was dropped. It stopped being a multiplication because the four are
not the same kind of quantity, and multiplying a duration by a regulation by
a penalty schedule is a category error that a unit check would have caught.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.score.severity import LEVEL_LABEL, LEVEL_RANK, Level

# The requested bands, expressed over the client's five rungs.
BAND_GREEN = (0, 40)
BAND_AMBER = (40, 70)
BAND_RED = (70, 101)

DECADE = 100.0 / len(LEVEL_RANK)   # 20 points per rung


@dataclass(frozen=True)
class AlertScore:
    score: int
    band: str
    level: Level
    level_label: str
    rationale: str
    escalated: bool = False
    escalation_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "band": self.band,
            "level": self.level.value,
            "level_label": self.level_label,
            "rationale": self.rationale,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
        }


def band_for(score: int) -> str:
    if score >= BAND_RED[0]:
        return "red"
    if score >= BAND_AMBER[0]:
        return "amber"
    return "green"


def escalate_for_damage(level: Level, verdicts: list) -> tuple[Level, str | None]:
    """Irreversible cargo damage raises the rung.

    The ladder measures time-to-act, and irreversible damage compresses the
    time to act to nothing: once the emulsion has broken there is no action
    left that saves the product, so the decision — remake, re-source, tell the
    customer — has to be taken now rather than when the truck arrives.

    Reversible degradation does NOT escalate. It is a cost, and costs are
    already carried by the consequence term.
    """
    irreversible = [
        v for v in verdicts
        if getattr(v, "damage", None) and bool(v.damage) and v.irreversible
    ]
    if not irreversible:
        return level, None

    if level is Level.RED:
        return level, None

    raised = {
        Level.GREEN: Level.YELLOW,
        Level.WHITE: Level.YELLOW,
        Level.BLUE: Level.YELLOW,
        Level.YELLOW: Level.RED,
    }[level]
    return raised, (
        "Raised from "
        f"{LEVEL_LABEL[level]} to {LEVEL_LABEL[raised]}: the cargo can be "
        "irreversibly damaged, so there is no later moment at which the same "
        "decision is still available."
    )


def score_for(
    level: Level,
    consequence_chf: float,
    consequence_cap: float,
    escalation_reason: str | None = None,
) -> AlertScore:
    """Project one shipment's verdict onto 0-100.

    ``consequence_cap`` is the largest consequence on the current board, so
    the fraction is a position within today's book rather than against an
    absolute CHF figure nobody agreed. It is a RANKING aid, which is all the
    fraction is for — the rung carries the meaning.
    """
    rank = LEVEL_RANK[level]
    if consequence_cap <= 0:
        fraction = 0.0
    else:
        fraction = min(0.999, max(0.0, consequence_chf / consequence_cap))

    # Clamped INSIDE the rung's own decade. Without this, a Bias shipment
    # carrying the book's largest exposure rounds 39.98 up to 40 and lands in
    # Amber — the exact cross-rung leak the lexicographic packing exists to
    # prevent, arriving through the rounding rather than the maths.
    ceiling = DECADE * (rank + 1) - 1
    score = int(round(DECADE * rank + DECADE * fraction))
    score = max(int(DECADE * rank), min(int(ceiling), score))
    score = max(0, min(100, score))
    band = band_for(score)

    rationale = (
        f"{LEVEL_LABEL[level]} sets {DECADE * rank:.0f}-{DECADE * (rank + 1):.0f}; "
        f"consequence of CHF {consequence_chf:,.0f} places it at {score}. "
        f"Band {band.upper()}."
    )
    return AlertScore(
        score=score,
        band=band,
        level=level,
        level_label=LEVEL_LABEL[level],
        rationale=rationale,
        escalated=escalation_reason is not None,
        escalation_reason=escalation_reason,
    )


def bands() -> list[dict]:
    """The band table, as data, for the UI and the API contract."""
    return [
        {
            "band": "green",
            "range": [BAND_GREEN[0], BAND_GREEN[1] - 1],
            "rungs": [Level.GREEN.value, Level.WHITE.value],
            "action": "Informational; monitor only.",
        },
        {
            "band": "amber",
            "range": [BAND_AMBER[0], BAND_AMBER[1] - 1],
            "rungs": [Level.BLUE.value, Level.YELLOW.value],
            "action": "Warning; evaluate an alternate route or schedule change.",
        },
        {
            "band": "red",
            "range": [BAND_RED[0], 100],
            "rungs": [Level.YELLOW.value, Level.RED.value],
            "action": "Critical; immediate intervention.",
        },
    ]
