#!/usr/bin/env python3
"""Run the funnel once with a model, and keep the answers.

    # on the machine that HAS a model
    RADAR_RECORD_REASONING=1 RADAR_ALLOW_NETWORK=1 \
      .venv/bin/python scripts/record_reasoning.py --as-of 2026-09-16

    git add data/reasoning && git commit -m "Record the reasoning layer"

    # everywhere else — Codespace, laptop, a stage with no wifi
    python run.py serve        # no model needed; the answers replay

WHY THIS WORKS
==============
A model's answer to a fixed prompt about a fixed headline is a fact about
that pair, not about the machine that asked. Record it once and every later
run replays it for free.

WHAT IT DOES NOT DO
===================
It does not make the demo model-driven for events it has never seen. A
headline outside the recording misses the cache and, with no model present,
is handled by the deterministic router alone — the same supported state as
before. Recording is a way to carry a rehearsed run; it is not a model in a
file.

RE-RECORD WHEN THE PROMPT CHANGES. The cache key includes the system prompt,
so an edited prompt misses rather than replaying answers to the old question.
That is the correct behaviour and it is also why the counts will drop the
first time you run after editing one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.clock import Clock  # noqa: E402
from engine.config import load_config  # noqa: E402
from engine.pipeline import RunOptions, run  # noqa: E402
from engine.reason import cache, llm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default="2026-09-16",
                        help="the instant to record, pinned and reproducible")
    parser.add_argument("--shipments", type=int, default=220)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be recorded, write nothing")
    args = parser.parse_args()

    if not cache.recording() and not args.dry_run:
        print("Recording is off. Re-run with:")
        print(f"  {cache.RECORD_ENV}=1 .venv/bin/python scripts/record_reasoning.py")
        print("\nIt is off by default so a demo machine can never quietly")
        print("write answers into the repository.")
        return 1

    status = llm.detect()
    print(f"backend : {status.backend.value}")
    print(f"model   : {status.model or '—'}")
    if not status.available:
        print("\nNo model is reachable, so there is nothing to record.")
        print("Run scripts/check_models.py — it names the one thing to fix.")
        return 1

    before = cache.report()
    print(f"on disk : {before['entries']} answer(s) already recorded")
    print(f"\nRunning the funnel at {args.as_of} over "
          f"{args.shipments} shipments…")

    context = run(
        clock=Clock.at(args.as_of),
        config=load_config(),
        options=RunOptions(shipment_count=args.shipments),
    )

    funnel = context.result.funnel
    print(f"  {funnel.raw_observations} arrived → "
          f"{funnel.after_resolution} distinct event(s) → "
          f"{funnel.reasoned} read by a model")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    written = cache.flush()
    after = cache.report()
    gained = after["entries"] - before["entries"]

    if not written:
        print("\nNothing new to write — every answer was already recorded.")
        return 0

    for path in written:
        print(f"\nwrote {path.relative_to(ROOT)}")
    print(f"  {gained} new answer(s), {after['entries']} in total")
    print(f"  from {', '.join(after['models'])}")
    print("\nCommit it, and the demo runs anywhere with no model:")
    print("  git add data/reasoning && git commit -m 'Record the reasoning layer'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
