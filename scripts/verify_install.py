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
    # Solution A — the delivery-first surface. First in the table because it
    # is the thing somebody reporting "still the old front end" is asking
    # about.
    #
    # This marker named the wrong page for a while. It checked that "/" served
    # fast.html, which was true for exactly as long as it took to hear that
    # the globe and the affected routes belong on one screen. "/" went back to
    # serving the globe board with the routes panel beside it, /fast stayed as
    # its own surface, and the marker was left pointing at the arrangement
    # that had been deliberately replaced — so it reported "YOUR CLONE IS
    # BEHIND" at anybody whose clone was perfectly current. A check that
    # cries wolf is worse than no check: it sends people to chase a git pull
    # that cannot help them. It now names what is actually on each page.
    "Globe and affected routes on one page": ("api/static/index.html", 'id="panel-list"'),
    "Front page links to the fast board": ("api/static/index.html", 'href="/fast"'),
    "Fast dashboard page":            ("api/static/fast.html", "What needs a decision"),
    "One-click execute":              ("api/static/fast.js", "/api/v2/act"),
    "Undo window with a countdown":   ("api/static/fast.js", "to change your mind"),
    "Delivery-first ranking":         ("engine/fast/options.py", "restores_delivery"),
    "Profitability veto":             ("engine/fast/margin.py", "margin_floor_chf"),
    "Generated reroutes":             ("engine/fast/contingency.py", "fastest_path"),
    "Event bus":                      ("engine/fast/bus.py", "TOPIC_DISRUPTION"),
    "Mixed-capacity plans":           ("engine/fast/capacity.py", "units_needed_by"),
    "Solution board tabs":            ("api/static/board.js", "ptab--on"),
    "A mode can be half shut":        ("engine/fast/view.py", "_mode_pressure"),
    "Options say if the trucks exist": ("engine/fast/view.py", "capacity_note"),
    "Declared corridor ceilings":     ("config.example/fast.yaml", "tonnes_per_unit"),

    "Globe stops when you touch it":  ("api/static/app.js", "pointerdown"),
    "Click a port to open its lane":  ("api/static/app.js", "busiestRouteThrough"),
    # NOT a modal. The pop-up was replaced by a real page with a real URL,
    # because a planner looking at one lane wants to send somebody the lane
    # and a dialog cannot be sent. The old marker for the modal was left
    # behind and reported "your clone is behind" for a feature that had been
    # deliberately removed — a false alarm that sends people to chase a git
    # pull which cannot help them.
    "Lane opens as its own page":     ("api/static/route.js", "selectVehicle"),
    "Matrix cells shaded by CHF":     ("api/static/charts.js", "cellTint"),
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
        served: dict[str, str] = {}
    else:
        print(f"server         : answering on port {PORT}")
        # Whatever a marker names, fetch that asset once. Hardcoding two file
        # names meant a new page could be entirely missing and the table would
        # still read "n/a" next to every row of it.
        served = {}
        for rel, _ in MARKERS.values():
            if not rel.startswith("api/static/"):
                continue
            name = rel.removeprefix("api/static/")
            if name not in served:
                served[name] = _served(f"/{name}") or ""
        # A page is served at its ROUTE, and the route is not always the
        # filename. "/" is the globe board; the fast board has its own path,
        # which the loop above already fetched. This line used to assign the
        # front page's HTML to fast.html — true while "/" served fast.html,
        # false the moment the globe went back to the front, and from then on
        # it checked one page's marker against another page's markup and
        # reported a missing dashboard on a clone that had one.
        served["index.html"] = index

    print("\n  FEATURE                              ON DISK   SERVED")
    missing_disk = missing_served = 0
    for name, (rel, marker) in MARKERS.items():
        path = ROOT / rel
        on_disk = path.exists() and marker in path.read_text(errors="replace")
        missing_disk += not on_disk

        if index is None:
            cell = "   -   "
        elif rel.startswith("api/static/"):
            body = served.get(rel.removeprefix("api/static/"), "")
            cell = " yes  " if marker in body else " NO   "
        else:
            cell = "  n/a "       # engine/ and config/ are not served as assets
        if cell.strip() == "NO":
            missing_served += 1
        print(f"  {name:<36} {'yes' if on_disk else 'NO ':<9} {cell}")

    print()
    if missing_disk:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "main"
        print("YOUR CLONE IS BEHIND. Run:")
        print(f"  git fetch origin && git checkout {branch} "
              f"&& git pull origin {branch}")
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
    print()
    # This said the opposite for a while — that "/" was the decision surface
    # and the globe had moved aside. That was true briefly, and then the globe
    # went back to the front because the globe is what people mean when they
    # say "the radar". A message that misdescribes the pages sends somebody
    # looking for a screen that is already in front of them.
    print("Where each screen lives:")
    print("  /                the globe board, with every affected route")
    print("                   beside it (/board is an alias)")
    print("  /fast            what needs a decision, fastest first")
    print("  /fast/<LANE_ID>  one lane: the options, and below them the")
    print("                   capacity tabs — how the freight actually moves")
    print("  /ops?route=<ID>  the operations console for that lane")
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
