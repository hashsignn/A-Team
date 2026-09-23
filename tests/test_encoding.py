"""Text files are read and written as UTF-8, whatever the machine thinks.

Python 3.11 on Windows opens a text file in the ANSI code page — cp1252 on a
Western install — unless it is told otherwise. On one machine that is
invisible, because the machine agrees with itself. It breaks the moment a
file crosses machines, and this project's recording design is nothing but
files crossing machines: sources are recorded where the network is, reasoning
is recorded where the model is, and both are replayed in a Codespace.

The failure it causes is silent, in the worst possible place. A reasoning
cache written as cp1252 on the laptop is read as UTF-8 in the Codespace. The
decode error is a ValueError, the loader treats a ValueError as "no
recording", and every answer the model gave is quietly discarded. Nothing
crashes and nothing replays. Before that, headlines read as cp1252 on the
laptop come out garbled, so the prompts are built from different text than
the Codespace will build, the cache keys differ, and every lookup would have
missed anyway.

Two tests, because they catch different things. The AST walk proves every
call names its encoding, which is what makes the platform default
irrelevant. The subprocess proves the round trip actually survives a
machine whose default is not UTF-8.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _text_io_without_encoding(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            name = fn.attr
        elif isinstance(fn, ast.Name):
            name = fn.id
        else:
            continue
        if "encoding" in {k.arg for k in node.keywords}:
            continue
        if name in ("read_text", "write_text"):
            yield node.lineno, name
        elif name == "open":
            # Pillow's Image.open reads pixels, not text, and takes no
            # encoding — flagging it would make the rule impossible to obey.
            if isinstance(fn, ast.Attribute) and ast.unparse(fn.value) == "Image":
                continue
            index = 1 if isinstance(fn, ast.Name) else 0
            mode = ""
            if len(node.args) > index and isinstance(node.args[index], ast.Constant):
                mode = str(node.args[index].value)
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value)
            if "b" not in mode:
                yield node.lineno, f"open({mode or 'r'})"


def test_every_text_read_and_write_names_its_encoding():
    """Walk the AST rather than grepping, the same way the wall-clock rule
    does: prose about ``read_text`` in a docstring is not a call site, and a
    real call split across lines cannot slip past a regex."""
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, what in _text_io_without_encoding(tree):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno} {what}")

    assert not offenders, (
        "text I/O that takes the platform's default encoding — cp1252 on "
        "Windows — and so breaks the moment the file changes machines:\n  "
        + "\n  ".join(offenders)
    )


# Run from a FILE, not with -c. Under a legacy locale the command line cannot
# carry a "ü" at all — the interpreter refuses the -c string before it runs a
# line of it — whereas a source file is always decoded as UTF-8 (PEP 3120)
# whatever the locale says. So the characters below reach the child intact
# and the only thing the locale can affect is the code under test.
_CHILD = r'''
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.environ["RADAR_TEST_ROOT"])
os.environ["RADAR_RECORD_REASONING"] = "1"

from engine.config import load_config
from engine.ingest import observations
from engine.reason import cache

tmp = Path(tempfile.mkdtemp())
text = "Düdingen → Antwerp — dockers’ strike 罢工"

# 1. The reasoning cache: written, then read back by a store that has never
#    seen it. This is the laptop-to-Codespace hop.
cache.STORE = tmp / "reasoning"
cache.record("triage", "system", text, {"summary": text}, "test-model",
             "2026-09-22T00:00:00+00:00")
cache.flush()
cache._STORES.clear()
hit = cache.lookup("triage", "system", text)
assert hit is not None, "the recording was written and did not replay"
assert hit.payload["summary"] == text, "the recording replayed garbled"

# 2. A fixture recorded on another machine.
observations.FIXTURE_DIR = tmp
(tmp / "recorded.json").write_text(
    json.dumps({"title": text}, ensure_ascii=False), encoding="utf-8")
assert observations.load_fixture("recorded.json")["title"] == text

# 3. The configuration every prompt is built from.
names = [lane["name"] for lane in load_config().lanes]
assert any("Düdingen" in name for name in names), "lane names garbled"

print("round trip intact")
'''


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="forcing a legacy default is a POSIX locale switch; on Windows "
           "the default already is one, and the AST rule covers it",
)
def test_a_recording_survives_a_machine_whose_default_is_not_utf8(tmp_path):
    """Run the record-and-replay path under a plain ASCII default.

    ASCII is harsher than Windows' cp1252 — cp1252 can at least hold "ü" and
    "—" — so anything that passes here passes there. The three environment
    variables switch off the two mechanisms Python uses to rescue a C locale
    into UTF-8, which would otherwise make this test pass for the wrong
    reason.
    """
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "RADAR_TEST_ROOT": str(ROOT),
    }
    env.pop("RADAR_ALLOW_NETWORK", None)

    probe = subprocess.run(
        [sys.executable, "-c",
         "import locale; print(locale.getpreferredencoding(False))"],
        env=env, capture_output=True, text=True, check=True,
    )
    if probe.stdout.strip().lower().replace("-", "") in {"utf8", "cp65001"}:
        pytest.skip("this platform will not give up a UTF-8 default")

    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(child)],
        env=env, capture_output=True, text=True, cwd=ROOT,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, (
        f"under default encoding {probe.stdout.strip()!r}:\n{result.stderr}"
    )
    assert "round trip intact" in result.stdout
