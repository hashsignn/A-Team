/* Operations: playbook, consignments, execute.
 *
 * The checklist ticks live in localStorage, deliberately. "Has Maria called
 * the carrier yet" is per-planner working state, not a fact about the world,
 * and putting it in the engine would make the board's answer depend on who
 * was looking at it. It is a per-viewer convenience, so every read and write
 * is wrapped — it can come back empty in a private window and the page has to
 * render correctly when it does.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const chf = (v) => v == null ? '—' : 'CHF ' + Math.round(v).toLocaleString('en-CH');

function hours(h) {
  if (h == null) return '—';
  if (h < 0) return 'passed';
  if (h < 48) return Math.round(h) + ' h';
  return Math.round(h / 24) + ' d';
}

const params = new URLSearchParams(location.search);
const AS_OF = params.get('as_of') || '2026-09-18T06:00:00+00:00';
const SHIPMENTS = params.get('shipments') || '150';
const ROUTE = params.get('route');
const qs = () => `as_of=${encodeURIComponent(AS_OF)}&shipments=${SHIPMENTS}`;

const state = { route: ROUTE, shipment: null, flow: null, cargo: null };

// ---------------------------------------------------------------
// Tick state
// ---------------------------------------------------------------
const tickKey = (route) => `scrr.flow.${route}`;

function readTicks(route) {
  try {
    const raw = localStorage.getItem(tickKey(route));
    return new Set(raw ? JSON.parse(raw) : []);
  } catch { return new Set(); }
}

function writeTicks(route, ticks) {
  try {
    localStorage.setItem(tickKey(route), JSON.stringify([...ticks]));
  } catch { /* private window — the page still works, it just forgets */ }
}

// ===============================================================
// Playbook
// ===============================================================
async function loadPlaybook() {
  if (!state.route) { $('view-playbook').innerHTML = noRoute(); return; }
  const ticks = [...readTicks(state.route)].join(',');
  const res = await fetch(
    `/api/flow/${encodeURIComponent(state.route)}?${qs()}&completed=${encodeURIComponent(ticks)}`
  );
  state.flow = await res.json();
  renderPlaybook();
}

function renderPlaybook() {
  const f = state.flow;
  const ticks = readTicks(state.route);

  const steps = f.stages.map((s, i) => `
    <div class="pb-step${s.reached ? ' is-reached' : ''}${s.stage === f.stage ? ' is-current' : ''}">
      <span class="pb-step-n">${i + 1}</span>
      <span>${esc(s.title)}</span>
    </div>`).join('');

  const byStage = {};
  for (const task of f.tasks) (byStage[task.stage] ||= []).push(task);

  const groups = f.stages.map((s) => {
    const tasks = byStage[s.stage] || [];
    if (!tasks.length) return '';
    return `
      <div class="pb-group${s.stage === f.stage ? ' is-current' : ''}">
        <h3>${esc(s.title)}</h3>
        ${tasks.map((t) => renderTask(t, ticks)).join('')}
      </div>`;
  }).join('');

  // The gate, stated before the actions rather than after them.
  const gate = `
    <div class="pb-gate ${f.gate_open ? 'is-open' : 'is-shut'}">
      <b>${f.gate_open ? 'Unlocked' : 'Locked'}</b>
      <span>${esc(f.gate_reason)}</span>
    </div>`;

  const actions = f.actions.length ? `
    <div class="psec">
      <h2>Mitigations</h2>
      <p class="psec-note">Withheld rather than greyed while the gate is shut:
        an option a planner can see and click is an option they will click.
        Notifying a customer is available throughout.</p>
      ${groupActions(f.actions).map((g) => `
        <div class="pb-action${g.locked ? ' is-locked' : ''}">
          <div class="pb-action-top">
            <span>${g.locked ? lockIcon() : ''}${esc(g.label)}${g.count > 1
              ? `<em class="pb-action-n">× ${g.count} consignments</em>` : ''}</span>
            <span class="num">${chf(g.value_chf)}</span>
          </div>
          <div class="pb-action-why">${g.locked
            ? 'Locked until confirmed.'
            : esc(g.sentence || '')}</div>
        </div>`).join('')}
    </div>` : '';

  $('view-playbook').innerHTML = `
    <div class="psec">
      <h2>${esc(f.route.name)}</h2>
      <p class="psec-note">A deterministic, auditable flow. The engine owns the
        steps and the gate; the ticks are yours and stay in this browser.</p>
      <div class="pb-steps">${steps}</div>
      <div class="pb-progress"><i style="width:${(f.progress * 100).toFixed(0)}%"></i></div>
      <div class="pb-rule">${esc(f.stage_rule)}</div>
    </div>
    ${gate}
    <div class="psec">${groups}</div>
    ${actions}`;

  $('view-playbook').querySelectorAll('[data-task]').forEach((box) => {
    box.addEventListener('change', () => {
      const current = readTicks(state.route);
      if (box.checked) current.add(box.dataset.task);
      else current.delete(box.dataset.task);
      writeTicks(state.route, current);
      loadPlaybook();
    });
  });
}

function renderTask(task, ticks) {
  const blocked = Boolean(task.blocked_reason);
  return `
    <label class="pb-task${blocked ? ' is-blocked' : ''}">
      <input type="checkbox" data-task="${esc(task.id)}"
             ${ticks.has(task.id) ? 'checked' : ''}
             ${blocked ? 'disabled' : ''}>
      <span class="pb-task-body">
        <span class="pb-task-label">
          ${esc(task.label)}
          ${task.sla_hours != null
            ? `<b>Within ${hours(task.sla_hours)}</b>` : ''}
          <em>${esc(task.owner)}</em>
        </span>
        <span class="pb-task-note">${esc(task.note)}</span>
        ${blocked
          ? `<span class="pb-task-blocked">${esc(task.blocked_reason)}</span>`
          : ''}
      </span>
    </label>`;
}

/* Group the action list by what the action actually IS.
 *
 * The board emits one action per consignment, so a lane with eight exposed
 * shipments produces eight rows reading "Switch barge leg to rail" that
 * differ only in their CHF figure. That is correct data and unreadable as a
 * playbook: a planner takes ONE decision here and applies it to the freight
 * it fits.
 *
 * The reason for a lock is stated ONCE, in the gate banner above. Repeating
 * the same sentence on every row buries the one thing that differs between
 * them, which is the value.
 */
function groupActions(actions) {
  const groups = new Map();
  for (const a of actions) {
    const key = `${a.action_type}|${a.label}`;
    const g = groups.get(key) || {
      label: a.label,
      action_type: a.action_type,
      locked: a.locked,
      sentence: a.sentence,
      value_chf: 0,
      count: 0,
    };
    g.value_chf += a.value_chf || 0;
    g.count += 1;
    // If any instance is locked the group is locked: a partially available
    // mitigation is not something to offer as available.
    g.locked = g.locked || a.locked;
    groups.set(key, g);
  }
  return [...groups.values()].sort((a, b) => b.value_chf - a.value_chf);
}

function lockIcon() {
  return `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"
    style="margin-right:5px;vertical-align:-1px">
    <rect x="3" y="7" width="10" height="7" rx="1.5" fill="currentColor"/>
    <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor"
          stroke-width="1.5"/></svg>`;
}

// ===============================================================
// Consignments
// ===============================================================
async function loadCargo() {
  if (!state.route) { $('view-cargo').innerHTML = noRoute(); return; }
  const res = await fetch(`/api/cargo/${encodeURIComponent(state.route)}?${qs()}`);
  state.cargo = await res.json();
  renderCargo();
}

function renderCargo() {
  const c = state.cargo;
  if (c.error) { $('view-cargo').innerHTML = `<div class="psec"><p>${esc(c.error)}</p></div>`; return; }
  const s = c.summary;

  const rows = c.consignments.map((x) => `
    <tr class="${x.touched ? '' : 'is-untouched'}" data-shipment="${esc(x.shipment_id)}">
      <td>
        <div class="r-route">${esc(x.shipment_id)}</div>
        <div class="r-focus">${esc(x.customer)}</div>
      </td>
      <td class="muted">${esc(x.mode)}</td>
      <td class="num">${chf(x.value_chf)}</td>
      <td class="num">${x.touched ? hours(x.lead_time_hours) : '<span class="r-dash">not touched</span>'}</td>
      <td>${x.touched
        ? `<span class="tag ${x.actionability === 'too_late' ? 'tag--warn' : 'tag--ok'}">${esc(x.actionability)}</span>`
        : '<span class="tag tag--off">out of scope</span>'}</td>
      <td class="num">${x.touched ? chf(x.expected_loss_chf) : '—'}</td>
      <td class="r-driver">${esc(x.driving_event || '')}</td>
    </tr>`).join('');

  $('view-cargo').innerHTML = `
    <div class="psec">
      <h2>${esc(c.name)}</h2>
      <p class="psec-note">One disruption on a lane reaches only some of the
        freight using it, and reaches it differently. That has always been
        true in the engine — the gate is per event, per shipment, per leg —
        and this is the first page that shows it.</p>
      <div class="pstat-row">
        <div class="pstat"><div class="pstat-v num">${s.total}</div>
          <div class="pstat-l">consignments</div></div>
        <div class="pstat"><div class="pstat-v num">${s.touched}</div>
          <div class="pstat-l">touched</div>
          <div class="pstat-s">${s.untouched} are out of scope entirely</div></div>
        <div class="pstat"><div class="pstat-v num">${s.distinct_deadlines}</div>
          <div class="pstat-l">distinct deadlines</div>
          <div class="pstat-s">if this were 1, a lane colour would be enough</div></div>
        <div class="pstat"><div class="pstat-v num">${chf(s.exposure_chf)}</div>
          <div class="pstat-l">expected loss</div></div>
      </div>
    </div>
    <div class="psec">
      <p class="psec-note">Click a row to open the execute view for that
        consignment.</p>
      <div class="table-wrap"><table class="rtable">
        <thead><tr>
          <th>Consignment</th><th>Mode</th><th class="num">Value</th>
          <th class="num">Decide by</th><th>State</th>
          <th class="num">Expected loss</th><th>Driven by</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>`;

  $('view-cargo').querySelectorAll('[data-shipment]').forEach((row) => {
    row.addEventListener('click', () => {
      state.shipment = row.dataset.shipment;
      switchView('execute');
    });
  });
}

// ===============================================================
// Execute
// ===============================================================
async function loadExecute() {
  if (!state.shipment) {
    $('view-execute').innerHTML = `<div class="psec">
      <h2>Pick a consignment</h2>
      <p class="psec-note">This view is scoped to one shipment. Choose one on
        the Consignments tab.</p></div>`;
    return;
  }
  const res = await fetch(`/api/execute/${encodeURIComponent(state.shipment)}?${qs()}`);
  if (!res.ok) {
    $('view-execute').innerHTML = `<div class="psec"><p>Not found.</p></div>`;
    return;
  }
  renderExecute(await res.json());
}

function renderExecute(v) {
  const legs = v.legs.map((l) => `
    <div class="xleg${l.affected ? ' is-affected' : ''}">
      <div class="xleg-top">
        <b>${esc(l.from_name)} → ${esc(l.to_name)}</b>
        <span class="tag ${l.affected ? 'tag--warn' : 'tag--off'}">${esc(l.mode)}${l.affected ? ' · affected' : ''}</span>
      </div>
      <div class="xleg-meta">${esc(l.carrier)} ·
        ${esc(l.planned_depart.slice(0, 16).replace('T', ' '))} →
        ${esc(l.planned_arrive.slice(0, 16).replace('T', ' '))}
        · ${l.buffer_hours} h buffer</div>
    </div>`).join('');

  const w = v.what_is_happening;
  const d = v.your_deadline;

  $('view-execute').innerHTML = `
    <div class="psec">
      <h2>${esc(v.shipment_id)} — ${esc(v.customer)}</h2>
      <p class="psec-note">Everything here was computed once, for the
        planner's board. Nothing on this page is recalculated — a second
        calculation would eventually disagree with the first.</p>
      <div class="pstat-row">
        <div class="pstat"><div class="pstat-v">${esc(v.status.level_label || '—')}</div>
          <div class="pstat-l">status</div>
          <div class="pstat-s">${esc(v.status.what_it_means || '')}</div></div>
        <div class="pstat"><div class="pstat-v num">${d ? hours(d.hours) : '—'}</div>
          <div class="pstat-l">decide by</div>
          <div class="pstat-s">${d ? esc(d.note) : 'no decision outstanding'}</div></div>
        <div class="pstat"><div class="pstat-v">${esc(v.shipment.carrier)}</div>
          <div class="pstat-l">carrier</div>
          <div class="pstat-s">${esc(v.shipment.mode)}${v.shipment.dangerous_goods ? ' · ADR' : ''}${v.shipment.temperature_controlled ? ' · temperature-controlled' : ''}</div></div>
      </div>
    </div>

    ${w ? `<div class="psec">
      <h2>What is happening</h2>
      <div class="xevent">
        <b>${esc(w.title)}</b>
        <div class="xleg-meta">${esc(w.where)} · from
          ${esc(w.starts_at.slice(0, 16).replace('T', ' '))}
          · ${esc(w.source)} (tier ${w.source_tier})</div>
      </div>
    </div>` : ''}

    <div class="psec">
      <h2>The route</h2>
      <p class="psec-note">Only some hops are affected. A Rhine low-water
        event hits the barge legs and leaves the road leg alone.</p>
      ${legs}
    </div>

    <div class="psec">
      <h2>Coordinate with</h2>
      ${v.coordinate_with.route_manager ? `<div class="contact">
        <div class="contact-top"><span class="contact-name">${esc(v.coordinate_with.route_manager.name)}</span></div>
        <div class="contact-role">${esc(v.coordinate_with.route_manager.role || '')}</div>
        <div class="contact-links">${v.coordinate_with.route_manager.email
          ? `<a href="mailto:${esc(v.coordinate_with.route_manager.email)}">${esc(v.coordinate_with.route_manager.email)}</a>` : ''}</div>
      </div>` : ''}
      <div class="contact">
        <div class="contact-top"><span class="contact-name">${esc(v.coordinate_with.carrier)}</span></div>
        <div class="contact-role">carrier on this move</div>
      </div>
    </div>

    <div class="psec">
      <h2>From the field</h2>
      <p class="psec-note">The only <b>tier-1 observed</b> source here. Every
        other input describes a region; a driver looking at their own trailer
        is looking at the freight.</p>
      <div id="x-reports">checking…</div>
      <div class="d-ops" style="padding:12px 0 0">
        <a class="ctl ctl--primary" target="_blank" rel="noopener"
           href="/driver?shipment=${encodeURIComponent(v.shipment_id)}&as_of=${encodeURIComponent(AS_OF)}">
          Open the driver app for this consignment</a>
      </div>
    </div>`;

  loadReports(v.shipment_id);
  watchReports(v.shipment_id);
}

/* Reports already filed on this consignment, and a live tail.
 *
 * The list is loaded from the append-only log, which is the record. The
 * stream is only a notification: a client that was disconnected re-reads the
 * log rather than expecting an in-memory buffer to have held its events.
 */
async function loadReports(shipmentId) {
  const el = document.getElementById('x-reports');
  if (!el) return;
  try {
    const res = await fetch(
      `/api/v1/reports?shipment_id=${encodeURIComponent(shipmentId)}&as_of=${encodeURIComponent(AS_OF)}`
    );
    const data = await res.json();
    if (!data.reports.length) {
      el.innerHTML = `<div class="socket">Nothing reported from the field on
        this consignment yet.</div>`;
      return;
    }
    el.innerHTML = data.reports.slice().reverse().map(reportCard).join('');
  } catch {
    el.innerHTML = `<div class="socket">Could not read the report log.</div>`;
  }
}

function reportCard(r) {
  return `<div class="xreport${r.confirms_disruption ? ' is-confirming' : ''}">
    <div class="xleg-top">
      <b>${esc(r.status)}</b>
      <span class="tag ${r.load_state === 'damaged' ? 'tag--warn' : 'tag--off'}">load ${esc(r.load_state)}</span>
    </div>
    ${r.position ? `<div class="xreport-pos">${esc(r.position)}</div>` : ''}
    ${r.note ? `<div class="xreport-note">“${esc(r.note)}”</div>` : ''}
    <div class="xleg-meta">observed ${esc(r.observed_at.slice(0, 16).replace('T', ' '))} UTC
      · tier ${r.source_tier} observed${r.confirms_disruption
        ? ' · <b>confirms the disruption</b>' : ''}</div>
  </div>`;
}

/* One stream at a time. Re-opening on every render would leak a connection
 * per tab switch, and the browser caps them at six per origin — the seventh
 * silently never connects. */
let reportStream = null;

function watchReports(shipmentId) {
  if (reportStream) { reportStream.close(); reportStream = null; }
  if (!window.EventSource) return;
  reportStream = new EventSource('/api/v1/reports/stream');
  reportStream.addEventListener('report', (e) => {
    try {
      const row = JSON.parse(e.data);
      if (row.shipment_id !== shipmentId) return;
      const el = document.getElementById('x-reports');
      if (!el) return;
      if (el.querySelector('.socket')) el.innerHTML = '';
      el.insertAdjacentHTML('afterbegin', reportCard(row));
    } catch { /* a malformed frame is not worth breaking the page for */ }
  });
}

// ===============================================================
// Shell
// ===============================================================
function noRoute() {
  return `<div class="psec">
    <h2>No route selected</h2>
    <p class="psec-note">Open this from a route on the radar, or add
      <code>?route=LANE_RHINE_01</code> to the address.</p></div>`;
}

function switchView(name) {
  document.querySelectorAll('.ops-tab').forEach((b) =>
    b.classList.toggle('is-on', b.dataset.view === name));
  document.querySelectorAll('.ops-view').forEach((v) =>
    v.classList.toggle('is-on', v.id === 'view-' + name));
  location.hash = name;
  if (name === 'playbook') loadPlaybook();
  if (name === 'cargo') loadCargo();
  if (name === 'execute') loadExecute();
}

$('ops-tabs').addEventListener('click', (e) => {
  const button = e.target.closest('.ops-tab');
  if (button) switchView(button.dataset.view);
});

(async () => {
  if (!state.route) {
    $('brand-sub').textContent = 'no route selected';
  } else {
    $('brand-sub').textContent = `${state.route} · as of ${AS_OF.slice(0, 16).replace('T', ' ')} UTC`;
  }
  const wanted = location.hash.slice(1);
  switchView(['playbook', 'cargo', 'execute'].includes(wanted) ? wanted : 'playbook');
})();
