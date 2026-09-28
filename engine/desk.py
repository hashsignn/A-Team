"""How the desk works: who owns a route, who comes first, how the room meets.

Everything here answers something the Sika team said in review, in their own
terms (config.example/desk.yaml quotes each one):

  sites          "Planners allocate work by origin site rather than by route."
                 A route belongs to the site its freight ships from, so a
                 planner can open the board on their own site.
  customers      "Filter by customer importance to ensure high-priority
                 contracts are serviced regardless of the crisis." Importance
                 is who is served FIRST, which is not what a delay costs them
                 (that is the impact tier, scoring.yaml).
  all_hands      "Cross-functional all-hands meetings increase in frequency
                 from biweekly to daily" once a crisis looms. The convene
                 rule's posture picks the cadence; the tool never declares.
  levers         What each function in that room can pull, computed from the
                 board: Procurement finds another source, Manufacturing runs
                 faster, Controlling raises the approval limit, Supply Chain
                 moves the freight.

Nothing here changes a number the engine computed. It sorts, groups and
proposes; the room decides.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from engine.clock import Clock
from engine.config import Config
from engine.schemas import ConveneVerdict, Shipment, ShipmentRisk

PRIORITY_ORDER = ("A", "B", "C")

# Hours to stage an order at another site before it can leave: pick, pack,
# book. ASSUMED — the alternative site still has to find the stock.
HANDOVER_HOURS = 48.0

# How far a production run is assumed to be pulled forward, at most. ASSUMED:
# two weeks is a planning horizon, not a line's measured capability.
MAX_PULL_DAYS = 14


# =====================================================================
# Sites
# =====================================================================
def _configured_sites(config: Config) -> list[dict]:
    return [s for s in (config.desk.get("sites") or []) if isinstance(s, dict) and s.get("id")]


def _by_node(config: Config) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for site in _configured_sites(config):
        for node_id in site.get("ships_from") or []:
            out.setdefault(node_id, site)
    return out


def _site_view(site: dict) -> dict:
    return {
        "id": str(site["id"]),
        "name": str(site.get("name") or site["id"]),
        "country": site.get("country"),
        "planner": site.get("planner"),
        "ships_from": list(site.get("ships_from") or []),
    }


def site_of_lane(lane: dict, config: Config) -> dict:
    """The site a route ships from.

    A lane's own ``site:`` wins. Otherwise the first place on the lane that a
    site ships from. A lane that starts nowhere listed falls under its own
    first node, so every route is somebody's."""
    configured = {str(s["id"]): s for s in _configured_sites(config)}
    if lane.get("site") in configured:
        return _site_view(configured[lane["site"]])
    by_node = _by_node(config)
    legs = lane.get("legs") or []
    for node_id in [legs[0]["from"], *(leg["to"] for leg in legs)] if legs else []:
        if node_id in by_node:
            return _site_view(by_node[node_id])
    origin = legs[0]["from"] if legs else lane["id"]
    node = config.nodes.get(origin)
    return {"id": origin, "name": node.name if node else origin, "country": node.country if node else None,
            "planner": None, "ships_from": [origin]}


def sites(config: Config) -> list[dict]:
    """Every site that ships at least one route, in the order desk.yaml lists
    them, then any a lane starts at that desk.yaml does not name."""
    used: dict[str, dict] = {}
    for lane in config.lanes:
        site = site_of_lane(lane, config)
        used.setdefault(site["id"], site)
    order = [str(s["id"]) for s in _configured_sites(config)]
    ranked = sorted(used.values(), key=lambda s: (order.index(s["id"]) if s["id"] in order else len(order),
                                                  s["name"]))
    return ranked


# =====================================================================
# Customers
# =====================================================================
def tiers(config: Config) -> dict[str, dict]:
    raw = (config.desk.get("customers") or {}).get("tiers") or {}
    out = {}
    for key in PRIORITY_ORDER:
        entry = raw.get(key) or {}
        out[key] = {
            "label": entry.get("label") or {"A": "Key account", "B": "Standard", "C": "Flexible"}[key],
            "rule": " ".join(str(entry.get("rule") or "").split()),
        }
    return out


def priority_of(customer: str, config: Config) -> str:
    block = config.desk.get("customers") or {}
    table = {str(k).casefold(): str(v).upper() for k, v in (block.get("by_customer") or {}).items()}
    value = table.get((customer or "").casefold()) or str(block.get("default") or "B").upper()
    return value if value in PRIORITY_ORDER else "B"


def priority_rank(customer: str, config: Config) -> int:
    return PRIORITY_ORDER.index(priority_of(customer, config))


def route_customers(shipments: list[Shipment], risks: list[ShipmentRisk], config: Config) -> list[dict]:
    """Who this route serves, most important first, with what is at risk."""
    loss: dict[str, float] = {}
    for risk in risks:
        loss[risk.shipment_id] = max(loss.get(risk.shipment_id, 0.0), risk.do_nothing.expected_loss_chf)
    rows: dict[str, dict] = {}
    for s in shipments:
        row = rows.setdefault(s.customer, {
            "name": s.customer, "priority": priority_of(s.customer, config),
            "shipments": 0, "at_risk": 0, "expected_loss_chf": 0.0,
        })
        row["shipments"] += 1
        if loss.get(s.shipment_id, 0.0) > 0:
            row["at_risk"] += 1
            row["expected_loss_chf"] += loss[s.shipment_id]
    out = sorted(rows.values(), key=lambda r: (PRIORITY_ORDER.index(r["priority"]),
                                               -r["expected_loss_chf"], r["name"]))
    for row in out:
        row["expected_loss_chf"] = round(row["expected_loss_chf"], 2)
    return out


# =====================================================================
# The all-hands meeting
# =====================================================================
_CADENCE_WORDS = {1: "daily", 7: "weekly", 14: "every two weeks", 28: "every four weeks"}


def cadence_words(days: float) -> str:
    return _CADENCE_WORDS.get(int(days), f"every {days:g} days")


def _hands(config: Config) -> dict:
    return config.desk.get("all_hands") or {}


def cadence(posture: str, config: Config) -> float:
    table = _hands(config).get("cadence_days") or {}
    fallback = {"normal": config.scoring.get("convene_rule", {}).get("meeting_cadence_days") or 14,
                "watch": 7,
                "convene": config.scoring.get("convene_rule", {}).get("escalated_cadence_days") or 1}
    return float(table.get(posture) or fallback.get(posture, 14))


def next_meeting(posture: str, config: Config, clock: Clock) -> datetime | None:
    """When the all-hands next sits at the cadence this posture calls for.

    Daily means working days: the room that meets every morning in a crisis
    does not also meet on a Sunday unless it decides to. Longer cadences are
    counted forward from the anchor, a date the meeting really sat."""
    hands = _hands(config)
    days = cadence(posture, config)
    if days <= 1:
        hh, mm = (str(hands.get("daily_at") or "08:30").split(":") + ["0"])[:2]
        at = clock.as_of.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        while at <= clock.as_of or at.weekday() >= 5:
            at += timedelta(days=1)
        return at
    anchor_text = hands.get("anchor")
    if not anchor_text:
        return None
    anchor = datetime.fromisoformat(str(anchor_text))
    step = timedelta(days=days)
    if anchor > clock.as_of:
        return anchor
    k = math.floor((clock.as_of - anchor) / step) + 1
    return anchor + k * step


def attendees(config: Config) -> list[dict]:
    wanted = _hands(config).get("functions") or [
        "FN_SUPPLY_CHAIN", "FN_PROCUREMENT", "FN_MANUFACTURING", "FN_CONTROLLING"]
    internal = {e["id"]: e for e in config.contacts.get("internal", [])}
    return [{"id": fn, "function": internal[fn]["function"], "role": internal[fn]["role"],
             "email": internal[fn].get("contact"), "phone": internal[fn].get("phone")}
            for fn in wanted if fn in internal]


def meeting(verdict: ConveneVerdict, config: Config, clock: Clock) -> dict:
    posture = verdict.posture.value
    days = cadence(posture, config)
    normal = cadence("normal", config)
    at = next_meeting(posture, config, clock)
    regular = next_meeting("normal", config, clock)
    if posture == "convene":
        change = "Convene rule crossed" + ("" if verdict.rule_agreed else " (rule not yet agreed)")
    elif posture == "watch":
        change = "Half-way to the convene rule: meet sooner"
    else:
        change = "As planned"
    return {
        "posture": posture,
        "cadence_days": days,
        "cadence_label": cadence_words(days),
        "normal_label": cadence_words(normal),
        "changed": days != normal,
        "change": change,
        "next_at": at.isoformat() if at else None,
        "next_label": f"{at:%a %d %b, %H:%M} UTC" if at else "not scheduled",
        "regular_at": regular.isoformat() if regular else None,
        "attendees": attendees(config),
        "rule_agreed": verdict.rule_agreed,
        "triggers": list(verdict.triggers_fired),
        "checks": rule_checks(verdict, config),
    }


def rule_checks(verdict: ConveneVerdict, config: Config) -> list[dict]:
    """The convene rule's three tests as rows — value, limit, crossed —
    so the room reads a table, not three sentences."""
    limits = (config.scoring.get("convene_rule") or {}).get("thresholds") or {}
    rows = [
        ("Expected loss", verdict.exposure_chf, limits.get("exposure_chf"), "chf"),
        ("Customers exposed", verdict.contracts_exposed, limits.get("contracts_exposed"), "n"),
        ("Decisions due in 48 h", verdict.shipments_needing_decision,
         limits.get("shipments_needing_decision"), "n"),
    ]
    return [{"label": label, "value": value, "limit": limit, "unit": unit,
             "crossed": limit is not None and value >= limit}
            for label, value, limit, unit in rows]


# =====================================================================
# Levers — what each function in the room can pull
# =====================================================================
def _lane_hours(lane: dict) -> float:
    return sum(float(leg["transit_hours"]) + float(leg.get("buffer_hours", 0)) for leg in lane["legs"])


def _worst(risks: list[ShipmentRisk]) -> dict[str, ShipmentRisk]:
    out: dict[str, ShipmentRisk] = {}
    for risk in risks:
        seen = out.get(risk.shipment_id)
        if seen is None or risk.do_nothing.expected_loss_chf > seen.do_nothing.expected_loss_chf:
            out[risk.shipment_id] = risk
    return out


def levers(routes: list[dict], shipments: list[Shipment], risks: list[ShipmentRisk],
           config: Config, clock: Clock, posture: str) -> list[dict]:
    """One card per function. Each is a proposal the room can accept or
    reject — never an action taken."""
    by_id = {s.shipment_id: s for s in shipments}
    lanes = {lane["id"]: lane for lane in config.lanes}
    level = {r["route_id"]: r["level"] for r in routes}
    urgent = {rid for rid, lvl in level.items() if lvl in ("red", "yellow")}
    calm = {rid for rid, lvl in level.items() if lvl in ("green", "white", "blue")}

    worst = _worst(risks)
    at_risk = [(by_id[sid], r) for sid, r in worst.items()
               if sid in by_id and r.do_nothing.expected_loss_chf > 0 and by_id[sid].lane_id in urgent]
    at_risk.sort(key=lambda sr: (priority_rank(sr[0].customer, config), -sr[1].do_nothing.expected_loss_chf))

    return [
        _supply_chain(routes, config),
        _procurement(at_risk, lanes, calm, config, clock),
        _manufacturing(at_risk, lanes, config),
        _controlling(routes, config, posture),
    ]


def _supply_chain(routes: list[dict], config: Config) -> dict:
    actions = [a | {"route_id": r["route_id"], "route": r["name"]}
               for r in routes for a in r.get("actions") or [] if r["level"] in ("red", "yellow", "blue")]
    actions.sort(key=lambda a: (PRIORITY_ORDER.index(a.get("customer_priority", "B")),
                                a["lead_time_hours"] if a["lead_time_hours"] is not None else 1e9))
    # One shipment under two events carries the same action twice; the room
    # books it once.
    seen: set[tuple[str, str]] = set()
    actions = [a for a in actions
               if (a["shipment_id"], a["label"]) not in seen
               and not seen.add((a["shipment_id"], a["label"]))]
    first = min((a["lead_time_hours"] for a in actions if a["lead_time_hours"] is not None), default=None)
    items = [{"text": f"{a['label']} · {a['shipment_id']}",
              "detail": f"{a['customer']} · {a['route']}", "priority": a.get("customer_priority", "B"),
              "route_id": a["route_id"], "lead_time_hours": a["lead_time_hours"]}
             for a in actions[:6]]
    return {
        "function": "Supply Chain", "id": "FN_SUPPLY_CHAIN",
        "lever": "Move the freight",
        "summary": ((f"{len(actions)} actions open"
                     + (f" · first closes in {first:.0f} h" if first is not None else ""))
                    if actions else "No action worth its cost"),
        "items": items,
        "basis": "The route actions: key accounts first, then the nearest deadline.",
    }


def _procurement(at_risk: list[tuple[Shipment, ShipmentRisk]], lanes: dict, calm: set[str],
                 config: Config, clock: Clock) -> dict:
    """Another source: the same destination served from a different site
    whose own route is calm. For the receiving company that IS an alternative
    supplier; for raw materials the model has no supplier list to offer."""
    groups: dict[tuple[str, str], dict] = {}
    for shipment, _risk in at_risk:
        lane = lanes.get(shipment.lane_id)
        if lane is None:
            continue
        own = site_of_lane(lane, config)
        best = None
        for other_id in calm:
            other = lanes[other_id]
            if other["legs"][-1]["to"] != shipment.destination_node:
                continue
            site = site_of_lane(other, config)
            if site["id"] == own["id"]:
                continue
            arrive = clock.as_of + timedelta(hours=HANDOVER_HOURS + _lane_hours(other))
            in_time = arrive <= shipment.otif_committed_date
            key = (not in_time, _lane_hours(other))
            if best is None or key < best[0]:
                best = (key, other, site, in_time, arrive)
        if best is None:
            continue
        _, other, site, in_time, arrive = best
        g = groups.setdefault((own["id"], other["id"]), {
            "from_site": own["name"], "to_site": site["name"], "route_id": other["id"],
            "route": other["name"], "orders": [], "customers": set(), "key_accounts": 0,
            "value_chf": 0.0, "in_time": 0,
        })
        g["orders"].append(shipment.shipment_id)
        g["customers"].add(shipment.customer)
        g["key_accounts"] += priority_of(shipment.customer, config) == "A"
        g["value_chf"] += shipment.value_chf
        g["in_time"] += in_time
    # A source that still misses the date only moves the lateness somewhere
    # else; it is counted, not proposed.
    missing = sum(len(g["orders"]) for g in groups.values() if not g["in_time"])
    groups = {k: g for k, g in groups.items() if g["in_time"]}
    items = []
    for g in sorted(groups.values(), key=lambda g: (-g["key_accounts"], -g["value_chf"])):
        items.append({
            "text": f"{g['to_site']} instead of {g['from_site']} · {len(g['orders'])} orders",
            "detail": (f"{g['in_time']}/{len(g['orders'])} on time · CHF {g['value_chf']:,.0f} · "
                       f"via {g['route']}"),
            "hint": ", ".join(sorted(g["customers"])),
            "priority": "A" if g["key_accounts"] else "B",
            "route_id": g["route_id"], "orders": g["orders"],
        })
    return {
        "function": "Procurement", "id": "FN_PROCUREMENT",
        "lever": "Find another source",
        "summary": (f"{sum(len(g['orders']) for g in groups.values())} orders can ship from "
                    "another site, on time" if groups else "No other site can serve these on time"),
        "items": items[:6],
        "basis": (f"Same destination, another site, a calm route, on time. Handover "
                  f"{HANDOVER_HOURS:.0f} h assumed."
                  + (f" {missing} more would still be late." if missing else "")
                  + " Raw-material suppliers are not modelled."),
    }


def _manufacturing(at_risk: list[tuple[Shipment, ShipmentRisk]], lanes: dict, config: Config) -> dict:
    """Run faster where the late orders come from: the days an earlier batch
    has to buy are the lateness the Monte Carlo expects if nobody acts."""
    groups: dict[tuple[str, str], dict] = {}
    for shipment, risk in at_risk:
        lane = lanes.get(shipment.lane_id)
        if lane is None:
            continue
        site = site_of_lane(lane, config)
        g = groups.setdefault((site["id"], shipment.product_family), {
            "site": site["name"], "product": shipment.product_family.replace("_", " "),
            "orders": 0, "key_accounts": 0, "value_chf": 0.0, "days": 0.0, "customers": set(),
        })
        g["orders"] += 1
        g["key_accounts"] += priority_of(shipment.customer, config) == "A"
        g["value_chf"] += shipment.value_chf
        g["days"] = max(g["days"], risk.do_nothing.expected_lateness_days)
        g["customers"].add(shipment.customer)
    items = []
    for g in sorted(groups.values(), key=lambda g: (-g["key_accounts"], -g["value_chf"])):
        if g["days"] <= 0:
            continue
        late = math.ceil(g["days"])
        text = (f"{g['product'].capitalize()} at {g['site']}: {late} days sooner"
                if late <= MAX_PULL_DAYS else
                f"{g['product'].capitalize()} at {g['site']}: as early as possible")
        items.append({
            "text": text,
            "detail": (f"{g['orders']} late orders · {g['key_accounts']} key accounts · "
                       f"expected {late} days late · CHF {g['value_chf']:,.0f}"),
            "hint": ", ".join(sorted(g["customers"])),
            "priority": "A" if g["key_accounts"] else "B",
        })
    return {
        "function": "Manufacturing", "id": "FN_MANUFACTURING",
        "lever": "Increase production speed",
        "summary": (f"{len(items)} product runs to bring forward" if items else
                    "No order on the urgent routes runs late"),
        "items": items[:6],
        "basis": (f"Days = expected lateness if nobody acts. Beyond {MAX_PULL_DAYS} days, pair it "
                  "with another source. Line capacity is Manufacturing's call."),
    }


def crisis_limit(config: Config) -> tuple[float | None, float | None]:
    normal = (config.contacts.get("approval") or {}).get("delegated_limit_chf")
    if normal is None:
        return None, None
    rule = config.desk.get("crisis_authority") or {}
    raised = float(normal) * float(rule.get("multiplier", 5))
    if rule.get("cap_chf") is not None:
        raised = min(raised, float(rule["cap_chf"]))
    return float(normal), raised


def _controlling(routes: list[dict], config: Config, posture: str) -> dict:
    normal, raised = crisis_limit(config)
    actions = [a for r in routes for a in r.get("actions") or []]
    if normal is None:
        return {"function": "Controlling", "id": "FN_CONTROLLING", "lever": "Raise authority limits",
                "summary": "No delegated limit is configured (contacts.yaml → approval).",
                "items": [], "basis": ""}
    above = [a for a in actions if a["cost_chf"] > normal]
    freed = [a for a in above if a["cost_chf"] <= raised]
    still = [a for a in above if a["cost_chf"] > raised]
    rule = config.desk.get("crisis_authority") or {}
    if posture == "convene":
        summary = f"Raise the planner limit: CHF {normal:,.0f} → {raised:,.0f}"
        note = (f"Frees {len(freed)} of {len(above)} actions from approval"
                + (f" · {len(still)} still need it" if still else "")
                if above else "No action is above today's limit yet")
    elif posture == "watch":
        summary = f"Agree a crisis limit now: CHF {raised:,.0f}"
        note = f"Normally CHF {normal:,.0f}"
    else:
        summary = f"Limit CHF {normal:,.0f}"
        note = "No crisis limit"
    items = [{"text": f"{a['label']} · CHF {a['cost_chf']:,.0f}",
              "detail": f"{a['shipment_id']} · {a['customer']}",
              "priority": a.get("customer_priority", "B")} for a in freed[:6]]
    return {
        "function": "Controlling", "id": "FN_CONTROLLING",
        "lever": "Raise authority limits",
        "summary": summary,
        "note": note,
        "items": items,
        "limit_chf": normal, "crisis_limit_chf": raised,
        "active": posture == "convene",
        "basis": (" ".join(str(rule.get("applies_to") or "").split())
                  + " Proposed by the board, granted only by Controlling in the room."),
    }


def key_accounts(routes: list[dict], shipments: list[Shipment], risks: list[ShipmentRisk],
                 config: Config) -> list[dict]:
    """Every key-account order at risk, on every site — the list that is
    shown whatever filter a planner has on, because those contracts are kept
    whatever the crisis."""
    by_id = {s.shipment_id: s for s in shipments}
    route = {r["route_id"]: r for r in routes}
    lanes = {lane["id"]: lane for lane in config.lanes}
    out = []
    for sid, risk in _worst(risks).items():
        s = by_id.get(sid)
        if s is None or risk.do_nothing.expected_loss_chf <= 0 or priority_of(s.customer, config) != "A":
            continue
        r = route.get(s.lane_id, {})
        action = next((a for a in r.get("actions") or [] if a["shipment_id"] == sid), None)
        out.append({
            "shipment_id": sid, "customer": s.customer, "route_id": s.lane_id,
            "route": r.get("name", s.lane_id), "level": r.get("level"),
            "site": site_of_lane(lanes[s.lane_id], config)["id"] if s.lane_id in lanes else None,
            "lead_time_hours": risk.lead_time_hours,
            "p_late": round(risk.do_nothing.p_late, 3),
            "expected_loss_chf": round(risk.do_nothing.expected_loss_chf, 2),
            "action": action["label"] if action else None,
        })
    out.sort(key=lambda k: (k["lead_time_hours"] if k["lead_time_hours"] is not None else 1e9,
                            -k["expected_loss_chf"]))
    return out
