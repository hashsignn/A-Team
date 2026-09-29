"""Drive the fleet map in a headless browser and fail on anything broken.

    .venv/bin/python scripts/dev_check_map.py [port] [screenshot dir]

A 200 from /api/map/assets proves the endpoint answers and nothing about the
map. WebGL can fail to start, a symbol layer can render no icons because an
image never loaded, a card can open empty — all while every request returns
200. So the map is looked at, clicked, hovered, dragged and closed, the way a
planner would use it.

It is also driven through ``window.MapAgent``, the way an agent would. The
contract is that both routes produce the same state, so both are exercised.

Tile requests to the basemap providers are NOT errors here: this checker has
to pass with the network cable pulled out, which is when the map falls back to
the vendored country outlines — and the legend has to say so. The fallback
itself is checked on a second page whose tile requests are answered here, not
by the providers: one refusing, the next serving a tile.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8600
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/shots-map")
OUT.mkdir(parents=True, exist_ok=True)
BASE = f"http://localhost:{PORT}"

TILE_HOSTS = ("server.arcgisonline.com", "gibs.earthdata.nasa.gov",
              "basemaps.cartocdn.com", "tile.openstreetmap.org")



def _grey_tile(size: int = 256, level: int = 200) -> bytes:
    """A plain grey PNG tile, built rather than pasted.

    A PNG copied in by hand with one wrong byte fails its checksum, the map
    cannot decode it, and the provider "serving" it then looks as if it were
    refusing too — which is how this check first failed.
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    row = b"\x00" + bytes([level] * 3) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))


TILE_PNG = _grey_tile()


def _ignorable(text: str) -> bool:
    return any(h in text for h in TILE_HOSTS) or "ERR_TUNNEL" in text \
        or "ERR_NAME_NOT_RESOLVED" in text or "ERR_INTERNET_DISCONNECTED" in text


def main() -> int:
    errors: list[str] = []

    def check(ok: bool, message: str, detail: str = "") -> bool:
        if ok:
            if detail:
                print(f"  ok   {detail}")
        else:
            errors.append(message)
            print(f"  FAIL {message}")
        return ok

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page(viewport={"width": 1680, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"[js] {e}"))
        page.on("requestfailed",
                lambda r: None if _ignorable(r.url) else errors.append(f"[net] {r.url} {r.failure}"))
        page.on("console",
                lambda m: errors.append(f"[console] {m.text}")
                if m.type == "error" and not _ignorable(m.text) else None)

        page.goto(f"{BASE}/", wait_until="load", timeout=90_000)
        page.wait_for_function(
            "window.MapAgent && MapAgent.getState().assets.status === 'ready'", timeout=90_000)
        page.wait_for_function("window.__fleetmap && window.__fleetmap.getLayer('asset-icons')",
                               timeout=30_000)
        page.wait_for_timeout(3_000)
        page.screenshot(path=str(OUT / "map.png"))

        # ---- the map is the default, and it drew -----------------------
        check(page.evaluate("document.querySelector('.globe-wrap').classList.contains('is-map')"),
              "[view] the map is not the default left pane", "map is the default pane")
        state = page.evaluate("""(() => { const s = MapAgent.getState();
            return { total: s.assets.items.length, visible: MapStore.select.visibleAssets(s).length,
                     counts: s.assets.counts }; })()""")
        check(state["visible"] > 5, f"[assets] only {state['visible']} visible assets",
              f"{state['visible']} active assets of {state['total']} not yet delivered")
        drawn = page.evaluate("""(() => { const m = window.__fleetmap;
            return { icons: m.queryRenderedFeatures({layers: ['asset-icons']}).length,
                     clusters: document.querySelectorAll('.mcluster').length }; })()""")
        check(drawn["icons"] + drawn["clusters"] > 0, "[assets] nothing rendered: no icons, no clusters",
              f"{drawn['icons']} icons + {drawn['clusters']} donut clusters on screen")
        images = page.evaluate("""['sea','barge','rail','road'].flatMap(m =>
            ['red','yellow','blue','white','green']
            .map(l => window.__fleetmap.hasImage(`asset-${m}-${l}`))).every(Boolean)""")
        check(images, "[icons] a modality/level icon failed to load",
              "20 SVG icons: 4 modes x 5 ladder levels")
        # One key on the board: every vehicle wears its route's ladder colour.
        mismatch = page.evaluate("""(() => { const s = MapAgent.getState();
            return window.__fleetmap.querySourceFeatures('assets')
              .filter(f => !f.properties.cluster)
              .filter(f => f.properties.level !== (s.assets.byId[f.properties.id].route_level || 'green'))
              .length; })()""")
        check(mismatch == 0, f"[icons] {mismatch} vehicle(s) not in their route's ladder colour",
              "vehicles coloured by their route's ladder level")
        check(page.locator("#posture").is_hidden(),
              "[chrome] the convene strip is drawn over the map", "no convene strip over the map")
        check(page.locator("#map-legend").count() == 0,
              "[legend] a map legend is back beside the ladder",
              "no map legend: the ladder is the board's one key")
        check(page.locator(".maplibregl-ctrl-compass").count() == 1,
              "[controls] no reset-bearing compass", "zoom + compass (reset bearing) controls")

        # ---- unusual volume: a burst of small orders on a route's flow ----
        volume = page.evaluate("""(() => { const s = MapAgent.getState();
            return s.assets.orders ? s.assets.lanes.filter(l => l.early_warning).length : null; })()""")
        check(volume is not None, "[volume] the map payload carries no order signals",
              f"unusual volume: {volume} route(s) with a burst of small orders")
        if volume:
            count = page.inner_text("#volume-count").strip()
            check(count == str(volume), f"[volume] button says {count}, payload says {volume}",
                  "the Unusual volume button counts the warned routes")
            lines = page.evaluate("""new Set(window.__fleetmap.querySourceFeatures('surges')
                .map(f => f.properties.id)).size""")
            check(lines == volume, f"[volume] {lines} blue lines for {volume} routes",
                  f"{lines} route(s) drawn in blue, with a chip at the origin")
            check(page.locator(".surge-chip").count() == volume,
                  "[volume] chip count does not match", "one ↑× orders chip per warned route")
            page.click("#btn-volume")
            page.wait_for_selector(".surge-card", timeout=5_000)
            check(page.locator(".surge-card").count() == volume,
                  "[volume] panel does not list every warned route", "the panel lists every warned route")
            page.uncheck("#vp-show")
            page.wait_for_timeout(300)
            gone = page.evaluate("window.__fleetmap.querySourceFeatures('surges').length") == 0
            check(gone and page.locator(".surge-chip").count() == 0,
                  "[volume] 'Show on map' off left the layer drawn", "the blue layer hides on request")
            page.check("#vp-show")
            page.click(".vp-close")

        # ---- context layers: only around what is selected -------------------
        idle = page.evaluate("window.__fleetmap.querySourceFeatures('ctx-points').length")
        check(idle == 0 and page.locator("#ctx-filter").is_hidden(),
              "[context] ports/inventory/vendors drawn with nothing selected",
              "no context layers while nothing is selected")
        target = page.evaluate("""(() => { const a = MapAgent.getState().assets.items
            .find(x => x.phase === 'in_transit' && x.mode !== 'sea'); return a ? a.id : null; })()""")
        if target:
            page.evaluate(f"MapAgent.selectAsset('{target}')")
            page.wait_for_function("MapAgent.getState().context.status === 'ready'", timeout=30_000)
            page.wait_for_timeout(1_200)
            ticks = page.locator("#ctx-filter .ctx-tick").count()
            drawn = page.evaluate("""(() => ({ points: window.__fleetmap.querySourceFeatures('ctx-points').length,
                links: window.__fleetmap.querySourceFeatures('ctx-links').length }))()""")
            check(ticks == 5 and drawn["points"] > 0 and drawn["links"] > 0,
                  f"[context] selecting {target} drew {drawn} with {ticks} ticks",
                  f"context around {target}: 5 ticks, places and road/rail drawn")
            page.locator('#ctx-filter .ctx-tick:has(input[data-layer="links"])').click()
            page.wait_for_timeout(400)
            off = page.evaluate("window.__fleetmap.querySourceFeatures('ctx-links').length")
            check(off == 0, "[context] the Road & rail tick did not hide the links",
                  "each tick hides its layer")
            page.locator('#ctx-filter .ctx-tick:has(input[data-layer="links"])').click()
            page.evaluate("MapAgent.clearSelection()")
            page.wait_for_timeout(600)
            # Closing the vehicle leaves its route's layers if the route is
            # open, and none otherwise.
            left = page.evaluate("""(() => { const s = MapAgent.getState();
                return { scope: s.context.scope, isolated: s.isolated, lane: s.focusLane }; })()""")
            expected = {"kind": "route", "id": left["lane"]} if left["isolated"] and left["lane"] else None
            check(left["scope"] == expected and page.locator("#ctx-filter").is_hidden() == (expected is None),
                  f"[context] after closing the vehicle the layers show {left['scope']}, expected {expected}",
                  "the layers follow the selection when it closes")

        # ---- reset bearing and view --------------------------------------
        page.evaluate("window.__fleetmap.jumpTo({bearing: 40})")
        page.locator(".maplibregl-ctrl-compass").click()
        page.wait_for_timeout(900)
        bearing = page.evaluate("window.__fleetmap.getBearing()")
        check(abs(bearing) < 0.5, f"[controls] compass did not reset bearing ({bearing})",
              "compass resets bearing to north")

        # ---- a real click on a red asset ----------------------------------
        # At the opening zoom the disrupted assets may all sit inside donut
        # clusters, with no single icon to click. A planner zooms in on one,
        # so the checker does too: past the zoom where clusters break up.
        find_icon = """(() => { const m = window.__fleetmap;
            const fs = m.queryRenderedFeatures({layers: ['asset-icons']});
            const f = fs.find(x => x.properties.status === 'red') || fs.find(x => x.properties.status === 'yellow');
            if (!f) return null;
            const p = m.project(f.geometry.coordinates), r = m.getContainer().getBoundingClientRect();
            return { x: r.left + p.x, y: r.top + p.y, id: f.properties.id }; })()"""
        target = page.evaluate(find_icon)
        if target is None:
            zoomed = page.evaluate("""(() => { const s = MapAgent.getState();
                const a = MapStore.select.visibleAssets(s).find(x => x.status === 'red')
                       || MapStore.select.visibleAssets(s).find(x => x.status === 'yellow');
                if (!a) return null;
                window.__fleetmap.jumpTo({center: [a.lon, a.lat], zoom: 8});
                return a.id; })()""")
            if zoomed:
                page.wait_for_function("window.__fleetmap.loaded() && !window.__fleetmap.isMoving()",
                                       timeout=15_000)
                page.wait_for_timeout(1_200)
                target = page.evaluate(find_icon)
                if target:
                    print(f"  note every disrupted asset was clustered at the opening zoom; "
                          f"zoomed in on {zoomed} to click one")
        if not check(target is not None, "[click] no red or yellow icon rendered to click"):
            browser.close()
            return _report(errors)
        page.mouse.click(target["x"], target["y"])
        page.wait_for_function("MapAgent.getState().selection.status === 'ready'", timeout=30_000)
        page.wait_for_function("MapAgent.getState().routing.status === 'ready' "
                               "&& MapAgent.getState().vendors.status === 'ready'", timeout=30_000)
        page.wait_for_timeout(1_500)
        page.screenshot(path=str(OUT / "hub-click.png"))
        check(page.evaluate("MapAgent.getState().selection.id") == target["id"],
              "[click] clicking the icon selected a different asset",
              f"clicked {target['id']} on the map — Action Hub opened")
        check(page.locator("#hub").is_visible(), "[hub] not visible after a click")
        hub_text = page.locator("#hub").inner_text()
        for needle, what in (("TEU", "capacity"), ("POSITION (", "position"),
                             ("LAST SYNC", "last sync"), ("STATUS LOG", "status log")):
            check(needle in hub_text.upper(), f"[hub] {what} missing from the card")
        # The dates and the ranked ways are the shipment's card in the route
        # panel; the Hub is the vehicle. Nothing is said in both.
        for needle in ("PLANNED ARRIVAL", "EXPECTED NOW", "VS PLAN"):
            check(needle not in hub_text.upper(), f"[hub] {needle!r} is repeated from the shipment card",
                  f"hub does not repeat the shipment card ({needle.lower()})")
        # What to do comes first on the card: the ways round before the
        # vehicle's own details.
        order = page.evaluate("""(() => [...document.querySelectorAll('#hub-body [data-sec]')]
            .map(e => e.dataset.sec))()""")
        check("routes" in order and "vehicle" in order and order.index("routes") < order.index("vehicle"),
              f"[hub] the recovery routes are not above the vehicle details: {order}",
              "hub: recovery routes first, the vehicle's details below")
        # One click on a vehicle opens its route in the panel too, so the map
        # and the panel describe the same thing.
        lane = page.evaluate("MapAgent.getState().assets.byId[MapAgent.getState().selection.id].lane_id")
        check(page.evaluate("state.selected") == lane and page.locator("#panel-body").is_visible(),
              f"[sync] the panel did not open the vehicle's route {lane}",
              f"the panel opened the vehicle's route ({lane})")
        # The same click marks the journey: where it starts, where it changes
        # mode, the ports, and who receives it.
        marks = page.evaluate("""(() => ({ route: (window.MapJourney.get() || {}).route_id,
            origin: document.querySelectorAll('.jm--origin').length,
            dest: document.querySelectorAll('.jm--dest').length,
            all: document.querySelectorAll('.jm').length }))()""")
        check(marks["route"] == lane and marks["origin"] == 1 and marks["dest"] == 1 and marks["all"] >= 3,
              f"[journey] the route's sites are not marked: {marks}",
              f"journey marked: {marks['all']} points from origin to destination")
        alone = page.evaluate("""(() => { const s = MapAgent.getState(), sel = MapStore.select;
            return { lanes: sel.visibleLanes(s).map(l => l.route_id),
                     other: sel.visibleAssets(s).filter(a => a.lane_id !== s.focusLane).length }; })()""")
        check(alone["lanes"] == [lane] and alone["other"] == 0,
              f"[focus] other routes still on the map: {alone}",
              "an open route stands alone: no other lane, no other vehicle")
        backdrop = page.evaluate("getComputedStyle(document.getElementById('hub')).backdropFilter")
        check("blur(10px)" in backdrop, f"[hub] no glass: backdrop-filter is {backdrop!r}",
              "glass card: backdrop-filter blur(10px)")
        cells = page.locator("#hub .rm .cell:not(.gutter)").count()
        check(cells == 25, f"[matrix] {cells} cells, expected 5 x 5", "risk matrix: 5 x 5")
        radar = page.evaluate("""(() => { const c = Chart.getChart('hub-radar');
            return c ? { type: c.config.type, n: c.data.labels.length } : null; })()""")
        check(bool(radar) and radar["type"] == "radar" and radar["n"] == 5,
              f"[radar] chart wrong or missing: {radar}", "radar chart: 5 axes (Chart.js)")

        # ---- recovery routes on the map --------------------------------------
        routes = page.evaluate("""(() => { const s = MapAgent.getState(), m = window.__fleetmap;
            return { n: MapStore.select.rankedRoutes(s).length, eligible: s.routing.data.eligible,
                     alts: m.querySourceFeatures('alts').length,
                     original: m.querySourceFeatures('original').length,
                     badges: document.querySelectorAll('.rbadge[data-route]').length }; })()""")
        if routes["n"]:
            check(routes["badges"] == routes["n"], f"[routes] {routes['badges']} badges for {routes['n']} routes",
                  f"{routes['n']} alternate route(s) drawn with rank badges")
            check(routes["original"] > 0, "[routes] original route not drawn",
                  "original route drawn (dotted grey)")
        else:
            print(f"  note {target['id']} has no alternate — {page.evaluate('MapAgent.getState().routing.data.note')}")

        # ---- choose an asset that exercises everything, via the agent -----
        chosen = page.evaluate("""(async () => {
            const s = MapAgent.getState();
            // Every vehicle, not only the visible ones: an open route shows
            // only its own, and the one worth testing may be on another.
            const reds = s.assets.items.filter(a => a.status !== 'green');
            for (const a of reds) {
              await MapAgent.selectAsset(a.id, {actor: 'LAYA'});
              const st = MapAgent.getState();
              if (MapStore.select.rankedRoutes(st).length >= 2
                  && st.selection.detail.containers.length >= 3) return a.id;
            }
            return null; })()""")
        if not check(chosen is not None, "[agent] no disrupted asset with two routes and three boxes"):
            browser.close()
            return _report(errors)
        page.wait_for_timeout(1_500)
        journal = page.evaluate("MapAgent.journal()")
        check(any(j["actor"] == "LAYA" and "select" in j["action"] for j in journal),
              "[agent] an agent's call was not journalled under its name",
              f"agent path: MapAgent.selectAsset('{chosen}', {{actor: 'LAYA'}}) journalled")
        hooks = page.evaluate("MapAgent.describe().map(h => h.name)")
        check(len(hooks) >= 12 and {"selectAsset", "calculateRoutes", "rankRoutes", "splitShipment",
                                    "queryVendors", "selectVendor"} <= set(hooks),
              f"[agent] hook list incomplete: {hooks}", f"{len(hooks)} named agent hooks described")

        # ---- hover a route: the tooltip carries the delta ------------------
        spot = page.evaluate("""(() => { const s = MapAgent.getState(), m = window.__fleetmap;
            const c = MapStore.select.rankedRoutes(s)[0];
            const leg = c.legs.find(l => l.new) || c.legs[0];
            const pt = leg.path[Math.floor(leg.path.length / 2)];
            const p = m.project([pt[1], pt[0]]), r = m.getContainer().getBoundingClientRect();
            return { x: r.left + p.x, y: r.top + p.y, id: c.id }; })()""")
        page.mouse.move(spot["x"], spot["y"])
        page.wait_for_timeout(500)
        tip = page.locator("#map-tip")
        tip_text = tip.inner_text() if tip.is_visible() else ""
        check("CHF" in tip_text and "Risk:" in tip_text,
              f"[hover] route tooltip missing or without its delta: {tip_text[:80]!r}",
              f"hover tooltip: {tip_text.splitlines()[-2] if tip_text else ''}")
        check(page.evaluate("MapAgent.getState().routing.hovered") == spot["id"],
              "[hover] hovering a route did not highlight it in the state")
        page.screenshot(path=str(OUT / "hover.png"))
        page.mouse.move(5, 500)

        # ---- the ranking responds to the weights ---------------------------
        by_cost = page.evaluate("""(async () => { await MapAgent.rankRoutes({time: 0, cost: 1, risk: 0});
            const r = MapStore.select.rankedRoutes(MapAgent.getState());
            return r[0].cost_chf === Math.min(...r.map(c => c.cost_chf)); })()""")
        by_time = page.evaluate("""(async () => { await MapAgent.rankRoutes({time: 1, cost: 0, risk: 0});
            const r = MapStore.select.rankedRoutes(MapAgent.getState());
            return r[0].hours === Math.min(...r.map(c => c.hours)); })()""")
        check(by_cost and by_time, "[rank] #1 is not the cheapest under cost-only / fastest under time-only",
              "re-ranking: cost-only puts the cheapest at #1, time-only the fastest")
        page.evaluate("MapAgent.rankRoutes({time: 0.5, cost: 0.3, risk: 0.2})")
        page.wait_for_timeout(800)

        # ---- split: the toggle, the branches, the slider -------------------
        # The checkbox is visually hidden behind its switch; click what a
        # person clicks. Scrolled into view FIRST: the card body scrolls
        # smoothly, and a click that scrolls for itself hit-tests the old
        # position — the map under the card — and blames a marker.
        switch = page.locator("#hub label.switch")
        switch.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        switch.click()
        page.wait_for_function("MapAgent.getState().split.status === 'ready'", timeout=20_000)
        page.wait_for_timeout(800)
        # querySourceFeatures returns a line once per tile it crosses, so
        # lines are counted by id, not by feature.
        split = page.evaluate("""(() => { const s = MapAgent.getState(), m = window.__fleetmap;
            const ids = new Set(m.querySourceFeatures('split').map(f => f.properties.id));
            return { branches: s.split.data.branches.length, lines: ids.size,
                     labels: document.querySelectorAll('.branch-label').length,
                     reason: s.split.data.suggestion_reason }; })()""")
        check(split["lines"] == split["branches"] and split["labels"] == split["branches"],
              f"[split] {split['branches']} branches but {split['lines']} lines / {split['labels']} labels",
              f"split: {split['branches']} tracking line(s) on the map")
        slider = page.locator("#split-n")
        n_boxes = int(slider.get_attribute("max"))
        slider.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); }",
                        str(n_boxes))
        page.wait_for_function("MapAgent.getState().split.status === 'ready'", timeout=20_000)
        page.wait_for_timeout(600)
        moved = page.evaluate("MapAgent.getState().split.data.summary.moved")
        check(moved == n_boxes, f"[split] slider to {n_boxes} moved {moved}",
              f"allocation slider: moved all {n_boxes} boxes")
        slider.evaluate("(el) => { el.value = '1'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
        page.wait_for_function("MapAgent.getState().split.data.summary.moved === 1", timeout=20_000)
        page.wait_for_timeout(600)
        lines = page.evaluate("new Set(window.__fleetmap.querySourceFeatures('split')"
                              ".map(f => f.properties.id)).size")
        check(lines >= 2, f"[split] a 1-box split drew {lines} line(s), expected 2",
              "1 urgent box moved: the shipment branches into 2 tracking lines")
        page.screenshot(path=str(OUT / "split.png"))

        # ---- partners: a pin, a card ---------------------------------------
        pins = page.locator(".vpin").count()
        check(pins > 0, "[vendors] no partner markers on the map", f"{pins} partner marker(s) mapped")
        if pins:
            # A pin the card does not cover — the one under the card is not
            # one a planner can click either.
            free = page.evaluate("""(() => { const hub = document.getElementById('hub').getBoundingClientRect();
                const pins = [...document.querySelectorAll('.vpin')];
                const i = pins.findIndex(p => { const b = p.getBoundingClientRect();
                  const x = b.left + b.width / 2, y = b.top + b.height / 2;
                  return document.elementFromPoint(x, y) && p.contains(document.elementFromPoint(x, y)); });
                return i; })()""")
            if free < 0:
                # A cluster donut can sit on the partners at this zoom. A
                # planner zooms in on them; so does the check.
                page.evaluate("""(() => { const v = MapStore.select.vendors(MapAgent.getState())[0];
                    if (v) window.__fleetmap.jumpTo({center: [v.lon, v.lat], zoom: 9}); })()""")
                page.wait_for_function("!window.__fleetmap.isMoving()", timeout=10_000)
                page.wait_for_timeout(900)
                free = page.evaluate("""(() => { const pins = [...document.querySelectorAll('.vpin')];
                    return pins.findIndex(p => { const b = p.getBoundingClientRect();
                      const x = b.left + b.width / 2, y = b.top + b.height / 2;
                      const at = document.elementFromPoint(x, y);
                      return at && p.contains(at); }); })()""")
            check(free >= 0, "[vendors] every partner marker is covered", "")
            page.locator(".vpin").nth(max(free, 0)).click()
            page.wait_for_timeout(600)
            card = page.locator("#vendor-card")
            check(card.is_visible(), "[vendors] clicking a partner marker opened no card")
            text = card.inner_text()
            check("SERVICEABLE ROUTES" in text.upper() and ("AVAILABLE NOW" in text.upper()),
                  "[vendors] partner card lacks capacity or serviceable routes",
                  "partner card: contact, capacity, serviceable routes")
            page.screenshot(path=str(OUT / "vendor.png"))

        # ---- drag the hub ---------------------------------------------------
        before = page.locator("#hub").bounding_box()
        head = page.locator("#hub-head").bounding_box()
        page.mouse.move(head["x"] + 60, head["y"] + 12)
        page.mouse.down()
        page.mouse.move(head["x"] - 140, head["y"] + 72, steps=6)
        page.mouse.up()
        after = page.locator("#hub").bounding_box()
        check(abs(after["x"] - before["x"]) > 100, "[hub] dragging the header did not move the card",
              "hub is draggable")
        check(page.locator("#hub-head").bounding_box()["y"] >= after["y"] - 1,
              "[hub] the header scrolled out of the card", "header stays pinned to the card")

        # ---- close: everything it drew goes with it ------------------------
        page.locator("#hub-close").click()
        page.wait_for_timeout(700)
        gone = page.evaluate("""(() => { const m = window.__fleetmap;
            return { hub: document.getElementById('hub').hidden,
                     card: document.getElementById('vendor-card').hidden,
                     alts: m.querySourceFeatures('alts').length, split: m.querySourceFeatures('split').length,
                     original: m.querySourceFeatures('original').length,
                     badges: document.querySelectorAll('.rbadge[data-route]').length,
                     pins: document.querySelectorAll('.vpin').length,
                     chart: !!Chart.getChart('hub-radar') }; })()""")
        check(gone == {"hub": True, "card": True, "alts": 0, "split": 0, "original": 0,
                       "badges": 0, "pins": 0, "chart": False},
              f"[close] something survived closing the hub: {gone}",
              "closing removes the card, charts, routes, split and partners")
        page.locator("#d-back").click()
        page.wait_for_timeout(300)
        left = page.evaluate("document.querySelectorAll('.jm').length")
        check(left == 0, f"[journey] {left} marker(s) left after Back", "Back clears the journey")
        back = page.evaluate("MapStore.select.visibleLanes(MapAgent.getState()).length")
        check(back > 1, f"[focus] only {back} lane(s) after Back", f"Back brings every route back ({back} lanes)")

        # ---- themes repaint the basemap -----------------------------------
        page.locator(".theme-toggle").click()
        page.locator('.theme-btn[data-theme="dark"]').click()
        page.wait_for_timeout(900)
        dark = page.evaluate("window.__fleetmap.getPaintProperty('bg', 'background-color')")
        page.locator(".theme-toggle").click()
        page.locator('.theme-btn[data-theme="light"]').click()
        page.wait_for_timeout(900)
        light = page.evaluate("window.__fleetmap.getPaintProperty('bg', 'background-color')")
        check(dark != light, "[theme] the basemap did not repaint on a theme switch",
              "basemap repaints with the theme")

        # ---- the globe is one click away, and the URL says so --------------
        page.locator('.viewswitch button[data-view="globe"]').click()
        page.wait_for_timeout(1_500)
        check("view=globe" in page.url and page.locator("#globe canvas").count() > 0,
              "[view] switching to the globe failed", "globe toggle, ?view=globe in the URL")
        page.screenshot(path=str(OUT / "globe.png"))
        page.locator('.viewswitch button[data-view="map"]').click()
        page.wait_for_timeout(800)
        check("view=" not in page.url, "[view] switching back left ?view in the URL")

        # ---- a shareable link opens the hub --------------------------------
        page.goto(f"{BASE}/?asset={chosen}", wait_until="load", timeout=90_000)
        page.wait_for_function(f"MapAgent.getState().selection.id === {chosen!r} "
                               "&& MapAgent.getState().selection.status === 'ready'", timeout=60_000)
        check(page.locator("#hub").is_visible(), "[link] ?asset= did not open the hub",
              f"?asset={chosen} opens its Action Hub")

        _check_basemap_fallback(browser, check)
        browser.close()

    return _report(errors)


def _legend_note(page) -> str:
    return page.evaluate(
        "(document.querySelector('#map-note') || {}).textContent || ''")


def _check_basemap_fallback(browser, check) -> None:
    """A refusing provider is replaced by the next, and none is OSM's own.

    Answered here rather than by the providers, so it runs the same with and
    without a network: the first provider refuses every tile, the second
    serves one. Then every provider refuses, which is the offline case.
    """
    # By default the map draws the theme's own outline and asks no tile
    # server for anything: borders, no names.
    fetched: list[str] = []
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.on("request", lambda r: fetched.append(r.url) if any(h in r.url for h in TILE_HOSTS) else None)
    page.goto(f"{BASE}/", wait_until="load", timeout=90_000)
    page.wait_for_function("window.__fleetmap && window.__fleetmap.getLayer('asset-icons')",
                           timeout=60_000)
    page.wait_for_timeout(1_500)
    check(not fetched and page.evaluate("!!window.__fleetmap.getLayer('land-line')")
          and not page.evaluate("!!window.__fleetmap.getLayer('basemap')") and not _legend_note(page),
          f"[basemap] the default map fetched {len(fetched)} tile(s) or lost its borders",
          "default map: the theme's land and borders, no names, no tile requests")
    page.close()

    asked: list[str] = []

    def serve(route):
        url = route.request.url
        asked.append(url)
        if "server.arcgisonline.com" in url or "tile.openstreetmap.org" in url:
            route.fulfill(status=403, body="refused")
        else:
            route.fulfill(status=200, content_type="image/png", body=TILE_PNG)

    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.route("**/*", lambda route: serve(route)
               if any(h in route.request.url for h in TILE_HOSTS) else route.continue_())
    page.goto(f"{BASE}/?tiles=on", wait_until="load", timeout=90_000)
    page.wait_for_function("window.__fleetmap && window.__fleetmap.getLayer('asset-icons')",
                           timeout=60_000)
    page.wait_for_function(
        "(document.querySelector('#map-note') || {}).textContent"
        ".includes('NASA Blue Marble relief tiles, desaturated')", timeout=30_000)
    note = _legend_note(page)
    check("Esri World Light Gray did not answer" in note,
          f"[basemap] the note did not say the first provider refused: {note!r}",
          "a refusing tile provider is replaced by the next, and the note says so")
    check(not any("tile.openstreetmap.org" in u for u in asked),
          "[basemap] tiles were asked of OpenStreetMap's own servers",
          "no tile is asked of OpenStreetMap's volunteer servers")
    page.close()

    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.route("**/*", lambda route: route.fulfill(status=403, body="refused")
               if any(h in route.request.url for h in TILE_HOSTS) else route.continue_())
    page.goto(f"{BASE}/?tiles=on", wait_until="load", timeout=90_000)
    page.wait_for_function(
        "(document.querySelector('#map-note') || {}).textContent"
        ".includes('Offline outline')", timeout=30_000)
    check(page.evaluate("!!window.__fleetmap.getLayer('land')"),
          "[basemap] every provider refused and the country outlines are missing",
          "every provider refusing leaves the offline outline, and the note says so")
    page.close()


def _report(errors: list[str]) -> int:
    if errors:
        print("\nERRORS")
        for e in dict.fromkeys(errors):
            print("  ", e)
        return 1
    print(f"\nclean — screenshots in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
