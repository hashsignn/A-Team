"""Social chatter: worthless alone, useful in volume.

Every test here is a defence against a specific way social media lies about
how many people are saying something.
"""

from datetime import UTC, datetime, timedelta

import pytest

from engine.ingest.sources import chatter

BASE = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)


def posts(n: int, text: str, author=lambda i: f"acct{i}", at=None, repost=None):
    return [
        chatter.Post(f"p{text[:6]}{i}", author(i), text, at or BASE, repost_of=repost)
        for i in range(n)
    ]


# ------------------------------------------------------------- the threshold
def test_one_post_is_not_evidence():
    buckets = chatter.bucket(posts(1, "Kaub closed, nothing moving"))
    items, stats = chatter.promote(buckets)
    assert items == []
    assert stats["below_threshold"] == 1


def test_enough_distinct_accounts_becomes_one_item():
    buckets = chatter.bucket(posts(20, "Kaub closed, nothing moving"))
    items, stats = chatter.promote(buckets)
    assert len(items) == 1
    assert items[0]["authors"] == 20


def test_the_threshold_is_configurable_per_source():
    buckets = chatter.bucket(posts(5, "Kaub closed"))
    assert chatter.promote(buckets, min_authors=4)[0]
    assert chatter.promote(buckets, min_authors=6)[0] == []


# ------------------------------------------------------------ amplification
def test_one_account_shouting_is_not_a_crowd():
    """Counting posts makes a megaphone look like corroboration."""
    loud = posts(40, "Rotterdam strike incoming", author=lambda i: "megaphone")
    buckets = chatter.bucket(loud)
    assert buckets[0].author_count == 1
    assert buckets[0].amplification == 40.0
    assert chatter.promote(buckets)[0] == []


def test_a_repost_does_not_manufacture_a_second_witness():
    """Forty accounts repeating one claim is one claim, travelling."""
    original = chatter.Post("orig", "firsthand", "Antwerp terminal blocked", BASE)
    echoes = [
        chatter.Post(f"rp{i}", f"sheep{i}", "Antwerp terminal blocked",
                     BASE, repost_of="orig")
        for i in range(40)
    ]
    buckets = chatter.bucket([original, *echoes])
    assert buckets[0].author_count == 1
    assert chatter.promote(buckets)[0] == []


# ------------------------------------------------------------------ bucketing
def test_posts_are_split_by_what_they_describe():
    mixed = (posts(15, "Kaub closed to traffic")
             + posts(15, "Kaub queue is enormous today",
                     author=lambda i: f"other{i}"))
    kinds = {b.kind for b in chatter.bucket(mixed)}
    assert kinds == {"blocked", "congestion"}


def test_a_post_naming_two_places_counts_for_both():
    """Picking one and being wrong half the time is not better."""
    both = posts(14, "Both Antwerp and Rotterdam blocked this morning")
    places = {b.place for b in chatter.bucket(both)}
    assert len(places) >= 2


def test_posts_with_no_disruption_vocabulary_are_ignored():
    assert chatter.bucket(posts(30, "Lovely morning at Kaub today")) == []


def test_windows_are_fixed_not_sliding():
    """A sliding window makes the same corpus bucket differently depending on
    which post you start from, and a hindcast has to be reproducible."""
    early = posts(10, "Kaub closed", at=BASE)
    late = posts(10, "Kaub closed", author=lambda i: f"late{i}",
                 at=BASE + timedelta(hours=7))
    buckets = chatter.bucket(early + late, window_hours=6.0)
    assert len(buckets) == 2, "seven hours apart must not pool into one window"


# ------------------------------------------------------------------ the item
def test_the_item_reports_a_spike_not_a_fact():
    """The board must never render chatter as "the lock is shut"."""
    items, _ = chatter.promote(chatter.bucket(posts(20, "Kaub closed")))
    assert "accounts reporting" in items[0]["title"]
    assert items[0]["unverified"] is True


def test_chatter_enters_at_tier_three_and_stays_there():
    """Volume is a reason to look, never a reason to believe."""
    items, _ = chatter.promote(chatter.bucket(posts(50, "Kaub closed")))
    assert items[0]["source_tier"] == 3


def test_a_quiet_day_is_distinguishable_from_a_dead_source():
    _, stats = chatter.promote([])
    assert "not connected" in stats["note"]
    _, busy = chatter.promote(chatter.bucket(posts(3, "Kaub closed")))
    assert "reached" in busy["note"]


def test_the_multilingual_vocabulary_is_used():
    """Rhine and port chatter is German and Dutch as often as English."""
    assert chatter.classify("Kaub gesperrt seit heute früh") == "blocked"
    assert chatter.classify("staking in de haven van Rotterdam") == "strike"


@pytest.mark.parametrize("text,kind", [
    ("terminal fire at the quay", "incident"),
    ("niedrigwasser again", "weather"),
    ("huge backlog at the gate", "congestion"),
])
def test_kinds_are_recognised(text, kind):
    assert chatter.classify(text) == kind
