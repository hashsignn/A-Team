#!/usr/bin/env python3
"""Turn Sika's intercompany flow export into configuration the radar can use.

    .venv/bin/python scripts/import_sika_flows.py config/20260921_innovathon_Sika_share.xlsx

WHERE THE FILE GOES
===================
``config/``, which is gitignored. This is the customer's real commercial
data: who ships what to whom, and how often. It does not belong in a public
history where it can never be redacted, and the repository already draws that
line — ``config.example/`` is the committed stand-in, ``config/`` is the real
thing and is ignored.

WHAT THE EXPORT IS
==================
Intercompany purchase documents: a Sika company in one country buying from a
Sika plant in another. One row per line item.

    Selling TP / Country    the DACH plant that supplies
    Purchasing Plant / Ctry the subsidiary that receives
    Creation Date           when the order was raised
    Net Weight Value        line weight in kg (Net Weight is per unit)

WHAT IT IS NOT
==============
Said plainly because the gap decides what the radar can honestly claim:

    no consignment value      there is no CHF column anywhere
    no promised date          only the date the order was raised
    no mode, no carrier       nothing says road, rail, barge or sea
    no delivery or ETA        the export ends at the purchase document

So this gives the real SHAPE of the network — which lanes exist, how busy
each one is, how that moves month to month — and none of the timing or money
the scoring needs. Those stay declared, and the panel says so.

WHAT THIS WRITES
================
``config/flows.yaml``: the real lanes with their document counts, weights and
monthly profile, plus the node mapping where one exists. Nothing is invented
to fill a gap — a lane whose endpoints the network cannot represent is
reported as unmapped rather than being quietly attached to the nearest port.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load_config  # noqa: E402

# Where a country's freight enters or leaves the modelled network.
#
# DERIVED from network.yaml rather than listed here. A hardcoded table is a
# second copy of the network that drifts: add a port and coverage silently
# stays where it was, which is exactly what happened the first time this ran
# after nine gateways were added. Deriving it means the network is the only
# place the answer lives.
#
# Plants and distribution sites count as origins; seaports and inland ports
# count as destinations. A chokepoint is neither — it is transited, never
# shipped to.
ORIGIN_KINDS = {"plant", "distribution"}
DESTINATION_KINDS = {"seaport", "inland_port"}


def gateways(config) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(origins, destinations) by ISO country code, from the network itself."""
    origins: dict[str, list[str]] = {}
    destinations: dict[str, list[str]] = {}
    for node_id, node in config.nodes.items():
        kind = node.kind.value
        if kind in ORIGIN_KINDS:
            origins.setdefault(node.country, []).append(node_id)
        if kind in DESTINATION_KINDS:
            destinations.setdefault(node.country, []).append(node_id)
    return origins, destinations


HEADER_ROW = 6   # 1-indexed; rows 1-5 are the filter notes Sika left on top


def read(path: Path) -> list[dict]:
    try:
        import openpyxl
    except ImportError:  # pragma: no cover - environment, not logic
        # The path differs by platform, and printing the wrong one is worse
        # than printing none: somebody types it, gets "not recognised", and
        # is now debugging their Python install instead of installing one
        # package. sys.executable is already the interpreter running this,
        # so it is right by construction on every platform.
        raise SystemExit(
            "openpyxl is needed to read the export, and is not installed.\n"
            f"  {sys.executable} -m pip install openpyxl"
        ) from None

    ws = openpyxl.load_workbook(path, data_only=True, read_only=True).worksheets[0]
    rows = list(ws.iter_rows(min_row=HEADER_ROW, values_only=True))
    if not rows:
        raise SystemExit(f"{path} has no rows from {HEADER_ROW} down")
    header = [str(h).strip() if h else f"col{i}" for i, h in enumerate(rows[0])]
    # strict=False deliberately: a trailing column with no header, or a short
    # row, is a spreadsheet being a spreadsheet. Refusing to read the file
    # over it would be worse than ignoring the ragged edge.
    return [dict(zip(header, r, strict=False)) for r in rows[1:] if r and r[0]]


def summarise(records: list[dict]) -> dict:
    """Per lane: documents, lines, kilos, and a month-by-month profile."""
    lanes: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"docs": set(), "lines": 0, "kg": 0.0,
                 "months": Counter(), "plants_from": Counter(),
                 "plants_to": Counter()}
    )
    dates: list[datetime] = []

    for row in records:
        origin = (row.get("Selling Country") or "").strip()
        destination = (row.get("Purchasing Ctry") or "").strip()
        if not origin or not destination:
            continue
        lane = lanes[(origin, destination)]
        lane["lines"] += 1
        if row.get("Pur. Doc.") is not None:
            lane["docs"].add(row["Pur. Doc."])
        weight = row.get("Net Weight Value")
        if isinstance(weight, (int, float)):
            lane["kg"] += float(weight)
        when = row.get("Creation Date")
        if isinstance(when, datetime):
            lane["months"][when.strftime("%Y-%m")] += 1
            dates.append(when)
        if row.get("Selling TP"):
            lane["plants_from"][str(row["Selling TP"])] += 1
        if row.get("Purchasing Plant"):
            lane["plants_to"][str(row["Purchasing Plant"])] += 1

    return {
        "lanes": lanes,
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "lines": len(records),
    }


def map_lane(origin: str, destination: str, links: tuple) -> dict:
    """Which modelled nodes this lane runs between, if any.

    Returns the mapping AND why it failed, because "we cannot route Colombia"
    is a finding about the network's coverage and belongs on screen, not in a
    silently shorter list.
    """
    origins, destinations = links
    from_nodes = origins.get(origin, [])
    to_nodes = destinations.get(destination, [])
    if from_nodes and to_nodes:
        return {"mapped": True, "from": from_nodes, "to": to_nodes, "why": ""}
    missing = []
    if not from_nodes:
        missing.append(f"no modelled origin in {origin}")
    if not to_nodes:
        missing.append(f"no modelled destination in {destination}")
    return {"mapped": False, "from": from_nodes, "to": to_nodes,
            "why": "; ".join(missing)}


def render(summary: dict, links: tuple) -> tuple[str, dict]:
    lanes = summary["lanes"]
    ranked = sorted(
        lanes.items(), key=lambda kv: len(kv[1]["docs"]), reverse=True
    )

    mapped = unmapped = 0
    mapped_docs = unmapped_docs = 0
    out = [
        "# Sika intercompany flows — DERIVED FROM THE CUSTOMER'S EXPORT",
        "#",
        "# Written by scripts/import_sika_flows.py. Do not edit by hand; edit",
        "# the source export and re-run. This file is gitignored: it is real",
        "# commercial data about who supplies whom and how often.",
        "#",
        f"# Source covers {summary['first'].date()} to {summary['last'].date()}"
        if summary["first"] else "#",
        f"# {summary['lines']:,} line items across {len(lanes)} lanes.",
        "#",
        "# WHAT IS NOT IN HERE, because it is not in the export: consignment",
        "# value, promised delivery date, mode and carrier. The radar's",
        "# scoring needs all four, so they stay declared in lanes.yaml and the",
        "# inputs panel reports them as assumptions rather than as Sika data.",
        "",
        "flows:",
    ]

    for (origin, destination), lane in ranked:
        link = map_lane(origin, destination, links)
        docs = len(lane["docs"])
        if link["mapped"]:
            mapped += 1
            mapped_docs += docs
        else:
            unmapped += 1
            unmapped_docs += docs
        months = lane["months"]
        busiest = months.most_common(1)[0] if months else ("", 0)
        out += [
            f"  - lane: {origin}_{destination}",
            f"    origin_country: {origin}",
            f"    destination_country: {destination}",
            f"    documents: {docs}",
            f"    line_items: {lane['lines']}",
            f"    net_weight_kg: {round(lane['kg'], 1)}",
            f"    busiest_month: {busiest[0]}  # {busiest[1]} documents",
            f"    mapped: {str(link['mapped']).lower()}",
        ]
        if link["mapped"]:
            out += [
                f"    from_nodes: [{', '.join(link['from'])}]",
                f"    to_nodes: [{', '.join(link['to'])}]",
            ]
        else:
            out.append(f"    unmapped_because: {link['why']}")
        out.append(
            "    monthly: {"
            + ", ".join(f"{m}: {n}" for m, n in sorted(months.items()))
            + "}"
        )
        out.append("")

    stats = {
        "lanes": len(lanes), "mapped": mapped, "unmapped": unmapped,
        "mapped_docs": mapped_docs, "unmapped_docs": unmapped_docs,
    }
    return "\n".join(out) + "\n", stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path,
                        help="the .xlsx Sika supplied (keep it under config/)")
    parser.add_argument("--out", type=Path, default=ROOT / "config" / "flows.yaml")
    args = parser.parse_args()

    if not args.export.exists():
        raise SystemExit(f"no such file: {args.export}")

    if "config" not in args.export.parts:
        print("NOTE: the export is not under config/, which is the gitignored")
        print("      path for the customer's real data. Move it there before")
        print("      committing anything, or it will land in a public history.")
        print()

    records = read(args.export)
    summary = summarise(records)
    config = load_config()
    links = gateways(config)
    text, stats = render(summary, links)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)

    print(f"read    : {summary['lines']:,} line items")
    if summary["first"]:
        print(f"covering: {summary['first'].date()} to {summary['last'].date()}")
    print(f"lanes   : {stats['lanes']}")
    print(f"wrote   : {args.out.relative_to(ROOT)}")
    print()
    print("COVERAGE against the modelled network")
    print(f"  mapped   : {stats['mapped']:3} lane(s), "
          f"{stats['mapped_docs']:,} document(s)")
    print(f"  unmapped : {stats['unmapped']:3} lane(s), "
          f"{stats['unmapped_docs']:,} document(s)")
    share = stats["mapped_docs"] / max(1, stats["mapped_docs"] + stats["unmapped_docs"])
    print(f"  the network can route {share:.0%} of the real order volume")
    print()
    print("Nothing was invented to close the gap: a lane whose endpoints the")
    print("network cannot represent is listed as unmapped, with the reason.")
    print("Adding a node for one of those countries is a network.yaml edit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
