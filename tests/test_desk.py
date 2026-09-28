"""The client review, September 2026: sites, customers, the all-hands, push-outs.

Each test holds one thing the Sika team said to the code that answers it
(config.example/desk.yaml quotes them).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import desk
from engine.clock import Clock
from engine.config import load_config
from engine.fast import capacity
from engine.ingest import pushouts
from engine.schemas import ConveneVerdict, Posture


@pytest.fixture(scope="module")
def config():
    return load_config()


# ---------------------------------------------------------------------
# Sites — "planners allocate work by origin site rather than by route"
# ---------------------------------------------------------------------
def test_every_route_belongs_to_exactly_one_site(config):
    ids = {s["id"] for s in desk.sites(config)}
    for lane in config.lanes:
        assert desk.site_of_lane(lane, config)["id"] in ids, lane["id"]


def test_a_route_belongs_to_the_site_it_ships_from(config):
    lanes = {lane["id"]: lane for lane in config.lanes}
    assert desk.site_of_lane(lanes["LANE_ASIA_08"], config)["name"] == "Düdingen"
    assert desk.site_of_lane(lanes["LANE_US_04"], config)["name"] == "Stuttgart"
    assert desk.site_of_lane(lanes["LANE_ASIA_01"], config)["name"] == "Zürich"


def test_a_lane_can_name_its_site(config):
    lane = dict(next(lane for lane in config.lanes if lane["id"] == "LANE_ASIA_08"), site="STU")
    assert desk.site_of_lane(lane, config)["id"] == "STU"


def test_a_lane_starting_nowhere_listed_falls_under_its_own_first_node(config):
    lane = {"id": "X", "legs": [{"from": "NLRTM", "to": "BEANR"}]}
    site = desk.site_of_lane(lane, config)
    assert site["id"] == "NLRTM" and site["name"] == "Rotterdam"


# ---------------------------------------------------------------------
# Customers — importance is who is served FIRST, not what a delay costs
# ---------------------------------------------------------------------
def test_customer_importance_comes_from_the_desk_file(config):
    assert desk.priority_of("Bauwerk Construction AG", config) == "A"
    assert desk.priority_of("Continental Roofing", config) == "C"
    assert desk.priority_of("bauwerk construction ag", config) == "A"   # a typed name matches
    assert desk.priority_of("Somebody Nobody Listed", config) == "B"    # the default


def test_key_accounts_go_first_onto_scarce_capacity_and_are_never_deferred():
    def d(sid, slack, priority):
        return capacity.Displaced(sid, "c", 10.0, "dry", slack, "barge", 1000.0, "mortars", priority)

    items = [d("B-tight", 2, "B"), d("A-slack", 200, "A"), d("C-tight", 1, "C")]
    assert [x.shipment_id for x in capacity._by_deadline(items)][0] == "A-slack"
    held = {x.shipment_id for x in capacity._triage_set(
        [d("A-slack", 400, "A"), d("B-slack", 400, "B")], cap_t=100)}
    assert "A-slack" in held and "B-slack" not in held


# ---------------------------------------------------------------------
# The all-hands — "biweekly to daily" once a crisis looms
# ---------------------------------------------------------------------
def _verdict(posture: Posture) -> ConveneVerdict:
    return ConveneVerdict(posture=posture, rule_agreed=False, triggers_fired=["x"], exposure_chf=0,
                          contracts_exposed=0, shipments_needing_decision=0, next_meeting_at=None,
                          headline="h")


def test_the_meeting_goes_from_biweekly_to_daily(config):
    clock = Clock(datetime(2026, 9, 26, 23, 0, tzinfo=UTC))   # a Saturday night
    calm = desk.meeting(_verdict(Posture.NORMAL), config, clock)
    crisis = desk.meeting(_verdict(Posture.CONVENE), config, clock)
    assert calm["cadence_label"] == "every two weeks" and not calm["changed"]
    assert crisis["cadence_label"] == "daily" and crisis["changed"]
    # Daily means working days: Monday morning, not Sunday.
    assert crisis["next_at"].startswith("2026-09-28T08:30")
    # The biweekly slot counts forward from the anchor (Tue 15 Sep).
    assert calm["next_at"].startswith("2026-09-29T09:00")
    assert {a["function"] for a in crisis["attendees"]} == {
        "Supply Chain", "Procurement", "Manufacturing", "Controlling"}
    # The rule's tests come as rows, so the room reads a table.
    assert [c["label"] for c in crisis["checks"]] == [
        "Expected loss", "Customers exposed", "Decisions due in 48 h"]


def test_watch_meets_sooner_but_not_daily(config):
    clock = Clock(datetime(2026, 9, 16, 12, 0, tzinfo=UTC))
    watch = desk.meeting(_verdict(Posture.WATCH), config, clock)
    assert watch["cadence_label"] == "weekly" and watch["next_at"].startswith("2026-09-22")


def test_controlling_raises_the_limit_only_in_a_crisis(config):
    normal, raised = desk.crisis_limit(config)
    assert normal == 10000 and raised == 50000
    routes = [{"route_id": "R", "name": "r", "level": "red", "actions": [
        {"label": "Reroute", "shipment_id": "S1", "customer": "c", "cost_chf": 30000.0,
         "lead_time_hours": 5, "customer_priority": "A"},
        {"label": "Charter", "shipment_id": "S2", "customer": "c", "cost_chf": 80000.0,
         "lead_time_hours": 5, "customer_priority": "B"}]}]
    crisis = desk._controlling(routes, config, "convene")
    assert crisis["active"] and len(crisis["items"]) == 1
    assert "10,000 → 50,000" in crisis["summary"] and "Frees 1 of 2" in crisis["note"]
    assert not desk._controlling(routes, config, "normal")["active"]


# ---------------------------------------------------------------------
# Push-outs — "carriers pushing out single orders before an official crisis"
# ---------------------------------------------------------------------
NOW = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)


def _notice(order, carrier="CARR_DSL", node="NLRTM", hours=48, noticed=1):
    planned = NOW + timedelta(days=3)
    return pushouts.Notice(order, carrier, node, NOW - timedelta(days=noticed),
                           planned, planned + timedelta(hours=hours))


def test_one_carrier_moving_several_orders_at_one_place_is_a_pattern(config):
    notices = [_notice("S1"), _notice("S2"), _notice("S3"), _notice("S9", node="BEANR")]
    patterns, singles = pushouts.detect(notices, config, Clock(NOW))
    assert [(p["carrier"], p["node"], p["orders"]) for p in patterns] == [("CARR_DSL", "NLRTM", 3)]
    assert [s["node"] for s in singles] == ["BEANR"]


def test_noise_does_not_make_a_pattern(config):
    notices = [_notice("S1"), _notice("S1"),                  # the same order twice
               _notice("S2", hours=4),                        # a few hours is schedule noise
               _notice("S3", noticed=20)]                     # outside the window
    patterns, _ = pushouts.detect(notices, config, Clock(NOW))
    assert patterns == []


def test_a_pattern_is_a_warning_with_no_invented_odds(config):
    patterns, _ = pushouts.detect([_notice(f"S{i}") for i in range(3)], config, Clock(NOW))
    obs = pushouts.observation(patterns[0], config, Clock(NOW))
    assert obs["variable_id"] == "CAP_CARRIER_PUSHOUT"
    assert obs["realized"] is False and obs["probability"] is None
    assert obs["modes"] == ["sea"]                  # a sea carrier: the trains are not implicated
    assert "no official notice" in obs["title"]


def test_a_carrier_export_replaces_the_synthetic_changes(config, tmp_path):
    (tmp_path / "carrier_notices.csv").write_text(
        "order_id,carrier,node,noticed_at,planned_departure,new_departure,reason\n"
        + "".join(f"O{i},CARR_RHN,CHBSL,2026-09-25T10:00:00+00:00,2026-09-28T08:00:00+00:00,"
                  f"2026-09-30T08:00:00+00:00,rolled\n" for i in range(4))
        + "broken row\n", encoding="utf-8")
    obs, report, summary = pushouts.assess([], config, Clock(NOW), customer_dir=tmp_path)
    assert report.status.value == "connected" and not summary["synthetic"]
    assert len(obs) == 1 and obs[0]["node_ids"] == ["CHBSL"]
