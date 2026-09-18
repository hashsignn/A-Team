"""Supply Chain Risk Radar — planner dashboard.

LAYOUT (BRIEF §7, with one addition)

    STATE STRIP    portfolio posture + the convene rule           <- NEW
    ┌──────────────────────┬─────────────────┬──────────────────┐
    │ ① MAP                │ ③ WEB CHART     │ ④ ACTIONS &      │
    │   routes, nodes,     │   active vars   │   RESPONSE       │
    │   event dots R/A/G   │   in DAYS       │   options,       │
    │   click a dot ▾      │                 │   contacts,      │
    │ ② RISK MATRIX        │   sources,      │   escalation,    │
    │   HIDDEN until a dot │   deadlines     │   deadline       │
    │   is clicked         │                 │                  │
    └──────────────────────┴─────────────────┴──────────────────┘

The state strip is the one addition to the brief's wireframe, and it exists
because of Sika's answer to Q6: *"The real problem is that we declare a crisis
too late and lose on available options."* BRIEF §12 forbids a global risk
matrix, and that still holds — there is no global P×I matrix here, and the
per-event matrix remains hidden until a dot is clicked. A one-line portfolio
posture is not a matrix.

EXPLICITLY ADVISORY. The wireframe says it twice: *"decision making still
depends on manual effort."* The tool proposes; the planner decides. That is
said in the UI, not only in the pitch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import theme  # noqa: E402
from dashboard.components import charts, map_pane  # noqa: E402
from engine.clock import Clock  # noqa: E402
from engine.config import load_config  # noqa: E402
from engine.ingest.observations import FeedStatus  # noqa: E402
from engine.ingest.watergauge import fetch_kaub  # noqa: E402
from engine.pipeline import RunOptions, run  # noqa: E402
from engine.score.impact import explain  # noqa: E402
from engine.score.leadtime import deadline_text  # noqa: E402

DEFAULT_AS_OF = "2026-09-18T06:00:00+00:00"

st.set_page_config(
    page_title="Supply Chain Risk Radar",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =====================================================================
# Styles
# =====================================================================
st.markdown(
    f"""
    <style>
      .stApp {{ background: {theme.PAGE}; }}
      html, body, [class*="css"] {{ font-family: {theme.FONT}; }}
      .block-container {{ padding-top: 2.2rem; max-width: 1650px; }}
      h1, h2, h3 {{ color: {theme.INK}; font-weight: 600; letter-spacing: -0.01em; }}
      .strip {{
        border-radius: 10px; padding: 14px 18px; margin-bottom: 14px;
        background: {theme.SURFACE}; border: 1px solid {theme.BORDER};
        border-left-width: 5px;
      }}
      .strip-posture {{
        font-size: 11px; font-weight: 700; letter-spacing: 0.10em;
        text-transform: uppercase;
      }}
      .strip-headline {{
        font-size: 15px; color: {theme.INK}; margin-top: 3px; line-height: 1.45;
      }}
      .strip-sub {{ font-size: 12px; color: {theme.INK_MUTED}; margin-top: 6px; }}
      .card {{
        background: {theme.SURFACE}; border: 1px solid {theme.BORDER};
        border-radius: 9px; padding: 13px 15px; margin-bottom: 9px;
      }}
      .lbl {{
        font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
        color: {theme.INK_MUTED}; margin-bottom: 3px;
      }}
      .big {{ font-size: 25px; font-weight: 600; color: {theme.INK}; }}
      .quote {{
        font-size: 12px; color: {theme.INK_SECONDARY}; font-style: italic;
        border-left: 3px solid {theme.GRID}; padding-left: 10px; margin: 7px 0;
        line-height: 1.5;
      }}
      .advisory {{
        font-size: 11px; color: {theme.INK_MUTED}; border-top: 1px solid {theme.GRID};
        padding-top: 9px; margin-top: 13px;
      }}
      .pill {{
        display: inline-block; font-size: 10px; padding: 2px 8px;
        border-radius: 10px; margin-right: 5px; font-weight: 600;
      }}
      div[data-testid="stMetricValue"] {{ font-size: 23px; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# Data
# =====================================================================
@st.cache_data(show_spinner="Running the pipeline…")
def _run(as_of: str, shipments: int, seed: int | None, overrides_key: str, _overrides: dict):
    """One pipeline run, cached.

    ``overrides_key`` is a hash of the assumption edits, so moving a slider in
    the Assumptions drawer invalidates exactly this run and nothing else.
    Without the cache, Streamlit would re-run the whole Monte Carlo on every
    widget touch.
    """
    config = load_config()
    if _overrides:
        _apply_overrides(config, _overrides)
    clock = Clock.at(as_of)
    return run(clock=clock, config=config, options=RunOptions(
        shipment_count=shipments, seed=seed
    ))


def _apply_overrides(config, overrides: dict) -> None:
    """Push the drawer's edits into the loaded config.

    Sika, answering Q3: these colleagues *"will also be able to play with the
    assumptions."* That is a direct instruction to make the parameter table
    editable, so the weakest point in the design — numbers we invented —
    becomes the most engaging moment in the demo: hand them the laptop and let
    them set "Antwerp strike = 4 days, not 3".
    """
    model = config.raw("delay_model")
    for family, severities in overrides.get("delay", {}).items():
        for severity, triple in severities.items():
            model["defaults"][family][severity] = triple
    for action, hours in overrides.get("min_action_hours", {}).items():
        config.scoring["min_action_hours"][action] = hours
    for key, value in overrides.get("convene", {}).items():
        config.scoring["convene_rule"]["thresholds"][key] = value


@st.cache_data(show_spinner=False)
def _gauge(as_of: str):
    config = load_config()
    series, report = fetch_kaub(config, Clock.at(as_of))
    return series, report


# =====================================================================
# Sidebar — settings drawer (BRIEF §7 top bar)
# =====================================================================
with st.sidebar:
    st.markdown("### Risk profile")

    as_of = st.text_input(
        "As-of instant (UTC)", DEFAULT_AS_OF,
        help=(
            "Nothing in the engine reads the wall clock. Pinning the as-of "
            "makes the demo reproducible — and a past as-of is a hindcast, "
            "through the identical code path."
        ),
    )
    shipment_count = st.slider("Shipments in the book", 50, 200, 150, 10)

    st.divider()
    st.markdown("#### Assumptions")
    st.caption(
        "Every number here is ours, not Sika's. Change them and the whole "
        "board recomputes."
    )

    overrides: dict = {"delay": {}, "min_action_hours": {}, "convene": {}}
    base_config = load_config()

    with st.expander("Delay estimates (days)", expanded=False):
        family = st.selectbox(
            "Risk family", base_config.families,
            format_func=lambda f: f.replace("_", " ").title(),
        )
        defaults = base_config.raw("delay_model")["defaults"][family]
        for severity in ("minor", "moderate", "severe"):
            triple = defaults[severity]
            st.markdown(f"**{severity}**")
            c1, c2, c3 = st.columns(3)
            opt = c1.number_input(
                "opt", value=float(triple["optimistic"]), step=0.5,
                key=f"{family}-{severity}-o", label_visibility="collapsed",
            )
            lik = c2.number_input(
                "likely", value=float(triple["likely"]), step=0.5,
                key=f"{family}-{severity}-l", label_visibility="collapsed",
            )
            pes = c3.number_input(
                "pess", value=float(triple["pessimistic"]), step=0.5,
                key=f"{family}-{severity}-p", label_visibility="collapsed",
            )
            # Ordering is enforced by the schema, so clamp here rather than
            # letting the run fail on a half-typed value.
            lik = max(opt, lik)
            pes = max(lik, pes)
            if (opt, lik, pes) != (
                triple["optimistic"], triple["likely"], triple["pessimistic"]
            ):
                overrides["delay"].setdefault(family, {})[severity] = {
                    "optimistic": opt, "likely": lik, "pessimistic": pes,
                }
        st.caption("optimistic · likely · pessimistic")

    with st.expander("Convene thresholds", expanded=False):
        rule = base_config.scoring["convene_rule"]["thresholds"]
        overrides["convene"]["recoverable_chf"] = st.number_input(
            "Recoverable value at risk (CHF)",
            value=int(rule["recoverable_chf"]), step=10_000,
        )
        overrides["convene"]["contracts_exposed"] = st.number_input(
            "Customer contracts exposed", value=int(rule["contracts_exposed"]), step=1,
        )
        overrides["convene"]["cost_of_waiting_chf"] = st.number_input(
            "Cost of waiting one cycle (CHF)",
            value=int(rule["cost_of_waiting_chf"]), step=10_000,
        )
        st.caption(
            "Agreed in calm conditions, not in the middle of a crisis. "
            "That is the point."
        )

    st.divider()
    show_lanes = st.checkbox("Show lanes", True)
    map_scope = st.selectbox(
        "Map scope",
        ["world", "europe", "asia", "north america"],
        help=(
            "Vector geometry, no tiles and no CDN — the map renders with no "
            "network at all."
        ),
    )

overrides_key = str(sorted(str(overrides).encode()))[:64] + str(len(str(overrides)))
context = _run(as_of, shipment_count, None, overrides_key, overrides)
result = context.result
verdict = result.convene


# =====================================================================
# Header + state strip
# =====================================================================
head_l, head_r = st.columns([3, 1])
with head_l:
    st.markdown("## Supply Chain Risk Radar")
    st.caption(
        f"As of {context.clock} · {result.shipments_total} shipments "
        f"(synthetic) · {len(context.config.variables)} risk variables · "
        f"config {result.config_version}"
    )
with head_r:
    st.metric(
        "Events on your lanes",
        result.events_total,
        help="Events touching nothing get no dot. That is the noise filter.",
    )

posture = verdict.posture.value
st.markdown(
    f"""
    <div class="strip" style="border-left-color:{theme.POSTURE_COLOR[posture]}">
      <div class="strip-posture" style="color:{theme.POSTURE_COLOR[posture]}">
        {theme.SEVERITY_ICON.get('severe') if posture == 'convene' else '◆'}
        &nbsp;{theme.POSTURE_LABEL[posture]}
      </div>
      <div class="strip-headline">{verdict.headline}</div>
      <div class="strip-sub">
        {theme.chf(verdict.recoverable_chf)} recoverable ·
        {verdict.contracts_exposed} contracts exposed ·
        {theme.chf(verdict.cost_of_waiting_chf)} expires before the next meeting
        {'· <b>convene rule not yet agreed with the planning team</b>'
         if not verdict.rule_agreed else ''}
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# =====================================================================
# Event selection
# =====================================================================
if "selected_event" not in st.session_state:
    st.session_state.selected_event = (
        result.assessments[0].event.event_id if result.assessments else None
    )

by_id = {a.event.event_id: a for a in result.assessments}
if st.session_state.selected_event not in by_id and result.assessments:
    st.session_state.selected_event = result.assessments[0].event.event_id

tab_board, tab_decay, tab_rhine, tab_inputs = st.tabs(
    ["Board", "Option decay", "Rhine (anchor)", "Inputs"]
)


# =====================================================================
# BOARD
# =====================================================================
with tab_board:
    left, mid, right = st.columns([1.5, 1, 1.15], gap="medium")

    # ---------------- PANE 1: map ------------------------------------
    with left:
        st.markdown("#### Map")

        geo_map = map_pane.build_map(
            context,
            selected_event_id=st.session_state.selected_event,
            show_lanes=show_lanes,
            scope=map_scope,
        )
        # A real click event, not a tooltip string parsed back out. The earlier
        # prototype passed returned_objects=[] to st_folium, which discarded
        # every click and made this interaction impossible.
        selection = st.plotly_chart(
            geo_map,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            key="map",
            config={
                "displayModeBar": False,
                "scrollZoom": True,
                # Vendored geometry, served by Streamlit's own static handler.
                # Plotly would otherwise fetch this from cdn.plot.ly, and the
                # map is the first pane a planner looks at — it must not depend
                # on a CDN being reachable. See static/topojson/README.md.
                "topojsonURL": "/app/static/topojson/",
            },
        )
        clicked = map_pane.event_id_from_selection(selection, context)
        if clicked and clicked in by_id:
            st.session_state.selected_event = clicked

        # A demo must never depend on a click landing, so the same selection is
        # always reachable from a control.
        ids = [a.event.event_id for a in result.assessments]
        if ids:
            st.selectbox(
                "Selected event",
                ids,
                index=ids.index(st.session_state.selected_event)
                if st.session_state.selected_event in ids else 0,
                format_func=lambda eid: (
                    f"{theme.SEVERITY_ICON[by_id[eid].event.severity.value]} "
                    f"{by_id[eid].event.title[:64]}"
                ),
                key="event_picker",
                on_change=lambda: st.session_state.update(
                    selected_event=st.session_state.event_picker
                ),
            )

        # ---------------- PANE 2: the matrix -------------------------
        if st.session_state.selected_event:
            assessment = by_id[st.session_state.selected_event]
            st.markdown("#### Risk matrix — this event only")
            st.plotly_chart(
                charts.risk_matrix(assessment, context.config),
                use_container_width=True,
                config={"displayModeBar": False},
            )
            st.caption(
                "Solid = still actionable · hollow = too late to reroute · "
                "size = value of acting. The event is the question; the "
                "shipments are the answer."
            )

    # ---------------- PANE 3: web chart + evidence -------------------
    with mid:
        if not result.assessments:
            st.info("Nothing on your lanes needs you today.")
        else:
            assessment = by_id[st.session_state.selected_event]
            event = assessment.event

            st.markdown("#### Active variables")
            st.plotly_chart(
                charts.web_chart(assessment, context.config),
                use_container_width=True,
                config={"displayModeBar": False},
            )

            p_text = (
                f"{event.probability:.0%}"
                if event.probability_known
                else "unsourced"
            )
            st.markdown(
                f"""<div class="card">
                  <div class="lbl">Probability</div>
                  <div class="big">{p_text}</div>
                  <div style="font-size:11.5px;color:{theme.INK_SECONDARY};
                              margin-top:5px;line-height:1.5">
                    {event.probability_basis}
                  </div>
                </div>""",
                unsafe_allow_html=True,
            )
            if not event.probability_known:
                st.caption(
                    "Shown in a separate band outside the probability axis. "
                    "We will not invent a number here — a made-up 0.6 looks "
                    "like evidence."
                )

            st.markdown("**Evidence**")
            if event.provenance.verbatim_quote:
                st.markdown(
                    f'<div class="quote">"{event.provenance.verbatim_quote}"</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div class="quote">inferred — no verbatim span</div>',
                    unsafe_allow_html=True,
                )
            st.caption(
                f"{event.provenance.source} · tier {event.provenance.source_tier} · "
                f"retrieved {event.provenance.retrieved_at:%d %b %H:%M} UTC"
            )

            st.markdown("**Contracts affected**")
            for customer in assessment.contracts_affected[:6]:
                st.markdown(
                    f'<span class="pill" style="background:{theme.PAGE};'
                    f'color:{theme.INK_SECONDARY}">{customer}</span>',
                    unsafe_allow_html=True,
                )

    # ---------------- PANE 4: actions & response ---------------------
    with right:
        if result.assessments:
            assessment = by_id[st.session_state.selected_event]
            st.markdown("#### Actions & response")

            actionable = [
                r for r in assessment.shipment_risks if r.value_of_acting_chf > 0
            ]
            if not actionable:
                st.markdown(
                    f"""<div class="card">
                      <div class="lbl">Recommendation</div>
                      <div style="font-size:14px;color:{theme.INK};margin-top:4px">
                        Monitor. No action currently saves more than it costs.
                      </div>
                      <div style="font-size:12px;color:{theme.INK_MUTED};margin-top:7px">
                        This event touches {assessment.shipments_affected} shipments,
                        and their buffers absorb it. That is a real answer, not a
                        gap — it is what lets you stop worrying about this one.
                      </div>
                    </div>""",
                    unsafe_allow_html=True,
                )

            for risk in actionable[:4]:
                action = risk.best_action
                if action is None:
                    continue
                sentence = explain(
                    risk.do_nothing.expected_loss_chf,
                    risk.act_outcome.expected_loss_chf if risk.act_outcome else 0.0,
                    action.cost_chf,
                    action.label,
                    deadline_text(context.clock, risk.decision_deadline),
                )
                colour = theme.ACTIONABILITY_COLOR[risk.actionability]
                st.markdown(
                    f"""<div class="card" style="border-left:4px solid {colour}">
                      <div class="lbl">{risk.shipment_id} · {risk.customer}</div>
                      <div style="font-size:13.5px;color:{theme.INK};
                                  margin:5px 0 8px;line-height:1.5">{sentence}</div>
                      <div style="font-size:11.5px;color:{theme.INK_MUTED}">
                        {theme.ACTIONABILITY_LABEL[risk.actionability]} ·
                        lead {theme.hours(risk.lead_time_hours)} ·
                        needs {action.min_hours:.0f} h ·
                        lever held by <b>{action.owner}</b>
                      </div>
                    </div>""",
                    unsafe_allow_html=True,
                )
                with st.expander(f"Who to involve — {risk.shipment_id}", expanded=False):
                    for contact in action.contacts:
                        st.markdown(f"- {contact}")
                    st.caption(action.description)

                with st.expander(f"Show the arithmetic — {risk.shipment_id}"):
                    st.markdown(
                        f"""
| | CHF |
|---|---:|
| Expected loss, do nothing | {risk.do_nothing.expected_loss_chf:,.0f} |
| Expected loss after acting | {(risk.act_outcome.expected_loss_chf if risk.act_outcome else 0):,.0f} |
| Cost of acting | {action.cost_chf:,.0f} |
| **Value of acting** | **{risk.value_of_acting_chf:,.0f}** |

P(late) doing nothing **{risk.do_nothing.p_late:.0%}** ·
expected delay **{risk.do_nothing.expected_delay_days:.1f} d** ·
P90 delay **{risk.do_nothing.p90_delay_days:.1f} d**

Lateness is measured against the committed date, not the planned ETA, and
`max(0, ·)` is evaluated inside the Monte Carlo — not applied to the mean.
                        """
                    )

            st.markdown(
                '<div class="advisory">This tool is <b>advisory</b>. It proposes; '
                'the planner decides. Decision making still depends on manual '
                'effort — and that is deliberate.</div>',
                unsafe_allow_html=True,
            )


# =====================================================================
# OPTION DECAY
# =====================================================================
with tab_decay:
    st.markdown("#### Option decay — what waiting costs")
    st.markdown(
        "Sika, on what actually goes wrong: *\"The real problem is that we "
        "declare a crisis too late and lose on available options.\"* "
        "Authority is not the bottleneck — the decision to convene is."
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Recoverable now", theme.chf(verdict.recoverable_chf))
    # No delta_color="inverse" here: options expiring is bad news, so the
    # default (negative reads red) is the correct signal. The delta shows the
    # share of the book's optionality being lost rather than repeating the
    # number immediately above it.
    share = (
        verdict.cost_of_waiting_chf / verdict.recoverable_chf
        if verdict.recoverable_chf > 0
        else 0.0
    )
    c2.metric(
        "Expires before next meeting",
        theme.chf(verdict.cost_of_waiting_chf),
        delta=f"-{share:.0%} of options open today",
    )
    c3.metric("Contracts exposed", verdict.contracts_exposed)
    c4.metric("Posture", theme.POSTURE_LABEL[posture])

    st.plotly_chart(
        charts.decay_curve(result.decay_curve, verdict, context.config),
        use_container_width=True,
        config={"displayModeBar": False},
    )

    st.markdown("**Why this is not a new model**")
    st.markdown(
        """
The brief already computes `decision_deadline = impact_time − action_duration`.
That number exists only because options **expire** — so the real decision, even
for one shipment, is never "act or don't" but **act now vs. wait**. Once the
deadline passes, waiting is worth zero. The curve above is the sum of that
across the book. No new parameters: the cliff edges are the `min_action_hours`
table the brief already requires.
        """
    )

    if verdict.triggers_fired:
        st.markdown("**Convene rule — what tripped**")
        for trigger in verdict.triggers_fired:
            st.markdown(f"- {trigger}")
    if not verdict.rule_agreed:
        st.warning(
            "The convene thresholds are placeholders. They only do their job "
            "once the planning team agrees them **in advance, in calm "
            "conditions** — that is what removes the personal cost from the "
            "person who would otherwise have to call it."
        )


# =====================================================================
# RHINE
# =====================================================================
with tab_rhine:
    st.markdown("#### Rhine low water at Kaub — the anchor story")
    st.markdown(
        "*\"When the Rhine recently saw record-low water levels, a planner "
        "first heard about it from a carrier calling to say a barge shipment "
        "would be delayed.\"* This pane runs on numeric feeds alone — no model "
        "anywhere in it."
    )

    series, report = _gauge(as_of)
    if series:
        latest_at, latest_cm = series[-1]
        week = series[-min(len(series), 28)]
        slope = (latest_cm - week[1]) / max(
            1e-6, (latest_at - week[0]).total_seconds() / 86400.0
        )
        g1, g2, g3 = st.columns(3)
        g1.metric("Kaub level", f"{latest_cm:.0f} cm")
        g2.metric("7-day trend", f"{slope:+.1f} cm/day")
        g3.metric("Reading taken", f"{latest_at:%d %b %H:%M} UTC")

        st.plotly_chart(
            charts.gauge_history(series, context.config),
            use_container_width=True,
            config={"displayModeBar": False},
        )

    st.info(
        "**Low water is a payload derate, not a stoppage.** Vessels keep "
        "sailing; they load to reduced draught. The same tonnage then needs "
        "more sailings, and low-water surcharges apply. Modelling it as "
        "\"barge blocked\" is wrong, and a Sika logistics colleague will know "
        "it is wrong.\n\n"
        "**The OTIF consequence runs through \"in full\", not only \"on "
        "time\".** A derate that splits one consignment across two sailings "
        "fails OTIF even when the first part arrives early. That is the "
        "mechanism no generic weather alert would find."
    )

    if report.status is not FeedStatus.CONNECTED:
        st.warning(
            f"**{theme.FEED_STATUS_LABEL[report.status.value]}** — {report.detail}. "
            "The centimetre values are generated, not measured. Swapping in a "
            "real Pegelonline download changes the data, not a line of logic."
        )


# =====================================================================
# INPUTS
# =====================================================================
with tab_inputs:
    st.markdown("#### Inputs — what is real, what is standing in, what is missing")
    st.caption(
        "Anything we cannot wire up ships as a socket: the surface exists, the "
        "panel says plainly it is not connected, and says what connecting it "
        "would unlock. Never a silent default, never faked data."
    )

    order = {FeedStatus.CONNECTED: 0, FeedStatus.FIXTURE: 1, FeedStatus.ABSENT: 2}
    for report in sorted(context.reports, key=lambda r: order[r.status]):
        status = report.status.value
        st.markdown(
            f"""<div class="card" style="border-left:4px solid
                        {theme.FEED_STATUS_COLOR[status]}">
              <div style="display:flex;justify-content:space-between;
                          align-items:baseline">
                <div style="font-size:13.5px;font-weight:600;color:{theme.INK}">
                  {theme.FEED_STATUS_ICON[status]}&nbsp;{report.label}
                </div>
                <div style="font-size:10px;text-transform:uppercase;
                            letter-spacing:0.07em;
                            color:{theme.FEED_STATUS_COLOR[status]}">
                  {theme.FEED_STATUS_LABEL[status]}
                </div>
              </div>
              <div style="font-size:12px;color:{theme.INK_SECONDARY};margin-top:5px">
                {report.detail}
              </div>
              {f'<div style="font-size:11.5px;color:{theme.INK_MUTED};margin-top:6px">'
               f'↳ would unlock: {report.unlocks_if_connected}</div>'
               if report.unlocks_if_connected else ''}
            </div>""",
            unsafe_allow_html=True,
        )

    st.divider()
    f1, f2 = st.columns([1, 1])
    with f1:
        st.markdown("**Ingestion funnel — measured, not asserted**")
        st.plotly_chart(
            charts.funnel_chart(result.funnel),
            use_container_width=True,
            config={"displayModeBar": False},
        )
        st.caption(
            f"{result.funnel.gated_hits} gate hits across "
            f"{result.funnel.shipments_touched} of {result.shipments_total} "
            "shipments. The brief's 10,000 → 20 illustration is a design "
            "sketch; these are this run's real counts."
        )
    with f2:
        st.markdown("**Correctly filtered out**")
        st.caption(
            "The noise filter is the product, so it has to be visible working."
        )
        for item_id, reason in list(context.router_notes.items())[:8]:
            st.markdown(
                f"<div style='font-size:12px;color:{theme.INK_SECONDARY};"
                f"margin-bottom:5px'><b>{item_id}</b> — {reason}</div>",
                unsafe_allow_html=True,
            )
        if context.unpromoted:
            st.markdown("**Held below the corroboration threshold**")
            for item_id, reason in context.unpromoted.items():
                st.markdown(
                    f"<div style='font-size:12px;color:{theme.INK_SECONDARY};"
                    f"margin-bottom:5px'><b>{item_id}</b> — {reason}</div>",
                    unsafe_allow_html=True,
                )
            st.caption(
                "Social media is not a *better* signal, it is an *earlier* one. "
                "Its whole value is arriving while options are still cheap — "
                "which is a lead-time argument, so it belongs on the decay "
                "curve rather than in the accuracy story."
            )

    st.divider()
    st.markdown("**Risk ledger coverage**")
    st.caption(
        "Sika confirmed no risk ledger for outgoing shipments exists today "
        "(Q1). This is the proposed one — 45 fully specified variables rather "
        "than 100 half-specified ones."
    )
    cols = st.columns(5)
    for i, fam in enumerate(context.config.families):
        variables = context.config.variables_for_family(fam)
        cols[i % 5].metric(fam.replace("_", " ").title(), len(variables))
