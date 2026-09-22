"""Calibrating the book against Sika's own order history.

The generator used to weight the Rhine up 3x because the demo needed
shipments there. These tests pin the properties that replaced that: the mix
comes from the export when it is present, the fallback is unchanged when it
is not, and neither state is misreported as the other.
"""

from __future__ import annotations

import textwrap

import pytest

from engine.config import load_config
from engine.ingest import flows as F


@pytest.fixture(scope="module")
def config():
    return load_config()


def write(tmp_path, body: str):
    path = tmp_path / "flows.yaml"
    path.write_text(textwrap.dedent(body))
    return path


SAMPLE = """
    flows:
      - lane: CH_CN
        origin_country: CH
        destination_country: CN
        documents: 2000
        monthly: {2025-01: 100, 2025-07: 50, 2026-01: 120}
      - lane: DE_US
        origin_country: DE
        destination_country: US
        documents: 500
        monthly: {2025-01: 30, 2025-07: 20}
    """


# ------------------------------------------------------------- absent
def test_no_export_is_reported_as_absent_not_faked(tmp_path):
    flows = F.load(tmp_path / "nothing.yaml")
    assert flows.available is False
    assert "not present" in flows.detail
    assert flows.report()["status"] == "absent"


def test_without_an_export_the_old_rule_is_unchanged(config, tmp_path):
    """A machine with no export must behave exactly as before — this is
    gitignored, so most machines are that machine."""
    weights = F.lane_weights(config, F.load(tmp_path / "nothing.yaml"))
    for lane, weight in zip(config.lanes, weights, strict=True):
        expected = F.ANCHOR_WEIGHT if lane.get("focus") == "rhine" else 1.0
        assert weight == expected


def test_a_corrupt_export_is_an_absent_one_not_a_crash(tmp_path):
    path = tmp_path / "flows.yaml"
    path.write_text("{ this is not: [valid")
    assert F.load(path).available is False


def test_an_export_with_no_usable_rows_is_absent(tmp_path):
    assert F.load(write(tmp_path, "flows: []\n")).available is False


# -------------------------------------------------------------- present
def test_an_export_is_read_and_reported_with_its_span(tmp_path):
    flows = F.load(write(tmp_path, SAMPLE))
    assert flows.available is True
    assert flows.documents == 2500
    assert flows.by_pair[("CH", "CN")] == 2000
    assert "2,500 purchase document(s)" in flows.detail
    assert flows.report()["status"] == "connected"


def test_the_mix_follows_the_real_volume(config, tmp_path):
    flows = F.load(write(tmp_path, SAMPLE))
    weights = F.lane_weights(config, flows)
    pairs = [F.pair_of(lane, config) for lane in config.lanes]

    ch_cn = [w for p, w in zip(pairs, weights, strict=True) if p == ("CH", "CN")]
    de_us = [w for p, w in zip(pairs, weights, strict=True) if p == ("DE", "US")]
    assert ch_cn and de_us
    assert sum(ch_cn) > sum(de_us), "the busier pair must be drawn more often"


def test_one_pair_served_by_several_lanes_shares_its_volume(config, tmp_path):
    """Stuttgart→Shanghai and Stuttgart→Ningbo are both DE→CN. Counting the
    pair's documents once per lane would make it look twice as busy."""
    flows = F.load(write(tmp_path, SAMPLE))
    weights = F.lane_weights(config, flows)
    pairs = [F.pair_of(lane, config) for lane in config.lanes]

    serving = [w for p, w in zip(pairs, weights, strict=True) if p == ("CH", "CN")]
    assert len(serving) > 1, "the fixture network serves CH→CN with several lanes"
    assert sum(serving) == pytest.approx(2000, rel=0.01)


def test_a_lane_with_no_real_traffic_is_floored_not_deleted(config, tmp_path):
    """The first version used a flat 0.15 against counts in the thousands,
    which is not a floor — it is deletion with extra steps."""
    flows = F.load(write(tmp_path, SAMPLE))
    weights = F.lane_weights(config, flows)
    matched = [w for w in weights if w > 100]
    floored = [w for w in weights if w <= 100]
    assert floored, "the fixture has lanes with no counterpart"
    assert min(floored) > 0, "a floored lane must still be drawable"
    assert min(floored) == pytest.approx(
        (sum(matched) / len(matched)) * F.FLOOR_SHARE_OF_MEAN, rel=0.3
    )


def test_every_lane_keeps_a_weight(config, tmp_path):
    weights = F.lane_weights(config, F.load(write(tmp_path, SAMPLE)))
    assert len(weights) == len(config.lanes)
    assert all(w > 0 for w in weights)


# ----------------------------------------------------------- seasonality
def test_seasonality_is_by_month_of_year_not_by_calendar_month(tmp_path):
    """The board runs at an as-of the export may not cover, so the useful
    signal is the seasonal shape rather than a specific month."""
    months = F.month_weights(F.load(write(tmp_path, SAMPLE)))
    assert set(months) <= {f"{m:02d}" for m in range(1, 13)}
    assert months["01"] > months["07"], "January is busier in the fixture"


def test_seasonality_is_normalised_around_one(tmp_path):
    months = F.month_weights(F.load(write(tmp_path, SAMPLE)))
    assert months
    assert sum(months.values()) / len(months) == pytest.approx(1.0, abs=0.01)


def test_no_export_means_no_seasonality_claim(tmp_path):
    assert F.month_weights(F.load(tmp_path / "nothing.yaml")) == {}


# --------------------------------------------------------- what it claims
def test_the_report_does_not_claim_value_or_dates(tmp_path):
    """The export ends at the purchase document. Saying otherwise because a
    spreadsheet arrived is the easiest lie available here."""
    text = F.load(write(tmp_path, SAMPLE)).report()["unlocks_if_connected"]
    assert "stay declared" in text
    assert "not in the export" in text
