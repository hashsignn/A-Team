"""Gate, geometry and end-to-end pipeline tests.

The gate is the cheapest, most defensible filter in the system and it has zero
parameters — so it is the part most worth pinning down. All three conditions
get a test that fails for the right reason.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.gate.intersect import gate
from engine.ingest.observations import FeedStatus
from engine.network.geo import Point, cross_track_distance_km, haversine_km
from engine.network.graph import Network
from engine.pipeline import RunOptions, run
from engine.schemas import Event, Mode, Provenance, Severity
from engine.variables import rules
from engine.variables.mask import build_mask, describe_mask, mode_applies, node_exposed

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def network(config):
    return Network(config)


@pytest.fixture(scope="module")
def context(config):
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150))


# =====================================================================
# Geometry
# =====================================================================


@pytest.mark.parametrize(
    "a, b, expected_km",
    [
        # Rotterdam -> Hamburg
        ((51.95, 4.14), (53.54, 9.99), 431),
        # London -> Paris, a widely quoted ~344 km great circle
        ((51.51, -0.13), (48.86, 2.35), 344),
        # Equator quarter-circumference along a meridian: 10,007 km
        ((0.0, 0.0), (90.0, 0.0), 10007),
    ],
)
def test_haversine_against_known_distances(a, b, expected_km):
    d = haversine_km(Point(*a), Point(*b))
    assert d == pytest.approx(expected_km, rel=0.01)


def test_cross_track_distance_is_zero_on_the_line():
    a, b = Point(50.0, 0.0), Point(50.0, 10.0)
    on_line = Point(50.0, 5.0)
    assert cross_track_distance_km(on_line, a, b) < 12.0


def test_cross_track_falls_back_to_the_endpoint_beyond_the_segment():
    """Past either end the perpendicular is not on the segment.

    Without this, an event far beyond a leg's destination measures as "close to
    the corridor" and the spatial gate lets it through.
    """
    a, b = Point(50.0, 0.0), Point(50.0, 10.0)
    beyond = Point(50.0, 30.0)
    assert cross_track_distance_km(beyond, a, b) == pytest.approx(
        haversine_km(beyond, b), rel=0.01
    )
    behind = Point(50.0, -20.0)
    assert cross_track_distance_km(behind, a, b) == pytest.approx(
        haversine_km(behind, a), rel=0.01
    )


def test_rhine_barge_leg_follows_the_river_not_a_great_circle(network):
    """A straight line Basel→Rotterdam misses the Rhine entirely."""
    geometry = network.geometry("CHBSL", "GAUGE_KAUB", Mode.BARGE)
    assert geometry.routed_by == "river"
    # Kaub sits on the routed path.
    hit, distance = geometry.touches(Point(50.085, 7.765))
    assert hit and distance < 25


def test_sea_leg_routes_through_its_chokepoint(network):
    geometry = network.geometry("CHOKE_SUEZ", "CHOKE_BAB", Mode.SEA)
    # The Red Sea transit should pass near the middle of the Red Sea, not
    # across the Arabian peninsula.
    hit, _ = geometry.touches(Point(20.0, 38.5))
    assert hit


# =====================================================================
# The three gate conditions
# =====================================================================


def _event(node_ids, starts, ends, variables, modes, lat=None, lon=None) -> Event:
    return Event(
        event_id="TEST-EVT",
        title="test event",
        node_ids=node_ids,
        lat=lat,
        lon=lon,
        starts_at=starts,
        ends_at=ends,
        duration_confidence="stated",
        event_class="labour",
        active_variables=variables,
        severity=Severity.MODERATE,
        modes_affected=modes,
        realized=True,
        probability=1.0,
        probability_basis="test",
        provenance=Provenance(
            source="test", source_tier=1, verbatim_quote="test",
            inferred=False, retrieved_at=AS_OF.as_of,
        ),
    )


def test_modal_condition_rejects_a_rail_strike_on_a_sea_leg(config, network, context):
    sea = [s for s in context.shipments if any(leg.mode is Mode.SEA for leg in s.legs)]
    assert sea, "fixture book has no sea legs"
    shipment = sea[0]
    leg = next(leg for leg in shipment.legs if leg.mode is Mode.SEA)

    event = _event(
        node_ids=[leg.to_node],
        starts=leg.planned_depart,
        ends=leg.planned_arrive,
        variables=["LAB_RAIL_STRIKE"],
        modes=[Mode.RAIL],
    )
    hits, stats = gate([event], [shipment], network, config, AS_OF)
    assert hits == []
    assert stats.failed_modal > 0


def test_temporal_condition_rejects_a_strike_that_ends_before_arrival(
    config, network, context
):
    shipment = context.shipments[0]
    leg = shipment.legs[0]
    event = _event(
        node_ids=[leg.to_node],
        starts=leg.planned_depart - timedelta(days=30),
        ends=leg.planned_depart - timedelta(days=20),
        variables=["LAB_PORT_STRIKE"],
        modes=[leg.mode],
    )
    hits, stats = gate([event], [shipment], network, config, AS_OF)
    assert hits == []
    assert stats.failed_temporal > 0


def test_spatial_condition_rejects_an_event_far_off_the_corridor(
    config, network, context
):
    shipment = context.shipments[0]
    leg = shipment.legs[0]
    event = _event(
        node_ids=[],
        starts=leg.planned_depart,
        ends=leg.planned_arrive,
        variables=["CLI_STORM"],
        modes=[leg.mode],
        lat=-33.9,   # Cape Town: nowhere near a European inland leg
        lon=18.4,
    )
    hits, stats = gate([event], [shipment], network, config, AS_OF)
    assert hits == []
    assert stats.failed_spatial > 0


def test_a_matching_event_produces_a_hit_carrying_all_three_reasons(
    config, network, context
):
    shipment = context.shipments[0]
    leg = shipment.legs[0]
    event = _event(
        node_ids=[leg.to_node],
        starts=leg.planned_depart,
        ends=leg.planned_arrive + timedelta(hours=6),
        variables=["LAB_PORT_STRIKE"],
        modes=[leg.mode],
    )
    hits, _ = gate([event], [shipment], network, config, AS_OF)
    assert len(hits) == 1
    hit = hits[0]
    # Every hit explains itself; a planner is shown why, not asked to trust it.
    assert hit.spatial_reason and hit.temporal_reason and hit.modal_reason
    assert context.network.node(leg.to_node).name in hit.spatial_reason


# =====================================================================
# Exposure mask
# =====================================================================


def test_waterway_variables_only_touch_river_nodes(config):
    low_water = config.variables["WAT_LOW_WATER"]
    assert node_exposed(config.nodes["GAUGE_KAUB"], low_water).exposed
    assert node_exposed(config.nodes["CHBSL"], low_water).exposed
    # Singapore is a seaport, not a river node.
    assert not node_exposed(config.nodes["SGSIN"], low_water).exposed


def test_port_variables_do_not_touch_inland_plants(config):
    congestion = config.variables["POR_CONGESTION"]
    assert node_exposed(config.nodes["NLRTM"], congestion).exposed
    assert not node_exposed(config.nodes["SIKA_STU"], congestion).exposed


def test_exposure_verdicts_always_carry_a_sentence(config):
    node = config.nodes["NLRTM"]
    for var in config.variables.values():
        verdict = node_exposed(node, var)
        assert verdict.reason, f"{var.id} produced a verdict with no reason"


def test_unknown_exposure_rule_refuses_rather_than_guessing(config):
    from engine.schemas import RiskVariable

    broken = RiskVariable(
        id="X", family="climate", name="X", description="x",
        modes_affected=[Mode.ROAD], typical_lead_time_hours=1,
        typical_duration_days=1, exposure={"nonsense_rule": True},
        probability_sourceable=False,
    )
    verdict = node_exposed(config.nodes["NLRTM"], broken)
    assert not verdict.exposed
    assert "not understood" in verdict.reason


def test_mask_is_sparse_and_describes_itself(config):
    mask = build_mask(["WAT_LOW_WATER", "POR_CONGESTION"], config.variables)
    assert sum(mask.values()) == 2
    assert len(mask) == 45
    description = describe_mask(mask, config.variables)
    assert "2 of 45" in description
    assert "Low water" in description


def test_mask_refuses_unknown_variable_ids(config):
    with pytest.raises(KeyError):
        build_mask(["NOT_A_VARIABLE"], config.variables)


def test_mode_applies_is_directional(config):
    rail_strike = config.variables["LAB_RAIL_STRIKE"]
    assert mode_applies(rail_strike, Mode.RAIL).exposed
    assert not mode_applies(rail_strike, Mode.SEA).exposed


# =====================================================================
# Rules router (the challenger)
# =====================================================================


def test_router_abstains_rather_than_guessing(config):
    result = rules.route("Quarterly earnings beat expectations.", config.variables)
    assert result.abstained
    assert result.abstain_reason


def test_router_finds_a_strike_and_quotes_its_span(config):
    text = "Dockworkers at Antwerp voted to strike from Monday unless talks resume"
    result = rules.route(text, config.variables)
    assert "LAB_PORT_STRIKE" in result.active_variables
    assert result.matched_spans["LAB_PORT_STRIKE"]


def test_router_keeps_the_active_set_sparse(config):
    text = (
        "Storm, flood, strike, congestion, fog, ice, derailment, cyber attack, "
        "sanctions, tariff, blank sailing, grounding, collision and earthquake."
    )
    result = rules.route(text, config.variables)
    # k = 2..5 active out of 45. More than that and the match is spurious.
    assert len(result.active_variables) <= 5


def test_agreement_metric_is_decomposable(config):
    result = rules.route("Rail strike announced for Wednesday", config.variables)
    out = rules.agreement(result, ["LAB_RAIL_STRIKE", "CAP_BLANK_SAILING"])
    assert "LAB_RAIL_STRIKE" in out["agreed"]
    assert "CAP_BLANK_SAILING" in out["model_only"]
    assert 0.0 <= out["jaccard"] <= 1.0


# =====================================================================
# End to end
# =====================================================================


def test_pipeline_runs_and_the_rhine_event_reaches_the_board(context):
    titles = [a.event.title for a in context.result.assessments]
    assert any("Kaub" in t for t in titles), f"anchor event missing from {titles}"


def test_noise_events_get_no_dot(context):
    """Events touching nothing must not appear at all.

    That is the noise filter, and it sits upstream of severity: a real typhoon
    that threatens none of our freight is not a small dot, it is no dot.
    """
    ids = {a.event.event_id for a in context.result.assessments}
    assert "SYN-NEWS-007" not in ids  # typhoon, Philippine Sea
    assert "SYN-NEWS-009" not in ids  # Genoa crane repair, already finished


def test_every_board_event_touches_at_least_one_shipment(context):
    for assessment in context.result.assessments:
        assert assessment.shipments_affected > 0
        assert assessment.contracts_affected


def test_recoverable_value_never_counts_negative_options(context):
    """A shipment nobody should touch must not cancel out one that needs a
    decision."""
    for assessment in context.result.assessments:
        positive = sum(
            r.value_of_acting_chf
            for r in assessment.shipment_risks
            if r.value_of_acting_chf > 0
        )
        assert assessment.total_value_of_acting_chf == pytest.approx(positive, rel=1e-6)
        assert assessment.total_value_of_acting_chf >= 0


def test_funnel_counts_are_monotonically_non_increasing(context):
    f = context.result.funnel
    stages = [
        f.raw_observations, f.after_geographic, f.after_type,
        f.after_temporal, f.after_resolution,
    ]
    for earlier, later in zip(stages, stages[1:], strict=False):
        assert later <= earlier


def test_inputs_panel_declares_every_socket(context):
    """Absence is stated, never silent (BRIEF §8.2)."""
    absent = [r for r in context.reports if r.status is FeedStatus.ABSENT]
    assert absent, "no sockets declared — absences are being hidden"
    for report in context.reports:
        assert report.detail
        if report.status is not FeedStatus.CONNECTED:
            assert report.unlocks_if_connected, (
                f"{report.key} does not say what connecting it would buy"
            )


def test_synthetic_data_is_labelled_everywhere(context):
    assert all(s.synthetic for s in context.shipments)
    assert all(s.shipment_id.startswith("SYN-") for s in context.shipments)


def test_run_is_reproducible_at_a_pinned_as_of(config):
    a = run(clock=AS_OF, config=config, options=RunOptions(shipment_count=80, seed=42))
    b = run(clock=AS_OF, config=config, options=RunOptions(shipment_count=80, seed=42))
    assert a.result.convene.exposure_chf == b.result.convene.exposure_chf
    assert [x.event.event_id for x in a.result.assessments] == [
        x.event.event_id for x in b.result.assessments
    ]
