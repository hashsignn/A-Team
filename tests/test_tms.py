"""The TMS contract, and the secret-leakage audit that guards it.

The audit test is the important one. The failure it prevents — a token pasted
into a config, serialised into every alert, and shipped to a third-party
system that logs request bodies — is obvious in hindsight and invisible
beforehand, and no amount of care at review time catches it reliably. A
deterministic check in the suite does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.clock import Clock
from engine.config import load_config
from engine.export import tms
from engine.export.board import build_board
from engine.pipeline import RunOptions, run
from engine.score.severity import Level
from engine.taxonomy.resolve import Encounter, resolve

AS_OF = Clock.at("2026-09-18T06:00:00+00:00")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def context(config):
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=80))


@pytest.fixture(scope="module")
def board(context):
    return build_board(context)


@pytest.fixture(scope="module")
def alerts(board, context):
    return tms.alerts_for_board(board, context, band="all")


# =====================================================================
# Secret leakage
# =====================================================================


def test_no_alert_payload_carries_anything_credential_shaped(alerts):
    assert alerts, "no alerts produced, so the audit proved nothing"
    for alert in alerts:
        problems = tms.audit_for_secrets(alert)
        assert not problems, f"{alert['idempotency_key']}: {problems}"


def test_the_audit_actually_catches_a_planted_secret():
    """A check that cannot fail is not a check."""
    planted = {"delivery": {"auth_token": "sk-live-" + "a" * 40}}
    assert tms.audit_for_secrets(planted)

    nested = {"contacts": [{"api_key": "AKIA" + "B" * 16}]}
    assert tms.audit_for_secrets(nested)

    bearer = {"x": {"y": {"authorization": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa"}}}
    assert tms.audit_for_secrets(bearer)


def test_describing_where_a_credential_comes_from_is_not_a_leak(alerts):
    """`auth` says the token comes from the environment. That sentence must
    not trip the audit, or the audit gets disabled."""
    assert "environment" in alerts[0]["delivery"]["auth"]
    assert not tms.audit_for_secrets(alerts[0])


def test_no_endpoint_or_hostname_is_baked_into_a_payload(alerts):
    blob = json.dumps(alerts)
    for marker in ("http://", "https://", ".sika.com", "sap.", "localhost"):
        assert marker not in blob, f"{marker!r} leaked into a TMS payload"


def test_the_committed_config_contains_no_secrets():
    """config.example/ is public and committed. It is also the file a hurried
    person would paste a real key into."""
    for path in (ROOT / "config.example").glob("*.yaml"):
        problems = tms.audit_for_secrets({"file": path.read_text(encoding="utf-8")})
        assert not problems, f"{path.name}: {problems}"


def test_gitignore_covers_the_files_that_would_carry_credentials():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "config/"):
        assert pattern in ignored, f"{pattern} is not gitignored"


# =====================================================================
# The contract
# =====================================================================


def test_every_alert_validates_against_the_published_schema(alerts):
    schema = json.loads((ROOT / "schemas" / "event_radar_taxonomy.json").read_text(encoding="utf-8"))
    layer1 = set(schema["properties"]["layer1_event"]["properties"]["variable_id"]["enum"])
    layer2 = set(schema["properties"]["layer2_asset"]["properties"]["asset_id"]["enum"])
    layer3 = set(schema["properties"]["layer3_channel"]["properties"]["channel_id"]["enum"])
    layer4 = set(schema["properties"]["layer4_cargo"]["properties"]["cargo_class"]["enum"])
    for alert in alerts:
        tax = alert["taxonomy"]
        assert tax["layer1_event"]["variable_id"] in layer1
        assert tax["layer2_asset"]["asset_id"] in layer2
        assert tax["layer3_channel"]["channel_id"] in layer3
        assert tax["layer4_cargo"]["cargo_class"] in layer4


def test_the_schema_on_disk_is_not_stale():
    """Generated from the config, so it cannot drift. A schema that disagrees
    with the runtime is worse than no schema — integrators build against it
    and fail in production."""
    import subprocess

    result = subprocess.run(
        ["python", str(ROOT / "scripts" / "gen_schema.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_idempotency_key_is_stable_across_rebuilds(board, context):
    first = tms.alerts_for_board(board, context, band="all")
    second = tms.alerts_for_board(board, context, band="all")
    assert [a["idempotency_key"] for a in first] == [
        a["idempotency_key"] for a in second
    ]


def test_a_null_probability_survives_into_the_payload_as_null(alerts):
    """Never coerced to 0.5. The payload also says so in its own
    data_quality block, so an integrator reads it before their parser does."""
    unsourced = [
        a for a in alerts if a["taxonomy"]["layer1_event"]["probability"] is None
    ]
    assert unsourced, "fixture has no unsourced event to check"
    for alert in unsourced:
        assert alert["data_quality"]["probability_is_null_means_unsourced"] is True


def test_suppressed_actions_are_present_not_omitted(config):
    """A TMS offering air freight for a Class 3 solvent has wasted the hours
    a planner had left."""
    result = resolve(config, Encounter(
        variable_id="GEO_REGULATORY",
        cargo_class="adr_class_3",
        asset_id="adr_tanker",
        channel_id="jit_jis",
        late_hours=52.0,
        consequence_cap=60_000.0,
        base_level=Level.YELLOW,
    )).as_dict()
    assert result["actions_suppressed"]
    assert any(f["asset"] == "air_cargo" for f in result["actions_suppressed"])


def test_every_alert_carries_the_reason_for_its_score(alerts):
    for alert in alerts:
        assert alert["alert"]["rationale"].strip()
        assert alert["assessment"]["pathway_headline"].strip()
        assert alert["assessment"]["exposure_explanation"].strip()


def test_the_payload_states_that_the_book_is_synthetic(alerts):
    """Shipping modelled figures to a TMS without saying they are modelled is
    how a demo becomes a production dependency by accident."""
    for alert in alerts:
        assert "data_quality" in alert
        assert "modelled, not invoiced" in alert["data_quality"]["note"]
