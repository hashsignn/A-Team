"""scripts/check_models.py: says exactly what to type, and never "Ready" wrongly.

Ollama calls a model by its full tag, and `qwen2.5:7b` is not
`qwen2.5:7b-instruct` to it. The checker used to report "have" on a matching
family and finish with "Ready" — and the radar then asked Ollama for a tag it
did not have and got nothing back, on the machine where somebody had just
been told everything was fine.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def checker(monkeypatch):
    spec = importlib.util.spec_from_file_location("check_models", ROOT / "scripts" / "check_models.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_models"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/ollama")
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": "ollama version 0.9"})())
    return module


def _pulled(checker, monkeypatch, models):
    monkeypatch.setattr(checker, "_installed_models", lambda: [
        {"name": name, "size": int(gb * 1e9)} for name, gb in models])


def _wanted(checker, monkeypatch, triage, extract):
    monkeypatch.setattr(checker, "WANTED", (
        {**checker.WANTED[0], "model": triage}, {**checker.WANTED[1], "model": extract}))


def test_the_smallest_capable_model_triages_and_the_largest_reads(checker):
    choice = checker.pick([
        {"name": "qwen2.5:14b", "size": 9_000_000_000},
        {"name": "nomic-embed-text:latest", "size": 300_000_000},
        {"name": "qwen2.5:1.5b-instruct", "size": 986_000_000},
        {"name": "qwen2.5:7b", "size": 4_700_000_000},
    ])
    # Not the 1.5B: it kept a celebrity trial as freight news.
    assert choice == {"RADAR_TRIAGE_MODEL": "qwen2.5:7b",
                      "RADAR_EXTRACT_MODEL": "qwen2.5:14b",
                      "RADAR_LOCAL_MODEL": "qwen2.5:14b"}
    # With nothing bigger, the small one is still better than none.
    only_small = checker.pick([{"name": "qwen2.5:1.5b-instruct", "size": 986_000_000}])
    assert only_small["RADAR_TRIAGE_MODEL"] == "qwen2.5:1.5b-instruct"
    assert checker.pick([]) is None
    assert checker.pick([{"name": "nomic-embed-text", "size": 1}]) is None


def test_a_matching_family_is_not_ready(checker, monkeypatch, capsys):
    """Qwen 7B and 14B pulled under their plain tags, the radar configured
    for the -instruct tags: not ready, and the lines to paste are printed."""
    _pulled(checker, monkeypatch, [("qwen2.5:7b", 4.7), ("qwen2.5:14b", 9.0)])
    _wanted(checker, monkeypatch, "qwen2.5:1.5b-instruct", "qwen2.5:7b-instruct")
    assert checker.main() == 1
    out = capsys.readouterr().out
    assert "Ready." not in out
    assert checker.setter("RADAR_TRIAGE_MODEL", "qwen2.5:7b") in out
    assert checker.setter("RADAR_EXTRACT_MODEL", "qwen2.5:14b") in out
    assert checker.setter("RADAR_LOCAL_MODEL", "qwen2.5:14b") in out
    assert "not the same tag to Ollama" in out


def test_the_exact_tags_are_ready(checker, monkeypatch, capsys):
    _pulled(checker, monkeypatch, [("qwen2.5:7b", 4.7), ("qwen2.5:14b", 9.0)])
    _wanted(checker, monkeypatch, "qwen2.5:7b", "qwen2.5:14b")
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert "Ready." in out
    assert "qwen2.5:14b" in out and "9.0 GB" in out


def test_it_says_where_ollama_keeps_the_models(checker, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", "D:/models")
    assert checker.models_folder() == "D:/models"
    monkeypatch.delenv("OLLAMA_MODELS")
    assert checker.models_folder().endswith("models")


def test_model_files_from_other_tools_are_found(checker, tmp_path):
    lm = tmp_path / ".lmstudio" / "models" / "Qwen" / "Qwen2.5-14B-Instruct-GGUF"
    lm.mkdir(parents=True)
    (lm / "qwen2.5-14b-instruct-q4_k_m.gguf").write_bytes(b"\0" * 1024)
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "notes.txt").write_text("x", encoding="utf-8")
    found = checker.find_gguf(home=tmp_path)
    assert [p.name for p, _gb in found] == ["qwen2.5-14b-instruct-q4_k_m.gguf"]


def _unset_models(monkeypatch):
    for name in ("RADAR_TRIAGE_MODEL", "RADAR_EXTRACT_MODEL", "RADAR_LOCAL_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_it_says_which_variable_chose_each_model(checker, monkeypatch, capsys):
    """Pasted into PowerShell, `set RADAR_EXTRACT_MODEL=...` set nothing, and
    the check said Ready — for the defaults, with no sign that they were.
    Now every model says what chose it, and a default says so."""
    _pulled(checker, monkeypatch, [("qwen2.5:7b-instruct", 4.7), ("qwen2.5:14b-instruct", 9.0)])
    _wanted(checker, monkeypatch, "qwen2.5:7b-instruct", "qwen2.5:14b-instruct")
    _unset_models(monkeypatch)
    monkeypatch.setenv("RADAR_EXTRACT_MODEL", "qwen2.5:14b-instruct")
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert "chosen by RADAR_EXTRACT_MODEL" in out
    assert "the built-in default" in out
    assert "Note: RADAR_TRIAGE_MODEL is not set in this\n" in out
    assert "Ready." in out


def test_with_both_chosen_there_is_no_note(checker, monkeypatch, capsys):
    _pulled(checker, monkeypatch, [("qwen2.5:7b-instruct", 4.7), ("qwen2.5:14b-instruct", 9.0)])
    _wanted(checker, monkeypatch, "qwen2.5:7b-instruct", "qwen2.5:14b-instruct")
    _unset_models(monkeypatch)
    monkeypatch.setenv("RADAR_TRIAGE_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setenv("RADAR_EXTRACT_MODEL", "qwen2.5:14b-instruct")
    assert checker.main() == 0
    out = capsys.readouterr().out
    assert "Note:" not in out
    assert "chosen by RADAR_TRIAGE_MODEL" in out


def test_in_powershell_the_lines_to_paste_are_powershell_lines(checker, monkeypatch, capsys):
    monkeypatch.setattr(checker.shell, "kind", lambda *a, **k: checker.shell.POWERSHELL)
    _pulled(checker, monkeypatch, [("qwen2.5:7b", 4.7), ("qwen2.5:14b", 9.0)])
    _wanted(checker, monkeypatch, "qwen2.5:1.5b-instruct", "qwen2.5:7b-instruct")
    assert checker.main() == 1
    out = capsys.readouterr().out
    assert '$env:RADAR_TRIAGE_MODEL = "qwen2.5:7b"' in out
    assert "set RADAR_TRIAGE_MODEL" not in out
    assert "In cmd.exe the form is" in out


def test_a_trailing_space_is_not_part_of_the_model_name(monkeypatch):
    """cmd.exe keeps the space at the end of `set NAME=value ` in the value,
    and "qwen2.5:7b-instruct " is not a tag Ollama has."""
    from engine.reason import llm

    monkeypatch.setenv("RADAR_TRIAGE_MODEL", "qwen2.5:7b-instruct ")
    assert llm._named("RADAR_TRIAGE_MODEL") == "qwen2.5:7b-instruct"
    monkeypatch.setenv("RADAR_TRIAGE_MODEL", "   ")
    monkeypatch.delenv("RADAR_LOCAL_MODEL", raising=False)
    assert llm.configured_by("RADAR_TRIAGE_MODEL") is None
