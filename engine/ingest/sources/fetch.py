"""One HTTP path for every source, and one honest answer about what happened.

WHY ONE FETCHER
===============
Ten sources means ten chances to forget a timeout. A feed that hangs takes the
board with it, and a board that will not load during a disruption is worse than
no board — the planner reaches for it exactly when it matters.

So: one function, one timeout, one failure mode. A source that 500s, times out,
returns HTML, or has its schema changed underneath us produces a FIXTURE or
ABSENT report and the other nine carry on.

NETWORK IS OPT-IN
-----------------
``RADAR_ALLOW_NETWORK`` must be set before anything is fetched. Default off.

Two reasons. The demo environment blocks every data host, so the default has to
work with the cable out. And a tool that quietly starts calling nine external
services the first time it is run is one nobody can deploy inside a corporate
network without a conversation they were not expecting to have.

WHAT THE FEEDREPORT PROMISES
----------------------------
``CONNECTED`` means bytes came from the real host, now. ``FIXTURE`` means
committed sample data is standing in and the panel says so. ``ABSENT`` means
nothing is wired and the report says what connecting it would buy. Those three
are the whole contract, and none of them is ever a silent default.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from engine.ingest.observations import FeedReport, FeedStatus, load_fixture
from engine.ingest.sources.mapping import to_items
from engine.ingest.sources.spec import Cost, SourceSpec

log = logging.getLogger(__name__)

TIMEOUT_S = float(os.environ.get("RADAR_FEED_TIMEOUT", "12"))
MAX_BYTES = int(os.environ.get("RADAR_FEED_MAX_BYTES", str(8 * 1024 * 1024)))
USER_AGENT = "supply-chain-risk-radar/1.0 (+contact: planner desk)"


def network_allowed() -> bool:
    return os.environ.get("RADAR_ALLOW_NETWORK", "").strip().lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------
def collect(
    spec: SourceSpec,
    retrieved_at: datetime,
    window_days: float | None = None,
) -> tuple[list[dict], FeedReport]:
    """Fetch one source. Never raises. Always returns a truthful report."""
    if not spec.runnable:
        return [], _absent(spec, spec.why_not_runnable())

    blob, error = (None, "network not enabled (set RADAR_ALLOW_NETWORK=1)")
    if network_allowed():
        blob, error = _get(spec, as_of=retrieved_at, window_days=window_days)

    if blob is not None:
        items, dropped = to_items(blob, spec, retrieved_at)
        detail = f"{len(items)} item(s)"
        if spec.window is not None:
            span = spec.window.days if window_days is None else window_days
            detail += (f" over the {span:g} day(s) ending "
                       f"{retrieved_at.strftime('%Y-%m-%d')}")
        if dropped:
            detail += f", {dropped} unmapped (spec may be stale)"
        return items, FeedReport(
            key=spec.key,
            label=spec.label,
            status=FeedStatus.CONNECTED,
            detail=detail,
            unlocks_if_connected="",
            records=len(items),
            retrieved_at=retrieved_at,
            source_tier=spec.source_tier,
            url=spec.resolved_url,
            nature=spec.nature.value,
            cost=spec.cost.value,
        )

    if spec.fixture:
        cached = load_fixture(spec.fixture)
        if cached is not None:
            items, dropped = to_items(cached, spec, retrieved_at)
            return items, FeedReport(
                key=spec.key,
                label=spec.label,
                status=FeedStatus.FIXTURE,
                detail=f"{len(items)} item(s) from a recorded sample — {error}",
                unlocks_if_connected=spec.unlocks_if_connected,
                records=len(items),
                retrieved_at=retrieved_at,
                source_tier=spec.source_tier,
                url=spec.resolved_url,
                nature=spec.nature.value,
                cost=spec.cost.value,
            )

    return [], _absent(spec, error)


def collect_all(
    specs: list[SourceSpec],
    retrieved_at: datetime,
    window_days: float | None = None,
) -> tuple[list[dict], list[FeedReport]]:
    items: list[dict] = []
    reports: list[FeedReport] = []
    for spec in specs:
        got, report = collect(spec, retrieved_at, window_days=window_days)
        items.extend(got)
        reports.append(report)
    return items, reports


# ---------------------------------------------------------------------
def _get(
    spec: SourceSpec,
    as_of: datetime | None = None,
    window_days: float | None = None,
) -> tuple[Any | None, str]:
    """GET and decode. Returns (blob, error) with exactly one of them set."""
    url = spec.resolved_url
    if not url:
        return None, "no URL configured"

    params = dict(spec.params)

    # Ask for the window that ends at the as-of, when the source supports it.
    # Without this every request means "the last few hours from whenever you
    # happen to be running", which makes replaying a past day impossible and
    # makes a recording over a period that already happened impossible too.
    if spec.window is not None and as_of is not None:
        add, drop = spec.window.params_for(as_of, window_days)
        for key in drop:
            params.pop(key, None)
        params.update(add)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **spec.headers}

    secret = spec.auth.resolve()
    if secret:
        if spec.auth.kind == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif spec.auth.kind == "header":
            headers[spec.auth.name or "X-API-Key"] = secret
        elif spec.auth.kind == "query":
            params[spec.auth.name or "key"] = secret

    if params:
        joiner = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{joiner}{urllib.parse.urlencode(params)}"

    return get_json(url, headers)


def get_json(url: str, headers: dict[str, str] | None = None) -> tuple[Any | None, str]:
    """GET one URL and decode it. Returns (blob, error) with exactly one set.

    Everything that leaves the machine goes through here — the catalogue's
    sources and the Kaub gauge alike — so the timeout, the size cap and the
    wording of a refusal are decided once.
    """
    if headers is None:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            return None, f"response exceeded {MAX_BYTES} bytes"
        return json.loads(payload.decode("utf-8", errors="replace")), ""
    except urllib.error.HTTPError as exc:
        # A refusal that says how long to wait is worth passing on: it is the
        # difference between a planner reading "HTTP 429" and one reading
        # when to try again — and the recorder backs off by exactly this.
        wait = (exc.headers.get("Retry-After") or "").strip() if exc.headers else ""
        if wait.isdigit():
            return None, f"HTTP {exc.code} (retry after {int(wait)}s)"
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"unreachable ({exc.reason})"
    except json.JSONDecodeError:
        return None, "response was not JSON (a proxy login page usually does this)"
    except Exception as exc:  # noqa: BLE001 — one bad feed never takes the board down
        # The host, not the URL: a key passed in the query string would
        # otherwise be written to the log.
        log.warning("fetch from %s failed: %s", urllib.parse.urlsplit(url).hostname, exc)
        return None, f"failed ({type(exc).__name__})"


def _absent(spec: SourceSpec, detail: str) -> FeedReport:
    return FeedReport(
        key=spec.key,
        label=spec.label,
        status=FeedStatus.ABSENT,
        detail=detail,
        unlocks_if_connected=spec.unlocks_if_connected,
        records=0,
        source_tier=spec.source_tier,
        url=spec.url if spec.cost is not Cost.PAID else None,
        nature=spec.nature.value,
        cost=spec.cost.value,
    )
