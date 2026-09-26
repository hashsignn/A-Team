"""Weather and sea state at every place on the focus routes (Open-Meteo).

A reading is a number, so it is handled like the Kaub gauge and not like the
news: thresholds turn it into an observation, no model is involved, and the
observation becomes an event at the node where it was read. A storm at
Rotterdam is then an event at Rotterdam — it touches every route through
Rotterdam, focus route or not, because the weather does not know which
routes we chose to watch.

WHAT IS RECORDED
================
One request for the weather and one for the sea, each asking for every
watched place at once: daily values for the last 60 days (Open-Meteo's
``past_days``) and the next 7 (``forecast_days``). Free, no key. The raw
answers are kept as recorded, and read here.

NO HINDSIGHT
============
A recording made on the 27th holds the weather of the 20th as it turned out.
A board replayed at the 18th must not know it: on the 18th nobody had it.
So a replay reads only days that were already over at its as-of — what a
planner could have seen — and no forecast at all, because the forecast that
was issued on the 18th is not in a recording made on the 27th. Only a board
at the recording's own instant reads the forecast part, and every forecast
observation says how many days out it is and is weighted by it.

That makes an old replay quieter than the day really was. It is the right
side to err on: a replay that "saw" the storm coming because it read the
storm from the future would be a demo of a tool that does not exist.

THE THRESHOLDS
==============
In thresholds.yaml → weather_rules, each with the variable it raises, the
kinds of place it applies to, and ``source:`` — every figure there is marked
assumed until somebody replaces it with a terminal's own operating limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from engine.clock import UTC, Clock
from engine.config import Config
from engine.focus import WatchPoint, watch_points
from engine.ingest.observations import (
    FeedReport,
    FeedStatus,
    fixture_origin,
    left_out,
    load_fixture,
    parse_iso,
)

KEY = "weather_focus"
LABEL = "Weather and sea state on the focus routes (Open-Meteo)"
WEATHER_FIXTURE = "focus_weather.json"
MARINE_FIXTURE = "focus_marine.json"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

DAILY = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "snowfall_sum",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
)
MARINE_DAILY = ("wave_height_max",)

# How far back and ahead a recording asks. Open-Meteo answers up to 92 days
# back and 16 ahead; beyond a week a daily forecast is not something a
# planner rebooks on.
PAST_DAYS = 60
FORECAST_DAYS = 7

# A board this close to the recording's instant is "at" it: it may read the
# forecast part. Further back is a replay, and reads observed days only.
AT_RECORDING = timedelta(hours=6)

# An observed spell that ended this long before the as-of no longer counts:
# the backlog it left is the port's, and the port-congestion feed's to see.
STILL_FELT = timedelta(days=1)

UNITS = {
    "temperature_2m_max": "°C", "temperature_2m_min": "°C",
    "precipitation_sum": "mm", "snowfall_sum": "cm",
    "wind_speed_10m_max": "m/s", "wind_gusts_10m_max": "m/s",
    "wave_height_max": "m",
}

# The shipped rules, used when a thresholds.yaml predates them. The same list
# is in config.example/thresholds.yaml, which is where to change them.
DEFAULT_RULES: tuple[dict, ...] = (
    {"id": "storm_gusts", "reading": "wind_gusts_10m_max", "above": 28.0,
     "variable": "CLI_STORM", "kinds": ["any"], "severity": "severe",
     "source": "assumed", "says": "storm-force gusts"},
    {"id": "crane_wind", "reading": "wind_gusts_10m_max", "above": 20.0,
     "variable": "CLI_HIGH_WIND", "kinds": ["seaport", "inland_port"],
     "severity": "moderate", "source": "assumed",
     "says": "gusts above the usual crane limit"},
    {"id": "barge_wind", "reading": "wind_speed_10m_max", "above": 15.0,
     "variable": "CLI_HIGH_WIND", "kinds": ["inland_port"], "severity": "minor",
     "source": "assumed", "says": "wind above the barge restriction"},
    {"id": "sea_state", "reading": "wave_height_max", "above": 4.5,
     "variable": "CLI_WAVE_HEIGHT", "kinds": ["seaport", "chokepoint"],
     "severity": "moderate", "source": "assumed", "says": "high sea state"},
    {"id": "heavy_rain", "reading": "precipitation_sum", "above": 60.0,
     "variable": "FOR_FLOOD", "kinds": ["any"], "severity": "moderate",
     "source": "assumed", "says": "rain heavy enough to flood roads and yards"},
    {"id": "heavy_snow", "reading": "snowfall_sum", "above": 15.0,
     "variable": "CLI_STORM", "kinds": ["plant", "distribution", "inland_port", "gauge"],
     "severity": "minor", "source": "assumed", "says": "heavy snow"},
    {"id": "extreme_heat", "reading": "temperature_2m_max", "above": 40.0,
     "variable": "CLI_EXTREME_HEAT", "kinds": ["any"], "severity": "minor",
     "source": "assumed", "says": "heat beyond the handling range"},
    {"id": "hard_frost", "reading": "temperature_2m_min", "below": -10.0,
     "variable": "CLI_ICE", "kinds": ["plant", "distribution", "inland_port", "gauge"],
     "severity": "minor", "source": "assumed", "says": "hard frost"},
)


# ---------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------
def request_urls(points: list[WatchPoint], *, past_days: int = PAST_DAYS,
                 forecast_days: int = FORECAST_DAYS) -> tuple[str, str | None]:
    """The two requests a recording makes: every place at once, each."""
    common = {"past_days": str(past_days), "forecast_days": str(forecast_days),
              "timezone": "UTC"}
    weather = FORECAST_URL + "?" + urlencode({
        "latitude": ",".join(f"{p.lat:.3f}" for p in points),
        "longitude": ",".join(f"{p.lon:.3f}" for p in points),
        "daily": ",".join(DAILY), "wind_speed_unit": "ms", **common,
    })
    sea = [p for p in points if p.marine]
    marine = None
    if sea:
        marine = MARINE_URL + "?" + urlencode({
            "latitude": ",".join(f"{p.marine_lat:.3f}" for p in sea),
            "longitude": ",".join(f"{p.marine_lon:.3f}" for p in sea),
            "daily": ",".join(MARINE_DAILY), **common,
        })
    return weather, marine


# ---------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------
Daily = dict[str, dict[date, dict[str, float]]]


def parse(blob: Any) -> Daily:
    """node id → day → reading → value, from a recording in its raw shape.

    ``blob`` is what record_fixture writes: the node ids asked about, in
    order, and Open-Meteo's answer — a list of locations when more than one
    was asked, a single object when one was. A null value is a reading the
    model does not have (a point that fell on land for the wave model), and
    is left out rather than read as zero.
    """
    if not isinstance(blob, dict):
        raise TypeError("expected a recording object")
    nodes = list(blob.get("nodes") or [])
    answer = blob.get("response")
    locations = answer if isinstance(answer, list) else [answer]
    if len(locations) != len(nodes):
        raise ValueError(f"{len(nodes)} place(s) asked, {len(locations)} answered")
    out: Daily = {}
    for node_id, location in zip(nodes, locations, strict=True):
        daily = (location or {}).get("daily") or {}
        days = [date.fromisoformat(str(t)[:10]) for t in daily.get("time") or []]
        per_day: dict[date, dict[str, float]] = {}
        for reading, values in daily.items():
            if reading == "time" or not isinstance(values, list):
                continue
            for day, value in zip(days, values, strict=False):
                if isinstance(value, int | float):
                    per_day.setdefault(day, {})[reading] = float(value)
        out[node_id] = per_day
    return out


def merge(*parts: Daily) -> Daily:
    out: Daily = {}
    for part in parts:
        for node_id, days in part.items():
            for day, readings in days.items():
                out.setdefault(node_id, {}).setdefault(day, {}).update(readings)
    return out


@dataclass
class Recording:
    """What the board has to read: the values, and when they were taken."""

    daily: Daily = field(default_factory=dict)
    recorded_at: datetime | None = None
    origin: str = ""


def load(points: list[WatchPoint]) -> tuple[Recording | None, str]:
    """The recorded weather, or None and why not."""
    why = left_out(KEY)
    if why is not None:
        return None, f"left out of this recording ({why})"
    weather = load_fixture(WEATHER_FIXTURE)
    if weather is None:
        return None, ("not recorded yet — run scripts/record_fixture.py "
                      f"{KEY} on a machine with a network")
    marine = load_fixture(MARINE_FIXTURE)
    try:
        daily = merge(parse(weather), parse(marine) if marine is not None else {})
    except (TypeError, ValueError) as exc:
        return None, f"the recording could not be read ({exc})"
    stamp = weather.get("_recorded_at")
    recorded_at = parse_iso(stamp) if isinstance(stamp, str) and stamp else None
    wanted = {p.node_id for p in points}
    daily = {node: days for node, days in daily.items() if node in wanted}
    return Recording(daily=daily, recorded_at=recorded_at,
                     origin=fixture_origin(weather)), ""


# ---------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------
def rules(config: Config) -> list[dict]:
    declared = config.thresholds.get("weather_rules")
    return [r for r in (declared if isinstance(declared, list) else DEFAULT_RULES)
            if isinstance(r, dict) and r.get("reading") and r.get("variable")]


def _crosses(rule: dict, value: float) -> bool:
    if "above" in rule:
        return value >= float(rule["above"])
    if "below" in rule:
        return value <= float(rule["below"])
    return False


def _applies(rule: dict, point: WatchPoint) -> bool:
    kinds = rule.get("kinds") or ["any"]
    if "any" not in kinds and point.kind not in kinds:
        return False
    # Sea state is read at sea; nothing inland has one.
    return rule["reading"] not in MARINE_DAILY or point.marine


def _forecast_p(lead_days: int) -> float:
    """How far to trust a daily forecast this many days out. ASSUMED: the
    shape (skill falls with lead time) is standard; the figures are ours."""
    if lead_days <= 1:
        return 0.9
    if lead_days <= 3:
        return 0.75
    if lead_days <= 5:
        return 0.6
    return 0.5


def _runs(days: list[date]) -> list[list[date]]:
    """Consecutive days, grouped: a three-day gale is one event, not three."""
    runs: list[list[date]] = []
    for day in sorted(days):
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _mode(clock: Clock, recording: Recording) -> tuple[bool, date]:
    """(at the recording, last complete day this board may read)."""
    recorded = recording.recorded_at or clock.as_of
    at_recording = clock.as_of >= recorded - AT_RECORDING
    known_until = min(clock.as_of, recorded)
    return at_recording, known_until.date() - timedelta(days=1)


def assess(config: Config, clock: Clock, points: list[WatchPoint],
           recording: Recording) -> list[dict]:
    """Observations the board should carry at this as-of."""
    at_recording, last_observed = _mode(clock, recording)
    recorded_day = (recording.recorded_at or clock.as_of).date()
    today = clock.as_of.date()
    by_node = {p.node_id: p for p in points}
    out: list[dict] = []

    for rule in rules(config):
        if rule["variable"] not in config.variables:
            continue
        reading = rule["reading"]
        unit = UNITS.get(reading, "")
        for node_id, days in recording.daily.items():
            point = by_node.get(node_id)
            if point is None or node_id not in config.nodes or not _applies(rule, point):
                continue
            observed = [d for d, r in days.items()
                        if d <= last_observed and reading in r and _crosses(rule, r[reading])]
            forecast = []
            if at_recording:
                forecast = [d for d, r in days.items()
                            if d >= recorded_day and d <= today + timedelta(days=FORECAST_DAYS)
                            and reading in r and _crosses(rule, r[reading])]

            for run in _runs(observed):
                if _midnight(run[-1]) + timedelta(days=1) + STILL_FELT < clock.as_of:
                    continue                      # over, and no longer felt
                out.append(_observation(rule, point, run, days, unit, lead=None))
            for run in _runs(forecast):
                lead = max(0, (run[0] - today).days)
                out.append(_observation(rule, point, run, days, unit, lead=lead))
    return out


def _observation(rule: dict, point: WatchPoint, run: list[date],
                 days: dict[date, dict[str, float]], unit: str,
                 lead: int | None) -> dict:
    reading = rule["reading"]
    values = [days[d][reading] for d in run]
    peak = min(values) if "below" in rule else max(values)
    peak_day = run[values.index(peak)]
    where = point.marine_name if reading in MARINE_DAILY and point.marine_name else point.name
    limit = rule.get("above", rule.get("below"))
    side = "≤" if "below" in rule else "≥"
    span = (f"{run[0]:%d %b}" if len(run) == 1
            else f"{run[0]:%d %b}–{run[-1]:%d %b}")
    forecast = lead is not None
    quote = (f"Open-Meteo {'forecast' if forecast else 'daily'} "
             f"{reading.replace('_', ' ')} {peak:.1f} {unit} at {where} on "
             f"{peak_day:%Y-%m-%d}")
    return {
        "observation_id": f"OBS-WX-{rule.get('id', reading).upper()}-{point.node_id}-{run[0]:%Y%m%d}",
        "node_ids": [point.node_id],
        "variable_id": rule["variable"],
        "severity": rule.get("severity", "minor"),
        "starts_at": _midnight(run[0]),
        "ends_at": _midnight(run[-1]) + timedelta(days=1),
        # The days over the threshold are read; how long the disruption
        # lasts past them is not, so the window is an estimate either way.
        "duration_confidence": "estimated",
        "realized": not forecast,
        "probability": _forecast_p(lead) if forecast else 1.0,
        "probability_basis": (
            f"Open-Meteo daily forecast {lead} day(s) out; confidence falls "
            "with lead time (assumed)" if forecast else
            f"observed: {span}, {peak:.1f} {unit} {side} {limit} {unit}"
        ),
        "threshold_source": rule.get("source", "assumed"),
        "verbatim_quote": quote,
        "title": (f"{rule.get('says', reading).capitalize()} at {point.name}"
                  f" — {peak:.0f} {unit}, {span}"
                  + (" (forecast)" if forecast else "")),
        "source": "open_meteo",
        "source_tier": 1,
        "reading": reading,
        "value": peak,
        "focus_lanes": list(point.lanes),
    }


# ---------------------------------------------------------------------
# What the route page shows
# ---------------------------------------------------------------------
def conditions(clock: Clock, points: list[WatchPoint], recording: Recording) -> dict:
    """Per place: the last day this board may read, and the week ahead.

    The week ahead is only there at the recording's instant. A replay says
    so rather than showing a forecast nobody had on that day.
    """
    at_recording, last_observed = _mode(clock, recording)
    recorded_day = (recording.recorded_at or clock.as_of).date()
    out = {}
    for point in points:
        days = recording.daily.get(point.node_id) or {}
        seen = sorted(d for d in days if d <= last_observed)
        ahead = sorted(d for d in days if at_recording and d >= recorded_day)[:FORECAST_DAYS]
        latest = days[seen[-1]] if seen else {}
        out[point.node_id] = {
            "name": point.name,
            "kind": point.kind,
            "marine_at": point.marine_name or None,
            "observed_day": seen[-1].isoformat() if seen else None,
            "observed": {k: round(v, 1) for k, v in latest.items()},
            "observed_days": len(seen),
            "forecast_from": ahead[0].isoformat() if ahead else None,
            "forecast_max": {
                reading: round(max(days[d][reading] for d in ahead if reading in days[d]), 1)
                for reading in (*DAILY, *MARINE_DAILY)
                if any(reading in days[d] for d in ahead)
                and not reading.endswith("_min")
            },
            "forecast_min": {
                reading: round(min(days[d][reading] for d in ahead if reading in days[d]), 1)
                for reading in DAILY
                if reading.endswith("_min") and any(reading in days[d] for d in ahead)
            },
        }
    return out


def assess_weather(config: Config, clock: Clock) -> tuple[list[dict], FeedReport | None, dict]:
    """(observations, the socket row, conditions per place).

    No focus routes, no row: the socket belongs to the focus routes and a
    board without any has nothing to say about it.
    """
    points = watch_points(config)
    if not points:
        return [], None, {}

    recording, why = load(points)
    if recording is None:
        return [], FeedReport(
            key=KEY, label=LABEL, status=FeedStatus.ABSENT,
            detail=f"{len(points)} place(s) on the focus routes — {why}",
            unlocks_if_connected=(
                "Storms, crane-stopping wind, high seas, flooding rain, snow and "
                "heat at every place on the focus routes: 60 days observed and "
                "7 forecast, free and without a key."
            ),
            source_tier=1, nature="instrument", cost="free", url=FORECAST_URL,
        ), {}

    observations = assess(config, clock, points, recording)
    at_recording, last_observed = _mode(clock, recording)
    places = sum(1 for p in points if recording.daily.get(p.node_id))
    detail = (f"{places} of {len(points)} place(s) from {recording.origin}; "
              f"{len(observations)} over a threshold at this as-of")
    if not at_recording:
        detail += (f" — a replay: observed days up to {last_observed:%Y-%m-%d} "
                   "only, no forecast (the one issued then was not recorded)")
    report = FeedReport(
        key=KEY, label=LABEL, status=FeedStatus.FIXTURE, detail=detail,
        unlocks_if_connected="", records=len(observations),
        retrieved_at=recording.recorded_at, source_tier=1,
        nature="instrument", cost="free", url=FORECAST_URL,
    )
    return observations, report, conditions(clock, points, recording)
