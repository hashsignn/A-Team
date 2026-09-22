/* The operations console.
 *
 * Every step renders its own controls, and every control does the thing it
 * is named after. Pressing "Open the event" opens the event; pressing "Find
 * alternate routes" runs the contingency search and puts the results, each
 * with an Execute button, inside the step that asked for them.
 *
 * The page never asks the planner to assert anything. The only click that
 * changes a step's state without producing a result is "Reviewed", which
 * acknowledges something the system worked out on its own — and that exists
 * precisely so the automatic answer is seen rather than assumed.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const params = new URLSearchParams(location.search);
const ROUTE = params.get('route') || '';
const AS_OF = params.get('as_of') || '2026-09-18T06:00:00+00:00';
const SHIPMENTS = params.get('shipments') || '150';
const QS = `as_of=${encodeURIComponent(AS_OF)}&shipments=${encodeURIComponent(SHIPMENTS)}`;

const state = { console: null, outputs: {} };

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const chf = (n) => (n === null || n === undefined) ? '—'
  : n === 0 ? 'no extra cost'
  : `CHF ${Math.round(n).toLocaleString('de-CH').replace(/,/g, '’')}`;

const hrs = (h) => (h === null || h === undefined) ? '—'
  : h < 1 ? 'under an hour' : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} days`;

const when = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso
    : d.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
};

const STATE_WORD = { open: 'to do', evidence: 'review this', done: 'settled' };

// =====================================================================
async function load() {
  try {
    const res = await fetch(
      `/api/v2/console/${encodeURIComponent(ROUTE)}?${QS}`,
      { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    state.console = await res.json();
    render();
  } catch (err) {
    $('console').innerHTML = `
      <p class="cons-loading">Could not open this lane — ${esc(err.message)}.
      <a href="/">Back to the radar</a>.</p>`;
  }
}

function render() {
  const c = state.console;
  $('brand-sub').textContent = `${ROUTE} · as of ${AS_OF.slice(0, 16).replace('T', ' ')} UTC`;
  $('link-lane').href = `/route/${encodeURIComponent(ROUTE)}?${QS}`;
  renderRail(c);

  const stages = c.stages.map((s) => stageHTML(c, s)).join('');
  $('console').innerHTML = `
    <div class="cons-head">
      <span class="level-chip" style="color:var(--lvl-${esc(c.level)})">${esc(c.level_label)}</span>
      <h2>${esc(c.route_name)}</h2>
      <p class="cause">${esc(c.reason)}</p>
    </div>

    <dl class="cons-facts">
      <div class="cons-fact"><dt>Directive</dt><dd style="font-size:15px">${esc(c.directive)}</dd></div>
      <div class="cons-fact"><dt>Action by</dt><dd>${hrs(c.lead_time_hours)}</dd></div>
      <div class="cons-fact"><dt>Exposure</dt><dd>${chf(c.exposure_chf)}</dd></div>
      <div class="cons-fact"><dt>At risk</dt><dd>${c.shipments_at_risk} / ${c.shipments}</dd></div>
      <div class="cons-fact"><dt>Settled</dt><dd>${c.counts.done} / ${c.counts.total}</dd></div>
    </dl>

    ${c.caution ? `
      <p class="cons-caution"><b>Thin evidence.</b>
        <span>${esc(c.caution.text)}</span></p>` : ''}

    ${stages}`;

  wire();
  // Put any output the planner had already opened back on screen, so a
  // refresh after running a tool does not throw its result away.
  Object.entries(state.outputs).forEach(([stepId, html]) => {
    const box = document.querySelector(`.out[data-step="${stepId}"]`);
    if (box) { box.innerHTML = html; box.hidden = false; }
  });
}

/* The rail. Four segments, each filled by its own progress — so a glance
 * answers "how far in am I", which counting ticks down a page does not. */
function renderRail(c) {
  $('rail').innerHTML = c.stages.map((s, i) => {
    const cls = [
      'rail-seg',
      s.settled ? 'is-settled' : '',
      s.stage === c.current_stage && !s.settled ? 'is-current' : '',
      s.evidence ? 'has-evidence' : '',
    ].filter(Boolean).join(' ');
    return `
      <div class="${cls}" style="--fill:${Math.round(s.progress * 100)}%">
        <div class="rail-seg-top">
          <span class="rail-seg-num">${s.settled ? '✓' : i + 1}</span>
          ${esc(s.title)}
          ${s.evidence ? `<span class="rail-amber">${s.evidence} to review</span>` : ''}
        </div>
        <div class="rail-seg-sub">${s.done} of ${s.steps} settled</div>
      </div>`;
  }).join('');
}

function stageHTML(c, stage) {
  const steps = c.steps.filter((s) => s.stage === stage.stage);
  if (!steps.length) return '';
  return `
    <section class="stage-block">
      <h3 class="stage-title">${esc(stage.title)}
        <span class="count">${stage.done}/${stage.steps} settled${
          stage.evidence ? ` · ${stage.evidence} awaiting review` : ''}</span>
      </h3>
      ${steps.map(stepHTML).join('')}
    </section>`;
}

function stepHTML(step) {
  const mark = step.state === 'done' ? '✓' : step.state === 'evidence' ? '!' : '';
  const sla = step.sla_hours ? ` · within ${Math.round(step.sla_hours)} h` : '';

  const tools = step.tools.map((t) => toolHTML(step, t)).join('');
  const review = step.state === 'evidence'
    ? `<button type="button" class="tool tool--review"
               data-step="${esc(step.step_id)}" data-tool="review">
         Reviewed — settle it
       </button>` : '';

  return `
    <article class="step is-${esc(step.state)}" data-step="${esc(step.step_id)}">
      <div class="step-head">
        <span class="step-mark">${mark}</span>
        <div>
          <div class="step-label">${esc(step.label)}</div>
          <div class="step-note">${esc(step.note)}</div>
          <div class="step-owner">${esc(step.owner)}${esc(sla)}</div>
        </div>
        <span class="step-state">${STATE_WORD[step.state] || step.state}</span>
      </div>

      ${step.evidence.length ? `
        <div class="ev">
          ${step.evidence.map((e) => `
            <div class="ev-row">
              <span class="ev-tag ev-tag--${esc((e.weight || '').replace(/\s+/g, '-'))}">${esc(e.weight || e.kind)}</span>
              <span>${esc(e.text)}${e.detail ? ` — ${esc(e.detail)}` : ''}
                ${e.at ? `<span class="ev-tag" style="border:0;padding-left:6px">${esc(when(e.at))}</span>` : ''}</span>
            </div>`).join('')}
        </div>` : ''}

      <div class="tools">${tools}${review}</div>
      <div class="out" data-step="${esc(step.step_id)}" hidden></div>
    </article>`;
}

function toolHTML(step, tool) {
  if (tool.kind === 'link') {
    return `<a class="tool${tool.primary ? ' tool--primary' : ''}"
               href="${esc(tool.href)}${tool.href.includes('?') ? '&' : '?'}${QS}"
               title="${esc(tool.hint)}">${esc(tool.label)}</a>`;
  }
  return `<button type="button" class="tool${tool.primary ? ' tool--primary' : ''}"
            data-step="${esc(step.step_id)}" data-tool="${esc(tool.tool_id)}"
            data-kind="${esc(tool.kind)}" data-key="${esc(tool.data_key || '')}"
            title="${esc(tool.hint)}">${esc(tool.label)}</button>`;
}

// =====================================================================
function wire() {
  document.querySelectorAll('.tool[data-tool]').forEach((btn) => {
    btn.addEventListener('click', () => press(btn));
  });
}

function stepOf(id) {
  return state.console.steps.find((s) => s.step_id === id);
}

function show(stepId, html) {
  state.outputs[stepId] = html;
  const box = document.querySelector(`.out[data-step="${stepId}"]`);
  if (box) { box.innerHTML = html; box.hidden = false; }
}

async function press(btn) {
  const stepId = btn.dataset.step;
  const toolId = btn.dataset.tool;
  const kind = btn.dataset.kind;
  const step = stepOf(stepId);

  // REVEAL needs no round trip: the data came with the console.
  if (kind === 'reveal') {
    show(stepId, revealHTML(toolId, (step.data || {})[btn.dataset.key] || []));
    return;
  }
  if (kind === 'log') {
    show(stepId, formHTML(stepId, toolId, step));
    wireForm(stepId, toolId);
    return;
  }

  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'working…';
  try {
    const res = await fetch(
      `/api/v2/console/${encodeURIComponent(ROUTE)}/tool?${QS}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_id: toolId, step_id: stepId }),
      });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || res.statusText);
    handle(stepId, payload);
  } catch (err) {
    show(stepId, `<p class="note-line bad">That did not run — ${esc(err.message)}.</p>`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function handle(stepId, payload) {
  if (payload.kind === 'options') {
    show(stepId, optionsHTML(payload));
    wireExecute(stepId);
  } else if (payload.kind === 'vendors') {
    show(stepId, vendorsHTML(payload.vendors));
  } else if (payload.kind === 'dispatch') {
    show(stepId, `
      <p class="out-title">Sent to the road</p>
      <p class="note-line">${esc(payload.dispatch.sentence)}</p>`);
  } else if (payload.kind === 'draft') {
    show(stepId, draftHTML(payload.draft));
    wireCopy(stepId);
  } else if (payload.kind === 'record') {
    show(stepId, recordHTML(payload.record));
  } else if (payload.kind === 'review' || payload.kind === 'log') {
    // The console came back with it; re-render so the rail moves.
    state.console = payload.console;
    render();
    return;
  }
  if (payload.console) { state.console = payload.console; renderRail(payload.console); }
}

// ---------------------------------------------------------------- reveals
function revealHTML(toolId, rows) {
  if (!rows.length) {
    return `<p class="note-line">Nothing to show here yet.</p>`;
  }
  if (toolId === 'show.event') return eventsHTML(rows);
  if (toolId === 'show.consignments') return consignmentsHTML(rows);
  if (toolId === 'show.positions') return positionsHTML(rows);
  if (toolId === 'show.eta') return etaHTML(rows);
  if (toolId === 'show.sources') return sourcesHTML(rows);
  if (toolId === 'show.capacity') return capacityHTML(rows);
  if (toolId === 'show.contacts') return contactsHTML(rows);
  if (toolId === 'show.options') return optionsHTML({ options: rows, sentence: '' });
  return `<pre class="note-line">${esc(JSON.stringify(rows, null, 1))}</pre>`;
}

function eventsHTML(events) {
  return `
    <p class="out-title">What was detected, and who says so</p>
    <table class="dtable">
      <tr><th>Event</th><th>Window</th><th>Source</th><th class="num">On this lane</th></tr>
      ${events.map((e) => `
        <tr>
          <td class="wrap"><b>${esc(e.title)}</b>
            <div class="note-line">severity ${esc(e.severity)}${
              e.probability === null
                ? ' · probability not sourceable'
                : ` · p ${Number(e.probability).toFixed(2)}`}</div>
            ${e.quote ? `<div class="note-line">“${esc(e.quote)}”</div>` : ''}</td>
          <td>${esc(when(e.starts_at))}<div class="note-line">${
            e.ends_at ? `to ${esc(when(e.ends_at))}` : 'open-ended'}</div></td>
          <td>${esc(e.source || 'unstated')}
            <div class="note-line">tier ${e.source_tier ?? '?'}${
              e.inferred ? ' · inferred' : ''}</div></td>
          <td class="num">${e.shipments_here}<div class="note-line">${chf(e.exposure_chf)}</div></td>
        </tr>`).join('')}
    </table>`;
}

function consignmentsHTML(rows) {
  const risk = rows.filter((r) => r.at_risk);
  return `
    <p class="out-title">${risk.length} of ${rows.length} consignments in scope</p>
    <table class="dtable">
      <tr><th>Consignment</th><th>Customer</th><th class="num">Value</th>
          <th>Committed</th><th>Decide within</th><th>Fastest option</th></tr>
      ${rows.map((r) => `
        <tr class="${r.at_risk ? 'is-risk' : ''}">
          <td>${esc(r.shipment_id)}</td>
          <td class="wrap">${esc(r.customer)}</td>
          <td class="num">${chf(r.value_chf)}</td>
          <td>${esc(when(r.committed))}</td>
          <td>${r.at_risk ? hrs(r.lead_time_hours) : '—'}</td>
          <td class="wrap">${esc(r.best_action || (r.at_risk ? 'nothing worth doing' : 'on plan'))}</td>
        </tr>`).join('')}
    </table>`;
}

function positionsHTML(rows) {
  return `
    <p class="out-title">Last seen, per consignment</p>
    <table class="dtable">
      <tr><th>Consignment</th><th>When</th><th>Status</th><th>Where</th><th>Who</th></tr>
      ${rows.map((r) => `
        <tr>
          <td>${esc(r.shipment_id)}</td>
          <td>${esc(when(r.observed_at))}</td>
          <td>${esc(r.status)}${r.load_state ? ` · ${esc(r.load_state)}` : ''}</td>
          <td class="wrap">${esc(r.position || '—')}${
            r.lat !== null && r.lat !== undefined
              ? `<div class="note-line">${r.lat.toFixed(3)}, ${r.lon.toFixed(3)}</div>` : ''}</td>
          <td>${esc(r.reported_by || '—')}
            <div class="note-line">${r.first_hand ? 'first hand' : 'relayed'}${
              r.authenticated ? ' · verified' : ' · unverified'}</div></td>
        </tr>`).join('')}
    </table>`;
}

function etaHTML(rows) {
  return `
    <p class="out-title">Revised arrivals already filed</p>
    <table class="dtable">
      <tr><th>Consignment</th><th>New arrival</th><th>Filed</th></tr>
      ${rows.map((r) => `
        <tr><td>${esc(r.shipment_id)}</td><td>${esc(when(r.revised_eta))}</td>
            <td>${esc(when(r.observed_at))}</td></tr>`).join('')}
    </table>`;
}

function sourcesHTML(rows) {
  return `
    <p class="out-title">What already agrees</p>
    <table class="dtable">
      <tr><th>Tier</th><th>Source</th><th>Weight</th></tr>
      ${rows.map((r) => `
        <tr><td class="num">${r.tier}</td><td class="wrap">${esc(r.text)}</td>
            <td class="${r.weight === 'official' ? 'good' : ''}">${esc(r.weight)}</td></tr>`).join('')}
    </table>
    <p class="note-line">Two independent tier-1 sources is what the engine treats
      as corroboration. Below that the step stays amber and says so.</p>`;
}

function capacityHTML(rows) {
  return `
    <p class="out-title">Carriers on these modes</p>
    <table class="dtable">
      <tr><th>Carrier</th><th>Modes</th><th>Contact</th></tr>
      ${rows.map((r) => `
        <tr><td>${esc(r.name || r.id || '—')}</td>
            <td>${esc((r.modes || []).join(', '))}</td>
            <td>${esc(r.phone || r.contact || '—')}</td></tr>`).join('')}
    </table>`;
}

function contactsHTML(rows) {
  return `
    <p class="out-title">Who to tell</p>
    <table class="dtable">
      <tr><th>Function</th><th>Role</th><th>Acknowledge within</th><th>Contact</th></tr>
      ${rows.map((r) => `
        <tr><td>${esc(r.function || r.name || '—')}</td>
            <td class="wrap">${esc(r.role || '')}</td>
            <td>${r.response_sla_hours ? `${r.response_sla_hours} h` : '—'}</td>
            <td>${esc(r.contact || r.phone || '—')}</td></tr>`).join('')}
    </table>`;
}

// ---------------------------------------------------------------- results
function optionsHTML(payload) {
  const options = payload.options || [];
  if (!options.length) {
    return `<p class="note-line">${esc(payload.sentence
      || 'Nothing on this lane both holds the date and pays for itself.')}</p>`;
  }
  return `
    <p class="out-title">${esc(payload.sentence || 'Options, fastest first')}</p>
    ${options.map((o) => `
      <div class="opt">
        <div>
          <div class="opt-label">${esc(o.label)}</div>
          <div class="opt-meta">resolved in ${hrs(o.hours_to_resolve)} ·
            ${o.on_time ? 'arrives on the agreed date' : `${Number(o.days_late_after).toFixed(1)} days late`} ·
            ${chf(o.cost_chf)}${o.shipments ? ` · covers ${o.shipments} consignment(s)` : ''}</div>
          <div class="opt-why">${esc(o.detail || '')}</div>
        </div>
        ${o.executable
          ? `<button type="button" class="tool tool--primary exec"
                     data-option="${esc(o.option_id)}">Execute it</button>`
          : `<span class="note-line">${esc(o.owner)} owns this lever — call, do not click</span>`}
      </div>`).join('')}
    ${(payload.vetoed || []).length ? `
      <p class="note-line">${payload.vetoed.length} option(s) were discarded for
        losing money: ${payload.vetoed.map((v) => esc(v.label)).join('; ')}.</p>` : ''}`;
}

function wireExecute(stepId) {
  document.querySelectorAll(`.out[data-step="${stepId}"] .exec`).forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = 'running…';
      try {
        const res = await fetch(`/api/v2/act?${QS}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            route_id: ROUTE, option_id: btn.dataset.option,
            trigger: 'console', confidence: 'reported',
          }),
        });
        const payload = await res.json();
        if (!res.ok || !(payload.executed || []).length) {
          const why = (payload.refused && payload.refused[0]) || {};
          btn.textContent = 'refused';
          show(stepId, `<p class="note-line bad">${esc(why.detail || 'It did not run.')}</p>`);
          return;
        }
        // The step is now settled by something that happened, not by a tick.
        await load();
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function vendorsHTML(vendors) {
  if (!vendors.length) {
    return `<p class="note-line">No vendor on file is close enough to take
      this over inside the window.</p>`;
  }
  return `
    <p class="out-title">${vendors.length} local operator(s) within reach</p>
    <table class="dtable">
      <tr><th>Operator</th><th>Service</th><th>At</th><th class="num">Distance</th>
          <th class="num">Ready in</th><th class="num">Cost</th><th>Call</th></tr>
      ${vendors.map((v) => `
        <tr><td class="wrap">${esc(v.vendor)}</td>
            <td>${esc(String(v.service).replace(/_/g, ' '))}</td>
            <td>${esc(v.at)}</td>
            <td class="num">${Math.round(v.distance_km)} km</td>
            <td class="num">${hrs(v.ready_in_hours)}</td>
            <td class="num">${chf(v.cost_chf)}</td>
            <td>${v.phone ? `<a href="tel:${esc(v.phone)}">${esc(v.phone)}</a>` : '—'}</td>
        </tr>`).join('')}
    </table>`;
}

function draftHTML(draft) {
  return `
    <p class="out-title">Drafted from this board's own numbers</p>
    <div class="fieldset">
      <label><span>To</span><input type="text" id="draft-to" value="${esc((draft.to || []).join(', '))}"></label>
      <label><span>Subject</span><input type="text" id="draft-subject" value="${esc(draft.subject)}"></label>
      <label><span>Message</span><textarea id="draft-body">${esc(draft.body)}</textarea></label>
      <div class="tools" style="padding:0">
        <button type="button" class="tool tool--primary" id="draft-copy">Copy</button>
        <a class="tool" id="draft-mail" href="#">Open in mail client</a>
      </div>
    </div>
    <p class="note-line">The app composes; it does not send. Nothing is wired,
      so this hands the draft to your own client.</p>`;
}

function wireCopy(stepId) {
  const box = document.querySelector(`.out[data-step="${stepId}"]`);
  if (!box) return;
  const copy = box.querySelector('#draft-copy');
  const mail = box.querySelector('#draft-mail');
  const body = () => box.querySelector('#draft-body').value;
  const subject = () => box.querySelector('#draft-subject').value;
  const to = () => box.querySelector('#draft-to').value;

  if (mail) {
    const sync = () => {
      mail.href = `mailto:${encodeURIComponent(to())}` +
        `?subject=${encodeURIComponent(subject())}&body=${encodeURIComponent(body())}`;
    };
    sync();
    box.addEventListener('input', sync);
  }
  if (copy) {
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(body()); copy.textContent = 'Copied'; }
      catch { box.querySelector('#draft-body').select(); copy.textContent = 'Press Ctrl+C'; }
      setTimeout(() => { copy.textContent = 'Copy'; }, 2000);
    });
  }
}

function recordHTML(record) {
  const rows = record.actions_taken;
  return `
    <p class="out-title">Written by the system, from what actually ran</p>
    ${rows.length ? `
      <table class="dtable">
        <tr><th>Action</th><th>Consignment</th><th>When</th><th>Acting on</th><th class="num">Cost</th></tr>
        ${rows.map((r) => `
          <tr><td class="wrap">${esc(r.action)}</td><td>${esc(r.shipment_id)}</td>
              <td>${esc(when(r.at))}</td><td>${esc(r.confidence)}</td>
              <td class="num">${chf(r.cost_chf)}</td></tr>`).join('')}
        <tr><td colspan="4"><b>Total spend</b></td>
            <td class="num"><b>${chf(record.total_spend_chf)}</b></td></tr>
      </table>`
      : `<p class="note-line">Nothing has been executed on this lane, so the
         record says so rather than inventing a narrative.</p>`}
    ${record.actions_pulled_back.length ? `
      <p class="note-line">${record.actions_pulled_back.length} action(s) were
        pulled back inside the undo window.</p>` : ''}
    <p class="note-line">${record.evidence_from_the_road.length} field report(s)
      and ${record.decisions_logged_by_hand.length} hand-logged decision(s) are
      attached. ${esc(record.note)}</p>`;
}

// -------------------------------------------------------------------- logs
function formHTML(stepId, toolId, step) {
  const tool = step.tools.find((t) => t.tool_id === toolId);
  const fields = (tool && tool.fields) || [];
  return `
    <p class="out-title">${esc(tool ? tool.label : 'Record it')}</p>
    <div class="fieldset" data-form="${esc(toolId)}">
      ${fields.map((f) => `
        <label><span>${esc(f.label)}</span>
          ${f.type === 'select'
            ? `<select name="${esc(f.name)}">${(f.options || [])
                .map((o) => `<option>${esc(o)}</option>`).join('')}</select>`
            : `<input type="${esc(f.type || 'text')}" name="${esc(f.name)}">`}
        </label>`).join('')}
      <div class="tools" style="padding:0">
        <button type="button" class="tool tool--primary save">Save it</button>
      </div>
    </div>
    <p class="note-line">Saved against this step and folded into the close-out
      record, so it is written once rather than typed again later.</p>`;
}

function wireForm(stepId, toolId) {
  const box = document.querySelector(`.out[data-step="${stepId}"]`);
  const save = box && box.querySelector('.save');
  if (!save) return;
  save.addEventListener('click', async () => {
    const fields = {};
    box.querySelectorAll('[name]').forEach((el) => { fields[el.name] = el.value; });
    save.disabled = true; save.textContent = 'saving…';
    const res = await fetch(
      `/api/v2/console/${encodeURIComponent(ROUTE)}/tool?${QS}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_id: toolId, step_id: stepId, fields }),
      });
    const payload = await res.json();
    delete state.outputs[stepId];
    if (payload.console) { state.console = payload.console; render(); }
  });
}

/* A field report or an executed action changes what this page says, so it
 * listens rather than waiting to be reloaded. */
function listen() {
  try {
    const src = new EventSource('/api/v2/stream?topics=field,disruption,action,undo');
    ['field', 'disruption', 'action', 'undo'].forEach((topic) => {
      src.addEventListener(topic, () => load());
    });
  } catch { /* no EventSource: the page still works, just not live */ }
}

if (!ROUTE) {
  $('console').innerHTML =
    `<p class="cons-loading">No route in the address. Open one from
     <a href="/">the radar</a>.</p>`;
} else {
  load();
  listen();
}
