"""Text in, node ids out — deterministically, for free.

WHY THIS EXISTS
===============
A wire story says "the Strait of Hormuz". It does not say ``CHOKE_HORMUZ``, it
carries no latitude, and no newsroom on earth will ever print a UN/LOCODE. So
a text source arrives with nothing the geographic filter can use, and without
this module every GDELT item would be dropped at layer 1 for having no
position — the noise filter would reject the entire feed as noise.

WHY IT IS NOT A MODEL
=====================
This is a lookup. The network has 36 nodes with known names; the question "does
this sentence mention one of them" is a string search, and a string search is
free, instant, reproducible and auditable. Spending a model call on it would be
paying for a worse answer — and, because the geographic filter runs before the
funnel, paying it tens of thousands of times a day.

The model's job starts where this ends: this finds that a text mentions Hormuz.
Working out that a closed Hormuz strands freight at Jebel Ali — a port the text
never mentions — is the inference, and that is stage 2's.

WHAT IT REFUSES TO DO
=====================
Guess. A match must be a whole-word hit on a name or a declared alias. "Basel"
matches the node; "Baselworld" does not. Substring matching would make "Genoa"
match "Genoan", "Suez" match "Suezmax" — a vessel class, not a canal — and
every false node here becomes a false event on somebody's board.

Ambiguous names are handled by NOT listing them. There is no alias for "Port"
or "Gulf", and a text mentioning only those resolves to nothing, which is the
correct answer: the geographic filter then drops it, and if it mattered it will
be reported again by a source that names the place.
"""

from __future__ import annotations

import re
from functools import lru_cache

from engine.config import Config

# Names journalists use that are not the node's own name. Kept short and
# specific on purpose — every entry here is a claim that this phrase means
# this node, and a wrong one manufactures events.
ALIASES: dict[str, tuple[str, ...]] = {
    "CHOKE_HORMUZ": ("hormuz", "strait of hormuz", "persian gulf", "arabian gulf"),
    "CHOKE_SUEZ": ("suez", "suez canal"),
    "CHOKE_BAB": ("bab el-mandeb", "bab al-mandab", "red sea", "gulf of aden"),
    "CHOKE_MALACCA": ("malacca", "strait of malacca", "malacca strait"),
    "CHOKE_GOODHOPE": ("cape of good hope", "cape route"),
    "NLRTM": ("rotterdam", "port of rotterdam", "maasvlakte"),
    "BEANR": ("antwerp", "antwerpen", "anvers", "port of antwerp"),
    "DEHAM": ("hamburg", "port of hamburg"),
    "FRLEH": ("le havre",),
    "ESVLC": ("valencia",),
    "ESBCN": ("barcelona",),
    "ITGOA": ("genoa", "genova"),
    "ITSPE": ("la spezia",),
    "GBFXT": ("felixstowe",),
    "GBSOU": ("southampton",),
    "CNSHA": ("shanghai",),
    "CNNGB": ("ningbo", "ningbo-zhoushan"),
    "SGSIN": ("singapore",),
    "MYPKG": ("port klang", "klang"),
    "AEJEA": ("jebel ali", "dubai"),
    "USNYC": ("new york", "new jersey", "newark"),
    "USORF": ("norfolk",),
    "USHOU": ("houston",),
    "USLAX": ("los angeles", "long beach", "san pedro"),
    "DEDUI": ("duisburg", "duisport"),
    "GAUGE_KAUB": ("kaub",),
    "CHBSL": ("basel", "basle"),
}

# Phrases that look like a place and are not one. Checked before aliases, so a
# text containing only these resolves to nothing.
NOT_A_PLACE = (
    "suezmax", "panamax", "aframax", "capesize", "baselworld",
    "basel iii", "basel ii", "basel convention", "new york times",
)


@lru_cache(maxsize=64)
def _pattern_for(phrase: str) -> re.Pattern:
    # Whole-phrase, word-bounded. Hyphens and spaces inside a phrase are
    # matched literally so "bab el-mandeb" does not need its own variants.
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


def resolve_nodes(text: str, config: Config) -> list[str]:
    """Node ids this text names. Empty when it names none — a real answer."""
    if not text:
        return []

    haystack = " ".join(text.split()).lower()
    for decoy in NOT_A_PLACE:
        haystack = haystack.replace(decoy, " ")

    found: list[str] = []
    for node_id, node in config.nodes.items():
        phrases = set(ALIASES.get(node_id, ()))
        name = (getattr(node, "name", "") or "").strip().lower()
        # The node's own name counts, but only if it is distinctive enough to
        # be worth matching. Two characters would match everything.
        if len(name) >= 4:
            phrases.add(name)
        if any(_pattern_for(p).search(haystack) for p in phrases if p):
            found.append(node_id)

    return sorted(set(found))


def enrich(items: list[dict], config: Config) -> int:
    """Fill node_hint on every item that has neither coordinates nor a hint.

    Returns how many were resolved. An item already carrying a hint from its
    source keeps it — a feed that told us the UN/LOCODE knows better than a
    string search over its own headline.
    """
    resolved = 0
    for item in items:
        if item.get("node_hint"):
            continue
        hints = resolve_nodes(item.get("text") or item.get("headline", ""), config)
        if hints:
            item["node_hint"] = hints
            item["node_hint_basis"] = "place names found in the text"
            resolved += 1
    return resolved
