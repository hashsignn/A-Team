"""Per-driver credentials for the one endpoint that can move freight.

WHY THIS ENDPOINT AND NOT THE OTHERS
====================================
Everything else in this system is read-only, and its inputs describe a region:
a gauge reading, a wire story, a disaster alert. A field report is different
in two ways that make it worth protecting properly. It is the only input that
OBSERVES a specific consignment, and it is the only one that can satisfy
``confirm.carrier`` and release a re-route. A forged confirmation sends freight
the long way round at somebody's expense.

WHAT A CREDENTIAL IS
====================
    <key_id>.<secret>

``key_id`` is public and is how the record is found — 12 hex characters, no
secret in it. ``secret`` is 32 bytes from ``secrets.token_urlsafe``. The store
keeps the key_id, the person's name, and a HASH of the secret. The secret
itself exists exactly once, in the output of the command that created it, and
is never recoverable: a store that can print its own tokens is a store that
leaks all of them the day somebody copies the file.

WHY A PLAIN SHA-256 AND NOT BCRYPT
==================================
Because these are not passwords. Password hashing is slow on purpose to make
dictionary attacks expensive, and that cost is justified because humans pick
"hunter2". A 256-bit random token has no dictionary to attack — brute force
means 2^256 guesses, and no work factor changes that. Adding scrypt here would
buy nothing and add ~100 ms to every photo upload from a phone on a bad
connection, which is a real cost paid for a theoretical benefit.

The comparison is constant-time regardless, because the key_id lookup tells an
attacker which record they hit and a timing difference on the secret would
leak it a byte at a time.

STILL OPT-IN, BUT NO LONGER OPTIONAL ONCE USED
==============================================
With no drivers registered the endpoint behaves exactly as before, because a
demo that needs a credential before it demonstrates anything is a demo nobody
runs. But the moment ONE driver is registered, the shared
``RADAR_REPORT_TOKEN`` stops being sufficient — otherwise registering drivers
would not actually increase security, it would just add a second way in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# config/ rather than data/: this is configuration a person maintains, not
# runtime state the app produces — and config/ is already gitignored as "the
# customer's real data", which is exactly what a list of driver names is.
DEFAULT_STORE = ROOT / "config" / "drivers.json"

KEY_ID_BYTES = 6                      # 12 hex characters
SECRET_BYTES = 32
_KEY_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_NAME_MAX = 120


class CredentialError(ValueError):
    """The credential cannot be issued or the store cannot be read."""


@dataclass(frozen=True)
class Driver:
    key_id: str
    name: str
    carrier: str | None
    created_at: str
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def as_public(self) -> dict:
        """Everything except the hash. What a listing may print."""
        return {
            "key_id": self.key_id,
            "name": self.name,
            "carrier": self.carrier,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
            "active": self.active,
        }


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def load(store: Path | None = None) -> dict[str, dict]:
    """The raw records. An absent store is an empty one, not an error."""
    path = store or DEFAULT_STORE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError(f"{path} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"{path} should contain an object keyed by key_id")
    return data


def drivers(store: Path | None = None) -> list[Driver]:
    return [
        Driver(
            key_id=key_id,
            name=record.get("name", "?"),
            carrier=record.get("carrier"),
            created_at=record.get("created_at", ""),
            revoked_at=record.get("revoked_at"),
        )
        for key_id, record in sorted(load(store).items())
    ]


def in_force(store: Path | None = None) -> bool:
    """Is per-driver auth in force? Decided by the STORE EXISTING, not by a count.

    The tempting rule is "any active driver". It is wrong, and dangerously so:
    revoke the last driver — the moment you most want the door shut — and the
    endpoint silently reopens to the shared token, or to nobody at all.

    So the existence of the file is the adoption of the mode. Once a
    deployment has issued one credential it is in per-driver mode forever,
    and revoking everybody means nobody can file. That is a deliberate,
    visible outage rather than a quiet downgrade, and it is undone by issuing
    a credential, which is the thing an administrator was going to do anyway.

    A store that cannot be read is also a closed door. Failing open on a
    broken credentials file is how an outage becomes an incident.
    """
    path = store or DEFAULT_STORE
    if not path.exists():
        return False
    try:
        load(path)
    except CredentialError:
        return True
    return True


def issue(name: str, carrier: str | None, now: datetime,
          store: Path | None = None) -> tuple[Driver, str]:
    """Create a driver and return (record, token). The token is shown ONCE."""
    clean = (name or "").strip()[:_NAME_MAX]
    if not clean:
        raise CredentialError("a driver needs a name — it is stamped on their reports")

    path = store or DEFAULT_STORE
    records = load(path)

    key_id = secrets.token_hex(KEY_ID_BYTES)
    while key_id in records:                       # astronomically unlikely, cheap to rule out
        key_id = secrets.token_hex(KEY_ID_BYTES)

    secret = secrets.token_urlsafe(SECRET_BYTES)
    records[key_id] = {
        "name": clean,
        "carrier": (carrier or "").strip() or None,
        "created_at": now.isoformat(),
        "revoked_at": None,
        "secret_sha256": _hash(secret),
    }
    _write(path, records)

    driver = Driver(key_id, clean, records[key_id]["carrier"],
                    records[key_id]["created_at"])
    return driver, f"{key_id}.{secret}"


def revoke(key_id: str, now: datetime, store: Path | None = None) -> Driver:
    path = store or DEFAULT_STORE
    records = load(path)
    if key_id not in records:
        raise CredentialError(f"no driver with key {key_id}")
    if records[key_id].get("revoked_at"):
        raise CredentialError(f"{key_id} was already revoked")
    records[key_id]["revoked_at"] = now.isoformat()
    _write(path, records)
    record = records[key_id]
    return Driver(key_id, record["name"], record.get("carrier"),
                  record["created_at"], record["revoked_at"])


def verify(token: str | None, store: Path | None = None) -> Driver | None:
    """The driver this token belongs to, or None.

    None for every failure — malformed, unknown, revoked, wrong secret —
    because the caller's response must be identical in all of them. Telling
    an attacker which key ids exist turns guessing a token into guessing a
    secret for a key you know is real.
    """
    if not token or "." not in token:
        return None
    key_id, _, secret = token.partition(".")
    if not _KEY_ID_RE.match(key_id) or not secret:
        return None

    try:
        records = load(store)
    except CredentialError:
        return None

    record = records.get(key_id)
    if record is None:
        # Still do the work, so a missing key and a wrong secret take the
        # same time. The lookup above is a dict hit either way; this is the
        # expensive half.
        hmac.compare_digest(_hash(secret), "0" * 64)
        return None

    if not hmac.compare_digest(_hash(secret), record.get("secret_sha256", "")):
        return None
    if record.get("revoked_at"):
        return None

    return Driver(key_id, record["name"], record.get("carrier"),
                  record["created_at"], record.get("revoked_at"))


def _write(path: Path, records: dict) -> None:
    """Write the store, readable only by its owner.

    chmod before the content goes in, not after: a file that is briefly
    world-readable while it holds hashes is a file that was world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.touch(mode=0o600, exist_ok=True)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps(records, indent=1, sort_keys=True))
    temporary.replace(path)
    path.chmod(0o600)
