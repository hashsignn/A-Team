"""The five focus routes: where the board is held to the real world.

Every route on the board is built the same way and scored the same way. What
differs is how much of what it reads is real. Most routes run on the
synthetic book and on whatever the free feeds happen to say about them. A
FOCUS route is one somebody has committed to making real: its places are
watched for weather and sea state, the news that touches it is recorded over
two months, and the companies that actually run its legs are named, each
with the public page it was checked against.

config/focus.yaml says which routes those are. With no file there are none,
and nothing about any route changes — this module only ever ADDS a label and
a watch list; it never changes a score.

WHAT THE LABEL MAY CLAIM
========================
"Real data" on a card is a claim a planner will act on, so it is built from
what the run actually read, source by source, never from the file saying the
route is a focus route. A focus route whose weather was never recorded says
"weather: not recorded yet", not "real".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.config import Config

# Node kinds whose sea state matters: a ship waits in the water off them.
MARINE_KINDS = frozenset({"seaport", "chokepoint"})


@dataclass(frozen=True)
class WatchPoint:
    """One place on a focus route whose conditions are recorded."""

    node_id: str
    name: str
    kind: str
    lat: float
    lon: float
    lanes: tuple[str, ...]
    # Where sea state is read, when it is: a port's own coordinates are on the
    # quay, where the wave model has no value. None for an inland place.
    marine_lat: float | None = None
    marine_lon: float | None = None
    marine_name: str = ""

    @property
    def marine(self) -> bool:
        return self.marine_lat is not None and self.marine_lon is not None


@dataclass(frozen=True)
class FocusRoute:
    lane_id: str
    sika_flow: str = ""
    why: str = ""
    operators: tuple[dict, ...] = field(default_factory=tuple)
    notes: tuple[dict, ...] = field(default_factory=tuple)
    # Where the route starts and which port it leaves by, each with the public
    # source it was chosen from: the export names neither.
    origin: dict = field(default_factory=dict)
    port: dict = field(default_factory=dict)
    # The Sika company that receives the goods at the far end, found the
    # same way (the export names it by country code only).
    destination: dict = field(default_factory=dict)
    # What public customs records show for this flow (US bills of lading are
    # public), and where each line was read. Empty where nothing is public.
    trade_records: tuple[str, ...] = field(default_factory=tuple)
    trade_sources: tuple[str, ...] = field(default_factory=tuple)


def _plain(entry: dict) -> dict:
    """A YAML entry as JSON can carry it: a `checked: 2026-09-26` arrives as a
    date object, and one date object is enough to fail the whole board."""
    out = {}
    for key, value in entry.items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, str):
            value = " ".join(value.split())
        out[str(key)] = value
    return out


def _block(config: Config) -> dict:
    loaded = config.files.get("focus")
    data = loaded.data if loaded is not None else None
    return data if isinstance(data, dict) else {}


def focus_routes(config: Config) -> dict[str, FocusRoute]:
    """lane id → its focus entry, for the lanes that exist. In file order.

    A lane named here that the lane file does not define is ignored rather
    than raised: focus.yaml is an overlay on top of lanes.yaml, and a lane
    renamed there should not take the board down.
    """
    block = _block(config)
    known = {lane["id"] for lane in config.lanes}
    operators = [_plain(o) for o in block.get("operators") or [] if isinstance(o, dict)]
    notes = [_plain(n) for n in block.get("corridor_notes") or [] if isinstance(n, dict)]
    out: dict[str, FocusRoute] = {}
    for entry in block.get("focus_routes") or []:
        if not isinstance(entry, dict):
            continue
        lane_id = str(entry.get("lane", "")).strip()
        if lane_id not in known or lane_id in out:
            continue
        out[lane_id] = FocusRoute(
            lane_id=lane_id,
            sika_flow=str(entry.get("sika_flow", "")).strip(),
            why=" ".join(str(entry.get("why", "")).split()),
            operators=tuple(o for o in operators if lane_id in (o.get("routes") or [])),
            notes=tuple(n for n in notes if lane_id in (n.get("routes") or [])),
            origin=_plain(entry["origin"]) if isinstance(entry.get("origin"), dict) else {},
            port=_plain(entry["port"]) if isinstance(entry.get("port"), dict) else {},
            destination=(_plain(entry["destination"])
                         if isinstance(entry.get("destination"), dict) else {}),
            trade_records=tuple(" ".join(str(x).split()) for x in entry.get("trade_records") or []),
            trade_sources=tuple(str(x).strip() for x in entry.get("trade_sources") or []),
        )
    return out


def lane_node_ids(lane: dict) -> list[str]:
    legs = lane.get("legs") or []
    if not legs:
        return []
    return [legs[0]["from"], *(leg["to"] for leg in legs)]


def watch_points(config: Config) -> list[WatchPoint]:
    """Every place on a focus route, once, in route order.

    Derived from the lanes rather than listed, so a leg added to a focus
    route is watched without anybody remembering to add it here too.
    """
    routes = focus_routes(config)
    lanes = {lane["id"]: lane for lane in config.lanes}
    marine = _block(config).get("marine_points") or {}
    seen: dict[str, list[str]] = {}
    for lane_id in routes:
        for node_id in lane_node_ids(lanes[lane_id]):
            seen.setdefault(node_id, [])
            if lane_id not in seen[node_id]:
                seen[node_id].append(lane_id)

    points = []
    for node_id, on_lanes in seen.items():
        node = config.nodes.get(node_id)
        if node is None:
            continue
        kind = getattr(node.kind, "value", str(node.kind))
        at = marine.get(node_id) if isinstance(marine.get(node_id), dict) else None
        if kind in MARINE_KINDS:
            m_lat = float(at["lat"]) if at else node.lat
            m_lon = float(at["lon"]) if at else node.lon
            m_name = str(at.get("name", "")) if at else ""
        else:
            m_lat = m_lon = None
            m_name = ""
        points.append(WatchPoint(
            node_id=node_id, name=node.name, kind=kind, lat=node.lat, lon=node.lon,
            lanes=tuple(on_lanes), marine_lat=m_lat, marine_lon=m_lon,
            marine_name=m_name,
        ))
    return points


def flow_volume(route: FocusRoute, flows) -> int | None:
    """Documents in the customer's export for this route's flow, or None.

    Read from config/flows.yaml at run time — it is gitignored, and this
    number never leaves the machine that holds it except on that machine's
    own board.
    """
    if flows is None or not getattr(flows, "available", False) or "_" not in route.sika_flow:
        return None
    origin, _, destination = route.sika_flow.partition("_")
    return flows.by_pair.get((origin, destination))


def operators(config: Config) -> list[dict]:
    """Every operator in focus.yaml once, whichever routes it serves."""
    seen: dict[str, dict] = {}
    for route in focus_routes(config).values():
        for entry in route.operators:
            seen.setdefault(entry.get("name", ""), entry)
    return [entry for name, entry in seen.items() if name]
