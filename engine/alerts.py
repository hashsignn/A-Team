"""Critical alerts by email: the board tells you when a route turns critical.

A planner is not always looking at the board. When a route newly turns
Critical (or Alert, if they ask for that too), or an early warning appears,
the server writes one short email: which route, by when, how much is at
risk, and a link straight to its decision tree. Sent to a phone, that is the
page that wakes someone up.

WHAT IS SENT, AND WHERE
-----------------------
Route names, deadlines, CHF at risk and order counts. Not customer names:
those stay on the board. The email goes through the planner's OWN mail
account (RADAR_SMTP_* in the environment, for example a Gmail app password),
over TLS with the certificate checked. With no mail server set, nothing
leaves the machine: the alert is recorded here and the settings panel says
it was recorded, not sent. The same rule as every other channel in this
codebase (engine/fast/dispatch.py): disabled is not silent.

WHAT COUNTS AS NEW
------------------
A route alerts once per level: when it first reaches a chosen level, and
again if it climbs (Alert to Critical). A route that calms down is forgotten,
so it alerts again if it comes back. Early warnings alert once each.

The settings and the record of what was sent live in config/ (gitignored),
because an email address is personal data and config/ is where this
codebase keeps what belongs to the customer.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode

from engine.clock import Clock
from engine.config import CUSTOMER_DIR

SETTINGS_PATH = CUSTOMER_DIR / "alerts.json"
STATE_PATH = CUSTOMER_DIR / "alerts_state.json"

LEVELS = {"red": "Critical", "yellow": "Alert"}
RANK = {"red": 0, "yellow": 1}
LOG_KEEP = 20
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LOCK = threading.Lock()


@dataclass
class Settings:
    email: str = ""
    levels: list[str] = field(default_factory=lambda: ["red"])
    early_warnings: bool = True
    enabled: bool = False
    # Where the links in the email point: the address the settings were
    # saved from, so a tap on the phone opens the same board.
    base_url: str = ""


@dataclass
class Receipt:
    status: str          # "sent" | "recorded" | "failed" | "queued" | "nothing"
    detail: str
    subject: str = ""
    to: str = ""
    at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- settings
def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    levels = [lv for lv in raw.get("levels") or [] if lv in LEVELS] or ["red"]
    return Settings(email=str(raw.get("email") or ""), levels=levels,
                    early_warnings=bool(raw.get("early_warnings", True)),
                    enabled=bool(raw.get("enabled")), base_url=str(raw.get("base_url") or ""))


def save_settings(data: dict, path: Path = SETTINGS_PATH) -> Settings:
    """Validate and store. Raises ValueError with a sentence to show."""
    email = str(data.get("email") or "").strip()
    enabled = bool(data.get("enabled", True))
    if enabled and not _EMAIL.match(email):
        raise ValueError("That does not look like an email address.")
    levels = [lv for lv in data.get("levels") or ["red"] if lv in LEVELS]
    if enabled and not levels:
        raise ValueError("Choose at least one level.")
    base = str(data.get("base_url") or "").strip().rstrip("/")
    if base and not re.match(r"^https?://", base):
        base = ""
    settings = Settings(email=email, levels=levels or ["red"],
                        early_warnings=bool(data.get("early_warnings", True)),
                        enabled=enabled, base_url=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=1), encoding="utf-8")
    return settings


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("routes", {})
    state.setdefault("warnings", [])
    state.setdefault("log", [])
    return state


def _save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["log"] = state["log"][-LOG_KEEP:]
    state["warnings"] = state["warnings"][-200:]
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def recent(path: Path = STATE_PATH, n: int = 5) -> list[dict]:
    return list(reversed(_load_state(path)["log"][-n:]))


# ---------------------------------------------------------------- mail server
def smtp_config() -> dict | None:
    """The planner's own mail server, from the environment. None when not
    set: then nothing is sent, and the alert is recorded instead."""
    host = os.environ.get("RADAR_SMTP_HOST", "").strip()
    if not host:
        return None
    user = os.environ.get("RADAR_SMTP_USER", "").strip()
    try:
        port = int(os.environ.get("RADAR_SMTP_PORT", "587").strip() or 587)
    except ValueError:
        port = 587
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": os.environ.get("RADAR_SMTP_PASSWORD", ""),
        "sender": os.environ.get("RADAR_SMTP_FROM", "").strip() or user,
    }


# ---------------------------------------------------------------- the email
def _chf(v: float | None) -> str:
    return "CHF –" if v is None else f"CHF {int(float(v) + 0.5):,}"


def _due(board: dict, r: dict) -> str:
    if r.get("clock_hours") is None:
        return "no option left: tell the customer now" if r.get("exposure_chf") else "no deadline"
    try:
        at = datetime.fromisoformat(board["as_of"]) + timedelta(hours=float(r["clock_hours"]))
    except (KeyError, TypeError, ValueError):
        return ""
    return f"decide by {at.strftime('%a %d %b, %H:%M UTC')}"


def _link(settings: Settings, board: dict, route_id: str) -> str:
    base = settings.base_url or os.environ.get("RADAR_PUBLIC_URL", "").rstrip("/") or "http://localhost:8000"
    # Encoded: a bare "+00:00" in a query string arrives as a space.
    return f"{base}/tree?{urlencode({'route': route_id, 'as_of': board['as_of']})}"


def _warnings(board: dict) -> list[tuple[str, str]]:
    out = []
    for p in (board.get("carrier_signals") or {}).get("patterns") or []:
        out.append((f"push:{p.get('carrier')}:{p.get('node')}:{str(p.get('last_noticed', ''))[:10]}",
                    f"{p.get('carrier_name')} pushed out {p.get('orders')} orders at {p.get('node_name')}, "
                    "no official notice yet"))
    for b in (board.get("order_signals") or {}).get("bursts") or []:
        out.append((f"burst:{b.get('flow')}:{b.get('day')}",
                    f"{b.get('orders')} small orders in one day on {b.get('flow')} (usual {b.get('usual')}): "
                    "a sign a crisis may be a week away"))
    return out


def compose(board: dict, settings: Settings, routes: list[dict],
            warnings: list[str], test: bool = False) -> EmailMessage | None:
    """One short email for the routes and warnings given. Plain text: it
    reads the same on every phone."""
    if not routes and not warnings:
        return None
    lines = [f"Horizon, {board.get('as_of_label', board.get('as_of', ''))}", ""]
    if test:
        lines += ["This is a test: alerts reach you like this.", ""]
    for level in ("red", "yellow"):
        mine = [r for r in routes if r.get("level") == level]
        if not mine:
            continue
        directive = next((lv["directive"] for lv in board.get("levels") or [] if lv["level"] == level), "")
        lines.append(f"{LEVELS[level].upper()}: {directive.lower()}")
        for r in mine:
            keys = sum(1 for c in r.get("customers") or [] if c.get("priority") == "A" and c.get("at_risk"))
            lines.append(f"• {r['name']}")
            lines.append(f"  {_due(board, r)} · {_chf(r.get('exposure_chf'))} at risk · "
                         f"{r.get('shipments_at_risk', 0)} of {r.get('shipments', 0)} orders"
                         + (f" · {keys} key account{'s' if keys != 1 else ''}" if keys else ""))
            event = next(iter(r.get("events") or []), None)
            if event:
                lines.append(f"  {event['title']}")
            lines.append(f"  Decision tree: {_link(settings, board, r['route_id'])}")
        lines.append("")
    if warnings:
        lines.append("EARLY WARNING")
        lines += [f"• {w}" for w in warnings]
        lines.append("")
    lines.append(f"You get this because alerts are on for {settings.email}. "
                 "Change or stop them on the board: the bell in the header.")

    counts = [f"{sum(1 for r in routes if r.get('level') == lv)} {LEVELS[lv]}"
              for lv in ("red", "yellow") if any(r.get("level") == lv for r in routes)]
    head = ", ".join(counts) if counts else f"{len(warnings)} early warning{'s' if len(warnings) != 1 else ''}"
    first = routes[0]["name"] if routes else warnings[0]
    subject = f"[Radar{' test' if test else ''}] {head}: {first[:70]}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = settings.email
    msg.set_content("\n".join(lines))
    return msg


# ---------------------------------------------------------------- sending
def send(msg: EmailMessage, settings: Settings) -> Receipt:
    """Through the planner's own mail server, or recorded when none is set.
    Never raises: a mail server that is down must not break the board."""
    # When it went out: the one real-time fact here, read the one way
    # this codebase reads the wall clock (engine/clock.py).
    now = Clock.wall().as_of.isoformat(timespec="seconds")
    smtp = smtp_config()
    if smtp is None:
        return Receipt("recorded", "No mail server set (RADAR_SMTP_HOST): recorded here, not sent.",
                       msg["Subject"], settings.email, now)
    msg["From"] = smtp["sender"] or settings.email
    try:
        context = ssl.create_default_context()   # certificates checked, always
        implicit = smtp["port"] == 465
        server = (smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=15, context=context) if implicit
                  else smtplib.SMTP(smtp["host"], smtp["port"], timeout=15))
        with server:
            if not implicit:
                server.starttls(context=context)
            if smtp["user"]:
                server.login(smtp["user"], smtp["password"])
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as err:
        return Receipt("failed", f"The mail server said: {err}", msg["Subject"], settings.email, now)
    return Receipt("sent", f"Sent to {settings.email}.", msg["Subject"], settings.email, now)


def _log(state: dict, receipt: Receipt) -> None:
    state["log"].append(receipt.as_dict())


# ---------------------------------------------------------------- the check
def due(board: dict, settings: Settings, state: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    """What is new since the last alert, and the state updated to match."""
    routes = []
    seen = state["routes"]
    current = {}
    for r in board.get("routes") or []:
        level = r.get("level")
        if level not in settings.levels:
            continue
        current[r["route_id"]] = level
        before = seen.get(r["route_id"])
        if before is None or RANK.get(level, 9) < RANK.get(before, 9):
            routes.append(r)
    # Forget the ones that calmed down, so they alert again if they return.
    state["routes"] = current
    warnings = []
    if settings.early_warnings:
        known = set(state["warnings"])
        for key, text in _warnings(board):
            if key not in known:
                warnings.append((key, text))
                state["warnings"].append(key)
    routes.sort(key=lambda r: (RANK.get(r.get("level"), 9), r.get("lead_time_hours") or 1e9))
    return routes, warnings


def on_board(board: dict, settings_path: Path = SETTINGS_PATH, state_path: Path = STATE_PATH,
             background: bool = True) -> Receipt | None:
    """After a new board is built: email what turned critical. Returns at
    once; the mail goes out on its own thread so the board is never slowed
    by a mail server."""
    settings = load_settings(settings_path)
    if not settings.enabled or not settings.email:
        return None
    with _LOCK:
        state = _load_state(state_path)
        # Only a board at or after the latest one checked can alert: going
        # back in time on the slider replays history, it does not page you.
        if str(board.get("as_of", "")) < str(state.get("latest_as_of", "")):
            return None
        state["latest_as_of"] = str(board.get("as_of", ""))
        routes, warnings = due(board, settings, state)
        msg = compose(board, settings, routes, [w for _, w in warnings])
        _save_state(state, state_path)
    if msg is None:
        return None

    def deliver() -> Receipt:
        receipt = send(msg, settings)
        with _LOCK:
            state = _load_state(state_path)
            _log(state, receipt)
            _save_state(state, state_path)
        return receipt

    if background:
        threading.Thread(target=deliver, daemon=True).start()
        return Receipt("queued", "Checking the board; the email is on its way.", msg["Subject"], settings.email)
    return deliver()


def test(board: dict, settings_path: Path = SETTINGS_PATH, state_path: Path = STATE_PATH) -> Receipt:
    """A test email now, with what is on the board right now."""
    settings = load_settings(settings_path)
    if not settings.email:
        return Receipt("nothing", "Save an email address first.")
    routes = sorted([r for r in board.get("routes") or [] if r.get("level") in settings.levels],
                    key=lambda r: (RANK.get(r.get("level"), 9), r.get("lead_time_hours") or 1e9))
    warnings = [w for _, w in _warnings(board)] if settings.early_warnings else []
    msg = compose(board, settings, routes, warnings, test=True)
    if msg is None:
        msg = EmailMessage()
        msg["Subject"] = "[Radar test] Nothing critical right now"
        msg["To"] = settings.email
        msg.set_content("This is a test: alerts reach you like this. Nothing is critical on the board now.")
    receipt = send(msg, settings)
    with _LOCK:
        state = _load_state(state_path)
        _log(state, receipt)
        _save_state(state, state_path)
    return receipt
