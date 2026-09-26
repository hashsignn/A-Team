"""The five focus routes: held to real data, and honest about how much of it.

What these pin down:

* the five routes, and that every operator named for them is a real company
  with the public page it was checked against — and no invented capacity;
* weather and sea state are recorded at every place on them, read against
  declared thresholds, and turned into events at the place they were read;
* NO HINDSIGHT: a board replayed before the recording was made sees only
  days that were over by then, and no forecast; nothing published after the
  as-of reaches the board from any source;
* the card's "N of M sources real" counts what the run read, never the route
  merely being on the list;
* the recording's instant becomes the board's default.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from engine import focus
from engine.clock import Clock
from engine.config import load_config
from engine.ingest import observations, weather
from engine.ingest.observations import FeedStatus

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "fixtures"
AS_OF = datetime(2026, 9, 18, 6, tzinfo=UTC)
FIVE = ["LANE_US_04", "LANE_ASIA_08", "LANE_US_01", "LANE_MX_01", "LANE_IN_01"]


@pytest.fixture(scope="module")
def config():
    return load_config()


def _recorder():
    spec = importlib.util.spec_from_file_location(
        "record_fixture", ROOT / "scripts" / "record_fixture.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_fixture"] = module
    spec.loader.exec_module(module)
    return module


# =====================================================================
# The routes and who runs them
# =====================================================================
def test_the_five_focus_routes(config):
    routes = focus.focus_routes(config)
    assert list(routes) == FIVE
    assert {r.sika_flow for r in routes.values()} == {"DE_US", "CH_CN", "CH_US", "CH_MX", "CH_IN"}


def test_every_operator_is_a_real_company_with_the_page_it_was_checked_against(config):
    """A name with no source is a name a planner cannot verify, and a guessed
    capacity is the number they would act on — so neither is allowed."""
    ops = focus.operators(config)
    assert len(ops) >= 10
    lanes = set(FIVE)
    for op in ops:
        assert op["website"].startswith("https://"), op["name"]
        assert op["source"].startswith("https://"), op["name"]
        assert op.get("checked"), op["name"]
        assert set(op["routes"]) <= lanes, op["name"]
        assert op["near"] in config.nodes, op["name"]
        assert set(op["modes"]) <= {"sea", "barge", "rail", "road"}, op["name"]
        assert not {"capacity", "price", "phone", "email"} & set(op), op["name"]
    # Each focus route has somebody on it.
    for route in focus.focus_routes(config).values():
        assert route.operators, route.lane_id


def test_the_committed_focus_file_repeats_no_customer_figure():
    """The customer's volumes live in the gitignored config/flows.yaml, and
    the repository is public. Compared against that file where it exists —
    the figures themselves are never written into a test either."""
    import yaml

    text = (ROOT / "config.example" / "focus.yaml").read_text(encoding="utf-8")
    assert "documents:" not in text
    flows = ROOT / "config" / "flows.yaml"
    if not flows.exists():
        return
    rows = (yaml.safe_load(flows.read_text(encoding="utf-8")) or {}).get("flows") or []
    for row in rows:
        count = row.get("documents")
        if isinstance(count, int) and count >= 100:
            assert str(count) not in text and f"{count:,}" not in text


def test_without_the_customer_export_there_is_no_volume(config):
    from engine.ingest import flows

    route = focus.focus_routes(config)["LANE_US_04"]
    assert focus.flow_volume(route, flows.load()) is None


def test_the_operators_appear_as_partners_on_the_map(config):
    from engine.fleet import vendors
    from engine.pipeline import RunOptions, run

    context = run(clock=Clock.at(AS_OF), config=config, options=RunOptions(seed=7))
    real = [p for p in vendors.partners(context) if p["source"].startswith("focus.yaml")]
    assert len(real) == len(focus.operators(config))
    for p in real:
        assert p["capacity"] is None and p["synthetic"] is False
        assert p["channel"] == "portal" and p["contact"]["portal"].startswith("https://")
        assert p["checked_against"].startswith("https://")


# =====================================================================
# Where the weather is read
# =====================================================================
def test_every_place_on_the_focus_routes_is_watched_once(config):
    points = focus.watch_points(config)
    ids = [p.node_id for p in points]
    assert len(ids) == len(set(ids))
    lanes = {lane["id"]: lane for lane in config.lanes}
    expected = {n for lane_id in FIVE for n in focus.lane_node_ids(lanes[lane_id])}
    assert set(ids) == expected


def test_sea_state_is_read_at_sea(config):
    """A port's coordinates are on the quay, where a wave model has no value."""
    points = {p.node_id: p for p in focus.watch_points(config)}
    for p in points.values():
        assert p.marine == (p.kind in {"seaport", "chokepoint"}), p.node_id
    suez = points["CHOKE_SUEZ"]
    assert (suez.marine_lat, suez.marine_lon) != (suez.lat, suez.lon)
    assert "Gulf of Suez" in suez.marine_name
    assert not points["CHBSL"].marine


def test_the_recording_asks_once_for_every_place_with_no_key(config):
    points = focus.watch_points(config)
    wx, sea = weather.request_urls(points)
    from urllib.parse import parse_qs, urlsplit

    q = parse_qs(urlsplit(wx).query)
    assert len(q["latitude"][0].split(",")) == len(points)
    assert q["past_days"] == ["60"] and q["forecast_days"] == ["7"]
    assert q["wind_speed_unit"] == ["ms"] and q["timezone"] == ["UTC"]
    m = parse_qs(urlsplit(sea).query)
    assert len(m["latitude"][0].split(",")) == sum(p.marine for p in points)
    for url in (wx, sea):
        assert url.startswith("https://") and "key=" not in url.lower()


# =====================================================================
# Reading a recording
# =====================================================================
def _location(days: list[date], **series):
    return {"daily": {"time": [d.isoformat() for d in days], **series}}


def test_parse_reads_one_location_or_many_and_drops_nulls():
    d = [date(2026, 9, 17), date(2026, 9, 18)]
    one = weather.parse({"nodes": ["NLRTM"],
                         "response": _location(d, wave_height_max=[1.5, None])})
    assert one == {"NLRTM": {d[0]: {"wave_height_max": 1.5}}}
    many = weather.parse({"nodes": ["NLRTM", "BEANR"], "response": [
        _location(d, wind_gusts_10m_max=[10.0, 12.0]),
        _location(d, wind_gusts_10m_max=[9.0, 30.0])]})
    assert many["BEANR"][d[1]]["wind_gusts_10m_max"] == 30.0
    with pytest.raises(ValueError):
        weather.parse({"nodes": ["NLRTM", "BEANR"], "response": [_location(d)]})


def _recording(config, recorded_at: datetime, values: dict) -> weather.Recording:
    """values: node → {day offset from AS_OF's date: {reading: value}}."""
    base = AS_OF.date()
    daily = {node: {base + timedelta(days=k): v for k, v in by_day.items()}
             for node, by_day in values.items()}
    return weather.Recording(daily=daily, recorded_at=recorded_at, origin="the recording")


def _assess(config, recording, as_of=AS_OF):
    return weather.assess(config, Clock.at(as_of), focus.watch_points(config), recording)


def test_at_the_recording_a_storm_yesterday_is_real_and_a_forecast_is_weighted(config):
    rec = _recording(config, AS_OF, {
        "NLRTM": {-1: {"wind_gusts_10m_max": 31.0}},
        "CHOKE_SUEZ": {2: {"wave_height_max": 5.2}},
    })
    obs = {o["observation_id"]: o for o in _assess(config, rec)}
    storm = next(o for o in obs.values() if o["variable_id"] == "CLI_STORM")
    assert storm["node_ids"] == ["NLRTM"] and storm["realized"] and storm["probability"] == 1.0
    assert storm["severity"] == "severe"
    # Gusts of 31 m/s are over the crane limit too: two rules, two readings.
    assert any(o["variable_id"] == "CLI_HIGH_WIND" for o in obs.values())
    sea = next(o for o in obs.values() if o["variable_id"] == "CLI_WAVE_HEIGHT")
    assert not sea["realized"] and sea["probability"] == 0.75
    assert "forecast" in sea["title"] and "2 day(s) out" in sea["probability_basis"]
    assert sea["verbatim_quote"].startswith("Open-Meteo forecast")


def test_consecutive_days_over_a_threshold_are_one_event(config):
    rec = _recording(config, AS_OF, {"BEANR": {
        1: {"wind_gusts_10m_max": 22.0}, 2: {"wind_gusts_10m_max": 24.0},
        3: {"wind_gusts_10m_max": 21.0}}})
    crane = [o for o in _assess(config, rec) if o["variable_id"] == "CLI_HIGH_WIND"]
    assert len(crane) == 1
    assert crane[0]["value"] == 24.0
    assert crane[0]["ends_at"] - crane[0]["starts_at"] == timedelta(days=3)


def test_a_replay_sees_no_day_that_was_not_over_and_no_forecast(config):
    """Recorded on the 23rd, replayed at the 18th: the storm of the 20th and
    the forecast of the 25th are both the future as seen from the 18th."""
    rec = _recording(config, AS_OF + timedelta(days=5), {
        "NLRTM": {-1: {"wind_gusts_10m_max": 30.0}, 2: {"wind_gusts_10m_max": 33.0},
                  7: {"wind_gusts_10m_max": 35.0}},
    })
    obs = _assess(config, rec)
    starts = {o["starts_at"].date() for o in obs}
    assert starts == {AS_OF.date() - timedelta(days=1)}
    assert all(o["realized"] for o in obs)
    # Nor does the day of the as-of count: its maximum includes hours after it.
    rec_today = _recording(config, AS_OF + timedelta(days=5),
                           {"NLRTM": {0: {"wind_gusts_10m_max": 30.0}}})
    assert _assess(config, rec_today) == []


def test_a_spell_that_ended_long_before_the_as_of_is_not_live(config):
    rec = _recording(config, AS_OF, {"NLRTM": {-10: {"wind_gusts_10m_max": 35.0}}})
    assert _assess(config, rec) == []


def test_frost_reads_the_minimum_and_rules_skip_the_wrong_kind_of_place(config):
    rec = _recording(config, AS_OF, {
        "SIKA_DUD": {-1: {"temperature_2m_min": -14.0}},
        "USNYC": {-1: {"temperature_2m_min": -14.0, "snowfall_sum": 30.0}},
    })
    obs = _assess(config, rec)
    frost = [o for o in obs if o["variable_id"] == "CLI_ICE"]
    assert [o["node_ids"] for o in frost] == [["SIKA_DUD"]]
    assert frost[0]["value"] == -14.0
    # A seaport is not in the snow rule's kinds; the plant is not a port.
    assert not [o for o in obs if o["node_ids"] == ["USNYC"]]


def test_conditions_say_when_a_replay_has_no_forecast(config):
    points = focus.watch_points(config)
    rec = _recording(config, AS_OF + timedelta(days=5), {
        "NLRTM": {-1: {"wind_gusts_10m_max": 12.0}, 3: {"wind_gusts_10m_max": 40.0}}})
    table = weather.conditions(Clock.at(AS_OF), points, rec)
    assert table["NLRTM"]["observed"] == {"wind_gusts_10m_max": 12.0}
    assert table["NLRTM"]["forecast_from"] is None
    live = weather.conditions(Clock.at(AS_OF), points, _recording(config, AS_OF, {
        "NLRTM": {-1: {"wind_gusts_10m_max": 12.0}, 3: {"wind_gusts_10m_max": 40.0}}}))
    assert live["NLRTM"]["forecast_max"]["wind_gusts_10m_max"] == 40.0


def test_with_nothing_recorded_the_socket_says_so(config):
    obs, report, table = weather.assess_weather(config, Clock.at(AS_OF))
    assert obs == [] and table == {}
    assert report.status is FeedStatus.ABSENT
    assert "not recorded yet" in report.detail and weather.KEY in report.detail


# =====================================================================
# End to end: a recording on disk reaches the board
# =====================================================================
@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """The test samples, plus a weather recording made by the real recorder
    from a fake Open-Meteo answer."""
    folder = tmp_path / "fixtures"
    shutil.copytree(SAMPLES, folder)
    recorder = _recorder()
    monkeypatch.setattr(recorder, "FIXTURES", folder)
    monkeypatch.setattr(observations, "FIXTURE_DIR", folder)

    points = focus.watch_points(load_config())
    days = [AS_OF.date() + timedelta(days=k) for k in range(-60, 8)]

    def answer(url: str):
        at_sea = "marine" in url
        asked = [p for p in points if p.marine] if at_sea else points
        out = []
        for p in asked:
            gusts = [8.0] * len(days)
            if p.node_id == "NLRTM" and not at_sea:
                gusts[59] = 31.0                  # the day before the as-of
            reading = "wave_height_max" if at_sea else "wind_gusts_10m_max"
            out.append(_location(days, **{reading: [1.0] * len(days) if at_sea else gusts}))
        return out, ""

    # Recorded at the as-of, so the board at the as-of is "at the recording".
    stamp = AS_OF.isoformat()
    real_write = recorder._write_file

    def write(name, blob):
        size = real_write(name, blob)
        path = folder / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_recorded_at"] = stamp
        path.write_text(json.dumps(data), encoding="utf-8")
        return size

    monkeypatch.setattr(recorder, "_write_file", write)
    assert recorder.record_weather(AS_OF, 60, fetch=answer, now=AS_OF) == 0
    return recorder, folder


def test_a_recording_round_trips_through_the_board_s_own_reader(recorded):
    recorder, folder = recorded
    assert recorder.is_recording(recorder.WEATHER_KEY)
    rec, why = weather.load(focus.watch_points(load_config()))
    assert why == "" and rec is not None
    assert rec.recorded_at == AS_OF
    assert len(rec.daily["NLRTM"]) == 68


def test_a_recorded_storm_becomes_an_event_on_every_route_through_the_port(recorded):
    from engine.export.board import build_board
    from engine.pipeline import RunOptions, run

    context = run(clock=Clock.at(AS_OF), config=load_config(), options=RunOptions(seed=7))
    storms = [e for e in context.events if e.event_id.startswith("OBS-WX-STORM_GUSTS-NLRTM")]
    assert storms, [e.event_id for e in context.events]
    assert storms[0].provenance.source == "open_meteo"
    board = build_board(context)
    via_rotterdam = [r for r in board["routes"] if "NLRTM" in r["node_ids"]]
    assert any(any(e["event_id"] == storms[0].event_id for e in r["events"]) for r in via_rotterdam)
    us04 = next(r for r in board["routes"] if r["route_id"] == "LANE_US_04")
    wx = next(s for s in us04["real_data"]["sources"] if s["key"] == "weather_focus")
    assert wx["real"] is True
    assert us04["conditions"] and us04["conditions"][0]["node_id"] == "SIKA_STU"


# =====================================================================
# The board's claims
# =====================================================================
def test_a_route_off_the_list_claims_nothing_and_a_listed_one_counts_honestly(config):
    from engine.export.board import build_board
    from engine.pipeline import RunOptions, run

    board = build_board(run(clock=Clock.at(AS_OF), config=config, options=RunOptions(seed=7)))
    by_id = {r["route_id"]: r for r in board["routes"]}
    assert by_id["LANE_RHINE_01"]["real_data"] == {"focus": False}
    for lane_id in FIVE:
        d = by_id[lane_id]["real_data"]
        assert d["focus"] and d["of"] == len(d["sources"])
        # Nothing is recorded in the test samples: every source is a sample
        # or absent, so nothing may be counted as real.
        assert d["real"] == 0, d["sources"]
    assert any(s["key"] == "watergauge_kaub" for s in by_id["LANE_ASIA_08"]["real_data"]["sources"])
    assert not any(s["key"] == "watergauge_kaub" for s in by_id["LANE_US_01"]["real_data"]["sources"])
    json.dumps(board)                       # a date object anywhere fails this


def test_nothing_published_after_the_as_of_reaches_the_board(config, monkeypatch):
    from engine import pipeline
    from engine.ingest import sources

    base = {"headline": "x", "body": "x", "text": "x", "source": "s", "source_tier": 2,
            "lat": None, "lon": None, "node_hint": [], "starts_at": AS_OF, "ends_at": None}
    past = {**base, "item_id": "PAST", "published_at": AS_OF - timedelta(hours=3)}
    future = {**base, "item_id": "FUTURE", "published_at": AS_OF + timedelta(days=2)}
    monkeypatch.setattr(sources, "collect_all", lambda *a, **k: ([past, future], []))
    items, _ = pipeline._collect_sources(config, Clock.at(AS_OF), pipeline.RunOptions())
    assert [i["item_id"] for i in items] == ["PAST"]


def test_the_board_opens_at_the_recording_s_instant(tmp_path, monkeypatch):
    monkeypatch.setattr(observations, "FIXTURE_DIR", tmp_path)
    assert observations.recorded_as_of() is None
    (tmp_path / observations.RECORDING).write_text(json.dumps(
        {"as_of": "2026-09-27T14:00:00+00:00", "sources": {}}), encoding="utf-8")
    assert observations.recorded_as_of() == "2026-09-27T14:00:00+00:00"


def test_quakes_and_disasters_can_be_asked_about_the_last_two_months():
    from engine.ingest.sources.catalog import by_key

    quakes = by_key("usgs_quakes")
    add, _ = quakes.window.params_for(AS_OF, 60)
    assert set(add) == {"starttime", "endtime"} and add["starttime"].startswith("2026-07-20")
    assert quakes.params["format"] == "geojson"
    gdacs = by_key("gdacs")
    add, _ = gdacs.window.params_for(AS_OF, 60)
    assert add == {"fromDate": "2026-07-20", "toDate": "2026-09-18"}
