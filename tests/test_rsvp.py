"""Who has confirmed the all-hands.

Pinned here: nobody is confirmed until they are; a reply is for one sitting,
so the next one starts empty; the latest reply wins, so an undo works; only
departments in the room can reply; and the board is annotated without
touching the cached one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine import rsvp

AT = datetime(2026, 9, 28, 9, tzinfo=UTC)


def _meeting(next_at="2026-09-18T08:30:00+00:00"):
    return {
        "next_at": next_at,
        "attendees": [{"id": "FN_SUPPLY_CHAIN", "function": "Supply Chain"},
                      {"id": "FN_PROCUREMENT", "function": "Procurement"},
                      {"id": "FN_CONTROLLING", "function": "Controlling"}],
    }


@pytest.fixture
def log(tmp_path):
    return tmp_path / "rsvp.jsonl"


def test_nobody_is_confirmed_until_they_reply(log):
    got = rsvp.replies(_meeting(), log)
    assert set(got) == {"FN_SUPPLY_CHAIN", "FN_PROCUREMENT", "FN_CONTROLLING"}
    assert all(r["status"] == "pending" for r in got.values())


def test_a_confirmation_counts_and_an_undo_takes_it_back(log):
    rsvp.record(_meeting(), "FN_PROCUREMENT", "confirmed", by="A. Keller", at=AT, log=log)
    board = rsvp.annotate({"all_hands": _meeting(), "routes": []}, log)
    assert board["all_hands"]["confirmed"] == 1
    assert board["all_hands"]["invited"] == 3
    assert board["all_hands"]["rsvp"]["FN_PROCUREMENT"]["by"] == "A. Keller"
    rsvp.record(_meeting(), "FN_PROCUREMENT", "pending", by=None, at=AT, log=log)
    assert rsvp.replies(_meeting(), log)["FN_PROCUREMENT"]["status"] == "pending"


def test_the_next_sitting_starts_with_nobody_confirmed(log):
    rsvp.record(_meeting(), "FN_SUPPLY_CHAIN", "confirmed", by=None, at=AT, log=log)
    later = _meeting(next_at="2026-09-21T08:30:00+00:00")
    assert rsvp.replies(later, log)["FN_SUPPLY_CHAIN"]["status"] == "pending"


def test_only_the_room_can_reply_and_only_with_a_known_status(log):
    with pytest.raises(rsvp.RsvpError, match="not in the room"):
        rsvp.record(_meeting(), "FN_MARKETING", "confirmed", by=None, at=AT, log=log)
    with pytest.raises(rsvp.RsvpError, match="status"):
        rsvp.record(_meeting(), "FN_PROCUREMENT", "maybe", by=None, at=AT, log=log)
    with pytest.raises(rsvp.RsvpError, match="no sitting"):
        rsvp.record(_meeting(next_at=None), "FN_PROCUREMENT", "confirmed", by=None, at=AT, log=log)
    assert not log.exists()


def test_the_cached_board_is_not_touched(log):
    board = {"all_hands": _meeting(), "routes": []}
    rsvp.record(_meeting(), "FN_CONTROLLING", "confirmed", by=None, at=AT, log=log)
    out = rsvp.annotate(board, log)
    assert "rsvp" in out["all_hands"]
    assert "rsvp" not in board["all_hands"]
