"""Load the UI in a headless browser and fail on anything broken.

HTTP 200 proves the server is up and nothing else. A WebGL globe can fail to
initialise, a radar can render as an empty <svg>, and a JS exception leaves the
page half-built — all while every request returns 200. So the page has to be
looked at.

Checks: JS errors, failed requests, that the globe canvas actually has pixels,
that the radar drew geometry, that the table has rows, and that clicking a
route updates the panel.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8600
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/shots")
OUT.mkdir(parents=True, exist_ok=True)


def _lit_pixels(path: Path, threshold: int = 42) -> float:
    """Fraction of pixels brighter than the near-black page background."""
    img = Image.open(path).convert("RGB")
    px = list(img.getdata())
    lit = sum(1 for r, g, b in px if r + g + b > threshold)
    return lit / max(1, len(px))


def main() -> int:
    errors: list[str] = []

    # One check deliberately provokes a 422 — it asserts that a ladder with
    # Critical later than Alert is REFUSED. The browser logs every non-2xx as
    # a console error, so that expected refusal would fail the run it proves.
    # Set while the refusal is being driven, and only then.
    expecting_refusal = [False]

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("pageerror", lambda e: errors.append(f"[js] {e}"))
        page.on(
            "requestfailed",
            lambda r: errors.append(f"[net] {r.url} — {r.failure}"),
        )

        def _console(message) -> None:
            if message.type != "error":
                return
            if expecting_refusal[0] and "422" in message.text:
                return
            errors.append(f"[console] {message.text}")

        page.on("console", _console)

        page.goto(f"http://localhost:{PORT}/", wait_until="load", timeout=90_000)
        # The globe needs a few frames to build geometry and settle.
        page.wait_for_timeout(9_000)

        # --- the globe actually drew something -----------------------
        canvas = page.locator("#globe canvas")
        if canvas.count() == 0:
            errors.append("[globe] no canvas — WebGL never initialised")
        else:
            box = canvas.first.bounding_box()
            if not box or box["width"] < 100 or box["height"] < 100:
                errors.append(f"[globe] canvas collapsed: {box}")
            # A black canvas means it initialised but rendered nothing.
            # NOT via gl.readPixels: without preserveDrawingBuffer the buffer
            # is cleared once the frame is presented, so readPixels reports
            # zero on a perfectly good render. Screenshot the element instead
            # and look at what the user actually sees.
            #
            # A CLIPPED PAGE screenshot, not an element screenshot. An element
            # screenshot first waits for the element to be "stable", and on a
            # continuously animating canvas that wait can simply never finish —
            # it times out after 20s and fails a check that has nothing wrong
            # with it. Clipping to the same box captures exactly the same
            # pixels and skips the wait entirely.
            shot = OUT / "_globe_probe.png"
            page.screenshot(path=str(shot), clip=box)
            lit = _lit_pixels(shot)
            if lit < 0.02:
                errors.append(
                    f"[globe] canvas is essentially black ({lit:.3%} lit pixels)"
                )
            else:
                print(f"  globe: rendering ({lit:.1%} of pixels lit)")

        # --- the panel populated -------------------------------------
        if page.locator("#panel-body").is_hidden():
            errors.append("[panel] never opened — no route auto-selected")
        else:
            name = page.locator("#d-name").inner_text()
            print(f"  panel: opened on '{name[:52]}'")

        # --- the radar drew geometry ---------------------------------
        shapes = page.locator("#radar polygon, #radar line, #radar circle").count()
        if shapes < 5:
            errors.append(f"[radar] only {shapes} shapes — chart did not draw")
        else:
            print(f"  radar: {shapes} shapes")

        # --- the table has rows --------------------------------------
        rows = page.locator("#rtable-body tr").count()
        if rows < 2:
            errors.append(f"[table] only {rows} rows")
        else:
            print(f"  table: {rows} rows")

        # --- ladder rendered -----------------------------------------
        rungs = page.locator("#ladder .rung").count()
        if rungs != 5:
            errors.append(f"[ladder] {rungs} rungs, expected 5")
        else:
            print("  ladder: 5 rungs")

        page.screenshot(path=str(OUT / "stage.png"))

        # --- clicking a table row changes the panel ------------------
        before = page.locator("#d-name").inner_text()
        target = page.locator("#rtable-body tr").nth(min(3, rows - 1))
        target.scroll_into_view_if_needed()
        target.click()
        page.wait_for_timeout(2_500)
        after = page.locator("#d-name").inner_text()
        if before == after:
            errors.append("[interaction] clicking a table row did not change the panel")
        else:
            print(f"  click: panel switched to '{after[:52]}'")

        page.wait_for_timeout(1_500)
        page.screenshot(path=str(OUT / "stage-selected.png"))

        # --- the as-of control re-runs the board ---------------------
        before_sub = page.locator("#brand-sub").inner_text()
        page.locator("#asof-input").fill("2026-09-19T12:00")
        page.locator("#asof-apply").click()
        page.wait_for_timeout(9_000)
        after_sub = page.locator("#brand-sub").inner_text()
        if "could not load" in after_sub:
            errors.append(f"[as-of] reload failed: {after_sub}")
        elif before_sub == after_sub:
            errors.append("[as-of] applying a new instant did not change the board")
        else:
            print(f"  as-of: reloaded to '{after_sub[:34]}'")
            if "as_of" not in page.url:
                errors.append("[as-of] the URL was not updated, so it is not shareable")
            reds = page.locator("#rtable-body tr .level-chip", has_text="Critical").count()
            print(f"  as-of: {reds} Critical route(s) at the new instant")
        page.screenshot(path=str(OUT / "asof-switched.png"))

        page.locator("#ranked").scroll_into_view_if_needed()
        page.wait_for_timeout(1_500)

        # --- the response workspace ----------------------------------
        if page.locator("#r-name").inner_text().strip() in ("", "—"):
            errors.append("[response] pane never populated")
        else:
            print(f"  response: '{page.locator('#r-name').inner_text()[:40]}'")

        acts = page.locator("#r-actions .act").count()
        empty = page.locator("#r-actions .response-empty").count()
        if acts == 0 and empty == 0:
            errors.append("[response] actions tab rendered nothing at all")
        else:
            print(f"  response: {acts} action card(s)")

        page.get_by_role("tab", name="Who to contact").click()
        page.wait_for_timeout(900)
        groups = page.locator("#r-contacts .cgroup").count()
        people = page.locator("#r-contacts .contact").count()
        if groups < 3 or people < 3:
            errors.append(f"[response] contacts thin: {groups} groups, {people} people")
        else:
            print(f"  contacts: {groups} groups, {people} people")
        page.screenshot(path=str(OUT / "response-contacts.png"))

        page.get_by_role("tab", name="Escalate & report").click()
        page.wait_for_timeout(2_500)
        if page.locator("#r-escalate .esc-card").count() == 0:
            errors.append("[response] escalation card missing")
        body = page.locator("#send-body").input_value()
        if body.startswith("loading summary") or len(body) < 120:
            errors.append(f"[response] summary did not load: {body[:90]!r}")
        else:
            print(f"  summary: {len(body)} chars composed")
        if "recoverable" in body.lower():
            errors.append("[response] summary still quotes a recoverable figure")
        # The summary must describe the SAME instant as the header above it.
        # primeCompose used to re-read the URL, which reload() rewrites after
        # rendering — so a Critical header sat above an Alert summary.
        shown_level = page.locator("#r-chip").inner_text().strip().upper()
        if shown_level and f"[{shown_level}]" not in body.upper():
            errors.append(
                f"[response] summary/header disagree: chip says {shown_level}, "
                f"summary starts {body.splitlines()[0][:60]!r}"
            )
        mail = page.locator("#send-mail").get_attribute("href") or ""
        if not mail.startswith("mailto:"):
            errors.append(f"[response] mailto not built: {mail[:60]!r}")
        pdf_href = page.locator("#send-pdf").get_attribute("href") or ""
        if ".pdf" not in pdf_href:
            errors.append(f"[response] pdf link not built: {pdf_href[:60]!r}")
        else:
            resp = page.request.get(f"http://localhost:{PORT}{pdf_href}")
            body_bytes = resp.body()
            if resp.status != 200 or body_bytes[:4] != b"%PDF":
                errors.append(
                    f"[response] pdf endpoint bad: {resp.status}, {body_bytes[:12]!r}"
                )
            else:
                print(f"  pdf: {len(body_bytes)} bytes, valid header")
        page.screenshot(path=str(OUT / "response-escalate.png"))

        page.get_by_role("tab", name="Actions").click()
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "ranked.png"))

        # --- recoverable must be gone from the UI --------------------
        page_text = page.inner_text("body")
        if "RECOVERABLE" in page_text.upper():
            errors.append("[recoverable] still shown somewhere on the page")

        # --- full page, for a look at the whole thing ----------------
        page.screenshot(path=str(OUT / "full.png"), full_page=True)

        # =============================================================
        # RISK PROFILE
        # =============================================================
        # The one page in the app where a UI action writes into the engine's
        # own configuration, so it is worth driving rather than eyeballing.
        page.locator("#link-profile").click()
        page.wait_for_url("**/profile*", timeout=30_000)
        page.wait_for_timeout(2_500)

        sub = page.locator("#brand-sub").inner_text().strip()
        if "loading" in sub.lower() or "could not" in sub.lower():
            errors.append(f"[profile] never loaded: {sub!r}")

        tabs = ["desk", "network", "ledger", "appetite", "response", "sources"]
        for name in tabs:
            page.locator(f'.prail[data-tab="{name}"]').click()
            page.wait_for_timeout(400)
            text = page.locator(f"#tab-{name}").inner_text().strip()
            if len(text) < 120:
                errors.append(f"[profile] {name} tab rendered empty ({len(text)} chars)")
            page.screenshot(path=str(OUT / f"profile-{name}.png"))

        # Every one of the 45 variables has to be listed, not a sample.
        page.locator('.prail[data-tab="ledger"]').click()
        page.wait_for_timeout(300)
        page.evaluate("document.querySelectorAll('.fam').forEach(d => d.open = true)")
        page.wait_for_timeout(400)
        listed = page.locator("#tab-ledger .rtable tbody tr").count()
        if listed != 45:
            errors.append(f"[profile] ledger lists {listed} variables, expected 45")
        else:
            print(f"  ledger: {listed} variables across "
                  f"{page.locator('#tab-ledger .fam').count()} families")
        page.screenshot(path=str(OUT / "profile-ledger.png"), full_page=True)

        # --- the save bar only appears once something is dirty -------
        page.locator('.prail[data-tab="appetite"]').click()
        page.wait_for_timeout(400)
        if page.locator("#savebar").is_visible():
            errors.append("[profile] save bar showing before any edit")

        red = page.locator('input[data-path="alert_levels.red_hours"]')
        red.fill("4")
        red.dispatch_event("input")
        page.wait_for_timeout(300)
        if not page.locator("#savebar").is_visible():
            errors.append("[profile] save bar did not appear after an edit")
        page.screenshot(path=str(OUT / "profile-appetite.png"))

        # --- an ordering that breaks the ladder must be REFUSED ------
        # Critical later than Alert makes the Alert rung unreachable. The
        # failure this guards is silent, so the refusal has to be visible.
        expecting_refusal[0] = True
        red.fill("999")
        red.dispatch_event("input")
        page.wait_for_timeout(200)
        page.locator("#btn-save").click()
        page.wait_for_timeout(1_200)
        expecting_refusal[0] = False
        flash = page.locator("#savebar-text").inner_text()
        if "Nothing saved" not in flash:
            errors.append(f"[profile] a broken ladder was accepted: {flash[:120]!r}")
        else:
            print(f"  refused: {flash[:96]}")
        page.screenshot(path=str(OUT / "profile-refused.png"))

        # --- a valid save round-trips, and Reset puts it back --------
        red.fill("4")
        red.dispatch_event("input")
        agreed = page.locator('input[data-path="convene_meta.agreed_by"]')
        agreed.fill("S&OP meeting")
        agreed.dispatch_event("input")
        page.wait_for_timeout(200)
        page.locator("#btn-save").click()
        page.wait_for_timeout(2_500)
        saved = page.locator('input[data-path="alert_levels.red_hours"]').input_value()
        if saved != "4":
            errors.append(f"[profile] saved value did not come back: {saved!r}")
        flag = page.locator("#overlay-flag").inner_text()
        if "config/" not in flag:
            errors.append(f"[profile] overlay not reported after save: {flag!r}")
        else:
            print(f"  saved: overlay now reads {flag.strip()!r}")
        # Reset lives on the save bar. Hiding the bar once the edits are saved
        # would leave no route back to the committed stand-in short of making a
        # dummy edit first, so the bar must survive a successful save.
        if not page.locator("#savebar").is_visible():
            errors.append("[profile] save bar hidden while an overlay is in force")
        if not page.locator("#btn-save").is_disabled():
            errors.append("[profile] Save still enabled with nothing to save")
        page.screenshot(path=str(OUT / "profile-saved.png"))

        page.locator("#btn-reset-profile").click()
        page.wait_for_timeout(2_500)
        restored = page.locator('input[data-path="alert_levels.red_hours"]').input_value()
        if restored == "4":
            errors.append("[profile] reset did not restore the committed stand-in")
        else:
            print(f"  reset: red_hours back to {restored!r}")
        after = page.locator("#overlay-flag").inner_text()
        if "stand-in" not in after:
            errors.append(f"[profile] overlay still reported after reset: {after!r}")

        # =============================================================
        # THEMES, MATRIX, ASSISTANT
        # =============================================================
        page.goto(f"http://localhost:{PORT}/", wait_until="load", timeout=90_000)
        page.wait_for_timeout(8_000)

        for theme in ("light", "blue", "sika", "dark"):
            page.locator(f'.theme-btn[data-theme="{theme}"]').click()
            page.wait_for_timeout(1_200)
            if page.evaluate("document.documentElement.getAttribute('data-theme')") != theme:
                errors.append(f"[theme] {theme} did not apply")

            # The stylesheet's own rule: a level colour means "how soon must
            # someone decide" and nothing else. An accent equal to a rung
            # means a planner seeing that colour on a button and on a route
            # cannot tell which of them carried meaning.
            vals = page.evaluate("""() => {
                const cs = getComputedStyle(document.documentElement);
                const t = (n) => cs.getPropertyValue(n).trim().toLowerCase();
                return {
                  accent: t('--accent'),
                  rungs: ['green','white','blue','yellow','red'].map((l) => t('--lvl-'+l)),
                };
            }""")
            if vals["accent"] in vals["rungs"]:
                errors.append(
                    f"[theme:{theme}] --accent {vals['accent']} collides with "
                    "a reserved ladder colour"
                )

            # The matrix has to survive a repaint in every theme.
            page.locator("[data-matrix]").first.click()
            page.wait_for_timeout(600)
            if page.locator(".mx-cell.has").count() == 0:
                errors.append(f"[matrix] no occupied cell in {theme}")
            page.screenshot(path=str(OUT / f"theme-{theme}.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        print("  themes: 4 applied, accent distinct from the ladder in each")

        # The unsourced band must sit OUTSIDE the probability axis.
        board = page.evaluate("JSON.stringify(state.board)")
        import json as _json
        data = _json.loads(board)
        unsourced = [
            (r["route_id"], e) for r in data["routes"]
            for e in r.get("events", []) if e["matrix"]["unsourced"]
        ]
        if not unsourced:
            errors.append("[matrix] fixture has no unsourced-probability event to check")
        else:
            rid, ev = unsourced[0]
            page.evaluate(f"select({_json.dumps(rid)}, {{fly:false}})")
            page.wait_for_timeout(600)
            page.locator(f'[data-matrix="{ev["event_id"]}"]').click()
            page.wait_for_timeout(600)
            if page.locator(".mx-unsourced-h").count() == 0:
                errors.append("[matrix] unsourced band not drawn for an unsourced event")
            else:
                print(f"  matrix: unsourced band shown ({len(unsourced)} such events)")
            page.keyboard.press("Escape")

        # With no model reachable the assistant must EXPLAIN, not fail.
        page.locator("#btn-ask").click()
        page.wait_for_timeout(500)
        page.locator("#ask-input").fill("which route needs a decision first?")
        page.locator("#ask-send").click()
        page.wait_for_timeout(2_500)
        answer = page.locator("#ask-log").inner_text()
        status = page.request.get(f"http://localhost:{PORT}/api/model").json()
        if status["status"] == "connected":
            if "generated by" not in answer.lower():
                errors.append("[ask] a model answered but the output was not marked generated")
            else:
                print(f"  ask: answered by {status['model']}, marked as generated")
        else:
            if "No model is connected" not in answer:
                errors.append(f"[ask] no-model socket message missing: {answer[:100]!r}")
            elif "unaffected" not in answer:
                errors.append("[ask] did not say the board is computed without a model")
            else:
                print("  ask: no model, socket message shown, board unaffected")
        page.screenshot(path=str(OUT / "assistant.png"))
        page.locator("#ask-close").click()

        health = page.request.get(f"http://localhost:{PORT}/api/health").json()
        if health.get("status") != "ok":
            errors.append(f"[profile] health after refused save: {health}")

        browser.close()

    if errors:
        print("\nERRORS")
        for e in dict.fromkeys(errors):
            print("  ", e)
        return 1

    print(f"\nclean — screenshots in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
