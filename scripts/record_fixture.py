#!/usr/bin/env python3
"""Record a live source response as the fixture the demo falls back to.

    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py gdelt_doc
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py watergauge_kaub
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py --all
    RADAR_ALLOW_NETWORK=1 .venv/bin/python scripts/record_fixture.py --all \\
        --as-of 2026-09-22 --days 60

WHY THIS EXISTS
===============
Every fixture in data/fixtures is a SHAPE, written by the build team, because
the environment this was built in blocks every data host at the egress proxy.
The field names, nesting and date formats are the real ones; the content is
not, and the /inputs panel says so. The same shapes are kept in
tests/fixtures, which is what the tests read: a recording here changes the
demo, never a test.

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

The Kaub gauge sits between the two. Pegelonline keeps the last thirty days
of readings and nothing older, so it is recorded as that month, up to now,
and the board cuts it at its own as-of when it replays. It is not in the
source catalogue — a level is a measurement with thresholds, read by
engine/ingest/watergauge.py, not a report to triage — so it is recorded here
by name, and --all includes it.

An archive is asked ONE DAY AT A TIME. A single GDELT answer holds at most
250 articles, and a query naming the world's busiest ports fills 250 inside a
day, so asking once for sixty days returns roughly the last day and calls it
sixty. Sliced, sixty days is sixty requests, merged, with anything returned
twice kept once.

Inside each day the articles are ranked by relevance to the query rather
than by recency. Newest-first, sliced by day, would keep the last few hours
of every day; relevance keeps the stories that mattered that day.

GDELT asks for no more than one request every five seconds, so sixty days
takes about six minutes when it is answering.

WHEN IT IS NOT ANSWERING
========================
GDELT rate-limits by network, and a shared one — university Wi-Fi, a VPN —
can be refused before this script has sent a second request. So:

    every day is kept the moment it arrives, under data/cache/ (gitignored).
    A run that is refused, interrupted or stopped with Ctrl+C loses nothing,
    and the next run fetches only the days that are missing.

    a refusal is waited out, not hammered: 30 s, then 60, then 120 — longer
    if the server's Retry-After asks for longer — resetting after any
    success, because requests arriving inside a cooldown extend it.

    if a day is still refused after all of that, the run stops, writes what
    it has, lists what it does not, and says to come back later. Grinding
    through the remaining days would only fail each of them the same way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.ingest.sources import CATALOG, network_allowed  # noqa: E402
from engine.ingest.sources.fetch import _get  # noqa: E402
from engine.ingest.sources.mapping import resolve  # noqa: E402

FIXTURES = ROOT / "data" / "fixtures"

# Not a catalogue source; see the module docstring.
GAUGE_KEY = "watergauge_kaub"

NOTE = (
    "RECORDED FROM THE LIVE SOURCE by scripts/record_fixture.py. Real response, "
    "real content, frozen at the time below so the demo replays identically."
)

# One request per day of a historical window. See the module docstring: one
# request per window is capped at a day's worth of a busy query.
SLICE_DAYS = 1.0

# GDELT's stated limit is one request every five seconds, plus a second of
# margin.
PAUSE_S = 6.0

# How long to wait after a refusal or a failure, in turn, before asking again.
# Shared across days and reset by any success: a source that refused one day
# and answered the next has recovered, and one refusing everything has not.
# When the schedule runs out, the run stops — see the module docstring.
BACKOFF_S = (30.0, 60.0, 120.0)

# Days are kept here as they arrive, so a re-run resumes rather than restarts.
# Under data/cache/, which is gitignored: the merged fixture is what gets
# committed, not its parts.
CACHE = ROOT / "data" / "cache" / "record_fixture"

# A day ending this recently may still be filling in — GDELT indexes with a
# lag — so it is fetched each time rather than frozen half-complete.
SETTLED_AFTER = timedelta(hours=24)

_RETRY_AFTER = re.compile(r"retry after (\d+)s")

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


@dataclass
class WindowResult:
    """What a historical window came back as."""

    blob: object | None                     # merged, in the source's shape
    missing: list[dict] = field(default_factory=list)
    stopped: str | None = None              # why the run ended early, if it did
    fetched: int = 0                        # days asked for this run
    reused: int = 0                         # days already on disk


def _slice_path(spec, params: dict, start, end) -> Path:
    """Where one day's answer is kept. The params are part of the name, so a
    changed query never reuses days recorded for the old one."""
    digest = hashlib.sha256(
        json.dumps(params, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    return CACHE / spec.key / f"{start:%Y%m%dT%H%M%S}_{end:%Y%m%dT%H%M%S}_{digest}.json"


def _load_slice(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                         # half-written or missing: refetch


def _keep_slice(path: Path, blob) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_text(json.dumps(blob, ensure_ascii=False),
                       encoding="utf-8", newline="\n")
    partial.replace(path)                   # a Ctrl+C mid-write leaves no day


def record_window(spec, as_of, span_days: float, *, fetch=_get,
                  pause=time.sleep, say=print, now=None) -> WindowResult:
    """Ask an archive about ``span_days`` ending at ``as_of``, a day at a time.

    ``fetch``, ``pause`` and ``now`` are parameters so this can be tested
    without a network, a six-minute wait, or a real clock.
    """
    if now is None:
        from engine.clock import Clock  # noqa: PLC0415
        now = Clock.wall().as_of

    sliced = replace(
        spec, params={**spec.params, **SLICE_PARAMS.get(spec.key, {})}
    )
    count = max(1, math.ceil(span_days / SLICE_DAYS - 1e-9))
    plan = []
    for index in range(count):
        end = as_of - timedelta(days=index * SLICE_DAYS)
        length = min(SLICE_DAYS, span_days - index * SLICE_DAYS)
        start = end - timedelta(days=length)
        plan.append((start, end, length, _slice_path(spec, sliced.params, start, end)))

    have = sum(1 for *_, path in plan if path.exists())
    todo = count - have
    if have:
        say(f"    {have} of {count} day(s) already recorded; fetching the "
            f"other {todo}, about {max(0, todo - 1) * PAUSE_S / 60:.0f} min")

    merged: list = []
    seen: set[str] = set()
    template = None
    days: list[dict] = []
    result = WindowResult(blob=None)
    waits = iter(BACKOFF_S)
    asked_before = False

    for position, (start, end, length, path) in enumerate(plan):
        label = f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"

        blob = _load_slice(path) if path.exists() else None
        if blob is not None:
            source = "kept"
            result.reused += 1
        else:
            source = "fetched"
            if asked_before:
                pause(PAUSE_S)
            asked_before = True
            while True:
                blob, error = fetch(sliced, as_of=end, window_days=length)
                if blob is not None:
                    waits = iter(BACKOFF_S)          # recovered
                    break
                wait = next(waits, None)
                if wait is None:
                    break                            # the schedule is spent
                asked = _RETRY_AFTER.search(error or "")
                if asked:
                    wait = max(wait, float(asked.group(1)))
                say(f"    {label}  {error} — waiting {wait:.0f}s")
                pause(wait)
            result.fetched += 1

            if blob is None:
                waited = ", ".join(f"{w:.0f} s" for w in BACKOFF_S)
                result.stopped = f"still {error} after waiting {waited}"
                for gap_start, gap_end, _, _ in plan[position:]:
                    result.missing.append({
                        "from": gap_start.isoformat(),
                        "to": gap_end.isoformat(),
                        "error": error if gap_end == end else "not fetched — stopped",
                    })
                say(f"    {label}  still {error} — stopping")
                break

            if now - end >= SETTLED_AFTER:
                _keep_slice(path, blob)

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
                     "records": len(records), "new": new, "source": source})
        if source == "fetched":
            say(f"    {label}  {len(records):4d} returned, {new:4d} new")

    if template is None:
        return result

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
                "missing": result.missing,
            },
            **out,
        }
    result.blob = out
    return result


def _write(spec, blob) -> int:
    return _write_file(spec.fixture, blob)


def _write_file(name: str, blob) -> int:
    # The note has always said "frozen at the time below", and nothing ever
    # wrote a time. The reasoning recorder reads this to say how old the
    # fixture it is about to reason over is.
    if isinstance(blob, dict):
        from engine.clock import Clock  # noqa: PLC0415
        blob = {**blob, "_recorded_at": Clock.wall().as_of.isoformat()}
    path = FIXTURES / name
    path.write_text(json.dumps(blob, indent=1, ensure_ascii=False),
                    encoding="utf-8", newline="\n")
    return path.stat().st_size


def _stop_advice(stopped: str) -> list[str]:
    """What to do next — which depends on why it stopped.

    Only a 429 means the source is limiting this network. Anything else
    after that long is the connection, and saying "the source is limiting
    you" about a dead Wi-Fi link would send somebody to the wrong fix.
    """
    kept = [
        "Every day already fetched is kept and will not be fetched again, so",
        "running exactly the same command carries on where this stopped.",
    ]
    if "HTTP 429" in stopped:
        return [
            "The source is limiting requests from this network. Wait a while,",
            "then run the command again.",
            *kept,
            "If it is refused from the very first request every time, the",
            "network is probably shared (university Wi-Fi, a VPN) and other",
            "people's requests count against it — try from another one.",
        ]
    return [
        "The source could not be reached. Check the connection, then run",
        "the command again.",
        *kept,
    ]


def record_gauge(as_of=None, *, fetch=None) -> int:
    """Record the Kaub gauge: the last month, as Pegelonline sends it.

    The raw answer is kept, as for every source, and checked with the same
    parser the board will read it with before anything is written — a
    recording the board cannot read is worse than the sample it replaced.
    """
    from engine.ingest import watergauge  # noqa: PLC0415
    from engine.ingest.sources.fetch import get_json  # noqa: PLC0415

    if as_of is not None:
        print(f"  {GAUGE_KEY}: the last 30 days up to now — Pegelonline keeps "
              f"no more")
    payload, error = (fetch or get_json)(watergauge.LIVE_URL)
    if payload is None:
        print(f"  {GAUGE_KEY}: FAILED — {error}", file=sys.stderr)
        return 1
    try:
        series = watergauge.parse_pegelonline(payload)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"  {GAUGE_KEY}: FAILED — the answer was not a list of readings "
              f"({type(exc).__name__}); the existing fixture was left as it was",
              file=sys.stderr)
        return 1
    if not series:
        print(f"  {GAUGE_KEY}: FAILED — the answer held no readings; the "
              f"existing fixture was left as it was", file=sys.stderr)
        return 1

    first, last = series[0][0], series[-1][0]
    size = _write_file(watergauge.FIXTURE_NAME, {
        "_fixture_note": NOTE,
        "station": "KAUB",
        "river": "Rhine",
        "unit": "cm",
        "label": "RECORDED FROM PEGELONLINE — observed readings",
        "is_real_data": True,
        "source_url": watergauge.LIVE_URL,
        "covers": {"from": first.isoformat(), "to": last.isoformat()},
        "measurements": payload,
    })
    print(f"  {GAUGE_KEY}: wrote {watergauge.FIXTURE_NAME} — {len(series):,} "
          f"readings, {first:%Y-%m-%d} to {last:%Y-%m-%d %H:%M}, latest "
          f"{series[-1][1]:.0f} cm, {size:,} bytes")
    if as_of is not None and as_of < first:
        print(f"  {GAUGE_KEY}: the readings start after {as_of:%Y-%m-%d}, so a "
              f"board at that date will have no gauge reading", file=sys.stderr)
    return 0


def record(key: str, as_of=None, days: float | None = None) -> int:
    if key == GAUGE_KEY:
        return record_gauge(as_of)
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
              f"{max(0, slices - 1) * PAUSE_S / 60:.0f} min if nothing is "
              f"refused")
        result = record_window(spec, as_of, span)

        if result.blob is not None:
            size = _write(spec, result.blob)
            held = (result.blob["_window"]["records"]
                    if isinstance(result.blob, dict) else len(result.blob))
            print(f"  {key}: wrote {spec.fixture} — {held:,} records from "
                  f"{slices - len(result.missing)} of {slices} day(s), "
                  f"{size:,} bytes")
        else:
            print(f"  {key}: nothing fetched — the existing fixture was left "
                  f"as it was", file=sys.stderr)

        if result.stopped:
            print(f"\n  {key} STOPPED: {result.stopped}.", file=sys.stderr)
            for line in _stop_advice(result.stopped):
                print(f"  {line}", file=sys.stderr)
            print(file=sys.stderr)
            return 1
        if result.missing:
            print(f"  {key}: {len(result.missing)} day(s) missing — re-run "
                  f"to fill them", file=sys.stderr)
            return 1
        return 0 if result.blob is not None else 1

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

    runnable = [s.key for s in CATALOG if s.runnable] + [GAUGE_KEY]
    keys = runnable if args.all else args.keys
    if not keys:
        print("Nothing to do. Pass source keys or --all. Runnable sources:")
        for spec in CATALOG:
            if spec.runnable:
                print(f"  {spec.key:22s} {spec.label}")
        print(f"  {GAUGE_KEY:22s} Rhine water level — Kaub (Pegelonline)")
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
