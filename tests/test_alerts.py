"""Critical alerts by email (engine/alerts.py).

What must hold: a route alerts once per level and again only if it climbs;
going back in time never pages anyone; the email carries no customer names;
without a mail server nothing leaves the machine; with one, TLS is on and the
certificate is checked.
"""

from __future__ import annotations

import copy
import ssl

import pytest

from engine import alerts


def _board(as_of="2026-09-26T23:00:00+00:00", levels=None):
    levels = levels or {"R1": "red", "R2": "yellow", "R3": "green"}
    routes = []
    for rid, level in levels.items():
        routes.append({
            "route_id": rid, "name": f"Plant → Port → {rid}", "level": level,
            "level_label": {"red": "Critical", "yellow": "Alert", "green": "Normal"}[level],
            "clock_hours": 5.0 if level != "green" else None, "lead_time_hours": 3.0,
            "exposure_chf": 12345.6, "shipments": 9, "shipments_at_risk": 4,
            "customers": [{"name": "Secret Customer AG", "priority": "A", "at_risk": 1}],
            "events": [{"title": "Rhine low water at Kaub"}],
        })
    return {
        "as_of": as_of, "as_of_label": as_of[:16].replace("T", " ") + " UTC",
        "levels": [{"level": "red", "directive": "Take action within 8 hours"},
                   {"level": "yellow", "directive": "Take action within 24–36 hours"}],
        "routes": routes,
        "carrier_signals": {"patterns": [{"carrier": "C1", "carrier_name": "Line One", "node": "NLRTM",
                                          "node_name": "Rotterdam", "orders": 5,
                                          "last_noticed": "2026-09-26T13:00:00+00:00"}]},
        "order_signals": {"bursts": []},
    }


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "alerts.json", tmp_path / "alerts_state.json"


@pytest.fixture(autouse=True)
def no_mail_server(monkeypatch):
    for name in ("RADAR_SMTP_HOST", "RADAR_SMTP_USER", "RADAR_SMTP_PASSWORD", "RADAR_SMTP_FROM", "RADAR_SMTP_PORT"):
        monkeypatch.delenv(name, raising=False)


def _on(paths, **extra):
    settings, _ = paths
    return alerts.save_settings({"email": "planner@example.com", "levels": ["red"], "enabled": True, **extra},
                                settings)


def test_settings_are_checked_and_kept(paths):
    settings_path, _ = paths
    with pytest.raises(ValueError, match="email"):
        alerts.save_settings({"email": "not-an-address", "enabled": True}, settings_path)
    _on(paths, levels=["red", "yellow", "purple"], base_url="javascript:alert(1)")
    s = alerts.load_settings(settings_path)
    assert s.email == "planner@example.com" and s.levels == ["red", "yellow"] and s.enabled
    assert s.base_url == "", "only an http(s) address may go into the links"


def test_nothing_happens_until_alerts_are_on(paths):
    settings_path, state_path = paths
    assert alerts.on_board(_board(), settings_path, state_path, background=False) is None
    assert not state_path.exists()


def test_a_route_alerts_once_per_level_and_again_when_it_climbs(paths):
    settings_path, state_path = paths
    _on(paths, levels=["red", "yellow"])
    first = alerts.on_board(_board(), settings_path, state_path, background=False)
    assert first.status == "recorded" and "1 Critical, 1 Alert" in first.subject
    # The same board again: nothing new.
    assert alerts.on_board(_board(), settings_path, state_path, background=False) is None
    # R2 climbs from Alert to Critical: that is news.
    later = _board("2026-09-27T05:00:00+00:00", {"R1": "red", "R2": "red", "R3": "green"})
    again = alerts.on_board(later, settings_path, state_path, background=False)
    assert again is not None and "1 Critical" in again.subject and "R2" in again.subject


def test_a_route_that_calms_down_alerts_again_when_it_returns(paths):
    settings_path, state_path = paths
    _on(paths)
    alerts.on_board(_board(), settings_path, state_path, background=False)
    alerts.on_board(_board("2026-09-27T00:00:00+00:00", {"R1": "green"}), settings_path, state_path, background=False)
    back = alerts.on_board(_board("2026-09-27T01:00:00+00:00", {"R1": "red"}), settings_path, state_path,
                           background=False)
    assert back is not None and "R1" in back.subject


def test_going_back_in_time_never_pages_anyone(paths):
    settings_path, state_path = paths
    _on(paths)
    alerts.on_board(_board("2026-09-27T00:00:00+00:00", {"R1": "green"}), settings_path, state_path,
                    background=False)
    earlier = _board("2026-09-20T00:00:00+00:00", {"R1": "red"})
    assert alerts.on_board(earlier, settings_path, state_path, background=False) is None


def test_the_email_names_routes_not_customers(paths):
    settings_path, _ = paths
    s = _on(paths, base_url="https://radar.example")
    msg = alerts.compose(_board(), s, [r for r in _board()["routes"] if r["level"] == "red"], ["a warning"])
    body = msg.get_content()
    assert "Plant → Port → R1" in body and "CHF 12,346" in body and "1 key account" in body
    assert "Secret Customer AG" not in body and "Secret Customer AG" not in msg["Subject"]
    assert "https://radar.example/tree?route=R1&as_of=2026-09-26T23%3A00%3A00%2B00%3A00" in body


def test_early_warnings_alert_once(paths):
    settings_path, state_path = paths
    _on(paths)
    quiet = _board(levels={"R3": "green"})
    first = alerts.on_board(quiet, settings_path, state_path, background=False)
    assert first is not None and "early warning" in first.subject
    assert alerts.on_board(copy.deepcopy(quiet), settings_path, state_path, background=False) is None


def test_with_a_mail_server_it_uses_tls_with_the_certificate_checked(paths, monkeypatch):
    settings_path, state_path = paths
    _on(paths)
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["where"] = (host, port)

        def starttls(self, context):
            assert isinstance(context, ssl.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg):
            sent["to"] = msg["To"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("RADAR_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("RADAR_SMTP_USER", "planner@example.com")
    monkeypatch.setenv("RADAR_SMTP_PASSWORD", "app-password")
    receipt = alerts.on_board(_board(), settings_path, state_path, background=False)
    assert receipt.status == "sent"
    assert sent == {"where": ("smtp.example.com", 587), "tls": True, "login": "planner@example.com",
                    "to": "planner@example.com"}
    assert alerts.recent(state_path)[0]["status"] == "sent"


def test_a_mail_server_that_fails_is_a_receipt_not_a_crash(paths, monkeypatch):
    settings_path, state_path = paths
    _on(paths)

    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(alerts.smtplib, "SMTP", refuse)
    monkeypatch.setenv("RADAR_SMTP_HOST", "smtp.example.com")
    receipt = alerts.on_board(_board(), settings_path, state_path, background=False)
    assert receipt.status == "failed" and "connection refused" in receipt.detail


def test_a_test_email_says_it_is_a_test(paths):
    settings_path, state_path = paths
    _on(paths)
    receipt = alerts.test(_board(), settings_path, state_path)
    assert receipt.status == "recorded" and receipt.subject.startswith("[Radar test]")
