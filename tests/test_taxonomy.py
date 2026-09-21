"""The four-layer taxonomy, the pathway gate, and the three validation
scenarios from the architecture spec.

Scenario B is the one that matters. It is the clash the spec names — a severe
event against invulnerable cargo — and it is the case where a multiplicative
score has to choose between two wrong answers. Here it produces a third,
correct one, and the test asserts BOTH halves of it: no cargo risk, and a
real delay.
"""

from __future__ import annotations

import pytest

from engine.config import load_config
from engine.score import alert as A
from engine.score.severity import LEVEL_RANK, Level
from engine.taxonomy import channel as CH
from engine.taxonomy import pathways as P
from engine.taxonomy.resolve import Encounter, resolve


@pytest.fixture(scope="module")
def config():
    return load_config()


# =====================================================================
# The pathway split
# =====================================================================


def test_delay_applies_regardless_of_what_is_in_the_box(config):
    """Delay does not care about cargo. That is the whole point of splitting
    it away from damage."""
    for cargo in ("none", "freeze_critical", "adr_class_3", "high_theft_risk"):
        verdict = P.evaluate(config, "LAB_PORT_STRIKE", cargo, "deep_sea", {})
        assert bool(verdict.delay), cargo


def test_a_strike_cannot_damage_goods(config):
    """A labour event has no damage pathway at all. Tagging it otherwise
    would put every sensitive shipment on the board for every strike."""
    verdict = P.evaluate(config, "LAB_PORT_STRIKE", "freeze_critical", "deep_sea", {})
    assert not bool(verdict.damage)
    assert "only delays" in verdict.damage.reason


def test_fog_is_delay_only(config):
    """Explicitly overridden. Fog as a damage risk would flag every
    freeze-critical shipment on every autumn morning, which is how a radar
    gets switched off."""
    pathway, _ = P.declared_pathway(config, "CLI_FOG")
    assert pathway is P.Pathway.DELAY


def test_ice_carries_both_pathways(config):
    """Black ice closes roads AND the temperature that made it breaks an
    emulsion. Two independent harms from one observation."""
    pathway, _ = P.declared_pathway(config, "CLI_ICE")
    assert pathway is P.Pathway.BOTH


def test_an_unmapped_variable_falls_back_to_delay_not_damage(config):
    """Fail quiet. Assuming damage for something we cannot classify would
    manufacture cargo alerts out of a config gap."""
    pathway, note = P.declared_pathway(config, "XX_NOT_A_VARIABLE")
    assert pathway is P.Pathway.DELAY
    assert "delay assumed" in note["why"]


# =====================================================================
# Layer 2 — the asset gate
# =====================================================================


def test_a_terminal_event_cannot_reach_freight_that_never_enters_one(config):
    """POR_CUSTOMS_BACKLOG affects road, so mode does not separate these two —
    the terminal flag is the only thing that does. That is the gate under
    test, and picking a sea-only variable would have tested the mode check by
    accident."""
    ftl = P.asset_reachable(config, "road_ftl", "POR_CUSTOMS_BACKLOG")
    ltl = P.asset_reachable(config, "road_ltl", "POR_CUSTOMS_BACKLOG")
    assert not ftl.open
    assert "never enters a terminal" in ftl.reason
    assert ltl.open, "LTL does cross-dock and must stay exposed"


def test_the_exclusion_reason_is_the_most_fundamental_one(config):
    """An FTL escapes sea-port congestion because ROAD is not affected, not
    because it skips terminals. Told the latter, a planner would reasonably
    conclude their LTL on the same road lane is exposed. It is not."""
    ftl = P.asset_reachable(config, "road_ftl", "POR_CONGESTION")
    assert not ftl.open
    assert "this moves by road" in ftl.reason


def test_a_waterway_event_cannot_reach_a_truck(config):
    assert not P.asset_reachable(config, "road_ftl", "WAT_LOW_WATER").open


def test_an_unknown_asset_is_not_filtered_out(config):
    """A config gap must not silently delete exposure."""
    assert P.asset_reachable(config, "hovercraft", "LAB_PORT_STRIKE").open


# =====================================================================
# Layer 4 — the damage gate
# =====================================================================


def test_an_unobserved_temperature_does_not_freeze_anything(config):
    """An absence never becomes a value. If nobody told us the temperature,
    we do not get to claim the polymer froze."""
    verdict = P.evaluate(config, "CLI_ICE", "freeze_critical", "dry_van", {})
    assert not bool(verdict.damage)
    assert "no temperature observed" in verdict.damage.reason


def test_the_precautionary_and_physical_thresholds_are_different(config):
    """5 °C is the margin and 0 °C is the physics. Between them the product
    is at risk but recoverable; below 0 °C it is scrap."""
    warned = P.evaluate(config, "CLI_ICE", "freeze_critical", "dry_van", {"ambient_c": 3.0})
    scrap = P.evaluate(config, "CLI_ICE", "freeze_critical", "dry_van", {"ambient_c": -7.0})
    assert bool(warned.damage) and not warned.irreversible
    assert bool(scrap.damage) and scrap.irreversible
    assert scrap.scrap_fraction == 1.0
    assert warned.scrap_fraction == 0.0


def test_a_reefer_protects_until_its_fuel_runs_out(config):
    """Protection is a countdown, not immunity — the only place a delay
    converts into a damage risk by way of equipment."""
    inside = P.evaluate(
        config, "CLI_ICE", "freeze_critical", "reefer",
        {"ambient_c": -7.0, "delay_hours": 10.0},
    )
    beyond = P.evaluate(
        config, "CLI_ICE", "freeze_critical", "reefer",
        {"ambient_c": -7.0, "delay_hours": 48.0},
    )
    assert not bool(inside.damage)
    assert "countdown, not immunity" in inside.damage.reason
    assert bool(beyond.damage), "past the autonomy the damage pathway re-opens"
    assert "ran out" in " ".join(beyond.notes)


def test_moisture_sensitive_cargo_is_safe_in_a_box_trailer(config):
    wet_open = P.evaluate(
        config, "CLI_STORM", "moisture_sensitive", "flatbed",
        {"open_handling": True, "precipitation": True},
    )
    wet_boxed = P.evaluate(
        config, "CLI_STORM", "moisture_sensitive", "dry_van",
        {"open_handling": True, "precipitation": True},
    )
    assert bool(wet_open.damage)
    assert not bool(wet_boxed.damage)


def test_a_gale_damages_open_deck_equipment_only(config):
    """The override requires an asset flag: a curtain is a sail, a box
    trailer is not."""
    assert P.declared_pathway(config, "CLI_HIGH_WIND")[0] is P.Pathway.BOTH
    boxed = P.evaluate(config, "CLI_HIGH_WIND", "controlled_ambient", "dry_van",
                       {"ambient_c": 10.0})
    assert not bool(boxed.damage)


# =====================================================================
# Layer 3 — consequence, not criticality
# =====================================================================


def test_lateness_inside_the_tolerance_costs_nothing(config):
    cost = CH.cost_of_lateness(config, "b2b_distributor", 6.0)
    assert cost.within_tolerance
    assert cost.cost_chf == 0.0


def test_the_same_lateness_costs_wildly_different_things_by_channel(config):
    late = 6.0
    distributor = CH.cost_of_lateness(config, "b2b_distributor", late)
    jobsite = CH.cost_of_lateness(config, "direct_to_jobsite", late)
    jit = CH.cost_of_lateness(config, "jit_jis", late)
    assert distributor.cost_chf == 0.0 < jobsite.cost_chf < jit.cost_chf


def test_a_step_penalty_stops_growing(config):
    """Being six hours late and six days late cost the same at retail, which
    changes what mitigation is worth buying."""
    short = CH.cost_of_lateness(config, "retail_diy", 6.0)
    long = CH.cost_of_lateness(config, "retail_diy", 144.0)
    assert short.cost_chf == long.cost_chf
    assert short.shape == "step"


def test_a_cliff_penalty_keeps_growing(config):
    short = CH.cost_of_lateness(config, "direct_to_jobsite", 6.0)
    long = CH.cost_of_lateness(config, "direct_to_jobsite", 144.0)
    assert long.cost_chf > short.cost_chf


def test_an_unmapped_channel_invents_no_penalty(config):
    cost = CH.cost_of_lateness(config, "mystery_channel", 48.0)
    assert cost.cost_chf == 0.0
    assert "not mapped" in cost.explanation


# =====================================================================
# The Alert Score cannot disagree with the ladder
# =====================================================================


@pytest.mark.parametrize("level", list(Level))
def test_every_rung_occupies_its_own_decade(level):
    """The property that makes two scales safe. A Bias shipment carrying the
    book's largest exposure can never outscore a Watch one."""
    lowest = A.score_for(level, 0.0, 1_000_000).score
    highest = A.score_for(level, 999_999.0, 1_000_000).score
    rank = LEVEL_RANK[level]
    assert 20 * rank <= lowest <= highest <= 20 * rank + 19


def test_the_bands_land_on_the_clients_own_rungs():
    assert A.band_for(A.score_for(Level.WHITE, 9e5, 1e6).score) == "green"
    assert A.band_for(A.score_for(Level.BLUE, 0.0, 1e6).score) == "amber"
    assert A.band_for(A.score_for(Level.RED, 0.0, 1e6).score) == "red"


def test_irreversible_damage_escalates_the_rung(config):
    scrap = P.evaluate(config, "CLI_ICE", "freeze_critical", "dry_van", {"ambient_c": -7.0})
    raised, why = A.escalate_for_damage(Level.BLUE, [scrap])
    assert raised is Level.YELLOW
    assert "no later moment" in why


def test_reversible_degradation_does_not_escalate(config):
    warned = P.evaluate(config, "CLI_ICE", "controlled_ambient", "dry_van",
                        {"ambient_c": -18.0})
    raised, why = A.escalate_for_damage(Level.BLUE, [warned])
    assert raised is Level.BLUE and why is None


# =====================================================================
# SCENARIO A — freeze wave (-7 °C) / water-based polymer / jobsite
# =====================================================================


def test_scenario_a_freeze_polymer_jobsite(config):
    result = resolve(config, Encounter(
        variable_id="CLI_ICE",
        cargo_class="freeze_critical",
        asset_id="dry_van",
        channel_id="direct_to_jobsite",
        late_hours=9.0,
        observation={"ambient_c": -7.0},
        consequence_cap=20_000.0,
        base_level=Level.BLUE,
    ))
    # Both pathways open.
    assert bool(result.pathway.delay)
    assert bool(result.pathway.damage)
    assert result.pathway.irreversible
    assert result.pathway.scrap_fraction == 1.0
    # Escalated off the back of the irreversibility.
    assert result.alert.escalated
    assert result.alert.level is Level.YELLOW
    assert result.alert.band in ("amber", "red")
    # Jobsite has almost no tolerance, so the delay is chargeable too.
    assert result.cost.cost_chf > 0
    print("\nSCENARIO A:", result.alert.score, result.alert.band,
          "|", result.pathway.headline)


# =====================================================================
# SCENARIO B — dock strike / ambient dry mortar / distributor
# THE CLASH THE SPEC NAMES
# =====================================================================


def test_scenario_b_strike_dry_mortar_distributor(config):
    """High event severity, zero cargo vulnerability.

    A multiplicative score has to pick between a false Red and silently
    dropping a four-day delay. This produces the third answer: no cargo risk,
    and a real delay priced at the distributor's own tolerance.
    """
    result = resolve(config, Encounter(
        variable_id="LAB_PORT_STRIKE",
        cargo_class="none",
        asset_id="deep_sea",
        channel_id="b2b_distributor",
        late_hours=96.0,
        observation={},
        consequence_cap=20_000.0,
        base_level=Level.BLUE,
    ))
    # The cargo is NOT at risk, and the board says so in as many words.
    assert not bool(result.pathway.damage)
    assert "Delay only" in result.pathway.headline
    assert not result.alert.escalated

    # ...but the delay is real and priced.
    assert bool(result.pathway.delay)
    assert result.cost.chargeable_hours == pytest.approx(96.0 - 8.0)
    assert result.cost.cost_chf > 0

    # And the clash is recorded rather than hidden.
    rules = {c["rule"] for c in result.clashes}
    assert "severe_event_invulnerable_cargo" in rules

    # Not a false Red.
    assert result.alert.band != "red"
    print("\nSCENARIO B:", result.alert.score, result.alert.band,
          "|", result.pathway.headline,
          "| CHF", result.cost.cost_chf)


def test_scenario_b_stays_calm_even_at_a_critical_channel(config):
    """The second half of the clash: a critical channel cannot manufacture a
    cargo risk, but its consequence still stands in full."""
    result = resolve(config, Encounter(
        variable_id="LAB_PORT_STRIKE",
        cargo_class="none",
        asset_id="deep_sea",
        channel_id="jit_jis",
        late_hours=96.0,
        consequence_cap=100_000.0,
        base_level=Level.BLUE,
    ))
    assert not bool(result.pathway.damage)
    rules = {c["rule"] for c in result.clashes}
    assert "critical_channel_zero_vulnerability" in rules
    assert result.cost.cost_chf > 10_000, "the line-stoppage consequence stands"


# =====================================================================
# SCENARIO C — weekend driving ban / ADR solvent / JIT plant
# =====================================================================


def test_scenario_c_driving_ban_adr_solvent_jit(config):
    result = resolve(config, Encounter(
        variable_id="GEO_REGULATORY",
        cargo_class="adr_class_3",
        asset_id="adr_tanker",
        channel_id="jit_jis",
        late_hours=52.0,
        observation={},
        consequence_cap=60_000.0,
        base_level=Level.YELLOW,
    ))
    # Delay only — a driving ban harms nothing in the tank.
    assert bool(result.pathway.delay)
    assert not bool(result.pathway.damage)
    # JIT tolerance is 15 minutes, so essentially all of it is chargeable.
    assert result.cost.chargeable_hours == pytest.approx(52.0 - 0.25)
    assert result.cost.cost_chf > 15_000
    assert result.alert.band == "red"
    # The standard expedite is closed to Class 3, and is removed rather than
    # offered.
    assert any(f["asset"] == "air_cargo" for f in result.forbidden)
    assert "adr_blocks_the_obvious_mitigation" in {c["rule"] for c in result.clashes}
    print("\nSCENARIO C:", result.alert.score, result.alert.band,
          "| suppressed:", [f["asset"] for f in result.forbidden])


def test_an_adr_tanker_has_no_substitute_declared(config):
    """Deliberately empty. The fleet is small and the driver needs a
    certificate, so pretending a swap exists would be the same failure as
    recommending air freight."""
    assets = config.raw("taxonomy")["layer2_assets"]
    assert assets["adr_tanker"]["substitutable_by"] == []


# =====================================================================
# Overlapping events
# =====================================================================


def test_overlapping_events_take_the_worst_delay_not_the_sum(config):
    """A strike and a blizzard on one corridor do not queue end to end — the
    trucks are stopped once. This is already how the Monte Carlo composes
    concurrent events; the rule is recorded so the behaviour is stated."""
    rules = {r["id"] for r in config.raw("taxonomy")["clash_rules"]}
    assert "overlapping_events_same_corridor" in rules
    rule = next(
        r for r in config.raw("taxonomy")["clash_rules"]
        if r["id"] == "overlapping_events_same_corridor"
    )
    assert rule["resolve"] == "max_per_pathway_not_sum"


def test_independent_damage_harms_are_both_reported(config):
    """Damage does NOT combine like delay. A freeze and a theft risk are
    separate harms and collapsing them to a max would hide one."""
    freeze = P.evaluate(config, "CLI_ICE", "freeze_critical", "dry_van",
                        {"ambient_c": -7.0})
    theft = P.evaluate(config, "INF_ROAD_CLOSURE", "high_theft_risk", "dry_van",
                       {"unsecured_stop_hours": 14.0})
    assert bool(freeze.damage) and bool(theft.damage)
    assert freeze.damage.reason != theft.damage.reason
