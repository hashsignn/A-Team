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
  refreshPaths();
  renderTable();

  const first = visibleRoutes()[0];
  if (first) {
    select(first.route_id, { fly: true });
  } else {
    $('panel-body').hidden = true;
    $('panel-empty').hidden = false;
  }
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
    .onPointHover((n) => n ? showTip(nodeTip(n)) : hideTip());

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
    .onPathClick((d) => select(d.route_id, { fly: false }))
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
  sizeGlobe();
  window.addEventListener('resize', sizeGlobe);

  el.addEventListener('mousemove', (e) => {
    const tip = $('globe-tooltip');
    if (tip.hidden) return;
    const r = el.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - r.left + 16, r.width - 296) + 'px';
    tip.style.top = Math.min(e.clientY - r.top + 16, r.height - 90) + 'px';
  });

  $('btn-spin').addEventListener('click', toggleSpin);
  $('btn-reset').addEventListener('click', () => {
    globe.pointOfView({ lat: 34, lng: 12, altitude: 2.35 }, 900);
  });
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
  const ops = $('link-ops');
  const cargo = $('link-cargo');
  if (ops) ops.href = `/ops?${query}`;
  if (cargo) cargo.href = `/ops?${query}#cargo`;
}

function renderDetail(r) {
  $('panel-empty').hidden = true;
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

  drawRadar(r.radar);
  renderSubcats(r.radar);
  renderEvents(r.events);
  $('panel').scrollTop = 0;
}

// ===============================================================
// Radar — hand-drawn SVG
// ===============================================================
/* Families as spokes, three ordered severity bands as overlaid polygons.
 * Spokes are labelled in DAYS OF DELAY, not abstract weights, so a planner
 * reads "low water 4 days" rather than "variable 47: 0.63".
 */
function drawRadar(radar) {
  const svg = $('radar');
  const W = 340, H = 300, cx = W / 2, cy = H / 2 + 6, R = 96;
  const axes = radar.axes;
  const n = axes.length;

  if (!n) {
    svg.innerHTML = `<text x="${cx}" y="${cy}" text-anchor="middle"
      fill="var(--muted)" font-size="12">No risk variables active on this route</text>`;
    $('radar-legend').innerHTML = '';
    return;
  }

  const max = Math.max(radar.max, 0.5);
  const bands = ['minor', 'moderate', 'severe'];
  const parts = [];

  // A single axis cannot make a polygon, and two make a line. Below three
  // spokes the radar is drawn as labelled rays — honest, and still readable.
  const angle = (i) => (Math.PI * 2 * i) / Math.max(n, 3) - Math.PI / 2;
  const at = (i, v) => {
    const a = angle(i), rad = (v / max) * R;
    return [cx + Math.cos(a) * rad, cy + Math.sin(a) * rad];
  };

  // rings
  for (let g = 1; g <= 4; g++) {
    const rr = (R * g) / 4;
    if (n >= 3) {
      const pts = axes.map((_, i) => {
        const a = angle(i);
        return `${cx + Math.cos(a) * rr},${cy + Math.sin(a) * rr}`;
      }).join(' ');
      parts.push(`<polygon points="${pts}" fill="none" stroke="var(--grid)" stroke-width="1"/>`);
    } else {
      parts.push(`<circle cx="${cx}" cy="${cy}" r="${rr}" fill="none" stroke="var(--grid)" stroke-width="1"/>`);
    }
  }

  // spokes + labels
  axes.forEach((label, i) => {
    const a = angle(i);
    const ex = cx + Math.cos(a) * R, ey = cy + Math.sin(a) * R;
    parts.push(`<line x1="${cx}" y1="${cy}" x2="${ex}" y2="${ey}" stroke="var(--axis)" stroke-width="1"/>`);

    const lx = cx + Math.cos(a) * (R + 22), ly = cy + Math.sin(a) * (R + 22);
    const anchor = Math.abs(Math.cos(a)) < 0.3 ? 'middle' : (Math.cos(a) > 0 ? 'start' : 'end');
    parts.push(`<text x="${lx}" y="${ly}" text-anchor="${anchor}" dominant-baseline="middle"
      fill="var(--text-secondary)" font-size="11">${esc(label)}</text>`);
  });

  // bands, cumulative so the polygons nest instead of hiding one another
  const cum = axes.map(() => 0);
  bands.forEach((band) => {
    const vals = radar.series[band] || [];
    let any = false;
    vals.forEach((v, i) => { cum[i] += v; if (v > 0) any = true; });
    if (!any) return;

    const snapshot = cum.slice();
    if (n >= 3) {
      const pts = snapshot.map((v, i) => at(i, v).join(',')).join(' ');
      parts.push(`<polygon points="${pts}" fill="${BAND_COLOR[band]}" fill-opacity="0.26"
        stroke="${BAND_COLOR[band]}" stroke-width="2" stroke-linejoin="round"/>`);
    } else {
      snapshot.forEach((v, i) => {
        const [x, y] = at(i, v);
        parts.push(`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"
          stroke="${BAND_COLOR[band]}" stroke-width="7" stroke-linecap="round" opacity="0.72"/>`);
      });
    }
    snapshot.forEach((v, i) => {
      if (v <= 0) return;
      const [x, y] = at(i, v);
      parts.push(`<circle cx="${x}" cy="${y}" r="3.4" fill="${BAND_COLOR[band]}"/>`);
    });
  });

  // value labels on the spoke tips — selective, never one per point
  cum.forEach((v, i) => {
    if (v <= 0) return;
    const [x, y] = at(i, v);
    // Nudge the value label off the spoke so it never sits under the axis
    // name, which collides whenever a value lands near the outer ring.
    const a = angle(i);
    const ox = Math.cos(a) * 13, oy = Math.sin(a) * 13;
    parts.push(`<text x="${x - ox}" y="${y - oy}" text-anchor="middle"
      dominant-baseline="middle"
      fill="var(--text-primary)" font-size="10.5" font-family="var(--mono)">${v.toFixed(1)}d</text>`);
  });

  svg.innerHTML = parts.join('');

  const used = bands.filter((b) => (radar.series[b] || []).some((v) => v > 0));
  $('radar-legend').innerHTML = used.map((b) =>
    `<span class="rl"><span class="rl-sw" style="background:${BAND_COLOR[b]}"></span>${b}</span>`
  ).join('') || '<span class="rl muted">no contribution</span>';
}

function renderSubcats(radar) {
  const keys = radar.axis_keys || [];
  if (!keys.length) { $('subcats').innerHTML = ''; return; }

  // Split the families that are firing from the ones the mask merely checked.
  // Both matter, but they are different statements: one needs reading, the
  // other is coverage. Giving each quiet family a full card buries the two
  // that are actually doing something.
  const active = [], quiet = [];
  keys.forEach((key, i) => {
    const items = radar.subcategories[key] || [];
    const total = items.reduce((s, it) => s + it.days, 0);
    (total > 0 ? active : quiet).push({ key, i, items, total });
  });

  const cards = active.map((f, n) => {
    const maxD = Math.max(...f.items.map((it) => it.days), 0.001);
    return `
      <details class="sub-family" ${n === 0 ? 'open' : ''}>
        <summary>
          <span>${esc(radar.axes[f.i])}</span>
          <span class="sf-days">${f.total.toFixed(1)} d · ${f.items.length} variable${f.items.length === 1 ? '' : 's'}</span>
        </summary>
        <div class="sub-list">
          ${f.items.map((it) => `
            <div class="sub-item">
              <div class="sub-top">
                <span class="sub-name">${esc(it.name)}</span>
                <span class="sub-days">${it.days.toFixed(1)} d</span>
              </div>
              <div class="sub-bar"><i style="width:${(it.days / maxD * 100).toFixed(1)}%;background:${BAND_COLOR[it.severity] || BAND_COLOR.minor}"></i></div>
              <div class="sub-desc">${esc(it.description)}</div>
            </div>`).join('')}
        </div>
      </details>`;
  }).join('');

  const coverage = quiet.length
    ? `<p class="sub-quiet">
         <b>${quiet.length}</b> further ${quiet.length === 1 ? 'family' : 'families'}
         checked on this route, contributing nothing:
         ${quiet.map((f) => esc(radar.axes[f.i])).join(', ')}.
       </p>`
    : '';

  $('subcats').innerHTML = (cards || '<p class="sub-quiet">No family is contributing delay on this route.</p>') + coverage;
}

// ===============================================================
// Events & actions
// ===============================================================
function renderEvents(events) {
  $('block-events').hidden = !events.length;
  $('d-events').innerHTML = events.map((e) => {
    // An unsourceable probability is shown as unsourced. It is never a 0.5 —
    // a made-up number looks exactly like evidence.
    const p = e.probability == null
      ? '<span class="pill pill--unsourced">P unsourced</span>'
      : `<span class="pill">P ${Math.round(e.probability * 100)}%</span>`;
    return `
      <div class="ev" data-event="${esc(e.event_id)}">
        <div class="ev-top">
          <span class="ev-title">${esc(e.title)}</span>
          <span class="ev-tools">
            ${p}
            <button type="button" class="ev-btn" data-matrix="${esc(e.event_id)}"
                    title="Risk matrix for this event"
                    aria-label="Risk matrix for ${esc(e.title)}">
              <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                <rect x="1.5" y="1.5" width="13" height="13" rx="1.5"
                      fill="none" stroke="currentColor" stroke-width="1.3"/>
                <path d="M6 1.5v13M10.5 1.5v13M1.5 6h13M1.5 10.5h13"
                      stroke="currentColor" stroke-width="1"/>
                <rect x="2" y="11" width="3.5" height="3" fill="currentColor" opacity=".55"/>
              </svg>
            </button>
            <button type="button" class="ev-btn" data-ask="${esc(e.event_id)}"
                    title="Ask about this event"
                    aria-label="Ask about ${esc(e.title)}">
              <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                <path d="M14 9.5a2 2 0 0 1-2 2H6l-3.5 2.6V11.5h-.5a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"
                      transform="translate(1 0)" fill="none" stroke="currentColor"
                      stroke-width="1.3" stroke-linejoin="round"/>
              </svg>
            </button>
          </span>
        </div>
        <div class="ev-meta">
          ${esc(e.severity)} · ${esc(e.event_class.replace(/_/g, ' '))} ·
          ${e.shipments_here} shipment(s) · ${chf(e.exposure_chf)}
        </div>
        ${e.quote
          ? `<div class="ev-quote">“${esc(e.quote)}”</div>`
          : '<div class="ev-quote">inferred — no verbatim span</div>'}
        <div class="ev-meta">${esc(e.source)} · tier ${e.source_tier}</div>
      </div>`;
  }).join('');
}

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
  const c = LEVEL_COLOR[r.level];
  const chip = $('r-chip');
  chip.textContent = r.level_label;
  chip.style.color = c;
  $('r-name').textContent = r.name;
  $('r-directive').textContent = r.directive;

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
function renderFilters() {
  $('ranked-filters').innerHTML =
    `<button type="button" class="ctl" id="f-all">Show all levels</button>`;
  $('f-all').addEventListener('click', () => {
    state.hidden.clear();
    $('ladder').querySelectorAll('.rung').forEach((b) => b.classList.remove('is-off'));
    refreshPaths();
    renderTable();
  });
}

function renderTable() {
  const rows = visibleRoutes();
  $('rtable-body').innerHTML = rows.map((r) => `
    <tr data-route="${esc(r.route_id)}" class="${state.selected === r.route_id ? 'is-selected' : ''}">
      <td class="num r-score" style="color:${LEVEL_COLOR[r.level]}">${r.severity_score.toFixed(3)}</td>
      <td><span class="level-chip" style="color:${LEVEL_COLOR[r.level]}">${esc(r.level_label)}</span></td>
      <td>
        <div class="r-route">${esc(r.name)}</div>
        <div class="r-focus">${esc(r.focus.replace(/_/g, ' '))} · ${r.legs.length} legs</div>
      </td>
      <td class="r-when" style="color:${LEVEL_COLOR[r.level]}">${hours(r.lead_time_hours)}</td>
      <td class="num">${r.shipments_at_risk}<span class="r-dash"> / ${r.shipments}</span></td>
      <td class="num">${r.exposure_chf > 0 ? chf(r.exposure_chf) : '<span class="r-dash">—</span>'}</td>
      <td class="r-driver">${esc(r.events[0] ? r.events[0].title : '—')}</td>
    </tr>`).join('');

  $('rtable-body').querySelectorAll('tr').forEach((tr) => {
    // Selecting from the table opens the response beside it. It deliberately
    // does NOT scroll back to the globe: you came down here to act on a route,
    // and yanking the page away from the response pane would undo that.
    tr.addEventListener('click', () => select(tr.dataset.route, { fly: true }));
  });

  const f = state.board.funnel;
  $('ranked-foot').innerHTML =
    `${rows.length} of ${state.board.routes.length} routes shown. ` +
    `Measured this run: ${f.raw_observations} raw observations → ${f.after_resolution} distinct events → ` +
    `${f.gated_hits} gate hits across ${f.shipments_touched} of ${state.board.shipments_total} shipments. ` +
    `Severity ranks by level first, then CHF exposure within the level — no weighted blend, ` +
    `so a Bias route can never outrank a Watch one however much money is on it.`;
}

function markTableRow(routeId) {
  $('rtable-body').querySelectorAll('tr').forEach((tr) => {
    tr.classList.toggle('is-selected', tr.dataset.route === routeId);
  });
}

boot().catch((err) => {
  console.error(err);
  $('brand-sub').textContent = 'failed to load — ' + err.message;
});

// ===============================================================
// Per-event risk matrix
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
const MATRIX = { open: null };

function impactRows(grid) {
  // Declared most severe first; the grid draws worst at the top.
  return grid.impact_bands;
}

function openMatrix(eventId, anchorEl) {
  const board = state.board;
  if (!board) return;
  let event = null;
  for (const route of board.routes) {
    const found = (route.events || []).find((e) => e.event_id === eventId);
    if (found) { event = found; break; }
  }
  if (!event) return;

  const grid = board.matrix_grid;
  const m = event.matrix;
  const rows = impactRows(grid);
  const cols = grid.probability_bands;

  // Count shipments per cell. A cell is a COUNT, not a heat value: colouring
  // by density would invent a severity ordering the bands already state.
  const cells = new Map();
  const solid = new Map();
  for (const pt of m.points) {
    const key = `${pt.impact_band}|${pt.probability_band}`;
    cells.set(key, (cells.get(key) || 0) + 1);
    if (pt.ring === 'solid') solid.set(key, (solid.get(key) || 0) + 1);
  }

  const unsourced = m.points.filter((p) => p.p_late === null);

  const head = `
    <div class="mx-head">
      <div>
        <div class="mx-title">${esc(event.title)}</div>
        <div class="mx-sub">${m.shipments} shipment(s) · ${m.still_actionable} still actionable</div>
      </div>
      <button type="button" class="mx-close" id="mx-close" aria-label="Close">×</button>
    </div>`;

  const body = `
    <table class="mx-grid">
      <thead>
        <tr>
          <th class="mx-corner"><span>impact if late</span></th>
          ${cols.map((c) => `<th>${esc(c.label)}</th>`).join('')}
          ${unsourced.length ? `<th class="mx-unsourced-h">${esc(grid.unsourced_band.label)}</th>` : ''}
        </tr>
      </thead>
      <tbody>
        ${rows.map((r) => `
          <tr>
            <th class="mx-row" title="${esc(r.action)}">${esc(r.label)}</th>
            ${cols.map((c) => {
              const key = `${r.id}|${c.id}`;
              const n = cells.get(key) || 0;
              const s = solid.get(key) || 0;
              return `<td class="mx-cell${n ? ' has' : ''}"
                          title="${n ? `${n} shipment(s) — ${esc(r.action)}` : 'empty'}">
                ${n ? dots(n, s) : ''}
              </td>`;
            }).join('')}
            ${unsourced.length ? (() => {
              const n = unsourced.filter((p) => p.impact_band === r.id).length;
              const s = unsourced.filter((p) => p.impact_band === r.id && p.ring === 'solid').length;
              return `<td class="mx-cell mx-unsourced${n ? ' has' : ''}">${n ? dots(n, s) : ''}</td>`;
            })() : ''}
          </tr>`).join('')}
      </tbody>
      <tfoot>
        <tr>
          <td></td>
          <td colspan="${cols.length + (unsourced.length ? 1 : 0)}" class="mx-xaxis">
            P(this shipment is late) →
          </td>
        </tr>
      </tfoot>
    </table>`;

  const note = unsourced.length
    ? `<p class="mx-note"><b>${unsourced.length}</b> shipment(s) sit in the
       <b>${esc(grid.unsourced_band.label)}</b> band, outside the axis: this event's
       probability could not be sourced, so there is no x for them. They are not
       placed at 0.5 — a made-up number looks exactly like evidence.</p>`
    : '';

  const legend = `
    <div class="mx-legend">
      <span><i class="mx-dot mx-dot--solid"></i> options still open</span>
      <span><i class="mx-dot mx-dot--hollow"></i> too late to act</span>
    </div>
    <p class="mx-note">Vertical axis is the bill <b>if</b> the shipment is late,
      not the probability-weighted loss — otherwise the odds would be counted on
      both axes.</p>`;

  const card = document.createElement('div');
  card.className = 'mx-card';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-label', `Risk matrix for ${event.title}`);
  card.innerHTML = head + body + legend + note;
  document.body.appendChild(card);
  positionMatrix(card, anchorEl);

  MATRIX.open = { card, eventId };
  card.querySelector('#mx-close').addEventListener('click', closeMatrix);
}

function dots(n, solidCount) {
  // Capped: past a handful the count is the information, not the arrangement.
  const shown = Math.min(n, 6);
  let out = '';
  for (let i = 0; i < shown; i += 1) {
    out += `<i class="mx-dot mx-dot--${i < solidCount ? 'solid' : 'hollow'}"></i>`;
  }
  if (n > shown) out += `<span class="mx-more">+${n - shown}</span>`;
  return out;
}

function positionMatrix(card, anchorEl) {
  const pad = 10;
  const box = anchorEl ? anchorEl.getBoundingClientRect() : null;
  const size = card.getBoundingClientRect();
  let left = box ? box.left - size.width - pad : window.innerWidth - size.width - 24;
  let top = box ? box.top - 8 : 80;
  if (left < pad) left = box ? Math.min(box.right + pad, window.innerWidth - size.width - pad) : pad;
  top = Math.max(pad, Math.min(top, window.innerHeight - size.height - pad));
  card.style.left = `${Math.max(pad, left)}px`;
  card.style.top = `${top}px`;
}

function closeMatrix() {
  if (!MATRIX.open) return;
  MATRIX.open.card.remove();
  MATRIX.open = null;
}

// Close on Escape, on a click outside, and on anything that changes the board
// underneath it — a matrix describing a route you have navigated away from is
// worse than no matrix.
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMatrix(); });
document.addEventListener('click', (e) => {
  if (!MATRIX.open) return;
  if (e.target.closest('.mx-card') || e.target.closest('[data-matrix]')) return;
  closeMatrix();
});

// Delegated so it survives every re-render of the event list.
document.addEventListener('click', (e) => {
  const mx = e.target.closest('[data-matrix]');
  if (mx) {
    e.stopPropagation();
    const id = mx.dataset.matrix;
    const already = MATRIX.open && MATRIX.open.eventId === id;
    closeMatrix();
    if (!already) openMatrix(id, mx);
  }
});

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
