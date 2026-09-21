"""The pathway gate: does this event threaten the goods, or only the clock?

THE IDEA THIS FILE EXISTS FOR
=============================
A four-factor multiplicative score —

    severity x overlap x vulnerability x criticality

— cannot answer the commonest question on a freight desk, which is what to do
about a severe event hitting invulnerable cargo. Set vulnerability near zero
and a four-day dock strike stops mattering. Set it near one and ambient dry
mortar gets flagged for a freeze. No override rule fixes this, because the
question was malformed: "how vulnerable is this cargo" is not one question.

It is two.

    DELAY   the freight moves late. Applies to everything on the corridor.
            Delay does not care what is in the box.
    DAMAGE  the goods are harmed. Applies only where Layer 4 opens the gate.

Split them and every clash in the brief resolves itself:

    dock strike + dry mortar   delay opens, damage stays shut
                               -> "no cargo risk, four days late"
    freeze + dry mortar        neither opens -> nothing, correctly
    freeze + water polymer     damage opens -> scrap, not late
    freeze + polymer in reefer damage gated by the genset's fuel autonomy

None of those needed a rule. They fall out of asking the right question.

WHAT IS A GATE AND WHAT IS A WEIGHT
-----------------------------------
Everything here is a GATE — a yes/no about physics or regulation. Nothing in
this file is a weight, because there is no defensible way to calibrate one.
"Freeze-critical cargo is 0.8 vulnerable to a freeze" is a number nobody can
source; "the emulsion breaks below 0 °C" is in the product data sheet.

The graded part of the answer lives where it can be sourced: the delay
distribution (Monte Carlo) and the channel's own penalty schedule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from engine.config import Config


class Pathway(str, Enum):
    DELAY = "delay"
    DAMAGE = "damage"
    BOTH = "both"

    def carries_delay(self) -> bool:
        return self in (Pathway.DELAY, Pathway.BOTH)

    def carries_damage(self) -> bool:
        return self in (Pathway.DAMAGE, Pathway.BOTH)


@dataclass(frozen=True)
class GateDecision:
    """One yes/no, with the sentence that justifies it.

    A gate without a reason is an assertion a planner cannot argue with,
    which is the same problem as a score without a reason.
    """

    open: bool
    reason: str
    clash_rule: str | None = None

    def __bool__(self) -> bool:
        return self.open


@dataclass
class PathwayVerdict:
    """What this event does to this shipment, split by pathway."""

    delay: GateDecision
    damage: GateDecision
    irreversible: bool = False
    scrap_fraction: float = 0.0
    clashes_resolved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def any_open(self) -> bool:
        return bool(self.delay) or bool(self.damage)

    @property
    def headline(self) -> str:
        """The sentence the board shows. Written so the two pathways are
        never confused for one another."""
        if bool(self.damage) and bool(self.delay):
            harm = "scrapped" if self.irreversible else "degraded"
            return f"Both pathways open: the goods can be {harm}, and the freight is late."
        if bool(self.damage):
            harm = "scrapped" if self.irreversible else "degraded"
            return f"Cargo risk: the goods can be {harm} whether or not they arrive on time."
        if bool(self.delay):
            return "Delay only — nothing in this consignment is harmed by the event."
        return "Does not reach this shipment."


def declared_pathway(config: Config, variable_id: str) -> tuple[Pathway, dict]:
    """The pathway for one Layer 1 variable, plus its override record."""
    spec = config.raw("taxonomy")["layer1_pathways"]
    overrides = spec.get("overrides") or {}
    if variable_id in overrides:
        entry = overrides[variable_id]
        return Pathway(entry["pathway"]), entry

    variable = config.variables.get(variable_id)
    if variable is None:
        # An unmapped variable is treated as delay-only. Assuming DAMAGE for
        # something we cannot classify would manufacture cargo alerts out of
        # a config gap, and the whole point of the fallback is to fail quiet.
        return Pathway.DELAY, {"why": "variable not in the ledger; delay assumed"}

    default = spec["defaults"].get(variable.family, Pathway.DELAY.value)
    return Pathway(default), {"why": f"family default for {variable.family}"}


def asset_reachable(config: Config, asset_id: str, variable_id: str) -> GateDecision:
    """Can this event physically reach this equipment?

    Layer 2. A cross-dock gridlock cannot touch an FTL that never enters one;
    a lock closure cannot touch a truck. Cheap, and it removes a whole class
    of false alarm before anything is scored.
    """
    assets = config.raw("taxonomy")["layer2_assets"]
    asset = assets.get(asset_id)
    if asset is None:
        return GateDecision(True, f"unknown asset {asset_id!r}; not filtered")

    variable = config.variables.get(variable_id)
    if variable is None:
        return GateDecision(True, "unknown variable; not filtered")

    # Mode first, then terminal. The order matters only for the REASON, and
    # the reason is most of the value: told that an FTL escapes a sea-port
    # congestion event because "it never enters a terminal", a planner would
    # reasonably conclude their LTL on the same road lane IS exposed. It is
    # not — road is not affected at all. Always give the most fundamental
    # exclusion.
    modes = {m.value for m in variable.modes_affected}
    if asset.get("mode") and asset["mode"] not in modes:
        return GateDecision(
            False,
            f"{variable.name} affects {', '.join(sorted(modes))}; this moves "
            f"by {asset['mode']}",
        )

    # Terminal events need a terminal.
    if variable.family == "port_ops" and not asset.get("touches_terminals", True):
        return GateDecision(
            False,
            f"{asset['label']} never enters a terminal, so a terminal event "
            f"cannot reach it",
        )

    return GateDecision(True, f"{variable.name} can reach {asset['label']}")


def _condition_met(
    condition: dict,
    observation: dict,
) -> tuple[bool, bool, str]:
    """(met, hard, why) for one Layer 4 damage condition.

    ``hard`` marks the condition being past the physics threshold rather than
    the precautionary one — 0 °C rather than 5 °C. That distinction is what
    separates "expedite it" from "write it off and remake it".

    A missing observation is NOT a met condition. If nobody told us the
    temperature, we do not get to claim the polymer froze.
    """
    kind = condition["condition"]

    if kind == "ambient_below_c":
        value = observation.get("ambient_c")
        if value is None:
            return False, False, "no temperature observed"
        soft = condition["threshold_c"]
        hard_at = condition.get("hard_threshold_c", soft)
        if value < hard_at:
            return True, True, f"{value:.0f} °C is below the {hard_at:.0f} °C physical limit"
        if value < soft:
            return True, False, f"{value:.0f} °C is below the {soft:.0f} °C precautionary limit"
        return False, False, f"{value:.0f} °C is above the {soft:.0f} °C limit"

    if kind == "ambient_above_c":
        value = observation.get("ambient_c")
        if value is None:
            return False, False, "no temperature observed"
        soft = condition["threshold_c"]
        hard_at = condition.get("hard_threshold_c", soft)
        if value > hard_at:
            return True, True, f"{value:.0f} °C is above the {hard_at:.0f} °C physical limit"
        if value > soft:
            return True, False, f"{value:.0f} °C is above the {soft:.0f} °C precautionary limit"
        return False, False, f"{value:.0f} °C is below the {soft:.0f} °C limit"

    if kind == "elapsed_hours_above":
        value = observation.get("elapsed_hours")
        if value is None:
            return False, False, "no elapsed time supplied"
        soft = condition["threshold_hours"]
        hard_at = condition.get("hard_threshold_hours", soft)
        if value > hard_at:
            return True, True, f"{value:.0f} h exceeds the {hard_at:.0f} h pot life outright"
        if value > soft:
            return True, False, f"{value:.0f} h is past the {soft:.0f} h working limit"
        return False, False, f"{value:.0f} h is inside the {soft:.0f} h pot life"

    if kind == "unsecured_stop_hours_above":
        value = observation.get("unsecured_stop_hours")
        if value is None:
            return False, False, "no unplanned stop recorded"
        soft = condition["threshold_hours"]
        hard_at = condition.get("hard_threshold_hours", soft)
        if value > hard_at:
            return True, True, f"{value:.0f} h parked unsecured"
        if value > soft:
            return True, False, f"{value:.0f} h parked unsecured"
        return False, False, f"{value:.0f} h stop is inside tolerance"

    if kind == "precipitation_on_open_handling":
        if not observation.get("open_handling"):
            return False, False, "the load is never handled in the open"
        if not observation.get("precipitation"):
            return False, False, "no precipitation in the window"
        return True, True, "open handling during precipitation"

    if kind == "extra_handling_events_above":
        value = observation.get("extra_handling_events")
        if value is None:
            return False, False, "no extra handling recorded"
        limit = condition["threshold_count"]
        if value > limit:
            return True, False, f"{value} extra handling events, above {limit}"
        return False, False, f"{value} extra handling events, within {limit}"

    return False, False, f"unknown condition {kind!r}; not asserted"


def evaluate(
    config: Config,
    variable_id: str,
    cargo_class: str,
    asset_id: str,
    observation: dict | None = None,
) -> PathwayVerdict:
    """The whole gate for one (event variable, cargo, asset) triple.

    ``observation`` carries what is actually known about this encounter —
    the temperature at the exposed leg, the hours of unplanned stop, whether
    handling happens in the open. Anything absent is absent, never defaulted:
    an unobserved temperature does not freeze anything.
    """
    observation = dict(observation or {})
    taxonomy = config.raw("taxonomy")
    cargo = taxonomy["layer4_cargo"].get(cargo_class)
    asset = taxonomy["layer2_assets"].get(asset_id, {})
    pathway, override = declared_pathway(config, variable_id)
    clashes: list[str] = []
    notes: list[str] = []

    # ---- Layer 2: can it reach the equipment at all? ------------------
    reach = asset_reachable(config, asset_id, variable_id)
    if not reach:
        return PathwayVerdict(
            delay=GateDecision(False, reach.reason),
            damage=GateDecision(False, reach.reason),
        )

    # ---- delay pathway ------------------------------------------------
    if pathway.carries_delay():
        delay = GateDecision(
            True,
            "Delay applies to every consignment on this corridor, whatever "
            "is in the box.",
        )
    else:
        delay = GateDecision(
            False, "This event class does not delay freight."
        )

    # ---- damage pathway -----------------------------------------------
    if cargo is None:
        return PathwayVerdict(
            delay=delay,
            damage=GateDecision(False, f"unknown cargo class {cargo_class!r}"),
            notes=["cargo class not in the taxonomy; damage not asserted"],
        )

    if not pathway.carries_damage():
        damage = GateDecision(
            False,
            f"{_variable_name(config, variable_id)} cannot harm goods — it "
            f"only delays them.",
        )
        if cargo.get("damage_conditions"):
            clashes.append("severe_event_invulnerable_cargo")
        return PathwayVerdict(
            delay=delay, damage=damage, clashes_resolved=clashes, notes=notes
        )

    # An event that CAN damage, meeting cargo that declares no way to be
    # damaged. This is the canonical clash and it needs no adjudication.
    conditions = cargo.get("damage_conditions") or []
    if not conditions:
        clashes.append("severe_event_invulnerable_cargo")
        return PathwayVerdict(
            delay=delay,
            damage=GateDecision(
                False,
                f"{cargo['label']} has no physical failure mode for this "
                f"event. Nothing in the consignment is harmed.",
                clash_rule="severe_event_invulnerable_cargo",
            ),
            clashes_resolved=clashes,
            notes=notes,
        )

    # Does the family even reach this cargo class?
    variable = config.variables.get(variable_id)
    at_risk = cargo.get("families_at_risk") or []
    if variable is not None and at_risk and variable.family not in at_risk:
        clashes.append("severe_event_invulnerable_cargo")
        return PathwayVerdict(
            delay=delay,
            damage=GateDecision(
                False,
                f"{cargo['label']} is not exposed to {variable.family} events.",
                clash_rule="severe_event_invulnerable_cargo",
            ),
            clashes_resolved=clashes,
            notes=notes,
        )

    # An override may require an asset flag — a gale damages open-deck
    # equipment and leaves a box trailer alone.
    required_flag = override.get("damage_requires_asset_flag")
    if required_flag and not asset.get(required_flag):
        return PathwayVerdict(
            delay=delay,
            damage=GateDecision(
                False,
                f"{asset.get('label', asset_id)} is not {required_flag.replace('_', ' ')}.",
            ),
            clashes_resolved=clashes,
            notes=notes,
        )

    met: list[tuple[dict, bool, str]] = []
    for condition in conditions:
        is_met, hard, why = _condition_met(condition, observation)
        if is_met:
            met.append((condition, hard, why))

    if not met:
        reasons = [
            _condition_met(c, observation)[2] for c in conditions
        ]
        return PathwayVerdict(
            delay=delay,
            damage=GateDecision(
                False,
                "No damage condition is met: " + "; ".join(reasons) + ".",
            ),
            clashes_resolved=clashes,
            notes=notes,
        )

    # ---- conditioning: a reefer protects while it runs ----------------
    protected_by = cargo.get("protected_by_assets") or []
    if asset_id in protected_by and asset.get("powered_conditioning"):
        autonomy = float(asset.get("conditioning_autonomy_hours", 0.0))
        stopped = float(observation.get("delay_hours", 0.0))
        if stopped <= autonomy:
            clashes.append("conditioned_asset_protects_vulnerable_cargo")
            return PathwayVerdict(
                delay=delay,
                damage=GateDecision(
                    False,
                    f"{asset['label']} holds the load for {autonomy:.0f} h and "
                    f"the expected delay is {stopped:.0f} h. Protected — but "
                    f"this is a countdown, not immunity.",
                    clash_rule="conditioned_asset_protects_vulnerable_cargo",
                ),
                clashes_resolved=clashes,
                notes=[
                    f"Damage re-opens if the delay passes {autonomy:.0f} h.",
                ],
            )
        clashes.append("conditioned_asset_protects_vulnerable_cargo")
        notes.append(
            f"{asset['label']} ran out: {stopped:.0f} h of delay against "
            f"{autonomy:.0f} h of autonomy."
        )
    elif asset_id in protected_by:
        # Passive protection — an enclosed trailer against rain.
        return PathwayVerdict(
            delay=delay,
            damage=GateDecision(
                False,
                f"{asset.get('label', asset_id)} protects this cargo class.",
                clash_rule="conditioned_asset_protects_vulnerable_cargo",
            ),
            clashes_resolved=["conditioned_asset_protects_vulnerable_cargo"],
            notes=notes,
        )

    condition, hard, why = max(met, key=lambda m: m[1])
    irreversible = bool(condition.get("irreversible")) and hard
    scrap = float(cargo.get("scrap_fraction_if_damaged", 0.0)) if hard else 0.0

    if condition.get("condition") == "elapsed_hours_above":
        clashes.append("pot_life_makes_delay_into_damage")

    return PathwayVerdict(
        delay=delay,
        damage=GateDecision(True, f"{cargo['label']}: {why}."),
        irreversible=irreversible,
        scrap_fraction=scrap,
        clashes_resolved=clashes,
        notes=notes,
    )


def _variable_name(config: Config, variable_id: str) -> str:
    variable = config.variables.get(variable_id)
    return variable.name if variable else variable_id
