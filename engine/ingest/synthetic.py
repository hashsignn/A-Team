"""Synthetic shipment book — ~150 plausible shipments (BRIEF §3.3).

LABELLED SYNTHETIC EVERYWHERE. Every ``Shipment`` this module produces carries
``synthetic=True``, every id is prefixed ``SYN-``, and the dashboard says so on
screen. BRIEF §12: synthetic is fine, presenting it as real is not.

The generator is seeded off the as-of instant, so a given demo run is
reproducible, and a hindcast at a past as-of gets a book consistent with that
date rather than today's.
"""

from __future__ import annotations

import random
from datetime import timedelta

from engine.clock import Clock
from engine.config import Config
from engine.ingest import flows as flows_mod
from engine.schemas import (
    ContractType,
    CustomerImpactTier,
    Leg,
    Mode,
    Shipment,
)

# Synthetic customers, named so nobody can mistake them for real accounts.
CUSTOMERS = [
    ("Bauwerk Construction AG", CustomerImpactTier.LINE_DOWN),
    ("Nordbau Projekt GmbH", CustomerImpactTier.STOCK_OUT),
    ("Meridian Infrastructure", CustomerImpactTier.LINE_DOWN),
    ("Aurora Precast", CustomerImpactTier.STOCK_OUT),
    ("Helvetia Tunnelbau", CustomerImpactTier.LINE_DOWN),
    ("Pacific Rim Builders", CustomerImpactTier.STOCK_OUT),
    ("Delta Marine Works", CustomerImpactTier.INCONVENIENCE),
    ("Continental Roofing", CustomerImpactTier.INCONVENIENCE),
    ("Iberia Obras Civiles", CustomerImpactTier.STOCK_OUT),
    ("Gulf Industrial Projects", CustomerImpactTier.LINE_DOWN),
    ("Anhui Construction Group", CustomerImpactTier.STOCK_OUT),
    ("Midwest Commercial Build", CustomerImpactTier.INCONVENIENCE),
]

PRODUCT_FAMILIES = [
    ("sealants", False, False),
    ("adhesives", True, False),
    ("concrete_admixtures", False, False),
    ("waterproofing", False, False),
    ("flooring_resins", True, True),
    ("mortars", False, False),
    ("roofing_membranes", False, False),
    ("structural_bonding", True, True),
]

CARRIERS_BY_MODE = {
    Mode.BARGE: ["CARR_RHN"],
    Mode.SEA: ["CARR_DSL", "CARR_MED"],
    Mode.RAIL: ["CARR_EUR"],
    Mode.ROAD: ["CARR_ALP"],
}


def generate_shipments(
    config: Config,
    clock: Clock,
    count: int = 150,
    seed: int | None = None,
) -> list[Shipment]:
    """Build a synthetic shipment book around *clock*.

    Shipments are spread from 20 days in the past to 35 days ahead, so the book
    contains both in-flight and planned movements. BRIEF §13 recommends both,
    with planned ranked lower — the 48-hour lead-time value mostly lives in
    shipments that have not left yet.
    """
    rng = random.Random(seed if seed is not None else int(clock.as_of.timestamp()))
    lanes = config.lanes
    shipments: list[Shipment] = []

    # How often each lane is drawn. With Sika's flow export present this is
    # their real document count per country pair; without it, the old rule.
    #
    # The old rule was a thumb on the scale and said so: it weighted the Rhine
    # up 3x because the demo needed shipments there. Calibrating against the
    # export replaces that with evidence — and the first thing it did was drop
    # the Rhine to nothing, because those lanes ended at Rotterdam and Sika
    # ships nothing TO the Netherlands. Rotterdam is transit. The corridor
    # earns its share back through the through-lanes, from real volume.
    weights = flows_mod.lane_weights(config)

    # When the book moves. Sika's export is strongly seasonal — April runs at
    # 1.52x the mean and August at 0.56x, so nearly threefold between peak and
    # trough — and a uniform spread of departure dates throws that away. A
    # disruption in a quiet month and the same disruption in a busy one are
    # different events, and the whole point of the board is to say which.
    season = flows_mod.month_weights(flows_mod.load())

    for i in range(count):
        lane = rng.choices(lanes, weights=weights, k=1)[0]
        shipment = _build_one(lane, rng, clock, index=i, season=season)
        shipments.append(shipment)

    return shipments


# How many times to redraw a departure date before accepting whatever came
# up. Rejection sampling is the right shape here — it needs no inverse of the
# monthly distribution and stays correct if the shape changes — but it must
# not be able to spin, so it gives up and takes the last draw.
SEASON_TRIES = 12


def _departure(
    rng: random.Random,
    clock: Clock,
    season: dict[str, float],
) -> timedelta:
    """An offset into the planning window, weighted by month if we know it.

    The window is 20 days behind to 35 ahead, so it spans at most three
    calendar months. Seasonality within it is a nudge, not a cliff — which is
    correct: a book is not emptied because August is quiet.
    """
    low, high = -20 * 24, 35 * 24
    if not season:
        return timedelta(hours=rng.uniform(low, high))

    ceiling = max(season.values())
    offset = rng.uniform(low, high)
    for _ in range(SEASON_TRIES):
        month = (clock.as_of + timedelta(hours=offset)).strftime("%m")
        if rng.random() <= season.get(month, 1.0) / ceiling:
            break
        offset = rng.uniform(low, high)
    return timedelta(hours=offset)


def _build_one(
    lane: dict,
    rng: random.Random,
    clock: Clock,
    index: int,
    season: dict[str, float] | None = None,
) -> Shipment:
    depart = clock.as_of + _departure(rng, clock, season or {})

    legs: list[Leg] = []
    cursor = depart
    for leg_spec in lane["legs"]:
        mode = Mode(leg_spec["mode"])
        # Real transit varies around the plan; the plan is what is recorded.
        transit = float(leg_spec["transit_hours"])
        arrive = cursor + timedelta(hours=transit)
        legs.append(
            Leg(
                from_node=leg_spec["from"],
                to_node=leg_spec["to"],
                mode=mode,
                planned_depart=cursor,
                planned_arrive=arrive,
                buffer_hours=float(leg_spec["buffer_hours"]),
                carrier=rng.choice(CARRIERS_BY_MODE[mode]),
            )
        )
        # Dwell between legs at the transfer node.
        dwell = rng.uniform(4, 20) if mode in (Mode.SEA, Mode.BARGE) else rng.uniform(2, 8)
        cursor = arrive + timedelta(hours=dwell)

    eta = legs[-1].planned_arrive

    # The customer commitment sits LATER than the plan. That gap is the
    # commitment slack, and keeping it distinct from leg buffer is what stops
    # the cost model charging penalties for delay that never breached the SLA.
    commitment_slack_hours = rng.choice([24, 48, 48, 72, 96, 120])
    committed = eta + timedelta(hours=commitment_slack_hours)

    modes = {leg.mode for leg in legs}
    mode_label = legs[0].mode.value if len(modes) == 1 else "multimodal"

    customer, tier = rng.choice(CUSTOMERS)
    product, dangerous, temp_controlled = rng.choice(PRODUCT_FAMILIES)

    value = round(rng.lognormvariate(10.6, 0.85), 2)
    value = float(min(max(value, 4_000.0), 480_000.0))

    # 70% of shipments sit under freight agreements (BRIEF §1).
    contract = ContractType.AGREEMENT if rng.random() < 0.70 else ContractType.SPOT

    # Per-day penalties are NOT universal. Sika has not confirmed these exist;
    # roughly half of synthetic contracts carry none, so the cost model is
    # exercised in both states rather than silently assuming the easy one.
    if rng.random() < 0.5:
        penalty = 0.0
    else:
        penalty = round(value * rng.uniform(0.004, 0.02), 2)

    return Shipment(
        shipment_id=f"SYN-{index + 1:04d}",
        lane_id=lane["id"],
        origin_node=legs[0].from_node,
        destination_node=legs[-1].to_node,
        mode=mode_label,  # type: ignore[arg-type]
        legs=legs,
        carrier=legs[-1].carrier,
        contract_type=contract,
        etd=depart,
        eta=eta,
        otif_committed_date=committed,
        value_chf=value,
        product_family=product,
        customer=customer,
        customer_impact_tier=tier,
        sla_penalty_per_day=penalty,
        dangerous_goods=dangerous,
        temperature_controlled=temp_controlled,
        synthetic=True,
    )


def in_scope(shipment: Shipment, clock: Clock) -> bool:
    """Is this shipment still capable of being affected?

    A shipment that has already been delivered cannot be rescued, and keeping
    it on the board is exactly the noise the tool exists to remove.
    """
    return shipment.otif_committed_date > clock.as_of
