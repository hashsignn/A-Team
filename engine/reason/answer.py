"""Answers from the board itself, with no model at all.

Ask used to need a language model running on the planner's machine, and
without one it said so and stopped. That is honest and useless: most of what
a planner asks ("which route first", "what about Kestrel", "where is
SYN-0041", "who do I call") is a lookup the board has already computed. So
this module answers those directly, from the same numbers on screen.

It reads everything the board carries: every route and its events, every
customer and order, the places on each route, the all-hands meeting, the
early-warning signals, the contacts, and (through ``tree_for``) the Action
decision tree of any route it names. What it cannot do is reason past the
data: "why would the strike spread" is a question for a model, and the
answer says so and says how to connect one.

Each answer is a first line that answers, then a few lines of numbers. It
comes back with ``links`` (open the route, open its decision tree) and the
``facts`` it used, which are also handed to a model when one is connected,
so a small local model starts from the right rows instead of guessing.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta

TreeFor = Callable[[str], dict | None]

URGENT = ("red", "yellow")
LEVEL_ORDER = {"red": 0, "yellow": 1, "blue": 2, "white": 3, "green": 4}

# Words too common in route and customer names to identify one on their own.
_COMMON = {
    "sika", "route", "routes", "port", "the", "and", "via", "group", "stores",
    "construction", "builders", "gmbh", "ltd", "inc", "assembly", "infrastructure",
    "projekt", "obras", "civiles", "canal", "strait", "rhine port", "arrives",
}

_HELP = (
    "Ask me about anything on the board, for example:\n"
    "• Which route needs a decision first?\n"
    "• What about the Rhine? · What about Houston?\n"
    "• Which key accounts are at risk? · What about Kestrel Auto Assembly?\n"
    "• Where is SYN-0041? · Who do I call on the Shanghai route?\n"
    "• What does it cost if nobody acts? · When is the all-hands?\n"
    "• Any early warnings? · What are the alternatives on the Rhine route?"
)


# ---------------------------------------------------------------- helpers
def fold(text: str) -> str:
    """Lower case, accents off: "Düdingen" and "dudingen" match."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", fold(text)))


def chf(value: float | None) -> str:
    # Half up, as the pages round: 181,096.5 is 181,097 everywhere.
    return "CHF –" if value is None else f"CHF {math.floor(float(value) + 0.5):,}"


def _hours(h: float | None, working: bool = True) -> str:
    if h is None:
        return "no deadline"
    unit = " working" if working else ""
    if h < 0.5:
        return "now"
    if h < 48:
        return f"{h:.0f}{unit} h"
    return f"{h / 24:.0f}{unit} days"


def _at(as_of: str, hours: float | None) -> str:
    if hours is None:
        return ""
    try:
        moment = datetime.fromisoformat(as_of) + timedelta(hours=float(hours))
    except (TypeError, ValueError):
        return ""
    return moment.strftime("%a %d %b, %H:%M UTC")


def orders(n: int) -> str:
    return f"{n} order{'' if n == 1 else 's'}"


def _short(name: str, n: int = 60) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def _has(q: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", q) for w in words)


def _urgency(route: dict) -> tuple:
    lead = route.get("lead_time_hours")
    return (LEVEL_ORDER.get(route.get("level"), 9), lead if lead is not None else 1e9,
            -(route.get("exposure_chf") or 0))


# ---------------------------------------------------------------- finding things
def _route_tokens(route: dict) -> dict[str, float]:
    """The words that name a route, and how much each says: a word in the
    route's own name (the Rhine in "Basel → Rhine → Rotterdam") counts for
    more than one only in a stop ("Basel (Rhine port)")."""
    tokens: dict[str, float] = {}
    for stop in route.get("stops") or []:
        for t in _words(stop.get("name", "")):
            tokens[t] = 0.5
    for t in _words(route.get("name", "")) | {fold(route["route_id"])}:
        tokens[t] = 1.0
    return {t: w for t, w in tokens.items() if len(t) >= 4 and t not in _COMMON}


def find_routes(board: dict, q: str) -> list[dict]:
    """Routes the question names: by id, or by a place on them. A place on
    many routes (the plant everything leaves from) counts for less than one
    on a single route."""
    words = _words(q)
    routes = board.get("routes") or []
    for r in routes:
        if fold(r["route_id"]) in fold(q):
            return [r]
    seen: dict[str, int] = {}
    for r in routes:
        for t in _route_tokens(r):
            seen[t] = seen.get(t, 0) + 1
    scored = []
    for r in routes:
        tokens = _route_tokens(r)
        hit = [t for t in tokens if t in words]
        if hit:
            score = sum(tokens[t] / seen[t] for t in hit)
            scored.append((score, r))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    out = [r for s, r in scored if s >= top - 1e-9]
    return sorted(out, key=_urgency)


def _customers(board: dict) -> dict[str, list[tuple[dict, dict]]]:
    out: dict[str, list[tuple[dict, dict]]] = {}
    for r in board.get("routes") or []:
        for c in r.get("customers") or []:
            out.setdefault(c["name"], []).append((r, c))
    return out


def find_customers(board: dict, q: str) -> list[str]:
    fq = fold(q)
    words = _words(q)
    names = list(_customers(board))
    full = [n for n in names if fold(n) in fq]
    if full:
        return full
    out = []
    for n in names:
        # The name's own first distinctive word, in order: "Kestrel" for
        # Kestrel Auto Assembly (a set would pick any of its words).
        distinctive = [w for w in re.findall(r"[a-z0-9]+", fold(n)) if len(w) >= 4 and w not in _COMMON]
        if distinctive and distinctive[0] in words:
            out.append(n)
    return out


def find_orders(q: str) -> list[str]:
    return sorted({m.upper() for m in re.findall(r"\b[a-z]{2,5}-\d{2,6}\b", fold(q))})


# ---------------------------------------------------------------- answers
def _route_line(board: dict, r: dict) -> str:
    lead = r.get("lead_time_hours")
    at = _at(board["as_of"], r.get("clock_hours"))
    if lead is not None:
        when = ("decide now, before the next working day" if lead < 0.5
                else f"decide within {_hours(lead)}") + (f" (by {at})" if at else "")
    elif r.get("level") in URGENT and (r.get("exposure_chf") or 0) > 0:
        # Urgent with nothing left to try: what is left is telling them.
        when = "no option left, tell the customer now"
    else:
        when = "no decision deadline"
    return (f"{r['level_label']} · {_short(r['name'])}: {when}, {chf(r.get('exposure_chf'))} at risk, "
            f"{r.get('shipments_at_risk', 0)} of {orders(r.get('shipments', 0))}")


def _tree_lines(board: dict, tree: dict | None) -> list[str]:
    """What the route's Action decision tree says, in three lines."""
    if not tree:
        return []
    keep, need = tree.get("keep") or {}, (tree.get("hit") or {}).get("need", 0)
    if not need:
        return ["Nothing to decide: the buffers absorb every order."]
    lines = []
    stay = next((c for c in tree.get("compare") or [] if c.get("baseline")), None)
    if keep.get("stay_best") and stay:
        lines.append(f"Best: stay as planned. {stay['on_time']} of {stay['orders']} likely on time, "
                     f"{chf(stay['exposure_chf'])} expected, less than any way costs.")
    elif keep.get("options"):
        o = keep["options"][0]
        closes = o.get("closes_at")
        lines.append(f"Best way: {o['label']}. {o['on_time']} of {o['orders']} on time, "
                     f"{'no extra cost' if not o['cost_chf'] else chf(o['cost_chf']) + ' extra'}"
                     + (f", closes {_fmt_iso(closes)}" if closes else "") + ".")
    kept = len(keep.get("kept") or [])
    reduced = len((tree.get("reduce") or {}).get("orders") or [])
    told = len((tree.get("tell") or {}).get("orders") or [])
    lines.append(f"Of {orders(need)}: {kept} keep their date, {reduced} cut the damage, {told} tell the customer.")
    return lines


def _fmt_iso(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%a %d %b, %H:%M UTC")
    except ValueError:
        return iso


def _links(route_id: str) -> list[dict]:
    return [{"label": "Open the route", "route_id": route_id},
            {"label": "Open its decision tree", "href": f"/tree?route={route_id}"}]


def _if_nobody_acts(tree: dict | None, r: dict) -> list[str]:
    """Staying as planned, in numbers: the loss, who is late, and why."""
    stay = next((c for c in (tree or {}).get("compare") or [] if c.get("baseline")), None)
    parts = ((tree or {}).get("cost") or {}).get("parts") or {}
    lines = [f"If nobody acts: {chf(r.get('exposure_chf'))} expected loss"
             + (f", {stay['on_time']} of {orders(stay['orders'])} on time, the latest about "
                f"{stay['late_after_days']:.0f} days late" if stay else "") + "."]
    drivers = [(k, parts.get(v)) for k, v in (("customer impact", "customer_impact"),
               ("expediting", "expediting"), ("surcharges", "surcharge"), ("penalties", "penalty"))]
    drivers = [(k, v) for k, v in drivers if v and v > 0.5]
    if drivers:
        lines.append("• Made of: " + ", ".join(f"{k} {chf(v)}" for k, v in sorted(drivers, key=lambda d: -d[1])))
    if parts.get("penalty_if_counted") and not ((tree or {}).get("cost") or {}).get("penalties_counted"):
        lines.append(f"• Contract delay penalties would add {chf(parts['penalty_if_counted'])} if counted.")
    return lines


def about_route(board: dict, r: dict, q: str, tree_for: TreeFor | None) -> dict:
    tree = tree_for(r["route_id"]) if tree_for else None
    lines = [_route_line(board, r)]
    if _has(q, "nobody", "do nothing", "if we wait", "no action", "happens if", "what if"):
        lines = _if_nobody_acts(tree, r) + lines
    events = r.get("events") or []
    if events:
        lines += [f"• {e.get('kind_label') or 'Event'}: {_short(e['title'], 90)} "
                  f"({orders(e.get('shipments_here', 0))})" for e in events[:3]]
    if _has(q, "alternat", "reroute", "option", "instead", "other way", "way", "how", "fix", "do we do"):
        if tree:
            for o in (tree.get("keep") or {}).get("options", [])[:3]:
                lines.append(f"• {o['label']}: {o['on_time']}/{o['orders']} on time, "
                             f"+{chf(o['cost_chf'])}, closes {_fmt_iso(o.get('closes_at'))}")
            for s in tree.get("sources") or []:
                lines.append(f"• Other site: {s['label']}, {s['on_time']}/{s['orders']} on time")
    lines += [f"• {x}" for x in _tree_lines(board, tree)]
    at_risk = [c for c in r.get("customers") or [] if c.get("at_risk")]
    if at_risk:
        names = ", ".join(f"{c['name']}{' (key account)' if c.get('priority') == 'A' else ''}"
                          for c in sorted(at_risk, key=lambda c: -(c.get('expected_loss_chf') or 0))[:4])
        lines.append(f"• Customers at risk: {names}")
    manager = ((r.get("response") or {}).get("route_manager") or {})
    if manager.get("name"):
        lines.append(f"• Route manager: {manager['name']}, {manager.get('phone', '')}")
    return {"answer": "\n".join(lines), "links": _links(r["route_id"]), "facts": lines,
            "route_id": r["route_id"]}


def top_priority(board: dict, tree_for: TreeFor | None) -> dict:
    urgent = sorted([r for r in board.get("routes") or [] if r.get("level") in URGENT], key=_urgency)
    if not urgent:
        watch = sorted([r for r in board.get("routes") or [] if r.get("shipments_at_risk")], key=_urgency)
        if not watch:
            return {"answer": "Nothing needs a decision: every route is Normal.", "facts": []}
        r = watch[0]
        return {"answer": "No route is Critical or Alert. The nearest to watch:\n" + _route_line(board, r),
                "links": _links(r["route_id"]), "facts": [], "route_id": r["route_id"]}
    first = urgent[0]
    reply = about_route(board, first, "", tree_for)
    rest = [f"• Then: {_route_line(board, r)}" for r in urgent[1:3]]
    reply["answer"] = "\n".join([f"First: {reply['answer']}", *rest])
    return reply


def about_customer(board: dict, name: str) -> dict:
    rows = _customers(board).get(name) or []
    orders = sum(c.get("shipments", 0) for _, c in rows)
    at_risk = sum(c.get("at_risk", 0) for _, c in rows)
    loss = sum(c.get("expected_loss_chf") or 0 for _, c in rows)
    tier = next((c.get("priority") for _, c in rows), "B")
    label = (board.get("priorities") or {}).get(tier, {}).get("label", tier)
    lines = [f"{name} ({label}): {at_risk} of {orders} orders at risk, {chf(loss)} if nobody acts."]
    for r, c in sorted(rows, key=lambda rc: _urgency(rc[0])):
        if not c.get("at_risk"):
            continue
        lines.append(f"• {r['level_label']} · {_short(r['name'], 50)}: {', '.join(c.get('orders_at_risk') or [])}"
                     f" ({chf(c.get('expected_loss_chf'))})")
    impact = next((c.get("impact") for _, c in rows if c.get("impact")), None)
    if impact:
        lines.append(f"• If late: {impact.replace('_', ' ')}")
    route = next((r for r, c in sorted(rows, key=lambda rc: _urgency(rc[0])) if c.get("at_risk")), None)
    return {"answer": "\n".join(lines), "facts": lines,
            "links": _links(route["route_id"]) if route else [],
            "route_id": route["route_id"] if route else None}


def key_accounts(board: dict) -> dict:
    rows = board.get("key_accounts") or []
    if not rows:
        return {"answer": "No key account has an order at risk.", "facts": []}
    by: dict[str, list[dict]] = {}
    for row in rows:
        by.setdefault(row["customer"], []).append(row)
    lines = [f"{len(by)} key accounts have orders at risk ({len(rows)} orders):"]
    for name, items in sorted(by.items(), key=lambda kv: -sum(i.get("expected_loss_chf") or 0 for i in kv[1])):
        loss = sum(i.get("expected_loss_chf") or 0 for i in items)
        worst = min(items, key=lambda i: i.get("lead_time_hours") if i.get("lead_time_hours") is not None else 1e9)
        lines.append(f"• {name}: {', '.join(i['shipment_id'] for i in items)} · {chf(loss)}"
                     f" · {worst.get('action') or 'tell the customer'}")
    return {"answer": "\n".join(lines[:8]), "facts": lines}


def about_order(board: dict, order_id: str, tree_for: TreeFor | None) -> dict:
    for r in board.get("routes") or []:
        for c in r.get("customers") or []:
            if order_id in (c.get("orders") or []):
                risky = order_id in (c.get("orders_at_risk") or [])
                lines = [f"{order_id}: {c['name']} on {_short(r['name'])} ({r['level_label']})."]
                action = next((a for a in r.get("actions") or [] if a["shipment_id"] == order_id), None)
                tree = tree_for(r["route_id"]) if (tree_for and risky) else None
                row = next((o for o in (tree or {}).get("orders") or [] if o["shipment_id"] == order_id), None)
                if row:
                    word = {"kept": "keeps its date by a new way", "reduced": "will be late; cut the damage",
                            "told": "will be late; tell the customer", "absorbed": "buffers absorb it"}
                    lines.append(f"• {chf(row['loss_chf'])} at risk, {row['p_late']:.0%} chance late, "
                                 f"about {row['late_days']:.1f} days · {word.get(row['branch'], row['branch'])}")
                    if row.get("due"):
                        lines.append(f"• Promised for {_fmt_iso(row['due'])}")
                elif not risky:
                    lines.append("• Not at risk: no event reaches it, or its buffers absorb it.")
                if action:
                    lines.append(f"• Playbook: {action['sentence']}")
                return {"answer": "\n".join(lines), "facts": lines, "links": _links(r["route_id"]),
                        "route_id": r["route_id"]}
    return {"answer": f"{order_id} is not on this board.", "facts": []}


def money(board: dict) -> dict:
    routes = board.get("routes") or []
    total = sum(r.get("exposure_chf") or 0 for r in routes)
    counted = (board.get("penalties") or {}).get("enabled")
    lines = [f"{chf(total)} of expected loss across the book if nobody acts."]
    for r in sorted(routes, key=lambda r: -(r.get("exposure_chf") or 0))[:4]:
        if r.get("exposure_chf"):
            lines.append(f"• {_short(r['name'], 50)}: {chf(r['exposure_chf'])} ({r['level_label']})")
    lines.append("• Delay penalties from the contracts are "
                 + ("counted." if counted else "not counted; switch them on in the header to include them."))
    return {"answer": "\n".join(lines), "facts": lines}


def deadlines(board: dict) -> dict:
    rows = sorted([r for r in board.get("routes") or [] if r.get("lead_time_hours") is not None],
                  key=lambda r: r["lead_time_hours"])
    if not rows:
        return {"answer": "No route has a decision deadline right now.", "facts": []}
    lines = ["The next decisions, soonest first (working time; weekends and holidays do not count):"]
    lines += [f"• {_route_line(board, r)}" for r in rows[:5]]
    return {"answer": "\n".join(lines), "facts": lines, "links": _links(rows[0]["route_id"]),
            "route_id": rows[0]["route_id"]}


def meeting(board: dict) -> dict:
    ah = board.get("all_hands") or {}
    lines = [f"All-hands: {ah.get('cadence_label', '')}, next {ah.get('next_label', '')}."
             + ("" if ah.get("posture") != "convene" else f" Normally {ah.get('normal_label', '')}.")]
    for t in ah.get("triggers") or []:
        lines.append(f"• Why: {t}")
    people = [a.get("function") for a in ah.get("attendees") or [] if a.get("function")]
    if people:
        lines.append(f"• Who: {', '.join(people)}")
    if ah.get("rule_agreed") is False:
        lines.append("• The rule is proposed, not yet agreed.")
    return {"answer": "\n".join(lines), "facts": lines}


def signals(board: dict) -> dict:
    lines = []
    for p in (board.get("carrier_signals") or {}).get("patterns") or []:
        lines.append(f"• {p.get('carrier_name')} pushed out {p.get('orders')} orders at {p.get('node_name')}"
                     f" (average {p.get('mean_hours', 0):.0f} h each), no official notice yet.")
    for b in (board.get("order_signals") or {}).get("bursts") or []:
        lines.append(f"• {b.get('orders')} small orders in one day on {b.get('flow')} (usual {b.get('usual')})"
                     " : a sign a crisis may be coming within a week.")
    if not lines:
        return {"answer": "No early warning is active: no carrier push-outs and no burst of small orders.",
                "facts": []}
    return {"answer": "\n".join([f"{len(lines)} early warning{'s' if len(lines) != 1 else ''}:", *lines]),
            "facts": lines}


def contacts(board: dict, r: dict) -> dict:
    resp = r.get("response") or {}
    lines = [f"Who to contact on {_short(r['name'], 50)}:"]
    m = resp.get("route_manager") or {}
    if m.get("name"):
        lines.append(f"• {m['name']} ({m.get('role', 'route manager')}): {m.get('phone', '')}, {m.get('email', '')}")
    for t in (resp.get("standing_teams") or [])[:4]:
        lines.append(f"• {t['name']} ({t.get('role', '')}): {t.get('phone', '')}")
    esc = resp.get("escalation") or {}
    if esc.get("notify"):
        lines.append(f"• Escalation level {esc.get('level')}: tell {', '.join(esc['notify'])} "
                     f"(answer within {esc.get('acknowledge_within_hours', '?')} h)")
    return {"answer": "\n".join(lines), "facts": lines, "links": _links(r["route_id"]),
            "route_id": r["route_id"]}


def levels(board: dict) -> dict:
    parts = [f"{lv['count']} {lv['label']}" for lv in board.get("levels") or []]
    lines = [f"{len(board.get('routes') or [])} routes: " + ", ".join(parts) + "."]
    for lv in board.get("levels") or []:
        if lv["count"] and lv["level"] in URGENT:
            names = [_short(r["name"], 44) for r in board["routes"] if r.get("level") == lv["level"]]
            lines.append(f"• {lv['label']} ({lv['directive'].lower()}): {'; '.join(names)}")
    return {"answer": "\n".join(lines), "facts": lines}


def events_about(board: dict, q: str) -> dict | None:
    words = {w for w in _words(q) if len(w) >= 4 and w not in _COMMON}
    hits = []
    for r in board.get("routes") or []:
        for e in r.get("events") or []:
            if words & _words(e.get("title", "") + " " + e.get("event_class", "")):
                hits.append((r, e))
    if not hits:
        return None
    seen, lines = set(), []
    for _r, e in sorted(hits, key=lambda re_: -(re_[1].get("exposure_chf") or 0)):
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        routes = [x["name"] for x, y in hits if y["event_id"] == e["event_id"]]
        lines.append(f"• {e['title']} · {e.get('kind_label', '')} · {chf(e.get('exposure_chf'))}"
                     f" · {len(routes)} route{'s' if len(routes) != 1 else ''}")
    first = hits[0][0]
    return {"answer": "\n".join([f"{len(seen)} event{'s' if len(seen) != 1 else ''} on the board match:",
                                 *lines[:5]]),
            "facts": lines, "links": _links(first["route_id"]), "route_id": first["route_id"]}


def search(board: dict, q: str) -> dict:
    """The last resort: the rows whose words the question shares."""
    words = {w for w in _words(q) if len(w) >= 4}
    scored = []
    for r in board.get("routes") or []:
        text = " ".join([r.get("name", ""), r.get("reason", ""),
                         *[e.get("title", "") for e in r.get("events") or []],
                         *[c["name"] for c in r.get("customers") or []],
                         *[a.get("label", "") for a in r.get("actions") or []]])
        n = len(words & _words(text))
        if n:
            scored.append((n, r))
    if not scored:
        return {"answer": "The board does not carry that.\n" + _HELP, "facts": [], "unsure": True}
    scored.sort(key=lambda s: (-s[0], _urgency(s[1])))
    lines = ["Closest matches on the board:", *[f"• {_route_line(board, r)}" for _, r in scored[:4]]]
    return {"answer": "\n".join(lines), "facts": lines, "links": _links(scored[0][1]["route_id"]),
            "route_id": scored[0][1]["route_id"], "unsure": True}


# ---------------------------------------------------------------- the router
def reply(board: dict, question: str, route_id: str | None = None,
          tree_for: TreeFor | None = None) -> dict:
    """The answer to one question, from the board alone."""
    q = fold(question).strip()
    routes = board.get("routes") or []
    selected = next((r for r in routes if r["route_id"] == route_id), None) if route_id else None

    orders = find_orders(q)
    if orders:
        return about_order(board, orders[0], tree_for)

    if len(q) < 40 and (_has(q, "hi", "hello", "hey", "help", "what can you") or q in {"?", "hi", "hello"}):
        return {"answer": _HELP, "facts": []}

    named = find_customers(board, q)
    if named:
        return about_customer(board, named[0])

    if _has(q, "meeting", "all-hands", "all hands", "allhands", "convene"):
        return meeting(board)
    if _has(q, "early warning", "warning", "signal", "burst", "push-out", "pushout", "pushed", "pattern"):
        return signals(board)
    if _has(q, "key account", "key customer", "important customer", "a-tier", "tier a", "priority customer"):
        return key_accounts(board)

    mentioned = find_routes(board, q)
    route = mentioned[0] if mentioned else selected
    if _has(q, "who", "contact", "call", "phone", "email", "escalat") and route:
        return contacts(board, route)
    if mentioned:
        out = about_route(board, route, q, tree_for)
        if len(mentioned) > 1:
            out["answer"] += "\n" + "Also on: " + "; ".join(_short(r["name"], 40) for r in mentioned[1:4])
        return out

    if _has(q, "first", "priority", "urgent", "most", "worst", "top", "what should", "what do i", "start",
            "biggest", "now"):
        return top_priority(board, tree_for)
    if _has(q, "cost", "exposure", "money", "chf", "penalt", "lose", "loss", "nobody acts", "do nothing"):
        return money(board)
    if _has(q, "deadline", "when", "how long", "time left", "closes", "hours"):
        return deadlines(board)
    if _has(q, "how many", "count", "level", "critical", "alert", "summary", "overview", "status"):
        return levels(board)
    if selected and _has(q, "this route", "this one", "here", "alternat", "reroute", "option", "instead"):
        return about_route(board, selected, q, tree_for)

    found = events_about(board, q)
    if found:
        return found
    if selected:
        out = about_route(board, selected, q, tree_for)
        out["unsure"] = True
        return out
    return search(board, q)


def event_reply(board: dict, event_id: str) -> dict:
    """One event, from its row: what, how likely, what it touches."""
    for r in board.get("routes") or []:
        for e in r.get("events") or []:
            if e["event_id"] != event_id:
                continue
            p = e.get("probability")
            lines = [f"{e['title']} ({e.get('kind_label', e.get('severity', ''))})",
                     f"• Chance: {'unsourced, no defensible figure' if p is None else f'{p:.0%}'}"
                     + (f" · {e['probability_basis']}" if e.get("probability_basis") else ""),
                     f"• Touches {e.get('shipments_here', 0)} orders on {_short(r['name'], 50)}, "
                     f"{chf(e.get('exposure_chf'))} expected loss",
                     f"• From {e.get('starts_at', '')[:16].replace('T', ' ')} to "
                     f"{(e.get('ends_at') or 'open')[:16].replace('T', ' ')} UTC",
                     f"• Source: {e.get('source', '')} (tier {e.get('source_tier', '?')})"]
            return {"answer": "\n".join(lines), "facts": lines, "links": _links(r["route_id"]),
                    "route_id": r["route_id"]}
    return {"answer": "That event is not on the current board.", "facts": []}
