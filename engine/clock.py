"""The as-of clock.

ONE RULE, AND IT IS LOAD-BEARING: nothing in engine/ ever calls
``datetime.now()``. Every stage receives an explicit as-of instant.

Three things fall out of that discipline, and all three matter:

1. The demo is reproducible. ``--as-of`` pins the run, so what you rehearse is
   what happens on stage.
2. The hindcast is almost free. Replaying August 2022 is just an as-of in the
   past plus feed adapters serving archived data through the identical code
   path. No parallel "backtest mode" to keep in sync.
3. The option-decay curve is expressible at all. R(t) is the pipeline
   evaluated at a sequence of as-of instants; if "now" were implicit there
   would be nothing to sweep.

Everything is timezone-aware UTC internally. Naive datetimes are rejected at
the door rather than silently assumed to be local — the temporal gate is a
datetime comparison, and a naive value mis-fires it by whole hours in a way
nobody notices until the numbers are wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def ensure_utc(value: datetime) -> datetime:
    """Return *value* as an aware UTC datetime, refusing naive input."""
    if value.tzinfo is None:
        raise ValueError(
            f"naive datetime {value!r}: every instant crossing the engine "
            "boundary must carry a timezone. Parse with a UTC offset."
        )
    return value.astimezone(UTC)


def parse_instant(text: str) -> datetime:
    """Parse an ISO-8601 string that carries an offset.

    A bare date is accepted and taken as midnight UTC, which is the one place
    an assumption is cheap enough to be worth the convenience.
    """
    cleaned = text.strip().replace("Z", "+00:00")
    if len(cleaned) == 10:  # YYYY-MM-DD
        return datetime.fromisoformat(cleaned).replace(tzinfo=UTC)
    return ensure_utc(datetime.fromisoformat(cleaned))


@dataclass(frozen=True)
class Clock:
    """The instant a pipeline run is evaluated at.

    Pass this object down rather than a bare datetime, so that any call site
    reaching for the wall clock is a visible type error instead of a silent
    correctness bug.
    """

    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_utc(self.as_of))

    @classmethod
    def at(cls, value: str | datetime) -> "Clock":
        if isinstance(value, str):
            return cls(parse_instant(value))
        return cls(ensure_utc(value))

    @classmethod
    def wall(cls) -> "Clock":
        """The real current time.

        The single sanctioned reading of the system clock. It lives here so
        that ``grep -rn 'datetime.now' engine/`` returns exactly one line and
        that line is this one.
        """
        return cls(datetime.now(UTC))

    def plus(self, *, hours: float = 0.0, days: float = 0.0) -> "Clock":
        return Clock(self.as_of + timedelta(hours=hours, days=days))

    def hours_until(self, moment: datetime) -> float:
        """Signed hours from as-of to *moment*. Negative means it has passed."""
        return (ensure_utc(moment) - self.as_of).total_seconds() / 3600.0

    def days_until(self, moment: datetime) -> float:
        return self.hours_until(moment) / 24.0

    def __str__(self) -> str:
        return self.as_of.strftime("%Y-%m-%d %H:%M UTC")


def overlaps(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> bool:
    """Do two closed intervals intersect?

    This is condition 2 of the gate (BRIEF §5.1) and the brief is right that it
    is the one everyone forgets: a strike that ends before your vessel arrives
    is not your problem, and roughly half the false positives die on this line.
    """
    a_start, a_end = ensure_utc(a_start), ensure_utc(a_end)
    b_start, b_end = ensure_utc(b_start), ensure_utc(b_end)
    return a_start <= b_end and b_start <= a_end
