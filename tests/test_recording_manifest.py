"""A recording says what it holds, and the board shows nothing else.

When GDELT could not be reached, the recorder used to leave its fixture as
it was — which, on a first recording, is the sample: "Iran announces closure
of the Strait of Hormuz", invented, dated 18 September. Beside real Wikipedia
events and a real Rhine, that invented headline would be read as real.

So a --all run writes data/fixtures/_recording.json, naming every source it
tried and whether it was recorded, and the board leaves out any source that
was not — reporting why — instead of serving its sample. The sample stays on
disk (and in tests/fixtures), so nothing is lost; re-recording the source
later brings it back.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.ingest import observations, watergauge
from engine.ingest.observations import FeedStatus
from engine.ingest.sources import fetch
from engine.ingest.sources.catalog import CATALOG, by_key

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "fixtures"
AS_OF = datetime(2026, 9, 22, tzinfo=UTC)
HORMUZ = "Iran announces closure of Strait of Hormuz to commercial shipping"


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


class Network:
    """Every source answers as it would live, except those told to refuse."""

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.asked: list[str] = []

    def source(self, spec, as_of=None, window_days=None, timeout=None):
        self.asked.append(spec.key)
        if spec.key in self.refuse:
            return None, "unreachable (timed out)"
        if spec.key == "wikipedia_events":
            params, _ = spec.window.params_for(as_of, window_days)
            day = params["page"]
            text = ("'''International relations'''\n"
                    f"*Talks resume in Oman ({day}). ([https://news.test/{day[-2:]} Wire])\n")
            return {"parse": {"title": day, "wikitext": text}}, ""
        blob = json.loads((SAMPLES / spec.fixture).read_text(encoding="utf-8"))
        blob.pop("_fixture_note", None)          # a live answer carries no note
        return blob, ""

    def gauge(self, url, headers=None, timeout=None):
        self.asked.append("watergauge_kaub")
        if "watergauge_kaub" in self.refuse:
            return None, "unreachable (timed out)"
        start = datetime(2026, 9, 23, 8, tzinfo=UTC) - timedelta(days=30)
        cest = timezone(timedelta(hours=2))
        return [{"timestamp": (start + timedelta(hours=h)).astimezone(cest).isoformat(),
                 "value": 180.0} for h in range(30 * 24)], ""


@pytest.fixture
def world(recorder, reasoner, tmp_path, monkeypatch):
    """A fixture folder holding the samples, as a fresh clone's does."""
    folder = tmp_path / "fixtures"
    shutil.copytree(SAMPLES, folder)
    monkeypatch.setattr(recorder, "FIXTURES", folder)
    monkeypatch.setattr(recorder, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(reasoner, "FIXTURES", folder)
    monkeypatch.setattr(observations, "FIXTURE_DIR", folder)
    monkeypatch.setattr(recorder.time, "sleep", lambda _s: None)
    return folder


def run(recorder, monkeypatch, network, *args):
    monkeypatch.setattr(recorder, "_get", network.source)
    monkeypatch.setattr(fetch, "get_json", network.gauge)
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    monkeypatch.setattr(sys, "argv", ["record_fixture.py", *args])
    status = recorder.main()
    monkeypatch.delenv("RADAR_ALLOW_NETWORK")
    return status


def manifest(folder) -> dict:
    return json.loads((folder / observations.RECORDING).read_text(encoding="utf-8"))


EVERYTHING = ("--all", "--as-of", "2026-09-22", "--days", "2")


# =====================================================================
# Writing it
# =====================================================================
def test_a_whole_recording_says_what_it_holds(recorder, world, monkeypatch, capsys):
    run(recorder, monkeypatch, Network(refuse={"gdelt_doc"}), *EVERYTHING)
    sources = manifest(world)["sources"]

    assert sources["gdelt_doc"]["recorded"] is False
    assert "unreachable" in sources["gdelt_doc"]["error"]
    runnable = [s.key for s in CATALOG if s.runnable and s.key != "gdelt_doc"]
    assert all(sources[key]["recorded"] for key in [*runnable, "watergauge_kaub"])

    out = capsys.readouterr().out
    assert "Not recorded : gdelt_doc" in out
    assert "the board leaves out gdelt_doc" in out


def test_nothing_reached_writes_nothing_and_says_do_not_commit(
    recorder, world, monkeypatch, capsys,
):
    everything = {s.key for s in CATALOG} | {"watergauge_kaub"}
    before = {p.name: p.read_bytes() for p in world.glob("*.json")}
    run(recorder, monkeypatch, Network(refuse=everything), *EVERYTHING)

    assert not (world / observations.RECORDING).exists()
    assert {p.name: p.read_bytes() for p in world.glob("*.json")} == before
    assert "Do not commit" in capsys.readouterr().out


def test_a_skipped_source_is_not_asked_and_is_left_out(recorder, world, monkeypatch):
    network = Network()
    run(recorder, monkeypatch, network, *EVERYTHING, "--skip", "gdelt_doc")
    assert "gdelt_doc" not in network.asked
    assert manifest(world)["sources"]["gdelt_doc"] == {
        "recorded": False, "error": "skipped with --skip",
    }


# =====================================================================
# The board reading it
# =====================================================================
def test_the_scripted_sample_never_stands_in_for_a_source_that_was_not_recorded(
    recorder, world, monkeypatch,
):
    gdelt = by_key("gdelt_doc")
    items, report = fetch.collect(gdelt, AS_OF)
    assert any(i["headline"] == HORMUZ for i in items), "before: the sample is served"

    run(recorder, monkeypatch, Network(refuse={"gdelt_doc"}), *EVERYTHING)
    items, report = fetch.collect(gdelt, AS_OF)
    assert items == []
    assert report.status is FeedStatus.ABSENT
    assert "left out of this recording" in report.detail
    assert "unreachable" in report.detail


def test_nor_does_the_generated_river(recorder, world, monkeypatch):
    run(recorder, monkeypatch, Network(refuse={"watergauge_kaub"}), *EVERYTHING)
    series, report = watergauge.fetch_kaub(load_config(), Clock.at(AS_OF))
    assert series == []
    assert report.status is FeedStatus.ABSENT
    assert "left out of this recording" in report.detail


def test_the_sources_that_were_recorded_are_served(recorder, world, monkeypatch):
    run(recorder, monkeypatch, Network(refuse={"gdelt_doc"}), *EVERYTHING)
    items, report = fetch.collect(by_key("wikipedia_events"), AS_OF)
    assert report.status is FeedStatus.FIXTURE
    assert [i["headline"] for i in items][:1] == [
        "Talks resume in Oman (Portal:Current events/2026 September 21)"
    ]
    series, report = watergauge.fetch_kaub(load_config(), Clock.at(AS_OF))
    assert series and "RECORDED FROM PEGELONLINE" in report.detail


def test_a_recording_is_labelled_as_one_with_its_date(recorder, world, monkeypatch):
    """It used to say "from a recorded sample" whatever it was reading."""
    run(recorder, monkeypatch, Network(), *EVERYTHING)
    _, report = fetch.collect(by_key("wikipedia_events"), AS_OF)
    assert "from the recording of 20" in report.detail
    assert "sample" not in report.detail


def test_without_a_recording_nothing_is_left_out(world):
    """A fresh clone has samples and no manifest: the scripted scenario."""
    assert observations.left_out("gdelt_doc") is None
    items, _ = fetch.collect(by_key("gdelt_doc"), AS_OF)
    assert any(i["headline"] == HORMUZ for i in items)


def test_a_damaged_manifest_leaves_nothing_out(world):
    (world / observations.RECORDING).write_text("{ half", encoding="utf-8")
    assert observations.left_out("gdelt_doc") is None


# =====================================================================
# Recording one source later
# =====================================================================
def test_recording_it_later_brings_it_back(recorder, world, monkeypatch):
    run(recorder, monkeypatch, Network(refuse={"gdelt_doc"}), *EVERYTHING)
    run(recorder, monkeypatch, Network(), "gdelt_doc", "--as-of", "2026-09-22", "--days", "2")

    assert manifest(world)["sources"]["gdelt_doc"] == {"recorded": True}
    assert manifest(world)["sources"]["wikipedia_events"] == {"recorded": True}
    _, report = fetch.collect(by_key("gdelt_doc"), AS_OF)
    assert report.status is FeedStatus.FIXTURE


def test_one_real_source_added_to_the_scenario_writes_no_manifest(
    recorder, world, monkeypatch,
):
    """Recording just the gauge into the samples is deliberate: a real Rhine
    under the scripted scenario. Nothing else is left out."""
    run(recorder, monkeypatch, Network(), "watergauge_kaub")
    assert not (world / observations.RECORDING).exists()
    items, _ = fetch.collect(by_key("gdelt_doc"), AS_OF)
    assert any(i["headline"] == HORMUZ for i in items)


# =====================================================================
# The reasoning recorder agrees
# =====================================================================
def test_the_reasoning_recorder_reports_a_left_out_source_as_left_out(
    recorder, reasoner, world, monkeypatch,
):
    run(recorder, monkeypatch, Network(refuse={"gdelt_doc", "watergauge_kaub"}), *EVERYTHING)
    what, not_sample = reasoner.fixture_status(by_key("gdelt_doc"))
    assert what.startswith("left out of this recording")
    assert not_sample, "nothing scripted will be reasoned over, so no sample warning"
    what, real = reasoner.fixture_status(by_key("wikipedia_events"))
    assert real and what.startswith("recorded")
    assert reasoner.gauge_status()[0].startswith("left out of this recording")
