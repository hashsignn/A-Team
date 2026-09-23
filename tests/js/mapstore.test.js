/* The fleet map's state contract, tested with no browser.
 *
 *   node --test tests/js/*.test.js
 *
 * Run from pytest by tests/test_map_store_js.py when Node is installed, and
 * skipped (not failed) when it is not — the base install has no Node.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const MapStore = require('../../api/static/mapstore.js');
const { createAgent } = require('../../api/static/mapagent.js');

const { T, createStore, select } = MapStore;

/* A fake server: the three payload shapes the agent reads, and a log of
 * every URL it was asked for. */
function fakeServer(overrides = {}) {
  const calls = [];
  const assets = {
    as_of: '2026-09-18T06:00:00+00:00',
    counts: { green: 1, yellow: 1, red: 1 },
    lanes: [],
    meta: { basemap: { tiles: [] } },
    assets: [
      { id: 'A', status: 'red', phase: 'in_transit', lat: 50, lon: 7, mode: 'barge' },
      { id: 'B', status: 'yellow', phase: 'staging', lat: 51, lon: 4, mode: 'road' },
      { id: 'C', status: 'green', phase: 'booked', lat: 1, lon: 103, mode: 'sea' },
    ],
  };
  const routes = (id) => ({
    shipment_id: id, eligible: true, weights: { time: 0.5, cost: 0.3, risk: 0.2 },
    original: { id: 'ORIGINAL', path: [[50, 7]], legs: [] },
    candidates: [
      { id: 'ALT-RAIL', rank: 1, badge: '#1', path: [[50, 7]], legs: [], delta: { text: '' } },
      { id: 'ALT-ROAD', rank: 2, badge: '#2', path: [[50, 7]], legs: [], delta: { text: '' } },
    ],
  });
  const fetchJson = async (url, opts) => {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    if (overrides[url]) return overrides[url](url, opts);
    if (url.startsWith('/api/map/assets?') || url === '/api/map/assets') return assets;
    const m = url.match(/^\/api\/map\/assets\/([^/?]+)(\/[a-z]+)?/);
    const id = decodeURIComponent(m[1]);
    if (!m[2]) return { shipment_id: id, status: { level: id === 'C' ? 'green' : 'red' }, containers: [] };
    if (m[2] === '/routes') return routes(id);
    if (m[2] === '/vendors') return { shipment_id: id, vendors: [{ id: 'P1' }, { id: 'P2' }] };
    if (m[2] === '/split') {
      const body = JSON.parse(opts.body);
      return { shipment_id: id, allocation: body.allocation || { X: 'ALT-RAIL' }, branches: [{ route_id: 'ORIGINAL' }, { route_id: 'ALT-RAIL' }] };
    }
    throw new Error(`unexpected ${url}`);
  };
  return { fetchJson, calls };
}

function setup(overrides) {
  const store = createStore();
  const server = fakeServer(overrides);
  const agent = createAgent({ store, fetchJson: server.fetchJson, now: () => '2026-09-18T06:00:00Z' });
  return { store, agent, server };
}

test('selecting a disrupted asset also calculates routes and queries partners', async () => {
  const { agent, server } = setup();
  await agent.loadAssets({ as_of: '2026-09-18T06:00:00+00:00', shipments: '150' });
  const s = await agent.selectAsset('A').then(() => agent.getState());
  assert.equal(s.selection.id, 'A');
  assert.equal(s.routing.status, 'ready');
  assert.equal(s.vendors.status, 'ready');
  assert.equal(select.rankedRoutes(s).length, 2);
  assert.ok(server.calls.some((c) => c.url.includes('/A/routes')));
  assert.ok(server.calls.every((c) => c.url.includes('as_of=2026-09-18')),
    'every call carries the board instant');
});

test('a green asset opens the hub without drawing routes', async () => {
  const { agent, server } = setup();
  await agent.loadAssets();
  await agent.selectAsset('C');
  assert.equal(agent.getState().routing.status, 'idle');
  assert.ok(!server.calls.some((c) => c.url.includes('/C/routes')));
});

test('closing removes everything the card drew', async () => {
  const { agent } = setup();
  await agent.loadAssets();
  await agent.selectAsset('A');
  await agent.toggleSplit(true);
  await agent.selectVendor('P1');
  await agent.clearSelection();
  const s = agent.getState();
  assert.equal(s.selection.id, null);
  assert.equal(s.routing.data, null);
  assert.equal(s.split.enabled, false);
  assert.equal(s.split.data, null);
  assert.equal(s.vendors.data, null);
  assert.equal(s.vendors.selected, null);
});

test('a late answer to a superseded question is dropped', async () => {
  let release;
  const slow = new Promise((r) => { release = r; });
  const { agent } = setup({
    '/api/map/assets/A': async () => { await slow; return { shipment_id: 'A', status: { level: 'green' } }; },
  });
  await agent.loadAssets();
  const first = agent.selectAsset('A');
  await agent.selectAsset('C');
  release();
  await first;
  assert.equal(agent.getState().selection.id, 'C');
  assert.equal(agent.getState().selection.detail.shipment_id, 'C');
});

test('weights are normalised and sent with the re-rank', async () => {
  const { agent, server } = setup();
  await agent.loadAssets();
  await agent.selectAsset('A');
  await agent.rankRoutes({ time: 2, cost: 1, risk: 1 });
  assert.deepEqual(agent.getState().routing.weights, { time: 0.5, cost: 0.25, risk: 0.25 });
  const last = server.calls.filter((c) => c.url.includes('/routes')).pop();
  assert.match(last.url, /w_time=0\.5/);
  assert.match(last.url, /w_cost=0\.25/);
});

test('split: toggling asks for the suggestion, assigning keeps the rest', async () => {
  const { agent, server } = setup();
  await agent.loadAssets();
  await agent.selectAsset('A');
  await agent.toggleSplit(true);
  const first = server.calls.filter((c) => c.url.includes('/split')).pop();
  assert.equal(first.body.allocation, null, 'the toggle evaluates the suggestion');
  await agent.assignContainers(['Y'], 'ALT-ROAD');
  const second = server.calls.filter((c) => c.url.includes('/split')).pop();
  assert.deepEqual(second.body.allocation, { X: 'ALT-RAIL', Y: 'ALT-ROAD' });
  assert.equal(select.splitBranches(agent.getState()).length, 2);
});

test('every action is journalled under the actor that took it', async () => {
  const { agent } = setup();
  await agent.loadAssets(null, { actor: 'system' });
  await agent.selectAsset('A', { actor: 'JEVA' });
  await agent.chooseRoute('ALT-RAIL', { actor: 'LAYA', summary: 'fastest that meets the deadline' });
  const j = agent.journal();
  assert.ok(j.some((e) => e.actor === 'JEVA' && e.action === T.ASSET_SELECT));
  assert.ok(j.some((e) => e.actor === 'JEVA' && e.action === T.ROUTES_REQUEST),
    'the routes a selection triggers are attributed to the same actor');
  assert.ok(j.some((e) => e.actor === 'LAYA' && e.summary === 'fastest that meets the deadline'));
});

test('a planner hovering does not flood the journal or re-render', async () => {
  const { agent, store } = setup();
  await agent.loadAssets();
  await agent.selectAsset('A');
  const before = agent.journal().length;
  let renders = 0;
  store.subscribe(() => { renders += 1; });
  await agent.highlightRoute('ALT-RAIL');
  await agent.highlightRoute('ALT-RAIL');
  await agent.highlightRoute('ALT-RAIL');
  assert.equal(renders, 1, 'an unchanged hover must not produce a new state');
  assert.equal(agent.journal().length, before);
});

test('booked freight is hidden until asked for, statuses filter', async () => {
  const { agent } = setup();
  await agent.loadAssets();
  assert.deepEqual(select.visibleAssets(agent.getState()).map((a) => a.id), ['A', 'B']);
  await agent.setFilter({ showBooked: true });
  assert.equal(select.visibleAssets(agent.getState()).length, 3);
  await agent.setFilter({ statuses: { red: false } });
  assert.deepEqual(select.visibleAssets(agent.getState()).map((a) => a.id).sort(), ['B', 'C']);
});

test('the hook list describes every public action', () => {
  const { agent } = setup();
  const names = agent.describe().map((h) => h.name);
  for (const name of names) assert.equal(typeof agent[name], 'function', name);
  for (const must of ['selectAsset', 'calculateRoutes', 'rankRoutes', 'splitShipment',
    'queryVendors', 'selectVendor', 'clearSelection']) {
    assert.ok(names.includes(must), must);
  }
});

test('the reducer is pure: dispatching never mutates the previous state', () => {
  const store = createStore();
  const before = store.getState();
  const frozen = JSON.stringify(before);
  store.dispatch({ type: T.ASSET_SELECT, id: 'A', seq: 1, meta: { actor: 'x' } });
  store.dispatch({ type: T.VIEW_SET, view: 'globe' });
  assert.equal(JSON.stringify(before), frozen);
  assert.notEqual(store.getState(), before);
});
