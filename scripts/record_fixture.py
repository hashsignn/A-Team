#!/usr/bin/env python3
"""Record a live source response as the fixture the demo falls back to.

    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py gdelt_doc
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py --all
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py --all \\
        --as-of 2026-09-22 --days 60

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

A WINDOW THAT ALREADY HAPPENED
==============================
Only a source with an archive can be asked about the past — today that is
GDELT alone. The rest publish what is true now and never had a yesterday, so
they are recorded as the snapshot they are, and say so.

An archive is asked ONE DAY AT A TIME. A single GDELT answer holds at most
250 articles, and a query naming the world's busiest ports fills 250 inside a
day, so asking once for sixty days returns roughly the last day and calls it
sixty. Sliced, sixty days is sixty requests, merged, with anything returned
twice kept once.

Inside each day the articles are ranked by relevance to the query rather
than by recency. Newest-first, sliced by day, would keep the last few hours
of every day; relevance keeps the stories that mattered that day.

GDELT asks for no more than one request every five seconds, so sixty days
takes about six minutes. A day that fails is retried once; a day that fails
twice is written down as a gap in the fixture and in the exit code, never
papered over.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.ingest.sources import CATALOG, network_allowed  # noqa: E402
from engine.ingest.sources.fetch import _get  # noqa: E402
from engine.ingest.sources.mapping import resolve  # noqa: E402

FIXTURES = ROOT / "data" / "fixtures"

NOTE = (
    "RECORDED FROM THE LIVE SOURCE by scripts/record_fixture.py. Real response, "
    "real content, frozen at the time below so the demo replays identically."
)

# One request per day of a historical window. See the module docstring: one
# request per window is capped at a day's worth of a busy query.
SLICE_DAYS = 1.0

# GDELT's stated limit is one request every five seconds. A second of margin,
# because being refused mid-window costs a retry and a retry costs twenty.
PAUSE_S = 6.0
RETRY_PAUSE_S = 20.0

# Inside each day, rank by relevance rather than recency. Per source, because
# the parameter is the source's own vocabulary.
SLICE_PARAMS: dict[str, dict[str, str]] = {"gdelt_doc": {"sort": "hybridrel"}}


def _put(blob, path: str, value):
    """Set ``value`` at a dotted ``path`` in a decoded response."""
    if not path:
        return value
    out = dict(blob) if isinstance(blob, dict) else {}
    head, _, rest = path.partition(".")
    out[head] = _put(out.get(head, {}), rest, value) if rest else value
    return out


def record_window(spec, as_of, span_days: float, *, fetch=_get,
                  pause=time.sleep, say=print):
    """Ask an archive about ``span_days`` ending at ``as_of``, a day at a time.

    Returns ``(blob, missing)``: the merged response in the source's own
    shape, or None if every day failed, and the days that could not be had.
    ``fetch`` and ``pause`` are parameters so this can be tested without a
    network or a six-minute wait.
    """
    sliced = replace(
        spec, params={**spec.params, **SLICE_PARAMS.get(spec.key, {})}
    )
    count = max(1, math.ceil(span_days / SLICE_DAYS - 1e-9))

    merged: list = []
    seen: set[str] = set()
    template = None
    days: list[dict] = []
    missing: list[dict] = []

    for index in range(count):
        end = as_of - timedelta(days=index * SLICE_DAYS)
        length = min(SLICE_DAYS, span_days - index * SLICE_DAYS)
        start = end - timedelta(days=length)
        label = f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"

        if index:
            pause(PAUSE_S)
        blob, error = fetch(sliced, as_of=end, window_days=length)
        if blob is None:
            pause(RETRY_PAUSE_S)
            blob, error = fetch(sliced, as_of=end, window_days=length)
        if blob is None:
            missing.append({"from": start.isoformat(), "to": end.isoformat(),
                            "error": error})
            say(f"    {label}  FAILED twice — {error}")
            continue

        records = resolve(blob, spec.items_path) if spec.items_path else blob
        if not isinstance(records, list):
            records = []
        new = 0
        for record in records:
            url = resolve(record, spec.fields.url) if spec.fields.url else None
            key = str(url) if url else json.dumps(record, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
            new += 1
        if template is None:
            template = blob
        days.append({"from": start.isoformat(), "to": end.isoformat(),
                     "records": len(records), "new": new})
        say(f"    {label}  {len(records):4d} returned, {new:4d} new")

    if template is None:
        return None, missing

    out = _put(template, spec.items_path, merged) if spec.items_path else merged
    if isinstance(out, dict):
        out = {
            "_fixture_note": NOTE,
            "_window": {
                "ends": as_of.isoformat(),
                "days": span_days,
                "request_per": f"{SLICE_DAYS:g} day",
                "params": SLICE_PARAMS.get(spec.key, {}),
                "records": len(merged),
                "slices": days,
                "missing": missing,
            },
            **out,
        }
    return out, missing


def _write(spec, blob) -> int:
    # The note has always said "frozen at the time below", and nothing ever
    # wrote a time. The reasoning recorder reads this to say how old the
    # fixture it is about to reason over is.
    if isinstance(blob, dict):
        from engine.clock import Clock  # noqa: PLC0415
        blob = {**blob, "_recorded_at": Clock.wall().as_of.isoformat()}
    path = FIXTURES / spec.fixture
    path.write_text(json.dumps(blob, indent=1, ensure_ascii=False),
                    encoding="utf-8", newline="\n")
    return path.stat().st_size


def record(key: str, as_of=None, days: float | None = None) -> int:
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

    reach = spec.window is not None

    if as_of is not None and reach:
        span = days if days is not None else spec.window.days
        slices = max(1, math.ceil(span / SLICE_DAYS - 1e-9))
        print(f"  {key}: {span:g} days, {slices} request(s), about "
              f"{max(0, slices - 1) * PAUSE_S / 60:.0f} min")
        blob, missing = record_window(spec, as_of, span)
        if blob is None:
            print(f"  {key}: FAILED — no day could be fetched; the existing "
                  f"fixture was left as it was", file=sys.stderr)
            return 1
        size = _write(spec, blob)
        held = blob["_window"]["records"] if isinstance(blob, dict) else len(blob)
        print(f"  {key}: wrote {spec.fixture} — {held:,} records, {size:,} bytes")
        if missing:
            print(f"  {key}: {len(missing)} of {slices} day(s) MISSING — "
                  f"re-run to fill them:", file=sys.stderr)
            for gap in missing:
                print(f"      {gap['from'][:10]}  {gap['error']}", file=sys.stderr)
            return 1
        return 0

    # Most sources can only answer "what is happening now" — a motorway
    # closure feed has no archive and never did. Asking them for a past
    # window would quietly hand back today anyway, so say which is which
    # rather than letting the filename imply a recording that is not one.
    if as_of is not None and not reach:
        print(f"  {key}: now only — this source has no archive")

    blob, error = _get(spec)
    if blob is None:
        print(f"  {key}: FAILED — {error}", file=sys.stderr)
        return 1
    if isinstance(blob, dict):
        blob = {"_fixture_note": NOTE, **blob}
    size = _write(spec, blob)
    print(f"  {key}: wrote {spec.fixture} ({size:,} bytes)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("keys", nargs="*", help="source keys, or use --all")
    parser.add_argument("--all", action="store_true", help="every runnable source")
    parser.add_argument(
        "--as-of", default=None,
        help="record the window ENDING here (ISO, e.g. 2026-09-22). Only "
             "sources with an archive honour it; the rest record now.",
    )
    parser.add_argument(
        "--days", type=float, default=None,
        help="how far back from --as-of to ask, fetched one day per request. "
             "Defaults to each source's own declared window.",
    )
    args = parser.parse_args()

    as_of = None
    if args.as_of:
        from engine.clock import Clock  # noqa: PLC0415
        as_of = Clock.at(args.as_of).as_of
    if args.days is not None and as_of is None:
        print("--days needs --as-of: a span with no end is not a window.",
              file=sys.stderr)
        return 2
    if args.days is not None and args.days <= 0:
        print("--days must be more than zero.", file=sys.stderr)
        return 2

    if not network_allowed():
        print("RADAR_ALLOW_NETWORK is not set, so nothing would be fetched. "
              "Re-run as:", file=sys.stderr)
        if platform.system() == "Windows":
            print("  set RADAR_ALLOW_NETWORK=1", file=sys.stderr)
            print(r"  .venv\Scripts\python scripts\record_fixture.py --all",
                  file=sys.stderr)
        else:
            print("  RADAR_ALLOW_NETWORK=1 .venv/bin/python "
                  "scripts/record_fixture.py --all", file=sys.stderr)
        return 2

    keys = [s.key for s in CATALOG if s.runnable] if args.all else args.keys
    if not keys:
        print("Nothing to do. Pass source keys or --all. Runnable sources:")
        for spec in CATALOG:
            if spec.runnable:
                print(f"  {spec.key:22s} {spec.label}")
        return 0

    if as_of is not None:
        archived = [k for k in keys
                    if next((s for s in CATALOG if s.key == k), None)
                    and next(s for s in CATALOG if s.key == k).window is not None]
        span = f"{args.days:g} days" if args.days else "each source's own window"
        print(f"Window ends {as_of:%Y-%m-%d %H:%M} UTC, reaching back {span}.")
        print(f"{len(archived)} of {len(keys)} source(s) have an archive to "
              f"reach into: {', '.join(archived) or 'none'}.")
        print()

    print(f"Recording {len(keys)} source(s) into {FIXTURES}:")
    return max(record(key, as_of=as_of, days=args.days) for key in keys)


if __name__ == "__main__":
    raise SystemExit(main())
