"""Delay penalties: what a customer's contract charges Sika for arriving late.

Penalties differ by the kind of customer, not by the shipment. A car maker's
line-feed contract recharges premium freight and the cost of a stopped line;
a DIY retailer fines a share of the order value when it misses its window; a
construction project flows its own delay damages down per day, with a cap;
a distributor, which holds stock, usually charges nothing. So the terms live
in one table, by customer type (scoring.yaml -> cost.components.
contractual_penalty.by_type), and each customer is given a type (by_customer).

A clause has at most five numbers, all optional:

    once_chf      charged once if late at all   (premium freight recharge)
    once_pct      share of the consignment value, once if late  (an OTIF fine)
    per_day_chf   per day late
    per_day_pct   share of the value per day late  (delay damages)
    per_week_pct  the same, stated per week, as supply contracts usually do
    cap_chf / cap_pct   the most the clause can ever charge
    grace_days    lateness inside it costs nothing

The switch is ``contractual_penalty.enabled``. Off, no clause is charged and
the cost of lateness is expediting plus customer impact alone, which is the
case the model was built to survive. The board can flip it without editing
any file (POST /api/penalties).

A shipment whose customer has no type falls back to its own record's
``sla_penalty_per_day``, which is where a real contract export would put it.
"""

from __future__ import annotations

import numpy as np

from engine.config import Config
from engine.schemas import Shipment


def _spec(config: Config) -> dict:
    return config.scoring.get("cost", {}).get("components", {}).get("contractual_penalty", {}) or {}


def enabled(config: Config) -> bool:
    return bool(_spec(config).get("enabled", False))


def type_of(customer: str, config: Config) -> str | None:
    spec = _spec(config)
    return (spec.get("by_customer") or {}).get(customer) or spec.get("default_type")


def clause(customer: str, config: Config) -> dict | None:
    """The customer's delay clause, with its type and label, or None."""
    kind = type_of(customer, config)
    terms = (_spec(config).get("by_type") or {}).get(kind) if kind else None
    if terms is None:
        return None
    return {"type": kind, **terms}


def _charge(terms: dict, value_chf: float, lateness: np.ndarray) -> np.ndarray:
    grace = float(terms.get("grace_days", 0.0))
    days = np.maximum(0.0, lateness - grace)
    late = days > 0.0
    once = float(terms.get("once_chf", 0.0)) + float(terms.get("once_pct", 0.0)) * value_chf
    per_day = (float(terms.get("per_day_chf", 0.0))
               + float(terms.get("per_day_pct", 0.0)) * value_chf
               + float(terms.get("per_week_pct", 0.0)) * value_chf / 7.0)
    total = np.where(late, once, 0.0) + per_day * days
    caps = [c for c in (terms.get("cap_chf"),
                        terms.get("cap_pct") and float(terms["cap_pct"]) * value_chf) if c]
    if caps:
        total = np.minimum(total, float(min(caps)))
    return total


def per_draw(shipment: Shipment, lateness_days: np.ndarray, config: Config) -> np.ndarray:
    """CHF of penalty in each Monte Carlo draw (zeros when switched off)."""
    lateness = np.asarray(lateness_days, dtype=float)
    if not enabled(config):
        return np.zeros_like(lateness)
    terms = clause(shipment.customer, config)
    if terms is None:
        return float(shipment.sla_penalty_per_day) * np.maximum(0.0, lateness)
    return _charge(terms, float(shipment.value_chf), lateness)


def for_days(shipment: Shipment, days_late: float, config: Config) -> float:
    """The same charge for one lateness figure (the fast path's margin)."""
    return float(per_draw(shipment, np.array([float(days_late)]), config)[0])


def summary(terms: dict) -> str:
    """The clause as one short line, for the Contract button."""
    parts = []
    if terms.get("once_chf"):
        parts.append(f"CHF {float(terms['once_chf']):,.0f} once late")
    if terms.get("once_pct"):
        parts.append(f"{float(terms['once_pct']):.0%} of the order once late")
    if terms.get("per_day_chf"):
        parts.append(f"CHF {float(terms['per_day_chf']):,.0f} a day")
    if terms.get("per_day_pct"):
        parts.append(f"{float(terms['per_day_pct']):.1%} of the order a day")
    if terms.get("per_week_pct"):
        parts.append(f"{float(terms['per_week_pct']):.1%} of the order a week")
    if terms.get("grace_days"):
        parts.append(f"after {float(terms['grace_days']):g} day(s) of grace")
    cap = []
    if terms.get("cap_chf"):
        cap.append(f"CHF {float(terms['cap_chf']):,.0f}")
    if terms.get("cap_pct"):
        cap.append(f"{float(terms['cap_pct']):.0%} of the order")
    if cap:
        parts.append("capped at " + " or ".join(cap))
    return ", ".join(parts) if parts else "no delay penalty"
