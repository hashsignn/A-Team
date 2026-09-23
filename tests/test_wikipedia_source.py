"""A second news archive: Wikipedia's Current events portal, read offline.

GDELT refused the first real recordings from both networks tried, and a news
layer that depends on one archive answering fails exactly when it is needed.
The portal is the other trade — dozens of curated items a day rather than
thousands of matched ones — served free and keyless by the Wikimedia API.

The parser was written without a live page (the build environment blocks the
host), so these tests pin it to the portal's layout as documented, including
the older and styled variants of a heading. The recording keeps each day's
page raw and the board parses it on load, so a parser fix improves an old
recording without fetching it again.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.ingest.sources import fetch, places, wikipedia
from engine.ingest.sources.catalog import by_key
from engine.ingest.sources.decoders import DECODERS
from engine.ingest.sources.mapping import to_items

ROOT = Path(__file__).resolve().parent.parent
AS_OF = datetime(2026, 9, 22, tzinfo=UTC)

PAGE = """{{Current events|year=2026|month=9|day=21|content=
<!-- All news items below this line -->
'''Armed conflicts and attacks'''
*[[Red Sea crisis]]
**[[Houthi attacks on commercial shipping|Houthi attacks on shipping]]
***The [[Houthis]] strike the [[Liberia]]n-flagged bulk carrier ''Example Star'' with two missiles; the crew abandons ship. ([https://www.reuters.com/world/houthis-2026-09-21/ Reuters]) ([https://apnews.com/article/red-sea AP])
*[[Russian invasion of Ukraine]]
**A drone attack sets fire to fuel tanks at an oil depot in {{ill|Ust-Luga|ru|Усть-Луга}}. {{flagicon|Russia}} ([https://www.bbc.com/news/world-europe-1 BBC News])

'''Business and economy'''
*Dockworkers at the [[Port of Antwerp-Bruges|Port of Antwerp]] begin a 48-hour strike over pension reforms.<ref>{{cite web|url=https://example.org}}</ref> ([https://www.brusselstimes.com/antwerp-strike The Brussels Times])
*The [[European Commission]] fines a shipping alliance {{US$|1.2 billion}} for price coordination. ([https://www.ft.com/content/abc Financial Times])

;Disasters and accidents
*A fire at a chemical plant near [[Ludwigshafen]] forces the evacuation of {{convert|3|km|mi}} around the site. ([https://www.dw.com/en/fire DW])

<div class="current-events-content-heading" role="heading" aria-level="2">International relations</div>
*[[Iran]] threatens to close the [[Strait of Hormuz]] if new sanctions are imposed. ([https://www.aljazeera.com/news/iran Al Jazeera])

'''Sports'''
*[[FC Basel]] win the [[Swiss Cup]] final. ([https://www.srf.ch/sport SRF])
<!-- All news items above this line -->|}}"""


def answer(text=PAGE, title="Portal:Current events/2026 September 21", version=2):
    wikitext = text if version == 2 else {"*": text}
    return {"parse": {"title": title, "pageid": 1, "wikitext": wikitext}}


@pytest.fixture(scope="module")
def spec():
    return by_key("wikipedia_events")


@pytest.fixture(scope="module")
def events():
    return wikipedia.decode(answer())["events"]


def _by(events, fragment):
    return next(e for e in events if fragment in e["headline"])


@pytest.fixture(scope="module")
def recorder():
    path = ROOT / "scripts" / "record_fixture.py"
    module_spec = importlib.util.spec_from_file_location("record_fixture", path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["record_fixture"] = module      # before it runs, for @dataclass
    module_spec.loader.exec_module(module)
    return module


# =====================================================================
# Asking for a day
# =====================================================================
def test_a_page_is_named_the_way_the_portal_names_it():
    assert wikipedia.page_title(date(2026, 9, 1)) == "Portal:Current events/2026 September 1"
    assert wikipedia.day_of("Portal:Current events/2026 September 1") == date(2026, 9, 1)
    assert wikipedia.day_of("Portal:Current events/September 2026") is None
    assert wikipedia.day_of("Main Page") is None


def test_the_window_asks_for_the_last_whole_day_before_the_as_of(spec):
    params, _ = spec.window.params_for(AS_OF, 1.0)
    assert params == {"page": "Portal:Current events/2026 September 21"}
    params, _ = spec.window.params_for(datetime(2026, 9, 1, tzinfo=UTC), 1.0)
    assert params == {"page": "Portal:Current events/2026 August 31"}


def test_the_month_is_english_whatever_the_machine_speaks(spec, monkeypatch):
    """strftime("%B") follows the locale; "2026 septembre 21" is no page."""
    import locale
    for name in ("fr_FR.UTF-8", "de_DE.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    try:
        params, _ = spec.window.params_for(AS_OF, 1.0)
    finally:
        locale.setlocale(locale.LC_TIME, "C")
    assert params["page"].endswith("September 21")


def test_the_source_is_free_keyless_and_names_itself(spec):
    assert spec.runnable
    assert spec.cost.value == "free"
    assert "github.com/hashsignn/A-Team" in spec.headers["User-Agent"]
    assert spec.decode in DECODERS


# =====================================================================
# Reading a page
# =====================================================================
def test_every_cited_line_under_a_freight_heading_is_an_event(events):
    assert [e["category"] for e in events] == [
        "Armed conflicts and attacks", "Armed conflicts and attacks",
        "Business and economy", "Business and economy",
        "Disasters and accidents", "International relations",
    ]


def test_sport_is_dropped_before_it_can_name_a_node(events):
    """FC Basel is a football club. Left in, "Basel" would put it on the board."""
    assert not any("Swiss Cup" in e["headline"] for e in events)


def test_a_line_reads_as_a_reader_sees_it(events):
    houthis = _by(events, "Houthis")
    assert houthis["headline"] == (
        "The Houthis strike the Liberian-flagged bulk carrier Example Star with "
        "two missiles; the crew abandons ship"
    )
    assert _by(events, "drone")["headline"].endswith("an oil depot in Ust-Luga")
    assert _by(events, "Dockworkers")["headline"] == (
        "Dockworkers at the Port of Antwerp begin a 48-hour strike over pension reforms"
    )
    assert "US$1.2 billion" in _by(events, "fines")["headline"]
    assert "3 km around the site" in _by(events, "chemical")["headline"]


def test_the_topic_it_was_filed_under_is_kept_as_context(events):
    houthis = _by(events, "Houthis")
    assert houthis["context"] == "Red Sea crisis › Houthi attacks on shipping"
    assert _by(events, "Iran")["context"] == ""


def test_the_cited_outlet_is_the_source(events):
    houthis = _by(events, "Houthis")
    assert houthis["url"] == "https://www.reuters.com/world/houthis-2026-09-21/"
    assert houthis["source"] == "reuters.com"
    assert houthis["source_label"] == "Reuters"


def test_a_day_is_dated_midday_because_the_page_gives_no_time(events):
    assert {e["published"] for e in events} == {"2026-09-21T12:00:00Z"}


def test_the_older_api_format_reads_the_same(events):
    assert wikipedia.decode(answer(version=1))["events"] == events


def test_a_page_that_is_not_a_day_yields_nothing():
    assert wikipedia.decode(answer(title="Main Page"))["events"] == []
    assert wikipedia.decode({"error": {"code": "missingtitle"}})["events"] == []
    assert wikipedia.decode(None)["events"] == []


# =====================================================================
# Through the framework
# =====================================================================
def test_the_topic_is_what_puts_a_line_on_the_network(spec):
    """"The Houthis strike a bulk carrier" names no sea. Its topic does."""
    items, dropped = to_items(answer(), spec, AS_OF)
    assert dropped == 0
    houthis = next(i for i in items if "Houthis" in i["headline"])
    assert "Red Sea" not in houthis["headline"]
    assert "CHOKE_BAB" in places.resolve_nodes(houthis["text"], load_config())
    iran = next(i for i in items if "Iran" in i["headline"])
    assert places.resolve_nodes(iran["text"], load_config()) == ["CHOKE_HORMUZ"]


def test_items_carry_the_source_and_age_out_like_any_other_report(spec):
    items, _ = to_items(answer(), spec, AS_OF)
    item = items[0]
    assert item["source_key"] == "wikipedia_events"
    assert item["source_tier"] == 2
    assert item["published_at"] == datetime(2026, 9, 21, 12, tzinfo=UTC)
    assert item["default_ends_at"] == item["published_at"] + timedelta(days=7)


def test_an_error_in_the_body_is_a_refusal_not_a_quiet_day(spec, monkeypatch):
    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    body = json.dumps({"error": {"code": "ratelimited", "info": "slow down"}}).encode()
    monkeypatch.setattr(fetch.urllib.request, "urlopen", lambda *_a, **_k: Answer(body))
    assert fetch._get(spec, as_of=AS_OF, window_days=1.0) == (
        None, "Wikipedia API error: ratelimited"
    )
    assert wikipedia.refusal({"error": {"code": "missingtitle"}}) is None


def test_the_shipped_sample_reaches_nothing(spec):
    """The sample shows the shape. Its invented events name no place of ours."""
    blob = json.loads((ROOT / "tests" / "fixtures" / spec.fixture).read_text(encoding="utf-8"))
    items, _ = to_items(blob, spec, Clock.at("2026-09-18T06:00:00+00:00").as_of)
    assert items
    config = load_config()
    assert all(places.resolve_nodes(i["text"], config) == [] for i in items)


# =====================================================================
# Recording sixty days of it
# =====================================================================
class Portal:
    """Answers like the Wikimedia API: one page per day asked for, each with
    that day's own line and one story that is on every page."""

    def __init__(self):
        self.asked: list[str] = []
        self.timeouts: list = []

    def __call__(self, spec, as_of=None, window_days=None, timeout=None):
        params, _ = spec.window.params_for(as_of, window_days)
        page = params["page"]
        self.asked.append(page)
        self.timeouts.append(timeout)
        day = wikipedia.day_of(page)
        text = (
            "'''Business and economy'''\n"
            f"*Freight rates move on {day:%d %B}. ([https://news.test/{day} Wire])\n"
            "*A strike at the Port of Antwerp enters another week. "
            "([https://news.test/antwerp Wire])\n"
        )
        return answer(text, title=page), ""


def _record(recorder, spec, portal, days, tmp_path, monkeypatch, pauses=None):
    monkeypatch.setattr(recorder, "CACHE", tmp_path / "cache")
    return recorder.record_window(
        spec, AS_OF, days, fetch=portal,
        pause=(pauses.append if pauses is not None else (lambda _s: None)),
        say=lambda *_: None, now=AS_OF + timedelta(days=2),
    )


def test_each_day_is_its_own_request_and_its_own_page(recorder, spec, tmp_path, monkeypatch):
    portal = Portal()
    _record(recorder, spec, portal, 3, tmp_path, monkeypatch)
    assert portal.asked == [
        "Portal:Current events/2026 September 21",
        "Portal:Current events/2026 September 20",
        "Portal:Current events/2026 September 19",
    ]


def test_the_raw_pages_are_kept_and_read_when_the_board_loads(
    recorder, spec, tmp_path, monkeypatch,
):
    result = _record(recorder, spec, Portal(), 3, tmp_path, monkeypatch)
    blob = result.blob
    assert len(blob["days"]) == 3
    assert all("wikitext" in day["parse"] for day in blob["days"]), "raw, not parsed"

    items, _ = to_items(blob, spec, AS_OF)
    headlines = [i["headline"] for i in items]
    assert headlines.count("A strike at the Port of Antwerp enters another week") == 1
    assert len(items) == 4                      # three days' own lines + the story once
    assert blob["_window"]["records"] == 4


def test_it_is_paced_to_its_own_limit_not_gdelts(recorder, spec, tmp_path, monkeypatch):
    pauses: list[float] = []
    _record(recorder, spec, Portal(), 4, tmp_path, monkeypatch, pauses)
    assert pauses == [recorder.PAUSE_FOR["wikipedia_events"]] * 3
    assert recorder.pause_for(by_key("gdelt_doc")) == recorder.PAUSE_S


def test_a_recording_waits_longer_for_an_answer_than_the_board_does(
    recorder, spec, tmp_path, monkeypatch,
):
    """A slow archive timed out at the board's twelve seconds and read as
    unreachable. Recording is a batch job, and can wait."""
    portal = Portal()
    monkeypatch.setattr(recorder, "_get", portal)
    monkeypatch.setattr(recorder, "CACHE", tmp_path / "cache")
    recorder.record_window(spec, AS_OF, 1, pause=lambda _s: None,
                           say=lambda *_: None, now=AS_OF + timedelta(days=2))
    assert portal.timeouts == [recorder.RECORD_TIMEOUT_S]
    assert recorder.RECORD_TIMEOUT_S > fetch.TIMEOUT_S
