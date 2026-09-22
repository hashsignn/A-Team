"""Instant execution, with an undo instead of a checklist.

The old flow made the planner walk Detect → Confirm → Act → Close out before a
reroute would unlock. Every step was defensible on its own and the sequence was
indefensible: it put four clicks and a phone call between the alert and the
action, on a product whose entire claim is speed.

So the gate is gone. ``execute`` runs on the first click, dispatches, and
returns. Nothing is asked first.

WHAT REPLACES THE GATE
----------------------
An undo window. For a configurable period after execution the action can be
pulled back in one click, and the retraction is dispatched on the same channels
that carried the original. This is strictly better than a pre-confirmation for
the case that actually happens — a planner acting on a report that turns out to
be wrong — because it costs nothing when the report was right, which is most of
the time, and the checklist charged its full price every single time.

It is not free, and the honest statement of the trade is: executing on an
unconfirmed signal can commit spend against something that did not happen.
That is why ``confidence`` rides on every execution and is shown next to it,
why a low-confidence trigger is labelled rather than blocked, and why the undo
window exists at all. The planner is being trusted with the decision, which is
the right place for it — but they are being told what they are trusting.

WHAT IS STILL REFUSED
---------------------
Two things, and neither is a checklist:

    an option we do not own    a carrier's decision cannot be "executed" by us
                               however fast the button is
    an option that was vetoed  a loss-making route does not become viable
                               because somebody clicked it

Both are refusals with a reason, returned in the same shape as a success, so
the UI never has to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.config import Config
from engine.fast import dispatch as dispatch_mod
from engine.fast.bus import BUS, TOPIC_UNDO, Bus
from engine.fast.options import FastOption

# Who hears about an executed action, by option kind. Authorities are not on
# this list: notifying a public body is a decision with legal weight and it is
# not something a planner should be able to trigger by accident from a route
# page. It is available explicitly, through `audiences=`.
AUDIENCE_BY_KIND: dict[str, set[str]] = {
    "reroute": {"driver", "ground_ops", "carrier", "planner"},
    "template": {"ground_ops", "carrier", "planner"},
    "local": {"ground_ops", "planner"},
}
DEFAULT_AUDIENCE = {"planner"}


@dataclass
class Execution:
    """One action that was actually set running."""

    execution_id: str
    shipment_id: str
    option_id: str
    label: str
    kind: str
    cost_chf: float
    executed_at: datetime
    undo_until: datetime
    confidence: str
    trigger: str
    by: str
    dispatch: dict = field(default_factory=dict)
    undone_at: datetime | None = None
    undo_reason: str | None = None

    @property
    def undone(self) -> bool:
        return self.undone_at is not None

    def undoable_at(self, now: datetime) -> bool:
        return not self.undone and now < self.undo_until

    def as_dict(self, now: datetime | None = None) -> dict:
        payload = {
            "execution_id": self.execution_id,
            "shipment_id": self.shipment_id,
            "option_id": self.option_id,
            "label": self.label,
            "kind": self.kind,
            "cost_chf": round(self.cost_chf, 2),
            "executed_at": self.executed_at.isoformat(),
            "undo_until": self.undo_until.isoformat(),
            "confidence": self.confidence,
            "trigger": self.trigger,
            "by": self.by,
            "undone": self.undone,
            "undone_at": self.undone_at.isoformat() if self.undone_at else None,
            "undo_reason": self.undo_reason,
            "dispatch": self.dispatch,
        }
        if now is not None:
            payload["undoable"] = self.undoable_at(now)
            payload["undo_seconds_left"] = max(
                0, int((self.undo_until - now).total_seconds())
            ) if self.undoable_at(now) else 0
        return payload


@dataclass(frozen=True)
class Refusal:
    """A no, with the reason, in the same shape as a yes."""

    ok: bool
    reason: str
    detail: str

    def as_dict(self) -> dict:
        return {"ok": self.ok, "refused": self.reason, "detail": self.detail}


class Ledger:
    """Everything that has been executed this session.

    In memory on purpose: it is a live operational record, not the audit
    trail. The audit trail is the dispatch outbox and the report log, both of
    which survive a restart.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Execution] = {}
        self._order: list[str] = []

    def add(self, execution: Execution) -> Execution:
        # An id that is already here is the SAME action booked twice in the
        # same second. Keep the first and hand it back: two ledger rows with
        # one id leaves the second one impossible to undo.
        existing = self._rows.get(execution.execution_id)
        if existing is not None:
            return existing
        self._rows[execution.execution_id] = execution
        self._order.append(execution.execution_id)
        return execution

    def get(self, execution_id: str) -> Execution | None:
        return self._rows.get(execution_id)

    def for_shipment(self, shipment_id: str) -> list[Execution]:
        return [
            self._rows[eid] for eid in self._order
            if self._rows[eid].shipment_id == shipment_id
        ]

    def recent(self, limit: int = 20) -> list[Execution]:
        return [self._rows[eid] for eid in reversed(self._order)][:limit]

    def active_option_ids(self) -> set[str]:
        return {e.option_id for e in self._rows.values() if not e.undone}

    def clear(self) -> None:
        self._rows.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._rows)


LEDGER = Ledger()


def execute(
    option: FastOption,
    config: Config,
    now: datetime,
    *,
    by: str = "planner",
    trigger: str = "manual",
    confidence: str = "reported",
    ledger: Ledger | None = None,
    bus: Bus | None = None,
    audiences: set[str] | None = None,
) -> Execution | Refusal:
    """Run it. No confirmation, no checklist, no second screen."""
    ledger = ledger if ledger is not None else LEDGER

    if option.expired:
        return Refusal(
            False, "expired",
            option.expired_reason or "this option is no longer available",
        )
    if not option.executable:
        return Refusal(
            False, "not ours",
            f"{option.owner} owns this lever — we can ask, not execute. "
            f"Use the contacts on the option.",
        )
    if option.cost_chf > 0 and not option.margin.viable:
        return Refusal(
            False, "vetoed",
            option.margin.reason or "this option does not leave a margin",
        )

    window = dispatch_mod.undo_window_seconds(config)
    execution = Execution(
        execution_id=f"EX:{option.option_id}:{int(now.timestamp())}",
        shipment_id=option.shipment_id,
        option_id=option.option_id,
        label=option.label,
        kind=option.kind,
        cost_chf=option.cost_chf,
        executed_at=now,
        undo_until=now + timedelta(seconds=window),
        confidence=confidence,
        trigger=trigger,
        by=by,
    )

    body = {
        "type": "action.executed",
        "at": now.isoformat(),
        "execution_id": execution.execution_id,
        "shipment_id": option.shipment_id,
        "action": option.label,
        "kind": option.kind,
        "detail": option.detail,
        "route": list(option.route),
        "hours_to_resolve": option.hours_to_resolve,
        "days_late_after": option.days_late_after,
        "cost_chf": option.cost_chf,
        "confidence": confidence,
        "by": by,
        "undo_until": execution.undo_until.isoformat(),
    }
    receipts = dispatch_mod.fan_out(
        config, body,
        audiences=audiences or AUDIENCE_BY_KIND.get(option.kind, DEFAULT_AUDIENCE),
        bus=bus,
    )
    execution.dispatch = dispatch_mod.summarise(receipts)

    return ledger.add(execution)


def undo(
    execution_id: str,
    config: Config,
    now: datetime,
    *,
    reason: str = "pulled back by the planner",
    ledger: Ledger | None = None,
    bus: Bus | None = None,
) -> Execution | Refusal:
    """Pull an executed action back, inside its window.

    Outside the window it is refused rather than silently accepted: once a
    carrier has moved a vehicle, saying "undone" in our UI and nothing else
    would be a lie the planner then acts on.
    """
    ledger = ledger if ledger is not None else LEDGER
    bus = bus or BUS

    execution = ledger.get(execution_id)
    if execution is None:
        return Refusal(False, "unknown", f"no execution {execution_id!r}")
    if execution.undone:
        return Refusal(
            False, "already undone",
            f"pulled back at {execution.undone_at.isoformat()}"
            if execution.undone_at else "already pulled back",
        )
    if now >= execution.undo_until:
        return Refusal(
            False, "window closed",
            "the undo window has passed — this is now a phone call to the "
            "carrier, not a click",
        )

    execution.undone_at = now
    execution.undo_reason = reason

    body = {
        "type": "action.undone",
        "at": now.isoformat(),
        "execution_id": execution.execution_id,
        "shipment_id": execution.shipment_id,
        "action": execution.label,
        "reason": reason,
    }
    receipts = dispatch_mod.fan_out(
        config, body,
        audiences=AUDIENCE_BY_KIND.get(execution.kind, DEFAULT_AUDIENCE),
        bus=bus,
    )
    execution.dispatch = dispatch_mod.summarise(receipts)
    bus.publish(TOPIC_UNDO, body, at=now.isoformat())

    return execution
