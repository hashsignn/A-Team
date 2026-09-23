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
const chf = (v) => v == null ? '—' : 'CHF ' + Math.round(v).toLocaleString('en-CH');

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
    ['ACTION BY', v.lead_time_hours == null ? '—'
      : (v.lead_time_hours < 48 ? `${Math.round(v.lead_time_hours)} h`
         : `${Math.round(v.lead_time_hours / 24)} days`),
      'until the first option closes'],
    ['EXPOSURE', chf(v.exposure_chf), `${v.shipments_affected} of ${v.shipments_total} consignments`],
    ['ALREADY HIT', String(v.totals.affected), 'no option left'],
    ['NEED A DECISION', String(v.totals.at_risk), 'still time to change them'],
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
        <b>${Math.round(pr.travelled_km).toLocaleString()} km</b> travelled
        <span class="muted">·</span>
        <b>${Math.round(pr.remaining_km).toLocaleString()} km</b> to go
        <span class="muted">of ${Math.round(pr.total_km).toLocaleString()} km (${pr.percent}%)</span>
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
        <span class="muted">WHERE THE PLAN PUTS IT</span>
        ${plan ? `<b>${coord(plan)}</b> ${mapLink(plan)}` : '<b>—</b>'}
        <em>interpolated along the leg; freight follows roads, not geodesics</em>
      </div>
      <div class="where-col">
        <span class="muted">LAST SEEN BY SOMEBODY</span>
        ${seen ? `<b>${coord(seen)}</b> ${mapLink(seen)}
           <em>${esc(seen.reported_by || 'on site')},
             ${esc(String(seen.observed_at).slice(0, 16).replace('T', ' '))} UTC${
               seen.accuracy_m ? ` · ±${Math.round(seen.accuracy_m)} m` : ''}${
               seen.first_hand === false ? ' · second hand' : ''}</em>`
          : '<b>not reported</b><em>nobody with this consignment has sent a position</em>'}
      </div>
      <div class="where-col">
        <span class="muted">OFF PLAN BY</span>
        ${veh.drift_km == null
          ? '<b>unknown</b><em>needs a position from site to compute</em>'
          : `<b class="${veh.drift_km > 50 ? 'drift-bad' : ''}">${veh.drift_km} km</b>
             <em>between where it should be and where it was seen</em>`}
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
    : `<p class="socket"><b>Nothing reported from the road.</b> Whoever is with
       this consignment can file in four taps at <code>/driver</code> — and a
       first-hand confirmation is what releases a re-route.</p>`;

  $('rt-vehicle-title').textContent = `${veh.shipment_id} — ${WORD[veh.status]}`;
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

  document.title = `${v.name} — Risk Radar`;
  $('rt-name').textContent = v.name;
  $('rt-chip').textContent = v.level_label || v.level;
  $('rt-chip').className = `level-chip level-${v.level}`;
  $('rt-directive').textContent = v.directive || '';

  statRow(v);
  renderConvoy(v);

  const onward = new URLSearchParams({ route: ROUTE_ID });
  if (params.get('as_of')) onward.set('as_of', params.get('as_of'));
  $('rt-next').href = `/ops?${onward}`;

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
