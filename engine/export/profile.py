"""The planner's risk profile — what the engine believes about this desk.

The sibling operational-risk radar has a profile page describing the
institution, so the engine can judge "could this have happened here". Its tabs
are bank-shaped: identity, activities, technology, vendors, controls,
weaknesses, history.

A freight desk needs a different set. The question here is not "could this
happen to us" but **"does this touch my freight, and how soon must I decide"**,
so the profile has to describe the lanes, the clock and the people:

    Desk        who this planner is, which corridors and modes they own
    Network     the nodes and lanes in scope
    Risk ledger the 45 variables, by family, and which modes each can touch
    Appetite    the five-level cutoffs and the convene rule  (EDITABLE)
    Response    route owners, standing teams, seniors, escalation, spend limit
    Sources     every feed as connected / example stand-in / absent

Nothing here is a separate store. The profile IS the config — it is a readable
view of the same YAML the engine runs on, which is what makes "onboarding a
new customer is a profile swap" a true statement rather than a claim.

EDITS GO TO config/, NEVER TO config.example/
---------------------------------------------
``config.example/`` is the committed public stand-in and stays pristine.
Saving writes a complete file into ``config/``, which is gitignored and takes
precedence at load time. Reset deletes the overlay. A planner can therefore
experiment freely without ever touching what ships in the repo.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from engine.config import CUSTOMER_DIR, Config
from engine.ingest.observations import FeedStatus
from engine.score.severity import LEVEL_DIRECTIVE, LEVEL_LABEL, Level


def build_profile(context) -> dict:
    """Assemble every tab from the loaded config and the last run."""
    config = context.config
    return {
        "as_of_label": str(context.clock),
        "config_version": config.version,
        "overlay_active": _overlay(config),
        "desk": _desk(context),
        "network": _network(context),
        "ledger": _ledger(config),
        "appetite": _appetite(config),
        "response": _response(config),
        "sources": _sources(context),
    }


# ---------------------------------------------------------------------
def _overlay(config: Config) -> dict:
    """Which files are running on the customer overlay vs the public stand-in.

    Stated per file rather than as one flag, because a half-configured profile
    is the normal state during onboarding and pretending otherwise hides it.
    """
    return {
        name: {
            "source": "config/" if not loaded.is_example else "config.example/",
            "is_example": loaded.is_example,
            "path": loaded.path.name,
        }
        for name, loaded in sorted(config.files.items())
    }


def _desk(context) -> dict:
    config = context.config
    lanes = config.lanes

    corridors: dict[str, int] = {}
    modes: dict[str, int] = {}
    for lane in lanes:
        focus = lane.get("focus") or "unassigned"
        corridors[focus] = corridors.get(focus, 0) + 1
        for leg in lane["legs"]:
            modes[leg["mode"]] = modes.get(leg["mode"], 0) + 1

    owners = config.contacts.get("route_owners", {})
    return {
        "shipments_in_book": context.result.shipments_total,
        "book_is_synthetic": all(s.synthetic for s in context.shipments),
        "lanes": len(lanes),
        "corridors": [
            {
                "focus": focus,
                "lanes": count,
                "owner": (owners.get(focus) or owners.get("default") or {}).get(
                    "name", "no owner configured"
                ),
            }
            for focus, count in sorted(corridors.items(), key=lambda kv: -kv[1])
        ],
        "modes": [
            {"mode": mode, "legs": count}
            for mode, count in sorted(modes.items(), key=lambda kv: -kv[1])
        ],
        "countries": sorted({n.country for n in config.nodes.values()}),
    }


def _network(context) -> dict:
    config = context.config
    used: dict[str, int] = {}
    for shipment in context.shipments:
        for node_id in shipment.node_ids:
            used[node_id] = used.get(node_id, 0) + 1

    nodes = []
    for node in config.nodes.values():
        nodes.append(
            {
                "id": node.id,
                "name": node.name,
                "kind": node.kind.value,
                "country": node.country,
                "modes": [m.value for m in node.modes],
                "chokepoint": node.chokepoint,
                "on_river": node.on_river,
                "alternatives": node.alternatives,
                "shipments": used.get(node.id, 0),
            }
        )
    nodes.sort(key=lambda n: (-n["shipments"], n["name"]))

    return {
        "nodes": nodes,
        "nodes_in_use": sum(1 for n in nodes if n["shipments"]),
        "lanes": [
            {
                "id": lane["id"],
                "name": lane["name"],
                "focus": lane.get("focus", ""),
                "legs": len(lane["legs"]),
                "modes": sorted({leg["mode"] for leg in lane["legs"]}),
                "nodes": [lane["legs"][0]["from"]] + [leg["to"] for leg in lane["legs"]],
            }
            for lane in config.lanes
        ],
    }


def _ledger(config: Config) -> dict:
    """The 45 variables, grouped by family.

    Sika confirmed no risk ledger for outgoing shipments exists today (Q1), so
    this file IS the proposal — which is why it belongs in the profile rather
    than buried in the engine.
    """
    families: dict[str, list] = {}
    for var in config.variables.values():
        families.setdefault(var.family, []).append(
            {
                "id": var.id,
                "name": var.name,
                "description": var.description.strip(),
                "modes": [m.value for m in var.modes_affected],
                "lead_time_hours": var.typical_lead_time_hours,
                "duration_days": var.typical_duration_days,
                # Where P cannot be sourced we say so rather than invent one.
                "probability_sourceable": var.probability_sourceable,
            }
        )

    model = config.raw("delay_model")
    return {
        "total": len(config.variables),
        "families": [
            {
                "family": family,
                "label": family.replace("_", " ").title(),
                "variables": sorted(items, key=lambda v: v["name"]),
                "unsourceable": sum(1 for v in items if not v["probability_sourceable"]),
                # The three-point estimates a planner is meant to argue with.
                "delay": model["defaults"].get(family, {}),
            }
            for family, items in sorted(families.items())
        ],
        "overrides": sorted(model.get("overrides") or {}),
    }


def _appetite(config: Config) -> dict:
    """The cutoffs — the editable part.

    Sika, answering Q3: these colleagues can read a matrix and "will also be
    able to play with the assumptions". This is that surface.
    """
    scoring = config.scoring
    levels = scoring.get("alert_levels", {})
    convene = scoring.get("convene_rule", {})

    return {
        "ladder": [
            {
                "level": lvl.value,
                "label": LEVEL_LABEL[lvl],
                "directive": LEVEL_DIRECTIVE[lvl],
            }
            for lvl in (Level.RED, Level.YELLOW, Level.BLUE, Level.WHITE, Level.GREEN)
        ],
        "alert_levels": {
            "red_hours": levels.get("red_hours"),
            "yellow_hours": levels.get("yellow_hours"),
            "blue_hours": levels.get("blue_hours"),
            "material_chf": levels.get("material_chf"),
        },
        "convene_rule": {
            "agreed_on": convene.get("agreed_on"),
            "agreed_by": convene.get("agreed_by"),
            "meeting_cadence_days": convene.get("meeting_cadence_days"),
            "escalated_cadence_days": convene.get("escalated_cadence_days"),
            "thresholds": dict(convene.get("thresholds", {})),
            "watch_fraction": convene.get("watch_fraction"),
        },
        "simulation": {
            "draws": scoring.get("simulation", {}).get("draws"),
            "seed": scoring.get("simulation", {}).get("seed"),
        },
        "min_action_hours": dict(scoring.get("min_action_hours", {})),
        # What is ours versus what came from the client, stated per field so a
        # reviewer can see which numbers still need agreeing.
        "provenance": {
            "red_hours": "client",
            "yellow_hours": "client",
            "blue_hours": "client",
            "material_chf": "assumed by us",
            "thresholds": "assumed by us — not yet agreed with the planning team",
            "min_action_hours": "assumed by us",
        },
    }


def _response(config: Config) -> dict:
    """Who gets drawn in, and when.

    The YAML refers to teams by id — ``FN_SUPPLY_CHAIN`` — because a reference
    has to be stable if a rename is not to break the escalation table. Those
    ids are resolved to readable names HERE rather than in the page: the
    mapping is config, the page is not, and an id that cannot be resolved must
    say so rather than being printed raw at a planner.
    """
    contacts = config.contacts
    teams = contacts.get("internal", [])
    by_id = {team["id"]: team for team in teams}

    def name_of(team_id: str) -> str:
        if not team_id:
            return ""
        team = by_id.get(team_id)
        if team is None:
            # A dangling reference is a config error. Printing it raw would
            # look like a team that exists and simply has an odd name.
            return f"{team_id} (not in the contact list)"
        return team.get("function") or team.get("name") or team_id

    return {
        "route_owners": [
            {"corridor": key, **value}
            for key, value in contacts.get("route_owners", {}).items()
        ],
        "standing_teams": [
            {
                "name": team.get("function", team["id"]),
                "role": team.get("role", ""),
                "tier": team.get("tier"),
                "responds_within_hours": team.get("response_sla_hours"),
                "convene_member": bool(team.get("convene_member")),
                "email": team.get("contact"),
                "phone": team.get("phone"),
            }
            for team in teams
        ],
        "seniors": contacts.get("seniors", []),
        "convene_by_level": {
            level: [name_of(team_id) for team_id in (ids or [])]
            for level, ids in contacts.get("convene_by_level", {}).items()
        },
        "escalation": [
            {
                "level": step.get("level"),
                "trigger_chf": step.get("trigger_chf"),
                "acknowledge_within_hours": step.get("acknowledge_within_hours"),
                "notify": [name_of(team_id) for team_id in step.get("notify", [])],
            }
            for step in contacts.get("escalation", [])
        ],
        "approval": {
            "delegated_limit_chf": contacts.get("approval", {}).get(
                "delegated_limit_chf"
            ),
            "approver": name_of(contacts.get("approval", {}).get("approver", "")),
        },
        "carriers": [
            {
                "name": carrier.get("name", carrier.get("id", "")),
                "modes": carrier.get("modes", []),
                "email": carrier.get("contact"),
                "phone": carrier.get("phone"),
                "responds_within_hours": carrier.get("response_sla_hours"),
            }
            for carrier in contacts.get("carriers", [])
        ],
        "vendors": [
            {"node": node, "entries": entries}
            for node, entries in contacts.get("local_vendors", {}).items()
        ],
    }


def _sources(context) -> dict:
    order = {FeedStatus.CONNECTED: 0, FeedStatus.FIXTURE: 1, FeedStatus.ABSENT: 2}
    tiers = context.config.thresholds.get("source_tiers", {})
    return {
        "feeds": [
            {
                "key": r.key,
                "label": r.label,
                "status": r.status.value,
                "detail": r.detail,
                "unlocks_if_connected": r.unlocks_if_connected,
                "records": r.records,
                "source_tier": r.source_tier,
                "url": r.url,
            }
            for r in sorted(context.reports, key=lambda r: order[r.status])
        ],
        "tiers": [
            {"tier": key, **(value or {})}
            for key, value in sorted(tiers.items(), key=lambda kv: str(kv[0]))
        ],
    }


# =====================================================================
# Saving — the customer overlay
# =====================================================================

# Only these may be written. An allow-list rather than a filter: an endpoint
# that can write arbitrary keys into the engine's config is an endpoint that
# can change what the numbers mean.
EDITABLE = {
    "alert_levels": {"red_hours", "yellow_hours", "blue_hours", "material_chf"},
    "convene_thresholds": {
        "exposure_chf", "contracts_exposed", "shipments_needing_decision",
    },
    "convene_meta": {"agreed_on", "agreed_by", "meeting_cadence_days"},
}


def check(scoring: dict) -> list[str]:
    """Reasons this profile would not behave as the person editing it expects.

    The ladder cutoffs are read as a chain of ``<=`` tests in
    ``score/severity.classify``. Put them out of order and a rung stops being
    reachable at all: with yellow below red, no route can ever be Alert, and
    nothing on screen says so. That is a silent failure, so it is refused here
    rather than saved.
    """
    problems: list[str] = []
    levels = scoring.get("alert_levels", {})

    red = _as_float(levels.get("red_hours"))
    yellow = _as_float(levels.get("yellow_hours"))
    blue = _as_float(levels.get("blue_hours"))
    floor = _as_float(levels.get("material_chf"))

    for name, value in (
        ("red_hours", red), ("yellow_hours", yellow), ("blue_hours", blue),
    ):
        if value is None or value <= 0:
            problems.append(f"{name} must be a positive number of hours")
    if floor is None or floor < 0:
        problems.append("material_chf must be zero or more")

    if None not in (red, yellow) and red >= yellow:
        problems.append(
            f"Critical ({red:g} h) must be sooner than Alert ({yellow:g} h), "
            "or nothing can ever be classed Alert"
        )
    if None not in (yellow, blue) and yellow >= blue:
        problems.append(
            f"Alert ({yellow:g} h) must be sooner than Watch ({blue:g} h), "
            "or nothing can ever be classed Watch"
        )

    thresholds = scoring.get("convene_rule", {}).get("thresholds", {})
    for name in ("exposure_chf", "contracts_exposed", "shipments_needing_decision"):
        value = _as_float(thresholds.get(name))
        if value is None or value < 0:
            problems.append(f"convene threshold {name} must be zero or more")

    return problems


def apply_edits(config: Config, edits: dict, customer_dir: Path | None = None) -> dict:
    """Write an edited scoring profile into config/.

    Returns what changed, so the UI can report it rather than claim success.
    Values outside the allow-list are reported as rejected, not silently
    dropped — a setting that appears to save and does not is worse than one
    that refuses.
    """
    customer_dir = customer_dir or CUSTOMER_DIR
    scoring = _deep_copy(config.scoring)

    applied: list[str] = []
    rejected: list[str] = []

    for key, value in (edits.get("alert_levels") or {}).items():
        if key not in EDITABLE["alert_levels"]:
            rejected.append(f"alert_levels.{key}")
            continue
        scoring.setdefault("alert_levels", {})[key] = _number(value)
        applied.append(f"alert_levels.{key}")

    for key, value in (edits.get("convene_thresholds") or {}).items():
        if key not in EDITABLE["convene_thresholds"]:
            rejected.append(f"convene_rule.thresholds.{key}")
            continue
        scoring.setdefault("convene_rule", {}).setdefault("thresholds", {})[key] = (
            _number(value)
        )
        applied.append(f"convene_rule.thresholds.{key}")

    for key, value in (edits.get("convene_meta") or {}).items():
        if key not in EDITABLE["convene_meta"]:
            rejected.append(f"convene_rule.{key}")
            continue
        # A cleared field must become null, not "". ``convene.evaluate`` reads
        # agreement as ``agreed_on is not None``, so an empty string would make
        # the board claim the team had signed off a threshold they never saw —
        # which is the one failure that breaks the mechanism outright.
        if isinstance(value, str) and not value.strip():
            value = None
        scoring.setdefault("convene_rule", {})[key] = _number(value)
        applied.append(f"convene_rule.{key}")

    problems = check(scoring)
    if problems:
        # Nothing is written. A half-applied ladder is worse than a refused one.
        return {"applied": [], "rejected": rejected, "problems": problems, "written": None}

    if applied:
        customer_dir.mkdir(parents=True, exist_ok=True)
        target = customer_dir / "scoring.yaml"
        target.write_text(
            "# Customer overlay — written from the risk profile page.\n"
            "# This file is gitignored and takes precedence over\n"
            "# config.example/scoring.yaml, which stays pristine.\n"
            "# Delete it (or press Reset) to fall back to the public stand-in.\n\n"
            + yaml.safe_dump(scoring, sort_keys=False, allow_unicode=True)
        )

    return {
        "applied": applied,
        "rejected": rejected,
        "problems": [],
        "written": str(customer_dir / "scoring.yaml") if applied else None,
    }


def clear_overlay(customer_dir: Path | None = None) -> dict:
    """Drop the overlay and fall back to the committed stand-in."""
    customer_dir = customer_dir or CUSTOMER_DIR
    target = customer_dir / "scoring.yaml"
    existed = target.exists()
    if existed:
        target.unlink()
    return {"removed": existed, "path": str(target)}


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number(value):
    """Keep integers integral so the YAML reads like a person wrote it.

    Every value arrives from a form field as a string. Anything that is not a
    number — a date, a team name — is passed through untouched.
    """
    if value is None or isinstance(value, bool):
        return value
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value
    return int(as_float) if as_float.is_integer() else as_float


def _deep_copy(value):
    import copy

    return copy.deepcopy(value)
