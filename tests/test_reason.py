"""The reasoning layer and the check mechanism that keeps it honest.

The sibling operational-risk radar pairs an LLM with a deterministic
challenger. This is the supply-chain version, and the tests below are mostly
about the challenger: a check that never fails is not a check, so each one is
given something that SHOULD fail it.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from engine.config import load_config
from engine.reason import challenge as C
from engine.reason import llm
from engine.schemas import DelayTriple, Extraction

SOURCE = (
    "Rhine water levels at Kaub fell to 78 cm on Tuesday morning, and the "
    "Federal Waterways Administration said vessels should expect loading "
    "restricted to roughly 45 percent of full payload until the weekend at "
    "the earliest. Barge operators have begun applying low-water surcharges."
)


@pytest.fixture(scope="module")
def config():
    return load_config()


def _extraction(**overrides) -> Extraction:
    base = dict(
        what_happened="Rhine low water at Kaub restricts barge loading",
        event_class="waterway",
        location_text="Kaub, Rhine",
        resolved_node_ids=["GAUGE_KAUB"],
        starts_at=datetime(2026, 9, 18, 6, 0, tzinfo=UTC),
        ends_at=None,
        duration_confidence="estimated",
        realized=True,
        probability=None,
        probability_basis="already realized; the gauge reading is observed",
        delay_days=DelayTriple(optimistic=1.0, likely=4.0, pessimistic=9.0),
        delay_reasoning="Part-loading means more sailings for the same tonnage.",
        active_variables=["WAT_LOW_WATER"],
        why_active={"WAT_LOW_WATER": "gauge at 78 cm, below the derate threshold"},
        second_order_nodes=[],
        verbatim_quote=(
            "vessels should expect loading restricted to roughly 45 percent of "
            "full payload"
        ),
        confidence=0.86,
    )
    base.update(overrides)
    return Extraction(**base)


# =====================================================================
# Grounding — the check that matters most
# =====================================================================


def test_a_faithful_quote_passes(config):
    verdict = C.review(_extraction(), SOURCE, config)
    assert verdict.accepted, verdict.headline
    assert C.grounded(_extraction(), SOURCE).passed


def test_a_fabricated_quote_is_caught_and_blocks(config):
    """The most dangerous output this system can produce, because it is the
    one a planner will trust without checking."""
    fake = _extraction(
        verbatim_quote="the port authority confirmed a full closure until October"
    )
    verdict = C.review(fake, SOURCE, config)
    assert not verdict.accepted
    assert "grounded" in {c.name for c in verdict.blocking_failures}
    assert "does not appear in the source" in verdict.headline


def test_typographic_differences_are_not_treated_as_fabrication():
    """A model that reproduces a quote faithfully still swaps curly quotes and
    collapses line breaks. Rejecting that would throw away good extractions."""
    source = 'The authority said “vessels should expect—extended\ndelays” today.'
    ok = _extraction(verbatim_quote='vessels should expect-extended delays')
    assert C.grounded(ok, source).passed


def test_a_quote_too_short_to_be_evidence_is_rejected():
    """"strike" appears in every article about a strike; matching it proves
    nothing about whether the model read the source."""
    check = C.grounded(_extraction(verbatim_quote="low water"), SOURCE)
    assert not check.passed
    assert "too short" in check.detail


def test_an_empty_quote_blocks():
    check = C.grounded(_extraction(verbatim_quote=""), SOURCE)
    assert not check.passed and check.blocking


# =====================================================================
# The ledger is the authority, not the model
# =====================================================================


def test_an_invented_variable_id_blocks(config):
    bad = _extraction(
        active_variables=["WAT_KRAKEN"],
        why_active={"WAT_KRAKEN": "there is a kraken"},
    )
    verdict = C.review(bad, SOURCE, config)
    assert not verdict.accepted
    assert "variables_known" in {c.name for c in verdict.blocking_failures}


def test_activating_nothing_blocks(config):
    verdict = C.review(_extraction(active_variables=[], why_active={}), SOURCE, config)
    assert not verdict.accepted


def test_an_invented_node_blocks(config):
    bad = _extraction(resolved_node_ids=["XXNOWHERE"])
    verdict = C.review(bad, SOURCE, config)
    assert not verdict.accepted
    assert "nodes_known" in {c.name for c in verdict.blocking_failures}


def test_an_unexplained_activation_flags_but_does_not_block(config):
    """Useful-but-unexplained is still useful. The planner is told, and the
    reading is kept."""
    bare = _extraction(why_active={})
    verdict = C.review(bare, SOURCE, config)
    assert verdict.accepted
    assert "variables_justified" in {c.name for c in verdict.failures}
    assert "flags" in verdict.headline


# =====================================================================
# Probability: an absence must not become a value
# =====================================================================


def test_leaving_the_probability_unsourced_is_the_passing_answer(config):
    assert C.probability_honest(_extraction(probability=None), config).passed


def test_a_probability_outside_zero_to_one_blocks(config):
    check = C.probability_honest(_extraction(probability=1.4), config)
    assert not check.passed and check.blocking


def test_odds_invented_for_an_unsourceable_variable_block(config):
    """A union ballot has no published base rate. A model returning P=0.6 for
    one has produced something that looks like evidence and is not."""
    unsourceable = next(
        v.id for v in config.variables.values() if not v.probability_sourceable
    )
    bad = _extraction(
        active_variables=[unsourceable],
        why_active={unsourceable: "the article describes exactly this"},
        probability=0.6,
        probability_basis="   ",
    )
    check = C.probability_honest(bad, config)
    assert not check.passed and check.blocking
    assert unsourceable in check.detail


def test_a_stated_basis_lets_a_probability_through(config):
    unsourceable = next(
        v.id for v in config.variables.values() if not v.probability_sourceable
    )
    ok = _extraction(
        active_variables=[unsourceable],
        why_active={unsourceable: "described in the source"},
        probability=0.6,
        probability_basis="ballot result published by the union on 14 Sep",
    )
    assert C.probability_honest(ok, config).passed


# =====================================================================
# Delay: wide on purpose
# =====================================================================


def test_reading_a_longer_duration_than_the_family_default_is_allowed(config):
    """This is the value of the reasoning layer, not a fault in it: clamping
    to the family default would delete the thing being paid for."""
    longer = _extraction(
        delay_days=DelayTriple(optimistic=8.0, likely=12.0, pessimistic=18.0),
        delay_reasoning="the source says 'until the weekend at the earliest'",
    )
    assert C.delay_plausible(longer, config).passed


def test_an_absurd_delay_flags_without_blocking(config):
    absurd = _extraction(
        delay_days=DelayTriple(optimistic=200.0, likely=400.0, pessimistic=900.0)
    )
    check = C.delay_plausible(absurd, config)
    assert not check.passed
    assert not check.blocking
    assert C.review(absurd, SOURCE, config).accepted


def test_an_unordered_triple_blocks(config):
    """Pydantic guards the DelayTriple itself, so the malformed case has to be
    constructed around it — the check still has to exist, because a future
    schema change could relax that validator."""
    bad = _extraction()
    object.__setattr__(
        bad, "delay_days",
        DelayTriple.model_construct(optimistic=9.0, likely=2.0, pessimistic=1.0),
    )
    check = C.delay_plausible(bad, config)
    assert not check.passed and check.blocking


# =====================================================================
# Agreement with the keyword router
# =====================================================================


def test_the_router_runs_alongside_and_is_reported(config):
    verdict = C.review(_extraction(), SOURCE, config)
    assert "agreed" in verdict.agreement
    assert "model_only" in verdict.agreement
    assert "rules_only" in verdict.agreement
    assert 0.0 <= verdict.agreement["jaccard"] <= 1.0


def test_agreement_is_reported_even_when_the_router_abstains(config):
    silent = "Quarterly logistics newsletter: nothing of note this week."
    extraction = _extraction(
        verbatim_quote="Quarterly logistics newsletter: nothing of note this week."
    )
    verdict = C.review(extraction, silent, config)
    assert "rules_abstained" in verdict.agreement


def test_a_rejected_extraction_still_carries_the_router_s_answer(config):
    """A rejection is not a gap: the deterministic answer is right there."""
    verdict = C.review(_extraction(verbatim_quote="invented"), SOURCE, config)
    assert not verdict.accepted
    assert verdict.agreement != {}


# =====================================================================
# The backend is a component, not the architecture
# =====================================================================


def test_no_backend_is_a_supported_state_not_a_crash(monkeypatch):
    monkeypatch.setenv("RADAR_LLM_BACKEND", "none")
    status = llm.detect()
    assert status.backend is llm.Backend.NONE
    assert not status.available
    assert llm.parse(Extraction, "sys", "prompt", status) is None
    assert llm.ask_text("sys", "prompt", status) is None


def test_an_absent_model_says_what_connecting_it_would_unlock(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")  # nothing listens
    monkeypatch.delenv("RADAR_LLM_BACKEND", raising=False)
    status = llm.detect()
    assert status.backend is llm.Backend.NONE
    assert status.unlocks_if_connected.strip()
    assert "router still produces a complete board" in status.unlocks_if_connected


def test_the_report_line_has_the_same_shape_as_a_feed_socket(monkeypatch):
    monkeypatch.setenv("RADAR_LLM_BACKEND", "none")
    line = llm.report()
    for key in ("key", "label", "status", "detail", "unlocks_if_connected"):
        assert key in line
    assert line["status"] in ("connected", "absent")


def test_review_does_not_mutate_what_it_is_given(config):
    extraction = _extraction()
    before = copy.deepcopy(extraction.model_dump())
    C.review(extraction, SOURCE, config)
    assert extraction.model_dump() == before
