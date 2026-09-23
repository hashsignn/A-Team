"""Tests for the planner's risk profile.

Two things are worth defending here.

The first is that the profile is not a SECOND store. It is the config the
engine actually ran on, rendered readable — so every tab has to be derived,
never transcribed, or it will drift the first time somebody edits a YAML file
directly.

The second is the save path. It writes into the engine's own configuration,
so it is the one place in the app where a UI action can change what every
number means. It is allow-listed, it refuses settings that would make a rung
of the ladder unreachable, and it never touches config.example/.
"""

from __future__ import annotations

import pytest
import yaml

from engine.clock import Clock
from engine.config import load_config
from engine.export import profile as P
from engine.pipeline import RunOptions, run

AS_OF = Clock.at("2026-09-19T12:00:00+00:00")


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def context(config):
    return run(clock=AS_OF, config=config, options=RunOptions(shipment_count=120))


@pytest.fixture(scope="module")
def profile(context):
    return P.build_profile(context)


# =====================================================================
# The profile IS the config
# =====================================================================


def test_every_tab_is_present(profile):
    for tab in ("desk", "network", "ledger", "appetite", "response", "sources"):
        assert profile[tab], f"{tab} tab is empty"


def test_ledger_carries_every_variable_the_engine_loaded(profile, config):
    assert profile["ledger"]["total"] == len(config.variables)
    listed = sum(len(f["variables"]) for f in profile["ledger"]["families"])
    assert listed == len(config.variables)


def test_lanes_and_nodes_match_the_loaded_network(profile, config):
    assert len(profile["network"]["lanes"]) == len(config.lanes)
    assert len(profile["network"]["nodes"]) == len(config.nodes)


def test_cutoffs_are_the_ones_classify_actually_reads(profile, config):
    """If these ever diverge, a planner edits a number that does nothing."""
    shown = profile["appetite"]["alert_levels"]
    live = config.scoring["alert_levels"]
    for key in ("red_hours", "yellow_hours", "blue_hours", "material_chf"):
        assert shown[key] == live[key]


def test_every_file_reports_where_it_was_loaded_from(profile, config):
    overlay = profile["overlay_active"]
    assert set(overlay) == set(config.files)
    for name, entry in overlay.items():
        assert entry["is_example"] == config.is_example(name)


def test_an_unsourceable_probability_is_stated_not_hidden(profile):
    """The board keeps these out of the axis rather than assuming a half, so
    the profile has to say which variables they are."""
    flags = [
        v["probability_sourceable"]
        for f in profile["ledger"]["families"]
        for v in f["variables"]
    ]
    assert any(flag is False for flag in flags)
    counted = sum(f["unsourceable"] for f in profile["ledger"]["families"])
    assert counted == sum(1 for flag in flags if not flag)


def test_assumed_numbers_are_labelled_as_ours(profile):
    """A planner being asked to agree a threshold must be able to see which
    numbers came from them and which we invented."""
    prov = profile["appetite"]["provenance"]
    assert prov["red_hours"] == "client"
    assert "assumed" in prov["material_chf"]
    assert "assumed" in prov["thresholds"]


def test_sources_say_what_connecting_an_absent_feed_would_unlock(profile):
    absent = [f for f in profile["sources"]["feeds"] if f["status"] != "connected"]
    assert absent, "the fixture should have at least one feed standing in"
    for feed in absent:
        assert feed["unlocks_if_connected"].strip()


def test_a_node_with_no_alternative_is_empty_not_invented(profile):
    for node in profile["network"]["nodes"]:
        assert isinstance(node["alternatives"], list)


# =====================================================================
# Saving — allow-list, validation, and the overlay
# =====================================================================


def test_an_edit_lands_in_the_customer_dir_not_the_example(config, tmp_path):
    out = P.apply_edits(config, {"alert_levels": {"red_hours": 8}}, tmp_path)
    assert out["applied"] == ["alert_levels.red_hours"]
    written = yaml.safe_load((tmp_path / "scoring.yaml").read_text(encoding="utf-8"))
    assert written["alert_levels"]["red_hours"] == 8
    # and the committed stand-in is untouched
    assert config.scoring["alert_levels"]["red_hours"] != 8


def test_the_whole_scoring_file_is_written_not_just_the_edit(config, tmp_path):
    """A partial overlay would load as a config missing everything it did not
    mention, which fails at scoring time rather than at load time."""
    P.apply_edits(config, {"alert_levels": {"red_hours": 5}}, tmp_path)
    written = yaml.safe_load((tmp_path / "scoring.yaml").read_text(encoding="utf-8"))
    assert set(written) == set(config.scoring)
    assert written["min_action_hours"] == config.scoring["min_action_hours"]


def test_a_key_outside_the_allow_list_is_reported_rejected(config, tmp_path):
    """Silently dropping it is worse than refusing: the planner would believe
    the setting had saved."""
    out = P.apply_edits(
        config,
        {"alert_levels": {"red_hours": 8, "simulation_seed": 1}},
        tmp_path,
    )
    assert out["rejected"] == ["alert_levels.simulation_seed"]
    written = yaml.safe_load((tmp_path / "scoring.yaml").read_text(encoding="utf-8"))
    assert "simulation_seed" not in written["alert_levels"]


def test_action_durations_cannot_be_edited_from_the_page(config, tmp_path):
    """They set every decision deadline. Changing one moves deadlines all over
    the board, so it belongs in a reviewed config change."""
    out = P.apply_edits(
        config, {"min_action_hours": {"sea_reroute": 1}}, tmp_path
    )
    assert out["applied"] == []
    assert not (tmp_path / "scoring.yaml").exists()


@pytest.mark.parametrize(
    "levels, needle",
    [
        ({"red_hours": 72, "yellow_hours": 48}, "sooner than Alert"),
        ({"yellow_hours": 200, "blue_hours": 168}, "sooner than Watch"),
        ({"red_hours": 0}, "positive number"),
        ({"material_chf": -5}, "zero or more"),
    ],
)
def test_an_ordering_that_makes_a_rung_unreachable_is_refused(
    config, tmp_path, levels, needle
):
    """classify() reads the cutoffs as a chain of <= tests. Put yellow below
    red and NOTHING can ever be classed Alert — and nothing on screen would
    say so. That silent failure is the one worth refusing."""
    out = P.apply_edits(config, {"alert_levels": levels}, tmp_path)
    assert out["applied"] == []
    assert any(needle in p for p in out["problems"]), out["problems"]
    assert not (tmp_path / "scoring.yaml").exists()


def test_a_refused_save_writes_nothing_at_all(config, tmp_path):
    """Not even the valid half of the edit — a half-applied ladder is worse
    than a refused one."""
    out = P.apply_edits(
        config,
        {
            "alert_levels": {"material_chf": 2500, "red_hours": 999},
            "convene_thresholds": {"exposure_chf": 50_000},
        },
        tmp_path,
    )
    assert out["problems"]
    assert out["written"] is None
    assert not (tmp_path / "scoring.yaml").exists()


def test_convene_thresholds_and_agreement_are_editable(config, tmp_path):
    out = P.apply_edits(
        config,
        {
            "convene_thresholds": {"exposure_chf": 90_000},
            "convene_meta": {"agreed_on": "2026-10-01", "agreed_by": "S&OP"},
        },
        tmp_path,
    )
    assert not out["problems"]
    written = yaml.safe_load((tmp_path / "scoring.yaml").read_text(encoding="utf-8"))
    assert written["convene_rule"]["thresholds"]["exposure_chf"] == 90_000
    assert written["convene_rule"]["agreed_on"] == "2026-10-01"


def test_an_agreed_rule_changes_what_the_headline_can_claim(config, tmp_path):
    """The mechanism depends on the group having owned the threshold in calm
    conditions, so the wording must follow the agreement, not decorate it."""
    from engine.portfolio import convene

    P.apply_edits(config, {"convene_meta": {"agreed_on": "2026-10-01"}}, tmp_path)
    edited = load_config(customer_dir=tmp_path)
    context = run(clock=AS_OF, config=edited, options=RunOptions(shipment_count=120))
    verdict = convene.evaluate(context.result.assessments, edited, AS_OF)
    assert verdict.rule_agreed
    assert "not yet agreed" not in verdict.headline


def test_a_saved_cutoff_actually_re_levels_the_board(config, tmp_path):
    """The point of the tab. If an edit does not move the levels it is a
    decorative setting, which is worse than no setting."""
    from engine.export.board import build_board

    before = build_board(
        run(clock=AS_OF, config=config, options=RunOptions(shipment_count=120))
    )
    # Collapse the ladder: everything beyond 1 h becomes Bias.
    P.apply_edits(
        config,
        {"alert_levels": {"red_hours": 0.5, "yellow_hours": 0.75, "blue_hours": 1}},
        tmp_path,
    )
    after = build_board(
        run(
            clock=AS_OF,
            config=load_config(customer_dir=tmp_path),
            options=RunOptions(shipment_count=120),
        )
    )
    levels_before = [r["level"] for r in before["routes"]]
    levels_after = [r["level"] for r in after["routes"]]
    assert levels_before != levels_after


def test_reset_removes_the_overlay_and_says_whether_there_was_one(config, tmp_path):
    P.apply_edits(config, {"alert_levels": {"red_hours": 9}}, tmp_path)
    assert P.clear_overlay(tmp_path)["removed"] is True
    assert not (tmp_path / "scoring.yaml").exists()
    # Idempotent, and honest about having done nothing the second time.
    assert P.clear_overlay(tmp_path)["removed"] is False


def test_reset_falls_back_to_the_committed_stand_in(config, tmp_path):
    original = config.scoring["alert_levels"]["red_hours"]
    P.apply_edits(config, {"alert_levels": {"red_hours": 9}}, tmp_path)
    P.clear_overlay(tmp_path)
    restored = load_config(customer_dir=tmp_path)
    assert restored.scoring["alert_levels"]["red_hours"] == original
    assert restored.is_example("scoring")


def test_a_cleared_agreement_date_becomes_null_not_an_empty_string(config, tmp_path):
    """convene.evaluate reads agreement as `agreed_on is not None`. An empty
    string is not None, so clearing the field in the form would make the board
    claim the team had signed off a threshold they never saw — the one failure
    that breaks the mechanism outright."""
    from engine.portfolio import convene

    P.apply_edits(config, {"convene_meta": {"agreed_on": "  "}}, tmp_path)
    written = yaml.safe_load((tmp_path / "scoring.yaml").read_text(encoding="utf-8"))
    assert written["convene_rule"]["agreed_on"] is None

    edited = load_config(customer_dir=tmp_path)
    context = run(clock=AS_OF, config=edited, options=RunOptions(shipment_count=120))
    verdict = convene.evaluate(context.result.assessments, edited, AS_OF)
    assert verdict.rule_agreed is False


def test_a_numeric_field_is_stored_as_a_number_a_date_as_text(config, tmp_path):
    """Every value arrives from a form field as a string; the YAML must still
    read like a person wrote it."""
    P.apply_edits(
        config,
        {"convene_meta": {"meeting_cadence_days": "14", "agreed_on": "2026-10-01"}},
        tmp_path,
    )
    written = yaml.safe_load((tmp_path / "scoring.yaml").read_text(encoding="utf-8"))
    assert written["convene_rule"]["meeting_cadence_days"] == 14
    assert str(written["convene_rule"]["agreed_on"]).startswith("2026-10-01")


def test_team_ids_are_resolved_to_readable_names(profile):
    """The YAML refers to teams by id so a rename cannot break the escalation
    table. Printing `FN_SUPPLY_CHAIN` at a planner is a leak of that
    mechanism, not information."""
    response = profile["response"]
    for names in response["convene_by_level"].values():
        for name in names:
            assert not name.startswith("FN_"), name
    for step in response["escalation"]:
        for name in step["notify"]:
            assert not name.startswith("FN_"), name
    assert not response["approval"]["approver"].startswith("FN_")
    assert all(team["name"] for team in response["standing_teams"])


def test_an_unresolvable_team_id_says_so_rather_than_printing_raw(config):
    """A dangling reference is a config error. Showing it as a plain id would
    look like a team called FN_GHOST."""
    import copy

    stripped = copy.deepcopy(config)
    stripped.files["contacts"].data = dict(config.contacts)
    stripped.files["contacts"].data["convene_by_level"] = {"red": ["FN_GHOST"]}
    out = P._response(stripped)
    assert out["convene_by_level"]["red"] == ["FN_GHOST (not in the contact list)"]


def test_more_urgent_levels_draw_in_more_teams(profile):
    by_level = profile["response"]["convene_by_level"]
    assert by_level["green"] == []
    assert len(by_level["white"]) < len(by_level["blue"]) < len(by_level["red"])


def test_an_unset_approver_is_blank_not_a_dangling_reference(config):
    import copy

    stripped = copy.deepcopy(config)
    stripped.files["contacts"].data = dict(config.contacts)
    stripped.files["contacts"].data["approval"] = {"delegated_limit_chf": 10_000}
    assert P._response(stripped)["approval"]["approver"] == ""
