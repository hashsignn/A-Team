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
const LEVEL_WHEN = {
  red: 'within 6 h',
  yellow: 'within 24–48 h',
  blue: 'within 3–7 days',
  white: 'monitor',
  green: 'no action',
};

const state = {
  board: null,
  selected: null,
  hidden: new Set(),   // levels toggled off in the ladder
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
const chf = (v) => v == null ? '—' : 'CHF ' + Math.round(v).toLocaleString('en-CH');
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function hours(h) {
  if (h == null) return '—';
  if (h < 0) return 'passed';
  if (h < 48) return Math.round(h) + ' h';
  return Math.round(h / 24) + ' days';
}

// ===============================================================
// The as-of instant
// ===============================================================
/* Nothing in engine/ reads the wall clock — every stage takes an explicit
 * as-of. That makes this the only "now" there is, and it is why the URL can
 * carry it: `?as_of=...` is a complete, shareable description of a board.
 * A past instant is a hindcast through the identical code path.
 */
const DEFAULT_AS_OF = '2026-09-18T06:00:00+00:00';

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
  $('asof-input').value = isoToInput(params.as_of);

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
  applyBoard(board);

  $('asof-form').addEventListener('submit', (e) => {
    e.preventDefault();
    reload(inputToIso($('asof-input').value));
  });
  $('asof-now').addEventListener('click', () => {
    $('asof-input').value = isoToInput(DEFAULT_AS_OF);
    reload(DEFAULT_AS_OF);
  });

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
  } catch (err) {
    console.error(err);
    $('brand-sub').textContent = `could not load that instant — ${err.message}`;
  } finally {
    stage.classList.remove('is-loading');
    $('asof-apply').disabled = false;
  }
}

/* Everything that depends on the board, in one place, so boot and reload
 * cannot drift apart. */
function applyBoard(board) {
  $('brand-sub').textContent =
    `${board.as_of_label} · ${board.shipments_total} shipments (synthetic) · ` +
    `${board.variables_total} risk variables · ${board.routes.length} routes`;

  renderPosture(board.posture);
  renderLadder(board.levels);
  initPanelTabs();
  refreshPaths();
  renderTable();

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
  return visibleRoutes()
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
function pathData() {
  const routes = visibleRoutes();
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
function renderPosture(p) {
  const tone = { convene: 'red', watch: 'yellow', normal: 'green' }[p.posture] || 'blue';
  const c = LEVEL_COLOR[tone];
  $('posture').style.borderLeftColor = c;
  $('posture').innerHTML =
    `<span class="posture-label" style="color:${c}">${esc(p.posture)}</span>
     <span class="posture-text">${esc(p.headline)}</span>`;
}

function renderLadder(levels) {
  $('ladder').innerHTML = levels.map((l) => `
    <button type="button" class="rung${state.hidden.has(l.level) ? ' is-off' : ''}"
            data-level="${l.level}" title="${esc(l.directive)}"
            aria-pressed="${!state.hidden.has(l.level)}">
      <span class="rung-dot" style="background:${LEVEL_COLOR[l.level]}"></span>
      <span>
        <span class="rung-name">${esc(l.label)}</span>
        <span class="rung-count">${l.count}</span>
        <span class="rung-when">${esc(LEVEL_WHEN[l.level])}</span>
      </span>
    </button>`).join('');

  $('ladder').querySelectorAll('.rung').forEach((b) => {
    b.addEventListener('click', () => {
      const lvl = b.dataset.level;
      state.hidden.has(lvl) ? state.hidden.delete(lvl) : state.hidden.add(lvl);
      b.classList.toggle('is-off', state.hidden.has(lvl));
      b.setAttribute('aria-pressed', String(!state.hidden.has(lvl)));
      refreshPaths();
      renderTable();
    });
  });
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

function visibleRoutes() {
  return state.board.routes.filter((r) => !state.hidden.has(r.level));
}

// ===============================================================
// Selection & detail panel
// ===============================================================
function select(routeId, { fly } = {}) {
  const r = state.board.routes.find((x) => x.route_id === routeId);
  if (!r) return;
  state.selected = routeId;

  refreshPaths();
  renderDetail(r);
  renderResponse(r);
  markTableRow(routeId);
  linkOps(routeId);

  if (fly && state.globe && r.legs.length) {
    const pts = r.legs.flatMap((l) => l.path);
    const mid = pts[Math.floor(pts.length / 2)];
    if (mid) state.globe.pointOfView({ lat: mid[0], lng: mid[1], altitude: 2.1 }, 1100);
  }
  holdRotation();
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
  // ONE way onward from this panel, to the route's own page. It used to
  // offer two links to two surfaces that showed overlapping subsets of the
  // same thing; the route page is where all of it lives now.
  const link = $('link-route');
  if (link) link.href = `/route/${encodeURIComponent(routeId)}?${query}`;
}

function renderDetail(r) {
  $('panel-list').hidden = true;
  $('panel-body').hidden = false;

  const c = LEVEL_COLOR[r.level];
  const chip = $('d-chip');
  chip.textContent = r.level_label;
  chip.style.color = c;

  $('d-name').textContent = r.name;
  $('d-directive').textContent = r.directive;
  $('d-reason').textContent = r.reason;

  $('d-stats').innerHTML = `
    <div class="stat">
      <div class="stat-k">Action by</div>
      <div class="stat-v" style="color:${c}">${hours(r.lead_time_hours)}</div>
      <div class="stat-sub">first option to expire</div>
    </div>
    <div class="stat">
      <div class="stat-k">Exposure</div>
      <div class="stat-v">${chf(r.exposure_chf)}</div>
      <div class="stat-sub">${r.shipments_at_risk} of ${r.shipments} shipments</div>
    </div>
    <div class="stat">
      <div class="stat-k">Options open</div>
      <div class="stat-v">${r.actions.length}</div>
      <div class="stat-sub">worth more than they cost</div>
    </div>`;

  // No radar, no event list, no matrix here. They were on this panel AND on
  // the route page AND in a modal — three copies of three charts, none big
  // enough to read. One copy, on /route/<id>, which is a page you can send.
  $('panel').scrollTop = 0;
}

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

function renderRActions(r) {
  const actions = r.actions || [];
  if (!actions.length) {
    $('r-actions').innerHTML = `
      <div class="response-empty">
        No option on this route currently saves more than it costs.
        That is a real answer, not a gap — it is what lets you stop worrying
        about this one.
      </div>`;
    return;
  }
  $('r-actions').innerHTML = actions.map((a) => `
    <div class="act">
      <div class="act-sentence">${esc(a.sentence)}</div>
      <div class="act-meta">
        ${esc(a.shipment_id)} · ${esc(a.customer)} · lead ${hours(a.lead_time_hours)} ·
        needs ${Math.round(a.min_hours)} h · lever held by <b>${esc(a.owner)}</b>
      </div>
    </div>`).join('');
}

function renderRContacts(r) {
  const resp = r.response || {};
  const out = [];

  const card = (p) => `
    <div class="contact">
      <div class="contact-top"><span class="contact-name">${esc(p.name)}</span></div>
      <div class="contact-role">${esc(p.role)}</div>
      ${p.why ? `<div class="contact-why">${esc(p.why)}</div>` : ''}
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

  if (resp.route_manager) {
    out.push(group('Route manager', 'runs this corridor', [resp.route_manager]));
  }
  out.push(group('Convene', `${r.level_label} draws these in`, resp.standing_teams));
  out.push(group('Seniors', 'above the standing teams', resp.seniors));
  out.push(group('Alternate vendors on this route', 'at nodes this lane uses', resp.vendors));
  out.push(group('Carriers', 'running this route\'s modes', resp.carriers));

  // Declared alternatives. An empty list means "none configured" and is shown
  // as exactly that — never a silent zero, and never a claim that no
  // alternative exists in the world.
  const routing = resp.alternate_routing || [];
  out.push(`<div class="cgroup">
    <div class="cgroup-head"><span>Alternate routing</span>
      <span>declared on this route's nodes</span></div>
    ${routing.length
      ? routing.map((x) => `<div class="altroute">
          <b>${esc(x.node_name)}</b> → ${x.alternatives.map((a) => esc(a.name)).join(', ')}
        </div>`).join('')
      : `<div class="altroute">No alternative route configured for the nodes on
          this route. That is a gap in the profile, not a finding that none exists.</div>`}
  </div>`);

  $('r-contacts').innerHTML = out.join('');
}

function renderREscalate(r) {
  const resp = r.response || {};
  const esc_ = resp.escalation || {};
  const c = LEVEL_COLOR[r.level];

  let html = `
    <div class="esc-card" style="border-left-color:${c}">
      <div class="esc-level" style="color:${c}">
        ${esc(r.level_label)} · escalation level ${esc(esc_.level ?? '—')}
      </div>
      <div class="esc-body">${esc(r.directive)} — ${esc(r.reason)}</div>
      ${esc_.notify && esc_.notify.length ? `<div class="esc-list">
        Notify: <b>${esc_.notify.map(esc).join(', ')}</b>
        ${esc_.acknowledge_within_hours
          ? ` · acknowledge within ${esc_.acknowledge_within_hours} h` : ''}
      </div>` : ''}
      ${esc_.note ? `<div class="contact-why">${esc(esc_.note)}</div>` : ''}
    </div>`;

  if (resp.approval) {
    html += `<div class="esc-card" style="border-left-color:${LEVEL_COLOR.yellow}">
      <div class="esc-level" style="color:${LEVEL_COLOR.yellow}">Spend approval</div>
      <div class="esc-body">${esc(resp.approval.note)}</div>
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
  $('send-subject').value = `[${r.level_label}] ${r.name} — action by ${hours(r.lead_time_hours)}`;
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
    $('send-body').value = `could not build the summary — ${err.message}`;
  }
}

function initResponseTabs() {
  const back = $('d-back');
  if (back) back.addEventListener('click', showRouteList);

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
      btn.textContent = 'Selected — press Ctrl+C';
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
  if (!tabs) return;
  tabs.addEventListener('click', (event) => {
    const tab = event.target.closest('.ptab');
    if (!tab) return;
    tabs.querySelectorAll('.ptab').forEach((t) => t.classList.remove('is-on'));
    tab.classList.add('is-on');
    const signals = tab.dataset.ptab === 'signals';
    $('rlist').hidden = signals;
    $('siglist').hidden = !signals;
    $('f-all').hidden = signals;
    // The subtitle belongs to the routes list. Leaving "17 of 17 routes"
    // above a funnel is the kind of stale line that makes a planner distrust
    // every other number on the page.
    $('panel-list-count').textContent = signals
      ? 'What arrived, what the filter removed, and what read the rest'
      : state.listSubtitle || '';
    if (signals && !signalsLoaded) loadSignals();
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
    host.innerHTML = signalsHTML(data);
  } catch (err) {
    host.innerHTML = `<p class="sig-note">Could not read it — ${esc(err.message)}</p>`;
  }
}

function signalsHTML(data) {
  const m = data.model;
  const widest = Math.max(...data.stages.map((s) => s.count), 1);

  const funnel = data.stages.map((s) => `
    <div class="sig-stage">
      <div class="sig-stage-top">
        <span>${esc(s.label)}</span>
        <span class="sig-count">${s.count}${
          s.removed ? `<i>−${s.removed}</i>` : ''}</span>
      </div>
      <div class="sig-bar"><span style="width:${Math.round(s.count / widest * 100)}%"></span></div>
      <div class="sig-rule">${esc(s.note)}</div>
    </div>`).join('');

  const rows = data.signals.slice(0, 40).map((row) => `
    <div class="sig-row sig-row--${esc(row.state)}">
      <span class="sig-state">${esc(row.state)}</span>
      <span>
        <span class="sig-title">${esc(row.title)}</span>
        <span class="sig-why">${esc(row.why || '')}</span>
        ${row.source ? `<span class="sig-src">${esc(row.source)}${
          row.tier ? ` · tier ${row.tier}` : ''}${
          row.inferred ? ' · inferred' : ''}</span>` : ''}
      </span>
    </div>`).join('');

  return `
    <div class="sig-model ${m.status === 'connected' ? 'is-on' : ''}">
      <b>${m.status === 'connected'
        ? `Reading: ${esc(m.model)}`
        : 'No model connected'}</b>
      <span>${esc(m.detail)}</span>
      ${m.status === 'connected' ? `
        <span class="sig-src">triage ${esc(m.triage)} · extract ${esc(m.extract)}</span>`
        : `<span class="sig-src">The deterministic filter below still runs.
           It is arithmetic, not judgement, and costs nothing either way.</span>`}
      ${m.recording && m.recording.available ? `
        <span class="sig-replay">Replaying ${m.recording.entries} recorded
          answer(s) from ${esc(m.recording.models.join(', '))}, recorded
          ${esc(m.recording.recorded_at.slice(0, 10))}. Shown as recordings,
          not as live reads.</span>` : ''}
    </div>

    <div class="sig-funnel">${funnel}</div>

    <div class="sig-rows">
      <p class="sig-head">${data.counts.events} event(s) survived ·
        ${data.counts.dropped} dropped by the filter ·
        ${data.counts.unpromoted} seen but not trusted</p>
      ${rows}
    </div>

    <p class="sig-note">${esc(m.funnel_note || '')}</p>`;
}

function renderFilters() {
  const all = $('f-all');
  if (!all) return;
  all.addEventListener('click', () => {
    state.hidden.clear();
    $('ladder').querySelectorAll('.rung').forEach((b) => b.classList.remove('is-off'));
    refreshPaths();
    renderTable();
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
function renderTable() {
  const rows = visibleRoutes();
  const list = $('rlist');
  if (!list) return;

  list.innerHTML = rows.map((r) => `
    <button type="button" role="listitem" class="rli${state.selected === r.route_id ? ' is-selected' : ''}"
            data-route="${esc(r.route_id)}" style="--lvl:${LEVEL_COLOR[r.level]}">
      <span class="rli-top">
        <span class="level-chip" style="color:${LEVEL_COLOR[r.level]}">${esc(r.level_label)}</span>
        <span class="rli-when" style="color:${LEVEL_COLOR[r.level]}">${hours(r.lead_time_hours)}</span>
      </span>
      <span class="rli-name">${esc(r.name)}</span>
      <span class="rli-driver">${esc(r.events[0] ? r.events[0].title : 'no event recorded')}</span>
      <span class="rli-foot">
        <span>${r.shipments_at_risk} of ${r.shipments} shipments</span>
        <span>${r.exposure_chf > 0 ? chf(r.exposure_chf) : '—'}</span>
      </span>
    </button>`).join('');

  list.querySelectorAll('.rli').forEach((li) => {
    li.addEventListener('click', () => select(li.dataset.route, { fly: true }));
  });

  state.listSubtitle =
    `${rows.length} of ${state.board.routes.length} routes · ranked by level, ` +
    `then CHF within the level`;
  const count = $('panel-list-count');
  const onSignals = $('siglist') && !$('siglist').hidden;
  if (count && !onSignals) count.textContent = state.listSubtitle;

  const f = state.board.funnel;
  $('ranked-foot').innerHTML =
    `Measured this run: ${f.raw_observations} raw observations → ` +
    `${f.after_resolution} distinct events → ${f.gated_hits} gate hits across ` +
    `${f.shipments_touched} of ${state.board.shipments_total} shipments.`;
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
}

boot().catch((err) => {
  console.error(err);
  $('brand-sub').textContent = 'failed to load — ' + err.message;
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
    if (out.answered) {
      askBubble('bot',
        `<div class="ask-gen">generated by ${esc(out.model || out.backend)}</div>`
        + esc(out.answer).replace(/\n/g, '<br>'),
        'is-generated');
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

function initAsk() {
  $('btn-ask').addEventListener('click', () => {
    askPanel().hidden ? openAsk({ kind: 'board' }) : closeAsk();
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
      ? `Assistant — ${m.model}`
      : 'Assistant — no model connected';
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


