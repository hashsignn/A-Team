"""PANE 1 — the map.

WHY NOT FOLIUM
==============
The earlier prototype used folium + st_folium. Rebuilding it revealed a
blocker that is not a style problem: **folium loads Leaflet from a CDN.** Under
a locked-down network policy — this build environment, a locked corporate
laptop, a conference wifi that hates you — Leaflet never arrives and the map
renders at zero height. Not a degraded map: no map. It also quietly broke the
brief's own rule (§8.7: *no build step, no CDN*).

Plotly's JS is bundled and served locally by Streamlit, and ``Scattergeo`` ships
its own land, ocean, country, coastline and river geometry. That is the same set
of Natural Earth layers the prototype was loading five shapefiles to draw — for
free, offline, with no `gpd.read_file` on every rerun.

WHAT ELSE CHANGED FROM THE PROTOTYPE
------------------------------------
* ``returned_objects=[]`` discarded every click, which made the
  click-a-dot-to-open-its-matrix interaction — the distinctive idea in the whole
  product — structurally impossible. Selection is now a native Plotly event
  carrying the event id in ``customdata``, rather than a tooltip string parsed
  back out.
* Five shapefiles were re-read on every rerun, and Streamlit reruns on every
  widget touch.
* ~200 ``DivIcon`` country and ocean labels were ~200 DOM nodes of decoration
  competing with about a dozen nodes of information.
* ``nearest_port()`` iterated a GeoDataFrame per vessel; distance now lives in
  engine/network/geo.py where it is vectorised and tested.
* ``m.save(...)`` wrote a file to disk on every rerun.
* Hardcoded vessels and a hardcoded Suez disruption are gone. Every dot here is
  produced by the pipeline, and the alternative route is computed.
* Every port in the PortWatch file was plotted. That is precisely the noise the
  product exists to remove — a planner does not need Callao on screen to decide
  about Rotterdam. Only nodes carrying freight in this book are drawn.

Layers, back to front: lanes → nodes → event dots, so a dot is never occluded
by the network it sits on.
"""

from __future__ import annotations

import plotly.graph_objects as go

from dashboard import theme
from engine.pipeline import RunContext
from engine.schemas import Mode

# Trace order matters: the events trace must be last so its point indices are
# stable and clicks resolve against a known curve.
EVENTS_TRACE_NAME = "events"


def build_map(
    context: RunContext,
    selected_event_id: str | None = None,
    show_lanes: bool = True,
    scope: str = "world",
) -> go.Figure:
    fig = go.Figure()

    if show_lanes:
        _add_lanes(fig, context)
    _add_nodes(fig, context)
    _add_selection_ring(fig, context, selected_event_id)
    _add_events(fig, context)

    geo = {
        "projection_type": "natural earth",
        "showland": True, "landcolor": "#f1f0ea",
        "showocean": True, "oceancolor": "#e8eff4",
        "showcountries": True, "countrycolor": theme.AXIS,
        "showcoastlines": True, "coastlinecolor": theme.INK_MUTED,
        "coastlinewidth": 0.6,
        "showrivers": True, "rivercolor": "#c3dceb", "riverwidth": 0.8,
        "showlakes": True, "lakecolor": "#e8eff4",
        "showframe": False,
        "bgcolor": theme.SURFACE,
        "resolution": 110,
        "scope": scope,
    }
    # Fit to the freight, not to the globe. At world scope the default view
    # wastes most of the pane on empty Pacific while the European legs — where
    # the anchor story lives — collapse into a few pixels.
    if scope == "world":
        geo["fitbounds"] = "locations"
    fig.update_geos(**geo)
    fig.update_layout(
        paper_bgcolor=theme.SURFACE,
        plot_bgcolor=theme.SURFACE,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        height=360,
        showlegend=False,
        font={"family": theme.FONT, "size": 11, "color": theme.INK_SECONDARY},
        hoverlabel={
            "bgcolor": theme.SURFACE,
            "bordercolor": theme.AXIS,
            "font": {"family": theme.FONT, "size": 12, "color": theme.INK},
        },
        dragmode="pan",
    )
    return fig


# ---------------------------------------------------------------------
def _add_lanes(fig: go.Figure, context: RunContext) -> None:
    """One trace per mode, weighted by how much freight actually uses each leg.

    Grouping by mode rather than by leg keeps the trace count at four instead of
    forty, which matters because every trace is a separate SVG group.
    """
    counts: dict[tuple[str, str, str], int] = {}
    for shipment in context.shipments:
        for leg in shipment.legs:
            key = (leg.from_node, leg.to_node, leg.mode.value)
            counts[key] = counts.get(key, 0) + 1

    by_mode: dict[str, dict] = {}
    for (from_node, to_node, mode), count in counts.items():
        try:
            geometry = context.network.geometry(from_node, to_node, Mode(mode))
        except (KeyError, ValueError):
            continue

        bucket = by_mode.setdefault(mode, {"lat": [], "lon": [], "text": []})
        label = (
            f"{context.network.node(from_node).name} → "
            f"{context.network.node(to_node).name}<br>"
            f"{mode} · {count} shipments"
        )
        for point in geometry.path:
            bucket["lat"].append(point.lat)
            bucket["lon"].append(point.lon)
            bucket["text"].append(label)
        # None breaks the line between legs without needing a separate trace.
        bucket["lat"].append(None)
        bucket["lon"].append(None)
        bucket["text"].append(None)

    for mode, data in by_mode.items():
        fig.add_trace(
            go.Scattergeo(
                lat=data["lat"], lon=data["lon"],
                mode="lines",
                line={"width": 1.6, "color": theme.MODE_COLOR.get(mode, theme.INK_MUTED)},
                opacity=0.65,
                text=data["text"],
                hoverinfo="text",
                name=mode,
            )
        )


def _add_nodes(fig: go.Figure, context: RunContext) -> None:
    used: dict[str, int] = {}
    for shipment in context.shipments:
        for node_id in shipment.node_ids:
            used[node_id] = used.get(node_id, 0) + 1

    lats, lons, texts, sizes, colors, lines = [], [], [], [], [], []
    for node_id, count in used.items():
        node = context.network.node(node_id)
        lats.append(node.lat)
        lons.append(node.lon)
        texts.append(
            f"<b>{node.name}</b><br>"
            f"{node.kind.value.replace('_', ' ')}<br>"
            f"{count} shipments"
        )
        sizes.append(6 if node.chokepoint else 8)
        # Chokepoints read as hollow: they are transited, never an origin or a
        # destination, and the difference matters when reading a lane.
        colors.append(theme.SURFACE if node.chokepoint else theme.SERIES[0])
        lines.append(theme.INK_SECONDARY if node.chokepoint else theme.SURFACE)

    fig.add_trace(
        go.Scattergeo(
            lat=lats, lon=lons, mode="markers",
            marker={
                "size": sizes,
                "color": colors,
                "line": {"width": 1.4, "color": lines},
            },
            text=texts, hoverinfo="text", name="network",
        )
    )


def _add_selection_ring(
    fig: go.Figure, context: RunContext, selected_event_id: str | None
) -> None:
    """Mark the selection with a ring, never by recolouring the marker.

    The marker's colour has to keep meaning severity — repainting it to show
    selection would make the same colour mean two different things.
    """
    if not selected_event_id:
        fig.add_trace(go.Scattergeo(lat=[], lon=[], mode="markers", name="selection"))
        return

    assessment = next(
        (a for a in context.result.assessments if a.event.event_id == selected_event_id),
        None,
    )
    if assessment is None or assessment.event.lat is None:
        fig.add_trace(go.Scattergeo(lat=[], lon=[], mode="markers", name="selection"))
        return

    fig.add_trace(
        go.Scattergeo(
            lat=[assessment.event.lat], lon=[assessment.event.lon],
            mode="markers",
            marker={
                "size": _dot_size(assessment.total_value_of_acting_chf) + 12,
                "color": "rgba(0,0,0,0)",
                "line": {"width": 2, "color": theme.INK},
            },
            hoverinfo="skip", name="selection",
        )
    )


def _add_events(fig: go.Figure, context: RunContext) -> None:
    """Event dots — severity by colour AND shape, value of acting by size.

    Every GATED event gets a dot. Severity controls colour and priority order,
    not whether the click responds: a green dot whose matrix says "yes this
    touches you, but only two shipments and both have four days of buffer" is
    exactly the reassurance that lets a planner stop worrying.

    Events touching nothing get no dot at all. That is the noise filter, and it
    sits upstream of severity.

    Symbol carries severity alongside colour, because on a light surface the
    warning and serious steps sit below 3:1 contrast — colour alone must never
    be the only channel.
    """
    symbols = {"severe": "circle", "moderate": "diamond", "minor": "triangle-up"}

    lats, lons, sizes, colors, texts, custom, marks = [], [], [], [], [], [], []
    for assessment in context.result.assessments:
        event = assessment.event
        if event.lat is None or event.lon is None:
            continue

        severity = event.severity.value
        p = f"{event.probability:.0%}" if event.probability_known else "unsourced"

        lats.append(event.lat)
        lons.append(event.lon)
        sizes.append(_dot_size(assessment.total_value_of_acting_chf))
        colors.append(theme.SEVERITY_COLOR[severity])
        marks.append(symbols.get(severity, "circle"))
        custom.append(event.event_id)
        texts.append(
            f"<b>{event.title}</b><br>"
            f"{severity} · P {p}<br>"
            f"{assessment.shipments_affected} shipments · "
            f"{len(assessment.contracts_affected)} contracts<br>"
            f"At risk {theme.chf(assessment.total_value_at_risk_chf)}<br>"
            f"<b>Recoverable {theme.chf(assessment.total_value_of_acting_chf)}</b>"
            f"<br><i>click to open this event's matrix</i>"
        )

    fig.add_trace(
        go.Scattergeo(
            lat=lats, lon=lons, mode="markers",
            marker={
                "size": sizes,
                "color": colors,
                "symbol": marks,
                # A 2px surface ring keeps overlapping dots readable.
                "line": {"width": 2, "color": theme.SURFACE},
                "opacity": 0.92,
            },
            text=texts, hoverinfo="text",
            customdata=custom,
            name=EVENTS_TRACE_NAME,
        )
    )


def _dot_size(recoverable_chf: float) -> float:
    """Marker size by value of acting — what you can still save.

    BRIEF §5.6: rank by what you can still save, not by expected loss and not
    by severity. A catastrophic event you can do nothing about ranks below a
    moderate one you can still fix, and the map should say so at a glance.
    """
    return 11 + min(20.0, (max(0.0, recoverable_chf) / 20_000.0) ** 0.6 * 6)


def event_id_from_selection(selection, context: RunContext) -> str | None:
    """Resolve a Plotly selection event back to an event id.

    Deliberately tolerant: a click landing on a lane or a node returns None
    rather than raising, and the selectbox beside the map stays the reliable
    path. A demo must never depend on a click landing.
    """
    if not selection:
        return None
    points = (selection.get("selection") or {}).get("points") or []
    valid = {a.event.event_id for a in context.result.assessments}
    for point in points:
        candidate = point.get("customdata")
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else None
        if candidate in valid:
            return candidate
    return None
