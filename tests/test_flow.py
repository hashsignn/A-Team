"""The operational flow, the cargo page and the execute view.

Three things are defended here, and they came from the team rather than from
the brief:

  "one disruption on a route may only affect some of the vessels using that
   route. And the effects will not be the same for all vessels."
  "The calculations cannot be simplified."
  "Do not reroute until the disruption has been confirmed."

The first two are architectural constraints and have tests that fail if the
architecture drifts. The third is a gate, and a gate that is not tested is a
caption.
"""

from __future__ import annotations

import pytest

from engine.act import flow as F
from engine.clock import Clock
from engine.config import load_config
from engine.export import cargo as C
from engine.export.board import build_board
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")
LANE = "LANE_RHINE_01"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def context(config):
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


@pytest.fixture(scope="module")
def route(board):
    return next(r for r in board["routes"] if r["route_id"] == LANE)


@pytest.fixture(scope="module")
def tasks(config, route):
    return F.build(config, route)


CONFIRMING = {"confirm.carrier", "confirm.position", "confirm.eta"}


# =====================================================================
# THE GATE — "do not reroute until the disruption has been confirmed"
# =====================================================================


def test_routing_actions_are_locked_before_confirmation(tasks, route):
    state = F.evaluate(tasks, set())
    assert not state.gate_open
    actions = F.unlocked_actions(route["actions"], state)
    routing = [a for a in actions if a["action_type"] in F.ROUTING_ACTION_TYPES]
    assert routing, "fixture has no routing action to lock"
    assert all(a["locked"] for a in routing)
    assert all(a["locked_reason"] for a in routing)


def test_confirming_unlocks_them(tasks, route):
    state = F.evaluate(tasks, CONFIRMING)
    assert state.gate_open
    actions = F.unlocked_actions(route["actions"], state)
    assert not any(a["locked"] for a in actions)


def test_a_partial_confirmation_does_not_unlock(tasks, route):
    """Two of three is not confirmation. A gate that opens early is a gate
    that exists to be worked around."""
    for subset in ({"confirm.carrier"}, {"confirm.carrier", "confirm.position"}):
        state = F.evaluate(tasks, subset)
        assert not state.gate_open, subset


def test_notifying_the_customer_is_never_locked(tasks, route):
    """Always available, at every stage. Telling a customer early costs
    nothing and is the one action that never needs confirming."""
    state = F.evaluate(tasks, set())
    actions = F.unlocked_actions(route["actions"], state)
    safe = [a for a in actions if a["action_type"] not in F.ROUTING_ACTION_TYPES]
    assert all(not a["locked"] for a in safe)


def test_the_gate_matches_on_action_type_not_on_the_label(route):
    """Matching a reroute by its human label would break the moment somebody
    reworded it, and a gate that silently stops matching is worse than no
    gate — the lock still LOOKS applied."""
    for action in route["actions"]:
        assert "action_type" in action, "the board must emit the machine kind"


def test_an_uncorroborated_tier3_event_cannot_be_confirmed(config, route):
    """Nothing about ticking a box makes a rumour true."""
    tasks = F.build(config, route, source_tiers=[3])
    state = F.evaluate(tasks, CONFIRMING)
    assert not state.gate_open
    assert "uncorroborated" in state.gate_reason
    assert "would not make the report true" in state.gate_reason


def test_two_tier3_sources_or_an_authority_notice_allow_confirmation(config, route):
    for tiers in ([3, 3], [1], [2], [1, 3]):
        tasks = F.build(config, route, source_tiers=tiers)
        assert F.evaluate(tasks, CONFIRMING).gate_open, tiers


def test_the_blocked_task_says_what_would_resolve_it(config, route):
    tasks = F.build(config, route, source_tiers=[3])
    blocked = [t for t in tasks if t.blocked_reason]
    assert blocked
    assert "carrier callback" in blocked[0].blocked_reason


# =====================================================================
# The flow itself
# =====================================================================


DETECTING = {"detect.read", "detect.scope"}


def test_the_stage_advances_only_when_its_required_tasks_are_done(tasks):
    assert F.evaluate(tasks, set()).stage is F.Stage.DETECT
    assert F.evaluate(tasks, DETECTING).stage is F.Stage.CONFIRM
    assert F.evaluate(tasks, DETECTING | CONFIRMING).stage is F.Stage.ACT
    assert F.evaluate(
        tasks, DETECTING | CONFIRMING | {"act.choose"}
    ).stage is F.Stage.CLOSE


def test_a_stage_with_no_required_tasks_is_not_skipped(config, route):
    """Detect is informational, and a planner should still sit there until
    they have read the event — the screenshots' step 1. A stage that vanishes
    because nothing in it was marked required is a stage nobody meant to put
    on the checklist."""
    tasks = F.build(config, route)
    detect = [t for t in tasks if t.stage is F.Stage.DETECT]
    assert detect
    assert F.evaluate(tasks, set()).stage is F.Stage.DETECT


def test_every_task_carries_an_owner(tasks):
    """A checklist without an owner per line is a wish list."""
    for task in tasks:
        assert task.owner and task.owner.strip()
        assert task.note.strip()


def test_slas_come_from_the_contact_list_not_from_a_template(config, tasks):
    """A playbook quoting invented response times is one nobody is held to."""
    configured = {
        t.get("response_sla_hours")
        for t in config.contacts.get("internal", [])
    }
    from_contacts = [
        t for t in tasks
        if t.sla_hours is not None and t.sla_hours in configured
    ]
    assert from_contacts, "no task inherited a configured SLA"


def test_the_confirm_window_tightens_with_the_rung(config, board):
    """At Critical there is no six-hour callback window."""
    def window(level_name):
        route = next(
            (r for r in board["routes"] if r["level"] == level_name), None
        )
        if route is None:
            return None
        task = next(
            t for t in F.build(config, route) if t.id == "confirm.carrier"
        )
        return task.sla_hours

    yellow, blue = window("yellow"), window("blue")
    if yellow is not None and blue is not None:
        assert yellow < blue


def test_a_tick_for_an_unknown_task_is_ignored(tasks):
    """State arrives from the browser. Trusting an id we never issued would
    let a stale bookmark open the gate."""
    state = F.evaluate(tasks, {"confirm.carrier", "nonsense.injected"})
    assert "nonsense.injected" not in state.completed


def test_evaluate_is_pure(tasks):
    """Two planners with the same ticks must see the same stage."""
    first = F.evaluate(tasks, CONFIRMING)
    second = F.evaluate(tasks, CONFIRMING)
    assert first.stage is second.stage
    assert first.gate_open == second.gate_open


# =====================================================================
# THE CARGO PAGE — "the effects will not be the same for all vessels"
# =====================================================================


def test_one_lane_carries_many_different_deadlines(board, context):
    """The team's point, and the reason this page exists. It has always been
    true in the engine — the gate is per (event, shipment, leg) — and was
    never visible, because the board showed one colour for the lane."""
    view = C.lane_view(board, context, LANE)
    assert view["summary"]["total"] > 5
    assert view["summary"]["distinct_deadlines"] > 3, (
        "if every consignment on a lane had the same answer, a lane-level "
        "colour would be sufficient and this page would be decoration"
    )


def test_untouched_consignments_stay_visible_as_untouched(board, context):
    """"This event does not reach six of your seventeen" is an answer a
    planner wants. Dropping them off the page hides it."""
    view = C.lane_view(board, context, LANE)
    untouched = [c for c in view["consignments"] if not c["touched"]]
    assert untouched
    for consignment in untouched:
        # None, not zero. Zero would read as "assessed and found harmless",
        # which is a different claim.
        assert consignment["lead_time_hours"] is None
        assert consignment["expected_loss_chf"] is None


def test_consignments_are_ordered_by_urgency(board, context):
    view = C.lane_view(board, context, LANE)
    leads = [
        c["lead_time_hours"] for c in view["consignments"] if c["touched"]
    ]
    assert leads == sorted(leads)
    touched_flags = [c["touched"] for c in view["consignments"]]
    assert touched_flags == sorted(touched_flags, reverse=True)


def test_an_unknown_lane_is_reported_not_guessed(board, context):
    assert "error" in C.lane_view(board, context, "LANE_NOWHERE")


# =====================================================================
# THE EXECUTE VIEW — "the calculations cannot be simplified"
# =====================================================================


def test_the_execute_view_recomputes_nothing(board, context):
    """THE architectural constraint, from the team, and the one that decides
    whether this is a filter or a second app.

    A driver's screen that works out its own ETA will disagree with the
    planner's, and a planner contradicted by the tool once stops using it.
    Every figure here has to be one the board already published.
    """
    view = C.lane_view(board, context, LANE)
    first = view["consignments"][0]
    execute = C.execute_view(board, context, first["shipment_id"])

    assert execute["your_deadline"]["hours"] == first["lead_time_hours"]
    assert execute["your_deadline"]["state"] == first["actionability"]

    route = next(r for r in board["routes"] if r["route_id"] == LANE)
    assert execute["status"]["level"] == route["level"]
    assert execute["status"]["what_it_means"] == route["directive"]


def test_the_execute_view_omits_the_decision_making(board, context):
    """The transport manager is not being asked to decide, so the ladder
    arithmetic, the matrix and the convene rule are absent — because they are
    not theirs to make, not because they were too complicated to show."""
    view = C.lane_view(board, context, LANE)
    execute = C.execute_view(board, context, view["consignments"][0]["shipment_id"])
    import json

    blob = json.dumps(execute)
    for absent in ("convene", "severity_score", "matrix", "urgency",
                   "exposure_chf", "recoverable"):
        assert absent not in blob, f"{absent} leaked into the execute view"


def test_it_names_which_legs_are_affected_not_just_the_shipment(board, context):
    """A Rhine low-water event hits the barge legs and leaves the road leg
    alone. Someone executing needs to know which hop is the problem."""
    view = C.lane_view(board, context, LANE)
    touched = next(c for c in view["consignments"] if c["touched"])
    execute = C.execute_view(board, context, touched["shipment_id"])
    assert execute["legs"]
    assert execute["affected_legs"], "a touched shipment must name a hit leg"
    assert len(execute["affected_legs"]) < len(execute["legs"]), (
        "not every leg should be affected, or the per-leg gate is not working"
    )


def test_the_deadline_is_labelled_as_a_decision_not_a_delivery(board, context):
    view = C.lane_view(board, context, LANE)
    execute = C.execute_view(board, context, view["consignments"][0]["shipment_id"])
    assert "decision has to be made" in execute["your_deadline"]["note"]


def test_the_report_back_is_an_honest_socket(board, context):
    """The app composes; it does not submit. No store is wired, and putting a
    confirmation on screen for something that went nowhere is worse than the
    gap."""
    view = C.lane_view(board, context, LANE)
    execute = C.execute_view(board, context, view["consignments"][0]["shipment_id"])
    report = execute["report_back"]
    assert report["fields"]
    assert "Not connected" in report["socket"]
    assert "TIER-1" in report["socket"]


def test_an_unknown_shipment_is_reported_not_guessed(board, context):
    assert "error" in C.execute_view(board, context, "SYN-NOPE")
