/* The fleet map — a renderer of MapStore, and nothing else.
 *
 * It owns no domain state. Everything it draws comes from the store, and
 * every click becomes a MapAgent call: a marker click is
 * `MapAgent.selectAsset(id)`, hovering a recovery route is
 * `MapAgent.highlightRoute(id)`. An agent calling those functions produces
 * exactly the same map.
 *
 * BASEMAP
 * -------
 * Raster tiles from a list of keyless providers (fleet.yaml), tried in
 * order: one that answers nothing but errors is replaced by the next.
 * Desaturated in the renderer (MapLibre's raster paint properties) so the
 * assets and routes are the only saturated things on screen; inverted on
 * the dark theme. UNDER them, always, the vendored country outlines — so
 * with the network cable pulled out the map still has a world on it, and
 * the legend says which one you are looking at.
 *
 * Not OpenStreetMap's own tile servers: they refuse apps like this one, and
 * refuse them with an image of the words "Access blocked" delivered as a
 * normal tile, which nothing here could tell from a map.
 *
 * NO TEXT IN WEBGL
 * ----------------
 * A symbol layer with text needs glyph PBFs from a font server, which is a
 * network dependency for every label. So every piece of text — cluster
 * counts, rank badges, branch labels — is a DOM marker. Icons are SVG,
 * rasterised once at 2x.
 */
'use strict';

(function (root) {
  const $ = (id) => document.getElementById(id);
  const tokenOf = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const STATUSES = ['green', 'yellow', 'red'];
  const MODES = ['sea', 'barge', 'rail', 'road'];
  const CLUSTER_MAX_ZOOM = 6;
  // Errors from a provider, with no tile loaded yet, before the next is
  // tried. More than one, because a single miss is routine; few enough that
  // a provider refusing everything is replaced within the first screenful.
  const BASEMAP_SWITCH_AFTER = 4;
  const HOME = { center: [22, 34], zoom: 2.1 };

  /* Mode glyphs in a 24x24 box, drawn white on the status disc. Drawn, not
   * emoji: an emoji renders differently on every platform and at whatever
   * size the browser picks, and these have to read at 22 pixels. */
  const GLYPH = {
    sea: '<path d="M2.5 14.5h19l-2.6 5.2H5.1z"/><rect x="5" y="10" width="3.6" height="3.6" rx=".4"/><rect x="9.2" y="10" width="3.6" height="3.6" rx=".4"/><rect x="9.2" y="5.8" width="3.6" height="3.6" rx=".4"/><path d="M15 6.5h3.4v7.3H15z"/>',
    barge: '<path d="M1.5 15h21l-2 4H3.5z"/><rect x="3.5" y="10.4" width="4.6" height="4" rx=".4"/><rect x="8.8" y="10.4" width="4.6" height="4" rx=".4"/><rect x="14.1" y="10.4" width="4.6" height="4" rx=".4"/><path d="M19.4 8.6h2v5.8h-2z"/>',
    rail: '<rect x="5" y="3" width="14" height="13.5" rx="3.2"/><rect x="7.3" y="5.3" width="9.4" height="4.4" rx="1" fill="COL"/><circle cx="8.6" cy="13" r="1.25" fill="COL"/><circle cx="15.4" cy="13" r="1.25" fill="COL"/><path d="M7.5 17.5l-2.5 3.3h2.2l1.8-2.3h6l1.8 2.3H19l-2.5-3.3z"/>',
    road: '<path d="M1.8 6.2h12v10H1.8z"/><path d="M14.6 9.2h4.3l3.3 3.6v3.4h-7.6z"/><circle cx="6" cy="17.6" r="2.1"/><circle cx="17.5" cy="17.6" r="2.1"/>',
  };
  const CHANNEL = {
    phone: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1z"/></svg>',
    email: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5l8.5 6.5 8.5-6.5"/></svg>',
    portal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.6 3.8 5.6 3.8 9s-1.2 6.4-3.8 9c-2.6-2.6-3.8-5.6-3.8-9S9.4 5.6 12 3z"/></svg>',
  };

  function assetSvg(mode, colour) {
    const glyph = (GLYPH[mode] || GLYPH.road).replace(/COL/g, colour);
    return `<svg xmlns="http://www.w3.org/2000/svg" width="44" height="44" viewBox="0 0 44 44">
      <circle cx="22" cy="22" r="19.5" fill="${colour}" stroke="#ffffff" stroke-width="3"/>
      <g transform="translate(10.5 10.5) scale(0.96)" fill="#ffffff">${glyph}</g></svg>`;
  }

  function legendIcon(mode) {
    return `<svg viewBox="0 0 44 44">${assetSvg(mode, tokenOf('--muted'))
      .replace(/^[\s\S]*?<circle/, '<circle').replace(/<\/svg>\s*$/, '')}</svg>`;
  }

  function loadImage(svg) {
    return new Promise((resolve, reject) => {
      const img = new Image(44, 44);
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    });
  }

  function circlePolygon(lat, lon, km, steps = 72) {
    const coords = [];
    const R = 6371.0088;
    const d = km / R;
    const la = (lat * Math.PI) / 180;
    const lo = (lon * Math.PI) / 180;
    for (let i = 0; i <= steps; i++) {
      const b = (2 * Math.PI * i) / steps;
      const lat2 = Math.asin(Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(b));
      const lon2 = lo + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la),
        Math.cos(d) - Math.sin(la) * Math.sin(lat2));
      coords.push([(lon2 * 180) / Math.PI, (lat2 * 180) / Math.PI]);
    }
    return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [coords] }, properties: {} };
  }

  /* [[lat, lon], …] (the wire format) → a GeoJSON LineString. Longitudes are
   * unwrapped so a line crossing the antimeridian goes the short way. */
  function line(latlon, props) {
    const coords = [];
    let prev = null;
    for (const [lat, lon] of latlon || []) {
      let x = lon;
      if (prev !== null) {
        while (x - prev > 180) x -= 360;
        while (x - prev < -180) x += 360;
      }
      coords.push([x, lat]);
      prev = x;
    }
    return { type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: props || {} };
  }
  const fc = (features) => ({ type: 'FeatureCollection', features });

  /* Russia and Fiji cross the antimeridian, and in the world-atlas geometry
   * their rings jump from +180 to -180. A globe does not care; a flat map
   * draws that jump as a band across the whole world. Making each ring's
   * longitudes continuous (Chukotka at 190° rather than -170°) fixes it, and
   * MapLibre's world copies draw the overhang where it belongs. */
  function unwrapAntimeridian(collection) {
    const ring = (coords) => {
      let prev = null;
      return coords.map(([lon, lat]) => {
        let x = lon;
        if (prev !== null) {
          while (x - prev > 180) x -= 360;
          while (x - prev < -180) x += 360;
        }
        prev = x;
        return [x, lat];
      });
    };
    for (const f of collection.features) {
      const g = f.geometry;
      if (!g) continue;
      if (g.type === 'Polygon') g.coordinates = g.coordinates.map(ring);
      else if (g.type === 'MultiPolygon') g.coordinates = g.coordinates.map((poly) => poly.map(ring));
    }
    return collection;
  }

  function pointAlong(latlon, fraction) {
    if (!latlon || !latlon.length) return null;
    if (latlon.length === 1) return latlon[0];
    const seg = [];
    let total = 0;
    for (let i = 1; i < latlon.length; i++) {
      const [a, b] = [latlon[i - 1], latlon[i]];
      const d = Math.hypot(b[0] - a[0], (b[1] - a[1]) * Math.cos((a[0] * Math.PI) / 180));
      seg.push(d); total += d;
    }
    let target = total * fraction;
    for (let i = 0; i < seg.length; i++) {
      if (target <= seg[i] && seg[i] > 0) {
        const t = target / seg[i];
        const [a, b] = [latlon[i], latlon[i + 1]];
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
      }
      target -= seg[i];
    }
    return latlon[latlon.length - 1];
  }

  // =================================================================
  function createMapView({ store, agent, select }) {
    const el = $('fleetmap');
    const wrap = el.closest('.globe-wrap');
    const view = {
      map: null, ready: false, prev: null,
      clusters: new Map(), routeMarkers: [], vendorMarkers: new Map(), branchMarkers: [],
      hazard: null, fittedFor: null,
      // Which basemap provider is drawing, and how it is going. `refused`
      // names the ones given up on, for the legend.
      tiles: { index: -1, errors: 0, ok: false, offline: false, refused: [], meta: null },
      hoverAsset: null,
    };

    function altColour(rank) {
      const n = Math.max(1, Math.min(4, rank || 4));
      return tokenOf(`--alt-${n}`);
    }
    const statusColour = (s) => tokenOf(`--status-${s}`);

    // ---------------------------------------------------------------
    // View switching — map or globe in the left pane
    // ---------------------------------------------------------------
    function applyView(v) {
      wrap.classList.toggle('is-map', v === 'map');
      wrap.classList.toggle('is-globe', v === 'globe');
      document.querySelectorAll('.viewswitch button').forEach((b) =>
        b.classList.toggle('is-on', b.dataset.view === v));
      if (root.RadarGlobe) {
        if (v === 'map') root.RadarGlobe.pause(); else root.RadarGlobe.resume();
      }
      if (v === 'map' && view.map) requestAnimationFrame(() => view.map.resize());
      try {
        const q = new URLSearchParams(location.search);
        if (v === 'globe') q.set('view', 'globe'); else q.delete('view');
        const url = q.toString() ? `${location.pathname}?${q}` : location.pathname;
        history.replaceState(null, '', url);
      } catch { /* not fatal */ }
    }

    // ---------------------------------------------------------------
    // Map construction
    // ---------------------------------------------------------------
    function rasterPaint() {
      return {
        'raster-saturation': -1,
        'raster-contrast': parseFloat(tokenOf('--map-raster-contrast')) || 0,
        'raster-brightness-min': parseFloat(tokenOf('--map-raster-min')) || 0,
        'raster-brightness-max': parseFloat(tokenOf('--map-raster-max')) || 1,
        'raster-opacity': parseFloat(tokenOf('--map-raster-opacity')) || 1,
      };
    }

    function build() {
      if (!root.maplibregl) {
        el.innerHTML = '<p class="map-busy">The map library did not load.</p>';
        return;
      }
      let map;
      try {
        map = new root.maplibregl.Map({
          container: el,
          style: {
            version: 8,
            sources: {},
            layers: [{ id: 'bg', type: 'background', paint: { 'background-color': tokenOf('--map-sea') } }],
          },
          center: HOME.center, zoom: HOME.zoom, minZoom: 1.1, maxZoom: 16,
          attributionControl: false, pitchWithRotate: false, touchPitch: false,
          dragRotate: true, renderWorldCopies: true,
        });
      } catch (err) {
        el.innerHTML = `<p class="map-busy">The map could not start (WebGL unavailable): ${esc(err.message)}</p>`;
        return;
      }
      view.map = map;
      root.__fleetmap = map;   // for the headless check; nothing reads it in-app

      map.addControl(new root.maplibregl.NavigationControl({ visualizePitch: false }), 'top-right');
      map.addControl(new ResetControl(), 'top-right');
      map.addControl(new root.maplibregl.ScaleControl({ maxWidth: 110 }), 'bottom-right');
      map.addControl(new root.maplibregl.AttributionControl({ compact: true }), 'bottom-right');

      // Tile failures are expected offline. They are reported in the legend,
      // not thrown at the console as if something were broken.
      map.on('error', (e) => {
        if (e && typeof e.sourceId === 'string' && e.sourceId.startsWith('basemap-')) {
          onTileError(e.sourceId);
        } else if (e && e.error) {
          console.warn('[map]', e.error.message || e.error);
        }
      });
      map.on('sourcedata', (e) => {
        if (e.sourceId === basemapSource(view.tiles.index) && e.tile && !view.tiles.ok) {
          view.tiles.ok = true;
          renderLegend(store.getState());
        }
      });

      map.on('load', async () => {
        await addWorld(map);
        await addIcons(map);
        addDataLayers(map);
        bindInteractions(map);
        view.ready = true;
        render(store.getState(), null, { force: true });
      });
      map.on('render', () => { if (view.ready) syncClusters(); });
    }

    class ResetControl {
      onAdd(map) {
        this._map = map;
        const c = document.createElement('div');
        c.className = 'maplibregl-ctrl maplibregl-ctrl-group';
        c.innerHTML = '<button type="button" class="map-reset-btn" title="Reset view" aria-label="Reset view">⌂</button>';
        c.querySelector('button').addEventListener('click', () => resetView());
        this._c = c;
        return c;
      }
      onRemove() { this._c.remove(); }
    }

    /* Home: north up, flat, and every visible asset in frame. */
    function resetView() {
      const map = view.map;
      if (!map) return;
      const assets = select.visibleAssets(store.getState());
      if (!assets.length) {
        map.easeTo({ ...HOME, bearing: 0, pitch: 0, duration: 700 });
        return;
      }
      const b = new root.maplibregl.LngLatBounds();
      assets.forEach((a) => b.extend([a.lon, a.lat]));
      map.fitBounds(b, { padding: 70, maxZoom: 6, bearing: 0, pitch: 0, duration: 800 });
    }

    async function addWorld(map) {
      try {
        const topo = await fetch('/geo/countries-110m.json').then((r) => r.json());
        const land = unwrapAntimeridian(root.topojson.feature(topo, topo.objects.countries));
        map.addSource('land', { type: 'geojson', data: land });
        map.addLayer({ id: 'land', type: 'fill', source: 'land', paint: { 'fill-color': tokenOf('--map-land') } });
        map.addLayer({ id: 'land-line', type: 'line', source: 'land',
          paint: { 'line-color': tokenOf('--map-border'), 'line-width': 0.6 } });
      } catch (err) {
        console.warn('[map] country outlines unavailable', err);
      }
    }

    // ---------------------------------------------------------------
    // Basemap — the first provider that answers
    // ---------------------------------------------------------------
    function providersOf(meta) {
      const b = meta && meta.basemap;
      if (!b) return [];
      if (Array.isArray(b.providers)) return b.providers;
      return b.tiles ? [{ name: 'Tiles', ...b }] : [];     // an older server's shape
    }

    /* One source id per provider, so a replaced provider's late errors —
     * its requests are still in flight when it is removed — are recognised
     * as its own and never counted against the one that replaced it. */
    const basemapSource = (index) => `basemap-${index}`;

    function ensureBasemap(meta) {
      const t = view.tiles;
      if (!view.ready || !meta || t.index >= 0 || t.offline) return;
      t.meta = meta;
      useProvider(0);
    }

    function useProvider(index) {
      const map = view.map;
      const t = view.tiles;
      const list = providersOf(t.meta);
      if (t.index >= 0) {
        if (list[t.index]) t.refused.push(list[t.index].name);
        if (map.getLayer('basemap')) map.removeLayer('basemap');
        if (map.getSource(basemapSource(t.index))) map.removeSource(basemapSource(t.index));
      }
      Object.assign(t, { index, errors: 0, ok: false });
      const provider = list[index];
      if (!provider) {
        t.offline = true;               // the vendored outlines stay underneath
        renderLegend(store.getState());
        return;
      }
      map.addSource(basemapSource(index), {
        type: 'raster', tiles: provider.tiles, tileSize: 256,
        maxzoom: provider.max_zoom || 18, attribution: provider.attribution,
      });
      map.addLayer({ id: 'basemap', type: 'raster', source: basemapSource(index), paint: rasterPaint() }, 'lanes');
      renderLegend(store.getState());
    }

    function onTileError(sourceId) {
      const t = view.tiles;
      // A provider that has drawn a tile keeps its place: a missing tile
      // here and there is not a refusal.
      if (sourceId !== basemapSource(t.index) || t.ok || t.offline) return;
      t.errors += 1;
      if (t.errors >= BASEMAP_SWITCH_AFTER) useProvider(t.index + 1);
    }

    async function addIcons(map) {
      const jobs = [];
      for (const mode of MODES) {
        for (const s of STATUSES) {
          const name = `asset-${mode}-${s}`;
          jobs.push(loadImage(assetSvg(mode, statusColour(s))).then((img) => {
            if (map.hasImage(name)) map.removeImage(name);
            map.addImage(name, img, { pixelRatio: 2 });
          }));
        }
      }
      await Promise.all(jobs);
    }

    function addDataLayers(map) {
      const empty = fc([]);
      map.addSource('lanes', { type: 'geojson', data: empty });
      map.addSource('radius', { type: 'geojson', data: empty });
      map.addSource('original', { type: 'geojson', data: empty });
      map.addSource('alts', { type: 'geojson', data: empty });
      map.addSource('split', { type: 'geojson', data: empty });
      map.addSource('assets', {
        type: 'geojson', data: empty, cluster: true,
        clusterRadius: 46, clusterMaxZoom: CLUSTER_MAX_ZOOM,
        clusterProperties: {
          red: ['+', ['case', ['==', ['get', 'status'], 'red'], 1, 0]],
          yellow: ['+', ['case', ['==', ['get', 'status'], 'yellow'], 1, 0]],
          green: ['+', ['case', ['==', ['get', 'status'], 'green'], 1, 0]],
        },
      });

      map.addLayer({ id: 'lanes', type: 'line', source: 'lanes',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': tokenOf('--map-lane'), 'line-width': 1.1, 'line-opacity': 0.35 } });
      map.addLayer({ id: 'radius-fill', type: 'fill', source: 'radius',
        paint: { 'fill-color': tokenOf('--accent'), 'fill-opacity': 0.05 } });
      map.addLayer({ id: 'radius-line', type: 'line', source: 'radius',
        paint: { 'line-color': tokenOf('--accent'), 'line-width': 1.2, 'line-opacity': 0.55, 'line-dasharray': [3, 3] } });
      // The original route: faded, dotted, grey — the plan the disruption broke.
      map.addLayer({ id: 'route-original', type: 'line', source: 'original',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': tokenOf('--map-original'), 'line-width': 3, 'line-opacity': 0.75,
          'line-dasharray': [0.2, 2.2] } });
      // Road and rail alternatives often share a corridor. Offsetting each
      // rank sideways keeps #2 visible beside #1 instead of hidden under it.
      const offset = ['match', ['get', 'rank'], 1, 0, 2, 6, 3, -6, 4, 12, -12];
      map.addLayer({ id: 'alt-casing', type: 'line', source: 'alts',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': tokenOf('--map-casing'), 'line-width': 7, 'line-opacity': 0.85,
          'line-offset': offset } });
      map.addLayer({ id: 'alt-line', type: 'line', source: 'alts',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': ['get', 'colour'], 'line-width': 4, 'line-opacity': 1,
          'line-offset': offset } });
      map.addLayer({ id: 'alt-hit', type: 'line', source: 'alts',
        paint: { 'line-color': '#000', 'line-width': 18, 'line-opacity': 0, 'line-offset': offset } });
      map.addLayer({ id: 'split-casing', type: 'line', source: 'split',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': tokenOf('--map-casing'), 'line-width': 9 } });
      map.addLayer({ id: 'split-line', type: 'line', source: 'split',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': ['get', 'colour'], 'line-width': 5,
          'line-dasharray': ['case', ['get', 'stays'], ['literal', [2, 1.4]], ['literal', [1, 0]]] } });
      map.addLayer({ id: 'asset-halo', type: 'circle', source: 'assets',
        filter: ['all', ['!', ['has', 'point_count']], ['==', ['get', 'selected'], true]],
        paint: { 'circle-radius': 21, 'circle-color': tokenOf('--accent'), 'circle-opacity': 0.18,
          'circle-stroke-color': tokenOf('--accent'), 'circle-stroke-width': 2.5 } });
      map.addLayer({ id: 'asset-icons', type: 'symbol', source: 'assets',
        filter: ['!', ['has', 'point_count']],
        layout: {
          'icon-image': ['get', 'icon'],
          'icon-size': ['case', ['==', ['get', 'selected'], true], 1.3, 1],
          'icon-allow-overlap': true, 'icon-ignore-placement': true,
          'symbol-sort-key': ['get', 'sort'],
        } });
      // Invisible, for clicks on clusters that land between DOM markers.
      map.addLayer({ id: 'clusters-hit', type: 'circle', source: 'assets',
        filter: ['has', 'point_count'], paint: { 'circle-radius': 20, 'circle-opacity': 0 } });
    }

    // ---------------------------------------------------------------
    // Interaction — every one of these is a MapAgent call
    // ---------------------------------------------------------------
    function bindInteractions(map) {
      const tip = $('map-tip');
      const place = (e) => {
        const r = el.getBoundingClientRect();
        tip.style.left = `${Math.min(e.point.x + 14, r.width - 310)}px`;
        tip.style.top = `${Math.min(e.point.y + 14, r.height - 90)}px`;
      };

      map.on('click', 'asset-icons', (e) => {
        const f = e.features && e.features[0];
        if (f) agent.selectAsset(f.properties.id);
      });
      map.on('mouseenter', 'asset-icons', () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mousemove', 'asset-icons', (e) => {
        const f = e.features && e.features[0];
        if (!f) return;
        const a = store.getState().assets.byId[f.properties.id];
        if (!a) return;
        tip.innerHTML = `<div class="tip-k">${esc(a.status_label)} · ${esc(a.mode)}</div>
          <b>${esc(a.id)} · ${esc(a.name)}</b>
          <div class="muted">${esc(a.leg)}</div>
          <div style="margin-top:4px">${esc(a.reason)}</div>`;
        tip.hidden = false; place(e);
      });
      map.on('mouseleave', 'asset-icons', () => { map.getCanvas().style.cursor = ''; tip.hidden = true; });
      map.on('click', 'clusters-hit', (e) => {
        const f = e.features && e.features[0];
        if (f) expandCluster(f.properties.cluster_id, f.geometry.coordinates);
      });

      /* Hovering an alternate: the delta, in the planner's words. */
      map.on('mousemove', 'alt-hit', (e) => {
        const f = e.features && e.features[0];
        if (!f) return;
        const id = f.properties.id;
        if (store.getState().routing.hovered !== id) agent.highlightRoute(id);
        const c = select.routeById(store.getState(), id);
        if (!c) return;
        tip.innerHTML = `<div class="tip-k">${esc(c.badge || `#${c.rank}`)} · ${esc(c.kind.replace(/_/g, ' '))}</div>
          <b>${esc(c.label)}</b>
          <div class="tip-delta">${esc(c.delta.text)}</div>
          <div class="muted">vs the original route · score ${c.score.toFixed(2)}</div>`;
        tip.hidden = false; place(e);
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'alt-hit', () => {
        agent.highlightRoute(null);
        tip.hidden = true;
        map.getCanvas().style.cursor = '';
      });
      map.on('click', 'alt-hit', (e) => {
        const f = e.features && e.features[0];
        if (f) agent.chooseRoute(f.properties.id);
      });
    }

    async function expandCluster(clusterId, coords) {
      const src = view.map.getSource('assets');
      try {
        const zoom = await src.getClusterExpansionZoom(clusterId);
        view.map.easeTo({ center: coords, zoom: Math.min(zoom + 0.2, 12), duration: 500 });
      } catch { /* cluster vanished mid-zoom */ }
    }

    // ---------------------------------------------------------------
    // Clusters as DOM donuts: count, and what the count is made of
    // ---------------------------------------------------------------
    function donut(p) {
      const total = p.point_count;
      const r = total >= 50 ? 24 : total >= 12 ? 20 : 17;
      const w = 6;
      const C = 2 * Math.PI * (r - w / 2);
      let offset = 0;
      const arcs = ['red', 'yellow', 'green'].map((s) => {
        const n = p[s] || 0;
        if (!n) return '';
        const len = (n / total) * C;
        const arc = `<circle r="${r - w / 2}" cx="${r}" cy="${r}" fill="none" stroke="${statusColour(s)}"
          stroke-width="${w}" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-offset}"
          transform="rotate(-90 ${r} ${r})"/>`;
        offset += len;
        return arc;
      }).join('');
      return `<svg width="${2 * r}" height="${2 * r}" viewBox="0 0 ${2 * r} ${2 * r}">
        <circle r="${r - w}" cx="${r}" cy="${r}" fill="${tokenOf('--surface-1')}"/>
        ${arcs}
        <text x="${r}" y="${r}" text-anchor="middle" dominant-baseline="central">${total}</text></svg>`;
    }

    function syncClusters() {
      const map = view.map;
      if (!map.getSource('assets') || !map.isSourceLoaded('assets')) return;
      const seen = new Set();
      for (const f of map.querySourceFeatures('assets')) {
        const p = f.properties;
        if (!p.cluster) continue;
        const id = p.cluster_id;
        if (seen.has(id)) continue;
        seen.add(id);
        let m = view.clusters.get(id);
        const key = `${p.point_count}|${p.red}|${p.yellow}|${p.green}`;
        if (!m) {
          const node = document.createElement('div');
          node.className = 'mcluster';
          node.title = `${p.point_count} assets — ${p.red || 0} red, ${p.yellow || 0} yellow, ${p.green || 0} green. Click to expand.`;
          node.addEventListener('click', (ev) => {
            ev.stopPropagation();
            expandCluster(id, f.geometry.coordinates);
          });
          m = new root.maplibregl.Marker({ element: node }).setLngLat(f.geometry.coordinates);
          m._key = '';
          view.clusters.set(id, m);
        }
        if (m._key !== key) {
          m.getElement().innerHTML = donut(p);
          m._key = key;
        }
        m.setLngLat(f.geometry.coordinates);
        if (!m._added) { m.addTo(map); m._added = true; }
      }
      for (const [id, m] of view.clusters) {
        if (!seen.has(id)) { m.remove(); view.clusters.delete(id); }
      }
    }

    // ---------------------------------------------------------------
    // Render — state in, map out
    // ---------------------------------------------------------------
    function render(state, prev, opts) {
      const force = opts && opts.force;
      if (!prev || force || state.view !== prev.view) applyView(state.view);
      renderLegend(state);
      if (!view.ready) return;
      const map = view.map;

      if (force || !prev || state.assets.meta !== prev.assets.meta) ensureBasemap(state.assets.meta);

      if (force || !prev || state.assets.lanes !== prev.assets.lanes || state.focusLane !== prev.focusLane) {
        map.getSource('lanes').setData(fc(state.assets.lanes.map((l) =>
          line(l.path, { id: l.route_id, focus: l.route_id === state.focusLane }))));
        map.setPaintProperty('lanes', 'line-width', ['case', ['get', 'focus'], 3.2, 1.1]);
        map.setPaintProperty('lanes', 'line-opacity', ['case', ['get', 'focus'], 0.85, 0.35]);
      }

      if (force || !prev || state.assets.items !== prev.assets.items
          || state.filters !== prev.filters || state.selection.id !== prev.selection.id) {
        const sort = { green: 1, yellow: 2, red: 3 };
        map.getSource('assets').setData(fc(select.visibleAssets(state).map((a) => ({
          type: 'Feature',
          geometry: { type: 'Point', coordinates: [a.lon, a.lat] },
          properties: {
            id: a.id, status: a.status, mode: a.mode,
            icon: `asset-${MODES.includes(a.mode) ? a.mode : 'road'}-${a.status}`,
            selected: a.id === state.selection.id,
            sort: (a.id === state.selection.id ? 10 : 0) + sort[a.status],
          },
        }))));
      }

      if (force || !prev || state.selection.id !== prev.selection.id) {
        const a = select.selectedAsset(state);
        if (a) ensureVisible(a);
        view.fittedFor = null;
      }

      const routeData = !prev || state.routing.data !== prev.routing.data
        || state.selection.id !== prev.selection.id || state.split !== prev.split;
      if (force || routeData) renderRoutes(state);
      else if (state.routing.hovered !== prev.routing.hovered
               || state.routing.chosen !== prev.routing.chosen) emphasise(state);
      if (force || !prev || state.vendors !== prev.vendors || state.routing.data !== (prev && prev.routing.data)) renderVendors(state);
    }

    /* Only move the camera if the asset cannot be seen: off screen, or
     * folded into a cluster. A planner who clicked a dot is already looking
     * at it, and yanking the view away throws the context they clicked in. */
    function ensureVisible(a) {
      const map = view.map;
      const inView = map.getBounds().contains([a.lon, a.lat]);
      const drawn = inView && map.queryRenderedFeatures({ layers: ['asset-icons'] })
        .some((f) => f.properties.id === a.id);
      if (drawn) return;
      map.easeTo({ center: [a.lon, a.lat], zoom: Math.max(map.getZoom(), CLUSTER_MAX_ZOOM + 0.6), duration: 650 });
    }

    function clearMarkers(list) {
      list.forEach((m) => m.remove());
      list.length = 0;
    }

    function renderRoutes(state) {
      const map = view.map;
      const data = state.routing.data;
      const splitOn = state.split.enabled && state.split.data;
      clearMarkers(view.routeMarkers);
      clearMarkers(view.branchMarkers);
      if (view.hazard) { view.hazard.remove(); view.hazard = null; }

      if (!data || !state.selection.id || data.shipment_id !== state.selection.id) {
        map.getSource('original').setData(fc([]));
        map.getSource('alts').setData(fc([]));
        map.getSource('split').setData(fc([]));
        return;
      }

      const eligible = data.eligible && data.candidates.length;
      map.getSource('original').setData(fc(eligible || data.status.level !== 'green'
        ? [line(data.original.path, { id: 'ORIGINAL' })] : []));

      const alts = data.candidates.map((c) => line(c.path, {
        id: c.id, rank: c.rank, colour: altColour(c.rank),
      }));
      // Draw the lowest rank on top: #1 must never be hidden under #3.
      alts.sort((x, y) => y.properties.rank - x.properties.rank);
      map.getSource('alts').setData(fc(alts));

      // Rank badges, on the new stretch of each route.
      for (const c of data.candidates) {
        const newPath = c.legs.filter((l) => l.new).flatMap((l) => l.path);
        const at = pointAlong(newPath.length ? newPath : c.path, 0.45 + 0.08 * ((c.rank - 1) % 3));
        if (!at) continue;
        const node = document.createElement('span');
        node.className = 'rbadge';
        node.dataset.route = c.id;
        node.style.setProperty('--c', altColour(c.rank));
        node.textContent = c.badge || `#${c.rank}`;
        node.title = `${c.label} — ${c.delta.text}`;
        node.addEventListener('mouseenter', () => agent.highlightRoute(c.id));
        node.addEventListener('mouseleave', () => agent.highlightRoute(null));
        node.addEventListener('click', (ev) => { ev.stopPropagation(); agent.chooseRoute(c.id); });
        view.routeMarkers.push(new root.maplibregl.Marker({ element: node }).setLngLat([at[1], at[0]]).addTo(map));
      }
      emphasise(state);

      const d = data.disruption;
      if (d && d.lat != null) {
        const node = document.createElement('div');
        node.className = 'hazard';
        node.textContent = '!';
        node.title = `${d.title}${d.name ? ` — at ${d.name}` : ''}`;
        view.hazard = new root.maplibregl.Marker({ element: node }).setLngLat([d.lon, d.lat]).addTo(map);
      }

      // The split: one tracking line per branch.
      const branches = select.splitBranches(state);
      const letters = 'ABCDEFG';
      map.getSource('split').setData(fc(branches.map((b, i) => {
        const route = select.routeById(state, b.route_id);
        const colour = b.route_id === 'ORIGINAL' ? statusColour(data.status.level) : altColour(route && route.rank);
        return line(b.path, { id: b.route_id, colour, stays: b.route_id === 'ORIGINAL', letter: letters[i] });
      })));
      branches.forEach((b, i) => {
        const route = select.routeById(state, b.route_id);
        const colour = b.route_id === 'ORIGINAL' ? statusColour(data.status.level) : altColour(route && route.rank);
        const at = pointAlong(b.path, b.route_id === 'ORIGINAL' ? 0.3 : 0.62);
        if (!at) return;
        const node = document.createElement('div');
        node.className = 'branch-label';
        node.style.setProperty('--c', colour);
        node.innerHTML = `<b>${letters[i]}</b> · ${b.containers.length} box${b.containers.length === 1 ? '' : 'es'} · ${b.teu} TEU`;
        node.title = `${b.label} — ETA ${b.eta.slice(0, 16).replace('T', ' ')} UTC`;
        view.branchMarkers.push(new root.maplibregl.Marker({ element: node, offset: [0, -16] })
          .setLngLat([at[1], at[0]]).addTo(map));
      });

      if (view.fittedFor !== state.selection.id && data.candidates.length) {
        view.fittedFor = state.selection.id;
        const b = new root.maplibregl.LngLatBounds();
        const add = (latlon) => latlon.forEach(([lat, lon]) => b.extend([lon, lat]));
        add(data.original.path);
        data.candidates.forEach((c) => add(c.path));
        // Frame the routes in the part of the map the cards do not cover.
        const hub = $('hub');
        const legend = $('map-legend');
        const pane = el.getBoundingClientRect();
        const right = hub && !hub.hidden
          ? Math.max(60, pane.right - hub.getBoundingClientRect().left + 30) : 60;
        const bottom = legend ? legend.offsetHeight + 40 : 60;
        const room = pane.width - right - 50;
        map.fitBounds(b, {
          padding: { top: 90, bottom, left: 50, right: room > 240 ? right : 60 },
          maxZoom: 8, duration: 900,
        });
      }
    }

    /* Hover and choice only restyle — nothing is rebuilt, so the badge under
     * the pointer stays the element the pointer is over. */
    function emphasise(state) {
      const map = view.map;
      const hot = state.routing.hovered || state.routing.chosen;
      const splitOn = !!(state.split.enabled && state.split.data);
      const dim = splitOn ? 0.22 : 1;
      map.setPaintProperty('alt-line', 'line-width',
        hot ? ['case', ['==', ['get', 'id'], hot], 6.5, 3.2] : ['case', ['==', ['get', 'rank'], 1], 5, 3.6]);
      map.setPaintProperty('alt-line', 'line-opacity',
        hot ? ['case', ['==', ['get', 'id'], hot], 1, 0.35 * dim] : dim);
      map.setPaintProperty('alt-casing', 'line-opacity', splitOn ? 0.2 : 0.85);
      for (const m of view.routeMarkers) {
        const node = m.getElement();
        node.classList.toggle('is-hot', hot === node.dataset.route);
        node.classList.toggle('is-dim', (!!hot && hot !== node.dataset.route) || splitOn);
      }
    }

    function renderVendors(state) {
      const map = view.map;
      const data = state.vendors.data;
      const live = data && state.selection.id && data.shipment_id === state.selection.id;
      map.getSource('radius').setData(fc(live
        ? [circlePolygon(data.center.lat, data.center.lon, data.radius_km)] : []));

      const wanted = new Set(live ? data.vendors.map((v) => v.id) : []);
      for (const [id, m] of view.vendorMarkers) {
        if (!wanted.has(id)) { m.remove(); view.vendorMarkers.delete(id); }
      }
      if (!live) return;
      for (const v of data.vendors) {
        let m = view.vendorMarkers.get(v.id);
        if (!m) {
          const node = document.createElement('div');
          node.className = 'vpin';
          node.innerHTML = CHANNEL[v.channel] || CHANNEL.phone;
          node.addEventListener('click', (ev) => { ev.stopPropagation(); agent.selectVendor(v.id); });
          m = new root.maplibregl.Marker({ element: node, anchor: 'bottom-left', offset: [-4, 4] })
            .setLngLat([v.lon, v.lat]).addTo(map);
          view.vendorMarkers.set(v.id, m);
        }
        const node = m.getElement();
        node.classList.toggle('covers', !!v.covers_any);
        node.classList.toggle('outside', !v.within_radius);
        node.classList.toggle('is-on', state.vendors.selected === v.id);
        node.title = `${v.name} — ${v.distance_km} km · ${v.channel}${v.covers_any ? ' · can cover a recovery route' : ''}`;
        node.dataset.vendor = v.id;
      }
    }

    // ---------------------------------------------------------------
    // Legend
    // ---------------------------------------------------------------
    function renderLegend(state) {
      const host = $('map-legend');
      if (!host) return;
      const visible = select.visibleAssets({ ...state, filters: { ...state.filters, statuses: { green: true, yellow: true, red: true } } });
      const count = (s) => visible.filter((a) => a.status === s).length;
      const booked = state.assets.items.filter((a) => a.phase === 'booked').length;
      const labels = (state.assets.meta && state.assets.meta.status_labels) || { green: 'Nominal', yellow: 'Minor disruption', red: 'Major disruption' };
      const th = (state.assets.meta && state.assets.meta.thresholds) || { minor_max_hours: 4 };
      const t = view.tiles;
      const current = providersOf(t.meta || (state.assets && state.assets.meta))[Math.max(0, t.index)];
      const refused = t.refused.length
        ? ` ${t.refused.map((n) => esc(n)).join(' and ')} did not answer.` : '';
      const basemap = t.offline
        ? `<b>Offline outline</b> — no tile provider answered, so the vendored country shapes are drawn instead.${refused}`
        : t.ok && current
          ? `<b>${esc(current.name)}</b> tiles, desaturated.${refused}`
          : `<b>${esc(current ? current.name : 'Map')}</b> tiles loading; country outlines underneath.${refused}`;
      host.innerHTML = `
        <div class="map-legend-row">
          ${['red', 'yellow', 'green'].map((s) => `
            <button type="button" class="lg-status${state.filters.statuses[s] === false ? ' is-off' : ''}" data-status="${s}"
              title="${esc(labels[s])}${s === 'yellow' ? ` — under ${th.minor_max_hours} h late` : s === 'red' ? ` — ${th.minor_max_hours} h or more, or stopped` : ' — on schedule'}">
              <i style="background:${statusColour(s)}"></i>${esc(labels[s])} <b>${count(s)}</b>
            </button>`).join('')}
        </div>
        <details class="lg-more"${view.legendOpen ? ' open' : ''}>
          <summary>Modes, booked freight, basemap</summary>
          <div class="map-legend-row" style="margin-top:6px">
            ${MODES.map((m) => `<span class="lg-mode">${legendIcon(m)}${m === 'sea' ? 'ship' : m === 'road' ? 'truck' : m === 'rail' ? 'train' : 'barge'}</span>`).join('')}
          </div>
          <label class="lg-check" style="margin-top:6px"><input type="checkbox" id="lg-booked" ${state.filters.showBooked ? 'checked' : ''}>
            Include booked freight not yet departed (${booked})</label>
          <div class="lg-note">${basemap} Positions are where the schedule puts each asset unless somebody on site sent a fix. ${state.assets.items.length && state.assets.items.every((a) => a.id.startsWith('SYN-')) ? 'Book is <b>synthetic</b>.' : ''}</div>
        </details>`;
      const more = host.querySelector('.lg-more');
      if (more) more.addEventListener('toggle', () => { view.legendOpen = more.open; });
      host.querySelectorAll('.lg-status').forEach((b) => b.addEventListener('click', () => {
        const s = b.dataset.status;
        agent.setFilter({ statuses: { [s]: state.filters.statuses[s] === false } });
      }));
      const box = host.querySelector('#lg-booked');
      if (box) box.addEventListener('change', () => agent.setFilter({ showBooked: box.checked }));
      const busy = $('map-busy');
      if (busy) {
        busy.hidden = state.assets.status !== 'loading' && state.assets.status !== 'error';
        busy.textContent = state.assets.status === 'error'
          ? `Could not load assets — ${state.assets.error}` : 'Loading assets…';
      }
    }

    // ---------------------------------------------------------------
    // Theme — repaint what came from a token
    // ---------------------------------------------------------------
    document.addEventListener('themechange', async () => {
      if (!view.ready) return;
      const map = view.map;
      map.setPaintProperty('bg', 'background-color', tokenOf('--map-sea'));
      if (map.getLayer('land')) {
        map.setPaintProperty('land', 'fill-color', tokenOf('--map-land'));
        map.setPaintProperty('land-line', 'line-color', tokenOf('--map-border'));
      }
      if (map.getLayer('basemap')) {
        for (const [k, v] of Object.entries(rasterPaint())) map.setPaintProperty('basemap', k, v);
      }
      map.setPaintProperty('lanes', 'line-color', tokenOf('--map-lane'));
      map.setPaintProperty('route-original', 'line-color', tokenOf('--map-original'));
      map.setPaintProperty('alt-casing', 'line-color', tokenOf('--map-casing'));
      map.setPaintProperty('split-casing', 'line-color', tokenOf('--map-casing'));
      map.setPaintProperty('asset-halo', 'circle-color', tokenOf('--accent'));
      map.setPaintProperty('asset-halo', 'circle-stroke-color', tokenOf('--accent'));
      map.setPaintProperty('radius-fill', 'fill-color', tokenOf('--accent'));
      map.setPaintProperty('radius-line', 'line-color', tokenOf('--accent'));
      view.clusters.forEach((m) => { m._key = ''; });
      render(store.getState(), null, { force: true });
    });

    // ---------------------------------------------------------------
    document.querySelectorAll('.viewswitch button').forEach((b) =>
      b.addEventListener('click', () => agent.setView(b.dataset.view)));

    const topbar = document.querySelector('.topbar');
    const measure = () => {
      if (topbar) document.documentElement.style.setProperty('--topbar-h', `${topbar.offsetHeight}px`);
      if (view.map) view.map.resize();
    };
    measure();
    window.addEventListener('resize', measure);
    // The bar grows after boot — the ladder and the theme picker are filled in
    // once the board arrives — so a single measurement at startup is stale.
    if (topbar && root.ResizeObserver) new root.ResizeObserver(measure).observe(topbar);

    build();
    store.subscribe((state, prev) => render(state, prev));
    render(store.getState(), null, { force: true });
    return view;
  }

  root.MapView = { createMapView, assetSvg, GLYPH, CHANNEL };
})(typeof globalThis !== 'undefined' ? globalThis : this);
