"""How a container reaches its port — truck, barge or rail — priced.

Sika's export has no mode column, so the five focus routes' first legs were
a guess. They are now the cheapest chain at published rates (ASNAV operator
costs, the Swiss heavy-vehicle fee, German and Italian tolls), and the lane
file is held to that answer.
"""

from __future__ import annotations

import pytest

from engine.config import load_config
from engine.fast import precarriage as pc
from engine.network.graph import Network


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def network(config):
    return Network(config)


def _gateway(config, origin, port):
    return next(g for g in pc.gateways(config) if g["origin"] == origin and g["port"] == port)


def test_a_road_leg_pays_operator_cost_fee_and_tolls(config, network):
    spec = {"id": "t", "legs": [{"from": "SIKA_DUD", "to": "NLRTM", "mode": "road",
                                 "km": 100, "swiss_km": 40, "german_km": 60}]}
    [leg] = pc.price_chain(spec, config, network).legs
    params = pc.block(config)
    expected = (100 * params["payload_t"]["value"] * params["chf_per_tkm"]["road"]["value"]
                + 40 * params["gross_t"]["value"] * params["swiss_fee_chf_per_tkm"]["value"]
                + 60 * params["german_toll_eur_per_km"]["value"] * params["eur_chf"]["value"])
    assert leg.cost_chf == pytest.approx(expected)


def test_a_change_of_mode_costs_a_transfer_and_its_handling_time(config, network):
    one = pc.price_chain({"id": "a", "legs": [
        {"from": "SIKA_DUD", "to": "CHBSL", "mode": "road"}]}, config, network)
    two = pc.price_chain({"id": "b", "legs": [
        {"from": "SIKA_DUD", "to": "CHBSL", "mode": "road"},
        {"from": "CHBSL", "to": "NLRTM", "mode": "rail"}]}, config, network)
    assert one.transfers == 0 and two.transfers == 1
    assert two.cost_chf > two.legs[0].cost_chf + two.legs[1].cost_chf


def test_the_rhine_is_cheapest_until_kaub_is_low(config, network):
    """Normally the barge from Basel; at today's Kaub derate (a 2.2x
    low-water surcharge, thresholds.yaml) the same freight goes cheaper by
    rail — the switch the board's Rhine event recommends."""
    gateway = _gateway(config, "SIKA_DUD", "NLRTM")
    assert pc.choose(pc.compare(gateway, config, network), config).id == "truck + barge"
    low = pc.compare(gateway, config, network, barge_surcharge=2.2)
    assert pc.choose(low, config).id == "truck + rail"


def test_within_the_tie_the_faster_chain_wins(config, network):
    """To Antwerp the barge and the Schweizerzug price within 2% of each
    other — a rounding difference at these rates — and the train is two
    days faster."""
    chains = pc.compare(_gateway(config, "SIKA_DUD", "BEANR"), config, network)
    barge = next(c for c in chains if c.id == "truck + barge")
    rail = next(c for c in chains if c.id == "truck + rail")
    assert abs(barge.cost_chf - rail.cost_chf) / min(barge.cost_chf, rail.cost_chf) < 0.05
    assert pc.choose(chains, config).id == "truck + rail"


def test_trucking_all_the_way_is_never_the_cheapest(config, network):
    for gateway in pc.gateways(config):
        chains = pc.compare(gateway, config, network)
        assert chains[0].id != "truck", gateway["port"]


def _main_mode(legs):
    """The mode that carries a chain: its longest non-road leg, else road."""
    carrying = [leg for leg in legs if leg["mode"] != "road"]
    if not carrying:
        return "road"
    return max(carrying, key=lambda leg: leg.get("km") or leg.get("transit_hours") or 0)["mode"]


def test_every_focus_lane_is_drawn_on_the_chain_that_won(config, network):
    """The lane file and the simulation cannot drift apart: a rate that
    changes the cheapest chain fails here, rather than leaving the map on
    the old answer."""
    lanes = {lane["id"]: lane for lane in config.lanes}
    for gateway in pc.gateways(config):
        chosen = pc.choose(pc.compare(gateway, config, network), config)
        for lane_id in gateway["lanes"]:
            legs = lanes[lane_id]["legs"]
            to_port = legs[: next(i for i, leg in enumerate(legs) if leg["to"] == gateway["port"]) + 1]
            assert to_port[0]["from"] == gateway["origin"], lane_id
            drawn = _main_mode([{"mode": leg["mode"], "transit_hours": leg["transit_hours"]}
                                for leg in to_port])
            assert drawn == _main_mode(chosen.as_dict()["legs"]), (lane_id, chosen.id)


def test_every_rate_says_where_it_came_from(config):
    params = pc.block(config)
    entries = [params[k] for k in ("payload_t", "gross_t", "circuity", "swiss_fee_chf_per_tkm",
                                   "german_toll_eur_per_km", "italian_toll_eur_per_km",
                                   "eur_chf", "tie_fraction")]
    entries += list(params["chf_per_tkm"].values())
    assert all(isinstance(e, dict) and e.get("source") for e in entries)
