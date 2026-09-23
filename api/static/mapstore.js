/* The fleet map's state — one store, one reducer, no DOM.
 *
 * WHY A STORE AT ALL
 * ------------------
 * Everything on the map — which asset is selected, which recovery routes
 * exist and how they rank, how a load is split, which partners are nearby —
 * lives HERE, and only here. The map view and the Action Hub are renderers:
 * they subscribe, draw what the state says, and turn clicks into calls on
 * MapAgent. They hold no domain state of their own.
 *
 * That is what lets an agent (JEVA, LAYA, or a test) drive the map through
 * the exact functions a planner's clicks use, and see the same result: there
 * is no second copy of "the selected asset" hiding in a click handler for
 * the two to disagree about.
 *
 * Redux-shaped, not Redux: a pure reducer, plain actions, a subscribe list.
 * Sixty lines of pattern are cheaper than a dependency and a build step, and
 * the rest of this front end has neither.
 *
 * Loads as a plain script in the browser (window.MapStore) and as a CommonJS
 * module in Node, where tests/js/ exercises the reducer with no browser.
 */
'use strict';

(function (root) {
  const T = {
    VIEW_SET: 'map/view/set',
    PARAMS_SET: 'map/params/set',
    ASSETS_REQUEST: 'map/assets/request',
    ASSETS_SUCCESS: 'map/assets/success',
    ASSETS_FAILURE: 'map/assets/failure',
    FILTER_SET: 'map/filter/set',
    LANE_FOCUS: 'map/lane/focus',
    ASSET_SELECT: 'map/asset/select',
    ASSET_DETAIL_SUCCESS: 'map/asset/detail/success',
    ASSET_DETAIL_FAILURE: 'map/asset/detail/failure',
    SELECTION_CLEAR: 'map/selection/clear',
    ROUTES_REQUEST: 'map/routes/request',
    ROUTES_SUCCESS: 'map/routes/success',
    ROUTES_FAILURE: 'map/routes/failure',
    ROUTE_HOVER: 'map/route/hover',
    ROUTE_CHOOSE: 'map/route/choose',
    WEIGHTS_SET: 'map/weights/set',
    SPLIT_TOGGLE: 'map/split/toggle',
    SPLIT_REQUEST: 'map/split/request',
    SPLIT_SUCCESS: 'map/split/success',
    SPLIT_FAILURE: 'map/split/failure',
    VENDORS_REQUEST: 'map/vendors/request',
    VENDORS_SUCCESS: 'map/vendors/success',
    VENDORS_FAILURE: 'map/vendors/failure',
    VENDOR_SELECT: 'map/vendor/select',
  };

  const JOURNAL_MAX = 200;

  function initialState() {
    return {
      view: 'map',
      params: { as_of: null, shipments: '150' },
      assets: {
        status: 'idle', seq: 0, items: [], byId: {}, counts: null,
        lanes: [], meta: null, asOfLabel: null, error: null,
      },
      filters: { showBooked: false, statuses: { green: true, yellow: true, red: true } },
      focusLane: null,
      selection: { id: null, status: 'idle', seq: 0, detail: null, error: null },
      routing: {
        id: null, status: 'idle', seq: 0, data: null, weights: null,
        hovered: null, chosen: null, error: null,
      },
      split: { enabled: false, status: 'idle', seq: 0, data: null, error: null },
      vendors: {
        id: null, status: 'idle', seq: 0, data: null, radiusKm: null,
        selected: null, error: null,
      },
      // Every action an actor took, planner or agent, in order. The audit
      // trail a planner reads when an agent did something they did not expect.
      journal: [],
    };
  }

  // A response is applied only if it answers the CURRENT question: same
  // asset, and the latest request of its kind. Anything else is a late
  // answer to a question nobody is asking any more.
  const current = (slice, a) => a.seq === slice.seq && (a.id == null || a.id === slice.id);

  function reduce(state, a) {
    switch (a.type) {
      case T.VIEW_SET: {
        const v = a.view === 'globe' ? 'globe' : 'map';
        return v === state.view ? state : { ...state, view: v };
      }

      case T.PARAMS_SET:
        return { ...state, params: { ...state.params, ...a.params } };

      case T.ASSETS_REQUEST:
        return { ...state, assets: { ...state.assets, status: 'loading', seq: a.seq, error: null } };
      case T.ASSETS_SUCCESS: {
        if (a.seq !== state.assets.seq) return state;
        const items = a.payload.assets || [];
        const byId = {};
        for (const item of items) byId[item.id] = item;
        // A selection that no longer exists at the new instant is dropped
        // rather than left pointing at nothing.
        const keep = state.selection.id && byId[state.selection.id];
        const next = {
          ...state,
          assets: {
            ...state.assets, status: 'ready', items, byId,
            counts: a.payload.counts || null, lanes: a.payload.lanes || [],
            meta: a.payload.meta || null, asOfLabel: a.payload.as_of_label || null,
            error: null,
          },
        };
        return keep ? next : clearSelection(next);
      }
      case T.ASSETS_FAILURE:
        if (a.seq !== state.assets.seq) return state;
        return { ...state, assets: { ...state.assets, status: 'error', error: a.error } };

      case T.FILTER_SET:
        return {
          ...state,
          filters: {
            ...state.filters,
            ...(a.patch.showBooked != null ? { showBooked: !!a.patch.showBooked } : {}),
            statuses: { ...state.filters.statuses, ...(a.patch.statuses || {}) },
          },
        };

      case T.LANE_FOCUS:
        if ((a.routeId || null) === state.focusLane) return state;
        return { ...state, focusLane: a.routeId || null };

      case T.ASSET_SELECT: {
        const base = clearSelection(state);
        return {
          ...base,
          selection: { id: a.id, status: 'loading', seq: a.seq, detail: null, error: null },
        };
      }
      case T.ASSET_DETAIL_SUCCESS:
        if (!current(state.selection, a)) return state;
        return { ...state, selection: { ...state.selection, status: 'ready', detail: a.detail } };
      case T.ASSET_DETAIL_FAILURE:
        if (!current(state.selection, a)) return state;
        return { ...state, selection: { ...state.selection, status: 'error', error: a.error } };
      case T.SELECTION_CLEAR:
        return clearSelection(state);

      case T.WEIGHTS_SET:
        return { ...state, routing: { ...state.routing, weights: normaliseWeights(a.weights) } };
      case T.ROUTES_REQUEST:
        return {
          ...state,
          routing: {
            ...state.routing, id: a.id, status: 'loading', seq: a.seq, error: null,
            // Keep the last answer on screen while the re-rank is in flight,
            // but only if it is for the same asset.
            data: state.routing.id === a.id ? state.routing.data : null,
          },
        };
      case T.ROUTES_SUCCESS: {
        if (!current(state.routing, a)) return state;
        const ids = new Set((a.data.candidates || []).map((c) => c.id));
        return {
          ...state,
          routing: {
            ...state.routing, status: 'ready', data: a.data,
            chosen: ids.has(state.routing.chosen) ? state.routing.chosen : null,
            hovered: ids.has(state.routing.hovered) ? state.routing.hovered : null,
          },
        };
      }
      case T.ROUTES_FAILURE:
        if (!current(state.routing, a)) return state;
        return { ...state, routing: { ...state.routing, status: 'error', error: a.error } };
      // Hover arrives on every mouse move. An unchanged value must return the
      // SAME state, or every move re-renders the map under the pointer.
      case T.ROUTE_HOVER:
        if ((a.routeId || null) === state.routing.hovered) return state;
        return { ...state, routing: { ...state.routing, hovered: a.routeId || null } };
      case T.ROUTE_CHOOSE:
        if ((a.routeId || null) === state.routing.chosen) return state;
        return { ...state, routing: { ...state.routing, chosen: a.routeId || null } };

      case T.SPLIT_TOGGLE:
        return {
          ...state,
          split: a.enabled
            ? { ...state.split, enabled: true }
            : { enabled: false, status: 'idle', seq: state.split.seq, data: null, error: null },
        };
      case T.SPLIT_REQUEST:
        return { ...state, split: { ...state.split, status: 'loading', seq: a.seq, error: null } };
      case T.SPLIT_SUCCESS:
        if (a.seq !== state.split.seq || !state.split.enabled) return state;
        if (a.data.shipment_id !== state.selection.id) return state;
        return { ...state, split: { ...state.split, status: 'ready', data: a.data } };
      case T.SPLIT_FAILURE:
        if (a.seq !== state.split.seq) return state;
        return { ...state, split: { ...state.split, status: 'error', error: a.error } };

      case T.VENDORS_REQUEST:
        return {
          ...state,
          vendors: {
            ...state.vendors, id: a.id, status: 'loading', seq: a.seq,
            radiusKm: a.radiusKm ?? null, error: null,
            data: state.vendors.id === a.id ? state.vendors.data : null,
          },
        };
      case T.VENDORS_SUCCESS: {
        if (!current(state.vendors, a)) return state;
        const still = (a.data.vendors || []).some((v) => v.id === state.vendors.selected);
        return {
          ...state,
          vendors: {
            ...state.vendors, status: 'ready', data: a.data,
            selected: still ? state.vendors.selected : null,
          },
        };
      }
      case T.VENDORS_FAILURE:
        if (!current(state.vendors, a)) return state;
        return { ...state, vendors: { ...state.vendors, status: 'error', error: a.error } };
      case T.VENDOR_SELECT:
        if ((a.vendorId || null) === state.vendors.selected) return state;
        return { ...state, vendors: { ...state.vendors, selected: a.vendorId || null } };

      default:
        return state;
    }
  }

  /* Closing the card removes everything that belonged to it: the routes, the
   * split, the partners. "Disappear on close" is a property of the state, so
   * the renderers cannot forget to honour it. */
  function clearSelection(state) {
    return {
      ...state,
      selection: { id: null, status: 'idle', seq: state.selection.seq, detail: null, error: null },
      routing: {
        ...state.routing, id: null, status: 'idle', data: null,
        hovered: null, chosen: null, error: null,
      },
      split: { enabled: false, status: 'idle', seq: state.split.seq, data: null, error: null },
      vendors: {
        ...state.vendors, id: null, status: 'idle', data: null,
        selected: null, error: null,
      },
    };
  }

  function normaliseWeights(w) {
    if (!w) return null;
    const out = {};
    let total = 0;
    for (const k of ['time', 'cost', 'risk']) {
      out[k] = Math.max(0, Number(w[k]) || 0);
      total += out[k];
    }
    if (total <= 0) return { time: 1 / 3, cost: 1 / 3, risk: 1 / 3 };
    for (const k of Object.keys(out)) out[k] = Math.round((out[k] / total) * 10000) / 10000;
    return out;
  }

  function reducer(state, a) {
    const next = reduce(state, a);
    if (next === state) return state;
    if (!a.meta || !a.meta.actor || a.meta.journal === false) return next;
    const entry = {
      at: a.meta.at || new Date().toISOString(),
      actor: a.meta.actor,
      action: a.type,
      summary: a.meta.summary || null,
    };
    const journal = next.journal.concat(entry);
    return { ...next, journal: journal.slice(-JOURNAL_MAX) };
  }

  // ---------------------------------------------------------------
  // Selectors — pure functions of the state. Renderers read through these
  // rather than reaching into the shape, so the shape can change once.
  // ---------------------------------------------------------------
  const PHASES_ALWAYS = new Set(['in_transit', 'at_node', 'staging']);

  const select = {
    visibleAssets(s) {
      return s.assets.items.filter((a) =>
        (PHASES_ALWAYS.has(a.phase) || (s.filters.showBooked && a.phase === 'booked'))
        && s.filters.statuses[a.status] !== false);
    },
    selectedAsset(s) {
      return s.selection.id ? s.assets.byId[s.selection.id] || null : null;
    },
    rankedRoutes(s) {
      return (s.routing.data && s.routing.data.candidates) || [];
    },
    routeById(s, id) {
      if (!s.routing.data) return null;
      if (id === 'ORIGINAL') return s.routing.data.original;
      return (s.routing.data.candidates || []).find((c) => c.id === id) || null;
    },
    splitBranches(s) {
      return (s.split.enabled && s.split.data && s.split.data.branches) || [];
    },
    vendors(s) {
      return (s.vendors.data && s.vendors.data.vendors) || [];
    },
    selectedVendor(s) {
      return select.vendors(s).find((v) => v.id === s.vendors.selected) || null;
    },
  };

  function createStore(preloaded) {
    let state = preloaded || initialState();
    const listeners = new Set();
    return {
      getState: () => state,
      dispatch(action) {
        const prev = state;
        state = reducer(state, action);
        if (state !== prev) listeners.forEach((fn) => fn(state, prev, action));
        return action;
      },
      subscribe(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
    };
  }

  const MapStore = { T, initialState, reducer, createStore, select, normaliseWeights };
  if (typeof module !== 'undefined' && module.exports) module.exports = MapStore;
  else root.MapStore = MapStore;
})(typeof globalThis !== 'undefined' ? globalThis : this);
