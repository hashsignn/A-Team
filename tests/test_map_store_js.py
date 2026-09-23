"""The fleet map's front-end state contract, run under Node.

The store and the agent hooks are the part of the map an AI agent will call,
so they are tested as code rather than eyeballed as pixels: selecting a
disrupted asset also fetches its routes and partners, closing the card drops
everything it drew, a late answer to a superseded question is ignored, every
action is journalled under the actor that took it.

Node is not part of the base install (requirements.txt is Python only), so
this SKIPS rather than fails where it is missing. scripts/dev_check_map.py
drives the same functions in a real browser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = sorted((ROOT / "tests" / "js").glob("*.test.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_map_store_and_agent_hooks_hold_their_contract():
    assert TESTS, "no JavaScript tests found"
    result = subprocess.run(  # noqa: S603
        ["node", "--test", *map(str, TESTS)],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
