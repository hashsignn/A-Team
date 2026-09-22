#!/usr/bin/env python3
"""Recapture the deck's screenshots from a running server.

The deck is built from real screens, so they have to be reproducible. Start
the app at a pinned as-of date, then run this:

    python run.py serve --host 127.0.0.1 --port 8099 &
    python docs/pitch/capture.py

Everything is pinned — the as-of date, the shipment count, the theme, the
viewport and the device scale — so two runs produce the same pixels.
"""
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
QUERY = "as_of=2026-09-16&shipments=220"
LANE = "LANE_RHINE_01"
OUT = Path(__file__).resolve().parent / "img"

# Playwright's own download is skipped in this container; the image ships a
# browser instead. Fall back to whatever Playwright resolves if it is absent.
CHROMIUM = "/opt/pw-browsers/chromium"

# Crops, in the captured pixel space (device_scale_factor=2 on a 1600-wide
# viewport, so 3200 wide). Each one isolates the panel the slide talks about.
CROPS = {
    "escalate.png": ("escalate_panel.png", (1925, 20, 3160, 1760)),
    "route_vehicle.png": ("vehicle_panel.png", (400, 1180, 3130, 2190)),
    "driver_phone.png": ("driver_tall.png", (0, 0, 1290, 2300)),
    "board_routes.png": ("routes_table.png", (40, 0, 1900, 2000)),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        kwargs = {"args": ["--no-sandbox"]}
        if Path(CHROMIUM).exists():
            kwargs["executable_path"] = CHROMIUM
        browser = pw.chromium.launch(**kwargs)

        desk = browser.new_context(
            viewport={"width": 1600, "height": 1100}, device_scale_factor=2)
        # Set before the first paint, or the page comes up in the stored theme.
        desk.add_init_script(
            "try{localStorage.setItem('scrr.theme','sika')}catch(e){}")
        page = desk.new_page()

        def shot(name, url, scroll=0, click=None, settle=2500):
            page.goto(f"{BASE}{url}", wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(settle)
            if click:
                page.click(click)
                page.wait_for_timeout(1500)
            if scroll:
                page.mouse.wheel(0, scroll)
                page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / name))
            print("captured", name)

        shot("board.png", f"/?{QUERY}")
        shot("board_routes.png", f"/?{QUERY}", scroll=1080)
        shot("escalate.png", f"/?{QUERY}", click='.rtab[data-tab="escalate"]')
        shot("route.png", f"/route/{LANE}?{QUERY}")
        shot("ops.png", f"/ops?{QUERY}")
        shot("profile.png", f"/profile?{QUERY}")
        shot("alternatives.png", f"/ops?route={LANE}&as_of=2026-09-16")

        # The vehicle panel only exists once a consignment is selected.
        page.goto(f"{BASE}/route/{LANE}?{QUERY}", wait_until="networkidle",
                  timeout=45000)
        page.wait_for_timeout(2500)
        veh = page.query_selector(".veh--hit") or page.query_selector(".veh--at_risk")
        if veh is None:
            raise SystemExit("no affected vehicle on this lane at this as-of")
        veh.scroll_into_view_if_needed()
        veh.click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "route_vehicle.png"))
        print("captured route_vehicle.png")

        phone = browser.new_context(
            viewport={"width": 430, "height": 900}, device_scale_factor=3)
        phone.add_init_script(
            "try{localStorage.setItem('scrr.theme','sika')}catch(e){}")
        mobile = phone.new_page()
        mobile.goto(f"{BASE}/driver", wait_until="networkidle", timeout=45000)
        mobile.wait_for_timeout(2000)
        mobile.screenshot(path=str(OUT / "driver_phone.png"), full_page=True)
        print("captured driver_phone.png")

        browser.close()

    for src, (dst, box) in CROPS.items():
        Image.open(OUT / src).crop(box).save(OUT / dst)
        print("cropped", dst)


if __name__ == "__main__":
    main()
