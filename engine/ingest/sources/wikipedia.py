"""Wikipedia's Current events portal: each day's notable events, curated.

WHY A SECOND NEWS SOURCE
========================
Until this, every headline on the board came from GDELT, and the first real
attempts to record it were refused from both networks tried: HTTP 429 from
one, unreachable from the other. A news layer that rides on a single archive
answering is a news layer with a single point of failure, and it failed at
exactly the moment it was needed.

WHAT IT IS
==========
Every day has a page — "Portal:Current events/2026 September 21" — on which
editors list that day's significant events under fixed headings (armed
conflicts, business and economy, international relations, law, politics),
each line citing the outlet that reported it. It is the opposite trade to
GDELT: dozens of items a day instead of thousands, chosen for significance
rather than matched on keywords, which is close to what "the events of most
effect" means. It is served by the Wikimedia API: free, keyless, and
reachable from almost anywhere.

WHAT IT IS NOT
==============
A logistics feed. A strike at one Antwerp terminal will be in GDELT and may
never reach this page; a closure of Hormuz will be on both. So it sits beside
the index rather than replacing it, and its items go through the same funnel:
the place filter keeps only lines that name a node of ours, and the model
reads what is left.

THE RAW PAGE IS KEPT
====================
The recorder keeps each day exactly as the API returned it, and this module
parses it every time the board loads. The parse was written without a live
page to test against — the build environment blocks the host — so keeping
the raw text means a better parser improves an existing recording without
fetching anything again. A line this parser cannot read is skipped, never
guessed at.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from engine.ingest.sources.spec import MONTH_NAMES as MONTHS

# One page per day, named in English with no zero padding. Declared once and
# used both to ask for a page (the source's window) and to read its date back.
PAGE = "Portal:Current events/{year} {month_name} {day}"
_TITLE = re.compile(r"^Portal:Current events/(\d{4}) ([A-Z][a-z]+) (\d{1,2})$")

# The headings the portal files items under. A line naming one opens it.
CATEGORIES = (
    "Armed conflicts and attacks", "Arts and culture", "Business and economy",
    "Disasters and accidents", "Health and environment",
    "International relations", "Law and crime", "Politics and elections",
    "Science and technology", "Sports",
)
_CANONICAL = {name.lower(): name for name in CATEGORIES}

# Headings nothing on a freight lane can feel. Dropped here, before the funnel,
# the way GDELT's query drops them on the server.
IRRELEVANT = frozenset({"sports", "arts and culture"})

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_BULLET = re.compile(r"^(\*+)\s*(.*)$")
_BOLD_LINE = re.compile(r"^'''\s*(.+?)\s*'''\s*:?$")
_SECTION = re.compile(r"^=+\s*(.+?)\s*=+$")
_EXTERNAL = re.compile(r"\[(https?://[^\s\]]+)(?:\s+([^\]]*))?\]")
_TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")
_FILE = re.compile(r"\[\[(?:File|Image):[^\]]*\]\]", re.I)
_PIPED = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
_LINK = re.compile(r"\[\[([^\]]*)\]\]")


def page_title(day: date) -> str:
    return PAGE.format(year=day.year, month_name=MONTHS[day.month - 1], day=day.day)


def day_of(title: str) -> date | None:
    """The date a page is about, from its title. None for any other page."""
    match = _TITLE.match(title.strip())
    if not match or match.group(2) not in MONTHS:
        return None
    try:
        return date(int(match.group(1)), MONTHS.index(match.group(2)) + 1, int(match.group(3)))
    except ValueError:
        return None


# ---------------------------------------------------------------------
# What the source framework calls
# ---------------------------------------------------------------------
def decode(blob: Any) -> dict:
    """One day's API answer, or a recording of many, as ``{"events": [...]}``.

    A recording is ``{"days": [answer, answer, ...]}``. An event reported on
    two days is kept once, from the first day it appears in.
    """
    answers = blob.get("days") if isinstance(blob, dict) and isinstance(blob.get("days"), list) else [blob]
    events: list[dict] = []
    seen: set[str] = set()
    for answer in answers:
        for event in read_day(answer):
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            events.append(event)
    return {"events": events}


def refusal(blob: Any) -> str | None:
    """An API error worth asking again about, as text; None if the answer is usable.

    A missing page is an answer — nobody wrote that day up — not a refusal.
    """
    if isinstance(blob, dict) and isinstance(blob.get("error"), dict):
        code = str(blob["error"].get("code") or "error")
        return None if code == "missingtitle" else f"Wikipedia API error: {code}"
    return None


def read_day(answer: Any) -> list[dict]:
    """The events on one page, from ``action=parse`` in either format version."""
    parsed = answer.get("parse") if isinstance(answer, dict) else None
    if not isinstance(parsed, dict):
        return []
    day = day_of(str(parsed.get("title", "")))
    text = parsed.get("wikitext")
    if isinstance(text, dict):              # formatversion=1 wraps it
        text = text.get("*")
    if day is None or not isinstance(text, str):
        return []
    return parse_wikitext(text, day)


# ---------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------
def parse_wikitext(text: str, day: date) -> list[dict]:
    """Every cited line under a heading that could touch freight.

    A page is headings and nested bullets. The outer bullets are usually
    topics — "[[Red Sea crisis]]" — and the cited lines under them are the
    news. The topic chain is kept as context, because "the Houthis strike a
    tanker" names no sea, and "Red Sea crisis" does.
    """
    text = _REF.sub("", _COMMENT.sub("", text))
    category = ""
    chain: list[str] = []
    events: list[dict] = []

    for line in (raw.strip() for raw in text.splitlines()):
        if not line:
            continue
        bullet = _BULLET.match(line)
        if bullet is None:
            heading = _heading(line)
            if heading is not None:
                category, chain = heading, []
            continue

        depth = len(bullet.group(1))
        content = bullet.group(2)
        cited = _EXTERNAL.findall(content)
        sentence = clean(content)

        del chain[depth - 1:]
        chain.extend([""] * (depth - 1 - len(chain)))
        chain.append(sentence)

        if not cited or not sentence or category.lower() in IRRELEVANT:
            continue
        url, label = cited[0]
        events.append(_event(sentence, [c for c in chain[:-1] if c], category, url, label, day))

    return events


def _heading(line: str) -> str | None:
    """The heading a line opens, or None if it opens nothing.

    Current pages write ``'''Business and economy'''`` on a line of its own;
    older ones ``;Business and economy``, and a styled one may wrap it in a
    ``<div>``. Any line that is exactly a known heading opens it, however it
    is dressed. A bold, ``;`` or ``==`` line naming a heading this list does
    not know opens that one too, so a new heading is kept rather than filed
    under the one before it.
    """
    bare = re.sub(r"<[^>]+>", "", line).strip()
    section = _SECTION.match(bare)
    bold = _BOLD_LINE.match(bare)
    marked = bool(section or bold or bare.startswith(";"))
    if section:
        name = section.group(1)
    elif bold:
        name = bold.group(1)
    else:
        name = bare.removeprefix(";")
    name = re.sub(r"'{2,}", "", name).strip().rstrip(":").strip()
    if name.lower() in _CANONICAL:
        return _CANONICAL[name.lower()]
    if not marked or not name or any(mark in name for mark in "[{|"):
        return None
    return name


def _event(sentence: str, topics: list[str], category: str, url: str,
           label: str, day: date) -> dict:
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    # A headline, not a paragraph: the mapper joins it to its context with
    # ". ", and a sentence keeping its own full stop would read "..".
    headline = sentence.rstrip(" .")
    return {
        # Keyed on the sentence, so the same line on two days is one event.
        "id": hashlib.sha256(headline.lower().encode("utf-8")).hexdigest()[:12],
        "headline": headline,
        "context": " › ".join(topics),
        "category": category,
        "day": day.isoformat(),
        # The page gives a day, not a time. Midday is the honest middle of it.
        "published": f"{day.isoformat()}T12:00:00Z",
        "url": url,
        "source": host or "wikipedia.org",
        "source_label": (label or "").strip(),
    }


def clean(content: str) -> str:
    """Wikitext to the sentence a reader sees, citations removed."""
    text = _EXTERNAL.sub("", content)
    for _ in range(8):                      # innermost first; templates nest
        stripped = _TEMPLATE.sub(_template_text, text)
        if stripped == text:
            break
        text = stripped
    text = _FILE.sub("", text)
    text = _PIPED.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)        # what a citation leaves
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([.,;:])", r"\1", text)


_KEEP_FIRST = frozenset({
    "ill", "interlanguage link", "interlanguage link multi", "nowrap", "nobr",
    "nobreak", "noitalic", "flag", "abbr", "small",
})
_CURRENCY = {"us$": "US$", "usd": "US$", "€": "€", "eur": "€", "euro": "€",
             "£": "£", "gbp": "£"}
_DASHES = {"ndash": "–", "mdash": "—", "snd": " – ", "spnd": " – ", "nbsp": " "}


def _template_text(match: re.Match) -> str:
    """What a template shows a reader, for the few that carry the sentence.

    Everything else — flags, citation-needed tags, date helpers — shows
    nothing a headline needs, and is dropped.
    """
    parts = [part.strip() for part in match.group(1).split("|")]
    name = parts[0].lower().replace("_", " ")
    args = [part for part in parts[1:] if "=" not in part]
    if name in _DASHES:
        return _DASHES[name]
    if name in _KEEP_FIRST and args:
        return args[0]
    if name == "lang" and len(args) > 1:
        return args[1]
    if name in {"convert", "cvt"} and len(args) > 1:
        return f"{args[0]} {args[1]}"
    if name == "sortname" and len(args) > 1:
        return f"{args[0]} {args[1]}"
    if name in _CURRENCY and args:
        return f"{_CURRENCY[name]}{args[0]}"
    return ""
