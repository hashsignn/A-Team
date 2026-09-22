"""The board fields the front end depends on, defended as a contract.

Everything in here is a field some piece of UI reads. None of it is checked by
the engine tests, because the engine does not care — and that is exactly the
problem: delete ``node_ids`` and every test still passes while clicking a port
on the globe silently stops working. A field with a consumer and no test is a
field that will be removed by somebody doing a tidy-up, six months from now,
with no way to know.

So this file is deliberately shallow and wide: it asserts SHAPE, not values.
"""

from __future__ import annotations

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")


@pytest.fixture(scope="module")
def context():
    return run(clock=AS_OF, config=load_config(),
               options=RunOptions(shipment_count=125, seed=7))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


# =====================================================================
# The globe
# =====================================================================
def test_every_route_carries_the_nodes_it_passes_through(board):
    """Read by the globe to answer "what is happening at this port".

    Without it, clicking a port finds no lane and silently does nothing —
    which looks identical to a port that has no problems.
    """
    for route in board["routes"]:
        assert "node_ids" in route, f"{route['route_id']} has no node_ids"
        assert route["node_ids"], f"{route['route_id']} has an empty node list"
        assert all(isinstance(n, str) for n in route["node_ids"])


def test_route_nodes_are_real_nodes(board, context):
    known = set(context.config.nodes)
    for route in board["routes"]:
        unknown = [n for n in route["node_ids"] if n not in known]
        assert not unknown, f"{route['route_id']} names nodes that do not exist: {unknown}"


def test_route_nodes_are_in_travel_order(board, context):
    """Origin first, destination last. The order IS the route, and a shuffled
    list would still pass a membership test while breaking anything that
    reasons about what comes before what."""
    lanes = {lane["id"]: lane for lane in context.config.lanes}
    for route in board["routes"]:
        legs = lanes[route["route_id"]]["legs"]
        assert route["node_ids"][0] == legs[0]["from"]
        assert route["node_ids"][-1] == legs[-1]["to"]
        assert len(route["node_ids"]) == len(legs) + 1


def test_routes_carry_a_severity_to_rank_ports_by(board):
    """Clicking a port opens the WORST lane through it, so there has to be a
    comparable number on every route or the choice is arbitrary."""
    for route in board["routes"]:
        assert isinstance(route.get("severity_score"), (int, float))


def test_nodes_carry_coordinates(board):
    for node in board["nodes"]:
        assert node["lat"] is not None and node["lon"] is not None, node["id"]
        assert "id" in node and "name" in node


# =====================================================================
# The event modal
# =====================================================================
def _events(board):
    return [e for r in board["routes"] for e in (r.get("events") or [])]


def test_there_are_events_to_open(board):
    assert _events(board), "no events on the board — the modal has nothing to show"


def test_every_event_has_a_stable_id(board):
    ids = [e["event_id"] for e in _events(board)]
    assert all(ids), "an event with no id cannot be deep-linked"


def test_an_event_appears_once_per_route_but_may_span_routes(board):
    """A Suez closure is on every lane through Suez, and that is correct — it
    is one event affecting many routes, not many events.

    The invariant is uniqueness WITHIN a route. It also means an event id
    alone does not identify a context, which is why the deep link carries the
    route as well: the modal shows that route's radar, and reopening a link
    against a different one would show a chart for a lane the reader never
    chose.
    """
    for route in board["routes"]:
        ids = [e["event_id"] for e in (route.get("events") or [])]
        assert len(ids) == len(set(ids)), (
            f"{route['route_id']} lists the same event twice: {ids}"
        )

    spanning = {}
    for route in board["routes"]:
        for event in (route.get("events") or []):
            spanning.setdefault(event["event_id"], set()).add(route["route_id"])
    assert any(len(routes) > 1 for routes in spanning.values()), (
        "no event spans two routes — the multi-route case is untested, and it "
        "is the one that makes the route part of the deep link necessary"
    )


def test_every_event_carries_its_provenance(board):
    """The modal states which source, at which tier, and whether a model read
    it. A planner should never have to guess which parts of a screen were
    computed and which were written."""
    for event in _events(board):
        assert event.get("source"), f"{event['event_id']} has no source"
        assert event.get("source_tier") in (1, 2, 3), event.get("source_tier")
        assert isinstance(event.get("inferred"), bool), (
            f"{event['event_id']}: 'inferred' must be present and boolean — it is "
            "what the modal uses to say a model read this one"
        )


def test_matrix_points_carry_what_the_cells_are_shaded_by(board):
    """Cells are tinted by CHF at stake. Drop expected_loss_chf and the whole
    grid renders flat, which reads as 'nothing is at risk here'."""
    seen = 0
    for event in _events(board):
        matrix = event.get("matrix")
        if not matrix:
            continue
        for point in matrix["points"]:
            assert "expected_loss_chf" in point
            assert "impact_band" in point and "probability_band" in point
            assert point["ring"] in ("solid", "hollow")
            seen += 1
    assert seen, "no matrix points anywhere — the shading is untested"


def test_the_matrix_grid_defines_its_own_axes(board):
    grid = board["matrix_grid"]
    assert grid["impact_bands"] and grid["probability_bands"]
    for band in grid["impact_bands"]:
        assert band.get("id") and band.get("label")
    assert grid.get("unsourced_band"), (
        "the off-axis band must exist: an event whose probability cannot be "
        "sourced has no x, and placing it at 0.5 would look like evidence"
    )


def test_radar_axes_exist_for_routes_that_have_events(board):
    """The modal draws the radar large. A route with events and no radar would
    open a dialog with an empty right-hand pane."""
    for route in board["routes"]:
        if route.get("events"):
            assert "radar" in route, f"{route['route_id']} has events but no radar"
            assert "axes" in route["radar"]


# =====================================================================
# The solution board
# =====================================================================
@pytest.fixture(scope="module")
def solution(context):
    from engine.fast import view as fast_view
    rows = fast_view.route_summaries(context)
    assert rows, "the fixture must produce at least one affected lane"
    return fast_view.solution_board(context, rows[0]["route_id"])


def test_the_board_carries_the_fields_the_tabs_render(solution):
    for key in ("route_id", "name", "displaced_shipments", "displaced_tonnes",
                "blocked_modes", "derated_modes", "horizon_hours", "plans",
                "vendors", "sentence"):
        assert key in solution, f"the board lost {key}"


def test_every_plan_carries_what_its_tab_needs(solution):
    for plan in solution["plans"]:
        for key in ("plan_id", "label", "thesis", "sentence", "coverage",
                    "covered_tonnes", "deferred_tonnes", "displaced_tonnes",
                    "extra_cost_chf", "worst_days_late", "late_shipments",
                    "hours_to_first_move", "feasible", "viable", "limits",
                    "also", "allocations", "deferred", "margin_chf",
                    "unprofitable_shipments"):
            assert key in plan, f"{plan.get('plan_id')} lost {key}"


def test_every_allocation_carries_what_its_bar_needs(solution):
    """The fleet bar divides units by units_available. A missing or zero
    denominator renders a bar of NaN% wide, which is a blank row rather than
    a visible error."""
    for plan in solution["plans"]:
        for row in plan["allocations"]:
            for key in ("mode", "tonnes", "units", "units_available",
                        "unit_name", "unit_name_available", "shipments",
                        "hours_to_ready", "hours_to_last_away", "cost_chf"):
                assert key in row, f"{row.get('mode')} lost {key}"
            assert row["units_available"] > 0
            assert row["units"] > 0


def test_a_plan_id_is_unique_so_tabs_and_panels_pair_up(solution):
    """The tab sets aria-controls to plan-<id> and the panel uses it as its
    DOM id. Two plans sharing an id makes one tab unreachable."""
    ids = [p["plan_id"] for p in solution["plans"]]
    assert len(ids) == len(set(ids))
