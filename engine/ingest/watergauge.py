"""Rhine water level — THE ANCHOR STORY (BRIEF §2, §3.4).

    "When the Rhine recently saw record-low water levels, a planner first
     heard about it from a carrier calling to say a barge shipment would be
     delayed."

The whole point is to flag Kaub days before that call. This module runs with no
model at all, which is why the demo survives an API outage, a missing key, or a
frontier model having a bad afternoon.

THE DOMAIN POINT THAT MATTERS
-----------------------------
Low water is a **payload derate, not a stoppage**. Vessels keep sailing; they
load to reduced draught. The same tonnage then needs more sailings, and
low-water surcharges apply. Modelling it as "barge blocked" is wrong, and a
Sika logistics colleague will know it is wrong within one sentence.

The OTIF consequence runs through *in full*, not only *on time*: a derate that
splits one consignment across two sailings fails OTIF even when the first part
arrives early. That is the mechanism no generic weather-alert tool would find,
and it is the reason this lane is worth demonstrating.

High water is the opposite case and genuinely does suspend navigation.
"""

from __future__ import annotations

from datetime import timedelta

from engine.clock import Clock
from engine.config import Config
from engine.ingest.observations import (
    FeedReport,
    FeedStatus,
    crossed_above,
    crossed_below,
    load_fixture,
    parse_iso,
    project,
    trend,
)

FIXTURE_NAME = "kaub_levels.json"

# Where the live reading would come from. Blocked by network policy in the
# build environment; see the module docstring in ingest/observations.py.
PEGELONLINE_URL = (
    "https://www.pegelonline.wsv.de/webservices/rest-api/v2/"
    "stations/KAUB/W/measurements.json"
)


def fetch_kaub(config: Config, clock: Clock) -> tuple[list[tuple], FeedReport]:
    """Return (series, report) where series is [(datetime, level_cm), ...].

    Tries the live feed, falls back to the committed fixture, and says which
    happened. It never returns an empty series pretending to be a real one.
    """
    series, live_error = _try_live(clock)
    if series:
        return series, FeedReport(
            key="watergauge_kaub",
            label="Rhine water level — Kaub (Pegelonline)",
            status=FeedStatus.CONNECTED,
            detail=f"{len(series)} readings, latest {series[-1][1]:.0f} cm",
            unlocks_if_connected="",
            records=len(series),
            retrieved_at=clock.as_of,
            source_tier=1,
            url=PEGELONLINE_URL,
        )

    raw = load_fixture(FIXTURE_NAME)
    if raw is None:
        return [], FeedReport(
            key="watergauge_kaub",
            label="Rhine water level — Kaub (Pegelonline)",
            status=FeedStatus.ABSENT,
            detail=f"no live feed ({live_error}) and no fixture at {FIXTURE_NAME}",
            unlocks_if_connected=(
                "The anchor demo: Rhine low-water derate on the "
                "Basel/Düdingen → Rotterdam barge lane, days before a carrier calls."
            ),
            source_tier=1,
            url=PEGELONLINE_URL,
        )

    series = [(parse_iso(row["t"]), float(row["cm"])) for row in raw["readings"]]
    series = [(t, v) for t, v in series if t <= clock.as_of]
    series.sort(key=lambda pair: pair[0])

    return series, FeedReport(
        key="watergauge_kaub",
        label="Rhine water level — Kaub (Pegelonline)",
        status=FeedStatus.FIXTURE,
        detail=(
            f"{raw.get('label', 'fixture')} — {len(series)} readings up to as-of, "
            f"latest {series[-1][1]:.0f} cm" if series else "fixture has no readings before as-of"
        ),
        unlocks_if_connected=(
            "Live Kaub readings. The logic is identical; only the source changes."
        ),
        records=len(series),
        retrieved_at=clock.as_of,
        source_tier=1,
        url=PEGELONLINE_URL,
    )


def _try_live(clock: Clock) -> tuple[list[tuple], str]:
    """Attempt the live Pegelonline call.

    Written so that wiring it up is a matter of the network allowing it, not of
    code changes. In this environment it always fails, visibly.
    """
    try:
        import urllib.request

        with urllib.request.urlopen(  # noqa: S310 - fixed, known host
            f"{PEGELONLINE_URL}?start=P30D", timeout=8
        ) as response:
            import json

            payload = json.load(response)
        series = [
            (parse_iso(row["timestamp"]), float(row["value"]))
            for row in payload
        ]
        series = [(t, v) for t, v in series if t <= clock.as_of]
        series.sort(key=lambda pair: pair[0])
        return series, ""
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return [], type(exc).__name__


def assess_kaub(config: Config, clock: Clock) -> tuple[list[dict], FeedReport]:
    """Turn the gauge series into zero or more observation records.

    An observation is not yet an event — it is a numeric finding with a node, a
    window and a severity. engine/resolve turns observations into events.
    """
    series, report = fetch_kaub(config, clock)
    if not series:
        return [], report

    spec = config.thresholds["water_gauges"]["GAUGE_KAUB"]
    latest_at, latest_cm = series[-1]
    per_day = trend(series, window=14)

    observations: list[dict] = []

    # ---- falling: capacity derate -------------------------------------
    band = crossed_below(latest_cm, spec["low_water_bands"], key="below_cm")

    # Look ahead. A gauge falling 5cm a day that is 20cm above the next band is
    # four days from it — and four days is the difference between rebooking to
    # rail and telling a customer their order is short. This projection is the
    # lead time the tool exists to produce.
    projected_band = None
    days_to_next = None
    if per_day < -0.5:
        for horizon in (3, 7, 14):
            projected = project(latest_cm, per_day, horizon)
            candidate = crossed_below(projected, spec["low_water_bands"], key="below_cm")
            if candidate and (band is None or candidate["payload_fraction"] < band["payload_fraction"]):
                projected_band = candidate
                days_to_next = horizon
                break

    effective = projected_band or band
    if effective is not None:
        starts = latest_at if projected_band is None else latest_at + timedelta(days=days_to_next or 0)
        surcharge = spec["low_water_surcharge_multiplier"][effective["severity"]]
        observations.append(
            {
                "observation_id": f"OBS-KAUB-LOW-{starts:%Y%m%d}",
                "node_ids": ["GAUGE_KAUB"],
                "variable_id": "WAT_LOW_WATER",
                "severity": effective["severity"],
                "starts_at": starts,
                "ends_at": starts + timedelta(days=14),
                "duration_confidence": "estimated",
                "realized": projected_band is None,
                "probability": _probability_from_trend(per_day, projected_band is not None),
                "probability_basis": (
                    f"Kaub {latest_cm:.0f} cm at {latest_at:%Y-%m-%d %H:%M UTC}, "
                    f"trending {per_day:+.1f} cm/day over 14 days"
                    + (
                        f"; projected below {effective['below_cm']} cm within {days_to_next} days"
                        if projected_band
                        else f"; currently below the {effective['below_cm']} cm band"
                    )
                ),
                "payload_fraction": effective["payload_fraction"],
                "cost_multiplier": surcharge,
                "threshold_source": effective.get("source", "assumed"),
                "verbatim_quote": (
                    f"Kaub gauge {latest_cm:.0f} cm ({latest_at:%Y-%m-%d %H:%M UTC}), "
                    f"14-day trend {per_day:+.1f} cm/day"
                ),
                "title": (
                    f"Rhine low water at Kaub — loading restricted to "
                    f"{effective['payload_fraction']:.0%} of full payload"
                ),
                "source": "pegelonline",
                "source_tier": 1,
                "level_cm": latest_cm,
                "trend_cm_per_day": per_day,
            }
        )

    # ---- rising: navigation suspension ---------------------------------
    high = crossed_above(latest_cm, spec.get("high_water_bands", []), key="above_cm")
    if high is not None:
        observations.append(
            {
                "observation_id": f"OBS-KAUB-HIGH-{latest_at:%Y%m%d}",
                "node_ids": ["GAUGE_KAUB"],
                "variable_id": "WAT_HIGH_WATER",
                "severity": high["severity"],
                "starts_at": latest_at,
                "ends_at": latest_at + timedelta(days=4),
                "duration_confidence": "estimated",
                "realized": True,
                "probability": 1.0,
                "probability_basis": f"Kaub {latest_cm:.0f} cm, above the {high['above_cm']} cm mark",
                "payload_fraction": 0.0,
                "cost_multiplier": 1.0,
                "threshold_source": high.get("source", "assumed"),
                "verbatim_quote": f"Kaub gauge {latest_cm:.0f} cm ({latest_at:%Y-%m-%d %H:%M UTC})",
                "title": "Rhine high water at Kaub — navigation suspended",
                "source": "pegelonline",
                "source_tier": 1,
                "level_cm": latest_cm,
                "trend_cm_per_day": per_day,
            }
        )

    return observations, report


def _probability_from_trend(per_day: float, is_projection: bool) -> float:
    """P that the restriction is in force during the window.

    A realized reading is certain. A projection is not, and how confident we are
    depends on how hard the gauge is falling. This is one of the few places P is
    honestly sourceable (BRIEF §5.3) — it comes from a measured trend, not from
    a number someone liked the look of.
    """
    if not is_projection:
        return 1.0
    steepness = min(abs(per_day) / 10.0, 1.0)
    return round(0.45 + 0.45 * steepness, 2)
