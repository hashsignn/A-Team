"""The decision tree for ONE shipment (engine/export/shipment.py).

Each order on a disrupted route has its own ways out, ranked the way the
Action Hub ranks them under "Plan recovery". What must hold: one pool,
ranked once, the plan in it; the best is rank 1; days late count from the
promised date; a way on time says when it closes; a partner marked as
serving a way can take it; the box above a branch says what the branch
shows; and booking books only a way the engine offered.

The handlers are called directly, as in test_fast_api.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import api.main as main
from api import fast_routes
from engine.export.shipment import late_days
from engine.fast.execute import LEDGER

AS_OF = "2026-09-26T23:00:00Z"
SHIPMENTS = 150


def body(response):
    return json.loads(response.body)


def shipment(sid: str, **weights) -> dict:
    w = {f"w_{k}": v for k, v in weights.items()}
    return body(main.decision_shipment(sid, as_of=AS_OF, shipments=SHIPMENTS,
                                       w_time=w.get("w_time"), w_cost=w.get("w_cost"),
                                       w_risk=w.get("w_risk")))


@pytest.fixture(scope="module")
def routes() -> dict[str, dict]:
    """Every route with orders that need a decision, with each order's
    one-line verdict (?ways=1)."""
    board = main._board(AS_OF, SHIPMENTS)
    out = {}
    for r in board["routes"]:
        if not r["shipments_at_risk"]:
            continue
        d = body(main.decision(r["route_id"], as_of=AS_OF, shipments=SHIPMENTS, ways=True))
        if any(o["branch"] != "absorbed" for o in d["orders"]):
            out[r["route_id"]] = d
    assert out, "the demo board has orders that need a decision"
    return out


@pytest.fixture(scope="module")
def ships(routes) -> dict[str, dict]:
    return {o["shipment_id"]: shipment(o["shipment_id"])
            for d in routes.values() for o in d["orders"] if o["branch"] != "absorbed"}


@pytest.fixture(autouse=True)
def clean():
    fast_routes.bind(main._context)
    LEDGER.clear()
    yield
    LEDGER.clear()


def pool(data: dict) -> list[dict]:
    return sorted(data["options"] + ([data["stay"]] if data["stay"] else []), key=lambda o: o["rank"])


# =====================================================================
# One pool, ranked once
# =====================================================================


def test_each_shipment_ranks_its_ways_once_with_the_plan_in_the_pool(ships):
    located = 0
    for sid, d in ships.items():
        if not d["located"]:
            continue
        located += 1
        ways = pool(d)
        assert d["stay"] and d["stay"]["plan"], sid
        assert [o["rank"] for o in ways] == list(range(1, len(ways) + 1)), sid
        assert d["best"] == ways[0]["id"], sid
        assert [o["id"] for o in ways if o["best"]] == [d["best"]], sid
        assert d["stay_best"] is ways[0]["plan"], sid
        assert not any(o["plan"] for o in d["options"]), sid
        # Lower score is better, and the ranks follow it.
        scores = [o["score"] for o in ways if o["score"] is not None]
        assert scores == sorted(scores), sid
    assert located, "the demo board has shipments on the map"


def test_a_shipment_the_map_cannot_place_says_so(ships, routes):
    """Past its planned arrival the map has no position for it (it counts as
    delivered there), so no route from "here" exists. The branch says that
    and goes to telling the customer; it never shows an empty choice."""
    verdicts = {sid: v for d in routes.values() for sid, v in d["ways"].items()}
    for sid, d in ships.items():
        if d["located"]:
            continue
        assert not d["options"] and d["stay"] is None and d["best"] is None, sid
        assert "planned arrival" in d["note"], sid
        assert verdicts[sid]["located"] is False and verdicts[sid]["late_days"] is None, sid


def test_fastest_puts_the_earliest_arrival_first(ships):
    """The weights reorder the ways; they never add or remove one."""
    checked = 0
    for sid, d in ships.items():
        if not d["options"]:
            continue
        fast = pool(shipment(sid, time=100, cost=0, risk=0))
        assert {o["id"] for o in fast} == {o["id"] for o in pool(d)}, sid
        etas = [datetime.fromisoformat(o["eta"]) for o in fast]
        assert etas[0] - min(etas) <= timedelta(hours=1), sid
        checked += 1
    assert checked, "at least one shipment on the demo board has a way round"


# =====================================================================
# Late means after the promise
# =====================================================================


def test_days_late_count_from_the_promised_date():
    """The recovery engine's delay_hours is the delay against the PLANNED
    arrival; a plan with slack can be delayed and still land on time."""
    assert late_days("2026-10-10T12:00:00+00:00", "2026-10-08T00:00:00+00:00") == 2.5
    assert late_days("2026-10-06T00:00:00+00:00", "2026-10-08T00:00:00+00:00") == 0.0
    assert late_days(None, "2026-10-08T00:00:00+00:00") == 0.0
    assert late_days("not a date", "2026-10-08T00:00:00+00:00") == 0.0


def test_a_way_is_on_time_exactly_when_it_lands_by_the_promise(ships):
    for sid, d in ships.items():
        promised = datetime.fromisoformat(d["order"]["committed"])
        for o in pool(d):
            eta = datetime.fromisoformat(o["eta"])
            assert o["on_time"] is (eta <= promised), (sid, o["id"])
            if o["on_time"]:
                assert o["late_days"] == 0.0, (sid, o["id"])
            else:
                assert o["late_days"] == round((eta - promised).total_seconds() / 86400, 1), (sid, o["id"])


def test_a_way_on_time_says_when_it_closes(ships):
    as_of = datetime.fromisoformat(AS_OF)
    seen = 0
    for sid, d in ships.items():
        assert d["stay"] is None or d["stay"]["closes_at"] is None, sid
        for o in d["options"]:
            if not o["on_time"]:
                assert o["closes_at"] is None, (sid, o["id"])
                continue
            seen += 1
            closes = datetime.fromisoformat(o["closes_at"])
            assert closes >= as_of, (sid, o["id"])
            # Its slack to the promise, from now: start later and it misses.
            slack = datetime.fromisoformat(d["order"]["committed"]) - datetime.fromisoformat(o["eta"])
            assert abs((closes - as_of) - slack).total_seconds() < 1, (sid, o["id"])
    assert seen, "at least one shipment on the demo board has a way that keeps its date"


# =====================================================================
# Who carries it
# =====================================================================


def test_a_partner_marked_as_serving_a_way_can_take_it(ships):
    served = 0
    for sid, d in ships.items():
        ids = {o["id"] for o in pool(d)}
        partners = d["partners"]
        for p in partners:
            assert set(p["serves"]) <= ids, (sid, p["id"])
            assert set(p["serves"]) <= set(p["reasons"]), (sid, p["id"])
            served += bool(p["serves"])
        # Those who can take a way are listed first.
        flags = [bool(p["serves"]) for p in partners]
        assert flags == sorted(flags, reverse=True), sid
    assert served, "at least one partner can carry a way"


# =====================================================================
# The box says what its branch shows
# =====================================================================


def test_a_shipment_box_says_what_its_branch_shows(routes, ships):
    for route_id, d in routes.items():
        need = {o["shipment_id"] for o in d["orders"] if o["branch"] != "absorbed"}
        assert set(d["ways"]) == need, route_id
        for sid, v in d["ways"].items():
            s = ships[sid]
            assert v["located"] is s["located"], sid
            if not s["located"]:
                continue
            assert v["ways"] == len(s["options"]), sid
            assert v["on_time"] == sum(o["on_time"] for o in s["options"]), sid
            assert v["plan_on_time"] is s["stay"]["on_time"], sid
            assert v["late_days"] == s["stay"]["late_days"], sid
            assert v["plan_eta"] == s["stay"]["eta"], sid


def test_the_route_tree_is_unchanged_without_ways(routes):
    route_id = next(iter(routes))
    plain = body(main.decision(route_id, as_of=AS_OF, shipments=SHIPMENTS, ways=False))
    assert "ways" not in plain
    assert plain["orders"] == routes[route_id]["orders"]


def test_an_unknown_shipment_is_a_404():
    with pytest.raises(HTTPException) as exc:
        shipment("SYN-NOPE")
    assert exc.value.status_code == 404


# =====================================================================
# Booking
# =====================================================================


def request(site: str | None = "same-origin"):
    from starlette.requests import Request  # noqa: PLC0415

    headers = [(b"sec-fetch-site", site.encode())] if site else []
    return Request({"type": "http", "method": "POST", "path": "/", "query_string": b"",
                    "headers": headers})


def book(sid: str, payload: dict, site: str | None = "same-origin"):
    return main.decision_shipment_book(sid, payload, request(site), as_of=AS_OF, shipments=SHIPMENTS)


def bookable(ships) -> tuple[str, dict]:
    """A way we can book ourselves: ours to pull, and within the margin."""
    for sid, d in ships.items():
        for o in d["options"]:
            if o["owner"] != "us":
                continue
            response = book(sid, {"option_id": o["id"]})
            if response.status_code == 200:
                LEDGER.clear()
                return sid, o
    pytest.skip("no way on the demo board is ours to book within the margin")


def test_a_booked_way_is_the_engines_not_the_clients(ships):
    """The body names the way; everything else is re-derived. A client that
    sent its own price or label books the engine's, not its own."""
    sid, o = bookable(ships)
    done = body(book(sid, {"option_id": o["id"], "cost_chf": 0, "label": "free ride",
                           "on_time": True, "shipment_id": "SYN-OTHER"}))
    assert done["ok"] is True and not done["refused"]
    (executed,) = done["executed"]
    assert executed["option_id"] == f"{sid}:recovery:{o['id']}"
    assert executed["shipment_id"] == sid
    assert executed["label"] == o["label"]
    assert executed["cost_chf"] == pytest.approx(max(0.0, o["extra_chf"]), abs=0.01)
    assert len(LEDGER) == 1


def test_a_booked_way_can_be_pulled_back(ships):
    sid, o = bookable(ships)
    done = body(book(sid, {"option_id": o["id"]}))
    result = body(fast_routes.undo({"execution_ids": [e["execution_id"] for e in done["executed"]]},
                                   as_of=AS_OF, shipments=SHIPMENTS))
    assert len(result["undone"]) == 1 and not result["refused"]


def test_another_sites_page_cannot_book(ships):
    """A page elsewhere could post to this server from the planner's own
    browser; the browser marks such a request cross-site, and it is refused
    before anything is looked up. A script with no browser header is not."""
    sid, o = bookable(ships)
    for site in ("cross-site", "same-site"):
        with pytest.raises(HTTPException) as exc:
            book(sid, {"option_id": o["id"]}, site=site)
        assert exc.value.status_code == 403
    assert len(LEDGER) == 0
    assert book(sid, {"option_id": o["id"]}, site=None).status_code == 200


def test_a_way_that_was_not_offered_is_refused_by_name(ships):
    sid = next(iter(ships))
    with pytest.raises(HTTPException) as exc:
        book(sid, {"option_id": "ALT-MADE-UP"})
    assert exc.value.status_code == 404
    assert "ALT-MADE-UP" in exc.value.detail
    with pytest.raises(HTTPException):
        book(sid, {})
    assert len(LEDGER) == 0


# =====================================================================
# The card's legs are the route page's row for that shipment
# =====================================================================


def test_a_shipments_legs_are_its_row_of_the_route_page(ships):
    """The Shipments tab's card and the route page colour a leg by the same
    rule (engine/export/route.leg_status), so they cannot disagree."""
    from engine.export import route as route_mod  # noqa: PLC0415

    board = main._board(AS_OF, SHIPMENTS)
    context = main._context(AS_OF, SHIPMENTS)
    views: dict[str, dict] = {}
    for sid, d in ships.items():
        journey = d["journey"]
        assert journey and journey["legs"], sid
        view = views.setdefault(d["route_id"], route_mod.route_view(board, context, d["route_id"]))
        for leg in journey["legs"]:
            assert leg["status"] in ("ok", "at_risk", "affected"), sid
            row = next(v for v in view["legs"][leg["index"]]["vehicles"] if v["shipment_id"] == sid)
            assert row["status"] == leg["status"], (sid, leg["index"])
        p = journey["progress"]
        assert 0 <= p["percent"] <= 100 and p["total_km"] >= p["remaining_km"] >= 0, sid
