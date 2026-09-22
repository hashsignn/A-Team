"""Profitability as a veto, not as an objective.

The pivot this package implements is that delivery beats cost. That is a real
instruction and it has a real edge: taken literally it would charter a jet to
save a pallet of mortar. So cost does not disappear — it changes shape.

    BEFORE:  rank by expected loss; the cheapest survivable option wins.
    NOW:     discard options that turn this consignment into a loss;
             among the rest, the fastest wins outright.

A veto is the honest encoding of "cost is secondary but we must stay
profitable". A weight is not: any weight small enough to let delivery dominate
is also small enough to let a ruinous option through when the delay is large,
and any weight large enough to stop that is large enough to start trading days
for francs again. The threshold has no such failure mode, and a planner can
argue with a number in a config file in a way they cannot argue with a weight.

WHAT COUNTS AS THE MARGIN
-------------------------
Per consignment, not per company:

    contribution     = value_chf x gross_margin_rate(product family)
    residual penalty = days still late after acting x SLA rate,
                       plus the customer-impact charge if it is still late
    margin           = contribution - action cost - residual penalty

The rate comes from config. It is the one number here we cannot derive, so it
is declared rather than invented in code, and ``source`` on each entry says
whether anybody has checked it.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.config import Config
from engine.schemas import Shipment

# Used when fast.yaml is absent entirely. Deliberately pessimistic: a low
# assumed margin vetoes MORE options, so a missing config file makes the tool
# more cautious rather than less. The opposite default would let an
# unconfigured install authorise spend it has no basis for.
FALLBACK_MARGIN_RATE = 0.12
FALLBACK_FLOOR_CHF = 0.0


@dataclass(frozen=True)
class Margin:
    """What one option does to one consignment's contribution."""

    contribution_chf: float
    action_cost_chf: float
    residual_penalty_chf: float
    margin_chf: float
    floor_chf: float
    viable: bool
    reason: str | None
    rate_source: str

    @property
    def headroom_chf(self) -> float:
        """How much more could be spent before this option stops paying."""
        return round(self.margin_chf - self.floor_chf, 2)


def _fast_config(config: Config) -> dict:
    raw = config.raw("fast")
    return raw if isinstance(raw, dict) else {}


def margin_rate(config: Config, product_family: str) -> tuple[float, str]:
    """Gross margin rate for a family, and where the number came from."""
    block = _fast_config(config).get("gross_margin", {}) or {}
    families = block.get("by_product_family", {}) or {}
    entry = families.get(product_family) or block.get("default")

    if isinstance(entry, dict):
        rate = entry.get("rate")
        source = entry.get("source", "assumed")
        if isinstance(rate, (int, float)):
            return float(rate), str(source)
    if isinstance(entry, (int, float)):
        return float(entry), "assumed"

    return FALLBACK_MARGIN_RATE, "fallback — fast.yaml missing or incomplete"


def floor_chf(config: Config) -> float:
    """The margin an option must leave behind to be offered at all.

    Zero means "must not lose money". A positive number means "must still
    clear this much", which is how a controller expresses a policy stricter
    than break-even.
    """
    value = _fast_config(config).get("margin_floor_chf", FALLBACK_FLOOR_CHF)
    return float(value) if isinstance(value, (int, float)) else FALLBACK_FLOOR_CHF


def residual_penalty_chf(
    shipment: Shipment,
    config: Config,
    days_late_after: float,
) -> float:
    """What lateness still costs once the option has done its work.

    Nothing is charged for a consignment that lands on time — that is the
    whole point of paying for speed. A fraction of a day is charged as a
    fraction: rounding 0.3 days up to a full day's penalty would veto fast
    options for arriving very slightly late, which is exactly backwards.
    """
    if days_late_after <= 0:
        return 0.0

    penalty = days_late_after * float(shipment.sla_penalty_per_day)

    cost_cfg = config.scoring.get("cost", {}).get("components", {})
    impact = cost_cfg.get("customer_impact", {})
    if impact.get("enabled"):
        by_tier = impact.get("chf_by_tier", {})
        tier = shipment.customer_impact_tier.value
        penalty += float(by_tier.get(tier, by_tier.get(impact.get("default_tier"), 0.0)))

    return round(penalty, 2)


def evaluate(
    shipment: Shipment,
    config: Config,
    action_cost_chf: float,
    days_late_after: float,
) -> Margin:
    """Does this option leave the consignment profitable?"""
    rate, source = margin_rate(config, shipment.product_family)
    contribution = round(float(shipment.value_chf) * rate, 2)
    penalty = residual_penalty_chf(shipment, config, days_late_after)
    floor = floor_chf(config)
    margin = round(contribution - float(action_cost_chf) - penalty, 2)

    viable = margin >= floor
    reason = None
    if not viable:
        shortfall = round(floor - margin, 2)
        reason = (
            f"would leave CHF {margin:,.0f} against a floor of CHF {floor:,.0f} "
            f"— CHF {shortfall:,.0f} short"
        )

    return Margin(
        contribution_chf=contribution,
        action_cost_chf=round(float(action_cost_chf), 2),
        residual_penalty_chf=penalty,
        margin_chf=margin,
        floor_chf=floor,
        viable=viable,
        reason=reason,
        rate_source=source,
    )
