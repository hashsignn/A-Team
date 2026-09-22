"""The v2 surface, exercised through its handlers.

Called directly rather than over HTTP, the same way ``test_serving.py`` does:
the routing is FastAPI's problem and is not what these tests are about. What
they are about is that the contract the browser depends on does not move, and
that a refusal comes back as a reason rather than as a stack trace.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from api import fast_routes
from engine.clock import Clock
from engine.config import load_config
from engine.fast.execute import LEDGER
from engine.fast.watch import WATCHER
from engine.pipeline import RunOptions, run

AS_OF = "2026-09-16"
SHIPMENTS = 220


@pytest.fixture(scope="module", autouse=True)
def bound():
    """One pipeline run, shared, bound the way main.py binds it."""
    context = run(
        clock=Clock.at(AS_OF),
        config=load_config(),
        options=RunOptions(shipment_count=SHIPMENTS),
    )
    fast_routes.bind(lambda as_of, shipments: context)
    yield context
    fast_routes.bind(None)


@pytest.fixture(autouse=True)
def clean():
    LEDGER.clear()
    WATCHER.clear()
    yield
    LEDGER.clear()
    WATCHER.clear()


def body(response):
    return json.loads(response.body)


def first_executable(payload):
    for option in payload["headline"]["options"]:
        if option["executable"]:
            return payload["headline"]["route_id"], option["option_id"]
    raise AssertionError("no executable option on the headline lane")


# ------------------------------------------------------------------ /now
def test_now_leads_with_one_decision_not_a_list():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    assert payload["headline"] is not None
    assert isinstance(payload["queue"], list)
    assert len(payload["queue"]) <= 6, "the queue is a short list, not a table"


def test_the_headline_carries_a_sentence_anyone_can_read():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    assert payload["sentence"]
    assert "lane" in payload["sentence"] or "Nothing" in payload["sentence"]


def test_no_risk_matrix_or_radar_reaches_the_fast_payload():
    """Removed deliberately. If either comes back, the declutter has been
    undone by a well-meaning merge."""
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    blob = json.dumps(payload)
    for gone in ("matrix_grid", '"radar"', "probability_band", "impact_band"):
        assert gone not in blob, f"{gone} is back on the fast payload"


def test_every_offered_option_is_time_first():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    for option in payload["headline"]["options"]:
        assert "hours_to_resolve" in option
        assert "days_late_after" in option
        assert "on_time" in option


def test_the_options_come_back_already_ranked():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    options = payload["headline"]["options"]
    keys = [
        (not o["on_time"], not o["restores_delivery"], not o["executable"],
         o["days_late_after"], o["hours_to_resolve"])
        for o in options
    ]
    assert keys == sorted(keys), "the browser must not have to sort these"


# ---------------------------------------------------------------- /route
def test_a_route_page_has_delay_and_alternatives_and_nothing_else():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    route_id = payload["headline"]["route_id"]
    detail = body(fast_routes.route(route_id, as_of=AS_OF, shipments=SHIPMENTS))

    assert detail["delay_days"] is not None
    assert "options" in detail
    assert "vehicles" not in detail and "legs" not in detail, (
        "the per-vehicle grid does not belong on the fast route page"
    )


def test_an_unaffected_route_is_a_404_not_an_empty_page():
    with pytest.raises(HTTPException) as exc:
        fast_routes.route("LANE_DOES_NOT_EXIST", as_of=AS_OF, shipments=SHIPMENTS)
    assert exc.value.status_code == 404


# ------------------------------------------------------------------ /act
def test_acting_takes_one_call_and_no_confirmation():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    route_id, option_id = first_executable(payload)

    response = fast_routes.act(
        {"route_id": route_id, "option_id": option_id},
        as_of=AS_OF, shipments=SHIPMENTS,
    )
    result = body(response)

    assert response.status_code == 200
    assert result["ok"] is True
    assert result["executed"], "one POST, and it is running"
    assert result["sentence"]


def test_acting_executes_once_per_consignment_not_once_per_assessment():
    """A consignment hit by two events is still one consignment. Booking it
    twice leaves the second execution impossible to undo, because the two
    share an id."""
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    route_id, option_id = first_executable(payload)

    result = body(fast_routes.act(
        {"route_id": route_id, "option_id": option_id},
        as_of=AS_OF, shipments=SHIPMENTS,
    ))
    ids = [e["shipment_id"] for e in result["executed"]]
    assert len(ids) == len(set(ids))


def test_every_execution_reports_what_dispatch_actually_did():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    route_id, option_id = first_executable(payload)

    result = body(fast_routes.act(
        {"route_id": route_id, "option_id": option_id},
        as_of=AS_OF, shipments=SHIPMENTS,
    ))
    dispatch = result["executed"][0]["dispatch"]
    assert dispatch["sentence"]
    assert dispatch["delivered"] + dispatch["recorded"] + dispatch["failed"] > 0


def test_an_option_that_is_no_longer_offered_is_refused_by_name():
    with pytest.raises(HTTPException) as exc:
        fast_routes.act(
            {"route_id": "LANE_RHINE_01", "option_id": "reroute:Fly it"},
            as_of=AS_OF, shipments=SHIPMENTS,
        )
    assert exc.value.status_code == 404
    assert "no longer" in exc.value.detail


def test_a_request_missing_its_target_is_rejected():
    with pytest.raises(HTTPException) as exc:
        fast_routes.act({}, as_of=AS_OF, shipments=SHIPMENTS)
    assert exc.value.status_code == 400


# ----------------------------------------------------------------- /undo
def test_what_was_executed_can_be_pulled_back():
    payload = body(fast_routes.now(as_of=AS_OF, shipments=SHIPMENTS))
    route_id, option_id = first_executable(payload)
    done = body(fast_routes.act(
        {"route_id": route_id, "option_id": option_id},
        as_of=AS_OF, shipments=SHIPMENTS,
    ))

    result = body(fast_routes.undo(
        {"execution_ids": [e["execution_id"] for e in done["executed"]]},
        as_of=AS_OF, shipments=SHIPMENTS,
    ))

    assert len(result["undone"]) == len(done["executed"])
    assert not result["refused"]


def test_undoing_nothing_is_a_bad_request_not_a_silent_success():
    with pytest.raises(HTTPException) as exc:
        fast_routes.undo({}, as_of=AS_OF, shipments=SHIPMENTS)
    assert exc.value.status_code == 400


# ----------------------------------------------------------------- hooks
def test_a_hook_raises_an_incident_and_computes_the_options():
    result = body(fast_routes.hook_incident(
        {
            "shipment_id": None,
            "route_id": "LANE_RHINE_01",
            "headline": "Lock closure at Kaub",
            "kind": "external",
            "reference": "TEST-HOOK-1",
        },
        as_of=AS_OF, shipments=SHIPMENTS,
    ))
    assert result["ok"] is True
    assert result["incident"]["headline"] == "Lock closure at Kaub"
    assert isinstance(result["options"], list)


def test_a_hook_cannot_execute_anything():
    """The line where the safety of instant execution actually lives."""
    result = body(fast_routes.hook_incident(
        {"headline": "Everything is on fire", "reference": "TEST-HOOK-2"},
        as_of=AS_OF, shipments=SHIPMENTS,
    ))
    assert len(LEDGER) == 0
    assert "Nothing has been executed" in result["note"]


def test_an_unauthenticated_hook_says_so_rather_than_pretending(monkeypatch):
    monkeypatch.delenv(fast_routes.HOOK_TOKEN_ENV, raising=False)
    result = body(fast_routes.hook_incident(
        {"headline": "Port strike", "reference": "TEST-HOOK-3"},
        as_of=AS_OF, shipments=SHIPMENTS,
    ))
    assert result["authenticated"] is False


def test_a_wrong_token_is_rejected_when_one_is_configured(monkeypatch):
    monkeypatch.setenv(fast_routes.HOOK_TOKEN_ENV, "the-real-token")
    with pytest.raises(HTTPException) as exc:
        fast_routes.hook_incident(
            {"headline": "Port strike", "reference": "TEST-HOOK-4"},
            as_of=AS_OF, shipments=SHIPMENTS, authorization="Bearer wrong",
        )
    assert exc.value.status_code == 401


def test_a_right_token_is_accepted_and_marked_authenticated(monkeypatch):
    monkeypatch.setenv(fast_routes.HOOK_TOKEN_ENV, "the-real-token")
    result = body(fast_routes.hook_incident(
        {"headline": "Port strike", "reference": "TEST-HOOK-5"},
        as_of=AS_OF, shipments=SHIPMENTS, authorization="Bearer the-real-token",
    ))
    assert result["authenticated"] is True
