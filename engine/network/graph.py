"""The freight network: nodes, lanes, and the geometry each leg follows.

BRIEF §5.8 is right that "supply chain is a DAG" is wrong and should not be
said out loud. The general network is not acyclic — a port can be an origin on
one lane and a transshipment point on another. What *is* true, and what this
module encodes, is narrower and more useful: **each shipment is an ordered
sequence of legs; the network is a directed graph.**
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.config import Config
from engine.network.geo import (
    CORRIDOR_WIDTH_KM,
    Point,
    bounding_box,
    great_circle_points,
    haversine_km,
    on_corridor,
)
from engine.schemas import Mode, Node

# Waypoints inserted between two nodes when the direct great circle would cross
# land or miss the chokepoint a vessel actually transits. Keyed by the
# unordered node pair. Explicit and auditable — a planner can check these.
SEA_WAYPOINTS: dict[frozenset[str], list[tuple[float, float]]] = {
    frozenset({"CHOKE_GIB", "CHOKE_SUEZ"}): [(37.0, 5.0), (34.5, 18.0), (32.0, 28.0)],
    frozenset({"DEHAM", "CHOKE_GIB"}): [(51.0, 2.0), (48.5, -5.0), (43.0, -9.5), (37.0, -9.0)],
    frozenset({"NLRTM", "CHOKE_GIB"}): [(51.0, 2.0), (48.0, -5.5), (43.0, -9.5), (37.0, -9.0)],
    frozenset({"BEANR", "CHOKE_GIB"}): [(51.2, 2.0), (48.0, -5.5), (43.0, -9.5), (37.0, -9.0)],
    frozenset({"FRLEH", "CHOKE_PANAMA"}): [(49.0, -5.0), (43.0, -20.0), (30.0, -45.0), (20.0, -65.0)],
    frozenset({"CHOKE_SUEZ", "CHOKE_BAB"}): [(27.5, 34.0), (20.0, 38.5), (14.0, 42.5)],
    frozenset({"CHOKE_BAB", "CHOKE_MALACCA"}): [(12.0, 45.0), (10.0, 60.0), (6.0, 80.0), (5.0, 95.0)],
    frozenset({"CHOKE_BAB", "SGSIN"}): [(12.0, 45.0), (10.0, 60.0), (6.0, 80.0), (3.0, 97.0), (1.5, 102.5)],
    frozenset({"CHOKE_BAB", "MYPKG"}): [(12.0, 45.0), (10.0, 60.0), (6.0, 80.0), (4.0, 96.0)],
    frozenset({"CHOKE_MALACCA", "CNSHA"}): [(3.0, 104.5), (10.0, 110.0), (20.0, 115.0), (26.0, 120.0)],
    frozenset({"CHOKE_MALACCA", "CNNGB"}): [(3.0, 104.5), (10.0, 110.0), (20.0, 115.0), (26.0, 120.5)],
    frozenset({"CHOKE_SUEZ", "CHOKE_MALACCA"}): [
        (27.5, 34.0), (14.0, 42.5), (12.0, 45.0), (10.0, 60.0), (6.0, 80.0), (5.0, 95.0),
    ],
    frozenset({"CHOKE_SUEZ", "AEJEA"}): [(27.5, 34.0), (14.0, 42.5), (12.5, 52.0), (20.0, 60.0), (25.0, 57.0)],
    frozenset({"ITGOA", "CHOKE_SUEZ"}): [(40.0, 10.0), (35.0, 18.0), (32.5, 28.0)],
    frozenset({"ESVLC", "CHOKE_SUEZ"}): [(37.5, 2.0), (35.5, 12.0), (32.5, 26.0)],
    frozenset({"BEANR", "USNYC"}): [(51.0, 2.0), (49.5, -6.0), (46.0, -25.0), (42.0, -50.0)],
    frozenset({"NLRTM", "USHOU"}): [(51.0, 2.0), (49.0, -8.0), (40.0, -35.0), (28.0, -70.0), (24.5, -82.0)],
    frozenset({"NLRTM", "USORF"}): [(51.0, 2.0), (49.0, -8.0), (43.0, -30.0), (38.0, -60.0)],
    frozenset({"CHOKE_PANAMA", "USLAX"}): [(10.5, -87.0), (16.0, -100.0), (24.0, -112.0)],
    frozenset({"BEANR", "GBFXT"}): [(51.6, 3.0)],
    frozenset({"ESBCN", "ITGOA"}): [(42.0, 5.0)],
}

# Barge follows the river, not a great circle. Rhine waypoints, upstream first.
RIVER_WAYPOINTS: dict[frozenset[str], list[tuple[float, float]]] = {
    frozenset({"CHBSL", "GAUGE_KAUB"}): [
        (47.8, 7.6), (48.6, 7.8), (49.0, 8.4), (49.5, 8.45), (49.9, 8.3), (50.0, 8.0),
    ],
    frozenset({"GAUGE_KAUB", "DEDUI"}): [
        (50.3, 7.6), (50.7, 7.15), (50.94, 6.96), (51.2, 6.75),
    ],
    frozenset({"GAUGE_KAUB", "NLRTM"}): [
        (50.3, 7.6), (50.7, 7.15), (51.2, 6.75), (51.5, 6.1), (51.85, 5.2), (51.9, 4.5),
    ],
    frozenset({"DEDUI", "NLRTM"}): [(51.5, 6.1), (51.85, 5.2), (51.9, 4.5)],
}


@dataclass(frozen=True)
class LegGeometry:
    """The path a leg follows, and the corridor around it."""

    from_node: str
    to_node: str
    mode: Mode
    path: list[Point]
    corridor_km: float
    distance_km: float
    routed_by: str  # "river" | "sea_waypoints" | "searoute" | "great_circle"

    def touches(self, point: Point) -> tuple[bool, float]:
        return on_corridor(point, self.path, self.mode.value, self.corridor_km)


class Network:
    """Nodes plus per-leg geometry, built once per run."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.nodes: dict[str, Node] = config.nodes
        self._geometry: dict[tuple[str, str, str], LegGeometry] = {}
        self._searoute_failed = False
        self._bounds: tuple[float, float, float, float] | None = None

    # ---------------------------------------------------------------
    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown node {node_id!r}") from exc

    def point(self, node_id: str) -> Point:
        node = self.node(node_id)
        return Point(lat=node.lat, lon=node.lon)

    # ---------------------------------------------------------------
    def geometry(self, from_node: str, to_node: str, mode: Mode) -> LegGeometry:
        key = (from_node, to_node, mode.value)
        if key not in self._geometry:
            self._geometry[key] = self._build_geometry(from_node, to_node, mode)
        return self._geometry[key]

    def _build_geometry(self, from_node: str, to_node: str, mode: Mode) -> LegGeometry:
        a, b = self.point(from_node), self.point(to_node)
        pair = frozenset({from_node, to_node})

        if mode is Mode.BARGE and pair in RIVER_WAYPOINTS:
            path = self._through_waypoints(a, b, RIVER_WAYPOINTS[pair], from_node, to_node)
            routed_by = "river"
        elif mode is Mode.SEA and pair in SEA_WAYPOINTS:
            path = self._through_waypoints(a, b, SEA_WAYPOINTS[pair], from_node, to_node)
            routed_by = "sea_waypoints"
        elif mode is Mode.SEA:
            path, routed_by = self._sea_route(a, b)
        else:
            path = great_circle_points(a, b, segments=12)
            routed_by = "great_circle"

        distance = sum(
            haversine_km(p, q) for p, q in zip(path, path[1:])
        )
        return LegGeometry(
            from_node=from_node,
            to_node=to_node,
            mode=mode,
            path=path,
            corridor_km=CORRIDOR_WIDTH_KM.get(mode.value, 60.0),
            distance_km=distance,
            routed_by=routed_by,
        )

    @staticmethod
    def _through_waypoints(
        a: Point,
        b: Point,
        waypoints: list[tuple[float, float]],
        from_node: str,
        to_node: str,
    ) -> list[Point]:
        """Chain a -> waypoints -> b, ordering the waypoints for this direction.

        The waypoint lists are stored against an unordered pair, so a lane
        running the other way reuses the same geometry reversed rather than
        needing a second entry that can drift out of step.
        """
        pts = [Point(lat, lon) for lat, lon in waypoints]
        if haversine_km(a, pts[0]) > haversine_km(a, pts[-1]):
            pts.reverse()

        chain = [a, *pts, b]
        path: list[Point] = [chain[0]]
        for start, end in zip(chain, chain[1:]):
            path.extend(great_circle_points(start, end, segments=4)[1:])
        return path

    def _sea_route(self, a: Point, b: Point) -> tuple[list[Point], str]:
        """Try searoute; fall back to a great circle.

        searoute is offline and bundles its own network, so this stays inside
        the no-egress constraint. The fallback is visible in ``routed_by`` and
        surfaces in the /inputs panel rather than passing silently.
        """
        if not self._searoute_failed:
            try:
                import searoute as sr

                route = sr.searoute((a.lon, a.lat), (b.lon, b.lat))
                coords = route["geometry"]["coordinates"]
                if coords and len(coords) >= 2:
                    return ([Point(lat=lat, lon=lon) for lon, lat in coords], "searoute")
            except Exception:  # noqa: BLE001 - any failure means fall back
                self._searoute_failed = True
        return (great_circle_points(a, b, segments=24), "great_circle")

    # ---------------------------------------------------------------
    def lane_path(self, lane: dict) -> list[LegGeometry]:
        return [
            self.geometry(leg["from"], leg["to"], Mode(leg["mode"]))
            for leg in lane["legs"]
        ]

    def bounds(self) -> tuple[float, float, float, float]:
        """Bounding box over every node — funnel layer 1."""
        if self._bounds is None:
            self._bounds = bounding_box(
                [self.point(nid) for nid in self.nodes], pad_deg=3.0
            )
        return self._bounds

    def alternatives_for(self, node_id: str) -> list[Node]:
        """Declared alternatives for a node.

        An empty list means "no alternative route configured" and must be
        rendered as exactly that. BRIEF §8.1: it is not a silent zero, and it
        is not an assertion that no alternative exists in the world.
        """
        return [self.node(a) for a in self.node(node_id).alternatives]
