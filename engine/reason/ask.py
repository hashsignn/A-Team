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
Critical = act within 6 h, Alert = 24-48 h, Watch = decide in 3-7 days,
Bias = monitor, Normal = nothing needed."""


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
    return _ask(context, question)


def board_question(board: dict, question: str, route_id: str | None = None) -> dict:
    """Answer a question about the whole board.

    The selected route is included in full when there is one, because most
    questions asked with a route open are about that route.
    """
    context: dict = {
        "as_of": board["as_of_label"],
        "posture": board["posture"],
        "levels": board["levels"],
        "routes": [
            {
                "name": r["name"],
                "level": r["level_label"],
                "directive": r["directive"],
                "why": r["reason"],
                "expected_loss_chf": r["exposure_chf"],
                "shipments_at_risk": r["shipments_at_risk"],
                "hours_until_a_decision_is_needed": r["lead_time_hours"],
            }
            for r in board["routes"]
        ],
    }
    if route_id:
        route = next(
            (r for r in board["routes"] if r["route_id"] == route_id), None
        )
        if route:
            context["selected_route"] = {
                "name": route["name"],
                "level": route["level_label"],
                "why": route["reason"],
                "events": [_event_context(e) for e in route.get("events", [])],
                "actions": [
                    {
                        "what": a["label"],
                        "costs_chf": a["cost_chf"],
                        "avoids_chf": a.get("avoids_chf"),
                        "decide_by": a.get("deadline_text"),
                    }
                    for a in route.get("actions", [])
                ],
                "who_to_contact": route.get("response", {}).get("route_manager"),
            }
    return _ask(context, question)


def _ask(context: dict, question: str) -> dict:
    status = llm.detect()
    if not status.available:
        return _no_model(status)

    prompt = (
        "CONTEXT (the planner's current board):\n"
        f"{json.dumps(context, indent=1, default=str)}\n\n"
        f"QUESTION: {question.strip()}"
    )
    answer = llm.ask_text(SYSTEM, prompt, status)
    if answer is None:
        return _no_answer(
            "The model did not answer. Everything on the board was computed "
            "without it and is unaffected."
        )
    return {
        "answered": True,
        "answer": answer,
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
