"""The resolver: one encounter, all four layers, every clash recorded.

This is the function the validation scenarios exercise and the TMS payload is
built from. It takes one (event, shipment) encounter and returns a decision
that carries its own audit trail — which clashes arose, which rule settled
each, and the sentence a planner would use.

WHY IT RECORDS CLASHES IT DID NOT HAVE TO ADJUDICATE
----------------------------------------------------
Most of the clashes in the spec dissolve under the pathway split rather than
being resolved by a rule. It would be tidier to stay silent about those, and
it would be wrong: a planner looking at a severe dock strike scored calmly
needs to see WHY it is calm. "Damage pathway shut — dry mortar has no failure
mode for a strike" is the whole answer, and hiding it because the code found
it easy would make the calm answer look like a missed alarm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.config import Config
from engine.score import alert as alert_mod
from engine.score.severity import Level
from engine.taxonomy import channel as channel_mod
from engine.taxonomy import pathways


@dataclass
class Encounter:
    """What is known about one event meeting one shipment."""

    variable_id: str
    cargo_class: str
    asset_id: str
    channel_id: str
    late_hours: float = 0.0
    observation: dict = field(default_factory=dict)
    consequence_cap: float = 0.0
    base_level: Level = Level.GREEN


@dataclass
class Resolution:
    pathway: pathways.PathwayVerdict
    cost: channel_mod.ChannelCost
    alert: alert_mod.AlertScore
    clashes: list[dict] = field(default_factory=list)
    forbidden: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pathway": {
                "delay": {
                    "open": bool(self.pathway.delay),
                    "reason": self.pathway.delay.reason,
                },
                "damage": {
                    "open": bool(self.pathway.damage),
                    "reason": self.pathway.damage.reason,
                    "irreversible": self.pathway.irreversible,
                    "scrap_fraction": self.pathway.scrap_fraction,
                },
                "headline": self.pathway.headline,
                "notes": list(self.pathway.notes),
            },
            "channel_cost": {
                "channel": self.cost.channel_id,
                "label": self.cost.label,
                "free_hours": self.cost.free_hours,
                "chargeable_hours": round(self.cost.chargeable_hours, 2),
                "cost_chf": self.cost.cost_chf,
                "shape": self.cost.shape,
                "within_tolerance": self.cost.within_tolerance,
                "explanation": self.cost.explanation,
            },
            "alert": self.alert.as_dict(),
            "clashes_resolved": self.clashes,
            "actions_suppressed": self.forbidden,
        }


CLASH_TEXT = {
    "severe_event_invulnerable_cargo": (
        "Severe event, invulnerable cargo",
        "Scored on delay alone. The event cannot harm this consignment, but "
        "it can still make it late, and both halves of that are reported.",
    ),
    "critical_channel_zero_vulnerability": (
        "Critical channel, no cargo risk",
        "The channel's consequence stands in full — a stopped line costs the "
        "same whatever was on the pallet — but it does not manufacture a "
        "cargo risk that physics says is absent.",
    ),
    "conditioned_asset_protects_vulnerable_cargo": (
        "Conditioned equipment vs vulnerable cargo",
        "The damage pathway is gated by the equipment's autonomy, not closed "
        "by it. Protection here is a countdown.",
    ),
    "pot_life_makes_delay_into_damage": (
        "Delay became damage",
        "This cargo has a clock running from the moment it was made, so "
        "lateness is itself the harm.",
    ),
    "adr_blocks_the_obvious_mitigation": (
        "Regulation blocks the standard mitigation",
        "The action was removed rather than offered. A plan that cannot "
        "legally be executed costs the planner the hours they had left.",
    ),
}


def resolve(config: Config, encounter: Encounter) -> Resolution:
    """Evaluate one encounter end to end."""
    observation = dict(encounter.observation)
    observation.setdefault("delay_hours", encounter.late_hours)

    verdict = pathways.evaluate(
        config,
        encounter.variable_id,
        encounter.cargo_class,
        encounter.asset_id,
        observation,
    )

    cost = channel_mod.cost_of_lateness(
        config, encounter.channel_id, encounter.late_hours
    )

    level, escalation = alert_mod.escalate_for_damage(
        encounter.base_level, [verdict]
    )
    score = alert_mod.score_for(
        level,
        cost.cost_chf,
        encounter.consequence_cap or max(cost.cost_chf, 1.0),
        escalation,
    )

    clashes = []
    seen = list(dict.fromkeys(verdict.clashes_resolved))

    # The canonical clash: the corridor is hit, the goods are not. Recorded
    # whenever delay is open and damage is shut, because that is exactly the
    # case a planner needs explained — a severe event producing a calm answer
    # looks like a missed alarm until you can see WHY it is calm.
    if bool(verdict.delay) and not bool(verdict.damage):
        seen.append("severe_event_invulnerable_cargo")
    # A critical channel meeting invulnerable cargo is worth naming even
    # though nothing had to be decided — see the module docstring.
    critical = config.raw("taxonomy")["layer3_channels"].get(
        encounter.channel_id, {}
    ).get("free_hours", 99.0) <= 1.0
    if critical and not bool(verdict.damage) and bool(verdict.delay):
        seen.append("critical_channel_zero_vulnerability")

    for rule_id in dict.fromkeys(seen):
        title, explanation = CLASH_TEXT.get(rule_id, (rule_id, ""))
        clashes.append({"rule": rule_id, "title": title, "resolution": explanation})

    forbidden = channel_mod.forbidden_actions(
        config, encounter.cargo_class, ["air_cargo", "parcel", "road_ltl"]
    )
    if forbidden:
        clashes.append({
            "rule": "adr_blocks_the_obvious_mitigation",
            "title": CLASH_TEXT["adr_blocks_the_obvious_mitigation"][0],
            "resolution": CLASH_TEXT["adr_blocks_the_obvious_mitigation"][1],
        })

    return Resolution(
        pathway=verdict, cost=cost, alert=score,
        clashes=clashes, forbidden=forbidden,
    )
