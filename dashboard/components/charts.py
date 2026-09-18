"""PANES 2 and 3 — the per-event matrix, the option-decay curve, the web chart.

All three are built in Python from numbers the engine already produced, so the
same code path serves the UI, the export and the tests. No chart logic lives in
JavaScript where it cannot be asserted on.
"""

from __future__ import annotations

import plotly.graph_objects as go

from dashboard import theme
from engine.config import Config
from engine.schemas import EventAssessment, Posture

# =====================================================================
# PANE 2 — the per-event risk matrix
# =====================================================================


def risk_matrix(assessment: EventAssessment, config: Config) -> go.Figure:
    """A banded grid with labelled quadrant actions — NOT a scatter plot.

    The points inside the cells are SHIPMENTS. The event is the question; the
    shipments are the answer. A planner clicks a strike and sees which six of
    their two hundred shipments it touches and how badly each is exposed.

    Four dimensions on one readable chart:
      x     P(shipment is late)
      y     CHF impact band
      ring  lead time — solid = still actionable, hollow = too late
      size  value of acting

    Lead time is an overlay, not a third axis. A third spatial axis would
    destroy the chart.
    """
    grid = config.scoring["matrix"]
    impact_bands = grid["impact_bands"]          # declared most severe first
    prob_bands = grid["probability_bands"]

    n_impact = len(impact_bands)
    n_prob = len(prob_bands)

    # The unsourced column sits at x = -1, visually outside the axis. An event
    # whose probability cannot be sourced has no place ON the 0–1 scale, and
    # putting it at 0.5 would be the exact bug BRIEF §8.1 forbids.
    has_unsourced = any(
        r.probability_band == "P0" for r in assessment.shipment_risks
    )
    x_min = -1.6 if has_unsourced else -0.05

    fig = go.Figure()

    # --- band shading: impact rows, recessive ------------------------
    for i, band in enumerate(impact_bands):
        y_top = n_impact - i
        fig.add_shape(
            type="rect", x0=x_min, x1=n_prob, y0=y_top - 1, y1=y_top,
            fillcolor=theme.SURFACE if i % 2 else theme.PAGE,
            line={"width": 0}, layer="below",
        )
        # Paper coordinates, not data coordinates: at x = n_prob + 0.06 these
        # labels sit outside the x range and Plotly clips them, so the quadrant
        # actions — the whole reason this is a banded grid rather than a
        # scatter plot — silently disappear.
        fig.add_annotation(
            x=1.015, y=y_top - 0.5, xref="paper", yref="y",
            text=f"<b>{band['id']}</b> &nbsp;{band['label']}<br>"
                 f"<span style='font-size:10px'>{band['action']}</span>",
            showarrow=False, xanchor="left", align="left",
            font={"size": 11, "color": theme.INK_SECONDARY},
        )

    # --- gridlines -----------------------------------------------------
    for j in range(n_prob + 1):
        fig.add_shape(
            type="line", x0=j, x1=j, y0=0, y1=n_impact,
            line={"color": theme.GRID, "width": 1}, layer="below",
        )
    for i in range(n_impact + 1):
        fig.add_shape(
            type="line", x0=x_min, x1=n_prob, y0=i, y1=i,
            line={"color": theme.GRID, "width": 1}, layer="below",
        )

    if has_unsourced:
        fig.add_shape(
            type="rect", x0=x_min, x1=-0.35, y0=0, y1=n_impact,
            fillcolor=theme.PAGE,
            line={"color": theme.AXIS, "width": 1, "dash": "dot"},
            layer="below",
        )
        fig.add_annotation(
            x=(x_min - 0.35) / 2, y=n_impact + 0.18,
            text="<b>P unsourced</b>", showarrow=False,
            font={"size": 10, "color": theme.INK_MUTED},
        )

    # --- shipment markers ---------------------------------------------
    impact_row = {band["id"]: n_impact - i - 0.5 for i, band in enumerate(impact_bands)}
    prob_col = {band["id"]: j + 0.5 for j, band in enumerate(prob_bands)}

    # Deterministic jitter so markers in one cell separate without the chart
    # jumping around between reruns.
    from hashlib import md5

    def jitter(key: str, spread: float) -> float:
        h = int(md5(key.encode()).hexdigest()[:8], 16)
        return ((h % 1000) / 1000.0 - 0.5) * spread

    for actionable in (True, False):
        xs, ys, sizes, texts, colors = [], [], [], [], []
        for risk in assessment.shipment_risks:
            still_open = risk.actionability in ("comfortable", "tightening")
            if still_open != actionable:
                continue

            if risk.probability_band == "P0":
                x = (x_min - 0.35) / 2 + jitter(risk.shipment_id, 0.55)
            else:
                x = prob_col.get(risk.probability_band, 0.5) + jitter(
                    risk.shipment_id, 0.62
                )
            y = impact_row.get(risk.impact_band, 0.5) + jitter(
                risk.shipment_id + "y", 0.55
            )

            xs.append(x)
            ys.append(y)
            sizes.append(
                10 + min(24.0, (max(0.0, risk.value_of_acting_chf) / 4000.0) ** 0.6 * 5)
            )
            colors.append(theme.SEVERITY_COLOR[assessment.event.severity.value])
            texts.append(
                f"<b>{risk.shipment_id}</b><br>"
                f"{risk.customer}<br>"
                f"Value {theme.chf(risk.value_chf)}<br>"
                f"P(late) {risk.do_nothing.p_late:.0%}<br>"
                f"Expected loss {theme.chf(risk.do_nothing.expected_loss_chf)}<br>"
                f"Value of acting {theme.chf(risk.value_of_acting_chf)}<br>"
                f"{theme.ACTIONABILITY_LABEL[risk.actionability]}"
            )

        if not xs:
            continue

        fig.add_trace(
            go.Scatter(
                x=xs, y=ys, mode="markers",
                marker={
                    "size": sizes,
                    "color": colors if actionable else theme.SURFACE,
                    "line": {
                        "color": colors if not actionable else theme.SURFACE,
                        "width": 2,
                    },
                    "opacity": 0.9 if actionable else 1.0,
                },
                text=texts, hoverinfo="text",
                name="still actionable" if actionable else "too late",
            )
        )

    tickvals = [j + 0.5 for j in range(n_prob)]
    ticktext = [b["label"] for b in prob_bands]
    if has_unsourced:
        tickvals = [(x_min - 0.35) / 2] + tickvals
        ticktext = ["unsourced"] + ticktext

    fig.update_layout(
        **theme.plotly_layout(
            height=380,
            margin={"l": 20, "r": 196, "t": 46, "b": 52},
            title={
                "text": (
                    f"{assessment.shipments_affected} shipments affected · "
                    f"{theme.chf(assessment.total_value_at_risk_chf)} at risk · "
                    f"one dot = one shipment"
                ),
                "font": {"size": 12, "color": theme.INK_MUTED},
                "x": 0, "xanchor": "left",
            },
            xaxis={
                "range": [x_min - 0.1, n_prob + 0.05],
                "tickvals": tickvals, "ticktext": ticktext,
                "title": {
                    "text": "P(shipment is late)",
                    "font": {"size": 11, "color": theme.INK_MUTED},
                },
                "showgrid": False, "zeroline": False,
                "tickfont": {"size": 10, "color": theme.INK_MUTED},
            },
            yaxis={
                "range": [0, n_impact + 0.4],
                "showticklabels": False, "showgrid": False, "zeroline": False,
            },
        )
    )
    return fig


# =====================================================================
# THE OPTION-DECAY CURVE
# =====================================================================


def decay_curve(points, verdict, config: Config) -> go.Figure:
    """R(t) — recoverable value as options expire.

    Monotonically non-increasing by construction: options only expire. The
    steps are the cliff edges, and each one is a moment worth convening before.

    The shaded region between now and the next standing meeting is the sentence
    the whole reframe produces: *this is what waiting costs*.
    """
    if not points:
        return _empty("No mitigation options currently open")

    xs = [p.hours_from_now / 24.0 for p in points]
    ys = [p.recoverable_chf for p in points]

    fig = go.Figure()

    # Cost of waiting: now → next meeting.
    if verdict.next_meeting_at is not None and points:
        meeting_days = (
            verdict.next_meeting_at - points[0].at
        ).total_seconds() / 86400.0
        if 0 < meeting_days < max(xs):
            fig.add_shape(
                type="rect", x0=0, x1=meeting_days,
                y0=0, y1=max(ys) * 1.08,
                fillcolor=theme.CRITICAL, opacity=0.07,
                line={"width": 0}, layer="below",
            )
            fig.add_shape(
                type="line", x0=meeting_days, x1=meeting_days,
                y0=0, y1=max(ys) * 1.08,
                line={"color": theme.CRITICAL, "width": 1.5, "dash": "dash"},
            )
            fig.add_annotation(
                x=meeting_days, y=max(ys) * 1.04,
                text=f"next standing meeting<br>{verdict.next_meeting_at:%a %d %b}",
                showarrow=False, xanchor="left", xshift=6,
                font={"size": 10, "color": theme.CRITICAL}, align="left",
            )

    fig.add_trace(
        go.Scatter(
            x=xs, y=ys, mode="lines",
            line={"color": theme.SERIES[0], "width": 2, "shape": "hv"},
            fill="tozeroy", fillcolor="rgba(42,120,214,0.10)",
            hovertemplate=(
                "%{x:.1f} days from now<br>"
                "<b>CHF %{y:,.0f}</b> still recoverable<extra></extra>"
            ),
            name="Recoverable value",
        )
    )

    # Label the cliff edges — the moments something actually falls off.
    for i in range(1, len(points)):
        drop = points[i - 1].recoverable_chf - points[i].recoverable_chf
        if drop > max(ys) * 0.12 and points[i - 1].expiring_next:
            fig.add_annotation(
                x=xs[i], y=ys[i],
                text=f"−{theme.chf(drop)}", showarrow=True,
                arrowhead=0, arrowcolor=theme.INK_MUTED, arrowwidth=1,
                ax=0, ay=-26,
                font={"size": 10, "color": theme.INK_SECONDARY},
            )

    fig.update_layout(
        **theme.plotly_layout(
            height=300,
            margin={"l": 70, "r": 24, "t": 40, "b": 46},
            xaxis={
                "title": {
                    "text": "days from now",
                    "font": {"size": 11, "color": theme.INK_MUTED},
                },
                "gridcolor": theme.GRID, "linecolor": theme.AXIS, "zeroline": False,
                "tickfont": {"size": 10, "color": theme.INK_MUTED},
            },
            yaxis={
                "title": {
                    "text": "recoverable (CHF)",
                    "font": {"size": 11, "color": theme.INK_MUTED},
                },
                "gridcolor": theme.GRID, "linecolor": theme.AXIS,
                "zeroline": False, "rangemode": "tozero",
                "tickformat": ",.0f",
                "tickfont": {"size": 10, "color": theme.INK_MUTED},
            },
        )
    )
    return fig


# =====================================================================
# PANE 3 — the web chart
# =====================================================================


def web_chart(assessment: EventAssessment, config: Config) -> go.Figure:
    """Active variables for the selected event, in DAYS OF DELAY.

    Spokes are labelled in days, not abstract weights, so a planner reads
    "water level 4 days, lock closure 2 days, port congestion 1 day" rather
    than "variable 47: 0.63".

    Only the ACTIVE subset appears. Everything else was masked to zero before
    it touched anything, which is what keeps the chart legible and the
    reasoning arguable.
    """
    contributions = assessment.variable_contributions
    if not contributions:
        return _empty("No variables active for this event")

    variables = config.variables
    labels, values, hovers = [], [], []
    for vid, days in sorted(contributions.items(), key=lambda kv: -kv[1]):
        var = variables.get(vid)
        labels.append(var.name if var else vid)
        values.append(days)
        hovers.append(
            f"<b>{var.name if var else vid}</b><br>"
            f"{days:.1f} days expected delay<br>"
            f"<span style='font-size:11px'>{(var.family if var else '')}</span>"
        )

    # A radar needs three spokes to be a shape. Below that a bar is honest and
    # readable; forcing a polygon out of two points is chart junk.
    if len(labels) < 3:
        fig = go.Figure(
            go.Bar(
                x=values, y=labels, orientation="h",
                marker={"color": theme.SERIES[0], "cornerradius": 4},
                text=[f"{v:.1f} d" for v in values],
                textposition="outside",
                textfont={"size": 11, "color": theme.INK_SECONDARY},
                hovertext=hovers, hoverinfo="text",
            )
        )
        fig.update_layout(
            **theme.plotly_layout(
                height=240,
                margin={"l": 8, "r": 60, "t": 28, "b": 40},
                xaxis={
                    "title": {
                        "text": "days of delay",
                        "font": {"size": 11, "color": theme.INK_MUTED},
                    },
                    "gridcolor": theme.GRID, "zeroline": False,
                    "tickfont": {"size": 10, "color": theme.INK_MUTED},
                },
                yaxis={"automargin": True, "showgrid": False,
                       "tickfont": {"size": 11, "color": theme.INK_SECONDARY}},
            )
        )
        return fig

    closed_labels = labels + [labels[0]]
    closed_values = values + [values[0]]

    fig = go.Figure(
        go.Scatterpolar(
            r=closed_values, theta=closed_labels,
            fill="toself",
            fillcolor="rgba(42,120,214,0.16)",
            line={"color": theme.SERIES[0], "width": 2},
            marker={"size": 8, "color": theme.SERIES[0]},
            hovertext=hovers + [hovers[0]], hoverinfo="text",
        )
    )
    fig.update_layout(
        paper_bgcolor=theme.SURFACE,
        font={"family": theme.FONT, "size": 11, "color": theme.INK_SECONDARY},
        height=300,
        margin={"l": 60, "r": 60, "t": 34, "b": 30},
        showlegend=False,
        polar={
            "bgcolor": theme.SURFACE,
            "radialaxis": {
                "visible": True,
                "gridcolor": theme.GRID,
                "linecolor": theme.GRID,
                "tickfont": {"size": 9, "color": theme.INK_MUTED},
                "ticksuffix": " d",
                "angle": 90,
            },
            "angularaxis": {
                "gridcolor": theme.GRID,
                "linecolor": theme.AXIS,
                "tickfont": {"size": 10, "color": theme.INK_SECONDARY},
            },
        },
    )
    return fig


# =====================================================================
def funnel_chart(funnel) -> go.Figure:
    """The MEASURED ingestion funnel.

    BRIEF §3.2 illustrates 10,000 → 500 → 100 → 60 → 20. Those are design
    numbers. These are the counts from the run just executed, so the panel
    shows a measurement instead of a claim.
    """
    stages = [
        ("Raw observations", funnel.raw_observations),
        ("On our geography", funnel.after_geographic),
        ("Looks like disruption", funnel.after_type),
        ("Could still touch us", funnel.after_temporal),
        ("After resolution", funnel.after_resolution),
        ("Reasoned over", funnel.reasoned),
    ]
    labels = [s[0] for s in stages]
    values = [s[1] for s in stages]

    # Ordinal ramp: discrete ordered stages, so it starts no lighter than the
    # step that still clears contrast against this surface.
    ramp = theme.ORDINAL_6

    fig = go.Figure(
        go.Bar(
            x=values, y=labels, orientation="h",
            marker={"color": ramp, "cornerradius": 4},
            text=[str(v) for v in values],
            textposition="outside",
            textfont={"size": 11, "color": theme.INK_SECONDARY},
            hovertemplate="%{y}: <b>%{x}</b><extra></extra>",
        )
    )
    fig.update_layout(
        **theme.plotly_layout(
            height=230,
            margin={"l": 8, "r": 46, "t": 10, "b": 30},
            xaxis={"showgrid": True, "gridcolor": theme.GRID, "zeroline": False,
                   "tickfont": {"size": 10, "color": theme.INK_MUTED}},
            yaxis={"automargin": True, "autorange": "reversed", "showgrid": False,
                   "tickfont": {"size": 11, "color": theme.INK_SECONDARY}},
        )
    )
    return fig


def gauge_history(series, config: Config) -> go.Figure:
    """Kaub level with the loading-restriction bands drawn on.

    The bands are what make the chart readable: a number in centimetres means
    nothing to most people, but "we are below the line where barges start
    loading light, and still falling" is immediately legible.
    """
    if not series:
        return _empty("No gauge readings available")

    spec = config.thresholds["water_gauges"]["GAUGE_KAUB"]
    xs = [t for t, _ in series]
    ys = [v for _, v in series]

    fig = go.Figure()

    for band in spec["low_water_bands"]:
        fig.add_hline(
            y=band["below_cm"],
            line={"color": theme.SEVERITY_COLOR[band["severity"]],
                  "width": 1, "dash": "dot"},
            annotation_text=(
                f"{band['below_cm']} cm → {band['payload_fraction']:.0%} payload"
            ),
            annotation_position="right",
            annotation_font={"size": 9, "color": theme.INK_MUTED},
        )

    fig.add_trace(
        go.Scatter(
            x=xs, y=ys, mode="lines",
            line={"color": theme.SERIES[0], "width": 2},
            hovertemplate="%{x|%d %b %H:%M}<br><b>%{y:.0f} cm</b><extra></extra>",
            name="Kaub",
        )
    )

    fig.update_layout(
        **theme.plotly_layout(
            height=260,
            margin={"l": 56, "r": 170, "t": 16, "b": 40},
            yaxis={
                "title": {"text": "cm", "font": {"size": 11, "color": theme.INK_MUTED}},
                "gridcolor": theme.GRID, "zeroline": False,
                "tickfont": {"size": 10, "color": theme.INK_MUTED},
            },
            xaxis={"gridcolor": theme.GRID, "zeroline": False,
                   "tickfont": {"size": 10, "color": theme.INK_MUTED}},
        )
    )
    return fig


def _empty(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message, showarrow=False,
        font={"size": 12, "color": theme.INK_MUTED},
        xref="paper", yref="paper", x=0.5, y=0.5,
    )
    fig.update_layout(
        **theme.plotly_layout(
            height=200,
            xaxis={"visible": False}, yaxis={"visible": False},
        )
    )
    return fig
