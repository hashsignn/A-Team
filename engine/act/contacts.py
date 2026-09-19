"""Who to call, for one route, at one alert level.

This answers the third failure the client named, in their own words:

    "Even when a crisis is identified, it is unclear what to do and who to
     involve."

The brief says it is the one most teams skip and worth the most.

WHAT IS DERIVED AND WHAT IS DECLARED
------------------------------------
Nothing here is guessed. Each group comes from somewhere specific:

  route manager     declared per corridor in contacts.yaml
  standing teams    the level -> teams map, which follows the ladder
  seniors           declared with the least urgent rung that draws them in
  alternate vendors the local_vendors list at the nodes THIS route passes
                    through — not a global directory
  alternate carriers the carriers that run the modes THIS route uses
  alternate routing  the `alternatives` already declared on the affected nodes

The last three are filtered by the route, which is the whole point. A planner
looking at a Rhine barge problem should not be handed the Singapore agency.

WHY THE LADDER DRIVES IT, NOT A CHF FIGURE
------------------------------------------
Sika's answer to Q6: the standing teams already hold full authority once
convened, and the failure is declaring too late. So convening IS the
escalation. The level — which is a deadline — decides who is drawn in, and
money only decides whether spend approval is also needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.config import Config
from engine.score.severity import Level


@dataclass
class Contact:
    """One person or organisation, with the reason they are on this list."""

    name: str
    role: str
    group: str
    why: str
    email: str | None = None
    phone: str | None = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "group": self.group,
            "why": self.why,
            "email": self.email,
            "phone": self.phone,
            **({"meta": self.meta} if self.meta else {}),
        }


def route_manager(lane: dict, config: Config) -> Contact:
    """The person who runs this corridor day to day."""
    owners = config.contacts.get("route_owners", {})
    entry = owners.get(lane.get("focus")) or owners.get("default")
    if not entry:
        # No owner configured is a real gap, not a person to invent.
        return Contact(
            name="No route owner configured",
            role="—",
            group="route_manager",
            why=(
                f"contacts.yaml declares no owner for the "
                f"'{lane.get('focus') or 'unknown'}' corridor and no default."
            ),
        )
    return Contact(
        name=entry["name"],
        role=entry["role"],
        group="route_manager",
        why=f"Runs the {lane.get('focus', 'this').replace('_', '–')} corridor.",
        email=entry.get("email"),
        phone=entry.get("phone"),
        meta={"hours": entry.get("hours")} if entry.get("hours") else {},
    )


def standing_teams(level: Level, config: Config) -> list[Contact]:
    """The teams this level convenes.

    Sika named these four as already meeting weekly and going daily in a
    crisis, with full authority to decide mitigation. Convening them is the
    escalation — there is no separate approval step to wait on.
    """
    by_level = config.contacts.get("convene_by_level", {})
    wanted = by_level.get(level.value, [])
    internal = {e["id"]: e for e in config.contacts.get("internal", [])}

    out = []
    for fn_id in wanted:
        entry = internal.get(fn_id)
        if entry is None:
            continue
        out.append(
            Contact(
                name=entry["function"],
                role=entry["role"],
                group="standing_team",
                why=f"Convened at {level.value} — responds within "
                    f"{entry['response_sla_hours']} h.",
                email=entry.get("contact"),
                phone=entry.get("phone"),
                meta={
                    "tier": entry.get("tier"),
                    "response_sla_hours": entry.get("response_sla_hours"),
                },
            )
        )
    return out


def seniors_for(level: Level, config: Config) -> list[Contact]:
    """Seniors drawn in at this rung or a quieter one."""
    from engine.score.severity import LEVEL_RANK

    rank = LEVEL_RANK[level]
    out = []
    for entry in config.contacts.get("seniors", []):
        try:
            threshold = LEVEL_RANK[Level(entry.get("from_level", "red"))]
        except ValueError:
            continue
        if rank < threshold:
            continue
        out.append(
            Contact(
                name=entry["name"],
                role=entry["role"],
                group="senior",
                why=entry.get("why", ""),
                email=entry.get("email"),
                phone=entry.get("phone"),
                meta={"from_level": entry.get("from_level")},
            )
        )
    return out


def alternate_vendors(lane: dict, config: Config) -> list[Contact]:
    """Local vendors at the nodes THIS route passes through.

    Filtered by the route, not the global directory. A planner looking at a
    Rhine barge problem should not be handed the Singapore agency.
    """
    nodes = _lane_nodes(lane)
    directory = config.contacts.get("local_vendors", {})
    node_names = config.nodes

    out = []
    for node_id in nodes:
        for vendor in directory.get(node_id, []):
            node = node_names.get(node_id)
            out.append(
                Contact(
                    name=vendor["name"],
                    role=vendor.get("service", "").replace("_", " "),
                    group="vendor",
                    why=f"At {node.name if node else node_id}, on this route.",
                    phone=vendor.get("phone"),
                    meta={"node": node_id},
                )
            )
    return out


def alternate_carriers(lane: dict, config: Config) -> list[Contact]:
    """Carriers running the modes this route uses."""
    modes = {leg["mode"] for leg in lane["legs"]}
    out = []
    for carrier in config.contacts.get("carriers", []):
        shared = modes & set(carrier.get("modes", []))
        if not shared:
            continue
        out.append(
            Contact(
                name=carrier["name"],
                role=f"{', '.join(sorted(shared))} carrier",
                group="carrier",
                why=f"Runs {', '.join(sorted(shared))} on this route.",
                email=carrier.get("contact"),
                phone=carrier.get("phone"),
                meta={"response_sla_hours": carrier.get("response_sla_hours")},
            )
        )
    return out


def alternate_routing(lane: dict, network) -> list[dict]:
    """Alternative nodes already declared on this route's own nodes.

    An empty list means "no alternative route configured" and is rendered as
    exactly that. It is not a silent zero, and it is not a claim that no
    alternative exists in the world.
    """
    out = []
    for node_id in _lane_nodes(lane):
        if node_id not in network.nodes:
            continue
        node = network.node(node_id)
        alternatives = network.alternatives_for(node_id)
        if not alternatives:
            continue
        out.append(
            {
                "node": node_id,
                "node_name": node.name,
                "alternatives": [
                    {"id": a.id, "name": a.name, "country": a.country}
                    for a in alternatives
                ],
            }
        )
    return out


def approval_needed(cost_chf: float, config: Config) -> dict | None:
    """Whether this spend needs Controlling before it can happen."""
    approval = config.contacts.get("approval", {})
    limit = approval.get("delegated_limit_chf")
    if limit is None or cost_chf <= float(limit):
        return None

    internal = {e["id"]: e for e in config.contacts.get("internal", [])}
    approver = internal.get(approval.get("approver", ""), {})
    return {
        "limit_chf": float(limit),
        "cost_chf": round(cost_chf, 2),
        "approver": approver.get("function", "Controlling"),
        "email": approver.get("contact"),
        "note": (
            f"CHF {cost_chf:,.0f} is above the delegated limit of "
            f"CHF {float(limit):,.0f} — {approver.get('function', 'Controlling')} "
            "must release it."
        ),
    }


def escalation_step(level: Level, exposure_chf: float, config: Config) -> dict:
    """The rung of the escalation ladder this route currently sits on."""
    ladder = config.contacts.get("escalation", [])
    step = ladder[0] if ladder else {}
    for entry in ladder:
        if exposure_chf >= float(entry.get("trigger_chf", 0)):
            step = entry

    internal = {e["id"]: e for e in config.contacts.get("internal", [])}
    return {
        "level": step.get("level"),
        "trigger_chf": step.get("trigger_chf"),
        "acknowledge_within_hours": step.get("acknowledge_within_hours"),
        "notify": [
            internal[f]["function"] for f in step.get("notify", []) if f in internal
        ],
        "note": step.get("note"),
    }


def build(lane: dict, level: Level, exposure_chf: float, best_cost_chf: float,
          config: Config, network) -> dict:
    """Everything the response pane needs, in one payload."""
    return {
        "route_manager": route_manager(lane, config).as_dict(),
        "standing_teams": [c.as_dict() for c in standing_teams(level, config)],
        "seniors": [c.as_dict() for c in seniors_for(level, config)],
        "vendors": [c.as_dict() for c in alternate_vendors(lane, config)],
        "carriers": [c.as_dict() for c in alternate_carriers(lane, config)],
        "alternate_routing": alternate_routing(lane, network),
        "approval": approval_needed(best_cost_chf, config),
        "escalation": escalation_step(level, exposure_chf, config),
    }


def _lane_nodes(lane: dict) -> list[str]:
    """Nodes on the lane, in travel order, without duplicates."""
    seen: list[str] = []
    for leg in lane["legs"]:
        for node_id in (leg["from"], leg["to"]):
            if node_id not in seen:
                seen.append(node_id)
    return seen
