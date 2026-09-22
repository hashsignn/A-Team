"""The source layer: free, keyless, honest when absent, and refusing loudly.

The rule these tests exist to hold: nothing in the shipped catalogue can cost
money, leak a credential, or fail silently.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from engine.config import load_config
from engine.ingest.observations import FeedStatus
from engine.ingest.sources import (
    CATALOG,
    Auth,
    Cost,
    FieldMap,
    Nature,
    SourceConfigError,
    SourceSpec,
    build_gdelt_query,
    collect,
    load_sources,
    network_allowed,
    places,
    to_items,
)
from engine.ingest.sources.loader import _custom
from engine.ingest.sources.mapping import items_of, parse_moment, resolve

AS_OF = dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC)
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example"


@pytest.fixture(scope="module")
def config():
    return load_config(EXAMPLE)


# =====================================================================
# The money rule
# =====================================================================
def test_nothing_in_the_catalogue_costs_money():
    """The whole point. A paid source cannot ship enabled, or a planner who
    ran the demo would find out from an invoice."""
    paid = [s.key for s in CATALOG if s.cost is Cost.PAID]
    assert not paid, f"paid sources in the shipped catalogue: {paid}"


def test_every_enabled_source_is_free_and_keyless():
    for spec in CATALOG:
        if not spec.enabled:
            continue
        assert spec.cost is Cost.FREE, f"{spec.key} is enabled but {spec.cost.value}"
        assert spec.auth.kind == "none", f"{spec.key} is enabled but needs auth"


def test_sources_needing_a_key_ship_disabled():
    for spec in CATALOG:
        if spec.cost is Cost.FREE_WITH_KEY:
            assert not spec.enabled, (
                f"{spec.key} needs a registration and must not ship enabled — "
                "it would show up ABSENT with a confusing reason"
            )


# A value that looks like a real credential: a long opaque run of characters,
# or a known key prefix followed by one. Deliberately NOT a bare substring
# search — "sk-" appears inside "supply-chain-risk-radar", and a check that
# cries wolf on the word "risk" in a risk tool is a check somebody deletes.
_SECRETISH = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|Bearer\s+\S{8,}|[A-Za-z0-9_\-]{32,})"
)


def test_no_spec_carries_a_credential():
    """A token in a spec is a token in the repository.

    Checks the places a value could actually hide — auth, params, headers, url
    — rather than the whole repr, so the assertion means what it says.
    """
    for spec in CATALOG:
        assert not getattr(spec.auth, "value", None), f"{spec.key}: auth holds a value"
        assert spec.auth.env == spec.auth.env.upper(), (
            f"{spec.key}: auth.env must be a variable NAME, got {spec.auth.env!r}"
        )
        candidates = [spec.url, *spec.params.values(), *spec.headers.values()]
        for value in candidates:
            hit = _SECRETISH.search(str(value))
            assert hit is None, (
                f"{spec.key} carries something that looks like a secret: "
                f"{hit.group(0)[:12]}…"
            )


def test_the_credential_check_would_actually_catch_one():
    """A guard nobody has seen fire is a guard nobody should trust."""
    assert _SECRETISH.search("sk-livedeadbeefdeadbeefdeadbeef")
    assert _SECRETISH.search("Bearer abcdef123456")
    assert not _SECRETISH.search("supply-chain-risk-radar")


def test_every_runnable_source_has_a_fixture():
    """Without one it is ABSENT in this environment, and the demo has a hole."""
    for spec in CATALOG:
        if spec.runnable:
            assert spec.fixture, f"{spec.key} is runnable but has no fixture"


def test_fixtures_exist_on_disk():
    root = Path(__file__).resolve().parent.parent / "data" / "fixtures"
    for spec in CATALOG:
        if spec.runnable and spec.fixture:
            assert (root / spec.fixture).exists(), f"missing fixture {spec.fixture}"


# =====================================================================
# The spec refuses bad input
# =====================================================================
def test_a_bad_key_is_refused():
    with pytest.raises(ValueError, match="lowercase"):
        SourceSpec(key="Bad Key", label="x", nature=Nature.REPORT, source_tier=2,
                   url="", items_path="", fields=FieldMap(headline="t"))


def test_a_bad_tier_is_refused():
    """The corroboration cap reads source_tier; a tier of 9 would cap nothing."""
    with pytest.raises(ValueError, match="source_tier"):
        SourceSpec(key="ok", label="x", nature=Nature.REPORT, source_tier=9,
                   url="", items_path="", fields=FieldMap(headline="t"))


def test_a_source_needing_an_unset_variable_is_not_runnable():
    spec = SourceSpec(key="k", label="x", nature=Nature.REPORT, source_tier=2,
                      url="https://x", items_path="", fields=FieldMap(headline="t"),
                      auth=Auth(kind="bearer", env="DEFINITELY_NOT_SET_ANYWHERE"))
    assert not spec.runnable
    assert "DEFINITELY_NOT_SET_ANYWHERE" in spec.why_not_runnable()


# =====================================================================
# The path language
# =====================================================================
def test_paths_resolve_through_dicts_and_lists():
    blob = {"a": {"b": [{"c": 7}]}}
    assert resolve(blob, "a.b[0].c") == 7


def test_a_missing_path_is_none_and_never_raises():
    """Somebody else's JSON changes without warning. It must not take the
    board down — a missing field drops one item, not the feed."""
    blob = {"a": 1}
    for path in ("nope", "a.b.c", "a[4]", "a.b[0].c.d"):
        assert resolve(blob, path) is None


def test_a_const_path_supplies_a_literal():
    assert resolve({}, "const:GDACS") == "GDACS"


def test_an_empty_items_path_means_the_body_is_the_list():
    assert len(items_of([1, 2, 3], "")) == 3


@pytest.mark.parametrize(("value", "fmt", "year"), [
    ("20260921T143000Z", "gdelt", 2026),
    ("20260916", "compact_date", 2026),
    ("2026-09-18T04:15:00+00:00", "iso", 2026),
    ("2026-09-18T04:15:00Z", "iso", 2026),
    (1758168000000, "epoch_ms", 2025),
])
def test_every_date_format_these_feeds_use(value, fmt, year):
    parsed = parse_moment(value, fmt)
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed.year == year


def test_an_unreadable_date_is_none_not_nineteen_seventy():
    """1970 would silently fail the temporal filter and drop the item."""
    assert parse_moment("not a date", "iso") is None
    assert parse_moment("", "gdelt") is None


# =====================================================================
# The mapper
# =====================================================================
def _spec(**kw):
    base = dict(key="t", label="T", nature=Nature.REPORT, source_tier=2,
                url="", items_path="items", fields=FieldMap(headline="title"))
    base.update(kw)
    return SourceSpec(**base)


def test_an_item_with_no_headline_is_dropped_and_counted():
    """An empty string is not a report of anything, and the funnel would spend
    a model call on it. The count is what stops a stale spec being invisible."""
    blob = {"items": [{"title": "real"}, {"other": "x"}, {"title": "  "}]}
    items, dropped = to_items(blob, _spec(), AS_OF)
    assert len(items) == 1
    assert dropped == 2


def test_item_ids_are_short_and_stable_even_when_the_id_is_a_url():
    url = "https://example.com/a/very/long/path?with=query&and=more#fragment"
    blob = {"items": [{"title": "t", "u": url}]}
    first, _ = to_items(blob, _spec(fields=FieldMap(headline="title", identifier="u")), AS_OF)
    second, _ = to_items(blob, _spec(fields=FieldMap(headline="title", identifier="u")), AS_OF)
    assert first[0]["item_id"] == second[0]["item_id"], "ids must be stable across polls"
    assert len(first[0]["item_id"]) < 30


def test_nature_is_carried_onto_every_item():
    items, _ = to_items({"items": [{"title": "t"}]}, _spec(nature=Nature.INSTRUMENT), AS_OF)
    assert items[0]["source_nature"] == "instrument"


def test_source_modes_are_carried_onto_every_item():
    items, _ = to_items({"items": [{"title": "t"}]}, _spec(modes=("road",)), AS_OF)
    assert items[0]["source_modes"] == ["road"]


# =====================================================================
# Fetching
# =====================================================================
def test_network_is_off_by_default():
    """A tool that starts calling nine external services on first run cannot
    be deployed inside a corporate network without a surprise conversation."""
    assert not network_allowed()


def test_an_unreachable_source_reports_absent_rather_than_raising():
    spec = _spec(url="https://nothing.invalid/x", fixture="")
    items, report = collect(spec, AS_OF)
    assert items == []
    assert report.status is FeedStatus.ABSENT
    assert report.detail


def test_a_source_with_a_fixture_falls_back_and_says_so():
    spec = next(s for s in CATALOG if s.key == "gdelt_doc")
    items, report = collect(spec, AS_OF)
    assert report.status is FeedStatus.FIXTURE
    assert items, "the fixture should produce items"
    assert "recorded sample" in report.detail


# =====================================================================
# The custom-source loader
# =====================================================================
def test_a_credential_in_the_yaml_is_refused():
    """.gitignore protects a file somebody might still email. A value that was
    never in the file cannot be emailed."""
    with pytest.raises(SourceConfigError, match="credential"):
        _custom({"key": "x", "label": "L", "url": "u",
                 "fields": {"headline": "h"}, "auth": {"token": "sk-live-abc"}})


def test_a_custom_source_without_a_headline_mapping_is_refused():
    with pytest.raises(SourceConfigError, match="headline"):
        _custom({"key": "x", "label": "L", "url": "u", "fields": {"body": "b"}})


def test_an_unknown_field_name_is_refused_with_the_known_ones_listed():
    with pytest.raises(SourceConfigError) as exc:
        _custom({"key": "x", "label": "L", "url": "u",
                 "fields": {"headline": "h", "hedline": "typo"}})
    assert "hedline" in str(exc.value)
    assert "headline" in str(exc.value)


def test_a_builtin_cannot_have_its_parsing_overridden(tmp_path):
    """Changing how a built-in parses would silently diverge from the fixture
    it is tested against. Copy it into custom: under a new key instead."""
    path = tmp_path / "sources.yaml"
    path.write_text("builtin:\n  gdelt_doc:\n    fields:\n      headline: nope\n")
    with pytest.raises(SourceConfigError, match="cannot override"):
        load_sources(path)


def test_patching_an_unknown_builtin_names_the_real_ones(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("builtin:\n  gdlet_doc:\n    enabled: false\n")
    with pytest.raises(SourceConfigError) as exc:
        load_sources(path)
    assert "gdelt_doc" in str(exc.value)


def test_the_shipped_config_loads_and_adds_custom_slots():
    specs = load_sources(EXAMPLE / "sources.yaml")
    keys = {s.key for s in specs}
    assert {"gdelt_doc", "gdacs", "usgs_quakes"} <= keys
    assert {"tms_events", "nap_road_incidents"} <= keys, "custom examples missing"
    assert all(not s.enabled for s in specs if not s.builtin), (
        "a custom example must ship disabled — its URL does not exist yet"
    )


def test_no_config_at_all_is_a_complete_configuration():
    assert len(load_sources(None)) == len(CATALOG)


# =====================================================================
# The GDELT query — the funnel layer that costs zero bytes
# =====================================================================
def test_the_query_filters_on_both_place_and_disruption():
    """Geography alone returns the shipping news; a disruption word alone
    returns every strike on earth. The AND is what makes it affordable."""
    query = build_gdelt_query()
    assert " AND " in query
    assert query.count("(") >= 2


def test_the_query_asks_about_hormuz():
    assert "Hormuz" in build_gdelt_query()


def test_the_query_stays_inside_its_budget():
    """GDELT rejects an over-long query rather than truncating it."""
    long_geo = tuple(f"Placename Number {i}" for i in range(400))
    assert len(build_gdelt_query(geo_terms=long_geo)) <= 1800


# =====================================================================
# Place resolution — layer 1 for text sources
# =====================================================================
def test_a_wire_story_resolves_to_a_node(config):
    assert places.resolve_nodes(
        "Iran announces closure of the Strait of Hormuz", config
    ) == ["CHOKE_HORMUZ"]


@pytest.mark.parametrize("decoy", [
    "Suezmax tanker rates climb again",
    "Basel III capital rules tightened",
    "The New York Times reported yesterday",
])
def test_lookalikes_do_not_resolve(decoy, config):
    """Substring matching would make Suez match Suezmax — a vessel class, not
    a canal. Every false node here manufactures an event."""
    assert places.resolve_nodes(decoy, config) == []


def test_a_source_supplied_hint_is_never_overwritten(config):
    """A feed that told us the UN/LOCODE knows better than a string search."""
    items = [{"text": "Trouble at Rotterdam", "node_hint": ["BEANR"]}]
    places.enrich(items, config)
    assert items[0]["node_hint"] == ["BEANR"]


def test_text_naming_nothing_resolves_to_nothing(config):
    assert places.resolve_nodes("Markets rallied on optimism", config) == []


# =====================================================================
# Asking a source about a PAST window
# =====================================================================
def test_gdelt_can_be_asked_about_a_window_that_already_happened():
    """Without this every request means "the last few hours from whenever you
    happen to be running", which makes replaying a past day impossible."""
    from datetime import UTC, datetime

    from engine.ingest.sources import catalog

    spec = catalog.by_key("gdelt_doc")
    assert spec.window is not None

    add, drop = spec.window.params_for(datetime(2026, 9, 16, tzinfo=UTC), 60)
    assert add["startdatetime"] == "20260718000000"
    assert add["enddatetime"] == "20260916000000"


def test_the_relative_window_is_dropped_when_a_date_range_is_asked_for():
    """GDELT silently returns the RELATIVE window if both are sent, which
    looks exactly like the historical query working."""
    from datetime import UTC, datetime

    from engine.ingest.sources import catalog

    spec = catalog.by_key("gdelt_doc")
    assert "timespan" in spec.params, "the live default is still relative"

    add, drop = spec.window.params_for(datetime(2026, 9, 16, tzinfo=UTC), 60)
    params = {k: v for k, v in spec.params.items() if k not in drop}
    params.update(add)
    assert "timespan" not in params


def test_a_source_with_no_window_is_left_alone():
    """Most sources only answer about now, and saying so is better than
    sending them a parameter they will ignore."""
    from engine.ingest.sources import catalog

    assert catalog.by_key("usgs_quakes").window is None


@pytest.mark.parametrize("fmt,expected", [
    ("gdelt", "20260916000000"),
    ("iso", "2026-09-16T00:00:00Z"),
    ("date", "2026-09-16"),
])
def test_window_stamps_match_each_api_s_convention(fmt, expected):
    from datetime import UTC, datetime

    from engine.ingest.sources.spec import Window

    window = Window(start_param="s", format=fmt)
    assert window.stamp(datetime(2026, 9, 16, tzinfo=UTC)) == expected
