#!/usr/bin/env python3
"""Record the interactive demo of Horizon and build its player.

    python scripts/demo/record.py                          # http://localhost:8000
    python scripts/demo/record.py --base http://localhost:8800 --out /tmp/demo
    python scripts/demo/record.py --build-only             # re-time, no browser

A real browser walks the board the way the storyboard tells the story
(scripts/demo/storyboard.json): one high-resolution frame per state of the
UI, plus where every element the camera visits sits in it. The player pans
and zooms across those frames, moves a cursor, and waits for a click at each
breakpoint, like an Arcade demo. Out comes demo.html: one self-contained
file that plays offline, anywhere.

It records whatever the server shows. Against a server with config/ in place
the demo carries your own data and logo; the output goes to data/exports/demo/,
which is gitignored, so a recording of real data never ends up in a commit.

The story picks its own targets from the board: the first critical route with
a barge or a ship that has two ways round and a split that moves only some of
its boxes, falling back to any disrupted shipment with a way round. Nothing is
tied to one dataset.

What it changes on the server, and puts back: the delay-penalty switch, one
all-hands reply, one closed case, one booking (undone at once). The one thing
it cannot put back is the field report it files from the showcase vehicle:
reports are append-only by design.

Needs Playwright with Chromium (free): pip install playwright, then
python -m playwright install chromium. Pillow writes the frames as WebP.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VIEW = {"width": 1600, "height": 900}
PHONE = {"width": 390, "height": 844}
DEMO_AS_OF = "2026-09-26T23:00:00Z"

# In-page helpers: every target is a box [x, y, w, h] in CSS pixels of the
# screenshot it belongs to (page coordinates for a full-page shot).
HELPERS = r"""
window.__demo = (() => {
  const vis = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // Page coordinates for a full-page shot, viewport coordinates otherwise.
  const box = (el) => { if (!vis(el)) return null; const r = el.getBoundingClientRect();
    const dx = window.__demoFull ? scrollX : 0, dy = window.__demoFull ? scrollY : 0;
    return [r.left + dx, r.top + dy, r.width, r.height]; };
  const qa = (s, root) => [...(root || document).querySelectorAll(s)];
  const within = (scope) => (typeof scope === 'string' ? document.querySelector(scope) : scope) || null;
  // The smallest visible element whose text holds `text`, any case (the UI
  // uppercases some labels in CSS, and innerText follows the CSS).
  const byText = (scope, text, sel) => {
    const root = within(scope);
    if (!root) return null;
    const want = String(text).toLowerCase();
    let best = null;
    for (const el of qa(sel || '*', root)) {
      if (!vis(el)) continue;
      const t = (el.innerText || '').toLowerCase();
      if (t.includes(want) && (!best || t.length < best.innerText.length)) best = el;
    }
    return best;
  };
  const union = (list) => { const b = list.filter(Boolean); if (!b.length) return null;
    const x0 = Math.min(...b.map((v) => v[0])), y0 = Math.min(...b.map((v) => v[1]));
    const x1 = Math.max(...b.map((v) => v[0] + v[2])), y1 = Math.max(...b.map((v) => v[1] + v[3]));
    return [x0, y0, x1 - x0, y1 - y0]; };
  const mapRect = () => { const m = document.getElementById('fleetmap'); return m && m.getBoundingClientRect(); };
  const geo = (lon, lat, r = 14) => {
    const map = window.__fleetmap, c = mapRect();
    if (!map || !c || lon == null || lat == null) return null;
    const p = map.project([lon, lat]);
    const x = c.left + p.x, y = c.top + p.y;
    if (x < c.left + 6 || x > c.right - 6 || y < c.top + 6 || y > c.bottom - 6) return null;
    return [x - r + (window.__demoFull ? scrollX : 0), y - r + (window.__demoFull ? scrollY : 0), 2 * r, 2 * r];
  };
  const path = (pts, r = 8) => union((pts || []).map(([lat, lon]) => geo(lon, lat, r)));
  const scroller = (el) => {
    let e = el && el.parentElement;
    while (e && e !== document.body) {
      const s = getComputedStyle(e);
      if (/(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 2) return e;
      e = e.parentElement;
    }
    return document.scrollingElement;
  };
  // Put an element `at` px below the top of whatever scrolls it.
  const reveal = (el, at = 12) => {
    if (!el) return false;
    const sc = scroller(el);
    const top = sc === document.scrollingElement ? 0 : sc.getBoundingClientRect().top;
    sc.scrollTop += el.getBoundingClientRect().top - top - at;
    return true;
  };
  const sel = (s, n = 0) => box(qa(s).filter(vis)[n]);
  const text = (scope, t, closest) => { const el = byText(scope, t); return box(closest && el ? (el.closest(closest) || el) : el); };
  const head = (s, n = 4) => { const el = qa(s).filter(vis)[0]; return el ? union([...el.children].slice(0, n).map(box)) : null; };
  const near = (s, closest) => { const el = qa(s).filter(vis)[0]; return box(el && (el.closest(closest) || el)); };
  return { vis, box, qa, byText, union, geo, path, scroller, reveal, sel, text, head, near };
})();
"""


def S(selector: str, n: int = 0) -> str:
    return f"d.sel({json.dumps(selector)}, {n})"


def T(scope: str, text: str, closest: str | None = None) -> str:
    return f"d.text({json.dumps(scope)}, {json.dumps(text)}, {json.dumps(closest)})"


def G(lon: float | None, lat: float | None, r: int = 14) -> str:
    return "null" if lon is None or lat is None else f"d.geo({lon}, {lat}, {r})"


def U(*parts: str) -> str:
    return "d.union([" + ", ".join(parts) + "])"


# ---------------------------------------------------------------- the server
class Api:
    def __init__(self, base: str, as_of: str, shipments: int = 150):
        self.base = base.rstrip("/")
        self.q = urllib.parse.urlencode({"as_of": as_of, "shipments": shipments})

    def url(self, path: str, **extra) -> str:
        q = self.q + ("&" + urllib.parse.urlencode(extra) if extra else "")
        return f"{self.base}{path}{'&' if '?' in path else '?'}{q}"

    def get(self, path: str, **extra):
        with urllib.request.urlopen(self.url(path, **extra), timeout=300) as r:
            return json.load(r)

    def post(self, path: str, body: dict, **extra):
        req = urllib.request.Request(self.url(path, **extra), data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)


def pick(api: Api, route: str | None, ship: str | None) -> dict:
    """The route and the vehicle the story follows."""
    board = api.get("/api/board")
    fleet = api.get("/api/map/assets")
    assets = fleet["assets"]
    routes = board["routes"]
    order = {"red": 0, "yellow": 1, "green": 2}
    rank = {"A": 0, "B": 1, "C": 2}

    def mine(rid):
        return sorted((a for a in assets if a["lane_id"] == rid),
                      key=lambda a: (order.get(a["status"], 3), rank.get(a.get("customer_priority") or "B", 1),
                                     -(a.get("delay_hours") or 0)))

    def ways(sid):
        try:
            return api.get(f"/api/map/assets/{urllib.parse.quote(sid)}/routes")
        except Exception:  # noqa: BLE001
            return {}

    def split(sid):
        try:
            s = api.post(f"/api/map/assets/{urllib.parse.quote(sid)}/split", {})
        except Exception:  # noqa: BLE001
            return 0, 0
        alloc = s.get("allocation") or {}
        return sum(1 for v in alloc.values() if v != "ORIGINAL"), len(s.get("containers") or [])

    chosen = None
    if ship:
        a = next(x for x in assets if x["id"] == ship)
        chosen = (a, ways(ship))
    else:
        critical = [r for r in routes if r["level"] == "red" and (not route or r["route_id"] == route)]
        tiers = ("partial", "vessel", "any")
        best: dict[str, tuple] = {}
        for r in critical:
            for a in mine(r["route_id"]):
                if a["status"] == "green":
                    continue
                w = ways(a["id"])
                n = len(w.get("candidates") or [])
                if n and "any" not in best:
                    best["any"] = (a, w)
                if n >= 2 and a["mode"] in ("barge", "sea"):
                    best.setdefault("vessel", (a, w))
                    moved, total = split(a["id"])
                    if 0 < moved < total:
                        best["partial"] = (a, w)
                        break
            if "partial" in best:
                break
        chosen = next((best[t] for t in tiers if t in best), None)
    if chosen is None:
        sys.exit("No disrupted shipment with a way round on this board: nothing to demo.")
    a, w = chosen
    r = next(x for x in routes if x["route_id"] == a["lane_id"])
    lane = next((x for x in fleet.get("lanes") or [] if x["route_id"] == r["route_id"]), {})
    d = w.get("disruption") or {}
    cands = w.get("candidates") or []
    moved, total = split(a["id"])
    return {
        "route": r["route_id"], "route_name": r["name"], "ship": a["id"], "vehicle": a["asset_id"],
        "vessel": a["name"], "customer": a["customer"], "mode": a["mode"],
        "lat": a["lat"], "lon": a["lon"], "lane_path": lane.get("path") or [],
        "fleet": [x for x in assets if x["lane_id"] == r["route_id"]],
        "event": d.get("title") or (r["events"][0]["title"] if r.get("events") else ""),
        "place": (d.get("name") or "").split(" (")[0], "hazard": [d.get("lon"), d.get("lat")],
        "alt1": cands[0]["label"] if cands else "", "alt2": cands[1]["label"] if len(cands) > 1 else "",
        "moved": moved, "boxes": total,
    }


# ------------------------------------------------------------------ frames
class Recorder:
    def __init__(self, out: Path, dpr: float, quality: int):
        self.out = out
        self.dpr = dpr
        self.quality = quality
        self.frames: dict[str, dict] = {}
        (out / "frames").mkdir(parents=True, exist_ok=True)

    def snap(self, page, name: str, targets: dict[str, str], *, full: bool = False, kind: str = "screen") -> dict:
        from PIL import Image  # noqa: PLC0415

        page.evaluate(HELPERS)
        page.evaluate(f"window.__demoFull = {'true' if full else 'false'}")
        body = "".join(f"try {{ o[{json.dumps(k)}] = {v}; }} catch (e) {{ o[{json.dumps(k)}] = null; }}\n"
                       for k, v in targets.items())
        boxes = page.evaluate(f"(() => {{ const d = window.__demo; const o = {{}};\n{body} return o; }})()")
        png = page.screenshot(full_page=full, type="png")
        img = Image.open(io.BytesIO(png)).convert("RGB")
        dpr = page.evaluate("devicePixelRatio")
        img.save(self.out / "frames" / f"{name}.webp", "WEBP", quality=self.quality, method=5)
        w, h = round(img.width / dpr), round(img.height / dpr)
        missing = [k for k, v in boxes.items() if not v]
        self.frames[name] = {"file": f"frames/{name}.webp", "w": w, "h": h, "kind": kind,
                             "page": page.evaluate("location.pathname"),
                             "targets": {k: [round(x, 1) for x in v] if v else None for k, v in boxes.items()}}
        print(f"  {name:<14} {w}x{h}" + (f"   missing: {', '.join(missing)}" if missing else ""))
        return boxes


def settle(page, ms: int = 600):
    page.wait_for_timeout(ms)


def fit_map(page, points, padding: dict, max_zoom: float = 8.0):
    """Frame the map on these [lat, lon] points, clear of the chips at the
    bottom and of the Action Hub, and wait for it to finish drawing."""
    page.evaluate("""([pts, pad, mz]) => new Promise((done) => {
      const map = window.__fleetmap;
      if (!map || !pts.length) return done();
      const b = new maplibregl.LngLatBounds();
      pts.forEach(([lat, lon]) => b.extend([lon, lat]));
      map.once('idle', () => done());
      map.fitBounds(b, { padding: pad, maxZoom: mz, duration: 0 });
      setTimeout(done, 4000);
    })""", [points, padding, max_zoom])
    settle(page, 900)


def board_ready(page, api: Api):
    page.goto(f"{api.base}/?{api.q}", wait_until="load")
    page.wait_for_function("window.MapAgent && MapAgent.getState().assets.status === 'ready'", timeout=180000)
    page.wait_for_selector("#rlist .rli", timeout=180000)
    settle(page, 2200)


def record(api: Api, story: dict, rec: Recorder, chromium: str | None):
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    R, SID, V = story["route"], story["ship"], story["vehicle"]
    card = f'#rlist .rli[data-route="{R}"]'
    row = f'.dship-row[data-ship="{SID}"]'
    header = {"ladder": S("#ladder"), "link_profile": S("#link-profile"), "ask_btn": S("#btn-ask"),
              "alerts_btn": S("#btn-alerts"), "vol_btn": S("#btn-volume"),
              "tab_signals": S('.ptab[data-ptab="signals"]'), "tab_allhands": S('.ptab[data-ptab="allhands"]'),
              "tab_routes": S('.ptab[data-ptab="routes"]'), "map": S(".globe-wrap")}
    vessels = [a for a in story["fleet"] if a["status"] != "green" and a["mode"] in ("barge", "sea")]
    vessels = vessels or [a for a in story["fleet"] if a["status"] != "green"]
    pen0 = api.get("/api/penalties")["enabled"]
    if pen0:
        api.post("/api/penalties", {"enabled": False})

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": chromium} if chromium else {}))
        desk = browser.new_context(viewport=VIEW, device_scale_factor=rec.dpr)
        page = desk.new_page()
        page.on("pageerror", lambda e: print("  page error:", e))

        # ---- Shanshan: the board and its map
        board_ready(page, api)
        page.evaluate(HELPERS)
        page.evaluate(f"__demo.reveal(document.querySelector({json.dumps(card)}), 170)")
        settle(page)
        rec.snap(page, "board", {**header, "list": S("#panel-list"), "card": S(card), "card_open": S(card + " .rli-open"),
                                 "card_event": S(card + " .rli-driver"), "card_when": S(card + " .rli-when")})

        page.click(card + " .rli-open")
        page.wait_for_selector("#panel-body:not([hidden])")
        page.wait_for_selector("#ctx-filter:not([hidden])", timeout=60000)
        settle(page, 1800)
        fit_map(page, story["lane_path"], {"top": 70, "bottom": 150, "left": 70, "right": 70}, 7.5)
        ctx = api.get("/api/map/context", route=R)
        ports = [x for x in ctx["layers"]["ports"] if x.get("lat") is not None]
        stock = [x for x in ctx["layers"]["inventories"] if x.get("lat") is not None]
        route_targets = {
            **header, "panel": S("#panel-body"), "chip": S("#d-chip"), "title": S("#d-name"),
            "stats": S("#d-stats"), "stat_when": S("#d-stats > *", 0), "stat_exposure": S("#d-stats > *", 1),
            "stat_options": S("#d-stats > *", 2), "happening": T("#r-actions", "What is happening", ".dt-step, li, section"),
            "who_hit": T("#r-actions", "Who is hit", ".dt-step, li, section"), "tab_ships": S('.rtab[data-tab="ships"]'),
            "ctx": S("#ctx-filter"), "ctx_ports": T("#ctx-filter", "Ports", "label"),
            "ctx_inv": T("#ctx-filter", "Inventory", "label"), "ctx_vendors": T("#ctx-filter", "Vendors", "label"),
            "ctx_links": T("#ctx-filter", "Road & rail", "label"),
            "vessel": G(vessels[0]["lon"], vessels[0]["lat"], 22) if vessels else "null",
            "vessels": U(*[G(a["lon"], a["lat"], 18) for a in vessels[:6]]) if vessels else "null",
            "lane": f"d.path({json.dumps(story['lane_path'])}, 10)",
            "ports": U(*[G(x["lon"], x["lat"], 16) for x in ports]) if ports else "null",
            "port_1": G(ports[-1]["lon"], ports[-1]["lat"], 18) if ports else "null",
            "stock": U(*[G(x["lon"], x["lat"], 16) for x in stock]) if stock else "null",
        }
        rec.snap(page, "route", route_targets)

        # Shipments: first where the impact changes from one vehicle to the
        # next on the same stretch, then the one the story follows.
        page.click('.rtab[data-tab="ships"]')
        settle(page, 900)
        page.evaluate(HELPERS)
        split_at = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('.dship-row')];
          const st = (r) => (r.querySelector('.dship-status') || {}).innerText || '';
          const i = rows.findIndex((r) => st(r) !== st(rows[0]));
          const at = i > 0 ? i : Math.min(rows.length - 1, 3);
          __demo.reveal(rows[Math.max(0, at - 3)], 64);
          rows.forEach((r, k) => { r.dataset.demo = k < at ? 'hit' : 'fine'; });
          return at;
        }""")
        settle(page, 500)
        rec.snap(page, "ships_mid", {
            "rows_major": "d.union(d.qa('.dship-row[data-demo=\"hit\"]').filter(d.vis).slice(-3).map(d.box))",
            "rows_nominal": "d.union(d.qa('.dship-row[data-demo=\"fine\"]').filter(d.vis).slice(0, 3).map(d.box))",
            "rows_mid": "d.union(d.qa('.dship-row').filter(d.vis).slice(0, 7).map(d.box))",
            "status_major": "d.box(d.qa('.dship-row[data-demo=\"hit\"] .dship-status').filter(d.vis).slice(-1)[0])",
            "status_nominal": "d.box(d.qa('.dship-row[data-demo=\"fine\"] .dship-status').filter(d.vis)[0])",
            "leg_shared": "d.box(d.qa('.dship-row[data-demo=\"fine\"] .dship-main .muted').filter(d.vis)[0])",
        })
        print(f"  impact changes at row {split_at + 1}")
        page.evaluate(f"__demo.reveal(document.querySelector({json.dumps(row)}), 64)")
        settle(page, 500)
        rec.snap(page, "ships_top", {"row": S(row), "row_main": S(row + " .dship-main")})

        page.click(row + " .dship-main")
        page.wait_for_function(f"MapAgent.getState().selection.id === {json.dumps(SID)} "
                               "&& MapAgent.getState().routing.status === 'ready'", timeout=120000)
        page.wait_for_selector(".scard .stiles", timeout=60000)
        settle(page, 1500)
        paths = page.evaluate("""() => { const d = MapAgent.getState().routing.data;
          return [...(d.original ? d.original.path : []), ...d.candidates.flatMap((c) => c.path)]; }""")
        hub_left = page.evaluate("document.getElementById('hub').getBoundingClientRect().left")
        pane_right = page.evaluate("document.getElementById('fleetmap').getBoundingClientRect().right")
        fit_map(page, paths, {"top": 90, "bottom": 150, "left": 60, "right": max(80, int(pane_right - hub_left + 40))}, 7.5)
        page.evaluate(f"__demo.reveal(document.querySelector({json.dumps(row)}), 64)")
        settle(page, 500)
        card_targets = {
            **header, "scard": S(".scard"), "row": S(row),
            "tile_promised": S(".scard .stiles > *", 0), "tile_plan": S(".scard .stiles > *", 1),
            "tile_way": S(".scard .stiles > *", 2), "tile_risk": S(".scard .stiles > *", 3),
            "jour": S(".scard .sjour"), "leg_now": S(".scard .sleg.is-now"),
            "leg_risk": "d.box(d.qa('.scard .sleg--affected, .scard .sleg--at_risk').filter(d.vis)[0])",
            "prog": U(S(".scard .sprog"), S(".scard .sprog-n")), "sways": S(".scard .sways"),
            "btn_tree": "d.box(d.qa('.scard a').filter((a) => /tree/.test(a.href))[0])",
            "btn_page": "d.box(d.qa('.scard a').filter((a) => /\\/shipment\\//.test(a.href))[0])",
            "hazard": S(".hazard"), "sel": G(story["lon"], story["lat"], 24),
            "badge1": S(".maplibregl-marker.rbadge, .rbadge", 0), "badge2": S(".maplibregl-marker.rbadge, .rbadge", 1),
            "routes_all": f"d.path({json.dumps(paths)}, 6)",
            "hub": S("#hub"), "hub_head": S("#hub-head"), "hub_sliders": S("#hub .weights"),
            "hub_alts": S("#hub .alts"), "hub_alt1": S("#hub .alt", 0), "hub_alt2": S("#hub .alt", 1),
            "jm_dest": "d.box(d.qa('.jm').filter(d.vis).slice(-1)[0])",
            "ctx_links": T("#ctx-filter", "Road & rail", "label"),
            "stat_when": S("#d-stats > *", 0), "stat_exposure": S("#d-stats > *", 1), "stat_options": S("#d-stats > *", 2),
            "tab_ships": S('.rtab[data-tab="ships"]'),
        }
        rec.snap(page, "card", card_targets)

        # The Action Hub: the split, then who is nearby.
        page.evaluate("__demo.reveal(document.querySelector('.hsec[data-sec=\"split\"]'), 8)")
        settle(page, 500)
        keep = {"btn_tree": card_targets["btn_tree"], "hub": S("#hub"), "map": S(".globe-wrap")}
        rec.snap(page, "hub_split0", {**keep, "split_sec": S('.hsec[data-sec="split"]'),
                                      "split_toggle": "d.box(document.querySelector('#split-toggle').closest('label'))"})
        page.click('label.switch:has(#split-toggle)')
        page.wait_for_selector('.hsec[data-sec="split"] .chips', timeout=90000)
        settle(page, 1600)
        page.evaluate("__demo.reveal(document.querySelector('.hsec[data-sec=\"split\"]'), 8)")
        settle(page, 400)
        rec.snap(page, "hub_split", {
            **keep, "split_sec": S('.hsec[data-sec="split"]'), "split_toggle": "d.box(document.querySelector('#split-toggle').closest('label'))",
            "split_sum": S('.hsec[data-sec="split"] .hnote'),
            "split_boxes": S('.hsec[data-sec="split"] .chips'),
            "split_urgent": "d.near('.hsec[data-sec=\"split\"] .chip .crit', '.chip')",
            "split_stats": S(".split-sum"), "split_branches": S('.hsec[data-sec="split"] .branches'),
            "branch_a": S(".branch-label", 0), "branch_b": S(".branch-label", 1),
            "branches": U(S(".branch-label", 0), S(".branch-label", 1)),
        })
        page.evaluate("__demo.reveal(document.getElementById('hub-partners'), 8)")
        settle(page, 400)
        rec.snap(page, "hub_partners", {**keep, "partners": S("#hub-partners"),
                                        "partner_1": "d.head('#hub-partners', 3)"})

        # ---- Harjot: the decision tree
        page.goto(api.url("/tree", route=R, ship=SID), wait_until="load")
        page.wait_for_selector(f'.tn[data-id^="opt:{SID}:"]', timeout=120000)
        settle(page, 2200)
        opt = f'.tn[data-id^="opt:{SID}:"]'
        rec.snap(page, "tree", {
            "tr_decide": S("#tr-decide"), "keys": U(S(".tn--key", 0), S(".tn--key", 1)),
            "key_1": S(".tn--key", 0), "key_2": S(".tn--key", 1), "ship_box": S(f'.tn[data-id="ship:{SID}"]'),
            "opt_best": S(opt), "opt_best_date": S(opt + " .tn-big"), "opt_best_cost": S(opt + " .tn-sub"),
            "opt_best_co2": S(opt + " .tn-sub--2"), "side": S("#tr-side"), "side_ways": S("#tr-side .wt"),
        })
        page.evaluate(HELPERS)
        page.evaluate("__demo.reveal(__demo.byText('#tr-side', 'if nobody acts'), 60)")
        settle(page, 500)
        cost = T("#tr-side", "if nobody acts", ".ins-sec")
        rec.snap(page, "tree_cost", {"cost_block": cost, "pen_toggle": S("#pen-toggle"), "side": S("#tr-side")})
        page.click("#pen-toggle")
        page.wait_for_function("document.getElementById('pen-toggle') && /Leave/.test(document.getElementById('pen-toggle').innerText)",
                               timeout=120000)
        settle(page, 900)
        page.evaluate("__demo.reveal(__demo.byText('#tr-side', 'if nobody acts'), 60)")
        settle(page, 400)
        rec.snap(page, "tree_cost_pen", {"cost_block": cost, "pen_toggle": S("#pen-toggle"),
                                         "pen_row": T("#tr-side", "enalt", "div, li, tr")})

        page.click(opt)
        settle(page, 1600)
        rec.snap(page, "tree_opt", {"opt_best": S(opt), "par_1": S(f'.tn[data-id^="par:{SID}:"]'),
                                    "lvl_partners": S('.tr-level[data-level="4"]'), "side": S("#tr-side")})
        par = page.locator(f'.tn[data-id^="par:{SID}:"]')
        if par.count():
            par.first.click()
            settle(page, 1200)
        page.click(f'.tn[data-id^="book:{SID}:"]')
        settle(page, 1500)
        rec.snap(page, "tree_book", {"sign_box": S(f'.tn[data-id^="sign:{SID}:"]'), "book_box": S(f'.tn[data-id^="book:{SID}:"]'),
                                     "book_btn": S("#book-btn"), "book_note": T("#tr-side", "Undo within", "p")})
        page.click("#book-btn")
        page.wait_for_selector("#undo-btn", timeout=60000)
        settle(page, 700)
        rec.snap(page, "tree_booked", {"undo_btn": S("#undo-btn"), "booked_msg": T("#tr-side", "booked for", "p, div"),
                                       "book_box": S(f'.tn[data-id^="book:{SID}:"]'), "toast": S("#tr-toast")})
        page.click("#undo-btn")
        settle(page, 1500)

        # ---- the shipment's own page
        def ship_page(name: str, extra: dict | None = None):
            page.goto(api.url(f"/shipment/{urllib.parse.quote(SID)}"), wait_until="load")
            page.wait_for_selector("#sp-strs .sp-unit", timeout=120000)
            page.wait_for_function("document.querySelector('#sp-radar-measured') && document.querySelector('#sp-radar-measured').children.length",
                                   timeout=60000)
            settle(page, 1800)
            rec.snap(page, name, {
                "sp_back": S("#sp-back"), "stats": S("#sp-stats"), "strs": S("#sp-strs"), "unit_card": S("#sp-unit"),
                "unit_cur": S(".sp-unit.is-on") if page.locator(".sp-unit.is-on").count() else S(".sp-unit"),
                "unit_wait": "d.box(d.qa('.sp-str').filter((s) => /waits/i.test(s.innerText)).map((s) => s.querySelector('.sp-unit'))[0])",
                "boxes": S("#sp-unit .sp-bxs"), "box_crit": S("#sp-unit .sp-bx.is-crit"),
                "rep_1": S("#sp-unit .rep-list .rep"), "reports": S("#sp-unit .sp-reps-h"),
                "matrix": S("#sp-matrix"), "matrix_dot": S("#sp-matrix .mx-cell.has"),
                "radars": S(".rt-radars"), "radar_m": S("#sp-radar-measured"), "radar_legend": S("#sp-radar-legend"),
                "radar_cat": S("#sp-gauges-measured > *"), "gauge_1": S("#sp-gauges-measured > *"),
                **(extra or {}),
            }, full=True)

        ship_page("ship_page")

        # ---- the driver's app, on a phone
        phone = browser.new_context(viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True)
        dv = phone.new_page()
        dv.goto(api.url("/driver", shipment=SID, vehicle=V), wait_until="load")
        dv.wait_for_selector("#f-status [data-status]", timeout=60000)
        settle(dv, 1200)
        role = dv.locator("#f-role button").first
        if role.count():
            role.click()
        dv.click('#f-status [data-status="queued"]')
        where = f"{story['place']}, third in the queue" if story["place"] else "Third in the queue"
        dv.fill("#f-position", where)
        dv.fill("#f-note", "Waiting for the water to rise. Nothing has moved since 06:00.")
        if dv.locator("#dv-confirm-wrap:not([hidden]) #f-confirm").count():
            dv.check("#f-confirm")
        settle(dv, 500)
        dv.evaluate("window.scrollTo(0, 0)")
        rec.snap(dv, "drv_form", {"phone": "[0, 0, innerWidth, innerHeight]", "st_queued": S('#f-status [data-status="queued"]'),
                                  "f_status": S("#f-status"), "f_position": S("#f-position"), "f_note": S("#f-note"),
                                  "dv_send": S("#dv-send"), "dv_sub": S("#dv-sub")}, full=True, kind="phone")
        dv.click("#dv-send")
        dv.wait_for_function("document.querySelector('#dv-log') && document.querySelector('#dv-log').children.length", timeout=60000)
        settle(dv, 900)
        rec.snap(dv, "drv_sent", {"dv_log": "d.box(document.querySelector('#dv-log').firstElementChild)",
                                  "dv_send": S("#dv-send")}, full=True, kind="phone")
        phone.close()
        ship_page("ship_report")

        # ---- under the hood: the sources
        page.goto(api.url("/profile"), wait_until="load")
        page.wait_for_selector('.prail[data-tab="sources"]', timeout=60000)
        settle(page, 1200)
        page.click('.prail[data-tab="sources"]')
        page.wait_for_selector("#tab-sources .feed", timeout=60000)
        settle(page, 800)
        page.evaluate(HELPERS)
        feed = "(re) => d.box(d.qa('#tab-sources .feed').filter((f) => re.test(f.innerText))[0])"
        rec.snap(page, "prof_top", {
            "prof_back": T("header", "Horizon", "a"), "rail_sources": S('.prail[data-tab="sources"]'),
            "src_count": S("#tab-sources .rpanel-note b"),
            "src_free": "d.box(d.qa('#tab-sources .tag').filter((t) => /free/i.test(t.innerText))[0])",
            "src_free_row": f"({feed})(/free/i)",
            "src_custom": f"({feed})(/carrier_notices|import_sika|CSV/i)",
        })
        page.evaluate("""() => { const f = __demo.qa('#tab-sources .feed').find((x) => /AIS vessel/i.test(x.innerText))
            || __demo.qa('#tab-sources .feed').find((x) => /licen[cs]e/i.test(x.innerText) && /absent/i.test(x.innerText));
          if (f) __demo.reveal(f, 380); }""")
        settle(page, 600)
        rec.snap(page, "prof_absent", {
            "src_ais": f"({feed})(/AIS vessel/i)",
            "src_portals": f"({feed})(/forwarder portals|licensed; requires/i)",
            "src_paid": U(f"({feed})(/AIS vessel/i)", f"({feed})(/forwarder portals|licensed; requires/i)"),
        })

        # ---- the board again: the signals, the early warnings, Ask, the
        # all-hands, alerts, and closing the case
        board_ready(page, api)
        page.click('.ptab[data-ptab="signals"]')
        page.wait_for_selector("#siglist .sig-funnel", timeout=60000)
        settle(page, 900)
        page.evaluate(HELPERS)
        page.evaluate("__demo.reveal(document.querySelector('#siglist .sig-funnel'), 6)")
        settle(page, 500)
        row_re = "(re) => d.box(d.qa('#siglist .sig-row').filter(d.vis).filter((r) => re.test(r.innerText))[0])"
        rec.snap(page, "sig_funnel", {
            **header, "funnel": S("#siglist .sig-funnel"), "st_first": S("#siglist .sig-stage", 0),
            "st_ai": "d.box(d.qa('#siglist .sig-stage').filter(d.vis).slice(-1)[0])",
            "kept": S("#siglist .sig-rows .sig-head"),
            "ev_first": S("#siglist .sig-row--event, #siglist .sig-row"),
            "ev_gauge": f"({row_re})(/pegel|gauge|water level/i)",
            "ev_forecast": f"({row_re})(/forecast|open_meteo/i)",
            "ev_sudden": f"({row_re})(/reuters|gulfnews|blockade|strike/i)",
            "ev_closure": f"({row_re})(/autobahn|closure|Sperrung/i)",
            "ev_predict": U(f"({row_re})(/pegel|gauge|water level/i)", f"({row_re})(/forecast|open_meteo/i)"),
            "ev_sudden_rows": U(f"({row_re})(/reuters|gulfnews|blockade|strike/i)", f"({row_re})(/autobahn|closure|Sperrung/i)"),
        })
        # The same list further down: the sudden events, news and closures.
        page.evaluate("""() => { const rows = __demo.qa('#siglist .sig-row');
          const r = rows.find((x) => /reuters|gulfnews|blockade|strike/i.test(x.innerText)) || rows[3];
          if (r) __demo.reveal(r, 90); }""")
        settle(page, 500)
        rec.snap(page, "sig_rows", {
            **header, "ev_sudden": f"({row_re})(/reuters|gulfnews|blockade|strike/i)",
            "ev_closure": f"({row_re})(/autobahn|closure|Sperrung/i)",
            "ev_sudden_rows": U(f"({row_re})(/reuters|gulfnews|blockade|strike/i)", f"({row_re})(/autobahn|closure|Sperrung/i)"),
        })
        page.evaluate("document.getElementById('siglist').scrollIntoView(); __demo.scroller(document.querySelector('#siglist .cs')).scrollTop = 0")
        settle(page, 500)
        rec.snap(page, "sig_top", {
            **header, "ews": U(S("#siglist .cs", 0), S("#siglist .cs", 1)),
            "burst": "d.box(d.qa('#siglist .cs')[0] && d.qa('#siglist .cs')[0].querySelector('.cs-row'))",
            "pushout": "d.box(d.qa('#siglist .cs')[1] && d.qa('#siglist .cs')[1].querySelector('.cs-row'))",
            "burst_sec": S("#siglist .cs", 0), "pushout_sec": S("#siglist .cs", 1),
        })
        if page.locator("#btn-volume:not([hidden])").count():
            page.click("#btn-volume")
            settle(page, 1200)
        rec.snap(page, "volume", {**header, "vol_panel": S("#volume-panel"),
                                  "vol_row": "d.head('#volume-panel', 3)",
                                  "surge_chip": S(".surge-chip"),
                                  "vol_area": U(S("#volume-panel"), S(".surge-chip"))})
        if page.locator("#volume-panel:not([hidden])").count():
            page.click("#btn-volume")
        page.click("#btn-ask")
        page.wait_for_selector("#ask-panel:not([hidden])")
        settle(page, 600)
        rec.snap(page, "ask0", {**header, "ask_panel": S("#ask-panel"), "chip_2": S(".ask-chip", 1)})
        page.locator(".ask-chip").nth(1).click()
        page.wait_for_selector(".ask-msg--bot", timeout=180000)
        settle(page, 1500)
        rec.snap(page, "ask1", {**header, "ask_panel": S("#ask-panel"), "ask_q": S(".ask-msg--you"),
                                "ask_a": S(".ask-msg--bot"), "ask_a_head": "d.head('.ask-msg--bot', 3)",
                                "ask_tag": T(".ask-msg--bot", "FROM THE BOARD") if page.locator(".ask-msg--bot.is-board").count()
                                else "d.head('.ask-msg--bot', 1)"})
        page.click("#ask-close")
        page.click('.ptab[data-ptab="allhands"]')
        settle(page, 1200)
        page.evaluate(HELPERS)
        ah = {**header, "ah_top": S("#allhands .ah2-hero"), "ah_meets": S("#allhands .ah2-when"),
              "ah_tiles": "d.union(d.qa('#allhands .ah2-tile').map(d.box))", "ah_tile1": S("#allhands .ah2-tile"),
              "rsvp": S("#rsvp .rsvp-ring"), "rsvp_dot1": S(".rsvp-dot"), "rsvp_n": S(".rsvp-n")}
        rec.snap(page, "ah", ah)
        fn = page.locator(".rsvp-dot").first.get_attribute("data-fn") if page.locator(".rsvp-dot").count() else None
        if fn:
            page.locator(".rsvp-dot").first.click()
            settle(page, 600)
            rec.snap(page, "ah_card", {**ah, "rsvp_card": S("#rsvp-card"), "rsvp_yes": S('#rsvp-card [data-set="confirmed"]')})
            page.click('#rsvp-card [data-set="confirmed"]')
            page.wait_for_selector(".rsvp-dot.is-yes", timeout=60000)
            settle(page, 700)
            rec.snap(page, "ah_conf", {**ah, "rsvp_card": S("#rsvp-card")})
        # Open it with its own button; something after a write can fold it
        # away again before the shot, so make sure it is open when taken.
        settle(page, 1500)
        page.evaluate("document.getElementById('btn-alerts').click()")
        settle(page, 900)
        page.evaluate("""() => { const p = document.getElementById('al-pop');
          if (!p.hidden) return;
          const b = document.getElementById('btn-alerts').getBoundingClientRect();
          p.style.top = `${Math.round(b.bottom + 8)}px`;
          p.style.right = `${Math.max(12, Math.round(innerWidth - b.right))}px`;
          p.hidden = false; }""")
        settle(page, 400)
        rec.snap(page, "alerts", {**header, "al_pop": S("#al-pop"), "al_levels": S("#al-pop .al-levels"),
                                  "al_red": "d.box(document.getElementById('al-red').closest('label'))"})
        shut = "document.getElementById('al-pop').hidden = true; document.getElementById('btn-alerts').setAttribute('aria-expanded', 'false')"
        page.evaluate(shut)
        page.click('.ptab[data-ptab="routes"]')
        settle(page, 500)
        page.evaluate(f"__demo.reveal(document.querySelector({json.dumps(card)}), 170)")
        page.click(card + " .rli-open")
        page.wait_for_selector("#panel-body:not([hidden])")
        settle(page, 1500)
        page.click("#dt-write summary")
        settle(page, 600)
        page.evaluate(HELPERS)
        page.evaluate("__demo.reveal(document.getElementById('send-pdf'), 520)")
        settle(page, 600)
        page.evaluate(shut)
        rec.snap(page, "esc", {**header, "pdf_btn": S("#send-pdf"), "esc_block": S("#r-escalate"), "compose": S(".compose")})
        page.evaluate("__demo.reveal(document.getElementById('d-next'), 150)")
        settle(page, 400)
        page.evaluate(shut)
        page.evaluate("document.getElementById('case-close').click()")
        page.wait_for_selector("#case-pick:not([hidden])", timeout=30000)
        settle(page, 600)
        rec.snap(page, "case0", {**header, "case_close": S("#case-close"), "case_pick": S("#case-pick"),
                                 "case_opt1": S('.case-opt[data-outcome="rerouted"]')})
        page.evaluate("document.querySelector('.case-opt[data-outcome=\"rerouted\"]').click()")
        settle(page, 1800)
        # Closed, the route leaves the list; the board says so in its ladder.
        board_ready(page, api)
        rec.snap(page, "final", {**header, "list": S("#panel-list")})
        # ...and the case is in the risk ledger's history.
        page.goto(api.url("/profile"), wait_until="load")
        page.wait_for_selector('.prail[data-tab="ledger"]', timeout=60000)
        page.click('.prail[data-tab="ledger"]')
        page.wait_for_selector("#case-history tbody tr", timeout=60000)
        settle(page, 800)
        rec.snap(page, "ledger", {"ledger_hist": S("#case-history"), "ledger_row": S("#case-history tbody tr")})
        browser.close()

    # Put back what the walk changed.
    try:
        if fn:
            api.post("/api/allhands/rsvp", {"function_id": fn, "status": "pending"})
        case = next((c for c in api.get("/api/cases")["cases"] if c.get("route_id") == R and not c.get("reopened")), None)
        if case:
            api.post(f"/api/cases/{urllib.parse.quote(case['case_id'])}/reopen", {})
        api.post("/api/penalties", {"enabled": pen0})
    except Exception as exc:  # noqa: BLE001
        print("  could not undo everything:", exc)


# ------------------------------------------------------------------ player
# Which page a frame is on, for recordings made before frames said so.
PAGES = (("tree", "/tree"), ("ship_", "/shipment"), ("drv_", "/driver"), ("prof_", "/profile"), ("ledger", "/profile"))


def build(out: Path, story: dict | None = None) -> Path:
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    if story is not None:
        manifest["vars"] = story
    board = json.loads((HERE / "storyboard.json").read_text(encoding="utf-8"))
    frames = {}
    for name, f in manifest["frames"].items():
        data = (out / f["file"]).read_bytes()
        # A target scrolled out of the shot is not a target: clip what shows,
        # drop what does not.
        kept = {}
        for k, b in f["targets"].items():
            if b:
                x0, y0 = max(0.0, b[0]), max(0.0, b[1])
                x1, y1 = min(float(f["w"]), b[0] + b[2]), min(float(f["h"]), b[1] + b[3])
                b = [x0, y0, x1 - x0, y1 - y0] if x1 - x0 > 2 and y1 - y0 > 2 else None
            kept[k] = b
        page = f.get("page") or next((v for k, v in PAGES if name.startswith(k)), "/")
        frames[name] = {**f, "page": page, "targets": kept,
                        "src": "data:image/webp;base64," + base64.b64encode(data).decode()}
    payload = {"story": board, "frames": frames, "vars": manifest.get("vars", {})}
    html = (HERE / "player.html").read_text(encoding="utf-8").replace("/*__DEMO__*/null", json.dumps(payload, ensure_ascii=False))
    target = out / "demo.html"
    target.write_text(html, encoding="utf-8")
    print(f"  player: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return target


# ------------------------------------------------------------------ video
def find_ffmpeg() -> str | None:
    import shutil  # noqa: PLC0415

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # noqa: PLC0415

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def _slice(job: tuple) -> str:
    """Render frames [first, last) of the player into one MP4 of its own."""
    import subprocess  # noqa: PLC0415

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    html, chromium, ffmpeg, fps, width, first, last, target = job
    height = round(width * 9 / 16)
    proc = subprocess.Popen([ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps),
                             "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                             "-pix_fmt", "yuv420p", "-r", str(fps), target], stdin=subprocess.PIPE)
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": chromium} if chromium else {}))
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(html + "?video=1", wait_until="load")
        page.wait_for_function("!document.getElementById('start').disabled", timeout=180000)
        for i in range(first, last):
            page.evaluate(f"__demo.seek({i / fps})")
            proc.stdin.write(page.screenshot(type="jpeg", quality=92))
        browser.close()
    proc.stdin.close()
    proc.wait()
    return target


def video(out: Path, chromium: str | None, fps: int = 25, width: int = 1920, workers: int = 0) -> Path | None:
    """The demo as one continuous MP4: the player in its video mode, no
    pauses, no controls, frame by frame into ffmpeg. Slices of the timeline
    render side by side, one browser each, and are joined without
    re-encoding."""
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("  video: no ffmpeg. Install one (free): pip install imageio-ffmpeg")
        return None
    board = json.loads((HERE / "storyboard.json").read_text(encoding="utf-8"))
    n = int(sum(s["dur"] for s in board["segments"]) * fps) + 1
    workers = workers or max(1, min(4, os.cpu_count() or 1))
    cuts = [round(n * k / workers) for k in range(workers + 1)]
    html = (out / "demo.html").resolve().as_uri()
    parts = [str(out / f".demo-part{k}.mp4") for k in range(workers)]
    jobs = [(html, chromium, ffmpeg, fps, width, cuts[k], cuts[k + 1], parts[k]) for k in range(workers)]
    t0 = time.time()
    print(f"  video: {n} frames at {fps} fps, {workers} at a time ...")
    with ProcessPoolExecutor(workers) as pool:
        list(pool.map(_slice, jobs))
    listing = out / ".demo-parts.txt"
    listing.write_text("".join(f"file '{Path(x).name}'\n" for x in parts), encoding="utf-8")
    target = out / "demo.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(target)], check=True)
    for x in [*parts, listing]:
        Path(x).unlink(missing_ok=True)
    print(f"  video: {target} ({target.stat().st_size / 1e6:.1f} MB, {time.time() - t0:.0f} s)")
    return target


# ------------------------------------------------------------------ the script
# What each camera target is, in words, for the written camera plan.
WHAT = {
    "map": "the map pane", "ladder": "the alert ladder in the header (Critical, Alert, Watch, Bias, Normal)",
    "card": "the critical route's card in the list", "card_open": "the card's open button (›)",
    "vessels": "the vessel icons at the origin port", "lane": "the route line, end to end",
    "ctx": "the map layer chips along the bottom", "ctx_ports": "the Ports chip", "ctx_inv": "the Inventory chip",
    "happening": "step 1 of the route's tree, What is happening: the event and its trend",
    "chip": "the CRITICAL chip", "title": "the route name", "who_hit": "step 2, Who is hit: orders on the route and exposure",
    "tab_ships": "the Shipments tab", "rows_mid": "the list of shipments on the route",
    "rows_major": "the rows marked Major disruption", "rows_nominal": "the rows marked Nominal",
    "status_major": "the word Major disruption", "status_nominal": "the word Nominal",
    "leg_shared": "the stretch both groups are on", "row": "the showcase vessel's row", "row_main": "the showcase vessel's row",
    "scard": "the shipment card that opens under the row", "leg_now": "the pill of the stretch it is on now",
    "prog": "the progress bar: % done, km to go", "jour": "the journey pills, stop by stop",
    "tile_plan": "the 'as planned' tile: its ETA and days late", "leg_risk": "the amber pill where the event hits",
    "tile_risk": "the 'at risk' tile in CHF", "hazard": "the red hazard marker on the map",
    "sel": "the selected vessel's halo", "routes_all": "the old route (dotted) and the two new ones",
    "badge1": "the #1 badge on the map", "badge2": "the #2 badge on the map",
    "hub": "the Action Hub", "hub_head": "the Action Hub's header: vessel, CRITICAL ROUTE, Major disruption",
    "hub_alts": "the Hub's list of ways (#1, #2)", "hub_sliders": "the Hub's Time, Cost and Risk sliders",
    "jm_dest": "the destination port's label on the map",
    "stat_when": "the Action by clock", "stat_exposure": "the Exposure tile", "stat_options": "the Options tile",
    "split_sec": "the Smart split section", "split_toggle": "the Split shipment switch",
    "split_boxes": "the box chips, urgent ones marked !", "split_urgent": "the first urgent box",
    "branches": "the two branch labels on the map", "branch_a": "branch A on the map", "branch_b": "branch B on the map",
    "partners": "the Partners nearby list", "partner_1": "the first partner", "btn_tree": "the card's Decision tree button",
    "keys": "the key-account boxes in Who is hit", "key_1": "the first key account", "key_2": "the second key account",
    "opt_best": "the BEST way's box", "opt_best_date": "its arrival date", "opt_best_cost": "its extra cost",
    "opt_best_co2": "its CO₂e figure (leaf icon: lowest)", "cost_block": "the If nobody acts breakdown",
    "pen_toggle": "the Count them (penalties) link", "pen_row": "the delay penalties line",
    "sign_box": "the Sign-off box (your limit)", "book_btn": "the Book it now button", "undo_btn": "the Undo button",
    "strs": "the vehicles, stretch by stretch", "unit_cur": "the vehicle carrying it now",
    "unit_wait": "a vehicle it has not reached yet (grey)", "boxes": "the box chips with their deadlines",
    "box_crit": "a critical box", "matrix": "the risk matrix", "matrix_dot": "the event's cell in the matrix",
    "radars": "the two risk-in-effect radars", "radar_m": "the Measured radar", "radar_legend": "the severity legend",
    "gauge_1": "the top category row with its days of delay", "rep_1": "the new report on the vehicle",
    "st_queued": "the Queued button", "f_position": "the Where are you field", "dv_send": "the Send report button",
    "dv_log": "the sent report", "link_profile": "the Risk profile link in the header",
    "src_count": "the line '16 external sources configured, 0 billable'", "src_free_row": "a free API's row",
    "src_free": "its FREE · NO KEY tag", "src_custom": "a custom connector's row (our own export)",
    "src_paid": "the commercial feeds' rows", "src_ais": "the AIS vessel tracking row (commercial licence)",
    "prof_back": "the ← Horizon link", "tab_signals": "the Signals tab", "funnel": "the signal funnel",
    "st_ai": "the Read by AI stage", "kept": "the kept / dropped count", "ev_first": "the first event read",
    "ev_predict": "the measured events (gauge, forecast)", "ev_gauge": "the river gauge event", "ev_forecast": "the forecast event",
    "ev_sudden_rows": "the reported events (news, closures)", "ev_sudden": "the news event", "ev_closure": "the road closure event",
    "ews": "the two early-warning lists", "burst": "the burst of small orders", "pushout": "the carrier push-out pattern",
    "vol_btn": "the Unusual volume button", "vol_area": "the Unusual volume panel and the route it lights up",
    "vol_row": "the route in the panel", "surge_chip": "the order-surge label on the map",
    "ask_btn": "the Ask button", "ask_panel": "the Ask panel", "chip_2": "the suggested question 'Which key accounts are at risk?'",
    "ask_a_head": "the answer", "ask_tag": "the answer's source tag",
    "tab_allhands": "the All-hands tab", "ah_top": "the meeting card", "ah_tiles": "the three limits",
    "ah_tile1": "the first limit crossed", "ah_meets": "Meets: Daily, and when it next sits", "rsvp": "the ring of departments",
    "rsvp_dot1": "the first department's dot", "rsvp_yes": "its Confirmed button", "alerts_btn": "the Alerts button",
    "al_pop": "the Critical alerts by email window", "al_red": "the Critical tick", "pdf_btn": "the Download PDF pack button",
    "case_pick": "the Close case choices", "case_opt1": "Rerouted", "d_case": "the closed case",
    "ctx_links": "the Road & rail chip", "ports": "the port icons on the map",
    "stock": "the plants and warehouses on the map", "sways": "the ways table: every way, evaluated",
    "ledger_row": "the closed case in the risk ledger's history",
}


def timings(board: dict) -> list[dict]:
    """The player's clock, in Python: when each segment starts, when each
    word is said, and when each cue fires. Must match player.html."""
    import re  # noqa: PLC0415

    def norm(w):
        return re.sub(r"[^\w\-₂]", "", w.lower().replace("_", ""))

    t0, out = 0.0, []
    for s in board["segments"]:
        words = s["text"].split()
        a, b = s.get("speak") or [0.2, s["dur"] - 0.25]
        wt = [len(re.sub(r"[^\w₂]", "", w)) + 1.6 + (2.4 if re.search(r"[,:;]$", w) else 0)
              + (3.4 if re.search(r"[.!?]$", w) else 0) for w in words]
        tot, acc, times = sum(wt), 0.0, []
        for x in wt:
            times.append(t0 + a + (b - a) * acc / tot)
            acc += x
        keys = [norm(w) for w in words]
        frm, cues = 0, []
        for c in s["cues"]:
            if "at" in c:
                at = t0 + c["at"]
            else:
                ph = [norm(w) for w in c["word"].split()]
                hit = next((i for i in list(range(frm, len(keys) - len(ph) + 1)) + list(range(0, len(keys)))
                            if keys[i:i + len(ph)] == ph), None)
                frm = hit if hit is not None else frm
                at = max(t0, min(t0 + s["dur"] - 0.05, times[hit] - c.get("lead", 0.25))) if hit is not None else t0
            cues.append({**c, "time": at, "found": "at" in c or hit is not None})
        out.append({**s, "start": t0, "cues": sorted(cues, key=lambda c: c["time"])})
        t0 += s["dur"]
    return out


def script_md(board: dict) -> str:
    segs = timings(board)
    total = sum(s["dur"] for s in segs)

    def clock(t):
        return f"{int(t // 60)}:{t % 60:04.1f}"

    def what(t):
        return WHAT.get(t, t.replace("_", " "))

    def camera(c):
        cam = c.get("cam")
        if cam is None:
            return ""
        if cam == "full":
            return "pull back to the full screen"
        how = f"zoom {cam['z']}×" if "z" in cam else "frame tightly"
        return f"{how} on {what(cam['t'])}"

    lines = [f"# {board['title']} demo: script and camera plan", "",
             f"Runtime without pauses: **{int(total // 60)}:{int(total % 60):02d}**. "
             + " · ".join(f"{who} {sum(s['dur'] for s in segs if s['speaker'] == who):.0f} s"
                          for who in board["parts"])
             + f". With the {sum(1 for s in segs if s.get('pause'))} pauses it runs about 3:00, depending on how long "
               "each one is held.", "",
             "Generated from `scripts/demo/storyboard.json` by `python scripts/demo/record.py --script`; "
             "edit the storyboard, not this file.", ""]
    for who, part in board["parts"].items():
        mine = [s for s in segs if s["speaker"] == who]
        a, b = mine[0]["start"], mine[-1]["start"] + mine[-1]["dur"]
        lines += [f"## {who}: {part} ({clock(a)} to {clock(b)})", "", "**The words**", ""]
        lines += ["> " + " ".join(s["text"] for s in mine), ""]
        lines += ["**Camera, cues and breakpoints**", ""]
        for s in mine:
            lines += [f"### {clock(s['start'])} · {s['id']}", "", f"_{s['text']}_", ""]
            for c in s["cues"]:
                bits = []
                if c.get("frame"):
                    bits.append(f"screen: **{c['frame']}**")
                if camera(c):
                    bits.append(camera(c) + (f" ({c['dur']} s)" if c.get("dur") else ""))
                if c.get("click"):
                    bits.append(f"click {what(c['click'])}")
                elif c.get("cursor"):
                    bits.append(f"cursor to {what(c['cursor'])}")
                if c.get("ring"):
                    bits.append("ring " + " and ".join(what(r) for r in [c["ring"]] if isinstance(c["ring"], str))
                                if isinstance(c["ring"], str) else "ring " + " and ".join(what(r) for r in c["ring"]))
                if c.get("note"):
                    bits.append(f"label \"{c['note']['text']}\"")
                on = f" on \"{c['word']}\"" if c.get("word") else ""
                lines.append(f"- `{clock(c['time'])}`{on}: " + "; ".join(bits))
            if s.get("pause"):
                lines += ["", f"**[INTERACTIVE PAUSE - WAIT FOR CLICK]** at `{clock(s['start'] + s['dur'])}`: "
                              f"hotspot on {what(s['pause']['t'])}, \"{s['pause']['label']}\""]
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://localhost:8000", help="the running server")
    ap.add_argument("--as-of", default=DEMO_AS_OF, help="the board's instant")
    ap.add_argument("--out", default=str(ROOT / "data" / "exports" / "demo"))
    ap.add_argument("--route", help="the route to follow (default: picked from the board)")
    ap.add_argument("--ship", help="the shipment to follow (default: picked from the board)")
    ap.add_argument("--dpr", type=float, default=2.0, help="pixels per CSS pixel in the frames")
    ap.add_argument("--quality", type=int, default=82, help="WebP quality")
    ap.add_argument("--chromium", help="path to a Chromium binary (default: Playwright's)")
    ap.add_argument("--build-only", action="store_true", help="rebuild demo.html from the frames already recorded")
    ap.add_argument("--script", metavar="PATH", help="only write the script and camera plan (Markdown) to PATH")
    ap.add_argument("--video", action="store_true", help="also render demo.mp4 (needs ffmpeg or imageio-ffmpeg)")
    args = ap.parse_args()
    out = Path(args.out)
    if args.script:
        Path(args.script).write_text(script_md(json.loads((HERE / "storyboard.json").read_text(encoding="utf-8"))), encoding="utf-8")
        print(f"  script: {args.script}")
        return
    if args.build_only:
        build(out)
        if args.video:
            video(out, args.chromium)
        return
    api = Api(args.base, args.as_of)
    t0 = time.time()
    print(f"Picking the story on {args.base} ...")
    story = pick(api, args.route, args.ship)
    print(f"  route {story['route']}: {story['route_name']}")
    print(f"  follows {story['ship']} ({story['vessel']}, {story['mode']}), split moves {story['moved']} of {story['boxes']}")
    rec = Recorder(out, args.dpr, args.quality)
    print("Recording frames ...")
    record(api, story, rec, args.chromium)
    public = {k: v for k, v in story.items() if k not in ("fleet", "lane_path")}
    (out / "manifest.json").write_text(json.dumps({"frames": rec.frames, "vars": public}, indent=1), encoding="utf-8")
    build(out)
    if args.video:
        video(out, args.chromium)
    print(f"Done in {time.time() - t0:.0f} s. Open {out / 'demo.html'} in a browser.")


if __name__ == "__main__":
    main()
