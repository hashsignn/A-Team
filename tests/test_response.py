"""Tests for the response workspace: contacts, escalation, report.

This covers the third failure the client named — "it is unclear what to do and
who to involve" — so the tests are mostly about FILTERING. A contact list that
returns everyone is the same as no contact list.
"""

from __future__ import annotations

import pytest

from engine.act import contacts as C
from engine.clock import Clock
from engine.config import load_config
from engine.export import report as R
from engine.export.board import build_board
from engine.pipeline import RunOptions, run
from engine.score.severity import Level

AS_OF = Clock.at("2026-09-19T12:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def context(config):
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


def _lane(config, lane_id: str) -> dict:
    return next(x for x in config.lanes if x["id"] == lane_id)


# =====================================================================
# "We don't know it" — recoverable must not surface as a figure
# =====================================================================


def test_recoverable_is_not_presented_anywhere_in_the_payload(board):
    """It is the least defensible number in the system: it rests on our
    invented action costs and residual fractions."""
    import json

    blob = json.dumps(board)
    assert '"recoverable_chf"' not in blob
    assert "cost_of_waiting_chf" not in blob
    # the optionality framing went with it
    assert "options_expiring" not in blob


def test_route_reason_does_not_quote_a_recoverable_figure(board):
    for route in board["routes"]:
        assert "of mitigation is still open" not in route["reason"]


def test_convene_triggers_rest_on_checkable_quantities(board):
    """Expected loss and two counts. A crisis rule whose trigger is a number
    nobody can stand behind is a broken rule, not a conservative one."""
    posture = board["posture"]
    assert "exposure_chf" in posture
    assert "shipments_needing_decision" in posture
    assert "recoverable_chf" not in posture
    for trigger in posture["triggers_fired"]:
        assert "recoverable" not in trigger.lower()


def test_summary_never_quotes_recoverable(board):
    for route in board["routes"]:
        text = R.build_summary(route, board["as_of_label"], board["posture"])
        assert "recoverable" not in text.lower()


# =====================================================================
# Contacts are filtered BY ROUTE — that is the whole point
# =====================================================================


def test_vendors_are_only_those_on_this_route(config, context):
    """A planner looking at a Rhine barge problem should not be handed the
    Singapore agency."""
    rhine = C.alternate_vendors(_lane(config, "LANE_RHINE_01"), config)
    nodes_on_rhine = {v.meta["node"] for v in rhine}
    assert nodes_on_rhine <= {"SIKA_DUD", "CHBSL", "GAUGE_KAUB", "NLRTM"}

    asia = C.alternate_vendors(_lane(config, "LANE_ASIA_04"), config)
    assert "CHBSL" not in {v.meta["node"] for v in asia}


def test_carriers_match_the_modes_this_route_runs(config):
    barge = C.alternate_carriers(_lane(config, "LANE_RHINE_01"), config)
    assert any("barge" in c.role for c in barge)
    assert not any("sea carrier" == c.role for c in barge)

    road_only = C.alternate_carriers(_lane(config, "LANE_EU_01"), config)
    assert {c.role for c in road_only} == {"road carrier"}


def test_alternate_routing_comes_from_the_route_s_own_nodes(config, context):
    routing = C.alternate_routing(_lane(config, "LANE_RHINE_01"), context.network)
    named = {r["node"] for r in routing}
    assert named <= {"SIKA_DUD", "CHBSL", "GAUGE_KAUB", "NLRTM"}
    rotterdam = next(r for r in routing if r["node"] == "NLRTM")
    assert {a["id"] for a in rotterdam["alternatives"]} == {"BEANR", "DEHAM"}


def test_no_alternative_configured_is_stated_not_silently_empty(config, context):
    """An empty list means 'none configured', which the UI renders as exactly
    that — not as a finding that no alternative exists in the world."""
    lane = {"legs": [{"from": "AEJEA", "to": "AEJEA", "mode": "sea"}]}
    assert C.alternate_routing(lane, context.network) == []


# =====================================================================
# The ladder drives who is drawn in
# =====================================================================


def test_more_urgent_levels_convene_more_teams(config):
    counts = {
        level: len(C.standing_teams(level, config))
        for level in (Level.GREEN, Level.WHITE, Level.BLUE, Level.YELLOW, Level.RED)
    }
    assert counts[Level.GREEN] == 0
    assert counts[Level.WHITE] < counts[Level.BLUE] < counts[Level.YELLOW] < counts[Level.RED]


def test_seniors_are_drawn_in_by_level_not_by_money(config):
    assert C.seniors_for(Level.GREEN, config) == []
    assert C.seniors_for(Level.WHITE, config) == []
    yellow = {s.role for s in C.seniors_for(Level.YELLOW, config)}
    red = {s.role for s in C.seniors_for(Level.RED, config)}
    assert yellow, "no senior is drawn in at Alert"
    # Red includes everyone Yellow does, plus at least one more.
    assert yellow < red


def test_route_manager_is_resolved_per_corridor(config):
    rhine = C.route_manager(_lane(config, "LANE_RHINE_01"), config)
    asia = C.route_manager(_lane(config, "LANE_ASIA_04"), config)
    assert rhine.name != asia.name
    assert "Rhine" in rhine.role


def test_missing_owner_is_reported_not_invented(config):
    import copy

    stripped = copy.deepcopy(config)
    stripped.files["contacts"].data = dict(config.contacts)
    stripped.files["contacts"].data["route_owners"] = {}
    verdict = C.route_manager({"focus": "moon", "legs": []}, stripped)
    assert "No route owner configured" in verdict.name


def test_every_contact_carries_the_reason_it_is_listed(config):
    lane = _lane(config, "LANE_RHINE_01")
    groups = (
        [C.route_manager(lane, config)]
        + C.standing_teams(Level.RED, config)
        + C.seniors_for(Level.RED, config)
        + C.alternate_vendors(lane, config)
        + C.alternate_carriers(lane, config)
    )
    for contact in groups:
        assert contact.why.strip(), f"{contact.name} has no stated reason"
        assert contact.group


# =====================================================================
# Approval and escalation
# =====================================================================


def test_spend_below_the_delegated_limit_needs_no_approval(config):
    assert C.approval_needed(100.0, config) is None


def test_spend_above_the_limit_names_the_approver(config):
    out = C.approval_needed(999_999.0, config)
    assert out is not None
    assert out["approver"]
    assert "above the delegated limit" in out["note"]


def test_escalation_step_rises_with_exposure(config):
    low = C.escalation_step(Level.WHITE, 0.0, config)
    high = C.escalation_step(Level.RED, 500_000.0, config)
    assert high["level"] > low["level"]
    assert len(high["notify"]) >= len(low["notify"])


# =====================================================================
# Report
# =====================================================================


def test_every_route_renders_a_valid_pdf(board):
    for route in board["routes"]:
        data = R.build_pdf(route, board["as_of_label"], board["posture"])
        assert data[:4] == b"%PDF", route["route_id"]
        assert len(data) > 1200


def test_latin_folds_typography_the_core_fonts_cannot_encode():
    """fpdf2's built-in fonts are ISO-8859-1, which is narrower than cp1252:
    it has the umlauts but none of the typographic punctuation."""
    tricky = "Düdingen → Basel — “no”… trend –2.5"
    out = R.latin(tricky)
    out.encode("latin-1")  # must not raise
    assert "->" in out and '"no"' in out and "..." in out
    assert "ü" in out, "umlauts are encodable and must survive"


def test_latin_replaces_an_unknown_glyph_rather_than_raising():
    out = R.latin("emoji \U0001f600 here")
    out.encode("latin-1")
    assert "here" in out


def test_summary_leads_with_the_level_and_the_deadline(board):
    route = board["routes"][0]
    text = R.build_summary(route, board["as_of_label"], board["posture"])
    assert text.startswith(f"[{route['level_label'].upper()}]")
    assert "Action by" in text
    assert "WHO" in text


def test_summary_names_who_to_involve(board):
    route = next(r for r in board["routes"] if r["level"] in ("red", "yellow"))
    text = R.build_summary(route, board["as_of_label"], board["posture"])
    manager = route["response"]["route_manager"]["name"]
    assert manager in text


def test_report_filename_is_safe_and_dated(board):
    name = R.filename(board["routes"][0], "2026-09-19T12:00:00+00:00")
    assert name.endswith(".pdf")
    assert "2026-09-19" in name
    assert "/" not in name and " " not in name
