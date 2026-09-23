"""Old news ages out, even when the source never says it ended.

News indexes report that something happened and never that it stopped. The
mapper has always given such an item a default window from the moment it was
reported — and nothing read it, so an article stayed open forever. That was
invisible while the news fetch only reached back three days. It is not
invisible once the fetch reaches back two months: every strike since July
would sit on the board as a disruption happening now, and the rescue path —
which skipped the temporal layer entirely — would spend the model's triage
budget reading them.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.ingest import observations
from engine.pipeline import RunOptions, _closed_before, run

AS_OF = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
CLOCK = Clock.at(AS_OF)
# The samples the tests are written against, not data/fixtures — which a
# recording replaces. See conftest.py.
FIXTURES = Path(__file__).resolve().parent / "fixtures"

BLOCKADE = "Hauliers blockade Antwerp terminal gates over tariff dispute"
HORMUZ = "Iran announces closure of Strait of Hormuz to commercial shipping"


def item(**overrides) -> dict:
    base = {
        "starts_at": AS_OF - timedelta(days=1),
        "published_at": AS_OF - timedelta(days=1),
        "ends_at": None,
        "default_ends_at": AS_OF + timedelta(days=6),
    }
    base.update(overrides)
    return base


# =====================================================================
# The rule
# =====================================================================
def test_a_report_with_no_end_is_live_inside_its_default_window():
    assert _closed_before(item(), CLOCK) is None


def test_a_report_with_no_end_ages_out_after_its_default_window():
    stale = item(
        starts_at=AS_OF - timedelta(days=30),
        default_ends_at=AS_OF - timedelta(days=23),
    )
    reason = _closed_before(stale, CLOCK)
    assert reason is not None
    assert "no end stated" in reason
    assert "2026-08-19" in reason, "the note should say when it was reported"


def test_a_stated_end_is_believed_over_the_default():
    """The default only ever fills an absence. A source that says the closure
    lasts a fortnight is not overruled by a seven-day guess."""
    stated = item(
        ends_at=AS_OF + timedelta(days=10),
        default_ends_at=AS_OF - timedelta(days=5),
    )
    assert _closed_before(stated, CLOCK) is None

    ended = item(
        ends_at=AS_OF - timedelta(hours=1),
        default_ends_at=AS_OF + timedelta(days=5),
    )
    assert _closed_before(ended, CLOCK) == "event window closed before as-of"


def test_an_item_with_neither_end_is_left_alone():
    """Feeds that predate the fallback carry no default at all. Dropping them
    would change behaviour nobody asked to change."""
    assert _closed_before(item(default_ends_at=None), CLOCK) is None


# =====================================================================
# Through the whole pipeline
# =====================================================================
@pytest.fixture
def aged_fixtures(tmp_path, monkeypatch):
    """The samples, with the blockade report moved back 30 days."""
    target = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, target)
    path = target / "gdelt_articles.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    for article in blob["articles"]:
        if article["title"] == BLOCKADE:
            article["seendate"] = "20260819T033000Z"
    path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(observations, "FIXTURE_DIR", target)
    return target


def _run():
    return run(clock=CLOCK, config=load_config(),
               options=RunOptions(shipment_count=125, seed=7))


def test_a_month_old_report_no_longer_reaches_the_board(aged_fixtures):
    ctx = _run()
    titles = {e.title for e in ctx.events}

    assert BLOCKADE not in titles, "a 30-day-old blockade is on the board as live"
    assert HORMUZ in titles, "the control: fresh news must still get through"

    notes = [note for note in ctx.router_notes.values() if "no end stated" in note]
    assert notes, "the item was dropped without saying why"


def test_the_same_report_is_on_the_board_when_it_is_fresh():
    """The control for the test above: without the ageing, the blockade is an
    event. Otherwise the test above would pass for some other reason."""
    titles = {e.title for e in _run().events}
    assert BLOCKADE in titles
