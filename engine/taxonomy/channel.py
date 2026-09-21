"""Layer 3: what lateness costs, and how much of it is free.

A CONSEQUENCE MODEL, NOT A MULTIPLIER
=====================================
"Channel criticality 1-5" cannot express the distinction that actually
decides what to do, which is not how important the customer is but how much
lateness they absorb before anything happens:

    b2b_distributor     8 hours of dock flexibility, then CHF 180/day
    direct_to_jobsite   30 minutes, then a crew stands idle for a day
    retail_diy          1 hour, then a flat CHF 350 chargeback, and being
                        six days late costs the same as six hours

Those are three different SHAPES, not three points on one scale. A jobsite
and a retail store might both be "criticality 4", and the right action for
one is an expedite while for the other it is a phone call — because the
retail penalty stops growing and the jobsite penalty does not.

So the channel contributes two things and neither is a weight:

    free_hours    lateness inside it costs nothing
    penalty       what accrues once it is gone, in CHF, by shape

This also keeps the model honest about the clash the brief names. Channel
criticality cannot manufacture a cargo risk that physics says is absent, and
it does not shrink because the cargo turned out to be robust: a stopped
production line costs the same whether the missing pallet was fragile.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.config import Config


@dataclass(frozen=True)
class ChannelCost:
    """What being this late costs at this channel."""

    channel_id: str
    label: str
    free_hours: float
    chargeable_hours: float
    cost_chf: float
    shape: str
    reroute_flexible: bool
    explanation: str

    @property
    def within_tolerance(self) -> bool:
        return self.chargeable_hours <= 0.0


def cost_of_lateness(
    config: Config,
    channel_id: str,
    late_hours: float,
) -> ChannelCost:
    """CHF cost of arriving ``late_hours`` late at this channel.

    ``late_hours`` is lateness against the COMMITTED date, not delay against
    the plan. The gap between them is real commitment slack and charging
    across it is the arithmetic error this project corrected in the brief.
    """
    channels = config.raw("taxonomy")["layer3_channels"]
    spec = channels.get(channel_id)
    if spec is None:
        # Unknown channel: the most tolerant assumption, stated. Guessing a
        # harsh penalty for a channel nobody mapped would invent exposure.
        return ChannelCost(
            channel_id=channel_id,
            label=f"{channel_id} (not in the taxonomy)",
            free_hours=0.0,
            chargeable_hours=max(0.0, late_hours),
            cost_chf=0.0,
            shape="unknown",
            reroute_flexible=True,
            explanation=(
                f"Channel {channel_id!r} is not mapped, so no penalty schedule "
                "is applied. The lateness is reported; the cost is not invented."
            ),
        )

    free = float(spec.get("free_hours", 0.0))
    chargeable = max(0.0, late_hours - free)
    shape = spec.get("penalty_shape", "linear")

    if chargeable <= 0.0:
        return ChannelCost(
            channel_id=channel_id,
            label=spec["label"],
            free_hours=free,
            chargeable_hours=0.0,
            cost_chf=0.0,
            shape=shape,
            reroute_flexible=bool(spec.get("reroute_flexible", False)),
            explanation=(
                f"{late_hours:.1f} h late is inside {spec['label']}'s "
                f"{free:.1f} h tolerance — nothing is owed."
            ),
        )

    if shape == "step":
        cost = float(spec.get("step_chf", 0.0))
        why = (
            f"{spec['label']}: a flat CHF {cost:,.0f} chargeback once the "
            f"{free:.1f} h slot is missed. It does not grow with the delay, "
            f"so an expedite has to beat CHF {cost:,.0f} and no more."
        )
    elif shape == "cliff":
        cliff = float(spec.get("cliff_chf", 0.0))
        per_day = float(spec.get("per_day_chf", 0.0))
        cost = cliff + per_day * (chargeable / 24.0)
        why = (
            f"{spec['label']}: CHF {cliff:,.0f} the moment the window is "
            f"missed, then CHF {per_day:,.0f}/day. {chargeable:.1f} h "
            f"chargeable — CHF {cost:,.0f}."
        )
    else:
        per_day = float(spec.get("per_day_chf", 0.0))
        cost = per_day * (chargeable / 24.0)
        why = (
            f"{spec['label']}: CHF {per_day:,.0f}/day beyond {free:.1f} h of "
            f"tolerance. {chargeable:.1f} h chargeable — CHF {cost:,.0f}."
        )

    return ChannelCost(
        channel_id=channel_id,
        label=spec["label"],
        free_hours=free,
        chargeable_hours=chargeable,
        cost_chf=round(cost, 2),
        shape=shape,
        reroute_flexible=bool(spec.get("reroute_flexible", False)),
        explanation=why,
    )


def forbidden_actions(
    config: Config,
    cargo_class: str,
    candidate_assets: list[str],
) -> list[dict]:
    """Mitigations this cargo cannot legally take.

    Clash rule ``adr_blocks_the_obvious_mitigation``. Air freight is the
    standard expedite and is closed to Class 3 solvent; recommending it is
    worse than recommending nothing, because it looks like a plan and costs
    the planner the hours they had left.
    """
    cargo = config.raw("taxonomy")["layer4_cargo"].get(cargo_class, {})
    forbidden = set(cargo.get("forbidden_assets") or [])
    if not forbidden:
        return []
    assets = config.raw("taxonomy")["layer2_assets"]
    out = []
    for asset_id in candidate_assets:
        if asset_id in forbidden:
            out.append({
                "asset": asset_id,
                "label": assets.get(asset_id, {}).get("label", asset_id),
                "cargo_class": cargo_class,
                "reason": (
                    f"{cargo.get('label', cargo_class)} may not move by "
                    f"{assets.get(asset_id, {}).get('label', asset_id)}."
                ),
                "clash_rule": "adr_blocks_the_obvious_mitigation",
            })
    return out
