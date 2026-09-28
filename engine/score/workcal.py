"""The working clock: time left to act, with weekends and public holidays out.

Sika's rungs (8 h, 24-36 h, 3 days, 5 days) are counted in WORKING time. A
barge booking that closes Monday morning, seen on Friday evening, is not
"60 hours away": nobody books over the weekend, so the team has the hours
left on Friday and Monday morning, and the ladder has to say so.

So the lead time the ladder reads is

    working hours between now and the decision deadline

where a Saturday, a Sunday or a public holiday of the team's country counts
zero and every other day counts in full. Days are cut in the team's local
time (Central European, with EU summer time), not in UTC, because a holiday
starts at local midnight.

The deadline itself is not moved. The option still closes when it closes;
only the time the team has to act on it is counted honestly.

No holiday library and no timezone database: the rules are in
config.example/scoring.yaml (``working_calendar``) as fixed dates and Easter
offsets, and Easter is computed. That keeps the engine free of a dependency
that Windows machines often lack.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from engine.config import Config

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Holidays that move with Easter, as days after Easter Sunday.
_EASTER_OFFSETS = {
    "good_friday": -2,
    "easter_monday": 1,
    "ascension": 39,
    "whit_monday": 50,
    "corpus_christi": 60,
}


def easter(year: int) -> date:
    """Easter Sunday (Gregorian), by the anonymous algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _last_sunday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def utc_offset_hours(moment: datetime, base: float = 1.0, summer_time: bool = True) -> float:
    """Central European offset: +1, or +2 between the last Sundays of March
    and October at 01:00 UTC (the EU rule)."""
    if not summer_time:
        return base
    y = moment.year
    start = datetime.combine(_last_sunday(y, 3), datetime.min.time(), UTC) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(y, 10), datetime.min.time(), UTC) + timedelta(hours=1)
    return base + 1.0 if start <= moment.astimezone(UTC) < end else base


def _spec(config: Config) -> dict:
    return config.scoring.get("working_calendar", {}) or {}


def enabled(config: Config) -> bool:
    return bool(_spec(config).get("enabled", False))


@lru_cache(maxsize=256)
def _holidays(rules: tuple[str, ...], year: int) -> frozenset[date]:
    out: set[date] = set()
    sunday = easter(year)
    for rule in rules:
        rule = str(rule).strip().lower()
        if rule in _EASTER_OFFSETS:
            out.add(sunday + timedelta(days=_EASTER_OFFSETS[rule]))
            continue
        try:
            month, day = (int(x) for x in rule.split("-"))
            out.add(date(year, month, day))
        except ValueError:
            continue
    return frozenset(out)


def _calendar(config: Config, country: str | None) -> tuple[frozenset[int], tuple[str, ...], float, bool]:
    spec = _spec(config)
    weekend = frozenset(_WEEKDAYS[d] for d in (spec.get("weekend") or ["sat", "sun"])
                        if d in _WEEKDAYS)
    table = spec.get("holidays") or {}
    key = country if country in table else spec.get("country")
    rules = tuple(str(r) for r in (table.get(key) or []))
    return (weekend, rules, float(spec.get("utc_offset_hours", 1.0)),
            bool(spec.get("summer_time", True)))


def is_working_day(day: date, config: Config, country: str | None = None) -> bool:
    weekend, rules, _, _ = _calendar(config, country)
    return day.weekday() not in weekend and day not in _holidays(rules, day.year)


def working_hours(start: datetime, end: datetime, config: Config,
                  country: str | None = None) -> float:
    """Hours between *start* and *end* that fall on working days.

    A deadline already passed keeps its plain (negative) hours: "passed by
    30 hours" is still the useful statement, and a weekend does not make a
    missed deadline any less missed. With the calendar off this is plain
    elapsed time.
    """
    plain = (end - start).total_seconds() / 3600.0
    if plain <= 0 or not enabled(config):
        return plain
    weekend, rules, base, summer = _calendar(config, country)
    total = 0.0
    cursor = start.astimezone(UTC)
    end = end.astimezone(UTC)
    for _ in range(800):  # a deadline two years out is not a working-day question
        if cursor >= end:
            return total
        shift = timedelta(hours=utc_offset_hours(cursor, base, summer))
        local = cursor + shift
        next_midnight = datetime.combine(local.date() + timedelta(days=1),
                                         datetime.min.time(), UTC) - shift
        stop = min(next_midnight, end)
        day = local.date()
        if day.weekday() not in weekend and day not in _holidays(rules, day.year):
            total += (stop - cursor).total_seconds() / 3600.0
        cursor = stop
    return plain


def skipped(start: datetime, end: datetime, config: Config,
            country: str | None = None) -> list[date]:
    """The non-working days between two instants, for the sentence that
    explains why a deadline 60 hours out reads as 12."""
    if end <= start or not enabled(config):
        return []
    _, _, base, summer = _calendar(config, country)
    shift = timedelta(hours=utc_offset_hours(start, base, summer))
    first = (start.astimezone(UTC) + shift).date()
    last = (end.astimezone(UTC) + shift).date()
    out = []
    day = first
    while day <= last and len(out) < 60:
        if not is_working_day(day, config, country):
            out.append(day)
        day += timedelta(days=1)
    return out
