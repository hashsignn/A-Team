/* The deadline clock: how long each option stays open, counted down.
 *
 * Calm on purpose. A deadline in red that flashes its seconds is a stress
 * signal, and a planner under stress decides worse. So: a small green clock,
 * hours and minutes in the text colour, the seconds in grey so they tick
 * without pulling the eye, and "closed" in grey once it is past. Never red.
 *
 * "Now" is the board's moment plus the time since this page learned it, so
 * a board opened at a fixed demo time counts down from that time, second by
 * second, the same as a live one would.
 *
 *   DeadlineClock.start(board.as_of);          // once, when the data lands
 *   el.innerHTML = DeadlineClock.html(iso);    // anywhere, any number
 *
 * Every element made by html() is refreshed by one shared one-second tick.
 */
'use strict';

const DeadlineClock = (() => {
  let base = null;   // the board's moment, in ms since the epoch
  let t0 = 0;        // Date.now() when the page learned it
  let timer = null;

  const pad = (n) => String(n).padStart(2, '0');
  const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function start(asOf) {
    const ms = Date.parse(asOf);
    if (!Number.isFinite(ms)) return;
    base = ms;
    t0 = Date.now();
    if (!timer) timer = setInterval(tick, 1000);
    tick();
  }

  function now() {
    return base == null ? Date.now() : base + (Date.now() - t0);
  }

  function left(iso) {
    const due = Date.parse(iso);
    return Number.isFinite(due) ? due - now() : NaN;
  }

  /* "Tue 6 Oct, 23:28 UTC": the moment itself, for a title or a table. */
  function at(iso) {
    const due = Date.parse(iso);
    if (!Number.isFinite(due)) return '';
    const d = new Date(due);
    return `${WD[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]}, `
      + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  }

  function inner(iso) {
    const ms = left(iso);
    if (!(ms > 0)) return '<span class="dl-closed">closed</span>';
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return '<i class="dl-ico" aria-hidden="true"></i>'
      + `<b class="dl-hm">${d ? `${d}d ` : ''}${pad(h)}:${pad(m)}</b>`
      + `<span class="dl-sec">:${pad(sec)}</span>`;
  }

  /* One clock. `iso` is the moment the option closes; null gives a quiet
   * dash, because "no deadline" is an answer too. */
  function html(iso) {
    if (!iso) return '<span class="dl dl--none">no deadline</span>';
    const closed = !(left(iso) > 0);
    return `<span class="dl${closed ? ' is-closed' : ''}" data-due="${iso}" `
      + `title="Closes ${at(iso)}">${inner(iso)}</span>`;
  }

  function tick() {
    document.querySelectorAll('.dl[data-due]').forEach((el) => {
      const closed = !(left(el.dataset.due) > 0);
      el.innerHTML = inner(el.dataset.due);
      el.classList.toggle('is-closed', closed);
    });
  }

  return { start, now, html, at, left, tick };
})();

// Also on window, so a page can ask whether the clock is loaded.
window.DeadlineClock = DeadlineClock;
