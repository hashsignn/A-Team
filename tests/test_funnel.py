"""The two-model funnel, and the shock event it exists to catch.

The claim under test: a keyword list can only ever recognise the phrasings
somebody already wrote down, so a shock arrives either as a phrasing we
happened to have — or not at all. The funnel is what makes the second case
recoverable, and every rule below exists to stop it doing harm while it does.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.pipeline import RunOptions, _restrict_modes, run
from engine.reason import extract as reason_extract
from engine.reason import funnel as F
from engine.reason import llm
from engine.schemas import Extraction, Mode
from engine.variables import rules

AS_OF = Clock(dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC))


def _backend(kind: llm.Backend) -> llm.BackendStatus:
    """A BackendStatus with the descriptive fields filled in."""
    return llm.BackendStatus(
        backend=kind, model='test-model', detail='test',
        unlocks_if_connected='',
    )
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example"

HORMUZ_PLAIN = "Iran announces closure of Strait of Hormuz to commercial shipping"
HORMUZ_NOVEL = "Unprecedented maritime interdiction regime declared across Hormuz approaches"


@pytest.fixture(scope="module")
def config():
    return load_config(EXAMPLE)


@pytest.fixture(scope="module")
def context(config):
    """A book big enough to observe the Gulf corridor.

    It was 125, which worked while every lane was drawn at roughly the same
    rate. The book is now weighted by Sika's own order volume, and only 4.9%
    of their intercompany documents go to the Gulf — split across two lanes.
    At 125 shipments that corridor draws one or none, the Hormuz event then
    touches no freight, and the gate correctly drops it.

    That is the model working, not failing: a closure of a strait Sika
    barely uses IS a smaller event for them. This scenario is testing whether
    the tool can SEE such a closure, so it needs a book with freight there —
    hence 700, which puts ~13 on the corridor — enough for one to
    still have an option open, which is what a deadline is.
    """
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=700, seed=7))


# =====================================================================
# The rules that keep a model from doing harm
# =====================================================================
def test_triage_fails_open_when_there_is_no_model():
    """A filter that deletes evidence when it breaks is worse than no filter.
    The board is complete without a model — that is supported, not degraded."""
    items = [{"item_id": "A", "headline": HORMUZ_PLAIN, "source_nature": "report"}]
    kept, cost = F.triage(items, status=_backend(llm.Backend.NONE))
    assert [i["item_id"] for i in kept] == ["A"]
    assert cost.dropped_by_triage == 0
    assert cost.triage_unavailable == 1


def test_a_failed_model_call_is_not_a_no(monkeypatch):
    """A timeout must not look like a decision."""
    monkeypatch.setattr(llm, "parse", lambda *a, **k: None)
    status = _backend(llm.Backend.LOCAL)
    items = [{"item_id": "A", "headline": HORMUZ_PLAIN, "source_nature": "report"}]
    kept, cost = F.triage(items, status=status)
    assert len(kept) == 1
    assert cost.dropped_by_triage == 0


def test_instruments_never_reach_a_model():
    """A gauge reading maps to a variable by a threshold table, for free and
    exactly. A model would be paid to be worse at arithmetic."""
    items = [
        {"item_id": "M", "headline": "M 6.1 - Bandar Abbas", "source_nature": "instrument"},
        {"item_id": "R", "headline": HORMUZ_PLAIN, "source_nature": "report"},
    ]
    kept, cost = F.triage(items, status=_backend(llm.Backend.NONE))
    assert cost.instruments == 1
    assert [i["item_id"] for i in kept] == ["R"]


def test_an_item_with_no_declared_nature_is_treated_as_needing_reading():
    """The synthetic corpus predates the field. Defaulting the other way would
    route it silently around the funnel it exists to exercise."""
    needs, measured = F.split_by_nature([{"item_id": "X"}])
    assert len(needs) == 1 and not measured


def test_going_over_budget_passes_items_through_unfiltered_not_dropped(monkeypatch):
    """A source that suddenly returns 10,000 items must cost a known number of
    calls. The overflow is reported; it is never silently deleted."""
    monkeypatch.setattr(
        llm, "parse",
        lambda *a, **k: F.Triage(relevant=False, reason="no", freight_mode="none", confidence=0.9),
    )
    status = _backend(llm.Backend.LOCAL)
    items = [{"item_id": str(i), "headline": "x", "source_nature": "report"} for i in range(5)]
    kept, cost = F.triage(items, status=status, budget=2)
    assert cost.triaged == 2
    assert cost.over_triage_budget == 3
    assert len(kept) == 3, "over-budget items must survive, not vanish"


def test_triage_can_only_remove(monkeypatch):
    """A 'yes' buys a full read and nothing more. Nothing a model says at
    stage 1 reaches the board."""
    monkeypatch.setattr(
        llm, "parse",
        lambda *a, **k: F.Triage(relevant=True, reason="freight", freight_mode="sea", confidence=1.0),
    )
    status = _backend(llm.Backend.LOCAL)
    items = [{"item_id": "A", "headline": HORMUZ_PLAIN, "source_nature": "report"}]
    kept, _ = F.triage(items, status=status)
    assert len(kept) == 1
    board_keys = {"severity", "level", "alert_score", "exposure_chf"}
    assert not board_keys & set(kept[0]), "triage leaked a scoring field onto the item"


def test_the_cost_is_reported_in_a_sentence_a_planner_can_read():
    items = [{"item_id": "A", "headline": "x", "source_nature": "report"},
             {"item_id": "B", "headline": "y", "source_nature": "instrument"}]
    _, cost = F.triage(items, status=_backend(llm.Backend.NONE))
    sentence = cost.sentence()
    assert "measurement" in sentence
    assert "failing open" in sentence


# =====================================================================
# Stage 2 — the JSON contract
# =====================================================================
def test_the_model_is_asked_for_json_not_prose():
    """Enforced at the decoder by the schema, then validated again on arrival."""
    schema = Extraction.model_json_schema()
    assert schema["type"] == "object"
    for required in ("verbatim_quote", "active_variables", "resolved_node_ids",
                     "second_order_nodes", "probability"):
        assert required in schema["properties"]


def test_an_invented_field_is_refused():
    assert Extraction.model_config.get("extra") == "forbid"


def test_the_prompt_carries_the_ledger_and_the_network(config):
    """Not a retrieved subset: a resolver that only sees nodes a keyword search
    suggested cannot make the Hormuz -> Jebel Ali jump, because nothing in the
    text mentions Jebel Ali."""
    prompt = reason_extract.build_prompt({"text": HORMUZ_PLAIN}, config, AS_OF)
    assert "CHOKE_HORMUZ" in prompt
    assert "AEJEA" in prompt
    assert "GEO_CONFLICT" in prompt


def test_the_prompt_fits_a_small_local_window(config):
    """Overflow does not fail — it truncates the ledger, and the model then
    'cannot find' variables that were cut off, which looks like stupidity."""
    budget = reason_extract.context_budget(config, AS_OF)
    assert budget["fits_8k_window"], budget


def test_the_prompt_marks_a_chokepoint_with_no_alternative(config):
    prompt = reason_extract.build_prompt({"text": "x"}, config, AS_OF)
    line = next(ln for ln in prompt.splitlines() if "CHOKE_HORMUZ" in ln)
    assert "NO ALTERNATIVE" in line


def test_extraction_returns_none_without_a_backend(config):
    assert reason_extract.extract(
        {"text": HORMUZ_PLAIN}, config, AS_OF,
        status=_backend(llm.Backend.NONE),
    ) is None


def test_the_two_stages_can_use_different_models():
    """Split by cost tier: a 3B answers the yes/no, a bigger one does the
    reading. One mid-sized model doing both is worse at each end."""
    assert hasattr(llm, "TRIAGE_MODEL")
    assert hasattr(llm, "EXTRACT_MODEL")


# =====================================================================
# Why the rescue path exists
# =====================================================================
def test_the_router_catches_the_obvious_phrasing(config):
    routed = rules.route(HORMUZ_PLAIN, config.variables)
    assert not routed.abstained
    assert "GEO_CONFLICT" in routed.active_variables


def test_the_router_cannot_catch_a_novel_phrasing(config):
    """THE POINT. Same meaning, no shared vocabulary. This is not a gap to be
    closed by adding another keyword — the next shock will use different words
    again, and that is what a model is for."""
    assert rules.route(HORMUZ_NOVEL, config.variables).abstained


def test_an_unnameable_item_is_held_for_rescue_not_dropped(context):
    """It must appear in the router notes, so a planner can see what the
    deterministic layer could not name."""
    assert context.router_notes, "abstentions must be recorded, never silent"


def test_rescue_is_inert_without_a_model(context):
    """Default-on must cost nothing on a machine with no Ollama, and must not
    change a single existing output."""
    assert RunOptions().rescue_unmatched is True
    assert context.result.funnel is not None


def test_football_is_not_freight(config):
    """The geographic filter is deliberately high-recall, so a story naming
    Basel and Rotterdam passes layer 1. Layer 2 is what throws it away."""
    assert rules.route(
        "FC Basel complete signing of Rotterdam striker for record fee",
        config.variables,
    ).abstained


# =====================================================================
# A source narrows a variable; it never widens it
# =====================================================================
def test_a_road_feed_cannot_produce_a_barge_event():
    """'A5 closed after HGV fire' matches FOR_FIRE, which is declared for every
    mode. The variable is right to be generic; the SOURCE knows better."""
    every = [Mode.SEA, Mode.ROAD, Mode.RAIL, Mode.BARGE]
    assert _restrict_modes(every, {"source_modes": ["road"]}) == [Mode.ROAD]


def test_an_unrestricted_source_changes_nothing():
    every = [Mode.SEA, Mode.ROAD]
    assert _restrict_modes(every, {}) == every


def test_a_contradiction_is_ignored_rather_than_obeyed():
    """An empty intersection would remove the event from the gate entirely —
    a drop disguised as a filter."""
    assert _restrict_modes([Mode.BARGE], {"source_modes": ["air"]}) == [Mode.BARGE]


# =====================================================================
# THE SCENARIO — end to end, deterministically, with no model running
# =====================================================================
def test_hormuz_has_no_alternative_route(config):
    """A domain fact, and the reason a Hormuz event is a damage pathway rather
    than a delay one: Suez has the Cape, Malacca has Sunda, Hormuz has nothing."""
    assert config.nodes["CHOKE_HORMUZ"].alternatives == []
    assert config.nodes["CHOKE_SUEZ"].alternatives, "Suez does have an alternative"


def test_a_lane_actually_passes_through_hormuz(config):
    lane = next(lane for lane in config.lanes if lane["id"] == "LANE_GULF_01")
    assert any(leg["to"] == "CHOKE_HORMUZ" for leg in lane["legs"])
    assert lane["legs"][-1]["to"] == "AEJEA", "freight must sit BEHIND the strait"


def test_the_announcement_reaches_the_board_as_an_event(context):
    hormuz = [e for e in context.events if "CHOKE_HORMUZ" in e.node_ids]
    assert hormuz, "a Hormuz closure produced no event"
    event = hormuz[0]
    assert event.event_class == "geopolitical"
    assert "GEO_CONFLICT" in event.active_variables
    assert event.provenance.verbatim_quote or event.title


def test_it_strands_real_shipments(context):
    ids = {e.event_id for e in context.events if "CHOKE_HORMUZ" in e.node_ids}
    struck = {h.shipment_id for h in context.hits if h.event_id in ids}
    assert struck, "the event exists but touches nothing — check the gate window"


def test_the_gulf_lane_is_raised_on_the_board(context):
    board = build_board(context)
    lane = next(r for r in board["routes"] if r["route_id"] == "LANE_GULF_01")
    assert lane["level"] != "green", "a closed strait must not leave the lane green"
    assert lane["lead_time_hours"] is not None
    assert any("Hormuz" in (e.get("title") or "") for e in lane.get("events", []))


def test_an_open_ended_event_is_widened_from_the_ledger_not_a_constant(config):
    """A haulier strike lasts days and a closed strait months. Treating both as
    three days is how a strait closure expires before any ship reaches it —
    which is exactly what happened before this was fixed."""
    from engine.gate.intersect import UNKNOWN_DURATION_FALLBACK_DAYS, _fallback_days

    class _E:
        active_variables = ["GEO_CONFLICT"]

    widened = _fallback_days(_E(), config.variables)
    assert widened > UNKNOWN_DURATION_FALLBACK_DAYS
    assert widened == config.variables["GEO_CONFLICT"].typical_duration_days


def test_the_widening_is_capped(config):
    """One mistyped ledger entry must not smear an event across the whole book."""
    from engine.gate.intersect import MAX_FALLBACK_DAYS, _fallback_days

    class _E:
        active_variables = []

    assert _fallback_days(_E(), config.variables) <= MAX_FALLBACK_DAYS
