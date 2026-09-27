"""The German motorway closure feed, read for WHEN each closure is.

verkehr.autobahn.de answers ``/{road}/services/closure`` with every closure on
that road. Two things about it matter here, and neither is in a field:

**Every item is a closure.** The endpoint lists nothing else, so the spec
declares the variable (``declares=("INF_ROAD_CLOSURE",)``) instead of hoping
the keyword router spots English words in German titles. It could not: "A3 |
Sandgraben - Würzburg/Kist" says nothing but the road and two junctions, and
before this every real closure went to the model to be recognised as one.

**The times are prose.** ``startTimestamp`` is often missing and there is no
end field at all; the window is written in the description, in German, in
German local time:

    Beginn: 05.10.26 um 00:00 Uhr / Ende: 27.11.26 um 18:00 Uhr
    27.09.26 von 00:00 bis 06:00 Uhr
    27.09.26 17:00 bis zum 28.09.26 06:00 Uhr.
    (Ende der Gesamtmaßnahme: 27.09.26)

Without reading it, a six-hour night closure had no end and was given the
mapper's default week — a week-long motorway closure on a "real data" board
that closed for one night. Several windows (a closure over three nights) are
read as their envelope: first start to last end, and the body lists the
windows so the planner sees they are nights, not one long stoppage.

The closure is also PLANNED: an item may start next month. It is known now,
which is the lead time a planner wants, so nothing here is dated "published"
by its start — see the spec in catalog.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

_DAY = r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})"
_TIME = r"(\d{1,2}):(\d{2})"

_BEGIN = re.compile(rf"Beginn:\s*{_DAY}(?:\s*um)?\s*{_TIME}", re.IGNORECASE)
_END = re.compile(rf"Ende:\s*{_DAY}(?:\s*um)?\s*{_TIME}", re.IGNORECASE)
_SAME_DAY = re.compile(rf"{_DAY}\s*von\s*{_TIME}\s*bis\s*{_TIME}", re.IGNORECASE)
_SPAN = re.compile(rf"{_DAY}\s*{_TIME}\s*bis\s*(?:zum\s*)?{_DAY}\s*{_TIME}", re.IGNORECASE)
_WHOLE = re.compile(rf"Ende der Gesamtma(?:ß|ss)nahme:\s*{_DAY}", re.IGNORECASE)

KIND = {
    "CLOSURE": "carriageway closed",
    "CLOSURE_ENTRY_EXIT": "junction entry/exit closed",
}


def _last_sunday(year: int, month: int) -> datetime:
    last = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def berlin(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """German local time as an aware datetime: CEST from the last Sunday of
    March to the last Sunday of October, CET otherwise. The EU rule, written
    out rather than read from a time-zone database, which a Windows Python
    does not ship."""
    naive = datetime(year, month, day, hour, minute)
    summer = _last_sunday(year, 3).replace(hour=2) <= naive < _last_sunday(year, 10).replace(hour=3)
    return naive.replace(tzinfo=timezone(timedelta(hours=2 if summer else 1)))


def _moment(day: str, month: str, year: str, hour: str = "0", minute: str = "0") -> datetime:
    y = int(year)
    y = y + 2000 if y < 100 else y
    return berlin(y, int(month), int(day), int(hour), int(minute))


def windows(lines: list[str]) -> list[tuple[datetime, datetime]]:
    """Every (start, end) the description states, in UTC order."""
    text = " ".join(str(line) for line in lines or [])
    found: list[tuple[datetime, datetime]] = []
    for m in _SPAN.finditer(text):
        found.append((_moment(m[1], m[2], m[3], m[4], m[5]),
                      _moment(m[6], m[7], m[8], m[9], m[10])))
    for m in _SAME_DAY.finditer(text):
        start = _moment(m[1], m[2], m[3], m[4], m[5])
        end = _moment(m[1], m[2], m[3], m[6], m[7])
        if end <= start:                     # "22:00 bis 05:00" runs past midnight
            end += timedelta(days=1)
        found.append((start, end))
    begin, finish = _BEGIN.search(text), _END.search(text)
    if begin and finish:
        found.append((_moment(*begin.groups()), _moment(*finish.groups())))
    return sorted({(s.astimezone(UTC), e.astimezone(UTC)) for s, e in found if e > s})


def whole_end(lines: list[str]) -> datetime | None:
    """"Ende der Gesamtmaßnahme: 27.09.26" — the end of that day."""
    m = _WHOLE.search(" ".join(str(line) for line in lines or []))
    if not m:
        return None
    return (_moment(m[1], m[2], m[3]) + timedelta(days=1)).astimezone(UTC)


def _prose(lines: list[str]) -> str:
    """The first line that says something other than a date: not a header
    ("Zeitraum dieser Bauphase:"), not a window, not the overall end."""
    for line in lines or []:
        text = str(line).strip()
        if not text or text.endswith(":"):
            continue
        if _SPAN.search(text) or _SAME_DAY.search(text) or _WHOLE.search(text):
            continue
        if _BEGIN.search(text) or _END.search(text):
            continue
        return text
    return ""


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def decode(blob: Any) -> Any:
    """The answer with each closure's window read out of its prose.

    Adds ``_starts``, ``_ends`` and ``_body`` to every closure and changes
    nothing else, so the raw answer is still there to read.
    """
    if not isinstance(blob, dict) or not isinstance(blob.get("closure"), list):
        return blob
    out = []
    for raw in blob["closure"]:
        if not isinstance(raw, dict):
            continue
        closure = dict(raw)
        lines = closure.get("description") or []
        spans = windows(lines)
        stated = closure.get("startTimestamp")
        start = spans[0][0] if spans else None
        if start is None and stated:
            try:
                start = datetime.fromisoformat(str(stated).replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                start = None
        end = max((e for _s, e in spans), default=None) or whole_end(lines)
        closure["_starts"] = _iso(start)
        closure["_ends"] = _iso(end)

        where = str(closure.get("subtitle") or "").strip()
        kind = KIND.get(str(closure.get("display_type") or ""), "closure")
        when = "; ".join(f"{s:%d %b %H:%M}–{e:%d %b %H:%M} UTC" for s, e in spans[:4])
        if len(spans) > 4:
            when += f"; and {len(spans) - 4} more"
        closure["_body"] = " · ".join(p for p in (where, kind, when, _prose(lines)) if p)
        out.append(closure)
    return {**blob, "closure": out}
