"""Closing a case: it leaves the board and goes to the risk ledger's history.

Pinned here: a close is recorded with what the case was and how it ended; the
route then reads closed on the board; a route that gets WORSE is an open case
again, while the same condition re-observed under a new event id is not; a
board replayed before the close shows it open; a reopen takes the close back
but keeps it in the history; and the log is append-only and survives a torn
line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine.act import cases

AS_OF = datetime(2026, 9, 18, 6, tzinfo=UTC)
NOW = datetime(2026, 9, 28, 10, 30, tzinfo=UTC)


def _route(level="yellow", event_id="OBS-KAUB-LOW-20260918"):
    return {
        "route_id": "LANE_RHINE_01", "name": "Düdingen → Basel → Rotterdam",
        "site": {"id": "DUD", "name": "Düdingen"},
        "level": level, "level_label": {"yellow": "Alert", "red": "Critical", "blue": "Watch"}[level],
        "reason": "Rhine low water at Kaub",
        "events": [{"event_id": event_id, "title": "Rhine low water at Kaub"}],
        "exposure_chf": 121494.0, "shipments": 11, "shipments_at_risk": 5,
    }


@pytest.fixture
def log(tmp_path):
    return tmp_path / "cases.jsonl"


def _close(log, route=None, outcome="rerouted", as_of=AS_OF):
    return cases.close(route or _route(), outcome=outcome, note="  moved to rail  ",
                       actor="C. Roth", closed_at=NOW, as_of=as_of, log=log)


def test_a_close_records_what_the_case_was_and_how_it_ended(log):
    record = _close(log)
    assert record["route_id"] == "LANE_RHINE_01"
    assert record["level"] == "yellow"
    assert record["site"] == "Düdingen"
    assert record["outcome_label"] == "Rerouted"
    assert record["note"] == "moved to rail"
    assert record["closed_by"] == "C. Roth"
    assert record["events"] == [{"event_id": "OBS-KAUB-LOW-20260918", "title": "Rhine low water at Kaub"}]
    assert record["exposure_chf"] == 121494.0
    [only] = cases.history(log)
    assert only["case_id"] == record["case_id"]
    assert only["reopened"] is False


def test_the_route_reads_closed_on_the_board(log):
    record = _close(log)
    board = cases.annotate({"routes": [_route()], "as_of": "x"}, AS_OF, log)
    case = board["routes"][0]["case"]
    assert case["status"] == "closed"
    assert case["case_id"] == record["case_id"]
    assert board["case_outcomes"] == cases.OUTCOMES


def test_the_same_condition_re_observed_the_next_day_stays_closed(log):
    _close(log)
    tomorrow = _route(event_id="OBS-KAUB-LOW-20260919")
    assert cases.status_for(tomorrow, AS_OF + timedelta(days=1), log) is not None


def test_a_route_that_gets_worse_is_an_open_case_again(log):
    _close(log)
    assert cases.status_for(_route(level="red"), AS_OF + timedelta(days=1), log) is None
    # Calmer is still covered.
    assert cases.status_for(_route(level="blue"), AS_OF + timedelta(days=1), log) is not None


def test_a_board_replayed_before_the_close_shows_it_open(log):
    _close(log)
    assert cases.status_for(_route(), AS_OF - timedelta(hours=1), log) is None


def test_a_reopen_takes_the_close_back_and_keeps_it_in_the_history(log):
    record = _close(log)
    cases.reopen(record["case_id"], actor="M. Brunner", at=NOW + timedelta(hours=1), log=log)
    assert cases.status_for(_route(), AS_OF, log) is None
    [only] = cases.history(log)
    assert only["reopened"] is True
    assert only["reopened_by"] == "M. Brunner"
    with pytest.raises(cases.CaseError, match="already"):
        cases.reopen(record["case_id"], actor=None, at=NOW, log=log)
    # And it can be closed again, as a second entry in the history.
    _close(log, outcome="customer_told")
    assert sorted(c["outcome"] for c in cases.history(log)) == ["customer_told", "rerouted"]
    assert cases.status_for(_route(), AS_OF, log)["outcome"] == "customer_told"


def test_closing_twice_or_with_an_unknown_outcome_is_refused(log):
    with pytest.raises(cases.CaseError, match="outcome"):
        _close(log, outcome="teleported")
    _close(log)
    with pytest.raises(cases.CaseError, match="already closed"):
        _close(log)
    with pytest.raises(cases.CaseError, match="no closed case"):
        cases.reopen("nope", actor=None, at=NOW, log=log)


def test_the_log_is_append_only_and_survives_a_torn_line(log):
    first = _close(log)
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "close", "case_id": "torn", "route_id"')   # a crash mid-write
    lines_before = log.read_text(encoding="utf-8").splitlines()
    cases.reopen(first["case_id"], actor=None, at=NOW, log=log)
    after = log.read_text(encoding="utf-8").splitlines()
    assert after[: len(lines_before) - 1] == lines_before[:-1]
    assert [c["case_id"] for c in cases.history(log)] == [first["case_id"]]


def test_history_is_newest_first(log):
    a = _close(log)
    cases.reopen(a["case_id"], actor=None, at=NOW, log=log)
    b = cases.close(_route(), outcome="split", note=None, actor=None,
                    closed_at=NOW + timedelta(days=1), as_of=AS_OF, log=log)
    assert [c["case_id"] for c in cases.history(log)] == [b["case_id"], a["case_id"]]
    assert cases.history(log)[0]["closed_by"] == "planner"
