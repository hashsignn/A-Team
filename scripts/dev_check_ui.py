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

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page.on("pageerror", lambda e: errors.append(f"[js] {e}"))
        page.on(
            "requestfailed",
            lambda r: errors.append(f"[net] {r.url} — {r.failure}"),
        )
        page.on(
            "console",
            lambda m: errors.append(f"[console] {m.text}") if m.type == "error" else None,
        )

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
            shot = OUT / "_globe_probe.png"
            canvas.first.screenshot(path=str(shot))
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
        page.wait_for_timeout(1_200)
        page.screenshot(path=str(OUT / "ranked.png"))

        # --- full page, for a look at the whole thing ----------------
        page.screenshot(path=str(OUT / "full.png"), full_page=True)

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
