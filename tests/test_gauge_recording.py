"""The Kaub gauge is recorded like a source and read back like the live feed.

The anchor story is the Rhine, and until now the only Rhine the board had
seen was generated: record_fixture.py recorded every source but the gauge.
Recording it is one request. Reading it correctly is the part tested here:
Pegelonline publishes every fifteen minutes, the trend is taken over the
last 14 readings, and 14 fifteen-minute readings are three and a half hours
— in which a centimetre of jitter reads as a river falling fast enough to
raise a derate that is not there.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.ingest import observations, watergauge
from engine.ingest.observations import FeedStatus, trend

ROOT = Path(__file__).resolve().parent.parent
CEST = timezone(timedelta(hours=2))          # Pegelonline answers in local time
RECORDED_AT = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
AS_OF = datetime(2026, 9, 22, tzinfo=UTC)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module              # before it runs, for @dataclass
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recorder():
    return _load("record_fixture")


@pytest.fixture(scope="module")
def reasoner():
    return _load("record_reasoning")


@pytest.fixture
def fixtures(recorder, reasoner, tmp_path, monkeypatch):
    """Recordings land in a scratch folder, and the board reads from it."""
    monkeypatch.setattr(recorder, "FIXTURES", tmp_path)
    monkeypatch.setattr(reasoner, "FIXTURES", tmp_path)
    monkeypatch.setattr(observations, "FIXTURE_DIR", tmp_path)
    monkeypatch.delenv("RADAR_ALLOW_NETWORK", raising=False)
    return tmp_path


def pegelonline(level_cm=160.0, per_day=0.0, jitter=1.0, days=30):
    """Answers like Pegelonline: a reading every fifteen minutes, in local
    time, alternating a centimetre either side of the true level."""
    count = days * 96
    rows = []
    for index in range(count + 1):
        moment = RECORDED_AT - timedelta(minutes=15 * (count - index))
        level = level_cm + per_day * index / 96 + (jitter if index % 2 else -jitter)
        rows.append({"timestamp": moment.astimezone(CEST).isoformat(),
                     "value": round(level, 1)})
    return rows


def answering(payload, error=""):
    asked = []

    def fetch(url):
        asked.append(url)
        return (payload, error) if payload is not None else (None, error)

    fetch.asked = asked
    return fetch


def _kaub():
    return watergauge.assess_kaub(load_config(), Clock.at(AS_OF))


# =====================================================================
# Recording
# =====================================================================
def test_the_gauge_is_recorded_raw_and_stamped(recorder, fixtures):
    fetch = answering(pegelonline())
    assert recorder.record_gauge(AS_OF, fetch=fetch) == 0
    assert fetch.asked == [watergauge.LIVE_URL]

    blob = json.loads((fixtures / watergauge.FIXTURE_NAME).read_text(encoding="utf-8"))
    assert blob["is_real_data"] is True
    assert blob["_fixture_note"] == recorder.NOTE
    assert datetime.fromisoformat(blob["_recorded_at"]).tzinfo is not None
    assert blob["measurements"] == pegelonline(), "kept as Pegelonline sent it"


@pytest.mark.parametrize("payload, error", [
    (None, "HTTP 503"),
    ({"message": "station not found"}, ""),
    ([], ""),
    ([{"when": "yesterday"}], ""),
])
def test_a_bad_answer_leaves_the_fixture_as_it_was(recorder, fixtures, payload, error):
    path = fixtures / watergauge.FIXTURE_NAME
    path.write_text('{"readings": []}', encoding="utf-8")
    assert recorder.record_gauge(fetch=answering(payload, error)) == 1
    assert path.read_text(encoding="utf-8") == '{"readings": []}'


def test_recording_everything_includes_the_gauge(recorder, fixtures, monkeypatch):
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    monkeypatch.setattr(sys, "argv", ["record_fixture.py", "--all"])
    asked = []
    monkeypatch.setattr(recorder, "record", lambda key, **_k: asked.append(key) or 0)
    assert recorder.main() == 0
    assert recorder.GAUGE_KEY in asked
    assert "gdelt_doc" in asked


# =====================================================================
# Replaying
# =====================================================================
def test_a_recording_is_read_back_as_a_recording(recorder, fixtures):
    recorder.record_gauge(fetch=answering(pegelonline()))
    series, report = watergauge.fetch_kaub(load_config(), Clock.at(AS_OF))

    assert report.status is FeedStatus.FIXTURE
    assert "RECORDED FROM PEGELONLINE" in report.detail
    assert "SHAPED" not in report.detail
    assert series[-1][0] <= AS_OF, "nothing after the board's as-of"


def test_a_reading_is_said_at_its_time_in_utc(recorder, fixtures):
    """Pegelonline stamps local time. The midnight-UTC reading is 02:00 in
    Koblenz, and was printed as "02:00 UTC" — two hours out."""
    recorder.record_gauge(fetch=answering(pegelonline(level_cm=120.0)))
    found, _ = _kaub()
    assert found
    assert "at 2026-09-22 00:00 UTC" in found[0]["probability_basis"]


def test_fifteen_minute_readings_are_read_four_a_day(recorder, fixtures):
    recorder.record_gauge(fetch=answering(pegelonline()))
    series, _ = watergauge.fetch_kaub(load_config(), Clock.at(AS_OF))
    per_day: dict = {}
    for moment, _level in series:
        day = moment.astimezone(UTC).date()
        per_day[day] = per_day.get(day, 0) + 1
    assert max(per_day.values()) <= 4


def test_jitter_does_not_read_as_a_falling_river(recorder, fixtures):
    """The same river, recorded: falling 3 cm a day under a centimetre of
    jitter. Over the last 14 fifteen-minute readings the jitter alone is
    worth ~15 cm a day; over the last 14 six-hourly ones, well under one."""
    raw = pegelonline(per_day=-3.0)
    recorder.record_gauge(fetch=answering(raw))

    unthinned = watergauge.parse_pegelonline(raw)
    unthinned = [(t, v) for t, v in unthinned if t <= AS_OF]
    assert abs(trend(unthinned, window=14) + 3.0) > 5, "the problem this guards against"

    found, _ = _kaub()
    assert found, "a river this low and falling is a derate"
    assert all(abs(o["trend_cm_per_day"] + 3.0) < 1.0 for o in found)


def test_a_steady_river_above_the_bands_raises_nothing(recorder, fixtures):
    """Read over hours, the jitter on this flat 200 cm river came out as
    -15 cm a day, projected below the 110 cm band within a week: a derate
    on the board for a river doing nothing."""
    recorder.record_gauge(fetch=answering(pegelonline(level_cm=200.0)))
    found, _ = _kaub()
    assert found == []


def test_the_sample_passes_through_unchanged():
    """Already four a day: the anchor story's numbers do not move."""
    raw = json.loads((ROOT / "tests" / "fixtures" / watergauge.FIXTURE_NAME)
                     .read_text(encoding="utf-8"))
    series = sorted((observations.parse_iso(r["t"]), float(r["cm"])) for r in raw["readings"])
    assert watergauge._six_hourly(series) == series


def test_the_trend_says_the_span_it_was_measured_over():
    """It said "over 14 days" while measuring 14 readings: three days."""
    found, _ = watergauge.assess_kaub(load_config(), Clock.at("2026-09-18T06:00:00+00:00"))
    assert found
    for observation in found:
        assert "over 3 days" in observation["probability_basis"]
        assert "14 days" not in observation["probability_basis"]
        assert "14-day" not in observation["verbatim_quote"]


# =====================================================================
# The reasoning recorder says which one it is looking at
# =====================================================================
def test_the_reasoning_recorder_tells_a_recorded_gauge_from_the_sample(
    recorder, reasoner, fixtures,
):
    sample = ROOT / "tests" / "fixtures" / watergauge.FIXTURE_NAME
    (fixtures / watergauge.FIXTURE_NAME).write_text(
        sample.read_text(encoding="utf-8"), encoding="utf-8")
    what, real = reasoner.gauge_status()
    assert not real and "GENERATED" in what

    recorder.record_gauge(fetch=answering(pegelonline()))
    what, real = reasoner.gauge_status()
    assert real
    assert f"{len(pegelonline()):,} readings" in what
