"""The built-in sources. Every one free, keyless, and $0 to run.

WHAT IS IN HERE AND WHY
=======================
Nothing in this file costs money, and nothing in it needs a registration.
That is a hard rule, not a default: a source with a bill attached is declared
``Cost.PAID`` and is never enabled, and a source needing a key is
``FREE_WITH_KEY`` and stays off until somebody sets the variable. The demo has
to be runnable by a planner who has been handed a laptop and no budget code.

THE ONE THAT MATTERS MOST
-------------------------
``gdelt_doc``. Everything else here reports a thing that a sensor measured or
an agency declared — a magnitude, an alert level, a closed Autobahn. GDELT is
the only source that catches the sentence *"Iran announces closure of the
Strait of Hormuz to commercial shipping"* an hour after somebody writes it,
and that class of event is the reason this product exists. No API predicts it.
No threshold table sees it coming. It arrives as a sentence, or not at all.

PUSHING THE FIRST FUNNEL LAYER INTO THE QUERY
---------------------------------------------
GDELT indexes the whole world. Downloading it to filter locally would be
absurd, so the geographic and type filters run SERVER-SIDE, in the query
string, built from this deployment's own network and risk ledger
(``build_gdelt_query``). Nothing arrives that could not touch a node we own.

That is the cheapest layer of the funnel and the only one that costs zero
bytes: the items we do not want are never sent. The local deterministic layers
in ``pipeline._to_events`` still run afterwards — a query is a coarse
instrument and "Basel" matches a football club — but they run over hundreds of
items instead of millions.
"""

from __future__ import annotations

from engine.ingest.sources.spec import Auth, Cost, FieldMap, Nature, SourceSpec

# ---------------------------------------------------------------------
# GDELT query construction
# ---------------------------------------------------------------------
# Terms that make a report about FREIGHT rather than about a place. A story
# mentioning Rotterdam is not interesting; a story mentioning Rotterdam AND a
# closure is. This is the "type" half of the server-side filter.
DISRUPTION_TERMS: tuple[str, ...] = (
    "closure", "closed", "blockade", "blocked", "strike", "shutdown",
    "suspended", "halted", "congestion", "embargo", "sanctions", "curfew",
    "attack", "seizure", "detained", "rerouting", "diverted", "force majeure",
    "state of emergency", "port", "shipping", "freight", "vessel", "tanker",
)

# Chokepoints and corridors by the names newsrooms actually use. A UN/LOCODE
# never appears in a wire story, so the node id is useless as a search term —
# these are the human names, mapped back to nodes by the resolver downstream.
GEO_TERMS: tuple[str, ...] = (
    "Strait of Hormuz", "Hormuz", "Suez Canal", "Bab el-Mandeb", "Red Sea",
    "Strait of Malacca", "Panama Canal", "Gibraltar", "Bosphorus",
    "Rotterdam", "Antwerp", "Hamburg", "Le Havre", "Valencia", "Algeciras",
    "Genoa", "Felixstowe", "Shanghai", "Ningbo", "Singapore", "Jebel Ali",
    "Port Klang", "Rhine", "Duisburg", "Basel", "Gotthard", "Brenner",
)

# GDELT's query parser rejects an over-long string rather than truncating it,
# so the builder stays inside a budget it can state.
MAX_QUERY_CHARS = 1800


def build_gdelt_query(
    geo_terms: tuple[str, ...] = GEO_TERMS,
    disruption_terms: tuple[str, ...] = DISRUPTION_TERMS,
    max_chars: int = MAX_QUERY_CHARS,
) -> str:
    """``(place OR place) AND (disruption OR disruption)``, inside the limit.

    Both halves are required. Geography alone returns the shipping news; a
    disruption term alone returns every strike on earth. The AND is what makes
    the result set small enough to reason over, and it is the reason this
    source costs nothing to poll every fifteen minutes.
    """
    def group(terms: tuple[str, ...]) -> str:
        quoted = [f'"{t}"' if " " in t else t for t in terms]
        return "(" + " OR ".join(quoted) + ")"

    geo, dis = list(geo_terms), list(disruption_terms)
    while True:
        query = f"{group(tuple(geo))} AND {group(tuple(dis))}"
        if len(query) <= max_chars or len(geo) <= 4:
            return query
        geo.pop()  # drop the least central place first; the list is priority-ordered


# ---------------------------------------------------------------------
# The built-in catalogue
# ---------------------------------------------------------------------
def _autobahn(road: str) -> SourceSpec:
    """One German motorway's live closures.

    Split per road because the API is shaped that way — ``/A5/services/closure``
    — and because a planner reading the /inputs panel should see which roads
    are actually being watched rather than a single opaque "roads" row.
    """
    return SourceSpec(
        key=f"autobahn_{road.lower()}",
        label=f"Autobahn {road} — closures (BASt)",
        nature=Nature.REPORT,
        source_tier=1,
        url=f"https://verkehr.autobahn.de/o/autobahn/{road}/services/closure",
        items_path="closure",
        date_format="iso",
        families=("infrastructure",),
        modes=("road",),
        cost=Cost.FREE,
        fixture=f"autobahn_{road.lower()}.json",
        unlocks_if_connected=(
            f"Live closures on the {road}, which carries the Rhine-corridor road legs. "
            "Without it a road closure is only seen when a driver reports it."
        ),
        fields=FieldMap(
            headline="title",
            body="description[0]",
            identifier="identifier",
            lat="coordinate.lat",
            lon="coordinate.long",
            published="startTimestamp",
            starts="startTimestamp",
            source_name="const:autobahn.de",
        ),
        builtin=True,
        notes="Free, no key, no registration. Germany only.",
    )


CATALOG: tuple[SourceSpec, ...] = (
    # =================================================================
    # REPORT sources — somebody said a thing. These reach the funnel.
    # =================================================================
    SourceSpec(
        key="gdelt_doc",
        label="GDELT — global news index",
        nature=Nature.REPORT,
        source_tier=2,
        url="https://api.gdeltproject.org/api/v2/doc/doc",
        items_path="articles",
        date_format="gdelt",
        families=("geopolitical", "labour", "port_ops", "force_majeure", "capacity"),
        cost=Cost.FREE,
        fixture="gdelt_articles.json",
        params={
            "query": build_gdelt_query(),
            "mode": "artlist",
            "format": "json",
            "maxrecords": "250",
            "timespan": "3d",
            "sort": "datedesc",
        },
        unlocks_if_connected=(
            "The shock events nothing else here can see: a strait closed by "
            "announcement, a union calling a stoppage, a border shut overnight. "
            "Without it the radar only knows what a gauge can measure."
        ),
        fields=FieldMap(
            headline="title",
            body="title",
            url="url",
            published="seendate",
            starts="seendate",
            source_name="domain",
            identifier="url",
        ),
        builtin=True,
        notes=(
            "Free, no key. Updates every 15 minutes across 100+ countries. "
            "Returns headlines, not article bodies — the funnel reads the "
            "headline and the source domain, which is what tier 2 means here."
        ),
    ),
    SourceSpec(
        key="gdacs",
        label="GDACS — global disaster alerts (EU JRC)",
        nature=Nature.REPORT,
        source_tier=1,
        url="https://www.gdacs.org/gdacsapi/api/events/geteventlist/MAP",
        items_path="features",
        date_format="iso",
        families=("force_majeure", "climate"),
        cost=Cost.FREE,
        fixture="gdacs_events.json",
        unlocks_if_connected=(
            "Official flood, cyclone, quake and wildfire alerts with a computed "
            "severity, from the body the EU itself uses."
        ),
        fields=FieldMap(
            headline="properties.name",
            body="properties.htmldescription",
            identifier="properties.eventid",
            lat="geometry.coordinates[1]",
            lon="geometry.coordinates[0]",
            published="properties.fromdate",
            starts="properties.fromdate",
            ends="properties.todate",
            severity_hint="properties.alertlevel",
            url="properties.url.report",
            source_name="const:GDACS",
        ),
        builtin=True,
        notes="Free, no key. Alert level Green/Orange/Red arrives as a severity hint.",
    ),
    SourceSpec(
        key="reliefweb",
        label="ReliefWeb — situation reports (UN OCHA)",
        nature=Nature.REPORT,
        source_tier=2,
        url="https://api.reliefweb.int/v1/reports",
        items_path="data",
        date_format="iso",
        families=("force_majeure", "geopolitical"),
        cost=Cost.FREE,
        fixture="reliefweb_reports.json",
        params={
            "appname": "supply-chain-risk-radar",
            "profile": "list",
            "limit": "40",
            "sort[]": "date:desc",
        },
        unlocks_if_connected=(
            "Ground-truth situation reports where wire coverage is thin — the "
            "corroborating tier-2 account that promotes a tier-3 rumour."
        ),
        fields=FieldMap(
            headline="fields.title",
            body="fields.title",
            identifier="id",
            published="fields.date.created",
            starts="fields.date.created",
            url="fields.url",
            source_name="const:ReliefWeb",
        ),
        builtin=True,
        notes="Free, no key. 'appname' is a courtesy identifier, not a credential.",
    ),
    SourceSpec(
        key="cisa_kev",
        label="CISA KEV — actively exploited vulnerabilities",
        nature=Nature.REPORT,
        source_tier=1,
        url="https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        items_path="vulnerabilities",
        date_format="compact_date",
        families=("cyber",),
        cost=Cost.FREE,
        fixture="cisa_kev.json",
        unlocks_if_connected=(
            "The cyber family has no other source. A port community system or "
            "TMS vendor appearing here is the earliest warning available that "
            "a booking channel is about to go down."
        ),
        fields=FieldMap(
            headline="vulnerabilityName",
            body="shortDescription",
            identifier="cveID",
            published="dateAdded",
            starts="dateAdded",
            source_name="const:CISA",
        ),
        builtin=True,
        notes=(
            "Free, no key. HONEST LIMIT: this is a watchlist, not an outage "
            "feed. It says a vendor's software is being exploited, not that "
            "your terminal is down. Treated as a WATCH input, never a CRITICAL one."
        ),
    ),
    _autobahn("A5"),
    _autobahn("A61"),
    _autobahn("A3"),

    # =================================================================
    # INSTRUMENT sources — something measured a number. Never reach a model.
    # =================================================================
    SourceSpec(
        key="usgs_quakes",
        label="USGS — earthquakes M4.5+ (24h)",
        nature=Nature.INSTRUMENT,
        source_tier=1,
        url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson",
        items_path="features",
        date_format="epoch_ms",
        families=("force_majeure",),
        cost=Cost.FREE,
        fixture="usgs_quakes.json",
        unlocks_if_connected=(
            "Magnitude and epicentre within minutes. A quake near a supplier "
            "plant or a port is a damage pathway, not a delay one."
        ),
        fields=FieldMap(
            headline="properties.title",
            body="properties.place",
            identifier="id",
            lat="geometry.coordinates[1]",
            lon="geometry.coordinates[0]",
            published="properties.time",
            starts="properties.time",
            severity_hint="properties.mag",
            url="properties.url",
            source_name="const:USGS",
        ),
        builtin=True,
        notes="Free, no key. Magnitude maps to a band by table — no model needed.",
    ),
    SourceSpec(
        key="open_meteo_marine",
        label="Open-Meteo — marine wave height",
        nature=Nature.INSTRUMENT,
        source_tier=1,
        url="https://marine-api.open-meteo.com/v1/marine",
        items_path="",
        date_format="iso",
        families=("climate",),
        modes=("sea", "barge"),
        cost=Cost.FREE,
        fixture="open_meteo_marine.json",
        params={
            "latitude": "51.95",
            "longitude": "4.13",
            "hourly": "wave_height",
            "forecast_days": "5",
        },
        unlocks_if_connected=(
            "Wave height at the approach, five days out. PREDICTIVE, not a "
            "shock feed: this is the class of risk an API already forecasts."
        ),
        fields=FieldMap(
            headline="const:Marine forecast — Rotterdam approach",
            body="const:Significant wave height, hourly, five days.",
            published="current.time",
            lat="latitude",
            lon="longitude",
            source_name="const:Open-Meteo",
        ),
        builtin=True,
        notes=(
            "Free, no key, includes an ERA5 archive back to 1940 — the archive "
            "is what makes a real hindcast possible."
        ),
    ),

    # =================================================================
    # FREE, but needing a registration. OFF until somebody sets the variable.
    # =================================================================
    SourceSpec(
        key="entsoe_outages",
        label="ENTSO-E — generation and grid outages",
        nature=Nature.REPORT,
        source_tier=1,
        url="https://web-api.tp.entsoe.eu/api",
        items_path="",
        cost=Cost.FREE_WITH_KEY,
        enabled=False,
        families=("infrastructure",),
        auth=Auth(kind="query", env="ENTSOE_TOKEN", name="securityToken"),
        unlocks_if_connected=(
            "Planned and forced outages across the European grid — the earliest "
            "signal that a supplier plant is about to stop producing."
        ),
        fields=FieldMap(headline="const:ENTSO-E outage"),
        builtin=True,
        notes="Free account, no charge. Register at transparency.entsoe.eu.",
    ),
    SourceSpec(
        key="opensanctions",
        label="OpenSanctions — consolidated sanctions lists",
        nature=Nature.REPORT,
        source_tier=1,
        url="https://api.opensanctions.org/search/default",
        items_path="results",
        cost=Cost.FREE_WITH_KEY,
        enabled=False,
        families=("geopolitical",),
        auth=Auth(kind="header", env="OPENSANCTIONS_KEY", name="Authorization"),
        params={"q": "maritime shipping", "limit": "25"},
        unlocks_if_connected=(
            "EU, OFAC and UN designations in one place. A newly listed carrier "
            "or vessel operator is a booking that has to move today."
        ),
        fields=FieldMap(
            headline="caption",
            body="schema",
            identifier="id",
            source_name="const:OpenSanctions",
        ),
        builtin=True,
        notes="Free tier available; bulk data is free to download without a key.",
    ),
)


def builtin_specs() -> tuple[SourceSpec, ...]:
    return CATALOG


def by_key(key: str) -> SourceSpec | None:
    return next((s for s in CATALOG if s.key == key), None)
