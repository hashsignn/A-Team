"""The playbook — "what do I do, and who do I involve" (BRIEF §5.5, §7 pane 4).

This answers the third failure the client named, in their own words:

    "Even when a crisis is identified, it is unclear what to do and who to
     involve."

The brief says it is the one most teams skip and worth the most.

ACTION OWNERSHIP IS EXPLICIT
----------------------------
Every option records who actually controls the lever. This matters more than it
looks: for a deep-sea leg mid-voyage, rerouting is the carrier's decision, not
the shipper's. A playbook that offers "reroute the vessel" on an ocean leg is
offering something the planner cannot do, and one such row costs the tool its
credibility with someone who has done the job.

So the realistic options on a sea leg are mostly: escalate to the carrier,
notify the customer, re-sequence production, release safety stock. "Escalate to
carrier rep" is a first-class action with a real duration and a named contact —
not a fallback for when the model has nothing to say.

RESIDUAL FRACTION
-----------------
Each option carries the fraction of the event's delay that survives it. 0.0
removes the exposure; 1.0 does nothing. This is what the act-scenario Monte
Carlo re-runs with, so the value of acting comes out of the same mechanism as
everything else rather than being asserted.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.config import Config
from engine.network.graph import Network
from engine.schemas import ActionOption, Event, GateHit, Mode, Shipment


@dataclass(frozen=True)
class ActionTemplate:
    action_type: str
    label: str
    owner: str
    modes: tuple[Mode, ...]
    residual_fraction: float
    base_cost_chf: float
    cost_fraction_of_value: float
    description: str
    needs_alternative_node: bool = False


# The option set. Small, explicit, and arguable — which is the point.
TEMPLATES: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        action_type="barge_to_rail_switch",
        label="Switch barge leg to rail",
        owner="us",
        modes=(Mode.BARGE,),
        residual_fraction=0.15,
        base_cost_chf=1800.0,
        cost_fraction_of_value=0.035,
        description=(
            "Move the affected leg off the river onto rail. The standard answer "
            "to a low-water derate, and the reason a Rhine restriction is "
            "survivable if you see it early enough."
        ),
    ),
    ActionTemplate(
        action_type="barge_part_load",
        label="Accept part load, add a sailing",
        owner="us",
        modes=(Mode.BARGE,),
        residual_fraction=0.55,
        base_cost_chf=900.0,
        cost_fraction_of_value=0.02,
        description=(
            "Sail at reduced draught and book a second sailing for the balance. "
            "Cheaper than a rail switch, but it splits the consignment — which "
            "fails OTIF on 'in full' even when the first part arrives on time."
        ),
    ),
    ActionTemplate(
        action_type="road_reroute",
        label="Reroute road leg",
        owner="us",
        modes=(Mode.ROAD,),
        residual_fraction=0.20,
        base_cost_chf=650.0,
        cost_fraction_of_value=0.012,
        description="Divert around the affected road segment or border crossing.",
    ),
    ActionTemplate(
        action_type="rail_rebook",
        label="Rebook rail path",
        owner="us",
        modes=(Mode.RAIL,),
        residual_fraction=0.30,
        base_cost_chf=1200.0,
        cost_fraction_of_value=0.02,
        description="Book an alternative path or operator around the blockage.",
    ),
    ActionTemplate(
        action_type="sea_rebook",
        label="Rebook via alternative port",
        owner="us",
        modes=(Mode.SEA,),
        residual_fraction=0.35,
        base_cost_chf=4200.0,
        cost_fraction_of_value=0.04,
        description=(
            "Discharge or load at a declared alternative port and move inland "
            "from there. Only available before the vessel is committed."
        ),
        needs_alternative_node=True,
    ),
    ActionTemplate(
        action_type="escalate_carrier",
        label="Escalate to carrier",
        owner="carrier",
        modes=(Mode.SEA, Mode.BARGE, Mode.RAIL, Mode.ROAD),
        residual_fraction=0.80,
        base_cost_chf=0.0,
        cost_fraction_of_value=0.0,
        description=(
            "Ask the carrier to prioritise this booking. The lever belongs to "
            "them, not to us — which is exactly why it is cheap, fast, and "
            "usually only worth a fraction of the delay."
        ),
    ),
    ActionTemplate(
        action_type="reserve_capacity",
        label="Reserve contingency capacity",
        owner="us",
        modes=(Mode.SEA, Mode.BARGE, Mode.RAIL, Mode.ROAD),
        residual_fraction=0.60,
        base_cost_chf=1500.0,
        cost_fraction_of_value=0.01,
        description=(
            "Hold space on an alternative service without committing to it. "
            "Buys back optionality that would otherwise expire."
        ),
    ),
    ActionTemplate(
        action_type="expedite",
        label="Expedite downstream legs",
        owner="us",
        modes=(Mode.ROAD, Mode.RAIL),
        residual_fraction=0.65,
        base_cost_chf=1100.0,
        cost_fraction_of_value=0.018,
        description="Recover time on the legs after the disruption.",
    ),
    ActionTemplate(
        action_type="notify_customer",
        label="Notify customer and re-agree the date",
        owner="customer",
        modes=(Mode.SEA, Mode.BARGE, Mode.RAIL, Mode.ROAD),
        residual_fraction=1.00,
        base_cost_chf=0.0,
        cost_fraction_of_value=0.0,
        description=(
            "Does not recover a single day. It converts a missed commitment "
            "into an agreed one, which is often worth more than the days. "
            "Always available, and the only thing left once the deadline "
            "for everything else has passed."
        ),
    ),
)


def options_for(
    shipment: Shipment,
    event: Event,
    hits: list[GateHit],
    config: Config,
    network: Network,
    hours_until_impact: float,
) -> list[ActionOption]:
    """Every action for this (shipment, event), feasible or not.

    Infeasible options are RETAINED with a reason rather than filtered out. A
    planner needs to see that the rail switch existed and expired — that is the
    whole argument for acting sooner next time, and deleting the row deletes
    the argument.
    """
    affected_modes = {
        h.mode for h in hits
        if h.shipment_id == shipment.shipment_id and h.event_id == event.event_id
    }
    if not affected_modes:
        return []

    affected_nodes = {
        h.node_id for h in hits
        if h.shipment_id == shipment.shipment_id and h.event_id == event.event_id
    }

    out: list[ActionOption] = []
    for template in TEMPLATES:
        if not affected_modes & set(template.modes):
            continue

        min_hours = config.min_action_hours(template.action_type)
        if min_hours is None:
            # Not configured. Unknown is not zero — an unconfigured action must
            # not look permanently available (BRIEF §8.1).
            out.append(
                _build(
                    template, shipment, config, network, affected_nodes,
                    min_hours=float("inf"),
                    feasible=False,
                    reason=(
                        f"no minimum action time configured for "
                        f"'{template.action_type}' — cannot say whether this is "
                        "still open"
                    ),
                )
            )
            continue

        feasible = True
        reason: str | None = None

        if hours_until_impact < min_hours:
            feasible = False
            reason = (
                f"needs {min_hours:.0f} h to execute; only "
                f"{max(0.0, hours_until_impact):.0f} h remain"
            )

        if template.needs_alternative_node and feasible:
            has_alt = any(
                network.alternatives_for(nid) for nid in affected_nodes
                if nid in network.nodes
            )
            if not has_alt:
                feasible = False
                reason = "no alternative route configured for the affected node"

        out.append(
            _build(
                template, shipment, config, network, affected_nodes,
                min_hours=float(min_hours),
                feasible=feasible,
                reason=reason,
            )
        )

    return out


def _build(
    template: ActionTemplate,
    shipment: Shipment,
    config: Config,
    network: Network,
    affected_nodes: set[str],
    min_hours: float,
    feasible: bool,
    reason: str | None,
) -> ActionOption:
    cost = template.base_cost_chf + shipment.value_chf * template.cost_fraction_of_value

    # Spot cargo has no negotiated rate to fall back on, so a rebook costs more.
    if shipment.contract_type.value == "spot" and template.action_type in (
        "sea_rebook", "rail_rebook", "barge_to_rail_switch"
    ):
        cost *= 1.35

    label = template.label
    if template.needs_alternative_node:
        for nid in affected_nodes:
            if nid in network.nodes:
                alternatives = network.alternatives_for(nid)
                if alternatives:
                    label = f"{template.label} ({alternatives[0].name})"
                    break

    return ActionOption(
        action_id=f"{shipment.shipment_id}:{template.action_type}",
        label=label,
        action_type=template.action_type,
        owner=template.owner,  # type: ignore[arg-type]
        min_hours=min_hours,
        cost_chf=round(cost, 2),
        residual_delay_days=template.residual_fraction,
        feasible=feasible,
        infeasible_reason=reason,
        contacts=_contacts_for(template, config, affected_nodes),
        description=template.description,
    )


def _contacts_for(
    template: ActionTemplate,
    config: Config,
    affected_nodes: set[str],
) -> list[str]:
    """Who to ring, by role.

    The four internal functions are the ones Sika named in answer to Q6 —
    Procurement, Manufacturing, Supply Chain, Controlling — because those are
    the teams that already hold full authority once convened.
    """
    contacts = config.contacts
    names: list[str] = []

    by_id = {entry["id"]: entry for entry in contacts.get("internal", [])}

    def add(fn_id: str) -> None:
        entry = by_id.get(fn_id)
        if entry and entry["function"] not in names:
            names.append(f"{entry['function']} — {entry['role']}")

    add("FN_SUPPLY_CHAIN")

    if template.action_type in ("sea_rebook", "rail_rebook", "barge_to_rail_switch",
                               "reserve_capacity", "expedite"):
        add("FN_PROCUREMENT")
    if template.action_type == "notify_customer":
        add("FN_CUSTOMER_SERVICE")
    if template.action_type in ("barge_part_load", "expedite"):
        add("FN_MANUFACTURING")

    # Anything above the delegated limit needs Controlling before it can happen.
    limit = contacts.get("approval", {}).get("delegated_limit_chf")
    if limit is not None and template.base_cost_chf > float(limit):
        add("FN_CONTROLLING")

    for node_id in sorted(affected_nodes):
        for vendor in contacts.get("local_vendors", {}).get(node_id, []):
            names.append(f"{vendor['name']} ({vendor['service']})")

    return names


def best_option(options: list[ActionOption]) -> ActionOption | None:
    """The cheapest feasible action that actually recovers time.

    Notification recovers nothing, so it is never "best" while a real option
    survives — but it is returned when nothing else does, because at that point
    it is genuinely the correct advice (BRIEF §5.5: below 1x, stop proposing
    reroutes and switch to notification).
    """
    feasible = [o for o in options if o.feasible]
    if not feasible:
        return None
    recovering = [o for o in feasible if o.residual_delay_days < 0.95]
    if recovering:
        return min(recovering, key=lambda o: (o.residual_delay_days, o.cost_chf))
    return min(feasible, key=lambda o: o.cost_chf)
