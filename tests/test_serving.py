"""How the pages are served.

This file exists because of a real failure, not a hypothetical one: the
dashboard was rebuilt three times over and a reviewer opening it saw the
version from two days earlier. Nothing was wrong with the code, the merge or
the server. The HTML had been served with no cache directive, so a browser
that had loaded the page before kept serving its stored copy — and that copy
named the OLD script and stylesheet, so none of the new assets were ever
requested.

A deploy that lands on the server and never reaches the screen is
indistinguishable from a deploy that never happened. These tests are cheap
and they close that gap.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api.main import _asset_version, _page

STATIC = Path(__file__).resolve().parent.parent / "api" / "static"
PAGES = ("index.html", "profile.html", "tree.html", "route.html", "driver.html", "shipment.html")


# =====================================================================
# The HTML is a manifest and must never be stored
# =====================================================================


@pytest.mark.parametrize("page", PAGES)
def test_the_html_is_never_cached(page):
    """It is the file that says which assets to load. Cache it and a browser
    keeps asking for last week's assets by last week's names."""
    response = _page(page)
    cache = response.headers["cache-control"]
    assert "no-store" in cache, cache


@pytest.mark.parametrize("page", PAGES)
def test_every_asset_reference_carries_a_version(page):
    """An unversioned asset can be served stale forever, and the page will
    look untouched while the server holds the new one."""
    html = _page(page).body.decode()
    bare = re.findall(r'(?:src|href)="/(?!/)([\w./-]+\.(?:js|css))"', html)
    assert not bare, f"{page}: unversioned asset reference(s): {bare}"


@pytest.mark.parametrize("page", PAGES)
def test_no_version_token_is_left_unsubstituted(page):
    """A literal __ASSETV__ reaching the browser is a 404 on every asset —
    the page would render completely unstyled, which is at least loud."""
    assert "__ASSETV__" not in _page(page).body.decode()


# =====================================================================
# The version has to actually change
# =====================================================================


def test_the_version_changes_when_an_asset_changes(tmp_path, monkeypatch):
    """A stable-looking version that never moves is worse than none: it
    invites hard caching while still serving stale content."""
    import api.main as main

    fake = tmp_path / "static"
    fake.mkdir()
    (fake / "app.js").write_text("console.log(1);", encoding="utf-8")
    (fake / "styles.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(main, "STATIC", fake)

    first = main._asset_version()
    (fake / "app.js").write_text("console.log(2);", encoding="utf-8")
    second = main._asset_version()
    assert first != second

    # ...and is stable when nothing changes, or every page load busts the
    # cache it exists to manage.
    assert second == main._asset_version()


def test_the_version_covers_every_served_asset_type(tmp_path, monkeypatch):
    """A CSS-only change must bust too. Versioning the JS alone would leave
    a restyle invisible, which is the same failure in a quieter form."""
    import api.main as main

    fake = tmp_path / "static"
    fake.mkdir()
    (fake / "app.js").write_text("x", encoding="utf-8")
    (fake / "styles.css").write_text("body{color:red}", encoding="utf-8")
    monkeypatch.setattr(main, "STATIC", fake)

    before = main._asset_version()
    (fake / "styles.css").write_text("body{color:blue}", encoding="utf-8")
    assert main._asset_version() != before


def test_the_version_is_short_enough_to_read_in_a_url():
    assert 8 <= len(_asset_version()) <= 16


# =====================================================================
# HEAD, because monitors use it
# =====================================================================


def test_head_is_registered_on_both_pages():
    """`@app.get` alone answers HEAD with 404, so an uptime probe on the
    app's own front page reports it down. A false alarm somebody has to
    chase at 3am is worse than no probe."""
    from api.main import app

    methods = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in ("/", "/profile"):
            methods.setdefault(path, set()).update(getattr(route, "methods", set()))
    for path in ("/", "/profile"):
        assert "HEAD" in methods.get(path, set()), f"{path} does not answer HEAD"


# =====================================================================
# The pages really do carry the current feature set
# =====================================================================


def test_the_board_page_carries_every_control_that_was_shipped():
    """Not a style check — a delivery check. Each of these is a feature that
    a stale cache silently removed from the page."""
    html = _page("index.html").body.decode()
    for marker, feature in (
        ('id="themes"', "theme picker"),
        ('theme.js', "theme module"),
        ('id="btn-ask"', "assistant button"),
        ('id="ask-panel"', "assistant panel"),
        ('id="link-profile"', "risk profile link"),
        ('id="fleetmap"', "2D fleet map"),
        ('mapstore.js', "map state store"),
        ('mapagent.js', "map agent hooks"),
        ('maplibre-gl.js', "vendored MapLibre"),
        ('id="hub"', "Action Hub"),
        ('id="vendor-card"', "partner card"),
        ('data-view="globe"', "map / globe toggle"),
    ):
        assert marker in html, f"{feature} missing from the served page"


def test_the_map_libraries_are_vendored_not_fetched():
    """The page renders with the network cable pulled out. A CDN script tag
    would make the map the one part of the board that does not."""
    html = _page("index.html").body.decode()
    external = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)
    assert not external, f"scripts loaded from the network: {external}"
    for name in ("maplibre-gl.js", "maplibre-gl.css", "chart.umd.min.js"):
        assert (STATIC / "vendor" / name).exists(), name
    for licence in ("LICENSE.maplibre-gl", "LICENSE.chart.js"):
        assert (STATIC / "vendor" / licence).exists(), licence


def test_a_vendored_stylesheet_swap_busts_the_cache(tmp_path, monkeypatch):
    """maplibre-gl.css is referenced with a version like everything else; a
    library upgrade that changed only the CSS must still change it."""
    import api.main as main

    fake = tmp_path / "static"
    (fake / "vendor").mkdir(parents=True)
    (fake / "app.js").write_text("x", encoding="utf-8")
    (fake / "vendor" / "lib.css").write_text("a{}", encoding="utf-8")
    monkeypatch.setattr(main, "STATIC", fake)
    before = main._asset_version()
    (fake / "vendor" / "lib.css").write_text("a{color:red}", encoding="utf-8")
    assert main._asset_version() != before


def test_the_profile_page_carries_its_controls():
    html = _page("profile.html").body.decode()
    for marker in ('id="themes"', 'theme.js', 'id="prof-rail"', 'id="savebar"'):
        assert marker in html, marker


# =====================================================================
# Retired pages still land somewhere useful
# =====================================================================


def _request(path: str, query: str = "", method: str = "GET", site: str | None = None):
    from starlette.requests import Request  # noqa: PLC0415

    headers = [(b"sec-fetch-site", site.encode())] if site else []
    return Request({"type": "http", "method": method, "path": path,
                    "query_string": query.encode(), "headers": headers})


def test_the_retired_checklist_and_act_fast_pages_redirect():
    """The decision tree replaced the checklist (/ops) and Act fast (/fast).
    An old link or bookmark goes to the tree for its route, or the board."""
    from api.main import fast_page, fast_route_page, ops_page  # noqa: PLC0415

    to_tree = ops_page(_request("/ops", "route=LANE_ASIA_08&as_of=2026-09-18"))
    assert to_tree.status_code == 307
    assert to_tree.headers["location"] == "/tree?route=LANE_ASIA_08&as_of=2026-09-18"
    assert ops_page(_request("/ops")).headers["location"] == "/"
    assert fast_page(_request("/fast", "as_of=2026-09-18")).headers["location"] == "/?as_of=2026-09-18"
    lane = fast_route_page("LANE_RHINE_01", _request("/fast/LANE_RHINE_01", "as_of=2026-09-18"))
    assert lane.headers["location"] == "/tree?as_of=2026-09-18&route=LANE_RHINE_01"
    for gone in ("ops.html", "fast.html", "fast-route.html"):
        assert not (STATIC / gone).exists(), gone


def test_the_logo_falls_back_to_the_horizon_mark(tmp_path, monkeypatch):
    """Sika's logo is theirs to supply (config/brand/logo.*); without it the
    header gets the neutral mark, never a broken image."""
    import api.main as main  # noqa: PLC0415

    monkeypatch.setattr(main, "CUSTOMER_DIR", tmp_path)
    assert str(main.brand_logo().path) == str(STATIC / "horizon-mark.svg")
    (tmp_path / "brand").mkdir()
    (tmp_path / "brand" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    served = main.brand_logo()
    assert str(served.path) == str(tmp_path / "brand" / "logo.png")
    assert served.media_type == "image/png"


def test_a_logo_can_be_uploaded_and_only_as_a_picture(tmp_path, monkeypatch):
    """The header's logo is set with one click: the image is kept in
    config/brand/ (gitignored). Only PNG, JPEG or WebP, told apart by their
    first bytes; an SVG or a script with an image name is refused."""
    from fastapi import HTTPException  # noqa: PLC0415

    import api.main as main  # noqa: PLC0415

    monkeypatch.setattr(main, "CUSTOMER_DIR", tmp_path)
    webp = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
    assert main.save_logo(webp)["file"] == "config/brand/logo.webp"
    assert main.brand_logo().media_type == "image/webp"

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(HTTPException) as refused:
        main.save_logo(svg)
    assert refused.value.status_code == 415
    assert (tmp_path / "brand" / "logo.webp").is_file(), "a refused upload leaves the logo alone"
    with pytest.raises(HTTPException) as big:
        main.save_logo(b"\x89PNG\r\n\x1a\n" + b"\x00" * main.LOGO_MAX_BYTES)
    assert big.value.status_code == 413

    main.save_logo(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    assert (tmp_path / "brand" / "logo.png").is_file() and not (tmp_path / "brand" / "logo.webp").exists()
    assert "default-src 'none'" in main.brand_logo().headers["content-security-policy"]

    # Another site's page cannot reset it from the planner's browser.
    with pytest.raises(HTTPException) as foreign:
        main.brand_logo_reset(_request("/brand/logo", method="DELETE", site="cross-site"))
    assert foreign.value.status_code == 403
    assert (tmp_path / "brand" / "logo.png").is_file()

    own = _request("/brand/logo", method="DELETE", site="same-origin")
    assert json.loads(main.brand_logo_reset(own).body)["removed"] == 1
    assert main.brand_logo().media_type == "image/svg+xml"
