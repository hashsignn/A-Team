"""The map's context layers: what is around a shipment, route or customer.

Pinned here: each scope anchors on its own freight; ports carry their role
and whether an event sits on them; a vessel at sea gets road and rail from
its next port, never across the sea; rail only where both ends have rail;
nearby-and-available is inside the reach AND available; and an unknown
scope is refused rather than answered with the world.
"""

from __future__ import annotations

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.fleet import assets as assets_mod
from engine.fleet import context as ctx_mod
from engine.network.geo import Point, haversine_km
from engine.pipeline import RunOptions, run

AS_OF = "2026-09-18T06:00:00+00:00"


@pytest.fixture(scope="module")
def context():
    return run(clock=Clock.at(AS_OF), config=load_config(), options=RunOptions(shipment_count=150))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


@pytest.fixture(scope="module")
def fleet(board, context):
    return assets_mod.fleet_assets(board, context)["assets"]


def test_a_shipment_is_measured_from_its_own_vehicle(board, context, fleet):
    truck = next(a for a in fleet if a["mode"] != "sea" and a["phase"] == "in_transit")
    out = ctx_mod.build(board, context, shipment_id=truck["id"])
    assert out["scope"] == {"kind": "shipment", "id": truck["id"], "label": out["scope"]["label"]}
    assert [a["id"] for a in out["anchors"]] == [truck["id"]]
    assert (out["origin"]["lat"], out["origin"]["lon"]) == (truck["lat"], truck["lon"])
    assert set(out["layers"]) == {"ports", "inventories", "vendors", "links", "nearby"}


def test_a_vessel_at_sea_takes_road_and_rail_from_its_next_port(board, context, fleet):
    ship = next(a for a in fleet if a["mode"] == "sea" and a["phase"] == "in_transit")
    out = ctx_mod.build(board, context, shipment_id=ship["id"])
    assert out["origin"]["why"] == "next port"
    here = Point(out["origin"]["lat"], out["origin"]["lon"])
    for link in out["layers"]["links"]:
        end = link["path"][-1]
        assert haversine_km(here, Point(end[0], end[1])) <= ctx_mod.LINK_KM + 1


def test_ports_say_their_role_and_whether_an_event_sits_on_them(board, context):
    out = ctx_mod.build(board, context, route_id="LANE_RHINE_01")
    roles = {p["role"] for p in out["layers"]["ports"]}
    assert "alternative" in roles and "on_route" in roles
    disrupted = {nid for e in context.events for nid in e.node_ids or []}
    for port in out["layers"]["ports"]:
        assert port["available"] == (port["id"] not in disrupted)


def test_rail_only_where_both_ends_have_rail(board, context):
    out = ctx_mod.build(board, context, route_id="LANE_RHINE_01")
    by_id = {x["id"]: x for x in out["layers"]["ports"] + out["layers"]["inventories"]}
    rails = [link for link in out["layers"]["links"] if link["mode"] == "rail"]
    assert rails, "a Rhine route should have rail around it"
    assert all("rail" in by_id[link["to"]]["modes"] for link in rails)
    assert all(link["km"] > 0 and link["hours"] > 0 for link in out["layers"]["links"])


def test_nearby_means_inside_the_reach_and_available(board, context):
    out = ctx_mod.build(board, context, route_id="LANE_RHINE_01")
    items = {x["id"]: x for k in ("ports", "inventories", "vendors") for x in out["layers"][k]}
    assert out["layers"]["nearby"]
    for item_id in out["layers"]["nearby"]:
        assert items[item_id]["distance_km"] <= out["reach_km"]
        assert items[item_id]["available"] is True


def test_a_customer_anchors_on_its_own_freight(board, context, fleet):
    name = fleet[0]["customer"]
    out = ctx_mod.build(board, context, customer=name)
    mine = {a["id"] for a in fleet if a["customer"] == name}
    assert out["anchors"] and {a["id"] for a in out["anchors"]} <= mine
    assert len(out["anchors"]) <= ctx_mod.MAX_ANCHORS


def test_an_unknown_scope_is_refused(board, context):
    with pytest.raises(ctx_mod.ContextError):
        ctx_mod.build(board, context, shipment_id="SYN-9999")
    with pytest.raises(ctx_mod.ContextError):
        ctx_mod.build(board, context, route_id="LANE_NOWHERE")
    with pytest.raises(ctx_mod.ContextError):
        ctx_mod.build(board, context)
