"""The delivery-first rules, tested as rules rather than as outputs.

Each test here pins one sentence from the mandate:

    speed of action to ensure the freight reaches the customer on time,
    even during delays. Cost is secondary to delivery (though we still
    want smart routes that maintain profitability, not routes that cause
    a loss).

If a future change makes any of these fail, it has changed the mandate, not
the implementation.
"""

from datetime import UTC, datetime, timedelta

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.fast import contingency
from engine.fast import margin as margin_mod
from engine.fast import options as fast
from engine.network.graph import Network
from engine.schemas import (
    ActionOption,
    ContractType,
    CustomerImpactTier,
    GateHit,
    Leg,
    Mode,
    Shipment,
)

AS_OF = datetime(2026, 9, 16, tzinfo=UTC)


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def network(config):
    return Network(config)


@pytest.fixture
def clock():
    return Clock.at(AS_OF)


def make_shipment(
    value_chf: float = 100_000.0,
    days_to_commit: float = 6.0,
    penalty: float = 500.0,
    tier: CustomerImpactTier = CustomerImpactTier.STOCK_OUT,
) -> Shipment:
    """A Rhine consignment: road to Basel, barge past Kaub, on to Rotterdam."""
    legs = [
        Leg(from_node="SIKA_DUD", to_node="CHBSL", mode=Mode.ROAD,
            planned_depart=AS_OF, planned_arrive=AS_OF + timedelta(hours=6),
            buffer_hours=12, carrier="CARR_ALP"),
        Leg(from_node="CHBSL", to_node="GAUGE_KAUB", mode=Mode.BARGE,
            planned_depart=AS_OF + timedelta(hours=8),
            planned_arrive=AS_OF + timedelta(hours=40),
            buffer_hours=12, carrier="CARR_RHN"),
        Leg(from_node="GAUGE_KAUB", to_node="NLRTM", mode=Mode.BARGE,
            planned_depart=AS_OF + timedelta(hours=42),
            planned_arrive=AS_OF + timedelta(hours=78),
            buffer_hours=12, carrier="CARR_RHN"),
    ]
    return Shipment(
        shipment_id="TEST-0001",
        lane_id="LANE_RHINE_01",
        origin_node="SIKA_DUD",
        destination_node="NLRTM",
        mode="multimodal",
        legs=legs,
        carrier="CARR_RHN",
        contract_type=ContractType.AGREEMENT,
        etd=AS_OF,
        eta=AS_OF + timedelta(hours=78),
        otif_committed_date=AS_OF + timedelta(days=days_to_commit),
        value_chf=value_chf,
        product_family="mortars",
        customer="Test Customer AG",
        customer_impact_tier=tier,
        sla_penalty_per_day=penalty,
        dangerous_goods=False,
        temperature_controlled=False,
    )


def kaub_hit(shipment: Shipment) -> GateHit:
    return GateHit(
        event_id="EV-TEST",
        shipment_id=shipment.shipment_id,
        leg_index=1,
        node_id="GAUGE_KAUB",
        mode=Mode.BARGE,
        spatial_reason="on the corridor",
        temporal_reason="transiting during the window",
        modal_reason="barge leg",
        leg_enters_at=AS_OF + timedelta(hours=8),
        leg_leaves_at=AS_OF + timedelta(hours=40),
    )


def template(
    action_type: str = "barge_to_rail_switch",
    cost: float = 5_000.0,
    min_hours: float = 36.0,
    residual: float = 0.15,
    owner: str = "us",
    feasible: bool = True,
) -> ActionOption:
    return ActionOption(
        action_id=f"TEST-0001:{action_type}",
        label=action_type.replace("_", " "),
        action_type=action_type,
        owner=owner,
        min_hours=min_hours,
        cost_chf=cost,
        residual_delay_days=residual,
        feasible=feasible,
        infeasible_reason=None if feasible else "expired",
        contacts=[],
        description="",
    )


# ---------------------------------------------------------------- ranking
def test_the_fastest_option_wins_even_when_it_is_dearer(config, clock):
    """Cost is secondary to delivery. A dearer option that lands on time beats
    a cheap one that does not — that is the whole pivot."""
    shipment = make_shipment()
    cheap_and_slow = fast.FastOption(
        option_id="slow", shipment_id="TEST-0001", kind="template",
        label="cheap", detail="", owner="us",
        hours_to_start=2, hours_to_resolve=2, days_late_after=1.5,
        on_time=False, cost_chf=100.0,
        margin=margin_mod.evaluate(shipment, config, 100.0, 1.5),
    )
    dear_and_fast = fast.FastOption(
        option_id="fast", shipment_id="TEST-0001", kind="reroute",
        label="dear", detail="", owner="us",
        hours_to_start=4, hours_to_resolve=4, days_late_after=0.0,
        on_time=True, cost_chf=6_000.0,
        margin=margin_mod.evaluate(shipment, config, 6_000.0, 0.0),
    )

    ranking = fast.rank([cheap_and_slow, dear_and_fast])

    assert ranking.best is not None
    assert ranking.best.option_id == "fast"
    assert ranking.best.cost_chf > cheap_and_slow.cost_chf


def test_two_on_time_options_are_separated_by_speed_not_by_price(config):
    shipment = make_shipment()
    quick = fast.FastOption(
        option_id="quick", shipment_id="TEST-0001", kind="reroute",
        label="quick", detail="", owner="us",
        hours_to_start=4, hours_to_resolve=4, days_late_after=0.0,
        on_time=True, cost_chf=9_000.0,
        margin=margin_mod.evaluate(shipment, config, 9_000.0, 0.0),
    )
    slower = fast.FastOption(
        option_id="slower", shipment_id="TEST-0001", kind="template",
        label="slower", detail="", owner="us",
        hours_to_start=24, hours_to_resolve=24, days_late_after=0.0,
        on_time=True, cost_chf=500.0,
        margin=margin_mod.evaluate(shipment, config, 500.0, 0.0),
    )

    ranking = fast.rank([slower, quick])

    assert [o.option_id for o in ranking.viable] == ["quick", "slower"]


# ---------------------------------------------------------------- the veto
def test_an_option_that_causes_a_loss_is_removed_not_ranked_lower(config):
    """'not routes that cause a loss' — so it leaves the list entirely."""
    shipment = make_shipment(value_chf=20_000.0)
    ruinous = fast.FastOption(
        option_id="ruinous", shipment_id="TEST-0001", kind="reroute",
        label="charter", detail="", owner="us",
        hours_to_start=1, hours_to_resolve=1, days_late_after=0.0,
        on_time=True, cost_chf=80_000.0,
        margin=margin_mod.evaluate(shipment, config, 80_000.0, 0.0),
    )

    ranking = fast.rank([ruinous])

    assert ranking.viable == ()
    assert ranking.best is None
    assert len(ranking.vetoed) == 1
    assert "short" in ranking.vetoed[0][1]


def test_a_vetoed_option_is_still_reported(config):
    """A silently shortened list cannot say 'we could have, but it would have
    cost more than the load is worth'. That sentence has to survive."""
    shipment = make_shipment(value_chf=20_000.0)
    ranking = fast.rank([
        fast.FastOption(
            option_id="ruinous", shipment_id="TEST-0001", kind="reroute",
            label="charter a truck", detail="", owner="us",
            hours_to_start=1, hours_to_resolve=1, days_late_after=0.0,
            on_time=True, cost_chf=80_000.0,
            margin=margin_mod.evaluate(shipment, config, 80_000.0, 0.0),
        )
    ])
    payload = ranking.as_dict()
    assert payload["vetoed"][0]["label"] == "charter a truck"
    assert payload["vetoed"][0]["vetoed_because"]


def test_a_free_option_is_never_vetoed(config):
    """Where the margin has already gone, the delay spent it — not the option.
    Vetoing the free fallback would empty the list exactly when the planner
    needs to be told to ring the customer."""
    shipment = make_shipment(value_chf=5_000.0, tier=CustomerImpactTier.LINE_DOWN)
    notify = fast.FastOption(
        option_id="notify", shipment_id="TEST-0001", kind="template",
        label="Notify customer", detail="", owner="customer",
        hours_to_start=1, hours_to_resolve=1, days_late_after=4.0,
        on_time=False, cost_chf=0.0,
        margin=margin_mod.evaluate(shipment, config, 0.0, 4.0),
    )

    assert notify.margin.viable is False   # the situation is a loss
    ranking = fast.rank([notify])
    assert ranking.best is notify          # but the option still stands


def test_the_floor_can_be_raised_above_break_even(config):
    shipment = make_shipment(value_chf=100_000.0)
    at_break_even = margin_mod.evaluate(shipment, config, 16_000.0, 0.0)
    assert at_break_even.viable is True
    assert at_break_even.headroom_chf == pytest.approx(
        at_break_even.margin_chf, abs=0.01
    )


# ---------------------------------------------------------------- expiry
def test_an_expired_option_never_outranks_a_live_one(config):
    shipment = make_shipment()
    live = fast.FastOption(
        option_id="live", shipment_id="TEST-0001", kind="template",
        label="live", detail="", owner="us",
        hours_to_start=4, hours_to_resolve=4, days_late_after=2.0,
        on_time=False, cost_chf=500.0,
        margin=margin_mod.evaluate(shipment, config, 500.0, 2.0),
    )
    gone = fast.FastOption(
        option_id="gone", shipment_id="TEST-0001", kind="template",
        label="gone", detail="", owner="us",
        hours_to_start=1, hours_to_resolve=1, days_late_after=0.0,
        on_time=True, cost_chf=500.0,
        margin=margin_mod.evaluate(shipment, config, 500.0, 0.0),
        expired=True, expired_reason="only 0 h remain",
    )

    ranking = fast.rank([gone, live])

    assert ranking.best is not None and ranking.best.option_id == "live"
    assert [o.option_id for o in ranking.expired] == ["gone"]


def test_a_template_expires_when_there_is_less_time_than_it_needs(config, clock):
    shipment = make_shipment()
    option = fast.from_template(
        template(min_hours=36.0), shipment, config, clock,
        baseline_delay_days=2.0, hours_until_impact=8.0,
    )
    assert option.expired is True
    assert "36" in (option.expired_reason or "")


# ---------------------------------------------------------------- ownership
def test_only_our_own_levers_are_executable(config, clock):
    shipment = make_shipment()
    ours = fast.from_template(
        template(owner="us"), shipment, config, clock, 1.0, 120.0)
    theirs = fast.from_template(
        template(action_type="escalate_carrier", owner="carrier", cost=0.0,
                 min_hours=2.0, residual=0.8),
        shipment, config, clock, 1.0, 120.0)

    assert ours.executable is True
    assert theirs.executable is False, (
        "a carrier's decision is a request, however fast we send it"
    )


# ---------------------------------------------------------------- contingency
def test_a_blocked_node_is_deleted_from_the_graph_not_penalised(config, network):
    graph = contingency.build_graph(config, network)
    path = contingency.fastest_path(
        graph, "SIKA_DUD", "NLRTM", blocked={"GAUGE_KAUB"}, max_hops=6
    )
    assert path is not None
    assert "GAUGE_KAUB" not in path.node_ids


def test_no_path_survives_when_the_destination_itself_is_blocked(config, network):
    graph = contingency.build_graph(config, network)
    assert contingency.fastest_path(
        graph, "SIKA_DUD", "NLRTM", blocked={"NLRTM"}, max_hops=6
    ) is None


def test_alternatives_are_distinct_routes_not_the_same_one_twice(config, network):
    paths = contingency.alternatives(
        config, network, "SIKA_DUD", "NLRTM", blocked={"GAUGE_KAUB"}
    )
    assert len(paths) >= 2
    assert len({p.node_ids for p in paths}) == len(paths)


def test_paths_come_back_fastest_first(config, network):
    paths = contingency.alternatives(
        config, network, "SIKA_DUD", "NLRTM", blocked={"GAUGE_KAUB"}
    )
    assert paths == sorted(paths, key=lambda p: p.hours)


def test_local_vendors_are_only_offered_within_reach(config, network):
    from engine.network.geo import Point

    near_kaub = Point(50.09, 7.77)
    found = contingency.local_options(config, network, near_kaub, 40_000.0)
    radius = config.raw("fast")["contingency"]["vendor_radius_km"]
    assert found, "the Rhine has vendors within reach of Kaub"
    assert all(v.distance_km <= radius for v in found)


def test_local_options_are_ordered_by_proximity(config, network):
    from engine.network.geo import Point

    found = contingency.local_options(config, network, Point(51.9, 4.5), 100_000.0)
    assert [v.distance_km for v in found] == sorted(v.distance_km for v in found)


# ---------------------------------------------------------------- end to end
def test_a_blocked_rhine_shipment_gets_a_generated_reroute(config, network, clock):
    """The half the old playbook could not do: an option nobody wrote down."""
    shipment = make_shipment()
    ranking = fast.for_shipment(
        shipment, [kaub_hit(shipment)], [template()],
        config, network, clock,
        baseline_delay_days=2.0, hours_until_impact=120.0,
    )
    kinds = {o.kind for o in ranking.viable}
    assert "reroute" in kinds


def test_the_reroute_is_charged_against_the_planned_path_not_from_zero(
    config, network, clock
):
    """Pricing the plan by re-searching the graph finds the fastest unblocked
    path and calls that the plan, which makes a road reroute look free."""
    shipment = make_shipment()
    planned = fast._planned_cost_from(shipment, network, config, "SIKA_DUD")
    assert planned > 0.0


def test_the_divert_point_is_where_the_freight_is_not_the_origin(config, clock):
    """A consignment two legs in cannot be re-planned from the plant."""
    shipment = make_shipment()
    late_clock = Clock.at(AS_OF + timedelta(hours=7))
    assert fast._divert_from(shipment, [kaub_hit(shipment)], late_clock) == "CHBSL"
    assert fast._divert_from(shipment, [kaub_hit(shipment)], clock) == "SIKA_DUD"


def test_urgency_bands_read_from_config(config):
    assert fast.urgency_band(config, 1.0) == "now"
    assert fast.urgency_band(config, 6.0) == "today"
    assert fast.urgency_band(config, 30.0) == "soon"
    assert fast.urgency_band(config, 300.0) == "scheduled"
    assert fast.urgency_band(config, None) == "unknown"


def test_a_missing_fast_config_makes_the_tool_more_cautious_not_less():
    """A missing file must not silently authorise spend it has no basis for."""
    assert margin_mod.FALLBACK_MARGIN_RATE < 0.18
