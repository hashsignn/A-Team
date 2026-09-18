"""Load the dashboard in a headless browser and fail on any rendered error.

Streamlit returns HTTP 200 even when the script raised — the traceback is
rendered into the page. So "the server is up" proves nothing; the page has to
be looked at. This drives each tab, captures screenshots, and exits non-zero if
any error surface appears.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8512
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/shots")
OUT.mkdir(parents=True, exist_ok=True)

ERROR_MARKERS = (
    "Traceback",
    "StreamlitAPIException",
    "StreamlitDuplicateElementKey",
    "KeyError",
    "AttributeError",
    "TypeError:",
    "ValueError:",
    "IndexError",
)
TABS = ("Board", "Option decay", "Rhine (anchor)", "Inputs")


def scan(text: str, where: str) -> list[str]:
    found = []
    for marker in ERROR_MARKERS:
        if marker in text:
            i = text.find(marker)
            found.append(f"[{where}] {marker}: {text[i:i + 300]}")
    return found


def main() -> int:
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        page = browser.new_page(viewport={"width": 1680, "height": 1080})
        page.on("pageerror", lambda e: errors.append(f"[js] {e}"))

        page.goto(f"http://localhost:{PORT}", wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(14_000)

        for tab in TABS:
            if tab != "Board":
                page.get_by_role("tab", name=tab).click()
                page.wait_for_timeout(5_000)

            body = page.inner_text("body")
            errors.extend(scan(body, tab))

            slug = tab.split()[0].lower()
            page.screenshot(path=str(OUT / f"{slug}.png"))

            # Plotly and folium both render into containers that must have real
            # height; a zero-height pane means the chart silently did not draw.
            for selector, label in (
                (".stPlotlyChart", "plotly"),
                ("iframe", "folium"),
            ):
                for i, box in enumerate(page.locator(selector).all()):
                    try:
                        bb = box.bounding_box()
                    except Exception:
                        continue
                    if bb and bb["height"] < 30:
                        errors.append(
                            f"[{tab}] {label}[{i}] rendered at height "
                            f"{bb['height']:.0f}px — pane is collapsed"
                        )

            print(f"  {tab}: ok")

        browser.close()

    if errors:
        print("\nERRORS")
        for e in errors:
            print(" ", e)
        return 1

    print(f"\nclean — screenshots in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
