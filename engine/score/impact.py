"""Money (BRIEF §5.4) — and the two arithmetic corrections it needs.

The brief's formula is

    E[loss | do nothing] = penalty_per_day × E[delay] + P(late) × customer_cost

and it has two problems that would quietly hollow out the headline number.

**Lateness is not delay.** ``E[delay]`` is measured against the planned ETA.
Penalties accrue against the date promised to the customer. Those differ by the
commitment slack — you plan earlier than you promise — so the formula charges
penalty for delay that never breached the SLA. Fixed by computing
``max(0, arrival − otif_committed_date)`` per draw in simulate/draws.py.

**max(0, ·) is convex.** Applying it to the mean is not the same as taking the
mean of it, so it must live inside the expectation. Same fix.

The two terms also double-count: ``penalty_per_day × days`` and
``P(late) × customer_cost`` are both charged for the same lateness. Here they
are separated into genuinely different things — a contractual term that scales
with days, and a customer-impact term that does not.

WHY THREE COMPONENTS
--------------------
Sika has not confirmed that per-day contractual penalties exist in their
customer contracts. In B2B specialty chemicals they are much rarer than the
formula assumes; the real cost is expediting and customer escalation. If the
model leans entirely on ``penalty_per_day`` and a Sika colleague says "we don't
have those", the CHF axis goes to zero live on stage. So:

    contractual penalty   scales with days late   (often zero — that is fine)
  + expediting cost       incurred once if late
  + customer impact       categorical, set per customer by the planner

stays non-zero and defensible whatever their contracts say.

NO MULTIPLIED [0,1] FACTORS ANYWHERE. Chaining probabilities collapses
everything toward zero and the weights stop describing what the formula does.
"""

from __future__ import annotations

import numpy as np

from engine.config import Config
from engine.schemas import Shipment
from engine.simulate.draws import ShipmentDraws


def value_band(shipment: Shipment, config: Config) -> str:
    bands = config.scoring["cost"]["value_bands"]
    if shipment.value_chf < bands["low"]:
        return "low"
    if shipment.value_chf < bands["mid"]:
        return "mid"
    return "high"


def loss_per_draw(
    shipment: Shipment,
    draws: ShipmentDraws,
    config: Config,
    cost_multiplier: float = 1.0,
) -> np.ndarray:
    """CHF loss for each Monte Carlo draw.

    Returned per draw rather than as a mean, so the caller can sum across
    shipments *within a draw* and get a correctly correlated portfolio
    distribution. Summing means would throw that away.
    """
    cost = config.scoring["cost"]["components"]
    lateness = draws.lateness_days
    is_late = lateness > 0.0

    total = np.zeros_like(lateness)

    if cost["contractual_penalty"]["enabled"]:
        total += shipment.sla_penalty_per_day * lateness

    if cost["expediting"]["enabled"]:
        band = value_band(shipment, config)
        per_event = cost["expediting"]["chf_per_event_by_value_band"][band]
        total += np.where(is_late, float(per_event), 0.0)

    if cost["customer_impact"]["enabled"]:
        tiers = cost["customer_impact"]["chf_by_tier"]
        tier_value = tiers.get(
            shipment.customer_impact_tier.value,
            tiers[cost["customer_impact"]["default_tier"]],
        )
        total += np.where(is_late, float(tier_value), 0.0)

    # Surcharges (e.g. a Rhine low-water surcharge) raise the cost of moving
    # the freight whether or not it ends up late.
    if cost_multiplier != 1.0:
        surcharge = shipment.value_chf * 0.04 * (cost_multiplier - 1.0)
        total = total + surcharge

    return total


def summarise(
    shipment: Shipment,
    draws: ShipmentDraws,
    config: Config,
    cost_multiplier: float = 1.0,
) -> dict:
    """Headline numbers for one shipment under one scenario."""
    losses = loss_per_draw(shipment, draws, config, cost_multiplier)
    return {
        "p_late": draws.p_late,
        "expected_delay_days": draws.expected_delay,
        "p90_delay_days": draws.p90_delay,
        "expected_lateness_days": draws.expected_lateness,
        "expected_loss_chf": float(np.mean(losses)),
        "p90_loss_chf": float(np.percentile(losses, 90)),
        "losses": losses,
    }


def value_of_acting(
    loss_do_nothing: float,
    loss_after_acting: float,
    cost_of_acting: float,
) -> float:
    """BRIEF §5.4.

        Value of acting = E[loss|nothing] − E[loss|act] − cost_of_acting

    Recommend the action when this is positive. The output is not "risk score
    7.3" but a sentence:

        "Reroute via Antwerp: costs CHF 4,200, avoids CHF 11,800 of expected
         penalty. Net benefit CHF 7,600. Decide by Thursday 14:00."

    That sentence is the product.
    """
    return loss_do_nothing - loss_after_acting - cost_of_acting


def explain(
    loss_do_nothing: float,
    loss_after_acting: float,
    cost_of_acting: float,
    action_label: str,
    deadline_text: str | None,
) -> str:
    """The same arithmetic as prose, with no symbols.

    Sika's answer to Q3: these colleagues come from economics or logistics and
    can read a matrix and play with assumptions, but will not read the compute.
    So every number the UI shows has a plain-language form, and the formula is
    behind a "show the arithmetic" toggle for the sceptic in the room.
    """
    net = value_of_acting(loss_do_nothing, loss_after_acting, cost_of_acting)
    avoided = loss_do_nothing - loss_after_acting
    sentence = (
        f"{action_label}: costs CHF {cost_of_acting:,.0f}, "
        f"avoids CHF {avoided:,.0f} of expected loss. "
        f"Net benefit CHF {net:,.0f}."
    )
    if deadline_text:
        sentence += f" Decide by {deadline_text}."
    return sentence


def portfolio_distribution(
    per_shipment_losses: list[np.ndarray],
) -> dict[str, float]:
    """Total exposure across the book, correctly correlated.

    This is the payoff from sharing event draws. Because every shipment's loss
    array indexes the same iteration, summing elementwise preserves the fact
    that one Antwerp strike hits all of them at once. Summing the means would
    give the same central value and a badly understated tail — and the tail is
    what the convene decision turns on.
    """
    if not per_shipment_losses:
        return {"mean": 0.0, "p10": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0}

    total = np.sum(np.column_stack(per_shipment_losses), axis=1)
    return {
        "mean": float(np.mean(total)),
        "p10": float(np.percentile(total, 10)),
        "p50": float(np.percentile(total, 50)),
        "p90": float(np.percentile(total, 90)),
        "p99": float(np.percentile(total, 99)),
    }
