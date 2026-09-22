"""Distance, position, and the gap between plan and reality.

The number this module exists for is the GAP. A consignment 280 km from
where the plan puts it is the most actionable fact on the page, and it exists
only because the planned and observed positions are kept apart rather than
merged into one dot.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.export.progress import drift_km, interpolate, leg_fraction, observed, progress
from engine.export.route import route_view
from engine.network.geo import Point, haversine_km
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")
RAIL_LANE = "LANE_RHINE_02"


@pytest.fixture(scope="module")
def context():
    return run(clock=AS_OF, config=load_config(),
               options=RunOptions(shipment_count=150, seed=7))


@pytest.fixture(scope="module")
def view(context):
    return route_view(build_board(context), context, RAIL_LANE)


# =====================================================================
# Geometry
# =====================================================================
def test_interpolation_follows_the_great_circle():
    """A linear average of the coordinates puts a Rotterdam-Shanghai midpoint
    in Kazakhstan. The real one is over Siberia, and a map drawn from the
    wrong one is wrong by a thousand kilometres."""
    rtm, sha = Point(51.95, 4.13), Point(31.23, 121.47)
    mid = interpolate(rtm, sha, 0.5)
    assert mid.lat > 55.0, "midpoint is south of the great circle — linear average?"
    # and it really is the midpoint: equidistant from both ends
    assert abs(haversine_km(rtm, mid) - haversine_km(mid, sha)) < 1.0


def test_interpolation_is_clamped():
    a, b = Point(0, 0), Point(0, 10)
    assert interpolate(a, b, -5).lon == pytest.approx(a.lon, abs=1e-6)
    assert interpolate(a, b, 99).lon == pytest.approx(b.lon, abs=1e-6)


def test_interpolating_between_identical_points_is_stable():
    """A leg whose ends share coordinates must not divide by zero."""
    a = Point(47.5, 7.6)
    assert interpolate(a, a, 0.5).lat == pytest.approx(47.5)


def test_leg_fraction_spans_zero_to_one(context):
    leg = context.shipments[0].legs[0]
    before = leg.planned_depart - timedelta(hours=1)
    after = leg.planned_arrive + timedelta(hours=1)
    middle = leg.planned_depart + (leg.planned_arrive - leg.planned_depart) / 2
    assert leg_fraction(leg, before) == 0.0
    assert leg_fraction(leg, after) == 1.0
    assert leg_fraction(leg, middle) == pytest.approx(0.5, abs=0.01)


# =====================================================================
# Progress
# =====================================================================
def test_distances_add_up(view):
    for leg in view["legs"]:
        for veh in leg["vehicles"]:
            p = veh["progress"]
            assert p["travelled_km"] + p["remaining_km"] == pytest.approx(
                p["total_km"], abs=0.2
            ), "travelled + remaining must be the whole journey"


def test_nothing_travels_a_negative_distance(view):
    for leg in view["legs"]:
        for veh in leg["vehicles"]:
            p = veh["progress"]
            assert p["travelled_km"] >= 0 and p["remaining_km"] >= 0
            assert 0 <= p["percent"] <= 100


def test_the_leg_distances_are_real(view):
    """A 320 km rail leg from Duisburg to Hamburg is about right. A zero would
    mean a node lost its coordinates and nobody noticed."""
    for leg in view["legs"]:
        assert leg["km"] > 0, f"leg {leg['index']} has no length"


def test_progress_is_measured_against_the_board_not_the_wall_clock(context):
    """Otherwise a hindcast would report today's positions for a past day,
    and every replayed board would drift as the afternoon went on."""
    shipment = context.shipments[0]
    nodes = context.config.nodes
    early = progress(shipment, nodes, AS_OF.as_of)
    later = progress(shipment, nodes, AS_OF.as_of + timedelta(days=3))
    assert later["travelled_km"] >= early["travelled_km"]


def test_a_finished_journey_has_nothing_left(context):
    shipment = context.shipments[0]
    done = progress(shipment, context.config.nodes,
                    shipment.legs[-1].planned_arrive + timedelta(days=30))
    assert done["remaining_km"] == 0.0
    assert done["percent"] == pytest.approx(100.0, abs=0.1)


def test_there_is_always_a_planned_position(view):
    """Before departure, mid-leg, between legs, after arrival. A None here
    would leave a vehicle off the map for no stated reason."""
    for leg in view["legs"]:
        for veh in leg["vehicles"]:
            assert veh["progress"]["planned_position"] is not None


def test_the_planned_position_is_on_earth(view):
    for leg in view["legs"]:
        for veh in leg["vehicles"]:
            pos = veh["progress"]["planned_position"]
            assert -90 <= pos["lat"] <= 90 and -180 <= pos["lon"] <= 180
            assert not math.isnan(pos["lat"]) and not math.isnan(pos["lon"])


# =====================================================================
# Observed, and the gap
# =====================================================================
def test_a_text_position_is_not_a_fix():
    """"Kaub, third in the queue" is worth more as those words than as a dot
    two kilometres from the vehicle. Geocoding it would give the dot the same
    visual authority as a GPS fix."""
    assert observed([{"position": "Kaub, third in the queue",
                      "observed_at": "2026-09-18T05:00:00+00:00"}]) is None


def test_the_latest_fix_wins():
    reports = [
        {"lat": 47.0, "lon": 7.0, "observed_at": "2026-09-18T04:00:00+00:00"},
        {"lat": 50.0, "lon": 7.7, "observed_at": "2026-09-18T05:30:00+00:00"},
    ]
    assert observed(reports)["lat"] == 50.0


def test_drift_is_unknown_not_zero_when_nothing_was_seen():
    """Zero would read as "exactly on plan", which is the opposite of "we have
    no idea where this is"."""
    assert drift_km({"lat": 1, "lon": 1}, None) is None
    assert drift_km(None, {"lat": 1, "lon": 1}) is None


def test_drift_measures_the_gap():
    basel = {"lat": 47.564, "lon": 7.596}
    kaub = {"lat": 50.086, "lon": 7.766}
    gap = drift_km(basel, kaub)
    assert 270 < gap < 290, f"Basel to Kaub is about 280 km, got {gap}"


def test_every_vehicle_carries_both_claims_separately(view):
    """One dot would have to pick, and whichever it picked it is wrong half
    the time."""
    for leg in view["legs"]:
        for veh in leg["vehicles"]:
            assert "planned_position" in veh["progress"]
            assert "observed_position" in veh
            assert "drift_km" in veh
