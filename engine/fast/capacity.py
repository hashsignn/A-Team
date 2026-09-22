"""Mixed-capacity planning: what actually moves the displaced volume.

THE GAP THIS CLOSES
-------------------
Until now every option in ``options.py`` was costed and timed but never
QUANTIFIED. A blocked barge leg would be replaced by a road reroute that
ranked first because it was fastest and cheapest per kilometre, and nothing
anywhere asked the only question a freight desk actually asks first:

    is there enough of it?

One reference push barge carries 2,450 t. One truck carries 24 t. Replacing a
single sailing therefore needs about 102 trucks, drawn from a European road
market that was short roughly half a million drivers in 2026. "Reroute by
road" is not wrong — it is incomplete, and the missing half is the half that
makes it undeliverable.

WHAT THIS MODULE IS
-------------------
An assignment of consignments to modes, under declared per-corridor ceilings,
four times over with four different preferences. It is deliberately greedy and
deliberately not a linear program, for one reason: a planner has to be able to
say WHY a consignment ended up on a truck, to a customer, on the phone, in one
sentence. "It was the cheapest mode still able to make your date" is such a
sentence. "It fell out of the simplex" is not.

THE FOUR PLANS
--------------
They are not four algorithms. They are one assignment loop with four
preferences, which is the honest shape — the difference between them is a
value judgement, not a technique.

    consolidate   High-capacity freight first, trucks for the remainder.
                  Fewest external units, lowest cost per tonne, slowest to
                  start. The plan you want if the deadline allows it.
    fastest       Whatever can be loaded soonest, cost unconstrained.
                  Most trucks, highest bill, earliest first movement.
    balanced      Per consignment, the CHEAPEST mode that still makes ITS
                  date. Neither the cheapest plan nor the fastest; the
                  cheapest one that does not cost a delivery.
    triage        Accepts that not all displaced volume has to move now.
                  Serves the tightest deadlines first and lets the volume
                  with slack wait for next week's capacity, inside a declared
                  ceiling on how much may be deferred.

WHAT A PLAN REPORTS
-------------------
Coverage, not just cost. The sentence a plan exists to produce is

    "this mix moves 78% of the displaced volume on time; the remaining 22%
     slips four days, and it needs 61 trucks against a ceiling of 90."

and every field here is there to make that sentence true.

WHERE THE NUMBERS COME FROM
---------------------------
``fast.yaml``'s ``capacity:`` block, every entry carrying a ``source``. The
tonnages are marked ``assumed``: Sika's own export describes specialist
product at a median of 3.7 kg per document, which is not the bulk flow a barge
carries, so a tonnage derived from it would be precise and wrong. Declared and
replaceable beats derived and misleading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from engine.clock import Clock
from engine.config import Config
from engine.fast import margin as margin_mod
from engine.schemas import Shipment

# A week. The ceilings are declared per week, so this is the divisor that turns
# them into "how much of it can I have in the next H hours".
WEEK_HOURS = 168.0

# Volume that does not fit this week's declared capacity is next week's. Not a
# guess about queues — a direct reading of what "units per week" means.
DEFER_WAIT_HOURS = WEEK_HOURS

# Used when the capacity block is missing entirely. A consignment with no
# declared tonnage still has to weigh something or every ceiling is infinite,
# which is the exact failure this module exists to remove.
FALLBACK_TONNES = 22.0

# The class a product family falls into when the equipment map does not name
# it. "packaged" is the permissive one — palletised goods that any equipment
# can take — which is right, because the map lists the families that NEED
# something special and silence means they do not.
DEFAULT_CLASS = "packaged"

# A mode's unit consolidates when it can hold more than one consignment. A
# barge takes a hundred; a truck takes one and a bit. Derived rather than
# declared so there is no third knob to keep consistent with the first two.
CONSOLIDATES_AT = 2.0


# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ModeCapacity:
    """One declared mode on one corridor."""

    name: str
    tonnes_per_unit: float
    units_per_week: float
    hours_to_ready: float
    cost_chf_per_tonne: float
    carries: frozenset[str]
    source: str = "assumed"
    note: str = ""

    @property
    def tonnes_per_week(self) -> float:
        return self.tonnes_per_unit * self.units_per_week

    @property
    def consolidates(self) -> bool:
        """Can one unit carry more than one consignment?"""
        return self.tonnes_per_unit >= CONSOLIDATES_AT * FALLBACK_TONNES

    def loading_hours(self, hours: float) -> float:
        """The part of ``hours`` this mode can actually load in.

        Time before it is ready is not capacity. Counting from zero would let
        a barge that needs 48 h to load offer a week of sailings to a 49-hour
        deadline, which is not a rounding error — it is the difference
        between a plan and a wish.
        """
        return max(0.0, min(hours, WEEK_HOURS) - self.hours_to_ready)

    def available_units(self, hours: float) -> int:
        """WHOLE vehicles or sailings obtainable inside ``hours``.

        Pro-rated over the loading window and then floored, and both halves
        matter. Pro-rating says capacity arrives evenly through the week
        rather than in a Monday block. Flooring says you cannot plan on part
        of a barge: half a sailing inside your window is a sailing you miss.
        Rounding the other way is how a plan comes back needing a vessel that
        leaves the day after the deadline.
        """
        return int(self.units_per_week * (self.loading_hours(hours) / WEEK_HOURS))

    def available_tonnes(self, hours: float) -> float:
        """The ceiling in tonnes — whole units only, so the two agree.

        Derived from units rather than computed alongside them, because a
        board that offers 1,050 t and zero sailings in the same row is a board
        nobody believes twice.
        """
        return self.available_units(hours) * self.tonnes_per_unit

    @property
    def headway_hours(self) -> float:
        """Hours between consecutive units.

        Six sailings a week is one every 28 hours, and the freight on the
        third one is not ready when the first one is. Without this, a plan
        that needs forty trucks reports all forty standing on the ramp at
        hour twelve, which is the single most flattering lie this module
        could tell.
        """
        if self.units_per_week <= 0:
            return WEEK_HOURS
        return WEEK_HOURS / self.units_per_week

    def ready_hours_for(self, unit_index: int) -> float:
        """When freight riding the Nth unit can actually leave."""
        return self.hours_to_ready + max(0, unit_index - 1) * self.headway_hours

    def units_needed_by(self, tonnes: float) -> float:
        """What one consignment of this weight consumes of the fleet.

        A consolidating mode is billed the fraction it fills — twenty tonnes
        of a 2,450 t barge is eight thousandths of a sailing. A truck is
        billed whole, because a part-full truck is still a truck and still a
        driver. Budgeting both in tonnes is what lets 274 half-empty trucks
        fit inside a ceiling of 195: the tonnage clears and the vehicles do
        not exist.
        """
        if self.tonnes_per_unit <= 0:
            return 0.0
        if self.consolidates:
            return tonnes / self.tonnes_per_unit
        return float(max(1, math.ceil(tonnes / self.tonnes_per_unit)))

    def units_to_replace(self, tonnes: float) -> int:
        """How many of THIS mode it takes to carry that much freight.

        The comparison the whole module exists to make sayable: one reference
        push barge is 2,450 t, one truck is 24 t, so the river closing does
        not cost you a sailing, it costs you a hundred and three drivers.
        """
        if tonnes <= 0 or self.tonnes_per_unit <= 0:
            return 0
        return math.ceil(tonnes / self.tonnes_per_unit)


@dataclass(frozen=True)
class Displaced:
    """One consignment that has lost its path, with what it weighs."""

    shipment_id: str
    customer: str
    tonnes: float
    equipment_class: str
    hours_of_slack: float         # delay it can absorb and still hit the date
    original_mode: str
    value_chf: float
    product_family: str


@dataclass
class Allocation:
    """One mode's share of one plan."""

    mode: str
    tonnes: float = 0.0
    # Fleet actually consumed, accumulated as consignments land rather than
    # re-derived from the total tonnage afterwards. A 26 t consignment needs
    # two trucks and a 12 t one needs a truck; totalling the tonnage first
    # loses both facts and under-reports the fleet by a third.
    units: float = 0.0
    # The ceiling this mode was actually assigned against, which is the
    # corridor's fleet after any derate. Stored rather than recomputed at
    # render time so the bar on screen cannot disagree with the budget the
    # allocator used.
    units_available: int = 0
    shipment_ids: list[str] = field(default_factory=list)
    cost_chf: float = 0.0
    # When each consignment on this mode can leave — the mode's readiness
    # plus the headway of whichever unit it ended up on. Kept per consignment
    # rather than per mode because that is the granularity the lateness and
    # the margin are both computed at, and a single mode-level number would
    # have to be either the first truck or the last.
    ready_hours: dict[str, float] = field(default_factory=dict)

    @property
    def whole_units(self) -> int:
        return math.ceil(self.units - 1e-9)

    def as_dict(self, capacity: ModeCapacity, hours: float) -> dict:
        available = self.units_available
        return {
            "mode": self.mode,
            "tonnes": round(self.tonnes, 1),
            "shipments": len(self.shipment_ids),
            "shipment_ids": sorted(self.shipment_ids),
            "units": self.whole_units,
            "unit_name": unit_name(self.mode, self.whole_units),
            "unit_name_available": unit_name(self.mode, available),
            "units_available": available,
            "tonnes_available": round(available * capacity.tonnes_per_unit, 1),
            "share_of_ceiling": (
                round(self.units / available, 3) if available else None
            ),
            "hours_to_ready": capacity.hours_to_ready,
            "hours_to_last_away": round(max(self.ready_hours.values()), 1)
            if self.ready_hours else capacity.hours_to_ready,
            "cost_chf": round(self.cost_chf, 2),
            "source": capacity.source,
        }


_UNIT_NAMES = {
    "barge": "sailings",
    "rail": "train slots",
    "road": "trucks",
    "sea": "sailings",
}


def unit_name(mode: str, count: float = 2) -> str:
    """What one of this mode's units is called, in the right number.

    "1 train slots" is the kind of thing that makes a reader stop trusting
    the rest of the sentence, and the rest of the sentence is where the
    numbers are.
    """
    plural = _UNIT_NAMES.get(mode, "units")
    if abs(count) == 1:
        return plural[:-1] if plural.endswith("s") else plural
    return plural


@dataclass(frozen=True)
class Plan:
    """One way to move the displaced volume, quantified."""

    plan_id: str
    label: str
    thesis: str                       # the one-line argument for this plan
    # Every consignment the plan is answerable for, allocated or not. Carried
    # rather than derived so the plan can price itself and state its own
    # coverage without the caller having to hand back the input it was built
    # from — and so "100% covered" can never mean "we dropped the rest".
    members: tuple[Displaced, ...]
    allocations: tuple[Allocation, ...]
    deferred: tuple[Displaced, ...]
    horizon_hours: float
    extra_cost_chf: float
    worst_days_late: float
    late_shipments: int
    hours_to_first_move: float
    limits: tuple[str, ...]           # what ran out, in words
    # Did the plan actually fail to move freight it should have moved?
    # Deliberately NOT "limits is non-empty". A plan that books the last
    # truck on the corridor and still lands everything on the date has hit a
    # ceiling, which is worth saying, and has not come up short, which is a
    # different sentence and the one a planner acts on.
    short: bool = False
    margin: margin_mod.Margin | None = None
    # Consignments this plan individually pushes under the floor, even where
    # the block as a whole still pays. Reported, never vetoed: spending one
    # consignment's contribution to keep a customer is a decision a planner
    # is allowed to make, and a decision they cannot make if nobody tells
    # them they are making it.
    unprofitable_ids: tuple[str, ...] = ()
    # Other strategies that produced exactly this assignment. When the
    # corridor is tight enough there is only one answer, and four tabs
    # showing it four times is worse than one tab saying so — "every strategy
    # lands here" is itself the finding.
    also: tuple[str, ...] = ()

    @property
    def displaced_tonnes(self) -> float:
        return round(sum(d.tonnes for d in self.members), 1)

    @property
    def covered_tonnes(self) -> float:
        return round(sum(a.tonnes for a in self.allocations), 1)

    def slack_of(self, shipment_id: str) -> float:
        for item in self.members:
            if item.shipment_id == shipment_id:
                return item.hours_of_slack
        return 0.0

    @property
    def deferred_tonnes(self) -> float:
        return round(sum(d.tonnes for d in self.deferred), 1)

    @property
    def coverage(self) -> float:
        """Share of displaced tonnage that moves inside the horizon."""
        if self.displaced_tonnes <= 0:
            return 1.0
        return round(self.covered_tonnes / self.displaced_tonnes, 3)

    @property
    def on_time(self) -> bool:
        return self.worst_days_late <= 0.0 and not self.deferred

    @property
    def feasible(self) -> bool:
        return not self.short

    @property
    def viable(self) -> bool:
        """Does it still pay? Same veto as an option, same reason it exists."""
        return self.margin is None or self.margin.viable

    @property
    def rank_key(self) -> tuple:
        """Delivery first, exactly as ``FastOption.rank_key`` is.

        Coverage leads because a plan that moves 60% of the freight has not
        solved the lane however fast the 60% arrives; then lateness, then
        how soon anything moves at all, and money only as the tie-break.
        """
        return (
            not self.feasible,
            -round(self.coverage, 3),
            round(self.worst_days_late, 3),
            round(self.hours_to_first_move, 2),
            round(self.extra_cost_chf, 2),
        )

    def sentence(self) -> str:
        """The line the solution board puts under the tab heading."""
        pct = f"{self.coverage * 100:.0f}%"
        if self.coverage >= 1.0 and self.worst_days_late <= 0:
            return (
                f"Moves all {self.displaced_tonnes:,.0f} t on the agreed date."
            )
        head = f"Moves {pct} of {self.displaced_tonnes:,.0f} t"
        if self.worst_days_late > 0:
            head += f", worst case {self.worst_days_late:.1f} day(s) late"
        if self.deferred:
            head += (
                f"; {self.deferred_tonnes:,.0f} t "
                f"({len(self.deferred)} consignment(s)) waits for next week"
            )
        return head + "."

    def as_dict(self, capacities: dict[str, ModeCapacity]) -> dict:
        return {
            "plan_id": self.plan_id,
            "label": self.label,
            "thesis": self.thesis,
            "sentence": self.sentence(),
            "horizon_hours": round(self.horizon_hours, 1),
            "displaced_tonnes": round(self.displaced_tonnes, 1),
            "covered_tonnes": self.covered_tonnes,
            "deferred_tonnes": self.deferred_tonnes,
            "coverage": self.coverage,
            "extra_cost_chf": round(self.extra_cost_chf, 2),
            "worst_days_late": round(self.worst_days_late, 2),
            "late_shipments": self.late_shipments,
            "hours_to_first_move": round(self.hours_to_first_move, 1),
            "on_time": self.on_time,
            "feasible": self.feasible,
            "at_ceiling": [
                limit for limit in self.limits if "at its ceiling" in limit
            ],
            "viable": self.viable,
            "limits": list(self.limits),
            "also": list(self.also),
            "allocations": [
                a.as_dict(capacities[a.mode], self.horizon_hours)
                for a in self.allocations
            ],
            "deferred": [
                {
                    "shipment_id": d.shipment_id,
                    "customer": d.customer,
                    "tonnes": round(d.tonnes, 1),
                    "days_late": round(
                        max(0.0, DEFER_WAIT_HOURS - d.hours_of_slack) / 24.0, 2
                    ),
                }
                for d in self.deferred
            ],
            "margin_chf": self.margin.margin_chf if self.margin else None,
            "margin_viable": self.margin.viable if self.margin else None,
            "unprofitable_shipments": len(self.unprofitable_ids),
            "unprofitable_ids": list(self.unprofitable_ids),
            "vetoed_because": (
                self.margin.reason if self.margin and not self.margin.viable else None
            ),
        }


# ---------------------------------------------------------------------
# Reading the declared block.
# ---------------------------------------------------------------------
def _block(config: Config) -> dict:
    raw = config.raw("fast")
    fast_cfg = raw if isinstance(raw, dict) else {}
    block = fast_cfg.get("capacity")
    return block if isinstance(block, dict) else {}


def _value(entry, fallback: float) -> float:
    """Read a ``{value: n, source: ...}`` entry, or a bare number."""
    if isinstance(entry, dict) and isinstance(entry.get("value"), (int, float)):
        return float(entry["value"])
    if isinstance(entry, (int, float)):
        return float(entry)
    return float(fallback)


def _source(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("source", "assumed"))
    return "assumed"


def tonnes_of(config: Config, product_family: str) -> float:
    """What one consignment of this family weighs."""
    block = _block(config).get("tonnes_per_consignment", {}) or {}
    families = block.get("by_product_family", {}) or {}
    entry = families.get(product_family)
    if entry is None:
        entry = block.get("default")
    return _value(entry, FALLBACK_TONNES)


def equipment_class(config: Config, product_family: str) -> str:
    """Which equipment this family needs — the 4flow point, encoded.

    Not every truck can move every load. A tank trailer is not a curtainsider
    and a walking floor is neither, so a mode that cannot carry the class is
    not short of capacity for it, it has none.
    """
    equipment = _block(config).get("equipment", {}) or {}
    for name, families in equipment.items():
        if product_family in (families or []):
            return str(name)
    return DEFAULT_CLASS


def modes(config: Config) -> dict[str, ModeCapacity]:
    """Declared corridor modes, keyed by name."""
    declared = _block(config).get("modes", {}) or {}
    out: dict[str, ModeCapacity] = {}
    for name, entry in declared.items():
        if not isinstance(entry, dict):
            continue
        out[str(name)] = ModeCapacity(
            name=str(name),
            tonnes_per_unit=_value(entry.get("tonnes_per_unit"), FALLBACK_TONNES),
            units_per_week=_value(entry.get("units_per_week"), 0.0),
            hours_to_ready=_value(entry.get("hours_to_ready"), 0.0),
            cost_chf_per_tonne=_value(entry.get("cost_chf_per_tonne"), 0.0),
            carries=frozenset(entry.get("carries") or [DEFAULT_CLASS]),
            source=_source(entry.get("tonnes_per_unit")),
            note=str(entry.get("note", "")).strip(),
        )
    return out


def defer_share(config: Config) -> float:
    """How much of the displaced volume may be left for next week."""
    entry = (_block(config).get("defer", {}) or {}).get("max_share")
    return max(0.0, min(1.0, _value(entry, 0.0)))


# ---------------------------------------------------------------------
def hours_of_slack(shipment: Shipment, clock: Clock) -> float:
    """Delay this consignment can absorb and still land on the agreed date.

    Measured from now, against the planned arrival rather than the planned
    departure: a consignment already three days into a ten-day lane has spent
    none of its commitment slack, and charging it for elapsed transit would
    make every long lane look critical from the moment it leaves.
    """
    remaining = max(0.0, (shipment.eta - clock.as_of).total_seconds() / 3600.0)
    to_commit = (
        shipment.otif_committed_date - clock.as_of
    ).total_seconds() / 3600.0
    return to_commit - remaining


def displaced_from(
    shipments: list[Shipment],
    config: Config,
    clock: Clock,
) -> list[Displaced]:
    """Turn consignments into tonnage, which is the unit capacity is in."""
    return [
        Displaced(
            shipment_id=s.shipment_id,
            customer=s.customer,
            tonnes=tonnes_of(config, s.product_family),
            equipment_class=equipment_class(config, s.product_family),
            hours_of_slack=hours_of_slack(s, clock),
            original_mode=_original_mode(s),
            value_chf=float(s.value_chf),
            product_family=s.product_family,
        )
        for s in shipments
    ]


def _original_mode(shipment: Shipment) -> str:
    """The mode the consignment was actually planned on.

    The declared ``mode`` field says "multimodal" for anything with more than
    one leg, which is true and useless for pricing a switch. The mode carrying
    the most legs is the one the plan was built around.
    """
    counts: dict[str, int] = {}
    for leg in shipment.legs:
        name = leg.mode.value if hasattr(leg.mode, "value") else str(leg.mode)
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return str(shipment.mode)
    return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


def _baseline_rate(capacities: dict[str, ModeCapacity], mode: str) -> float:
    """What the ORIGINAL mode cost per tonne, for pricing the difference.

    A switch is charged the difference, never the whole move — the freight was
    always going to cost something. When the original mode has no declared
    ceiling (sea, typically, which this corridor model does not cover) the
    cheapest declared rate stands in, which makes the switch look MORE
    expensive than it is. That is the correct direction to be wrong in: it
    vetoes more, not less.
    """
    known = capacities.get(mode)
    if known is not None:
        return known.cost_chf_per_tonne
    if not capacities:
        return 0.0
    return min(c.cost_chf_per_tonne for c in capacities.values())


# ---------------------------------------------------------------------
# The assignment loop. One engine, four preferences.
# ---------------------------------------------------------------------
def _assign(
    plan_id: str,
    label: str,
    thesis: str,
    displaced: list[Displaced],
    capacities: dict[str, ModeCapacity],
    horizon_hours: float,
    *,
    blocked: frozenset[str],
    share: dict[str, float],
    order_consignments,
    prefer_modes,
    defer_cap_tonnes: float,
) -> Plan:
    """Fill consignments into modes under declared ceilings.

    ``order_consignments`` decides who is served first and ``prefer_modes``
    decides what each one is offered, in order. Everything else — the
    ceilings, the equipment gate, the deferral cap, the pricing — is identical
    across all four plans, which is what makes them comparable at all.
    """
    # Budgeted in UNITS, which is the currency the corridor is actually
    # short of. Tonnes are what the freight weighs; sailings and drivers are
    # what runs out.
    fleet = {
        name: float(
            math.floor(cap.available_units(horizon_hours) * share.get(name, 1.0))
        )
        for name, cap in capacities.items()
        if name not in blocked
    }
    used: dict[str, float] = dict.fromkeys(fleet, 0.0)
    allocations: dict[str, Allocation] = {}
    deferred: list[Displaced] = []
    limits: list[str] = []
    extra_cost = 0.0
    worst_late = 0.0
    late_count = 0
    deferred_tonnes = 0.0
    no_equipment: set[str] = set()

    for item in order_consignments(displaced):
        placed = False
        offered = 0
        for name in prefer_modes(item):
            cap = capacities.get(name)
            if cap is None or name in blocked:
                continue
            if item.equipment_class not in cap.carries:
                continue
            offered += 1
            need = cap.units_needed_by(item.tonnes)
            if math.ceil(used.get(name, 0.0) + need - 1e-9) > fleet.get(name, 0.0):
                continue

            used[name] = used.get(name, 0.0) + need
            row = allocations.setdefault(
                name,
                Allocation(mode=name, units_available=int(fleet.get(name, 0.0))),
            )
            ready = cap.ready_hours_for(math.ceil(row.units + need - 1e-9))
            row.units += need
            row.tonnes += item.tonnes
            row.shipment_ids.append(item.shipment_id)
            row.ready_hours[item.shipment_id] = ready
            premium = max(
                0.0,
                cap.cost_chf_per_tonne - _baseline_rate(capacities, item.original_mode),
            )
            row.cost_chf += premium * item.tonnes
            extra_cost += premium * item.tonnes

            days_late = max(0.0, ready - item.hours_of_slack) / 24.0
            if days_late > 0:
                late_count += 1
            worst_late = max(worst_late, days_late)
            placed = True
            break

        if placed:
            continue

        if offered == 0:
            no_equipment.add(item.equipment_class)
        deferred.append(item)
        deferred_tonnes += item.tonnes
        worst_late = max(
            worst_late,
            max(0.0, DEFER_WAIT_HOURS - item.hours_of_slack) / 24.0,
        )
        late_count += 1

    short = bool(no_equipment) or deferred_tonnes > defer_cap_tonnes + 1e-9
    if deferred_tonnes > defer_cap_tonnes + 1e-9:
        over = deferred_tonnes - defer_cap_tonnes
        limits.append(
            f"{deferred_tonnes:,.0f} t cannot be moved inside "
            f"{horizon_hours:,.0f} h — {over:,.0f} t beyond what may be deferred"
        )
    for missing in sorted(no_equipment):
        limits.append(
            f"no available mode carries {missing} on this corridor"
        )
    for name in sorted(capacities):
        if name in blocked:
            continue
        available = fleet.get(name, 0.0)
        if available and math.ceil(used.get(name, 0.0)) >= available:
            derated = share.get(name, 1.0)
            note = (
                f" — and it is running at {derated * 100:.0f}% of normal"
                if derated < 1.0 else ""
            )
            limits.append(
                f"{name} is at its ceiling — {available:,.0f} "
                f"{unit_name(name, available)} is all this corridor has "
                f"inside {horizon_hours:,.0f} h{note}"
            )
        elif deferred and not available and share.get(name, 1.0) < 1.0:
            # Only when the plan actually came up short. A mode squeezed to
            # nothing on a lane whose freight all fits elsewhere has cost
            # this plan nothing, and listing it under "what runs out" would
            # mark a perfectly executable plan infeasible for a shortage it
            # never felt.
            limits.append(
                f"{name} is derated to {share[name] * 100:.0f}% of normal, "
                f"which leaves nothing usable inside {horizon_hours:,.0f} h"
            )

    ordered = sorted(
        allocations.values(), key=lambda a: -capacities[a.mode].tonnes_per_unit
    )
    first_move = min(
        (h for a in ordered for h in a.ready_hours.values()),
        default=DEFER_WAIT_HOURS,
    )

    return Plan(
        plan_id=plan_id,
        label=label,
        thesis=thesis,
        members=tuple(displaced),
        allocations=tuple(ordered),
        deferred=tuple(deferred),
        horizon_hours=horizon_hours,
        extra_cost_chf=round(extra_cost, 2),
        worst_days_late=round(worst_late, 3),
        late_shipments=late_count,
        hours_to_first_move=first_move,
        limits=tuple(limits),
        short=short,
    )


# --- the four preferences -------------------------------------------
def _by_deadline(items: list[Displaced]) -> list[Displaced]:
    """Tightest first. The default, and the only defensible dispatch rule:
    serving a consignment with a week of slack ahead of one due tomorrow is
    how a plan that looks fine on paper loses a delivery."""
    return sorted(items, key=lambda d: (d.hours_of_slack, -d.tonnes, d.shipment_id))


def _by_slack_then_value(items: list[Displaced]) -> list[Displaced]:
    """Tightest first, and among equals the most valuable — the one whose
    customer notices."""
    return sorted(
        items, key=lambda d: (d.hours_of_slack, -d.value_chf, d.shipment_id)
    )


def _order_by(capacities: dict[str, ModeCapacity], key):
    return [name for name, _ in sorted(capacities.items(), key=lambda kv: key(kv[1]))]


def plans(
    displaced: list[Displaced],
    config: Config,
    *,
    blocked_modes: frozenset[str] | set[str] = frozenset(),
    derate: dict[str, float] | None = None,
    horizon_hours: float | None = None,
) -> list[Plan]:
    """The three-to-four mixes, ranked delivery-first.

    The disruption arrives here in two forms, because disruptions come in two
    forms. ``blocked_modes`` is the clean case: a closed river takes barge off
    the board entirely and every plan has to work without it.

    ``derate`` is the commoner and more interesting one. "Rhine low water at
    Kaub — loading restricted to 45%" is not a closure; the barges still sail,
    they sail half empty, and a model that can only say open or shut has to
    round that to one or the other. Both roundings are wrong: calling it shut
    invents an emergency, calling it open misses the one that is happening.
    So a mode's ceiling is scaled, and 0.0 is simply where the scale ends.
    """
    capacities = modes(config)
    if not capacities or not displaced:
        return []

    blocked = frozenset(blocked_modes)
    share = {k: max(0.0, min(1.0, v)) for k, v in (derate or {}).items()}
    horizon = (
        horizon_hours
        if horizon_hours is not None
        else max(1.0, max(d.hours_of_slack for d in displaced))
    )
    cap_t = defer_share(config) * sum(d.tonnes for d in displaced)

    big_first = _order_by(capacities, lambda c: -c.tonnes_per_unit)
    soon_first = _order_by(capacities, lambda c: c.hours_to_ready)
    cheap_first = _order_by(capacities, lambda c: c.cost_chf_per_tonne)

    def cheapest_that_holds(item: Displaced) -> list[str]:
        """Cheapest mode that still makes THIS consignment's date, then the
        rest in cost order as a fallback — because a plan that refuses to
        move a consignment at all is worse than one that moves it late."""
        holds = [
            n for n in cheap_first
            if capacities[n].hours_to_ready <= item.hours_of_slack
        ]
        return holds + [n for n in cheap_first if n not in holds]

    built = [
        _assign(
            "consolidate",
            "Freight first, trucks for the rest",
            "Fill the high-capacity modes before touching the road market. "
            "Fewest external units, lowest cost per tonne — and the slowest "
            "to start, which is the price of it.",
            displaced, capacities, horizon,
            blocked=blocked, share=share,
            order_consignments=_by_deadline,
            prefer_modes=lambda _item: big_first,
            defer_cap_tonnes=cap_t,
        ),
        _assign(
            "fastest",
            "Everything that can move now",
            "Whatever loads soonest takes the freight, cost unconstrained. "
            "Earliest first movement, most trucks, biggest bill.",
            displaced, capacities, horizon,
            blocked=blocked, share=share,
            order_consignments=_by_deadline,
            prefer_modes=lambda _item: soon_first,
            defer_cap_tonnes=cap_t,
        ),
        _assign(
            "balanced",
            "Cheapest mix that still holds the date",
            "Per consignment, the cheapest mode that still makes ITS date. "
            "Not the cheapest plan and not the fastest — the cheapest one "
            "that does not cost a delivery.",
            displaced, capacities, horizon,
            blocked=blocked, share=share,
            order_consignments=_by_deadline,
            prefer_modes=cheapest_that_holds,
            defer_cap_tonnes=cap_t,
        ),
    ]

    # Triage only earns a tab when there is something to triage. Offering
    # "defer some of it" on a block where nothing has slack is a tab that
    # cannot be pressed, and a board of four where one is dead is worse than
    # a board of three.
    if cap_t > 0 and any(d.hours_of_slack > DEFER_WAIT_HOURS / 2 for d in displaced):
        built.append(
            _assign(
                "triage",
                "Move what cannot wait, defer what can",
                "Not all displaced volume has to move now. The tightest "
                f"deadlines get the capacity; up to "
                f"{defer_share(config) * 100:.0f}% of the tonnage waits for "
                "next week's sailings.",
                _triage_set(displaced, cap_t), capacities, horizon,
                blocked=blocked, share=share,
                order_consignments=_by_slack_then_value,
                prefer_modes=cheapest_that_holds,
                defer_cap_tonnes=cap_t,
            )
        )
        built[-1] = _restore_deferred(built[-1], displaced, cap_t)

    return _distinct(sorted(built, key=lambda p: p.rank_key))


def _signature(plan: Plan) -> tuple:
    """Two plans are the same plan when they move the same freight the same way."""
    return (
        tuple(sorted(
            (sid, a.mode) for a in plan.allocations for sid in a.shipment_ids
        )),
        tuple(sorted(d.shipment_id for d in plan.deferred)),
    )


def _distinct(ranked: list[Plan]) -> list[Plan]:
    """Collapse strategies that reached identical assignments.

    The best-ranked survivor keeps the tab and names the others. This is not
    tidying: on a corridor with one mode left every strategy converges, and a
    planner who sees that stops looking for a cleverer mix.
    """
    out: list[Plan] = []
    index: dict[tuple, int] = {}
    for plan in ranked:
        key = _signature(plan)
        seen = index.get(key)
        if seen is None:
            index[key] = len(out)
            out.append(plan)
            continue
        kept = out[seen]
        out[seen] = replace(kept, also=kept.also + (plan.label,))
    return out


def _triage_set(displaced: list[Displaced], cap_t: float) -> list[Displaced]:
    """Hold back the slackest consignments, up to the declared cap.

    Done BEFORE the assignment rather than inside it, because deferring by
    choice and deferring because nothing was left are different facts and a
    plan that blurs them cannot be argued with.
    """
    by_slack = sorted(displaced, key=lambda d: -d.hours_of_slack)
    held: list[str] = []
    running = 0.0
    for item in by_slack:
        if item.hours_of_slack <= DEFER_WAIT_HOURS / 2:
            break
        if running + item.tonnes > cap_t:
            break
        held.append(item.shipment_id)
        running += item.tonnes
    holding = set(held)
    return [d for d in displaced if d.shipment_id not in holding]


def _restore_deferred(
    plan: Plan, everyone: list[Displaced], cap_tonnes: float
) -> Plan:
    """Put the deliberately-held consignments back into the plan's totals.

    They were removed so they would not compete for capacity. They are still
    displaced, they are still somebody's freight, and a plan that reports 100%
    coverage by quietly dropping a third of the volume is the dishonest
    version of this whole module.
    """
    placed = {sid for a in plan.allocations for sid in a.shipment_ids}
    accounted = placed | {d.shipment_id for d in plan.deferred}
    held = [d for d in everyone if d.shipment_id not in accounted]
    if not held:
        return plan

    worst = plan.worst_days_late
    for item in held:
        worst = max(worst, max(0.0, DEFER_WAIT_HOURS - item.hours_of_slack) / 24.0)

    deferred = plan.deferred + tuple(held)
    deferred_t = sum(d.tonnes for d in deferred)
    # Restoring the held volume can push the plan past the deferral cap, and
    # the cap is the whole point: a triage that quietly defers everything is
    # not a plan, it is a shrug. Re-checked here rather than trusted from the
    # assignment, which only ever saw the reduced set.
    limits = tuple(x for x in plan.limits if "beyond what may be deferred" not in x)
    short = any("carries" in x for x in limits)
    if deferred_t > cap_tonnes + 1e-9:
        short = True
        limits += (
            f"{deferred_t:,.0f} t cannot be moved inside "
            f"{plan.horizon_hours:,.0f} h — "
            f"{deferred_t - cap_tonnes:,.0f} t beyond what may be deferred",
        )

    return Plan(
        plan_id=plan.plan_id,
        label=plan.label,
        thesis=plan.thesis,
        members=tuple(everyone),
        allocations=plan.allocations,
        deferred=deferred,
        horizon_hours=plan.horizon_hours,
        extra_cost_chf=plan.extra_cost_chf,
        worst_days_late=round(worst, 3),
        late_shipments=plan.late_shipments + len(held),
        hours_to_first_move=plan.hours_to_first_move,
        limits=limits,
        short=short,
        margin=plan.margin,
    )


# ---------------------------------------------------------------------
# The margin veto, applied to a whole plan.
# ---------------------------------------------------------------------
def price(
    plan: Plan,
    shipments: dict[str, Shipment],
    config: Config,
    capacities: dict[str, ModeCapacity] | None = None,
) -> Plan:
    """Attach the block's margin, computed consignment by consignment.

    Two numbers come out of this and they answer different questions.

    The BLOCK margin is the veto. A plan is a decision about a lane, so it is
    a lane's contribution it has to stay inside, and that is the number a
    controller signs off.

    The CASUALTY COUNT is the warning. A plan can keep the block comfortably
    profitable while pushing three individual consignments under water, and
    summing first would hide exactly that. So each consignment is priced on
    its own — its own contribution, its own share of the premium, its own
    residual penalty — and the ones that end up negative are named. They do
    not veto the plan. They are the sentence "this holds the date, and three
    consignments pay for it", which a planner is entitled to hear before
    pressing the button rather than afterwards.
    """
    capacities = capacities if capacities is not None else modes(config)
    by_id = {d.shipment_id: d for d in plan.members}

    contribution = 0.0
    penalty = 0.0
    rate_sources: set[str] = set()
    unprofitable: list[str] = []

    def account(sid: str, days_late: float, own_cost: float) -> None:
        nonlocal contribution, penalty
        shipment = shipments.get(sid)
        if shipment is None:
            return
        rate, source = margin_mod.margin_rate(config, shipment.product_family)
        rate_sources.add(source)
        own_contribution = float(shipment.value_chf) * rate
        own_penalty = margin_mod.residual_penalty_chf(shipment, config, days_late)
        contribution += own_contribution
        penalty += own_penalty
        if own_contribution - own_cost - own_penalty < 0:
            unprofitable.append(sid)

    for allocation in plan.allocations:
        cap = capacities.get(allocation.mode)
        fallback = cap.hours_to_ready if cap else 0.0
        for sid in allocation.shipment_ids:
            item = by_id.get(sid)
            ready = allocation.ready_hours.get(sid, fallback)
            slack = item.hours_of_slack if item else 0.0
            own_cost = 0.0
            if cap is not None and item is not None:
                premium = max(
                    0.0,
                    cap.cost_chf_per_tonne
                    - _baseline_rate(capacities, item.original_mode),
                )
                own_cost = premium * item.tonnes
            account(sid, max(0.0, ready - slack) / 24.0, own_cost)

    for item in plan.deferred:
        account(
            item.shipment_id,
            max(0.0, DEFER_WAIT_HOURS - item.hours_of_slack) / 24.0,
            0.0,
        )

    floor = margin_mod.floor_chf(config)
    margin = round(contribution - plan.extra_cost_chf - penalty, 2)
    viable = margin >= floor
    reason = None
    if not viable:
        reason = (
            f"the block would leave CHF {margin:,.0f} against a floor of "
            f"CHF {floor:,.0f}"
        )

    return Plan(
        plan_id=plan.plan_id,
        label=plan.label,
        thesis=plan.thesis,
        members=plan.members,
        allocations=plan.allocations,
        deferred=plan.deferred,
        horizon_hours=plan.horizon_hours,
        extra_cost_chf=plan.extra_cost_chf,
        worst_days_late=plan.worst_days_late,
        late_shipments=plan.late_shipments,
        hours_to_first_move=plan.hours_to_first_move,
        limits=plan.limits,
        short=plan.short,
        margin=margin_mod.Margin(
            contribution_chf=round(contribution, 2),
            action_cost_chf=round(plan.extra_cost_chf, 2),
            residual_penalty_chf=round(penalty, 2),
            margin_chf=margin,
            floor_chf=floor,
            viable=viable,
            reason=reason,
            rate_source="; ".join(sorted(rate_sources)) or "assumed",
        ),
        unprofitable_ids=tuple(sorted(unprofitable)),
    )
