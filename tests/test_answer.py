"""Ask answers from the board with no model (engine/reason/answer.py).

The rule for a built-in answer is the rule for the model: nothing that is not
on the board. So the tests check each kind of question lands on the right
rows, and that the numbers quoted are the board's own.
"""

from __future__ import annotations

import re

import pytest

from engine.clock import Clock
from engine.export import decision
from engine.export.board import build_board
from engine.pipeline import RunOptions, run
from engine.reason import answer, ask, llm

AS_OF = Clock.at("2026-09-26T23:00:00+00:00")


@pytest.fixture(scope="module")
def world():
    context = run(clock=AS_OF, options=RunOptions(shipment_count=150, seed=7))
    board = build_board(context)
    def tree_for(route_id):
        route = next((r for r in board["routes"] if r["route_id"] == route_id), None)
        return decision.build(context, route, board) if route else None

    return board, tree_for


def _urgent(board):
    rank = {"red": 0, "yellow": 1}
    rows = [r for r in board["routes"] if r["level"] in rank]
    return sorted(rows, key=lambda r: (rank[r["level"]], r["lead_time_hours"]
                                       if r["lead_time_hours"] is not None else 1e9,
                                       -r["exposure_chf"]))


def test_which_route_first_names_the_most_urgent_one(world):
    board, tree_for = world
    out = answer.reply(board, "Which route needs a decision first, and why?", tree_for=tree_for)
    first = _urgent(board)[0]
    assert out["route_id"] == first["route_id"]
    assert out["answer"].startswith("First:")
    assert f"CHF {first['exposure_chf']:,.0f}" in out["answer"]


def test_a_customer_by_name_lists_their_orders_at_risk(world):
    board, _ = world
    rows = [(r, c) for r in board["routes"] for c in r["customers"] if c.get("at_risk")]
    assert rows, "the demo board has a customer at risk"
    name = rows[0][1]["name"]
    out = answer.reply(board, f"What about {name}?")
    assert out["answer"].startswith(name)
    for _r, c in rows:
        if c["name"] == name:
            for order in c["orders_at_risk"]:
                assert order in out["answer"]
    loss = sum(c["expected_loss_chf"] for r in board["routes"] for c in r["customers"] if c["name"] == name)
    assert f"CHF {loss:,.0f}" in out["answer"]


def test_an_order_id_finds_its_customer_and_route(world):
    board, tree_for = world
    route = next(r for r in board["routes"] if r["customers"])
    customer = route["customers"][0]
    order = customer["orders"][0]
    out = answer.reply(board, f"where is {order.lower()}?", tree_for=tree_for)
    assert customer["name"] in out["answer"] and out["route_id"] == route["route_id"]


def test_a_place_on_a_route_answers_about_that_route(world):
    board, tree_for = world
    out = answer.reply(board, "What about the Rhine?", tree_for=tree_for)
    route = next(r for r in board["routes"] if r["route_id"] == out["route_id"])
    assert "rhine" in answer.fold(route["name"] + " ".join(s["name"] for s in route["stops"]))
    assert any(link.get("href", "").startswith("/tree?route=") for link in out["links"])


def test_alternatives_come_from_the_decision_tree(world):
    board, tree_for = world
    with_ways = [(r, tree_for(r["route_id"])) for r in _urgent(board)]
    with_ways = [(r, t) for r, t in with_ways if t["keep"]["options"]]
    assert with_ways, "an urgent route on the demo board has a way that keeps the date"
    first, tree = with_ways[0]
    out = answer.reply(board, f"what are the alternatives on {first['route_id']}?", tree_for=tree_for)
    assert tree["keep"]["options"][0]["label"] in out["answer"]


def test_the_meeting_signals_and_money_answer_from_their_blocks(world):
    board, _ = world
    meet = answer.reply(board, "When is the all-hands?")
    assert board["all_hands"]["next_label"] in meet["answer"]
    money = answer.reply(board, "What does it cost if nobody acts?")
    total = sum(r["exposure_chf"] for r in board["routes"])
    assert f"CHF {total:,.0f}" in money["answer"]
    warn = answer.reply(board, "Any early warnings?")
    patterns = board["carrier_signals"]["patterns"]
    if patterns:
        assert patterns[0]["node_name"] in warn["answer"]


def test_a_question_the_board_cannot_answer_says_so(world):
    board, _ = world
    out = answer.reply(board, "qwxz blorp frobnicate")
    assert out["answer"].startswith("The board does not carry that") and out["unsure"]


def test_every_franc_in_an_answer_is_one_the_board_carries(world):
    board, tree_for = world
    # Every CHF figure the board or the trees could quote, rounded as shown.
    known = set()
    for r in board["routes"]:
        known.add(round(r["exposure_chf"]))
        for c in r["customers"]:
            known.add(round(c["expected_loss_chf"] or 0))
    total = sum(r["exposure_chf"] for r in board["routes"])
    known.add(round(total))
    for q in ("Which route needs a decision first?", "What does it cost if nobody acts?",
              "What about the Rhine?", "Which key accounts are at risk?"):
        out = answer.reply(board, q, tree_for=tree_for)
        for figure in re.findall(r"CHF ([\d,]+)", out["answer"]):
            value = int(figure.replace(",", ""))
            ok = value in known or any(abs(value - k) <= 1 for k in known)
            if not ok:
                # A decision-tree figure: a way's cost or the stay loss.
                tree_values = set()
                for r in board["routes"]:
                    t = tree_for(r["route_id"]) if r["shipments_at_risk"] else None
                    for o in (t or {}).get("keep", {}).get("options", []):
                        tree_values.add(round(o["cost_chf"]))
                    for c in (t or {}).get("compare", []):
                        tree_values.add(round(c.get("exposure_chf") or 0))
                    for row in (t or {}).get("orders", []):
                        tree_values.add(round(row["loss_chf"]))
                for k in board["key_accounts"]:
                    tree_values.add(round(k["expected_loss_chf"]))
                ok = any(abs(value - k) <= 1 for k in tree_values) or _sums_of(board, value)
            assert ok, f"{q!r} quoted CHF {figure}, which the board does not carry"


def _sums_of(board, value):
    """Key-account totals are sums of the board's rows."""
    by = {}
    for k in board["key_accounts"]:
        by[k["customer"]] = by.get(k["customer"], 0) + k["expected_loss_chf"]
    return any(abs(round(v) - value) <= 1 for v in by.values())


def test_ask_answers_without_a_model(world, monkeypatch):
    board, tree_for = world
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        llm.Backend.NONE, None, "no Ollama at http://localhost:11434", "a model would add reasoning"))
    out = ask.board_question(board, "Which route needs a decision first?", tree_for=tree_for)
    assert out["answered"] is True and out["generated"] is False and out["backend"] == "builtin"
    assert out["answer"].startswith("First:")
    assert "no Ollama" in out["model_status"]


def test_an_event_question_answers_from_its_row(world, monkeypatch):
    board, _ = world
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        llm.Backend.NONE, None, "no model", ""))
    event = next(e for r in board["routes"] for e in r["events"])
    out = ask.event_question(board, event["event_id"], "how bad is this?")
    assert out["answered"] is True and event["title"] in out["answer"]


def test_a_customer_by_the_first_word_of_its_name(world):
    board, _ = world
    names = {c["name"] for r in board["routes"] for c in r["customers"] if c.get("at_risk")}
    for name in names:
        first = next(w for w in re.findall(r"[a-z0-9]+", answer.fold(name))
                     if len(w) >= 4 and w not in answer._COMMON)
        out = answer.reply(board, f"What about {first.title()}?")
        found = out["answer"].split(" (")[0]
        assert answer.fold(found).split()[0] == answer.fold(name).split()[0] or first in answer.fold(found), (name, out["answer"][:80])
