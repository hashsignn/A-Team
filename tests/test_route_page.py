"""The route page: one lane, and the vehicles carrying it.

The board says which lanes are in trouble. This answers the question that
follows, and that question is about VEHICLES — nine trucks, of which two are
behind a closed motorway, is something a planner can act on. "A lane at
Alert" is not.
"""

from __future__ import annotations

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.export.route import route_view
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")
LANE = "LANE_RHINE_01"


@pytest.fixture(scope="module")
def context():
    return run(clock=AS_OF, config=load_config(),
               options=RunOptions(shipment_count=150, seed=7))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


@pytest.fixture(scope="module")
def view(board, context):
    return route_view(board, context, LANE)


def test_an_unknown_route_is_none_not_an_empty_page(board, context):
    """A page that renders emptily for a bad id looks like a lane with no
    freight on it, which is a different and false statement."""
    assert route_view(board, context, "LANE_DOES_NOT_EXIST") is None


def test_the_route_is_drawn_as_its_legs(view):
    assert view["legs"], "a route with no legs cannot be drawn"
    for leg in view["legs"]:
        assert leg["from_name"] and leg["to_name"]
        assert leg["mode"] in ("road", "rail", "barge", "sea", "air")


def test_every_leg_carries_vehicles(view):
    """One icon per consignment on that leg. An empty leg on a lane the board
    says is in trouble means the join is broken."""
    for leg in view["legs"]:
        assert leg["vehicles"], f"leg {leg['index']} has no vehicles"


def test_a_vehicle_carries_what_the_panel_shows(view):
    veh = view["legs"][0]["vehicles"][0]
    for field in ("shipment_id", "customer", "carrier", "value_chf",
                  "status", "leg_arrives", "reports"):
        assert field in veh, f"{field} missing — the detail panel reads it"


def test_the_three_colours_are_the_only_three(view):
    seen = {v["status"] for leg in view["legs"] for v in leg["vehicles"]}
    assert seen <= {"ok", "at_risk", "affected"}, seen


def test_amber_exists_and_is_not_just_red(view):
    """The distinction that matters. A page that paints everything touched in
    red tells a planner to panic about nine vehicles when two need a decision
    today and the rest are fine for a week."""
    statuses = [v["status"] for leg in view["legs"] for v in leg["vehicles"]]
    assert "at_risk" in statuses, "nothing is amber — the middle state is unused"
    assert "ok" in statuses, "nothing is green — everything cannot be affected"


def test_the_counts_match_the_vehicles(view):
    """The header count is read at a glance and trusted. If it can drift from
    the icons below it, it is worse than not being there."""
    for leg in view["legs"]:
        counted = {
            "affected": sum(1 for v in leg["vehicles"] if v["status"] == "affected"),
            "at_risk": sum(1 for v in leg["vehicles"] if v["status"] == "at_risk"),
            "ok": sum(1 for v in leg["vehicles"] if v["status"] == "ok"),
        }
        assert leg["counts"] == counted, f"leg {leg['index']} header disagrees with its icons"


def test_the_totals_match_the_legs(view):
    for key in ("affected", "at_risk", "ok"):
        assert view["totals"][key] == sum(leg["counts"][key] for leg in view["legs"])


def test_the_page_is_a_projection_of_the_board(view, board):
    """Every figure here already appears on the planner's board. Two screens
    that disagree about the same number destroy trust in both, and the one a
    planner believes is whichever they saw last."""
    route = next(r for r in board["routes"] if r["route_id"] == LANE)
    assert view["level"] == route["level"]
    assert view["directive"] == route["directive"]
    assert view["exposure_chf"] == route["exposure_chf"]
    assert view["lead_time_hours"] == route["lead_time_hours"]


def test_it_carries_the_charts_the_page_draws(view):
    assert view["radar"] and "axes" in view["radar"]
    assert view["matrix_grid"], "the matrix cannot be drawn without its axes"
    assert view["events"], "a route in trouble with no events is a broken join"


def test_the_driving_event_is_named_and_real(view):
    """The page opens on one matrix, and it has to be the one that matters."""
    assert view["driving_event_id"]
    assert any(e["event_id"] == view["driving_event_id"] for e in view["events"])


def test_the_mode_changes_down_the_route(view):
    """The Rhine lane is road to Basel then barge. If every leg reported the
    same mode the icons would all be trucks, including the barges."""
    modes = {leg["mode"] for leg in view["legs"]}
    assert len(modes) > 1, f"every leg is {modes} — per-leg mode is not being read"


def test_reports_are_attached_per_vehicle(view):
    """Field reports are the reason this page is worth opening: every other
    number is inferred from a feed describing a region."""
    for leg in view["legs"]:
        for veh in leg["vehicles"]:
            assert isinstance(veh["reports"], list)
