"""Sources whose answer is not yet a list of items.

Most sources answer with JSON that already holds a list, and the mapper finds
it by path. Wikipedia's Current events portal answers with a page of wikitext.
A decoder turns an answer like that into the list the mapper expects. The spec
names its decoder (``SourceSpec.decode``), so the spec stays declarative and
the decoding code stays in the one module that knows the source.

A decoder may also say that an answer is a REFUSAL — an API that replies 200
with an error in the body. The fetcher treats that as a failed request, so it
is retried and reported, not read as a quiet day.
"""

from __future__ import annotations

from typing import Any

from engine.ingest.sources import wikipedia

DECODERS = {"wikipedia_current_events": wikipedia.decode}
REFUSALS = {"wikipedia_current_events": wikipedia.refusal}


def decoded(spec, blob: Any) -> Any:
    """The answer as the mapper reads it."""
    if not spec.decode:
        return blob
    if spec.decode not in DECODERS:
        raise KeyError(f"{spec.key}: no decoder named {spec.decode!r}")
    return DECODERS[spec.decode](blob)


def refused(spec, blob: Any) -> str | None:
    """Why this answer is a refusal, or None when it is an answer."""
    check = REFUSALS.get(spec.decode) if spec.decode else None
    return check(blob) if check else None
