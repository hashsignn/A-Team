"""A small, explicit sea-lane graph, for routes nobody planned.

WHY A GRAPH AND NOT MORE WAYPOINTS
==================================
engine/network/graph.py draws the PLANNED sea legs through declared waypoints,
one list per node pair. That is right for a fixed lane set and does not scale
to recovery: "Suez is cut" means every Asia lane needs a Cape route from
wherever each vessel happens to be, and a waypoint list per (position,
destination, closure) is not a list anybody can maintain.

So recovery routing is a shortest path on a graph: the sea-capable nodes of
the network (ports and chokepoints) plus open-water waypoints placed to keep
great-circle edges off land. A closure is a node REMOVED from the graph.
Bypassing Suez is then not a rule somebody wrote — it is what Dijkstra returns
when CHOKE_SUEZ is gone, and "Jebel Ali has no sea alternative" is what it
returns when CHOKE_HORMUZ is gone and there is no path left at all.

Explicit and auditable, like SEA_WAYPOINTS: every coordinate is here, and a
planner can check any of them against a chart. `searoute` would be finer and
is optional; this needs nothing installed and is the same on every machine.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from engine.network.geo import Point, great_circle_points, haversine_km

# Open-water waypoints. Placed so the great circle between neighbours stays
# at sea at the zoom a planner reads a map at — not surveyed routes.
WAYPOINTS: dict[str, tuple[float, float]] = {
    # North Sea and Channel
    "WP_NORTHSEA_S": (51.90, 3.00),
    "WP_SCHELDT": (51.45, 3.50),
    "WP_FRISIAN": (53.70, 4.80),
    "WP_ELBE": (53.95, 8.30),
    "WP_DOVER": (51.00, 1.45),
    "WP_CHANNEL_M": (50.10, -0.80),
    "WP_CHANNEL_W": (49.80, -3.50),
    "WP_USHANT": (48.50, -5.90),
    "WP_FINISTERRE": (43.20, -9.90),
    "WP_LISBON": (38.70, -9.90),
    # Mediterranean
    "WP_ALBORAN": (36.00, -3.00),
    "WP_PALOS": (37.30, -0.30),
    "WP_BALEARIC_S": (38.30, 1.80),
    "WP_BCN": (41.00, 2.80),
    "WP_ALGERIA_N": (37.50, 3.00),
    "WP_LIGURIAN": (43.90, 8.60),
    "WP_CORSICA_W": (42.00, 8.00),
    "WP_SARDINIA_W": (40.00, 7.90),
    "WP_SARDINIA_S": (38.50, 8.90),
    "WP_SICILY_CH": (37.40, 11.30),
    "WP_MALTA_S": (35.60, 14.80),
    "WP_CRETE_S": (34.40, 24.50),
    "WP_PORTSAID": (31.60, 32.30),
    # Red Sea, Arabian Sea, Gulf
    "WP_REDSEA_N": (27.50, 34.00),
    "WP_REDSEA_M": (20.00, 38.50),
    "WP_ADEN": (12.20, 46.00),
    "WP_SOCOTRA_N": (13.50, 54.00),
    "WP_ARABIAN": (12.00, 60.00),
    "WP_RAS_HADD": (22.80, 60.20),
    "WP_GULF_OMAN": (25.00, 57.50),
    "WP_GULF_W": (26.30, 55.60),
    # Indian Ocean to the Far East
    "WP_LACCADIVE": (8.00, 73.50),
    "WP_DONDRA": (5.20, 81.00),
    "WP_MALACCA_N": (5.90, 97.80),
    "WP_SCS_S": (3.50, 107.50),
    "WP_SCS": (12.00, 112.00),
    "WP_SCS_N": (19.00, 116.00),
    "WP_TAIWAN_ST": (24.50, 119.80),
    "WP_ZHEJIANG": (27.50, 122.00),
    "WP_JAVASEA": (-5.20, 107.20),
    "WP_KARIMATA": (-1.00, 108.00),
    "WP_SUNDA_W": (-7.50, 103.50),
    # Around Africa
    "WP_MOROCCO_W": (33.50, -10.00),
    "WP_CANARY_W": (28.00, -19.50),
    "WP_CAPEVERDE": (15.00, -20.80),
    "WP_GUINEA_W": (4.50, -14.00),
    "WP_GUINEA_S": (-5.00, 3.00),
    "WP_NAMIBIA": (-22.00, 11.00),
    "WP_AGULHAS": (-36.50, 21.00),
    "WP_SIO": (-30.00, 42.00),
    "WP_MASCARENE": (-17.50, 55.00),
    "WP_CIO": (-10.00, 80.00),
    # Atlantic and the Americas
    "WP_NATL_E": (46.00, -20.00),
    "WP_NATL_W": (41.50, -50.00),
    "WP_NY_APP": (40.30, -73.20),
    "WP_NORFOLK_APP": (36.95, -75.80),
    "WP_AZORES": (38.00, -25.00),
    "WP_BAHAMAS_N": (27.50, -76.00),
    "WP_FLORIDA_E": (26.20, -79.70),
    "WP_FLORIDA_ST": (24.20, -81.50),
    "WP_GULF_MX": (26.50, -89.00),
    "WP_ANEGADA": (18.90, -63.80),
    "WP_CARIB": (14.50, -75.00),
    "WP_YUCATAN": (21.50, -85.80),
    "WP_PAN_PAC": (7.80, -79.40),
    "WP_TEHUANTEPEC": (13.00, -95.00),
    "WP_BAJA": (21.50, -110.50),
    "WP_BAJA_W": (28.00, -116.20),
}

# Undirected edges. Network node ids (ports, chokepoints) are graph nodes too,
# which is what lets a closure named on a node remove it from the graph.
EDGES: tuple[tuple[str, str], ...] = (
    ("NLRTM", "WP_NORTHSEA_S"), ("BEANR", "WP_SCHELDT"), ("WP_SCHELDT", "WP_NORTHSEA_S"),
    ("GBFXT", "WP_NORTHSEA_S"), ("WP_NORTHSEA_S", "WP_DOVER"), ("DEHAM", "WP_ELBE"),
    ("WP_ELBE", "WP_FRISIAN"), ("WP_FRISIAN", "WP_NORTHSEA_S"),
    ("WP_DOVER", "WP_CHANNEL_M"), ("GBSOU", "WP_CHANNEL_M"), ("FRLEH", "WP_CHANNEL_M"),
    ("WP_CHANNEL_M", "WP_CHANNEL_W"), ("WP_CHANNEL_W", "WP_USHANT"),
    ("WP_USHANT", "WP_FINISTERRE"), ("WP_FINISTERRE", "WP_LISBON"),
    ("WP_LISBON", "CHOKE_GIB"),
    # Mediterranean
    ("CHOKE_GIB", "WP_ALBORAN"), ("WP_ALBORAN", "WP_PALOS"), ("WP_PALOS", "ESVLC"),
    ("ESVLC", "WP_BALEARIC_S"), ("WP_ALBORAN", "WP_ALGERIA_N"),
    ("WP_BALEARIC_S", "WP_ALGERIA_N"), ("WP_BALEARIC_S", "WP_SARDINIA_S"),
    ("ESBCN", "WP_BCN"), ("WP_BCN", "WP_BALEARIC_S"), ("WP_BCN", "WP_LIGURIAN"),
    ("WP_ALGERIA_N", "WP_SARDINIA_S"), ("ITGOA", "WP_LIGURIAN"), ("ITSPE", "WP_LIGURIAN"),
    ("WP_LIGURIAN", "WP_CORSICA_W"), ("WP_CORSICA_W", "WP_SARDINIA_W"),
    ("WP_SARDINIA_W", "WP_SARDINIA_S"), ("WP_SARDINIA_S", "WP_SICILY_CH"),
    ("WP_SICILY_CH", "WP_MALTA_S"), ("WP_MALTA_S", "WP_CRETE_S"),
    ("WP_CRETE_S", "WP_PORTSAID"), ("WP_PORTSAID", "CHOKE_SUEZ"),
    # Red Sea, Arabian Sea, Gulf
    ("CHOKE_SUEZ", "WP_REDSEA_N"), ("WP_REDSEA_N", "WP_REDSEA_M"),
    ("WP_REDSEA_M", "CHOKE_BAB"), ("CHOKE_BAB", "WP_ADEN"), ("WP_ADEN", "WP_SOCOTRA_N"),
    ("WP_SOCOTRA_N", "WP_ARABIAN"), ("WP_ARABIAN", "WP_RAS_HADD"),
    ("WP_RAS_HADD", "WP_GULF_OMAN"), ("WP_GULF_OMAN", "CHOKE_HORMUZ"),
    ("CHOKE_HORMUZ", "WP_GULF_W"), ("WP_GULF_W", "AEJEA"),
    # Indian Ocean to the Far East
    ("WP_ARABIAN", "WP_LACCADIVE"), ("WP_LACCADIVE", "WP_DONDRA"),
    ("WP_DONDRA", "WP_MALACCA_N"), ("WP_MALACCA_N", "CHOKE_MALACCA"),
    ("CHOKE_MALACCA", "MYPKG"), ("CHOKE_MALACCA", "SGSIN"), ("MYPKG", "SGSIN"),
    ("SGSIN", "WP_SCS_S"), ("WP_SCS_S", "WP_SCS"), ("WP_SCS", "WP_SCS_N"),
    ("WP_SCS_N", "WP_TAIWAN_ST"), ("WP_TAIWAN_ST", "WP_ZHEJIANG"),
    ("WP_ZHEJIANG", "CNNGB"), ("WP_ZHEJIANG", "CNSHA"), ("CNNGB", "CNSHA"),
    ("CHOKE_SUNDA", "WP_SUNDA_W"), ("CHOKE_SUNDA", "WP_JAVASEA"),
    ("WP_JAVASEA", "WP_KARIMATA"), ("WP_KARIMATA", "WP_SCS_S"),
    # Around Africa
    ("CHOKE_GIB", "WP_MOROCCO_W"), ("WP_LISBON", "WP_MOROCCO_W"),
    ("WP_MOROCCO_W", "WP_CANARY_W"), ("WP_CANARY_W", "WP_CAPEVERDE"),
    ("WP_CAPEVERDE", "WP_GUINEA_W"), ("WP_GUINEA_W", "WP_GUINEA_S"),
    ("WP_GUINEA_S", "WP_NAMIBIA"), ("WP_NAMIBIA", "CHOKE_GOODHOPE"),
    ("CHOKE_GOODHOPE", "WP_AGULHAS"), ("WP_AGULHAS", "WP_SIO"),
    ("WP_SIO", "WP_MASCARENE"), ("WP_SIO", "WP_CIO"), ("WP_MASCARENE", "WP_ARABIAN"),
    ("WP_MASCARENE", "WP_CIO"), ("WP_CIO", "WP_MALACCA_N"), ("WP_CIO", "WP_SUNDA_W"),
    ("WP_CIO", "WP_DONDRA"),
    # Atlantic and the Americas
    ("WP_USHANT", "WP_NATL_E"), ("WP_NATL_E", "WP_NATL_W"), ("WP_NATL_E", "WP_AZORES"),
    ("WP_LISBON", "WP_AZORES"), ("WP_NATL_W", "WP_NY_APP"), ("WP_NY_APP", "USNYC"),
    ("WP_NATL_W", "WP_NORFOLK_APP"), ("WP_NY_APP", "WP_NORFOLK_APP"),
    ("WP_NORFOLK_APP", "USORF"), ("WP_AZORES", "WP_NATL_W"),
    ("WP_AZORES", "WP_BAHAMAS_N"), ("WP_NORFOLK_APP", "WP_BAHAMAS_N"),
    ("WP_BAHAMAS_N", "WP_FLORIDA_E"), ("WP_FLORIDA_E", "WP_FLORIDA_ST"),
    ("WP_FLORIDA_ST", "WP_GULF_MX"), ("WP_GULF_MX", "USHOU"),
    ("WP_AZORES", "WP_ANEGADA"), ("WP_ANEGADA", "WP_CARIB"),
    ("WP_CARIB", "CHOKE_PANAMA"), ("WP_CARIB", "WP_YUCATAN"), ("WP_YUCATAN", "WP_GULF_MX"),
    ("CHOKE_PANAMA", "WP_PAN_PAC"), ("WP_PAN_PAC", "WP_TEHUANTEPEC"),
    ("WP_TEHUANTEPEC", "WP_BAJA"), ("WP_BAJA", "WP_BAJA_W"), ("WP_BAJA_W", "USLAX"),
)


@dataclass(frozen=True)
class SeaPath:
    """A route through the graph, and what it passes."""

    node_ids: list[str]
    path: list[Point]
    distance_km: float

    def passes(self, node_id: str) -> bool:
        return node_id in self.node_ids


class SeaGraph:
    """The lanes, with network nodes placed at their configured coordinates."""

    def __init__(self, nodes: dict) -> None:
        self.coords: dict[str, Point] = {
            k: Point(lat, lon) for k, (lat, lon) in WAYPOINTS.items()
        }
        for node_id, node in nodes.items():
            if any(getattr(m, "value", m) == "sea" for m in node.modes):
                self.coords[node_id] = Point(node.lat, node.lon)

        self.adj: dict[str, dict[str, float]] = {}
        for a, b in EDGES:
            if a not in self.coords or b not in self.coords:
                continue    # a customer network without that port simply lacks the edge
            d = haversine_km(self.coords[a], self.coords[b])
            self.adj.setdefault(a, {})[b] = d
            self.adj.setdefault(b, {})[a] = d

    def has(self, node_id: str) -> bool:
        return node_id in self.adj

    def route(
        self,
        start: str,
        end: str,
        avoid: set[str] | frozenset[str] = frozenset(),
        start_point: Point | None = None,
        start_links: dict[str, list[Point]] | None = None,
    ) -> SeaPath | None:
        """Shortest path from *start* to *end* that never enters *avoid*.

        ``start_point`` + ``start_links`` let the path begin mid-leg: the
        vessel is somewhere between two nodes, and each link is the actual
        geometry from the vessel to that node along the leg it is on. Joining
        the graph along the leg rather than by a straight line is what stops
        a vessel in the Mediterranean being routed overland to the nearest
        waypoint.

        None when there is no path. That is an answer, not an error: Jebel
        Ali with Hormuz closed has no sea route, and saying so is the point.
        """
        if end in avoid or not self.has(end):
            return None

        links: dict[str, tuple[float, list[Point]]] = {}
        if start_point is not None and start_links:
            for node_id, geometry in start_links.items():
                if node_id in avoid or not self.has(node_id):
                    continue
                d = sum(haversine_km(p, q) for p, q in zip(geometry, geometry[1:], strict=False))
                links[node_id] = (d, geometry)
            if not links:
                return None
            origin = "__START__"
        else:
            if start in avoid or not self.has(start):
                return None
            origin = start

        dist: dict[str, float] = {origin: 0.0}
        prev: dict[str, str] = {}
        queue: list[tuple[float, str]] = [(0.0, origin)]
        while queue:
            d, node = heapq.heappop(queue)
            if node == end:
                break
            if d > dist.get(node, float("inf")):
                continue
            neighbours = (
                {k: v[0] for k, v in links.items()} if node == "__START__"
                else self.adj.get(node, {})
            )
            for nxt, w in neighbours.items():
                if nxt in avoid:
                    continue
                nd = d + w
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    prev[nxt] = node
                    heapq.heappush(queue, (nd, nxt))

        if end not in dist:
            return None

        chain = [end]
        while chain[-1] != origin:
            chain.append(prev[chain[-1]])
        chain.reverse()

        path: list[Point] = []
        for a, b in zip(chain, chain[1:], strict=False):
            if a == "__START__":
                segment = links[b][1]
            else:
                segment = great_circle_points(self.coords[a], self.coords[b], segments=6)
            path.extend(segment if not path else segment[1:])

        node_ids = [n for n in chain if n != "__START__"]
        return SeaPath(node_ids=node_ids, path=path, distance_km=dist[end])
