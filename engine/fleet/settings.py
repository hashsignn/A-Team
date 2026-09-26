"""fleet.yaml, with a default under every key.

The file is optional (engine/config.py): a customer overlay written before the
map existed must still load. So every key the package reads has a default
here, and the defaults are the committed stand-in's values — a missing file
behaves exactly like the example, rather than like a different product.
"""

from __future__ import annotations

import copy

from engine.config import Config

DEFAULTS: dict = {
    "status": {
        "colours": {"green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"},
        "nominal_max_hours": 0.5,
        "minor_max_hours": 4.0,
        "stoppage_report_statuses": ["held", "stopped"],
        "damage_load_states": ["damaged"],
    },
    "visibility": {"staging_window_hours": 72},
    "modes": {
        "road": {"speed_kmh": 48, "detour_factor": 1.25, "chf_per_vehicle_km": 2.10,
                 "teu_per_vehicle": 2, "baseline_risk": 0.06},
        "rail": {"speed_kmh": 34, "detour_factor": 1.20, "chf_per_teu_km": 0.42,
                 "baseline_risk": 0.05},
        "barge": {"speed_kmh": 11, "detour_factor": 1.00, "chf_per_teu_km": 0.22,
                  "baseline_risk": 0.07},
        "sea": {"speed_kmh": 31, "detour_factor": 1.00, "chf_per_teu_km": 0.06,
                "baseline_risk": 0.06},
    },
    "transfer": {"hours": 6, "chf_per_teu": 160, "risk": 0.03},
    "levers": {
        "inland_to_road": "road_reroute",
        "inland_to_rail": "barge_to_rail_switch",
        "road_detour": "road_reroute",
        "sea_bypass": "sea_reroute",
        "port_swap": "sea_rebook",
    },
    "ranking": {
        "weights": {"time": 0.5, "cost": 0.3, "risk": 0.2},
        "badges": 3,
        "risk_labels": [
            {"max": 0.25, "label": "Low"},
            {"max": 0.50, "label": "Medium"},
            {"max": 1.01, "label": "High"},
        ],
        "severity_weight": {"minor": 0.35, "moderate": 0.60, "severe": 0.85},
    },
    "radar": {
        "axes": [
            {"key": "weather", "label": "Weather", "families": ["climate", "force_majeure"]},
            {"key": "geopolitics", "label": "Geopolitics", "families": ["geopolitical", "labour"]},
            {"key": "port_congestion", "label": "Port Congestion",
             "families": ["port_ops", "capacity", "cyber"]},
            {"key": "route_infrastructure", "label": "Route Infrastructure",
             "families": ["infrastructure", "waterway"]},
            {"key": "mechanical", "label": "Mechanical Status", "families": ["mechanical"]},
        ],
        "scale_hours": 24,
    },
    "matrix": {
        "probability_bands": [
            {"id": "P1", "label": "Rare", "max_p": 0.2},
            {"id": "P2", "label": "Unlikely", "max_p": 0.4},
            {"id": "P3", "label": "Possible", "max_p": 0.6},
            {"id": "P4", "label": "Likely", "max_p": 0.8},
            {"id": "P5", "label": "Almost certain", "max_p": 1.01},
        ],
        "impact_bands": [
            {"id": "I5", "label": "Severe", "min_chf": 100000},
            {"id": "I4", "label": "Major", "min_chf": 40000},
            {"id": "I3", "label": "Moderate", "min_chf": 10000},
            {"id": "I2", "label": "Minor", "min_chf": 2000},
            {"id": "I1", "label": "Negligible", "min_chf": 0},
        ],
    },
    "routing": {"osrm_url": "https://router.project-osrm.org", "timeout_s": 4},
    # Tried in order by the browser, which moves to the next when one refuses.
    # Both are keyless: there is no account to bill and no key to leak.
    #
    # NOT tile.openstreetmap.org. Those are volunteer-run servers whose usage
    # policy rules out an app like this one, and an app they have blocked is
    # answered with a picture of the words "Access blocked" — served as an
    # ordinary image, so the map could not even tell that it had failed. The
    # board showed a wall of them.
    "basemap": {
        "providers": [
            {
                "name": "CARTO Positron",
                "tiles": [
                    f"https://{host}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png"
                    for host in "abcd"
                ],
                "attribution": (
                    '&copy; <a href="https://www.openstreetmap.org/copyright">'
                    'OpenStreetMap</a> contributors &copy; '
                    '<a href="https://carto.com/attributions">CARTO</a>'
                ),
                "max_zoom": 19,
            },
            {
                "name": "Esri World Light Gray",
                "tiles": [
                    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
                    "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
                ],
                "attribution": (
                    "Tiles &copy; Esri — Esri, HERE, Garmin, &copy; OpenStreetMap "
                    "contributors, and the GIS User Community"
                ),
                "max_zoom": 16,
            },
        ],
    },
    "vendors": {
        "radius_km": {"road": 120, "rail": 150, "barge": 150, "sea": 900},
        "fallback_nearest": 3,
        "partners": [],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def basemap(block: dict | None) -> dict:
    """The basemap as an ordered list of providers, whatever shape it came in.

    ``tiles:`` at the top level is the older, single-provider form — and the
    one somebody self-hosting will write. It is honoured on its own: a
    company pointing the map at its own tile server does not want the
    browser falling back to a third party the moment that server is slow.
    A provider with no usable tile template is dropped rather than handed to
    the browser to fail on.
    """
    block = block or {}
    if block.get("tiles"):
        candidates = [{
            "name": block.get("name") or "Custom tiles",
            "tiles": block["tiles"],
            "attribution": block.get("attribution", ""),
            "max_zoom": block.get("max_zoom", 18),
        }]
    else:
        candidates = list(block.get("providers") or [])

    providers = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        tiles = entry.get("tiles")
        tiles = [tiles] if isinstance(tiles, str) else list(tiles or [])
        tiles = [t for t in tiles if isinstance(t, str) and "{z}" in t]
        if not tiles:
            continue
        providers.append({
            "name": str(entry.get("name") or "Tiles"),
            "tiles": tiles,
            "attribution": str(entry.get("attribution") or ""),
            "max_zoom": int(entry.get("max_zoom") or 18),
        })
    return {"providers": providers}


def settings(config: Config) -> dict:
    """The effective fleet settings for this config."""
    out = _merge(DEFAULTS, config.fleet)
    out["basemap"] = basemap(out.get("basemap"))
    return out
