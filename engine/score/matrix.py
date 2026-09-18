"""Band placement for the per-event risk matrix (BRIEF §5.7).

The matrix is PER EVENT and hidden until that event's dot is clicked. There is
no dashboard-level matrix — BRIEF §12 is explicit, and the reasoning holds: a
global P×I scatter of everything aggregates away the one thing a planner needs,
which is *which of my shipments*.

    The event is the question; the shipments are the answer.

The points inside the cells are shipments, not events. A planner clicks a
strike and sees which six of their two hundred shipments it touches and how
badly each is exposed.

THE UNSOURCED BAND
------------------
The x-axis is P(shipment is late), 0→1. An event whose probability genuinely
cannot be sourced — a union ballot, most strikes — has no x. The brief mandates
``probability_unknown`` (§5.3) and forbids absences becoming values (§8.1), but
its matrix spec leaves such an event nowhere to go, and whoever implements the
grid will quietly place it at 0.5. That is the sibling project's exact bug
reappearing in the UI layer.

So unsourced events are drawn in a separate band OUTSIDE the probability axis.
The planner sees "this could be bad, we cannot price the odds", which is the
truth.
"""

from __future__ import annotations

from engine.config import Config

UNSOURCED_BAND_ID = "P0"


def impact_band(loss_chf: float, config: Config) -> tuple[str, str, str]:
    """(band_id, label, quadrant action) for a CHF exposure."""
    bands = config.scoring["matrix"]["impact_bands"]
    for band in bands:  # declared most severe first
        if loss_chf >= band["min_chf"]:
            return band["id"], band["label"], band["action"]
    last = bands[-1]
    return last["id"], last["label"], last["action"]


def probability_band(p_late: float | None, config: Config) -> tuple[str, str]:
    """(band_id, label). ``None`` goes to the unsourced band, never to 0.5."""
    if p_late is None:
        label = config.scoring["matrix"].get("unsourced_band_label", "P unsourced")
        return UNSOURCED_BAND_ID, label

    for band in config.scoring["matrix"]["probability_bands"]:
        if p_late < band["max_p"]:
            return band["id"], band["label"]
    last = config.scoring["matrix"]["probability_bands"][-1]
    return last["id"], last["label"]


def cell_action(impact_band_id: str, config: Config) -> str:
    for band in config.scoring["matrix"]["impact_bands"]:
        if band["id"] == impact_band_id:
            return band["action"]
    return "MONITOR"


def band_grid(config: Config) -> dict:
    """The grid definition the dashboard renders.

    Returned as data rather than drawn here, so the same band logic serves the
    UI, the CSV export and the tests without three copies drifting apart.
    """
    matrix = config.scoring["matrix"]
    return {
        "impact_bands": matrix["impact_bands"],
        "probability_bands": matrix["probability_bands"],
        "unsourced_band": {
            "id": UNSOURCED_BAND_ID,
            "label": matrix.get("unsourced_band_label", "P unsourced"),
        },
    }


def ring_style(actionability: str) -> str:
    """Lead time as a visual overlay, not a third axis (BRIEF §5.7).

    Solid = still actionable, hollow = too late. Four dimensions on one
    readable chart; a third spatial axis would destroy it.
    """
    return "solid" if actionability in ("comfortable", "tightening") else "hollow"
