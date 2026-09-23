"""The synthetic manifest: vehicle, crew, containers. LABELLED SYNTHETIC.

The shipment book has a value, a product family and a customer commitment. It
has no container list, no vessel name and nobody's name at the wheel — a TMS
would supply those, and none is connected. The Action Hub needs them to be
readable, so they are generated here and every payload that carries them says
``synthetic: true``. BRIEF §12: synthetic is fine, presenting it as real is not.

DETERMINISTIC, NOT RANDOM
-------------------------
Seeded off the shipment id, so the same consignment has the same containers on
every page load and every as-of — a manifest that reshuffled on refresh would
look like cargo moving between boxes.

THE CONTAINER IDS VALIDATE
--------------------------
ISO 6346, owner code ``SYN`` (plainly synthetic), with a real check digit. A
TMS integration test that feeds these to a validator should pass, and a
planner who knows the format should not be able to spot them by arithmetic.

WHY SOME CONTAINERS ARE CRITICAL
--------------------------------
A consignment is rarely one order. Some boxes feed a customer's line on the
committed date; the rest are replenishment with days of slack. That split is
what makes load-splitting worth anything: moving only the boxes that would
otherwise miss is cheaper than moving the barge, and it is the decision a
planner actually takes on a low-water week.
"""

from __future__ import annotations

import hashlib
import random
from datetime import timedelta

from engine.schemas import Mode, Shipment

VEHICLE_NAMES = {
    "sea": ["MV Aurora Bay", "MV Cape Meridian", "MV Levant Trader", "MV Pacific Lark",
            "MV Baltic Wren", "MV Nordic Crest", "MV Solent Pride", "MV Coral Ridge"],
    "barge": ["MS Rheinfalke", "MS Loreley", "MS Basilea", "MS Kaubstern",
              "MS Mittelrhein", "MS Lorelei II", "MS Nahe"],
    "rail": ["Block train", "Intermodal shuttle", "Container train"],
    "road": ["Truck"],
}
VEHICLE_PREFIX = {"sea": "VES", "barge": "BRG", "rail": "TRN", "road": "TRK", "air": "AIR"}
CREW_ROLE = {"sea": "Captain", "barge": "Skipper", "rail": "Train driver",
             "road": "Driver", "air": "Captain"}
CREW = ["J. Moreau", "K. Albrecht", "T. Visser", "L. Rossi", "A. Novak", "S. Brandt",
        "M. Ferreira", "D. Okafor", "R. Lindqvist", "P. Janssen", "H. Yilmaz",
        "C. Dubois", "E. Kowalski", "N. Haddad", "B. Schmid", "F. Castillo"]

# Asset capacity in TEU, by mode: (low, high). Synthetic, plausible.
CAPACITY_TEU = {"sea": (4000, 14000), "barge": (96, 208), "rail": (60, 90), "road": (2, 2)}
# How many of OUR containers a consignment of this main mode carries.
OUR_BOXES = {"sea": (3, 10), "barge": (4, 12), "rail": (3, 8), "road": (1, 2)}

PRODUCT_LABEL = {
    "sealants": "Sealants", "adhesives": "Adhesives",
    "concrete_admixtures": "Concrete admixtures", "waterproofing": "Waterproofing",
    "flooring_resins": "Flooring resins", "mortars": "Mortars",
    "roofing_membranes": "Roofing membranes", "structural_bonding": "Structural bonding",
}

_ISO_LETTERS = {}
_value = 10
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if _value % 11 == 0:
        _value += 1
    _ISO_LETTERS[_ch] = _value
    _value += 1


def iso6346_check_digit(code10: str) -> int:
    """The check digit for an owner code + category + 6-digit serial."""
    total = 0
    for i, ch in enumerate(code10):
        v = _ISO_LETTERS[ch] if ch.isalpha() else int(ch)
        total += v * (2 ** i)
    return total % 11 % 10


def container_id(serial: int) -> str:
    code = f"SYNU{serial:06d}"
    return f"{code}{iso6346_check_digit(code)}"


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def main_mode(shipment: Shipment) -> str:
    """The mode that carries the consignment furthest — it decides the box count."""
    longest = max(shipment.legs, key=lambda leg: (leg.planned_arrive - leg.planned_depart))
    return longest.mode.value


def vehicle(shipment: Shipment, leg_index: int) -> dict:
    """The vehicle carrying this consignment on one leg, and who is driving it.

    A consignment changes vehicle at every mode change — the truck to Basel is
    not the barge to Rotterdam — so identity is per leg.
    """
    leg = shipment.legs[leg_index]
    mode = leg.mode.value
    rng = _rng(shipment.shipment_id, "vehicle", leg_index)
    names = VEHICLE_NAMES.get(mode, ["Vehicle"])
    base = rng.choice(names)
    number = rng.randint(1000, 9999)
    if mode == "rail":
        name = f"{base} {number}"
    elif mode == "road":
        name = f"{base} {rng.choice(['ZH', 'BS', 'S', 'KA', 'GE'])} {number}"
    else:
        name = base
    low, high = CAPACITY_TEU.get(mode, (2, 2))
    capacity = rng.randint(low, high) if high > low else low
    if mode in ("sea", "barge"):
        capacity = int(round(capacity / 2.0)) * 2
    return {
        "asset_id": f"{VEHICLE_PREFIX.get(mode, 'VEH')}-{number}",
        "name": name,
        "mode": mode,
        "carrier": leg.carrier,
        "capacity_teu": capacity,
        "crew": {"name": rng.choice(CREW), "role": CREW_ROLE.get(mode, "Operator")},
        "synthetic": True,
    }


def containers(shipment: Shipment) -> list[dict]:
    """Our boxes on this consignment, each with its own deadline."""
    rng = _rng(shipment.shipment_id, "containers")
    mode = main_mode(shipment)
    low, high = OUR_BOXES.get(mode, (1, 2))
    count = rng.randint(low, high)

    committed = shipment.otif_committed_date
    critical_share = 0.35 if count > 2 else 0.5
    out = []
    for i in range(count):
        size = 40 if rng.random() < 0.6 else 20
        teu = 2 if size == 40 else 1
        critical = i == 0 or rng.random() < critical_share
        if count >= 3 and i == count - 1 and all(c["priority"] == "critical" for c in out):
            critical = False       # at least one box with slack, or a split has nothing to leave behind
        deadline = committed if critical else committed + timedelta(
            days=rng.randint(4, 10), hours=rng.randint(0, 20))
        serial = int(hashlib.sha256(f"{shipment.shipment_id}:{i}".encode()).hexdigest()[:8], 16) % 1_000_000
        out.append({
            "container_id": container_id(serial),
            "size_ft": size,
            "teu": teu,
            "content": PRODUCT_LABEL.get(shipment.product_family, shipment.product_family),
            "gross_t": round(rng.uniform(9.0, 24.0 if size == 40 else 18.0), 1),
            "priority": "critical" if critical else "standard",
            "deadline": deadline.isoformat(),
            "dangerous_goods": shipment.dangerous_goods,
            "temperature_controlled": shipment.temperature_controlled,
            "synthetic": True,
        })
    return out


def load(shipment: Shipment, leg_index: int, boxes: list[dict],
         payload_fraction: float | None = None) -> dict:
    """Capacity utilisation of the vehicle on this leg.

    Our TEU is a floor on what is loaded; the rest is other shippers' freight
    at a synthetic utilisation. A payload derate (Rhine low water) shrinks the
    usable capacity, and the card says so, because "88 of 94 usable TEU" and
    "88 of 208 TEU" are different problems.
    """
    veh = vehicle(shipment, leg_index)
    ours = sum(b["teu"] for b in boxes)
    capacity = veh["capacity_teu"]
    rng = _rng(shipment.shipment_id, "load", leg_index)
    if veh["mode"] == "road":
        loaded = min(capacity, ours)
    else:
        loaded = max(ours, int(capacity * rng.uniform(0.55, 0.93)))
    usable = None
    if payload_fraction is not None and veh["mode"] == "barge":
        usable = max(ours, int(capacity * payload_fraction))
        loaded = min(loaded, usable)
    loaded = max(loaded, ours)
    return {
        "capacity_teu": capacity,
        "loaded_teu": loaded,
        "usable_teu": usable,
        "ours_teu": ours,
        "utilisation": round(loaded / capacity, 3) if capacity else None,
        "unit": "TEU",
        "synthetic": True,
    }


def teu_of(boxes: list[dict]) -> int:
    return sum(int(b["teu"]) for b in boxes)


MODE_WORD = {Mode.SEA.value: "vessel", Mode.BARGE.value: "barge",
             Mode.RAIL.value: "train", Mode.ROAD.value: "truck"}
