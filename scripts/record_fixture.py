#!/usr/bin/env python3
"""Record a live source response as the fixture the demo falls back to.

    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py gdelt_doc
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py --all

WHY THIS EXISTS
===============
Every fixture in data/fixtures is a SHAPE, written by the build team, because
the environment this was built in blocks every data host at the egress proxy.
The field names, nesting and date formats are the real ones; the content is
not, and the /inputs panel says so.

On a machine with network access, this replaces a shape with a real recording
in one command. That matters for a demo: a recorded Tuesday replays
identically forever, which a live call does not, and a recording is the only
way to show a past event to an audience on a stage with bad wifi.

It writes the RAW response, unparsed. Storing our own parse would freeze
today's mapping into the fixture, and the next spec change would then be
tested against a file that already agrees with it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.ingest.sources import CATALOG, network_allowed  # noqa: E402
from engine.ingest.sources.fetch import _get  # noqa: E402

FIXTURES = ROOT / "data" / "fixtures"

NOTE = (
    "RECORDED FROM THE LIVE SOURCE by scripts/record_fixture.py. Real response, "
    "real content, frozen at the time below so the demo replays identically."
)


def record(key: str) -> int:
    spec = next((s for s in CATALOG if s.key == key), None)
    if spec is None:
        print(f"  {key}: not in the catalogue", file=sys.stderr)
        return 1
    if not spec.fixture:
        print(f"  {key}: no fixture filename declared", file=sys.stderr)
        return 1
    if not spec.runnable:
        print(f"  {key}: skipped — {spec.why_not_runnable()}")
        return 0

    blob, error = _get(spec)
    if blob is None:
        print(f"  {key}: FAILED — {error}", file=sys.stderr)
        return 1

    if isinstance(blob, dict):
        blob = {"_fixture_note": NOTE, **blob}

    path = FIXTURES / spec.fixture
    path.write_text(json.dumps(blob, indent=1, ensure_ascii=False))
    size = path.stat().st_size
    print(f"  {key}: wrote {spec.fixture} ({size:,} bytes)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys", nargs="*", help="source keys, or use --all")
    parser.add_argument("--all", action="store_true", help="every runnable source")
    args = parser.parse_args()

    if not network_allowed():
        print(
            "RADAR_ALLOW_NETWORK is not set, so nothing would be fetched.\n"
            "Re-run as:  RADAR_ALLOW_NETWORK=1 .venv/bin/python "
            "scripts/record_fixture.py --all",
            file=sys.stderr,
        )
        return 2

    keys = [s.key for s in CATALOG if s.runnable] if args.all else args.keys
    if not keys:
        print("Nothing to do. Pass source keys or --all. Runnable sources:")
        for spec in CATALOG:
            if spec.runnable:
                print(f"  {spec.key:22s} {spec.label}")
        return 0

    print(f"Recording {len(keys)} source(s) into {FIXTURES}:")
    return max(record(key) for key in keys)


if __name__ == "__main__":
    raise SystemExit(main())
