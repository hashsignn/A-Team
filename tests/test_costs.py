"""Running this prototype costs nothing, and these tests are how we know.

Every paid service the product could use is declared — shown as something
that can be attached — and none is connected. See engine/costs.py for why the
switch is a constant in code rather than an environment variable.

The strongest test here runs the whole pipeline with outbound networking
replaced by a trap that records every attempt to leave the machine, an API
key in the environment, and a fake paid SDK that records its own use. Before
this rule existed that combination was enough to bill: with no local model
running, the board switched itself onto the paid API.
"""

from __future__ import annotations

import sys
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from engine import costs
from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.fast import dispatch as dispatch_mod
from engine.ingest import watergauge
from engine.ingest.sources import fetch
from engine.ingest.sources.catalog import CATALOG
from engine.ingest.sources.spec import Cost
from engine.pipeline import RunOptions, run
from engine.reason import ask, llm

AS_OF = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class NetworkTrap:
    """Stands in for urlopen. Every call fails as if offline, and is recorded.

    Recorded rather than raised as a test failure, because every caller in
    this codebase catches network errors on purpose — a raised assertion
    would be swallowed, and the test would pass for the wrong reason.
    """

    def __init__(self):
        self.local: list[str] = []
        self.outside: list[str] = []

    def __call__(self, url, *args, **kwargs):
        target = getattr(url, "full_url", None) or str(url)
        host = urllib.parse.urlsplit(target).hostname or ""
        (self.local if host in LOCAL_HOSTS else self.outside).append(target)
        raise urllib.error.URLError("trapped: no network in this test")

    def hosts(self) -> set[str]:
        return {urllib.parse.urlsplit(u).hostname for u in self.outside}


class PaidSdk:
    """A fake `anthropic` package that records whether anything used it."""

    def __init__(self):
        self.used: list[str] = []
        module = types.ModuleType("anthropic")
        module.Anthropic = self._client
        self.module = module

    def _client(self, *args, **kwargs):
        self.used.append("constructed a paid client")
        raise AssertionError("a paid client was constructed")


@pytest.fixture
def trap(monkeypatch):
    t = NetworkTrap()
    monkeypatch.setattr(urllib.request, "urlopen", t)
    return t


@pytest.fixture
def paid_sdk(monkeypatch):
    sdk = PaidSdk()
    monkeypatch.setitem(sys.modules, "anthropic", sdk.module)
    return sdk


@pytest.fixture
def key_in_environment(monkeypatch):
    """The situation that used to bill: a key present for some other reason."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv("RADAR_LLM_BACKEND", raising=False)


class Answer(BaseModel):
    text: str


# =====================================================================
# The switch
# =====================================================================
def test_nothing_that_bills_is_connected():
    assert costs.PAID_SERVICES_CONNECTED is False, (
        "engine/costs.py now connects paid services. This build is a "
        "prototype that must cost nothing to run; if that has deliberately "
        "changed, change this test in the same review."
    )


def test_a_refusal_says_what_would_have_billed_and_how_to_attach_it():
    with pytest.raises(costs.PaidServiceNotConnected) as exc:
        costs.refuse("The Anthropic API")
    message = str(exc.value)
    assert "bills per use" in message
    assert "engine/costs.py" in message


# =====================================================================
# The reasoning model
# =====================================================================
def test_a_key_in_the_environment_does_not_select_the_paid_model(
    monkeypatch, key_in_environment,
):
    monkeypatch.setattr(llm, "_ollama_reachable", lambda: False)
    status = llm.detect()
    assert status.backend is llm.Backend.NONE
    assert "ignored" in status.detail
    assert "not connected in this prototype" in status.detail


def test_asking_for_the_paid_backend_by_name_does_not_select_it(
    monkeypatch, key_in_environment,
):
    monkeypatch.setenv("RADAR_LLM_BACKEND", "api")
    monkeypatch.setattr(llm, "_ollama_reachable", lambda: False)
    status = llm.detect()
    assert status.backend is llm.Backend.NONE
    assert "bills per use" in status.detail


def test_the_free_local_model_is_still_used_when_it_is_running(
    monkeypatch, key_in_environment,
):
    monkeypatch.setattr(llm, "_ollama_reachable", lambda: True)
    assert llm.detect().backend is llm.Backend.LOCAL


def test_the_paid_socket_is_still_shown_as_something_to_attach(
    monkeypatch, key_in_environment,
):
    """Declared, not deleted: the status a planner sees names it."""
    monkeypatch.setattr(llm, "_ollama_reachable", lambda: False)
    shown = llm.report()
    assert shown["status"] == "absent"
    assert "Anthropic API" in shown["unlocks_if_connected"]


def test_the_funnel_cannot_bill_even_with_a_hand_built_status(paid_sdk):
    """``parse`` takes its status from the caller, so guarding ``detect``
    alone would leave a way round it."""
    paid = llm.BackendStatus(llm.Backend.API, llm.API_MODEL, "", "")
    assert llm.parse(Answer, "system", "prompt", status=paid) is None
    assert paid_sdk.used == []


def test_the_ask_box_cannot_bill_even_with_a_hand_built_status(paid_sdk):
    paid = llm.BackendStatus(llm.Backend.API, llm.API_MODEL, "", "")
    assert llm.ask_text("system", "prompt", status=paid) is None
    assert paid_sdk.used == []


# =====================================================================
# Sources and dispatch
# =====================================================================
def test_a_paid_source_is_never_fetched_even_with_its_key_and_the_network(
    monkeypatch, trap,
):
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    gdelt = next(s for s in CATALOG if s.key == "gdelt_doc")
    paid = replace(gdelt, cost=Cost.PAID, enabled=True)

    assert not paid.runnable
    items, report = fetch.collect(paid, AS_OF)

    assert items == []
    assert trap.outside == []
    assert report.cost == "paid"
    assert "paid" in report.detail.lower()


def test_every_source_that_can_run_is_free():
    for spec in CATALOG:
        if spec.runnable:
            assert spec.cost in (Cost.FREE, Cost.FREE_WITH_KEY), spec.key


def test_outbound_channels_ship_disabled():
    for channel in dispatch_mod.channels(load_config()):
        if channel.kind != "socket":
            assert not channel.enabled, f"{channel.channel_id} ships enabled"


def test_a_disabled_channel_sends_nothing_with_a_url_and_the_network(
    monkeypatch, trap,
):
    """Every precondition for sending, except the switch. Still recorded."""
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    for channel in dispatch_mod.channels(load_config()):
        if channel.url_env:
            monkeypatch.setenv(channel.url_env, "https://paid-sms.example/hook")

    dispatch_mod.OUTBOX.clear()
    receipts = dispatch_mod.fan_out(load_config(), {"type": "test"})

    assert trap.outside == []
    external = [r for r in receipts if r.channel_id != "driver_socket"]
    assert external and all(r.status == "recorded" for r in external)


def test_the_gauge_stays_off_the_network_unless_it_is_allowed(monkeypatch, trap):
    monkeypatch.delenv("RADAR_ALLOW_NETWORK", raising=False)
    series, report = watergauge.fetch_kaub(load_config(), Clock.at(AS_OF))
    assert trap.outside == [], "the gauge phoned out with egress off"
    assert series, "it should have served the recorded fixture instead"


# =====================================================================
# The whole thing
# =====================================================================
def _everything():
    ctx = run(clock=Clock.at(AS_OF), config=load_config(),
              options=RunOptions(shipment_count=125, seed=7))
    board = build_board(ctx)
    ask.board_question(board, "Which lane needs a decision first?")
    return ctx


def test_a_whole_run_reaches_nothing_but_this_machine(
    trap, paid_sdk, key_in_environment, monkeypatch,
):
    """The board, the funnel and the Ask box, with a key in the environment
    and egress off: the only thing touched is this machine."""
    monkeypatch.delenv("RADAR_ALLOW_NETWORK", raising=False)
    _everything()

    assert trap.outside == [], f"reached outside this machine: {trap.outside}"
    assert paid_sdk.used == []


def test_with_the_network_on_only_free_public_sources_are_reached(
    trap, paid_sdk, key_in_environment, monkeypatch,
):
    """Egress on is the recording step. It may reach the free feeds and the
    free gauge, and nothing else — and still never the paid SDK."""
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    _everything()

    free = {urllib.parse.urlsplit(s.url).hostname for s in CATALOG if s.runnable}
    free.add(urllib.parse.urlsplit(watergauge.PEGELONLINE_URL).hostname)
    assert trap.hosts() <= free, f"reached beyond the free sources: {trap.hosts() - free}"
    assert paid_sdk.used == []
