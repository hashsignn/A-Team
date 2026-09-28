"""The assistant: answers grounded in the board, and nothing else.

WHAT THIS IS FOR
================
A planner looking at a red route has questions the UI has not anticipated —
"why is this one worse than the Antwerp one", "which customers are on it",
"what happens if I do nothing until Friday". Those are answerable from what
the board already contains. Making them typeable is worth a lot; making them
answerable by a model that will guess is worth less than nothing.

THE ONE RULE
------------
The context handed to the model is assembled HERE, from the board, and the
system prompt says to answer from it or say it is not there. The model is not
given tools, a search path, or the internet. If the answer is not in the
context, the honest output is "the board does not carry that", and the prompt
asks for exactly that sentence.

This is not a guarantee — no prompt is — which is why the answer is rendered
with a visible marker saying it came from a model, next to the numbers that
did not. A planner should be able to tell at a glance which parts of the
screen were computed and which were written.

WHY NOT GIVE IT THE WHOLE BOARD
-------------------------------
Because the board is ~200 KB of JSON and most of it is geometry. The context
builders below select the fields that answer questions a planner actually
asks, which also keeps a local 7B model inside a window it can use well.
"""

from __future__ import annotations

import json

from engine.reason import answer as answer_mod
from engine.reason import llm

SYSTEM = """You are a supply chain risk assistant embedded in a planning tool.

You answer ONLY from the CONTEXT below, which is the current state of the
planner's risk board. It is the same data they are looking at.

Rules, in order of importance:
1. If the context does not contain the answer, say exactly: "The board does
   not carry that." Then say what would. Never guess, never fill a gap from
   general knowledge about shipping.
2. Never invent a number. Every figure you give must appear in the context.
3. Where a probability is marked unsourced, say it is unsourced. Do not
   substitute a value, and do not describe it as "about 50%".
4. Be short. A planner reads this between two other things: two or three
   sentences, no preamble, no restating the question.
5. You advise; the planner decides. Do not instruct them to take an action —
   say what the board implies and what it would cost.

The levels are a TIME-TO-ACT scale, not a damage scale:
Critical = act within 8 h, Alert = 24-36 h, Watch = decide in 3 days,
Bias = decide within 5 days, Normal = nothing needed. All counted in working
time: weekends and public holidays do not count."""


def _event_context(event: dict) -> dict:
    """One event, trimmed to what a question about it could need."""
    matrix = event.get("matrix", {})
    return {
        "title": event["title"],
        "severity": event["severity"],
        "class": event["event_class"],
        "starts_at": event["starts_at"],
        "ends_at": event["ends_at"],
        "already_happened": event["realized"],
        "probability": (
            "UNSOURCED — no defensible figure exists for this"
            if event["probability"] is None
            else event["probability"]
        ),
        "probability_basis": event["probability_basis"],
        "shipments_touched": event["shipments_here"],
        "expected_loss_chf": event["exposure_chf"],
        "worst_case_if_it_happens_chf": matrix.get("worst_conditional_chf"),
        "shipments_still_actionable": matrix.get("still_actionable"),
        "risk_variables_active": event["active_variables"],
        "source": event["source"],
        "source_tier": event["source_tier"],
        "quote": event["quote"],
        "inferred_not_quoted": event["inferred"],
    }


def event_question(board: dict, event_id: str, question: str) -> dict:
    """Answer a question about one event, grounded in that event's row."""
    found = None
    route_name = None
    for route in board["routes"]:
        for event in route.get("events", []):
            if event["event_id"] == event_id:
                found, route_name = event, route["name"]
                break
        if found:
            break
    if found is None:
        return _no_answer("That event is not on the current board.")

    context = {
        "as_of": board["as_of_label"],
        "route": route_name,
        "event": _event_context(found),
    }
    return _ask(context, question, answer_mod.event_reply(board, event_id))


def board_brief(board: dict, route: dict | None = None, tree: dict | None = None) -> str:
    """Everything a question about the board could need, as short lines: the
    routes at risk, the key accounts, the meeting, the early warnings, the
    penalties, and the route the question is about with its decision tree.

    Lines, not JSON: a small model on a laptop reads 1,500 tokens in
    seconds and 8,000 in minutes, and the rows say the same either way."""
    lines = [f"BOARD at {board['as_of_label']}. {(board.get('posture') or {}).get('headline', '')}",
             "Levels: " + ", ".join(f"{lv['count']} {lv['label']} ({lv['directive'].lower()})"
                                    for lv in board["levels"]),
             "Delay penalties from contracts counted: "
             + ("yes" if (board.get("penalties") or {}).get("enabled") else "no"),
             "", "ROUTES WITH ORDERS AT RISK:"]
    at_risk = sorted([r for r in board["routes"] if r.get("shipments_at_risk")], key=answer_mod._urgency)
    lines += [f"- {answer_mod._route_line(board, r)}" for r in at_risk]
    calm = [r["name"] for r in board["routes"] if not r.get("shipments_at_risk")]
    if calm:
        lines.append(f"Calm routes (nothing at risk): {len(calm)}")
    keys = answer_mod.key_accounts(board)["facts"]
    if keys:
        lines += ["", "KEY ACCOUNTS AT RISK:", *keys[1:]]
    lines += ["", "ALL-HANDS:", *answer_mod.meeting(board)["facts"]]
    warn = answer_mod.signals(board)["facts"]
    lines += ["", "EARLY WARNINGS:", *(warn or ["none"])]
    if route:
        lines += ["", f"THE ROUTE ASKED ABOUT: {route['name']} ({route['level_label']})",
                  *[f"- event: {e['title']} ({e.get('kind_label', '')}, {answer_mod.orders(e.get('shipments_here', 0))}, "
                    f"{answer_mod.chf(e.get('exposure_chf'))})" for e in route.get("events", [])[:4]]]
        lines += [f"- {x.lstrip('• ')}" for x in answer_mod._if_nobody_acts(tree, route)]
        lines += [f"- {x.lstrip('• ')}" for x in answer_mod._tree_lines(board, tree)]
        for o in ((tree or {}).get("keep") or {}).get("options", [])[:3]:
            lines.append(f"- way: {o['label']}, {o['on_time']}/{o['orders']} on time, "
                         f"extra {answer_mod.chf(o['cost_chf'])}, closes {answer_mod._fmt_iso(o.get('closes_at'))}")
        for a in route.get("actions", [])[:5]:
            lines.append(f"- playbook: {a.get('sentence', a['label'])}")
        manager = (route.get("response") or {}).get("route_manager") or {}
        if manager.get("name"):
            lines.append(f"- route manager: {manager['name']}, {manager.get('phone', '')}, {manager.get('email', '')}")
    return "\n".join(lines)


def board_question(board: dict, question: str, route_id: str | None = None,
                   tree_for: answer_mod.TreeFor | None = None) -> dict:
    """Answer a question about the whole board.

    Answered from the board first, always (engine/reason/answer.py): that
    works with no model at all. With a model connected, the model writes
    the answer from the full context, starting from those same rows.
    """
    builtin = answer_mod.reply(board, question, route_id, tree_for)
    status = llm.detect()
    if not status.available:
        return _builtin(builtin, status)
    about = builtin.get("route_id") or route_id
    route = next((r for r in board["routes"] if r["route_id"] == about), None) if about else None
    tree = tree_for(about) if (tree_for and about) else None
    return _ask(board_brief(board, route, tree), question, builtin, status)


def _builtin(reply: dict, status: llm.BackendStatus | None = None, reason: str = "") -> dict:
    return {
        "answered": True,
        "answer": reply["answer"],
        "links": reply.get("links") or [],
        "backend": "builtin",
        "model": None,
        # Not written by a model: every line is a row of the board.
        "generated": False,
        "unsure": bool(reply.get("unsure")),
        "model_status": reason or (status.detail if status else ""),
        "unlocks_if_connected": status.unlocks_if_connected if status else "",
    }


def _ask(context: dict | str, question: str, builtin: dict | None = None,
         status: llm.BackendStatus | None = None) -> dict:
    status = status or llm.detect()
    if not status.available:
        if builtin:
            return _builtin(builtin, status)
        return _no_model(status)
    missing = llm.model_missing(status)
    if missing and builtin:
        return _builtin(builtin, status, missing)

    facts = (builtin or {}).get("facts") or []
    text = context if isinstance(context, str) else json.dumps(context, indent=1, default=str)
    prompt = (
        "CONTEXT (the planner's current board):\n"
        f"{text}\n\n"
        + ("ROWS THAT ANSWER IT (from the board, already checked):\n"
           + "\n".join(facts) + "\n\n" if facts else "")
        + f"QUESTION: {question.strip()}"
    )
    answer = llm.ask_text(SYSTEM, prompt, status)
    if answer is None:
        if builtin:
            return _builtin(builtin, status, "The model did not answer; this is from the board.")
        return _no_answer(
            "The model did not answer. Everything on the board was computed "
            "without it and is unaffected."
        )
    return {
        "answered": True,
        "answer": answer,
        "links": (builtin or {}).get("links") or [],
        "backend": status.backend.value,
        "model": status.model,
        # Carried so the UI can mark it. A planner must be able to tell at a
        # glance which parts of the screen were computed and which were
        # written by a model.
        "generated": True,
    }


def _no_model(status: llm.BackendStatus) -> dict:
    return {
        "answered": False,
        "answer": None,
        "backend": status.backend.value,
        "model": None,
        "generated": False,
        "reason": status.detail,
        "unlocks_if_connected": status.unlocks_if_connected,
    }


def _no_answer(reason: str) -> dict:
    return {
        "answered": False,
        "answer": None,
        "generated": False,
        "reason": reason,
    }
