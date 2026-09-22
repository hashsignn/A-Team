"""The operational flow — Detect, Confirm, Act, Close.

NOT THE SAME THING AS playbook.py
=================================
``playbook.py`` answers "what COULD I do" — it generates action options with
costs, durations and residual fractions, and the Monte Carlo prices them.

This module answers "what do I do FIRST, and what am I not allowed to do
yet". It is the procedure around the options, and its whole value is the
word *yet*.

THE GATE IS THE FEATURE
-----------------------
    "Do not reroute until the disruption has been confirmed."

That sentence is the entire point, and it has to be enforced rather than
printed. A rule that appears as advisory text above a button that still
works is not a control — it is a caption. So `unlocked_actions()` withholds
the mitigations until the confirming tasks are ticked, and the board's action
list is filtered through it.

Why it matters here specifically: this tool reads trade press and (per Q5)
social media. Those are *early* signals, which is their value, and early
signals are wrong more often than late ones. Rerouting a barge on an
unconfirmed rumour costs real money and burns the credibility that makes
anyone act on the next alert. The gate is what lets the tool be early
without being reckless.

CORROBORATION IS PART OF THE GATE, NOT A SEPARATE IDEA
------------------------------------------------------
An event whose only source is uncorroborated tier 3 cannot be confirmed by
ticking a box, because nothing about ticking a box makes a rumour true. The
flow says so and names what would resolve it — a carrier callback, an
authority notice — which is also the task list a planner would have written
themselves.

WHO HOLDS THE STATE
-------------------
The engine owns the DEFINITION and the GATE, both deterministic and testable.
The browser owns the TICKS, because "has Maria called the carrier yet" is
per-planner working state, not a fact about the world, and putting it in the
engine would mean the board's answer depended on who was looking at it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from engine.config import Config
from engine.score.severity import Level

# Actions that change where the freight goes. These are the ones the gate
# withholds: notifying a customer or asking a carrier for status is always
# safe and always allowed, but committing to a different routing on an
# unconfirmed report is the expensive mistake.
ROUTING_ACTION_TYPES = frozenset({
    "sea_reroute", "sea_rebook", "rail_reroute", "rail_rebook",
    "barge_to_rail_switch", "barge_part_load", "road_reroute",
    "reserve_capacity", "expedite",
})


class Stage(str, Enum):
    DETECT = "detect"
    CONFIRM = "confirm"
    ACT = "act"
    CLOSE = "close"


STAGE_ORDER = [Stage.DETECT, Stage.CONFIRM, Stage.ACT, Stage.CLOSE]

STAGE_TITLE = {
    Stage.DETECT: "Detect",
    Stage.CONFIRM: "Confirm",
    Stage.ACT: "Act",
    Stage.CLOSE: "Close out",
}

STAGE_RULE = {
    Stage.DETECT: (
        "A disruption has been detected. Confirm it before changing the route."
    ),
    Stage.CONFIRM: (
        "Do not reroute until the disruption has been confirmed."
    ),
    Stage.ACT: (
        "Confirmed. Mitigations are unlocked — take the one that is worth "
        "more than it costs, before its deadline."
    ),
    Stage.CLOSE: (
        "Record what was done and what it cost, so the next hindcast can "
        "measure whether it worked."
    ),
}


@dataclass(frozen=True)
class Task:
    """One line on the checklist.

    ``required`` is what makes it a gate rather than a suggestion: an
    unticked required task in Confirm keeps the routing actions locked.
    """

    id: str
    stage: Stage
    label: str
    owner: str
    owner_contact: str | None
    sla_hours: float | None
    note: str
    required: bool = False
    blocked_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "label": self.label,
            "owner": self.owner,
            "owner_contact": self.owner_contact,
            "sla_hours": self.sla_hours,
            "note": self.note,
            "required": self.required,
            "blocked_reason": self.blocked_reason,
        }


@dataclass
class FlowState:
    tasks: list[Task] = field(default_factory=list)
    stage: Stage = Stage.DETECT
    completed: set[str] = field(default_factory=set)
    gate_open: bool = False
    gate_reason: str = ""
    progress: float = 0.0
    # task id -> report id, for the ones a driver answered rather than the
    # planner. Shown differently, because "already done" and "done by
    # somebody in a tunnel two minutes ago" are different facts.
    from_reports: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "stage_title": STAGE_TITLE[self.stage],
            "stage_rule": STAGE_RULE[self.stage],
            "stages": [
                {"stage": s.value, "title": STAGE_TITLE[s],
                 "reached": STAGE_ORDER.index(s) <= STAGE_ORDER.index(self.stage)}
                for s in STAGE_ORDER
            ],
            "tasks": [t.as_dict() for t in self.tasks],
            "completed": sorted(self.completed),
            "gate_open": self.gate_open,
            "gate_reason": self.gate_reason,
            "progress": round(self.progress, 3),
            "from_reports": dict(self.from_reports),
        }


# =====================================================================
# Building the flow for one route
# =====================================================================


def _owner(config: Config, team_id: str) -> tuple[str, str | None, float | None]:
    """Resolve a team id to a name, a contact and its response SLA.

    Reads contacts.yaml rather than hardcoding, so the SLAs on screen are the
    ones the organisation actually agreed. A playbook quoting invented
    response times is a playbook nobody is held to.
    """
    for team in config.contacts.get("internal", []):
        if team["id"] == team_id:
            return (
                team.get("function", team_id),
                team.get("contact"),
                team.get("response_sla_hours"),
            )
    return team_id, None, None


def build(
    config: Config,
    route: dict,
    source_tiers: list[int] | None = None,
) -> list[Task]:
    """The checklist for one route, from its actual state.

    Tasks name the real corridor owner and the real teams, and their
    deadlines come from the event rather than from a template — the point of
    generating this rather than shipping a static PDF.
    """
    level = Level(route["level"])
    manager = (route.get("response") or {}).get("route_manager") or {}
    manager_name = manager.get("name", "the route owner")
    manager_contact = manager.get("email") or manager.get("phone")

    sc_name, sc_contact, sc_sla = _owner(config, "FN_SUPPLY_CHAIN")
    proc_name, proc_contact, proc_sla = _owner(config, "FN_PROCUREMENT")
    cs_name, cs_contact, cs_sla = _owner(config, "FN_CUSTOMER_SERVICE")

    # How long the confirming steps have. Tied to the rung, because the rung
    # IS a deadline: at Critical there is no six-hour callback window.
    confirm_sla = {
        Level.RED: 1.0, Level.YELLOW: 2.0, Level.BLUE: 8.0,
        Level.WHITE: 24.0, Level.GREEN: 24.0,
    }[level]

    corroborated, corroboration_note = _corroboration(config, source_tiers)

    tasks: list[Task] = [
        # ---- DETECT -------------------------------------------------
        Task(
            "detect.read", Stage.DETECT,
            "Read the event and what it touches",
            manager_name, manager_contact, confirm_sla,
            f"{route['reason']}",
            required=True,
        ),
        Task(
            "detect.scope", Stage.DETECT,
            "Check which consignments are affected",
            manager_name, manager_contact, confirm_sla,
            f"{route['shipments_at_risk']} of the shipments on this lane are "
            "in scope — they do not all have the same deadline.",
            required=True,
        ),
        # ---- CONFIRM ------------------------------------------------
        Task(
            "confirm.carrier", Stage.CONFIRM,
            "Confirm the disruption with the carrier",
            manager_name, manager_contact, confirm_sla,
            "The carrier is the operator and knows before any feed does.",
            required=True,
            blocked_reason=None if corroborated else corroboration_note,
        ),
        Task(
            "confirm.position", Stage.CONFIRM,
            "Confirm current position and status",
            manager_name, manager_contact, confirm_sla,
            "Request the GPS / AIS position. No AIS feed is connected, so "
            "this is a phone call today.",
            required=True,
        ),
        Task(
            "confirm.eta", Stage.CONFIRM,
            "Record the revised ETA",
            sc_name, sc_contact, sc_sla,
            "The revised ETA is what every number below is recomputed from.",
            required=True,
        ),
        # ---- ACT ----------------------------------------------------
        Task(
            "act.choose", Stage.ACT,
            "Take the mitigation worth more than it costs",
            manager_name, manager_contact, _hours(route.get("lead_time_hours")),
            "Only options with a positive value of acting are listed. "
            "Options the cargo may not legally take are removed, not ranked low.",
            required=True,
        ),
        Task(
            "act.capacity", Stage.ACT,
            "Secure the replacement capacity",
            proc_name, proc_contact, proc_sla,
            "An agreed reroute with no booked slot is not a mitigation.",
        ),
        Task(
            "act.customer", Stage.ACT,
            "Tell the customer before they ask",
            cs_name, cs_contact, cs_sla,
            "Always allowed, at every stage. Telling a customer early costs "
            "nothing and is the one action that never needs confirming.",
        ),
        # ---- CLOSE --------------------------------------------------
        Task(
            "close.record", Stage.CLOSE,
            "Record what was done and what it cost",
            sc_name, sc_contact, None,
            "Without this the hindcast cannot measure whether acting helped, "
            "and the value claim stays a promise.",
        ),
        Task(
            "close.review", Stage.CLOSE,
            "Note anything the tool got wrong",
            manager_name, manager_contact, None,
            "A missed event or a false alarm is worth more than a correct "
            "one — it is the only way the ledger improves.",
        ),
    ]
    return tasks


def _hours(value) -> float | None:
    return float(value) if value is not None else None


def _corroboration(
    config: Config, source_tiers: list[int] | None
) -> tuple[bool, str]:
    """Can this be confirmed at all yet?

    Nothing about ticking a box makes a rumour true. An event whose only
    source is uncorroborated tier 3 has to be corroborated FIRST, and the
    flow names what would do it rather than leaving a planner to guess.
    """
    if not source_tiers:
        return True, ""
    spec = config.scoring.get("corroboration", {})
    authoritative = int(spec.get("authoritative_tier_at_or_below", 2))
    needed = int(spec.get("tier3_sources_for_corroboration", 2))

    if any(t <= authoritative for t in source_tiers):
        return True, ""
    low = [t for t in source_tiers if t > authoritative]
    if len(low) >= needed:
        return True, ""
    return False, (
        f"Only {len(low)} uncorroborated tier-{min(low)} source. Confirming "
        "needs a carrier callback or an authority notice — ticking this box "
        "would not make the report true."
    )


# =====================================================================
# The gate
# =====================================================================


# Which confirming tasks a field report answers.
#
# THIS IS THE LOOP CLOSING. The planner's checklist says "confirm the
# disruption with the carrier" and "request the GPS position"; a driver with
# the freight in front of them answers both from their phone in ten seconds.
#
# The alternative — a planner phoning a carrier who phones a vessel — is the
# delay the client named in Q6, expressed as a process rather than a feeling.
REPORT_SATISFIES = {
    # Any report at all proves somebody looked, and carries where they were.
    "confirm.position": lambda r: bool(r.position) or r.status in (
        "queued", "held", "stopped", "moving", "delivered"
    ),
    # A revised ETA is exactly what this task asks for.
    "confirm.eta": lambda r: r.revised_eta is not None,
    # Only an explicit confirmation counts here, and only from somebody who
    # could SEE it.
    #
    # Two separate guards, for two separate mistakes:
    #
    # A driver saying "moving" does not confirm a disruption — it is evidence
    # AGAINST one, and treating a status as a confirmation would unlock a
    # reroute on good news.
    #
    # And a relayed account does not confirm one either. The app is for
    # anyone on site, which is right — the driver in the cab, the agent at the
    # quay, the person at the terminal gate are all looking at the freight.
    # But somebody passing on what they were told is not, and this gate is the
    # one place in the system where that distinction has teeth: it is what
    # sends freight the long way round at somebody's expense. The report is
    # still stored, still shown, still tier 2. It just cannot move the money
    # on its own.
    "confirm.carrier": lambda r: r.confirms_disruption and r.first_hand,
}


def satisfied_by_reports(tasks: list[Task], reports: list) -> dict[str, str]:
    """Task ids a field report has already answered, and which report did it.

    Returned rather than merged into ``completed`` so the UI can show these
    as ticked BY SOMEBODY ELSE — a planner seeing a box already checked needs
    to know a driver checked it, not wonder whether they did.
    """
    out: dict[str, str] = {}
    known = {t.id for t in tasks}
    for report in sorted(reports, key=lambda r: r.observed_at):
        for task_id, answers in REPORT_SATISFIES.items():
            if task_id in known and answers(report):
                out[task_id] = report.report_id
    return out


def evaluate(
    tasks: list[Task],
    completed: set[str] | None = None,
    reports: list | None = None,
) -> FlowState:
    """Where the flow is, and whether the routing actions are unlocked.

    Pure: same tasks, same ticks and same reports give the same answer every
    time, with no reference to a clock or a store. That is what makes the
    gate testable and keeps two planners looking at one route from seeing
    different stages.

    ``reports`` are field reports already filtered to this route AND to the
    board's as-of by the caller. Passing them in rather than reading them
    here keeps this function pure and keeps the as-of discipline at the one
    boundary that owns it.
    """
    completed = set(completed or ())
    from_reports = satisfied_by_reports(tasks, reports or [])
    # A task a driver has answered IS done. Requiring the planner to also
    # tick it would mean the gate stays shut while the evidence sits on the
    # screen, which is precisely the "we declare too late" failure.
    completed |= set(from_reports)
    # A tick for a task that does not exist is ignored rather than trusted.
    known = {t.id for t in tasks}
    completed &= known

    required_by_stage: dict[Stage, list[Task]] = {}
    for task in tasks:
        if task.required:
            required_by_stage.setdefault(task.stage, []).append(task)

    # A stage is current until ITS required tasks are done. A stage with no
    # required tasks at all would be skipped entirely, which is almost never
    # what anybody means by putting it on a checklist — so it holds until all
    # of its tasks are ticked instead.
    by_stage: dict[Stage, list[Task]] = {}
    for task in tasks:
        by_stage.setdefault(task.stage, []).append(task)

    stage = Stage.DETECT
    for candidate in STAGE_ORDER:
        needed = required_by_stage.get(candidate) or by_stage.get(candidate, [])
        if all(t.id in completed for t in needed):
            index = STAGE_ORDER.index(candidate)
            if index + 1 < len(STAGE_ORDER):
                stage = STAGE_ORDER[index + 1]
            else:
                stage = candidate
        else:
            stage = candidate
            break

    confirm_required = required_by_stage.get(Stage.CONFIRM, [])
    outstanding = [t for t in confirm_required if t.id not in completed]
    blocked = [t for t in confirm_required if t.blocked_reason]

    if blocked:
        gate_open = False
        gate_reason = blocked[0].blocked_reason or "Not yet corroborated."
    elif outstanding:
        gate_open = False
        gate_reason = (
            "Rerouting is locked until the disruption is confirmed — "
            f"{len(outstanding)} step(s) outstanding: "
            + ", ".join(t.label.lower() for t in outstanding)
            + "."
        )
    else:
        gate_open = True
        by_field = [t for t in confirm_required if t.id in from_reports]
        if by_field:
            gate_reason = (
                f"Confirmed — {len(by_field)} step(s) answered by a field "
                "report from the freight itself, not from a feed. Rerouting "
                "is unlocked."
            )
        else:
            gate_reason = (
                "Confirmed. Rerouting is unlocked. Notifying the customer was "
                "available throughout."
            )

    return FlowState(
        tasks=tasks,
        stage=stage,
        completed=completed,
        gate_open=gate_open,
        gate_reason=gate_reason,
        progress=(len(completed) / len(tasks)) if tasks else 0.0,
        from_reports=from_reports,
    )


def unlocked_actions(actions: list[dict], state: FlowState) -> list[dict]:
    """Filter a route's action list through the gate.

    Routing actions are WITHHELD, not greyed: an option a planner can see and
    click is an option they will click. Each withheld action carries the
    reason, so the list does not simply appear short.

    Notify-and-ask actions pass at every stage. Telling a customer early
    costs nothing and is the one action that never needs confirming.
    """
    if state.gate_open:
        return [dict(a, locked=False, locked_reason=None) for a in actions]

    out = []
    for action in actions:
        routing = action.get("action_type") in ROUTING_ACTION_TYPES
        out.append(dict(
            action,
            locked=routing,
            locked_reason=state.gate_reason if routing else None,
        ))
    return out
