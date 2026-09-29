/* One shipment on its own page (engine/export/route.shipment_view).
 *
 * The route page is every shipment on a route. This is one of them: the
 * vehicle on each stretch of its journey (the truck, the barge, the ship),
 * what was reported from it, where it is, and its own risk: one matrix dot
 * per event that touches it, and the radars cut to those events. Numbers
 * first; the explanations are in the tooltips.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const chf = (v) => (v == null ? '–' : `CHF ${Math.round(v).toLocaleString('en-US')}`);
const km = (v) => `${Math.round(v || 0).toLocaleString('en-US')} km`;
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayMon = (iso) => { const d = new Date(iso); return Number.isFinite(+d) ? `${d.getUTCDate()} ${MON[d.getUTCMonth()]}` : '–'; };
const days = (d) => (d == null ? '–' : d < 1 ? `${Math.round(d * 24)} h` : `${Math.round(d * 10) / 10} d`);
const place = (n) => String(n || '').replace(/\s*\(.*\)\s*$/, '');

const SHIP = decodeURIComponent(location.pathname.split('/shipment/')[1] || '');
const Q = new URLSearchParams(location.search);

const GLYPH = {
  road: '<path d="M1.8 6.2h12v10H1.8z"/><path d="M14.6 9.2h4.3l3.3 3.6v3.4h-7.6z"/><circle cx="6" cy="17.6" r="2.1"/><circle cx="17.5" cy="17.6" r="2.1"/>',
  rail: '<rect x="5" y="3" width="14" height="13.5" rx="3.2"/><circle cx="8.6" cy="13" r="1.25" fill="none"/><path d="M7.5 17.5l-2.5 3.3h2.2l1.8-2.3h6l1.8 2.3H19l-2.5-3.3z"/>',
  barge: '<path d="M1.5 15h21l-2 4H3.5z"/><rect x="3.5" y="10.4" width="4.6" height="4" rx=".4"/><rect x="8.8" y="10.4" width="4.6" height="4" rx=".4"/><rect x="14.1" y="10.4" width="4.6" height="4" rx=".4"/><path d="M19.4 8.6h2v5.8h-2z"/>',
  sea: '<path d="M2.5 14.5h19l-2.6 5.2H5.1z"/><rect x="5" y="10" width="3.6" height="3.6" rx=".4"/><rect x="9.2" y="10" width="3.6" height="3.6" rx=".4"/><rect x="9.2" y="5.8" width="3.6" height="3.6" rx=".4"/><path d="M15 6.5h3.4v7.3H15z"/>',
  air: '<path d="M2 11l18-7-5 7 5 7-18-7z"/>',
};
const icon = (mode) => `<svg viewBox="0 0 24 24" aria-hidden="true">${GLYPH[mode] || GLYPH.road}</svg>`;
const STATUS = { ok: 'clear', at_risk: 'at risk', affected: 'hit now' };

function qs(extra = {}) {
  const q = new URLSearchParams();
  if (Q.get('as_of')) q.set('as_of', Q.get('as_of'));
  if (Q.get('shipments')) q.set('shipments', Q.get('shipments'));
  Object.entries(extra).forEach(([k, v]) => v != null && q.set(k, v));
  return q.toString();
}

function stats(v) {
  const s = v.stats;
  const cells = [
    [chf(s.loss_chf), 'at risk', 'Expected loss if nobody acts, the worst event'],
    [s.p_late == null ? 'no odds' : `${Math.round(s.p_late * 100)}%`, 'chance late', 'P(late) for the worst event'],
    [days(s.survive_days), 'delay it absorbs', 'Time to survive: the delay it takes before it misses the promised date'],
    [dayMon(v.committed), 'promised', 'The date promised to the customer'],
    [String(s.reports), s.reports === 1 ? 'field report' : 'field reports', 'Reported from the vehicle or the site'],
  ];
  $('sp-stats').innerHTML = cells.map(([value, label, why]) => `
    <div class="rt-stat" title="${esc(why)}">
      <div class="rt-stat-value">${esc(value)}</div>
      <div class="rt-stat-note">${esc(label)}</div>
    </div>`).join('');
}

function progress(v) {
  const p = v.progress || {};
  if (!p.total_km) { $('sp-prog').innerHTML = ''; return; }
  $('sp-prog').innerHTML = `
    <div class="prog-bar"><span style="width:${Math.min(100, p.percent)}%"></span></div>
    <div class="sp-prog-n"><b>${p.percent}%</b> · ${km(p.travelled_km)} done · <b>${km(p.remaining_km)}</b> to go</div>`;
}

/* The journey as stretches (the road to Basel, the Rhine, the sea), each
 * with every vehicle on it: the four trucks one by one, the barge, the
 * ship. Grey: the shipment has not reached it yet. A click opens it. */
const STATE_TONE = { done: 'done', waiting: 'wait' };
let DATA = null;
let OPEN = null;

function unitTone(s) { return STATE_TONE[s.state] || s.status; }

function stretches(v) {
  $('sp-veh-n').textContent = String(v.vehicles);
  $('sp-strs').innerHTML = v.stretches.map((s, si) => `
    <div class="sp-str sp-str--${esc(unitTone(s))}">
      <div class="sp-str-head">
        <span class="sp-str-way"><b>${esc(place(s.from))}</b> → <b>${esc(place(s.to))}</b></span>
        <span class="sp-str-n">${km(s.km)} · ${dayMon(s.departs)} → ${dayMon(s.arrives)}</span>
        <span class="sp-state sp-state--${esc(s.state)}">${esc(s.state_word)}</span>
        <span class="sp-str-legs">${s.legs.map((l) => `<span class="sleg sleg--${esc(l.status)}"
          title="${esc(place(l.from))} → ${esc(place(l.to))} · ${km(l.km)} · ${esc(STATUS[l.status] || l.status)}">${esc(place(l.to))}</span>`).join('')}</span>
      </div>
      <div class="sp-units">${s.units.map((u, ui) => `
        <button type="button" class="sp-unit sp-unit--${esc(unitTone(s))}${OPEN === u.asset_id ? ' is-open' : ''}"
                data-unit="${esc(u.asset_id)}" data-s="${si}" data-u="${ui}" aria-expanded="${OPEN === u.asset_id}"
                title="${esc(u.name)} · ${esc(s.state_word)}">
          <span class="sp-unit-i">${icon(u.mode)}</span>
          <span class="sp-unit-n"><b>${esc(u.name)}</b><span class="mono">${esc(u.asset_id)}</span></span>
          <span class="sp-unit-teu">${u.ours_teu} TEU</span>
          ${u.reports.length ? `<span class="sp-unit-rep" title="Field reports from it">${u.reports.length}</span>` : ''}
        </button>`).join('')}</div>
    </div>`).join('');
  document.querySelectorAll('.sp-unit').forEach((btn) => btn.addEventListener('click', () => {
    OPEN = OPEN === btn.dataset.unit ? null : btn.dataset.unit;
    stretches(DATA);
    unitCard(DATA, +btn.dataset.s, +btn.dataset.u);
  }));
}

/* One vehicle: what it carries, who runs it, where it is, what it said. */
function unitCard(v, si, ui, scroll = true) {
  const host = $('sp-unit');
  if (!OPEN) { host.hidden = true; host.innerHTML = ''; return; }
  const s = v.stretches[si];
  const u = s.units[ui];
  const pos = u.position;
  const c = (p) => (p ? `${p.lat.toFixed(3)}, ${p.lon.toFixed(3)}` : '–');
  const tile = (value, k, why = '') => `<div class="sp-t" title="${esc(why)}"><b>${value}</b><span>${esc(k)}</span></div>`;
  const report = new URLSearchParams({ shipment: v.shipment_id, vehicle: u.asset_id });
  host.hidden = false;
  host.innerHTML = `
    <header>
      <span class="sp-unit-i sp-unit--${esc(unitTone(s))}">${icon(u.mode)}</span>
      <span class="sp-unit-n"><b>${esc(u.name)}</b><span class="mono">${esc(u.asset_id)} · ${esc(u.crew.name)}, ${esc(u.crew.role)}${u.crew.verified ? ' ✓' : ''}</span></span>
      <span class="sp-state sp-state--${esc(s.state)}">${esc(s.state_word)}</span>
    </header>
    <div class="sp-ts">
      ${tile(`${u.ours_teu} TEU`, 'ours on it')}
      ${tile(u.mode === 'road' ? `${u.capacity_teu} TEU` : `${u.loaded_teu}/${u.capacity_teu}`, u.mode === 'road' ? 'truck holds' : 'TEU loaded', u.mode === 'road' ? '' : 'Loaded of capacity, with other shippers\' freight')}
      ${u.usable_teu != null ? tile(`${u.usable_teu} TEU`, 'usable now', 'What the water level lets it carry today') : ''}
      ${tile(String(u.boxes.length), u.boxes.length === 1 ? 'box' : 'boxes')}
      ${pos ? tile(pos.drift_km == null ? '–' : `${pos.drift_km} km`, 'off plan', 'Last reported position against the planned one') : tile(dayMon(s.departs), s.state === 'done' ? 'left' : 'leaves')}
    </div>
    <div class="sp-bxs">${u.boxes.map((b) => `<span class="sp-bx${b.priority === 'critical' ? ' is-crit' : ''}"
        title="${esc(b.content)} · ${b.gross_t} t · ${esc(b.priority)} · due ${esc(dayMon(b.deadline))}">${b.priority === 'critical' ? '<i></i>' : ''}<span class="mono">${esc(b.container_id)}</span> ${b.size_ft}' · ${esc(dayMon(b.deadline))}</span>`).join('')}</div>
    ${pos ? `<p class="sp-pos">planned <b>${esc(c(pos.planned))}</b> · last seen <b>${esc(c(pos.seen))}</b></p>` : ''}
    <div class="sp-reps">
      <div class="sp-reps-h"><b>Reports from it</b> <span class="rtab-n">${u.reports.length || ''}</span>
        <a class="ctl ctl--mini" href="/driver?${report}" target="_blank" rel="noopener">File one from ${esc(u.name)}</a></div>
      ${u.reports.length ? `<div class="rep-list">${u.reports.map(reportHTML).join('')}</div>` : '<p class="muted sp-none">None yet.</p>'}
    </div>`;
  if (scroll) host.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function reportHTML(r) {
  return `
    <div class="rep">
      <div class="rep-top">
        <span class="tag ${r.load_state === 'damaged' ? 'tag--warn' : 'tag--ok'}">${esc(r.status)}</span>
        <b>${esc(r.role_label || 'on site')}</b>
        <span class="muted">${esc(String(r.observed_at).slice(0, 16).replace('T', ' '))} UTC</span>
        ${r.first_hand === false ? '<span class="tag tag--off">second hand</span>' : ''}
        ${r.placed_by_time ? '<span class="tag tag--off" title="The report did not say which vehicle; it is shown on the one carrying the freight then">by time</span>' : ''}
      </div>
      <div class="rep-line"><b>Load:</b> ${esc(r.load_state)}${r.position ? ` · ${esc(r.position)}` : ''}</div>
      ${r.note ? `<div class="rep-note">“${esc(r.note)}”</div>` : ''}
      ${(r.photos || []).length ? `<div class="rep-shots">${r.photos.map((ph) => {
        const id = typeof ph === 'string' ? ph : ph.id;
        const o = (typeof ph === 'object' && ph.orientation) || 1;
        return `<a href="/api/v1/photos/${esc(id)}" target="_blank" rel="noopener"><img class="shot-o${o}" src="/api/v1/photos/${esc(id)}" alt="photo from site" loading="lazy"></a>`;
      }).join('')}</div>` : ''}
    </div>`;
}

function events(v) {
  if (!(v.events || []).length) return;
  $('sp-events-block').hidden = false;
  const head = `<div class="sp-ev sp-ev--head" aria-hidden="true"><span></span><span></span>
    <span class="sp-ev-n">delay if it hits</span><span class="sp-ev-n">at risk</span><span class="sp-ev-n">chance late</span><span></span></div>`;
  $('sp-events').innerHTML = head + v.events.map((e) => {
    const p = e.point || {};
    const d = e.delay_days;
    return `
    <div class="sp-ev" title="${esc(e.title)}">
      <span class="kind-chip kind-${esc(e.kind)}">${esc(e.kind_label || e.kind || 'Event')}</span>
      <span class="sp-ev-t">${esc(e.title)}</span>
      <span class="sp-ev-n" title="Delay if it hits: best / likely / worst">${d ? `${days(d.best)} / <b>${days(d.likely)}</b> / ${days(d.worst)}` : '–'}</span>
      <span class="sp-ev-n" title="Expected loss from this event">${chf(p.expected_loss_chf)}</span>
      <span class="sp-ev-n" title="Chance late from this event">${p.p_late == null ? 'no odds' : `${Math.round(p.p_late * 100)}%`}</span>
      <span class="sp-ev-n" title="Nobody publishes odds for this: act if you judge it more likely than this">${e.break_even_probability != null ? `act &gt; ${Math.max(1, Math.round(e.break_even_probability * 100))}%` : ''}</span>
    </div>`;
  }).join('');
}

async function boot() {
  const res = await fetch(`/api/shipment/${encodeURIComponent(SHIP)}?${qs()}`);
  if (!res.ok) { $('sp-name').textContent = 'That shipment is not on this board'; return; }
  const v = await res.json();
  document.title = `${v.shipment_id} · Horizon`;
  $('sp-name').innerHTML = `${esc(v.shipment_id)} · ${esc(v.customer)}${v.tier === 'A' ? ' <span class="sp-key" title="Key account">★</span>' : ''}`;
  $('sp-route').textContent = v.route_name;
  $('sp-chip').textContent = v.level_label || v.level;
  $('sp-chip').className = `level-chip level-${v.level}`;
  $('sp-back').href = `/?${qs({ route: v.route_id })}`;
  $('sp-next').href = `/tree?${qs({ route: v.route_id, ship: v.shipment_id })}`;

  DATA = v;
  stats(v);
  progress(v);
  // The vehicle it is on now opens first: that is where a question starts.
  const nowAt = v.stretches.findIndex((s) => !['done', 'waiting'].includes(s.state));
  if (nowAt >= 0 && v.stretches[nowAt].units.length) OPEN = v.stretches[nowAt].units[0].asset_id;
  stretches(v);
  if (OPEN) unitCard(v, nowAt, 0, false);
  events(v);

  $('sp-matrix').innerHTML = (v.events || []).length && v.matrix_grid
    ? Charts.buildMatrixGrid({ matrix: { points: v.events.map((e) => e.point) } },
      { matrix_grid: v.matrix_grid, noun: 'event' })
    : '<p class="muted">No event touches this shipment.</p>';

  let legendDrawn = false;
  for (const [name, radar] of [['measured', v.radar_measured], ['reported', v.radar_reported]]) {
    if (!radar) continue;
    Charts.drawRadar(radar, { svg: `sp-radar-${name}`, legend: legendDrawn ? null : 'sp-radar-legend', scale: 1.5 });
    if ((radar.max || 0) > 0) legendDrawn = true;
    const host = $(`sp-gauges-${name}`);
    if (host) {
      host.innerHTML = Charts.familyGauges(radar, { empty: 'Nothing here adds delay.' });
      Charts.wireGauges(host);
    }
  }
}

boot();
