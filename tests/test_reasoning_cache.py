"""Record once with a model; replay everywhere without one.

The property under test is the one the whole idea rests on: a model's answer
to a fixed prompt is a fact about that pair, not about the machine that asked.
If that holds, a laptop with a model can carry a rehearsed run to a Codespace
that has none.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from engine.reason import cache, llm


class Answer(BaseModel):
    verdict: str
    confidence: float


SYSTEM = "You judge things."
PROMPT = "Is the Rhine low?"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Each test gets its own store. The repo's recordings are not touched."""
    monkeypatch.setattr(cache, "STORE", tmp_path / "reasoning")
    cache._STORES.clear()
    yield
    cache._STORES.clear()


@pytest.fixture
def live(monkeypatch):
    """A model that answers, and counts how often it was asked."""
    calls = {"n": 0}

    def fake_parse(model, system, prompt, status=None, model_name=""):
        calls["n"] += 1
        return model(verdict="yes", confidence=0.9)

    monkeypatch.setattr(llm, "parse", fake_parse)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.LOCAL, model="qwen2.5:7b-instruct",
        detail="stub", unlocks_if_connected=""))
    return calls


@pytest.fixture
def no_model(monkeypatch):
    """No backend at all — the Codespace case."""
    def refuse(*a, **k):
        raise AssertionError("a live call was made with no model available")

    monkeypatch.setattr(llm, "parse", refuse)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.NONE, model=None,
        detail="nothing reachable", unlocks_if_connected=""))


# ------------------------------------------------------------- the round trip
def test_an_answer_recorded_on_one_machine_replays_on_another(
    live, no_model, monkeypatch
):
    """The whole idea, in one test."""
    # 1. a machine WITH a model, recording
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.LOCAL, model="qwen2.5:7b-instruct",
        detail="stub", unlocks_if_connected=""))
    monkeypatch.setattr(llm, "parse", lambda m, s, p, status=None, model_name="":
                        m(verdict="yes", confidence=0.9))

    answer, origin = llm.parse_with_provenance(
        Answer, SYSTEM, PROMPT, stage="triage", model_name="qwen2.5:7b-instruct")
    assert origin == "live"
    assert cache.flush(), "nothing was written"

    # 2. a machine with NO model, reading what was written
    cache._STORES.clear()
    monkeypatch.delenv(cache.RECORD_ENV, raising=False)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.NONE, model=None, detail="none",
        unlocks_if_connected=""))
    monkeypatch.setattr(llm, "parse", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a live call was made with no model")))

    replayed, origin = llm.parse_with_provenance(
        Answer, SYSTEM, PROMPT, stage="triage")
    assert replayed == answer
    assert origin.startswith("replayed")
    assert "qwen2.5:7b-instruct" in origin


def test_a_replay_is_labelled_as_one(live, monkeypatch):
    """An answer whose age is invisible is an answer nobody can check."""
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    cache.flush()
    cache._STORES.clear()

    _, origin = llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    assert origin.startswith("replayed:recorded ")
    assert "by qwen2.5:7b-instruct" in origin


def test_a_replay_costs_no_call(live, monkeypatch):
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    cache.flush()
    cache._STORES.clear()
    before = live["n"]

    llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    assert live["n"] == before, "a recorded answer still went to the model"


# ------------------------------------------------------------------ the key
def test_an_edited_prompt_misses_rather_than_replaying_the_old_answer(
    live, monkeypatch
):
    """The worst failure available here: an edited prompt silently replaying
    answers to the question it used to ask, which looks exactly like the new
    prompt working."""
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    cache.flush()
    cache._STORES.clear()

    assert cache.lookup("triage", SYSTEM, PROMPT) is not None
    assert cache.lookup("triage", SYSTEM + " Be terse.", PROMPT) is None
    assert cache.lookup("triage", SYSTEM, "a different headline") is None


def test_the_key_does_not_name_the_model(live, monkeypatch):
    """It must not: the machine replaying has no model, so a key naming one
    could never hit."""
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(
        Answer, SYSTEM, PROMPT, stage="triage", model_name="qwen2.5:7b-instruct")
    cache.flush()
    cache._STORES.clear()

    # A different model asking the same question finds the same answer.
    hit = cache.lookup("triage", SYSTEM, PROMPT)
    assert hit is not None
    assert hit.model == "qwen2.5:7b-instruct"   # recorded in the VALUE


def test_stages_do_not_collide(live, monkeypatch):
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    cache.flush()
    cache._STORES.clear()
    assert cache.lookup("extract", SYSTEM, PROMPT) is None


# --------------------------------------------------------------- the guards
def test_nothing_is_written_unless_recording_is_switched_on(live, monkeypatch):
    """A demo machine must never quietly write answers into the repository."""
    monkeypatch.delenv(cache.RECORD_ENV, raising=False)
    llm.parse_with_provenance(Answer, SYSTEM, PROMPT, stage="triage")
    assert cache.flush() == []
    assert cache.lookup("triage", SYSTEM, PROMPT) is None


def test_a_call_with_no_stage_is_never_cached(live, monkeypatch):
    """The assistant's free-text answers are conversation, not findings."""
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(Answer, SYSTEM, PROMPT)
    assert cache.flush() == []


def test_a_corrupt_recording_is_a_missing_one_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "STORE", tmp_path / "reasoning")
    (tmp_path / "reasoning").mkdir()
    (tmp_path / "reasoning" / "triage.json").write_text("{not json")
    cache._STORES.clear()
    assert cache.lookup("triage", SYSTEM, PROMPT) is None


def test_a_recording_from_an_older_schema_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "STORE", tmp_path / "reasoning")
    (tmp_path / "reasoning").mkdir()
    (tmp_path / "reasoning" / "triage.json").write_text(json.dumps({
        "schema": cache.SCHEMA - 1, "stage": "triage",
        "entries": {"abc": {"payload": {"verdict": "yes", "confidence": 1.0}}},
    }))
    cache._STORES.clear()
    assert cache.lookup("triage", SYSTEM, PROMPT) is None


def test_a_stale_recording_falls_through_to_a_live_call(live, monkeypatch):
    """A recording that no longer fits the schema is stale, not authoritative."""
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    store = cache.store("triage")
    store.put(cache._key("triage", SYSTEM, PROMPT),
              {"nonsense": True}, "old-model", "2026-01-01T00:00:00+00:00")

    answer, origin = llm.parse_with_provenance(
        Answer, SYSTEM, PROMPT, stage="triage")
    assert origin == "live"
    assert answer.verdict == "yes"


# ------------------------------------------------------------------ report
def test_the_report_says_plainly_when_nothing_is_recorded():
    report = cache.report()
    assert report["available"] is False
    assert "supported state" in report["note"]


def test_the_report_names_the_model_and_the_date(live, monkeypatch):
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    llm.parse_with_provenance(
        Answer, SYSTEM, PROMPT, stage="triage", model_name="qwen2.5:7b-instruct")
    cache.flush()

    report = cache.report()
    assert report["available"] is True
    assert report["entries"] == 1
    assert "qwen2.5:7b-instruct" in report["models"]
    assert "recording, not as a live read" in report["note"]


def test_the_file_is_written_sorted_so_a_diff_is_reviewable(live, monkeypatch):
    """This file is committed. A diff that reorders itself on every write is
    a diff nobody reviews."""
    monkeypatch.setenv(cache.RECORD_ENV, "1")
    for i in range(6):
        llm.parse_with_provenance(Answer, SYSTEM, f"{PROMPT} {i}", stage="triage")
    written = cache.flush()

    data = json.loads(written[0].read_text())
    keys = list(data["entries"])
    assert keys == sorted(keys)
