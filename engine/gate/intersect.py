"""The gate — the noise filter, zero parameters (BRIEF §5.1).

Three conditions, all of which must hold:

    1. SPATIAL   the affected node or segment is on this shipment's route
    2. TEMPORAL  the event window overlaps the shipment's transit through it
    3. MODAL     the event applies to that leg's mode

This is the most defensible thing in the system because there is nothing to
tune. Either the event is on a lane you use while you are on it, or it is not.

    "We don't filter by what's newsworthy. We filter by what's on your lanes."

TEMPORAL IS THE ONE EVERYONE FORGETS. A strike that ends before your vessel
arrives is not your problem, and roughly half the false positives die on that
line alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from engine.clock import Clock, overlaps
from engine.config import Config
from engine.network.geo import PORT_CATCHMENT_KM, Point
from engine.network.graph import Network
from engine.schemas import Event, GateHit, Mode, Shipment
from engine.variables.mask import mode_applies

# How long a shipment is considered present at a transfer node. A strike at
# Rotterdam hits you if you are calling there, not only during the instant of
# arrival.
NODE_DWELL_HOURS = 24.0

# An unknown event end is widened rather than treated as instantaneous.
# Under-merging costs a row a planner dismisses; missing a live disruption
# costs the thing the tool exists to prevent.
#
# THIS IS ONLY THE FLOOR. The real figure comes from the ledger, because the
# ledger already knows: a haulier strike is declared as lasting days and a
# closed strait as lasting months, and treating both as three days is how a
# strait closure silently expires before any ship reaches it. That was a real
# bug — the Hormuz scenario produced an event, matched it to a lane, and then
# gated it out because the freight arrived three weeks after the event was
# assumed to have ended.
UNKNOWN_DURATION_FALLBACK_DAYS = 3.0

# A ceiling, so a mis-keyed ledger entry cannot widen one event across the
# whole book. 120 days is longer than any variable in the shipped ledger.
MAX_FALLBACK_DAYS = 120.0


@dataclass
class GateStats:
    """Funnel counters, measured rather than asserted (BRIEF §3.2)."""

    events_in: int = 0
    pairs_considered: int = 0
    failed_spatial: int = 0
    failed_temporal: int = 0
    failed_modal: int = 0
    hits: int = 0

    @property
    def survival_rate(self) -> float:
        if not self.pairs_considered:
            return 0.0
        return self.hits / self.pairs_considered


def gate(
    events: list[Event],
    shipments: list[Shipment],
    network: Network,
    config: Config,
    clock: Clock,
) -> tuple[list[GateHit], GateStats]:
    """Intersect every event against every shipment leg.

    Ordering matters for cost: the cheapest test runs first. Modal is a set
    membership check, temporal is two comparisons, spatial is the only one that
    touches geometry.
    """
    variables = config.variables
    stats = GateStats(events_in=len(events))
    hits: list[GateHit] = []

    for event in events:
        ev_start, ev_end = event.window(_fallback_days(event, variables))
        event_point = (
            Point(lat=event.lat, lon=event.lon)
            if event.lat is not None and event.lon is not None
            else None
        )
        event_nodes = set(event.node_ids) | set(event.second_order_nodes)

        for shipment in shipments:
            for index, leg in enumerate(shipment.legs):
                stats.pairs_considered += 1

                # --- 3. MODAL (cheapest) ------------------------------
                modal = _modal_check(event, leg.mode, variables)
                if not modal[0]:
                    stats.failed_modal += 1
                    continue

                # --- 2. TEMPORAL --------------------------------------
                enters = leg.planned_depart
                leaves = leg.planned_arrive + timedelta(hours=NODE_DWELL_HOURS)
                if not overlaps(ev_start, ev_end, enters, leaves):
                    stats.failed_temporal += 1
                    continue

                # --- 1. SPATIAL ---------------------------------------
                spatial = _spatial_check(
                    event_nodes, event_point, leg, network
                )
                if not spatial[0]:
                    stats.failed_spatial += 1
                    continue

                stats.hits += 1
                hits.append(
                    GateHit(
                        event_id=event.event_id,
                        shipment_id=shipment.shipment_id,
                        leg_index=index,
                        node_id=spatial[2],
                        mode=leg.mode,
                        spatial_reason=spatial[1],
                        temporal_reason=(
                            f"leg occupies {enters:%d %b %H:%M}–{leaves:%d %b %H:%M} UTC; "
                            f"event window {ev_start:%d %b %H:%M}–{ev_end:%d %b %H:%M} UTC"
                        ),
                        modal_reason=modal[1],
                        leg_enters_at=enters,
                        leg_leaves_at=leaves,
                    )
                )

    return hits, stats


def _modal_check(event: Event, mode: Mode, variables: dict) -> tuple[bool, str]:
    """Does any active variable on this event apply to this leg's mode?"""
    if mode in event.modes_affected:
        reasons = []
        for vid in event.active_variables:
            var = variables.get(vid)
            if var is None:
                continue
            verdict = mode_applies(var, mode)
            if verdict.exposed:
                reasons.append(verdict.reason)
        if reasons:
            return True, reasons[0]
        return True, f"event affects {mode.value} legs"
    affected = ", ".join(m.value for m in event.modes_affected) or "nothing"
    return False, f"event affects {affected}; this leg is {mode.value}"


def _spatial_check(
    event_nodes: set[str],
    event_point: Point | None,
    leg,
    network: Network,
) -> tuple[bool, str, str]:
    """Is the event on this leg? Returns (hit, reason, node_id)."""
    # Named-node match is exact and needs no geometry.
    for node_id in (leg.from_node, leg.to_node):
        if node_id in event_nodes:
            node = network.node(node_id)
            return True, f"event names {node.name}, a node on this leg", node_id

    if event_point is None:
        return False, "event has no coordinates and names no node on this leg", ""

    # Near either endpoint — an event 20km from Rotterdam is a Rotterdam event.
    for node_id in (leg.from_node, leg.to_node):
        node = network.node(node_id)
        distance = _distance(event_point, node)
        if distance <= PORT_CATCHMENT_KM:
            return (
                True,
                f"event is {distance:.0f} km from {node.name}, within its catchment",
                node_id,
            )

    # On the corridor between them.
    geometry = network.geometry(leg.from_node, leg.to_node, leg.mode)
    on_path, distance = geometry.touches(event_point)
    if on_path:
        return (
            True,
            (
                f"event is {distance:.0f} km from the "
                f"{network.node(leg.from_node).name} → {network.node(leg.to_node).name} "
                f"{leg.mode.value} corridor (width {geometry.corridor_km:.0f} km)"
            ),
            leg.to_node,
        )

    return (
        False,
        f"event is {distance:.0f} km off this leg's corridor",
        "",
    )


def _distance(point: Point, node) -> float:
    from engine.network.geo import haversine_km

    return haversine_km(point, Point(lat=node.lat, lon=node.lon))


def exposure_ranking(
    events: list[Event],
    hits: list[GateHit],
    shipments: list[Shipment],
) -> list[tuple[str, float]]:
    """Rank the triage queue before any reasoning happens (BRIEF §3.2).

        exposure(event) = Σ CHF of shipments routed through the affected node
                          in the relevant time window

    A database lookup, no model. The queue is ordered by how much Sika freight
    passes through there before a single token is spent, so a run that is cut
    short has still reasoned about the part that matters.
    """
    by_id = {s.shipment_id: s for s in shipments}
    totals: dict[str, float] = {e.event_id: 0.0 for e in events}
    for hit in hits:
        shipment = by_id.get(hit.shipment_id)
        if shipment is not None:
            totals[hit.event_id] = totals.get(hit.event_id, 0.0) + shipment.value_chf
    return sorted(totals.items(), key=lambda pair: pair[1], reverse=True)


def _fallback_days(event: Event, variables: dict) -> float:
    """How long to assume an open-ended event lasts, from the ledger.

    The LONGEST of the variables the event activates, because an event that is
    both a one-day stoppage and a two-month conflict has the footprint of the
    conflict — the short one finishes inside the long one, never the reverse.

    Floored at UNKNOWN_DURATION_FALLBACK_DAYS so a variable with no declared
    duration still gets a real window, and capped at MAX_FALLBACK_DAYS so one
    mistyped ledger entry cannot smear a single event across the whole book.
    """
    declared = [
        float(getattr(variables[v], "typical_duration_days", 0) or 0)
        for v in event.active_variables
        if v in variables
    ]
    longest = max(declared) if declared else 0.0
    return min(max(longest, UNKNOWN_DURATION_FALLBACK_DAYS), MAX_FALLBACK_DAYS)
