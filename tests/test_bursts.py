"""Bursts of small orders: the sign Sika sees a week or so before a crisis
("small shipments that normally go now and then start going in a single
day"). Synthetic order books only: Sika's own stays in config/."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml

from engine.clock import Clock
from engine.config import load_config
from engine.ingest import bursts as B

AS_OF = Clock.at("2026-09-26T23:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


def _book(burst_sizes=None, burst_day=date(2026, 9, 21), normal=(800.0, 900.0), idle=False):
    """Twenty-five working days of two ordinary orders a day, and one day
    with the orders given."""
    series, day, seen = {}, burst_day - timedelta(days=1), 0
    while seen < 25:
        if day.weekday() < 5:
            seen += 1
            # An idle spell: one order in the whole baseline.
            if not idle or seen == 1:
                series[day] = list(normal[:1] if idle else normal)
        day -= timedelta(days=1)
    if burst_sizes is not None:
        series[burst_day] = list(burst_sizes)
    return {"CH_US": series}


def test_many_small_orders_in_one_day_is_a_burst():
    got = B.detect(_book([150.0] * 8), AS_OF, B.Rule())
    assert len(got) == 1
    hit = got[0]
    assert hit["flow"] == "CH_US" and hit["orders"] == 8 and hit["usual"] == 2.0
    assert hit["small_share"] == 1.0 and hit["days_ago"] == 5


def test_a_busy_day_of_ordinary_orders_is_not():
    """Many orders of the usual size is demand, not the sign."""
    assert B.detect(_book([850.0, 900.0, 950.0] * 3), AS_OF, B.Rule()) == []


def test_a_few_orders_after_an_idle_spell_is_not():
    """After New Year the usual is near zero, and any ordinary day would
    otherwise look like a burst."""
    assert B.detect(_book([150.0] * 5, idle=True), AS_OF, B.Rule()) == []


def test_an_old_burst_is_history_not_a_warning():
    old = _book([150.0] * 8, burst_day=date(2026, 8, 3))
    assert B.detect(old, AS_OF, B.Rule()) == []
    events = [{"date": date(2026, 8, 7), "what": "a tariff takes effect", "source": "https://example.org"}]
    hist = B.backtest(old, B.Rule(), events)
    assert hist[0]["day"] == "2026-08-03"
    assert hist[0]["followed_by"]["days_later"] == 4


def test_without_the_order_book_a_labelled_sample_carries_one_burst(config, tmp_path):
    report, summary = B.assess(config, AS_OF, ["CH_CN", "CH_US"], customer_dir=tmp_path)
    assert summary["synthetic"] is True and report.status.value == "fixture"
    assert [b["flow"] for b in summary["bursts"]] == [B.SAMPLE_BURST_FLOW]
    assert 5 <= summary["bursts"][0]["days_ago"] <= 10
    assert summary["history"] == []   # the sample is never offered as evidence


def test_the_order_book_is_read_when_it_is_there(config, tmp_path):
    book = _book([150.0] * 8)
    (tmp_path / B.ORDERS_FILE).write_text(yaml.safe_dump({"flows": {
        flow: {d.isoformat(): sizes for d, sizes in series.items()} for flow, series in book.items()}}), encoding="utf-8")
    report, summary = B.assess(config, AS_OF, ["CH_US"], customer_dir=tmp_path)
    assert summary["synthetic"] is False and report.status.value == "connected"
    assert summary["bursts"][0]["orders"] == 8


def test_a_burst_lifts_a_quiet_route_to_bias_and_says_why(config, tmp_path, monkeypatch):
    from engine.export.board import build_board
    from engine.pipeline import RunOptions, run

    # A burst on the Switzerland to India flow, whose route is otherwise quiet.
    book = {"CH_IN": _book([150.0] * 8)["CH_US"]}
    (tmp_path / B.ORDERS_FILE).write_text(yaml.safe_dump({"flows": {
        flow: {d.isoformat(): sizes for d, sizes in series.items()} for flow, series in book.items()}}), encoding="utf-8")
    monkeypatch.setattr(B, "CUSTOMER_DIR", tmp_path)
    board = build_board(run(clock=AS_OF, config=config, options=RunOptions(shipment_count=150, seed=7)))
    route = next(r for r in board["routes"] if r["route_id"] == "LANE_IN_01")
    assert route["early_warning"]["orders"] == 8
    assert route["level"] in ("white", "blue", "yellow", "red")
    if route["level"] == "white":
        assert route["reason"].startswith("Early warning")
    assert board["order_signals"]["bursts"][0]["flow"] == "CH_IN"
