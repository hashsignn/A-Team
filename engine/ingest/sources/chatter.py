"""Social and forum chatter: worthless alone, useful in volume.

One post saying a lock is shut is not evidence. It is a person, possibly
wrong, possibly quoting a rumour, possibly describing last week. Feeding that
into the funnel puts a model call and a planner's attention behind something
that would embarrass both.

Forty posts from thirty different accounts, inside six hours, all naming the
same place and the same kind of disruption, is a different object. Nothing
about any individual post changed; the SHAPE changed, and shape is measurable
without reading a word.

So chatter is gated on volume before it is allowed to cost anything:

    every post        →  bucket by (place, disruption kind, time window)
    bucket            →  count DISTINCT authors, not posts
    over threshold    →  one synthesised item enters the funnel
    under threshold   →  nothing enters, and the count is still reported

DISTINCT AUTHORS, NOT POSTS
---------------------------
The whole failure mode of social as a source is amplification: one account
posting forty times, or forty accounts reposting one. Counting posts makes
both look like corroboration. Counting distinct authors makes the first
harmless, and a repost is only counted once because it carries the original's
id.

WHY THIS IS NOT A MODEL
-----------------------
For the same reason the rest of the noise filter is not: it has to be
reproducible in a hindcast, it has to cost nothing per item, and a model that
silently drops something leaves no trace. A threshold leaves a number.

WHAT COMES OUT
--------------
One item per bucket that clears the bar, carrying the count and the window so
the board can say "31 accounts in 4 h" rather than asserting a fact. It enters
at tier 3 and stays there: volume is a reason to LOOK, never a reason to
believe. Promotion to tier 2 happens the same way it always does — when an
independent source says the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.ingest.sources.places import ALIASES

# Defaults. Overridable per source in sources.yaml, because a niche forum for
# Rhine skippers and a global microblog do not deserve the same bar.
DEFAULT_MIN_AUTHORS = 12
DEFAULT_WINDOW_HOURS = 6.0

# The disruption vocabulary chatter is bucketed by. Deliberately coarse: the
# bucket only has to be specific enough that two unrelated events do not pool
# into one. The precise reading is the extraction model's job, later, and only
# for buckets that cleared the bar.
KINDS: dict[str, tuple[str, ...]] = {
    "blocked": ("blocked", "closed", "shut", "gesperrt", "stilgelegd",
                "fermé", "chiuso", "cerrado"),
    "strike": ("strike", "stoppage", "walkout", "streik", "staking",
               "grève", "sciopero", "huelga"),
    "congestion": ("queue", "backlog", "congestion", "delays", "stau",
                   "wachttijd", "embouteillage"),
    "weather": ("storm", "flood", "low water", "ice", "hochwasser",
                "niedrigwasser", "sturm"),
    "incident": ("fire", "collision", "derail", "aground", "explosion",
                 "brand", "unfall", "ongeval"),
}


@dataclass(frozen=True)
class Post:
    """One thing somebody said. Normalised before it reaches here."""

    post_id: str
    author: str
    text: str
    at: datetime
    # The id of the post this one repeats, if any. A repost is the same claim
    # travelling, not a second person making it.
    repost_of: str | None = None
    url: str | None = None


@dataclass
class Bucket:
    place: str
    kind: str
    window_start: datetime
    window_end: datetime
    authors: set[str] = field(default_factory=set)
    posts: list[Post] = field(default_factory=list)
    originals: set[str] = field(default_factory=set)

    @property
    def author_count(self) -> int:
        return len(self.authors)

    @property
    def amplification(self) -> float:
        """Posts per distinct author. High means one voice, loudly."""
        return round(len(self.posts) / max(1, self.author_count), 2)


def classify(text: str) -> str | None:
    """Which disruption vocabulary this post uses, if any."""
    low = text.lower()
    for kind, words in KINDS.items():
        if any(word in low for word in words):
            return kind
    return None


def places_in(text: str) -> list[str]:
    """Node ids the post names, using the same alias table as everything else.

    Shared deliberately: a place the gate can act on and a place chatter can
    be bucketed by have to be the same set, or a spike accumulates against a
    name nothing downstream recognises.
    """
    low = text.lower()
    found: list[str] = []
    for node_id, names in ALIASES.items():
        for name in names:
            token = name.lower()
            if len(token) < 3:
                continue
            if f" {token} " in f" {low} " or low.startswith(token) or low.endswith(token):
                if node_id not in found:
                    found.append(node_id)
                break
    return found


def bucket(
    posts: list[Post],
    window_hours: float = DEFAULT_WINDOW_HOURS,
) -> list[Bucket]:
    """Group posts by (place, kind, window). One post can land in several.

    A post naming two places is evidence about both. Splitting it is correct;
    the alternative is picking one and being wrong half the time.
    """
    window = timedelta(hours=window_hours)
    buckets: dict[tuple[str, str, int], Bucket] = {}

    for post in sorted(posts, key=lambda p: p.at):
        kind = classify(post.text)
        if kind is None:
            continue
        for place in places_in(post.text):
            # Fixed windows keyed off the epoch rather than a sliding window:
            # a sliding one makes the same corpus produce different buckets
            # depending on which post you start from, and a hindcast has to be
            # reproducible.
            slot = int(post.at.timestamp() // window.total_seconds())
            key = (place, kind, slot)
            entry = buckets.get(key)
            if entry is None:
                start = datetime.fromtimestamp(
                    slot * window.total_seconds(), tz=post.at.tzinfo)
                entry = Bucket(place, kind, start, start + window)
                buckets[key] = entry
            entry.posts.append(post)
            # A repost carries the original's author, not the reposter's.
            # Counting the reposter would let one claim manufacture its own
            # corroboration.
            entry.originals.add(post.repost_of or post.post_id)
            if post.repost_of is None:
                entry.authors.add(post.author)

    return sorted(
        buckets.values(),
        key=lambda b: (-b.author_count, b.place, b.kind),
    )


def promote(
    buckets: list[Bucket],
    min_authors: int = DEFAULT_MIN_AUTHORS,
) -> tuple[list[dict], dict]:
    """Buckets over the bar become items; the rest become a number.

    Returns ``(items, stats)``. The stats are not decoration: "nothing crossed
    the threshold today" and "the source is dead" look identical without them,
    and only one of those is a reason to worry.
    """
    items: list[dict] = []
    below = 0

    for entry in buckets:
        if entry.author_count < min_authors:
            below += 1
            continue
        sample = entry.posts[0]
        items.append({
            "id": f"chatter:{entry.place}:{entry.kind}:"
                  f"{int(entry.window_start.timestamp())}",
            # Phrased as what it IS — a spike — not as what it claims. The
            # board must never render this as "the lock is shut".
            "title": (
                f"{entry.author_count} accounts reporting "
                f"{entry.kind.replace('_', ' ')} near {entry.place} "
                f"in {_hours(entry)} h"
            ),
            "text": sample.text[:400],
            "at": entry.window_start.isoformat(),
            "place_hint": entry.place,
            "kind": entry.kind,
            "authors": entry.author_count,
            "posts": len(entry.posts),
            "amplification": entry.amplification,
            "url": sample.url,
            # Tier 3 and it stays there. Volume is a reason to look, never a
            # reason to believe.
            "source_tier": 3,
            "unverified": True,
        })

    return items, {
        "buckets": len(buckets),
        "promoted": len(items),
        "below_threshold": below,
        "min_authors": min_authors,
        "note": (
            f"{len(items)} of {len(buckets)} chatter bucket(s) reached "
            f"{min_authors} distinct accounts. The rest are counted and "
            "discarded without costing a model call."
            if buckets else
            "No chatter bucketed. Either nothing is being said, or the source "
            "is not connected — the source panel says which."
        ),
    }


def _hours(entry: Bucket) -> int:
    return max(1, round((entry.window_end - entry.window_start).total_seconds() / 3600))
