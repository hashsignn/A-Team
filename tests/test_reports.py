"""Field reports: the driver / on-site channel.

Two properties matter more than anything else here.

**The as-of discipline survives a live feed.** This is where that usually
breaks — a feed arrives in wall-clock time and every past board becomes
irreproducible. It does not break, because a report carries the instant it
was OBSERVED and a board at as-of T sees only reports observed at or before
T. Without that the hindcast, which is the project's only honest metric,
would quietly stop working.

**A report can unlock a reroute.** It is the one source that satisfies the
playbook's confirming steps, so a malformed report coerced into a
valid-looking one is a reroute taken on a misunderstanding. Validation
refuses loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.act import flow as F
from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.ingest import reports as R
from engine.pipeline import RunOptions, run

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
AS_OF = Clock.at("2026-09-18T06:00:00+00:00")
LANE = "LANE_RHINE_01"


@pytest.fixture
def log(tmp_path):
    return tmp_path / "reports.jsonl"


def _payload(**over):
    base = {
        "shipment_id": "SYN-0060",
        "status": "held",
        "position": "Kaub, third in the queue",
        "load_state": "intact",
        "note": "Lock shut since 06:00",
        "confirms_disruption": False,
    }
    base.update(over)
    return base


def _file(log, received=NOW, **over):
    return R.append(R.validate(_payload(**over), received), log)


# =====================================================================
# THE AS-OF DISCIPLINE
# =====================================================================


def test_a_board_does_not_see_a_report_observed_after_its_as_of(log):
    """The property the whole hindcast rests on. Replaying yesterday must
    give yesterday's answer even though the log has grown since."""
    _file(log, observed_at="2026-09-18T05:30:00+00:00")
    assert len(R.as_of(datetime(2026, 9, 18, 6, 0, tzinfo=UTC), log)) == 1
    assert R.as_of(datetime(2026, 9, 18, 5, 0, tzinfo=UTC), log) == []


def test_observed_at_is_when_the_driver_looked_not_when_it_arrived(log):
    """A report queued in a tunnel and delivered forty minutes later must not
    claim to be a forty-minute-old observation — the planner's decision turns
    on when somebody actually looked."""
    late_arrival = NOW + timedelta(minutes=40)
    report = R.validate(
        _payload(observed_at=NOW.isoformat()), received_at=late_arrival
    )
    assert report.observed_at == NOW
    assert report.received_at == late_arrival
    assert report.observed_at < report.received_at


def test_a_future_observation_is_clamped_not_trusted(log):
    """A phone with a wrong clock is a clock problem, not a prophecy. An
    observation from the future would leak into boards that should not see
    it yet."""
    report = R.validate(
        _payload(observed_at="2027-01-01T00:00:00+00:00"), received_at=NOW
    )
    assert report.observed_at == NOW


def test_a_report_with_no_observed_at_is_stamped_on_arrival(log):
    report = R.validate(_payload(), received_at=NOW)
    assert report.observed_at == NOW


def test_latest_for_respects_the_as_of(log):
    _file(log, observed_at="2026-09-18T04:00:00+00:00", status="moving")
    _file(log, observed_at="2026-09-18T08:00:00+00:00", status="held")
    early = R.latest_for("SYN-0060", datetime(2026, 9, 18, 6, 0, tzinfo=UTC), log)
    late = R.latest_for("SYN-0060", datetime(2026, 9, 18, 10, 0, tzinfo=UTC), log)
    assert early.status == "moving"
    assert late.status == "held"


# =====================================================================
# APPEND-ONLY
# =====================================================================


def test_a_correction_is_a_new_report_not_an_edit(log):
    """A driver who said 'held' at 09:00 and 'moving' at 11:00 has told us
    the duration of the hold. A mutable row would erase it."""
    _file(log, observed_at="2026-09-18T09:00:00+00:00", status="held")
    _file(log, observed_at="2026-09-18T11:00:00+00:00", status="moving")
    rows = R.read_all(log)
    assert len(rows) == 2
    assert [r.status for r in rows] == ["held", "moving"]
    assert len({r.report_id for r in rows}) == 2


def test_a_damaged_trailing_line_does_not_destroy_the_history(log):
    """The log is append-only and can be truncated mid-write by a crash. One
    bad line must not make the whole record unreadable."""
    _file(log, observed_at="2026-09-18T05:00:00+00:00")
    with log.open("a") as handle:
        handle.write('{"report_id": "truncated", "shipm')
    assert len(R.read_all(log)) == 1


def test_an_empty_log_is_not_an_error(log):
    assert R.read_all(log) == []
    assert R.as_of(NOW, log) == []


# =====================================================================
# VALIDATION — this source can unlock a reroute
# =====================================================================


@pytest.mark.parametrize("bad", [
    {"shipment_id": ""},
    {"shipment_id": "../../etc/passwd"},
    {"shipment_id": "has spaces"},
    {"status": "vibing"},
    {"status": ""},
    {"load_state": "melted"},
    {"observed_at": "not a date"},
    {"revised_eta": "soonish"},
])
def test_a_malformed_report_is_refused_not_coerced(bad):
    """Coercing this into something valid-looking would mean a reroute taken
    on a misunderstanding."""
    with pytest.raises(R.ReportError):
        R.validate(_payload(**bad), NOW)


def test_free_text_is_bounded():
    """The endpoint is unauthenticated in the prototype. An unbounded note is
    a denial-of-service and a log nobody can open."""
    report = R.validate(_payload(note="x" * 5000, position="y" * 5000), NOW)
    assert len(report.note) <= R.MAX_NOTE
    assert len(report.position) <= R.MAX_TEXT


def test_a_report_is_tier_one_observed():
    """Higher than every feed in the system, including the unconnected ones:
    it is somebody looking at the freight rather than at a region."""
    assert R.SOURCE_TIER == 1
    assert R.validate(_payload(), NOW).as_dict()["source_tier"] == 1


def test_every_report_gets_its_own_id():
    ids = {R.validate(_payload(), NOW).report_id for _ in range(50)}
    assert len(ids) == 50


# =====================================================================
# THE LOOP: a report unlocks the planner's reroute
# =====================================================================


@pytest.fixture(scope="module")
def route():
    config = load_config()
    context = run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150))
    board = build_board(context)
    return config, next(r for r in board["routes"] if r["route_id"] == LANE)


def test_a_confirming_report_opens_the_gate(route):
    """The single most valuable connection in the system. The planner's
    checklist says 'confirm with the carrier'; a driver with the freight in
    front of them answers it from their phone."""
    config, r = route
    tasks = F.build(config, r)

    assert not F.evaluate(tasks, set()).gate_open

    report = R.validate(_payload(
        confirms_disruption=True,
        revised_eta="2026-09-22T14:00:00+00:00",
    ), NOW)
    state = F.evaluate(tasks, set(), [report])
    assert state.gate_open
    assert set(state.from_reports) >= {
        "confirm.carrier", "confirm.position", "confirm.eta"
    }
    assert "field report" in state.gate_reason


def test_a_driver_saying_moving_does_not_confirm_a_disruption(route):
    """Evidence AGAINST one. Treating it as confirmation would unlock a
    reroute on good news."""
    config, r = route
    tasks = F.build(config, r)
    report = R.validate(_payload(status="moving", confirms_disruption=False), NOW)
    state = F.evaluate(tasks, set(), [report])
    assert "confirm.carrier" not in state.from_reports
    assert not state.gate_open


def test_a_report_without_an_eta_does_not_satisfy_the_eta_step(route):
    config, r = route
    tasks = F.build(config, r)
    report = R.validate(_payload(confirms_disruption=True), NOW)
    state = F.evaluate(tasks, set(), [report])
    assert "confirm.eta" not in state.from_reports


def test_the_ui_can_tell_who_ticked_what(route):
    """A planner seeing a box already checked needs to know a driver checked
    it, not wonder whether they did."""
    config, r = route
    tasks = F.build(config, r)
    report = R.validate(_payload(
        confirms_disruption=True, revised_eta="2026-09-22T14:00:00+00:00"
    ), NOW)
    state = F.evaluate(tasks, {"detect.read"}, [report])
    assert state.from_reports["confirm.carrier"] == report.report_id
    assert "detect.read" not in state.from_reports


def test_reports_cannot_confirm_a_tier3_only_event(route):
    """Corroboration still applies. A field report is strong evidence, but
    the blocked task is blocked on the EVENT being corroborated, which is a
    different question from whether somebody looked at the freight."""
    config, r = route
    tasks = F.build(config, r, source_tiers=[3])
    report = R.validate(_payload(
        confirms_disruption=True, revised_eta="2026-09-22T14:00:00+00:00"
    ), NOW)
    assert not F.evaluate(tasks, set(), [report]).gate_open


def test_summarise_counts_what_the_field_has_said(log):
    _file(log, observed_at="2026-09-18T05:00:00+00:00", confirms_disruption=True)
    _file(log, observed_at="2026-09-18T05:10:00+00:00",
          shipment_id="SYN-0011", load_state="damaged")
    out = R.summarise(datetime(2026, 9, 18, 6, 0, tzinfo=UTC), log)
    assert out["reports"] == 2
    assert out["shipments_reporting"] == 2
    assert out["confirmations"] == 1
    assert out["damaged"] == 1
