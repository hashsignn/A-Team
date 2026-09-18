"""One palette, used by every pane.

Validated categorical + status palette. The rules that matter here:

* **Status colours are reserved.** good / warning / serious / critical never
  double as "series 4". They always ship with an icon or a text label, never
  colour alone — on a light surface warning and serious sit below 3:1 contrast
  by design, and the label is the mitigation.
* **Colour follows the entity, not its rank.** Filtering the board must not
  repaint the survivors.
* **Sequential is one hue, light to dark.** No rainbows anywhere.

Severity on the map and in the matrix is a *status* scale, not a categorical
one — it is ordered and it means a state, so it uses the status ramp.
"""

from __future__ import annotations

# --- surfaces & ink ---------------------------------------------------
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BORDER = "rgba(11,11,11,0.10)"

# --- status (reserved — never a series colour) ------------------------
GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"

# --- categorical, in fixed order, never cycled ------------------------
SERIES = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

# --- sequential (single hue, light -> dark) ---------------------------
BLUE_RAMP = {
    100: "#cde2fb", 150: "#b7d3f6", 200: "#9ec5f4", 250: "#86b6ef",
    300: "#6da7ec", 350: "#5598e7", 400: "#3987e5", 450: "#2a78d6",
    500: "#256abf", 550: "#1c5cab", 600: "#184f95", 650: "#104281",
    700: "#0d366b",
}

# Ordinal ramps (discrete ordered stages, not continuous magnitude) must keep
# the step nearest the surface above 2:1 contrast. On this light surface that
# means starting no lighter than step 250.
ORDINAL_6 = [BLUE_RAMP[k] for k in (250, 350, 450, 550, 650, 700)]

# ---------------------------------------------------------------------
# Semantic mappings
# ---------------------------------------------------------------------

SEVERITY_COLOR = {
    "severe": CRITICAL,
    "moderate": SERIOUS,
    "minor": WARNING,
}

# Icons carry the meaning alongside colour, so severity never reads by hue
# alone — required, because warning and serious are sub-3:1 on this surface.
SEVERITY_ICON = {"severe": "●", "moderate": "◆", "minor": "▲"}

POSTURE_COLOR = {"convene": CRITICAL, "watch": WARNING, "normal": GOOD}
POSTURE_LABEL = {"convene": "CONVENE", "watch": "WATCH", "normal": "NORMAL"}

ACTIONABILITY_COLOR = {
    "comfortable": GOOD,
    "tightening": WARNING,
    "too_late": INK_MUTED,
    "no_action": INK_MUTED,
}
ACTIONABILITY_LABEL = {
    "comfortable": "Comfortable",
    "tightening": "Tightening",
    "too_late": "Too late to reroute",
    "no_action": "No action configured",
}

# Mode colours are categorical — identity, not magnitude.
MODE_COLOR = {
    "barge": SERIES[0],
    "sea": SERIES[2],
    "rail": SERIES[6],
    "road": SERIES[1],
}

FEED_STATUS_COLOR = {
    "connected": GOOD,
    "fixture": WARNING,
    "absent": INK_MUTED,
}
FEED_STATUS_ICON = {"connected": "●", "fixture": "◐", "absent": "○"}
FEED_STATUS_LABEL = {
    "connected": "Connected",
    "fixture": "Example stand-in",
    "absent": "Not connected",
}

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def plotly_layout(**overrides) -> dict:
    """Shared chart chrome: recessive grid, no chart junk."""
    base = {
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": FONT, "size": 12, "color": INK_SECONDARY},
        "margin": {"l": 56, "r": 20, "t": 32, "b": 44},
        "hoverlabel": {
            "bgcolor": SURFACE,
            "bordercolor": AXIS,
            "font": {"family": FONT, "size": 12, "color": INK},
        },
        "xaxis": {
            "gridcolor": GRID,
            "linecolor": AXIS,
            "zeroline": False,
            "tickfont": {"color": INK_MUTED, "size": 11},
        },
        "yaxis": {
            "gridcolor": GRID,
            "linecolor": AXIS,
            "zeroline": False,
            "tickfont": {"color": INK_MUTED, "size": 11},
        },
        "showlegend": False,
    }
    base.update(overrides)
    return base


def chf(value: float | None, dash: str = "—") -> str:
    if value is None:
        return dash
    return f"CHF {value:,.0f}"


def hours(value: float | None) -> str:
    """Hours the way a planner says them."""
    if value is None:
        return "—"
    if value < 0:
        return "passed"
    if value < 48:
        return f"{value:.0f} h"
    return f"{value / 24:.0f} days"
