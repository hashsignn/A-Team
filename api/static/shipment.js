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
const PHASE = { done: 'done', now: 'now', ahead: 'next' };

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

/* One card per vehicle: the truck, the barge, the ship. A run of legs in
 * one mode is one vehicle; its stretches are the pills under it. */
function vehicles(v) {
  $('sp-vehs').innerHTML = v.vehicles.map((x) => `
    <article class="sp-veh sp-veh--${esc(x.status)}${x.phase === 'now' ? ' is-now' : ''}${x.phase === 'done' ? ' is-done' : ''}">
      <header>
        <span class="sp-veh-i" title="${esc(STATUS[x.status] || x.status)}">${icon(x.mode)}</span>
        <span class="sp-veh-n"><b>${esc(x.vehicle)}</b><span class="mono">${esc(x.asset_id)}</span></span>
        <span class="sp-phase sp-phase--${esc(x.phase)}">${esc(PHASE[x.phase] || x.phase)}</span>
      </header>
      <div class="sp-veh-way"><b>${esc(place(x.from))}</b> → <b>${esc(place(x.to))}</b></div>
      <div class="sp-veh-nums">
        <span title="Distance">${km(x.km)}</span>
        <span title="Planned departure → planned arrival">${dayMon(x.departs)} → ${dayMon(x.arrives)}</span>
      </div>
      <div class="sp-veh-legs">${x.legs.map((l) => `<span class="sleg sleg--${esc(l.status)}"
          title="${esc(place(l.from))} → ${esc(place(l.to))} · ${km(l.km)} · ${esc(STATUS[l.status] || l.status)}">${esc(place(l.to))}</span>`).join('')}</div>
      <footer title="${esc(x.carrier || '')}">${esc(x.crew.name)} · ${esc(x.crew.role)}</footer>
    </article>`).join('');
}

function where(v) {
  const pos = v.position || {};
  const c = (p) => (p ? `${p.lat.toFixed(2)}, ${p.lon.toFixed(2)}` : '–');
  $('sp-where').innerHTML = `planned <b>${esc(c(pos.planned))}</b> · seen <b>${esc(c(pos.seen))}</b>${
    pos.drift_km != null ? ` · <b class="${pos.drift_km > 50 ? 'drift-bad' : ''}">${pos.drift_km} km</b> off plan` : ''}`;
}

function reports(v) {
  const n = (v.reports || []).length;
  $('sp-rep-n').textContent = n ? String(n) : '';
  $('sp-reports').innerHTML = n ? `<div class="rep-list">${v.reports.map((r) => `
    <div class="rep">
      <div class="rep-top">
        <span class="tag ${r.load_state === 'damaged' ? 'tag--warn' : 'tag--ok'}">${esc(r.status)}</span>
        <b>${esc(r.role_label || 'on site')}</b>
        <span class="muted">${esc(String(r.observed_at).slice(0, 16).replace('T', ' '))} UTC</span>
        ${r.first_hand === false ? '<span class="tag tag--off">second hand</span>' : ''}
      </div>
      <div class="rep-line"><b>Load:</b> ${esc(r.load_state)}${r.position ? ` · ${esc(r.position)}` : ''}</div>
      ${r.note ? `<div class="rep-note">“${esc(r.note)}”</div>` : ''}
      ${(r.photos || []).length ? `<div class="rep-shots">${r.photos.map((ph) => {
        const id = typeof ph === 'string' ? ph : ph.id;
        const o = (typeof ph === 'object' && ph.orientation) || 1;
        return `<a href="/api/v1/photos/${esc(id)}" target="_blank" rel="noopener"><img class="shot-o${o}" src="/api/v1/photos/${esc(id)}" alt="photo from site" loading="lazy"></a>`;
      }).join('')}</div>` : ''}
    </div>`).join('')}</div>`
    : '<p class="muted sp-none">None yet. <a href="/driver">File one</a>.</p>';
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

  stats(v);
  progress(v);
  vehicles(v);
  where(v);
  reports(v);
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
