"""Raw JSON in, FeedItem dicts out — driven by the spec, not by code.

WHY A MINI PATH LANGUAGE AND NOT JSONPath
=========================================
A dependency. The base install is seven packages, and the whole of what these
sources need is ``a.b.c``, ``a[0].b`` and a literal. That is forty lines. A
jsonpath library would be a forty-line problem solved by a 4,000-line
dependency, and every planner installing this would carry it forever.

THE RULE FOR A MISSING PATH
---------------------------
A path that does not resolve yields ``None``. It never raises.

That is deliberate and it is the opposite of how the rest of this codebase
treats bad input — ``ingest/reports.py`` refuses loudly, because a driver's
report is the only thing that can unlock a reroute. These feeds are different:
they are somebody else's JSON, they change without warning, and a field
appearing or vanishing in GDELT must not take the board down. A missing
headline drops the item (with a count); a missing latitude just means the item
resolves by node hint instead.

WHAT IS NOT NEGOTIABLE
----------------------
The headline. An item with no text is not a report of anything, and passing it
down the funnel would spend a model call on an empty string.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from engine.clock import UTC
from engine.ingest.sources.decoders import decoded
from engine.ingest.sources.spec import SourceSpec

_INDEX = re.compile(r"^(.*?)\[(\d+)\]$")


# ---------------------------------------------------------------------
# The path language
# ---------------------------------------------------------------------
def resolve(blob: Any, path: str) -> Any:
    """``properties.title`` / ``geometry.coordinates[1]`` / ``const:foo``."""
    if not path:
        return None
    if path.startswith("const:"):
        return path[len("const:"):]

    current = blob
    for part in path.split("."):
        if current is None:
            return None
        match = _INDEX.match(part)
        index: int | None = None
        if match:
            part, index = match.group(1), int(match.group(2))
        if part:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        if index is not None:
            if isinstance(current, (list, tuple)) and -len(current) <= index < len(current):
                current = current[index]
            else:
                return None
    return current


def items_of(blob: Any, path: str) -> list[Any]:
    """The list of raw items. An empty path means the body IS the list."""
    found = blob if not path else resolve(blob, path)
    if isinstance(found, list):
        return found
    if isinstance(found, dict):
        # Some feeds wrap a single result rather than returning a 1-list.
        return [found]
    return []


# ---------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------
def parse_moment(value: Any, fmt: str) -> datetime | None:
    """Every date format these feeds use, and None for anything else.

    Returning None rather than guessing matters: an item whose timestamp we
    cannot read gets the retrieval time, which is honest, instead of 1970,
    which would silently fail the temporal filter and drop the item.
    """
    if value is None:
        return None
    try:
        if fmt == "epoch_ms":
            return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
        if fmt == "epoch_s":
            return datetime.fromtimestamp(float(value), tz=UTC)
        text = str(value).strip()
        if not text:
            return None
        if fmt == "gdelt":
            # 20260921T143000Z — GDELT's own compact stamp.
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if fmt == "compact_date":
            return datetime.strptime(text[:8], "%Y%m%d").replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------
# The mapper
# ---------------------------------------------------------------------
def to_items(
    blob: Any,
    spec: SourceSpec,
    retrieved_at: datetime,
    default_window_days: float = 7.0,
) -> tuple[list[dict], int]:
    """Return (items, dropped). Items are the shape ``pipeline`` already reads.

    ``dropped`` is surfaced rather than swallowed. A source that silently maps
    900 of 1,000 items is a source whose spec is wrong, and the only way anyone
    finds out is if the number is on the screen.
    """
    out: list[dict] = []
    dropped = 0
    fields = spec.fields

    # A source that answers with something other than a list of items says
    # how to read it; for every other source this is the answer unchanged.
    blob = decoded(spec, blob)

    for index, raw in enumerate(items_of(blob, spec.items_path)):
        headline = _text(resolve(raw, fields.headline))
        if not headline:
            dropped += 1
            continue

        body = _text(resolve(raw, fields.body)) or headline
        published = parse_moment(resolve(raw, fields.published), spec.date_format)
        starts = parse_moment(resolve(raw, fields.starts), spec.date_format) or published
        ends = parse_moment(resolve(raw, fields.ends), spec.date_format)

        identifier = _short_id(_text(resolve(raw, fields.identifier)), index)
        node_hint = _as_list(resolve(raw, fields.node_hint))

        out.append({
            "item_id": f"{spec.key.upper()}-{identifier}",
            "headline": headline,
            "body": body,
            "text": f"{headline}. {body}" if body != headline else headline,
            "source": _text(resolve(raw, fields.source_name)) or spec.key,
            "source_tier": spec.source_tier,
            "source_key": spec.key,
            "source_nature": spec.nature.value,
            "source_modes": list(spec.modes),
            "published_at": published or retrieved_at,
            "starts_at": starts or published or retrieved_at,
            "ends_at": ends,
            "lat": _number(resolve(raw, fields.lat)),
            "lon": _number(resolve(raw, fields.lon)),
            "node_hint": node_hint,
            "url": _text(resolve(raw, fields.url)),
            "severity_hint": _text(resolve(raw, fields.severity_hint)),
            "probability": None,
            "probability_basis": (
                "not stated by the source; this feed reports occurrences, not odds"
            ),
            "realized": True,
            "synthetic": False,
            "default_ends_at": (starts or published or retrieved_at)
            + timedelta(days=default_window_days),
        })

    return out, dropped


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _short_id(raw: str, index: int) -> str:
    """A stable, short id, even when the source's identifier is a URL.

    Stable matters more than pretty: the id is what deduplicates an item across
    two polls and what a planner quotes when asking why something is on the
    board. Hashing the URL keeps both properties without putting 90 characters
    of query string into an event id.
    """
    if not raw:
        return f"{index:04d}"
    if len(raw) <= 24 and "/" not in raw:
        return raw
    return hashlib.sha1(raw.encode()).hexdigest()[:10]  # noqa: S324 — an id, not a secret
