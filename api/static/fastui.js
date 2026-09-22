/* Shared helpers for the two Solution A pages.
 *
 * Small on purpose. The only thing worth centralising between a dashboard and
 * a route page is the part that talks to the server and the part that renders
 * an option, because those two are what must not drift.
 */
'use strict';

const FastUI = (() => {
  /* The as-of and shipment count ride in the URL so a planner can send
   * somebody the exact screen they are looking at. Defaults match the API. */
  const params = new URLSearchParams(location.search);
  const AS_OF = params.get('as_of') || '2026-09-18T06:00:00+00:00';
  const SHIPMENTS = params.get('shipments') || '150';
  const QS = `as_of=${encodeURIComponent(AS_OF)}&shipments=${encodeURIComponent(SHIPMENTS)}`;

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const chf = (n) => {
    if (n === null || n === undefined) return '—';
    if (n === 0) return 'no extra cost';
    return `CHF ${Math.round(n).toLocaleString('de-CH').replace(/,/g, '’')}`;
  };

  /* Hours, said the way a person says them. "0.4 h" is a number pretending to
   * be information; "under an hour" is what the planner would write down. */
  const hours = (h) => {
    if (h === null || h === undefined) return 'unknown';
    if (h < 1) return 'under an hour';
    if (h < 48) return `${Math.round(h)} h`;
    return `${Math.round(h / 24)} days`;
  };

  /* The same quantity, tighter, for a table cell where "under an hour" wraps
   * onto two lines and stops the column scanning. */
  const hoursShort = (h) => {
    if (h === null || h === undefined) return '—';
    if (h < 1) return '< 1 h';
    if (h < 48) return `${Math.round(h)} h`;
    return `${Math.round(h / 24)} d`;
  };

  const lateness = (days) => {
    if (!days || days <= 0) return 'arrives on the agreed date';
    if (days < 1) return `${Math.round(days * 24)} h late`;
    return `${days.toFixed(1)} days late`;
  };

  async function get(path) {
    const response = await fetch(`${path}${path.includes('?') ? '&' : '?'}${QS}`,
      { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  }

  async function post(path, body) {
    const response = await fetch(`${path}${path.includes('?') ? '&' : '?'}${QS}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    /* A refusal is not an exception: the server answers 409 with the reason,
     * and the reason is the thing the planner needs to read. Throwing here
     * would replace it with "Failed to fetch". */
    return { ok: response.ok, status: response.status, payload };
  }

  function toast(message, bad = false) {
    document.querySelectorAll('.toast').forEach((t) => t.remove());
    const el = document.createElement('div');
    el.className = `toast${bad ? ' toast--bad' : ''}`;
    el.setAttribute('role', 'status');
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), bad ? 7000 : 4200);
  }

  /* One option, rendered the same way on both pages.
   *
   * The second line is always time, never money: how soon it is resolved and
   * whether the freight still makes the date. Cost is third and quiet,
   * because that is the ordering the product now argues for. */
  function optionHTML(option, primary) {
    const own = option.executable;
    const covers = option.shipments
      ? ` · covers ${option.shipments} consignment${option.shipments === 1 ? '' : 's'}`
      : '';
    const meta = [
      `resolved in ${hours(option.hours_to_resolve)}`,
      lateness(option.days_late_after),
      chf(option.cost_chf),
    ].join('  ·  ');

    const why = own
      ? esc(option.detail || '')
      : `${esc(option.owner)} owns this lever — we can ask, not execute.`;

    /* Whether the vehicles exist. Sits on the row rather than only on the
       capacity board below, because this is the row with the button on it. */
    const fleet = option.capacity_note
      ? `<span class="act__fleet">${esc(option.capacity_note)}</span>`
      : '';

    return `
      <button type="button"
              class="act${primary && own ? ' act--primary' : ''}${own ? '' : ' act--ask'}"
              data-option="${esc(option.option_id)}"
              ${own ? '' : 'aria-disabled="true"'}>
        <span>
          <span class="act__label">${esc(option.label)}</span>
          <span class="act__meta">${esc(meta)}${esc(covers)}</span>
          <span class="act__why">${why}</span>
          ${fleet}
        </span>
        <span class="act__go">${own ? 'Do it' : 'Who to call'}</span>
      </button>`;
  }

  /* Server-sent events, with the reconnect left to the browser.
   *
   * onMessage is called for every event; onOpen/onError only move the dot.
   * The page decides what a message means — the dashboard refetches, the
   * route page refetches its own lane. */
  function listen(topics, handlers) {
    const url = `/api/v2/stream?topics=${encodeURIComponent(topics.join(','))}`;
    let source;
    try {
      source = new EventSource(url);
    } catch (err) {
      return null;   // no EventSource: the page still works, just not live
    }
    source.addEventListener('open', () => handlers.onOpen && handlers.onOpen());
    source.addEventListener('error', () => handlers.onError && handlers.onError());
    ['signal', 'disruption', 'action', 'field', 'undo'].forEach((topic) => {
      source.addEventListener(topic, (event) => {
        let data = {};
        try { data = JSON.parse(event.data); } catch { /* keep going */ }
        handlers.onMessage && handlers.onMessage(topic, data);
      });
    });
    return source;
  }

  return { AS_OF, SHIPMENTS, QS, esc, chf, hours, hoursShort, lateness,
           get, post, toast, optionHTML, listen };
})();
