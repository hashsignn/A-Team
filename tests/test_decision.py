"""The Action tab's decision tree (engine/export/decision.py).

What it must never do is what the old tab did: show the same order under two
answers. Every order at risk sits in exactly one branch (kept on time, damage
reduced, customer told) or is absorbed by its buffers.
"""

from __future__ import annotations

import pytest

from engine.clock import Clock
from engine.export import decision
from engine.export.board import build_board
from engine.fast import view
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-26T23:00:00+00:00")


@pytest.fixture(scope="module")
def trees():
    context = run(clock=AS_OF, options=RunOptions(shipment_count=150, seed=7))
    board = build_board(context)
    lanes = {row["route_id"]: row for row in view.route_summaries(context)}
    return {r["route_id"]: decision.build(context, r, lanes.get(r["route_id"]))
            for r in board["routes"] if r["shipments_at_risk"]}


def test_every_order_at_risk_is_in_exactly_one_branch(trees):
    assert trees, "the demo board has routes at risk"
    for route_id, t in trees.items():
        kept = set(t["keep"]["kept"])
        reduced = set(t["reduce"]["orders"])
        told = {o["shipment_id"] for o in t["tell"]["orders"]}
        assert not (kept & reduced or kept & told or reduced & told), route_id
        assert len(kept) + len(reduced) + len(told) == t["hit"]["need"], route_id
        assert t["hit"]["need"] + t["hit"]["absorbed"] == t["hit"]["orders"], route_id


def test_the_answer_matches_the_branches(trees):
    for route_id, t in trees.items():
        need, kept = t["hit"]["need"], len(t["keep"]["kept"])
        expected = ("none" if not need else "yes" if kept == need
                    else "partly" if kept else "no")
        assert t["keep"]["answer"] == expected, route_id


def test_a_way_that_keeps_the_date_keeps_every_order_it_carries(trees):
    seen = 0
    for t in trees.values():
        for i, o in enumerate(t["keep"]["options"]):
            seen += 1
            assert o["on_time"] == o["orders"] and o["late_after_days"] == 0
            assert o["best"] is (i == 0)
            # A new route is drawable: a path of places and a line along the
            # sea lanes. A playbook action (divert a road leg) is a
            # description instead.
            if o["kind"] == "reroute":
                assert len(o["path"]) >= 2 and len(o["line"]) >= len(o["path"])
            else:
                assert o["detail"]
    assert seen, "at least one route on the demo board can keep its dates"


def test_the_comparison_starts_from_staying_as_planned(trees):
    for t in trees.values():
        rows = t["compare"]
        assert rows[0]["id"] == "as_planned" and rows[0]["baseline"] is True
        assert all(r["baseline"] is False for r in rows[1:])
        assert rows[0]["cost_chf"] == 0.0
