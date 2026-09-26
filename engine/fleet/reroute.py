"""Recovery routes for a disrupted asset, ranked on Time, Cost and Risk.

WHAT A CANDIDATE IS
===================
The original route from the live location to the destination, with ONE
disrupted stretch replaced. The stretch is a run of consecutive legs of the
same kind — inland (road, rail, barge) or sea — that contains a leg the gate
hit. Everything before and after it keeps its planned legs and its planned
timings, so a candidate differs from the plan exactly where the disruption
is and nowhere else.

The recipes, each a thing a planner actually does:

  inland_to_road   offload to trucks at the next terminal, drive the stretch
  inland_to_rail   switch the stretch to rail
  road_detour      drive around a road closure (OSRM when allowed)
  sea_bypass       sail the stretch avoiding the closed node — the sea-lane
                   graph with that node removed (engine/fleet/sealanes.py)
  port_swap        load or discharge at the port's declared alternative, or
                   at a nearby port with a land bridge when no sea route is
                   left, and cover the gap by road or rail

HOW EACH IS PRICED, TIMED AND RISKED
====================================
Time   the plan's own timings for the untouched legs; the rate card's speeds
       for the new ones; a transfer where the mode changes; the lever's
       setup time from scoring.yaml (a rail path is not booked instantly);
       plus whatever delay the route still cannot avoid.
Cost   the rate card in fleet.yaml, applied to the original route AND every
       candidate on the same basis — so the delta is a like-for-like number.
Risk   1 − Π(1 − r) over each leg's baseline, each transfer, and each live
       event the route still touches (P × severity weight).

Ranking min-max normalises each of the three across the candidates AND the
original route, and weights them. The original is in the pool on purpose:
when staying put scores better than every alternative, the answer is "stay",
and the payload says so rather than badging the least bad detour #1.

NOTHING HERE READS THE WALL CLOCK. Every time is relative to the board's as-of.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

from engine.fleet import manifest, osrm
from engine.fleet.assets import index, locate, remaining_legs, status_of
from engine.fleet.paths import as_latlon, length_km
from engine.fleet.sealanes import SeaGraph
from engine.fleet.settings import settings as fleet_settings
from engine.network.geo import (
    CORRIDOR_WIDTH_KM,
    PORT_CATCHMENT_KM,
    Point,
    great_circle_points,
    haversine_km,
    on_corridor,
)
from engine.pipeline import RunContext

INLAND = {"road", "rail", "barge"}
LANDBRIDGE_MAX_KM = {"road": 900.0, "rail": 4500.0}
# Events a truck can drive around: something closed at a place, not weather
# over a region.
DETOUR_FAMILIES = {"infrastructure", "force_majeure", "labour"}

# In order of what makes a reroute recognisable: "via the Cape" is the name
# a planner uses, even though the same path also passes Gibraltar.
NOTABLE = {
    "CHOKE_GOODHOPE": "Cape of Good Hope",
    "CHOKE_SUNDA": "Sunda Strait",
    "CHOKE_GIB": "Gibraltar",
    "CHOKE_MALACCA": "Malacca",
}


@dataclass
class _Leg:
    mode: str
    start: dict
    end: dict
    path: list[Point]
    km: float                  # effective km: what the vehicle drives
    hours: float               # moving time on the rate card (new legs only)
    routed_by: str
    new: bool
    leg_index: int | None = None   # the planned leg it came from, if unchanged
    via: list[str] = field(default_factory=list)

    def wire(self) -> dict:
        return {
            "mode": self.mode,
            "from": {k: self.start.get(k) for k in ("id", "name", "lat", "lon")},
            "to": {k: self.end.get(k) for k in ("id", "name", "lat", "lon")},
            "km": round(self.km, 1),
            "hours": round(self.hours, 2) if self.new else None,
            "routed_by": self.routed_by,
            "new": self.new,
            "path": as_latlon(self.path),
        }


# =====================================================================
# Entry point
# =====================================================================


def recovery(board: dict, context: RunContext, shipment_id: str,
             weights: dict | None = None, force: bool = False) -> dict | None:
    """The original route, the recovery candidates, and their ranking."""
    idx = index(context)
    shipment = idx.shipments.get(shipment_id)
    if shipment is None:
        return None
    where = locate(context, shipment)
    if where is None:
        return None

    cfg = fleet_settings(context.config)
    w = _weights(weights or cfg["ranking"]["weights"])
    status = status_of(context, shipment)
    rem = remaining_legs(context, shipment, where)
    teu = manifest.teu_of(manifest.containers(shipment))
    nodes = context.config.nodes

    hits = idx.hits.get(shipment_id, [])
    ahead = [h for h in hits if h.leg_index >= where["leg_index"]]
    disrupted_legs = {h.leg_index for h in ahead}
    chain = {leg["to"]["id"] for leg in rem} | {
        leg["from"]["id"] for leg in rem if leg["from"]["id"]}
    disrupted_nodes = _disrupted_nodes(context, ahead, chain)

    ctx = _Ctx(context, shipment, where, cfg, teu, status)
    original = ctx.evaluate(
        "ORIGINAL", "Original route", "original",
        [_planned(ctx, leg) for leg in rem], replaced=None,
    )

    disruption = _disruption_point(ctx, ahead)
    payload = {
        "shipment_id": shipment_id,
        "status": status,
        "weights": w,
        "teu": teu,
        "origin": {"name": "Live location" if where["phase"] == "in_transit"
                   else nodes[shipment.legs[where["leg_index"]].from_node].name,
                   "lat": round(where["point"].lat, 5), "lon": round(where["point"].lon, 5)},
        "destination": {"id": shipment.destination_node,
                        "name": nodes[shipment.destination_node].name,
                        "lat": nodes[shipment.destination_node].lat,
                        "lon": nodes[shipment.destination_node].lon},
        "disruption": disruption,
        "disrupted_nodes": sorted(disrupted_nodes),
        "original": original,
        "candidates": [],
        "eligible": status["level"] in ("yellow", "red") or force,
        "note": None,
        "no_route": [],
    }

    if not payload["eligible"]:
        payload["note"] = "Nominal — on schedule, so no recovery route is drawn."
        return payload
    if not ahead:
        payload["note"] = (
            "The disruption is on a leg this asset has already finished. No route "
            "change from here can recover it; the delay is carried to the destination."
        )
        return payload

    candidates, no_route = _generate(ctx, rem, disrupted_legs, disrupted_nodes)
    payload["no_route"] = no_route
    payload["candidates"] = _rank(original, candidates, w, cfg)
    ahead_chain = {leg["to"]["id"] for leg in rem}
    if rem and rem[0]["from"]["id"]:
        ahead_chain.add(rem[0]["from"]["id"])
    if not payload["candidates"] and disrupted_nodes and not disrupted_nodes & ahead_chain:
        names = ", ".join(sorted(nodes[n].name for n in disrupted_nodes if n in nodes))
        payload["note"] = (
            f"The disruption is at {names}, which this asset has already passed. The "
            "expected delay is carried to the destination; no route from here avoids it."
        )
    elif not payload["candidates"]:
        payload["note"] = "No alternative route could be built for this disruption."
    elif not any(c["beats_original"] for c in payload["candidates"]):
        payload["note"] = (
            "Staying on the original route scores better than every alternative "
            "at these weights. The alternatives are drawn so that is visible, not assumed."
        )
    return payload


def _disrupted_nodes(context: RunContext, ahead: list, chain: set[str]) -> set[str]:
    """Nodes an event is AT — named by it, or inside its catchment.

    The gate reports a corridor hit against the leg's destination node, which
    is right for "this leg is touched" and wrong for "this port is closed": a
    motorway fire near Karlsruhe does not close Rotterdam. Treating it as if
    it did would send every recovery route to a different port.
    """
    idx = index(context)
    nodes = context.config.nodes
    out: set[str] = set()
    for h in ahead:
        event = idx.events.get(h.event_id)
        named = set(getattr(event, "node_ids", []) or [])
        out |= named & chain
        node = nodes.get(h.node_id) if h.node_id else None
        if node is None:
            continue
        if h.node_id in named:
            out.add(h.node_id)
        elif event is not None and event.lat is not None and haversine_km(
                Point(event.lat, event.lon), Point(node.lat, node.lon)) <= PORT_CATCHMENT_KM:
            out.add(h.node_id)
    return out


def _weights(raw: dict) -> dict:
    w = {}
    for k in ("time", "cost", "risk"):
        try:
            value = float(raw.get(k, 0.0))
        except (TypeError, ValueError):
            value = 0.0            # a weight that is not a number weighs nothing
        w[k] = max(0.0, value) if math.isfinite(value) else 0.0
    total = sum(w.values())
    if total <= 0:
        return {"time": 1 / 3, "cost": 1 / 3, "risk": 1 / 3}
    return {k: round(v / total, 4) for k, v in w.items()}


# =====================================================================
# Shared evaluation context
# =====================================================================


class _Ctx:
    def __init__(self, context: RunContext, shipment, where: dict, cfg: dict,
                 teu: int, status: dict) -> None:
        self.context = context
        self.shipment = shipment
        self.where = where
        self.cfg = cfg
        self.teu = teu
        self.status = status
        self.as_of = context.clock.as_of
        self.nodes = context.config.nodes
        self.idx = index(context)
        self.sea = _sea_graph(context)
        self.current_mode = shipment.legs[where["leg_index"]].mode.value

        # Events already gated onto this shipment, by the leg they hit.
        self.hit_events: dict[int, set[str]] = {}
        for h in self.idx.hits.get(shipment.shipment_id, []):
            self.hit_events.setdefault(h.leg_index, set()).add(h.event_id)
        self.risk_by_event = {
            r.event_id: r for r in self.idx.risks.get(shipment.shipment_id, [])
        }

    # ---------------------------------------------------------------
    def node_ref(self, node_id: str) -> dict:
        n = self.nodes[node_id]
        return {"id": node_id, "name": n.name, "lat": n.lat, "lon": n.lon}

    def mode_cfg(self, mode: str) -> dict:
        return self.cfg["modes"].get(mode, self.cfg["modes"]["road"])

    def move_hours(self, mode: str, km: float) -> float:
        return km / float(self.mode_cfg(mode)["speed_kmh"])

    def setup_hours(self, kind: str, lever: str | None = None) -> tuple[float, str | None]:
        lever = lever or self.cfg["levers"].get(kind)
        if lever is None:
            return 0.0, None
        hours = self.context.config.min_action_hours(lever)
        return (float(hours) if hours is not None else 0.0), lever

    def event_p(self, event) -> tuple[float, bool]:
        """P(event), and whether that probability was sourced."""
        if getattr(event, "realized", False):
            return 1.0, True
        p = getattr(event, "probability", None)
        if p is None:
            return 1.0, False          # unsourced: counted in full, and flagged
        return float(p), True

    def event_delay_hours(self, event_id: str, mode: str) -> float:
        """What an event costs in hours if this route still meets it."""
        risk = self.risk_by_event.get(event_id)
        if risk is not None:
            return risk.do_nothing.expected_delay_days * 24.0
        event = self.idx.events.get(event_id)
        if event is None:
            return 0.0
        p, _ = self.event_p(event)
        for var_id in event.active_variables:
            var = self.context.config.variables.get(var_id)
            if var is None or not any(m.value == mode for m in var.modes_affected):
                continue
            triple = self.context.config.delay_triple(var_id, event.severity.value)
            return triple.likely * 24.0 * p
        return 0.0

    # ---------------------------------------------------------------
    def touches(self, leg: _Leg, window_start, window_end) -> list[tuple[str, str]]:
        """Live events a NEW leg still runs into: (event_id, why).

        The same three questions the gate asks — mode, time, place — with one
        narrowing for sea legs: an event anchored on a named port or strait
        (a strike, a canal cut) touches a vessel that CALLS there, not every
        vessel passing within the 250 km sea corridor. A dock strike in
        Antwerp does not slow a ship sailing past the Scheldt.
        """
        out = []
        ends = {leg.start.get("id"), leg.end.get("id"), *leg.via} - {None}
        for event in self.idx.events.values():
            if not any(m.value == leg.mode for m in event.modes_affected):
                continue
            if event.starts_at > window_end:
                continue
            if event.ends_at is not None and event.ends_at < window_start:
                continue
            anchored = set(event.node_ids or [])
            named = ends & anchored
            if named:
                first = sorted(named)[0]
                out.append((event.event_id, f"calls at {self.nodes[first].name}"
                            if first in self.nodes else "passes a named node"))
                continue
            if event.lat is None or event.lon is None:
                continue
            if leg.mode == "sea" and anchored:
                continue
            point = Point(event.lat, event.lon)
            near_end = any(
                haversine_km(point, Point(e["lat"], e["lon"])) <= PORT_CATCHMENT_KM
                for e in (leg.start, leg.end)
            )
            hit, dist = on_corridor(point, leg.path, leg.mode,
                                    CORRIDOR_WIDTH_KM.get(leg.mode, 60.0))
            if near_end or hit:
                out.append((event.event_id, f"{dist:.0f} km from its {leg.mode} corridor"))
        return out

    # ---------------------------------------------------------------
    def evaluate(self, option_id: str, label: str, kind: str, legs: list[_Leg],
                 replaced: tuple[int, int] | None, teu: int | None = None,
                 notes: list[str] | None = None, lever: str | None = None) -> dict:
        """Time, cost and risk for a full leg sequence from the live location."""
        teu = self.teu if teu is None else teu
        shipment = self.shipment
        eta_planned = shipment.eta
        tr = self.cfg["transfer"]

        transfers = sum(1 for a, b in zip(legs, legs[1:], strict=False) if a.mode != b.mode)
        if legs and legs[0].new and legs[0].mode != self.current_mode:
            transfers += 1

        # --- time -------------------------------------------------------
        touched: dict[str, str] = {}
        residual_events: set[str] = set()
        # Anything already gated on a leg this route keeps, or on a leg
        # already behind the asset, still applies.
        for leg in legs:
            if not leg.new and leg.leg_index is not None:
                residual_events |= self.hit_events.get(leg.leg_index, set())
        for i, evs in self.hit_events.items():
            if i < self.where["leg_index"]:
                residual_events |= evs

        setup = 0.0
        if replaced is None:
            lever = None
            delay = self.status["delay_hours"]
            eta = eta_planned + timedelta(hours=delay)
        else:
            first_new = next(i for i, leg in enumerate(legs) if leg.new)
            last_new = max(i for i, leg in enumerate(legs) if leg.new)
            prefix = legs[:first_new]
            suffix = legs[last_new + 1:]

            if prefix:
                t = shipment.legs[prefix[-1].leg_index].planned_arrive
            else:
                t = self.as_of
            setup, lever = self.setup_hours(kind, lever)
            t = max(t, self.as_of + timedelta(hours=setup))
            moving = sum(leg.hours for leg in legs[first_new:last_new + 1])
            t = t + timedelta(hours=moving + transfers * float(tr["hours"]))
            if suffix:
                span = eta_planned - shipment.legs[suffix[0].leg_index].planned_depart
                t = t + span
            clock = t - timedelta(hours=moving + transfers * float(tr["hours"]))
            for leg in legs[first_new:last_new + 1]:
                leg_end = clock + timedelta(hours=leg.hours)
                for event_id, why in self.touches(leg, clock, leg_end):
                    touched[event_id] = why
                    residual_events.add(event_id)
                clock = leg_end
            delay = max(
                (self.event_delay_hours(e, next((lg.mode for lg in legs if lg.new), "road"))
                 for e in residual_events),
                default=0.0,
            )
            eta = t + timedelta(hours=delay)

        hours = (eta - self.as_of).total_seconds() / 3600.0

        # --- cost -------------------------------------------------------
        cost = price(self.cfg, [(leg.mode, leg.km) for leg in legs], teu, transfers)

        # --- risk -------------------------------------------------------
        sev = self.cfg["ranking"]["severity_weight"]
        parts = [float(self.mode_cfg(leg.mode)["baseline_risk"]) for leg in legs]
        parts += [float(tr["risk"])] * transfers
        unsourced = False
        for event_id in residual_events:
            event = self.idx.events.get(event_id)
            if event is None:
                continue
            p, sourced = self.event_p(event)
            unsourced = unsourced or not sourced
            parts.append(min(0.99, p * float(sev.get(event.severity.value, 0.6))))
        risk = 1.0 - math.prod(1.0 - r for r in parts)

        path: list[Point] = []
        for leg in legs:
            path.extend(leg.path if not path else leg.path[1:])
        new_legs = [leg for leg in legs if leg.new]

        return {
            "id": option_id,
            "label": label,
            "kind": kind,
            "legs": [leg.wire() for leg in legs],
            "path": as_latlon(path),
            "branch_point": (
                {"lat": round(new_legs[0].path[0].lat, 5), "lon": round(new_legs[0].path[0].lon, 5),
                 "name": new_legs[0].start.get("name")}
                if new_legs else None
            ),
            "km": round(sum(leg.km for leg in legs), 1),
            "hours": round(hours, 2),
            "eta": eta.isoformat(),
            "delay_hours": round(delay, 2),
            "setup_hours": round(setup, 1),
            "lever": lever,
            "owner": "carrier" if kind == "sea_bypass" else "us",
            "transfers": transfers,
            "cost_chf": round(cost, 2),
            "risk": round(risk, 4),
            "risk_label": _risk_label(risk, self.cfg),
            "risk_unsourced": unsourced,
            "touches": [
                {"event_id": e, "title": getattr(self.idx.events.get(e), "title", e), "why": why}
                for e, why in sorted(touched.items())
            ],
            "avoids": [],
            "notes": list(notes or []),
            "teu": teu,
        }


def price(cfg: dict, legs: list[tuple[str, float]], teu: int, transfers: int) -> float:
    """The rate card, for (mode, km) legs carrying *teu*.

    Road is priced per VEHICLE, everything else per TEU: one 20-foot box on a
    truck costs the whole truck, and that is exactly the arithmetic that makes
    a split worth doing for eight boxes and not for one.
    """
    total = 0.0
    for mode, km in legs:
        m = cfg["modes"].get(mode, cfg["modes"]["road"])
        if teu <= 0:
            continue
        if "chf_per_vehicle_km" in m:
            vehicles = max(1, math.ceil(teu / float(m.get("teu_per_vehicle", 2))))
            total += vehicles * km * float(m["chf_per_vehicle_km"])
        else:
            total += teu * km * float(m["chf_per_teu_km"])
    return total + transfers * teu * float(cfg["transfer"]["chf_per_teu"])


def _risk_label(risk: float, cfg: dict) -> str:
    for band in cfg["ranking"]["risk_labels"]:
        if risk < float(band["max"]):
            return band["label"]
    return cfg["ranking"]["risk_labels"][-1]["label"]


def _sea_graph(context: RunContext) -> SeaGraph:
    graph = getattr(context, "_fleet_sea", None)
    if graph is None:
        graph = SeaGraph(context.config.nodes)
        context._fleet_sea = graph  # type: ignore[attr-defined]
    return graph


# =====================================================================
# Legs
# =====================================================================


def _planned(ctx: _Ctx, leg: dict) -> _Leg:
    mode = leg["mode"]
    km = leg["km"] * float(ctx.mode_cfg(mode)["detour_factor"])
    return _Leg(mode=mode, start=leg["from"], end=leg["to"], path=leg["path"], km=km,
                hours=ctx.move_hours(mode, km), routed_by="plan", new=False,
                leg_index=leg["index"])


def _inland(ctx: _Ctx, mode: str, start: dict, end: dict,
            via: Point | None = None) -> _Leg:
    """A new road or rail leg. Road asks OSRM first, when allowed."""
    a, b = Point(start["lat"], start["lon"]), Point(end["lat"], end["lon"])
    points = [a, via, b] if via is not None else [a, b]
    if mode == "road":
        routing = ctx.cfg["routing"]
        found = osrm.road(points, routing["osrm_url"], float(routing.get("timeout_s", 4)))
        if found is not None:
            path, km = found
            return _Leg(mode=mode, start=start, end=end, path=path, km=km,
                        hours=ctx.move_hours(mode, km), routed_by="osrm", new=True)
    path: list[Point] = []
    for p, q in zip(points, points[1:], strict=False):
        seg = great_circle_points(p, q, segments=10)
        path.extend(seg if not path else seg[1:])
    km = length_km(path) * float(ctx.mode_cfg(mode)["detour_factor"])
    return _Leg(mode=mode, start=start, end=end, path=path, km=km,
                hours=ctx.move_hours(mode, km), routed_by="corridor estimate", new=True)


def _sea(ctx: _Ctx, start: dict, end_id: str, avoid: set[str],
         start_links: dict | None = None) -> _Leg | None:
    avoid = set(avoid) - {end_id} - ({start.get("id")} if start.get("id") else set())
    if start_links:
        found = ctx.sea.route("", end_id, avoid=avoid,
                              start_point=Point(start["lat"], start["lon"]),
                              start_links=start_links)
    else:
        found = ctx.sea.route(start["id"], end_id, avoid=avoid)
    if found is None:
        return None
    km = found.distance_km * float(ctx.mode_cfg("sea")["detour_factor"])
    return _Leg(mode="sea", start=start, end=ctx.node_ref(end_id), path=found.path, km=km,
                hours=ctx.move_hours("sea", km), routed_by="sea-lane graph", new=True,
                via=found.node_ids)


# =====================================================================
# Candidate generation
# =====================================================================


def _groups(rem: list[dict]) -> list[tuple[int, int, str]]:
    """Consecutive runs of the same KIND of leg: (first, last, 'inland'|'sea')."""
    out: list[tuple[int, int, str]] = []
    for i, leg in enumerate(rem):
        kind = "sea" if leg["mode"] == "sea" else "inland"
        if out and out[-1][2] == kind:
            out[-1] = (out[-1][0], i, kind)
        else:
            out.append((i, i, kind))
    return out


def _generate(ctx: _Ctx, rem: list[dict], disrupted_legs: set[int],
              disrupted_nodes: set[str]) -> tuple[list[dict], list[str]]:
    planned = [_planned(ctx, leg) for leg in rem]
    candidates: list[dict] = []
    no_route: list[str] = []
    seen: set[tuple] = set()

    def add(option_id: str, label: str, kind: str, a: int, b: int,
            new_legs: list[_Leg], notes: list[str] | None = None,
            lever: str | None = None) -> None:
        legs = planned[:a] + new_legs + planned[b + 1:]
        sig = (kind, round(sum(leg.km for leg in new_legs)), new_legs[-1].end.get("id"))
        if sig in seen:
            return
        seen.add(sig)
        option = ctx.evaluate(option_id, label, kind, legs, replaced=(a, b), notes=notes,
                              lever=lever)
        option["avoids"] = sorted(
            n for n in disrupted_nodes
            if n in ctx.nodes and not any(
                n in (lg.start.get("id"), lg.end.get("id"), *lg.via) for lg in new_legs)
        )
        candidates.append(option)

    groups = _groups(rem)
    for gi, (a, b, kind) in enumerate(groups):
        if not any(rem[i]["index"] in disrupted_legs for i in range(a, b + 1)):
            # The group itself is clear — but its END may be a port that is
            # disrupted for the sea stretch after it. Handled by that stretch.
            continue
        start, end = rem[a]["from"], rem[b]["to"]
        modes = {rem[i]["mode"] for i in range(a, b + 1)}

        if kind == "inland":
            feeds_closed_port = (
                gi + 1 < len(groups) and groups[gi + 1][2] == "sea"
                and end.get("id") in disrupted_nodes
            )
            if not feeds_closed_port:
                # Delivering faster to a port that is shut helps nobody; the
                # loading-port swap below is the recovery for that case.
                _inland_recipes(ctx, add, rem, a, b, start, end, modes, disrupted_legs)
        else:
            _sea_recipes(ctx, add, no_route, rem, groups, gi, a, b, start, end,
                         disrupted_nodes)

    # A loading port that is shut: sail from its alternative instead. If an
    # inland stretch feeds it, that stretch is re-pointed at the alternative;
    # if the freight is already sitting in the port, it is trucked across.
    for gi, (a, b, kind) in enumerate(groups):
        if kind != "sea":
            continue
        load_port = rem[a]["from"].get("id")
        if load_port not in disrupted_nodes:
            continue
        if gi > 0:
            pa = groups[gi - 1][0]
            _loading_swap(ctx, add, rem, pa, b, rem[pa]["from"], load_port,
                          rem[b]["to"], disrupted_nodes)
        elif ctx.where["phase"] != "in_transit":
            _loading_swap(ctx, add, rem, a, b, rem[a]["from"], load_port,
                          rem[b]["to"], disrupted_nodes)

    return candidates, no_route


def _inland_recipes(ctx, add, rem, a, b, start, end, modes, disrupted_legs) -> None:
    end_node = ctx.nodes.get(end["id"])
    end_modes = {m.value for m in end_node.modes} if end_node else set()
    first_mode = rem[a]["mode"]
    live = start.get("id") is None

    if modes - {"road"} and "road" in end_modes:
        leg = _inland(ctx, "road", start, end)
        word = "Offload to trucks" if first_mode in ("barge", "rail") else "Truck direct"
        add("ALT-ROAD", f"{word} → {end['name']}", "inland_to_road", a, b, [leg],
            notes=["Transfer at the nearest terminal to the live location."] if live else None)

    rail_ok = "rail" in end_modes and (live or "rail" in {
        m.value for m in ctx.nodes[start["id"]].modes})
    if modes - {"rail"} and rail_ok:
        leg = _inland(ctx, "rail", start, end)
        # A barge-to-rail switch is its own lever with its own lead time;
        # moving road freight onto rail is an ordinary rail booking.
        add("ALT-RAIL", f"Switch to rail → {end['name']}", "inland_to_rail", a, b, [leg],
            lever=None if "barge" in modes else "rail_rebook")

    if modes == {"road"}:
        detour = _detour_point(ctx, rem, a, b, disrupted_legs)
        if detour is not None:
            via, title = detour
            leg = _inland(ctx, "road", start, end, via=via)
            add("ALT-DETOUR", f"Road detour around {title}", "road_detour", a, b, [leg])


def _detour_point(ctx, rem, a, b, disrupted_legs) -> tuple[Point, str] | None:
    """A waypoint beside the closure, far enough to leave its corridor."""
    for i in range(a, b + 1):
        if rem[i]["index"] not in disrupted_legs:
            continue
        for event_id in ctx.hit_events.get(rem[i]["index"], set()):
            event = ctx.idx.events.get(event_id)
            if event is None or event.lat is None:
                continue
            if not any(m.value == "road" for m in event.modes_affected):
                continue
            families = {
                ctx.context.config.variables[v].family for v in event.active_variables
                if v in ctx.context.config.variables
            }
            if not families & DETOUR_FAMILIES:
                continue          # a gale is regional: there is no "round" to drive
            point = Point(event.lat, event.lon)
            path = rem[i]["path"]
            if min(haversine_km(point, path[0]), haversine_km(point, path[-1])) <= PORT_CATCHMENT_KM:
                continue          # a closure AT the destination cannot be driven round
            hit, _ = on_corridor(point, path, "road", CORRIDOR_WIDTH_KM["road"])
            if not hit:
                continue
            # Perpendicular to the leg's overall direction, on both sides;
            # keep the side that makes the shorter trip.
            a_pt, b_pt = path[0], path[-1]
            dx, dy = b_pt.lon - a_pt.lon, b_pt.lat - a_pt.lat
            norm = math.hypot(dx, dy) or 1.0
            offset_deg = (CORRIDOR_WIDTH_KM["road"] * 1.4) / 111.0
            options = [
                Point(point.lat + (dx / norm) * offset_deg * s,
                      point.lon - (dy / norm) * offset_deg * s / max(0.2, math.cos(math.radians(point.lat))))
                for s in (1, -1)
            ]
            via = min(options, key=lambda v: haversine_km(a_pt, v) + haversine_km(v, b_pt))
            return via, _short(event.title, 40)
    return None


def _sea_recipes(ctx, add, no_route, rem, groups, gi, a, b, start, end,
                 disrupted_nodes) -> None:
    blocked = {n for n in disrupted_nodes if ctx.sea.has(n)}
    end_id = end["id"]
    start_links = None
    if start.get("id") is None:
        leg = ctx.shipment.legs[rem[a]["index"]]
        travelled = ctx.where["leg_travelled_path"]
        start_links = {
            leg.from_node: list(reversed(travelled)),
            leg.to_node: list(rem[a]["path"]),
        }

    # 1. Sail round the closure. Only closures still AHEAD in this stretch:
    # a port the vessel has already left is not something to route around.
    ahead_nodes = {rem[i]["to"]["id"] for i in range(a, b)}
    if start.get("id"):
        ahead_nodes.add(start["id"])
    through = (blocked & ahead_nodes) - {end_id, start.get("id")}
    at_start = a == 0 and ctx.where["phase"] == "at_node"
    if at_start and start.get("id") in blocked and not ctx.nodes[start["id"]].alternatives:
        no_route.append(f"Already at {start['name']}: the way on is through it, or back "
                        "the way it came. Waiting it out is the realistic option.")
    if through:
        sea = _sea(ctx, start, end_id, through, start_links)
        names = ", ".join(sorted(ctx.nodes[n].name for n in through if n in ctx.nodes))
        if sea is None:
            no_route.append(f"No sea route to {end['name']} avoids {names}.")
        else:
            planned_chain = {rem[i]["to"]["id"] for i in range(a, b + 1)}
            notable = [NOTABLE[n] for n in NOTABLE
                       if n in sea.via and n not in planned_chain]
            label = f"Reroute via {notable[0]}" if notable else f"Sea lane avoiding {names}"
            add("ALT-BYPASS", label, "sea_bypass", a, b, [sea],
                notes=["Rerouting a vessel is the carrier's decision: this is what to "
                       "escalate for, not an order the planner can give."])

    # 2. Discharge somewhere else, and close the gap over land.
    reachable = None if end_id in blocked else _sea(ctx, start, end_id, blocked, start_links)
    if end_id in blocked or reachable is None:
        ports = _alt_ports(ctx, end_id, blocked)
        if not ports and end_id not in blocked:
            pass
        elif not ports:
            no_route.append(f"{end['name']} has no alternative port configured.")
        for alt_id, land_mode in ports:
            sea = _sea(ctx, start, alt_id, blocked, start_links)
            if sea is None:
                continue
            land = _inland(ctx, land_mode, ctx.node_ref(alt_id), end)
            word = "truck" if land_mode == "road" else "rail"
            add(f"ALT-PORT-{alt_id}", f"Discharge at {ctx.nodes[alt_id].name}, {word} to "
                f"{end['name']}", "port_swap", a, b, [sea, land])


def _loading_swap(ctx, add, rem, a, b, start, load_port, end, disrupted_nodes) -> None:
    blocked = {n for n in disrupted_nodes if ctx.sea.has(n)}
    for alt_id in ctx.nodes[load_port].alternatives[:2]:
        alt = ctx.nodes.get(alt_id)
        if alt is None or alt_id in blocked or not ctx.sea.has(alt_id):
            continue
        land = _inland(ctx, "road", start, ctx.node_ref(alt_id))
        sea = _sea(ctx, ctx.node_ref(alt_id), end["id"], blocked)
        if sea is None:
            continue
        add(f"ALT-LOAD-{alt_id}", f"Load at {alt.name} instead of "
            f"{ctx.nodes[load_port].name}", "port_swap", a, b, [land, sea])


def _alt_ports(ctx, port_id: str, blocked: set[str]) -> list[tuple[str, str]]:
    """Declared alternatives first; then, only if none, the nearest sea ports
    with a land bridge — how "Panama is cut" becomes "sail to Houston, rail
    to Los Angeles" without anybody writing that rule down."""
    port = ctx.nodes.get(port_id)
    if port is None:
        return []
    here = Point(port.lat, port.lon)
    port_modes = {m.value for m in port.modes}

    def land_mode(other) -> str | None:
        km = haversine_km(here, Point(other.lat, other.lon))
        other_modes = {m.value for m in other.modes}
        if "rail" in port_modes and "rail" in other_modes and km <= LANDBRIDGE_MAX_KM["rail"]:
            return "rail" if km > 600 else ("road" if "road" in other_modes else "rail")
        if "road" in port_modes and "road" in other_modes and km <= LANDBRIDGE_MAX_KM["road"]:
            return "road"
        return None

    out = []
    for alt_id in port.alternatives:
        alt = ctx.nodes.get(alt_id)
        if alt and alt_id not in blocked and ctx.sea.has(alt_id) and land_mode(alt):
            out.append((alt_id, land_mode(alt)))
    if out:
        return out[:2]
    others = sorted(
        (n for n in ctx.nodes.values()
         if n.id != port_id and n.id not in blocked and n.kind.value == "seaport"
         and ctx.sea.has(n.id) and land_mode(n)),
        key=lambda n: haversine_km(here, Point(n.lat, n.lon)),
    )
    return [(n.id, land_mode(n)) for n in others[:2]]


def _disruption_point(ctx: _Ctx, ahead: list) -> dict | None:
    """Where the worst remaining hit is — the marker the original line leaves from."""
    if not ahead:
        return None
    worst = max(ahead, key=lambda h: (
        ctx.risk_by_event[h.event_id].do_nothing.expected_delay_days
        if h.event_id in ctx.risk_by_event else 0.0))
    event = ctx.idx.events.get(worst.event_id)
    node = ctx.nodes.get(worst.node_id) if worst.node_id else None
    lat = node.lat if node else getattr(event, "lat", None)
    lon = node.lon if node else getattr(event, "lon", None)
    return {
        "event_id": worst.event_id,
        "title": getattr(event, "title", worst.event_id),
        "node_id": worst.node_id or None,
        "name": node.name if node else None,
        "lat": lat,
        "lon": lon,
        "leg_index": worst.leg_index,
        "reason": worst.spatial_reason,
    }


# =====================================================================
# Ranking
# =====================================================================


def _rank(original: dict, candidates: list[dict], w: dict, cfg: dict) -> list[dict]:
    pool = [original, *candidates]
    spans = {}
    for key in ("hours", "cost_chf", "risk"):
        values = [o[key] for o in pool]
        spans[key] = (min(values), max(values))

    def norm(value: float, key: str) -> float:
        low, high = spans[key]
        return 0.0 if high - low < 1e-9 else (value - low) / (high - low)

    def score(o: dict) -> float:
        return (w["time"] * norm(o["hours"], "hours")
                + w["cost"] * norm(o["cost_chf"], "cost_chf")
                + w["risk"] * norm(o["risk"], "risk"))

    original["score"] = round(score(original), 4)
    for c in candidates:
        c["score"] = round(score(c), 4)
        c["components"] = {
            "time": round(norm(c["hours"], "hours"), 4),
            "cost": round(norm(c["cost_chf"], "cost_chf"), 4),
            "risk": round(norm(c["risk"], "risk"), 4),
        }
        c["beats_original"] = c["score"] < original["score"]
        dh = c["hours"] - original["hours"]
        dc = c["cost_chf"] - original["cost_chf"]
        c["delta"] = {
            "hours": round(dh, 2),
            "cost_chf": round(dc, 2),
            "risk": round(c["risk"] - original["risk"], 4),
            "text": f"{_signed_chf(dc)}, {_signed_hours(dh)}, Risk: {c['risk_label']}",
        }

    ranked = sorted(candidates, key=lambda c: (c["score"], c["hours"], c["id"]))
    badges = int(cfg["ranking"]["badges"])
    for i, c in enumerate(ranked, start=1):
        c["rank"] = i
        c["badge"] = f"#{i}" if i <= badges else None
    return ranked


def _signed_chf(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}CHF {abs(round(v)):,}".replace(",", "'")


def _signed_hours(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    a = abs(v)
    if a < 48:
        return f"{sign}{a:.0f} h" if a >= 10 else f"{sign}{a:.1f} h".replace(".0 h", " h")
    return f"{sign}{a / 24:.1f} days"


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
