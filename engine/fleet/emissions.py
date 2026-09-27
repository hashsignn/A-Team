"""CO2e for the journey after the factory gate — GLEC / ISO 14083.

Sika's Carbon Compass measures the product footprint up to the factory gate.
It stops there. This measures the delivery leg, for the planned route and for
every recovery alternative, with the method most freight CO2 tools use:

    CO2e = tonnes x km x factor          (factor in g CO2e per tonne-km,
                                          well-to-wheel)

A SEPARATE KPI, never part of the risk score or the weighted ranking. The
risk score answers "how bad is this disruption"; CO2e answers "what does each
fix cost the climate". Folded together, a truck that saves two days and
doubles the footprint would just look like a slightly better option.

The factors are GLEC DEFAULT VALUES (INDICATIVE), from a secondary source —
fleet.yaml says so, and so does every payload that carries a number from them.
"""

from __future__ import annotations


def factors(cfg: dict) -> dict[str, float]:
    return {k: float(v) for k, v in cfg["emissions"]["g_co2e_per_tkm"].items()}


def co2e_kg(cfg: dict, legs: list[tuple[str, float]], tonnes: float) -> float:
    """kg CO2e for (mode, km) legs carrying *tonnes*."""
    f = factors(cfg)
    return sum(tonnes * km * f.get(mode, f["road"]) for mode, km in legs) / 1000.0


def provenance(cfg: dict) -> dict:
    e = cfg["emissions"]
    return {
        "method": e["method"],
        "source": e["source"],
        "g_co2e_per_tkm": factors(cfg),
        "scope": "delivery leg after the factory gate — where Sika's Carbon Compass stops",
        "indicative": True,
    }
