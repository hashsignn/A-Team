"""End-to-end proof for the four systems that were added last.

Everything else tests a piece. This file tests the seams, because the seams
are where a design that reads correctly stops working:

* a CUSTOM source is fetched over real HTTP from a real server, not a fixture;
* the TWO-MODEL funnel runs with a stubbed backend and its output has to
  survive the deterministic challenger before it can reach the board;
* the NOISE FILTER is counted layer by layer, so "the funnel works" is a
  number rather than an assertion of faith.

No network, no model, no API key. A loopback HTTP server stands in for the
customer's endpoint, which is exactly what it would be.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.ingest.observations import FeedStatus
from engine.ingest.sources import Nature, collect, load_sources
from engine.pipeline import RunOptions, _rescue_event, run
from engine.reason import funnel as F
from engine.reason import llm
from engine.schemas import DelayTriple, Extraction

AS_OF = Clock(dt.datetime(2026, 9, 18, 6, 0, tzinfo=dt.UTC))
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example"

# What a customer's endpoint plausibly returns: nested, with its own field
# names, a UN/LOCODE it already knows, and one row that is missing the title —
# because a real feed always has one.
CUSTOMER_PAYLOAD = {
    "meta": {"generated": "2026-09-18T05:00:00Z"},
    "data": {
        "events": [
            {
                "eventId": "EX-4471",
                "summary": "Terminal gate closure at Antwerp following crane failure",
                "description": "Quay 1742 out of service. Gate closed to arrivals.",
                "reportedAt": "2026-09-18T05:30:00Z",
                "effectiveFrom": "2026-09-18T06:00:00Z",
                "effectiveTo": "2026-09-20T18:00:00Z",
                "location": {"unlocode": "BEANR", "name": "Antwerp"},
                "link": "https://tms.example.internal/events/EX-4471",
            },
            {"eventId": "EX-4472", "description": "no summary on this one"},
        ]
    },
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's interface
        if self.headers.get("Authorization") != "Bearer test-token-value":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorised"}')
            return
        body = json.dumps(CUSTOMER_PAYLOAD).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep pytest output readable


@pytest.fixture(scope="module")
def customer_endpoint():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def config():
    return load_config(EXAMPLE)


def _custom_yaml(tmp_path: Path, base_url: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(f"""
builtin:
  gdelt_doc: {{enabled: false}}
  gdacs: {{enabled: false}}
  reliefweb: {{enabled: false}}
  cisa_kev: {{enabled: false}}
  usgs_quakes: {{enabled: false}}
  open_meteo_marine: {{enabled: false}}
  autobahn_a5: {{enabled: false}}
  autobahn_a61: {{enabled: false}}
  autobahn_a3: {{enabled: false}}

custom:
  - key: customer_tms
    label: "Customer TMS — exception events"
    nature: report
    source_tier: 1
    url: "{base_url}/api/v1/exceptions"
    items_path: "data.events"
    date_format: iso
    auth: {{kind: bearer, env: E2E_TMS_TOKEN}}
    families: [port_ops]
    fields:
      headline: "summary"
      body: "description"
      identifier: "eventId"
      published: "reportedAt"
      starts: "effectiveFrom"
      ends: "effectiveTo"
      node_hint: "location.unlocode"
      url: "link"
      source_name: "const:Customer TMS"
""")
    return path


# =====================================================================
# 1. THE CUSTOM SOURCE, over real HTTP
# =====================================================================
def test_a_custom_source_is_fetched_over_real_http(tmp_path, customer_endpoint, monkeypatch):
    """The whole integration claim, proved rather than asserted: a block of
    YAML, a bearer token from the environment, somebody else's nesting, and
    items come back mapped."""
    monkeypatch.setenv("E2E_TMS_TOKEN", "test-token-value")
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    spec = next(s for s in load_sources(_custom_yaml(tmp_path, customer_endpoint))
                if s.key == "customer_tms")
    assert spec.runnable, spec.why_not_runnable()

    items, report = collect(spec, AS_OF.as_of)
    assert report.status is FeedStatus.CONNECTED, report.detail
    assert len(items) == 1, "the row with no summary must be dropped"
    assert "1 unmapped" in report.detail, "a dropped row must be COUNTED, not hidden"

    item = items[0]
    assert item["headline"].startswith("Terminal gate closure")
    assert item["node_hint"] == ["BEANR"], "the customer's own UN/LOCODE must survive"
    assert item["source_tier"] == 1
    assert item["source"] == "Customer TMS"
    assert item["ends_at"] is not None, "a stated end must be read, not defaulted"
    assert item["item_id"] == "CUSTOMER_TMS-EX-4471"


def test_the_token_is_actually_sent_and_actually_required(tmp_path, customer_endpoint, monkeypatch):
    """The endpoint 401s without it. If auth were silently skipped this test
    is the only thing that would notice."""
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("E2E_TMS_TOKEN", "wrong-token")

    spec = next(s for s in load_sources(_custom_yaml(tmp_path, customer_endpoint))
                if s.key == "customer_tms")
    items, report = collect(spec, AS_OF.as_of)
    assert items == []
    assert report.status is FeedStatus.ABSENT
    assert "401" in report.detail


def test_a_missing_token_never_reaches_the_network(tmp_path, customer_endpoint, monkeypatch):
    monkeypatch.setenv("RADAR_ALLOW_NETWORK", "1")
    monkeypatch.delenv("E2E_TMS_TOKEN", raising=False)
    spec = next(s for s in load_sources(_custom_yaml(tmp_path, customer_endpoint))
                if s.key == "customer_tms")
    assert not spec.runnable
    _, report = collect(spec, AS_OF.as_of)
    assert "E2E_TMS_TOKEN" in report.detail


def test_network_off_beats_a_perfectly_good_endpoint(tmp_path, customer_endpoint, monkeypatch):
    """The kill switch has to win over a reachable, authorised source, or it
    is not a kill switch."""
    monkeypatch.setenv("E2E_TMS_TOKEN", "test-token-value")
    monkeypatch.delenv("RADAR_ALLOW_NETWORK", raising=False)
    spec = next(s for s in load_sources(_custom_yaml(tmp_path, customer_endpoint))
                if s.key == "customer_tms")
    items, report = collect(spec, AS_OF.as_of)
    assert items == []
    assert "RADAR_ALLOW_NETWORK" in report.detail


# =====================================================================
# 2. THE FREE SOURCES
# =====================================================================
def test_every_free_source_actually_yields_items(config):
    """A spec that parses but maps nothing is a spec that is quietly wrong."""
    specs = [s for s in load_sources(EXAMPLE / "sources.yaml") if s.runnable]
    assert len(specs) == 9, f"expected 9 free runnable sources, got {len(specs)}"
    for spec in specs:
        items, report = collect(spec, AS_OF.as_of)
        assert report.status is FeedStatus.FIXTURE, f"{spec.key}: {report.detail}"
        assert items, f"{spec.key} produced no items from its own fixture"


def test_instruments_and_reports_are_both_represented():
    specs = [s for s in load_sources(EXAMPLE / "sources.yaml") if s.runnable]
    natures = {s.nature for s in specs}
    assert natures == {Nature.REPORT, Nature.INSTRUMENT}, (
        "both natures must be exercised, or the split that saves the model "
        "bill is untested"
    )


# =====================================================================
# 3. THE TWO MODELS, end to end through the rescue path
# =====================================================================
def _good_extraction() -> Extraction:
    """What a competent model returns for the novel Hormuz phrasing.

    The quote is copied from the source text below, because the challenger
    checks exactly that and a test that passes a fabricated quote would be
    testing nothing.
    """
    return Extraction(
        what_happened="A maritime interdiction regime has been declared across "
                      "the approaches to the Strait of Hormuz.",
        event_class="geopolitical",
        location_text="Strait of Hormuz",
        resolved_node_ids=["CHOKE_HORMUZ"],
        starts_at=AS_OF.as_of,
        ends_at=None,
        duration_confidence="unknown",
        realized=True,
        probability=None,
        probability_basis="the report states no odds and none can be sourced",
        delay_days=DelayTriple(optimistic=5.0, likely=14.0, pessimistic=40.0),
        delay_reasoning="No alternative sea route into the Gulf exists.",
        active_variables=["GEO_CONFLICT"],
        why_active={"GEO_CONFLICT": "an interdiction regime is a security "
                                    "threat to merchant shipping"},
        second_order_nodes=["AEJEA"],
        verbatim_quote="Unprecedented maritime interdiction regime declared "
                       "across Hormuz approaches",
        confidence=0.78,
    )


RESCUE_ITEM = {
    "item_id": "E2E-NOVEL-001",
    "headline": "Unprecedented maritime interdiction regime declared across Hormuz approaches",
    "text": "Unprecedented maritime interdiction regime declared across Hormuz approaches",
    "source": "tradewindsnews.com",
    "source_tier": 2,
    "source_nature": "report",
    "node_hint": ["CHOKE_HORMUZ"],
    "lat": None, "lon": None,
}


def test_stage_one_then_stage_two_produces_a_challenged_event(config, monkeypatch):
    """THE HEADLINE CLAIM. A phrasing the keyword router cannot name becomes an
    event on the board only after a model reads it AND the deterministic
    challenger accepts the reading."""
    calls: list[str] = []

    def fake_parse(model, system, prompt, status=None, model_name=""):
        calls.append(model.__name__)
        if model is F.Triage:
            return F.Triage(relevant=True, reason="strait closure affects sea freight",
                            freight_mode="sea", confidence=0.9)
        return _good_extraction()

    monkeypatch.setattr(llm, "parse", fake_parse)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.LOCAL, model="stub", detail="stub", unlocks_if_connected=""))

    kept, cost = F.triage([dict(RESCUE_ITEM)])
    assert cost.passed_triage == 1
    assert len(kept) == 1

    event = _rescue_event(kept[0], config, None, AS_OF)
    assert event is not None, "the challenger rejected a well-formed reading"
    assert event.node_ids == ["CHOKE_HORMUZ"]
    assert event.active_variables == ["GEO_CONFLICT"]
    assert event.provenance.inferred is True, (
        "a model-read event must be marked inferred, or the board cannot say "
        "which parts of the screen were computed and which were written"
    )
    assert calls == ["Triage", "Extraction"], f"wrong stage order: {calls}"


def test_a_fabricated_quote_is_refused_by_the_challenger(config, monkeypatch):
    """The most dangerous output this system can produce, because it looks
    like evidence. Substring matching catches it every time."""
    bad = _good_extraction().model_copy(
        update={"verbatim_quote": "Officials confirmed the closure will last six weeks"}
    )
    monkeypatch.setattr(llm, "parse", lambda model, *a, **k:
                        F.Triage(relevant=True, reason="x", freight_mode="sea", confidence=1.0)
                        if model is F.Triage else bad)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.LOCAL, model="stub", detail="stub", unlocks_if_connected=""))

    assert _rescue_event(dict(RESCUE_ITEM), config, None, AS_OF) is None


def test_an_invented_variable_id_is_refused(config, monkeypatch):
    bad = _good_extraction().model_copy(
        update={"active_variables": ["GEO_STRAIT_BLOCKADE"],
                "why_active": {"GEO_STRAIT_BLOCKADE": "invented"}}
    )
    monkeypatch.setattr(llm, "parse", lambda model, *a, **k:
                        F.Triage(relevant=True, reason="x", freight_mode="sea", confidence=1.0)
                        if model is F.Triage else bad)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.LOCAL, model="stub", detail="stub", unlocks_if_connected=""))

    assert _rescue_event(dict(RESCUE_ITEM), config, None, AS_OF) is None


def test_a_triage_no_means_stage_two_never_runs(config, monkeypatch):
    """The cost argument. If the expensive model ran anyway, the funnel would
    be decoration."""
    seen: list[str] = []

    def fake_parse(model, *a, **k):
        seen.append(model.__name__)
        return F.Triage(relevant=False, reason="markets commentary",
                        freight_mode="none", confidence=0.95)

    monkeypatch.setattr(llm, "parse", fake_parse)
    monkeypatch.setattr(llm, "detect", lambda *a, **k: llm.BackendStatus(
        backend=llm.Backend.LOCAL, model="stub", detail="stub", unlocks_if_connected=""))

    kept, cost = F.triage([dict(RESCUE_ITEM)])
    assert kept == []
    assert cost.dropped_by_triage == 1
    assert seen == ["Triage"], "extraction must not run on a triage no"


# =====================================================================
# 4. THE NOISE FILTER, counted layer by layer
# =====================================================================
def test_the_funnel_narrows_monotonically():
    """Each layer may only remove. A count that grows means a layer is
    inventing work for the one below it."""
    context = run(clock=AS_OF, config=load_config(EXAMPLE),
                  options=RunOptions(shipment_count=125, seed=7))
    f = context.result.funnel
    stages = [f.raw_observations, f.after_geographic, f.after_type,
              f.after_temporal, f.after_resolution]
    assert stages == sorted(stages, reverse=True), stages
    assert f.after_geographic < f.raw_observations, "layer 1 removed nothing"
    assert f.after_type < f.after_geographic, "layer 2 removed nothing"


def test_the_noise_in_the_corpus_is_actually_rejected(config):
    """The fixtures deliberately contain football, travel and market
    commentary. If any of them reached the board the filter is decorative."""
    context = run(clock=AS_OF, config=config,
                  options=RunOptions(shipment_count=125, seed=7))
    titles = " | ".join(e.title.lower() for e in context.events)
    for noise in ("striker", "weekend break", "equities rally"):
        assert noise not in titles, f"noise reached the board: {noise!r}"


def test_a_real_disruption_in_the_same_feed_does_reach_the_board(config):
    """The other half. A filter that rejects everything also rejects nothing
    of value, and would pass the test above."""
    context = run(clock=AS_OF, config=config,
                  options=RunOptions(shipment_count=125, seed=7))
    titles = " | ".join(e.title.lower() for e in context.events)
    assert "hormuz" in titles
    assert "antwerp" in titles


def test_every_dropped_item_leaves_a_trace(config):
    """The rule the whole design rests on: a deterministic filter that is
    wrong leaves a reason you can read. Nothing is allowed to vanish."""
    context = run(clock=AS_OF, config=config,
                  options=RunOptions(shipment_count=125, seed=7))
    accounted = set(context.router_notes) | set(context.unpromoted)
    assert accounted, "items were dropped with no recorded reason"
    for reason in context.router_notes.values():
        assert reason.strip(), "a drop reason must say something"
