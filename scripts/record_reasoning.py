#!/usr/bin/env python3
"""Run the funnel once with a model, over the recorded sources, and keep the answers.

    # 1. freeze the sources, anywhere with the network
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py \
        --all --as-of 2026-09-22 --days 60

    # 2. on the machine that HAS a model — with the network OFF
    RADAR_RECORD_REASONING=1 .venv/bin/python scripts/record_reasoning.py \
        --as-of 2026-09-22

    git add data/fixtures data/reasoning && git commit -m "Record sources and reasoning"

    # everywhere else — Codespace, laptop, a stage with no wifi
    python run.py serve        # no model needed; the answers replay

WHY THE NETWORK IS OFF FOR STEP 2
=================================
An answer is keyed by the prompt, and the prompt is built from the headline.
The Codespace builds its prompts from data/fixtures. So the answers have to
be recorded over data/fixtures too — with the network on, every source is
fetched live instead, the answers are for whatever the internet said at that
minute, and on replay every one of them misses. Nothing fails; nothing
replays. This script refuses to run with RADAR_ALLOW_NETWORK set for exactly
that reason, and reports which fixtures are real recordings before it spends
any model time.

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
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.clock import Clock  # noqa: E402
from engine.config import load_config  # noqa: E402
from engine.ingest import watergauge  # noqa: E402
from engine.ingest.sources import CATALOG, network_allowed  # noqa: E402
from engine.pipeline import RunOptions, run  # noqa: E402
from engine.reason import cache, llm  # noqa: E402

FIXTURES = ROOT / "data" / "fixtures"
RECORDED = "RECORDED FROM THE LIVE SOURCE"


def fixture_status(spec) -> tuple[str, bool]:
    """What a source's fixture is, and whether it is a real recording.

    Read from the file, because the file is what step 2 will actually reason
    over. The commonest way to waste a recording is to run step 2 before the
    fixtures from step 1 have been pulled onto this machine, and the only
    symptom would be answers recorded for the sample headlines.
    """
    path = FIXTURES / spec.fixture if spec.fixture else None
    if path is None or not path.exists():
        return "no fixture", False
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable ({exc.__class__.__name__})", False
    note = blob.get("_fixture_note", "") if isinstance(blob, dict) else ""
    if not str(note).startswith(RECORDED):
        return "still the SAMPLE shipped with the repo", False
    when = str(blob.get("_recorded_at", ""))[:10] or "date not stamped"
    window = blob.get("_window")
    if isinstance(window, dict):
        gaps = len(window.get("missing") or [])
        return (
            f"recorded {when}: {window.get('days', 0):g} days, "
            f"{window.get('records', 0):,} records"
            + (f", {gaps} day(s) missing" if gaps else "")
        ), True
    return f"recorded {when}, snapshot", True


def gauge_status() -> tuple[str, bool]:
    """The same question for the Kaub gauge, which is not a catalogue source."""
    path = FIXTURES / watergauge.FIXTURE_NAME
    if not path.exists():
        return "no fixture", False
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable ({exc.__class__.__name__})", False
    if not (isinstance(blob, dict) and blob.get("is_real_data") is True):
        return "still the GENERATED series shipped with the repo", False
    when = str(blob.get("_recorded_at", ""))[:10] or "date not stamped"
    return f"recorded {when}: {len(blob.get('measurements') or []):,} readings", True


def network_refusal() -> list[str]:
    """Why this must not run with the network on, and how to turn it off."""
    if platform.system() == "Windows":
        unset = "set RADAR_ALLOW_NETWORK="
    else:
        unset = "unset RADAR_ALLOW_NETWORK"
    return [
        "RADAR_ALLOW_NETWORK is set, so every source would be fetched live",
        "instead of read from data/fixtures. The answers would be recorded for",
        "whatever the internet says right now, and the Codespace — which reads",
        "data/fixtures — would miss every one of them on replay.",
        "",
        "Turn it off in this window and run again:",
        f"  {unset}",
    ]


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
        if platform.system() == "Windows":
            print(f"  set {cache.RECORD_ENV}=1")
            print(r"  .venv\Scripts\python scripts\record_reasoning.py")
        else:
            print(f"  {cache.RECORD_ENV}=1 "
                  ".venv/bin/python scripts/record_reasoning.py")
        print("\nIt is off by default so a demo machine can never quietly")
        print("write answers into the repository.")
        return 1

    if network_allowed():
        for line in network_refusal():
            print(line)
        return 2

    status = llm.detect()
    print(f"backend : {status.backend.value}")
    print(f"model   : {status.model or '—'}")
    if not status.available:
        print("\nNo model is reachable, so there is nothing to record.")
        print("Run scripts/check_models.py — it names the one thing to fix.")
        print("(It is platform-aware; the command it prints will be the right")
        print(" one for this machine.)")
        return 1

    print("network : off — sources are read from data/fixtures")
    print("fixtures:")
    archive_is_sample = False
    for spec in CATALOG:
        if not spec.runnable or not spec.fixture:
            continue
        what, real = fixture_status(spec)
        print(f"  {spec.key:20s} {what}")
        if spec.window is not None and not real:
            archive_is_sample = True
    print(f"  {'watergauge_kaub':20s} {gauge_status()[0]}")
    if archive_is_sample:
        print("\n  The news fixture is still the sample, so the answers would be")
        print("  recorded for the sample headlines. If you recorded sources on")
        print("  another machine, pull them here first:")
        print("    git pull origin claude/elegant-clarke-711wkt")
    print()

    before = cache.report()
    print(f"on disk : {before['entries']} answer(s) already recorded")
    print(f"\nRunning the funnel at {args.as_of} over "
          f"{args.shipments} shipments…")

    context = run(
        clock=Clock.at(args.as_of),
        config=load_config(),
        options=RunOptions(shipment_count=args.shipments),
    )

    live = [r for r in context.reports if r.status.value == "connected"]
    fixture = [r for r in context.reports if r.status.value == "fixture"]
    print(f"  sources: {len(live)} live, {len(fixture)} on recorded fixtures")
    for report in live:
        print(f"    live · {report.label}: {report.detail}")

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
    print("  git add data/fixtures data/reasoning")
    print('  git commit -m "Record sources and reasoning"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
