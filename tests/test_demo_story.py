"""The demo's storyboard (scripts/demo/): two and a half minutes without its
pauses, every camera move on a word that is actually said, and the written
camera plan in step with the storyboard it was generated from."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "demo"
_spec = importlib.util.spec_from_file_location("demo_record", DEMO / "record.py")
record = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record)
BOARD = json.loads((DEMO / "storyboard.json").read_text(encoding="utf-8"))


def test_it_runs_two_and_a_half_minutes_without_its_pauses():
    per = {}
    for s in BOARD["segments"]:
        per[s["speaker"]] = per.get(s["speaker"], 0) + s["dur"]
    assert sum(per.values()) == pytest.approx(150)
    assert per == {"Shanshan": pytest.approx(60), "Harjot": pytest.approx(90)}


def test_every_cue_lands_on_a_word_that_is_said_inside_its_segment():
    for s in record.timings(BOARD):
        for c in s["cues"]:
            assert c["found"], f"{s['id']}: {c.get('word')!r} is not in {s['text']!r}"
            assert s["start"] <= c["time"] <= s["start"] + s["dur"]


def test_every_breakpoint_says_what_to_click():
    pauses = [s["pause"] for s in BOARD["segments"] if s.get("pause")]
    assert len(pauses) == 10
    assert all(p["t"] and p["label"] for p in pauses)


def test_the_written_camera_plan_is_the_storyboard():
    assert (DEMO / "SCRIPT.md").read_text(encoding="utf-8") == record.script_md(BOARD), \
        "regenerate it: python scripts/demo/record.py --script scripts/demo/SCRIPT.md"
