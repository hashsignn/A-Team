"""Recording a window that already happened, tested without a network.

The recorder exists to turn "sixty days" into sixty real days of an archive.
A single request cannot do that — one GDELT answer holds at most 250
articles and a busy query fills 250 inside a day — so the window is asked
for one day at a time and merged. Every property here is one a real run
would only reveal six minutes in, on somebody else's machine.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.ingest.sources.catalog import CATALOG

ROOT = Path(__file__).resolve().parent.parent
AS_OF = datetime(2026, 9, 22, tzinfo=UTC)


@pytest.fixture(scope="module")
def recorder():
    path = ROOT / "scripts" / "record_fixture.py"
    spec = importlib.util.spec_from_file_location("record_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gdelt():
    return next(s for s in CATALOG if s.key == "gdelt_doc")


class FakeArchive:
    """Answers like GDELT: a day's articles, in the source's own shape."""

    def __init__(self, fail=(), fail_once=(), repeat=None):
        self.calls: list[tuple] = []
        self.fail = set(fail)             # day index -> fails every time
        self.fail_once = set(fail_once)   # day index -> fails the first time
        self.repeat = repeat              # (day, url) returned again that day

    def __call__(self, spec, as_of=None, window_days=None):
        self.calls.append((spec, as_of, window_days))
        day = round((AS_OF - as_of) / timedelta(days=1))
        if day in self.fail:
            return None, "HTTP 429"
        if day in self.fail_once:
            self.fail_once.discard(day)
            return None, "connection reset"
        articles = [
            {"url": f"https://news.example/{day}/{n}",
             "title": f"day {day} story {n}",
             "seendate": (as_of - timedelta(hours=n + 1)).strftime("%Y%m%dT%H%M%SZ")}
            for n in range(3)
        ]
        if self.repeat and self.repeat[0] == day:
            articles.append({"url": self.repeat[1], "title": "again", "seendate": ""})
        return {"articles": articles}, ""


def run(recorder, gdelt, archive, days):
    pauses: list[float] = []
    blob, missing = recorder.record_window(
        gdelt, AS_OF, days, fetch=archive, pause=pauses.append, say=lambda *_: None,
    )
    return blob, missing, pauses


# =====================================================================
# The window is really the window
# =====================================================================
def test_sixty_days_is_sixty_requests_not_one(recorder, gdelt):
    archive = FakeArchive()
    blob, missing, _ = run(recorder, gdelt, archive, 60)

    assert len(archive.calls) == 60
    assert not missing
    assert blob["_window"]["records"] == 180


def test_each_request_asks_for_one_day_stepping_back(recorder, gdelt):
    archive = FakeArchive()
    run(recorder, gdelt, archive, 5)

    ends = [call[1] for call in archive.calls]
    lengths = [call[2] for call in archive.calls]
    assert ends == [AS_OF - timedelta(days=d) for d in range(5)]
    assert all(length <= recorder.SLICE_DAYS for length in lengths)


def test_a_fractional_window_is_covered_exactly(recorder, gdelt):
    """2.5 days is three requests and the last is half a day — not a third
    full day that reaches past what was asked for."""
    archive = FakeArchive()
    run(recorder, gdelt, archive, 2.5)

    assert [call[2] for call in archive.calls] == [1.0, 1.0, 0.5]


def test_newest_day_comes_first(recorder, gdelt):
    """The board reasons over the first items it reaches, so order matters:
    the most recent day leads, as it does on the live path."""
    blob, _, _ = run(recorder, gdelt, FakeArchive(), 3)
    days = [a["title"].split()[1] for a in blob["articles"]]
    assert days == sorted(days, key=int)


# =====================================================================
# Relevance, duplicates, and the source's own shape
# =====================================================================
def test_each_day_is_ranked_by_relevance_not_recency(recorder, gdelt):
    archive = FakeArchive()
    run(recorder, gdelt, archive, 2)

    for spec, _, _ in archive.calls:
        assert spec.params["sort"] == "hybridrel"
        assert spec.params["query"] == gdelt.params["query"], "the query is untouched"
    assert gdelt.params["sort"] == "datedesc", "the live spec itself is not modified"


def test_an_article_returned_twice_is_kept_once(recorder, gdelt):
    archive = FakeArchive(repeat=(1, "https://news.example/0/0"))
    blob, _, _ = run(recorder, gdelt, archive, 2)

    urls = [a["url"] for a in blob["articles"]]
    assert len(urls) == len(set(urls))
    assert blob["_window"]["slices"][1] == {
        **blob["_window"]["slices"][1], "records": 4, "new": 3,
    }


def test_the_merge_keeps_the_sources_own_shape(recorder, gdelt):
    """It is still a raw GDELT answer — one the mapper reads unchanged."""
    blob, _, _ = run(recorder, gdelt, FakeArchive(), 2)
    assert isinstance(blob["articles"], list)
    assert blob["_fixture_note"].startswith("RECORDED FROM THE LIVE SOURCE")


# =====================================================================
# Failure is written down, not papered over
# =====================================================================
def test_a_day_that_fails_once_is_retried(recorder, gdelt):
    archive = FakeArchive(fail_once={2})
    blob, missing, pauses = run(recorder, gdelt, archive, 4)

    assert not missing
    assert len(archive.calls) == 5
    assert recorder.RETRY_PAUSE_S in pauses


def test_a_day_that_fails_twice_is_a_recorded_gap(recorder, gdelt):
    blob, missing, _ = run(recorder, gdelt, FakeArchive(fail={1}), 3)

    assert len(missing) == 1
    assert missing[0]["error"] == "HTTP 429"
    assert missing[0]["to"].startswith("2026-09-21")
    assert blob["_window"]["missing"] == missing
    assert blob["_window"]["records"] == 6


def test_nothing_is_written_when_every_day_fails(recorder, gdelt):
    blob, missing, _ = run(recorder, gdelt, FakeArchive(fail=set(range(3))), 3)
    assert blob is None
    assert len(missing) == 3


def test_requests_are_spaced_to_the_sources_rate_limit(recorder, gdelt):
    """GDELT asks for one request every five seconds. The first request has
    nothing to wait for; every later one waits."""
    _, _, pauses = run(recorder, gdelt, FakeArchive(), 4)
    assert pauses == [recorder.PAUSE_S] * 3
    assert recorder.PAUSE_S >= 5
