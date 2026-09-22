"""Orchestration: getting the decision out of the building.

An executed action is worth nothing until the people who have to carry it out
know about it. This module is the fan-out: one call, several channels, and a
receipt for each so the planner can see who was actually reached rather than
assuming.

THREE KINDS OF CHANNEL
----------------------
    socket    in-process fan-out onto the bus, which the browser reads over
              SSE. No egress, always available, and how the driver's phone and
              the planner's screen learn about each other.
    webhook   an HTTP POST to a URL held in an ENVIRONMENT VARIABLE. The
              config file names the variable; it never holds the value.
    api       the same mechanism with a carrier's or authority's contract
              layered on top. Separated from `webhook` because the audiences
              differ and a planner needs to see which one failed.

DISABLED IS NOT SILENT
----------------------
Every channel ships disabled. A disabled channel RECORDS the message it would
have sent and reports ``skipped`` with the reason, exactly like the rest of
this codebase treats an absent feed. The failure mode being avoided is a demo
where nothing is wired, everything says "sent", and the first real incident is
the one that discovers it.

NOTHING HERE RAISES
-------------------
A dead webhook must not be able to stop an executed reroute. Every failure
becomes a receipt with ``ok: False`` and a reason.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from engine.config import Config
from engine.fast.bus import BUS, TOPIC_ACTION, Bus

# A dispatch that has not answered in this many seconds is treated as failed.
# Short deliberately: the planner is waiting, and a slow integration must not
# become their problem.
TIMEOUT_SECONDS = 4.0

# Egress is opt-in, exactly as it is for source fetching. Without it, every
# outbound channel records instead of sending, and the whole app runs offline.
ALLOW_NETWORK_ENV = "RADAR_ALLOW_NETWORK"


@dataclass(frozen=True)
class Receipt:
    """What happened on one channel."""

    channel_id: str
    kind: str
    audience: tuple[str, ...]
    ok: bool
    status: str          # "delivered" | "recorded" | "failed"
    detail: str
    recorded: dict | None = None

    def as_dict(self) -> dict:
        return {
            "channel": self.channel_id,
            "kind": self.kind,
            "audience": list(self.audience),
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Channel:
    channel_id: str
    kind: str
    enabled: bool
    audience: tuple[str, ...]
    url_env: str | None = None


@dataclass
class Outbox:
    """What would have gone out. The audit trail for an offline run."""

    messages: list[dict] = field(default_factory=list)

    def record(self, channel_id: str, body: dict) -> dict:
        entry = {"channel": channel_id, "body": body}
        self.messages.append(entry)
        return entry

    def clear(self) -> None:
        self.messages.clear()


OUTBOX = Outbox()


def channels(config: Config) -> list[Channel]:
    raw = (config.raw("fast") or {}).get("dispatch", {}) or {}
    out: list[Channel] = []
    for entry in raw.get("channels", []) or []:
        out.append(
            Channel(
                channel_id=str(entry.get("id", "unnamed")),
                kind=str(entry.get("kind", "webhook")),
                enabled=bool(entry.get("enabled", False)),
                audience=tuple(entry.get("audience", []) or []),
                url_env=entry.get("url_env"),
            )
        )
    return out


def undo_window_seconds(config: Config) -> int:
    raw = (config.raw("fast") or {}).get("dispatch", {}) or {}
    value = raw.get("undo_window_seconds", 900)
    return int(value) if isinstance(value, (int, float)) else 900


def _network_allowed() -> bool:
    return os.environ.get(ALLOW_NETWORK_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _post(url: str, body: dict) -> tuple[bool, str]:
    """One HTTP path, and it never raises."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "supply-chain-risk-radar/fast"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            code = response.getcode()
            if 200 <= code < 300:
                return True, f"HTTP {code}"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"unreachable — {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return False, f"failed — {exc}"


def _deliver(channel: Channel, body: dict, bus: Bus) -> Receipt:
    if channel.kind == "socket":
        if not channel.enabled:
            return Receipt(
                channel.channel_id, channel.kind, channel.audience,
                ok=False, status="recorded",
                detail="channel disabled in fast.yaml",
                recorded=OUTBOX.record(channel.channel_id, body),
            )
        bus.publish(TOPIC_ACTION, body, at=str(body.get("at", "")))
        return Receipt(
            channel.channel_id, channel.kind, channel.audience,
            ok=True, status="delivered",
            detail=f"on the wire to {', '.join(channel.audience) or 'listeners'}",
        )

    if not channel.enabled:
        return Receipt(
            channel.channel_id, channel.kind, channel.audience,
            ok=False, status="recorded",
            detail="channel disabled in fast.yaml",
            recorded=OUTBOX.record(channel.channel_id, body),
        )

    url = os.environ.get(channel.url_env or "", "").strip()
    if not url:
        return Receipt(
            channel.channel_id, channel.kind, channel.audience,
            ok=False, status="recorded",
            detail=f"{channel.url_env} is not set",
            recorded=OUTBOX.record(channel.channel_id, body),
        )

    if not _network_allowed():
        return Receipt(
            channel.channel_id, channel.kind, channel.audience,
            ok=False, status="recorded",
            detail=f"egress is off — set {ALLOW_NETWORK_ENV}=1 to send",
            recorded=OUTBOX.record(channel.channel_id, body),
        )

    ok, detail = _post(url, body)
    return Receipt(
        channel.channel_id, channel.kind, channel.audience,
        ok=ok, status="delivered" if ok else "failed", detail=detail,
        recorded=None if ok else OUTBOX.record(channel.channel_id, body),
    )


def fan_out(
    config: Config,
    body: dict,
    audiences: set[str] | None = None,
    bus: Bus | None = None,
) -> list[Receipt]:
    """Send one message to every channel whose audience is wanted.

    ``audiences=None`` means everyone. Passing a set narrows it — telling the
    drivers about a reroute does not mean telling the authorities.
    """
    bus = bus or BUS
    out: list[Receipt] = []
    for channel in channels(config):
        if audiences is not None and not (set(channel.audience) & audiences):
            continue
        out.append(_deliver(channel, body, bus))
    return out


def summarise(receipts: list[Receipt]) -> dict:
    """One line a planner can read without counting rows."""
    delivered = [r for r in receipts if r.status == "delivered"]
    recorded = [r for r in receipts if r.status == "recorded"]
    failed = [r for r in receipts if r.status == "failed"]

    if not receipts:
        sentence = "No channel matched this audience."
    elif failed:
        sentence = (
            f"{len(delivered)} delivered, {len(failed)} failed — "
            + "; ".join(f"{r.channel_id}: {r.detail}" for r in failed)
        )
    elif delivered and not recorded:
        sentence = f"Delivered on {len(delivered)} channel(s)."
    elif delivered:
        sentence = (
            f"Delivered on {len(delivered)}; {len(recorded)} recorded but not "
            "sent because the channel is not wired."
        )
    else:
        sentence = (
            f"Nothing left the building: {len(recorded)} channel(s) recorded "
            "the message. Wire one to make this a real send."
        )

    return {
        "delivered": len(delivered),
        "recorded": len(recorded),
        "failed": len(failed),
        "sentence": sentence,
        "receipts": [r.as_dict() for r in receipts],
    }
