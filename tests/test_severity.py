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
from engine.score import severity as S
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
    p_late: float = 0.5,
) -> ShipmentRisk:
    outcome = ShipmentOutcome(
        shipment_id=shipment_id,
        scenario="do_nothing",
        p_late=p_late,
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


def _without_urgency(config):
    """The same config with the compression switched off.

    Deep-copied: mutating the module-scoped fixture would leak into every
    test that ran after it, and the failure would surface somewhere else
    entirely.
    """
    import copy

    plain = copy.deepcopy(config)
    plain.files["scoring"].data = copy.deepcopy(config.scoring)
    plain.files["scoring"].data.setdefault("urgency", {})["enabled"] = False
    return plain


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
def test_the_raw_ladder_is_the_clients_own_cutoffs(lead_hours, expected, config):
    """Uncompressed, the level IS the clock — 6 / 48 / 168 straight out of
    the client's own wording.

    Tested with the compression switched off rather than with a small
    exposure, because there is no such thing as a quiet shipment: P(late)
    alone contributes up to 0.4, so even CHF 1,050 compresses by a fifth.
    Turning the feature off is the only way to assert the underlying ladder,
    and it proves the switch works while it is at it.
    """
    plain = _without_urgency(config)
    risks = [_risk(lead_hours=lead_hours, value_of_acting=50_000, expected_loss=80_000)]
    verdict = classify(risks, plain)
    assert verdict.level is expected
    assert verdict.urgency["multiplier"] == 1.0


@pytest.mark.parametrize(
    "lead_hours, raw, compressed",
    [
        (6.1, Level.YELLOW, Level.RED),
        (49.0, Level.BLUE, Level.YELLOW),
        (200.0, Level.WHITE, Level.BLUE),
    ],
)
def test_large_exposure_compresses_the_clock_by_exactly_one_rung(
    lead_hours, raw, compressed, config
):
    """The Q6 fix, and the behaviour change this formula exists for.

    CHF 80,000 six hours out is not "act within 24-48 hours"; it is Critical.
    CHF 80,000 a week out is not "monitor"; it is a decision to take this
    week. Both of those used to read one rung calmer than they are, which is
    exactly the "we declare a crisis too late" failure the client named.
    """
    risks = [_risk(lead_hours=lead_hours, value_of_acting=50_000, expected_loss=80_000)]
    assert classify(risks, _without_urgency(config)).level is raw
    verdict = classify(risks, config)
    assert verdict.level is compressed
    assert verdict.urgency["re_levelled"] is True


def test_a_compressed_level_says_why_in_its_own_reason(config):
    """A level that moved for a reason the planner cannot see is a level they
    will argue with, and they would be right to."""
    verdict = classify(
        [_risk(lead_hours=200.0, value_of_acting=50_000, expected_loss=80_000)],
        config,
    )
    assert "rather than" in verdict.reason
    assert "at stake" in verdict.reason
    assert f"{verdict.urgency['raw_hours']:.0f} h" in verdict.reason


def test_the_reported_deadline_stays_the_real_one(config):
    """Compression changes the LEVEL, never the clock a planner works to.
    Telling somebody they have 105 hours when they have 200 would be a lie
    dressed as urgency."""
    verdict = classify(
        [_risk(lead_hours=200.0, value_of_acting=50_000, expected_loss=80_000)],
        config,
    )
    assert verdict.lead_time_hours == 200.0
    assert verdict.urgency["effective_hours"] < 200.0


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


# =====================================================================
# THE ONE-RUNG BOUND — the property that makes compression safe
# =====================================================================


def test_u_max_stays_below_the_tightest_threshold_ratio(config):
    """THE load-bearing invariant.

    A single compression can advance at most one rung IF AND ONLY IF U_max is
    below the smallest ratio between adjacent thresholds. With the client's
    6 / 48 / 168 those ratios are 8.0 and 3.5, so any U under 3.5 is safe and
    the configured 3.0 clears it.

    This asserts the two sides against each other rather than against
    constants, so changing EITHER the weights or the client's cutoffs fails
    here instead of quietly letting money manufacture an emergency.
    """
    u_max = S.max_multiplier(config)
    ratio = S.smallest_threshold_ratio(config)
    assert u_max < ratio, (
        f"U_max={u_max} is not below the tightest threshold ratio {ratio}; "
        "compression could advance two rungs"
    )


@pytest.mark.parametrize("lead_hours", [0.5, 5.9, 6.1, 18.0, 47.0, 49.0,
                                        100.0, 167.0, 169.0, 500.0, 5000.0])
def test_compression_never_advances_more_than_one_rung(lead_hours, config):
    """The bound, exercised. Every rung boundary, at maximum compression."""
    worst = [_risk(lead_hours=lead_hours, value_of_acting=50_000,
                   expected_loss=10_000_000, p_late=1.0)]
    raw = classify(worst, _without_urgency(config)).level
    got = classify(worst, config, irreversible_damage=True).level
    assert LEVEL_RANK[got] - LEVEL_RANK[raw] <= 1, (
        f"{lead_hours} h: {raw.value} -> {got.value} is more than one rung"
    )


def test_a_week_of_slack_can_never_become_a_six_hour_emergency(config):
    """The failure the bound exists to prevent, stated directly."""
    week_out = [_risk(lead_hours=169.0, value_of_acting=500_000,
                      expected_loss=50_000_000, p_late=1.0)]
    verdict = classify(week_out, config, irreversible_damage=True)
    assert verdict.level is not Level.RED
    assert verdict.lead_time_hours == 169.0


def test_compression_only_ever_makes_a_deadline_sooner(config):
    """U >= 1 always. A term that pushed a deadline further out would let a
    large exposure HIDE a real clock, which is the opposite of the point."""
    for exposure in (0.0, 1.0, 1_000.0, 80_000.0, 10_000_000.0):
        for p_late in (0.0, 0.5, 1.0):
            for damage in (False, True):
                multiplier, _ = S.urgency_multiplier(
                    exposure, p_late, damage, config
                )
                assert multiplier >= 1.0


def test_the_magnitude_term_is_log_scaled_not_linear(config):
    """Money here spans four orders of magnitude. A linear term saturates at
    the first big number, after which every large route looks identical."""
    floor = S.magnitude_term(1_000.0, config)
    mid = S.magnitude_term(12_247.0, config)   # geometric mean of 1k and 150k
    top = S.magnitude_term(150_000.0, config)
    assert floor == 0.0
    assert top == 1.0
    assert 0.45 < mid < 0.55, "a log scale puts the geometric mean near 0.5"
    # Linear would put it near 0.08.
    assert mid > 4 * (12_247.0 / 150_000.0)


def test_the_magnitude_term_is_clamped_at_both_ends(config):
    assert S.magnitude_term(0.0, config) == 0.0
    assert S.magnitude_term(-5.0, config) == 0.0
    assert S.magnitude_term(1e12, config) == 1.0


def test_the_dead_band_stops_a_marginal_nudge_from_re_levelling(config):
    """Near a boundary ANY continuous modifier tips. Without the band a route
    churns between rungs on successive runs as exposure wobbles, and a level
    that flickers is a level nobody believes."""
    import copy

    tight = copy.deepcopy(config)
    tight.files["scoring"].data = copy.deepcopy(config.scoring)
    tight.files["scoring"].data["urgency"]["dead_band"] = 0.0
    loose = copy.deepcopy(config)
    loose.files["scoring"].data = copy.deepcopy(config.scoring)
    loose.files["scoring"].data["urgency"]["dead_band"] = 0.6

    # 170 h, barely past the 168 h line, with a modest exposure.
    marginal = [_risk(lead_hours=170.0, value_of_acting=100.0,
                      expected_loss=1_400.0, p_late=0.1)]
    assert classify(marginal, tight).level is Level.BLUE
    assert classify(marginal, loose).level is Level.WHITE, (
        "a wide dead band should refuse a marginal re-levelling"
    )


# =====================================================================
# CORROBORATION — Q5, social media
# =====================================================================


def test_an_uncorroborated_tier3_report_cannot_move_a_delivery_date(config):
    """Sika, Q5. Social media is not a BETTER signal, it is an EARLIER one.
    It may raise a flag; it may not on its own set an Alert."""
    risks = [_risk(lead_hours=3.0, value_of_acting=50_000, expected_loss=80_000)]
    uncapped = classify(risks, config)
    capped = classify(risks, config, source_tiers=[3])
    assert uncapped.level is Level.RED
    assert capped.level is Level.BLUE
    assert "cannot move a delivery date" in capped.reason


def test_two_independent_tier3_sources_corroborate(config):
    risks = [_risk(lead_hours=3.0, value_of_acting=50_000, expected_loss=80_000)]
    assert classify(risks, config, source_tiers=[3, 3]).level is Level.RED


def test_an_authority_notice_needs_no_corroboration(config):
    """An authority notice is the record, not a claim about it."""
    risks = [_risk(lead_hours=3.0, value_of_acting=50_000, expected_loss=80_000)]
    assert classify(risks, config, source_tiers=[1]).level is Level.RED
    assert classify(risks, config, source_tiers=[2]).level is Level.RED


def test_the_cap_only_ever_lowers_a_level(config):
    """A cap must not PROMOTE a quiet route to Watch."""
    quiet = [_risk(lead_hours=5000.0, value_of_acting=10.0,
                   expected_loss=1_010.0, p_late=0.0)]
    assert classify(quiet, config, source_tiers=[3]).level is Level.WHITE


# =====================================================================
# Irreversible damage
# =====================================================================


def test_irreversible_damage_compresses_the_clock(config):
    """The ladder measures time-to-act, and irreversible damage compresses it
    to nothing: once the emulsion has broken there is no later moment at
    which the same decision is still available."""
    risks = [_risk(lead_hours=100.0, value_of_acting=50_000, expected_loss=20_000)]
    without = classify(risks, config)
    with_damage = classify(risks, config, irreversible_damage=True)
    assert with_damage.urgency["multiplier"] > without.urgency["multiplier"]
    assert LEVEL_RANK[with_damage.level] >= LEVEL_RANK[without.level]


def test_the_working_is_carried_so_a_planner_can_check_it(config):
    """A level that moved for a reason nobody can see is a level they will
    argue with, and they would be right to."""
    verdict = classify(
        [_risk(lead_hours=200.0, value_of_acting=50_000, expected_loss=80_000)],
        config, irreversible_damage=True,
    )
    for key in ("multiplier", "magnitude", "likelihood", "irreversible",
                "weights", "raw_hours", "effective_hours", "re_levelled"):
        assert key in verdict.urgency, key
