"""German motorway closures, read as what they are and when they are.

The first real recording carried 45 closures on the A3, A5 and A61. None
became an event: the English keyword router could not read "A3 | Sandgraben
- Würzburg/Kist" as a closure, so each went to a model — which, on a budget
of six reads, spent them all elsewhere. And with no end field, a six-hour
night closure would have been given the mapper's default week.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import pipeline
from engine.config import load_config
from engine.ingest.sources import autobahn
from engine.ingest.sources.catalog import by_key
from engine.ingest.sources.mapping import to_items
from engine.schemas import Severity

AS_OF = datetime(2026, 9, 26, 23, tzinfo=UTC)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.fixture(scope="module")
def config():
    return load_config()


# --------------------------------------------------------------- the prose
def test_begin_and_end_are_read_in_german_time():
    got = autobahn.windows(["Zeitraum dieser Bauphase:",
                            "Beginn: 05.10.26 um 00:00 Uhr", "Ende: 27.11.26 um 18:00 Uhr"])
    # 00:00 CEST is 22:00 UTC the day before; 18:00 CET in November is 17:00 UTC.
    assert got == [(utc(2026, 10, 4, 22), utc(2026, 11, 27, 17))]


def test_a_night_closure_is_six_hours_not_a_week():
    got = autobahn.windows(["Die Baustelle ist zu folgenden Zeiträumen gültig:",
                            "27.09.26 von 00:00 bis 06:00 Uhr",
                            "(Ende der Gesamtmaßnahme: 27.09.26)"])
    assert got == [(utc(2026, 9, 26, 22), utc(2026, 9, 27, 4))]


def test_a_window_past_midnight_ends_the_next_day():
    got = autobahn.windows(["01.10.26 von 22:00 bis 05:00 Uhr"])
    assert got == [(utc(2026, 10, 1, 20), utc(2026, 10, 2, 3))]


def test_several_nights_are_read_as_their_envelope():
    blob = autobahn.decode({"closure": [{
        "title": "A61 | Im Weidenfeld - Koblenz",
        "subtitle": "Mönchengladbach -> Koblenz",
        "display_type": "CLOSURE",
        "description": ["Die Baustelle ist zu folgenden Zeiträumen gültig:",
                        "27.09.26 17:00 bis zum 28.09.26 06:00 Uhr.",
                        "28.09.26 19:00 bis zum 29.09.26 06:00 Uhr."],
    }]})
    closure = blob["closure"][0]
    assert closure["_starts"] == utc(2026, 9, 27, 15).isoformat()
    assert closure["_ends"] == utc(2026, 9, 29, 4).isoformat()
    # The body says they are nights, so the envelope is not read as one stoppage.
    assert "27 Sep 15:00–28 Sep 04:00 UTC; 28 Sep 17:00–29 Sep 04:00 UTC" in closure["_body"]
    assert "carriageway closed" in closure["_body"]


def test_only_the_overall_end_is_the_end_of_that_day():
    assert autobahn.whole_end(["(Ende der Gesamtmaßnahme: 27.09.26)"]) == utc(2026, 9, 27, 22)


def test_summer_time_follows_the_eu_rule():
    # 2026: summer time from Sunday 29 March to Sunday 25 October.
    assert autobahn.berlin(2026, 3, 29, 1).utcoffset() == timedelta(hours=1)
    assert autobahn.berlin(2026, 3, 29, 3).utcoffset() == timedelta(hours=2)
    assert autobahn.berlin(2026, 10, 25, 1).utcoffset() == timedelta(hours=2)
    assert autobahn.berlin(2026, 10, 25, 4).utcoffset() == timedelta(hours=1)


# --------------------------------------------------------------- the items
def _items(closures):
    return to_items({"closure": closures}, by_key("autobahn_a5"), AS_OF)[0]


def test_every_closure_is_declared_a_road_closure_and_keeps_its_window():
    [item] = _items([{
        "identifier": "X1", "title": "A5 Sperrung im Knotenpunkt - AS Appenweier",
        "subtitle": "Basel -> Karlsruhe", "display_type": "CLOSURE_ENTRY_EXIT",
        "coordinate": {"lat": "48.54", "long": "7.95"},
        "description": ["Beginn: 30.09.26 um 20:00 Uhr", "Ende: 01.10.26 um 05:00 Uhr"],
    }])
    assert item["declared_variables"] == ["INF_ROAD_CLOSURE"]
    assert item["starts_at"] == utc(2026, 9, 30, 18)
    assert item["ends_at"] == utc(2026, 10, 1, 3)
    assert "Basel -> Karlsruhe" in item["body"]


def test_a_planned_closure_is_known_now_not_when_it_starts():
    """Dated by its start, a closure from next month was 'published after the
    as-of' and refused until it began — the lead time thrown away."""
    [item] = _items([{
        "identifier": "X2", "title": "A5 von Rust (AS) nach Riegel (AS) Erneuerung der Fahrbahn",
        "startTimestamp": "2026-10-05T00:00:00+02:00", "future": True,
        "description": ["Beginn: 05.10.26 um 00:00 Uhr", "Ende: 27.11.26 um 18:00 Uhr"],
    }])
    assert item["published_at"] == AS_OF
    assert item["starts_at"] == utc(2026, 10, 4, 22)


def test_the_router_is_not_asked_whether_a_closure_is_a_closure(config):
    [item] = _items([{
        "identifier": "X3", "title": "A3 | Sandgraben - Würzburg/Kist",
        "subtitle": "Nürnberg -> Frankfurt", "display_type": "CLOSURE",
        "description": ["27.09.26 von 00:00 bis 06:00 Uhr"],
    }])
    routed = pipeline._declared_route(item, config)
    assert routed is not None and not routed.abstained
    assert routed.active_variables == ["INF_ROAD_CLOSURE"]
    assert routed.router == "source"
    assert routed.matched_spans["INF_ROAD_CLOSURE"] == "A3 | Sandgraben - Würzburg/Kist"
    assert routed.severity is Severity.MINOR            # one night


def test_a_carriageway_shut_for_days_is_moderate_a_ramp_is_minor(config):
    long_closure, ramp = _items([
        {"identifier": "L", "title": "A61 | Kaldenkirchen - Grenzwald", "display_type": "CLOSURE",
         "description": ["Beginn: 01.10.26 um 22:00 Uhr", "Ende: 05.10.26 um 05:00 Uhr"]},
        {"identifier": "R", "title": "A5 BRI Nordwestkreuz Frankfurt",
         "display_type": "CLOSURE_ENTRY_EXIT",
         "description": ["Beginn: 18.05.26 um 15:30 Uhr", "Ende: 07.11.26 um 15:30 Uhr"]},
    ])
    assert pipeline._declared_route(long_closure, config).severity is Severity.MODERATE
    assert pipeline._declared_route(ramp, config).severity is Severity.MINOR


def test_an_ordinary_news_item_is_left_to_the_router(config):
    item = {"declared_variables": [], "text": "Dockworkers strike at Antwerp",
            "headline": "Dockworkers strike at Antwerp"}
    assert pipeline._declared_route(item, config) is None


# ------------------------------------------------------- the careful reads
def test_the_careful_reader_sees_the_freight_news_first():
    """At six reads in feed order, a celebrity trial and a theme-park lawsuit
    took the budget; the closures behind them were never read."""
    base = {"source_tier": 2, "published_at": AS_OF}
    trial = {**base, "item_id": "trial", "node_hint": [], "triage": {"freight_mode": "none"}}
    storm = {**base, "item_id": "storm", "node_hint": [], "triage": {"freight_mode": "sea"}}
    port = {**base, "item_id": "port", "node_hint": ["NLRTM"], "triage": {"freight_mode": "sea"}}
    older = {**base, "item_id": "older", "node_hint": ["NLRTM"], "triage": {"freight_mode": "sea"},
             "published_at": AS_OF - timedelta(days=3)}
    order = [i["item_id"] for i in pipeline._reading_order([trial, storm, older, port])]
    assert order == ["port", "older", "storm", "trial"]


def test_twelve_careful_reads():
    assert pipeline.RunOptions().max_rescued_events == 12


def test_a_direction_of_travel_is_not_where_the_closure_is(config):
    """"Basel -> Karlsruhe" names where the carriageway goes, not where it is
    closed: a closure at Rust landed on a route whose road leg ends in Basel."""
    from engine.ingest.sources import places

    [item] = _items([{
        "identifier": "D1", "title": "A5 von Rust (AS) nach Riegel (AS) Erneuerung der Fahrbahn",
        "subtitle": "Basel -> Karlsruhe", "display_type": "CLOSURE_ENTRY_EXIT",
        "coordinate": {"lat": "48.27", "long": "7.74"},
        "description": ["Beginn: 05.10.26 um 00:00 Uhr", "Ende: 27.11.26 um 18:00 Uhr"],
    }])
    assert "Basel" in item["text"]
    places.enrich([item], config)
    assert item["node_hint"] == []
    # Without coordinates, the words are all there is, and they are read.
    words = {"text": "Strike at the port of Rotterdam", "headline": "x",
             "lat": None, "lon": None, "node_hint": []}
    places.enrich([words], config)
    assert words["node_hint"] == ["NLRTM"]


def test_the_corridor_between_two_places_stops_at_them():
    """A 60 km corridor from Düdingen to Basel took in the A5 at Freiburg,
    45 km past Basel, on a road a truck to Basel never drives."""
    from engine.network.geo import Point, great_circle_points, on_corridor

    # Densified, as the network draws a road leg: every inner segment clamps
    # to its own end, so "not past the last segment" alone is not enough.
    path = great_circle_points(Point(46.85, 7.19), Point(47.56, 7.60), 12)  # Düdingen → Basel
    freiburg = Point(47.96, 7.78)                              # past Basel
    alongside = Point(47.20, 7.55)                             # beside the leg
    assert on_corridor(freiburg, path, "road", 60.0)[0]        # the old reach
    assert not on_corridor(freiburg, path, "road", 60.0, ends=False)[0]
    assert on_corridor(alongside, path, "road", 60.0, ends=False)[0]
