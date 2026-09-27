"""How a container reaches the seaport — truck, barge or rail — priced.

Sika's export says which country ships to which, and nothing about how the
freight reaches its port: there is no mode column (scripts/import_sika_flows.py).
So each focus route's first legs were a guess. This prices every realistic
chain from the plant to the gateway port with published figures and keeps the
cheapest — what a forwarder quoting the pre-carriage would do — and prices it
again under the conditions the board has recorded, because the cheapest way to
the sea from Switzerland is the Rhine, and the Rhine is what low water takes.

    cost  = sum over legs: km x payload x operator cost per tkm (ASNAV 2021)
          + Swiss heavy-vehicle fee on Swiss road km (BAZG, 2026)
          + German truck toll on German road km (Toll Collect, 2024)
          + a transfer at every change of mode (fast.yaml, assumed)
    hours = sum over legs: km / speed (fast.yaml) + handling at every change

Road and rail distances are the network's great-circle distance times the
published European circuity factor (Ballou et al. 2002). Barge distances are
Rhine kilometres, stated in fast.yaml: a river is not a straight line, and the
path the board draws is a sketch of one.

Every figure comes from config.example/fast.yaml → precarriage, with its
source beside it. Nothing here is Sika's data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.config import Config
from engine.fast.contingency import _fast, _value, handling_hours, speed_kph, transfer_chf
from engine.network.geo import Point, haversine_km
from engine.network.graph import Network
from engine.schemas import Mode


@dataclass
class Leg:
    from_id: str
    to_id: str
    mode: str
    km: float
    cost_chf: float
    hours: float
    tolls_chf: float = 0.0


@dataclass
class Chain:
    """One way of getting a container from the plant to the port, priced."""

    id: str
    legs: list[Leg]
    transfers: int
    transfer_chf: float
    note: str = ""
    # Chokepoints on the way — a barge down the Rhine passes Kaub.
    via: list[str] = field(default_factory=list)
    surcharge: float = 1.0

    @property
    def cost_chf(self) -> float:
        return round(sum(leg.cost_chf for leg in self.legs) + self.transfers * self.transfer_chf, 2)

    @property
    def hours(self) -> float:
        return round(sum(leg.hours for leg in self.legs), 1)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "cost_chf": self.cost_chf, "hours": self.hours,
            "transfers": self.transfers, "via": list(self.via), "note": self.note,
            "surcharge": self.surcharge,
            "legs": [{"from": leg.from_id, "to": leg.to_id, "mode": leg.mode,
                      "km": round(leg.km), "cost_chf": round(leg.cost_chf, 2),
                      "tolls_chf": round(leg.tolls_chf, 2), "hours": round(leg.hours, 1)}
                     for leg in self.legs],
        }


def block(config: Config) -> dict:
    raw = _fast(config).get("precarriage") or {}
    return raw if isinstance(raw, dict) else {}


def _point(place: str, network: Network, points: dict) -> Point:
    if place in points:
        return Point(float(points[place]["lat"]), float(points[place]["lon"]))
    return network.point(place)


def _tolled(entry, leg_km: float) -> float:
    if entry == "all":
        return leg_km
    return min(float(entry or 0.0), leg_km)


def price_chain(spec: dict, config: Config, network: Network,
                barge_surcharge: float = 1.0) -> Chain:
    """One chain from fast.yaml, priced. ``barge_surcharge`` multiplies every
    barge leg — the low-water surcharge the board applies at today's Kaub
    reading (thresholds.yaml → low_water_surcharge_multiplier)."""
    params = block(config)
    points = params.get("points") or {}
    payload = _value(params, "payload_t", 18.0)
    gross = _value(params, "gross_t", 40.0)
    circuity = _value(params, "circuity", 1.46)
    rates = params.get("chf_per_tkm") or {}
    swiss_fee = _value(params, "swiss_fee_chf_per_tkm", 0.0)
    eur_chf = _value(params, "eur_chf", 1.0)
    german_toll = _value(params, "german_toll_eur_per_km", 0.0) * eur_chf
    italian_toll = _value(params, "italian_toll_eur_per_km", 0.0) * eur_chf

    legs: list[Leg] = []
    via: list[str] = []
    for raw in spec.get("legs") or []:
        mode = str(raw["mode"])
        a = _point(raw["from"], network, points)
        b = _point(raw["to"], network, points)
        km = float(raw["km"]) if raw.get("km") is not None else haversine_km(a, b) * circuity
        cost = km * payload * _value(rates, mode, 0.0)
        tolls = 0.0
        if mode == "road":
            tolls += _tolled(raw.get("swiss_km"), km) * gross * swiss_fee
            tolls += _tolled(raw.get("german_km"), km) * german_toll
            tolls += _tolled(raw.get("italian_km"), km) * italian_toll
        if mode == "barge":
            cost *= barge_surcharge
        hours = km / speed_kph(config, Mode(mode))
        legs.append(Leg(raw["from"], raw["to"], mode, km, cost + tolls, hours, tolls))
        if raw.get("via"):
            via.append(str(raw["via"]))

    # A change of mode costs a lift and takes the handling time of the place
    # it happens at — the same transfer and handling the contingency engine
    # charges its generated paths.
    transfers = max(0, len(legs) - 1)
    for leg in legs[:-1]:
        place = leg.to_id
        leg.hours += (handling_hours(config, network, place) if place in network.nodes
                      else _value((_fast(config).get("transit") or {}).get("handling_hours") or {},
                                  "inland_port", 10.0))
    return Chain(
        id=str(spec["id"]), legs=legs, transfers=transfers,
        transfer_chf=transfer_chf(config), note=str(spec.get("note") or ""),
        via=via, surcharge=barge_surcharge if any(leg.mode == "barge" for leg in legs) else 1.0,
    )


def compare(gateway: dict, config: Config, network: Network,
            barge_surcharge: float = 1.0) -> list[Chain]:
    """Every chain for one origin and port, cheapest first."""
    chains = [price_chain(spec, config, network, barge_surcharge)
              for spec in gateway.get("chains") or []]
    return sorted(chains, key=lambda c: (c.cost_chf, c.hours))


def choose(chains: list[Chain], config: Config) -> Chain | None:
    """The chain a forwarder would book: the cheapest, unless another is
    within ``tie_fraction`` of it and faster — at the precision of published
    per-tkm rates, a 1% difference is not a difference."""
    if not chains:
        return None
    cheapest = min(c.cost_chf for c in chains)
    tie = _value(block(config), "tie_fraction", 0.0)
    close = [c for c in chains if c.cost_chf <= cheapest * (1.0 + tie)]
    return min(close, key=lambda c: (c.hours, c.cost_chf))


def gateways(config: Config) -> list[dict]:
    return [g for g in block(config).get("gateways") or [] if isinstance(g, dict)]


def for_lane(config: Config, lane_id: str) -> dict | None:
    return next((g for g in gateways(config) if lane_id in (g.get("lanes") or [])), None)
