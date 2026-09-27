"""Carrier push-outs — the signal before the crisis.

Sika, describing how disruptions actually reach the desk:

    "Disruptions are often identified by carriers pushing out single orders
     before an official crisis is announced."

A carrier moving one booking later is noise. The same carrier moving several
orders at the same place within a few days is a carrier pulling capacity back
— and it shows days before any authority, news desk or feed says why. So this
module reads booking changes, finds the patterns, and turns each one into an
observation of CAP_CARRIER_PUSHOUT: a warning sign (onset: precursor), with no
probability invented, that the gate and the Monte Carlo treat like any other.

WHAT COUNTS
-----------
A push-out is a booking the CARRIER moved later: a rolled container, a
cancelled train path, a postponed barge departure. Not one the planner moved.
It counts when it is at least ``min_hours`` late; a pattern is ``min_orders``
distinct orders by one carrier at one place within ``window_days`` before the
as-of (desk.yaml → carrier_pushouts). Fewer than that are listed as "seen",
never raised: the planner can watch one become a pattern.

WHERE THE CHANGES COME FROM
---------------------------
``config/carrier_notices.csv`` when it exists (gitignored — carriers' booking
data is Sika's), with the columns

    order_id, carrier, node, noticed_at, planned_departure, new_departure, reason

where ``node`` is a network node id and times are ISO 8601. Carriers send
these as booking confirmations and status messages (EDIFACT IFTMBC / IFTSTA,
or a portal export); this reads the export, it does not connect to anything.

Without that file, the changes are generated from the SYNTHETIC shipment book
and labelled so — one carrier pulling back at one place, plus scattered single
moves — because a signal nobody can see is a signal nobody believes exists.
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from engine.clock import Clock
from engine.config import CUSTOMER_DIR, Config
from engine.ingest.observations import FeedReport, FeedStatus
from engine.schemas import Shipment

NOTICES_FILE = "carrier_notices.csv"
VARIABLE = "CAP_CARRIER_PUSHOUT"


@dataclass
class Notice:
    order_id: str
    carrier: str
    node: str
    noticed_at: datetime
    planned_departure: datetime
    new_departure: datetime
    reason: str = ""
    synthetic: bool = False

    @property
    def hours(self) -> float:
        return (self.new_departure - self.planned_departure).total_seconds() / 3600.0

    def as_dict(self, config: Config) -> dict:
        node = config.nodes.get(self.node)
        return {
            "order_id": self.order_id,
            "carrier": self.carrier,
            "carrier_name": carrier_name(config, self.carrier),
            "node": self.node,
            "node_name": node.name if node else self.node,
            "noticed_at": self.noticed_at.isoformat(),
            "planned_departure": self.planned_departure.isoformat(),
            "new_departure": self.new_departure.isoformat(),
            "hours": round(self.hours, 1),
            "reason": self.reason,
            "synthetic": self.synthetic,
        }


def settings(config: Config) -> dict:
    desk = config.files.get("desk")
    raw = ((desk.data or {}) if desk else {}).get("carrier_pushouts") or {}
    return {
        "window_days": float(raw.get("window_days", 7)),
        "min_orders": int(raw.get("min_orders", 3)),
        "min_hours": float(raw.get("min_hours", 12)),
    }


def carrier_name(config: Config, carrier_id: str) -> str:
    for carrier in config.contacts.get("carriers", []):
        if carrier.get("id") == carrier_id:
            return carrier.get("name", carrier_id)
    return carrier_id


# ---------------------------------------------------------------------
# Reading the changes
# ---------------------------------------------------------------------
def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def read_file(path: Path) -> list[Notice]:
    out: list[Notice] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                out.append(Notice(
                    order_id=row["order_id"].strip(),
                    carrier=row["carrier"].strip(),
                    node=row["node"].strip(),
                    noticed_at=_parse(row["noticed_at"]),
                    planned_departure=_parse(row["planned_departure"]),
                    new_departure=_parse(row["new_departure"]),
                    reason=(row.get("reason") or "").strip(),
                ))
            except (KeyError, ValueError, AttributeError, TypeError):
                # A malformed row is skipped and counted by the caller, not
                # allowed to take the whole signal down.
                continue
    return out


def synthetic_notices(shipments: list[Shipment], config: Config, clock: Clock) -> list[Notice]:
    """Booking changes consistent with the synthetic book.

    One carrier pulls back at one transfer place: several of its departures
    there in the coming fortnight are moved one to four days. A few other
    bookings are moved once each, which is what normal looks like. Seeded
    off the as-of day, so a given board always shows the same changes.
    """
    rng = random.Random(f"pushouts:{clock.as_of:%Y%m%d}")
    lo, hi = clock.as_of - timedelta(days=2), clock.as_of + timedelta(days=12)

    candidates: dict[tuple[str, str], list[tuple[Shipment, int]]] = defaultdict(list)
    for shipment in shipments:
        for index, leg in enumerate(shipment.legs):
            node = config.nodes.get(leg.from_node)
            # Bookings leave from ports and terminals; a leg that "starts" at
            # a canal or a gauge is the route's geometry, not a departure.
            if node is None or node.kind.value not in ("seaport", "inland_port"):
                continue
            if lo <= leg.planned_depart <= hi:
                candidates[(leg.carrier, leg.from_node)].append((shipment, index))

    ranked = sorted(candidates.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ranked = [kv for kv in ranked if len(kv[1]) >= 3][:3]
    notices: list[Notice] = []
    used: set[str] = set()
    chosen: tuple[str, str] | None = None
    if ranked:
        chosen, orders = ranked[rng.randrange(len(ranked))]
        carrier, node_id = chosen
        rng.shuffle(orders)
        for shipment, index in orders[: min(len(orders), rng.randint(3, 5))]:
            leg = shipment.legs[index]
            notices.append(Notice(
                order_id=shipment.shipment_id, carrier=carrier, node=node_id,
                noticed_at=clock.as_of - timedelta(hours=rng.uniform(4, 5 * 24)),
                planned_departure=leg.planned_depart,
                new_departure=leg.planned_depart + timedelta(hours=rng.choice([24, 48, 72, 96])),
                reason=rng.choice(["rolled to next departure", "capacity withdrawn",
                                   "equipment shortage", "space not confirmed"]),
                synthetic=True,
            ))
            used.add(shipment.shipment_id)

    # Scattered singles: one move each, different carriers or places.
    loose = [(key, s, i) for key, orders in candidates.items() for s, i in orders
             if s.shipment_id not in used and key != chosen]
    rng.shuffle(loose)
    seen_keys: set[tuple[str, str]] = set()
    for (carrier, node_id), shipment, index in loose:
        if (carrier, node_id) in seen_keys or len(seen_keys) >= 3:
            continue
        seen_keys.add((carrier, node_id))
        leg = shipment.legs[index]
        notices.append(Notice(
            order_id=shipment.shipment_id, carrier=carrier, node=node_id,
            noticed_at=clock.as_of - timedelta(hours=rng.uniform(2, 6 * 24)),
            planned_departure=leg.planned_depart,
            new_departure=leg.planned_depart + timedelta(hours=rng.choice([14, 20, 30])),
            reason="rolled to next departure", synthetic=True,
        ))
    return notices


# ---------------------------------------------------------------------
# Finding the patterns
# ---------------------------------------------------------------------
def detect(notices: list[Notice], config: Config, clock: Clock) -> tuple[list[dict], list[dict]]:
    """(patterns, singles). A pattern is enough distinct orders by one carrier
    at one place, inside the window; everything else that counts is a single."""
    rule = settings(config)
    since = clock.as_of - timedelta(days=rule["window_days"])
    counted = [n for n in notices
               if since <= n.noticed_at <= clock.as_of and n.hours >= rule["min_hours"]]

    groups: dict[tuple[str, str], dict[str, Notice]] = defaultdict(dict)
    for notice in counted:
        # Distinct ORDERS: a carrier moving the same order twice is one order.
        groups[(notice.carrier, notice.node)][notice.order_id] = notice

    patterns: list[dict] = []
    singles: list[dict] = []
    for (carrier, node_id), by_order in sorted(groups.items()):
        items = sorted(by_order.values(), key=lambda n: n.noticed_at)
        entry = {
            "carrier": carrier,
            "carrier_name": carrier_name(config, carrier),
            "node": node_id,
            "node_name": config.nodes[node_id].name if node_id in config.nodes else node_id,
            "orders": len(items),
            "first_noticed": items[0].noticed_at.isoformat(),
            "last_noticed": items[-1].noticed_at.isoformat(),
            "mean_hours": round(sum(n.hours for n in items) / len(items), 1),
            "notices": [n.as_dict(config) for n in items],
            "synthetic": all(n.synthetic for n in items),
        }
        (patterns if len(items) >= rule["min_orders"] else singles).append(entry)
    patterns.sort(key=lambda p: -p["orders"])
    return patterns, singles


def _severity(orders: int, minimum: int) -> str:
    if orders >= 3 * minimum:
        return "severe"
    if orders >= 2 * minimum:
        return "moderate"
    return "minor"


def observation(pattern: dict, config: Config, clock: Clock) -> dict:
    """One pattern as an observation of CAP_CARRIER_PUSHOUT.

    A warning, not a disruption: nobody has said anything is wrong, so no
    probability is invented and every figure it produces is "if it
    happens". It starts now — the carrier is already moving orders — and
    lasts the variable's typical duration, because a carrier pulling back
    does not say for how long.
    """
    rule = settings(config)
    var = config.variables[VARIABLE]
    modes = sorted({leg["mode"] for lane in config.lanes for leg in lane["legs"]
                    if leg["from"] == pattern["node"]} & set(_carrier_modes(config, pattern["carrier"])))
    quote = "; ".join(
        f"{n['order_id']}: {n['planned_departure'][:16].replace('T', ' ')} → "
        f"{n['new_departure'][:16].replace('T', ' ')} ({n['reason'] or 'moved'})"
        for n in pattern["notices"][:6])
    days = rule["window_days"]
    return {
        "observation_id": f"OBS-PUSHOUT-{pattern['carrier']}-{pattern['node']}-{clock.as_of:%Y%m%d}",
        "node_ids": [pattern["node"]],
        "variable_id": VARIABLE,
        "severity": _severity(pattern["orders"], rule["min_orders"]),
        "starts_at": clock.as_of,
        "ends_at": clock.as_of + timedelta(days=var.typical_duration_days or 7),
        "duration_confidence": "estimated",
        "realized": False,
        "probability": None,
        "probability_basis": (
            f"a warning sign: {pattern['orders']} orders pushed out by one carrier at "
            f"{pattern['node_name']} in {days:.0f} days, and no announcement — whether it "
            "becomes a disruption is not known, so every figure here is IF it does"
        ),
        "modes": modes or None,
        "verbatim_quote": quote,
        "title": (
            f"{pattern['carrier_name']} pushed out {pattern['orders']} orders at "
            f"{pattern['node_name']} in {days:.0f} days — no official notice yet"
        ),
        "source": "carrier booking changes" + (" (synthetic)" if pattern["synthetic"] else ""),
        "source_tier": 2,
        "cost_multiplier": 1.0,
    }


def _carrier_modes(config: Config, carrier_id: str) -> list[str]:
    for carrier in config.contacts.get("carriers", []):
        if carrier.get("id") == carrier_id:
            return list(carrier.get("modes") or [])
    return ["sea", "barge", "rail", "road"]


def assess(shipments: list[Shipment], config: Config, clock: Clock,
           customer_dir: Path | None = None) -> tuple[list[dict], FeedReport, dict]:
    """Read the changes, find the patterns: (observations, report, summary)."""
    path = (customer_dir or CUSTOMER_DIR) / NOTICES_FILE
    if VARIABLE not in config.variables:
        report = FeedReport(
            key="carrier_pushouts", label="Carrier booking changes (push-outs)",
            status=FeedStatus.ABSENT,
            detail=f"{VARIABLE} is not in this ledger, so push-outs are not read",
            unlocks_if_connected="The earliest signal Sika named: carriers moving orders.",
            retrieved_at=clock.as_of,
        )
        return [], report, {"patterns": [], "singles": [], "synthetic": True}

    if path.exists():
        notices = read_file(path)
        status, detail = FeedStatus.CONNECTED, f"{len(notices)} booking change(s) from {path.name}"
    else:
        notices = synthetic_notices(shipments, config, clock)
        status = FeedStatus.FIXTURE
        detail = (f"{len(notices)} SYNTHETIC booking change(s), generated from the synthetic "
                  "book — put a carrier export at config/carrier_notices.csv to read real ones")
    patterns, singles = detect(notices, config, clock)
    report = FeedReport(
        key="carrier_pushouts",
        label="Carrier booking changes (push-outs)",
        status=status,
        detail=f"{detail}; {len(patterns)} pattern(s) raised, {len(singles)} single(s) watched",
        unlocks_if_connected=(
            "Carriers' own booking changes (IFTMBC / IFTSTA or a portal export): "
            "the earliest sign of a disruption, days before it is announced."
        ),
        records=len(notices),
        retrieved_at=clock.as_of,
        source_tier=2,
    )
    rule = settings(config)
    summary = {
        "patterns": patterns,
        "singles": singles,
        "synthetic": status is FeedStatus.FIXTURE,
        "rule": (f"{rule['min_orders']} or more orders moved at least {rule['min_hours']:.0f} h "
                 f"by one carrier at one place within {rule['window_days']:.0f} days"),
    }
    return [observation(p, config, clock) for p in patterns], report, summary
