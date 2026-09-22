"""External sources: declarative specs, one fetcher, one honest report.

    from engine.ingest.sources import load_sources, collect_all

    specs = load_sources(Path("config/sources.yaml"))
    items, reports = collect_all([s for s in specs if s.runnable], clock.as_of)

``items`` are dicts in the shape ``pipeline._to_events`` already reads, so the
deterministic funnel applies to a real GDELT article exactly as it does to the
synthetic corpus. That is the point: there is no "live mode" with different
rules, and a hindcast over recorded items goes down the identical path.
"""

from engine.ingest.sources import places
from engine.ingest.sources.catalog import CATALOG, build_gdelt_query, builtin_specs
from engine.ingest.sources.fetch import collect, collect_all, network_allowed
from engine.ingest.sources.loader import SourceConfigError
from engine.ingest.sources.loader import load as load_sources
from engine.ingest.sources.mapping import resolve, to_items
from engine.ingest.sources.spec import Auth, Cost, FieldMap, Nature, SourceSpec

__all__ = [
    "CATALOG", "Auth", "Cost", "FieldMap", "Nature", "SourceConfigError",
    "SourceSpec", "build_gdelt_query", "builtin_specs", "collect", "collect_all",
    "load_sources", "network_allowed", "places", "resolve", "to_items",
]
