"""The reasoning recorder refuses the mistakes that make a recording useless.

A recording is only worth making if it replays, and it only replays if the
answers were recorded over the same headlines the Codespace will read — the
committed fixtures. Two mistakes break that silently, and both are cheap to
make on a laptop at the end of a long day: leaving the network switched on,
so the sources are fetched live, and forgetting to pull the fixtures, so the
model reasons over the sample. The first is refused; the second is reported
before any model time is spent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.ingest.sources.catalog import CATALOG

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: @dataclass resolves annotations through
    # sys.modules, and a module loaded by path is not in it otherwise.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reasoner():
    return _load("record_reasoning")


@pytest.fixture(scope="module")
def recorder():
    return _load("record_fixture")


@pytest.fixture
def gdelt():
    return next(s for s in CATALOG if s.key == "gdelt_doc")


# =====================================================================
# The network
# =====================================================================
def test_it_refuses_to_record_with_the_network_on(reasoner, monkeypatch, capsys):
    """Refused before the model is even looked for: nothing it could do after
    that would produce answers that replay."""
    monkeypatch.setenv("RADAR_RECORD_REASONING", "1")
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    monkeypatch.setattr(sys, "argv", ["record_reasoning.py", "--as-of", "2026-09-22"])

    def not_reached(*_a, **_k):
        raise AssertionError("looked for a model after the network check failed")

    monkeypatch.setattr(reasoner.llm, "detect", not_reached)

    assert reasoner.main() == 2
    out = capsys.readouterr().out
    assert "RADAR_ALLOW_NETWORK is set" in out
    assert "miss every one of them" in out


def test_with_the_network_off_it_gets_as_far_as_the_model(reasoner, monkeypatch):
    monkeypatch.setenv("RADAR_RECORD_REASONING", "1")
    monkeypatch.delenv("RADAR_ALLOW_NETWORK", raising=False)
    monkeypatch.setattr(sys, "argv", ["record_reasoning.py", "--as-of", "2026-09-22"])
    seen = []

    def detect(*_a, **_k):
        seen.append(True)
        raise RuntimeError("stop here")

    monkeypatch.setattr(reasoner.llm, "detect", detect)
    with pytest.raises(RuntimeError, match="stop here"):
        reasoner.main()
    assert seen


@pytest.mark.parametrize("system, command", [
    ("Windows", "set RADAR_ALLOW_NETWORK="),
    ("Linux", "unset RADAR_ALLOW_NETWORK"),
])
def test_the_way_out_is_the_right_command_for_the_machine(
    reasoner, monkeypatch, system, command,
):
    """On Windows, `unset` is "not recognised" — the command printed has to
    be one the person reading it can actually type."""
    monkeypatch.setattr(reasoner.platform, "system", lambda: system)
    assert f"  {command}" in reasoner.network_refusal()


def test_the_window_flag_that_only_worked_live_is_gone(reasoner, monkeypatch):
    """--window-days asked the sources live for a past window. With the
    network refused, all it could do is nothing."""
    monkeypatch.setattr(sys, "argv", ["record_reasoning.py", "--window-days", "60"])
    with pytest.raises(SystemExit):
        reasoner.main()


# =====================================================================
# The fixtures it is about to reason over
# =====================================================================
def test_the_committed_sample_is_reported_as_the_sample(reasoner, gdelt):
    what, real = reasoner.fixture_status(gdelt)
    assert not real
    assert "SAMPLE" in what


def test_a_recorded_window_says_how_much_it_holds(
    reasoner, recorder, gdelt, tmp_path, monkeypatch,
):
    monkeypatch.setattr(recorder, "FIXTURES", tmp_path)
    monkeypatch.setattr(recorder, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(reasoner, "FIXTURES", tmp_path)

    def archive(spec, as_of=None, window_days=None):
        stamp = as_of.strftime("%Y%m%d")
        return {"articles": [{"url": f"https://x/{stamp}/{n}"} for n in range(2)]}, ""

    result = recorder.record_window(
        gdelt, datetime(2026, 9, 22, tzinfo=UTC), 3,
        fetch=archive, pause=lambda _s: None, say=lambda *_: None,
        now=datetime(2026, 9, 24, tzinfo=UTC),
    )
    recorder._write(gdelt, result.blob)

    what, real = reasoner.fixture_status(gdelt)
    assert real
    assert "3 days" in what
    assert "6 records" in what
    assert "missing" not in what


def test_a_recorded_window_with_gaps_says_so(
    reasoner, recorder, gdelt, tmp_path, monkeypatch,
):
    monkeypatch.setattr(recorder, "FIXTURES", tmp_path)
    monkeypatch.setattr(recorder, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(reasoner, "FIXTURES", tmp_path)

    def archive(spec, as_of=None, window_days=None):
        if as_of.day <= 21:
            return None, "HTTP 429"          # refused from the second day on
        return {"articles": [{"url": f"https://x/{as_of.day}"}]}, ""

    result = recorder.record_window(
        gdelt, datetime(2026, 9, 22, tzinfo=UTC), 3,
        fetch=archive, pause=lambda _s: None, say=lambda *_: None,
        now=datetime(2026, 9, 24, tzinfo=UTC),
    )
    assert result.stopped
    recorder._write(gdelt, result.blob)

    what, real = reasoner.fixture_status(gdelt)
    assert real
    assert "2 day(s) missing" in what


def test_a_recording_is_stamped_with_when_it_was_made(
    recorder, gdelt, tmp_path, monkeypatch,
):
    """The note has always promised "frozen at the time below"."""
    monkeypatch.setattr(recorder, "FIXTURES", tmp_path)
    recorder._write(gdelt, {"articles": []})
    blob = json.loads((tmp_path / gdelt.fixture).read_text(encoding="utf-8"))
    assert datetime.fromisoformat(blob["_recorded_at"]).tzinfo is not None


def test_an_unreadable_fixture_is_not_mistaken_for_a_recording(
    reasoner, gdelt, tmp_path, monkeypatch,
):
    monkeypatch.setattr(reasoner, "FIXTURES", tmp_path)
    (tmp_path / gdelt.fixture).write_text("{ half a file", encoding="utf-8")
    what, real = reasoner.fixture_status(gdelt)
    assert not real
    assert what.startswith("unreadable")
