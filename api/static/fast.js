/* The Solution A dashboard.
 *
 * One decision on screen. The queue behind it is one line per lane, and it is
 * a link, not an expander — a second lane opening inside the first is how the
 * old board ended up with four panels fighting for the same space.
 */
'use strict';

const el = (id) => document.getElementById(id);

const state = {
  payload: null,
  executed: [],        // executions from the last click, for the undo bar
  undoTimer: null,
  refreshing: false,
};

/* ------------------------------------------------------------------ load */
async function load() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    state.payload = await FastUI.get('/api/v2/now');
    render();
  } catch (err) {
    el('sentence').textContent =
      'The radar is not answering. The page is fine; the server is not.';
    FastUI.toast(`Could not load: ${err.message}`, true);
  } finally {
    state.refreshing = false;
  }
}

/* ---------------------------------------------------------------- render */
function render() {
  const data = state.payload;
  el('asof').textContent = data.as_of_label || '';
  el('sentence').textContent = data.sentence;

  if (data.quiet || !data.headline) {
    el('headline').innerHTML = `
      <div class="quiet">
        <strong>Nothing needs a decision.</strong>
        Every lane is running to plan. This screen will fill itself the moment
        that stops being true.
      </div>`;
    el('queue').innerHTML = '';
  } else {
    el('headline').innerHTML = headlineHTML(data.headline);
    el('queue').innerHTML = queueHTML(data.queue, data.more);
    wireActions(data.headline);
  }

  el('folds').innerHTML = foldsHTML(data);
  el('counts').textContent =
    `${data.counts.lanes_affected} of ${data.counts.lanes_total} lanes off plan · ` +
    `${data.counts.shipments_at_risk} of ${data.counts.shipments_total} consignments`;
}

function headlineHTML(row) {
  const options = row.options || [];
  const hasOwn = options.some((o) => o.executable);

  return `
    <div class="headline__top">
      <div class="headline__row">
        <span class="chip chip--${FastUI.esc(row.urgency)}">
          <span class="dot"></span>${urgencyWord(row)}
        </span>
        <a class="back" href="/fast/${encodeURIComponent(row.route_id)}?${FastUI.QS}">
          open this lane →
        </a>
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
        <dt>Running late by</dt>
        <dd>${row.delay_days ? `${row.delay_days.toFixed(1)} d` : '—'}
          <span class="note">if nobody acts</span></dd>
      </div>
      <div class="fact">
        <dt>Consignments</dt>
        <dd>${row.shipments_at_risk}<span class="note">of ${row.shipments_total} on this lane</span></dd>
      </div>
    </dl>

    <div class="actions">
      <div class="actions__head">
        <h3>What to do</h3>
        <span class="hint">${hasOwn
          ? 'One click runs it. You get 15 minutes to pull it back.'
          : 'Nothing here is ours to execute — these are calls to make.'}</span>
      </div>
      <div class="act-list" id="acts">
        ${options.length
          ? options.map((o, i) => FastUI.optionHTML(o, i === 0)).join('')
          : '<p class="note-row">No option on this lane currently holds the date and pays for itself. That is a real answer, not a gap.</p>'}
      </div>
      <div id="undo"></div>
    </div>`;
}

function urgencyWord(row) {
  if (row.urgency === 'now') return 'act now';
  if (row.urgency === 'today') return 'act today';
  if (row.urgency === 'soon') return 'this week';
  return 'scheduled';
}

function queueHTML(queue, more) {
  if (!queue || !queue.length) {
    return '<p class="note-row">Nothing else is off plan.</p>';
  }
  const rows = queue.map((row) => `
    <a class="qrow qrow--${FastUI.esc(row.urgency)}"
       href="/fast/${encodeURIComponent(row.route_id)}?${FastUI.QS}">
      <span class="qrow__when">${FastUI.hoursShort(row.hours_left)}</span>
      <span>
        <span class="qrow__name">${FastUI.esc(row.name)}</span>
        <span class="qrow__sub">${FastUI.esc(row.cause || '')}</span>
      </span>
      <span class="qrow__count">${row.shipments_at_risk}/${row.shipments_total}</span>
      <span class="qrow__best">${row.best ? FastUI.esc(row.best.label) : 'tell the customer'}</span>
      <span class="qrow__arrow">→</span>
    </a>`).join('');

  const tail = more > 0
    ? `<p class="note-row">${more} more lane(s) are off plan and none of them
       need a decision today.</p>`
    : '';
  return `<h3>Then these</h3>${rows}${tail}`;
}

/* The parts of the old board that were true but not urgent. Shut by default;
 * one line of summary on the closed triangle so nothing is hidden, only
 * folded. */
function foldsHTML(data) {
  const headline = data.headline;
  const vetoed = (headline && headline.vetoed) || [];
  const incidents = data.incidents || [];

  const vetoBody = vetoed.length
    ? `<table class="mini">
         <tr><th>Option</th><th>Resolves in</th><th class="num">Cost</th><th>Why not</th></tr>
         ${vetoed.map((v) => `
           <tr>
             <td>${FastUI.esc(v.label)}</td>
             <td>${FastUI.hours(v.hours_to_resolve)}</td>
             <td class="num">${FastUI.chf(v.cost_chf)}</td>
             <td>${FastUI.esc(v.vetoed_because)}</td>
           </tr>`).join('')}
       </table>
       <p class="note-row">Shown so the decision can be defended, not to be
       acted on. Each of these would take the consignment into a loss.</p>`
    : '<p class="note-row">Nothing was discarded on cost.</p>';

  const feedBody = incidents.length
    ? `<ul class="feed">${incidents.map((i) => `
         <li>
           <span class="tag tag--${FastUI.esc(i.confidence)}">${FastUI.esc(i.confidence)}</span>
           <strong>${FastUI.esc(i.headline)}</strong>
           <div class="when">${FastUI.esc(i.at)} · ${FastUI.esc(i.label)}</div>
           <div>${FastUI.esc(i.detail || '')}</div>
         </li>`).join('')}</ul>`
    : `<p class="note-row">Nothing has come in from the road or from an
       integration this session. Reports land here the moment they are filed.</p>`;

  return `
    <details class="fold">
      <summary>Incidents as they arrive (${incidents.length})</summary>
      <div class="fold__body">${feedBody}</div>
    </details>
    <details class="fold">
      <summary>Discarded because they lose money (${vetoed.length})</summary>
      <div class="fold__body">${vetoBody}</div>
    </details>
    <details class="fold">
      <summary>What would have been sent, and to whom</summary>
      <div class="fold__body" id="outbox">
        <p class="note-row">Open to load.</p>
      </div>
    </details>`;
}

/* ------------------------------------------------------------------- act */
function wireActions(row) {
  const host = el('acts');
  if (!host) return;
  host.addEventListener('click', async (event) => {
    const button = event.target.closest('.act');
    if (!button || button.classList.contains('act--ask')) {
      if (button) showContacts(row, button.dataset.option);
      return;
    }
    await run(row.route_id, button.dataset.option, button);
  });
}

function showContacts(row, optionId) {
  const option = (row.options || []).find((o) => o.option_id === optionId);
  if (!option) return;
  const who = (option.contacts || []).join(' · ') || 'no contact on file';
  FastUI.toast(`${option.owner} owns this. Call: ${who}`);
}

async function run(routeId, optionId, button) {
  button.disabled = true;
  const original = button.querySelector('.act__go').textContent;
  button.querySelector('.act__go').textContent = 'running…';

  const { ok, payload } = await FastUI.post('/api/v2/act', {
    route_id: routeId,
    option_id: optionId,
    confidence: 'reported',
    trigger: 'dashboard',
  });

  button.disabled = false;
  button.querySelector('.act__go').textContent = original;

  if (!ok || !payload.executed || !payload.executed.length) {
    const why = (payload.refused && payload.refused[0]) || {};
    renderUndo(null, why.detail || payload.detail || 'It did not run.');
    FastUI.toast(why.detail || 'Refused. See the note on the card.', true);
    return;
  }

  state.executed = payload.executed;
  renderUndo(payload);
  FastUI.toast(payload.sentence);
  startUndoCountdown();
}

function renderUndo(payload, failure) {
  const host = el('undo');
  if (!host) return;

  if (failure) {
    host.innerHTML = `
      <div class="undo undo--failed">
        <strong>Not done.</strong>
        <span>${FastUI.esc(failure)}</span>
      </div>`;
    return;
  }
  if (!payload) { host.innerHTML = ''; return; }

  const first = payload.executed[0];
  host.innerHTML = `
    <div class="undo">
      <strong>Running.</strong>
      <span>${FastUI.esc(payload.sentence)} ${FastUI.esc(first.dispatch.sentence)}</span>
      <span class="spacer"></span>
      <span class="undo__count" id="undo-count"></span>
      <button type="button" id="undo-btn">Pull it back</button>
    </div>`;

  el('undo-btn').addEventListener('click', async () => {
    const { ok, payload: result } = await FastUI.post('/api/v2/undo', {
      execution_ids: state.executed.map((e) => e.execution_id),
    });
    if (ok && result.undone.length) {
      FastUI.toast(`Pulled back on ${result.undone.length} consignment(s).`);
      state.executed = [];
      stopUndoCountdown();
      el('undo').innerHTML = '';
      load();
    } else {
      const why = (result.refused && result.refused[0]) || {};
      FastUI.toast(why.detail || 'Could not pull it back.', true);
    }
  });
}

/* The countdown is real: it reads the window the server set, and when it runs
 * out the button goes, because at that point the action is a phone call to
 * the carrier rather than a click. */
function startUndoCountdown() {
  stopUndoCountdown();
  const until = state.executed.length
    ? new Date(state.executed[0].undo_until).getTime() : 0;

  const tick = () => {
    const left = Math.max(0, Math.round((until - Date.now()) / 1000));
    const label = el('undo-count');
    if (!label) return stopUndoCountdown();
    if (left <= 0) {
      stopUndoCountdown();
      const bar = el('undo');
      if (bar) {
        bar.innerHTML = `
          <div class="undo">
            <strong>Running.</strong>
            <span>The window to pull this back has closed — it is a call to
            the carrier now, not a click.</span>
          </div>`;
      }
      return;
    }
    const m = Math.floor(left / 60);
    const s = String(left % 60).padStart(2, '0');
    label.textContent = `${m}:${s} to change your mind`;
  };

  tick();
  state.undoTimer = setInterval(tick, 1000);
}

function stopUndoCountdown() {
  if (state.undoTimer) clearInterval(state.undoTimer);
  state.undoTimer = null;
}

/* ------------------------------------------------------------------ live */
function goLive() {
  const badge = el('live');
  const hit = () => {
    badge.classList.add('is-hit');
    setTimeout(() => badge.classList.remove('is-hit'), 650);
  };

  FastUI.listen(['disruption', 'action', 'field', 'undo'], {
    onOpen: () => { badge.classList.add('is-on'); badge.querySelector('span:last-child').textContent = 'live'; },
    onError: () => { badge.classList.remove('is-on'); badge.querySelector('span:last-child').textContent = 'reconnecting'; },
    onMessage: (topic) => {
      hit();
      /* A field report or an incident changes what is on screen. An action
       * echo is usually our own click coming back, and reloading on it would
       * fight the undo bar the planner is looking at. */
      if (topic === 'disruption' || topic === 'field') load();
    },
  });
}

/* ---------------------------------------------------------------- outbox */
document.addEventListener('toggle', async (event) => {
  const box = el('outbox');
  if (!box || !event.target.contains(box) || !event.target.open) return;
  try {
    const data = await FastUI.get('/api/v2/outbox');
    box.innerHTML = data.messages.length
      ? `<table class="mini">
           <tr><th>Channel</th><th>Message</th></tr>
           ${data.messages.slice(-12).reverse().map((m) => `
             <tr><td>${FastUI.esc(m.channel)}</td>
                 <td>${FastUI.esc(m.body.action || m.body.type)} —
                     ${FastUI.esc(m.body.shipment_id || '')}</td></tr>`).join('')}
         </table>
         <p class="note-row">These were recorded, not sent: the channel is not
         wired. Set its URL variable to make it a real send.</p>`
      : '<p class="note-row">Nothing recorded. Either nothing has run, or every channel is live.</p>';
  } catch {
    box.innerHTML = '<p class="note-row">Could not read the outbox.</p>';
  }
}, true);

document.addEventListener('DOMContentLoaded', () => {
  load();
  goLive();
});
