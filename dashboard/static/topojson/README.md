# Vendored map geometry

World, country and coastline outlines for the map pane.

## Why these are committed

Plotly.js fetches its topojson from `cdn.plot.ly` at runtime. That means the
map — the first pane a planner looks at — silently fails to draw wherever a
CDN is unreachable: a locked-down corporate network, this build environment,
or conference wifi on the day.

BRIEF §8.7 already calls for it: **no build step, no CDN, vendored locally.**
These files are how that rule is kept for the map. `dashboard/app.py` points
Plotly at them with `topojsonURL: "/app/static/topojson/"`, served by
Streamlit's own static file handler (`server.enableStaticServing` in
`.streamlit/config.toml`).

The result is a map that renders with the network cable pulled out.

## Provenance

- Package: [`sane-topojson`](https://www.npmjs.com/package/sane-topojson) v4.0.0
- Source: npm registry
- Licence: MIT — see `LICENSE.sane-topojson`
- Derived from Natural Earth (public domain)

This is the same data Plotly's own CDN serves; only the delivery changes.

## What is here

Only the scopes the dashboard's scope selector offers, at both resolutions:

| scope | files |
|---|---|
| world | `world_110m.json`, `world_50m.json` |
| europe | `europe_110m.json`, `europe_50m.json` |
| asia | `asia_110m.json`, `asia_50m.json` |
| north america | `north-america_110m.json`, `north-america_50m.json` |

To add a scope, pull the matching file from the same package rather than
fetching it from the CDN at runtime.

## Refreshing

```bash
npm pack sane-topojson
tar -xzf sane-topojson-*.tgz
cp package/dist/{world,europe,asia,north-america}_{110,50}m.json static/topojson/
```
