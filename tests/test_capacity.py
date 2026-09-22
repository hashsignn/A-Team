"""Capacity as a constraint, tested as rules rather than as numbers.

Each test here pins one sentence of the argument the 4flow material makes and
the engine previously could not represent:

    a barge is not a truck, and replacing one with the other is an arithmetic
    problem before it is a routing problem.

If any of these fail, the model has stopped saying that.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.fast import capacity
from engine.schemas import (
    ContractType,
    CustomerImpactTier,
    Leg,
    Mode,
    Shipment,
)

AS_OF = datetime(2026, 9, 22, tzinfo=UTC)
FAMILIES = ["mortars", "concrete_admixtures", "sealants", "waterproofing",
            "adhesives", "roofing_membranes", "flooring_resins"]


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def caps(config):
    return capacity.modes(config)


@pytest.fixture
def clock():
    return Clock.at(AS_OF)


def make_shipment(
    index: int = 0,
    family: str = "mortars",
    days_to_commit: float = 6.0,
    value_chf: float = 90_000.0,
    transit_hours: float = 78.0,
) -> Shipment:
    legs = [
        Leg(from_node="CHBSL", to_node="GAUGE_KAUB", mode=Mode.BARGE,
            planned_depart=AS_OF + timedelta(hours=8),
            planned_arrive=AS_OF + timedelta(hours=40),
            buffer_hours=12, carrier="CARR_RHN"),
        Leg(from_node="GAUGE_KAUB", to_node="NLRTM", mode=Mode.BARGE,
            planned_depart=AS_OF + timedelta(hours=42),
            planned_arrive=AS_OF + timedelta(hours=transit_hours),
            buffer_hours=12, carrier="CARR_RHN"),
    ]
    return Shipment(
        shipment_id=f"CAP-{index:04d}", lane_id="LANE_RHINE_01",
        origin_node="CHBSL", destination_node="NLRTM", mode="barge",
        legs=legs, carrier="CARR_RHN", contract_type=ContractType.AGREEMENT,
        etd=AS_OF, eta=AS_OF + timedelta(hours=transit_hours),
        otif_committed_date=AS_OF + timedelta(days=days_to_commit),
        value_chf=value_chf, product_family=family,
        customer=f"Customer {index % 5} AG",
        customer_impact_tier=CustomerImpactTier.STOCK_OUT,
        sla_penalty_per_day=500.0, dangerous_goods=False,
        temperature_controlled=False,
    )


def block(n: int, spread: int = 8, **kwargs) -> list[Shipment]:
    return [
        make_shipment(
            index=i,
            family=FAMILIES[i % len(FAMILIES)],
            days_to_commit=3.5 + (i % spread) * 1.05,
            **kwargs,
        )
        for i in range(n)
    ]


# =====================================================================
# The arithmetic the old engine could not do
# =====================================================================
def test_one_barge_is_a_hundred_odd_trucks(caps):
    """The sentence the whole module exists to make sayable."""
    barge, road = caps["barge"], caps["road"]
    assert road.units_to_replace(barge.tonnes_per_unit) == 103


def test_a_mode_that_cannot_be_loaded_in_time_offers_nothing(caps):
    """Rail needs three days to make up a train. That is not an answer to a
    two-day deadline however many slots the corridor has."""
    rail = caps["rail"]
    assert rail.hours_to_ready == 72
    assert rail.available_units(48) == 0
    assert rail.available_tonnes(48) == 0.0
    assert rail.available_units(168) > 0


def test_capacity_accrues_from_readiness_not_from_zero(caps):
    """Time spent waiting for a mode to be ready is not time it is loading.

    Counting from zero would hand a 49-hour deadline a whole week of sailings
    on a mode that takes 48 hours to make one up.
    """
    barge = caps["barge"]
    assert barge.loading_hours(48) == 0.0
    assert barge.loading_hours(72) == 24.0
    assert barge.available_tonnes(49) < barge.tonnes_per_week


def test_you_cannot_plan_on_part_of_a_sailing(caps):
    """Whole units only, and tonnes derived from them so the two agree.

    A row that offers a thousand tonnes and zero sailings is a row nobody
    believes twice.
    """
    barge = caps["barge"]
    for hours in (24, 49, 60, 72, 96, 120, 168):
        units = barge.available_units(hours)
        assert units == int(units)
        assert barge.available_tonnes(hours) == units * barge.tonnes_per_unit


def test_a_part_full_truck_is_still_a_truck(caps):
    """A consolidating mode is billed the fraction it fills; a truck is not.

    This is the difference that let 274 half-empty trucks fit inside a ceiling
    of 195 while the tonnage cleared.
    """
    road, barge = caps["road"], caps["barge"]
    assert road.units_needed_by(12.0) == 1.0        # half a truck is a truck
    assert road.units_needed_by(26.0) == 2.0        # one tonne over is two
    assert barge.units_needed_by(24.5) == pytest.approx(0.01)


def test_the_fleet_is_never_oversubscribed(config, caps, clock):
    """Whatever the plan, the units it books exist."""
    displaced = capacity.displaced_from(block(700), config, clock)
    for plan in capacity.plans(displaced, config, blocked_modes={"barge"}):
        for allocation in plan.allocations:
            available = caps[allocation.mode].available_units(plan.horizon_hours)
            assert allocation.whole_units <= available, (
                f"{plan.plan_id} booked {allocation.whole_units} "
                f"{allocation.mode} against {available} available"
            )


# =====================================================================
# Equipment: not every truck can move every load
# =====================================================================
def test_rail_does_not_carry_liquids(config, caps):
    assert capacity.equipment_class(config, "sealants") == "liquids"
    assert "liquids" not in caps["rail"].carries
    assert "liquids" in caps["road"].carries


def test_freight_with_nowhere_to_go_is_named_not_silently_dropped(
    config, clock
):
    """With the river and the road gone, liquids have no equipment left.

    The plan must SAY that. Quietly deferring them would report the same
    tonnage as "waiting for next week", which is a different and much more
    comfortable sentence than "nothing on this corridor can carry it".
    """
    displaced = capacity.displaced_from(
        block(40, spread=4), config, clock
    )
    plans = capacity.plans(
        displaced, config, blocked_modes={"barge", "road"}
    )
    assert plans
    for plan in plans:
        assert any("carries liquids" in limit for limit in plan.limits)
        assert not plan.feasible


# =====================================================================
# The plans themselves
# =====================================================================
def test_every_consignment_is_accounted_for(config, clock):
    """Allocated or deferred — never neither.

    A plan that reports full coverage by losing track of a third of the book
    is the dishonest version of this whole module.
    """
    ships = block(120)
    displaced = capacity.displaced_from(ships, config, clock)
    everyone = {s.shipment_id for s in ships}

    for plan in capacity.plans(displaced, config, blocked_modes={"barge"}):
        placed = [sid for a in plan.allocations for sid in a.shipment_ids]
        deferred = [d.shipment_id for d in plan.deferred]
        assert len(placed) == len(set(placed)), "a consignment on two modes"
        assert set(placed).isdisjoint(deferred)
        assert set(placed) | set(deferred) == everyone
        assert plan.displaced_tonnes == pytest.approx(
            sum(d.tonnes for d in displaced), abs=0.1
        )


def test_triage_cannot_hide_behind_the_volume_it_held_back(config, clock):
    """Deferring by choice is still deferring.

    Triage removes the slackest consignments before assigning so they do not
    compete for capacity. If they were then left out of the totals it would
    report a coverage it had not earned.
    """
    displaced = capacity.displaced_from(block(200, spread=10), config, clock)
    plans = {p.plan_id: p for p in capacity.plans(
        displaced, config, blocked_modes={"barge"}
    )}
    triage = plans.get("triage")
    if triage is None:
        pytest.skip("triage did not survive the dedupe on this block")

    assert triage.displaced_tonnes == pytest.approx(
        sum(d.tonnes for d in displaced), abs=0.1
    )
    assert triage.coverage < 1.0
    assert triage.deferred


def test_delivery_leads_the_ranking_not_cost(config, clock):
    """Same rule as the option ranking: coverage, then lateness, then money.

    A cheaper plan never outranks one that gets more of the freight there.
    """
    displaced = capacity.displaced_from(block(150), config, clock)
    ranked = capacity.plans(displaced, config, blocked_modes={"barge"})
    for earlier, later in zip(ranked, ranked[1:], strict=False):
        if earlier.feasible == later.feasible:
            assert (earlier.coverage, -earlier.worst_days_late) >= (
                later.coverage, -later.worst_days_late
            ), f"{later.plan_id} beats {earlier.plan_id} on delivery"


def test_identical_plans_collapse_into_one_tab(config, clock):
    """When one mode is left there is one answer, and saying so beats
    printing it four times."""
    displaced = capacity.displaced_from(block(60, spread=2), config, clock)
    plans = capacity.plans(
        displaced, config, blocked_modes={"barge", "rail"}, horizon_hours=36
    )
    assert len(plans) == 1
    assert plans[0].also, "the collapsed strategies should be named"


def test_a_plan_reports_the_consignments_it_sinks(config, clock):
    """The block can stay profitable while individual consignments do not,
    and summing first would hide exactly that."""
    ships = block(80, value_chf=9_000.0)
    book = {s.shipment_id: s for s in ships}
    displaced = capacity.displaced_from(ships, config, clock)

    priced = [
        capacity.price(p, book, config)
        for p in capacity.plans(displaced, config, blocked_modes={"barge"})
    ]
    assert any(p.unprofitable_ids for p in priced), (
        "a low-value block trucked at CHF 145/t must sink somebody"
    )
    for plan in priced:
        assert set(plan.unprofitable_ids) <= set(book)


def test_the_margin_veto_is_on_the_block_not_on_the_company(config, clock):
    ships = block(40)
    book = {s.shipment_id: s for s in ships}
    displaced = capacity.displaced_from(ships, config, clock)
    plan = capacity.price(
        capacity.plans(displaced, config, blocked_modes={"barge"})[0],
        book, config,
    )
    assert plan.margin is not None
    assert plan.margin.contribution_chf > 0
    assert plan.margin.margin_chf == pytest.approx(
        plan.margin.contribution_chf
        - plan.extra_cost_chf
        - plan.margin.residual_penalty_chf,
        abs=0.01,
    )


def test_later_units_leave_later(config, caps, clock):
    """A hundred trucks do not stand on the ramp together at hour twelve."""
    displaced = capacity.displaced_from(block(120, spread=6), config, clock)
    plans = capacity.plans(displaced, config, blocked_modes={"barge", "rail"})
    road = next(
        a for p in plans for a in p.allocations if a.mode == "road"
    )
    ready = sorted(road.ready_hours.values())
    assert ready[0] >= caps["road"].hours_to_ready
    assert ready[-1] >= ready[0] + caps["road"].headway_hours, (
        "the last truck leaves after the first"
    )


def test_a_deadline_inside_the_lead_time_reaches_nothing(config, clock):
    """Six hours of slack does not buy a truck that takes twelve to arrange.

    The plan must come back empty-handed and say so rather than quietly
    booking capacity that cannot exist yet.
    """
    displaced = capacity.displaced_from(block(40, spread=1), config, clock)
    assert max(d.hours_of_slack for d in displaced) < 12
    for plan in capacity.plans(displaced, config, blocked_modes={"barge"}):
        assert not plan.allocations
        assert plan.coverage == 0.0
        assert len(plan.deferred) == 40
        assert not plan.feasible


def test_a_switch_is_charged_the_difference_not_the_whole_move(config, clock):
    """The freight was always going to cost something to move.

    Billing the full road rate against the disruption would veto almost every
    switch on a long lane.
    """
    ships = block(20)
    displaced = capacity.displaced_from(ships, config, clock)
    caps_ = capacity.modes(config)
    tonnes = sum(d.tonnes for d in displaced)

    plans = capacity.plans(displaced, config, blocked_modes={"barge"})
    on_road = next(
        (a for p in plans for a in p.allocations if a.mode == "road"), None
    )
    assert on_road is not None
    full_price = caps_["road"].cost_chf_per_tonne * on_road.tonnes
    assert 0 < on_road.cost_chf < full_price
    assert on_road.cost_chf == pytest.approx(
        (caps_["road"].cost_chf_per_tonne - caps_["barge"].cost_chf_per_tonne)
        * on_road.tonnes,
        abs=0.01,
    )
    assert tonnes > 0


def test_nothing_displaced_means_no_plans(config, clock):
    assert capacity.plans([], config) == []


def test_slack_is_measured_against_the_commitment_not_the_departure(
    config, clock
):
    """A consignment three days into a ten-day lane has spent none of its
    commitment slack. Charging it for elapsed transit would make every long
    lane look critical from the moment it leaves."""
    tight = make_shipment(days_to_commit=3.5, transit_hours=78.0)
    loose = make_shipment(days_to_commit=12.0, transit_hours=78.0)
    assert capacity.hours_of_slack(tight, clock) < capacity.hours_of_slack(
        loose, clock
    )
    assert capacity.hours_of_slack(loose, clock) == pytest.approx(
        12 * 24 - 78, abs=0.1
    )


def test_units_never_disagree_with_tonnes_on_the_wire(config, caps, clock):
    """What the board renders has to be internally consistent."""
    displaced = capacity.displaced_from(block(300), config, clock)
    for plan in capacity.plans(displaced, config, blocked_modes={"barge"}):
        for row in plan.as_dict(caps)["allocations"]:
            cap = caps[row["mode"]]
            assert row["units"] <= row["units_available"]
            assert row["units"] >= math.ceil(
                row["tonnes"] / cap.tonnes_per_unit - 1e-9
            )
            assert row["tonnes_available"] == pytest.approx(
                row["units_available"] * cap.tonnes_per_unit
            )
