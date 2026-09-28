"""What is around the freight: the map's context layers.

A planner looking at one shipment, one route or one customer needs to see
what they could use next to it, not a world atlas: the ports it could switch
to, where stock sits, who is local, how it would get there by road or rail,
and which of that is close AND free right now. The map draws none of this
until something is selected, and then only around it.

FIVE LAYERS, ONE PER TICK ON THE MAP
------------------------------------
ports        The ports on the route, their declared alternatives, and the
             other ports near the freight. Each says whether an event on the
             board is sitting on it right now.
inventories  Where stock or space is: Sika's own plants and distribution
             centres, and partner warehouses with their free slots.
vendors      Local partners: hauliers, rail and barge operators, forwarders,
             agents. Capacity only where a partner published one; unknown
             otherwise, never invented.
links        Road and rail from where the freight can next touch land to the
             ports and stock around it, with distance and hours. Rail only
             where both ends have rail. Drawn as corridor estimates, labelled
             so: offline-first, no routing service asked.
nearby       Everything above that is inside the reach radius AND available
             now: a port with no event on it, a warehouse or partner with
             capacity free.

ANCHORS
-------
Where "around" is measured from: the vehicle for a shipment; for a route or
a customer, the vehicles on it that are furthest behind (up to three), which
are the ones a planner is looking for somewhere to go. A vessel at sea
cannot take a truck, so road and rail start from its next port.
"""

from __future__ import annotations

from engine.fleet.assets import fleet_assets, index, locate
from engine.fleet.settings import settings as fleet_settings
from engine.fleet.vendors import partners
from engine.network.geo import Point, great_circle_points, haversine_km
from engine.pipeline import RunContext

PORT_KINDS = {"seaport", "inland_port"}
STOCK_KINDS = {"plant", "distribution"}
WAREHOUSE_KINDS = {"warehouse"}

# How far to look. Declared, not measured: a port 400 km away is a real
# alternative for a truck, one 2,000 km away is a different plan.
PORT_RADIUS_KM = 450.0
ALT_RADIUS_KM = 2500.0      # a declared alternative, if it is within a sea day or two
LINK_KM = 800.0             # road and rail links: a land distance, never across a sea
STOCK_RADIUS_KM = 700.0
VENDOR_RADIUS_KM = 250.0
REACH_KM = 250.0            # "nearby": about five hours on the road
MAX_PORTS = 10
MAX_STOCK = 6
MAX_VENDORS = 10
MAX_LINKS = 8
MAX_ANCHORS = 3


class ContextError(ValueError):
    """Nothing to draw around: an unknown shipment, route or customer."""


def _km(a: Point, b: Point) -> float:
    return haversine_km(a, b)


def _nearest(point: Point, anchors: list[Point]) -> float:
    return min(_km(point, a) for a in anchors)


def _anchors(assets: list[dict], *, shipment_id: str | None, route_id: str | None,
             customer: str | None) -> tuple[dict, list[dict]]:
    if shipment_id:
        chosen = [a for a in assets if a["id"] == shipment_id]
        if not chosen:
            raise ContextError(f"no active shipment {shipment_id!r}")
        a = chosen[0]
        return {"kind": "shipment", "id": shipment_id, "label": f"{a['name']} · {a['id']}"}, chosen
    if route_id:
        mine = [a for a in assets if a["lane_id"] == route_id]
        if not mine:
            raise ContextError(f"no active shipment on route {route_id!r}")
        label = mine[0].get("lane_name") or route_id
        scope = {"kind": "route", "id": route_id, "label": label}
    elif customer:
        mine = [a for a in assets if a.get("customer") == customer]
        if not mine:
            raise ContextError(f"no active shipment for {customer!r}")
        scope = {"kind": "customer", "id": customer, "label": customer}
    else:
        raise ContextError("choose a shipment, a route or a customer")
    moving = {"in_transit": 0, "at_node": 1, "staging": 2, "booked": 3}
    mine.sort(key=lambda a: (moving.get(a["phase"], 9), -a["delay_hours"], a["id"]))
    return scope, mine[:MAX_ANCHORS]


def _next_node(anchor: dict, context: RunContext) -> str | None:
    shipment = index(context).shipments.get(anchor["id"])
    where = locate(context, shipment) if shipment is not None else None
    if not where or where.get("leg_index") is None:
        return None
    return shipment.legs[where["leg_index"]].to_node


def _origin(anchor: dict, context: RunContext) -> dict:
    """Where road and rail can start: the vehicle, or a vessel's next port."""
    if anchor["mode"] == "sea" and anchor["phase"] == "in_transit":
        nxt = _next_node(anchor, context)
        node = context.config.nodes.get(nxt) if nxt else None
        if node is not None:
            return {"lat": node.lat, "lon": node.lon, "name": node.name, "node_id": node.id,
                    "why": "next port"}
    return {"lat": anchor["lat"], "lon": anchor["lon"], "name": anchor["name"],
            "node_id": None, "why": "vehicle"}


def _disrupted_nodes(context: RunContext) -> dict[str, str]:
    """Node id -> the title of an event on the board sitting on it."""
    out: dict[str, str] = {}
    for event in context.events:
        for node_id in event.node_ids or []:
            out.setdefault(node_id, event.title)
    return out


def _capacity(p: dict) -> tuple[bool | None, str]:
    cap = p.get("capacity")
    if not cap or cap.get("available") is None:
        return None, "capacity unknown"
    n = cap["available"]
    return n > 0, f"{n} {cap.get('unit', 'units')} free"


def build(board: dict, context: RunContext, *, shipment_id: str | None = None,
          route_id: str | None = None, customer: str | None = None) -> dict:
    """The context layers around one shipment, route or customer."""
    config = context.config
    nodes = config.nodes
    cfg = fleet_settings(config)
    assets = fleet_assets(board, context)["assets"]
    scope, anchors = _anchors(assets, shipment_id=shipment_id, route_id=route_id,
                              customer=customer)
    points = [Point(a["lat"], a["lon"]) for a in anchors]
    origin = _origin(anchors[0], context)
    here = Point(origin["lat"], origin["lon"])
    reach_from = [*points, here]
    disrupted = _disrupted_nodes(context)

    # The routes in scope, for "on the route" and declared alternatives.
    lane_ids = {a["lane_id"] for a in anchors}
    lanes = [lane for lane in config.lanes if lane["id"] in lane_ids]
    on_route = {nid for lane in lanes for leg in lane["legs"] for nid in (leg["from"], leg["to"])}
    alternatives = {alt for nid in on_route if nid in nodes for alt in nodes[nid].alternatives}

    # ---- ports -------------------------------------------------------------
    ports = []
    for node in nodes.values():
        if node.kind.value not in PORT_KINDS:
            continue
        dist = _nearest(Point(node.lat, node.lon), reach_from)
        role = ("on_route" if node.id in on_route
                else "alternative" if node.id in alternatives and dist <= ALT_RADIUS_KM
                else "nearby" if dist <= PORT_RADIUS_KM else None)
        if role is None:
            continue
        event = disrupted.get(node.id)
        ports.append({
            "id": node.id, "name": node.name, "kind": node.kind.value, "role": role,
            "lat": node.lat, "lon": node.lon, "distance_km": round(dist),
            "modes": [m.value for m in node.modes],
            "available": event is None,
            "detail": event or ("open" if role != "on_route" else "on the route"),
        })
    rank = {"alternative": 0, "on_route": 1, "nearby": 2}
    ports.sort(key=lambda p: (rank[p["role"]], p["distance_km"]))
    ports = ports[:MAX_PORTS]

    # ---- inventories: Sika's sites and partner warehouses --------------------
    stock = []
    for node in nodes.values():
        if node.kind.value not in STOCK_KINDS:
            continue
        dist = _nearest(Point(node.lat, node.lon), reach_from)
        if dist > STOCK_RADIUS_KM and node.id not in on_route:
            continue
        event = disrupted.get(node.id)
        stock.append({
            "id": node.id, "name": node.name, "kind": "sika_" + node.kind.value, "role": "sika",
            "lat": node.lat, "lon": node.lon, "distance_km": round(dist),
            "modes": [m.value for m in node.modes],
            "available": event is None,
            "detail": event or ("Sika plant" if node.kind.value == "plant" else "Sika distribution centre"),
            "contact": {},
        })
    everyone = partners(context)
    for p in everyone:
        if p["kind"] not in WAREHOUSE_KINDS:
            continue
        dist = _nearest(Point(p["lat"], p["lon"]), reach_from)
        if dist > STOCK_RADIUS_KM:
            continue
        free, words = _capacity(p)
        stock.append({
            "id": p["id"], "name": p["name"], "kind": "warehouse", "role": "partner",
            "lat": p["lat"], "lon": p["lon"], "distance_km": round(dist),
            "modes": p["modes"], "available": free, "detail": words,
            "contact": {k: v for k, v in (p.get("contact") or {}).items() if v},
        })
    stock.sort(key=lambda s: s["distance_km"])
    stock = stock[:MAX_STOCK]

    # ---- local vendors -------------------------------------------------------
    vendors = []
    for p in everyone:
        if p["kind"] in WAREHOUSE_KINDS:
            continue
        dist = _nearest(Point(p["lat"], p["lon"]), reach_from)
        if dist > max(VENDOR_RADIUS_KM, p["service_radius_km"]):
            continue
        free, words = _capacity(p)
        vendors.append({
            "id": p["id"], "name": p["name"], "kind": p["kind"], "role": "vendor",
            "lat": p["lat"], "lon": p["lon"], "distance_km": round(dist),
            "modes": p["modes"], "available": free, "detail": words,
            "within_service": dist <= p["service_radius_km"],
            "contact": {k: v for k, v in (p.get("contact") or {}).items() if v},
        })
    vendors.sort(key=lambda v: v["distance_km"])
    vendors = vendors[:MAX_VENDORS]

    # ---- road and rail from where the freight can next touch land ------------
    modes = cfg["modes"]
    # Rail needs rail at both ends: the next port has it, or the freight is
    # already on land (a vessel at sea is taken to its next port above).
    if origin["node_id"] is not None:
        here_rail = "rail" in [m.value for m in nodes[origin["node_id"]].modes]
    else:
        here_rail = anchors[0]["mode"] != "sea"
    targets = [t for t in [*ports, *stock]
               if t["id"] != origin.get("node_id")
               and 5.0 < _km(here, Point(t["lat"], t["lon"])) <= LINK_KM]
    targets.sort(key=lambda t: _km(here, Point(t["lat"], t["lon"])))
    links = []
    for t in targets[:MAX_LINKS]:
        end = Point(t["lat"], t["lon"])
        path = great_circle_points(here, end, segments=10)
        straight = _km(here, end)
        for mode in ("road", "rail"):
            if mode == "rail" and not (here_rail and "rail" in t["modes"]):
                continue
            m = modes.get(mode, modes["road"])
            km = straight * float(m["detour_factor"])
            links.append({
                "id": f"{mode}:{t['id']}", "mode": mode, "to": t["id"], "to_name": t["name"],
                "km": round(km), "hours": round(km / float(m["speed_kmh"]), 1),
                "path": [[round(p.lat, 4), round(p.lon, 4)] for p in path],
                "routed_by": "corridor estimate",
            })

    # ---- nearby and available ------------------------------------------------
    nearby = [x["id"] for x in [*ports, *stock, *vendors]
              if x["distance_km"] <= REACH_KM and x["available"]]

    return {
        "scope": scope,
        "anchors": [{"id": a["id"], "name": a["name"], "mode": a["mode"], "lat": a["lat"],
                     "lon": a["lon"], "status": a["status"]} for a in anchors],
        "origin": origin,
        "reach_km": REACH_KM,
        "layers": {"ports": ports, "inventories": stock, "vendors": vendors,
                   "links": links, "nearby": nearby},
        "counts": {"ports": len(ports), "inventories": len(stock), "vendors": len(vendors),
                   "links": len(links), "nearby": len(nearby)},
        "synthetic": True,
    }
