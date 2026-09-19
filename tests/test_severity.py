"""Tests for the five-level ladder and the board payload.

The ladder is the client's own scheme, so these tests pin the parts that are
theirs (the hour cutoffs, the directives) separately from the parts that are
ours (the material floor, the within-level ordering). When the real severity
formula lands, the second group is what should change.
"""

from __future__ import annotations

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.pipeline import RunOptions, run
from engine.schemas import ContractType, ShipmentOutcome, ShipmentRisk
from engine.score.severity import (
    LEVEL_RANK,
    Level,
    Verdict,
    classify,
    severity_score,
    worst,
)

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def board(config):
    context = run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150))
    return build_board(context)


def _risk(
    *,
    lead_hours: float | None,
    value_of_acting: float,
    expected_loss: float,
    shipment_id: str = "SYN-0001",
) -> ShipmentRisk:
    outcome = ShipmentOutcome(
        shipment_id=shipment_id,
        scenario="do_nothing",
        p_late=0.5,
        expected_delay_days=2.0,
        p90_delay_days=4.0,
        expected_lateness_days=1.0,
        expected_loss_chf=expected_loss,
        p90_loss_chf=expected_loss * 2,
        driving_event_ids=["E1"],
    )
    return ShipmentRisk(
        shipment_id=shipment_id,
        event_id="E1",
        do_nothing=outcome,
        best_action=None,
        act_outcome=None,
        value_of_acting_chf=value_of_acting,
        decision_deadline=None,
        lead_time_hours=lead_hours,
        actionability="comfortable",
        impact_band="B",
        probability_band="P2",
        value_chf=50_000.0,
        customer="Test Customer",
        contract_type=ContractType.AGREEMENT,
    )


# =====================================================================
# The ladder is a TIME-TO-ACT scale — these cutoffs are the client's
# =====================================================================


@pytest.mark.parametrize(
    "lead_hours, expected",
    [
        (1.0, Level.RED),       # "within 6 hours"
        (5.9, Level.RED),
        (6.1, Level.YELLOW),    # "within 24-48 hours"
        (47.0, Level.YELLOW),
        (49.0, Level.BLUE),     # "within 3-7 days"
        (167.0, Level.BLUE),
        (200.0, Level.WHITE),   # beyond the ladder: monitor, no decision yet
    ],
)
def test_level_follows_the_deadline_not_the_damage(lead_hours, expected, config):
    """Same money, different clock — only the clock moves the level."""
    risks = [_risk(lead_hours=lead_hours, value_of_acting=50_000, expected_loss=80_000)]
    assert classify(risks, config).level is expected


def test_no_events_is_green(config):
    verdict = classify([], config)
    assert verdict.level is Level.GREEN
    assert verdict.exposure_chf == 0.0
    assert "No external event" in verdict.reason


def test_touched_but_absorbed_is_white_not_green(config):
    """The reassuring answer, and a real one.

    A route an event reaches but whose buffers absorb it is not the same as a
    route nothing touched. White says "we looked, keep watching"; Green says
    "nothing is there".
    """
    risks = [_risk(lead_hours=100.0, value_of_acting=0.0, expected_loss=40.0)]
    verdict = classify(risks, config)
    assert verdict.level is Level.WHITE
    assert "buffers absorb" in verdict.reason


def test_material_exposure_with_no_option_left_is_red(config):
    """Nothing can be rescued, so the remaining action is immediate.

    'Too late to reroute' does not mean 'nothing to do' — it means tell the
    customer now, which the client's ladder puts at Red.
    """
    risks = [_risk(lead_hours=None, value_of_acting=0.0, expected_loss=90_000)]
    verdict = classify(risks, config)
    assert verdict.level is Level.RED
    assert "no mitigation option remains open" in verdict.reason


def test_the_earliest_deadline_on_the_route_sets_the_level(config):
    """The first option to expire starts the clock, not the average."""
    risks = [
        _risk(lead_hours=200.0, value_of_acting=10_000, expected_loss=20_000, shipment_id="A"),
        _risk(lead_hours=4.0, value_of_acting=10_000, expected_loss=20_000, shipment_id="B"),
        _risk(lead_hours=90.0, value_of_acting=10_000, expected_loss=20_000, shipment_id="C"),
    ]
    verdict = classify(risks, config)
    assert verdict.level is Level.RED
    assert verdict.lead_time_hours == 4.0


def test_every_verdict_carries_a_reason(config):
    """A level with no justification is an assertion nobody can argue with."""
    for risks in ([], [_risk(lead_hours=3, value_of_acting=9e4, expected_loss=9e4)]):
        assert classify(risks, config).reason.strip()


# =====================================================================
# Ranking — ours, and deliberately weight-free
# =====================================================================


def test_ranking_is_lexicographic_not_a_weighted_blend():
    """A Bias route must never outrank a Watch one, however rich it is.

    Blending level and money into one weighted score would allow exactly that,
    which is the "multiply several [0,1] factors together" mistake the brief
    warns against.
    """
    rich_white = Verdict(Level.WHITE, "r", None, exposure_chf=10_000_000, recoverable_chf=0)
    poor_blue = Verdict(Level.BLUE, "r", 100.0, exposure_chf=1.0, recoverable_chf=1.0)
    cap = 10_000_000
    assert severity_score(poor_blue, cap) > severity_score(rich_white, cap)


def test_score_stays_inside_its_own_rung():
    """The fraction must never cross a level boundary."""
    cap = 1000.0
    for level in Level:
        lo = severity_score(Verdict(level, "r", None, 0.0, 0.0), cap)
        hi = severity_score(Verdict(level, "r", None, cap * 10, 0.0), cap)
        rung = LEVEL_RANK[level] / 5
        assert rung <= lo <= hi < rung + 0.2 + 1e-9


def test_score_breaks_ties_by_exposure_within_a_level():
    cap = 100_000.0
    small = Verdict(Level.YELLOW, "r", 20.0, exposure_chf=1_000, recoverable_chf=0)
    large = Verdict(Level.YELLOW, "r", 20.0, exposure_chf=90_000, recoverable_chf=0)
    assert severity_score(large, cap) > severity_score(small, cap)


def test_zero_exposure_cap_does_not_divide_by_zero():
    assert severity_score(Verdict(Level.RED, "r", 1.0, 0.0, 0.0), 0.0) == 0.8


def test_worst_picks_the_most_urgent():
    v = worst([
        Verdict(Level.WHITE, "r", None, 999_999, 0),
        Verdict(Level.RED, "r", 2.0, 10, 0),
        Verdict(Level.BLUE, "r", 100.0, 500, 0),
    ])
    assert v.level is Level.RED


def test_worst_of_nothing_is_green():
    assert worst([]).level is Level.GREEN


# =====================================================================
# Board payload — what the UI actually consumes
# =====================================================================


def test_board_ranks_routes_descending(board):
    scores = [r["severity_score"] for r in board["routes"]]
    assert scores == sorted(scores, reverse=True)


def test_board_covers_every_lane(board, config):
    """Every lane appears, including the quiet ones.

    A route dropping off the board because nothing is wrong with it would make
    'no news' indistinguishable from 'not checked'.
    """
    assert len(board["routes"]) == len(config.lanes)
    assert {r["route_id"] for r in board["routes"]} == {lane["id"] for lane in config.lanes}


def test_level_counts_match_the_routes(board):
    for entry in board["levels"]:
        actual = sum(1 for r in board["routes"] if r["level"] == entry["level"])
        assert entry["count"] == actual


def test_every_route_has_drawable_geometry(board):
    for route in board["routes"]:
        assert route["legs"], f"{route['route_id']} has no legs"
        for leg in route["legs"]:
            assert len(leg["path"]) >= 2
            for lat, lon, alt in leg["path"]:
                assert -90 <= lat <= 90 and -180 <= lon <= 180
                assert alt > 0, "paths must float above the surface or they occlude"


def test_rhine_route_follows_the_river_through_kaub(board):
    """A great-circle arc Basel->Rotterdam passes nowhere near Kaub, and Kaub
    is the entire point of the anchor story."""
    rhine = next(r for r in board["routes"] if r["route_id"] == "LANE_RHINE_01")
    assert any(leg["routed_by"] == "river" for leg in rhine["legs"])
    assert any(leg["to"] == "GAUGE_KAUB" or leg["from"] == "GAUGE_KAUB"
               for leg in rhine["legs"])


def test_radar_axes_are_the_families_the_mask_checked(board, config):
    for route in board["routes"]:
        radar = route["radar"]
        assert len(radar["axes"]) == len(radar["axis_keys"])
        for band in ("severe", "moderate", "minor"):
            assert len(radar["series"][band]) == len(radar["axes"])
        for key in radar["axis_keys"]:
            assert key in config.families


def test_radar_never_shows_a_family_that_cannot_reach_the_route(board):
    """Port operations on a road-only inland lane would imply an assessment
    that never happened."""
    intra = next(r for r in board["routes"] if r["route_id"] == "LANE_EU_01")
    assert "waterway" not in intra["radar"]["axis_keys"]


def test_actions_are_only_those_worth_more_than_they_cost(board):
    for route in board["routes"]:
        for action in route["actions"]:
            assert action["value_chf"] > 0


def test_unsourceable_probability_survives_as_none(board):
    """It must reach the UI as null, never as a 0.5 somebody invented."""
    probs = [
        e["probability"]
        for r in board["routes"]
        for e in r["events"]
    ]
    assert any(p is None for p in probs), "no unsourced event in the fixture set"
    assert all(p is None or 0.0 <= p <= 1.0 for p in probs)
