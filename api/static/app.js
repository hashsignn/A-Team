/* Supply Chain Risk Radar — globe, radar, ranked routes.
 *
 * No build step, no CDN. globe.gl and topojson-client are vendored under
 * /vendor, the country geometry under /geo. The page renders with the network
 * cable pulled out, which is the only guarantee worth having on demo day.
 */
'use strict';

// ---------------------------------------------------------------
// Level colours. RESERVED — they mean "how soon must someone decide",
// and nothing else. Read from CSS so the palette lives in one file.
// ---------------------------------------------------------------
/* Read live, never cached at module load.
 *
 * Four themes share this script, and the White rung is a different colour in
 * each of them: white on the dark surface, slate on the three light ones,
 * because a white dot on a white page is not a quiet level, it is no level.
 * A palette snapshotted at startup would survive a theme switch and repaint
 * the globe in the previous theme's colours. */
const token = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const LEVEL_COLOR = {};
const BAND_COLOR = {};

function readPalette() {
  for (const level of ['green', 'white', 'blue', 'yellow', 'red']) {
    LEVEL_COLOR[level] = token(`--lvl-${level}`);
  }
  for (const band of ['minor', 'moderate', 'severe']) {
    BAND_COLOR[band] = token(`--band-${band}`);
  }
}
readPalette();
const LEVEL_ORDER = ['red', 'yellow', 'blue', 'white', 'green'];

/* Visual weight follows URGENCY, not luminance.
 *
 * The level colours are the client's and are not ours to change — but white
 * on a dark globe is intrinsically the loudest thing on screen, and White
 * means "Bias: monitor", the second-quietest rung. Left alone, the lines
 * that need nothing would shout over the ones that need a decision today.
 *
 * So stroke and opacity carry the urgency ordering while hue keeps its
 * prescribed meaning. Red is thick and solid; Green is a whisper. */
const WEIGHT = {
  red:    { stroke: 2.2, alpha: 1.00 },
  yellow: { stroke: 1.9, alpha: 0.95 },
  blue:   { stroke: 1.3, alpha: 0.78 },
  white:  { stroke: 0.8, alpha: 0.40 },
  green:  { stroke: 0.6, alpha: 0.24 },
};
// Counted in working time: weekends and public holidays do not count.
const LEVEL_WHEN = {
  red: 'within 8 h',
  yellow: 'within 24–36 h',
  blue: 'within 3 days',
  white: 'within 5 days',
  green: 'no action',
};

const state = {
  board: null,
  selected: null,
  // True while a route is open: the map and the globe draw only that route.
  isolate: false,
  // A ladder click shows ONLY that level; a second click shows them all.
  levelOnly: null,
  // Normal routes need nothing, so the list of affected routes leaves them
  // out until asked. The map and the globe still draw all freight.
  showNormal: false,
  focusOnly: false,    // the list narrowed to the focus routes
  // WHOSE BOARD THIS IS. The site a planner answers for, and which
  // customers they are looking at ('' all, 'A' key accounts, 'AB' key and
  // standard). Remembered, and in the URL, so a planner's view is a link.
  site: '',
  cust: '',
  // The shipment highlighted inside the open route — set when the route was
  // opened by clicking that shipment on the map.
  shipFocus: null,
  globe: null,
  spinning: true,
  resumeTimer: null,
  // The instant the CURRENT board was built at. Renders read this, never the
  // URL: reload() writes the URL after rendering, so a render that re-read it
  // would describe the previous instant.
  params: null,
};

// How long rotation pauses after a selection. The globe is meant to keep
// turning, but spinning the route you just clicked over the horizon defeats
// the click — so it holds still long enough to read, then resumes.
const RESUME_AFTER_MS = 7000;

const $ = (id) => document.getElementById(id);
const chf = (v) => v == null ? '—' : 'CHF ' + Math.round(v).toLocaleString('en-US');
// One grouping style everywhere (CHF 204,523), the same as the engine's
// sentences and the reports, so no two numbers on a screen look different.
const num = (v) => Math.round(v).toLocaleString('en-US');
// Stand-in names carry "(synthetic)"; the board says so once per section.
const plainName = (n) => String(n ?? '').replace(/\s*\(synthetic[^)]*\)\s*$/i, '');
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function hours(h) {
  if (h == null) return '—';
  if (h < 0) return 'passed';
  if (h < 0.5) return 'now';
  if (h < 48) return Math.round(h) + ' h';
  return Math.round(h / 24) + ' days';
}
// "decide in 6 h", or "decide now" when no working time is left before the
// option closes (it closes over a weekend or a holiday).
function inHours(h) {
  const t = hours(h);
  return t === 'now' || t === 'passed' || t === '—' ? t : `in ${t}`;
}
// Lead times are WORKING time: weekends and holidays do not count. When the
// plain clock says something else, say both, so a Saturday-night board that
// reads "now" for a Sunday cut-off explains itself.
function clockNote(working, clock) {
  if (working == null || clock == null || clock - working < 1) return '';
  const plain = hours(clock) === 'now' ? 'under 1 h' : hours(clock);
  return working < 0.5 ? `closes in ${plain}, before the next working day`
    : `working time (${plain} on the clock)`;
}

// ===============================================================
// The as-of instant
// ===============================================================
/* Nothing in engine/ reads the wall clock — every stage takes an explicit
 * as-of. That makes this the only "now" there is, and it is why the URL can
 * carry it: `?as_of=...` is a complete, shareable description of a board.
 * A past instant is a hindcast through the identical code path.
 */
// The server's default instant: the recording's own, once there is one.
const DEFAULT_AS_OF = ((m) => (m && !m.startsWith('__') ? m : '2026-09-18T06:00:00+00:00'))((document.querySelector('meta[name="radar-default-as-of"]') || {}).content);

function readParams() {
  const q = new URLSearchParams(location.search);
  return {
    as_of: q.get('as_of') || DEFAULT_AS_OF,
    shipments: q.get('shipments') || '150',
  };
}

function writeParams({ as_of, shipments }) {
  const q = new URLSearchParams();
  if (as_of !== DEFAULT_AS_OF) q.set('as_of', as_of);
  if (shipments !== '150') q.set('shipments', shipments);
  // The left pane's view belongs to the map (mapview.js); carry it across.
  const view = new URLSearchParams(location.search).get('view');
  if (view) q.set('view', view);
  if (state.site) q.set('site', state.site);
  if (state.cust) q.set('cust', state.cust);
  const url = q.toString() ? `${location.pathname}?${q}` : location.pathname;
  history.replaceState(null, '', url);
}

/* <input type="datetime-local"> has no timezone, so its value is read and
 * written as UTC directly rather than going through Date, which would apply
 * the viewer's local offset and silently shift the board by hours. */
function isoToInput(iso) {
  return iso.slice(0, 16);
}
function inputToIso(value) {
  return `${value.length === 16 ? value : value.slice(0, 16)}:00+00:00`;
}

// ===============================================================
// The desk: whose board this is
// ===============================================================
const DESK_KEY = 'radar.desk';
const PRIORITY_LABEL = { A: 'Key account', B: 'Standard', C: 'Flexible' };

function readDesk() {
  const q = new URLSearchParams(location.search);
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(DESK_KEY) || '{}') || {}; } catch { saved = {}; }
  const cust = q.has('cust') ? q.get('cust') : (saved.cust || '');
  return {
    site: q.has('site') ? q.get('site') : (saved.site || ''),
    cust: ['', 'A', 'AB'].includes(cust) ? cust : '',
  };
}

function writeDesk() {
  try { localStorage.setItem(DESK_KEY, JSON.stringify({ site: state.site, cust: state.cust })); } catch { /* private window */ }
  const q = new URLSearchParams(location.search);
  state.site ? q.set('site', state.site) : q.delete('site');
  state.cust ? q.set('cust', state.cust) : q.delete('cust');
  const url = q.toString() ? `${location.pathname}?${q}` : location.pathname;
  history.replaceState(null, '', url);
}

function priLabel(p) {
  const tiers = (state.board && state.board.priorities) || {};
  return (tiers[p] && tiers[p].label) || PRIORITY_LABEL[p] || p;
}
function priBadge(p) {
  if (!p) return '';
  return `<span class="pri pri-${esc(p)}" title="${esc(priLabel(p))}">${p === 'A' ? '★ ' : ''}${esc(priLabel(p))}</span>`;
}
function siteName(id) {
  const site = ((state.board && state.board.sites) || []).find((x) => x.id === id);
  return site ? site.name : id;
}

function custMatches(r) {
  if (!state.cust) return true;
  const want = state.cust === 'A' ? ['A'] : ['A', 'B'];
  return (r.customers || []).some((c) => want.includes(c.priority));
}

/* The routes of this desk: the site and customer filters only. The ladder
 * counts these, so a rung reads "how many of MY routes", not the world's. */
function deskRoutes() {
  if (!state.board) return [];
  return state.board.routes.filter((r) =>
    (!state.site || (r.site && r.site.id === state.site)) && custMatches(r));
}

function keyAtRisk(r) {
  return (r.customers || []).filter((c) => c.priority === 'A')
    .reduce((n, c) => n + (c.at_risk || 0), 0);
}

/* Run fn once the map's agent exists. actionhub.js boots after this file,
 * and a filter chosen before it is up must still reach the map. */
function withAgent(fn, tries = 40) {
  if (window.MapAgent) { fn(window.MapAgent); return; }
  if (tries > 0) setTimeout(() => withAgent(fn, tries - 1), 150);
}

function syncMapFilters() {
  const filtered = state.site || state.cust || state.levelOnly || state.focusOnly;
  const lanes = filtered ? visibleRoutes().map((r) => r.route_id) : null;
  const priorities = state.cust === 'A' ? ['A'] : state.cust === 'AB' ? ['A', 'B'] : null;
  withAgent((agent) => agent.setFilter({ lanes, priorities },
    { actor: 'board', summary: 'follow the board filters' }));
}

function initDesk() {
  const pick = $('f-site');
  const sites = state.board.sites || [];
  pick.innerHTML = '<option value="">All sites</option>' + sites.map((x) =>
    `<option value="${esc(x.id)}">${esc(x.name)} · ${x.routes} route${x.routes === 1 ? '' : 's'}${
      x.urgent_routes ? `, ${x.urgent_routes} urgent` : ''}</option>`).join('');
  if (state.site && !sites.some((x) => x.id === state.site)) state.site = '';
  pick.value = state.site;
  if (!pick.dataset.bound) {
    pick.dataset.bound = '1';
    pick.addEventListener('change', () => { state.site = pick.value; applyDesk(); });
    $('f-cust').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-cust]');
      if (!b) return;
      state.cust = b.dataset.cust;
      applyDesk();
    });
  }
  markCust();
}

function markCust() {
  $('f-cust').querySelectorAll('button').forEach((b) =>
    b.classList.toggle('is-on', b.dataset.cust === state.cust));
}

/* A filter changed: everything that reads it redraws, in place. The open
 * route stays open if it is still in view; otherwise the list comes back,
 * because a detail pane for a route you just filtered away is a lie. */
function applyDesk() {
  markCust();
  writeDesk();
  renderLadder(state.board.levels);
  refreshPaths();
  renderTable();
  syncMapFilters();
  if (state.selected && !visibleRoutes().some((r) => r.route_id === state.selected)) {
    state.selected = null;
    refreshPaths();
    showRouteList();
  }
}

async function fetchBoard({ as_of, shipments }) {
  const q = new URLSearchParams({ as_of, shipments });
  const res = await fetch(`/api/board?${q}`);
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`board ${res.status}: ${detail.slice(0, 160)}`);
  }
  return res.json();
}

// ===============================================================
// Boot
// ===============================================================
/* ?event=… reopens the modal after the board paints.
 *
 * Deferred to the end of boot rather than done eagerly: the modal reads
 * state.board, and a link that opened an empty dialog and then filled it in
 * would flash. If the id is not on this board — a stale link, or a different
 * as-of — nothing happens and the parameter is dropped, which is the honest
 * outcome. A dialog saying "event not found" helps nobody. */

async function boot() {
  const params = readParams();
  state.params = params;
  Object.assign(state, readDesk());
  $('asof-input').value = isoToInput(params.as_of);
  // Drawn before the board arrives, so the header has its final shape on
  // the first paint and nothing below it moves when the counts come in.
  renderLadder(null);
  initHowto();

  const [board, topo] = await Promise.all([
    fetchBoard(params),
    fetch('/geo/countries-110m.json').then((r) => r.json()),
  ]);

  const countries = topojson.feature(topo, topo.objects.countries);

  state.board = board;
  initGlobe(countries, board);
  renderFilters();
  initResponseTabs();
  initAsk();
  initAlerts();
  applyBoard(board);

  $('asof-form').addEventListener('submit', (e) => {
    e.preventDefault();
    reload(inputToIso($('asof-input').value));
  });
  $('asof-now').addEventListener('click', () => {
    $('asof-input').value = isoToInput(DEFAULT_AS_OF);
    reload(DEFAULT_AS_OF);
  });
  // The as-of folds away again on a click anywhere else, like any menu.
  document.addEventListener('click', (e) => {
    const pop = $('asof-pop');
    if (pop && pop.open && !pop.contains(e.target)) pop.open = false;
  });
  $('meet-chip').addEventListener('click', openAllHands);
  $('pen-chip').addEventListener('click', togglePenalties);
  document.addEventListener('click', (e) => {
    const card = $('cust-card');
    if (card && !card.hidden && !card.contains(e.target)) card.hidden = true;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('cust-card')) $('cust-card').hidden = true;
  });
  // The Contract card folds away on a click anywhere else, like any menu.
  document.addEventListener('click', (e) => {
    const pop = $('contract-pop');
    if (pop && pop.open && !pop.contains(e.target)) pop.open = false;
  });

  // ?route=<id> opens that route: the way back from the decision tree and
  // the route page lands on the route you left, not on the list.
  const wanted = new URLSearchParams(location.search).get('route');
  if (wanted && board.routes.some((r) => r.route_id === wanted)) select(wanted, { fly: true, fit: true });
}

/* Delay penalties on or off, for the whole server: every CHF figure moves,
 * so the board is fetched again and the open route, if any, reopened. */
function renderPenChip(board) {
  const on = !!(board.penalties && board.penalties.enabled);
  const chip = $('pen-chip');
  chip.setAttribute('aria-pressed', String(on));
  $('pen-state').textContent = on ? 'on' : 'off';
  chip.title = on
    ? "Counting each customer's contract delay penalty in the exposure. Click to leave them out."
    : "Leaving contract delay penalties out of the exposure. Click to count them.";
}
async function togglePenalties() {
  const chip = $('pen-chip');
  const on = !(state.board.penalties && state.board.penalties.enabled);
  const keep = !$('panel-body').hidden ? state.selected : null;
  chip.disabled = true;
  try {
    const res = await fetch('/api/penalties', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ enabled: on }),
    });
    if (!res.ok) throw new Error(`penalties ${res.status}`);
    await reload(state.params.as_of);
    if (keep && state.board.routes.some((r) => r.route_id === keep)) select(keep, {});
  } catch (err) {
    console.error(err);
  } finally {
    chip.disabled = false;
  }
}

/* The route's delay clauses, one line per customer, behind its Contract
 * button. At-risk customers first and marked; the full clause is the
 * tooltip, so the card stays short. */
function renderContract(r) {
  const pop = $('contract-pop');
  if (!pop) return;
  pop.open = false;
  const clauses = r.clauses || [];
  const counted = !!(state.board.penalties && state.board.penalties.enabled);
  const priority = Object.fromEntries((r.customers || []).map((c) => [c.name, c.priority]));
  const charged = clauses.filter((c) => c.at_risk && c.summary !== 'no delay penalty' && c.summary !== 'not on file');
  $('contract-n').hidden = !charged.length;
  $('contract-n').textContent = charged.length;
  $('contract-btn').title = charged.length
    ? `${charged.length} customer(s) at risk here have a delay penalty in their contract`
    : 'Delay clauses in this route\'s customer contracts';
  const line = (c) => `
      <li class="${c.at_risk ? 'is-risk' : ''}" title="${esc(c.clause)}">
        <div class="cc-who"><b>${esc(c.customer)}</b> ${priority[c.customer] === 'A' ? priBadge('A') : ''}<span class="cc-type">${esc(c.label)}</span></div>
        <div class="cc-terms">${esc(c.summary)}</div>
      </li>`;
  // At-risk customers are the answer; the rest of the route's customers are
  // one click away.
  const risky = clauses.filter((c) => c.at_risk);
  const rest = clauses.filter((c) => !c.at_risk);
  $('contract-card').innerHTML = `
    <div class="cc-head"><b>Delay clauses</b>
      <span class="cc-state${counted ? ' is-on' : ''}">${counted ? 'counted in the exposure' : 'not counted (penalties off)'}</span></div>
    ${risky.length ? `<ul>${risky.map(line).join('')}</ul>` : '<div class="cc-terms">No customer on this route is at risk.</div>'}
    ${rest.length ? `<button type="button" class="cc-more" id="cc-more">Show the other ${rest.length} customer(s) on this route</button>
      <ul class="cc-rest" id="cc-rest" hidden>${rest.map(line).join('')}</ul>` : ''}
    <div class="cc-foot">Red edge: at risk now. Hover a line for the clause. Example terms until Sika's contracts replace them.</div>`;
  const more = $('cc-more');
  if (more) more.addEventListener('click', (e) => {
    e.stopPropagation();
    $('cc-rest').hidden = false;
    more.remove();
  });
}

/* The all-hands tab, from anywhere: the header chip, the globe strip. */
function openAllHands() {
  if (!$('panel-body').hidden) showRouteList();
  openPanelTab('allhands');
}

/* Re-run at a different instant WITHOUT rebuilding the globe.
 * Tearing down and recreating the WebGL scene would drop the camera, restart
 * the rotation and flash the pane — so only the data layers are replaced. */
async function reload(asOf) {
  const params = { ...readParams(), as_of: asOf };
  const stage = document.querySelector('.stage');
  stage.classList.add('is-loading');
  $('asof-apply').disabled = true;
  try {
    const board = await fetchBoard(params);
    state.board = board;
    // Before applyBoard, which renders — see the note on state.params.
    state.params = params;
    writeParams(params);
    // The previous selection may not exist, or may no longer be visible, at
    // the new instant. applyBoard falls back to the most severe route.
    state.selected = null;
    if (state.globe) state.globe.pointsData(board.nodes);
    applyBoard(board);
    // The map is the same board at the same instant — never a different one.
    if (window.MapAgent) window.MapAgent.loadAssets({ as_of: asOf, shipments: params.shipments });
  } catch (err) {
    console.error(err);
    $('brand-sub').textContent = `could not load that instant: ${err.message}`;
  } finally {
    stage.classList.remove('is-loading');
    $('asof-apply').disabled = false;
    if ($('asof-pop')) $('asof-pop').open = false;
  }
}

/* Everything that depends on the board, in one place, so boot and reload
 * cannot drift apart. */
function applyBoard(board) {
  $('brand-sub').textContent =
    `${board.shipments_total} shipments (synthetic) · ` +
    `${board.variables_total} risk variables · ${board.routes.length} routes`;
  $('asof-text').textContent = board.as_of_label;
  // Every deadline clock on the board counts down from the board's moment.
  if (window.DeadlineClock) DeadlineClock.start(board.as_of);

  renderPosture(board.posture);
  renderMeetChip();
  renderPenChip(board);
  initDesk();
  initPanelTabs();
  renderAllHands();
  applyDesk();
  // A later instant's signals are a different answer; read them again.
  signalsLoaded = false;
  if ($('siglist') && !$('siglist').hidden) loadSignals();

  /* No auto-selection. The board used to open on its worst lane, which
   * meant the right column showed one route and the other sixteen were
   * somewhere else entirely. Opening on the LIST answers "what is affected"
   * before being asked, and picking a lane is one click from there. */
  $('panel-body').hidden = true;
  $('panel-list').hidden = false;

  /* Re-measure now that the ladder has rendered. The first measurement runs
   * before the level chips exist, so it reads a header one row shorter than
   * the one that ends up on screen — and the stage is then that much too
   * tall, which is the whole bug this measurement exists to avoid. */
  sizeStage();
  sizeGlobe();
}

// ===============================================================
// Globe
// ===============================================================
/* Everything on the globe that comes from a CSS token.
 *
 * Split out of initGlobe because a theme switch has to repaint a globe that
 * already exists — rebuilding it would drop the camera position and the
 * selection, and the point of switching themes is to look at the same board
 * in a different skin. */
/* Lane markers.
 *
 * Radius scales with the SQUARE ROOT of the affected count, not linearly:
 * a ring's visual weight is its area, so a linear radius makes a lane with
 * twice the freight look four times as bad. The eye reads area, so the
 * mapping has to compensate for it.
 */
function laneMarkers() {
  if (!state.board) return [];
  return drawnRoutes()
    .map((r) => {
      const m = r.marker || {};
      if (m.lat == null || !m.affected) return null;
      return {
        route_id: r.route_id,
        name: r.name,
        level: r.level,
        level_label: r.level_label,
        lat: m.lat,
        lon: m.lon,
        affected: m.affected,
        total: m.total,
        radius: 1.1 + Math.sqrt(m.affected) * 0.62,
        color: withAlpha(LEVEL_COLOR[r.level], 0.55),
      };
    })
    .filter(Boolean);
}

function refreshMarkers() {
  if (state.globe) state.globe.ringsData(laneMarkers());
}

function paintGlobe(globe) {
  globe
    .atmosphereColor(token('--globe-atmos'))
    .polygonCapColor(() => token('--globe-cap'))
    .polygonSideColor(() => token('--globe-side'))
    .polygonStrokeColor(() => token('--globe-stroke'));

  const material = globe.globeMaterial();
  material.color.set(token('--globe-land'));
  material.emissive.set(token('--globe-emissive'));
  material.shininess = parseFloat(token('--globe-shine')) || 0.1;
  material.needsUpdate = true;
}

document.addEventListener('themechange', () => {
  readPalette();
  if (!state.globe || !state.board) return;
  paintGlobe(state.globe);
  // Route and node colours are level colours, so they move with the palette.
  state.globe.pointsData(state.board.nodes);
  refreshPaths();
  refreshMarkers();
  renderLadder(state.board.levels);
  renderTable();
  renderAllHands();
  renderPosture(state.board.posture);
  // Re-select rather than re-render: the detail panel draws an SVG radar in
  // band colours, and the cheapest correct way to repaint it is the path that
  // already knows how to build it. `fly: false` keeps the camera where it is.
  if (state.selected) select(state.selected, { fly: false });
});

function initGlobe(countries, board) {
  const el = $('globe');

  const globe = Globe()(el)
    .backgroundColor('rgba(0,0,0,0)')
    .showAtmosphere(true)
    .atmosphereAltitude(0.17)
    .showGraticules(true);

  // No texture image: a vector globe stays sharp at any zoom, needs no
  // multi-megabyte binary, and keeps the routes as the brightest thing on
  // screen — which is the point of the pane.
  globe
    .polygonsData(countries.features)
    .polygonAltitude(0.004);

  paintGlobe(globe);

  // --- lane markers ---------------------------------------------------
  /* One marker per lane, radius scaled by how much freight the gate touched.
   *
   * Deliberately NOT one marker per vessel. Roughly 65 of 125 shipments are
   * touched in a typical run, and 65 dots on a globe is exactly the clutter
   * the brief warned about. The per-vessel divergence is real — thirteen
   * consignments on one lane routinely carry ten distinct deadlines — but it
   * belongs on a page that can hold it, which is what clicking this opens.
   */
  globe
    .ringsData(laneMarkers())
    .ringLat('lat').ringLng('lon')
    .ringMaxRadius((d) => d.radius)
    .ringColor((d) => () => d.color)
    .ringPropagationSpeed(0)
    .ringRepeatPeriod(0);

  // --- ports & nodes -------------------------------------------------
  globe
    .pointsData(board.nodes)
    .pointLat('lat').pointLng('lon')
    .pointAltitude(0.005)
    .pointRadius((n) => n.chokepoint ? 0.16 : 0.22)
    .pointColor((n) => n.chokepoint ? token('--globe-choke') : token('--globe-port'))
    .pointsMerge(false)
    /* A port is a legitimate way in. Clicking one selects the busiest route
     * through it, because "what is happening at Antwerp" is a question a
     * planner asks by pointing at Antwerp, not by reading a table of lanes
     * and working out which ones call there. */
    .onPointClick((n) => {
      const route = busiestRouteThrough(n.id);
      if (route) select(route.route_id, { fly: true });
    })
    .onPointHover((n) => {
      el.style.cursor = n ? 'pointer' : '';
      n ? showTip(nodeTip(n)) : hideTip();
    });

  // --- routes ----------------------------------------------------------
  globe
    .pathsData(pathData())
    .pathPoints('pts')
    .pathPointLat((p) => p[0])
    .pathPointLng((p) => p[1])
    .pathPointAlt((p) => p[2])
    .pathColor((d) => d.color)
    .pathStroke((d) => d.stroke)
    .pathTransitionDuration(0)
    .pathDashLength(1)
    .pathDashGap(0)
    /* Single click selects without moving the camera — the planner is
     * already looking at the thing they clicked, and flying the view to it
     * would throw away the spatial context that made them click. Double
     * click is the explicit "take me there". */
    .onPathClick((d) => {
      const now = Date.now();
      const same = state.lastPathClick?.id === d.route_id;
      const quick = now - (state.lastPathClick?.at || 0) < 350;
      state.lastPathClick = { id: d.route_id, at: now };
      select(d.route_id, { fly: same && quick });
    })
    .onPathHover((d) => {
      el.style.cursor = d ? 'pointer' : '';
      d ? showTip(routeTip(d)) : hideTip();
    });

  const controls = globe.controls();
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.42;
  controls.enableDamping = true;
  controls.minDistance = 190;
  controls.maxDistance = 620;

  // Open on Europe: the anchor lane is the Rhine and that is where the
  // demo's attention belongs on first paint.
  globe.pointOfView({ lat: 34, lng: 12, altitude: 2.35 }, 0);

  state.globe = globe;
  sizeStage();
  sizeGlobe();
  window.addEventListener('resize', () => { sizeStage(); sizeGlobe(); });

  /* The header changes height without the window changing size: the ladder
   * wraps, a theme swaps a font, a long as-of label pushes a row. A resize
   * listener never fires for any of those. */
  const header = document.querySelector('.topbar');
  if (header && 'ResizeObserver' in window) {
    new ResizeObserver(() => { sizeStage(); sizeGlobe(); }).observe(header);
  }
  // Built after the map has already claimed the pane: start paused.
  if (document.querySelector('.globe-wrap.is-map')) globe.pauseAnimation();

  el.addEventListener('mousemove', (e) => {
    const tip = $('globe-tooltip');
    if (tip.hidden) return;
    const r = el.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - r.left + 16, r.width - 296) + 'px';
    tip.style.top = Math.min(e.clientY - r.top + 16, r.height - 90) + 'px';
  });

  /* THE GLOBE STOPS THE MOMENT YOU TOUCH IT.
   *
   * A drag is a deliberate act — somebody has decided where they want to
   * look. Continuing to rotate under their hand, or resuming seven seconds
   * later while they are still reading, is the single most irritating thing
   * a globe can do. So a pointerdown on the canvas is a STOP, not a pause:
   * it flips the control to Paused and stays there until the person presses
   * Rotating again.
   *
   * That is different from holdRotation(), which is a temporary courtesy
   * while a hover tooltip is open. A hover is not a decision; a grab is.
   *
   * pointerdown, not mousedown: it is one event for mouse, pen and touch,
   * which is what "the moment we touch it" means on a laptop with a
   * touchscreen — and this demo will be shown on one. */
  el.addEventListener('pointerdown', () => {
    el.classList.add('is-grabbing');
    if (state.spinning) toggleSpin();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach((evt) =>
    el.addEventListener(evt, () => el.classList.remove('is-grabbing')));

  /* Scrolling to zoom is also a decision, and OrbitControls handles the zoom
   * itself — this only has to notice it happened. Passive: we never call
   * preventDefault, so the browser is free to scroll the page when the
   * pointer is outside the canvas. */
  el.addEventListener('wheel', () => {
    if (state.spinning) toggleSpin();
  }, { passive: true });

  $('btn-spin').addEventListener('click', toggleSpin);
  $('btn-reset').addEventListener('click', () => {
    globe.pointOfView({ lat: 34, lng: 12, altitude: 2.35 }, 900);
  });
}

/* How much room is left under the header.
 *
 * Read rather than assumed: the header is two bars, and its height moves with
 * the theme, the browser zoom and whether the ladder wraps onto a second row.
 * A hardcoded offset is right until one of those changes, and then the globe
 * is a few pixels too tall for the screen forever. */
function sizeStage() {
  const stage = document.querySelector('.stage');
  if (!stage) return;
  const top = stage.getBoundingClientRect().top + window.scrollY;
  document.documentElement.style.setProperty(
    '--stage-h', `${Math.max(460, window.innerHeight - top)}px`);
}

/* The map (mapview.js) shares the left pane. While it is showing, the globe
 * stops drawing frames: a WebGL scene nobody can see still costs a laptop
 * its fan. */
window.RadarGlobe = {
  pause() { if (state.globe) state.globe.pauseAnimation(); },
  resume() {
    if (!state.globe) return;
    state.globe.resumeAnimation();
    sizeStage();
    sizeGlobe();
  },
};

function sizeGlobe() {
  const el = $('globe');
  if (!state.globe || !el) return;
  state.globe.width(el.clientWidth).height(el.clientHeight);
}

/* Route lines.
 *
 * ONE PATH PER ROUTE, not per leg. A leg shared by several lanes would
 * otherwise be drawn once and clicking it would be ambiguous — which route's
 * radar should open? Per-route paths keep the click unambiguous.
 *
 * Shared segments are separated by a small altitude step per route, so
 * overlapping lanes read as parallel ribbons instead of z-fighting into a
 * single muddy line. That is the "don't cluster everything" requirement:
 * the clutter is spatial, so the fix is spatial.
 */
/* What the map and the globe draw: the visible routes, or only the open
 * one while a route is open. */
function drawnRoutes() {
  const routes = visibleRoutes();
  return state.isolate && state.selected ? routes.filter((r) => r.route_id === state.selected) : routes;
}

function pathData() {
  const routes = drawnRoutes();
  return routes.map((r, i) => {
    const pts = [];
    r.legs.forEach((leg) => {
      leg.path.forEach((p) => pts.push([p[0], p[1], p[2] + i * 0.0016]));
    });
    const selected = state.selected === r.route_id;
    const dim = state.selected && !selected;
    const w = WEIGHT[r.level];
    return {
      route_id: r.route_id,
      name: r.name,
      level: r.level,
      level_label: r.level_label,
      directive: r.directive,
      reason: r.reason,
      exposure_chf: r.exposure_chf,
      shipments_at_risk: r.shipments_at_risk,
      pts,
      color: withAlpha(LEVEL_COLOR[r.level],
        selected ? 1 : lift(dim ? w.alpha * 0.4 : w.alpha)),
      stroke: selected ? w.stroke + 1.4 : w.stroke,
    };
  });
}

/* Raise a transparency floor on the light themes.
 *
 * WEIGHT encodes the URGENCY ORDERING, which is a property of the ladder and
 * does not change with the theme — Red stays loudest, Green stays a whisper.
 * What does change is how much transparency a background can absorb. Against
 * near-black, alpha 0.24 still reads as a line. Against white it reads as
 * nothing, and a Normal route that renders invisible is indistinguishable
 * from a route the gate never found.
 *
 * So the floor moves, the ordering does not: each weight is remapped into
 * [floor, 1] instead of [0, 1]. */
function lift(alpha) {
  const floor = parseFloat(token('--path-alpha-floor')) || 0;
  return floor + alpha * (1 - floor);
}

function withAlpha(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function refreshPaths() {
  if (state.globe) state.globe.pathsData(pathData());
  refreshMarkers();
}

/* Pause the spin, then hand it back.
 * Only if the user has not deliberately parked it — their toggle outranks
 * ours, and silently restarting a globe someone paused is infuriating. */
function holdRotation() {
  if (!state.globe || !state.spinning) return;
  state.globe.controls().autoRotate = false;
  clearTimeout(state.resumeTimer);
  state.resumeTimer = setTimeout(() => {
    if (state.spinning && state.globe) state.globe.controls().autoRotate = true;
  }, RESUME_AFTER_MS);
}

function toggleSpin() {
  state.spinning = !state.spinning;
  state.globe.controls().autoRotate = state.spinning;
  const b = $('btn-spin');
  b.classList.toggle('is-on', state.spinning);
  b.setAttribute('aria-pressed', String(state.spinning));
  b.lastChild.textContent = state.spinning ? ' Rotating' : ' Paused';
}

function showTip(html) {
  const t = $('globe-tooltip');
  t.innerHTML = html;
  t.hidden = false;
}
function hideTip() { $('globe-tooltip').hidden = true; }

function routeTip(d) {
  return `<span class="tt-lvl" style="color:${LEVEL_COLOR[d.level]}">${esc(d.level_label)}</span>
    <b>${esc(d.name)}</b>
    <div class="muted">${esc(d.directive)}</div>
    <div style="margin-top:5px">${d.shipments_at_risk} shipment(s) · ${chf(d.exposure_chf)} exposed</div>`;
}
function nodeTip(n) {
  return `<b>${esc(n.name)}</b>
    <div class="muted">${esc(n.kind.replace(/_/g, ' '))} · ${esc(n.country)}</div>
    <div style="margin-top:4px">${n.shipments} shipments</div>`;
}

// ===============================================================
// Ladder & posture
// ===============================================================
/* The all-hands, in one line: how often it meets now, when it next sits and
 * how many of the rule's checks are crossed. The full sentence is the
 * tooltip; the details are the All-hands tab. The same line sits in the
 * header chip, so the meeting is in sight on the map as well as the globe. */
function meetLine() {
  const h = state.board && state.board.all_hands;
  if (!h) return null;
  const checks = h.checks || [];
  const crossed = checks.filter((c) => c.crossed).length;
  const tone = { convene: 'red', watch: 'yellow', normal: 'green' }[h.posture] || 'blue';
  return {
    tone,
    when: `${h.cadence_label} · next ${h.next_label.replace(' UTC', '')}`,
    crossed: checks.length ? `${crossed} of ${checks.length} checks crossed` : '',
  };
}

function renderPosture(p) {
  const m = meetLine();
  const host = $('posture');
  if (!m) { host.innerHTML = ''; return; }
  const c = LEVEL_COLOR[m.tone];
  host.style.borderLeftColor = c;
  host.title = p.headline || '';
  host.innerHTML =
    `<span class="posture-label" style="color:${c}">All-hands</span>
     <span class="posture-text">meets <b>${esc(m.when)}</b>${m.crossed ? ` · ${esc(m.crossed)}` : ''}</span>
     <button type="button" class="ctl ctl--mini posture-go">Details</button>`;
  host.querySelector('.posture-go').addEventListener('click', openAllHands);
}

function renderMeetChip() {
  const chip = $('meet-chip');
  const m = meetLine();
  if (!chip) return;
  chip.hidden = !m;
  if (!m) return;
  const h = state.board.all_hands;
  chip.className = `meet-chip meet-chip--${m.tone}`;
  chip.title = `${state.board.posture.headline || ''}\nClick for the agenda: who is in the room and what each function can pull.`;
  chip.innerHTML = `<i aria-hidden="true"></i><span><b>All-hands</b> ${esc(m.when)}</span>`;
  // The tab carries the same dot while the meeting is stepped up.
  const dot = $('ptab-meet');
  if (dot) {
    dot.hidden = !h.changed;
    dot.style.background = LEVEL_COLOR[m.tone];
  }
}

const LADDER_SKELETON = [
  ['red', 'Critical'], ['yellow', 'Alert'], ['blue', 'Watch'], ['white', 'Bias'], ['green', 'Normal'],
].map(([level, label]) => ({ level, label, directive: '', count: null }));

function renderLadder(levels) {
  // Counts are of THIS desk's routes — the site and customer filters — so a
  // rung reads "how many of mine". Null before the board arrives: the rungs
  // are drawn at their final size and filled in, so the header never moves.
  const rows = levels || LADDER_SKELETON;
  const mine = state.board ? deskRoutes() : null;
  const only = state.levelOnly;
  $('ladder').innerHTML = rows.map((l) => {
    const count = mine ? mine.filter((r) => r.level === l.level).length : null;
    const on = only === l.level;
    return `
    <button type="button" class="rung${on ? ' is-on' : ''}${only && !on ? ' is-off' : ''}${count ? ' has-items' : ''}"
            data-level="${l.level}" title="${esc(l.directive)}${l.directive ? '. ' : ''}${on ? 'Click to show every level' : 'Click to show only this level'}"
            aria-pressed="${on}">
      <span class="rung-dot" style="background:${LEVEL_COLOR[l.level]}"></span>
      <span>
        <span class="rung-name">${esc(l.label)}</span>
        <span class="rung-count">${count == null ? '–' : count}</span>
        <span class="rung-when">${esc(LEVEL_WHEN[l.level])}</span>
      </span>
    </button>`;
  }).join('');

  if (!levels) return;
  $('ladder').querySelectorAll('.rung').forEach((b) => {
    b.addEventListener('click', () => setLevel(state.levelOnly === b.dataset.level ? null : b.dataset.level));
  });
}

/* Show only one level, or (null) every level. Everything that reads the
 * level filter redraws: the ladder, the list, the globe, the map. */
function setLevel(level) {
  state.levelOnly = level;
  if (state.board) renderLadder(state.board.levels);
  markLevelChip();
  refreshPaths();
  renderTable();
  syncMapFilters();
  if (state.selected && !visibleRoutes().some((r) => r.route_id === state.selected)) {
    showRouteList();
  }
}

function markLevelChip() {
  const chip = $('f-all');
  if (!chip) return;
  const onRoutes = !$('rlist').hidden;
  const label = state.levelOnly && (state.board.levels.find((l) => l.level === state.levelOnly) || {}).label;
  chip.hidden = !state.levelOnly || !onRoutes;
  chip.innerHTML = label ? `Only ${esc(label)} <span aria-hidden="true">✕</span>` : '';
  chip.title = 'Show every level again';
}

/* Which lane to open when somebody clicks a port.
 *
 * The one where the most is at stake, among the lanes still visible under the
 * current level filters. A port sits on several lanes and only one panel can
 * open; picking the worst is the only choice that is never surprising —
 * anything else means clicking a red port can open a green lane. */
function busiestRouteThrough(nodeId) {
  const through = visibleRoutes().filter((r) => (r.node_ids || []).includes(nodeId));
  if (!through.length) return null;
  return through.reduce((worst, r) =>
    (r.severity_score || 0) > (worst.severity_score || 0) ? r : worst);
}

/* What the ladder, the focus toggle and the desk leave in view. The map and
 * the globe draw these. */
function visibleRoutes() {
  return deskRoutes().filter((r) => (!state.levelOnly || r.level === state.levelOnly)
    && (!state.focusOnly || (r.real_data && r.real_data.focus)));
}

/* The list of AFFECTED routes: the visible ones, less the Normal routes
 * (nothing to decide) unless they were asked for. */
function listRoutes() {
  const keepNormal = state.showNormal || state.levelOnly === 'green';
  return visibleRoutes().filter((r) => keepNormal || r.level !== 'green');
}

/* The focus-route label. It counts the sources this run actually read as
 * real (a recording or a live answer), never the route merely being on the
 * focus list: "5 of 5" is a claim a planner will act on. */
function focusBadge(r) {
  const d = r.real_data;
  if (!d || !d.focus) return '';
  const full = d.of > 0 && d.real === d.of;
  const none = d.real === 0;
  const detail = (d.sources || [])
    .map((s) => `${s.real ? '✓' : '✗'} ${s.label}${s.real ? '' : ' (not real yet)'}`).join('\n');
  return `<span class="rli-real${full ? ' is-full' : ''}${none ? ' is-none' : ''}"
      title="Sources read from real data this run:\n${esc(detail)}">${d.real}/${d.of} real sources</span>`;
}

// ===============================================================
// Selection & detail panel
// ===============================================================
function select(routeId, { fly, fit, ship, fromMap } = {}) {
  const r = state.board.routes.find((x) => x.route_id === routeId);
  if (!r) return;
  state.selected = routeId;
  state.shipFocus = ship || null;
  // An open route stands alone on the map and the globe; closing it brings
  // every other route back (showRouteList).
  state.isolate = true;
  // One route in view at a time: a vehicle card for a different route is
  // closed, so the map and the panel never describe two things.
  if (!fromMap) {
    withAgent((agent) => {
      const st = agent.getState();
      const a = st.selection.id && st.assets.byId[st.selection.id];
      if (a && a.lane_id !== routeId) agent.clearSelection({ actor: 'board', summary: 'another route was opened' });
    });
  }

  refreshPaths();
  renderDetail(r);
  renderResponse(r);
  markTableRow(routeId);
  linkOps(routeId);
  // Mark the journey on the map: where it starts, where it changes mode,
  // the ports and the Sika company that receives it.
  withAgent(() => {
    if (!window.MapJourney) return;
    const spec = journeySpec(r);
    if (spec) spec.fit = !fromMap;
    window.MapJourney.show(spec);
  });
  // Highlight the lane on the 2D map too. Framed only when the pick came
  // from the list (`fit`) and the lane is off screen; a click on the map
  // itself never moves it — the planner is already looking there.
  withAgent((agent) => agent.focusLane(routeId, undefined, { fit: !!fit, isolate: true }));

  if (fly && state.globe && r.legs.length) {
    const pts = r.legs.flatMap((l) => l.path);
    const mid = pts[Math.floor(pts.length / 2)];
    if (mid) state.globe.pointOfView({ lat: mid[0], lng: mid[1], altitude: 2.1 }, 1100);
  }
  holdRotation();
}

/* THE JOURNEY, for the map: one point per place that matters.
 *
 *   origin    the Sika site it starts from (the real plant on a focus route)
 *   transfer  where the freight changes mode (truck → barge at Basel)
 *   port      a sea port where it is loaded or arrives
 *   via       a choke point or gauge it passes (Suez, Kaub)
 *   dest      the Sika company that receives it, where known (focus routes),
 *             joined to the arrival port by a dashed on-carriage line
 * Hover a point for the operators that run it and where the fact came from. */
const MODE_WORD = { road: 'truck', rail: 'rail', barge: 'barge', sea: 'ship', air: 'air' };

function journeySpec(r) {
  const stops = Object.fromEntries((r.stops || []).map((x) => [x.id, x]));
  const legs = r.legs || [];
  if (!legs.length || !stops[legs[0].from]) return null;
  const d = r.real_data || {};
  const word = (m) => MODE_WORD[m] || m;
  const short = (t) => String(t || '').split(':')[0].trim();
  const operatorsAt = (name) => {
    const key = String(name).split(/[ (]/)[0].toLowerCase();
    return (d.operators || []).filter((o) => String(o.legs || '').toLowerCase().includes(key)).map((o) => o.name);
  };
  const points = [];
  const push = (stop, role, label, title) => points.push({ lat: stop.lat, lon: stop.lon, role, label, title });

  const o = stops[legs[0].from];
  const originLabel = d.origin && d.origin.site ? short(d.origin.site) : `${o.name} (Sika ${o.kind === 'plant' ? 'plant' : 'site'})`;
  push(o, 'origin', originLabel,
    `Starts here, leaves by ${word(legs[0].mode)}.${d.origin && d.origin.basis ? `\n${d.origin.basis}` : ''}`);

  legs.forEach((leg, i) => {
    const stop = stops[leg.to];
    if (!stop) return;
    const next = legs[i + 1];
    const ops = operatorsAt(stop.name);
    const who = ops.length ? `\nRun by: ${ops.join(', ')}` : '';
    if (!next) {
      push(stop, stop.kind === 'seaport' ? 'port' : 'transfer', `${stop.name}: arrives by ${word(leg.mode)}`,
        `Where this route ends.${who}`);
    } else if (next.mode !== leg.mode) {
      const loading = d.port && d.port.name && stop.name.startsWith(d.port.name);
      push(stop, stop.kind === 'seaport' ? 'port' : 'transfer', `${stop.name}: ${word(leg.mode)} → ${word(next.mode)}`,
        `${loading ? 'Port of loading. ' : ''}Changes from ${word(leg.mode)} to ${word(next.mode)}.${who}`
        + `${loading && d.port.basis ? `\n${d.port.basis}` : ''}`);
    } else {
      push(stop, 'via', stop.name, `Passes ${stop.name} by ${word(leg.mode)}.`);
    }
  });

  const dest = d.destination;
  const last = stops[legs[legs.length - 1].to];
  const onward = [];
  if (dest && dest.lat != null && dest.lon != null && last) {
    points.push({ lat: dest.lat, lon: dest.lon, role: 'dest', label: short(dest.site),
      title: `Receives the goods.${dest.basis ? `\n${dest.basis}` : ''}` });
    onward.push([[last.lon, last.lat], [dest.lon, dest.lat]]);
  }
  return { route_id: r.route_id, points, onward };
}

/* Carry the route AND the as-of across to the operational pages.
 *
 * Dropping the as-of would open the playbook at a different instant from the
 * board that sent you there — the same trap the detail panel already hit
 * once, where a header and a summary described two different moments. */
function linkOps(routeId) {
  const p = state.params || {};
  const query = new URLSearchParams({
    route: routeId,
    as_of: p.as_of || DEFAULT_AS_OF,
    shipments: String(p.shipments || 150),
  });
  const link = $('link-route');
  if (link) link.href = `/route/${encodeURIComponent(routeId)}?${query}`;
  // Where to act, one place: the Action decision tree, in its own window.
  if ($('link-tree')) $('link-tree').href = `/tree?${query}`;
}

function renderDetail(r) {
  $('panel-list').hidden = true;
  $('panel-body').hidden = false;

  const c = LEVEL_COLOR[r.level];
  const chip = $('d-chip');
  chip.textContent = r.level_label;
  chip.style.color = c;
  chip.className = `level-chip level-${r.level}`;
  // The level's rule is the tooltip; the panel states ONE deadline, the
  // real one, in the first tile.
  chip.title = `${r.level_label}: ${r.directive.toLowerCase()}${r.reason ? `\n${r.reason}` : ''}`;

  $('d-name').textContent = r.name;
  renderContract(r);
  // The week-ahead sign in one line; the full sentence is the tooltip.
  const ew = r.early_warning;
  $('d-ew').hidden = !ew;
  if (ew) {
    $('d-ew').innerHTML = `⚡ <b>Early warning:</b> ${ew.orders} small orders in one day (usual ${ew.usual}) · ${esc(fmtDay(ew.day))}${ew.synthetic ? ' <span class="pill">sample</span>' : ''}`;
    $('d-ew').title = ew.sentence;
  }

  // Critical with money at stake and no option left is not "no deadline":
  // the one thing left is telling the customer, and that is due now.
  const stuck = r.lead_time_hours == null && r.level === 'red' && r.exposure_chf > 0 && !r.actions.length;
  // The deadline as the calm clock: counting down, never red. The level
  // chip above already says how urgent; the clock only says how long.
  const due = r.clock_hours != null && state.board && window.DeadlineClock
    ? new Date(Date.parse(state.board.as_of) + r.clock_hours * 3.6e6).toISOString() : null;
  const by = r.lead_time_hours != null
    ? [due ? DeadlineClock.html(due) : hours(r.lead_time_hours),
      clockNote(r.lead_time_hours, r.clock_hours) || (due ? `closes ${DeadlineClock.at(due)}` : 'first option closes')]
    : stuck ? ['now', 'tell the customer'] : ['no deadline', 'nothing closes'];
  $('d-stats').innerHTML = `
    <div class="stat">
      <div class="stat-k">Action by</div>
      <div class="stat-v stat-v--clock">${by[0]}</div>
      <div class="stat-sub">${by[1]}</div>
    </div>
    <div class="stat">
      <div class="stat-k">Exposure</div>
      <div class="stat-v">${chf(r.exposure_chf)}</div>
      <div class="stat-sub">${r.shipments_at_risk} of ${r.shipments} shipments</div>
    </div>
    <div class="stat" id="d-stat-opt">
      <div class="stat-k">Options</div>
      <div class="stat-v">${r.actions.length}</div>
      <div class="stat-sub">worth doing</div>
    </div>`;
  $('d-next').hidden = !r.actions.length && r.level === 'green';

  renderAlloc(r);
  renderShips(r);

  // No radar, no event list, no matrix here. They were on this panel AND on
  // the route page AND in a modal — three copies of three charts, none big
  // enough to read. One copy, on /route/<id>, which is a page you can send.
  $('panel').scrollTop = 0;
}

/* Who the route is for. The review said it was unclear how things were
 * allocated to shipping points and customers, so it is said outright: the
 * site, the port, and every customer with what is at risk, key accounts
 * first. */
function renderAlloc(r) {
  const site = r.site || {};
  const port = r.real_data && r.real_data.port && r.real_data.port.name;
  const customers = r.customers || [];
  const keys = customers.filter((c) => c.priority === 'A').length;
  $('d-alloc').innerHTML =
    `<span>From <b>${esc(site.name || '—')}</b></span>
     ${port ? `<span>via <b>${esc(port)}</b></span>` : ''}
     <span><b>${customers.length}</b> customer${customers.length === 1 ? '' : 's'}${
       keys ? `, <b>★ ${keys}</b> key account${keys === 1 ? '' : 's'}` : ''}</span>`;
  const rows = customers.map((c) => `
    <tr class="${c.at_risk ? 'is-risk' : ''}" data-cust="${esc(c.name)}" tabindex="0" title="Click for ${esc(c.name)}'s card">
      <td>${esc(c.name)}</td>
      <td>${priBadge(c.priority)}</td>
      <td class="num">${c.at_risk} of ${c.shipments}</td>
      <td class="num">${c.expected_loss_chf > 0 ? chf(c.expected_loss_chf) : '—'}</td>
      <td class="cust-more"><button type="button" class="icon-btn icon-btn--mini" aria-label="Details of ${esc(c.name)}">ⓘ</button></td>
    </tr>`).join('');
  $('d-cust').innerHTML = customers.length ? `<h4 class="rp-h">Customers <span class="muted">· click one for its card</span></h4>
    <table class="alloc-tab">
      <thead><tr><th>Customer</th><th>Importance</th><th>At risk</th><th>If nobody acts</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>` : '';
  $('d-cust').querySelectorAll('tr[data-cust]').forEach((tr) => {
    const open = (e) => { e.stopPropagation(); showCustomer(r, tr.dataset.cust, tr); };
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(e); });
  });
}

/* One customer, in a small see-through card beside its row: who they are to
 * us, what a late delivery costs them and us, their orders on this route. */
const IMPACT_WORDS = {
  line_down: 'their line stops if late', stock_out: 'they run out if late',
  inconvenience: 'an inconvenience if late',
};
function showCustomer(r, name, anchor) {
  const card = $('cust-card');
  const c = (r.customers || []).find((x) => x.name === name);
  if (!card || !c) return;
  const clause = (r.clauses || []).find((x) => x.customer === name) || {};
  const tier = ((state.board && state.board.priorities) || {})[c.priority] || {};
  const counted = !!(state.board.penalties && state.board.penalties.enabled);
  const chips = (c.orders || []).map((id) => `<button type="button" class="dt-order${
    (c.orders_at_risk || []).includes(id) ? ' is-risk' : ''}" data-order="${esc(id)}">${esc(id)}</button>`).join('');
  card.innerHTML = `
    <div class="cc2-head">
      <b>${esc(c.name)}</b>${priBadge(c.priority)}
      <button type="button" class="icon-btn icon-btn--mini cc2-x" aria-label="Close">✕</button>
    </div>
    <p class="cc2-type">${esc(clause.label || 'Customer')} · ${esc(IMPACT_WORDS[c.impact] || '')}</p>
    <div class="cc2-nums">
      <div><span>Orders here</span><b>${c.shipments}</b></div>
      <div><span>At risk</span><b>${c.at_risk}</b></div>
      <div><span>If nobody acts</span><b>${c.expected_loss_chf > 0 ? chf(c.expected_loss_chf) : '—'}</b></div>
    </div>
    <p class="cc2-row" title="${esc(clause.clause || '')}"><span>Delay clause</span> ${esc(clause.summary || 'not on file')}
      <em>${counted ? 'counted' : 'not counted'}</em></p>
    ${tier.rule ? `<p class="cc2-row"><span>${esc(tier.label || '')}</span> ${esc(tier.rule)}</p>` : ''}
    <div class="dt-orders">${chips}</div>`;
  card.hidden = false;
  const box = anchor.getBoundingClientRect();
  const w = Math.min(340, window.innerWidth - 24);
  card.style.width = `${w}px`;
  card.style.left = `${Math.max(12, Math.min(box.left - w - 12, window.innerWidth - w - 12))}px`;
  card.style.top = `${Math.max(12, Math.min(box.top - 20, window.innerHeight - card.offsetHeight - 12))}px`;
  card.querySelector('.cc2-x').addEventListener('click', () => { card.hidden = true; });
  card.querySelectorAll('[data-order]').forEach((b) => b.addEventListener('click', () => {
    card.hidden = true;
    planRecovery(b.dataset.order);
  }));
}

function planRecovery(id) {
  state.shipFocus = id;
  withAgent((agent) => {
    agent.setView('map');
    agent.selectAsset(id);
  });
}

/* The route's shipments, from the map's own asset list, so the list here
 * and the icons there are the same freight. Recovery opens from a row. */
function renderShips(r) {
  const host = $('d-ships');
  if (!host) return;
  const agent = window.MapAgent;
  const st = agent && agent.getState();
  if (!st || st.assets.status !== 'ready') {
    host.innerHTML = '<p class="muted dship-note">Loading this route\'s shipments…</p>';
    return;
  }
  const order = { red: 0, yellow: 1, green: 2 };
  const rank = { A: 0, B: 1, C: 2 };
  const items = st.assets.items.filter((a) => a.lane_id === r.route_id)
    .sort((a, b) => order[a.status] - order[b.status]
      || rank[a.customer_priority || 'B'] - rank[b.customer_priority || 'B']
      || b.delay_hours - a.delay_hours);
  if (!items.length) {
    host.innerHTML = '<p class="muted dship-note">No shipment on this route is under way or staged.</p>';
    return;
  }
  host.innerHTML = `<h4>Shipments on this route <span class="muted">· ${items.length}</span></h4>
    <div class="dship-list">${items.map((a) => `
      <div class="dship-row${a.id === state.shipFocus ? ' is-focus' : ''}${a.status !== 'green' ? ' is-late' : ''}" data-ship="${esc(a.id)}">
        <span class="dship-main">
          <b>${esc(a.id)}</b> · ${esc(a.customer)} ${priBadge(a.customer_priority)}
          <span class="muted"><span class="dship-status">${esc(a.status_label)}</span> · ${esc(a.leg)}</span>
        </span>
        <button type="button" class="ctl ctl--mini${a.status !== 'green' ? ' ctl--primary' : ''}" data-plan="${esc(a.id)}">
          ${a.status !== 'green' ? 'Plan recovery' : 'Show'}</button>
      </div>`).join('')}</div>`;
  host.querySelectorAll('[data-plan]').forEach((b) => b.addEventListener('click', () => {
    host.querySelectorAll('.dship-row').forEach((row) => row.classList.toggle('is-focus', row.dataset.ship === b.dataset.plan));
    planRecovery(b.dataset.plan);
  }));
}

/* Keep the panel in step with the map.
 *
 * A vehicle opened on the map (one click, its Action Hub) also opens its
 * route here, with that shipment marked, so the map and the panel describe
 * the same thing. And the open route's shipment list follows the assets. */
withAgent((agent) => {
  agent.subscribe((st, prev) => {
    if (!state.board) return;
    const id = st.selection.id;
    if (id && id !== (prev && prev.selection.id)) {
      const a = st.assets.byId[id];
      if (a && state.board.routes.some((r) => r.route_id === a.lane_id)) {
        if (state.selected !== a.lane_id || $('panel-body').hidden) {
          openPanelTab('routes');
          select(a.lane_id, { fromMap: true, ship: id });
        } else {
          state.shipFocus = id;
          renderShips(state.board.routes.find((r) => r.route_id === a.lane_id));
        }
      }
    }
    if (st.assets.items !== (prev && prev.assets.items) && state.selected && !$('panel-body').hidden) {
      renderShips(state.board.routes.find((r) => r.route_id === state.selected));
    }
  });
});

// ===============================================================
// Radar — hand-drawn SVG
// ===============================================================
/* Families as spokes, three ordered severity bands as overlaid polygons.
 * Spokes are labelled in DAYS OF DELAY, not abstract weights, so a planner
 * reads "low water 4 days" rather than "variable 47: 0.63".
 */
/* Parameterised so the same chart can be drawn small in the side panel and
 * large in the event modal. The panel version is a glance; the modal version
 * is the one somebody actually reads a ten-spoke polygon off, and at 340px
 * wide that is not possible. Same data, same code, two sizes. */


// ===============================================================
// Events & actions
// ===============================================================

// ===============================================================
// Response workspace
// ===============================================================
/* The third failure the client named, in their own words:
 *   "Even when a crisis is identified, it is unclear what to do and who to
 *    involve."
 * The brief says it is the one most teams skip and worth the most, so it gets
 * its own pane beside the ranked table rather than a footnote under the map.
 */
function renderResponse(r) {
  /* No header of its own any more. It used to repeat the level chip, the
   * route name and the directive — which were already three lines higher up
   * the same column, in the panel head. Two copies of the same three facts,
   * a hand-span apart, was the clearest case of the "everything shows twice"
   * complaint on this board. */
  renderRActions(r);
  renderRContacts(r);
  renderREscalate(r);
  primeCompose(r);
}

/* THE ACTION TAB, AS A DECISION TREE (engine/export/decision.py).
 *
 * The questions in the order a planner asks them: what is happening, who is
 * hit, can we still keep the promised dates (and which way), if not what
 * reduces the damage, and who needs to know. A click on Yes or No shows that
 * branch; a click on an option opens it, with its path on the map; "Compare
 * routes" lays the ways to move the freight side by side. Every order at risk
 * sits in exactly one branch. */
let decisionSeq = 0;
state.dt = { view: 'tree', branch: null, open: null };

async function renderRActions(r) {
  const host = $('r-actions');
  const seq = ++decisionSeq;
  state.dt = { view: state.dt.view || 'tree', branch: null, open: null };
  host.innerHTML = '<p class="dt-loading">Working out the options…</p>';
  const p = state.params || {};
  const q = new URLSearchParams({ as_of: p.as_of || DEFAULT_AS_OF, shipments: String(p.shipments || 150) });
  let d;
  try {
    const res = await fetch(`/api/decision/${encodeURIComponent(r.route_id)}?${q}`);
    if (!res.ok) throw new Error(`the server answered ${res.status}`);
    d = await res.json();
  } catch (err) {
    if (seq === decisionSeq) host.innerHTML = `<div class="response-empty">Could not work out the options: ${esc(err.message)}</div>`;
    return;
  }
  if (seq !== decisionSeq || state.selected !== r.route_id) return;
  state.decision = d;
  const k = d.keep.answer;
  state.dt.branch = k === 'no' ? 'no' : 'yes';
  drawDecision(r, d);
  optionsTile(d);
}

/* The Options tile above the tabs says what the tree says: how many ways
 * keep the date, or else how many reduce the damage. */
function optionsTile(d) {
  const tile = $('d-stat-opt');
  if (!tile) return;
  const keep = d.keep.options.length;
  const reduce = d.reduce.options.length;
  const [n, words] = !d.hit.need ? [0, 'nothing to decide']
    : keep ? [keep, keep === 1 ? 'way keeps the date' : 'ways keep the date']
      : reduce ? [reduce, reduce === 1 ? 'way cuts the damage' : 'ways cut the damage']
        : [0, 'tell the customer'];
  tile.querySelector('.stat-v').textContent = n;
  tile.querySelector('.stat-sub').textContent = words;
}

const cleanLabel = (l) => String(l || '').replace(/\s*\+\d+ more$/, '');
// The decision tree page, opened on one option.
function treeLink(optionId) {
  const base = ($('link-tree') && $('link-tree').getAttribute('href')) || '/tree';
  return `${base}${base.includes('?') ? '&' : '?'}opt=${encodeURIComponent(optionId)}`;
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;
function pathWords(path) {
  return (path || []).map((x) => esc(x.name)).join(' <span class="dt-arrow">→</span> ');
}
function extra(chf_) { return chf_ > 0 ? `+${chf(chf_)}` : 'no extra cost'; }
function lateWords(days) {
  if (days == null) return '';
  if (days < 0.05) return 'on time';
  return days < 1 ? `late by ${Math.round(days * 24)} h` : `late by ${days.toFixed(1)} days`;
}

function drawDecision(r, d) {
  const host = $('r-actions');
  document.querySelectorAll('.dt-view').forEach((b) => b.classList.toggle('is-on', b.dataset.view === state.dt.view));
  host.innerHTML = state.dt.view === 'compare' ? compareHTML(r, d) : treeHTML(r, d);
  wireDecision(r, d);
}

function treeHTML(r, d) {
  const hit = d.hit;
  const keep = d.keep;
  const noCount = hit.need - keep.kept.length;
  const happening = (d.happening || []).map((e) => `
      <li title="${esc(e.title)}"><span>${esc(e.title)}</span>${e.kind ? `<span class="dt-tag" title="${esc(e.meaning || '')}">${esc(e.kind)}</span>` : ''}</li>`).join('');

  const root = `
    <div class="dt-node dt-root">
      <span class="dt-step">1</span>
      <div class="dt-body"><b class="dt-h">What is happening</b>
        ${happening ? `<ul class="dt-list">${happening}</ul>` : '<p class="dt-sub">Nothing reaches this route.</p>'}</div>
    </div>`;
  const who = `
    <div class="dt-node">
      <span class="dt-step">2</span>
      <div class="dt-body"><b class="dt-h">Who is hit</b>
        <p class="dt-sub"><b>${hit.orders} of ${hit.of}</b> orders on this route${hit.exposure_chf ? ` · ${chf(hit.exposure_chf)} exposure` : ''}</p>
        <div class="dt-split">
          <span class="dt-pill dt-pill--need"><i></i>${hit.need === 1 ? '1 order needs' : `${hit.need} orders need`} a decision</span>
          ${hit.absorbed ? `<span class="dt-pill"><i></i>${hit.absorbed} arrive on time anyway</span>` : ''}
        </div></div>
    </div>`;
  if (!hit.need) {
    return `<div class="dt">${root}${who}
      <div class="dt-node dt-end"><span class="dt-step">✓</span>
        <div class="dt-body"><b class="dt-h">Nothing to decide</b>
          <p class="dt-sub">No order on this route is expected to miss its date.</p></div></div></div>`;
  }

  const yesOn = state.dt.branch === 'yes';
  const question = `
    <div class="dt-node dt-q">
      <span class="dt-step">3</span>
      <div class="dt-body"><b class="dt-h">Can we still keep the promised dates?</b>
        <div class="dt-choice" role="group">
          <button type="button" class="dt-yes${yesOn ? ' is-on' : ''}" data-branch="yes" ${keep.kept.length ? '' : 'disabled'}>
            Yes <span>${keep.kept.length ? `for ${plural(keep.kept.length, 'order')}` : 'no way found'}</span></button>
          <button type="button" class="dt-no${yesOn ? '' : ' is-on'}" data-branch="no" ${noCount ? '' : 'disabled'}>
            No <span>${noCount ? `for ${plural(noCount, 'order')}` : 'every order is covered'}</span></button>
        </div></div>
    </div>`;

  const branch = yesOn ? yesHTML(r, d) : noHTML(r, d);
  // Steps are numbered in the order they are read: the No branch has one
  // step for "reduce the damage" and one for "tell the customer", if any.
  const last = yesOn ? 4 : 3 + (d.reduce.options.length ? 1 : 0) + (d.tell.orders.length ? 1 : 0);
  const esc_ = d.escalation || {};
  const end = `
    <div class="dt-node dt-end">
      <span class="dt-step">${last + 1}</span>
      <div class="dt-body"><b class="dt-h">Who needs to know</b>
        <p class="dt-sub">${esc_.level != null ? `Escalation level ${esc(esc_.level)}` : 'Escalation'}${
          esc_.notify && esc_.notify.length ? ` · tell <b>${esc_.notify.map(esc).join(', ')}</b>` : ''}${
          esc_.acknowledge_within_hours ? ` · they answer within ${esc_.acknowledge_within_hours} h` : ''}</p>
        <button type="button" class="ctl ctl--mini" id="dt-write-open">Write the summary ▸</button></div>
    </div>`;
  return `<div class="dt">${root}${who}${question}<div class="dt-branch dt-branch--${yesOn ? 'yes' : 'no'}">${branch}</div>${end}</div>`;
}

/* When a route's first option closes, on the clock. */
function dueAt(r) {
  return new Date(Date.parse(state.board.as_of) + r.clock_hours * 3.6e6).toISOString();
}

/* How long an option stays open, as the calm clock (clock.js). */
function closesHTML(iso) {
  if (!iso || !window.DeadlineClock) return '';
  return `<span class="dt-closes"><span class="dt-closes-k">closes in</span> ${DeadlineClock.html(iso)}</span>`;
}

function optionHTML(o, body) {
  const open = state.dt.open === o.id;
  return `<li class="dt-opt${o.best ? ' is-best' : ''}${open ? ' is-open' : ''}" data-opt="${esc(o.id)}">
      <button type="button" class="dt-opt-head" aria-expanded="${open}">
        <span class="dt-opt-name">${o.best ? '<span class="dt-star" title="Best: keeps the most orders on time, soonest, for the least">★</span>' : ''}${esc(cleanLabel(o.label))}</span>
        <span class="dt-opt-sum">${o.sum}${closesHTML(o.closes_at)}</span>
        <span class="dt-opt-caret" aria-hidden="true">▸</span>
      </button>
      <div class="dt-opt-body"${open ? '' : ' hidden'}>${body}</div>
    </li>`;
}

function routeBody(r, o) {
  const orders = (o.shipment_ids || []).map((id) => `<span class="dt-order">${esc(id)}</span>`).join('');
  // A new route has a path to draw; a playbook action (divert a road leg)
  // has a description instead.
  const drawable = (o.path || []).length >= 2;
  return `
    <p class="dt-path">${drawable ? pathWords(o.path) : esc(o.detail || '')}</p>
    ${o.capacity_note ? `<p class="dt-warn">${esc(o.capacity_note)}</p>` : ''}
    <div class="dt-orders">${orders}</div>
    <div class="dt-acts">
      ${drawable ? `<button type="button" class="ctl ctl--mini" data-map="${esc(o.id)}">Show on map</button>` : ''}
      <a class="ctl ctl--mini ctl--primary" href="${esc(treeLink(o.id))}" target="_blank" rel="noopener">Open in the decision tree ↗</a>
    </div>`;
}

function yesHTML(r, d) {
  const opts = d.keep.options.map((o) => optionHTML({ ...o,
    sum: `${o.on_time} of ${o.orders} on time · ${extra(o.cost_chf)} · starts in ${hours(o.starts_in_h)}` },
  routeBody(r, o))).join('');
  const stay = (d.compare || []).find((x) => x.baseline);
  const cheapest = Math.min(...d.keep.options.map((o) => o.cost_chf));
  return `
    <div class="dt-node dt-q">
      <span class="dt-step">4</span>
      <div class="dt-body"><b class="dt-h">Which way keeps the date?</b>
        ${d.keep.stay_best ? `<p class="dt-sub"><b>★ Best: stay as planned.</b> Every order is likely on time, and lateness would cost
          ${chf(stay && stay.exposure_chf)} in expectation, less than the cheapest way (${chf(cheapest)}).</p>` : ''}
        <p class="dt-sub">Click one to open it; <button type="button" class="dt-link" data-view-to="compare">compare the routes</button> side by side.</p></div>
    </div>
    <ul class="dt-opts">${opts}</ul>
    ${d.keep.more ? `<p class="dt-more">${plural(d.keep.more, 'other way')} checked: slower, dearer or late.</p>` : ''}`;
}

function noHTML(r, d) {
  const red = d.reduce.options.map((g) => {
    if (g.route) {
      return optionHTML({ ...g,
        sum: `${plural(g.orders_n, 'order')} · ${lateWords(g.late_after_days)} · ${extra(g.cost_chf)}` }, routeBody(r, g));
    }
    const orders = g.orders.map((o) => `<span class="dt-order" title="${esc(o.customer)}">${o.priority === 'A' ? '★ ' : ''}${esc(o.shipment_id)} · ${esc(o.customer)}</span>`).join('');
    return optionHTML({ id: `act:${g.label}`, label: g.label, best: false, closes_at: g.closes_at,
      sum: `${plural(g.orders.length, 'order')} · ${g.cost_chf > 0 ? `costs ${chf(g.cost_chf)}` : 'free'} · saves ${chf(g.saves_chf)} · decide ${inHours(g.decide_in_h)}` }, `
      <div class="dt-orders">${orders}</div>
      <p class="dt-sub">Takes ${Math.round(g.takes_h || 0)} h · ${g.owner === 'us' ? 'we can do this' : `${esc(g.owner || 'the carrier')} does this`}. "Saves" is the expected cost of lateness it avoids.</p>
      <div class="dt-acts"><a class="ctl ctl--mini ctl--primary" href="${esc(treeLink(`act:${g.label}`))}" target="_blank" rel="noopener">Open in the decision tree ↗</a></div>`);
  }).join('');
  const tell = d.tell.orders;
  const leaf = tell.length ? `
    <div class="dt-node dt-leaf">
      <span class="dt-step">✉</span>
      <div class="dt-body"><b class="dt-h">Tell the customer and agree a new date</b>
        <p class="dt-sub">No way left keeps ${tell.length === 1 ? 'this order' : 'these orders'} on time.</p>
        <div class="dt-orders">${tell.map((o) => `<span class="dt-order">${esc(o.shipment_id)} · ${esc(o.customer)} · ${
          o.late_days >= 0.5 ? lateWords(o.late_days) : `${Math.round(o.p_late * 100)}% chance late`}</span>`).join('')}</div></div>
    </div>` : '';
  return `
    ${red ? `
    <div class="dt-node dt-q">
      <span class="dt-step">4</span>
      <div class="dt-body"><b class="dt-h">What reduces the damage?</b>
        <p class="dt-sub">These do not make the date, but cut what being late costs.</p></div>
    </div>
    <ul class="dt-opts">${red}</ul>` : ''}
    ${leaf ? leaf.replace('<span class="dt-step">✉</span>', `<span class="dt-step">${red ? 5 : 4}</span>`) : ''}`;
}

function compareHTML(r, d) {
  const rows = d.compare || [];
  if (rows.length < 2) {
    return `<p class="dt-sub dt-compare-empty">No other way to move this freight was found: there is nothing to compare.
      <button type="button" class="dt-link" data-view-to="tree">Back to the tree</button></p>`;
  }
  const best = rows.find((x) => x.best);
  const body = rows.map((x) => {
    const share = x.orders ? x.on_time / x.orders : 0;
    return `<tr class="${x.baseline ? 'is-base' : ''}${x.best ? ' is-best' : ''}" data-map="${esc(x.id)}" tabindex="0"
        title="${esc((x.path || []).map((p) => p.name).join(' → '))}">
      <td><b>${x.best ? '★ ' : ''}${esc(cleanLabel(x.label))}</b><span class="dt-cpath">${(x.path || []).length >= 2 ? pathWords(x.path) : esc(x.detail || '')}</span></td>
      <td class="num"><span class="dt-meter"><span style="width:${Math.round(share * 100)}%"></span></span>${x.on_time} of ${x.orders}</td>
      <td class="num">${x.baseline ? (x.late_after_days >= 0.05 ? `up to ${x.late_after_days.toFixed(1)} d` : 'on time') : lateWords(x.late_after_days)}</td>
      <td class="num">${x.baseline ? '—' : extra(x.cost_chf)}</td>
      <td class="num">${x.baseline ? '—' : hours(x.starts_in_h)}</td>
    </tr>`;
  }).join('');
  return `
    <div class="dt-compare">
      <table class="dt-ctable">
        <thead><tr><th>Route</th><th class="num">On time</th><th class="num">Late by</th><th class="num">Extra cost</th><th class="num">Starts in</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
      <p class="dt-sub">${best ? `★ ${esc(cleanLabel(best.label))} keeps ${best.on_time} of ${best.orders} on time. ` : ''}Click a row to see it on the map.
        "Stay as planned" is the route today${rows[0].exposure_chf ? `, with ${chf(rows[0].exposure_chf)} exposure` : ''}.</p>
    </div>`;
}

/* Draw one way to move the freight on the map, over the route's own line;
 * "Stay as planned" puts the route's journey back. */
function showOption(r, d, id) {
  if (!window.MapJourney) return;
  const all = [...(d.keep.options || []), ...(d.reduce.options || []).filter((g) => g.route)];
  const o = all.find((x) => x.id === id);
  if (!o) {
    const spec = journeySpec(r);
    if (spec) { spec.fit = true; window.MapJourney.show(spec); }
    return;
  }
  const path = o.path || [];
  const points = path.filter((x) => x.lat != null).map((x, i, arr) => ({
    lat: x.lat, lon: x.lon,
    role: i === 0 ? 'origin' : i === arr.length - 1 ? 'dest' : (x.kind === 'seaport' ? 'port' : 'via'),
    label: i === arr.length - 1 ? `${x.name}: new route` : x.name,
    title: `${cleanLabel(o.label)}: ${o.on_time != null ? `${o.on_time} of ${o.orders} on time` : ''}`,
  }));
  window.MapJourney.show({ route_id: `${r.route_id}#${o.id}`, points, onward: o.line && o.line.length ? [o.line] : [], fit: true });
}

function wireDecision(r, d) {
  const host = $('r-actions');
  host.querySelectorAll('[data-branch]').forEach((b) => b.addEventListener('click', () => {
    state.dt.branch = b.dataset.branch;
    state.dt.open = null;
    drawDecision(r, d);
  }));
  host.querySelectorAll('.dt-opt-head').forEach((b) => b.addEventListener('click', () => {
    const id = b.closest('.dt-opt').dataset.opt;
    state.dt.open = state.dt.open === id ? null : id;
    drawDecision(r, d);
    const opened = [...(d.keep.options || []), ...(d.reduce.options || [])].find((x) => x.id === id);
    if (state.dt.open && opened && (opened.path || []).length >= 2) showOption(r, d, id);
  }));
  host.querySelectorAll('button[data-map], tr[data-map]').forEach((el) => {
    const go = () => showOption(r, d, el.dataset.map);
    el.addEventListener('click', go);
    if (el.tagName === 'TR') el.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  });
  host.querySelectorAll('[data-view-to]').forEach((b) => b.addEventListener('click', () => {
    state.dt.view = b.dataset.viewTo;
    drawDecision(r, d);
  }));
  const write = $('dt-write-open');
  if (write) write.addEventListener('click', () => {
    const box = $('dt-write');
    box.open = true;
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function renderRContacts(r) {
  const resp = r.response || {};
  const out = [];

  const card = (p) => `
    <div class="contact" title="${esc(p.why || '')}">
      <div class="contact-top"><span class="contact-name">${esc(plainName(p.name))}</span></div>
      <div class="contact-role">${esc(p.role)}</div>
      <div class="contact-links">
        ${p.email ? `<a href="mailto:${esc(p.email)}">${esc(p.email)}</a>` : ''}
        ${p.phone ? `<span>${esc(p.phone)}</span>` : ''}
        ${p.meta && p.meta.hours ? `<span>${esc(p.meta.hours)}</span>` : ''}
      </div>
    </div>`;

  const group = (title, note, items) => {
    if (!items || !items.length) return '';
    return `<div class="cgroup">
      <div class="cgroup-head"><span>${esc(title)}</span>
        ${note ? `<span>${esc(note)}</span>` : ''}</div>
      ${items.map(card).join('')}
    </div>`;
  };

  const people = [resp.route_manager, ...(resp.standing_teams || []), ...(resp.seniors || [])].filter(Boolean);
  if (people.some((p) => /\(synthetic/i.test(p.name || ''))) {
    out.push('<p class="synthetic-note">Stand-in contacts until Sika\'s own are loaded.</p>');
  }
  if (resp.route_manager) out.push(group('Route manager', '', [resp.route_manager]));
  out.push(group('Teams to convene', r.level_label, resp.standing_teams));
  out.push(group('Seniors', '', resp.seniors));
  out.push(group('Vendors on this route', '', resp.vendors));
  out.push(group('Carriers', '', resp.carriers));

  // Declared alternatives. An empty list means "none configured" and is shown
  // as exactly that — never a silent zero, and never a claim that no
  // alternative exists in the world.
  const routing = resp.alternate_routing || [];
  out.push(`<div class="cgroup">
    <div class="cgroup-head"><span>Alternate routing</span></div>
    ${routing.length
      ? routing.map((x) => `<div class="altroute">
          <b>${esc(x.node_name)}</b> → ${x.alternatives.map((a) => esc(a.name)).join(', ')}
        </div>`).join('')
      : '<div class="altroute">None configured for this route.</div>'}
  </div>`);

  $('r-contacts').innerHTML = out.join('');
}

function renderREscalate(r) {
  const resp = r.response || {};
  const step = resp.escalation || {};
  const c = LEVEL_COLOR[r.level];
  let html = `
    <div class="esc-card" style="border-left-color:${c}" title="${esc(step.note || '')}">
      <div class="esc-level" style="color:${c}">Escalation level ${esc(step.level ?? '—')}</div>
      <ul class="esc-list">
        ${step.notify && step.notify.length ? `<li>Notify <b>${step.notify.map(esc).join(', ')}</b></li>` : ''}
        ${step.acknowledge_within_hours ? `<li>Acknowledge within ${step.acknowledge_within_hours} h</li>` : ''}
      </ul>
    </div>`;
  if (resp.approval) {
    const a = resp.approval;
    html += `<div class="esc-card" style="border-left-color:${LEVEL_COLOR.yellow}" title="${esc(a.note)}">
      <div class="esc-level" style="color:${LEVEL_COLOR.yellow}">Needs approval</div>
      <ul class="esc-list">
        <li>${esc(a.approver)} releases spend above ${chf(a.limit_chf)}</li>
        <li>This option: ${chf(a.cost_chf)}</li>
      </ul>
    </div>`;
  }
  $('r-escalate').innerHTML = html;
}

/* Compose. The app produces the draft and hands it over; it does not send.
 * Sending is outward-facing and no mail path is wired, so faking it would be
 * claiming a delivery that never happened. */
async function primeCompose(r) {
  const resp = r.response || {};
  const recipients = []
    .concat(resp.route_manager ? [resp.route_manager] : [])
    .concat(resp.standing_teams || [])
    .concat(resp.seniors || [])
    .map((p) => p.email)
    .filter(Boolean);

  $('send-to').value = recipients.join(', ');
  $('send-subject').value = `[${r.level_label}] ${r.name}: action by ${hours(r.lead_time_hours)}`;
  $('send-body').value = 'loading summary…';

  const params = new URLSearchParams(state.params || readParams());
  const base = `/api/report/${encodeURIComponent(r.route_id)}`;
  $('send-pdf').href = `${base}.pdf?${params}`;

  try {
    const text = await fetch(`${base}.txt?${params}`).then((res) => res.text());
    $('send-body').value = text;
    $('send-mail').href =
      `mailto:${encodeURIComponent($('send-to').value)}` +
      `?subject=${encodeURIComponent($('send-subject').value)}` +
      `&body=${encodeURIComponent(text)}`;
  } catch (err) {
    $('send-body').value = `could not build the summary: ${err.message}`;
  }
}

function initResponseTabs() {
  const back = $('d-back');
  // Back to the overview: the journey markers go with the route.
  if (back) back.addEventListener('click', () => showRouteList());
  // The Action tab's two views: the decision tree, and the routes compared.
  document.querySelectorAll('.dt-view').forEach((b) => b.addEventListener('click', () => {
    state.dt.view = b.dataset.view;
    const r = state.board && state.board.routes.find((x) => x.route_id === state.selected);
    if (r && state.decision && state.decision.route_id === r.route_id) drawDecision(r, state.decision);
  }));

  document.querySelectorAll('.rtab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.rtab').forEach((t) => t.classList.remove('is-on'));
      document.querySelectorAll('.rpanel').forEach((p) => p.classList.remove('is-on'));
      tab.classList.add('is-on');
      document.querySelector(`.rpanel[data-panel="${tab.dataset.tab}"]`)
        .classList.add('is-on');
    });
  });

  $('send-copy').addEventListener('click', async () => {
    const btn = $('send-copy');
    try {
      await navigator.clipboard.writeText($('send-body').value);
      btn.textContent = 'Copied';
    } catch {
      // Clipboard needs a secure context, which a plain-http demo box is not.
      $('send-body').select();
      btn.textContent = 'Selected. Press Ctrl+C';
    }
    setTimeout(() => { btn.textContent = 'Copy'; }, 2200);
  });
}

// ===============================================================
// Ranked routes
// ===============================================================
/* The signals panel.
 *
 * Everything the filter layer did this run, in the order it did it, with the
 * rule beside each count. A number with no rule next to it is a number
 * nobody can argue with, which is the same as one nobody believes.
 */
let signalsLoaded = false;

function initPanelTabs() {
  const tabs = $('ptabs');
  if (!tabs || tabs.dataset.bound) return;
  tabs.dataset.bound = '1';
  tabs.addEventListener('click', (event) => {
    const tab = event.target.closest('.ptab');
    if (tab) openPanelTab(tab.dataset.ptab);
  });
}

function openPanelTab(name) {
  const tabs = $('ptabs');
  tabs.querySelectorAll('.ptab').forEach((t) => t.classList.toggle('is-on', t.dataset.ptab === name));
  const routes = name === 'routes';
  $('rlist').hidden = !routes;
  $('deskrow').hidden = !routes;
  if (!routes) showHowto(false);
  $('allhands').hidden = name !== 'allhands';
  $('siglist').hidden = name !== 'signals';
  $('btn-help').hidden = !routes;   // the help is about the route list
  markLevelChip();
  if ($('f-focus')) {
    $('f-focus').hidden = !routes
      || !state.board.routes.some((r) => r.real_data && r.real_data.focus);
  }
  // The subtitle belongs to the tab. Leaving "17 of 17 routes" above a
  // funnel is the kind of stale line that makes a planner distrust every
  // other number on the page.
  $('panel-list-count').textContent = {
    routes: state.listSubtitle || '',
    allhands: 'When the cross-functional team meets, and what each function can pull',
    signals: 'What came in, and what the filters kept',
  }[name];
  if (name === 'signals' && !signalsLoaded) loadSignals();
}

/* THE ALL-HANDS.
 *
 * Sika: once a crisis looms, Procurement, Supply Chain, Manufacturing and
 * Controlling go from meeting every two weeks to every day, with full
 * authority to act. So this tab is the room's agenda: the cadence the team's
 * own convene rule now calls for, who sits in it, and one card per function
 * with the lever that function holds — computed from this board, proposed,
 * never pulled. */
function renderAllHands() {
  const host = $('allhands');
  const h = state.board && state.board.all_hands;
  if (!host || !h) return;
  const tone = { convene: 'red', watch: 'yellow', normal: 'green' }[h.posture] || 'blue';
  const c = LEVEL_COLOR[tone];
  const fmt = (x) => (x.unit === 'chf' ? chf(x.value) : x.value);
  const lim = (x) => (x.limit == null ? '—' : x.unit === 'chf' ? chf(x.limit) : x.limit);

  // Two weeks at a glance: which days the team sits. Daily means working
  // days; the weekend is not a meeting day.
  const asOf = new Date(state.board.as_of);
  const next = h.next_at ? new Date(h.next_at) : null;
  const dayMs = 86400000;
  const day0 = Date.UTC(asOf.getUTCFullYear(), asOf.getUTCMonth(), asOf.getUTCDate());
  const nextDay = next ? Date.UTC(next.getUTCFullYear(), next.getUTCMonth(), next.getUTCDate()) : null;
  const cells = Array.from({ length: 14 }, (_, i) => {
    const t = day0 + i * dayMs;
    const d = new Date(t);
    const wd = d.getUTCDay();
    const k = nextDay == null ? -1 : Math.round((t - nextDay) / dayMs);
    const meets = k >= 0 && (h.cadence_days <= 1 ? wd !== 0 && wd !== 6 : k % h.cadence_days === 0);
    const label = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
    return `<span class="ah2-day${meets ? ' is-meet' : ''}${k === 0 ? ' is-next' : ''}${wd === 0 || wd === 6 ? ' is-wkend' : ''}"
      title="${esc(label)}${meets ? ': all-hands' : ''}"><i>${'SMTWTFS'[wd]}</i></span>`;
  }).join('');

  // Each limit as a tile: today's number, big; the limit under it in words;
  // how far past it, said plainly. The bar is drawn to its own scale: the
  // fill is today's number, the dark mark is the limit.
  const checks = (h.checks || []);
  const crossed = checks.filter((x) => x.crossed).length;
  const times = (x) => {
    if (!x.limit) return 'no limit set';
    const r = x.value / x.limit;
    if (x.crossed) return r >= 1.05 ? `${r.toFixed(1)}× the limit` : 'at the limit';
    return `${Math.round(r * 100)}% of the limit`;
  };
  const bars = checks.map((x) => {
    const top = Math.max(x.value, x.limit || 0) * 1.15 || 1;
    const fill = Math.min(100, (x.value / top) * 100);
    const mark = x.limit ? (x.limit / top) * 100 : null;
    return `<div class="ah2-tile${x.crossed ? ' is-crossed' : ''}"
        title="${esc(x.label)}: ${fmt(x)} today. The meeting goes daily at ${lim(x)}.">
      <span class="ah2-tl">${esc(x.label)}</span>
      <b class="ah2-tv">${fmt(x)}</b>
      <span class="ah2-track"><span class="ah2-fill" style="width:${fill}%"></span>${
        mark == null ? '' : `<span class="ah2-tick" style="left:${mark}%"></span>`}</span>
      <span class="ah2-tlim"><i class="ah2-tick-key" aria-hidden="true"></i>limit ${lim(x)}</span>
      <span class="ah2-tover">${x.crossed ? '▲ ' : ''}${times(x)}</span>
    </div>`;
  }).join('');

  // One row per function in the room: who, their lever, the one-line state.
  // The detail opens on a click.
  const levers = Object.fromEntries((h.levers || []).map((l) => [l.id, l]));
  const initials = (name) => name.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  const rows = (h.attendees || []).map((a) => {
    const l = levers[a.id] || {};
    const items = (l.items || []).map((i) => `
      <li${i.route_id ? ` data-route="${esc(i.route_id)}" tabindex="0"` : ''} title="${esc(i.hint || '')}">
        <span class="ah-text">${i.priority === 'A' ? '★ ' : ''}${esc(i.text)}</span>
        ${i.detail ? `<span class="ah-detail">${esc(i.detail)}</span>` : ''}
      </li>`).join('');
    const more = items || l.note || l.basis;
    return `<details class="ah2-fn">
      <summary title="${esc(a.role)}">
        <span class="ah2-ini">${esc(initials(a.function))}</span>
        <span class="ah2-who"><b>${esc(a.function)}</b><span class="muted">${esc(l.lever || a.role)}</span></span>
        <span class="ah2-sum">${esc(l.summary || '')}</span>
        ${more ? '<span class="ah2-open" aria-hidden="true">▸</span>' : ''}
      </summary>
      ${l.note ? `<p class="ah-note">${esc(l.note)}</p>` : ''}
      ${items ? `<ul class="ah-items">${items}</ul>` : ''}
      ${l.basis ? `<p class="ah2-basis">${esc(l.basis)}</p>` : ''}
    </details>`;
  }).join('');

  host.innerHTML = `
    <div class="ah2-hero" style="--tone:${c}">
      <div class="ah2-when">
        <div><span class="ah2-k">Meets</span><b class="ah2-big">${esc(h.cadence_label)}</b>
          ${h.changed ? `<span class="ah2-was">normally ${esc(h.normal_label)}</span>` : ''}</div>
        <div><span class="ah2-k">Next</span><b class="ah2-big ah2-next">${esc(h.next_label.replace(/ UTC$/, ''))}</b>
          <span class="ah2-was">UTC</span></div>
      </div>
      <div class="ah2-cal" aria-label="The next two weeks">${cells}</div>
      <div class="ah2-rule"><i></i>${esc(h.change)}</div>
    </div>
    ${bars ? `<section class="ah2-sec"><div class="ah2-title">Why ${esc(h.cadence_label)}
      <span class="muted">· any one limit crossed makes it daily · ${crossed} of ${checks.length} crossed now</span></div>
      <div class="ah2-tiles">${bars}</div></section>` : ''}
    <section class="ah2-sec"><div class="ah2-title">In the room <span class="muted">· click a row for its list</span></div>${keyAccountsHTML()}${rows}</section>
    <p class="ah-foot">Proposals only. Nothing is booked or approved.</p>`;
  host.querySelectorAll('li[data-route]').forEach((li) => {
    const go = () => {
      openPanelTab('routes');
      select(li.dataset.route, { fly: true, ship: li.dataset.ship || null });
    };
    li.addEventListener('click', go);
    li.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  });
}

async function loadSignals() {
  signalsLoaded = true;
  const host = $('siglist');
  host.innerHTML = '<p class="sig-note">Reading the filter layer…</p>';
  try {
    const p = state.params || {};
    const query = new URLSearchParams({
      as_of: p.as_of || DEFAULT_AS_OF,
      shipments: String(p.shipments || 150),
    });
    const data = await (await fetch(`/api/v2/signals?${query}`)).json();
    host.innerHTML = burstHTML(state.board && state.board.order_signals)
      + carrierHTML(state.board && state.board.carrier_signals) + signalsHTML(data);
  } catch (err) {
    host.innerHTML = `<p class="sig-note">Could not read it: ${esc(err.message)}</p>`;
  }
}

/* The key-account orders at risk, for the room: those contracts are kept
 * whatever the crisis, so they are read out first. */
function keyAccountsHTML() {
  const rows = (state.board && state.board.key_accounts) || [];
  if (!rows.length) return '';
  // Read out first in the room, so the first one is on the button; the list
  // opens on a click.
  const first = rows[0];
  return `<details class="ah2-fn ah-keys-card">
    <summary>
      <span class="ah2-ini ah2-ini--key">★</span>
      <span class="ah2-who"><b>Key accounts at risk: ${rows.length}</b><span class="muted">served first</span></span>
      <span class="ah2-sum">${esc(first.customer)} · ${first.lead_time_hours == null ? 'no deadline' : `decide ${inHours(first.lead_time_hours)}`}</span>
      <span class="ah2-open" aria-hidden="true">▸</span>
    </summary>
    <ul class="ah-items">${rows.map((k) => `
      <li data-route="${esc(k.route_id)}" data-ship="${esc(k.shipment_id)}" tabindex="0" title="${esc(k.route)}">
        <span class="ah-text"><b>${esc(k.customer)}</b> · ${k.lead_time_hours == null ? 'no deadline' : `decide ${inHours(k.lead_time_hours)}`}</span>
        <span class="ah-detail">${esc(k.action || 'no option worth its cost')} · order ${esc(k.shipment_id)} · ${esc(siteName(k.site))}</span>
      </li>`).join('')}</ul>
  </details>`;
}

/* Carriers pushing out orders — the signal Sika said arrives before any
 * announcement. Patterns were raised as warnings; singles are only watched,
 * so the planner can see one becoming a pattern. */
/* Bursts of small orders: Sika's week-ahead sign, per flow. */
function burstHTML(o) {
  if (!o) return '';
  const bursts = o.bursts || [];
  const row = (b) => `
    <div class="cs-row is-raised" title="${esc(`${Math.round(b.small_share * 100)}% of them smaller than usual; ${b.times}x the usual count`)}">
      <span class="cs-count">${b.orders}</span>
      <span><b>${esc(b.flow.replace('_', ' → '))}</b> · ${esc(fmtDay(b.day))}
        <span class="muted">· usual ${b.usual} a day</span></span>
    </div>`;
  return `
    <section class="cs">
      <p class="sig-head" title="Raised when ${esc(o.rule || '')}"><b>Bursts of small orders</b>
        <span class="muted">· a week ahead ⓘ</span>${o.synthetic ? ' <span class="pill">sample</span>' : ''}</p>
      ${bursts.length ? bursts.map(row).join('') : '<p class="sig-note">None in the last 10 days.</p>'}
    </section>`;
}

function fmtDay(iso) {
  const d = new Date(`${iso}T12:00:00Z`);
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
}

function carrierHTML(c) {
  if (!c) return '';
  const patterns = c.patterns || [];
  const singles = c.singles || [];
  const orders = (p) => (p.notices || []).map((n) =>
    `${n.order_id} +${Math.round(n.hours)} h${n.reason ? ` (${n.reason})` : ''}`).join('\n');
  const row = (p, raised) => `
    <div class="cs-row${raised ? ' is-raised' : ''}" title="${esc(orders(p))}">
      <span class="cs-count">${p.orders}</span>
      <span><b>${esc(p.carrier_name.replace(' (synthetic stand-in)', ''))}</b> · ${esc(p.node_name)}
        <span class="muted">· avg +${Math.round(p.mean_hours)} h</span></span>
    </div>`;
  return `
    <section class="cs">
      <p class="sig-head" title="Raised when ${esc(c.rule || 'a pattern forms')}"><b>Carrier push-outs</b>
        <span class="muted">· early warning ⓘ</span>${c.synthetic ? ' <span class="pill">synthetic</span>' : ''}</p>
      ${patterns.length ? patterns.map((p) => row(p, true)).join('') : '<p class="sig-note">None this week.</p>'}
      ${singles.length ? `<p class="cs-sub">Watching</p>${singles.map((p) => row(p, false)).join('')}` : ''}
    </section>`;
}

function signalsHTML(data) {
  const m = data.model;
  const widest = Math.max(...data.stages.map((st) => st.count), 1);
  const funnel = data.stages.map((st) => `
    <div class="sig-stage" title="${esc(st.note)}">
      <div class="sig-stage-top">
        <span>${esc(st.label)}</span>
        <span class="sig-count">${st.count}${st.removed ? `<i>−${st.removed}</i>` : ''}</span>
      </div>
      <div class="sig-bar"><span style="width:${Math.round(st.count / widest * 100)}%"></span></div>
    </div>`).join('');
  const rows = data.signals.slice(0, 40).map((row) => `
    <div class="sig-row sig-row--${esc(row.state)}" title="${esc(row.why || '')}">
      <span class="sig-state">${esc(row.state)}</span>
      <span>
        <span class="sig-title">${esc(row.title)}</span>
        ${row.source ? `<span class="sig-src">${esc(row.source)}${row.tier ? ` · tier ${row.tier}` : ''}${
          row.inferred ? ' · inferred' : ''}</span>` : ''}
      </span>
    </div>`).join('');
  const rec = m.recording && m.recording.available
    ? ` · replaying ${m.recording.entries} recorded answers` : '';
  return `
    <div class="sig-funnel">${funnel}</div>
    <div class="sig-rows">
      <p class="sig-head">${data.counts.events} kept · ${data.counts.dropped} dropped ·
        ${data.counts.unpromoted} not trusted yet</p>
      ${rows}
    </div>
    <details class="sig-tech">
      <summary>How the signals were read</summary>
      <p class="sig-model-line${m.status === 'connected' ? ' is-on' : ''}" title="${esc(m.detail)}">
        AI model: ${m.status === 'connected' ? esc(m.model) : 'none connected, rules only'}${rec}</p>
    </details>`;
}

function renderFilters() {
  const all = $('f-all');
  if (!all) return;
  all.addEventListener('click', () => setLevel(null));
  const focus = $('f-focus');
  if (!focus) return;
  // No focus routes configured, no button: a filter that always empties the
  // list is a broken control.
  focus.hidden = !(state.board && state.board.routes.some((r) => r.real_data && r.real_data.focus));
  focus.addEventListener('click', () => {
    state.focusOnly = !state.focusOnly;
    focus.classList.toggle('is-on', state.focusOnly);
    focus.setAttribute('aria-pressed', String(state.focusOnly));
    refreshPaths();
    renderTable();
    syncMapFilters();
  });
}

/* The affected routes, as the right column's default state.
 *
 * A list rather than the seven-column table it replaces. The table was a
 * fine table and the wrong shape for a column beside a globe: at that width
 * every row wrapped, and the two columns anybody actually scans — how long
 * is left, and what is hitting it — were the two that wrapped worst. Each
 * row here carries the same facts in reading order.
 */
/* Small inline icons. Drawn with currentColor, so they follow the theme. */
const ICON = {
  open: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 4l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  chart: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 16h14M6 13V8M10 13V4M14 13v-3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  clock: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M10 6v4l3 2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  chev: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 8l5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

/* Which route cards are unfolded, kept across redraws. */
const openCards = new Set();

/* WHY, in a few bullets, for the unfolded card. Built from the board's own
 * numbers: the deadline, what is hitting the route, what is at stake, which
 * key accounts, and the best option — the full view is one click on. */
function whyHTML(r) {
  const keys = (r.customers || []).filter((c) => c.priority === 'A' && c.at_risk > 0).map((c) => c.name);
  const best = (r.actions || [])[0];
  const events = (r.events || []).slice(0, 3).map((e) => `
    <li><span class="why-kind" title="${esc(e.kind_meaning || '')}">${esc(e.kind_label || e.kind || 'Event')}</span>${esc(e.title)}</li>`).join('');
  return `
    <ul class="why">
      <li class="why-deadline">${ICON.clock}<span><b>${r.lead_time_hours == null ? 'No decision due' : `Decide ${inHours(r.lead_time_hours)}`}</b>
        ${r.actions.length ? `· ${r.actions.length} option${r.actions.length === 1 ? '' : 's'} open` : '· no option worth its cost'}</span></li>
      ${events}
      <li><b>${chf(r.exposure_chf)}</b> at risk if nobody acts · ${r.shipments_at_risk} of ${r.shipments} shipments</li>
      ${keys.length ? `<li>★ <b>${keys.length} key account${keys.length === 1 ? '' : 's'}</b>: ${keys.map(esc).join(', ')}</li>` : ''}
      ${best ? `<li>Best option: <b>${esc(best.label)}</b> · net ${chf(best.value_chf)}${best.lead_time_hours == null ? '' : ` · decide ${inHours(best.lead_time_hours)}`}</li>` : ''}
    </ul>
    <div class="why-go">
      <a class="icon-btn" href="${routePageHref(r.route_id)}" title="Route page: matrix and charts" aria-label="Route page">${ICON.chart}</a>
      <button type="button" class="icon-btn icon-btn--primary rli-go" data-go="${esc(r.route_id)}" title="Open route" aria-label="Open route">${ICON.open}</button>
    </div>`;
}

function routePageHref(routeId) {
  const p = state.params || {};
  const q = new URLSearchParams({ route: routeId, as_of: p.as_of || DEFAULT_AS_OF, shipments: String(p.shipments || 150) });
  return `/route/${encodeURIComponent(routeId)}?${q}`;
}

function renderTable() {
  const rows = listRoutes();
  const normalHidden = visibleRoutes().length - rows.length;
  const list = $('rlist');
  if (!list) return;
  // Redrawn in place: a filter click must not throw the planner back to the
  // top of a list they were halfway down.
  const keep = list.scrollTop;

  /* A CARD: level, deadline and the count you act on at a glance; a click
   * unfolds why; the arrow opens the route. */
  list.innerHTML = rows.length ? rows.map((r) => {
    const key = keyAtRisk(r);
    const open = openCards.has(r.route_id);
    return `
    <div role="listitem" class="rli${state.selected === r.route_id ? ' is-selected' : ''}${open ? ' is-open' : ''} level-${esc(r.level)}"
         data-route="${esc(r.route_id)}" style="--lvl:${LEVEL_COLOR[r.level]}">
      <button type="button" class="rli-main" aria-expanded="${open}">
        <span class="rli-top">
          <span class="level-chip level-${esc(r.level)}" style="color:${LEVEL_COLOR[r.level]}">${esc(r.level_label)}</span>
          ${r.lead_time_hours == null ? '' : `<span class="rli-when" title="Decide within${r.clock_hours != null && state.board ? `, by ${esc(DeadlineClock.at(dueAt(r)))}` : ''}">${
            r.clock_hours != null && state.board ? DeadlineClock.html(dueAt(r)) : `${ICON.clock}${hours(r.lead_time_hours)}`}</span>`}
          ${r.early_warning ? `<span class="rli-ew" title="${esc(r.early_warning.sentence)}">⚡ orders</span>` : ''}
          <span class="rli-site">${esc(r.site ? r.site.name : '')}</span>
        </span>
        <span class="rli-title">
          <span class="rli-name">${esc(r.name)}</span>
          <span class="rli-count" title="Shipments at risk, of all on this route">${r.shipments_at_risk}/${r.shipments} shipments</span>
        </span>
        <span class="rli-driver" title="${esc(r.events[0] ? r.events[0].title : '')}">${esc(r.events[0] ? r.events[0].title : 'No event')}</span>
        <span class="rli-foot">
          <span>${focusBadge(r)}${key ? `<b class="rli-key">★ ${key} key account${key === 1 ? '' : 's'}</b>` : ''}</span>
          <span class="rli-chf">${r.exposure_chf > 0 ? chf(r.exposure_chf) : ''}</span>
        </span>
        <span class="rli-chev" aria-hidden="true">${ICON.chev}</span>
      </button>
      <button type="button" class="rli-open" data-go="${esc(r.route_id)}" title="Open route" aria-label="Open route">${ICON.open}</button>
      <div class="rli-more"><div><div class="rli-more-in">${whyHTML(r)}</div></div></div>
    </div>`;
  }).join('') : `<p class="rlist-empty">No route ${state.site ? `from ${esc(siteName(state.site))} ` : ''}needs
      attention under these filters. Try <b>All</b> customers or clear the level filter.</p>`;
  // Normal routes need nothing: counted, one click away, not in the way.
  const normalCount = visibleRoutes().filter((r) => r.level === 'green').length;
  if (!state.levelOnly && normalCount) {
    list.insertAdjacentHTML('beforeend', `<button type="button" class="rlist-more" id="f-normal">
      ${state.showNormal ? `Hide the ${normalCount} normal route${normalCount === 1 ? '' : 's'}`
        : `Show ${normalCount} normal route${normalCount === 1 ? '' : 's'} <span class="muted">(no action needed)</span>`}</button>`);
    $('f-normal').addEventListener('click', () => {
      state.showNormal = !state.showNormal;
      renderTable();
    });
  }

  list.querySelectorAll('.rli').forEach((card) => {
    const id = card.dataset.route;
    // The card unfolds and folds; the lane lights up on the map meanwhile.
    card.querySelector('.rli-main').addEventListener('click', () => {
      const open = !card.classList.contains('is-open');
      card.classList.toggle('is-open', open);
      card.querySelector('.rli-main').setAttribute('aria-expanded', String(open));
      open ? openCards.add(id) : openCards.delete(id);
      if (open) withAgent((agent) => agent.focusLane(id));
    });
    card.querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', (e) => {
      e.stopPropagation();
      select(id, { fly: true });
    }));
  });
  list.scrollTop = keep;

  const mine = deskRoutes().length;
  state.listSubtitle =
    `${rows.length} of ${mine} route${mine === 1 ? '' : 's'}${state.site ? ` from ${siteName(state.site)}` : ''}` +
    (normalHidden > 0 ? ` · ${normalHidden} normal hidden` : '');
  const count = $('panel-list-count');
  const onRoutes = !$('rlist').hidden;
  if (count && onRoutes) count.textContent = state.listSubtitle;

  const f = state.board.funnel;
  $('ranked-foot').innerHTML =
    `${num(f.raw_observations)} signals → ${f.after_resolution} events → ` +
    `${f.shipments_touched} of ${state.board.shipments_total} shipments hit`;
}

function markTableRow(routeId) {
  const list = $('rlist');
  if (!list) return;
  list.querySelectorAll('.rli').forEach((li) => {
    li.classList.toggle('is-selected', li.dataset.route === routeId);
  });
}

/* Back to the list. The selection is KEPT — the lane stays lit on the globe
 * and its row stays marked — because "show me the others" is not "I have
 * finished with this one", and clearing it would drop the highlight the
 * planner is using to keep their place. */
function showRouteList() {
  $('panel-body').hidden = true;
  $('panel-list').hidden = false;
  $('panel').scrollTop = 0;
  // Closing a route brings every other route back, and its journey goes.
  if (state.isolate) {
    state.isolate = false;
    refreshPaths();
    withAgent((agent) => agent.focusLane(null));
  }
  if (window.MapJourney) window.MapJourney.clear();
}

/* How the board works, in three lines — a card over the list, opened and
 * closed by the planner. It used to open by itself and push the list off a
 * laptop screen. */
function showHowto(on) {
  const box = $('howto');
  const btn = $('btn-help');
  if (!box || !btn) return;
  box.hidden = !on;
  btn.classList.toggle('is-on', on);
  btn.setAttribute('aria-pressed', String(on));
}
function initHowto() {
  $('btn-help').addEventListener('click', () => showHowto($('howto').hidden));
  $('howto-close').addEventListener('click', () => showHowto(false));
}

boot().catch((err) => {
  console.error(err);
  $('brand-sub').textContent = 'failed to load: ' + err.message;
});

// ===============================================================
/* A small translucent card, opened from the grid button on an event.
 *
 * WHY PER EVENT, AND WHY HIDDEN UNTIL ASKED
 * -----------------------------------------
 * There is deliberately no dashboard-level P×I scatter. Aggregating every
 * event into one grid throws away the only thing a planner needs from it —
 * *which of my shipments*. The event is the question; the points are the
 * answer, and each point is a shipment.
 *
 * WHAT THE AXES MEAN
 * ------------------
 *   x  P(this shipment is late)
 *   y  the bill IF it is late   (NOT the expected loss)
 *
 * Those are two different questions, which is the entire diagnostic value of
 * a matrix. The expected loss already has the probability multiplied in, so
 * plotting it against P would count the same probability on both axes and
 * collapse every unlikely shipment into one corner. The engine computes the
 * conditional loss instead, and the two axes multiply back to the expected
 * loss — see engine/score/impact.py.
 *
 * Lead time is a RING, not a third axis: solid means options remain, hollow
 * means they have run out. A third spatial axis would destroy legibility.
 */


// ===============================================================
// The assistant
// ===============================================================
/* One panel, two entry points: the topbar button asks about the board, the
 * speech-bubble on an event asks about that event.
 *
 * EVERY ANSWER IS MARKED. The numbers on this page were computed by the
 * engine and can be reproduced from the command line; an answer here was
 * written by a language model from a context assembled out of those numbers.
 * Those are different kinds of claim and the planner is entitled to tell them
 * apart at a glance, so generated text gets a visible rule and a label rather
 * than being dropped into the page looking like everything else.
 */
const ASK = { scope: null, busy: false };

function askPanel() { return $('ask-panel'); }

function openAsk(scope) {
  ASK.scope = scope || { kind: 'board' };
  const panel = askPanel();
  panel.hidden = false;
  $('ask-scope').textContent = ASK.scope.kind === 'event'
    ? `about: ${ASK.scope.title}`
    : 'about the whole board';
  $('btn-ask').classList.add('is-on');
  $('ask-input').focus();
  if (ASK.scope.kind === 'event') {
    $('ask-input').value = '';
    $('ask-input').placeholder = 'e.g. how bad is this if it happens?';
  } else {
    $('ask-input').placeholder = 'e.g. which route needs a decision first, and why?';
  }
}

function closeAsk() {
  askPanel().hidden = true;
  $('btn-ask').classList.remove('is-on');
}

/* An answer's lines: the first is the answer, the rest are its numbers. */
function askText(text) {
  const lines = String(text || '').split('\n');
  return lines.map((line, i) => {
    const t = esc(line);
    if (i === 0) return `<p class="ask-lead">${t}</p>`;
    return line.startsWith('•') ? `<p class="ask-li">${t.replace(/^•\s*/, '')}</p>` : `<p>${t}</p>`;
  }).join('');
}

function askLinks(links) {
  if (!links.length) return '';
  const p = state.params || {};
  const q = new URLSearchParams({ as_of: p.as_of || DEFAULT_AS_OF, shipments: String(p.shipments || 150) });
  return `<div class="ask-links">${links.map((l) => (l.href
    ? `<a class="ctl ctl--mini" href="${esc(l.href)}&${q}" target="_blank" rel="noopener">${esc(l.label)} ↗</a>`
    : `<button type="button" class="ctl ctl--mini" data-ask-route="${esc(l.route_id)}">${esc(l.label)}</button>`)).join('')}</div>`;
}

function askBubble(who, html, cls) {
  const log = $('ask-log');
  const el = document.createElement('div');
  el.className = `ask-msg ask-msg--${who}${cls ? ' ' + cls : ''}`;
  el.innerHTML = html;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

async function sendAsk() {
  if (ASK.busy) return;
  const input = $('ask-input');
  const question = input.value.trim();
  if (!question) return;

  askBubble('you', esc(question));
  input.value = '';
  $('ask-try').hidden = true;
  ASK.busy = true;
  const pending = askBubble('bot', '<span class="ask-wait">thinking…</span>');

  const body = { question };
  if (ASK.scope.kind === 'event') body.event_id = ASK.scope.event_id;
  if (state.selected) body.route_id = state.selected;

  const p = state.params || {};
  const qs = new URLSearchParams({
    as_of: p.as_of || DEFAULT_AS_OF,
    shipments: String(p.shipments || 150),
  });

  try {
    const res = await fetch(`/api/ask?${qs}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const out = await res.json();
    pending.remove();
    const links = askLinks(out.links || []);
    if (out.answered && out.generated) {
      askBubble('bot',
        `<div class="ask-gen">written by ${esc(out.model || out.backend)}, from the board</div>`
        + askText(out.answer) + links,
        'is-generated');
    } else if (out.answered) {
      // Straight from the board: every line is a row the engine computed.
      // Said once per session that a free local AI can be connected.
      const hint = !ASK.hinted && out.model_status
        ? `<div class="ask-unlock">No AI model is running (${esc(out.model_status)}), so this comes straight
            from the board. For free-form questions, start the free local AI: <code>scripts/setup_ai.sh</code>
            (Windows: <code>scripts\\setup_ai.ps1</code>).</div>` : '';
      ASK.hinted = ASK.hinted || Boolean(hint);
      askBubble('bot', `<div class="ask-gen ask-gen--board">from the board</div>${askText(out.answer)}${links}${hint}`,
        'is-board');
    } else {
      // Not an error. The board is computed without a model and is unaffected
      // by its absence, so the panel says what is missing and what it buys.
      askBubble('bot',
        `<div class="ask-none"><b>No model is connected.</b> ${esc(out.reason || '')}</div>`
        + (out.unlocks_if_connected
            ? `<div class="ask-unlock">Connecting one would add: ${esc(out.unlocks_if_connected)}</div>`
            : '')
        + '<div class="ask-unlock">Every number on this page was computed without '
        + 'one and is unaffected.</div>',
        'is-empty');
    }
  } catch (err) {
    pending.remove();
    askBubble('bot', `<div class="ask-none">Could not reach the assistant: ${esc(err.message)}</div>`, 'is-empty');
  } finally {
    ASK.busy = false;
  }
}

document.addEventListener('click', (e) => {
  const open = e.target.closest('[data-ask-route]');
  if (open) {
    select(open.dataset.askRoute, { fly: true });
    return;
  }
  const askBtn = e.target.closest('[data-ask]');
  if (askBtn) {
    e.stopPropagation();
    const id = askBtn.dataset.ask;
    let title = 'this event';
    for (const route of (state.board ? state.board.routes : [])) {
      const found = (route.events || []).find((x) => x.event_id === id);
      if (found) { title = found.title; break; }
    }
    openAsk({ kind: 'event', event_id: id, title });
  }
});

// ===============================================================
// Alerts by email (engine/alerts.py): the bell in the header.
// ===============================================================
async function alertsLoad() {
  try {
    const res = await fetch('/api/alerts');
    const d = await res.json();
    alertsFill(d);
    return d;
  } catch { return null; }
}

function alertsFill(d) {
  const s = d.settings || {};
  $('al-email').value = s.email || '';
  $('al-red').checked = (s.levels || ['red']).includes('red');
  $('al-yellow').checked = (s.levels || []).includes('yellow');
  $('al-ew').checked = s.early_warnings !== false;
  $('al-enabled').checked = s.email ? Boolean(s.enabled) : true;
  $('al-dot').hidden = !(s.enabled && s.email);
  $('al-status').textContent = d.mail_server
    ? `Mail server: ${d.mail_server_host}. Alerts are sent.`
    : 'No mail server is set on this server yet, so alerts are recorded here, not sent. See "How the email is sent".';
  const word = { sent: 'sent', recorded: 'recorded, not sent', failed: 'not sent', queued: 'on its way', nothing: '' };
  $('al-recent').innerHTML = (d.recent || []).map((r) => `
    <li><span class="al-when">${esc((r.at || '').replace('T', ' ').slice(0, 16))}</span>
      <span class="al-subj" title="${esc(r.detail || '')}">${esc(r.subject || '')}</span>
      <span class="al-st al-st--${esc(r.status)}">${esc(word[r.status] || r.status)}</span></li>`).join('');
}

function alertsBody() {
  const levels = [];
  if ($('al-red').checked) levels.push('red');
  if ($('al-yellow').checked) levels.push('yellow');
  return {
    email: $('al-email').value.trim(), levels, early_warnings: $('al-ew').checked,
    enabled: $('al-enabled').checked, base_url: location.origin,
  };
}

async function alertsSave() {
  $('al-status').textContent = 'Saving…';
  try {
    const res = await fetch('/api/alerts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(alertsBody()),
    });
    const d = await res.json();
    if (!res.ok) { $('al-status').textContent = d.detail || 'Could not save.'; return false; }
    alertsFill(d);
    $('al-status').textContent = d.settings.enabled
      ? `Saved. ${d.mail_server ? 'You will get an email' : 'Alerts will be recorded here'} when a route turns critical.`
      : 'Saved. Alerts are off.';
    return true;
  } catch { $('al-status').textContent = 'Could not reach the server.'; return false; }
}

async function alertsTest() {
  if (!(await alertsSave())) return;
  $('al-status').textContent = 'Sending a test…';
  const p = state.params || {};
  const q = new URLSearchParams({ as_of: p.as_of || DEFAULT_AS_OF, shipments: String(p.shipments || 150) });
  try {
    const res = await fetch(`/api/alerts/test?${q}`, { method: 'POST' });
    const d = await res.json();
    alertsFill(d);
    const r = d.receipt || {};
    $('al-status').textContent = r.status === 'sent' ? `Sent to ${r.to}. Check your phone.`
      : r.status === 'recorded' ? 'Recorded here, not sent: no mail server is set on this server.'
        : r.detail || 'Not sent.';
  } catch { $('al-status').textContent = 'Could not reach the server.'; }
}

function initAlerts() {
  const pop = $('al-pop');
  const btn = $('btn-alerts');
  if (!pop || !btn) return;
  const close = () => { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!pop.hidden) { close(); return; }
    // Under the bell, wherever the header wraps it.
    const at = btn.getBoundingClientRect();
    pop.style.top = `${Math.round(at.bottom + 8)}px`;
    pop.style.right = `${Math.max(12, Math.round(window.innerWidth - at.right))}px`;
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    await alertsLoad();
    $('al-email').focus();
  });
  $('al-close').addEventListener('click', close);
  $('al-save').addEventListener('click', alertsSave);
  $('al-test').addEventListener('click', alertsTest);
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !btn.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pop.hidden) close(); });
  alertsLoad();
}

function initAsk() {
  $('btn-ask').addEventListener('click', () => {
    askPanel().hidden ? openAsk({ kind: 'board' }) : closeAsk();
  });
  // Three questions to start from, so the box is never a blank page.
  $('ask-try').addEventListener('click', (e) => {
    const b = e.target.closest('.ask-chip');
    if (!b) return;
    $('ask-input').value = b.textContent.trim();
    sendAsk();
  });
  $('ask-close').addEventListener('click', closeAsk);
  $('ask-send').addEventListener('click', sendAsk);
  $('ask-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAsk(); }
  });

  // Say up front whether anything is running, so nobody types a question into
  // a box that was never going to answer.
  fetch('/api/model').then((r) => r.json()).then((m) => {
    $('btn-ask').title = m.status === 'connected'
      ? `Assistant (${m.model})`
      : 'Assistant (no model connected)';
    $('btn-ask').classList.toggle('has-model', m.status === 'connected');
  }).catch(() => {});
}


/* The charts live in charts.js, shared with the route page. These aliases
 * keep every call site in this file unchanged. */
const drawRadar = Charts.drawRadar;
const impactRows = Charts.impactRows;
const dots = Charts.dots;
const cellTint = Charts.cellTint;
const buildMatrixGrid = Charts.buildMatrixGrid;
const shortChf = Charts.shortChf;


