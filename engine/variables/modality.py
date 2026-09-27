"""Has it happened, might it, or is it over? Read from the words around it.

The keyword router finds a Hormuz closure in "Iran threatens to close the
Strait of Hormuz" and in "Iran closes the Strait of Hormuz" alike — it answers
WHAT the text is about, not whether the text says it is so. Without this, a
threat went on the board priced as the closure itself, with the full delay of
its family, from the moment it was reported.

This is the ConText algorithm (Harkema, Dowling, Thornblade and Chapman,
Journal of Biomedical Informatics, 2009): the standard rule-based way of
reading clinical notes for whether a finding is asserted, hypothetical or
negated, and the same question a freight report poses. A trigger phrase
modifies the concept after it — or before it, for a post-trigger — up to the
end of its clause, and a termination term ("but", "however", "which"...) ends
the clause early:

    Iran  [threatens to]  close the Strait of Hormuz     hypothetical
    Dockworkers vote to strike from Monday  [unless]      hypothetical
    The strike at Antwerp was  [called off]               ended
    Iran's [proposal to] [reopen] the strait              asserted: it is the
                                                          ENDING that is
                                                          hypothetical

Deliberately small and deliberately conservative about "ended", the one
reading that removes something from the board: only completed forms count
("called off", "reopened", "lifted"), never present ones ("the closure ends
on Friday" states an end, it does not report one).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.variables.rules import PATTERNS

_I = re.IGNORECASE

# Triggers that make what follows them hypothetical, future or conditional.
_HYPOTHETICAL_PRE = [re.compile(p, _I) for p in (
    r"\bthreat(?:s|en|ens|ened|ening)?\b",
    r"\bwarn(?:s|ed|ing)?\b",
    r"\bplan(?:s|ned|ning)? to\b",
    r"\bconsider(?:s|ed|ing)?\b",
    r"\b(?:could|might|would|will)\b",
    r"\bpropos(?:al|als|e|ed|es|ing)\b",
    r"\bvot(?:e|es|ed|ing) on\b",
    r"\bballot(?:s|ed|ing)?\b",
    r"\bpossib(?:le|ly|ility)\b",
    r"\bpotential(?:ly)?\b",
    r"\brisk of\b",
    r"\bfear(?:s|ed)?\b",
    r"\b(?:expected|set|poised|likely|about) to\b",
    r"\bultimatum\b",
    r"\b(?:if|unless)\b",
    r"\bloom(?:s|ing)?\b",
    r"\bimpending\b",
    r"\bon the brink\b",
    r"\bprepar(?:e|es|ed|ing) for\b",
    r"\bcall(?:s|ed)? for\b",
    r"\burg(?:e|es|ed|ing)\b",
)] + [
    # "may" the verb, not May the month: lower case, and not before a date.
    re.compile(r"\bmay\b(?!\s+\d)"),
]
_HYPOTHETICAL_POST = [re.compile(p, _I) for p in (
    r"\b(?:unless|if)\b",
    r"\b(?:is|are|was|were) (?:threatened|feared|possible|expected|likely|proposed|looming)\b",
    r"\bloom(?:s|ing)?\b",
    r"\bwarnings?\b",
    r"\bthreats?\b",
)]

# Triggers that say it is over. Completed forms only.
_ENDED_PRE = [re.compile(p, _I) for p in (
    r"\bcall(?:s|ed) off\b",
    r"\b(?:lifted|reopened|restored)\b",
    r"\bno longer\b",
    r"\brepairs? (?:completed|finished)\b",
)]
_ENDED_POST = [re.compile(p, _I) for p in (
    r"\bcalled off\b",
    r"\baverted\b",
    r"\b(?:lifted|ended|reopened|restored|resolved|completed|cancel(?:l)?ed)\b",
    r"\b(?:is|was|are|were) over\b",
)]

_TERMINATION = re.compile(
    r"\b(?:but|however|although|though|whereas|yet|except|which|who|while)\b", _I)
# A sentence ends at . ! ? ; before a capital, a quote or the end — but not
# after a capital, so "U.S. sanctions" stays one sentence.
_SENTENCE_END = re.compile(r"(?<![A-Z])[.!?;](?=\s+[A-Z\"'(]|\s*$)")

# Words that make a concept a warning by its own name: "gale warning",
# "strike notice", "strike threat".
_HYPOTHETICAL_IN_CONCEPT = re.compile(r"\b(?:warning|notice|threat|ballot)s?\b", _I)


@dataclass(frozen=True)
class Reading:
    status: str      # "asserted" | "hypothetical" | "ended"
    cue: str = ""    # the trigger, as written in the text

    @property
    def hypothetical(self) -> bool:
        return self.status == "hypothetical"

    @property
    def ended(self) -> bool:
        return self.status == "ended"


ASSERTED = Reading("asserted")

_COMPILED = {vid: [re.compile(p, _I) for p in patterns] for vid, patterns in PATTERNS.items()}


def _concept(text: str, variable_ids: list[str]) -> tuple[int, int] | None:
    """Where the event is named: the first variable, in the router's order,
    whose pattern matches — the one that decided the event's family."""
    for vid in variable_ids:
        for pattern in _COMPILED.get(vid, []):
            match = pattern.search(text)
            if match:
                return match.start(), match.end()
    return None


def _sentence(text: str, at: int) -> tuple[int, int]:
    start, end = 0, len(text)
    for match in _SENTENCE_END.finditer(text):
        if match.end() <= at:
            start = match.end()
        elif match.start() >= at:
            end = match.start()
            break
    return start, end


def _clear(text: str, a: int, b: int) -> bool:
    """No termination term between a and b."""
    return a <= b and not _TERMINATION.search(text, a, b)


def _before(patterns, text: str, lo: int, hi: int) -> list[re.Match]:
    """Pre-triggers in [lo, hi) with a clear run to hi."""
    return [m for p in patterns for m in p.finditer(text, lo, hi) if _clear(text, m.end(), hi)]


def _after(patterns, text: str, lo: int, hi: int) -> list[re.Match]:
    """Post-triggers in (lo, hi] with a clear run back to lo."""
    return [m for p in patterns for m in p.finditer(text, lo, hi) if _clear(text, lo, m.start())]


def read(text: str, variable_ids: list[str]) -> Reading:
    """Asserted, hypothetical or ended — for the concept the router matched."""
    if not text or not variable_ids:
        return ASSERTED
    found = _concept(text, variable_ids)
    if found is None:
        return ASSERTED
    start, end = found
    lo, hi = _sentence(text, start)

    named = _HYPOTHETICAL_IN_CONCEPT.search(text, start, end)
    if named:
        return Reading("hypothetical", named.group(0))

    ended = _before(_ENDED_PRE, text, lo, start) + _after(_ENDED_POST, text, end, hi)
    if ended:
        cue = min(ended, key=lambda m: abs(m.start() - start))
        # "proposal to reopen the strait", "the strike could be called off":
        # the ENDING is hypothetical, so the thing itself still stands. Only a
        # trigger on the ending's own side counts — in "the threatened strike
        # was called off" the threat is the strike's, and the strike is over.
        since = end if cue.start() >= end else lo
        if _before(_HYPOTHETICAL_PRE, text, since, cue.start()):
            return ASSERTED
        return Reading("ended", cue.group(0))

    hypothetical = _before(_HYPOTHETICAL_PRE, text, lo, start) + _after(
        _HYPOTHETICAL_POST, text, end, hi)
    if hypothetical:
        cue = min(hypothetical, key=lambda m: abs(m.start() - start))
        return Reading("hypothetical", cue.group(0))
    return ASSERTED
