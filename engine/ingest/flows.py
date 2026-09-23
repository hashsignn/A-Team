"""Calibrate the synthetic book against Sika's own order history.

The shipment generator used to say this, in a comment, out loud:

    # Weight the anchor lane up: the Rhine demo needs enough shipments on it
    # for the per-event matrix to have something to show.
    weights = [3.0 if lane["focus"] == "rhine" else 1.0 for lane in lanes]

That is a thumb on the scale. It is honest about being one, which is the only
thing to be said for it: the Rhine dominated the board because the demo needed
it to, not because anything said it should.

Sika's intercompany export says what should. 9,707 purchase documents over
eighteen months, by origin and destination country. Weighting the lane draw by
those counts replaces the fudge with evidence, and the Rhine's prominence — if
it survives — is then earned.

WHAT THIS DOES AND DOES NOT CLAIM
=================================
It claims the LANE MIX and the SEASONALITY. Both are in the export and both
are Sika's.

It does not claim the consignment. Value, promised date, mode, carrier and
cargo class are not in the export — it ends at the purchase document — so
those stay declared and the inputs panel keeps calling them assumptions. A
book calibrated on real lanes is still a synthetic book, and saying otherwise
because a spreadsheet arrived would be the easiest lie available here.

ABSENT IS THE NORMAL CASE
=========================
``config/flows.yaml`` is gitignored, so most machines will not have it — CI,
a fresh clone, anyone who is not the customer. The fallback is the old
behaviour, reported as an assumption rather than silently substituted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from engine.config import Config

FLOWS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "flows.yaml"

# A configured lane with no counterpart in the export still gets drawn, at a
# low rate. Zero would delete it, and a lane the customer did not happen to
# ship on during these eighteen months is not a lane that cannot exist — the
# export is one filtered slice, not their whole book.
#
# PROPORTIONAL, not absolute. The first version was a flat 0.15 against
# document counts in the thousands, which is not a floor — it is deletion
# with extra steps. A share of the mean keeps an unmatched lane genuinely
# present at any book size.
FLOOR_SHARE_OF_MEAN = 0.04

# The old rule, kept exactly, for when there is no export to read.
ANCHOR_FOCUS = "rhine"
ANCHOR_WEIGHT = 3.0


@dataclass(frozen=True)
class Flows:
    """Sika's real lane mix, or the honest absence of it."""

    available: bool
    by_pair: dict[tuple[str, str], int] = field(default_factory=dict)
    months: Counter = field(default_factory=Counter)
    documents: int = 0
    first_month: str = ""
    last_month: str = ""
    detail: str = ""

    @property
    def status(self) -> str:
        return "connected" if self.available else "absent"

    def report(self) -> dict:
        """The socket row, in the shape /inputs already prints."""
        return {
            "key": "sika_flows",
            "label": "Sika intercompany flow export",
            "status": self.status,
            "detail": self.detail,
            "unlocks_if_connected": (
                "The lane mix and the month-by-month volume come from Sika's "
                "own order history instead of from a weighting we chose. "
                "Consignment value and promised dates stay declared either "
                "way — they are not in the export."
            ),
        }


def load(path: Path | None = None) -> Flows:
    """Read the derived flow file, or report plainly that there is none."""
    path = path or FLOWS_PATH
    if not path.exists():
        return Flows(
            available=False,
            detail=(
                "not present — the lane mix is a weighting we chose, not "
                "Sika's. Run scripts/import_sika_flows.py against their "
                "export to replace it."
            ),
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        # A malformed file is a missing file. The generator has a working
        # fallback and does not need a third state.
        return Flows(available=False, detail=f"unreadable ({exc.__class__.__name__})")

    rows = raw.get("flows") or []
    by_pair: dict[tuple[str, str], int] = {}
    months: Counter = Counter()
    for row in rows:
        origin = str(row.get("origin_country", "")).strip()
        destination = str(row.get("destination_country", "")).strip()
        docs = row.get("documents")
        if not origin or not destination or not isinstance(docs, int):
            continue
        by_pair[(origin, destination)] = by_pair.get((origin, destination), 0) + docs
        for month, count in (row.get("monthly") or {}).items():
            if isinstance(count, int):
                months[str(month)] += count

    if not by_pair:
        return Flows(available=False, detail=f"{path.name} carried no usable lanes")

    total = sum(by_pair.values())
    ordered = sorted(months)
    return Flows(
        available=True,
        by_pair=by_pair,
        months=months,
        documents=total,
        first_month=ordered[0] if ordered else "",
        last_month=ordered[-1] if ordered else "",
        detail=(
            f"{total:,} purchase document(s) across {len(by_pair)} country "
            f"pair(s), {ordered[0]} to {ordered[-1]}"
            if ordered else f"{total:,} purchase document(s)"
        ),
    )


def pair_of(lane: dict, config: Config) -> tuple[str, str]:
    """The (origin, destination) countries a configured lane runs between."""
    legs = lane.get("legs") or []
    if not legs:
        return ("", "")
    nodes = config.nodes
    first, last = legs[0].get("from"), legs[-1].get("to")
    return (
        nodes[first].country if first in nodes else "",
        nodes[last].country if last in nodes else "",
    )


def lane_weights(config: Config, flows: Flows | None = None) -> list[float]:
    """How often each configured lane should be drawn.

    With the export: the real document count for that country pair, floored so
    a lane Sika did not ship on during these eighteen months still appears.

    Without it: the old rule, unchanged, so nothing about a machine that has
    no export behaves differently from before.
    """
    flows = flows if flows is not None else load()
    lanes = config.lanes

    if not flows.available:
        return [
            ANCHOR_WEIGHT if lane.get("focus") == ANCHOR_FOCUS else 1.0
            for lane in lanes
        ]

    # Several configured lanes can serve one country pair — Stuttgart to
    # Shanghai and Stuttgart to Ningbo are both DE→CN. The pair's documents
    # are SHARED between them rather than counted once each, or a pair served
    # by three lanes would look three times as busy as it is.
    served: Counter = Counter()
    pairs = [pair_of(lane, config) for lane in lanes]
    for pair in pairs:
        if pair in flows.by_pair:
            served[pair] += 1

    matched = [
        flows.by_pair[pair] / served[pair]
        for pair in pairs if pair in flows.by_pair
    ]
    floor = (sum(matched) / len(matched)) * FLOOR_SHARE_OF_MEAN if matched else 1.0

    weights = []
    for pair in pairs:
        docs = flows.by_pair.get(pair)
        weights.append(floor if docs is None else max(floor, docs / served[pair]))
    return weights


def month_weights(flows: Flows) -> dict[str, float]:
    """Relative order volume per calendar month, normalised to a mean of 1.

    Returned by month-of-year rather than by YYYY-MM, because the board runs
    at an as-of the export does not necessarily cover and the useful signal is
    the seasonal shape, not the specific month.
    """
    if not flows.available or not flows.months:
        return {}
    by_month: Counter = Counter()
    for stamp, count in flows.months.items():
        parts = stamp.split("-")
        if len(parts) == 2 and parts[1].isdigit():
            by_month[parts[1]] += count
    if not by_month:
        return {}
    mean = sum(by_month.values()) / len(by_month)
    return {m: round(n / mean, 3) for m, n in sorted(by_month.items())}
