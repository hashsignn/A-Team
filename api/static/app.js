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
const css = getComputedStyle(document.documentElement);
const LEVEL_COLOR = {
  green:  css.getPropertyValue('--lvl-green').trim(),
  white:  css.getPropertyValue('--lvl-white').trim(),
  blue:   css.getPropertyValue('--lvl-blue').trim(),
  yellow: css.getPropertyValue('--lvl-yellow').trim(),
  red:    css.getPropertyValue('--lvl-red').trim(),
};
const BAND_COLOR = {
  minor:    css.getPropertyValue('--band-minor').trim(),
  moderate: css.getPropertyValue('--band-moderate').trim(),
  severe:   css.getPropertyValue('--band-severe').trim(),
};
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
  $('asof-input').value = isoToInput(params.as_of);

  const [board, topo] = await Promise.all([
    fetchBoard(params),
    fetch('/geo/countries-110m.json').then((r) => r.json()),
  ]);

  const countries = topojson.feature(topo, topo.objects.countries);

  state.board = board;
  initGlobe(countries, board);
  renderFilters();
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
    // The previous selection may not exist, or may no longer be visible, at
    // the new instant. applyBoard falls back to the most severe route.
    state.selected = null;
    if (state.globe) state.globe.pointsData(board.nodes);
    applyBoard(board);
    writeParams(params);
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
function initGlobe(countries, board) {
  const el = $('globe');

  const globe = Globe()(el)
    .backgroundColor('rgba(0,0,0,0)')
    .showAtmosphere(true)
    .atmosphereColor('#3d8bfd')
    .atmosphereAltitude(0.17)
    .showGraticules(true);

  // No texture image: a vector globe stays sharp at any zoom, needs no
  // multi-megabyte binary, and keeps the routes as the brightest thing on
  // screen — which is the point of the pane.
  globe.globeMaterial().color.set('#0a1526');
  globe.globeMaterial().emissive.set('#050b16');
  globe.globeMaterial().shininess = 0.12;

  globe
    .polygonsData(countries.features)
    .polygonCapColor(() => 'rgba(28, 46, 78, 0.78)')
    .polygonSideColor(() => 'rgba(10, 21, 38, 0)')
    .polygonStrokeColor(() => 'rgba(112, 148, 204, 0.42)')
    .polygonAltitude(0.004);

  // --- ports & nodes -------------------------------------------------
  globe
    .pointsData(board.nodes)
    .pointLat('lat').pointLng('lon')
    .pointAltitude(0.005)
    .pointRadius((n) => n.chokepoint ? 0.16 : 0.22)
    .pointColor((n) => n.chokepoint ? 'rgba(169,182,206,0.75)' : 'rgba(224,235,255,0.92)')
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
      color: withAlpha(LEVEL_COLOR[r.level], selected ? 1 : dim ? w.alpha * 0.4 : w.alpha),
      stroke: selected ? w.stroke + 1.4 : w.stroke,
    };
  });
}

function withAlpha(hex, a) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function refreshPaths() {
  if (state.globe) state.globe.pathsData(pathData());
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
  markTableRow(routeId);

  if (fly && state.globe && r.legs.length) {
    const pts = r.legs.flatMap((l) => l.path);
    const mid = pts[Math.floor(pts.length / 2)];
    if (mid) state.globe.pointOfView({ lat: mid[0], lng: mid[1], altitude: 2.1 }, 1100);
  }
  holdRotation();
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
      <div class="stat-k">Recoverable</div>
      <div class="stat-v">${chf(r.recoverable_chf)}</div>
      <div class="stat-sub">if acted on in time</div>
    </div>`;

  drawRadar(r.radar);
  renderSubcats(r.radar);
  renderEvents(r.events);
  renderActions(r.actions);
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
      <div class="ev">
        <div class="ev-top">
          <span class="ev-title">${esc(e.title)}</span>${p}
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

function renderActions(actions) {
  $('block-actions').hidden = !actions.length;
  if (!actions.length) return;
  $('d-actions').innerHTML = actions.slice(0, 5).map((a) => `
    <div class="act">
      <div class="act-sentence">${esc(a.sentence)}</div>
      <div class="act-meta">
        ${esc(a.shipment_id)} · ${esc(a.customer)} · lead ${hours(a.lead_time_hours)} ·
        needs ${Math.round(a.min_hours)} h · lever held by <b>${esc(a.owner)}</b>
      </div>
      ${a.contacts.length ? `<div class="act-who">${esc(a.contacts.slice(0, 2).join(' · '))}</div>` : ''}
    </div>`).join('');
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
      <td class="num">${r.recoverable_chf > 0 ? chf(r.recoverable_chf) : '<span class="r-dash">—</span>'}</td>
      <td class="r-driver">${esc(r.events[0] ? r.events[0].title : '—')}</td>
    </tr>`).join('');

  $('rtable-body').querySelectorAll('tr').forEach((tr) => {
    tr.addEventListener('click', () => {
      select(tr.dataset.route, { fly: true });
      document.querySelector('.stage').scrollIntoView({ behavior: 'smooth' });
    });
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
