"""The rules router — the CHALLENGER, not the fallback (BRIEF §8.5).

Every reasoned path has a deterministic counterpart that always runs alongside
it. Three reasons, all of which pay for themselves:

* the system is demoable with no model at all, so a partial run still produces
  a complete board;
* there is a baseline to measure the reasoning against from day one;
* if the model is unavailable on the day, the demo still works.

WHAT THIS BASELINE CANNOT DO, AND WHY THAT IS THE POINT
-------------------------------------------------------
Keyword matching cannot tell a scheduled 24-hour stoppage from an indefinite
strike, and their delay distributions differ by an order of magnitude. It
cannot read "unless talks resume" as the probability estimate it is. It cannot
find second-order effects — "Panama cuts transits" pushing volume round the
Cape and congesting Singapore weeks later.

So the router deliberately does the cheap thing well and abstains loudly where
judgement is required. The measured gap between this and the reasoning layer is
the value claim, stated as a measurement rather than a promise (BRIEF §6.6).

ON THE AGREEMENT METRIC
-----------------------
Agreement between rules and model is worth shipping — it is cheap and it is a
real challenger — but it must not be sold as validation. Both were written by
the same people from the same variable list, so their errors correlate. The
honest headline metric is the hindcast: did we fire before the carrier called?
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from engine.schemas import Mode, RiskVariable, Severity

# Keyword patterns per variable. Deliberately readable: a wrong one is findable
# by a person, which is the only quality assurance this layer gets.
PATTERNS: dict[str, list[str]] = {
    "WAT_LOW_WATER": [r"low water", r"niedrigwasser", r"draught restriction", r"kaub", r"water level.*fall"],
    "WAT_HIGH_WATER": [r"high water", r"hochwasser", r"flood mark", r"navigation suspended"],
    "WAT_LOCK_CLOSURE": [r"lock (closure|closed|failure)", r"schleuse"],
    "WAT_CANAL_RESTRICTION": [r"canal (restriction|transit|slot)", r"draft limit", r"transit cap"],
    "WAT_WATERWAY_INCIDENT": [r"channel block", r"wreck", r"salvage", r"waterway closed"],
    "LAB_PORT_STRIKE": [r"dockworker", r"port strike", r"terminal strike", r"stevedore", r"walkout"],
    "LAB_HAULIER_STRIKE": [r"haulier", r"truck(er)? strike", r"road blockade", r"gate blockade"],
    "LAB_RAIL_STRIKE": [r"rail strike", r"train driver", r"gdl", r"signaller strike"],
    "LAB_UNION_BALLOT": [r"ballot", r"strike notice", r"industrial action.*vote", r"talks (broke|collapsed)"],
    "LAB_STAFF_SHORTAGE": [r"staff shortage", r"go-slow", r"work.to.rule", r"absenteeism"],
    "POR_CONGESTION": [r"congestion", r"waiting time", r"berth queue", r"anchorage"],
    "POR_BERTH_UNAVAILABLE": [r"berth (unavailable|closed|occupied)"],
    "POR_CRANE_FAILURE": [r"crane (failure|breakdown|out of service)", r"equipment failure"],
    "POR_CUSTOMS_BACKLOG": [r"customs (delay|backlog)", r"inspection regime", r"clearance delay"],
    "POR_HOLIDAY": [r"public holiday", r"reduced gate hours", r"terminal closed.*holiday"],
    "POR_CAPACITY_CUT": [r"capacity (cut|reduction)", r"quay (closed|lost)", r"terminal works"],
    "CLI_HIGH_WIND": [r"high wind", r"gale", r"wind warning", r"storm force"],
    "CLI_STORM": [r"storm", r"cyclone", r"typhoon", r"hurricane", r"depression"],
    "CLI_WAVE_HEIGHT": [r"wave height", r"heavy seas", r"swell"],
    "CLI_FOG": [r"\bfog\b", r"visibility"],
    "CLI_ICE": [r"\bice\b", r"freezing", r"frost"],
    "CLI_EXTREME_HEAT": [r"heatwave", r"extreme heat", r"track buckl"],
    "GEO_CONFLICT": [
        r"attack", r"missile", r"conflict", r"war risk", r"hostilit",
        # Chokepoint closure by announcement rather than by damage. Added
        # because the common cases should not cost a model call — but note
        # that a keyword list can only ever catch the phrasings somebody has
        # already thought of, which is why an abstention here is RESCUED to
        # the funnel rather than dropped (see pipeline._to_events).
        r"strait[^.]{0,40}(clos|blockad|restrict|shut)",
        r"(clos|blockad|shut)[^.]{0,40}strait",
        r"blockad", r"seizure", r"seized[^.]{0,30}(vessel|tanker|ship)",
        r"(vessel|tanker|ship)[^.]{0,30}(seized|detained|boarded)",
        r"naval[^.]{0,20}(exercise|escort|blockade)",
        r"freedom of navigation",
    ],
    "GEO_SANCTIONS": [r"sanction", r"embargo", r"designated (entity|vessel)"],
    "GEO_BORDER_CLOSURE": [r"border (closed|closure|control)", r"frontier"],
    "GEO_REGULATORY": [r"regulation", r"directive", r"compliance requirement", r"reach\b"],
    "GEO_TARIFF": [r"tariff", r"duty (increase|change)", r"customs duty"],
    "INF_BRIDGE_CLOSURE": [r"bridge (closed|closure|weight limit)"],
    "INF_TUNNEL_CLOSURE": [r"tunnel (closed|closure)", r"gotthard", r"brenner"],
    "INF_RAIL_WORKS": [r"engineering works", r"line possession", r"planned closure"],
    "INF_ROAD_CLOSURE": [r"motorway closed", r"autobahn", r"road closure", r"landslide"],
    "INF_RAIL_BLOCKAGE": [r"derailment", r"line blocked", r"signalling failure", r"embankment"],
    "FOR_EARTHQUAKE": [r"earthquake", r"seismic", r"magnitude \d"],
    "FOR_FLOOD": [r"flood", r"inundat", r"overtopp"],
    "FOR_FIRE": [r"\bfire\b", r"blaze"],
    "FOR_PANDEMIC": [r"pandemic", r"quarantine", r"health restriction"],
    "MEC_VESSEL_BREAKDOWN": [r"breakdown", r"engine failure", r"disabled vessel", r"under tow"],
    "MEC_COLLISION": [r"collision", r"collided", r"struck"],
    "MEC_GROUNDING": [r"aground", r"grounding", r"ran aground"],
    "MEC_CARGO_FIRE": [r"cargo fire", r"dangerous goods incident", r"container fire"],
    "CAP_BLANK_SAILING": [r"blank sailing", r"blanked", r"omit(ted)? (the )?call", r"void sailing"],
    "CAP_RATE_SPIKE": [r"rate (spike|surge|increase)", r"freight rates"],
    "CAP_CARRIER_INSOLVENCY": [r"insolvenc", r"ceased trading", r"administration", r"bankrupt"],
    "CYB_PORT_IT_OUTAGE": [r"cyber", r"ransomware", r"it outage", r"systems down"],
    "CYB_CARRIER_SYSTEMS": [r"booking system", r"platform outage", r"portal down"],
}

_COMPILED = {
    vid: [re.compile(p, re.IGNORECASE) for p in patterns]
    for vid, patterns in PATTERNS.items()
}

# Severity cues. Crude on purpose — this is the baseline, and its crudeness is
# the measurement.
_SEVERE = re.compile(
    r"indefinite|open-ended|complete (closure|shutdown)|record|unprecedented|"
    r"all terminals|nationwide|force majeure",
    re.IGNORECASE,
)
_MODERATE = re.compile(
    r"\b\d+[- ]day\b|\b\d+ hours?\b|partial|several|multiple|significant|"
    r"\b(two|three|four|five|six)\s+(weeks?|days?|months?)\b|"
    r"\bextended\b|\breduc(tion|ed|es)\b|\bfortnight\b",
    re.IGNORECASE,
)


@dataclass
class RouterResult:
    """What a router produced for one text.

    ``abstained`` is a first-class outcome. A router that always answers is a
    router that invents, which is the failure this whole discipline exists to
    prevent.
    """

    active_variables: list[str]
    severity: Severity
    modes_affected: list[Mode]
    matched_spans: dict[str, str] = field(default_factory=dict)
    abstained: bool = False
    abstain_reason: str | None = None
    router: str = "rules"


def route(
    text: str,
    variables: dict[str, RiskVariable],
) -> RouterResult:
    """Map free text to the variables it activates."""
    active: list[str] = []
    spans: dict[str, str] = {}

    for vid, patterns in _COMPILED.items():
        if vid not in variables:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                active.append(vid)
                spans[vid] = _span(text, match)
                break

    if not active:
        return RouterResult(
            active_variables=[],
            severity=Severity.MINOR,
            modes_affected=[],
            abstained=True,
            abstain_reason="no known risk vocabulary matched this text",
        )

    # Typically k = 2..5 active out of 45 (BRIEF §4). More than that and the
    # match is almost certainly spurious — a long article touching many topics
    # rather than one event. Keep the strongest and say so.
    if len(active) > 5:
        active = active[:5]

    modes: list[Mode] = []
    for vid in active:
        for mode in variables[vid].modes_affected:
            if mode not in modes:
                modes.append(mode)

    return RouterResult(
        active_variables=active,
        severity=_severity(text),
        modes_affected=modes,
        matched_spans=spans,
    )


def _severity(text: str) -> Severity:
    if _SEVERE.search(text):
        return Severity.SEVERE
    if _MODERATE.search(text):
        return Severity.MODERATE
    return Severity.MINOR


def _span(text: str, match: re.Match, width: int = 70) -> str:
    """The sentence fragment a match came from.

    BRIEF §8.4: every extracted value quotes the sentence it came from, or is
    marked inferred. A value with neither is a bug and is displayed as one.
    """
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    fragment = text[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def agreement(
    rules_result: RouterResult,
    model_variables: list[str],
) -> dict:
    """Compare the two routers over the same text (BRIEF §6.6).

    No human labels needed. Reports where they agree (the model adds nothing —
    use rules, save the compute), where the model finds variables the rules miss
    (the value claim, measured), and where the rules fire and the model does not
    (potential failure).
    """
    rules_set = set(rules_result.active_variables)
    model_set = set(model_variables)
    return {
        "agreed": sorted(rules_set & model_set),
        "model_only": sorted(model_set - rules_set),
        "rules_only": sorted(rules_set - model_set),
        "jaccard": (
            len(rules_set & model_set) / len(rules_set | model_set)
            if (rules_set | model_set)
            else 1.0
        ),
    }
