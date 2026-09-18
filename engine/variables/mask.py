"""The active/sleeping mask — the best idea in the design (BRIEF §4).

    m ∈ {0,1}^45          indicator mask, 1 = active for this event
    w_effective = w ⊙ m   masked elementwise

For any given event only a handful of variables are relevant. A drought on the
Rhine activates waterway condition; it has no bearing on port labour relations
in Singapore. Evaluating all 45 for every event manufactures correlations that
are not there and makes every event look the same.

The mask is also what makes a result explainable. "These four variables fired,
and here is why each one" is a sentence a planner can argue with. "Risk score
7.3" is not.

EXPOSURE IS DERIVED, NEVER STORED PER NODE
------------------------------------------
BRIEF §4 is explicit: do not give every node a 45-dimensional vulnerability
vector. With ~35 nodes that would be ~1,600 numbers nobody can justify, and
they would all end up as defaults. The rules below read a handful of node
attributes instead. Every one is explainable in a sentence, and a wrong one is
findable.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.schemas import Mode, Node, RiskVariable


@dataclass(frozen=True)
class ExposureVerdict:
    """Whether a variable can touch a node, and the sentence saying why."""

    exposed: bool
    reason: str


def node_exposed(node: Node, variable: RiskVariable) -> ExposureVerdict:
    """Can *variable* affect *node* at all?

    The rules are declared in variables.yaml under ``exposure`` and interpreted
    here. Keeping the interpretation in code and the declaration in config means
    a planner can read the ledger without reading Python, and an engineer can
    audit the logic in one screen.
    """
    rule = variable.exposure or {}

    if rule.get("any"):
        return ExposureVerdict(True, f"{variable.name} can affect any node")

    if rule.get("any_water"):
        if node.on_river or Mode.SEA in node.modes or Mode.BARGE in node.modes:
            return ExposureVerdict(
                True, f"{node.name} sits on water; {variable.name} applies"
            )
        return ExposureVerdict(
            False, f"{node.name} is not on water, so {variable.name} cannot apply"
        )

    if "on_river" in rule:
        if node.on_river == rule["on_river"]:
            river = node.river or "a river"
            return ExposureVerdict(
                True, f"{node.name} is on {river}; {variable.name} applies"
            )
        return ExposureVerdict(
            False, f"{node.name} is not on a river, so {variable.name} cannot apply"
        )

    if "kind_in" in rule:
        kinds = rule["kind_in"]
        if node.kind.value in kinds:
            return ExposureVerdict(
                True, f"{node.name} is a {node.kind.value}; {variable.name} applies"
            )
        return ExposureVerdict(
            False,
            f"{node.name} is a {node.kind.value}, not one of {', '.join(kinds)}",
        )

    if "chokepoint" in rule:
        if node.chokepoint == rule["chokepoint"]:
            return ExposureVerdict(
                True, f"{node.name} is a transit chokepoint; {variable.name} applies"
            )
        return ExposureVerdict(
            False, f"{node.name} is not a chokepoint, so {variable.name} cannot apply"
        )

    if "inland" in rule:
        if node.inland == rule["inland"]:
            where = "inland" if node.inland else "coastal"
            return ExposureVerdict(
                True, f"{node.name} is {where}; {variable.name} applies"
            )
        return ExposureVerdict(
            False, f"{node.name} does not meet the inland condition for {variable.name}"
        )

    # An unrecognised rule is a config bug. Refusing to guess is the whole
    # point of BRIEF §8.1 — a silent True here would quietly widen every event.
    return ExposureVerdict(
        False, f"exposure rule {rule!r} for {variable.id} is not understood"
    )


def mode_applies(variable: RiskVariable, mode: Mode) -> ExposureVerdict:
    """Does this variable apply to a leg run on *mode*?

    Condition 3 of the gate. A rail strike does not touch a sea leg, however
    dramatic the headline.
    """
    if mode in variable.modes_affected:
        return ExposureVerdict(
            True, f"{variable.name} affects {mode.value} legs"
        )
    affected = ", ".join(m.value for m in variable.modes_affected)
    return ExposureVerdict(
        False,
        f"{variable.name} affects {affected} only; this leg is {mode.value}",
    )


def build_mask(
    variable_ids: list[str],
    all_variables: dict[str, RiskVariable],
) -> dict[str, bool]:
    """The indicator vector itself.

    Returned as a dict rather than an array so that "which fired" survives into
    the UI without an index lookup nobody can read.
    """
    active = set(variable_ids)
    unknown = active - set(all_variables)
    if unknown:
        raise KeyError(f"mask references unknown variables: {sorted(unknown)}")
    return {vid: (vid in active) for vid in all_variables}


def active_count(mask: dict[str, bool]) -> int:
    return sum(1 for fired in mask.values() if fired)


def describe_mask(
    mask: dict[str, bool],
    all_variables: dict[str, RiskVariable],
) -> str:
    """One sentence a planner reads, naming the mechanism rather than the count.

    BRIEF §4: lead with the mechanism, not the number. Nobody asks whether you
    have enough variables; they ask how you know the right ones fired.
    """
    fired = [all_variables[vid].name for vid, on in mask.items() if on]
    if not fired:
        return "no variables active"
    if len(fired) == 1:
        return f"1 of {len(mask)} variables active: {fired[0]}"
    return (
        f"{len(fired)} of {len(mask)} variables active: "
        + ", ".join(fired[:-1])
        + f" and {fired[-1]}"
    )
