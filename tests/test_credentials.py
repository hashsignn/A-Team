"""Per-driver credentials for the endpoint that can move freight.

Everything else in this system is read-only and describes a region. A field
report observes a specific consignment and can satisfy `confirm.carrier`,
which releases a re-route — so a forged one sends freight the long way round
at somebody's expense.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.ingest import credentials as C

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return tmp_path / "drivers.json"


@pytest.fixture
def hans(store):
    return C.issue("Hans Meier", "CARR_RHN", NOW, store)


# =====================================================================
# The secret never survives issuing
# =====================================================================
def test_the_token_is_not_stored(hans, store):
    """A store that can print its own tokens leaks all of them the day
    somebody copies the file."""
    _, token = hans
    secret = token.split(".", 1)[1]
    assert secret not in store.read_text(encoding="utf-8")


def test_only_a_hash_is_stored(hans, store):
    import json
    record = next(iter(json.loads(store.read_text(encoding="utf-8")).values()))
    assert set(record) == {"name", "carrier", "created_at", "revoked_at", "secret_sha256"}
    assert len(record["secret_sha256"]) == 64


def test_the_store_is_not_world_readable(hans, store):
    assert store.stat().st_mode & 0o077 == 0, oct(store.stat().st_mode)


def test_two_drivers_get_different_keys(store):
    a, _ = C.issue("Hans", None, NOW, store)
    b, _ = C.issue("Anna", None, NOW, store)
    assert a.key_id != b.key_id


def test_a_driver_needs_a_name(store):
    """It is stamped on their reports. An anonymous credential defeats the
    entire point of having one."""
    with pytest.raises(C.CredentialError, match="needs a name"):
        C.issue("   ", None, NOW, store)


# =====================================================================
# Verification
# =====================================================================
def test_the_right_token_identifies_the_driver(hans, store):
    driver, token = hans
    found = C.verify(token, store)
    assert found is not None
    assert found.name == "Hans Meier"
    assert found.key_id == driver.key_id


@pytest.mark.parametrize("bad", [
    None, "", "nonsense", "no-dot-here", ".", "aaaaaaaaaaaa.",
    "NOTHEX000000.secret", "aaaaaaaaaaaa.wrongsecret",
    "../../etc/passwd.x", "aaaaaaaaaaaaaaaaaaaa.x",
])
def test_everything_else_is_refused(bad, hans, store):
    assert C.verify(bad, store) is None


def test_the_right_secret_under_the_wrong_key_fails(hans, store):
    _, token = hans
    secret = token.split(".", 1)[1]
    assert C.verify(f"ffffffffffff.{secret}", store) is None


def test_an_unreadable_store_refuses_rather_than_admits(tmp_path):
    broken = tmp_path / "drivers.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    assert C.verify("aaaaaaaaaaaa.whatever", broken) is None


# =====================================================================
# Revocation
# =====================================================================
def test_revoking_stops_the_token_working(hans, store):
    driver, token = hans
    assert C.verify(token, store) is not None
    C.revoke(driver.key_id, NOW, store)
    assert C.verify(token, store) is None


def test_revoking_twice_is_refused(hans, store):
    driver, _ = hans
    C.revoke(driver.key_id, NOW, store)
    with pytest.raises(C.CredentialError, match="already revoked"):
        C.revoke(driver.key_id, NOW, store)


def test_revoking_an_unknown_key_is_refused(store):
    with pytest.raises(C.CredentialError, match="no driver"):
        C.revoke("aaaaaaaaaaaa", NOW, store)


def test_revoking_the_last_driver_does_not_reopen_the_endpoint(hans, store):
    """The moment you most want the door shut.

    Keyed on the store existing rather than on a count of active drivers.
    Revoke everybody and nobody can file — a deliberate, visible outage
    rather than a quiet downgrade to the shared token.
    """
    driver, _ = hans
    C.revoke(driver.key_id, NOW, store)
    assert C.in_force(store) is True
    assert C.verify(_token_of(hans), store) is None


def _token_of(issued):
    return issued[1]


# =====================================================================
# When the mode is in force
# =====================================================================
def test_no_drivers_means_not_in_force(store):
    assert C.in_force(store) is False


def test_one_driver_is_enough(hans, store):
    assert C.in_force(store) is True


def test_a_broken_store_fails_closed(tmp_path):
    """Failing open on a broken credentials file is how an outage becomes an
    incident."""
    broken = tmp_path / "drivers.json"
    broken.write_text("[]", encoding="utf-8")          # a list, not an object
    assert C.in_force(broken) is True


# =====================================================================
# Listing
# =====================================================================
def test_a_listing_never_shows_a_hash(hans, store):
    blob = repr([d.as_public() for d in C.drivers(store)])
    assert "secret" not in blob and "sha256" not in blob


def test_revoked_drivers_stay_listed(hans, store):
    """Their reports stay in the append-only log; what they filed while
    trusted still happened, and the list has to explain who that was."""
    driver, _ = hans
    C.revoke(driver.key_id, NOW, store)
    listed = C.drivers(store)
    assert len(listed) == 1
    assert listed[0].active is False
    assert listed[0].revoked_at
