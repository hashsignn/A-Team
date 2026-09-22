"""Stage orchestration — the only place run order lives (BRIEF §9.2).

    1. ingest     shipments + observations + feeds
    2. resolve    many reports -> one event
    3. gate       does it touch our freight at all?   <- ~95% die here
    4. variables  which variables are active
    5. reason     extraction + relevance, with evidence
    6. simulate   delay distribution -> P(late)
    7. score      CHF, lead time, matrix cell
    8. act        options, contacts, decision deadline
    9. export     store, CSV, per-event report

Stages 1-8 need no LLM. The Rhine demo runs end to end on numeric feeds, so
the system is demonstrable before a model is wired — and if the model is
unavailable on the day, the board still fills.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np

from engine.act.playbook import best_option, options_for
from engine.clock import Clock
from engine.config import Config, load_config
from engine.gate.intersect import gate
from engine.ingest.feeds import load_feed_items, social_promotion_status
from engine.ingest.observations import FeedReport, FeedStatus, IngestBundle
from engine.ingest.synthetic import generate_shipments, in_scope
from engine.ingest.watergauge import assess_kaub
from engine.network.geo import Point, in_bounding_box
from engine.network.graph import Network
from engine.portfolio import convene
from engine.reason import funnel as reason_funnel
from engine.reason import llm as llm_mod
from engine.schemas import (
    Event,
    EventAssessment,
    FunnelCounts,
    GateHit,
    Mode,
    PipelineResult,
    Provenance,
    Severity,
    Shipment,
    ShipmentOutcome,
    ShipmentRisk,
)
from engine.score import impact as impact_mod
from engine.score import leadtime, matrix
from engine.simulate.draws import build_draw_matrix, propagate, variable_contributions
from engine.variables import rules as rules_router


@dataclass
class RunOptions:
    shipment_count: int = 150
    seed: int | None = None
    max_reasoned_events: int = 40
    include_delivered: bool = False

    # The rescue path (see _to_events). On by default, but it is inert without
    # a model — every call inside it checks for a backend first, so the default
    # costs nothing on a machine with no Ollama and changes no existing output.
    rescue_unmatched: bool = True

    # Bounded hard. Rescue runs the STRONG model once per survivor, which is
    # the most expensive thing in this pipeline. Six is a demo; a deployment
    # with a real GDELT sweep would raise it and watch the funnel counts.
    max_rescued_events: int = 6

    # External sources. Inert without RADAR_ALLOW_NETWORK — each one falls back
    # to its fixture — so leaving this on costs nothing offline.
    use_external_sources: bool = True


@dataclass
class RunContext:
    """Everything a run produced, including the parts the UI needs but the
    result schema does not carry."""

    config: Config
    clock: Clock
    network: Network
    shipments: list[Shipment]
    events: list[Event]
    hits: list[GateHit]
    result: PipelineResult
    reports: list[FeedReport] = field(default_factory=list)
    router_notes: dict[str, str] = field(default_factory=dict)
    unpromoted: dict[str, str] = field(default_factory=dict)


def run(
    clock: Clock | None = None,
    config: Config | None = None,
    options: RunOptions | None = None,
) -> RunContext:
    clock = clock or Clock.wall()
    config = config or load_config()
    options = options or RunOptions()
    network = Network(config)

    # ---- 1. INGEST ----------------------------------------------------
    bundle = IngestBundle()

    shipments = generate_shipments(
        config, clock, count=options.shipment_count, seed=options.seed
    )
    if not options.include_delivered:
        shipments = [s for s in shipments if in_scope(s, clock)]

    bundle.add(
        FeedReport(
            key="shipment_book",
            label="Internal shipment book",
            status=FeedStatus.FIXTURE,
            detail=f"{len(shipments)} SYNTHETIC shipments — generated, not Sika data",
            unlocks_if_connected=(
                "The planner supplies a CSV/JSON export. Integration with ERP or "
                "TMS is explicitly out of scope; this is a file, not a connector."
            ),
            records=len(shipments),
            retrieved_at=clock.as_of,
        )
    )

    gauge_obs, gauge_report = assess_kaub(config, clock)
    bundle.add(gauge_report, gauge_obs)

    feed_items, feed_report = load_feed_items(clock)
    bundle.add(feed_report)

    # Real external sources, on top of the synthetic corpus. Every one is free
    # and keyless; none of them reaches the network unless RADAR_ALLOW_NETWORK
    # is set, and each falls back to a recorded fixture and says so. The items
    # they produce are the same shape as the corpus, so the deterministic
    # funnel below treats a live GDELT article exactly as it treats a written
    # one — there is no "live mode" with different rules.
    source_items, source_reports = _collect_sources(config, clock, options)
    for report in source_reports:
        bundle.add(report)
    feed_items.extend(source_items)

    bundle.raw_count = len(gauge_obs) + len(feed_items)

    _add_absent_sockets(bundle, clock)

    # ---- 2-5. RESOLVE, GATE PREP, VARIABLES, REASON --------------------
    events, funnel_partial, router_notes, unpromoted = _to_events(
        gauge_obs, feed_items, config, network, clock, options
    )

    # ---- 3. GATE -------------------------------------------------------
    hits, gate_stats = gate(events, shipments, network, config, clock)

    # Keep only events that actually touch something. Events touching nothing
    # get no dot at all — that IS the noise filter, and it sits upstream of
    # severity (BRIEF §5.7).
    touched_event_ids = {h.event_id for h in hits}
    live_events = [e for e in events if e.event_id in touched_event_ids]

    # ---- 6. SIMULATE ---------------------------------------------------
    # One draw matrix for the whole portfolio: event durations drawn once per
    # iteration and shared across every shipment they touch.
    draws = build_draw_matrix(live_events, config)

    # ---- 7-8. SCORE and ACT --------------------------------------------
    assessments = _assess(live_events, shipments, hits, draws, config, network, clock)
    assessments.sort(key=lambda a: a.total_value_of_acting_chf, reverse=True)

    # ---- portfolio ------------------------------------------------------
    verdict = convene.evaluate(assessments, config, clock)

    funnel = FunnelCounts(
        raw_observations=bundle.raw_count,
        after_geographic=funnel_partial["after_geographic"],
        after_type=funnel_partial["after_type"],
        after_temporal=funnel_partial["after_temporal"],
        after_resolution=len(events),
        reasoned=funnel_partial["reasoned"],
        gated_hits=gate_stats.hits,
        shipments_touched=len({h.shipment_id for h in hits}),
    )

    result = PipelineResult(
        as_of=clock.as_of,
        shipments_total=len(shipments),
        events_total=len(live_events),
        assessments=assessments,
        convene=verdict,
        funnel=funnel,
        config_version=config.version,
    )

    return RunContext(
        config=config,
        clock=clock,
        network=network,
        shipments=shipments,
        events=live_events,
        hits=hits,
        result=result,
        reports=bundle.reports,
        router_notes=router_notes,
        unpromoted=unpromoted,
    )


# =====================================================================
# Stage 2-5
# =====================================================================


def _collect_sources(
    config: Config, clock: Clock, options: RunOptions
) -> tuple[list[dict], list[FeedReport]]:
    """Every configured external source, fetched or faithfully reported absent.

    Never raises. A source whose spec is malformed is a configuration error and
    would normally be loud, but taking the whole board down because somebody
    mistyped a URL in a feed nobody is watching is the wrong trade at RUN time
    — so the error becomes one ABSENT row carrying the message, which is both
    visible and survivable. ``sources.load`` still raises for the CLI, where
    the person who made the typo is standing right there.
    """
    if not options.use_external_sources:
        return [], []

    from engine.ingest import sources as source_pkg  # noqa: PLC0415

    loaded = config.files.get("sources")
    try:
        specs = source_pkg.load_sources(loaded.path if loaded else None)
    except source_pkg.SourceConfigError as exc:
        return [], [
            FeedReport(
                key="sources_config",
                label="External sources (sources.yaml)",
                status=FeedStatus.ABSENT,
                detail=f"configuration rejected: {exc}",
                unlocks_if_connected="Fix sources.yaml and every feed below returns.",
            )
        ]

    runnable = [s for s in specs if s.runnable]
    items, reports = source_pkg.collect_all(runnable, clock.as_of)

    # Text sources carry no coordinates — a wire story says "the Strait of
    # Hormuz", never a UN/LOCODE. Without this every one of them would be
    # dropped by the geographic filter for having no position, and the whole
    # news path would be rejected as noise. String search, not a model: free,
    # instant, reproducible, and auditable.
    source_pkg.places.enrich(items, config)

    # Sources that are off still get a row. A feed that is absent and says why
    # is information; a feed that is absent and silent is a hole in the board.
    for spec in specs:
        if spec.runnable:
            continue
        reports.append(
            FeedReport(
                key=spec.key,
                label=spec.label,
                status=FeedStatus.ABSENT,
                detail=spec.why_not_runnable(),
                unlocks_if_connected=spec.unlocks_if_connected,
                source_tier=spec.source_tier,
            )
        )
    return items, reports


def _to_events(
    gauge_obs: list[dict],
    feed_items: list[dict],
    config: Config,
    network: Network,
    clock: Clock,
    options: RunOptions,
) -> tuple[list[Event], dict, dict[str, str], dict[str, str]]:
    """Observations and reports become events, counting the funnel as we go."""
    events: list[Event] = []
    router_notes: dict[str, str] = {}
    unpromoted: dict[str, str] = {}
    bounds = network.bounds()

    counts = {
        "after_geographic": 0,
        "after_type": 0,
        "after_temporal": 0,
        "reasoned": 0,
        "rescued": 0,
        "rescue_considered": 0,
    }
    # Items the deterministic router could not name. See the rescue note below.
    rescue_candidates: list[dict] = []

    # --- numeric observations: already structured, already ours ---------
    for obs in gauge_obs:
        counts["after_geographic"] += 1
        counts["after_type"] += 1
        counts["after_temporal"] += 1
        events.append(_event_from_observation(obs, config, clock))

    # Route everything before the promotion check. Corroboration compares what
    # two reports actually CLAIM, not merely where they happened, so the routed
    # variables have to exist on every item before any one of them is judged.
    routed_by_item: dict[str, rules_router.RouterResult] = {}
    for item in feed_items:
        routed_by_item[item["item_id"]] = rules_router.route(
            item["text"], config.variables
        )
        item["active_variables"] = routed_by_item[item["item_id"]].active_variables

    # --- unstructured reports -------------------------------------------
    for item in feed_items:
        # Layer 1: geographic. Zero parameters, and it does the most work.
        if item["lat"] is not None and item["lon"] is not None:
            if not in_bounding_box(Point(item["lat"], item["lon"]), bounds):
                continue
        elif not item["node_hint"]:
            continue
        counts["after_geographic"] += 1

        # Layer 2: type. Does this even look like a disruption?
        #
        # An abstention here is NOT automatically a drop. The router is a list
        # of patterns, so it can only recognise the phrasings somebody already
        # wrote down — and a shock event is by definition the one nobody wrote
        # down. "Maritime interdiction regime declared across the Gulf" means a
        # blockade and shares no vocabulary with the word "blockade".
        #
        # So an item that passed the GEOGRAPHIC filter but that the router
        # cannot name is held as a RESCUE candidate: it goes to the funnel,
        # where a small model asks only "could this affect freight". That is
        # the one question a keyword list structurally cannot answer, and it is
        # the whole reason a model is in this system at all.
        #
        # With no model running, rescue does nothing and these drop exactly as
        # they did before. No model is a supported state, not a degraded one.
        routed = routed_by_item[item["item_id"]]
        if routed.abstained:
            router_notes[item["item_id"]] = routed.abstain_reason or "no match"
            rescue_candidates.append(item)
            continue
        counts["after_type"] += 1

        # Layer 3: temporal. Could it touch anything in flight or planned?
        if item["ends_at"] is not None and item["ends_at"] < clock.as_of:
            router_notes[item["item_id"]] = "event window closed before as-of"
            continue
        counts["after_temporal"] += 1

        # Tier-3 promotion (Sika Q5).
        promoted, reason = social_promotion_status(item, feed_items, config.thresholds)
        if not promoted:
            unpromoted[item["item_id"]] = reason
            continue

        counts["reasoned"] += 1
        if counts["reasoned"] > options.max_reasoned_events:
            break

        events.append(_event_from_item(item, routed, config, network, clock))

    # --- RESCUE: the shock path -----------------------------------------
    # Only runs when a model is available, and only over items that already
    # cleared the geographic filter. The budget is the funnel's, not ours.
    if rescue_candidates and options.rescue_unmatched:
        counts["rescue_considered"] = len(rescue_candidates)
        kept, funnel_cost = reason_funnel.triage(rescue_candidates)
        counts["funnel"] = funnel_cost.as_dict()
        counts["funnel_sentence"] = funnel_cost.sentence()
        for item in kept[: options.max_rescued_events]:
            event = _rescue_event(item, config, network, clock)
            if event is None:
                continue
            counts["rescued"] += 1
            events.append(event)
            router_notes[item["item_id"]] = (
                "the keyword router could not name this; a model read it and "
                "the deterministic challenger passed the reading"
            )

    return _cluster(events), counts, router_notes, unpromoted


def _event_from_observation(obs: dict, config: Config, clock: Clock) -> Event:
    var = config.variables[obs["variable_id"]]
    node_id = obs["node_ids"][0]
    node = config.nodes[node_id]
    return Event(
        event_id=obs["observation_id"],
        title=obs["title"],
        node_ids=obs["node_ids"],
        lat=node.lat,
        lon=node.lon,
        starts_at=obs["starts_at"],
        ends_at=obs["ends_at"],
        duration_confidence=obs["duration_confidence"],
        event_class=var.family,
        active_variables=[obs["variable_id"]],
        severity=Severity(obs["severity"]),
        modes_affected=var.modes_affected,
        realized=obs["realized"],
        probability=obs["probability"],
        probability_basis=obs["probability_basis"],
        provenance=Provenance(
            source=obs["source"],
            source_tier=obs["source_tier"],
            verbatim_quote=obs["verbatim_quote"],
            inferred=False,
            retrieved_at=clock.as_of,
        ),
        payload_fraction=obs.get("payload_fraction"),
        cost_multiplier=obs.get("cost_multiplier", 1.0),
    )


def _restrict_modes(modes: list[Mode], item: dict) -> list[Mode]:
    """Intersect a variable's modes with what the SOURCE could possibly know.

    A variable is written generically on purpose — a fire can stop a road, a
    rail line, a terminal or a ship, and FOR_FIRE says so. But a feed of German
    motorway closures cannot be telling you about a barge, whatever words the
    headline happens to contain, and "A5 closed after HGV fire" landing on a
    Rhine barge leg is a wrong event with a plausible explanation attached,
    which is the worst kind.

    The source narrows the variable; it never widens it. An empty restriction —
    a news index, which really can be about anything — changes nothing. An
    intersection that comes out empty is ignored rather than obeyed: that means
    the two disagree, and silently producing an event with no modes would
    remove it from the gate entirely, which is a drop disguised as a filter.
    """
    declared = item.get("source_modes") or []
    if not declared:
        return modes
    allowed = {m.lower() for m in declared}
    narrowed = [m for m in modes if m.value in allowed]
    return narrowed or modes


def _rescue_event(
    item: dict,
    config: Config,
    network: Network,
    clock: Clock,
) -> Event | None:
    """Stage 2 + the challenger, for one item the keyword router could not name.

    Returns None — silently to the caller, but always counted — when the model
    is unavailable, when the reading does not validate, or when the
    deterministic challenger rejects it. That last case is the important one:
    a model that invents a quote, cites a variable that is not in the ledger or
    resolves a node that does not exist produces NOTHING here, rather than
    producing a plausible-looking event nobody can check.

    A rescued event is marked ``inferred`` in its provenance, so the board can
    say which events are here because a model read them. A planner should never
    have to guess which parts of a screen were computed and which were written.
    """
    from engine.reason import challenge  # noqa: PLC0415
    from engine.reason import extract as reason_extract  # noqa: PLC0415

    reading = reason_extract.extract(item, config, clock)
    if reading is None:
        return None

    verdict = challenge.review(reading, item.get("text", ""), config)
    if not verdict.accepted:
        return None

    merged = reason_extract.to_item_fields(reading, item, clock)
    known = [v for v in reading.active_variables if v in config.variables]
    if not known:
        return None

    nodes = [n for n in merged["node_hint"] if n in config.nodes]
    if not nodes:
        return None
    anchor_node = config.nodes[nodes[0]]
    primary = config.variables[known[0]]

    return Event(
        event_id=item["item_id"],
        title=reading.what_happened[:180],
        node_ids=nodes,
        lat=item.get("lat") if item.get("lat") is not None else anchor_node.lat,
        lon=item.get("lon") if item.get("lon") is not None else anchor_node.lon,
        starts_at=reading.starts_at,
        ends_at=reading.ends_at,
        duration_confidence=reading.duration_confidence,
        event_class=primary.family,
        active_variables=known,
        severity=Severity(challenge.extraction_severity(reading)),
        modes_affected=primary.modes_affected,
        realized=reading.realized,
        probability=reading.probability,
        probability_basis=reading.probability_basis,
        provenance=Provenance(
            source=item.get("source", "unknown"),
            source_tier=int(item.get("source_tier", 3)),
            verbatim_quote=reading.verbatim_quote,
            inferred=True,
            retrieved_at=clock.as_of,
        ),
    )


def _event_from_item(
    item: dict,
    routed: rules_router.RouterResult,
    config: Config,
    network: Network,
    clock: Clock,
) -> Event:
    # Node resolution BEFORE clustering: feeds in four languages spell the same
    # port four ways, so clustering on raw location strings under-merges badly.
    node_ids = [n for n in item["node_hint"] if n in network.nodes]

    quote = next(iter(routed.matched_spans.values()), None)
    primary = routed.active_variables[0] if routed.active_variables else None
    family = config.variables[primary].family if primary else "unknown"

    return Event(
        event_id=item["item_id"],
        title=item["headline"],
        node_ids=node_ids,
        lat=item["lat"],
        lon=item["lon"],
        starts_at=item["starts_at"],
        ends_at=item["ends_at"],
        duration_confidence="stated" if item["ends_at"] else "unknown",
        event_class=family,
        active_variables=routed.active_variables,
        severity=routed.severity,
        modes_affected=_restrict_modes(routed.modes_affected, item),
        realized=item["realized"],
        probability=item["probability"],
        probability_basis=item["probability_basis"],
        provenance=Provenance(
            source=item["source"],
            source_tier=item["source_tier"],
            verbatim_quote=quote,
            inferred=quote is None,
            retrieved_at=item["published_at"],
        ),
    )


def _cluster(events: list[Event]) -> list[Event]:
    """Conservative merge on (node, time window, class) — BRIEF §3.5.

    Be conservative on purpose: over-merging hides a real disruption, while
    under-merging only costs a duplicate row a planner dismisses in a second.
    Asymmetric cost, asymmetric threshold.
    """
    merged: list[Event] = []
    for event in events:
        match = None
        for candidate in merged:
            if candidate.event_class != event.event_class:
                continue
            if not (set(candidate.node_ids) & set(event.node_ids)):
                continue
            gap = abs((candidate.starts_at - event.starts_at).total_seconds()) / 3600.0
            if gap <= 36:
                match = candidate
                break

        if match is None:
            merged.append(event)
            continue

        # Merge: union the variables, keep the higher severity, and prefer the
        # more reliable source's provenance.
        for vid in event.active_variables:
            if vid not in match.active_variables:
                match.active_variables.append(vid)
        order = {Severity.MINOR: 0, Severity.MODERATE: 1, Severity.SEVERE: 2}
        if order[event.severity] > order[match.severity]:
            match.severity = event.severity
        if event.provenance.source_tier < match.provenance.source_tier:
            match.provenance = event.provenance

    return merged


# =====================================================================
# Stage 6-8
# =====================================================================


def _assess(
    events: list[Event],
    shipments: list[Shipment],
    hits: list[GateHit],
    draws,
    config: Config,
    network: Network,
    clock: Clock,
) -> list[EventAssessment]:
    by_id = {s.shipment_id: s for s in shipments}
    assessments: list[EventAssessment] = []

    for event in events:
        event_hits = [h for h in hits if h.event_id == event.event_id]
        shipment_ids = sorted({h.shipment_id for h in event_hits})

        risks: list[ShipmentRisk] = []
        loss_columns: list[np.ndarray] = []

        for sid in shipment_ids:
            shipment = by_id.get(sid)
            if shipment is None:
                continue
            risk, losses = _assess_one(
                shipment, event, event_hits, draws, config, network, clock
            )
            if risk is not None:
                risks.append(risk)
                loss_columns.append(losses)

        if not risks:
            continue

        portfolio = impact_mod.portfolio_distribution(loss_columns)
        risks.sort(key=lambda r: r.value_of_acting_chf, reverse=True)

        # Only POSITIVE value of acting is recoverable. BRIEF §5.4 recommends an
        # action when value of acting > 0; a negative value means the cheapest
        # available option costs more than it saves, which is a reason to leave
        # the shipment alone, not a debt to subtract from what you can rescue
        # elsewhere. Summing the negatives would let a shipment nobody should
        # touch cancel out one that genuinely needs a decision.
        recoverable = sum(
            r.value_of_acting_chf for r in risks if r.value_of_acting_chf > 0
        )

        assessments.append(
            EventAssessment(
                event=event,
                shipment_risks=risks,
                total_value_at_risk_chf=round(portfolio["mean"], 2),
                total_value_of_acting_chf=round(recoverable, 2),
                shipments_affected=len(risks),
                contracts_affected=sorted({r.customer for r in risks}),
                max_priority_chf=max(r.value_of_acting_chf for r in risks),
                variable_contributions=variable_contributions(event, config),
            )
        )

    return assessments


def _assess_one(
    shipment: Shipment,
    event: Event,
    event_hits: list[GateHit],
    draws,
    config: Config,
    network: Network,
    clock: Clock,
) -> tuple[ShipmentRisk | None, np.ndarray]:
    # --- do nothing: the PLANNED route, unswitched --------------------
    base_draws = propagate(shipment, event_hits, draws)
    base = impact_mod.summarise(
        shipment, base_draws, config, cost_multiplier=event.cost_multiplier
    )

    impact_at = leadtime.impact_time(
        event.starts_at, event_hits, shipment.shipment_id, event.event_id
    )
    hours_left = leadtime.hours_until_impact(clock, impact_at)

    options = options_for(
        shipment, event, event_hits, config, network, hours_left
    )
    chosen = best_option(options)

    # --- act: the alternate, priced ------------------------------------
    act_outcome = None
    value = 0.0
    deadline = None
    lead_hours = None

    if chosen is not None:
        act_draws = propagate(
            shipment,
            event_hits,
            draws,
            residual_by_event={event.event_id: chosen.residual_delay_days},
        )
        act = impact_mod.summarise(shipment, act_draws, config)
        value = impact_mod.value_of_acting(
            base["expected_loss_chf"], act["expected_loss_chf"], chosen.cost_chf
        )
        act_outcome = ShipmentOutcome(
            shipment_id=shipment.shipment_id,
            scenario="act",
            p_late=act["p_late"],
            expected_delay_days=act["expected_delay_days"],
            p90_delay_days=act["p90_delay_days"],
            expected_lateness_days=act["expected_lateness_days"],
            expected_loss_chf=act["expected_loss_chf"],
            p90_loss_chf=act["p90_loss_chf"],
            conditional_loss_chf=act["conditional_loss_chf"],
            driving_event_ids=act_draws.driving_event_ids,
        )
        if impact_at is not None:
            deadline = leadtime.decision_deadline(impact_at, chosen)
            lead_hours = clock.hours_until(deadline)

    band = leadtime.actionability(
        lead_hours, chosen.min_hours if chosen else None, config
    )

    # P(late) on the matrix x-axis is None when the event's own probability is
    # unsourceable — it is not a computed 0.5 (BRIEF §8.1).
    p_for_axis = base["p_late"] if event.probability_known else None
    # The impact axis is the CONDITIONAL loss, not the expected one. Expected
    # loss already carries the probability (the Monte Carlo masks each event's
    # delay by its occurrence draw), so pairing it with P(late) counted the
    # same probability on both axes and pushed every unlikely shipment into the
    # bottom-left corner twice over.
    impact_id, _, _ = matrix.impact_band(base["conditional_loss_chf"], config)
    prob_id, _ = matrix.probability_band(p_for_axis, config)

    risk = ShipmentRisk(
        shipment_id=shipment.shipment_id,
        event_id=event.event_id,
        do_nothing=ShipmentOutcome(
            shipment_id=shipment.shipment_id,
            scenario="do_nothing",
            p_late=base["p_late"],
            expected_delay_days=base["expected_delay_days"],
            p90_delay_days=base["p90_delay_days"],
            expected_lateness_days=base["expected_lateness_days"],
            expected_loss_chf=base["expected_loss_chf"],
            p90_loss_chf=base["p90_loss_chf"],
            conditional_loss_chf=base["conditional_loss_chf"],
            driving_event_ids=base_draws.driving_event_ids,
        ),
        best_action=chosen,
        act_outcome=act_outcome,
        value_of_acting_chf=round(value, 2),
        decision_deadline=deadline,
        lead_time_hours=lead_hours,
        actionability=band,  # type: ignore[arg-type]
        impact_band=impact_id,
        probability_band=prob_id,
        value_chf=shipment.value_chf,
        customer=shipment.customer,
        contract_type=shipment.contract_type,
    )
    return risk, base["losses"]


# =====================================================================
# Sockets
# =====================================================================


def _add_absent_sockets(bundle: IngestBundle, clock: Clock) -> None:
    """Declare what is NOT wired, and what wiring it would buy (BRIEF §8.2).

    The feature surface exists and the panel says plainly that it is not
    connected. Never a silent default, never faked data. For a corporate
    audience this is the most persuasive screen in the demo.
    """
    absent = [
        (
            "portwatch",
            "IMF PortWatch — port traffic & chokepoint disruption",
            "coarse and weekly-ish; usable, not a live congestion feed",
            "Congestion and chokepoint exposure on intercontinental lanes.",
        ),
        (
            "portops",
            "Port operating hours, holidays, strike notices",
            "no clean API exists for this anywhere — it has to be socketed",
            "The most predictable constraints in the whole system, known months ahead.",
        ),
        (
            "weather_live",
            "Open-Meteo forecast",
            "blocked by network policy in this environment (403 at the egress proxy)",
            "Live 7-day wind, wave and visibility forecasts at every node.",
        ),
        (
            "osm_routing",
            "OSM / OSMnx road & rail routing",
            "dropped for v1: buffered corridors answer the gate's question at "
            "the resolution a planner cares about, without the dependency chain "
            "or a runtime Overpass call",
            "Turn-by-turn inland routing and true detour distances for reroute costing.",
        ),
        (
            "carrier_feeds",
            "Carrier / forwarder portals",
            "licensed; requires procurement",
            "Real booking status and carrier advisories instead of inferred ones.",
        ),
        (
            "sika_contracts",
            "Sika SLA penalties and customer commitments",
            "commercially sensitive; not supplied",
            "Real CHF exposure instead of the three-part synthetic cost model.",
        ),
        (
            "social_x",
            "X / Twitter firehose",
            "paid API; not wired",
            "Earliest signal on strikes and incidents — typically hours before "
            "trade press, which is where the lead time comes from.",
        ),
        # ---- the rest of the supply-chain API surface -------------------
        # Named rather than described, because "we would connect a weather
        # API" is a wish and "Copernicus Marine, product WAVE_GLO_PHY" is a
        # scoping decision somebody can cost. Each one below is a real
        # service with a real access model, and the access model is the
        # reason it is not wired rather than an oversight.
        (
            "ais",
            "AIS vessel tracking (Spire / MarineTraffic / AISHub)",
            "commercial licence per vessel-track; AISHub is free but "
            "coverage is contributor-dependent and coastal-biased",
            "The single highest-value feed on this list: actual vessel "
            "position against the schedule turns an inferred delay into an "
            "observed one, and moves the ETA from planned to measured.",
        ),
        (
            "notices_to_mariners",
            "Port authority notices & Notices to Mariners",
            "no common format — each authority publishes its own PDF or RSS "
            "(Rotterdam, Antwerp, Hamburg and Singapore all differ)",
            "Authoritative closures, draught restrictions and lock outages "
            "at tier 1, which is the only tier allowed to move a date on its "
            "own. Today those arrive via trade press at tier 2.",
        ),
        (
            "copernicus_marine",
            "Copernicus Marine / EMODnet — wave, current, sea ice",
            "free with registration; this environment's egress proxy blocks "
            "the host",
            "Significant wave height and current on the deep-sea legs, which "
            "is what actually decides a weather routing diversion — wind "
            "speed alone does not.",
        ),
        (
            "waterinfo_nl",
            "Rijkswaterstaat Waterinfo — Dutch waterway levels & lock status",
            "open API; blocked here alongside the German gauge",
            "The lower Rhine and the Dutch canal network. Kaub sets the "
            "loading limit, but a lock outage at Tiel strands the same barge.",
        ),
        (
            "rail_im",
            "Rail infrastructure managers (DB Netz, SBB, ProRail)",
            "TAF/TSI feeds require an operator agreement; the public portals "
            "are HTML",
            "Planned possessions months ahead and live disruption on the "
            "rail legs — the mode a barge derate reroutes ONTO, so its "
            "capacity is what decides whether the reroute is real.",
        ),
        (
            "sanctions",
            "EU / SECO / OFAC consolidated sanctions lists",
            "published as open data; not wired for v1",
            "The geopolitical family's only fully checkable source. A "
            "designated vessel or counterparty is a binary fact, not an "
            "estimate, and it is the one risk here that can strand cargo "
            "with no delay at all.",
        ),
        (
            "customs_waits",
            "Customs & border waiting times (EU TAXUD, Swiss BAZG)",
            "partial and per-crossing; no consolidated API",
            "Border dwell on the road legs. Swiss-EU crossings are the "
            "single most repeated hop in this book, so a systematic bias "
            "there biases everything.",
        ),
        (
            "terminal_slots",
            "Terminal slot / VBS booking systems",
            "per-terminal, licensed, usually behind a forwarder",
            "Whether a truck can actually get a slot, which is what turns "
            "'reroute to road' from an option into a plan.",
        ),
    ]
    # The reasoning model is an input like any other, and it gets the same
    # three states: connected / stand-in / absent. Reported here rather than
    # in a panel of its own, because "is the model running" is exactly the
    # same question as "is the gauge connected".
    model = llm_mod.detect()
    bundle.add(
        FeedReport(
            key="reasoning_model",
            label=f"Reasoning model ({model.backend.value})",
            status=(
                FeedStatus.CONNECTED if model.available else FeedStatus.ABSENT
            ),
            detail=model.detail,
            unlocks_if_connected=model.unlocks_if_connected,
            retrieved_at=clock.as_of,
        )
    )

    for key, label, detail, unlocks in absent:
        bundle.add(
            FeedReport(
                key=key,
                label=label,
                status=FeedStatus.ABSENT,
                detail=detail,
                unlocks_if_connected=unlocks,
                retrieved_at=clock.as_of,
            )
        )


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]
