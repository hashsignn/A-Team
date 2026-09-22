"""The funnel: what reaches a model, and what a model is allowed to decide.

THE SHAPE
=========
    ~50,000  items      server-side query        free, zero bytes transferred
       ~400  items      deterministic gate       free, microseconds
       ~200  items      instrument split         free — measured numbers leave here
        ~40  items      STAGE 1  triage          small model, one yes/no
         ~8  items      STAGE 2  extraction      strong model, structured JSON
         ~8  verdicts   deterministic challenge  free, auditable

Each layer is cheaper than the one below it and removes more. That ordering is
the entire cost argument: the expensive reader only ever sees what four free
filters could not dismiss.

WHY THE NOISE FILTER IS NOT A MODEL
===================================
It is tempting to put a model at the top and ask it "is this relevant". Three
reasons not to, in the order they will bite:

1. **Cost.** GDELT alone is tens of thousands of items a day. A call per item
   is a budget conversation; a bounding-box test is microseconds and free.
2. **Reproducibility.** A hindcast has to replay a past day and produce the
   board that day produced. Sampling from a model at the top of the funnel
   makes every historical board un-replayable.
3. **Silence.** This is the one that matters. A deterministic filter that is
   wrong leaves a rule you can read and a count you can see. A model that
   drops an item leaves nothing at all — no trace, no count, no way to find
   out. The most dangerous filter is the one that fails invisibly.

So the noise filter is ``pipeline._to_events``: geography, then type, then
time, all deterministic, all counted on screen. This module is what happens
*after* those, to the handful that survive.

THE TWO MODELS, AND WHY THEY ARE DIFFERENT SIZES
================================================
``STAGE 1 — triage``   answers one question: could this affect the physical
    movement of freight? A 3B model does that about as well as a 70B, because
    it is nearly a classification task. It runs over hundreds of headlines,
    so it must be small or it is the whole bill.

``STAGE 2 — extraction``  reads the survivor properly: what happened, where,
    when, for how long, how likely, which of the 45 ledger variables it
    activates, and what it quotes. This is where "unless talks resume" has to
    be read as a *conditional* rather than a fact, and where a Hormuz closure
    has to be understood to strand freight at Jebel Ali — a port the article
    does not mention. That judgement is worth a big model, and it only runs a
    few times.

Splitting them is cheaper than one mid-sized model doing both, and better at
each end: the small one is faster at the easy question, the big one is not
being spent on headlines about football in Basel.

THE RULES THAT KEEP THIS HONEST
===============================
**Triage fails OPEN.** No model, a timeout, a crash, a refusal to validate —
the item passes to extraction. A filter that deletes evidence when it breaks
is worse than no filter, and this one is optional by design.

**Triage may only REMOVE.** A "yes" from stage 1 is not evidence of anything;
it buys the item a look from stage 2 and nothing more. Extraction and the
deterministic challenger still run in full. Nothing a model says at stage 1
ever reaches the board.

**A budget, always.** Both stages are bounded. A source that suddenly returns
10,000 items must cost a known number of calls, not an open-ended one — and
the overflow is REPORTED, never silently dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.reason import cache, llm

log = logging.getLogger(__name__)

# Bounds. Deliberately small: a demo that makes 400 model calls on a laptop is
# a demo nobody waits for.
MAX_TRIAGE = 120
MAX_EXTRACT = 12


class Triage(BaseModel):
    """Stage 1's whole output. Four fields, and only one of them is load-bearing.

    Kept this small on purpose. A triage schema with ten fields invites the
    small model to do analysis it is not good at, and invites us to believe
    the result. ``relevant`` decides; the rest is for the audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    relevant: bool = Field(
        description="Could this affect the physical movement of freight?"
    )
    reason: str = Field(description="One short clause. Why, or why not.")
    freight_mode: Literal["sea", "rail", "road", "barge", "air", "several", "none"] = (
        Field(description="Which mode this touches, if the text says.")
    )
    confidence: float = Field(ge=0.0, le=1.0)


TRIAGE_SYSTEM = """\
You are a FILTER, not an analyst. You see one news headline at a time.

Answer one question: could this event affect the physical movement of freight
— ships, barges, trains, trucks or aircraft carrying cargo — or the ports,
canals, straits, borders, terminals and roads they pass through?

Say relevant=true for: closures, blockades, strikes, congestion, conflict
affecting shipping lanes, sanctions on carriers, border controls, severe
weather at ports, infrastructure failure, customs stoppages, canal or strait
restrictions, carrier insolvency, blank sailings.

Say relevant=false for: company earnings, sport, entertainment, politics with
no logistics consequence, crime with no transport effect, opinion pieces,
market commentary about prices alone.

WHEN GENUINELY UNSURE, SAY TRUE. A wrong 'true' costs one more model call. A
wrong 'false' deletes a warning nobody will ever know was there.

Judge only the text you are given. Do not use outside knowledge of the event.
"""


@dataclass
class FunnelCost:
    """What the funnel did, in numbers a planner can see and argue with.

    On the screen rather than in a log, because "the model looked at 8 of 412
    items" is the sentence that makes the cost argument, and because a funnel
    whose counts are hidden is indistinguishable from one that is broken.
    """

    seen: int = 0
    instruments: int = 0
    triaged: int = 0
    dropped_by_triage: int = 0
    passed_triage: int = 0
    triage_unavailable: int = 0
    over_triage_budget: int = 0
    over_extract_budget: int = 0
    # Answers that came from a recording rather than a live call. Counted
    # separately because "the model read 11 items" and "we replayed 11 answers
    # a model gave in September" are different claims about the same board.
    replayed: int = 0
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def reached_a_model(self) -> int:
        return self.triaged

    def as_dict(self) -> dict:
        return {
            "seen": self.seen,
            "instruments_no_model": self.instruments,
            "triaged": self.triaged,
            "dropped_by_triage": self.dropped_by_triage,
            "passed_triage": self.passed_triage,
            "triage_unavailable": self.triage_unavailable,
            "over_triage_budget": self.over_triage_budget,
            "over_extract_budget": self.over_extract_budget,
            "replayed": self.replayed,
        }

    def sentence(self) -> str:
        """The line the /inputs panel prints."""
        if not self.seen:
            return "Nothing reached the funnel."
        parts = [f"{self.seen} item(s) survived the deterministic filters"]
        if self.instruments:
            parts.append(f"{self.instruments} were measurements and skipped the model")
        if self.triaged:
            kept = self.passed_triage
            how = (f"{self.triaged} triaged" if not self.replayed
                   else f"{self.triaged} triaged ({self.replayed} replayed "
                        "from a recording)")
            parts.append(f"{how}, {kept} worth a full read")
        if self.triage_unavailable:
            parts.append(
                f"{self.triage_unavailable} passed through untriaged "
                "(no triage model — failing open)"
            )
        if self.over_triage_budget:
            parts.append(
                f"{self.over_triage_budget} over the triage budget, passed through "
                "unfiltered rather than dropped"
            )
        return "; ".join(parts) + "."


def split_by_nature(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(needs_reading, measured).

    An item from an INSTRUMENT source carries a number a threshold table reads
    for free and exactly. Sending it to a model would pay for a worse answer:
    the model has to be *talked out of* re-deriving arithmetic it cannot check.

    Items with no declared nature are treated as needing reading. The synthetic
    corpus predates this field, and defaulting the other way would silently
    route it around the funnel it exists to exercise.
    """
    measured = [i for i in items if i.get("source_nature") == "instrument"]
    needs_reading = [i for i in items if i.get("source_nature") != "instrument"]
    return needs_reading, measured


def triage(
    items: list[dict],
    status: llm.BackendStatus | None = None,
    budget: int = MAX_TRIAGE,
    model_name: str = "",
) -> tuple[list[dict], FunnelCost]:
    """Stage 1. Returns the items worth a full read, and what it cost.

    Removes only. Never annotates an item with anything the board will read —
    the triage verdict is recorded on the item for the audit trail and is
    deliberately not consulted by scoring, the gate or the ladder.
    """
    cost = FunnelCost()
    needs_reading, measured = split_by_nature(items)
    cost.seen = len(items)
    cost.instruments = len(measured)

    status = status or llm.detect()
    # A recording answers the same questions a live model would, so the stage
    # runs when either is available. Without both, it fails open.
    replay = cache.report()["available"]
    if not status.available and not replay:
        # Fail open, loudly in the counts. The board is complete without a
        # model — that is a supported state here, not a degraded one.
        cost.triage_unavailable = len(needs_reading)
        return needs_reading, cost

    kept: list[dict] = []
    for item in needs_reading:
        if cost.triaged >= budget:
            cost.over_triage_budget += 1
            kept.append(item)          # over budget means UNFILTERED, not dropped
            continue

        verdict, origin = llm.parse_with_provenance(
            Triage,
            TRIAGE_SYSTEM,
            _triage_prompt(item),
            status=status,
            model_name=model_name or llm.TRIAGE_MODEL,
            stage="triage",
        )
        cost.triaged += 1
        if origin.startswith("replayed"):
            cost.replayed += 1

        if verdict is None:
            cost.triage_unavailable += 1
            kept.append(item)          # a failed call is not a 'no'
            continue

        item["triage"] = verdict.model_dump()
        # Carried so the board can say "recorded on the 14th" rather than
        # presenting a replay as a live read.
        item["triage_origin"] = origin
        if verdict.relevant:
            cost.passed_triage += 1
            kept.append(item)
        else:
            cost.dropped_by_triage += 1
            cost.reasons[item.get("item_id", "?")] = verdict.reason

    return kept, cost


def _triage_prompt(item: dict) -> str:
    """Headline, source, date. Nothing else.

    Not the body: at stage 1 the extra tokens buy nothing a headline does not
    already decide, and they are the difference between a triage pass costing
    pennies and costing real money over a day's GDELT.
    """
    lines = [f"HEADLINE: {item.get('headline') or item.get('text', '')}"]
    if item.get("source"):
        lines.append(f"SOURCE: {item['source']}")
    if item.get("published_at"):
        lines.append(f"PUBLISHED: {item['published_at']}")
    return "\n".join(lines)


def report(cost: FunnelCost, status: llm.BackendStatus | None = None) -> dict:
    """What /inputs and /api/model render."""
    status = status or llm.detect()
    return {
        "counts": cost.as_dict(),
        "sentence": cost.sentence(),
        "triage_model": llm.TRIAGE_MODEL if status.available else None,
        "extract_model": llm.EXTRACT_MODEL if status.available else None,
        "backend": status.backend.value if status.available else "none",
        "note": (
            "Triage removes only. A 'relevant' verdict buys a full read and "
            "nothing more — extraction and the deterministic challenger run in "
            "full, and no triage output reaches the board."
        ),
    }
