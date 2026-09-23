"""Recording a window that already happened, tested without a network.

The recorder exists to turn "sixty days" into sixty real days of an archive.
A single request cannot do that — one GDELT answer holds at most 250
articles and a busy query fills 250 inside a day — so the window is asked
for one day at a time and merged.

And the archive does not always answer. GDELT rate-limits by network, and
the first real run of this refused the very first request (HTTP 429), then
the second, then timed out on the third — with the old recorder set to
grind through all sixty days the same way and start from scratch on a
re-run. So most of what is tested here is behaviour under refusal: every
day kept as it arrives, a re-run that resumes, a back-off that waits a
cooldown out, and a clean stop that keeps what it has.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest

from engine.ingest.sources import fetch
from engine.ingest.sources.catalog import CATALOG

ROOT = Path(__file__).resolve().parent.parent
AS_OF = datetime(2026, 9, 22, tzinfo=UTC)
SETTLED = AS_OF + timedelta(days=2)       # every day in the window is final


@pytest.fixture(scope="module")
def recorder():
    path = ROOT / "scripts" / "record_fixture.py"
    spec = importlib.util.spec_from_file_location("record_fixture", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: @dataclass resolves annotations through
    # sys.modules, and a module loaded by path is not in it otherwise.
    sys.modules["record_fixture"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def private_cache(recorder, tmp_path, monkeypatch):
    """Each test gets its own day cache, never the repository's."""
    monkeypatch.setattr(recorder, "CACHE", tmp_path / "cache")
    return tmp_path / "cache"


@pytest.fixture
def gdelt():
    return next(s for s in CATALOG if s.key == "gdelt_doc")


class FakeArchive:
    """Answers like GDELT: a day's articles, in the source's own shape.

    ``refuse`` maps a day index to how many times it is refused before it
    answers; ``float("inf")`` refuses forever.
    """

    def __init__(self, refuse=None, error="HTTP 429", repeat=None):
        self.calls: list[tuple] = []
        self.refuse = dict(refuse or {})
        self.error = error
        self.repeat = repeat              # (day, url) returned again that day

    def __call__(self, spec, as_of=None, window_days=None):
        self.calls.append((spec, as_of, window_days))
        day = round((AS_OF - as_of) / timedelta(days=1))
        if self.refuse.get(day, 0) > 0:
            self.refuse[day] -= 1
            return None, self.error
        articles = [
            {"url": f"https://news.example/{day}/{n}",
             "title": f"day {day} story {n}",
             "seendate": (as_of - timedelta(hours=n + 1)).strftime("%Y%m%dT%H%M%SZ")}
            for n in range(3)
        ]
        if self.repeat and self.repeat[0] == day:
            articles.append({"url": self.repeat[1], "title": "again", "seendate": ""})
        return {"articles": articles}, ""

    def days_asked(self) -> list[int]:
        return [round((AS_OF - call[1]) / timedelta(days=1)) for call in self.calls]


def run(recorder, gdelt, archive, days, now=SETTLED):
    pauses: list[float] = []
    result = recorder.record_window(
        gdelt, AS_OF, days, fetch=archive, pause=pauses.append,
        say=lambda *_: None, now=now,
    )
    return result, pauses


FOREVER = float("inf")


# =====================================================================
# The window is really the window
# =====================================================================
def test_sixty_days_is_sixty_requests_not_one(recorder, gdelt):
    archive = FakeArchive()
    result, _ = run(recorder, gdelt, archive, 60)

    assert len(archive.calls) == 60
    assert not result.missing and not result.stopped
    assert result.blob["_window"]["records"] == 180


def test_each_request_asks_for_one_day_stepping_back(recorder, gdelt):
    archive = FakeArchive()
    run(recorder, gdelt, archive, 5)

    assert [call[1] for call in archive.calls] == [
        AS_OF - timedelta(days=d) for d in range(5)
    ]
    assert all(call[2] <= recorder.SLICE_DAYS for call in archive.calls)


def test_a_fractional_window_is_covered_exactly(recorder, gdelt):
    """2.5 days is three requests and the last is half a day — not a third
    full day that reaches past what was asked for."""
    archive = FakeArchive()
    run(recorder, gdelt, archive, 2.5)
    assert [call[2] for call in archive.calls] == [1.0, 1.0, 0.5]


def test_newest_day_comes_first(recorder, gdelt):
    """The board reasons over the first items it reaches, so the most recent
    day leads, as it does on the live path."""
    result, _ = run(recorder, gdelt, FakeArchive(), 3)
    days = [a["title"].split()[1] for a in result.blob["articles"]]
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
    result, _ = run(recorder, gdelt, archive, 2)

    urls = [a["url"] for a in result.blob["articles"]]
    assert len(urls) == len(set(urls))
    second = result.blob["_window"]["slices"][1]
    assert (second["records"], second["new"]) == (4, 3)


def test_the_merge_keeps_the_sources_own_shape(recorder, gdelt):
    """It is still a raw GDELT answer — one the mapper reads unchanged."""
    result, _ = run(recorder, gdelt, FakeArchive(), 2)
    assert isinstance(result.blob["articles"], list)
    assert result.blob["_fixture_note"].startswith("RECORDED FROM THE LIVE SOURCE")


def test_requests_are_spaced_to_the_sources_rate_limit(recorder, gdelt):
    """GDELT asks for one request every five seconds. The first request has
    nothing to wait for; every later one waits."""
    _, pauses = run(recorder, gdelt, FakeArchive(), 4)
    assert pauses == [recorder.PAUSE_S] * 3
    assert recorder.PAUSE_S >= 5


# =====================================================================
# Refusal: waited out, not hammered
# =====================================================================
def test_a_refused_day_is_waited_out_and_then_answered(recorder, gdelt):
    archive = FakeArchive(refuse={0: 2})
    result, pauses = run(recorder, gdelt, archive, 2)

    assert not result.missing and not result.stopped
    assert archive.days_asked() == [0, 0, 0, 1]
    assert pauses[:2] == list(recorder.BACKOFF_S[:2]), "each wait longer than the last"


def test_the_back_off_resets_after_a_success(recorder, gdelt):
    """A source that refused one day and answered the next has recovered —
    a later refusal starts from the shortest wait again."""
    archive = FakeArchive(refuse={0: 1, 2: 1})
    _, pauses = run(recorder, gdelt, archive, 3)
    backoffs = [p for p in pauses if p != recorder.PAUSE_S]
    assert backoffs == [recorder.BACKOFF_S[0], recorder.BACKOFF_S[0]]


def test_retry_after_is_honoured_when_it_asks_for_longer(recorder, gdelt):
    archive = FakeArchive(refuse={0: 1}, error="HTTP 429 (retry after 300s)")
    _, pauses = run(recorder, gdelt, archive, 1)
    assert pauses == [300.0]


def test_a_source_that_keeps_refusing_stops_the_run(recorder, gdelt):
    """The failure as it first happened: refused from the first request.
    It stops after the back-off rather than failing the other 59 days."""
    archive = FakeArchive(refuse={d: FOREVER for d in range(60)})
    result, pauses = run(recorder, gdelt, archive, 60)

    assert result.blob is None, "nothing arrived, so nothing is written"
    assert result.stopped and "HTTP 429" in result.stopped
    assert len(archive.calls) == 1 + len(recorder.BACKOFF_S)
    assert pauses == list(recorder.BACKOFF_S)
    assert len(result.missing) == 60


def test_a_stop_keeps_what_had_already_arrived(recorder, gdelt):
    archive = FakeArchive(refuse={d: FOREVER for d in range(2, 5)})
    result, _ = run(recorder, gdelt, archive, 5)

    assert result.stopped
    assert result.blob["_window"]["records"] == 6          # days 0 and 1
    gaps = [g["to"][:10] for g in result.missing]
    assert gaps == ["2026-09-20", "2026-09-19", "2026-09-18"]
    assert result.missing[1]["error"] == "not fetched — stopped"


# =====================================================================
# Resuming: nothing fetched twice
# =====================================================================
def test_a_rerun_fetches_only_the_days_it_does_not_have(recorder, gdelt):
    first = FakeArchive(refuse={d: FOREVER for d in range(3, 6)})
    stopped, _ = run(recorder, gdelt, first, 6)
    assert stopped.stopped

    second = FakeArchive()
    result, _ = run(recorder, gdelt, second, 6)

    assert second.days_asked() == [3, 4, 5], "days 0-2 came from the cache"
    assert result.reused == 3 and result.fetched == 3
    assert not result.missing and not result.stopped
    assert result.blob["_window"]["records"] == 18


def test_an_interrupted_run_loses_nothing_it_had_fetched(
    recorder, gdelt, private_cache,
):
    """Ctrl+C mid-run: the days before it are already on disk."""
    class Interrupt(FakeArchive):
        def __call__(self, spec, as_of=None, window_days=None):
            if len(self.calls) == 2:
                raise KeyboardInterrupt
            return super().__call__(spec, as_of=as_of, window_days=window_days)

    with pytest.raises(KeyboardInterrupt):
        run(recorder, gdelt, Interrupt(), 5)

    kept = sorted((private_cache / "gdelt_doc").glob("*.json"))
    assert len(kept) == 2
    assert not list((private_cache / "gdelt_doc").glob("*.part"))


def test_a_changed_query_does_not_reuse_days_recorded_for_the_old_one(
    recorder, gdelt,
):
    run(recorder, gdelt, FakeArchive(), 2)
    changed = replace(gdelt, params={**gdelt.params, "query": "Rhine"})
    archive = FakeArchive()
    recorder.record_window(changed, AS_OF, 2, fetch=archive,
                           pause=lambda _s: None, say=lambda *_: None, now=SETTLED)
    assert len(archive.calls) == 2


def test_a_day_that_may_still_be_filling_in_is_not_kept(recorder, gdelt):
    """GDELT indexes with a lag, so yesterday is fetched again next time
    rather than frozen half-complete."""
    run(recorder, gdelt, FakeArchive(), 2, now=AS_OF + timedelta(hours=3))
    archive = FakeArchive()
    run(recorder, gdelt, archive, 2, now=AS_OF + timedelta(hours=3))
    assert archive.days_asked() == [0], "the day ending 3 h ago was fetched again"


def test_the_day_cache_is_never_committed():
    probe = "data/cache/record_fixture/gdelt_doc/x.json"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", probe], cwd=ROOT, check=False,
    )
    assert ignored.returncode == 0, f"{probe} would be committed"


# =====================================================================
# The fetcher passes on how long to wait
# =====================================================================
def test_a_refusal_carries_its_retry_after_through_the_fetcher(gdelt, monkeypatch):
    headers = Message()
    headers["Retry-After"] = "120"

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError(gdelt.url, 429, "Too Many", headers, None)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", refuse)
    blob, error = fetch._get(gdelt)
    assert blob is None
    assert error == "HTTP 429 (retry after 120s)"


@pytest.mark.parametrize("stopped, says, never", [
    ("still HTTP 429 after waiting 30 s, 60 s, 120 s", "limiting", "Check the connection"),
    ("still unreachable (timed out) after waiting 30 s, 60 s, 120 s",
     "Check the connection", "limiting"),
])
def test_the_advice_on_a_stop_depends_on_why_it_stopped(recorder, stopped, says, never):
    """Only a 429 is the source limiting this network. Telling somebody on a
    dead link that they are being rate-limited sends them to the wrong fix."""
    advice = " ".join(recorder._stop_advice(stopped, "gdelt_doc"))
    assert says in advice
    assert never not in advice
    assert "carries on where this stopped" in advice
    assert "--skip gdelt_doc" in advice, "the way to stop waiting on it"
