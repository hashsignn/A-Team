"""The tests' own copy of the samples stays complete, and stays the samples.

conftest.py points every test at tests/fixtures, so that committing a real
recording to data/fixtures — the point of recording — cannot fail a test
written about the Hormuz headlines or a falling Rhine. That only holds while
the copy is whole, and while nobody has copied a recording into it.
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.ingest import observations, watergauge
from engine.ingest.sources.catalog import CATALOG
from engine.reason import cache

SAMPLES = Path(__file__).resolve().parent / "fixtures"
DEMO = Path(__file__).resolve().parent.parent / "data" / "fixtures"
RECORDED = "RECORDED FROM THE LIVE SOURCE"


def _names() -> list[str]:
    names = [s.fixture for s in CATALOG if s.runnable and s.fixture]
    return [*names, watergauge.FIXTURE_NAME]


def test_the_tests_read_the_samples_and_no_recorded_answers():
    assert observations.FIXTURE_DIR == SAMPLES
    assert cache.STORE != cache.ROOT / "data" / "reasoning"


def test_every_source_the_demo_replays_has_a_sample():
    for name in _names():
        assert (SAMPLES / name).exists(), f"tests/fixtures has no {name}"


def test_no_sample_is_a_recording():
    for name in _names():
        blob = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
        note = str(blob.get("_fixture_note", "")) if isinstance(blob, dict) else ""
        assert not note.startswith(RECORDED), f"tests/fixtures/{name} is a recording"
        assert "_recorded_at" not in blob, f"tests/fixtures/{name} is a recording"
    gauge = json.loads((SAMPLES / watergauge.FIXTURE_NAME).read_text(encoding="utf-8"))
    assert gauge["is_real_data"] is False


def test_putting_the_scenario_back_is_a_copy():
    """Same filenames, so copying tests/fixtures over data/fixtures restores
    the scripted scenario — which docs/MODELS.md tells people they can do."""
    assert {p.name for p in SAMPLES.glob("*.json")} == {p.name for p in DEMO.glob("*.json")}
