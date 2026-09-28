"""The working clock: weekends and public holidays do not count toward the
time left to act (Sika, 28 Sep 2026: "3 days, 5 days, excluding holidays and
weekends")."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.score import leadtime
from engine.score import workcal as W


@pytest.fixture(scope="module")
def config():
    return load_config()


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_easter_is_computed_not_listed():
    assert W.easter(2026) == date(2026, 4, 5)
    assert W.easter(2027) == date(2027, 3, 28)
    assert W.easter(2030) == date(2030, 4, 21)


def test_a_weekday_counts_in_full(config):
    # Tuesday 06:00 UTC to Wednesday 06:00 UTC: 24 working hours.
    assert W.working_hours(_at("2026-09-29T06:00"), _at("2026-09-30T06:00"), config) == 24.0


def test_a_weekend_counts_zero(config):
    # Saturday 23:00 UTC is Sunday 01:00 in Zurich (summer time): the clock
    # starts again at Monday 00:00 local, 22:00 UTC on Sunday.
    start = _at("2026-09-26T23:00")
    assert W.working_hours(start, _at("2026-09-27T04:00"), config) == 0.0
    assert W.working_hours(start, _at("2026-09-28T04:00"), config) == pytest.approx(6.0)


def test_a_public_holiday_counts_zero(config):
    # Thursday 24 Dec 17:00 CET to Monday 28 Dec 09:00 CET: Christmas, Boxing
    # Day and the weekend are out, leaving 7 h on Thursday and 9 on Monday.
    got = W.working_hours(_at("2026-12-24T16:00"), _at("2026-12-28T08:00"), config, "CH")
    assert got == pytest.approx(16.0)


def test_each_country_keeps_its_own_holidays(config):
    # Friday 3 October 2025 is German Unity Day, a working day in Switzerland.
    start, end = _at("2025-10-02T22:00"), _at("2025-10-03T22:00")
    assert W.working_hours(start, end, config, "DE") == 0.0
    assert W.working_hours(start, end, config, "CH") == pytest.approx(24.0)


def test_a_passed_deadline_keeps_its_plain_hours(config):
    assert W.working_hours(_at("2026-09-27T12:00"), _at("2026-09-26T12:00"), config) == -24.0


def test_switched_off_it_is_plain_elapsed_time(config):
    off = copy.deepcopy(config)
    off.files["scoring"].data = copy.deepcopy(config.scoring)
    off.files["scoring"].data["working_calendar"]["enabled"] = False
    start = _at("2026-09-26T23:00")
    assert W.working_hours(start, start + timedelta(hours=29), off) == pytest.approx(29.0)


def test_the_deadline_sentence_says_working_time_when_it_differs(config):
    clock = Clock.at("2026-09-26T23:00:00+00:00")
    deadline = _at("2026-09-28T04:00")
    text = leadtime.deadline_text(clock, deadline, W.working_hours(clock.as_of, deadline, config))
    assert "6 working h" in text
    assert "Mon 04:00" in text
