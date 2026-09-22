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

const state = { status: null, load: 'intact', sending: false, role: 'driver',
                fix: null, photos: [], token: null };

/* Who can report, and what it means for the planner.
 *
 * Everyone here is on site EXCEPT 'relayed', and that single distinction is
 * the only thing this field is for: a report from somebody who can see the
 * freight is tier 1 and can unlock a reroute; one passed along second-hand
 * is kept, shown and tier 2, but cannot move the money on its own.
 *
 * Nobody is blocked from reporting. It records what kind of knowledge this
 * is, which is not recoverable from the words — "the lock is shut" reads
 * identically either way. */
const ROLES = [
  { id: 'driver',     label: 'Driver',       note: 'You are with the vehicle or on board.' },
  { id: 'site_agent', label: 'On site',      note: 'You are with the load.' },
  { id: 'terminal',   label: 'Terminal',     note: 'You are at the depot or quay.' },
  { id: 'relayed',    label: 'Told by someone', note:
      'Kept and shown to the planner, but a second-hand account on its own '
      + 'will not release a re-route.' },
];
const ROLE_KEY = 'radar.driver.role';
const TOKEN_KEY = 'radar.driver.token';

/* THE CREDENTIAL.
 *
 * Handed over once, as a link: ?k=<token>. The app keeps it and removes it
 * from the address bar immediately.
 *
 * A token in a URL is a real trade-off and worth naming. It can end up in a
 * server log, a proxy log or a browser history, so it is stripped from the
 * visible URL the moment it is read, and the planner can revoke it in one
 * command. It is still the only channel that works for handing a credential
 * to somebody standing next to a truck: a login form means a password, a
 * password means a reset flow, and a reset flow means the driver cannot file
 * the report they stopped to file.
 *
 * Kept in localStorage, which is per-origin and survives the app being
 * closed. It can throw in a private window, so every read and write is
 * guarded — a driver with no storage can still file, they just re-open the
 * link each time. */
function readToken() {
  try { return localStorage.getItem(TOKEN_KEY) || null; }
  catch { return null; }
}
function saveToken(value) {
  try { localStorage.setItem(TOKEN_KEY, value); } catch { /* private window */ }
}
function captureToken() {
  const fromLink = new URLSearchParams(location.search).get('k');
  if (fromLink) {
    saveToken(fromLink);
    state.token = fromLink;
    const url = new URL(location.href);
    url.searchParams.delete('k');
    history.replaceState(null, '', url);      // out of the address bar at once
  } else {
    state.token = readToken();
  }
}

function readRole() {
  try { return localStorage.getItem(ROLE_KEY) || 'driver'; }
  catch { return 'driver'; }
}
function saveRole(id) {
  try { localStorage.setItem(ROLE_KEY, id); } catch { /* private window */ }
}

/* Remembered between reports: the same person files from the same cab all
 * week, and asking every time is the kind of friction that gets an app
 * closed. One tap, once. */
function renderRoles() {
  const host = document.getElementById('f-role');
  if (!host) return;
  state.role = readRole();
  host.innerHTML = ROLES.map((r) => `
    <button type="button" class="dv-role-btn${r.id === state.role ? ' is-on' : ''}"
            data-role="${r.id}" aria-pressed="${r.id === state.role}">
      ${r.label}
    </button>`).join('');
  host.querySelectorAll('[data-role]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.role = btn.dataset.role;
      saveRole(state.role);
      captureToken();
  renderRoles();
  renderPhotos();
  renderAuth();
  $('f-photo').addEventListener('change', (e) => addPhotos(e.target.files));
    });
  });
  const note = document.getElementById('dv-role-note');
  if (note) note.textContent = (ROLES.find((r) => r.id === state.role) || {}).note || '';
}

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
        headers: {
          'Content-Type': 'application/json',
          ...(state.token ? { 'X-Report-Token': state.token } : {}),
        },
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
      } else if (res.status === 401) {
        // The credential is missing, wrong, or was revoked. Retrying will
        // never help either, and this is the failure most likely to go
        // unnoticed: the queue would grow quietly while the driver believed
        // every report had landed. So it comes out, and it says what to do.
        logRejected(row, 'this phone is not authorised — ask the planner for '
                       + 'a new link, then send again');
        state.authFailed = true;
        renderAuth();
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

/* The fix is KEPT AS NUMBERS, and the text box is left alone.
 *
 * This used to write "47.5564, 7.5904" into the position field and throw the
 * coordinates away. Nothing downstream could use that: a string that happens
 * to look like a position is not one, and it also destroyed the more useful
 * answer — "Kaub, third in the queue" — by overwriting it.
 *
 * Now both travel. The numbers put a pin on the planner's map; the words say
 * what the pin cannot. */
$('f-gps').addEventListener('click', () => {
  /* An insecure origin is the likeliest reason this fails, and the least
   * obvious.
   *
   * Browsers only hand out a position on HTTPS or localhost. Over plain HTTP
   * — which is what a phone hitting http://<laptop-ip>:8000 gets —
   * navigator.geolocation still EXISTS, so the old check passed and
   * getCurrentPosition then failed straight into "could not locate". That
   * blames the GPS for something the page did, and somebody would spend an
   * hour walking outside to get a better signal.
   *
   * The words the driver still can type are unaffected; only the pin is. */
  if (!window.isSecureContext) {
    $('f-gps').textContent = 'needs https — type where you are instead';
    $('f-gps').title =
      'Browsers only give a position on https or localhost. The forwarded '
      + 'Codespaces URL is https and works; a plain http://<ip>:8000 address '
      + 'does not.';
    return;
  }
  if (!navigator.geolocation) { $('f-gps').textContent = 'not available'; return; }
  $('f-gps').textContent = 'locating…';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      state.fix = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        accuracy_m: pos.coords.accuracy,
      };
      const acc = Math.round(pos.coords.accuracy || 0);
      $('f-gps').textContent = `location attached ±${acc} m`;
      $('f-gps').classList.add('is-on');
    },
    () => { $('f-gps').textContent = 'could not locate'; },
    { timeout: 8000, maximumAge: 60000, enableHighAccuracy: true },
  );
});

/* Photos.
 *
 * Uploaded as they are taken, not held until send: the upload is the slow
 * part on a bad connection, and doing it while the driver is still typing
 * means send is instant. Each returns an id; the report carries the ids.
 *
 * If the upload fails the photo is dropped with a visible message rather than
 * silently queued. A photo is evidence, and evidence that quietly did not
 * arrive is worse than one the driver knows to retake. */
const MAX_PHOTOS = 6;

async function addPhotos(files) {
  const note = $('dv-photo-note');
  for (const file of Array.from(files).slice(0, MAX_PHOTOS - state.photos.length)) {
    note.textContent = 'sending photo…';
    try {
      const res = await fetch('/api/v1/photos', {
        method: 'POST',
        headers: {
          'Content-Type': file.type || 'application/octet-stream',
          ...(state.token ? { 'X-Report-Token': state.token } : {}),
        },
        body: file,
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      state.photos.push(body.photo_id);
      note.textContent = '';
    } catch (err) {
      note.textContent = `photo not sent: ${err.message}. Try again.`;
    }
  }
  renderPhotos();
}

function renderPhotos() {
  const host = $('dv-photos');
  if (!host) return;
  host.innerHTML = state.photos.map((id) => `
    <div class="dv-thumb">
      <img src="/api/v1/photos/${id}" alt="photo from site">
      <button type="button" class="dv-thumb-x" data-photo="${id}"
              aria-label="Remove this photo">&times;</button>
    </div>`).join('');
  host.querySelectorAll('[data-photo]').forEach((b) =>
    b.addEventListener('click', () => {
      state.photos = state.photos.filter((x) => x !== b.dataset.photo);
      renderPhotos();
    }));
  const input = $('f-photo');
  if (input) input.disabled = state.photos.length >= MAX_PHOTOS;
}

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
    role: state.role,
    lat: state.fix ? state.fix.lat : null,
    lon: state.fix ? state.fix.lon : null,
    accuracy_m: state.fix ? state.fix.accuracy_m : null,
    photos: state.photos.slice(),
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
  captureToken();
  renderRoles();
  renderPhotos();
  renderAuth();
  $('f-photo').addEventListener('change', (e) => addPhotos(e.target.files));
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


/* Whether this phone can file at all, said before the driver fills the form.
 *
 * Checked on open rather than on send: somebody who has stopped at a lock to
 * report a problem should find out that their link expired BEFORE they type
 * it out, not after. */
async function renderAuth() {
  const banner = document.getElementById('dv-auth');
  if (!banner) return;
  if (state.authFailed) {
    banner.hidden = false;
    banner.className = 'dv-auth dv-auth--bad';
    banner.innerHTML = '<b>This phone is not authorised.</b> Ask the planner '
      + 'for a new link. Anything already typed is kept.';
    return;
  }
  try {
    const res = await fetch('/api/v1/reports?limit=1', {
      headers: state.token ? { 'X-Report-Token': state.token } : {},
    });
    if (res.status === 401) {
      state.authFailed = true;
      return renderAuth();
    }
    banner.hidden = true;
  } catch {
    banner.hidden = true;     // offline is not unauthorised
  }
}
