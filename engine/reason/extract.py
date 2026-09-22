"""STAGE 2 — the strong model reads one report properly and returns JSON.

WHAT THIS IS FOR, IN ONE CASE
=============================
    "Iran announces closure of the Strait of Hormuz to commercial shipping."

The keyword router can be taught that sentence. It cannot be taught the next
one — *"maritime interdiction regime declared across the Gulf"* means the same
thing and shares no vocabulary with it. A list of patterns only ever catches
the phrasings somebody already thought of, and a shock event is, by
definition, the one nobody thought of.

Worse, the sentence does not mention the thing that matters. Nothing in it
says **Jebel Ali**, and Jebel Ali is where the freight is. Reading "the Gulf is
shut" and concluding "therefore the consignments sitting behind Hormuz are
stranded, and they are stranded rather than delayed because there is no second
route into the Gulf" is a chain of inference over a network the text has never
heard of. That is the judgement this stage is bought for, and it is why it gets
the bigger model.

WHAT COMES BACK IS JSON, AND IT IS CHECKED
==========================================
The model fills ``Extraction`` — a pydantic schema, enforced at the decoder by
Ollama's ``format`` and by ``json_schema`` on the API path, then validated
again on arrival. It cannot return prose, and a field it invents is rejected
by ``extra="forbid"``.

Validating is not believing. Everything that comes back then goes through
``challenge.review()``, which is deterministic: is the verbatim quote actually
present in the source text, are the claimed variable ids real, are the nodes
real, did it invent a probability for something the ledger says cannot be
sourced, is the delay inside the family's own band. A schema stops it being
malformed. The challenger is what stops it being wrong.

WHAT IT IS NOT ALLOWED TO DO
----------------------------
Score. Nothing here touches severity, the ladder, the matrix or the convene
rule. The model's entire job is to turn a sentence into structured claims
about *what happened*; what that is worth is computed, from config, by code a
planner can read. Keeping that line sharp is the difference between a tool
that explains itself and one that says "the AI thinks it is red".
"""

from __future__ import annotations

import logging

from engine.clock import Clock
from engine.config import Config
from engine.reason import llm
from engine.schemas import Extraction

log = logging.getLogger(__name__)

# How many nodes and variables to show the model. Both lists are small enough
# to send whole, which is better than retrieval: a resolver that only sees the
# nodes a keyword search suggested cannot make the Hormuz -> Jebel Ali jump,
# because nothing in the text mentions Jebel Ali.
EXTRACT_SYSTEM = """\
You read one report about a possible supply-chain disruption and return JSON.

RULES, IN ORDER OF HOW MUCH THEY MATTER

1. verbatim_quote MUST be copied character-for-character from the report text
   you are given. Do not paraphrase it, do not tidy it, do not translate it.
   If you cannot find a sentence that supports your reading, return a quote of
   the closest sentence that does exist. A quote that is not in the text is
   the worst output you can produce, because it looks like evidence.

2. Use ONLY variable ids from the ledger given below, and ONLY node ids from
   the network given below. Never invent an id. If nothing fits, return an
   empty list rather than the nearest thing.

3. probability: if the report states odds, or states a condition that decides
   them ("unless talks resume"), give your reading and say so in
   probability_basis. If the report gives you nothing to base odds on, return
   null. Do NOT invent a number to fill the field. An honest null is worth
   more than a confident guess, and the system has a specific path for it.

4. second_order_nodes is where you earn your place. Ask: if this happened,
   which OTHER nodes in the network are affected that the report does not
   mention? A closed strait strands every port behind it. A closed canal adds
   weeks to every route that used it. List those node ids.

5. realized: true if it has already happened, false if it is announced,
   threatened, balloted or forecast. This distinction drives the whole
   lead-time calculation, so be exact about it.

6. delay_days is a three-point estimate in DAYS of delay to a shipment
   passing through an affected node: optimistic, likely, pessimistic, in that
   order of size. Base it on what the report says about duration. If duration
   is genuinely unknown, say so in duration_confidence and give a WIDE band
   rather than a narrow guess — a narrow band on an unknown is a lie the
   simulation will believe.

You are not scoring anything. Do not rate severity, urgency or priority.
Report what happened and what it touches; the system computes the rest.
"""


def build_prompt(item: dict, config: Config, clock: Clock) -> str:
    """The report, the ledger and the network — nothing else.

    No board state, no other events, no history. The extraction is a reading of
    ONE text, and giving the model the current board would let it agree with a
    verdict rather than read the source.
    """
    variables = "\n".join(
        f"  {v.id}  [{v.family}]  {v.name} — {_one_line(v.description)}"
        for v in config.variables.values()
    )
    nodes = "\n".join(
        f"  {n.id}  {n.name} ({n.country}, {getattr(n.kind, 'value', n.kind)})"
        + ("  NO ALTERNATIVE ROUTE" if getattr(n, "chokepoint", False)
           and not getattr(n, "alternatives", None) else "")
        for n in config.nodes.values()
    )
    text = item.get("text") or item.get("headline", "")
    return f"""\
AS OF: {clock.as_of.isoformat()}

REPORT
------
source: {item.get('source', 'unknown')} (tier {item.get('source_tier', 3)})
published: {item.get('published_at', 'unknown')}
text: {text}

RISK LEDGER — the only variable ids you may use
{variables}

NETWORK — the only node ids you may use
{nodes}
"""


def extract(
    item: dict,
    config: Config,
    clock: Clock,
    status: llm.BackendStatus | None = None,
    model_name: str = "",
) -> Extraction | None:
    """One report in, one validated Extraction out, or None.

    None covers every failure identically — no backend, a timeout, invalid
    JSON, a refusal — because the caller's response is the same in all of them:
    fall back to whatever the deterministic router found, which for a rescued
    item is nothing, so the item is dropped and counted. A dropped item that is
    counted is recoverable. A dropped item that is silent is not.
    """
    status = status or llm.detect()
    if not status.available:
        return None
    return llm.parse(
        Extraction,
        EXTRACT_SYSTEM,
        build_prompt(item, config, clock),
        status=status,
        model_name=model_name or llm.EXTRACT_MODEL,
    )


def to_item_fields(extraction: Extraction, item: dict, clock: Clock) -> dict:
    """Fold an Extraction back onto the raw item, in the shape the pipeline reads.

    The extraction's node list is UNIONED with any hint the source already
    carried, never replaced: a source that told us the UN/LOCODE knows better
    than a model inferring it from a place name, and losing that would be a
    downgrade dressed up as an upgrade.
    """
    merged = dict(item)
    merged.update({
        "active_variables": list(extraction.active_variables),
        "node_hint": sorted(
            {*item.get("node_hint", []), *extraction.resolved_node_ids}
        ),
        "second_order_nodes": list(extraction.second_order_nodes),
        "starts_at": extraction.starts_at,
        "ends_at": extraction.ends_at,
        "duration_confidence": extraction.duration_confidence,
        "realized": extraction.realized,
        "probability": extraction.probability,
        "probability_basis": extraction.probability_basis,
        "verbatim_quote": extraction.verbatim_quote,
        "what_happened": extraction.what_happened,
        # optimistic / likely / pessimistic — the names DelayTriple actually
        # uses. This read .low and .high until an end-to-end test ran the
        # rescue path with a stubbed model and it raised AttributeError: a
        # latent break that could only ever have fired on a machine that had
        # a model installed, which is to say not on any machine that ran the
        # test suite.
        "delay_days": [
            extraction.delay_days.optimistic,
            extraction.delay_days.likely,
            extraction.delay_days.pessimistic,
        ],
        "extracted": True,
        "extracted_at": clock.as_of,
    })
    return merged


def _one_line(text: str, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def prompt_size(item: dict, config: Config, clock: Clock) -> int:
    """Rough token budget check, in characters.

    Exposed so a test can assert the prompt stays inside what a 7B can use
    well. A prompt that overflows the window does not fail — it silently
    truncates the ledger, and the model then 'cannot find' variables that were
    cut off, which looks exactly like a model being stupid.
    """
    return len(build_prompt(item, config, clock)) + len(EXTRACT_SYSTEM)


def context_budget(config: Config, clock: Clock) -> dict:
    # clock.as_of, never datetime.now: nothing in engine/ reads the wall
    # clock, and a budget helper is not an exception to that.
    sample = {"text": "x" * 400, "source": "example.com", "source_tier": 2,
              "published_at": clock.as_of}
    chars = prompt_size(sample, config, clock)
    return {
        "chars": chars,
        "approx_tokens": chars // 4,
        "variables": len(config.variables),
        "nodes": len(config.nodes),
        "fits_8k_window": chars // 4 < 7000,
    }
