/* MapAgent — every action on the fleet map, as a named function.
 *
 * THE CONTRACT
 * ------------
 * A planner's click and an agent's call go through THE SAME FUNCTION. The map
 * view and the Action Hub never fetch, never mutate, never decide: a click on
 * a marker is `MapAgent.selectAsset(id)`, a drag of the weight slider is
 * `MapAgent.rankRoutes(weights)`, the split toggle is
 * `MapAgent.toggleSplit(true)`. So when JEVA or LAYA automate the pipeline
 * they call exactly what a person would, get exactly what a person would see,
 * and nothing about the map has to be refactored for them.
 *
 * Every hook:
 *   - takes an optional last argument `meta` = { actor: 'JEVA', summary }
 *     (default actor 'planner'); the store journals who did what, when;
 *   - returns a Promise of the resulting slice of state, so an agent can chain
 *     decisions without reading the DOM;
 *   - is idempotent to re-issue: late answers to superseded questions are
 *     dropped by the store, not raced by the caller.
 *
 * `MapAgent.describe()` returns the hook list as JSON-schema-ish tool specs,
 * ready to hand to a model as its tool list.
 *
 * Nothing here books, sends or writes. Recovery routes, splits and partner
 * queries are computed plans; the playbook gate still decides whether a
 * reroute may be taken (/ops).
 */
'use strict';

(function (root) {
  const DEFAULT_ACTOR = 'planner';

  function createAgent({ store, fetchJson, now }) {
    const { T } = (root.MapStore || require('./mapstore.js'));
    const stamp = now || (() => new Date().toISOString());
    let seq = 0;
    const nextSeq = () => ++seq;

    const meta = (m, summary, extra) => ({
      actor: (m && m.actor) || DEFAULT_ACTOR,
      at: stamp(),
      summary: (m && m.summary) || summary || null,
      ...(extra || {}),
    });
    const s = () => store.getState();

    function query(extra) {
      const p = s().params;
      const q = new URLSearchParams();
      if (p.as_of) q.set('as_of', p.as_of);
      if (p.shipments) q.set('shipments', String(p.shipments));
      for (const [k, v] of Object.entries(extra || {})) {
        if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
      }
      const text = q.toString();
      return text ? `?${text}` : '';
    }
    const enc = encodeURIComponent;

    // ---------------------------------------------------------------
    // Assets
    // ---------------------------------------------------------------
    async function loadAssets(params, m) {
      if (params) {
        store.dispatch({ type: T.PARAMS_SET, params, meta: meta(m, 'set board instant', { journal: false }) });
      }
      const n = nextSeq();
      store.dispatch({ type: T.ASSETS_REQUEST, seq: n, meta: meta(m, 'load assets') });
      try {
        const payload = await fetchJson(`/api/map/assets${query()}`);
        store.dispatch({ type: T.ASSETS_SUCCESS, seq: n, payload });
      } catch (err) {
        store.dispatch({ type: T.ASSETS_FAILURE, seq: n, error: String(err.message || err) });
      }
      return s().assets;
    }

    /* Select an asset and open its Action Hub.
     *
     * For a yellow or red asset this also calculates the recovery routes and
     * queries the partners in the radius — immediately, not on a second
     * click, because the moment a planner looks at a disrupted asset is the
     * moment they need to know what else can move it. Resolves once all of
     * it is in the store. */
    async function selectAsset(id, m) {
      const n = nextSeq();
      store.dispatch({ type: T.ASSET_SELECT, id, seq: n, meta: meta(m, `select ${id}`) });
      let detail = null;
      try {
        detail = await fetchJson(`/api/map/assets/${enc(id)}${query()}`);
        store.dispatch({ type: T.ASSET_DETAIL_SUCCESS, id, seq: n, detail });
      } catch (err) {
        store.dispatch({ type: T.ASSET_DETAIL_FAILURE, id, seq: n, error: String(err.message || err) });
        return s().selection;
      }
      if (s().selection.id !== id) return s().selection;
      const level = detail.status && detail.status.level;
      if (level === 'yellow' || level === 'red') {
        const inner = { actor: (m && m.actor) || DEFAULT_ACTOR };
        await Promise.all([calculateRoutes(id, {}, inner), queryVendors(id, {}, inner)]);
      }
      return s().selection;
    }

    function clearSelection(m) {
      store.dispatch({ type: T.SELECTION_CLEAR, meta: meta(m, 'close the Action Hub') });
      return Promise.resolve(s().selection);
    }

    // ---------------------------------------------------------------
    // Recovery routes
    // ---------------------------------------------------------------
    async function calculateRoutes(id, opts, m) {
      const target = id || s().selection.id;
      if (!target) throw new Error('calculateRoutes: no asset selected');
      const weights = (opts && opts.weights) || s().routing.weights;
      const n = nextSeq();
      store.dispatch({
        type: T.ROUTES_REQUEST, id: target, seq: n,
        meta: meta(m, `calculate recovery routes for ${target}`),
      });
      const extra = {};
      if (weights) {
        extra.w_time = weights.time; extra.w_cost = weights.cost; extra.w_risk = weights.risk;
      }
      if (opts && opts.force) extra.force = 'true';
      try {
        const data = await fetchJson(`/api/map/assets/${enc(target)}/routes${query(extra)}`);
        store.dispatch({ type: T.ROUTES_SUCCESS, id: target, seq: n, data });
      } catch (err) {
        store.dispatch({ type: T.ROUTES_FAILURE, id: target, seq: n, error: String(err.message || err) });
      }
      return s().routing;
    }

    /* Re-rank on new Time / Cost / Risk weights. The server does the
     * arithmetic (engine/fleet/reroute.py); the weights are the planner's. */
    async function rankRoutes(weights, m) {
      store.dispatch({
        type: T.WEIGHTS_SET, weights,
        meta: meta(m, `weights time ${weights.time} · cost ${weights.cost} · risk ${weights.risk}`),
      });
      const inner = { actor: (m && m.actor) || DEFAULT_ACTOR };
      const routing = await calculateRoutes(null, { weights: s().routing.weights }, inner);
      if (s().split.enabled && s().split.data) {
        await splitShipment(s().split.data.allocation, {}, inner);
      }
      return routing;
    }

    function highlightRoute(routeId, m) {
      store.dispatch({
        type: T.ROUTE_HOVER, routeId,
        meta: meta(m, routeId ? `highlight ${routeId}` : 'clear highlight', { journal: !!(m && m.actor) }),
      });
      return Promise.resolve(s().routing);
    }

    function chooseRoute(routeId, m) {
      store.dispatch({ type: T.ROUTE_CHOOSE, routeId, meta: meta(m, `choose ${routeId}`) });
      return Promise.resolve(s().routing);
    }

    // ---------------------------------------------------------------
    // Split shipment
    // ---------------------------------------------------------------
    async function toggleSplit(enabled, m) {
      store.dispatch({
        type: T.SPLIT_TOGGLE, enabled: !!enabled,
        meta: meta(m, enabled ? 'split the shipment' : 'undo the split'),
      });
      if (!enabled) return s().split;
      const inner = { actor: (m && m.actor) || DEFAULT_ACTOR };
      return splitShipment(null, { target: s().routing.chosen || undefined }, inner);
    }

    /* Evaluate an allocation { container_id: route_id } — or, with null, the
     * suggested one: only the boxes the original asset now makes late, onto
     * the route that gets them there in time. */
    async function splitShipment(allocation, opts, m) {
      const id = s().selection.id;
      if (!id) throw new Error('splitShipment: no asset selected');
      if (!s().split.enabled) {
        store.dispatch({ type: T.SPLIT_TOGGLE, enabled: true, meta: meta(m, 'split the shipment') });
      }
      const n = nextSeq();
      store.dispatch({
        type: T.SPLIT_REQUEST, seq: n,
        meta: meta(m, allocation ? 'evaluate a container allocation' : 'suggest a split'),
      });
      const body = { allocation: allocation || null };
      if (opts && opts.target) body.target = opts.target;
      if (s().routing.weights) body.weights = s().routing.weights;
      try {
        const data = await fetchJson(`/api/map/assets/${enc(id)}/split${query()}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        store.dispatch({ type: T.SPLIT_SUCCESS, seq: n, data });
      } catch (err) {
        store.dispatch({ type: T.SPLIT_FAILURE, seq: n, error: String(err.message || err) });
      }
      return s().split;
    }

    /* Move some containers onto a route, keeping every other assignment. */
    function assignContainers(containerIds, routeId, m) {
      const current = (s().split.data && s().split.data.allocation) || {};
      const next = { ...current };
      for (const cid of containerIds) next[cid] = routeId;
      return splitShipment(next, {}, m || { summary: `assign ${containerIds.length} box(es) to ${routeId}` });
    }

    // ---------------------------------------------------------------
    // Partners
    // ---------------------------------------------------------------
    async function queryVendors(id, opts, m) {
      const target = id || s().selection.id;
      if (!target) throw new Error('queryVendors: no asset selected');
      const radiusKm = opts && opts.radiusKm;
      const n = nextSeq();
      store.dispatch({
        type: T.VENDORS_REQUEST, id: target, seq: n, radiusKm,
        meta: meta(m, `query partners near ${target}`),
      });
      const w = s().routing.weights;
      const extra = { radius_km: radiusKm, teu: opts && opts.teu };
      if (w) { extra.w_time = w.time; extra.w_cost = w.cost; extra.w_risk = w.risk; }
      try {
        const data = await fetchJson(`/api/map/assets/${enc(target)}/vendors${query(extra)}`);
        store.dispatch({ type: T.VENDORS_SUCCESS, id: target, seq: n, data });
      } catch (err) {
        store.dispatch({ type: T.VENDORS_FAILURE, id: target, seq: n, error: String(err.message || err) });
      }
      return s().vendors;
    }

    function selectVendor(vendorId, m) {
      store.dispatch({
        type: T.VENDOR_SELECT, vendorId,
        meta: meta(m, vendorId ? `open partner ${vendorId}` : 'close partner card'),
      });
      return Promise.resolve(s().vendors);
    }

    // ---------------------------------------------------------------
    // View
    // ---------------------------------------------------------------
    function focusLane(routeId, m) {
      store.dispatch({ type: T.LANE_FOCUS, routeId, meta: meta(m, `focus lane ${routeId}`, { journal: false }) });
      return Promise.resolve(s().focusLane);
    }
    function setView(view, m) {
      store.dispatch({ type: T.VIEW_SET, view, meta: meta(m, `show the ${view}`) });
      return Promise.resolve(s().view);
    }
    function setFilter(patch, m) {
      store.dispatch({ type: T.FILTER_SET, patch, meta: meta(m, 'filter assets') });
      return Promise.resolve(s().filters);
    }

    const HOOKS = [
      ['loadAssets', 'Load every asset for a board instant.', { as_of: 'ISO-8601 instant, optional', shipments: 'book size, optional' }],
      ['selectAsset', 'Open the Action Hub for an asset; for yellow/red also calculates recovery routes and queries partners.', { id: 'shipment id, e.g. SYN-0001' }],
      ['clearSelection', 'Close the Action Hub; routes, split and partners disappear with it.', {}],
      ['calculateRoutes', 'Recalculate recovery routes for the selected (or given) asset.', { id: 'shipment id, optional', weights: '{time,cost,risk}, optional', force: 'compute even for a green asset' }],
      ['rankRoutes', 'Re-rank recovery routes on new Time / Cost / Risk weights.', { weights: '{time, cost, risk} — any non-negative numbers, normalised' }],
      ['highlightRoute', 'Highlight one route on the map (null clears).', { routeId: 'candidate id or ORIGINAL' }],
      ['chooseRoute', 'Mark a recovery route as the chosen one (the split target by default).', { routeId: 'candidate id' }],
      ['toggleSplit', 'Turn load-splitting on (evaluates the suggested split) or off.', { enabled: 'boolean' }],
      ['splitShipment', 'Evaluate a container allocation; null evaluates the suggestion.', { allocation: '{container_id: route_id} or null', target: 'candidate id, optional' }],
      ['assignContainers', 'Move containers onto a route, keeping the rest of the allocation.', { containerIds: 'array of container ids', routeId: 'candidate id or ORIGINAL' }],
      ['queryVendors', 'Find partners in the radius and which recovery routes each can cover.', { id: 'shipment id, optional', radiusKm: 'number, optional', teu: 'TEU to move, optional' }],
      ['selectVendor', 'Open a partner card (null closes it).', { vendorId: 'partner id' }],
      ['focusLane', 'Highlight a lane on the map.', { routeId: 'lane id' }],
      ['setView', "Switch the left pane between 'map' and 'globe'.", { view: "'map' | 'globe'" }],
      ['setFilter', 'Filter assets by status or include booked freight.', { patch: '{showBooked?, statuses?: {green,yellow,red}}' }],
    ];

    function describe() {
      return HOOKS.map(([name, description, params]) => ({
        name,
        description,
        parameters: params,
        returns: 'Promise of the resulting state slice',
        meta: 'optional last argument {actor, summary} — journalled',
      }));
    }

    return {
      loadAssets, selectAsset, clearSelection,
      calculateRoutes, rankRoutes, highlightRoute, chooseRoute,
      toggleSplit, splitShipment, assignContainers,
      queryVendors, selectVendor,
      focusLane, setView, setFilter,
      getState: () => store.getState(),
      subscribe: (fn) => store.subscribe(fn),
      journal: () => store.getState().journal.slice(),
      describe,
    };
  }

  /* The browser's fetch, with the server's error detail in the message. */
  async function fetchJson(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = '';
      try {
        const body = await res.json();
        detail = typeof body.detail === 'string' ? body.detail
          : (Array.isArray(body.detail) ? body.detail.map((d) => d.msg || JSON.stringify(d)).join('; ') : '');
      } catch { /* not JSON */ }
      throw new Error(`${res.status}${detail ? ` — ${detail}` : ''}`);
    }
    return res.json();
  }

  const api = { createAgent, fetchJson };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.MapAgentFactory = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
