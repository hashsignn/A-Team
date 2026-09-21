"""The check mechanism: does the model's answer survive scrutiny?

WHY THIS IS NOT AN LLM JUDGE
============================
The obvious way to check a model is to ask another model. It is also the
worst way available here, for two reasons a planner would spot immediately:
it costs a second call per event, and it fails in exactly the same places as
the first one — both models find the same sentence ambiguous, and two
confident wrong answers look like corroboration.

So every check below is DETERMINISTIC. Each one is a question with a
checkable answer, and a person can audit the whole file in one sitting:

    grounded          is the quote actually in the source text?
    variables_known   are the claimed variable ids in the ledger?
    nodes_known       are the resolved nodes real?
    probability_honest did it invent odds for something the ledger says
                      cannot be sourced?
    delay_plausible   is the delay estimate inside the family's own band?
    agreement         what does the keyword router say about the same text?

THE ONE THAT MATTERS MOST
-------------------------
``grounded``. A fabricated verbatim quote is the most dangerous output this
system can produce, because it is the thing a planner will trust without
checking — it looks like evidence. It is also trivially detectable: the quote
either appears in the source or it does not. Substring matching catches it
every time, costs nothing, and cannot itself hallucinate.

A REJECTED EXTRACTION IS NOT A GAP
----------------------------------
When the model's answer fails, the deterministic router's answer is used. The
board is complete either way; what changes is whether the reasoned layer got
to contribute. That is why the router is called the CHALLENGER and not the
fallback — it always runs, and here it is also the safety net.

ON THE AGREEMENT METRIC, HONESTLY
---------------------------------
Agreement between the router and the model is cheap and worth reporting, but
it is not validation: both were written by the same people from the same
variable list, so their errors correlate. The honest headline metric remains
the hindcast — did we fire before the carrier called. This file measures
internal consistency, which is a floor, not a ceiling.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from engine.config import Config
from engine.schemas import Extraction
from engine.variables import rules as rules_router

# A quote shorter than this proves nothing: "strike" appears in any article
# about a strike, so matching it is not evidence the model read the source.
MIN_QUOTE_CHARS = 24

# How far outside the family's own three-point band a model's delay estimate
# may sit before it is flagged. Generous on purpose — the whole point of the
# reasoning layer is to read "at least ten days" where the family default says
# three, and a tight band would flag exactly the cases worth having.
DELAY_TOLERANCE = 3.0


@dataclass
class Check:
    """One question and its answer, in the words a reviewer would use."""

    name: str
    passed: bool
    detail: str
    blocking: bool = False


@dataclass
class Verdict:
    """What to do with this extraction."""

    accepted: bool
    checks: list[Check] = field(default_factory=list)
    agreement: dict = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.failures if c.blocking]

    @property
    def headline(self) -> str:
        if self.accepted and not self.failures:
            return "Model output passed every check."
        if self.accepted:
            flags = ", ".join(c.name for c in self.failures)
            return f"Model output accepted with flags: {flags}."
        reasons = "; ".join(c.detail for c in self.blocking_failures)
        return f"Model output rejected — {reasons}. Using the rules router."

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "headline": self.headline,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "blocking": c.blocking,
                }
                for c in self.checks
            ],
            "agreement": self.agreement,
        }


# =====================================================================
# The individual checks
# =====================================================================


def _normalise(text: str) -> str:
    """Fold the differences that are not differences.

    A model that reproduces a quote faithfully still routinely swaps a curly
    apostrophe for a straight one or collapses a line break, and rejecting
    that as a fabrication would throw away good extractions and teach nobody
    anything. Case, whitespace, quote glyphs and dashes are folded; words are
    not.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def grounded(extraction: Extraction, source_text: str) -> Check:
    """Is the verbatim quote actually in the source?

    The single most important check in this file. See the module docstring.
    """
    quote = (extraction.verbatim_quote or "").strip()
    if not quote:
        return Check(
            "grounded", False,
            "no verbatim quote supplied, so nothing anchors this reading to "
            "the source",
            blocking=True,
        )
    if len(quote) < MIN_QUOTE_CHARS:
        return Check(
            "grounded", False,
            f"quote is {len(quote)} characters — too short to be evidence "
            f"(minimum {MIN_QUOTE_CHARS})",
            blocking=True,
        )
    if _normalise(quote) not in _normalise(source_text):
        return Check(
            "grounded", False,
            f"quoted text does not appear in the source: {quote[:70]!r}",
            blocking=True,
        )
    return Check("grounded", True, f"quote found in the source ({len(quote)} chars)")


def variables_known(extraction: Extraction, config: Config) -> Check:
    """Every claimed variable has to be one of the ledger's own."""
    unknown = [v for v in extraction.active_variables if v not in config.variables]
    if unknown:
        return Check(
            "variables_known", False,
            f"not in the {len(config.variables)}-variable ledger: "
            f"{', '.join(unknown)}",
            blocking=True,
        )
    if not extraction.active_variables:
        return Check(
            "variables_known", False,
            "no variable activated, so nothing downstream can price this",
            blocking=True,
        )
    return Check(
        "variables_known", True,
        f"{len(extraction.active_variables)} variable(s), all in the ledger",
    )


def variables_justified(extraction: Extraction) -> Check:
    """Each activated variable must carry the sentence that activated it.

    Not blocking: an unexplained-but-correct variable is still useful. It is
    reported because a reviewer reading the board deserves to know which
    activations came with a reason and which arrived bare.
    """
    missing = [
        v for v in extraction.active_variables
        if not (extraction.why_active or {}).get(v, "").strip()
    ]
    if missing:
        return Check(
            "variables_justified", False,
            f"activated without a stated reason: {', '.join(missing)}",
        )
    return Check("variables_justified", True, "every activation carries its reason")


def nodes_known(extraction: Extraction, config: Config) -> Check:
    """Resolved nodes have to exist in the network."""
    unknown = [n for n in extraction.resolved_node_ids if n not in config.nodes]
    if unknown:
        return Check(
            "nodes_known", False,
            f"not in the network: {', '.join(unknown)}",
            blocking=True,
        )
    return Check(
        "nodes_known", True,
        f"{len(extraction.resolved_node_ids)} node(s) resolved"
        if extraction.resolved_node_ids else "no node claimed",
    )


def probability_honest(extraction: Extraction, config: Config) -> Check:
    """Did it invent odds the ledger says cannot be sourced?

    The ledger marks each variable ``probability_sourceable``. A union ballot
    has no published base rate; a model that returns P=0.6 for one has produced
    a number that looks like evidence and is not. Blocking, because this is
    the sibling project's exact failure and the reason this project states
    ``probability_unknown`` everywhere.
    """
    if extraction.probability is None:
        return Check(
            "probability_honest", True,
            "probability left unsourced, which is the honest answer here",
        )
    if not 0.0 <= extraction.probability <= 1.0:
        return Check(
            "probability_honest", False,
            f"probability {extraction.probability} is outside [0, 1]",
            blocking=True,
        )
    unsourceable = [
        v for v in extraction.active_variables
        if v in config.variables and not config.variables[v].probability_sourceable
    ]
    if unsourceable and not extraction.probability_basis.strip():
        return Check(
            "probability_honest", False,
            f"gave P={extraction.probability:.2f} for {', '.join(unsourceable)}, "
            "which the ledger marks unsourceable, with no basis stated",
            blocking=True,
        )
    return Check(
        "probability_honest", True,
        f"P={extraction.probability:.2f}, basis stated",
    )


def delay_plausible(extraction: Extraction, config: Config) -> Check:
    """Is the delay estimate anywhere near the family's own band?

    Deliberately NOT blocking and deliberately wide. Reading "at least ten
    days" out of a sentence where the family default says three is the whole
    value of the reasoning layer — clamping it to the default would delete the
    thing being paid for. This flags an estimate far enough out to be worth a
    human glance, and nothing narrower.
    """
    triple = extraction.delay_days
    if not (triple.optimistic <= triple.likely <= triple.pessimistic):
        return Check(
            "delay_plausible", False,
            f"three-point estimate is not ordered: {triple.optimistic}/"
            f"{triple.likely}/{triple.pessimistic}",
            blocking=True,
        )

    # Compared against the family's worst case across EVERY severity, not the
    # band for the severity a keyword heuristic guessed.
    #
    # That distinction is the whole point. The heuristic reads this Kaub
    # article as "minor" — no casualty words, no "severe" — and the minor
    # waterway band tops out around a day and a half. The model reading
    # "until the weekend at the earliest" out of the same sentence and
    # returning nine days is doing its job, and checking it against the minor
    # band would flag exactly the extractions worth having. Against the
    # family's severe band it passes, and a 900-day reading still does not.
    ceilings = []
    for vid in extraction.active_variables:
        if vid not in config.variables:
            continue
        for severity in ("minor", "moderate", "severe"):
            try:
                ceilings.append(config.delay_triple(vid, severity).pessimistic)
            except KeyError:
                continue
    if not ceilings:
        return Check("delay_plausible", True, "no family band to compare against")

    ceiling = max(ceilings) * DELAY_TOLERANCE
    if triple.pessimistic > ceiling:
        return Check(
            "delay_plausible", False,
            f"pessimistic estimate {triple.pessimistic:.1f} d is more than "
            f"{DELAY_TOLERANCE:g}x the family's own worst case "
            f"({ceiling / DELAY_TOLERANCE:.1f} d) — worth a look",
        )
    return Check(
        "delay_plausible", True,
        f"{triple.optimistic:.1f}/{triple.likely:.1f}/{triple.pessimistic:.1f} d, "
        "within the family band",
    )


def extraction_severity(extraction: Extraction) -> str:
    """Severity the way the keyword router derives it, so both sides of any
    comparison read the same signal.

    Deliberately NOT used to pick the band in ``delay_plausible`` — see the
    comment there for why that would flag the good extractions.
    """
    return rules_router._severity(  # noqa: SLF001 — same package, one definition
        f"{extraction.what_happened} {extraction.delay_reasoning}"
    ).value


# =====================================================================
# The whole check
# =====================================================================


def review(
    extraction: Extraction,
    source_text: str,
    config: Config,
) -> Verdict:
    """Run every check and decide whether to use this extraction.

    Accepted when no BLOCKING check failed. Non-blocking failures travel with
    the verdict as flags rather than silently disappearing: a planner is
    entitled to know that a reading was used and that one thing about it did
    not look right.
    """
    checks = [
        grounded(extraction, source_text),
        variables_known(extraction, config),
        variables_justified(extraction),
        nodes_known(extraction, config),
        probability_honest(extraction, config),
        delay_plausible(extraction, config),
    ]
    rules_result = rules_router.route(source_text, config.variables)
    agreement = rules_router.agreement(rules_result, extraction.active_variables)
    agreement["rules_abstained"] = rules_result.abstained

    return Verdict(
        accepted=not any(c.blocking and not c.passed for c in checks),
        checks=checks,
        agreement=agreement,
    )
