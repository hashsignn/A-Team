"""Alternate routes and local fallback, generated rather than declared.

The old playbook could only offer what somebody had written down: a template
per mode, and ``alternatives:`` hand-listed on a node. That is fine for the
disruptions you have already met. It has nothing to say the first time a node
fails that nobody anticipated, which is the case that costs the most.

So this module searches instead. It builds a directed graph out of every leg
any configured lane uses, plus every declared node alternative as a lateral
hop, weights each edge in HOURS, and runs Dijkstra from where the freight
actually is to where it has to be — with the failed nodes deleted from the
graph. What comes back is a path with a real duration, not a label.

WHY HOURS AND NOT FRANCS
------------------------
Weighting by cost and then reporting the time is the mistake this rebuild
exists to undo: it finds the cheapest path and calls it the answer. Weighting
by time finds the fastest path, and money is then applied once, as a veto, by
``margin.py``. The two are not interchangeable — the cheapest path and the
fastest path are different paths, and on a broken lane they are very different.

THE FALLBACK
------------
When no path survives, the freight is not lost; it is stuck somewhere with a
phone signal. ``local_options`` asks a narrower question — who is near enough
to take this over today — and answers it from the vendor directory plus plain
geography. A 3PL handover is expensive by construction, which is correct: it
should only survive the margin veto when the alternative is a missed date on
a consignment worth protecting.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from engine.config import Config
from engine.network.geo import Point, haversine_km
from engine.network.graph import Network
from engine.schemas import Mode

FALLBACK_SPEED_KPH: dict[str, float] = {
    "road": 62.0, "rail": 45.0, "barge": 13.0, "sea": 31.0,
}
FALLBACK_HANDLING_HOURS = 6.0
FALLBACK_MAX_HOPS = 6
FALLBACK_MAX_PATHS = 3
FALLBACK_VENDOR_RADIUS_KM = 250.0
FALLBACK_VENDOR_HANDOVER_HOURS = 6.0
FALLBACK_COST_PER_KM: dict[str, float] = {
    "road": 1.85, "rail": 0.95, "barge": 0.42, "sea": 0.11,
}
FALLBACK_TRANSFER_CHF = 420.0


@dataclass(frozen=True)
class Hop:
    from_node: str
    to_node: str
    mode: Mode
    distance_km: float
    hours: float
    cost_chf: float


@dataclass(frozen=True)
class Path:
    """One way of getting there, priced in hours."""

    node_ids: tuple[str, ...]
    hops: tuple[Hop, ...]
    hours: float
    modes: tuple[Mode, ...]
    cost_chf: float = 0.0

    @property
    def distance_km(self) -> float:
        return round(sum(h.distance_km for h in self.hops), 1)

    def describe(self, network: Network) -> str:
        names = [network.node(n).name for n in self.node_ids]
        return " → ".join(names)


@dataclass(frozen=True)
class LocalOption:
    """Somebody near the freight who can take it on."""

    vendor: str
    service: str
    phone: str
    at_node: str
    at_node_name: str
    distance_km: float
    handover_hours: float
    cost_chf: float


@dataclass
class _Graph:
    edges: dict[str, list[Hop]] = field(default_factory=dict)

    def add(self, hop: Hop) -> None:
        self.edges.setdefault(hop.from_node, []).append(hop)


def _fast(config: Config) -> dict:
    raw = config.raw("fast")
    return raw if isinstance(raw, dict) else {}


def _value(block: dict, key: str, fallback: float) -> float:
    """Read a `{value: x, source: y}` entry, or a bare number, or the fallback."""
    entry = block.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("value"), (int, float)):
        return float(entry["value"])
    if isinstance(entry, (int, float)):
        return float(entry)
    return fallback


def speed_kph(config: Config, mode: Mode) -> float:
    block = _fast(config).get("transit", {}).get("speed_kph", {}) or {}
    return _value(block, mode.value, FALLBACK_SPEED_KPH.get(mode.value, 50.0))


def cost_per_km(config: Config, mode: Mode) -> float:
    block = _fast(config).get("transit", {}).get("cost_chf_per_km", {}) or {}
    return _value(block, mode.value, FALLBACK_COST_PER_KM.get(mode.value, 1.0))


def transfer_chf(config: Config) -> float:
    block = _fast(config).get("transit", {}).get("transfer_chf", {}) or {}
    return _value(block, "default", FALLBACK_TRANSFER_CHF)


def handling_hours(config: Config, network: Network, node_id: str) -> float:
    block = _fast(config).get("transit", {}).get("handling_hours", {}) or {}
    try:
        kind = network.node(node_id).kind.value
    except (KeyError, AttributeError):
        kind = "default"
    default = _value(block, "default", FALLBACK_HANDLING_HOURS)
    return _value(block, kind, default)


# Building the graph walks every leg geometry, which is the expensive part of
# this module. The result depends only on the config and the network, both of
# which are immutable for the life of a run, so it is cached per pair. Without
# this, ranking options for 200 consignments rebuilds the same graph 200 times.
_GRAPH_CACHE: dict[tuple[int, int], _Graph] = {}


def build_graph(config: Config, network: Network) -> _Graph:
    """Every hop the network is known to support, in both directions.

    Both directions deliberately: a lane declares Basel → Rotterdam, but a
    consignment diverted off the river may well need Duisburg → Basel to reach
    a railhead. Refusing to consider the reverse of a leg we already run is an
    artificial constraint, not a real one.
    """
    key = (id(config), id(network))
    cached = _GRAPH_CACHE.get(key)
    if cached is not None:
        return cached

    graph = _Graph()
    seen: set[tuple[str, str, str]] = set()
    transfer = transfer_chf(config)

    def add_pair(a: str, b: str, mode: Mode) -> None:
        for src, dst in ((a, b), (b, a)):
            key = (src, dst, mode.value)
            if key in seen or src == dst:
                continue
            if src not in network.nodes or dst not in network.nodes:
                continue
            seen.add(key)
            geometry = network.geometry(src, dst, mode)
            hours = geometry.distance_km / max(speed_kph(config, mode), 1.0)
            hours += handling_hours(config, network, dst)
            # The transfer fee rides on the hop rather than on the node, so a
            # path with more changes is dearer as well as slower. It is charged
            # on arrival, destination included: unloading at the end is work too.
            cost = geometry.distance_km * cost_per_km(config, mode) + transfer

            graph.add(
                Hop(src, dst, mode, round(geometry.distance_km, 1),
                    round(hours, 2), round(cost, 2))
            )

    for lane in config.lanes:
        for leg in lane["legs"]:
            add_pair(leg["from"], leg["to"], Mode(leg["mode"]))

    # A declared alternative is a substitution, and substituting means moving
    # the freight there. Road is the only mode that can be assumed between two
    # arbitrary points, so that is what the lateral hop uses.
    for node_id, node in network.nodes.items():
        for alt in node.alternatives:
            if alt in network.nodes:
                add_pair(node_id, alt, Mode.ROAD)

    _GRAPH_CACHE[key] = graph
    return graph


def fastest_path(
    graph: _Graph,
    origin: str,
    destination: str,
    blocked: set[str],
    max_hops: int,
    banned_hops: set[tuple[str, str]] | None = None,
) -> Path | None:
    """Dijkstra on hours, with nodes deleted rather than penalised.

    A blocked node is removed from the search, not given a large weight. A
    weight says "expensive but possible"; a closed lock is not expensive, it is
    shut, and a path that routes through it is not a slower answer but a wrong
    one.
    """
    if origin in blocked or destination in blocked:
        return None
    if origin == destination:
        return Path((origin,), (), 0.0, ())

    banned = banned_hops or set()
    # (hours, hop count, node, path so far, hops so far)
    queue: list[tuple[float, int, str, tuple[str, ...], tuple[Hop, ...]]] = [
        (0.0, 0, origin, (origin,), ())
    ]
    best: dict[str, float] = {origin: 0.0}

    while queue:
        hours, hops_used, node, seen_nodes, hops = heapq.heappop(queue)
        if node == destination:
            return Path(
                seen_nodes, hops, round(hours, 2), tuple(h.mode for h in hops),
                cost_chf=round(sum(h.cost_chf for h in hops), 2),
            )
        if hops_used >= max_hops:
            continue
        for hop in graph.edges.get(node, []):
            if hop.to_node in blocked or hop.to_node in seen_nodes:
                continue
            if (hop.from_node, hop.to_node) in banned:
                continue
            total = hours + hop.hours
            if total >= best.get(hop.to_node, float("inf")):
                continue
            best[hop.to_node] = total
            heapq.heappush(
                queue,
                (total, hops_used + 1, hop.to_node,
                 seen_nodes + (hop.to_node,), hops + (hop,)),
            )

    return None


def alternatives(
    config: Config,
    network: Network,
    origin: str,
    destination: str,
    blocked: set[str],
) -> list[Path]:
    """Up to `max_paths` genuinely different ways round the blockage.

    Distinct is enforced by banning one hop of the previous winner at a time
    (a cut-down Yen). Without it the second-best path is usually the best one
    with a single node swapped, which is not a second option a planner can use.
    """
    settings = _fast(config).get("contingency", {}) or {}
    max_hops = int(settings.get("max_hops", FALLBACK_MAX_HOPS))
    max_paths = int(settings.get("max_paths", FALLBACK_MAX_PATHS))

    graph = build_graph(config, network)
    first = fastest_path(graph, origin, destination, blocked, max_hops)
    if first is None:
        return []

    found = [first]
    for hop in first.hops:
        if len(found) >= max_paths:
            break
        candidate = fastest_path(
            graph, origin, destination, blocked, max_hops,
            banned_hops={(hop.from_node, hop.to_node)},
        )
        if candidate is None:
            continue
        if any(candidate.node_ids == p.node_ids for p in found):
            continue
        found.append(candidate)

    found.sort(key=lambda p: p.hours)
    return found[:max_paths]


def local_options(
    config: Config,
    network: Network,
    near: Point,
    value_chf: float,
) -> list[LocalOption]:
    """Vendors and 3PLs close enough to take the freight over today.

    Ranked by distance, because in an emergency handover proximity is the
    variable that decides whether it happens at all. Cost is reported so the
    margin veto can do its job; it is not what orders the list.
    """
    settings = _fast(config).get("contingency", {}) or {}
    radius = float(settings.get("vendor_radius_km", FALLBACK_VENDOR_RADIUS_KM))
    handover = _value(
        settings, "vendor_handover_hours", FALLBACK_VENDOR_HANDOVER_HOURS
    )
    cost_cfg = settings.get("vendor_cost", {}) or {}
    callout = _value(cost_cfg, "callout_chf", 2400.0)
    fraction = _value(cost_cfg, "fraction_of_value", 0.06)
    cost = round(callout + float(value_chf) * fraction, 2)

    directory = config.contacts.get("local_vendors", {}) or {}
    out: list[LocalOption] = []

    for node_id, vendors in directory.items():
        if node_id not in network.nodes:
            continue
        distance = haversine_km(near, network.point(node_id))
        if distance > radius:
            continue
        node = network.node(node_id)
        for vendor in vendors:
            out.append(
                LocalOption(
                    vendor=vendor.get("name", "unnamed vendor"),
                    service=vendor.get("service", "unspecified"),
                    phone=vendor.get("phone", ""),
                    at_node=node_id,
                    at_node_name=node.name,
                    distance_km=round(distance, 1),
                    handover_hours=handover,
                    cost_chf=cost,
                )
            )

    out.sort(key=lambda v: (v.distance_km, v.vendor))
    return out
