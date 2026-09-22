/* One lane, one page.
 *
 * What is NOT here is the point: no per-vehicle icon grid, no risk matrix, no
 * radar chart. A planner opening a lane wants three numbers and a list of
 * things they can press. Everything else the old route page showed is either
 * folded or gone.
 */
'use strict';

const el = (id) => document.getElementById(id);

const state = { row: null, executed: [], undoTimer: null };

function routeId() {
  const parts = location.pathname.split('/').filter(Boolean);
  return decodeURIComponent(parts[parts.length - 1] || '');
}

async function load() {
  try {
    state.row = await FastUI.get(`/api/v2/route/${encodeURIComponent(routeId())}`);
    render();
  } catch (err) {
    el('page').innerHTML = `
      <div class="quiet">
        <strong>This lane is not on the board.</strong>
        Either nothing is affecting it, or the board has moved on since the
        link was made.
        <p class="note-row"><a class="back" href="/fast?${FastUI.QS}">← back to what needs a decision</a></p>
      </div>`;
    if (!String(err.message).startsWith('404')) {
      FastUI.toast(`Could not load: ${err.message}`, true);
    }
  }
}

function render() {
  const row = state.row;
  const options = row.options || [];
  const hasOwn = options.some((o) => o.executable);

  el('page').innerHTML = `
    <a class="back" href="/fast?${FastUI.QS}">← everything that needs a decision</a>

    <div class="headline" style="margin-top:10px">
      <div class="headline__top">
        <div class="headline__row">
          <span class="chip chip--${FastUI.esc(row.urgency)}">
            <span class="dot"></span>${row.urgency === 'now' ? 'act now'
              : row.urgency === 'today' ? 'act today'
              : row.urgency === 'soon' ? 'this week' : 'scheduled'}
          </span>
          <span class="qrow__count">${row.shipments_at_risk} of
            ${row.shipments_total} consignments affected</span>
        </div>
        <h2>${FastUI.esc(row.name)}</h2>
        <p class="cause">${FastUI.esc(row.cause || 'Cause not stated.')}</p>
      </div>

      <dl class="facts">
        <div class="fact${row.urgency === 'now' ? ' fact--urgent' : ''}">
          <dt>Decide within</dt>
          <dd>${row.hours_left === null ? '—' : FastUI.hours(row.hours_left)}
            <span class="note">before the first option closes</span></dd>
        </div>
        <div class="fact">
          <dt>Delay right now</dt>
          <dd>${row.delay_days ? `${row.delay_days.toFixed(1)} d` : '—'}
            <span class="note">if nobody acts</span></dd>
        </div>
        <div class="fact">
          <dt>Options open</dt>
          <dd>${row.options_total}<span class="note">that hold the date and pay</span></dd>
        </div>
      </dl>

      <div class="actions">
        <div class="actions__head">
          <h3>Alternatives, fastest first</h3>
          <span class="hint">${hasOwn
            ? 'One click runs it.'
            : 'None of these are ours to execute.'}</span>
        </div>
        <div class="act-list" id="acts">
          ${options.length
            ? options.map((o, i) => FastUI.optionHTML(o, i === 0)).join('')
            : `<p class="note-row">Nothing on this lane both holds the date and
               pays for itself. The honest next step is to tell the customer and
               re-agree the date.</p>`}
        </div>
        <div id="undo"></div>
      </div>
    </div>

    <div id="sboard"></div>

    ${foldsHTML(row)}`;

  wireActions();
  // Loaded after the page paints. Allocating four hundred consignments
  // across four strategies is not something the lane header should wait
  // on, and the header is what the planner reads first.
  Board.load(el('sboard'), row.route_id);
}

function foldsHTML(row) {
  const customers = row.customers || [];
  const vetoed = row.vetoed || [];
  const executed = row.executed || [];
  const incidents = row.incidents || [];

  return `
    <details class="fold">
      <summary>Who is waiting for this (${customers.length})</summary>
      <div class="fold__body">
        ${customers.length
          ? `<p>${customers.map(FastUI.esc).join(' · ')}</p>`
          : '<p class="note-row">No customer recorded against the affected consignments.</p>'}
        ${row.causes && row.causes.length > 1
          ? `<p class="note-row">More than one thing is hitting this lane:
             ${row.causes.map(FastUI.esc).join('; ')}.</p>` : ''}
      </div>
    </details>

    <details class="fold">
      <summary>Discarded because they lose money (${vetoed.length})</summary>
      <div class="fold__body">
        ${vetoed.length
          ? `<table class="mini">
               <tr><th>Option</th><th>Resolves in</th><th class="num">Cost</th><th>Why not</th></tr>
               ${vetoed.map((v) => `
                 <tr><td>${FastUI.esc(v.label)}</td>
                     <td>${FastUI.hours(v.hours_to_resolve)}</td>
                     <td class="num">${FastUI.chf(v.cost_chf)}</td>
                     <td>${FastUI.esc(v.vetoed_because)}</td></tr>`).join('')}
             </table>`
          : '<p class="note-row">Nothing was discarded on cost.</p>'}
        ${row.expired
          ? `<p class="note-row">${row.expired} option(s) have already expired on
             this lane — they needed more notice than is left.</p>` : ''}
      </div>
    </details>

    <details class="fold">
      <summary>Already run on this lane (${executed.length})</summary>
      <div class="fold__body">
        ${executed.length
          ? `<table class="mini">
               <tr><th>Action</th><th>Consignment</th><th>When</th><th>State</th></tr>
               ${executed.slice(-15).reverse().map((e) => `
                 <tr><td>${FastUI.esc(e.label)}</td>
                     <td>${FastUI.esc(e.shipment_id)}</td>
                     <td>${FastUI.esc(e.executed_at)}</td>
                     <td>${e.undone ? 'pulled back' : 'running'}</td></tr>`).join('')}
             </table>`
          : '<p class="note-row">Nothing has been executed on this lane yet.</p>'}
      </div>
    </details>

    <details class="fold">
      <summary>Reported from the road (${incidents.length})</summary>
      <div class="fold__body">
        ${incidents.length
          ? `<ul class="feed">${incidents.map((i) => `
               <li><span class="tag tag--${FastUI.esc(i.confidence)}">${FastUI.esc(i.confidence)}</span>
                   <strong>${FastUI.esc(i.headline)}</strong>
                   <div class="when">${FastUI.esc(i.at)}</div>
                   <div>${FastUI.esc(i.detail || '')}</div></li>`).join('')}</ul>`
          : `<p class="note-row">Nothing reported from the road. Whoever is with
             a consignment can file in four taps at <code>/driver</code>.</p>`}
      </div>
    </details>`;
}

function wireActions() {
  const host = el('acts');
  if (!host) return;
  host.addEventListener('click', async (event) => {
    const button = event.target.closest('.act');
    if (!button) return;
    if (button.classList.contains('act--ask')) {
      const option = (state.row.options || [])
        .find((o) => o.option_id === button.dataset.option);
      const who = (option && option.contacts || []).join(' · ') || 'no contact on file';
      FastUI.toast(`${option ? option.owner : 'somebody else'} owns this. Call: ${who}`);
      return;
    }
    await run(button);
  });
}

async function run(button) {
  button.disabled = true;
  const go = button.querySelector('.act__go');
  const original = go.textContent;
  go.textContent = 'running…';

  const { ok, payload } = await FastUI.post('/api/v2/act', {
    route_id: state.row.route_id,
    option_id: button.dataset.option,
    confidence: 'reported',
    trigger: 'route-page',
  });

  button.disabled = false;
  go.textContent = original;

  if (!ok || !payload.executed || !payload.executed.length) {
    const why = (payload.refused && payload.refused[0]) || {};
    FastUI.toast(why.detail || 'Refused.', true);
    return;
  }

  state.executed = payload.executed;
  el('undo').innerHTML = `
    <div class="undo">
      <strong>Running.</strong>
      <span>${FastUI.esc(payload.sentence)}</span>
      <span class="spacer"></span>
      <span class="undo__count" id="undo-count"></span>
      <button type="button" id="undo-btn">Pull it back</button>
    </div>`;
  el('undo-btn').addEventListener('click', undo);
  countdown();
  FastUI.toast(payload.sentence);
}

async function undo() {
  const { ok, payload } = await FastUI.post('/api/v2/undo', {
    execution_ids: state.executed.map((e) => e.execution_id),
  });
  if (ok && payload.undone.length) {
    FastUI.toast(`Pulled back on ${payload.undone.length} consignment(s).`);
    stop();
    el('undo').innerHTML = '';
    load();
  } else {
    const why = (payload.refused && payload.refused[0]) || {};
    FastUI.toast(why.detail || 'Could not pull it back.', true);
  }
}

function countdown() {
  stop();
  const until = state.executed.length
    ? new Date(state.executed[0].undo_until).getTime() : 0;
  const tick = () => {
    const label = el('undo-count');
    if (!label) return stop();
    const left = Math.max(0, Math.round((until - Date.now()) / 1000));
    if (left <= 0) {
      stop();
      const bar = el('undo');
      if (bar) bar.innerHTML = `
        <div class="undo"><strong>Running.</strong>
        <span>The window to pull this back has closed.</span></div>`;
      return;
    }
    label.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} to change your mind`;
  };
  tick();
  state.undoTimer = setInterval(tick, 1000);
}

function stop() {
  if (state.undoTimer) clearInterval(state.undoTimer);
  state.undoTimer = null;
}

document.addEventListener('DOMContentLoaded', () => {
  load();
  const badge = el('live');
  FastUI.listen(['disruption', 'field'], {
    onOpen: () => badge && badge.classList.add('is-on'),
    onError: () => badge && badge.classList.remove('is-on'),
    onMessage: () => load(),
  });
});
