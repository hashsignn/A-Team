/* The driver app.
 *
 * OFFLINE FIRST, BECAUSE THE JOB IS OFFLINE
 * ==========================================
 * The people using this are in tunnels, at borders, on rivers and in steel
 * warehouses. Signal is the exception, not the rule. So a report is written
 * to the phone FIRST and sent afterwards:
 *
 *     tap send -> queued on the device -> "sent" when it actually lands
 *
 * A driver never waits for a spinner and never loses a report to a dead
 * zone. The queue drains on its own when the connection returns, and on the
 * next visit if the app was closed.
 *
 * `observed_at` is stamped when the driver taps send, NOT when the report
 * reaches the server. A report queued in the Gotthard and delivered forty
 * minutes later must not claim to be a forty-minute-old observation — the
 * planner's whole decision turns on when somebody actually looked.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const params = new URLSearchParams(location.search);
const QUEUE_KEY = 'scrr.driver.queue';
const LAST_KEY = 'scrr.driver.shipment';

/* THE DEMO CLOCK.
 *
 * In production a report is observed NOW, and the board — whose as-of is
 * also now — sees it immediately. In a demo the board is pinned to a past
 * instant so it is reproducible, and a report stamped with the real clock is
 * correctly excluded from it. Correct, and useless: the driver taps send and
 * nothing appears.
 *
 * So when this app is opened FROM a pinned board, it stamps observations
 * inside that board's frame and says so on screen. The fiction is labelled
 * rather than hidden, because a demo affordance a viewer cannot see is one
 * they will mistake for the real behaviour.
 */
const PINNED = params.get('as_of');

function observedNow() {
  if (!PINNED) return new Date().toISOString();
  // Just inside the board's instant, so it is included, and ordered after
  // anything filed earlier in the same session.
  const base = new Date(PINNED).getTime();
  const nth = readQueue().length;
  return new Date(base - 60000 + nth * 1000).toISOString();
}

const state = { status: null, load: 'intact', sending: false };

// ---------------------------------------------------------------
// The queue. Browser storage can throw or come back empty, so every
// read and write is guarded and the app renders correctly without it.
// ---------------------------------------------------------------
function readQueue() {
  try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]'); }
  catch { return []; }
}

function writeQueue(rows) {
  try { localStorage.setItem(QUEUE_KEY, JSON.stringify(rows)); }
  catch { /* private window: the app still sends, it just cannot retry */ }
}

function remember(shipment) {
  try { localStorage.setItem(LAST_KEY, shipment); } catch { /* ignore */ }
}

function recall() {
  try { return localStorage.getItem(LAST_KEY) || ''; } catch { return ''; }
}

// ---------------------------------------------------------------
// Sending
// ---------------------------------------------------------------
async function drain() {
  if (state.sending) return;
  const queue = readQueue();
  if (!queue.length) { renderQueue(); return; }

  state.sending = true;
  const remaining = [];
  for (const row of queue) {
    try {
      const res = await fetch('/api/v1/reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(row.payload),
      });
      if (res.status === 201) {
        logSent(await res.json());
      } else if (res.status === 422) {
        // The server refused it as malformed. Retrying will never help, so
        // it comes OUT of the queue and is shown — a report silently retried
        // forever is a report the driver thinks was delivered.
        const body = await res.json().catch(() => ({}));
        logRejected(row, body.detail || 'refused');
      } else {
        remaining.push(row);
      }
    } catch {
      remaining.push(row);   // no signal — keep it
    }
  }
  writeQueue(remaining);
  state.sending = false;
  renderQueue();
}

function enqueue(payload) {
  const queue = readQueue();
  queue.push({ id: Math.random().toString(36).slice(2, 10), payload });
  writeQueue(queue);
  renderQueue();
  drain();
}

// ---------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------
function renderQueue() {
  const queue = readQueue();
  const el = $('dv-queue');
  el.hidden = queue.length === 0;
  if (queue.length) {
    el.textContent = navigator.onLine
      ? `${queue.length} report(s) sending…`
      : `${queue.length} report(s) waiting for a connection. They will send themselves.`;
  }
}

function logSent(report) {
  const el = document.createElement('div');
  el.className = 'dv-sent';
  el.innerHTML = `<b>Sent</b> ${esc(report.shipment_id)} — ${esc(report.status)}
    <em>${esc(report.observed_at.slice(11, 16))} UTC</em>`;
  $('dv-log').prepend(el);
}

function logRejected(row, why) {
  const el = document.createElement('div');
  el.className = 'dv-sent is-bad';
  el.innerHTML = `<b>Not accepted</b> ${esc(row.payload.shipment_id || '')} —
    ${esc(why)}`;
  $('dv-log').prepend(el);
}

function renderNet() {
  const el = $('dv-net');
  el.className = 'dv-net' + (navigator.onLine ? ' is-on' : '');
  el.textContent = navigator.onLine ? 'online' : 'offline';
}

/* What the planner is currently waiting for on this consignment.
 *
 * Shown first, because it is the reason the driver opened the app. It is
 * also the honest version of "real time": the driver is not guessing what is
 * useful, they are answering a question somebody actually asked. */
async function loadAsk(shipment) {
  const wrap = $('dv-asked');
  if (!shipment) { wrap.hidden = true; return; }
  try {
    const res = await fetch(`/api/execute/${encodeURIComponent(shipment)}`);
    if (!res.ok) { wrap.hidden = true; return; }
    const v = await res.json();
    const w = v.what_is_happening;
    if (!w) {
      wrap.hidden = false;
      wrap.innerHTML = `<p class="dv-asked-quiet">Nothing reported on this
        consignment. A report is still useful.</p>`;
      $('dv-confirm-wrap').hidden = true;
      return;
    }
    const needs = (v.office_needs || []).filter((n) => !n.answered && !n.blocked_reason);
    wrap.hidden = false;
    wrap.innerHTML = `
      <div class="dv-asked-label">The office is asking about</div>
      <div class="dv-asked-title">${esc(w.title)}</div>
      <div class="dv-asked-meta">${esc(v.lane)}${
        v.your_deadline ? ` · they need an answer within ${Math.round(v.your_deadline.hours)} h` : ''}</div>
      ${needs.length ? `<ul class="dv-needs">${needs.map((n) =>
        `<li data-need="${esc(n.task_id)}">${esc(n.ask)}</li>`).join('')}</ul>` : `
        <div class="dv-needs-done">Everything they asked for has been
          answered. Another report is still useful if something changes.</div>`}`;

    // Nudge the driver to the field that answers what is outstanding, rather
    // than leaving them to work out which of five boxes matters.
    if (needs.some((n) => n.task_id === 'confirm.eta')) {
      document.querySelector('label[for-eta], .dv-field')
        && $('f-eta').closest('.dv-field').classList.add('is-wanted');
    }
    $('dv-confirm-wrap').hidden = false;
    $('dv-confirm-label').textContent = 'Yes — I can see this happening';
    $('dv-confirm-note').textContent =
      'Only tick this if you can actually see it. It is what lets the office '
      + 'change the route.';
  } catch {
    wrap.hidden = true;
  }
}

// ---------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------
$('f-status').addEventListener('click', (e) => {
  const b = e.target.closest('[data-status]');
  if (!b) return;
  state.status = b.dataset.status;
  $('f-status').querySelectorAll('button').forEach((x) =>
    x.classList.toggle('is-on', x === b));
});

$('f-load').addEventListener('click', (e) => {
  const b = e.target.closest('[data-load]');
  if (!b) return;
  state.load = b.dataset.load;
  $('f-load').querySelectorAll('button').forEach((x) =>
    x.classList.toggle('is-on', x === b));
});

$('f-gps').addEventListener('click', () => {
  if (!navigator.geolocation) { $('f-gps').textContent = 'not available'; return; }
  $('f-gps').textContent = 'locating…';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      $('f-position').value =
        `${pos.coords.latitude.toFixed(4)}, ${pos.coords.longitude.toFixed(4)}`;
      $('f-gps').textContent = 'Use my location';
    },
    () => { $('f-gps').textContent = 'could not locate'; },
    { timeout: 8000, maximumAge: 60000 },
  );
});

$('f-shipment').addEventListener('change', () => {
  const value = $('f-shipment').value.trim();
  remember(value);
  loadAsk(value);
});

$('dv-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const shipment = $('f-shipment').value.trim();
  if (!shipment) { $('f-shipment').focus(); return; }
  if (!state.status) {
    $('f-status').classList.add('is-missing');
    setTimeout(() => $('f-status').classList.remove('is-missing'), 1200);
    return;
  }

  const eta = $('f-eta').value;
  enqueue({
    shipment_id: shipment,
    status: state.status,
    position: $('f-position').value.trim() || null,
    // Local datetime-local has no zone. Sent as UTC because the whole system
    // is UTC and a naive local time would silently shift the deadline by
    // however many hours the driver happens to be from Zurich.
    revised_eta: eta ? new Date(eta).toISOString() : null,
    load_state: state.load,
    note: $('f-note').value.trim() || null,
    reported_by: null,
    confirms_disruption: $('f-confirm').checked,
    // Stamped NOW, on the device. See the header comment.
    observed_at: observedNow(),
  });

  remember(shipment);
  $('f-note').value = '';
  $('f-confirm').checked = false;
  $('dv-send').textContent = 'Sent';
  setTimeout(() => { $('dv-send').textContent = 'Send report'; }, 1600);
});

window.addEventListener('online', () => { renderNet(); drain(); });
window.addEventListener('offline', renderNet);

(function start() {
  if (PINNED) {
    const banner = document.createElement('div');
    banner.className = 'dv-pinned';
    banner.innerHTML = `<b>Demo clock</b> Reports are stamped inside the
      board's pinned instant (${esc(PINNED.slice(0, 16).replace('T', ' '))} UTC)
      so they appear on it. In production this is the real clock.`;
    document.querySelector('.dv-main').prepend(banner);
  }
  renderNet();
  renderQueue();
  const shipment = params.get('shipment') || recall();
  if (shipment) {
    $('f-shipment').value = shipment;
    loadAsk(shipment);
  }
  $('dv-sub').textContent = shipment
    ? `consignment ${shipment}`
    : 'enter your consignment number';
  drain();
  // Retry on a slow cadence as well as on the `online` event: a phone can
  // report itself online while sitting behind a captive portal.
  setInterval(drain, 20000);
})();
