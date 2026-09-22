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

import re
from pathlib import Path

import pytest

from api.main import _asset_version, _page

STATIC = Path(__file__).resolve().parent.parent / "api" / "static"
PAGES = ("index.html", "profile.html", "fast.html", "fast-route.html")


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
    (fake / "app.js").write_text("console.log(1);")
    (fake / "styles.css").write_text("body{}")
    monkeypatch.setattr(main, "STATIC", fake)

    first = main._asset_version()
    (fake / "app.js").write_text("console.log(2);")
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
    (fake / "app.js").write_text("x")
    (fake / "styles.css").write_text("body{color:red}")
    monkeypatch.setattr(main, "STATIC", fake)

    before = main._asset_version()
    (fake / "styles.css").write_text("body{color:blue}")
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
    ):
        assert marker in html, f"{feature} missing from the served page"


def test_the_profile_page_carries_its_controls():
    html = _page("profile.html").body.decode()
    for marker in ('id="themes"', 'theme.js', 'id="prof-rail"', 'id="savebar"'):
        assert marker in html, marker
