"""USGS earthquakes, read by their measured magnitude.

The first real recording held 1,310 earthquakes and not one became an event:
every title reads "M 4.9 - 78 km ESE of Kokopo", which has no word the
keyword router knows. The feed now declares what each item is, and the
magnitude — a number a seismometer measured — decides the severity by table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import pipeline
from engine.clock import Clock
from engine.config import load_config
from engine.ingest.sources.catalog import by_key
from engine.ingest.sources.mapping import to_items
from engine.network.graph import Network
from engine.schemas import Severity

AS_OF = datetime(2026, 9, 26, 23, tzinfo=UTC)
ROTTERDAM = (51.95, 4.14)


@pytest.fixture(scope="module")
def config():
    return load_config()


def _quake(mag, where=ROTTERDAM, ident="q1"):
    """One feature as the fdsnws query answers it."""
    lat, lon = where
    return {
        "type": "Feature", "id": ident,
        "properties": {"mag": mag, "place": "near a test point",
                       "time": int((AS_OF - timedelta(hours=1)).timestamp() * 1000),
                       "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{ident}",
                       "title": f"M {mag} - near a test point"},
        "geometry": {"type": "Point", "coordinates": [lon, lat, 10]},
    }


def _items(*features):
    return to_items({"features": list(features)}, by_key("usgs_quakes"), AS_OF)[0]


def test_every_usgs_item_is_declared_an_earthquake():
    assert by_key("usgs_quakes").declares == ("FOR_EARTHQUAKE",)
    [item] = _items(_quake(6.1))
    assert item["declared_variables"] == ["FOR_EARTHQUAKE"]
    assert float(item["severity_hint"]) == 6.1


@pytest.mark.parametrize(("mag", "severity"), [
    (6.0, Severity.MODERATE), (6.9, Severity.MODERATE),
    (7.0, Severity.SEVERE), (7.8, Severity.SEVERE),
])
def test_the_magnitude_sets_the_severity(config, mag, severity):
    [item] = _items(_quake(mag))
    routed = pipeline._declared_route(item, config)
    assert not routed.abstained
    assert routed.active_variables[0] == "FOR_EARTHQUAKE"
    assert routed.severity is severity
    assert routed.router == "source"
    # The quote is the feed's own title, not a phrase the router found.
    assert routed.matched_spans["FOR_EARTHQUAKE"] == item["headline"]


@pytest.mark.parametrize("mag", [4.5, 5.2, 5.99, None])
def test_below_the_floor_or_unmeasured_raises_nothing(config, mag):
    [item] = _items(_quake(mag))
    routed = pipeline._declared_route(item, config)
    assert routed.abstained
    assert routed.active_variables == []
    assert "earthquake_magnitude" in routed.abstain_reason


def test_the_table_is_read_from_thresholds_yaml(config, monkeypatch):
    """The bands are declared, not coded: a planner who wants M5.5 to count
    changes a number in thresholds.yaml, not the engine."""
    monkeypatch.setitem(config.thresholds, "earthquake_magnitude", {
        "ignore_below": 5.5, "bands": [{"at_least": 5.5, "severity": "severe"}]})
    [item] = _items(_quake(5.6))
    assert pipeline._declared_route(item, config).severity is Severity.SEVERE


def test_a_small_quake_is_not_sent_to_a_model(config):
    """A M5 the table ruled out is not something the router failed to read,
    so it is not a rescue candidate: the budget of careful reads is not
    spent asking a model the table's question again."""
    small, large = _items(_quake(5.2, ident="small"), _quake(6.4, ident="large"))
    events, counts, notes, _ = pipeline._to_events(
        [], [small, large], config, Network(config), Clock(AS_OF), pipeline.RunOptions())
    assert counts["rescue_considered"] == 0
    assert "floor" in notes[small["item_id"]]
    [event] = [e for e in events if "FOR_EARTHQUAKE" in e.active_variables]
    assert event.event_id == large["item_id"]
    assert event.severity is Severity.MODERATE
    assert event.provenance.verbatim_quote == "M 6.4 - near a test point"
