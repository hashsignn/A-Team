"""Feed plumbing and the socket contract (BRIEF §8.2).

Every input is in exactly one of three states, and the state is always visible:

    CONNECTED  live data arrived from the real source
    FIXTURE    committed sample data, clearly labelled, standing in
    ABSENT     the socket exists, nothing is wired, and we say what it would buy

An input is never silently defaulted. The /inputs panel renders this list
directly, which makes it the most honest screen in the product — and, for a
corporate audience, the most persuasive.

Relevant constraint for this build: the execution environment blocks outbound
HTTPS to every data host (``pegelonline.wsv.de``, ``api.open-meteo.com`` and
``archive-api.open-meteo.com`` all return 403 at the egress proxy). Every feed
therefore has to work from a fixture, and the live path is exercised elsewhere.
That is a constraint worth keeping even once egress opens: a demo that cannot
lose its network is a demo that cannot fail on stage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from engine.clock import UTC, Clock

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "fixtures"


class FeedStatus(str, Enum):
    CONNECTED = "connected"
    FIXTURE = "fixture"
    ABSENT = "absent"


@dataclass
class FeedReport:
    """What one input is doing, and what it costs if it is not doing it."""

    key: str
    label: str
    status: FeedStatus
    detail: str
    unlocks_if_connected: str
    records: int = 0
    retrieved_at: datetime | None = None
    source_tier: int = 1
    url: str | None = None

    # Set by the source layer. Both answer questions a planner asks out loud
    # and that a status word alone cannot: "does this one cost anything", and
    # "does this one get read by a model or just measured".
    nature: str | None = None          # report | instrument
    cost: str | None = None            # free | free_with_key | paid

    @property
    def is_live(self) -> bool:
        return self.status is FeedStatus.CONNECTED


@dataclass
class IngestBundle:
    """Everything the deterministic layer produced, plus how it went."""

    observations: list[dict] = field(default_factory=list)
    reports: list[FeedReport] = field(default_factory=list)
    raw_count: int = 0

    def add(self, report: FeedReport, observations: list[dict] | None = None) -> None:
        self.reports.append(report)
        if observations:
            self.observations.extend(observations)
            report.records = len(observations)

    def report_for(self, key: str) -> FeedReport | None:
        return next((r for r in self.reports if r.key == key), None)


# ---------------------------------------------------------------------
# Fixture access
# ---------------------------------------------------------------------


def load_fixture(name: str) -> Any | None:
    path = FIXTURE_DIR / name
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def fixture_exists(name: str) -> bool:
    return (FIXTURE_DIR / name).exists()


# ---------------------------------------------------------------------
# Threshold helpers — the deterministic layer's whole job
# ---------------------------------------------------------------------


def crossed_below(value: float, bands: list[dict], key: str = "below_cm") -> dict | None:
    """Return the most severe band whose threshold *value* has fallen below.

    Bands are declared from least to most severe; the last match wins, so a
    reading of 40cm returns the 35cm band rather than the 150cm one.
    """
    match: dict | None = None
    for band in bands:
        if value < band[key]:
            match = band
    return match


def crossed_above(value: float, bands: list[dict], key: str = "above_cm") -> dict | None:
    match: dict | None = None
    for band in bands:
        if value > band[key]:
            match = band
    return match


def trend(series: list[tuple[datetime, float]], window: int = 7) -> float:
    """Change per day over the last *window* points. Positive is rising.

    Used to turn a level into a forecast band: a gauge falling 6cm a day is a
    different decision from one that has been flat for a week at the same
    reading, and that difference is most of the lead time on the Rhine.
    """
    if len(series) < 2:
        return 0.0
    tail = series[-window:] if len(series) >= window else series
    (t0, v0), (t1, v1) = tail[0], tail[-1]
    days = (t1 - t0).total_seconds() / 86400.0
    if days <= 0:
        return 0.0
    return (v1 - v0) / days


def project(value: float, per_day: float, days: float) -> float:
    return value + per_day * days


def parse_iso(text: str) -> datetime:
    cleaned = text.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def within_horizon(moment: datetime, clock: Clock, days_back: float, days_ahead: float) -> bool:
    """Funnel layer 3 — could this touch anything in flight or planned?"""
    hours = clock.hours_until(moment)
    return -days_back * 24 <= hours <= days_ahead * 24
