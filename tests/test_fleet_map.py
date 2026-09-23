"""The fleet map: assets, status, the Action Hub, recovery routes, splits, partners.

Most of these assert a property a planner would notice the day it broke: a
green dot on a shipment three days late, a "#1" route that is neither the
fastest nor the cheapest under the weights that asked for exactly that, a
split that quietly drops a container, a partner offered for dangerous goods
it is not certified to carry.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import pytest
from fastapi import HTTPException

from engine.clock import Clock
from engine.config import load_config
from engine.export.board import build_board
from engine.fleet import assets as assets_mod
from engine.fleet import manifest, reroute, split, vendors
from engine.fleet.sealanes import SeaGraph
from engine.fleet.settings import settings
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")
LATER = Clock.at("2026-09-24T12:00:00+00:00")


@pytest.fixture(scope="module")
def context():
    return run(clock=AS_OF, config=load_config(), options=RunOptions(shipment_count=150))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


@pytest.fixture(scope="module")
def fleet(board, context):
    return assets_mod.fleet_assets(board, context)


@pytest.fixture(scope="module")
def later():
    ctx = run(clock=LATER, config=load_config(), options=RunOptions(shipment_count=150))
    return build_board(ctx), ctx


def _on_map(fleet):
    return [a for a in fleet["assets"] if a["phase"] != "booked"]


def _disrupted_with_routes(board, context, fleet, minimum=2):
    for a in _on_map(fleet):
        if a["status"] == "green":
            continue
        routes = reroute.recovery(board, context, a["id"])
        if len(routes["candidates"]) >= minimum:
            return a, routes
    pytest.fail("no disrupted asset with enough recovery routes on the demo board")


# =====================================================================
# Assets and their three colours
# =====================================================================


def test_every_undelivered_shipment_is_an_asset_exactly_once(fleet, context):
    ids = [a["id"] for a in fleet["assets"]]
    assert len(ids) == len(set(ids))
    undelivered = {s.shipment_id for s in context.shipments
                   if context.clock.as_of < s.legs[-1].planned_arrive}
    assert set(ids) == undelivered


def test_the_colours_are_the_clients_and_only_three(fleet):
    assert fleet["meta"]["status_colours"] == {
        "green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}
    assert {a["status"] for a in fleet["assets"]} <= {"green", "yellow", "red"}
    for a in fleet["assets"]:
        assert a["colour"] == fleet["meta"]["status_colours"][a["status"]]


def test_status_follows_the_delay_cutoffs(fleet, context):
    """Yellow is under four hours late, red is four or more — the brief's own
    boundary — unless a field report of a stoppage or damage overrides it."""
    cfg = settings(context.config)["status"]
    for a in fleet["assets"]:
        h = a["delay_hours"]
        if a["status"] == "yellow":
            assert cfg["nominal_max_hours"] < h < cfg["minor_max_hours"]
        elif a["status"] == "green":
            assert h <= cfg["nominal_max_hours"]
        elif "field report" not in a["reason"].lower() and "reported" not in a["reason"].lower():
            assert h >= cfg["minor_max_hours"]


def test_the_delay_is_the_boards_own_number(fleet, context):
    """Not a new estimate: the Monte Carlo's do-nothing mean for the worst
    event touching the shipment. Two screens disagreeing about how late a
    shipment is destroys trust in both."""
    worst: dict[str, float] = {}
    for assessment in context.result.assessments:
        for r in assessment.shipment_risks:
            worst[r.shipment_id] = max(worst.get(r.shipment_id, 0.0),
                                       r.do_nothing.expected_delay_days * 24)
    for a in fleet["assets"]:
        assert a["delay_hours"] == pytest.approx(worst.get(a["id"], 0.0), abs=0.01)


def test_the_board_has_all_three_colours_on_the_map(fleet):
    seen = {a["status"] for a in _on_map(fleet)}
    assert seen == {"green", "yellow", "red"}, seen


def test_an_asset_sits_on_the_line_it_is_travelling(fleet, context):
    """The dot is placed on the leg's actual geometry — on the river, in the
    sea lane — not on the geodesic between its nodes, which for a Rhine barge
    is a field."""
    from engine.network.geo import Point, on_corridor

    by_id = {s.shipment_id: s for s in context.shipments}
    checked = 0
    for a in fleet["assets"]:
        if a["phase"] != "in_transit" or a["position_source"] != "schedule":
            continue
        leg = next(lg for lg in by_id[a["id"]].legs
                   if lg.planned_depart <= context.clock.as_of < lg.planned_arrive)
        path = context.network.geometry(leg.from_node, leg.to_node, leg.mode).path
        # 10 km: interpolation inside a long ocean segment is linear in
        # lat/lon, a few km off the great circle. The failure this guards —
        # a dot on the geodesic between nodes — is hundreds of km off.
        hit, distance = on_corridor(Point(a["lat"], a["lon"]), path, leg.mode.value, 10.0)
        assert hit, f"{a['id']} is {distance:.1f} km off its {leg.mode.value} leg"
        checked += 1
    assert checked > 5


def test_modes_come_from_the_current_leg(fleet):
    assert {a["mode"] for a in _on_map(fleet)} >= {"sea", "road", "barge"}


# =====================================================================
# The Action Hub
# =====================================================================


def test_the_hub_carries_every_block_the_card_shows(board, context, fleet):
    a = next(x for x in _on_map(fleet) if x["status"] == "red")
    d = assets_mod.asset_detail(board, context, a["id"])
    for key in ("asset", "status", "position", "last_sync", "load", "cargo",
                "containers", "logistics", "logs", "matrix", "radar"):
        assert key in d, key
    assert d["asset"]["asset_id"] and d["asset"]["crew"]["name"]
    assert d["load"]["loaded_teu"] <= d["load"]["capacity_teu"]
    assert d["load"]["ours_teu"] == sum(b["teu"] for b in d["containers"])
    assert d["logs"], "a red asset must say why it is red"


def test_a_planned_position_is_never_presented_as_a_fix(board, context, fleet):
    """No GPS or AIS feed is connected. The card says the position is where
    the plan puts it — a planned dot labelled as telemetry is a lie with a
    timestamp."""
    a = next(x for x in _on_map(fleet) if x["position_source"] == "schedule")
    d = assets_mod.asset_detail(board, context, a["id"])
    assert d["position"]["source"] == "schedule"
    assert "schedule" in d["last_sync_source"]
    schedule_logs = [log for log in d["logs"] if log["kind"] == "schedule"]
    assert schedule_logs and "not a fix" in schedule_logs[0]["detail"]


def test_the_revised_eta_is_the_original_plus_the_delay(board, context, fleet):
    for a in _on_map(fleet)[:12]:
        d = assets_mod.asset_detail(board, context, a["id"])
        g = d["logistics"]
        gap = (datetime.fromisoformat(g["eta_revised"])
               - datetime.fromisoformat(g["eta_original"])).total_seconds() / 3600
        assert gap == pytest.approx(g["delay_hours"], abs=0.01)


def test_the_matrix_is_five_by_five_and_unsourced_stays_off_the_axis(board, context, fleet):
    for a in _on_map(fleet):
        m = assets_mod.asset_detail(board, context, a["id"])["matrix"]
        assert len(m["probability_bands"]) == 5 and len(m["impact_bands"]) == 5
        p_ids = {b["id"] for b in m["probability_bands"]}
        for point in m["points"]:
            if point["p_late"] is None:
                assert point["probability_band"] is None, "unsourced placed on the axis"
            else:
                assert point["probability_band"] in p_ids


def test_the_radar_has_the_five_axes_the_brief_names(board, context, fleet):
    a = next(x for x in _on_map(fleet) if x["status"] == "red")
    r = assets_mod.asset_detail(board, context, a["id"])["radar"]
    assert r["axes"] == ["Weather", "Geopolitics", "Port Congestion",
                         "Route Infrastructure", "Mechanical Status"]
    assert all(0 <= v <= 100 for v in r["values"])
    assert any(v > 0 for v in r["values"]), "a red asset with a flat radar"
    # The index is a transform of hours, and the hours ride along.
    scale = r["scale_hours"]
    for v, h in zip(r["values"], r["hours"], strict=True):
        if h:
            assert v == pytest.approx(100 * (1 - math.exp(-h / scale)), abs=0.1)


def test_container_ids_are_valid_iso_6346():
    for serial in (0, 1, 123456, 999999):
        cid = manifest.container_id(serial)
        assert len(cid) == 11 and cid.startswith("SYNU")
        assert int(cid[-1]) == manifest.iso6346_check_digit(cid[:10])
    # A published example: CSQU3054383.
    assert manifest.iso6346_check_digit("CSQU305438") == 3


def test_the_manifest_is_stable_and_labelled_synthetic(context):
    shipment = context.shipments[0]
    assert manifest.containers(shipment) == manifest.containers(shipment)
    assert all(b["synthetic"] for b in manifest.containers(shipment))
    assert manifest.vehicle(shipment, 0)["synthetic"] is True


def test_an_unknown_asset_is_none_not_an_empty_card(board, context):
    assert assets_mod.asset_detail(board, context, "SYN-NOPE") is None
    assert reroute.recovery(board, context, "SYN-NOPE") is None
    assert vendors.nearby(board, context, "SYN-NOPE") is None


# =====================================================================
# Recovery routes
# =====================================================================


def test_a_disrupted_asset_gets_ranked_alternatives(board, context, fleet):
    _, routes = _disrupted_with_routes(board, context, fleet)
    ranks = [c["rank"] for c in routes["candidates"]]
    assert ranks == list(range(1, len(ranks) + 1))
    badges = [c["badge"] for c in routes["candidates"][:3]]
    assert badges == [f"#{i}" for i in range(1, len(badges) + 1)]


def test_every_alternative_branches_from_the_live_location(board, context, fleet):
    _, routes = _disrupted_with_routes(board, context, fleet)
    start = routes["original"]["path"][0]
    for c in routes["candidates"]:
        assert c["path"][0] == start, f"{c['id']} does not start where the asset is"
        assert c["delta"]["text"].count(",") == 2 and "Risk:" in c["delta"]["text"]


def test_the_delta_is_candidate_minus_original(board, context, fleet):
    _, routes = _disrupted_with_routes(board, context, fleet)
    o = routes["original"]
    for c in routes["candidates"]:
        assert c["delta"]["cost_chf"] == pytest.approx(c["cost_chf"] - o["cost_chf"], abs=0.01)
        assert c["delta"]["hours"] == pytest.approx(c["hours"] - o["hours"], abs=0.01)


@pytest.mark.parametrize("key, field", [("time", "hours"), ("cost", "cost_chf"),
                                        ("risk", "risk")])
def test_a_single_weight_puts_the_best_on_that_measure_first(board, context, fleet, key, field):
    a, _ = _disrupted_with_routes(board, context, fleet)
    weights = {"time": 0, "cost": 0, "risk": 0, key: 1}
    ranked = reroute.recovery(board, context, a["id"], weights=weights)["candidates"]
    assert ranked[0][field] == min(c[field] for c in ranked)


def test_green_assets_draw_no_recovery_unless_forced(board, context, fleet):
    a = next(x for x in _on_map(fleet) if x["status"] == "green")
    routes = reroute.recovery(board, context, a["id"])
    assert routes["candidates"] == [] and routes["eligible"] is False
    assert routes["note"]


def test_the_suez_bypass_goes_round_the_cape_and_never_through_suez(board, context, fleet):
    found = False
    for a in _on_map(fleet):
        routes = reroute.recovery(board, context, a["id"])
        for c in routes["candidates"]:
            if c["kind"] != "sea_bypass" or "CHOKE_SUEZ" not in routes["disrupted_nodes"]:
                continue
            found = True
            assert "Cape of Good Hope" in c["label"]
            assert "CHOKE_SUEZ" in c["avoids"]
            assert c["owner"] == "carrier", "rerouting a vessel is the carrier's call"
            # South of the Cape, and nowhere near the canal.
            lats = [p[0] for p in c["path"]]
            assert min(lats) < -33
            assert all(abs(p[0] - 30.0) > 1 or abs(p[1] - 32.55) > 1 for p in c["path"])
    assert found, "no Suez bypass generated on the demo board"


def test_hormuz_has_no_sea_route_and_says_so(later):
    """Jebel Ali is behind the strait. The empty alternatives list is a domain
    fact, and the honest output is "there is no route", not a detour."""
    board, ctx = later
    fleet = assets_mod.fleet_assets(board, ctx)
    gulf = [a for a in fleet["assets"] if a["lane_id"] == "LANE_GULF_01" and a["phase"] != "booked"]
    assert gulf
    routes = reroute.recovery(board, ctx, gulf[0]["id"], force=True)
    assert "CHOKE_HORMUZ" in routes["disrupted_nodes"]
    assert not routes["candidates"]
    assert any("Hormuz" in note for note in routes["no_route"])


def test_a_closed_canal_finds_a_land_bridge(later):
    """Panama cut, Los Angeles unreachable by sea from Europe in this graph:
    sail to a Gulf or East Coast port and rail across. Nobody wrote that rule;
    it falls out of 'nearest port with a land bridge'."""
    board, ctx = later
    fleet = assets_mod.fleet_assets(board, ctx)
    for a in fleet["assets"]:
        if a["lane_id"] != "LANE_US_03" or a["phase"] == "booked":
            continue
        routes = reroute.recovery(board, ctx, a["id"], force=True)
        if "CHOKE_PANAMA" not in routes["disrupted_nodes"]:
            continue
        bridges = [c for c in routes["candidates"] if c["kind"] == "port_swap"]
        assert bridges
        for c in bridges:
            modes = [leg["mode"] for leg in c["legs"] if leg["new"]]
            assert modes[0] == "sea" and modes[-1] in ("rail", "road")
            assert all("CHOKE_PANAMA" not in (leg["to"]["id"] or "") for leg in c["legs"] if leg["new"])
        return
    pytest.skip("no Panama-disrupted asset at this instant")


def test_a_corridor_hit_does_not_close_a_port(board, context, fleet):
    """A motorway fire near Karlsruhe gates the Stuttgart → Rotterdam truck,
    and the gate reports it against Rotterdam. That must not make Rotterdam a
    closed port, or every recovery route diverts to Antwerp."""
    for a in _on_map(fleet):
        if a["lane_id"] != "LANE_EU_01":
            continue
        routes = reroute.recovery(board, context, a["id"], force=True)
        for c in routes["candidates"]:
            assert c["legs"][-1]["to"]["id"] == "NLRTM"


def test_the_sea_graph_is_connected_between_every_sea_port(context):
    graph = SeaGraph(context.config.nodes)
    ports = [n.id for n in context.config.nodes.values()
             if n.kind.value == "seaport" and graph.has(n.id)]
    assert len(ports) >= 15
    for a in ports:
        for b in ports:
            if a != b and not {a, b} & {"USLAX"}:
                assert graph.route(a, b) is not None, f"{a} -> {b}"


def test_removing_a_node_removes_it_from_every_path(context):
    graph = SeaGraph(context.config.nodes)
    via_suez = graph.route("ITGOA", "SGSIN")
    assert via_suez.passes("CHOKE_SUEZ")
    around = graph.route("ITGOA", "SGSIN", avoid={"CHOKE_SUEZ"})
    assert not around.passes("CHOKE_SUEZ") and around.passes("CHOKE_GOODHOPE")
    assert around.distance_km > via_suez.distance_km


# =====================================================================
# Splitting a load
# =====================================================================


def test_the_suggestion_moves_only_boxes_that_would_miss(board, context, fleet):
    a, routes = _disrupted_with_routes(board, context, fleet)
    s = split.evaluate(board, context, a["id"])
    original_eta = datetime.fromisoformat(routes["original"]["eta"])
    for box in s["containers"]:
        deadline = datetime.fromisoformat(box["deadline"])
        if box["route_id"] != split.ORIGINAL:
            assert deadline < original_eta, f"{box['container_id']} moved but was on time"
    assert s["summary"]["on_time_after"] >= s["summary"]["on_time_before"]


def test_every_box_is_in_exactly_one_branch(board, context, fleet):
    a, routes = _disrupted_with_routes(board, context, fleet)
    boxes = split.evaluate(board, context, a["id"])["containers"]
    half = {b["container_id"]: routes["candidates"][0]["id"] for b in boxes[: len(boxes) // 2]}
    s = split.evaluate(board, context, a["id"], allocation=half)
    placed = [cid for branch in s["branches"] for cid in branch["containers"]]
    assert sorted(placed) == sorted(b["container_id"] for b in boxes)
    assert sum(b["teu"] for b in s["branches"]) == s["summary"]["teu_total"]
    assert len(s["branches"]) == 2


def test_road_is_priced_per_truck_not_per_box():
    cfg = settings(load_config())
    one = reroute.price(cfg, [("road", 100.0)], 1, 0)
    two = reroute.price(cfg, [("road", 100.0)], 2, 0)
    three = reroute.price(cfg, [("road", 100.0)], 3, 0)
    assert one == two < three
    assert reroute.price(cfg, [("rail", 100.0)], 2, 0) == 2 * reroute.price(cfg, [("rail", 100.0)], 1, 0)


def test_a_bad_allocation_is_refused_with_the_reason(board, context, fleet):
    a, _ = _disrupted_with_routes(board, context, fleet)
    with pytest.raises(split.SplitError, match="not on"):
        split.evaluate(board, context, a["id"], allocation={"SYNU0000000": "ORIGINAL"})
    box = manifest.containers(context.shipments[0])[0]["container_id"]
    with pytest.raises(split.SplitError):
        split.evaluate(board, context, a["id"], allocation={box: "ALT-NOWHERE"})


# =====================================================================
# Partners
# =====================================================================


def test_partners_are_within_the_radius_nearest_first(board, context, fleet):
    a, _ = _disrupted_with_routes(board, context, fleet)
    v = vendors.nearby(board, context, a["id"])
    distances = [p["distance_km"] for p in v["vendors"]]
    assert distances == sorted(distances)
    if not v["fallback"]:
        assert all(d <= v["radius_km"] for d in distances)


def test_dangerous_goods_are_never_offered_to_a_carrier_without_adr(board, context, fleet):
    checked = 0
    for a in _on_map(fleet):
        shipment = next(s for s in context.shipments if s.shipment_id == a["id"])
        if not shipment.dangerous_goods or a["status"] == "green":
            continue
        v = vendors.nearby(board, context, a["id"], radius_km=2000)
        for p in v["vendors"]:
            for c in p["serviceable"]:
                if p["adr_certified"] is False and c["physical"] and \
                        any(leg["mode"] in ("road", "rail") for leg in
                            next(x for x in reroute.recovery(board, context, a["id"])["candidates"]
                                 if x["id"] == c["route_id"])["legs"] if leg["new"]):
                    assert not c["ok"] and not c["legal"]
                    checked += 1
    assert checked, "no non-ADR partner was ever tested against dangerous goods"


def test_unknown_capacity_is_unknown_not_a_number(board, context):
    local = [p for p in vendors.partners(context) if p["source"] == "contacts.yaml"]
    assert local and all(p["capacity"] is None for p in local)


# =====================================================================
# The API is a thin skin over the engine
# =====================================================================


def test_the_map_endpoints_answer():
    from api import main

    assets = json.loads(main.map_assets(as_of=AS_OF.as_of.isoformat(), shipments=150).body)
    red = next(a["id"] for a in assets["assets"] if a["status"] == "red" and a["phase"] != "booked")
    args = {"as_of": AS_OF.as_of.isoformat(), "shipments": 150}
    assert json.loads(main.map_asset(red, **args).body)["shipment_id"] == red
    routes = json.loads(main.map_routes(red, w_time=None, w_cost=None, w_risk=None,
                                        force=False, **args).body)
    assert "candidates" in routes
    found = json.loads(main.map_vendors(red, radius_km=None, teu=None, w_time=None,
                                        w_cost=None, w_risk=None, **args).body)
    assert "vendors" in found
    s = json.loads(main.map_split(red, {"allocation": None}, **args).body)
    assert s["suggested"] is True


def test_the_map_endpoints_refuse_what_they_cannot_answer():
    from api import main

    args = {"as_of": AS_OF.as_of.isoformat(), "shipments": 150}
    with pytest.raises(HTTPException) as err:
        main.map_asset("SYN-NOPE", **args)
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        main.map_split("SYN-0001", {"allocation": ["not", "a", "map"]}, **args)
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        main.map_split("SYN-0001", {"allocation": {"SYNU0000000": "ORIGINAL"}}, **args)
    assert err.value.status_code == 422


def test_nothing_in_the_fleet_package_reads_the_wall_clock():
    """The as-of discipline, which test_end_to_end enforces for engine/ — the
    fleet package included, but asserted here too so a failure names it."""
    import ast
    from pathlib import Path

    for path in Path("engine/fleet").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
                raise AssertionError(f"{path} calls .{node.attr}()")


def test_a_driver_reporting_a_stoppage_turns_the_dot_red(board, context, fleet, tmp_path,
                                                          monkeypatch):
    """Somebody looking at the freight outranks a forecast — and the board is
    cached per as-of, so the report must be read fresh, not frozen with the
    run. A report observed AFTER the board's instant must not count: that is
    the as-of discipline that keeps a pinned board reproducible."""
    from datetime import timedelta

    from engine.ingest import reports as R

    monkeypatch.setenv("RADAR_REPORT_LOG", str(tmp_path / "reports.jsonl"))
    green = next(a for a in _on_map(fleet) if a["status"] == "green")
    shipment = next(s for s in context.shipments if s.shipment_id == green["id"])
    as_of = context.clock.as_of

    later = R.validate({"shipment_id": green["id"], "status": "stopped",
                        "load_state": "intact", "confirms_disruption": True,
                        "observed_at": (as_of + timedelta(hours=2)).isoformat()},
                       as_of + timedelta(hours=2))
    R.append(later)
    assert assets_mod.status_of(context, shipment)["level"] == "green"

    earlier = R.validate({"shipment_id": green["id"], "status": "stopped",
                          "load_state": "intact", "confirms_disruption": True,
                          "observed_at": (as_of - timedelta(hours=1)).isoformat()},
                         as_of - timedelta(hours=1))
    R.append(earlier)
    status = assets_mod.status_of(context, shipment)
    assert status["level"] == "red" and status["field_stoppage"]
    refreshed = assets_mod.fleet_assets(board, context)
    assert next(a for a in refreshed["assets"] if a["id"] == green["id"])["status"] == "red"
    detail = assets_mod.asset_detail(board, context, green["id"])
    assert any(log["kind"] == "field report" for log in detail["logs"])


def test_weights_that_are_not_numbers_weigh_nothing(board, context, fleet):
    a, _ = _disrupted_with_routes(board, context, fleet)
    routes = reroute.recovery(board, context, a["id"],
                              weights={"time": "fast", "cost": None, "risk": 1})
    assert routes["weights"] == {"time": 0.0, "cost": 0.0, "risk": 1.0}


# =====================================================================
# The basemap
# =====================================================================


def test_the_basemap_never_asks_openstreetmaps_own_servers():
    """They refuse apps like this one with an image reading "Access blocked",
    served as a normal tile — the board showed a wall of them, and the map
    could not tell that anything had failed."""
    providers = settings(load_config())["basemap"]["providers"]
    assert len(providers) >= 2, "one provider refusing must not leave the map bare"
    for provider in providers:
        for url in provider["tiles"]:
            assert "tile.openstreetmap.org" not in url, provider["name"]


def test_every_basemap_provider_is_keyless_and_attributed():
    """Nothing to bill and nothing to leak: no key in any URL, https only,
    and the attribution each provider's terms ask for."""
    for provider in settings(load_config())["basemap"]["providers"]:
        assert provider["attribution"], provider["name"]
        for url in provider["tiles"]:
            assert url.startswith("https://"), url
            assert all(t in url for t in ("{z}", "{x}", "{y}")), url
            assert not any(k in url.lower() for k in ("key=", "token=", "access_token")), url


def test_a_self_hosted_tile_server_is_used_alone():
    """The short `tiles:` form is what somebody self-hosting writes, and they
    do not want the browser falling back to a third party."""
    from engine.fleet.settings import basemap

    own = basemap({"tiles": ["https://tiles.example.internal/{z}/{x}/{y}.png"],
                   "providers": [{"name": "CARTO", "tiles": ["https://c/{z}/{x}/{y}.png"]}]})
    assert [p["tiles"] for p in own["providers"]] == [
        ["https://tiles.example.internal/{z}/{x}/{y}.png"]
    ]


def test_a_provider_with_no_tile_template_is_dropped():
    from engine.fleet.settings import basemap

    kept = basemap({"providers": [{"name": "empty"}, {"name": "broken", "tiles": ["https://x/"]},
                                  {"name": "good", "tiles": "https://g/{z}/{x}/{y}.png"}]})
    assert [p["name"] for p in kept["providers"]] == ["good"]


def test_the_map_is_told_the_providers_in_order():
    from api import main

    meta = json.loads(main.map_assets(as_of=AS_OF.as_of.isoformat(), shipments=150).body)["meta"]
    names = [p["name"] for p in meta["basemap"]["providers"]]
    assert names == [p["name"] for p in settings(load_config())["basemap"]["providers"]]
