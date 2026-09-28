"""Load the dashboard in a headless browser and fail on anything broken.

    .venv/bin/python scripts/dev_check_ui.py [port] [screenshot dir]

HTTP 200 proves the server is up and nothing else. A WebGL globe can fail to
initialise, a radar can render as an empty <svg>, and a JS exception leaves the
page half-built — all while every request returns 200. So the page has to be
looked at, and clicked.

What it checks, in the order a planner meets it:

    the globe        drew something (the left pane opens on the 2D map, so
                     the globe is switched to first — the map has its own
                     checker, scripts/dev_check_map.py)
    the ladder       five rungs
    the routes       the right panel's cards, ranked by level
    a route          clicking a card opens it in the panel, Back returns to
                     the list, and another card opens another route
    the as-of        re-runs the board, and the URL can be shared
    the response     actions, contacts, escalation and the summary draft,
                     its mailto and its PDF
    the route page   "Open this route" leads to its page: both radars drew,
                     the matrix drew, and it survives every theme — with the
                     unsourced band outside the probability axis
    the profile      every tab renders, a broken ladder is refused, a valid
                     save round-trips and Reset puts it back
    the assistant    with no model it explains rather than fails

The profile check writes config/scoring.yaml and Reset deletes it. An overlay
that was already there is someone's real edit, so it is set aside before the
check and put back after, whatever happens.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8600
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/shots")
OUT.mkdir(parents=True, exist_ok=True)
BASE = f"http://localhost:{PORT}"
ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "config" / "scoring.yaml"

# The map's basemap tiles come from these hosts, and failing to reach them is
# not an error: the map falls back to the vendored country outlines and says
# so, which scripts/dev_check_map.py checks. Same list as that script.
TILE_HOSTS = ("server.arcgisonline.com", "gibs.earthdata.nasa.gov",
              "basemaps.cartocdn.com", "tile.openstreetmap.org")

# The ladder's own order, for "ranked by level".
LEVEL_RANK = {"Critical": 4, "Alert": 3, "Watch": 2, "Bias": 1, "Normal": 0}


def _drawn_fraction(path: Path, tolerance: int = 30) -> float:
    """Share of pixels that differ from the most common colour in the shot.

    An empty canvas shows one colour — the page behind it, whatever the theme.
    A drawn globe is land, sea, arcs and shading. This used to count pixels
    brighter than near-black, which was right for a dark page and meaningless
    on a light one: a blank canvas over a white page read as "100% lit".
    """
    img = Image.open(path).convert("RGB")
    img = img.resize((max(1, img.width // 4), max(1, img.height // 4)))
    data = img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()
    pixels = list(data)
    if not pixels:
        return 0.0
    background = Counter(pixels).most_common(1)[0][0]
    far = sum(1 for p in pixels
              if sum(abs(a - b) for a, b in zip(p, background, strict=True)) > tolerance)
    return far / len(pixels)


def _server_alive() -> bool:
    """Is the thing we were checking still there? Cheap, and never raises."""
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 — any failure means "no"
        return False


def _is_tile(url: str) -> bool:
    return any(host in url for host in TILE_HOSTS)


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

    # One check deliberately provokes a 422 — it asserts that a ladder with
    # Critical later than Alert is REFUSED. The browser logs every non-2xx as
    # a console error, so that expected refusal would fail the run it proves.
    # Set while the refusal is being driven, and only then.
    expecting_refusal = [False]

    kept_overlay = OVERLAY.read_bytes() if OVERLAY.exists() else None
    if kept_overlay is not None:
        print(f"  note {OVERLAY.relative_to(ROOT)} exists — set aside for the profile "
              "check, and put back after")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            page = browser.new_page(viewport={"width": 1680, "height": 1050})
            page.on("pageerror", lambda e: errors.append(f"[js] {e}"))
            page.on("requestfailed", lambda r: None if _is_tile(r.url)
                    else errors.append(f"[net] {r.url} — {r.failure}"))

            def _console(message) -> None:
                if message.type != "error":
                    return
                if expecting_refusal[0] and "422" in message.text:
                    return
                where = (message.location or {}).get("url", "")
                if _is_tile(where) or _is_tile(message.text):
                    return
                errors.append(f"[console] {message.text}")

            page.on("console", _console)

            _board(page, check)
            _route_page(page, check)
            _desk(page, check)
            _review(page, check)
            _profile(page, check, expecting_refusal)
            _themes_and_unsourced(page, check)
            _assistant(page, check)

            health = page.request.get(f"{BASE}/api/health").json()
            check(health.get("status") == "ok", f"[health] after the run: {health}",
                  "server healthy after a refused save")
            browser.close()
    finally:
        _put_overlay_back(kept_overlay)

    return _report(errors)


# =====================================================================
# THE BOARD — globe, ladder, route cards, as-of, response workspace
# =====================================================================
def _open_board(page, query: str = "") -> None:
    page.goto(f"{BASE}/?view=globe{query}", wait_until="load", timeout=90_000)
    page.wait_for_function(
        "typeof state !== 'undefined' && state.board && state.board.routes.length > 0",
        timeout=90_000)


def _cards(page):
    return page.locator("#rlist .rli")


def _settle(page, script: str, timeout: int = 60_000) -> None:
    """Wait until *script* is true, and carry on if it never is: the check
    that follows reports the failure it is. A fixed pause stopped being
    enough once the board ran on a real recording — a save re-runs the whole
    pipeline, about 3.5 s over 5,500 items, and every check read the page
    one step behind: the save not yet reported, then the reset not yet."""
    try:
        page.wait_for_function(script, timeout=timeout)
    except Exception:  # noqa: BLE001 — reported by the check that follows
        pass


def _board(page, check) -> None:
    _open_board(page)
    # The globe needs a few frames to build its geometry and settle.
    page.wait_for_function("typeof state !== 'undefined' && !!state.globe", timeout=60_000)
    page.wait_for_timeout(5_000)

    # --- the globe actually drew something ---------------------------
    check(page.evaluate("document.querySelector('.globe-wrap').classList.contains('is-globe')"),
          "[globe] ?view=globe did not put the globe in the left pane",
          "left pane switched to the globe")
    canvas = page.locator("#globe canvas")
    if check(canvas.count() > 0, "[globe] no canvas — WebGL never initialised"):
        box = canvas.first.bounding_box()
        if check(bool(box) and box["width"] >= 100 and box["height"] >= 100,
                 f"[globe] canvas collapsed: {box}"):
            # NOT via gl.readPixels: without preserveDrawingBuffer the buffer
            # is cleared once the frame is presented, so readPixels reports
            # zero on a perfectly good render. Look at what the user sees.
            #
            # A CLIPPED PAGE screenshot, not an element screenshot. An element
            # screenshot first waits for the element to be "stable", and on a
            # continuously animating canvas that wait can never finish.
            shot = OUT / "_globe_probe.png"
            page.screenshot(path=str(shot), clip=box)
            drawn = _drawn_fraction(shot)
            check(drawn >= 0.05, f"[globe] canvas is essentially blank ({drawn:.1%} drawn)",
                  f"globe: rendering ({drawn:.0%} of the pane drawn)")

    # --- the ladder ---------------------------------------------------
    rungs = page.locator("#ladder .rung").count()
    check(rungs == 5, f"[ladder] {rungs} rungs, expected 5", "ladder: 5 rungs")

    # --- the route cards: present, counted, ranked --------------------
    cards = _cards(page)
    n = cards.count()
    total = page.evaluate("state.board.routes.length")
    if not check(n >= 2, f"[routes] only {n} route card(s) in the panel"):
        return
    check(page.locator("#panel-list").is_visible() and page.locator("#panel-body").is_hidden(),
          "[routes] the panel did not open on the list of routes")
    subtitle = page.locator("#panel-list-count").inner_text()
    check(subtitle.startswith(f"{n} of {total} routes"),
          f"[routes] the subtitle does not count the cards: {subtitle!r} with {n} cards",
          f"routes: {n} cards — {subtitle[:60]!r}")
    labels = [t.strip().title() for t in page.locator("#rlist .rli .level-chip").all_inner_texts()]
    ranks = [LEVEL_RANK.get(label, -1) for label in labels]
    check(-1 not in ranks and ranks == sorted(ranks, reverse=True),
          f"[routes] cards are not ranked by level: {labels}",
          "cards ranked by level, most urgent first")
    page.screenshot(path=str(OUT / "stage.png"))

    # --- the count is on the card, and Critical blinks -------------------
    counts = page.locator("#rlist .rli .rli-count").count()
    check(counts == n, f"[routes] {counts} of {n} cards show the shipments-at-risk count",
          "every card shows its a/b shipments count")
    red = page.locator("#rlist .rli.level-red .level-chip")
    if red.count():
        blink = red.first.evaluate("(e) => getComputedStyle(e, '::before').animationName")
        check(blink == "crit-pulse", f"[routes] the Critical dot does not blink ({blink!r})",
              "Critical cards carry a blinking dot")

    # --- no em dash between words in what the board says -----------------
    dashed = page.evaluate(r"""(() => {
        const text = document.body.innerText + '\n' +
          [...document.querySelectorAll('[title]')].map((e) => e.title).join('\n');
        return (text.match(/\S — \S.{0,40}/g) || []).slice(0, 3); })()""")
    check(not dashed, f"[text] em dashes still in the board text: {dashed}",
          "no em dash between words on the board")

    # --- clicking a card opens that route in the panel ----------------
    target = min(3, n - 1)
    _open_card(page, check, target, "first open")
    first = page.locator("#d-name").inner_text().strip()
    page.locator("#d-back").click()
    page.wait_for_timeout(300)
    check(page.locator("#panel-list").is_visible() and page.locator("#panel-body").is_hidden(),
          "[route] Back did not return to the list", "Back returns to the list")
    marked = page.locator("#rlist .rli.is-selected").count()
    check(marked == 1, f"[route] {marked} card(s) marked selected after Back, expected 1",
          "the opened route stays marked on the list")
    _open_card(page, check, 0, "second open")
    second = page.locator("#d-name").inner_text().strip()
    check(first != second, f"[route] a different card opened the same route: {first!r}",
          f"another card opens another route ({second[:44]!r})")
    page.wait_for_timeout(1_000)
    page.screenshot(path=str(OUT / "stage-selected.png"))

    # --- the as-of control re-runs the board ---------------------------
    # Folded into one button that shows the instant; it opens a small form.
    before = page.locator("#asof-text").inner_text()
    page.locator("#asof-label").click()
    page.locator("#asof-input").fill("2026-09-19T12:00")
    page.locator("#asof-apply").click()
    try:
        page.wait_for_function(
            "(b) => document.getElementById('asof-text').innerText !== b", arg=before,
            timeout=60_000)
    except Exception:  # noqa: BLE001 — reported below as the failure it is
        pass
    after = page.locator("#asof-text").inner_text() + " · " + page.locator("#brand-sub").inner_text()
    if check("could not load" not in after and "failed to load" not in after,
             f"[as-of] reload failed: {after}") and check(
                 before != after, "[as-of] applying a new instant did not change the board"):
        print(f"  ok   as-of: reloaded to {after[:34]!r}")
        check("as_of" in page.url, "[as-of] the URL was not updated, so it is not shareable",
              "as-of: the URL carries it, so the board can be shared")
        critical = page.locator("#rlist .rli .level-chip", has_text="Critical").count()
        print(f"  ok   as-of: {critical} Critical route(s) at the new instant")
    page.screenshot(path=str(OUT / "asof-switched.png"))

    _response(page, check)

    # --- recoverable must be gone from the UI --------------------------
    check("RECOVERABLE" not in page.inner_text("body").upper(),
          "[recoverable] still shown somewhere on the page", "no 'recoverable' figure anywhere")
    page.screenshot(path=str(OUT / "full.png"), full_page=True)


# =====================================================================
# THE DESK — the client review: sites, customers, all-hands, push-outs
# =====================================================================
def _desk(page, check) -> None:
    """Planners work by SITE and serve key accounts first (Sika review), so
    the board must narrow to both, everywhere at once, and remember it."""
    _open_board(page)
    sites = page.evaluate("state.board.sites.map(s => s.id)")
    check(len(sites) >= 3, f"[desk] only {len(sites)} site(s) offered", f"desk: {len(sites)} sites to pick from")
    busiest = page.evaluate("state.board.sites.slice().sort((a, b) => b.routes - a.routes)[0].id")
    page.select_option("#f-site", busiest)
    page.wait_for_timeout(700)
    got = page.evaluate("""(() => {
        const ids = [...document.querySelectorAll('#rlist .rli')].map(e => e.dataset.route);
        const of = (id) => state.board.routes.find(r => r.route_id === id).site.id;
        const rungs = [...document.querySelectorAll('#ladder .rung-count')].map(e => Number(e.textContent) || 0);
        return { n: ids.length, all: ids.every(id => of(id) === state.site),
                 ladder: rungs.reduce((a, b) => a + b, 0), mine: deskRoutes().length,
                 visible: visibleRoutes().length,
                 lanes: (MapAgent.getState().filters.lanes || []).length,
                 url: location.search };
    })()""")
    check(got["n"] > 0 and got["all"], f"[desk] site filter leaked other sites' routes: {got}",
          f"site {busiest}: {got['n']} routes, every one ships from it")
    check(got["ladder"] == got["mine"], f"[desk] ladder counts {got['ladder']} routes, the desk has {got['mine']}",
          "ladder counts only this site's routes")
    check(got["lanes"] == got["visible"], f"[desk] map shows {got['lanes']} lanes for {got['visible']} routes",
          "the map narrows to the same routes")
    check(f"site={busiest}" in got["url"], f"[desk] the site is not in the URL: {got['url']}",
          "the site is in the URL, so a planner's view is a link")

    page.click("#f-cust button[data-cust='A']")
    page.wait_for_timeout(600)
    key = page.evaluate("""(() => {
        const ids = [...document.querySelectorAll('#rlist .rli')].map(e => e.dataset.route);
        const r = (id) => state.board.routes.find(x => x.route_id === id);
        const assets = MapStore.select.visibleAssets(MapAgent.getState());
        return { ok: ids.every(id => r(id).customers.some(c => c.priority === 'A')),
                 assets: assets.every(a => a.customer_priority === 'A'), n: ids.length };
    })()""")
    check(key["ok"] and key["assets"], f"[desk] key-account filter let others through: {key}",
          f"key accounts only: {key['n']} route(s), and only key-account icons on the map")
    page.select_option("#f-site", "")
    page.click("#f-cust button[data-cust='']")
    page.wait_for_timeout(600)
    # The filters sit in one slim row inside the existing layout: the route
    # list must still own the column. (The first version stacked four boxes
    # above it and left no route on a laptop screen.)
    room = page.evaluate("""(() => { const r = document.getElementById('rlist').getBoundingClientRect();
        const panel = document.getElementById('panel').getBoundingClientRect();
        return Math.round(r.height / panel.height * 100); })()""")
    check(room >= 55, f"[layout] the route list has only {room}% of the column",
          f"the route list keeps {room}% of the column")
    # And on the screens the board is actually shown on: a Windows laptop at
    # 125% scaling, and a 1366 x 768 one. This check did not exist when a
    # layout that left no route visible there was shipped.
    kept = page.viewport_size
    for w, h in ((1536, 730), (1366, 640)):
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(500)
        seen = page.evaluate("""(() => [...document.querySelectorAll('#rlist .rli')].filter(e => {
            const r = e.getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight - 60; }).length)()""")
        links = page.evaluate("""(() => ['link-fast', 'link-profile'].every(id => {
            const r = document.getElementById(id).getBoundingClientRect();
            return r.width > 0 && r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; }))()""")
        check(seen >= 2 and links, f"[layout] at {w}x{h}: {seen} route card(s) begin on screen, "
              f"header links {'visible' if links else 'CUT OFF'}",
              f"at {w}x{h}: {seen} route cards on screen, Act fast and Risk profile visible")
    page.set_viewport_size(kept)
    page.wait_for_timeout(300)

    page.click(".ptab[data-ptab='allhands']")
    page.wait_for_timeout(400)
    rows = page.locator("#allhands .ah2-fn").count()
    text = page.locator("#allhands").inner_text()
    keys = page.evaluate("state.board.key_accounts.length")
    days = page.locator("#allhands .ah2-day").count()
    check(rows == 4 + (1 if keys else 0) and days == 14
          and page.locator("#allhands .ah2-tile").count() == 3
          and all(f in text for f in ("Supply Chain", "Procurement", "Manufacturing", "Controlling")),
          f"[all-hands] {rows} row(s), {days} day(s)",
          "all-hands: when (two-week strip), why (3 limit tiles), the room (4 functions)")
    # Each limit says its number and its limit in words, never "10 / 4",
    # which reads as "10 out of 4".
    tiles = page.locator("#allhands .ah2-tile").all_inner_texts()
    check(all("limit" in t and " / " not in t for t in tiles),
          f"[all-hands] a limit tile does not say its limit plainly: {tiles}",
          "limit tiles: today's number, 'limit N', and how far past it in words")
    if keys:
        page.locator("#allhands .ah-keys-card > summary").click()
        check(page.locator("#allhands .ah-keys-card li:visible").count() == keys,
              "[all-hands] key-account orders at risk not listed",
              f"all-hands lists the {keys} key-account order(s) at risk, first, behind one click")
    page.click(".ptab[data-ptab='signals']")
    _settle(page, "document.querySelector('#siglist .cs') !== null", 20_000)
    check("Bursts of small orders" in page.locator("#siglist").inner_text(),
          "[signals] no bursts-of-small-orders section", "signals: bursts of small orders, a week ahead")
    check(page.locator("#siglist .cs").count() == 2, "[signals] no carrier push-out section",
          "signals: carriers pushing out orders, raised and watched")
    page.click(".ptab[data-ptab='routes']")
    page.wait_for_timeout(300)


def _open_card(page, check, index: int, what: str) -> None:
    """Unfold the index-th card, open it with its arrow, and confirm the
    panel opened on THAT route."""
    card = _cards(page).nth(index)
    route_id = card.get_attribute("data-route")
    name = card.locator(".rli-name").inner_text().strip()
    card.scroll_into_view_if_needed()
    # A click on the card unfolds WHY in place; it must not leave the list.
    main = card.locator(".rli-main")
    if "is-open" in (card.get_attribute("class") or ""):
        main.click()
        page.wait_for_timeout(300)
    main.click()
    page.wait_for_timeout(450)
    reasons = card.locator(".why > li").count()
    check("is-open" in (card.get_attribute("class") or "") and reasons >= 2
          and main.get_attribute("aria-expanded") == "true"
          and page.locator("#panel-body").is_hidden(),
          f"[route] clicking the card for {name!r} did not unfold its reasons in place "
          f"({reasons} bullet(s))",
          f"click ({what}): the card unfolds {reasons} reasons in place")
    # The arrow opens the route itself.
    card.locator(".rli-open").click()
    try:
        page.wait_for_function(
            "(n) => !document.getElementById('panel-body').hidden"
            " && document.getElementById('d-name').textContent.trim() === n",
            arg=name, timeout=10_000)
    except Exception:  # noqa: BLE001 — reported below
        pass
    shown = page.locator("#d-name").inner_text().strip()
    if check(page.locator("#panel-body").is_visible() and shown == name,
             f"[route] clicking the card for {name!r} opened {shown!r}",
             f"click ({what}): the panel opened on {name[:44]!r}"):
        href = page.locator("#link-route").get_attribute("href") or ""
        check(href.startswith(f"/route/{route_id}?") and "as_of=" in href,
              f"[route] 'Open this route' points at {href[:80]!r}",
              "the route's own page is linked, with the as-of")


def _response(page, check) -> None:
    """The response workspace on a route that has options, if one does."""
    route_id = page.evaluate("""(() => {
        const open = state.board.routes.filter((r) => !state.levelOnly || r.level === state.levelOnly);
        const acting = open.find((r) => (r.actions || []).length);
        return (acting || open[0]).route_id; })()""")
    if page.locator("#panel-body").is_visible():
        page.locator("#d-back").click()
        page.wait_for_timeout(300)
    card = page.locator(f'#rlist .rli[data-route="{route_id}"]')
    card.scroll_into_view_if_needed()
    card.locator(".rli-open").click()
    page.wait_for_timeout(800)

    check(page.locator("#d-name").inner_text().strip() not in ("", "—"),
          "[response] the panel head never populated",
          f"response: {page.locator('#d-name').inner_text()[:40]!r}")
    acts = page.locator("#r-actions .act").count()
    empty = page.locator("#r-actions .response-empty").count()
    check(acts > 0 or empty > 0, "[response] the actions list rendered nothing at all",
          f"response: {acts} action card(s)" if acts else "response: 'nothing worth doing' said plainly")

    page.get_by_role("tab", name="Who to contact").click()
    page.wait_for_timeout(600)
    groups = page.locator("#r-contacts .cgroup").count()
    people = page.locator("#r-contacts .contact").count()
    check(groups >= 3 and people >= 3,
          f"[response] contacts thin: {groups} groups, {people} people",
          f"contacts: {groups} groups, {people} people")
    page.screenshot(path=str(OUT / "response-contacts.png"))

    page.get_by_role("tab", name="Act & escalate").click()
    try:
        page.wait_for_function(
            "!document.getElementById('send-body').value.startsWith('loading summary')",
            timeout=20_000)
    except Exception:  # noqa: BLE001 — reported below
        pass
    check(page.locator("#r-escalate .esc-card").count() > 0, "[response] escalation card missing",
          "escalation card shown beside the actions")
    body = page.locator("#send-body").input_value()
    check(not body.startswith("loading summary") and len(body) >= 120,
          f"[response] summary did not load: {body[:90]!r}",
          f"summary: {len(body)} chars composed")
    check("recoverable" not in body.lower(), "[response] summary still quotes a recoverable figure")
    # The summary must describe the SAME instant as the header above it: it
    # was once re-read from a URL that reload() rewrites after rendering, and
    # a Critical header sat above an Alert summary.
    shown = page.locator("#d-chip").inner_text().strip().upper()
    check(not shown or f"[{shown}]" in body.upper(),
          f"[response] summary/header disagree: chip says {shown}, "
          f"summary starts {body.splitlines()[0][:60] if body else ''!r}",
          f"summary and header agree on {shown}")
    mail = page.locator("#send-mail").get_attribute("href") or ""
    check(mail.startswith("mailto:"), f"[response] mailto not built: {mail[:60]!r}",
          "mailto draft built")
    pdf = page.locator("#send-pdf").get_attribute("href") or ""
    if check(".pdf" in pdf, f"[response] pdf link not built: {pdf[:60]!r}"):
        resp = page.request.get(f"{BASE}{pdf}")
        head = resp.body()[:4]
        check(resp.status == 200 and head == b"%PDF",
              f"[response] pdf endpoint bad: {resp.status}, {head!r}",
              f"pdf: {len(resp.body())} bytes, valid header")
    page.screenshot(path=str(OUT / "response-escalate.png"))

    # The route's delay clauses: one button, one line per customer inside,
    # and a header switch that says whether they are counted.
    page.locator("#contract-btn").click()
    lines = page.locator("#contract-card li:visible").count()
    check(page.locator("#contract-card").is_visible() and lines >= 1,
          f"[contract] the Contract card is empty or hidden ({lines} lines)",
          f"contract: {lines} customer clause(s) behind one button")
    page.locator("#contract-btn").click()
    check(page.locator("#contract-card").is_hidden(), "[contract] the card did not fold away")
    pressed = page.locator("#pen-chip").get_attribute("aria-pressed")
    check(pressed in ("true", "false") and page.locator("#pen-state").inner_text() in ("on", "off"),
          f"[penalties] the header switch has no state: {pressed!r}",
          f"penalties switch: {page.locator('#pen-state').inner_text()}")


# =====================================================================
# THE ROUTE PAGE — radars and matrix, reached the way a planner does
# =====================================================================
def _route_page(page, check) -> None:
    expected = page.locator("#d-name").inner_text().strip()
    page.locator("#link-route").click()
    page.wait_for_url("**/route/**", timeout=30_000)
    page.wait_for_function("document.getElementById('rt-name').textContent.trim().length > 0",
                           timeout=30_000)
    page.wait_for_timeout(1_200)
    name = page.locator("#rt-name").inner_text().strip()
    check(name == expected, f"[route page] opened {name!r}, the panel was on {expected!r}",
          f"route page: {name[:48]!r}")
    check("as_of=" in page.url, "[route page] the as-of did not travel with the link")

    view = page.evaluate("(() => { const s = new URLSearchParams(location.search);"
                         " return location.pathname + '|' + s.toString(); })()")
    path, query = view.split("|", 1)
    data = page.request.get(f"{BASE}/api{path}?{query}").json()
    for pane in ("measured", "reported"):
        axes = len((data.get(f"radar_{pane}") or {}).get("axes") or [])
        shapes = page.locator(
            f"#rt-radar-{pane} polygon, #rt-radar-{pane} line, #rt-radar-{pane} circle").count()
        if axes:
            check(shapes >= 5, f"[radar] {pane}: only {shapes} shapes for {axes} axes",
                  f"radar ({pane}): {shapes} shapes over {axes} axes")
        else:
            print(f"  ok   radar ({pane}): no family can reach this route, nothing to draw")
    cells = page.locator("#rt-matrix .mx-cell").count()
    check(cells > 0 or page.locator("#rt-matrix").inner_text().strip() != "",
          "[matrix] the route page drew no matrix and said nothing",
          f"matrix: {cells} cells drawn" if cells else "matrix: this route has no event with one")
    page.screenshot(path=str(OUT / "route.png"), full_page=True)


# =====================================================================
# THE REVIEW OF 28 SEPTEMBER: one place to act, the urgent option first,
# one meeting time, affected routes only, a ladder that shows one level
# =====================================================================
def _review(page, check) -> None:
    _open_board(page)
    # Normal routes need nothing: out of the list, counted, one click away.
    got = page.evaluate("""(() => {
        const cards = [...document.querySelectorAll('#rlist .rli')];
        return { green: cards.filter(c => c.classList.contains('level-green')).length,
                 cards: cards.length, normal: visibleRoutes().filter(r => r.level === 'green').length,
                 more: !!document.getElementById('f-normal') };
    })()""")
    check(got["green"] == 0 and (got["more"] or not got["normal"]),
          f"[list] normal routes in the affected list by default: {got}",
          f"list: {got['cards']} affected routes, {got['normal']} normal ones one click away")
    if got["more"]:
        page.click("#f-normal")
        page.wait_for_timeout(400)
        shown = page.locator("#rlist .rli.level-green").count()
        check(shown == got["normal"], f"[list] showing normal routes gave {shown} of {got['normal']}",
              "the normal routes come back on one click")
        page.click("#f-normal")
        page.wait_for_timeout(300)

    # A ladder click shows only that level, and a second click shows all.
    page.locator('#ladder .rung[data-level="red"]').click()
    page.wait_for_timeout(500)
    only = page.evaluate("""(() => ({
        levels: [...new Set([...document.querySelectorAll('#rlist .rli')].map(c => [...c.classList].find(k => k.startsWith('level-'))))],
        chip: !document.getElementById('f-all').hidden,
        lanes: (MapAgent.getState().filters.lanes || []).length,
        red: deskRoutes().filter(r => r.level === 'red').length }))()""")
    check(only["levels"] in ([], ["level-red"]) and only["chip"] and only["lanes"] == only["red"],
          f"[ladder] clicking Critical did not show only Critical: {only}",
          f"ladder: Critical alone on the list and the map ({only['red']} routes), with a clear chip")
    page.click("#f-all")
    page.wait_for_timeout(400)
    check(page.evaluate("state.levelOnly === null && document.getElementById('f-all').hidden"),
          "[ladder] the clear chip did not bring every level back", "the chip clears the level filter")

    # The all-hands is in the header, and it opens its tab.
    chip = page.locator("#meet-chip")
    if check(chip.is_visible(), "[all-hands] no meeting chip in the header"):
        text = chip.inner_text()
        chip.click()
        page.wait_for_timeout(500)
        check(page.locator("#allhands").is_visible(), "[all-hands] the header chip did not open the tab",
              f"header chip: {text.strip()[:60]!r} opens the All-hands tab")
    # One meeting time everywhere: the rule's sentence names the same slot.
    same = page.evaluate("""(() => {
        const h = state.board.all_hands, words = state.board.posture.headline || '';
        const day = h.next_label.split(',')[0];
        return { posture: h.posture, day, ok: h.posture !== 'convene' || words.includes(day) };
    })()""")
    check(same["ok"], f"[all-hands] the headline and the tab disagree on the next meeting: {same}",
          f"one meeting time everywhere ({same['day']})")
    page.click(".ptab[data-ptab='routes']")
    page.wait_for_timeout(300)

    # The route panel: the option that closes first is listed first, and the
    # two ways onward are named the same as everywhere else.
    rid = page.evaluate("""(() => (state.board.routes.find(r => r.level === 'red' && r.actions.length > 1)
        || state.board.routes.find(r => r.actions.length) || {}).route_id)()""")
    if rid:
        page.evaluate("(id) => select(id, { fly: false })", rid)
        # The panel fetches its summary; let it land before navigating away.
        page.wait_for_function("!document.getElementById('send-body').value.startsWith('loading')",
                               timeout=30_000)
        page.wait_for_timeout(300)
        order = page.evaluate("""(id) => {
            const r = state.board.routes.find(x => x.route_id === id);
            const lead = r.actions.map(a => a.lead_time_hours == null ? 1e9 : a.lead_time_hours);
            return { first: lead[0], min: Math.min(...lead),
                     tag: document.querySelector('#r-actions .act .act-when').innerText,
                     act: document.getElementById('link-act').getAttribute('href'),
                     ops: document.getElementById('link-ops').getAttribute('href') };
        }""", rid)
        check(order["first"] <= 6 or order["first"] == order["min"] or order["min"] > 6,
              f"[options] the soonest option is not first: {order}",
              f"options: the one closing in {round(order['first'])} h is first ({order['tag'][:28]!r})")
        check(order["act"].startswith(f"/fast/{rid}") and order["ops"].startswith("/ops?route="),
              f"[options] the onward links are wrong: {order}",
              "Act fast on this route and Step by step are linked from the panel")
        # The way back from those pages lands on this route.
        page.goto(f"{BASE}/?route={rid}", wait_until="load", timeout=90_000)
        page.wait_for_function("typeof state !== 'undefined' && state.board && state.selected", timeout=60_000)
        check(page.evaluate("state.selected") == rid and page.locator("#panel-body").is_visible(),
              "[links] /?route= did not open the route", "?route= opens the board on that route")

    # The as-of and the theme are small menus, and nothing sits over the list.
    page.locator("#asof-label").click()
    check(page.locator("#asof-input").is_visible(), "[header] the as-of did not open",
          "as-of folds into one button that opens its form")
    page.mouse.click(5, 5)
    page.locator(".theme-toggle").click()
    check(page.locator(".theme-list .theme-btn").count() == 3, "[header] the theme menu is not three themes",
          "theme: one button, three themes")
    page.mouse.click(5, 5)

    # No em dash between words, and one number format.
    text = page.evaluate("document.body.innerText")
    grouped = re.findall(r"\d[’']\d{3}", text)
    check(not grouped, f"[format] a number still uses an apostrophe: {grouped[:3]}",
          "one number format (CHF 204,523)")

    # The phone: no sideways scrolling.
    kept = page.viewport_size
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(700)
    wide = page.evaluate("document.documentElement.scrollWidth")
    check(wide <= 392, f"[phone] the page is {wide}px wide on a 390px phone", "phone: nothing wider than the screen")
    page.set_viewport_size(kept)
    page.wait_for_timeout(300)


# =====================================================================
# RISK PROFILE
# =====================================================================
# The one page in the app where a UI action writes into the engine's own
# configuration, so it is worth driving rather than eyeballing.
def _profile(page, check, expecting_refusal) -> None:
    _open_board(page)
    page.locator("#link-profile").click()
    page.wait_for_url("**/profile*", timeout=30_000)
    page.wait_for_timeout(2_500)
    sub = page.locator("#brand-sub").inner_text().strip()
    check("loading" not in sub.lower() and "could not" not in sub.lower(),
          f"[profile] never loaded: {sub!r}", "profile: loaded")

    for name in ("desk", "network", "ledger", "appetite", "response", "sources"):
        page.locator(f'.prail[data-tab="{name}"]').click()
        page.wait_for_timeout(400)
        text = page.locator(f"#tab-{name}").inner_text().strip()
        check(len(text) >= 120, f"[profile] {name} tab rendered empty ({len(text)} chars)")
        page.screenshot(path=str(OUT / f"profile-{name}.png"))

    # Every one of the 45 variables has to be listed, not a sample.
    page.locator('.prail[data-tab="ledger"]').click()
    page.wait_for_timeout(300)
    page.evaluate("document.querySelectorAll('.fam').forEach(d => d.open = true)")
    page.wait_for_timeout(400)
    listed = page.locator("#tab-ledger .rtable tbody tr").count()
    check(listed == 46, f"[profile] ledger lists {listed} variables, expected 46",
          f"ledger: {listed} variables across {page.locator('#tab-ledger .fam').count()} families")
    page.screenshot(path=str(OUT / "profile-ledger.png"), full_page=True)

    # --- the save bar only appears once something is dirty -----------
    page.locator('.prail[data-tab="appetite"]').click()
    page.wait_for_timeout(400)
    check(not page.locator("#savebar").is_visible(), "[profile] save bar showing before any edit")
    red = page.locator('input[data-path="alert_levels.red_hours"]')
    red.fill("4")
    red.dispatch_event("input")
    page.wait_for_timeout(300)
    check(page.locator("#savebar").is_visible(), "[profile] save bar did not appear after an edit",
          "save bar appears once something is edited")
    page.screenshot(path=str(OUT / "profile-appetite.png"))

    # --- an ordering that breaks the ladder must be REFUSED ----------
    # Critical later than Alert makes the Alert rung unreachable. The failure
    # this guards is silent, so the refusal has to be visible.
    expecting_refusal[0] = True
    red.fill("999")
    red.dispatch_event("input")
    page.wait_for_timeout(200)
    page.locator("#btn-save").click()
    page.wait_for_timeout(1_200)
    expecting_refusal[0] = False
    flash = page.locator("#savebar-text").inner_text()
    check("Nothing saved" in flash, f"[profile] a broken ladder was accepted: {flash[:120]!r}",
          f"refused: {flash[:80]}")
    page.screenshot(path=str(OUT / "profile-refused.png"))

    # --- a valid save round-trips, and Reset puts it back ------------
    red.fill("4")
    red.dispatch_event("input")
    agreed = page.locator('input[data-path="convene_meta.agreed_by"]')
    agreed.fill("S&OP meeting")
    agreed.dispatch_event("input")
    page.wait_for_timeout(200)
    page.locator("#btn-save").click()
    _settle(page, "document.getElementById('overlay-flag').innerText.includes('config/')"
                  " && document.getElementById('btn-save').disabled")
    saved = page.locator('input[data-path="alert_levels.red_hours"]').input_value()
    check(saved == "4", f"[profile] saved value did not come back: {saved!r}")
    flag = page.locator("#overlay-flag").inner_text()
    check("config/" in flag, f"[profile] overlay not reported after save: {flag!r}",
          f"saved: overlay now reads {flag.strip()[:60]!r}")
    # Reset lives on the save bar. Hiding the bar once the edits are saved
    # would leave no way back to the committed stand-in short of a dummy edit.
    check(page.locator("#savebar").is_visible(), "[profile] save bar hidden while an overlay is in force")
    check(page.locator("#btn-save").is_disabled(), "[profile] Save still enabled with nothing to save")
    page.screenshot(path=str(OUT / "profile-saved.png"))

    page.locator("#btn-reset-profile").click()
    _settle(page, "document.getElementById('overlay-flag').innerText.includes('stand-in')")
    restored = page.locator('input[data-path="alert_levels.red_hours"]').input_value()
    check(restored != "4", "[profile] reset did not restore the committed stand-in",
          f"reset: red_hours back to {restored!r}")
    after = page.locator("#overlay-flag").inner_text()
    check("stand-in" in after, f"[profile] overlay still reported after reset: {after!r}")


def _put_overlay_back(kept: bytes | None) -> None:
    if kept is None:
        return
    OVERLAY.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY.write_bytes(kept)
    print(f"  note {OVERLAY.relative_to(ROOT)} put back as it was")


# =====================================================================
# THEMES AND THE UNSOURCED BAND — on a route page that draws one
# =====================================================================
def _themes_and_unsourced(page, check) -> None:
    """The matrix lives on the route page now, so that is where it has to
    survive every theme — on a route whose driving event has no published
    odds, which is also the route that has to show the unsourced band."""
    board = page.request.get(f"{BASE}/api/board").json()
    chosen, any_matrix = None, None
    for route in board["routes"]:
        view = page.request.get(f"{BASE}/api/route/{route['route_id']}").json()
        events = view.get("events") or []
        driving = next((e for e in events if e["event_id"] == view.get("driving_event_id")),
                       events[0] if events else None)
        if driving and driving.get("matrix") and any_matrix is None:
            any_matrix = route["route_id"]
        if driving and (driving.get("matrix") or {}).get("unsourced"):
            chosen = route["route_id"]
            break

    if chosen is not None:
        page.goto(f"{BASE}/route/{chosen}", wait_until="load", timeout=90_000)
        page.wait_for_function("document.querySelectorAll('#rt-matrix .mx-cell').length > 0",
                               timeout=30_000)
        check(page.locator("#rt-matrix .mx-unsourced-h").count() > 0,
              "[matrix] the unsourced band was not drawn for an event with no odds",
              f"matrix: unsourced band drawn outside the probability axis ({chosen})")
    else:
        # Only a warning sign lacks odds now: an event that has happened has
        # odds of one, and a forecast carries its forecaster's confidence. On
        # a board where no warning drives a route there is no band to draw —
        # which is the board being right, not the check passing. Said, not
        # failed, and the themes are still checked below.
        print("  note no route's driving event lacks odds on this board, so the "
              "unsourced band is not drawn anywhere to check")
        chosen = any_matrix
        if not check(chosen is not None, "[matrix] no route page draws a matrix at all"):
            return
        page.goto(f"{BASE}/route/{chosen}", wait_until="load", timeout=90_000)
        page.wait_for_function("document.querySelectorAll('#rt-matrix .mx-cell').length > 0",
                               timeout=30_000)

    for theme in ("light", "sika", "dark"):
        page.locator(".theme-toggle").click()
        page.locator(f'.theme-btn[data-theme="{theme}"]').click()
        page.wait_for_timeout(900)
        check(page.evaluate("document.documentElement.getAttribute('data-theme')") == theme,
              f"[theme] {theme} did not apply")
        # The stylesheet's own rule: a level colour means "how soon must
        # someone decide" and nothing else. An accent equal to a rung means a
        # planner seeing that colour on a button and on a route cannot tell
        # which of them carried meaning.
        tokens = page.evaluate("""() => {
            const cs = getComputedStyle(document.documentElement);
            const t = (n) => cs.getPropertyValue(n).trim().toLowerCase();
            return { accent: t('--accent'),
                     rungs: ['green','white','blue','yellow','red'].map((l) => t('--lvl-' + l)) };
        }""")
        check(tokens["accent"] not in tokens["rungs"],
              f"[theme:{theme}] --accent {tokens['accent']} collides with a ladder colour")
        # The matrix has to survive a repaint in every theme.
        check(page.locator("#rt-matrix .mx-cell.has").count() > 0,
              f"[matrix] no occupied cell after switching to {theme}")
        page.screenshot(path=str(OUT / f"theme-{theme}.png"), full_page=True)
    print("  ok   themes: 3 applied from one menu, accent distinct from the ladder, matrix intact in each")
    page.locator(".theme-toggle").click()
    page.locator('.theme-btn[data-theme="light"]').click()


# =====================================================================
# THE ASSISTANT
# =====================================================================
def _assistant(page, check) -> None:
    """With no model reachable the assistant must EXPLAIN, not fail."""
    _open_board(page)
    page.locator("#btn-ask").click()
    page.wait_for_timeout(500)
    page.locator("#ask-input").fill("which route needs a decision first?")
    page.locator("#ask-send").click()
    page.wait_for_timeout(2_500)
    answer = page.locator("#ask-log").inner_text()
    status = page.request.get(f"{BASE}/api/model").json()
    if status["status"] == "connected":
        check("generated by" in answer.lower(),
              "[ask] a model answered but the output was not marked generated",
              f"ask: answered by {status['model']}, marked as generated")
    else:
        check("No model is connected" in answer,
              f"[ask] no-model socket message missing: {answer[:100]!r}")
        check("unaffected" in answer, "[ask] did not say the board is computed without a model",
              "ask: no model, socket message shown, board unaffected")
    page.screenshot(path=str(OUT / "assistant.png"))
    page.locator("#ask-close").click()


# =====================================================================
def _report(errors: list[str]) -> int:
    if errors:
        # Tell a dead server apart from a broken page BEFORE printing a list
        # that blames the UI.
        #
        # A run was lost to this: dev_serve.sh kills every uvicorn before it
        # restarts, so restarting the server for an unrelated test mid-check
        # produced four errors reading "summary did not load" and "summary/
        # header disagree" — which is precisely what a genuine rendering bug
        # looks like. The page was fine. The socket was gone.
        #
        # A checker that misattributes its own environment failure to the
        # code under test is worse than one that simply crashes, because
        # somebody will spend an afternoon fixing a bug that was never there.
        refused = [e for e in errors if "ERR_CONNECTION_REFUSED" in e or "Failed to fetch" in e]
        if refused and not _server_alive():
            print("\nTHE SERVER WENT AWAY MID-RUN — these are not UI errors.")
            print(f"  {BASE} stopped answering mid-run.")
            print("  Most likely something restarted it (dev_serve.sh kills every")
            print("  uvicorn before it starts one). Re-run with nothing else")
            print("  touching the server.")
            print(f"\n  {len(refused)} of {len(set(errors))} error(s) were connection failures.")
            return 2

        print("\nERRORS")
        for e in dict.fromkeys(errors):
            print("  ", e)
        return 1

    print(f"\nclean — screenshots in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
