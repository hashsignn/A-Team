"""The response path: a decision flow that branches, not a fixed checklist.

What is pinned here is the behaviour the planner relies on: the first block
is always "confirm on site" and carries who to call; completing a block turns
it green and opens the next one on the same branch; choosing another branch
is always allowed and never erases what was already done; and every block
carries a contact, a number and a response time.
"""

from __future__ import annotations

import pytest

from engine.act import paths
from engine.act.paths import ACTIVE, DONE, IDLE, NEXT, ROOT
from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.pipeline import RunOptions, run

AS_OF = "2026-09-16"
LANE = "LANE_ASIA_08"


@pytest.fixture(scope="module")
def context():
    return run(clock=Clock.at(AS_OF), config=load_config(),
               options=RunOptions(shipment_count=220))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


@pytest.fixture(scope="module")
def graph(board, context):
    route = next(r for r in board["routes"] if r["route_id"] == LANE)
    return paths.build(board, context, route)


def _path(flow, path_id):
    return next(p for p in flow["paths"] if p["path_id"] == path_id)


def test_the_first_suggested_action_is_to_confirm_on_site(graph):
    flow = paths.evaluate(graph)
    assert flow["root"]["step_id"] == ROOT
    assert flow["root"]["state"] == ACTIVE
    assert flow["active_step"] == ROOT
    # Nothing on a branch is active before the situation is confirmed.
    assert all(s["state"] != ACTIVE for p in flow["paths"] for s in p["steps"])


def test_the_root_block_carries_what_is_needed_to_make_the_call(graph):
    root = graph["root"]
    assert root["contact"]["name"]
    assert root["contact"]["phone"]
    assert root["response_hours"] == paths.RESPONSE_HOURS[graph["level"]]
    labels = {r["label"] for r in root["info"]}
    assert {"Event", "Consignment", "Position", "Due"} <= labels


def test_every_block_names_a_contact_and_a_response_time(graph):
    for p in graph["paths"]:
        for s in p["steps"]:
            assert s["contact"]["name"], s["step_id"]
            if not s["step_id"].endswith(".close"):
                assert s["response_hours"], s["step_id"]


def test_the_four_branches_are_offered_and_hold_is_always_open(graph):
    ids = [p["path_id"] for p in graph["paths"]]
    assert ids == ["reroute", "alt_port", "split", "hold"]
    assert _path(graph, "hold")["available"]
    for p in graph["paths"]:
        if not p["available"]:
            assert p["unavailable_reason"], p["path_id"]


def test_the_reroute_branch_walks_the_steps_the_planner_expects(graph):
    labels = [s["label"] for s in _path(graph, "reroute")["steps"]]
    assert labels[0].startswith("Check inventory")
    assert any("sea, rail and road" in x for x in labels)
    assert any("Logistics and Procurement" in x for x in labels)
    assert any(x == "Obtain approval" for x in labels)
    assert labels[-1].startswith("Execute")


def test_the_recommendation_is_an_available_branch_with_a_reason(graph):
    assert _path(graph, graph["recommended"])["available"]
    assert graph["recommended_why"]


def test_completing_the_root_turns_it_green_and_opens_the_recommended_branch(graph):
    flow = paths.evaluate(graph, {ROOT})
    assert flow["root"]["state"] == DONE
    current = _path(flow, graph["recommended"])
    assert current["in_force"]
    assert current["steps"][0]["state"] == ACTIVE
    assert all(s["state"] == NEXT for s in current["steps"][1:])
    others = [p for p in flow["paths"] if p["path_id"] != graph["recommended"]]
    assert all(s["state"] == IDLE for p in others for s in p["steps"])


def test_completing_a_block_activates_the_next_one_on_the_same_path(graph):
    first, second = (s["step_id"] for s in _path(graph, "reroute")["steps"][:2])
    flow = paths.evaluate(graph, {ROOT, first}, chosen="reroute")
    steps = _path(flow, "reroute")["steps"]
    assert steps[0]["state"] == DONE
    assert steps[1]["state"] == ACTIVE
    assert flow["active_step"] == second


def test_the_planner_can_choose_another_branch_and_keeps_what_was_done(graph):
    first = _path(graph, "reroute")["steps"][0]["step_id"]
    flow = paths.evaluate(graph, {ROOT, first}, chosen="hold")
    assert flow["in_force"] == "hold"
    assert _path(flow, "hold")["steps"][0]["state"] == ACTIVE
    # The reroute work done before the switch stays green, not erased.
    assert _path(flow, "reroute")["steps"][0]["state"] == DONE
    assert _path(flow, "reroute")["in_force"] is False


def test_choosing_before_confirming_is_allowed_but_warned(graph):
    flow = paths.evaluate(graph, set(), chosen="hold")
    assert flow["root"]["state"] == ACTIVE
    assert _path(flow, "hold")["steps"][0]["state"] == ACTIVE
    assert flow["caution"]


def test_a_finished_path_is_reported_complete(graph):
    done = {ROOT} | {s["step_id"] for s in _path(graph, "hold")["steps"]}
    flow = paths.evaluate(graph, done, chosen="hold")
    assert flow["complete"]
    assert flow["active_step"] is None


def test_evaluate_is_pure_and_passes_the_trail_through(graph):
    trail = [{"kind": "complete", "step_id": ROOT, "actor": "maria", "at": "x"}]
    a = paths.evaluate(graph, {ROOT}, "hold", trail)
    b = paths.evaluate(graph, {ROOT}, "hold", trail)
    assert a == b
    assert a["trail"] == trail
    assert graph["root"].get("state") is None   # the graph itself is untouched


def test_an_unknown_choice_falls_back_to_the_recommendation(graph):
    flow = paths.evaluate(graph, {ROOT}, chosen="teleport")
    assert flow["chosen"] is None
    assert flow["in_force"] == graph["recommended"]


def test_every_lane_builds_a_flow(board, context):
    for route in board["routes"]:
        g = paths.build(board, context, route)
        assert g["recommended"] in {p["path_id"] for p in g["paths"]}
        assert _path(g, g["recommended"])["available"], route["route_id"]
