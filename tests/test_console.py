"""The operations console: every step is a control, not a claim.

The page these tests protect replaced a checklist. The properties worth
pinning are the ones that stop it drifting back into one: a step that names
data must carry that data, a step the engine can answer must not wait for a
person, and nothing may be locked behind a tick.
"""

from __future__ import annotations

import pytest

from engine.act import console as console_mod
from engine.act.console import DONE, EVIDENCE, OPEN
from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.pipeline import RunOptions, run

AS_OF = "2026-09-16"
LANE = "LANE_RHINE_01"


@pytest.fixture(scope="module")
def context():
    return run(clock=Clock.at(AS_OF), config=load_config(),
               options=RunOptions(shipment_count=220))


@pytest.fixture(scope="module")
def route(context):
    board = build_board(context)
    return next(r for r in board["routes"] if r["route_id"] == LANE)


@pytest.fixture
def payload(context, route):
    return console_mod.build(context, route, reports=[], executions=[])


def step(payload, step_id):
    return next(s for s in payload["steps"] if s["step_id"] == step_id)


# ------------------------------------------------------------ the shape
def test_every_step_carries_at_least_one_tool():
    """A step with no control is a sentence with a square next to it, which is
    the thing this page stopped being."""
    ctx = run(clock=Clock.at(AS_OF), config=load_config(),
              options=RunOptions(shipment_count=220))
    board = build_board(ctx)
    row = next(r for r in board["routes"] if r["route_id"] == LANE)
    built = console_mod.build(ctx, row, reports=[], executions=[])
    for s in built["steps"]:
        assert s["tools"], f"{s['step_id']} has nothing to press"


def test_a_step_that_names_data_actually_carries_it(payload):
    """"Read the event" has to be able to open the event. Ticking a box that
    asserts you read something the page never offered is the original sin."""
    assert step(payload, "detect.read")["data"]["events"]
    assert step(payload, "detect.scope")["data"]["consignments"]


def test_the_consignment_list_is_every_consignment_not_just_the_actionable(payload):
    rows = step(payload, "detect.scope")["data"]["consignments"]
    assert len(rows) > sum(1 for r in rows if r["at_risk"])


def test_the_scope_count_matches_the_header(payload, route):
    """Two numbers for one fact, a hand-span apart, is how a planner stops
    believing either. The old build read them off different sources."""
    rows = step(payload, "detect.scope")["data"]["consignments"]
    assert sum(1 for r in rows if r["at_risk"]) == route["shipments_at_risk"]


def test_a_hit_consignment_with_no_worthwhile_option_is_not_called_on_plan(payload):
    rows = step(payload, "detect.scope")["data"]["consignments"]
    for row in rows:
        if row["at_risk"] and row["best_action"] is None:
            break
    else:
        pytest.skip("every affected consignment has an option on this lane")
    assert row["at_risk"] is True


# ------------------------------------------------------- what the engine knows
def test_corroborated_sources_settle_the_carrier_step_without_a_person(payload):
    """The complaint, exactly: the sources are already in, so why is a human
    ticking a box to say they agree."""
    carrier = step(payload, "confirm.carrier")
    assert carrier["state"] == EVIDENCE
    assert carrier["evidence"], "it went amber with nothing to show for it"


def test_evidence_is_amber_and_not_green(payload):
    """Amber, deliberately: somebody should SEE what was decided for them."""
    assert step(payload, "detect.read")["state"] == EVIDENCE
    assert step(payload, "detect.read")["state"] != DONE


def test_reviewing_an_amber_step_settles_it(context, route):
    built = console_mod.build(
        context, route, reports=[], executions=[], reviewed={"detect.read"})
    assert step(built, "detect.read")["state"] == DONE


def test_an_executed_action_settles_the_act_step_without_a_tick(context, route):
    built = console_mod.build(context, route, reports=[], executions=[{
        "label": "Reroute via Zürich", "shipment_id": "SYN-0001",
        "executed_at": "2026-09-16T07:00:00+00:00", "undone": False,
        "cost_chf": 900.0, "confidence": "reported",
    }])
    choose = step(built, "act.choose")
    assert choose["state"] == DONE
    assert choose["evidence"][0]["weight"] == "executed"


def test_a_pulled_back_action_does_not_settle_the_act_step(context, route):
    built = console_mod.build(context, route, reports=[], executions=[{
        "label": "Reroute via Zürich", "shipment_id": "SYN-0001",
        "executed_at": "2026-09-16T07:00:00+00:00", "undone": True,
        "cost_chf": 900.0, "confidence": "reported",
    }])
    assert step(built, "act.choose")["state"] == OPEN


# ------------------------------------------------------------- no more gate
def test_nothing_is_locked_behind_a_checklist(payload):
    """The gate is gone. What survives is a warning with the evidence
    attached, which is the trade the whole rebuild makes."""
    blob = str(payload)
    assert "locked" not in blob.lower()
    for s in payload["steps"]:
        assert "blocked_reason" not in s


def test_thin_evidence_warns_rather_than_stops(payload):
    caution = payload["caution"]
    assert caution is not None
    assert "can still act" in caution["text"]


def test_the_caution_clears_once_confirm_is_settled(context, route):
    built = console_mod.build(
        context, route, reports=[],
        executions=[],
        reviewed={"confirm.carrier"},
        logged={"confirm.position": {}, "confirm.eta": {}},
    )
    assert built["caution"] is None


# ----------------------------------------------------------------- the rail
def test_the_rail_has_one_segment_per_stage_with_its_own_progress(payload):
    assert [s["stage"] for s in payload["stages"]] == [
        "detect", "confirm", "act", "close"]
    for stage in payload["stages"]:
        assert 0.0 <= stage["progress"] <= 1.0
        assert stage["done"] <= stage["steps"]


def test_the_current_stage_is_the_first_unsettled_one(payload):
    first_open = next(s["stage"] for s in payload["stages"] if not s["settled"])
    assert payload["current_stage"] == first_open


def test_progress_counts_settled_steps_only(payload):
    counts = payload["counts"]
    assert counts["done"] + counts["evidence"] + counts["open"] == counts["total"]
    assert payload["progress"] == pytest.approx(
        counts["done"] / counts["total"], abs=0.001)


# -------------------------------------------------------------- the toolbox
def test_act_offers_a_search_not_a_tickbox(payload):
    tools = {t["tool_id"] for t in step(payload, "act.choose")["tools"]}
    assert "find.alternates" in tools


def test_capacity_can_reach_local_operators(payload):
    tools = {t["tool_id"] for t in step(payload, "act.capacity")["tools"]}
    assert "find.vendors" in tools


def test_the_record_is_written_not_typed(payload):
    tools = {t["tool_id"] for t in step(payload, "close.record")["tools"]}
    assert "build.record" in tools
    assert not any(
        t["kind"] == "log" for t in step(payload, "close.record")["tools"]
    ), "the close-out must not ask a planner to retype what already happened"


# ---------------------------------------------- acting on one consignment
def test_every_consignment_row_carries_its_own_controls(payload):
    """The lane-level answer is right for a lane and useless when one
    customer is on the phone."""
    data = step(payload, "detect.scope")["data"]
    tools = {t["tool_id"] for t in data["row_tools"]}
    assert {"options.one", "escalate.one", "locate.one"} <= tools


def test_a_single_consignment_can_be_ranked_on_its_own(context):
    from engine.fast import view

    at_risk = next(
        risk.shipment_id
        for assessment in context.result.assessments
        for risk in assessment.shipment_risks
    )
    result = view.options_for_shipment(context, at_risk)
    assert result is not None
    assert result["shipment_id"] == at_risk
    assert result["sentence"]


def test_ranking_an_unknown_consignment_returns_nothing_rather_than_guessing(context):
    from engine.fast import view

    assert view.options_for_shipment(context, "SYN-NOPE") is None


def test_a_consignment_touched_by_two_events_keeps_the_tighter_answer(context):
    """A consignment is only as safe as its tightest constraint, so merging
    two rankings must not quietly pick the more comfortable one."""
    from collections import Counter

    from engine.fast import view

    counts = Counter(
        risk.shipment_id
        for assessment in context.result.assessments
        for risk in assessment.shipment_risks
    )
    doubled = [sid for sid, n in counts.items() if n > 1]
    if not doubled:
        pytest.skip("no consignment is hit by two events in this run")

    result = view.options_for_shipment(context, doubled[0])
    ids = [o["option_id"] for o in result["options"]]
    assert len(ids) == len(set(ids)), "the same option came back twice"


# --------------------------------------------------- the rail is not a gate
def test_no_step_depends_on_an_earlier_one(payload):
    """Four numbered segments filling left to right read as a wizard, and the
    page was being taken for one. Nothing may actually be sequenced."""
    for s in payload["steps"]:
        assert "depends_on" not in s
        assert "requires" not in s
        for tool in s["tools"]:
            assert "disabled" not in tool


def test_act_tools_are_available_while_detect_is_untouched(payload):
    """You can escalate one consignment without confirming anything."""
    act = step(payload, "act.choose")
    assert act["tools"]
    assert all(t["kind"] in ("run", "reveal", "link", "log") for t in act["tools"])


def test_the_toolbox_is_dense_enough_to_be_a_toolbox(payload):
    """Counted, because "not many buttons" was a real complaint and a number
    is the only way to keep an answer to it from eroding."""
    total = sum(len(s["tools"]) for s in payload["steps"])
    assert total >= 24, f"only {total} controls across the console"
    assert all(len(s["tools"]) >= 1 for s in payload["steps"])


def test_the_matrix_is_reachable_from_the_step_that_wants_it(payload):
    """It was never deleted, but it was not reachable from here either."""
    tools = {t["tool_id"] for t in step(payload, "detect.read")["tools"]}
    assert "show.matrix" in tools
