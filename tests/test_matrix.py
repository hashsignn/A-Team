"""The per-event risk matrix.

The one property worth defending here is that the two axes are INDEPENDENT.

A risk matrix asks two separate questions — how likely is this, and how bad if
it happens — and the whole diagnostic value comes from them being different
questions. Put probability on both axes and the matrix stops discriminating:
a 5%-chance CHF 1m event and a certain CHF 50k event land in the same cell,
which is precisely the pair a planner most needs told apart.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.pipeline import RunOptions, run
from engine.score import matrix as M

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def context(config):
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150))


def _risks(context):
    return [r for a in context.result.assessments for r in a.shipment_risks]


# =====================================================================
# The axes must not both carry the probability
# =====================================================================


def test_the_two_axes_multiply_back_to_the_expected_loss(context):
    """p_late x E[loss | late] == E[loss].

    This identity IS the proof that the probability is counted once. It held
    before only by accident of both sides being wrong in the same direction;
    it is asserted here so the impact axis cannot silently revert to the
    expected loss.

    Restricted to shipments carrying no always-on surcharge: a Rhine low-water
    surcharge is charged on every draw, late or not, so it sits outside the
    lateness decomposition by construction. The surcharge case is covered
    separately below.
    """
    multiplier = {
        a.event.event_id: a.event.cost_multiplier for a in context.result.assessments
    }
    checked = 0
    for assessment in context.result.assessments:
        if multiplier[assessment.event.event_id] != 1.0:
            continue
        for risk in assessment.shipment_risks:
            outcome = risk.do_nothing
            if outcome.p_late <= 0.0:
                continue
            recovered = outcome.p_late * outcome.conditional_loss_chf
            assert recovered == pytest.approx(outcome.expected_loss_chf, rel=1e-9)
            checked += 1
    assert checked, "no surcharge-free shipment to check the identity on"


def test_a_surcharge_is_the_only_thing_outside_the_identity(context):
    """It is charged whether or not the shipment ends up late, so it cannot be
    recovered from the late draws. The gap is therefore expected, and it is
    always in the same direction: expected loss exceeds the lateness part."""
    multiplier = {
        a.event.event_id: a.event.cost_multiplier for a in context.result.assessments
    }
    seen = False
    for assessment in context.result.assessments:
        if multiplier[assessment.event.event_id] == 1.0:
            continue
        for risk in assessment.shipment_risks:
            outcome = risk.do_nothing
            if outcome.p_late <= 0.0:
                continue
            recovered = outcome.p_late * outcome.conditional_loss_chf
            assert recovered <= outcome.expected_loss_chf + 1e-6
            seen = True
    if not seen:
        pytest.skip("no surcharge event in this fixture")


def test_conditional_loss_is_never_below_the_expected_loss_it_explains(context):
    """E[loss | late] >= E[loss] whenever a loss only accrues when late.

    A conditional average taken over a subset where the quantity is larger
    cannot come out smaller. If it does, the mask is wrong.
    """
    for risk in _risks(context):
        outcome = risk.do_nothing
        if outcome.p_late <= 0.0 or outcome.expected_loss_chf <= 0.0:
            continue
        assert outcome.conditional_loss_chf >= outcome.expected_loss_chf - 1e-6


def test_impact_band_reads_the_conditional_loss_not_the_expected_one(context, config):
    for risk in _risks(context):
        expected_band, _, _ = M.impact_band(risk.do_nothing.conditional_loss_chf, config)
        assert risk.impact_band == expected_band


def test_the_matrix_actually_discriminates(context):
    """A matrix whose points all land in one cell is a decoration.

    With the probability counted twice, the unlikely shipments collapsed into
    the bottom-left. This asserts real spread on both axes.
    """
    risks = _risks(context)
    probability_bands = {r.probability_band for r in risks}
    impact_bands = {r.impact_band for r in risks}
    assert len(probability_bands) >= 3, probability_bands
    assert len(impact_bands) >= 2, impact_bands


# =====================================================================
# The unsourced band
# =====================================================================


def test_an_unsourceable_probability_never_lands_on_the_axis(context):
    """It goes to the separate band, not to a computed 0.5 — the sibling
    project's exact bug, which this project exists partly to not repeat."""
    known = {
        a.event.event_id: a.event.probability_known for a in context.result.assessments
    }
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            if known[assessment.event.event_id]:
                assert risk.probability_band != M.UNSOURCED_BAND_ID
            else:
                assert risk.probability_band == M.UNSOURCED_BAND_ID


def test_the_unsourced_band_is_declared_outside_the_probability_axis(config):
    grid = M.band_grid(config)
    axis_ids = {b["id"] for b in grid["probability_bands"]}
    assert grid["unsourced_band"]["id"] not in axis_ids


def test_probability_band_of_none_is_the_unsourced_band(config):
    band_id, label = M.probability_band(None, config)
    assert band_id == M.UNSOURCED_BAND_ID
    assert "unsourced" in label.lower()


# =====================================================================
# Lead time is an overlay, not a third axis
# =====================================================================


@pytest.mark.parametrize(
    "actionability, style",
    [
        ("comfortable", "solid"),
        ("tightening", "solid"),
        ("too_late", "hollow"),
        ("no_action", "hollow"),
    ],
)
def test_lead_time_renders_as_a_ring_not_a_coordinate(actionability, style):
    assert M.ring_style(actionability) == style


def test_conditional_loss_is_zero_when_nothing_is_ever_late():
    """The degenerate case has to be a number, not a divide-by-zero."""
    from engine.score.impact import summarise
    from engine.simulate.draws import ShipmentDraws

    config = load_config()
    context = run(clock=AS_OF, config=config, options=RunOptions(shipment_count=20))
    shipment = context.shipments[0]
    n = 100
    never_late = ShipmentDraws(
        shipment_id=shipment.shipment_id,
        total_delay_days=np.zeros(n),
        lateness_days=np.zeros(n),
        driving_event_ids=[],
    )
    out = summarise(shipment, never_late, config)
    assert out["conditional_loss_chf"] == 0.0
    assert out["p_late"] == 0.0
