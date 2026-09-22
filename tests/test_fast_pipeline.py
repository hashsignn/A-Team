"""The event-driven half: bus, watcher, dispatch, instant execution.

The property being protected throughout is that nothing waits for a batch and
nothing silently claims to have happened.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from engine.config import load_config
from engine.fast import dispatch as dispatch_mod
from engine.fast import execute as execute_mod
from engine.fast import margin as margin_mod
from engine.fast import watch
from engine.fast.bus import TOPIC_DISRUPTION, TOPIC_FIELD, TOPIC_SIGNAL, Bus
from engine.fast.options import FastOption
from tests.test_fast_options import make_shipment

NOW = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def bus():
    return Bus()


def report(**overrides) -> dict:
    base = {
        "report_id": "R-1",
        "shipment_id": "SYN-0042",
        "status": "moving",
        "load_state": "intact",
        "position": "A5 near Karlsruhe",
        "observed_at": NOW.isoformat(),
        "note": None,
        "first_hand": True,
        "authenticated": True,
        "photos": [],
    }
    base.update(overrides)
    return base


def option(**overrides) -> FastOption:
    shipment = make_shipment()
    config = load_config()
    base = dict(
        option_id="SYN-0042:reroute:A-B",
        shipment_id="SYN-0042",
        kind="reroute",
        label="Reroute via Zürich",
        detail="",
        owner="us",
        hours_to_start=4.0,
        hours_to_resolve=4.0,
        days_late_after=0.0,
        on_time=True,
        cost_chf=1_200.0,
        margin=margin_mod.evaluate(shipment, config, 1_200.0, 0.0),
    )
    base.update(overrides)
    return FastOption(**base)


# ------------------------------------------------------------------- bus
def test_a_message_reaches_every_subscriber_in_order(bus):
    seen: list[int] = []
    bus.on(TOPIC_SIGNAL, lambda m: seen.append(m.seq))
    bus.on(TOPIC_SIGNAL, lambda m: seen.append(-m.seq))

    bus.publish(TOPIC_SIGNAL, {"item": 1}, at=NOW.isoformat())
    bus.publish(TOPIC_SIGNAL, {"item": 2}, at=NOW.isoformat())

    assert seen == [1, -1, 2, -2]


def test_a_broken_subscriber_cannot_silence_the_bus(bus):
    """One listener raising must not stop the others hearing."""
    heard: list[str] = []

    def explode(_):
        raise RuntimeError("boom")

    bus.on(TOPIC_SIGNAL, explode)
    bus.on(TOPIC_SIGNAL, lambda m: heard.append(m.payload["item"]))

    bus.publish(TOPIC_SIGNAL, {"item": "still delivered"}, at=NOW.isoformat())

    assert heard == ["still delivered"]


def test_a_subscriber_only_hears_its_own_topics(bus):
    heard: list[str] = []
    bus.on(TOPIC_FIELD, lambda m: heard.append(m.topic))

    bus.publish(TOPIC_SIGNAL, {}, at=NOW.isoformat())
    bus.publish(TOPIC_FIELD, {}, at=NOW.isoformat())

    assert heard == [TOPIC_FIELD]


def test_an_unknown_topic_is_a_failure_not_a_silent_no_op(bus):
    with pytest.raises(ValueError, match="unknown topic"):
        bus.publish("gossip", {}, at=NOW.isoformat())


def test_a_slow_reader_loses_the_oldest_not_the_newest(bus):
    """A dashboard that has fallen behind wants current state, not a backlog."""
    async def scenario():
        sub = bus.stream(TOPIC_SIGNAL, limit=2)
        for i in range(5):
            bus.publish(TOPIC_SIGNAL, {"n": i}, at=NOW.isoformat())
        await asyncio.sleep(0)   # let the loop deliver
        got = []
        while True:
            message = await bus.drain(sub, timeout=0.05)
            if message is None:
                break
            got.append(message.payload["n"])
        bus.release(sub)
        return got, sub.dropped

    got, dropped = asyncio.run(scenario())
    assert got == [3, 4]
    assert dropped == 3


def test_replay_returns_only_what_a_late_joiner_missed(bus):
    bus.publish(TOPIC_SIGNAL, {"n": 1}, at=NOW.isoformat())
    mark = bus.sequence
    bus.publish(TOPIC_SIGNAL, {"n": 2}, at=NOW.isoformat())

    assert [m.payload["n"] for m in bus.replay(TOPIC_SIGNAL, since_seq=mark)] == [2]


# --------------------------------------------------------------- watcher
def test_a_vehicle_that_is_moving_is_not_an_incident():
    """A stream that fires on every heartbeat gets muted, which costs more
    than the latency it saved."""
    assert watch.from_field_report(report(status="moving"), NOW) is None


def test_a_queue_at_a_port_is_not_an_incident():
    assert watch.from_field_report(report(status="queued"), NOW) is None


@pytest.mark.parametrize("status", ["held", "stopped"])
def test_a_stopped_vehicle_is_an_incident_immediately(status):
    incident = watch.from_field_report(report(status=status), NOW)
    assert incident is not None
    assert incident.kind == "vehicle_stopped"
    assert incident.shipment_id == "SYN-0042"


def test_damage_is_an_incident_even_while_the_vehicle_moves():
    incident = watch.from_field_report(
        report(status="moving", load_state="damaged"), NOW
    )
    assert incident is not None and incident.kind == "load_damaged"


def test_a_small_eta_correction_is_absorbed_not_escalated():
    soon = (NOW - timedelta(hours=2)).isoformat()
    assert watch.from_field_report(report(revised_eta=soon), NOW) is None


def test_a_large_eta_slip_raises_an_incident():
    late = (NOW - timedelta(hours=30)).isoformat()
    incident = watch.from_field_report(report(revised_eta=late), NOW)
    assert incident is not None and incident.kind == "eta_slip"


def test_confidence_comes_from_the_role_not_from_the_wording():
    observed = watch.from_field_report(
        report(status="held", first_hand=True, authenticated=True), NOW)
    relayed = watch.from_field_report(
        report(status="held", first_hand=False, authenticated=True), NOW)
    assert observed.confidence == "observed"
    assert relayed.confidence == "relayed"


def test_an_incoming_report_publishes_before_it_is_classified(bus):
    """The raw report goes out whatever it says; only the incident is filtered."""
    topics: list[str] = []
    bus.on([TOPIC_FIELD, TOPIC_DISRUPTION], lambda m: topics.append(m.topic))
    watcher = watch.Watcher(bus=bus)

    watcher.saw_report(report(status="moving"), NOW)
    assert topics == [TOPIC_FIELD]

    watcher.saw_report(report(report_id="R-2", status="held"), NOW)
    assert topics == [TOPIC_FIELD, TOPIC_FIELD, TOPIC_DISRUPTION]


def test_the_same_incident_twice_is_recorded_once(bus):
    watcher = watch.Watcher(bus=bus)
    watcher.saw_report(report(status="held"), NOW)
    watcher.saw_report(report(status="held"), NOW)
    assert len(watcher) == 1


def test_incidents_come_back_newest_first(bus):
    watcher = watch.Watcher(bus=bus)
    watcher.saw_report(report(report_id="R-1", status="held"), NOW)
    watcher.saw_report(report(report_id="R-2", status="stopped"), NOW)
    assert [i.incident_id for i in watcher.live()] == ["INC:R-2", "INC:R-1"]


# -------------------------------------------------------------- dispatch
def test_a_disabled_channel_records_rather_than_claiming_to_have_sent(config):
    dispatch_mod.OUTBOX.clear()
    receipts = dispatch_mod.fan_out(
        config, {"type": "test", "at": NOW.isoformat()}, audiences={"carrier"}
    )
    assert receipts
    assert all(r.status == "recorded" for r in receipts)
    assert all(not r.ok for r in receipts)
    assert dispatch_mod.OUTBOX.messages


def test_the_summary_says_plainly_when_nothing_left_the_building(config):
    dispatch_mod.OUTBOX.clear()
    receipts = dispatch_mod.fan_out(
        config, {"type": "test", "at": NOW.isoformat()}, audiences={"authority"}
    )
    summary = dispatch_mod.summarise(receipts)
    assert summary["delivered"] == 0
    assert "Nothing left the building" in summary["sentence"]


def test_the_socket_channel_works_offline(config, bus):
    """In-process fan-out needs no egress, which is how the driver's phone and
    the planner's screen hear about each other on a laptop."""
    receipts = dispatch_mod.fan_out(
        config, {"type": "test", "at": NOW.isoformat()},
        audiences={"driver"}, bus=bus,
    )
    assert any(r.kind == "socket" and r.ok for r in receipts)


def test_an_unset_url_variable_is_reported_by_name(config, monkeypatch):
    monkeypatch.setenv("RADAR_OPS_WEBHOOK_URL", "")
    receipts = dispatch_mod.fan_out(
        config, {"type": "test", "at": NOW.isoformat()}, audiences={"ground_ops"}
    )
    webhook = [r for r in receipts if r.kind == "webhook"]
    assert webhook
    assert any("disabled" in r.detail or "RADAR_" in r.detail for r in webhook)


# ------------------------------------------------------------- execution
def test_execution_needs_no_confirmation_step(config, bus):
    ledger = execute_mod.Ledger()
    result = execute_mod.execute(
        option(), config, NOW, ledger=ledger, bus=bus
    )
    assert isinstance(result, execute_mod.Execution)
    assert len(ledger) == 1


def test_an_option_we_do_not_own_is_refused_with_a_reason(config, bus):
    result = execute_mod.execute(
        option(owner="carrier"), config, NOW,
        ledger=execute_mod.Ledger(), bus=bus,
    )
    assert isinstance(result, execute_mod.Refusal)
    assert result.reason == "not ours"


def test_a_vetoed_option_cannot_be_clicked_into_viability(config, bus):
    shipment = make_shipment(value_chf=10_000.0)
    ruinous = option(
        cost_chf=90_000.0,
        margin=margin_mod.evaluate(shipment, config, 90_000.0, 0.0),
    )
    result = execute_mod.execute(
        ruinous, config, NOW, ledger=execute_mod.Ledger(), bus=bus
    )
    assert isinstance(result, execute_mod.Refusal)
    assert result.reason == "vetoed"


def test_an_expired_option_is_refused(config, bus):
    result = execute_mod.execute(
        option(expired=True, expired_reason="only 1 h remains"),
        config, NOW, ledger=execute_mod.Ledger(), bus=bus,
    )
    assert isinstance(result, execute_mod.Refusal)
    assert result.reason == "expired"


def test_an_execution_can_be_pulled_back_inside_its_window(config, bus):
    ledger = execute_mod.Ledger()
    done = execute_mod.execute(option(), config, NOW, ledger=ledger, bus=bus)
    undone = execute_mod.undo(
        done.execution_id, config, NOW + timedelta(minutes=2),
        ledger=ledger, bus=bus,
    )
    assert isinstance(undone, execute_mod.Execution)
    assert undone.undone is True


def test_the_undo_window_closes_rather_than_lying(config, bus):
    """Saying 'undone' in our UI once a vehicle has moved is a lie the planner
    then acts on."""
    ledger = execute_mod.Ledger()
    done = execute_mod.execute(option(), config, NOW, ledger=ledger, bus=bus)
    refused = execute_mod.undo(
        done.execution_id, config, NOW + timedelta(hours=4),
        ledger=ledger, bus=bus,
    )
    assert isinstance(refused, execute_mod.Refusal)
    assert refused.reason == "window closed"


def test_an_execution_carries_the_confidence_of_what_triggered_it(config, bus):
    """Instant execution is safe because the planner is told what they are
    trusting, not because the signal was verified first."""
    ledger = execute_mod.Ledger()
    done = execute_mod.execute(
        option(), config, NOW, ledger=ledger, bus=bus,
        confidence="relayed", trigger="hook:carrier",
    )
    payload = done.as_dict(NOW)
    assert payload["confidence"] == "relayed"
    assert payload["trigger"] == "hook:carrier"
    assert payload["undoable"] is True


def test_undoing_something_unknown_is_refused_not_ignored(config, bus):
    result = execute_mod.undo(
        "EX:nope", config, NOW, ledger=execute_mod.Ledger(), bus=bus
    )
    assert isinstance(result, execute_mod.Refusal)
    assert result.reason == "unknown"
