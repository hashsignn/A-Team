#!/usr/bin/env python3
"""Is the thing you are running the thing in the repository?

    .venv/bin/python scripts/verify_install.py

Written because "I cannot see the changes" came up three times and every
answer was the same three guesses: stale clone, stale browser, wrong page.
Guessing is not diagnosis. This prints which commit is checked out, which
features are present in the files on disk, and — if a server is running —
which are present in the bytes it is actually serving.

The three can disagree, and WHICH pair disagrees tells you what to fix:

    disk missing a feature      -> your clone is behind.  git pull
    disk has it, server does not -> restart the server
    both have it, browser does not -> hard refresh (Ctrl+Shift+R)
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

# A marker per user-visible feature: the string that must be in the shipped
# file for that feature to exist at all.
MARKERS = {
    "Globe stops when you touch it":  ("api/static/app.js", "pointerdown"),
    "Click a port to open its lane":  ("api/static/app.js", "busiestRouteThrough"),
    "Large event modal":              ("api/static/app.js", "openEventModal"),
    "Matrix cells shaded by CHF":     ("api/static/app.js", "cellTint"),
    "Modal styling":                  ("api/static/styles.css", "evm-scrim"),
    "Who-is-reporting on the app":    ("api/static/driver.js", "renderRoles"),
    "Free source catalogue":          ("engine/ingest/sources/catalog.py", "gdelt_doc"),
    "Two-model funnel":               ("engine/reason/funnel.py", "TRIAGE_SYSTEM"),
    "Hormuz chokepoint":              ("config.example/network.yaml", "CHOKE_HORMUZ"),
}


def _git(*args: str) -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        return ""


def _served(path: str) -> str | None:
    try:
        with urllib.request.urlopen(f"http://localhost:{PORT}{path}", timeout=5) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — not running is an answer, not a crash
        return None


def _codespace() -> str | None:
    """The Codespace name, if we are in one."""
    import os

    if os.environ.get("CODESPACES", "").lower() == "true":
        return os.environ.get("CODESPACE_NAME") or "unnamed"
    return None


def main() -> int:
    space = _codespace()
    if space:
        print(f"environment    : GitHub Codespace ({space})")
    print(f"commit on disk : {_git('rev-parse', '--short', 'HEAD') or '?'}")
    print(f"branch         : {_git('rev-parse', '--abbrev-ref', 'HEAD') or '?'}")
    dirty = _git("status", "--porcelain")
    print(f"working tree   : {'UNCOMMITTED CHANGES' if dirty else 'clean'}")

    index = _served("/")
    if index is None:
        print(f"server         : not running on port {PORT}")
        served_js = served_css = None
    else:
        print(f"server         : answering on port {PORT}")
        served_js = _served("/app.js")
        served_css = _served("/styles.css")

    print("\n  FEATURE                              ON DISK   SERVED")
    missing_disk = missing_served = 0
    for name, (rel, marker) in MARKERS.items():
        path = ROOT / rel
        on_disk = path.exists() and marker in path.read_text(errors="replace")
        missing_disk += not on_disk

        if served_js is None:
            served = "   -   "
        elif rel.endswith("app.js"):
            served = " yes  " if marker in (served_js or "") else " NO   "
        elif rel.endswith("styles.css"):
            served = " yes  " if marker in (served_css or "") else " NO   "
        else:
            served = "  n/a "        # engine/config files are not served as assets
        if served.strip() == "NO":
            missing_served += 1
        print(f"  {name:<36} {'yes' if on_disk else 'NO ':<9} {served}")

    print()
    if missing_disk:
        print("YOUR CLONE IS BEHIND. Run:  git checkout main && git pull origin main")
        if space:
            print("(A Codespace is a clone. It does not update itself when main moves.)")
        return 1
    if missing_served:
        print("The files are correct but the server is serving something older.")
        print("Restart it:  .venv/bin/python run.py serve")
        return 1
    if index is None:
        print("Everything is present on disk. Start the server to check what it serves:")
        print("  .venv/bin/python run.py serve")
        return 0

    print("Everything is present, on disk and in what the server sends.")
    if space:
        # In a Codespace the likeliest remaining cause is not the browser
        # cache at all — it is that you are not looking at a browser.
        print()
        print("You are in a Codespace, so check this FIRST:")
        print("  Are you looking at VS Code's preview pane rather than a browser?")
        print("  That pane caches hard and cannot be force-reloaded — it shows")
        print("  whatever the app looked like when it first opened, forever.")
        print()
        print("  Open the PORTS tab at the bottom of VS Code, find port 8000,")
        print("  and click the globe icon to open it in a real browser tab.")
        print()
        print("  Then Ctrl+Shift+R once, with that tab focused.")
        print()
        print("  See .devcontainer/README.md for the rest of the checklist.")
    else:
        print("If the browser still looks old, it is caching: press Ctrl+Shift+R")
        print("(Cmd+Shift+R on a Mac) once with the page open.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
