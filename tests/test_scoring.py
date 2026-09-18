"""Tests for the paths BRIEF §8.1 says will otherwise ship a bug.

    "Write a test per scoring path. It is the bug that will otherwise ship."

The sibling project shipped a confident 0.50 across six dimensions because a
field was optional-with-default. These tests exist so that cannot recur, and so
the three arithmetic corrections this build makes to the brief stay corrected.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from engine.clock import UTC, Clock, ensure_utc, overlaps
from engine.config import load_config
from engine.schemas import (
    ContractType,
    CustomerImpactTier,
    DelayTriple,
    Leg,
    Mode,
    Shipment,
)
from engine.score import impact, matrix
from engine.simulate.draws import ShipmentDraws, propagate

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


def _shipment(
    *,
    commitment_slack_hours: float = 48.0,
    buffer_hours: float = 24.0,
    penalty: float = 1000.0,
    value: float = 50_000.0,
) -> Shipment:
    depart = AS_OF.as_of + timedelta(hours=24)
    arrive = depart + timedelta(hours=72)
    leg = Leg(
        from_node="CHBSL",
        to_node="NLRTM",
        mode=Mode.BARGE,
        planned_depart=depart,
        planned_arrive=arrive,
        buffer_hours=buffer_hours,
        carrier="CARR_RHN",
    )
    return Shipment(
        shipment_id="TEST-0001",
        lane_id="LANE_TEST",
        origin_node="CHBSL",
        destination_node="NLRTM",
        mode="barge",
        legs=[leg],
        carrier="CARR_RHN",
        contract_type=ContractType.AGREEMENT,
        etd=depart,
        eta=arrive,
        otif_committed_date=arrive + timedelta(hours=commitment_slack_hours),
        value_chf=value,
        product_family="sealants",
        customer="Test Customer",
        customer_impact_tier=CustomerImpactTier.STOCK_OUT,
        sla_penalty_per_day=penalty,
        dangerous_goods=False,
        temperature_controlled=False,
    )


# =====================================================================
# Absence never becomes a value (BRIEF §8.1)
# =====================================================================


def test_unsourceable_probability_gets_its_own_band_not_a_midpoint(config):
    """The bug this whole discipline exists to prevent."""
    band_id, label = matrix.probability_band(None, config)
    assert band_id == matrix.UNSOURCED_BAND_ID
    assert "unsourced" in label.lower()

    # And critically: it must NOT land where 0.5 would.
    midpoint_band, _ = matrix.probability_band(0.5, config)
    assert band_id != midpoint_band


def test_every_real_probability_lands_on_the_axis(config):
    for p in (0.0, 0.1, 0.25, 0.49, 0.5, 0.75, 0.99, 1.0):
        band_id, _ = matrix.probability_band(p, config)
        assert band_id != matrix.UNSOURCED_BAND_ID


def test_unconfigured_action_time_is_not_zero(config):
    """An unknown minimum must never read as 'available forever'."""
    assert config.min_action_hours("a_made_up_action") is None
    assert config.min_action_hours("sea_reroute") == 72


def test_node_without_alternatives_returns_empty_not_a_silent_zero(config):
    from engine.network.graph import Network

    network = Network(config)
    assert network.alternatives_for("AEJEA") == []
    assert [n.id for n in network.alternatives_for("NLRTM")] == ["BEANR", "DEHAM"]


# =====================================================================
# Lateness is not delay  (correction to BRIEF §5.4)
# =====================================================================


def test_delay_inside_commitment_slack_costs_nothing(config):
    """Delay absorbed by the commitment slack must not incur penalty.

    The brief's formula is penalty_per_day × E[delay], where delay is measured
    against the planned ETA. Penalties accrue against the date promised to the
    customer, and those differ. Charging across the gap invents money.
    """
    shipment = _shipment(commitment_slack_hours=48.0, buffer_hours=0.0)

    # One day of post-buffer delay, well inside two days of commitment slack.
    delay = np.full(1000, 1.0)
    slack_days = shipment.commitment_slack_hours / 24.0
    lateness = np.maximum(0.0, delay - slack_days)

    draws = ShipmentDraws(
        shipment_id=shipment.shipment_id,
        total_delay_days=delay,
        lateness_days=lateness,
        driving_event_ids=[],
    )
    summary = impact.summarise(shipment, draws, config)

    assert summary["expected_delay_days"] == pytest.approx(1.0)
    assert summary["expected_lateness_days"] == 0.0
    assert summary["p_late"] == 0.0
    assert summary["expected_loss_chf"] == 0.0


def test_lateness_is_measured_past_the_committed_date(config):
    shipment = _shipment(commitment_slack_hours=48.0, buffer_hours=0.0, penalty=1000.0)
    delay = np.full(1000, 5.0)  # 5 days delay, 2 days slack -> 3 days late
    lateness = np.maximum(0.0, delay - 2.0)

    draws = ShipmentDraws(
        shipment_id=shipment.shipment_id,
        total_delay_days=delay,
        lateness_days=lateness,
        driving_event_ids=[],
    )
    summary = impact.summarise(shipment, draws, config)
    assert summary["expected_lateness_days"] == pytest.approx(3.0)
    # 3 days x CHF 1000 penalty, plus the once-off expediting and customer terms.
    assert summary["expected_loss_chf"] > 3000.0


def test_max_zero_is_applied_per_draw_not_to_the_mean(config):
    """max(0, ·) is convex, so E[max(0,X)] >= max(0, E[X]).

    A spread straddling the commitment date must produce a non-zero expected
    lateness even though the MEAN delay is exactly on time. Applying the floor
    after averaging would report zero and hide the whole downside.
    """
    shipment = _shipment(commitment_slack_hours=48.0, buffer_hours=0.0)
    rng = np.random.default_rng(7)

    # Mean delay exactly 2.0 days == the slack. Half the draws are late.
    delay = rng.normal(2.0, 1.5, size=20_000)
    lateness = np.maximum(0.0, delay - 2.0)

    assert np.mean(delay) == pytest.approx(2.0, abs=0.05)
    assert max(0.0, float(np.mean(delay)) - 2.0) == pytest.approx(0.0, abs=0.05)
    # The correct answer is materially above zero.
    assert float(np.mean(lateness)) > 0.4


def test_buffer_absorbs_delay_before_it_propagates():
    shipment = _shipment(buffer_hours=48.0, commitment_slack_hours=0.0)

    class _Draws:
        n_draws = 100
        index = {"E1": 0}

        def column(self, _event_id):
            return np.full(100, 1.5)  # 1.5 days, under the 2-day buffer

    from engine.schemas import GateHit

    hit = GateHit(
        event_id="E1",
        shipment_id=shipment.shipment_id,
        leg_index=0,
        node_id="NLRTM",
        mode=Mode.BARGE,
        spatial_reason="test",
        temporal_reason="test",
        modal_reason="test",
        leg_enters_at=shipment.legs[0].planned_depart,
        leg_leaves_at=shipment.legs[0].planned_arrive,
    )
    out = propagate(shipment, [hit], _Draws())
    assert out.expected_delay == 0.0
    assert out.p_late == 0.0


# =====================================================================
# Portfolio correlation (correction to BRIEF §5.3)
# =====================================================================


def test_shared_event_draws_preserve_the_tail():
    """Correlated exposure must have a fatter tail than independent exposure.

    This is the whole reason event durations are drawn once per iteration and
    shared. One Antwerp strike hits forty shipments together; forty independent
    strikes average out and understate the tail the convene decision turns on.
    """
    rng = np.random.default_rng(11)
    n_draws, n_shipments = 20_000, 40

    shared = rng.triangular(1.0, 3.0, 8.0, size=n_draws)
    correlated_total = np.sum(
        np.column_stack([shared * 1000.0] * n_shipments), axis=1
    )

    independent_total = np.sum(
        np.column_stack(
            [rng.triangular(1.0, 3.0, 8.0, size=n_draws) * 1000.0
             for _ in range(n_shipments)]
        ),
        axis=1,
    )

    # Same central value...
    assert np.mean(correlated_total) == pytest.approx(
        np.mean(independent_total), rel=0.02
    )
    # ...and a dramatically different tail. That difference is the bug.
    assert np.percentile(correlated_total, 99) > np.percentile(
        independent_total, 99
    ) * 1.5


def test_portfolio_distribution_sums_within_draws():
    a = np.array([10.0, 0.0, 10.0, 0.0])
    b = np.array([10.0, 0.0, 10.0, 0.0])
    out = impact.portfolio_distribution([a, b])
    assert out["mean"] == pytest.approx(10.0)
    assert out["p90"] > 10.0  # the all-or-nothing tail survives


# =====================================================================
# Value of acting (correction to BRIEF §5.2 / §5.4)
# =====================================================================


def test_value_of_acting_is_net_of_its_cost():
    assert impact.value_of_acting(11_800.0, 0.0, 4_200.0) == pytest.approx(7_600.0)


def test_value_of_acting_goes_negative_when_the_cure_costs_more():
    """A negative value is a real answer — 'leave it alone' — not a failure."""
    assert impact.value_of_acting(500.0, 100.0, 4_200.0) < 0


def test_explain_reads_as_a_sentence_with_no_symbols():
    sentence = impact.explain(11_800.0, 0.0, 4_200.0, "Reroute via Antwerp", "Thursday 14:00")
    assert "CHF 4,200" in sentence
    assert "CHF 11,800" in sentence
    assert "CHF 7,600" in sentence
    assert "Thursday 14:00" in sentence
    for symbol in ("E[", "max(", "×", "Σ"):
        assert symbol not in sentence


# =====================================================================
# Clock discipline
# =====================================================================


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError, match="naive datetime"):
        ensure_utc(datetime(2026, 9, 18, 6, 0))


def test_engine_never_reads_the_wall_clock():
    """The wall clock is read in exactly one module, and it is clock.py.

    Walk the AST rather than grepping, so prose about ``datetime.now`` in a
    docstring does not count as a call site and, more importantly, a real call
    hidden inside a string cannot escape.
    """
    import ast
    from pathlib import Path

    engine = Path(__file__).resolve().parent.parent / "engine"
    offenders: list[str] = []

    for path in engine.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "now":
                owner = getattr(func.value, "id", None) or getattr(
                    func.value, "attr", None
                )
                if owner in ("datetime", "dt"):
                    offenders.append(f"{path.relative_to(engine)}:{node.lineno}")

    outside = [o for o in offenders if not o.startswith("clock.py:")]
    assert not outside, f"wall-clock reads outside clock.py: {outside}"
    assert len(offenders) == 1, f"more than one wall-clock call site: {offenders}"


def test_temporal_gate_rejects_a_window_that_closed_before_arrival():
    """The condition everyone forgets. Half the false positives die here."""
    base = datetime(2026, 9, 18, tzinfo=timezone.utc)
    strike = (base, base + timedelta(days=2))
    vessel = (base + timedelta(days=5), base + timedelta(days=6))
    assert not overlaps(*strike, *vessel)

    early = (base + timedelta(days=1), base + timedelta(days=3))
    assert overlaps(*strike, *early)


# =====================================================================
# Config discipline
# =====================================================================


def test_ledger_has_45_fully_specified_variables(config):
    assert len(config.variables) == 45
    for var in config.variables.values():
        assert var.description.strip(), f"{var.id} has no router-readable description"
        assert var.modes_affected, f"{var.id} affects no modes"
        assert var.exposure, f"{var.id} has no exposure rule"


def test_every_variable_resolves_a_delay_triple_at_every_severity(config):
    for var_id in config.variables:
        for severity in ("minor", "moderate", "severe"):
            triple = config.delay_triple(var_id, severity)
            assert isinstance(triple, DelayTriple)


def test_delay_triples_must_be_ordered():
    with pytest.raises(ValueError, match="ordered"):
        DelayTriple(optimistic=5.0, likely=2.0, pessimistic=8.0)


def test_parameter_count_matches_the_claim(config):
    """BRIEF §5.0 claims ~90 invented numbers. Hold the claim to the file.

    Family-level triples: 10 families x 3 severities x 3 points = 90.
    """
    defaults = config.raw("delay_model")["defaults"]
    count = sum(len(sev) * 3 for sev in defaults.values())
    assert count == 90, f"family-level parameter count drifted to {count}"
