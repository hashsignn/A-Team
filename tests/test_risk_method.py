"""The risk method, rule by rule (docs/RISK_METHOD.md).

Every event is judged by what is uncertain about it. Four industry methods
carry the four questions:

    has it happened, might it, is it over?   ConText (Harkema et al. 2009)
    what kind of event is it?                 sudden / slow onset (UNDRR)
    will this shipment still be on time?      Time-to-Survive (Simchi-Levi 2014)
    is a warning worth acting on?             cost-loss ratio (Thompson 1952)
"""

from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.network.graph import Network
from engine.pipeline import RunOptions, _to_events, run
from engine.portfolio import convene
from engine.schemas import (
    ContractType,
    CustomerImpactTier,
    Event,
    GateHit,
    Leg,
    Mode,
    Provenance,
    Severity,
    Shipment,
)
from engine.score import survive
from engine.simulate.draws import DrawMatrix, propagate
from engine.variables import modality, onset, rules

AS_OF = datetime(2026, 9, 26, 23, tzinfo=UTC)


@pytest.fixture(scope="module")
def config():
    return load_config()


def _read(text, config):
    return modality.read(text, rules.route(text, config.variables).active_variables)


# =====================================================================
# Has it happened, might it, or is it over?
# =====================================================================
@pytest.mark.parametrize(("text", "status", "cue"), [
    ("Iran threatens to close the Strait of Hormuz", "hypothetical", "threatens"),
    ("Iran closes the Strait of Hormuz to commercial shipping", "asserted", ""),
    ("Iran announces closure of Strait of Hormuz to commercial shipping", "asserted", ""),
    ("Antwerp dockworkers vote to strike from Monday unless talks resume", "hypothetical", "unless"),
    ("The dockworker walkout at Antwerp was called off", "ended", "called off"),
    # The threat is the strike's; the strike is over.
    ("The threatened dockworker walkout at Antwerp was called off", "ended", "called off"),
    # The ENDING is hypothetical; the walkout stands.
    ("The dockworker walkout could be called off", "asserted", ""),
    ("Port congestion at Shanghai is expected to worsen", "hypothetical", "is expected"),
    ("The EU may impose new sanctions on Iran", "hypothetical", "may"),
    # "U.S." does not end the sentence, and a blockade reported is asserted.
    ("The United States Central Command says that the U.S. military has redirected 115 "
     "commercial vessels since the reinstatement of its blockade on July 14.", "asserted", ""),
])
def test_the_words_say_whether_it_is_so(config, text, status, cue):
    reading = _read(text, config)
    assert (reading.status, reading.cue) == (status, cue)


def test_may_the_month_is_not_may_the_verb(config):
    assert _read("Motorway closed from 3 May 2026 after a landslide", config).status == "asserted"


def test_a_present_tense_end_is_a_stated_end_not_a_reported_one(config):
    """ "the closure ends on Friday" says when it will be over, not that it
    is: only completed forms read as over."""
    assert not _read("Motorway closed near Basel; the closure ends on Friday", config).ended


# =====================================================================
# What kind of event is it?
# =====================================================================
def _v(config, vid):
    return config.variables[vid]


def test_a_ballot_is_a_warning_whatever_it_says(config):
    kind, why = onset.classify([_v(config, "LAB_UNION_BALLOT")], measured=False,
                               realized=True, probability=None, stated_window=False)
    assert kind == "warning" and "never a stoppage" in why


def test_a_threat_is_a_warning_and_a_weather_warning_a_forecast(config):
    threat = modality.Reading("hypothetical", "threatens")
    assert onset.classify([_v(config, "GEO_CONFLICT")], measured=False, realized=True,
                          probability=None, stated_window=False, reading=threat)[0] == "warning"
    gale = modality.Reading("hypothetical", "warning")
    assert onset.classify([_v(config, "CLI_HIGH_WIND")], measured=False, realized=False,
                          probability=0.72, stated_window=False, reading=gale)[0] == "building"


@pytest.mark.parametrize(("vid", "measured", "realized", "probability", "window", "kind"), [
    ("FOR_EARTHQUAKE", True, True, None, False, "sudden"),
    ("CLI_HIGH_WIND", True, False, 0.75, False, "building"),     # a forecast
    ("WAT_LOW_WATER", True, True, 1.0, False, "building"),        # the Rhine at Kaub
    ("INF_ROAD_CLOSURE", False, True, None, True, "scheduled"),   # an Autobahn window
    ("CAP_BLANK_SAILING", False, False, 0.95, False, "scheduled"),
    ("POR_CONGESTION", False, True, None, False, "building"),
    ("LAB_PORT_STRIKE", False, False, None, False, "warning"),    # no odds, not happened
    ("GEO_CONFLICT", False, True, None, False, "sudden"),
])
def test_each_kind(config, vid, measured, realized, probability, window, kind):
    assert onset.classify([_v(config, vid)], measured=measured, realized=realized,
                          probability=probability, stated_window=window)[0] == kind


def test_every_variable_says_how_it_arrives(config):
    assert {v.onset for v in config.variables.values()} == {"sudden", "slow", "scheduled", "precursor"}


# =====================================================================
# A warning behaves like one, and "over" leaves the board
# =====================================================================
def _news(item_id, text, published):
    return {
        "item_id": item_id, "headline": text, "body": text, "text": text,
        "source": "example.org", "source_tier": 2, "source_key": "wikipedia_events",
        "source_nature": "report", "source_modes": [], "declared_variables": [],
        "published_at": published, "starts_at": published, "ends_at": None,
        "lat": None, "lon": None, "node_hint": ["CHOKE_HORMUZ"], "url": None,
        "severity_hint": None, "probability": None,
        "probability_basis": "not stated by the source; this feed reports occurrences, not odds",
        "realized": True, "synthetic": False,
        "default_ends_at": published + timedelta(days=7),
    }


def test_a_threat_starts_where_the_closure_it_warns_of_would(config):
    """Priced as the closure, from the minute it was reported, a threat put
    every shipment through the strait on a six-hour clock."""
    published = AS_OF - timedelta(hours=6)
    threat = _news("T1", "Iran threatens to close the Strait of Hormuz", published)
    events, _, _, _ = _to_events([], [threat], config, Network(config), Clock(AS_OF),
                                 RunOptions(rescue_unmatched=False))
    [event] = events
    assert event.kind == "warning"
    assert not event.realized and event.probability is None
    assert not event.probability_known           # the matrix's "no odds" column
    lead = config.variables["GEO_CONFLICT"].typical_lead_time_hours
    assert event.starts_at == published + timedelta(hours=lead)
    assert "threatens" in event.kind_basis


def test_what_the_report_says_is_over_is_not_an_event(config):
    over = _news("O1", "The blockade of the Strait of Hormuz has been lifted", AS_OF)
    events, _, notes, _ = _to_events([], [over], config, Network(config), Clock(AS_OF),
                                     RunOptions(rescue_unmatched=False))
    assert events == []
    assert "over" in notes["O1"] and "lifted" in notes["O1"]


def test_something_that_happened_has_known_odds(config):
    """The motorway operator lists closures and states no probability,
    because none is left to state. Read as "odds unknown", every real event
    sat in the matrix's no-odds column."""
    blockade = _news("B1", "The US Navy blockade of the Strait of Hormuz continues", AS_OF)
    events, _, _, _ = _to_events([], [blockade], config, Network(config), Clock(AS_OF),
                                 RunOptions(rescue_unmatched=False))
    [event] = events
    assert event.realized and event.probability is None and event.probability_known


# =====================================================================
# Time-to-Survive
# =====================================================================
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _shipment(buffers=(12.0, 24.0, 36.0), slack_hours=48.0):
    legs, at = [], T0
    for i, buffer in enumerate(buffers):
        legs.append(Leg(from_node=f"N{i}", to_node=f"N{i + 1}", mode=Mode.ROAD,
                        planned_depart=at, planned_arrive=at + timedelta(hours=10),
                        buffer_hours=buffer, carrier="c"))
        at += timedelta(hours=10)
    eta = at
    return Shipment(
        shipment_id="S1", lane_id="L1", origin_node="N0", destination_node=f"N{len(buffers)}",
        mode="road", legs=legs, carrier="c", contract_type=ContractType.AGREEMENT,
        etd=T0, eta=eta, otif_committed_date=eta + timedelta(hours=slack_hours),
        value_chf=10_000, product_family="p", customer="C", customer_impact_tier=
        CustomerImpactTier.STOCK_OUT, sla_penalty_per_day=0.0, dangerous_goods=False,
        temperature_controlled=False,
    )


def _hit(leg_index, event_id="E1"):
    return GateHit(event_id=event_id, shipment_id="S1", leg_index=leg_index, node_id="N1",
                   mode=Mode.ROAD, spatial_reason="", temporal_reason="", modal_reason="",
                   leg_enters_at=T0, leg_leaves_at=T0 + timedelta(hours=10))


def test_time_to_survive_is_the_buffers_downstream_plus_the_slack():
    ship = _shipment()
    # Hit on the second leg: its buffer and the third's absorb, then the slack.
    assert survive.time_to_survive(ship, [_hit(1)], "E1") == pytest.approx((24 + 36 + 48) / 24, abs=1e-3)
    assert survive.time_to_survive(ship, [_hit(0)], "E1") == pytest.approx((12 + 24 + 36 + 48) / 24, abs=1e-3)


def test_the_break_even_is_where_the_simulation_turns_late():
    """Same walk as propagate(): a draw a hair under TTS is on time, a hair
    over is late — so the verdict and the matrix can never disagree."""
    ship, hits = _shipment(), [_hit(1)]
    tts = survive.time_to_survive(ship, hits, "E1")
    draws = DrawMatrix(event_ids=["E1"], delay_days=np.array([[tts - 0.01], [tts + 0.01]]),
                       occurs=np.ones((2, 1), dtype=bool), probability_known={"E1": True})
    out = propagate(ship, hits, draws)
    assert out.lateness_days[0] == 0.0 and out.lateness_days[1] > 0.0


def test_already_late_and_never_late():
    assert survive.time_to_survive(_shipment(slack_hours=-5), [_hit(1)], "E1") == 0.0
    assert survive.time_to_survive(_shipment(), [], "E1") is None


@pytest.mark.parametrize(("tts", "verdict"), [
    (0.5, "late_best"), (1.0, "late_likely"), (3.0, "late_worst"),
    (4.0, "on_time"), (None, "on_time"), (0.0, "late_already"),
])
def test_the_verdict_against_best_likely_worst(tts, verdict):
    # Exactly the TTS still arrives on the promised date: 4.0 against a
    # worst case of 4.0 is on time.
    assert survive.survival(tts, (1.0, 2.0, 4.0)) == verdict


# =====================================================================
# A scheduled closure is waited out, never longer
# =====================================================================
def _event(kind="scheduled", family="infrastructure", variable="INF_ROAD_CLOSURE",
           ends=T0 + timedelta(hours=36)):
    return Event(
        event_id="E1", title="t", node_ids=[], lat=None, lon=None, starts_at=T0, ends_at=ends,
        duration_confidence="stated", event_class=family, active_variables=[variable],
        severity=Severity.MODERATE, modes_affected=[Mode.ROAD], realized=True,
        probability=None, probability_basis="", kind=kind,
        provenance=Provenance(source="s", source_tier=1, verbatim_quote="t", inferred=False,
                              retrieved_at=T0),
    )


def test_a_closure_delays_at_most_until_its_stated_end():
    """A two-night A61 closure carried the family's worst case of five days
    and put a route on red: "notify the customer now"."""
    reached = T0 + timedelta(hours=12)
    assert survive.window_cap(_event(), reached) == pytest.approx(1.0)
    assert survive.capped((0.5, 2.0, 5.0), 1.0) == (0.5, 1.0, 1.0)


def test_a_strike_is_not_capped_it_leaves_a_backlog():
    strike = _event(family="labour", variable="LAB_PORT_STRIKE")
    assert survive.window_cap(strike, T0) is None
    assert survive.window_cap(_event(kind="sudden"), T0) is None
    assert survive.window_cap(_event(ends=None), T0) is None


def test_the_cap_reaches_the_simulation():
    ship, hits = _shipment(), [_hit(1)]
    draws = DrawMatrix(event_ids=["E1"], delay_days=np.array([[10.0]]),
                       occurs=np.ones((1, 1), dtype=bool), probability_known={"E1": True})
    assert propagate(ship, hits, draws).lateness_days[0] > 0
    assert propagate(ship, hits, draws, cap_by_event={"E1": 1.0}).lateness_days[0] == 0


# =====================================================================
# The cost-loss ratio
# =====================================================================
def test_act_on_a_warning_above_cost_over_loss():
    assert survive.break_even_probability(22_000, 2_000, 2_000) == pytest.approx(0.1)
    assert survive.break_even_probability(3_000, 2_000, 2_000) is None   # never pays
    assert survive.break_even_probability(1_000, 1_000, 0) is None       # avoids nothing


@pytest.mark.parametrize(("p", "words"), [
    (0.03, "almost no chance"), (0.12, "very unlikely"), (0.3, "unlikely"),
    (0.5, "roughly even chance"), (0.7, "likely"), (0.9, "very likely"), (0.99, "almost certain"),
])
def test_the_threshold_in_icd_203_words(p, words):
    assert survive.likelihood_words(p) == words


# =====================================================================
# End to end, on the scripted scenario
# =====================================================================
SCRIPTED = Clock(dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC))


@pytest.fixture(scope="module")
def context():
    return run(clock=SCRIPTED, config=load_config(), options=RunOptions(shipment_count=300, seed=7))


def test_every_board_event_says_how_it_is_judged(context):
    board = build_board(context)
    events = [e for r in board["routes"] for e in r["events"]]
    assert events
    for e in events:
        assert e["kind"] in onset.LABELS and e["kind_label"] and e["kind_basis"]
        assert set(e["survival"]) == set(survive.LABELS)
        assert sum(e["survival"].values()) == len(e["matrix"]["points"])
        for point in e["matrix"]["points"]:
            assert "time_to_survive_days" in point and "survival" in point


def test_the_room_sees_how_much_exposure_is_only_warnings(context, monkeypatch):
    """The Antwerp dockworkers voted to strike "unless talks resume": a
    warning. It counts toward the convene rule by default — convening early
    is the rule's purpose — but the room sees its share, and the team may
    decide it only means Watch."""
    kinds = {a.event.kind for a in context.result.assessments}
    assert "warning" in kinds
    verdict = convene.evaluate(context.result.assessments, context.config, context.clock)
    assert verdict.exposure_from_warnings_chf > 0
    assert verdict.exposure_from_warnings_chf <= verdict.exposure_chf

    rule = context.config.scoring["convene_rule"]
    monkeypatch.setitem(rule, "count_warnings", False)
    monkeypatch.setitem(rule["thresholds"], "exposure_chf",
                        verdict.exposure_chf - verdict.exposure_from_warnings_chf + 1)
    without = convene.evaluate(context.result.assessments, context.config, context.clock)
    assert not any("expected loss" in f for f in without.triggers_fired)
