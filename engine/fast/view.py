"""The payload the fast dashboard reads.

The old board sent everything: seventeen lanes, a radar chart per lane, a risk
matrix per event, per-vehicle grids, the funnel's working, and an action panel
with twelve rows. All of it true, none of it answering the question a planner
opens the screen with, which is:

    what needs me first, and what do I press?

So this view is shaped like that question. One headline — the single most
urgent lane, with the fastest surviving option already chosen. A queue behind
it, one line each. Everything else is available per route and not before.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
    the risk matrix        a four-by-four grid of probability against impact
                           is a portfolio instrument. It cannot be read in the
                           six minutes a planner has, and it never told them
                           what to press.
    the radar chart        same objection, less information.
    per-vehicle grids      twenty-eight icons is a picture of the problem, not
                           a decision. The count and the deadline carry what
                           the icons carried; the detail stays one click away.
    expected loss          the number the old build optimised for. Still
                           computed, still used by the margin veto, no longer
                           the thing on screen — because it is not what the
                           planner is being asked to fix.

ROUTE-LEVEL OPTIONS
-------------------
An option is computed per consignment, because feasibility is per consignment:
one barge past Kaub has three days of slack and the next has none. But a
planner does not act on one consignment, they act on a lane. So identical
options are GROUPED across the consignments they apply to, with the count, the
total cost, and the WORST deadline among them — worst, because the group moves
together and the tightest member is the one that decides when it has to go.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from engine.act import playbook
from engine.clock import Clock
from engine.config import Config
from engine.fast import options as fast
from engine.fast.execute import LEDGER, Ledger
from engine.network.graph import Network
from engine.pipeline import RunContext
from engine.schemas import GateHit, Shipment

# How many lanes go in the queue behind the headline. Past this it is a
# database view, not a dashboard.
QUEUE_LIMIT = 6

# How many grouped options are offered per route. Three is a choice; ten is a
# menu, and a menu is what this rebuild exists to remove.
OPTION_LIMIT = 3

URGENCY_RANK = {"now": 0, "today": 1, "soon": 2, "scheduled": 3, "unknown": 4}


@dataclass
class _Grouped:
    """The same option across several consignments on one lane."""

    key: tuple[str, str]
    example: fast.FastOption
    shipment_ids: set[str]
    cost_chf: float
    worst_days_late: float
    worst_hours_to_resolve: float
    on_time_count: int

    def as_dict(self) -> dict:
        payload = self.example.as_dict()
        payload.update(
            {
                "option_id": f"{self.key[0]}:{self.key[1]}",
                "shipment_ids": sorted(self.shipment_ids),
                "shipments": len(self.shipment_ids),
                "cost_chf": round(self.cost_chf, 2),
                "days_late_after": round(self.worst_days_late, 2),
                "hours_to_resolve": round(self.worst_hours_to_resolve, 1),
                "on_time": self.on_time_count == len(self.shipment_ids),
                "on_time_shipments": self.on_time_count,
            }
        )
        return payload


def _hits_for(context: RunContext, shipment_id: str) -> list[GateHit]:
    return [h for h in context.hits if h.shipment_id == shipment_id]


def _rank_shipment(
    shipment: Shipment,
    context: RunContext,
    risk,
    event,
) -> fast.Ranking:
    hits = [
        h for h in context.hits
        if h.shipment_id == shipment.shipment_id and h.event_id == event.event_id
    ]
    lead = risk.lead_time_hours if risk.lead_time_hours is not None else 0.0
    templates = playbook.options_for(
        shipment, event, hits, context.config, context.network, lead
    )
    return fast.for_shipment(
        shipment, hits, templates, context.config, context.network, context.clock,
        baseline_delay_days=risk.do_nothing.expected_delay_days,
        hours_until_impact=lead,
    )


def _group(rankings: list[fast.Ranking]) -> list[_Grouped]:
    buckets: dict[tuple[str, str], _Grouped] = {}
    for ranking in rankings:
        for option in ranking.viable:
            key = (option.kind, option.label)
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = _Grouped(
                    key=key,
                    example=option,
                    shipment_ids={option.shipment_id},
                    cost_chf=option.cost_chf,
                    worst_days_late=option.days_late_after,
                    worst_hours_to_resolve=option.hours_to_resolve,
                    on_time_count=1 if option.on_time else 0,
                )
                continue
            already = option.shipment_id in bucket.shipment_ids
            bucket.shipment_ids.add(option.shipment_id)
            if not already:
                # A consignment hit by two events is still one consignment, and
                # charging it twice would inflate the cost the veto reads.
                bucket.cost_chf += option.cost_chf
                bucket.on_time_count += 1 if option.on_time else 0
            bucket.worst_days_late = max(
                bucket.worst_days_late, option.days_late_after
            )
            bucket.worst_hours_to_resolve = max(
                bucket.worst_hours_to_resolve, option.hours_to_resolve
            )

    grouped = list(buckets.values())
    grouped.sort(
        key=lambda g: (
            g.on_time_count < len(g.shipment_ids),
            not g.example.restores_delivery,
            not g.example.executable,
            round(g.worst_days_late, 3),
            round(g.worst_hours_to_resolve, 2),
            -len(g.shipment_ids),
        )
    )
    return grouped


def _vetoed_rows(rankings: list[fast.Ranking], limit: int = 4) -> list[dict]:
    """The ones that would have lost money, said once rather than per consignment."""
    seen: dict[str, dict] = {}
    for ranking in rankings:
        for option, why in ranking.vetoed:
            if option.label in seen:
                seen[option.label]["shipments"] += 1
                continue
            seen[option.label] = {
                "label": option.label,
                "kind": option.kind,
                "hours_to_resolve": round(option.hours_to_resolve, 1),
                "cost_chf": round(option.cost_chf, 2),
                "vetoed_because": why,
                "shipments": 1,
            }
    return list(seen.values())[:limit]


def _delay_days(rankings: list[fast.Ranking], risks: list) -> float:
    """The delay the lane is carrying right now if nobody acts."""
    if not risks:
        return 0.0
    return round(max(r.do_nothing.expected_delay_days for r in risks), 2)


def route_summaries(context: RunContext, ledger: Ledger | None = None) -> list[dict]:
    """One row per affected lane: delay, deadline, and what to press.

    Unaffected lanes are not here at all. A dashboard listing seventeen lanes
    so that eleven of them can say "fine" is the clutter being removed; the
    count of quiet lanes is carried in ``counts`` instead, which is the part of
    that information anybody actually uses.
    """
    ledger = ledger if ledger is not None else LEDGER
    config = context.config
    by_lane: dict[str, list[tuple[Shipment, object, object]]] = defaultdict(list)
    ships = {s.shipment_id: s for s in context.shipments}

    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            shipment = ships.get(risk.shipment_id)
            if shipment is not None:
                by_lane[shipment.lane_id].append((shipment, risk, assessment.event))

    lanes = {lane["id"]: lane for lane in config.lanes}
    out: list[dict] = []

    for lane_id, rows in by_lane.items():
        lane = lanes.get(lane_id)
        if lane is None:
            continue

        rankings = [_rank_shipment(s, context, r, e) for s, r, e in rows]
        risks = [r for _, r, _ in rows]
        grouped = _group(rankings)

        leads = [
            r.lead_time_hours for _, r, _ in rows if r.lead_time_hours is not None
        ]
        hours_left = min(leads) if leads else None
        causes = sorted({e.title for _, _, e in rows})
        at_risk = len({s.shipment_id for s, _, _ in rows})
        total = sum(1 for s in context.shipments if s.lane_id == lane_id)

        best = grouped[0] if grouped else None
        executed = [
            e.as_dict(context.clock.as_of)
            for e in ledger.recent(50)
            if e.shipment_id in {s.shipment_id for s, _, _ in rows}
        ]

        out.append(
            {
                "route_id": lane_id,
                "name": lane["name"],
                "focus": lane.get("focus", ""),
                "urgency": fast.urgency_band(config, hours_left),
                "hours_left": round(hours_left, 1) if hours_left is not None else None,
                "delay_days": _delay_days(rankings, risks),
                "shipments_at_risk": at_risk,
                "shipments_total": total,
                "cause": causes[0] if causes else "",
                "causes": causes,
                "customers": sorted({r.customer for r in risks}),
                # The one thing to press, already chosen.
                "best": best.as_dict() if best else None,
                "options": [g.as_dict() for g in grouped[:OPTION_LIMIT]],
                "options_total": len(grouped),
                "vetoed": _vetoed_rows(rankings),
                "expired": len([o for r in rankings for o in r.expired]),
                "executed": executed,
                        # "can hold the date" means an option exists that both lands
                # on time AND actually moves the freight. Without the second
                # half, a lane with enough slack to absorb the delay reports
                # that it is fine because somebody could ring the customer.
                "can_hold_the_date": bool(
                    best and best.example.restores_delivery
                    and best.on_time_count == len(best.shipment_ids)
                ),
            }
        )

    out.sort(
        key=lambda r: (
            URGENCY_RANK.get(r["urgency"], 9),
            r["hours_left"] if r["hours_left"] is not None else 1e9,
            -r["shipments_at_risk"],
        )
    )
    return out


def build(
    context: RunContext,
    incidents: list[dict] | None = None,
    ledger: Ledger | None = None,
) -> dict:
    """The whole fast-dashboard payload. One headline, a short queue, nothing else."""
    summaries = route_summaries(context, ledger=ledger)
    headline = summaries[0] if summaries else None
    queue = summaries[1:1 + QUEUE_LIMIT]

    lanes_total = len(context.config.lanes)
    holding = sum(1 for r in summaries if r["can_hold_the_date"])

    return {
        "as_of": context.result.as_of.isoformat(),
        "as_of_label": str(context.clock),
        "quiet": not summaries,
        "headline": headline,
        "queue": queue,
        "more": max(0, len(summaries) - 1 - QUEUE_LIMIT),
        "incidents": incidents or [],
        "counts": {
            "lanes_affected": len(summaries),
            "lanes_total": lanes_total,
            "lanes_quiet": max(0, lanes_total - len(summaries)),
            "shipments_at_risk": sum(r["shipments_at_risk"] for r in summaries),
            "shipments_total": context.result.shipments_total,
            "act_now": sum(1 for r in summaries if r["urgency"] == "now"),
            "can_hold_the_date": holding,
        },
        # One sentence for the top of the screen. A dashboard that cannot say
        # "nothing is wrong" is a dashboard nobody trusts when it says
        # something is.
        "sentence": _sentence(summaries, holding),
    }


def _sentence(summaries: list[dict], holding: int) -> str:
    if not summaries:
        return "Nothing needs a decision. Every lane is running to plan."

    urgent = [r for r in summaries if r["urgency"] in ("now", "today")]
    if not urgent:
        return (
            f"{len(summaries)} lane(s) are off plan, none of them urgent. "
            f"{holding} can still be delivered on the agreed date."
        )

    first = urgent[0]
    hours = first["hours_left"]
    if hours is None:
        when = "with no deadline computed"
    elif hours < 1:
        # "inside 0 h" reads like a rounding error rather than an emergency.
        when = "now — the first option has already expired"
    else:
        when = f"inside {hours:.0f} h"

    missing = len(summaries) - holding
    tail = (
        f"{holding} of {len(summaries)} can still be delivered on the agreed date."
        if missing == 0
        else (
            f"{holding} of {len(summaries)} can still make the date; "
            f"{missing} will need the customer told."
        )
    )
    return f"{len(urgent)} lane(s) need a decision {when}. {tail}"


def route_detail(
    context: RunContext,
    route_id: str,
    ledger: Ledger | None = None,
) -> dict | None:
    for row in route_summaries(context, ledger=ledger):
        if row["route_id"] == route_id:
            return row
    return None


def options_for_shipment(context: RunContext, shipment_id: str) -> dict | None:
    """Every option for ONE consignment, ranked.

    The lane view groups options across the consignments they apply to, which
    is right for "what do I do about this lane" and wrong for "this one
    customer is on the phone". A consignment three days from its committed
    date and the one behind it with a week of slack get the same grouped
    answer, and only one of them deserves it.
    """
    shipment = next(
        (s for s in context.shipments if s.shipment_id == shipment_id), None
    )
    if shipment is None:
        return None

    rankings: list[fast.Ranking] = []
    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            if risk.shipment_id != shipment_id:
                continue
            rankings.append(
                _rank_shipment(shipment, context, risk, assessment.event)
            )

    if not rankings:
        return {
            "shipment_id": shipment_id,
            "customer": shipment.customer,
            "value_chf": round(shipment.value_chf, 2),
            "committed": shipment.otif_committed_date.isoformat(),
            "options": [], "vetoed": [], "expired": [],
            "sentence": "Nothing is touching this consignment.",
        }

    # One shipment hit by two events yields two rankings. Merge on option id
    # and keep the worst case, because a consignment is only as safe as its
    # tightest constraint.
    merged: dict[str, fast.FastOption] = {}
    vetoed: dict[str, tuple] = {}
    for ranking in rankings:
        for option in ranking.viable:
            keep = merged.get(option.option_id)
            if keep is None or option.rank_key > keep.rank_key:
                merged[option.option_id] = option
        for option, why in ranking.vetoed:
            vetoed.setdefault(option.option_id, (option, why))

    ordered = sorted(merged.values(), key=lambda o: o.rank_key)
    best = ordered[0] if ordered else None

    return {
        "shipment_id": shipment_id,
        "customer": shipment.customer,
        "value_chf": round(shipment.value_chf, 2),
        "committed": shipment.otif_committed_date.isoformat(),
        "options": [o.as_dict() for o in ordered],
        "vetoed": [
            {**o.as_dict(), "vetoed_because": why} for o, why in vetoed.values()
        ],
        "expired": [
            o.as_dict() for r in rankings for o in r.expired
        ][:4],
        "sentence": (
            f"{best.label} — resolved in {best.hours_to_resolve:.0f} h, "
            + ("arrives on the agreed date." if best.on_time
               else f"still {best.days_late_after:.1f} day(s) late.")
            if best else
            "Nothing for this consignment both holds the date and pays for "
            "itself. Tell the customer and re-agree."
        ),
    }


def option_by_id(
    context: RunContext,
    route_id: str,
    option_id: str,
) -> list[fast.FastOption]:
    """Re-derive the concrete per-consignment options behind a grouped id.

    The dashboard sends back the GROUP id, because that is what the planner
    clicked. Execution happens per consignment, so the group is expanded here
    rather than trusting a list of ids round-tripped through the browser —
    a client that sent a stale or edited list would otherwise be executing
    against consignments the engine never offered.
    """
    ships = {s.shipment_id: s for s in context.shipments}
    # Keyed, not appended. A consignment hit by two events is assessed twice
    # and yields the same option twice; executing it twice books the same
    # reroute twice and leaves the second execution un-undoable, because the
    # two share an id.
    found: dict[str, fast.FastOption] = {}

    for assessment in context.result.assessments:
        for risk in assessment.shipment_risks:
            shipment = ships.get(risk.shipment_id)
            if shipment is None or shipment.lane_id != route_id:
                continue
            ranking = _rank_shipment(shipment, context, risk, assessment.event)
            for option in ranking.viable:
                if f"{option.kind}:{option.label}" == option_id:
                    found.setdefault(option.option_id, option)

    return list(found.values())


def network_of(context: RunContext) -> Network:
    return context.network


def config_of(context: RunContext) -> Config:
    return context.config


def clock_of(context: RunContext) -> Clock:
    return context.clock
