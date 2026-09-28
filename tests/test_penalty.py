"""Delay penalties by customer type (Sika, 28 Sep 2026: "there are delay
penalties, and they differ by customer").

The switch decides whether they are charged at all; the table decides the
shape. A car maker recharges premium freight and a share of a stopped line;
a DIY retailer fines a share of the order once late; a construction project
passes down delay damages per week, capped; a distributor charges nothing.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from engine.config import load_config
from engine.schemas import ContractType, CustomerImpactTier, Leg, Mode, Shipment
from engine.score import penalty as P


@pytest.fixture(scope="module")
def config():
    return load_config()


def _switched(config, on: bool):
    out = copy.deepcopy(config)
    out.files["scoring"].data = copy.deepcopy(config.scoring)
    out.files["scoring"].data["cost"]["components"]["contractual_penalty"]["enabled"] = on
    return out


def _shipment(customer: str, value: float = 100_000.0, per_day: float = 0.0) -> Shipment:
    t0 = datetime(2026, 9, 28, tzinfo=UTC)
    leg = Leg(from_node="A", to_node="B", mode=Mode.SEA, planned_depart=t0,
              planned_arrive=t0 + timedelta(days=20), buffer_hours=24, carrier="C")
    return Shipment(
        shipment_id="SYN-9999", lane_id="L", origin_node="A", destination_node="B",
        mode="sea", legs=[leg], carrier="C", contract_type=ContractType.AGREEMENT,
        etd=t0, eta=t0 + timedelta(days=20), otif_committed_date=t0 + timedelta(days=22),
        value_chf=value, product_family="adhesives", customer=customer,
        customer_impact_tier=CustomerImpactTier.STOCK_OUT, sla_penalty_per_day=per_day,
        dangerous_goods=False, temperature_controlled=False,
    )


LATE = np.array([0.0, 1.0, 7.0, 30.0, 200.0])


def test_switched_off_nothing_is_charged(config):
    off = _switched(config, False)
    assert not P.per_draw(_shipment("Kestrel Auto Assembly"), LATE, off).any()


def test_a_car_maker_charges_once_then_by_day_up_to_a_cap(config):
    got = P.per_draw(_shipment("Kestrel Auto Assembly"), LATE, _switched(config, True))
    assert got[0] == 0.0                     # on time: nothing
    assert got[1] == 5_000 + 25_000          # premium freight + one day
    assert got[-1] == 250_000                # capped per incident


def test_a_diy_retailer_fines_a_share_of_the_order_once(config):
    got = P.per_draw(_shipment("Oakfield DIY Stores", value=40_000), LATE, _switched(config, True))
    assert got[0] == 0.0
    assert list(got[1:]) == pytest.approx([1_200.0] * 4)   # 3% of CHF 40,000, however late


def test_a_construction_project_charges_by_the_week_capped(config):
    got = P.per_draw(_shipment("Bauwerk Construction AG"), LATE, _switched(config, True))
    assert got[2] == pytest.approx(500.0)             # one week: 0.5% of CHF 100,000
    assert got[-1] == pytest.approx(5_000.0)          # never above 5%


def test_a_distributor_charges_nothing(config):
    assert not P.per_draw(_shipment("Continental Roofing"), LATE, _switched(config, True)).any()


def test_a_customer_without_a_type_falls_back_to_its_own_record(config):
    got = P.per_draw(_shipment("Unknown Customer Ltd", per_day=300.0), LATE, _switched(config, True))
    assert list(got) == [0.0, 300.0, 2_100.0, 9_000.0, 60_000.0]


def test_every_synthetic_customer_has_a_type(config):
    from engine.ingest.synthetic import CUSTOMERS

    for name, _tier in CUSTOMERS:
        assert P.clause(name, config) is not None, name


def test_the_clause_reads_in_one_line(config):
    assert P.summary(P.clause("Kestrel Auto Assembly", config)) == (
        "CHF 5,000 once late, CHF 25,000 a day, capped at CHF 250,000")
    assert P.summary(P.clause("Continental Roofing", config)) == "no delay penalty"


def test_penalties_raise_the_exposure_and_the_board_says_which_way(config):
    from engine.clock import Clock
    from engine.export.board import build_board
    from engine.pipeline import RunOptions, run

    clock = Clock.at("2026-09-18T06:00:00+00:00")
    boards = {on: build_board(run(clock=clock, config=_switched(config, on),
                                  options=RunOptions(shipment_count=120, seed=7)))
              for on in (False, True)}
    total = {on: sum(r["exposure_chf"] for r in b["routes"]) for on, b in boards.items()}
    assert total[True] > total[False]
    assert boards[True]["penalties"]["enabled"] and not boards[False]["penalties"]["enabled"]
    route = next(r for r in boards[True]["routes"] if r["clauses"])
    assert all({"customer", "label", "summary", "at_risk", "counted"} <= set(c) for c in route["clauses"])
