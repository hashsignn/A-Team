/* One route, and the vehicles carrying it.
 *
 * The board says which lanes are in trouble. This page answers the question
 * that follows, and that question is about VEHICLES: nine trucks, of which
 * two are behind a closed motorway and one has reported damage, is something
 * a planner can act on. "A lane at Alert" is not.
 *
 * A PAGE, NOT A DIALOG. A planner looking at one lane wants to send somebody
 * the lane, and a modal cannot be sent. /route/LANE_RHINE_01 can.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const chf = (v) => v == null ? '—' : 'CHF ' + Math.round(v).toLocaleString('en-US');

const ROUTE_ID = decodeURIComponent(location.pathname.split('/route/')[1] || '');
const state = { view: null, selected: null };

/* One glyph per mode. Drawn rather than emoji: an emoji renders differently
 * on every platform and at a size the browser picks, and these have to be
 * small, uniform and legible in a row of thirty. */
const GLYPH = {
  road:  '<rect x="1" y="6" width="12" height="8" rx="1.5"/><rect x="13" y="8" width="7" height="6" rx="1.5"/><circle cx="5" cy="16" r="2"/><circle cx="16" cy="16" r="2"/>',
  rail:  '<rect x="3" y="3" width="16" height="11" rx="2"/><circle cx="7" cy="16" r="1.8"/><circle cx="15" cy="16" r="1.8"/><path d="M1 19h20"/>',
  barge: '<path d="M2 13h18l-2 5H4z"/><rect x="6" y="6" width="10" height="6" rx="1"/>',
  sea:   '<path d="M2 13h18l-2 5H4z"/><rect x="6" y="5" width="10" height="7" rx="1"/><path d="M11 5V2"/>',
  air:   '<path d="M2 12l18-7-5 8 5 8-18-7z"/>',
};
const MODE_WORD = {
  road: 'trucks', rail: 'rail wagons', barge: 'barges',
  sea: 'vessels', air: 'aircraft',
};

function glyph(mode) {
  return GLYPH[mode] || GLYPH.road;
}

// ---------------------------------------------------------------
function statRow(v) {
  const cells = [
    ['Action by', v.lead_time_hours == null ? '—'
      : (v.lead_time_hours < 48 ? `${Math.round(v.lead_time_hours)} h`
         : `${Math.round(v.lead_time_hours / 24)} days`),
      'until the first option closes'],
    ['Exposure', chf(v.exposure_chf), `${v.shipments_affected} of ${v.shipments_total} consignments`],
    ['Already hit', String(v.totals.affected), 'no option left'],
    ['Need a decision', String(v.totals.at_risk), 'still time to change them'],
  ];
  $('rt-stats').innerHTML = cells.map(([label, value, note]) => `
    <div class="rt-stat">
      <div class="rt-stat-label">${esc(label)}</div>
      <div class="rt-stat-value">${esc(value)}</div>
      <div class="rt-stat-note">${esc(note)}</div>
    </div>`).join('');
}

/* The convoy: one row per leg, one icon per consignment on it.
 *
 * Icons rather than a table because the question is "how many, and how bad",
 * which is a shape you read at a glance and a number you have to add up. */
function renderConvoy(v) {
  $('convoy').innerHTML = v.legs.map((leg) => {
    const word = MODE_WORD[leg.mode] || 'vehicles';
    const g = glyph(leg.mode);
    return `
    <div class="leg">
      <div class="leg-head">
        <div class="leg-route">
          <b>${esc(leg.from_name)}</b>
          <span class="leg-arrow">→</span>
          <b>${esc(leg.to_name)}</b>
        </div>
        <div class="leg-meta">
          ${leg.km ? `<span class="muted">${Math.round(leg.km)} km</span>` : ''}
          ${leg.vehicles.length} ${esc(word)}
          ${leg.counts.affected ? `<span class="pill pill--hit">${leg.counts.affected} hit</span>` : ''}
          ${leg.counts.at_risk ? `<span class="pill pill--risk">${leg.counts.at_risk} to decide</span>` : ''}
        </div>
      </div>
      <div class="leg-vehicles">
        ${leg.vehicles.map((veh) => `
          <button type="button" class="veh veh--${esc(veh.status)}"
                  data-leg="${leg.index}" data-ship="${esc(veh.shipment_id)}"
                  title="${esc(veh.shipment_id)} · ${esc(veh.customer)}">
            <svg viewBox="0 0 22 22" aria-hidden="true">${g}</svg>
            <span class="veh-id">${esc(veh.shipment_id.replace(/^SYN-/, ''))}</span>
          </button>`).join('')}
      </div>
    </div>`;
  }).join('');

  document.querySelectorAll('.veh').forEach((btn) => {
    btn.addEventListener('click', () => selectVehicle(btn.dataset.ship, +btn.dataset.leg));
  });
}

/* What one vehicle is carrying, and what somebody on site said about it.
 *
 * The reports are the reason this page is worth opening: every other number
 * here is inferred from a feed describing a region. A field report is
 * somebody looking at this consignment. */
function selectVehicle(shipmentId, legIndex) {
  const leg = state.view.legs.find((l) => l.index === legIndex);
  const veh = leg && leg.vehicles.find((x) => x.shipment_id === shipmentId);
  if (!veh) return;

  document.querySelectorAll('.veh').forEach((b) => b.classList.toggle(
    'is-on', b.dataset.ship === shipmentId && +b.dataset.leg === legIndex));

  const WORD = { affected: 'Already hit', at_risk: 'Needs a decision', ok: 'On plan' };
  const pr = veh.progress || {};
  const facts = [
    ['Customer', veh.customer],
    ['Carrier', veh.carrier],
    ['Value', chf(veh.value_chf)],
    ['On this leg by', new Date(veh.leg_arrives).toUTCString().slice(5, 22) + ' UTC'],
    ['Decide within', veh.lead_time_hours == null ? '—' : `${Math.round(veh.lead_time_hours)} h`],
    ['What is hitting it', veh.driving_event || 'nothing on this leg'],
  ];

  /* Distance done and distance left, with the bar showing the whole journey
   * rather than the current leg — "60% of the way to Rotterdam" is the shape
   * of the question, and a per-leg bar resets to zero at every node, which
   * reads as going backwards. */
  const journey = pr.total_km ? `
    <div class="prog">
      <div class="prog-bar"><span style="width:${Math.min(100, pr.percent)}%"></span></div>
      <div class="prog-nums">
        <b>${Math.round(pr.travelled_km).toLocaleString('en-US')} km</b> travelled
        <span class="muted">·</span>
        <b>${Math.round(pr.remaining_km).toLocaleString('en-US')} km</b> to go
        <span class="muted">of ${Math.round(pr.total_km).toLocaleString('en-US')} km (${pr.percent}%)</span>
      </div>
    </div>` : '';

  /* WHERE IT IS: two claims, never merged.
   *
   * The plan says one thing; somebody with the freight said another. A single
   * dot would have to pick, and whichever it picked it would be wrong half
   * the time — a planned dot is fiction the moment a truck stops, an observed
   * one is stale the moment it starts again. The GAP is the useful number. */
  const seen = veh.observed_position;
  const plan = pr.planned_position;
  const coord = (c) => `${c.lat.toFixed(3)}, ${c.lon.toFixed(3)}`;
  const mapLink = (c) =>
    `<a class="maplink" target="_blank" rel="noopener"
        href="https://www.openstreetmap.org/?mlat=${c.lat}&mlon=${c.lon}#map=11/${c.lat}/${c.lon}">open map</a>`;

  const where = `
    <div class="where">
      <div class="where-col">
        <span class="muted" title="Interpolated along the leg">PLANNED POSITION</span>
        ${plan ? `<b>${coord(plan)}</b> ${mapLink(plan)}` : '<b>—</b>'}
      </div>
      <div class="where-col">
        <span class="muted">LAST SEEN</span>
        ${seen ? `<b>${coord(seen)}</b> ${mapLink(seen)}
           <em>${esc(seen.reported_by || 'on site')},
             ${esc(String(seen.observed_at).slice(0, 16).replace('T', ' '))} UTC${
               seen.accuracy_m ? ` · ±${Math.round(seen.accuracy_m)} m` : ''}${
               seen.first_hand === false ? ' · second hand' : ''}</em>`
          : '<b>not reported</b>'}
      </div>
      <div class="where-col">
        <span class="muted">OFF PLAN BY</span>
        ${veh.drift_km == null
          ? '<b>—</b>'
          : `<b class="${veh.drift_km > 50 ? 'drift-bad' : ''}">${veh.drift_km} km</b>`}
      </div>
    </div>`;

  const reports = (veh.reports || []).length
    ? `<div class="rep-list">${veh.reports.map((r) => `
        <div class="rep">
          <div class="rep-top">
            <span class="tag ${r.load_state === 'damaged' ? 'tag--warn' : 'tag--ok'}">${esc(r.status)}</span>
            <b>${esc(r.role_label || 'on site')}</b>
            <span class="muted">${esc(String(r.observed_at).slice(0, 16).replace('T', ' '))} UTC</span>
            ${r.first_hand === false ? '<span class="tag tag--off">second hand</span>' : ''}
          </div>
          ${r.position ? `<div class="rep-line"><b>Where:</b> ${esc(r.position)}</div>` : ''}
          <div class="rep-line"><b>Load:</b> ${esc(r.load_state)}</div>
          ${r.note ? `<div class="rep-note">“${esc(r.note)}”</div>` : ''}
          ${(r.photos || []).length ? `<div class="rep-shots">${r.photos.map((ph) => {
            // EXIF is stripped on upload, so the rotation cannot come from
            // the file any more. It comes back as a number and is applied
            // here — otherwise stripping the metadata would quietly lay a
            // whole class of phone photos on their side.
            const id = typeof ph === 'string' ? ph : ph.id;
            const o = (typeof ph === 'object' && ph.orientation) || 1;
            return `<a href="/api/v1/photos/${esc(id)}" target="_blank" rel="noopener">
              <img class="shot-o${o}" src="/api/v1/photos/${esc(id)}"
                   alt="photo from site" loading="lazy">
            </a>`;
          }).join('')}</div>` : ''}
          ${r.lat != null ? `<div class="rep-line"><b>Fix:</b> ${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}${r.accuracy_m ? ` ±${Math.round(r.accuracy_m)} m` : ''}</div>` : ''}
        </div>`).join('')}</div>`
    : `<p class="socket" title="A first-hand confirmation is what releases a re-route.">No field report yet. File one at <a href="/driver">/driver</a>.</p>`;

  $('rt-vehicle-title').textContent = `${veh.shipment_id} · ${WORD[veh.status]}`;
  $('rt-vehicle').innerHTML = `
    ${journey}
    ${where}
    <div class="veh-facts">
      ${facts.map(([k, val]) => `
        <div><span class="muted">${esc(k)}</span><b>${esc(val)}</b></div>`).join('')}
    </div>
    <h3 class="rep-h">Reported from site</h3>
    ${reports}`;
  $('rt-vehicle-block').hidden = false;
  $('rt-vehicle-block').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ---------------------------------------------------------------
/* Only ever an http(s) link: an operator's page comes from a config file,
 * and a javascript: URL in one should not become a click on this page. */
function safeUrl(url) {
  return /^https?:\/\//i.test(String(url || '')) ? String(url) : '';
}

const READING_WORD = {
  wind_gusts_10m_max: ['gusts', 'm/s'],
  wind_speed_10m_max: ['wind', 'm/s'],
  precipitation_sum: ['rain', 'mm'],
  snowfall_sum: ['snow', 'cm'],
  temperature_2m_max: ['max', '°C'],
  temperature_2m_min: ['min', '°C'],
  wave_height_max: ['waves', 'm'],
};

function readings(values) {
  const order = ['wind_gusts_10m_max', 'wave_height_max', 'precipitation_sum',
                 'snowfall_sum', 'temperature_2m_max', 'temperature_2m_min'];
  const parts = order.filter((k) => values && values[k] != null)
    .filter((k) => !(k === 'snowfall_sum' && !values[k]))
    .map((k) => `${READING_WORD[k][0]} ${values[k]} ${READING_WORD[k][1]}`);
  return parts.length ? parts.join(' · ') : '—';
}

/* How the freight reaches its port, priced (engine/fast/precarriage.py).
 * One row per chain; the route is drawn on the chosen one. When the board
 * has a low-water derate at Kaub, the same chains at today's surcharge. */
const CHF = (n) => `CHF ${Math.round(n).toLocaleString('en-US')}`;

function evidence(label, e) {
  if (!e || !(e.site || e.name)) return '';
  const url = safeUrl(e.source);
  return `<li title="${esc(e.basis || '')}"><span class="muted">${esc(label)}</span> <b>${esc(e.site || e.name)}</b>
    ${url ? ` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">source</a>` : ''}</li>`;
}

function precarriageHTML(pre) {
  if (!pre || !(pre.chains || []).length) return '';
  const today = pre.today;
  const todayCost = {};
  for (const c of (today && today.chains) || []) todayCost[c.id] = c.cost_chf;
  const rows = pre.chains.map((c) => `
    <tr class="${c.id === pre.chosen ? 'is-chosen' : ''}">
      <td>${esc(c.id)}${c.id === pre.chosen ? ' <span class="muted">✓ used</span>' : ''}</td>
      <td class="num">${CHF(c.cost_chf)}</td>
      <td class="num">${Math.round(c.hours)} h</td>
      ${today ? `<td class="num">${todayCost[c.id] !== undefined ? CHF(todayCost[c.id]) : '—'}</td>` : ''}
    </tr>`).join('');
  const switchNote = today && today.chosen && today.chosen !== pre.chosen
    ? `<p class="rt-pre-switch" title="${esc(today.because)}">At today's Kaub level, <b>${esc(today.chosen)}</b>
         is cheaper (barge ×${today.surcharge}).</p>`
    : '';
  const method = 'Priced from published rates: ASNAV operator costs per tonne-km, the Swiss '
    + 'heavy-vehicle fee, German and Italian tolls, a transfer per change of mode. The cheapest '
    + 'is used unless another is within 5% and faster.';
  return `
    <h3 title="${esc(method)}">How the freight reaches ${esc(pre.port_name || pre.port || 'the port')} ⓘ</h3>
    <table class="rt-pre">
      <thead><tr><th>chain</th><th class="num">per container</th><th class="num">time</th>
        ${today ? '<th class="num">at today\'s Kaub</th>' : ''}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${switchNote}`;
}

function renderReal(v) {
  const d = v.real_data || {};
  const block = $('rt-real-block');
  if (!block || !d.focus) return;
  block.hidden = false;
  $('rt-real-note').textContent = `${d.real} of ${d.of} sources real at this as-of`;

  const volume = d.flow_documents != null
    ? `<li><span class="muted">Sika export</span> <b>${Number(d.flow_documents).toLocaleString('en-US')} documents</b>
        ${esc((d.flow || '').replace('_', ' → '))} <span class="muted">(this machine only)</span></li>`
    : '';
  // What public customs records show for this flow, one line each, with the
  // pages they were read from.
  const trade = (d.trade_records || []).length ? `
    <div class="rt-trade"><b>Customs records</b>
      <ul>${d.trade_records.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>
      <span class="muted">${(d.trade_sources || []).map((u) => safeUrl(u))
        .filter(Boolean).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(new URL(u).hostname.replace(/^www\./, ''))}</a>`).join(' · ')}</span>
    </div>` : '';
  // Every burst of small orders on this flow in Sika's order book, and the
  // public event that followed it, if one did: the check that the sign
  // comes before the crisis. Only from the real book, never the sample.
  const bh = v.burst_history || [];
  const bursts = bh.length ? `
    <div class="rt-trade"><b>Bursts of small orders on this flow</b>
      <span class="muted">· Sika's order book</span>
      <ul>${bh.map((b) => `<li>${esc(b.day)}: ${b.orders} orders (usual ${b.usual})${b.followed_by
        ? ` <b>→ ${b.followed_by.days_later} day(s) later: ${esc(b.followed_by.what)}</b>` : ''}</li>`).join('')}</ul>
      <span class="muted">A reason to look, not a verdict: bursts also follow year-end ordering and campaigns.</span>
    </div>` : '';
  const chosen = `<ul class="rt-facts">${evidence('Starts at', d.origin)}${evidence('Leaves by', d.port)}${evidence('Received by', d.destination)}${volume}</ul>
    ${trade}
    ${bursts}
    ${precarriageHTML(d.precarriage)}`;
  const sources = (d.sources || []).map((s) => `
    <li class="${s.real ? 'is-real' : 'is-not'}" title="${esc(s.detail || s.status)}">
      <b>${s.real ? '✓' : '○'} ${esc(s.label)}</b>
    </li>`).join('');

  const places = (v.conditions || []);
  const conditions = places.length ? `
    <table class="rtable rt-cond">
      <thead><tr><th>Place</th><th>Last day read</th><th>Observed</th><th>Next 7 days (max)</th></tr></thead>
      <tbody>${places.map((c) => `
        <tr>
          <td>${esc(c.name)}${c.marine_at ? `<br><span class="muted">sea read at ${esc(c.marine_at)}</span>` : ''}</td>
          <td>${esc(c.observed_day || '—')}</td>
          <td>${esc(readings(c.observed))}</td>
          <td>${c.forecast_from ? esc(readings({ ...c.forecast_max, ...c.forecast_min })) : '<span class="muted">not recorded</span>'}</td>
        </tr>`).join('')}
      </tbody>
    </table>`
    : '<p class="muted">No conditions recorded yet.</p>';

  const operators = (d.operators || []).map((o) => {
    const url = safeUrl(o.website);
    const checked = safeUrl(o.source);
    const hover = [o.note, checked ? `Checked ${o.checked || ''} against its own page.` : ''].filter(Boolean).join(' ');
    return `
      <li title="${esc(hover)}">
        <b>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(o.name)}</a>` : esc(o.name)}</b>
        <span class="muted">${esc(o.legs || o.role || '')}</span>
      </li>`;
  }).join('');

  $('rt-real').innerHTML = `
    ${d.why ? `<p>${esc(d.why)}</p>` : ''}
    ${chosen}
    <div class="rt-real-grid">
      <div>
        <h3>Sources</h3>
        <ul class="rt-real-list">${sources}</ul>
      </div>
      <div>
        <h3>Who runs these legs</h3>
        ${operators ? `<ul class="rt-real-list">${operators}</ul>` : '<p class="muted">None listed yet.</p>'}
      </div>
    </div>
    ${(d.notes || []).map((n) => {
      // The first sentence on the page; the rest one click away.
      const text = String(n.text || '');
      const cut = text.indexOf('. ');
      const head = cut > 0 ? text.slice(0, cut + 1) : text;
      const rest = cut > 0 ? text.slice(cut + 2) : '';
      return `
      <div class="rt-note">
        <b>On the corridor, ${esc(n.date || '')}</b>
        <span>${esc(head)}</span>
        ${rest ? `<details><summary>more</summary><span>${esc(rest)}</span></details>` : ''}
        ${safeUrl(n.source) ? `<a class="muted" href="${esc(safeUrl(n.source))}" target="_blank" rel="noopener noreferrer">source</a>` : ''}
      </div>`;
    }).join('')}
    <details class="rt-cond-wrap">
      <summary>Weather at each place <span class="muted">${places.length} place${places.length === 1 ? '' : 's'}</span></summary>
      ${conditions}
    </details>
    <p class="muted">Assumed: ${esc((d.assumed || []).join(' · '))}</p>`;
}

/* How each event is judged (engine/export/board.py::_judgement).
 *
 * The kind says what is uncertain about it. The three points are the delay
 * it adds if it hits, judged in delay_model.yaml. The survival counts are
 * Time-to-Recover against Time-to-Survive: how many shipments break their
 * promise in the best, likely and worst case. A warning nobody can price
 * the odds of carries the cost-loss break-even instead of a probability. */
const SURVIVAL_ORDER = ['late_already', 'late_best', 'late_likely', 'late_worst', 'on_time'];

function days(d) {
  if (d === null || d === undefined) return '—';
  if (d < 1) return `${Math.round(d * 24)} h`;
  return `${Math.round(d * 10) / 10} d`;
}

function renderJudgement(v) {
  const events = (v.events || []).filter((e) => e.kind);
  if (!events.length) return;
  $('rt-judge-block').hidden = false;
  $('rt-judge').innerHTML = events.map((e) => {
    const counts = e.survival || {};
    const labels = e.survival_labels || {};
    const verdicts = SURVIVAL_ORDER.filter((k) => counts[k]).map((k) =>
      `<span class="sv sv-${k}"><b>${counts[k]}</b> ${esc(labels[k] || k)}</span>`).join('');
    const d = e.delay_days;
    const ends = e.capped_at ? new Date(e.capped_at) : null;
    const cap = ends && !Number.isNaN(ends.getTime())
      ? `<li>Ends by ${ends.toUTCString().slice(0, 22)} UTC</li>` : '';
    const delay = d
      ? `<li title="Judged in delay_model.yaml">Delay if it hits: <b>${days(d.best)}</b> / <b>${days(d.likely)}</b> / <b>${days(d.worst)}</b>
           <span class="muted">best / likely / worst</span></li>${cap}`
      : '<li class="muted">No delay model for this kind of event</li>';
    const surviving = ((e.matrix && e.matrix.points) || [])
      .map((p) => p.time_to_survive_days)
      .filter((t) => t !== null && t !== undefined);
    const tightest = surviving.length
      ? `<li title="Time-to-survive: the most delay before it misses the promised date">Tightest shipment can absorb <b>${days(Math.min(...surviving))}</b></li>`
      : '';
    let odds = '';
    if (e.break_even_probability !== null && e.break_even_probability !== undefined) {
      odds = `<li class="rt-judge-odds" title="Nobody publishes odds for this, so the tool gives the break-even, not a guess (ICD 203 scale).">
        Act if you judge it over <b>${Math.max(1, Math.round(e.break_even_probability * 100))}%</b> likely
        <span class="muted">(${esc(e.break_even_words || '')})</span></li>`;
    } else if (e.kind === 'warning') {
      odds = '<li class="muted">No action pays for itself, even if it happens</li>';
    }
    return `
      <article class="rt-judge kind-${esc(e.kind)}">
        <header>
          <span class="kind-chip kind-${esc(e.kind)}">${esc(e.kind_label || e.kind)}</span>
          <b>${esc(e.title)}</b>
        </header>
        <ul class="rt-judge-list" title="${esc(e.kind_basis || '')}">${delay}${tightest}${odds}</ul>
        ${verdicts ? `<div class="sv-row">${verdicts}</div>` : ''}
      </article>`;
  }).join('');
}

async function boot() {
  const params = new URLSearchParams(location.search);
  const q = new URLSearchParams();
  if (params.get('as_of')) q.set('as_of', params.get('as_of'));
  if (params.get('shipments')) q.set('shipments', params.get('shipments'));

  const res = await fetch(`/api/route/${encodeURIComponent(ROUTE_ID)}?${q}`);
  if (!res.ok) {
    $('rt-name').textContent = 'That route is not on this board';
    return;
  }
  const v = await res.json();
  state.view = v;

  document.title = `${v.name} · Risk Radar`;
  $('rt-name').textContent = v.name;
  $('rt-chip').textContent = v.level_label || v.level;
  $('rt-chip').className = `level-chip level-${v.level}`;
  // The level's rule is the chip's tooltip; the page states one deadline,
  // the real one, in the first tile.
  $('rt-chip').title = v.directive || '';

  statRow(v);
  renderConvoy(v);
  renderReal(v);
  renderJudgement(v);

  const onward = new URLSearchParams({ route: ROUTE_ID });
  if (params.get('as_of')) onward.set('as_of', params.get('as_of'));
  $('rt-next').href = `/tree?${onward}`;
  $('rt-ops').href = `/ops?${onward}`;
  $('rt-back').href = `/?${onward}`;

  // Drawn by charts.js, the same code the board uses. A second copy of this
  // arithmetic would drift, and the day the two disagree is the day a planner
  // stops believing either.
  // Two panes. The legend is shared — the severity bands mean the same
  // thing on both — so it is drawn once, from whichever pane has data.
  const panes = [
    ['measured', v.radar_measured],
    ['reported', v.radar_reported],
  ];
  let legendDrawn = false;
  for (const [name, radar] of panes) {
    if (!radar) continue;
    Charts.drawRadar(radar, {
      svg: `rt-radar-${name}`,
      legend: legendDrawn ? null : 'rt-radar-legend',
      scale: 1.5,
    });
    if ((radar.max || 0) > 0) legendDrawn = true;
    const host = $(`rt-gauges-${name}`);
    if (host) {
      host.innerHTML = Charts.familyGauges(radar, {
        empty: name === 'measured'
          ? 'Nothing measurable is contributing delay here.'
          : 'Nothing reported is contributing delay here.',
      });
      Charts.wireGauges(host);
    }
  }
  const driving = (v.events || []).find((e) => e.event_id === v.driving_event_id)
               || (v.events || [])[0];
  $('rt-matrix').innerHTML = driving && driving.matrix
    ? Charts.buildMatrixGrid(driving, { matrix_grid: v.matrix_grid })
    : '<p class="muted">No event on this route carries a matrix.</p>';
}

boot();
