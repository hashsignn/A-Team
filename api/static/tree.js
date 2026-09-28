/* The Action decision tree, in its own window.
 *
 * One route, every way out of its trouble, as a tree that grows as you
 * choose. The disruption sits at the top; each click opens the level under
 * it and fills the panel on the right with the numbers behind that box.
 *
 *    What is happening      the events, with the contract clocks they start
 *    Who is hit             not hit | absorbed by buffers | need action
 *    Keep the dates?        keep the date | cut the damage | tell the customer
 *    Which way              the ways, ranked, best first; another site; as planned
 *    Who carries it         per mode, the carriers along the way
 *    Sign off and book      within the limit or not; book it, with undo
 *
 * The data is /api/decision/{route}: the same engines as the board, arranged
 * so that every order at risk sits in exactly one branch. Nothing is decided
 * here that the board did not already advise; the page only makes the
 * choice readable and one click away.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (v) => (v == null || !Number.isFinite(+v) ? '–' : Math.round(+v).toLocaleString('en-US'));
const chf = (v) => (v == null ? '–' : `CHF ${num(v)}`);
const pct = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`);
const hrs = (h) => (h == null ? '–' : h < 1 ? '<1 h' : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`);
const days = (d) => (d == null ? '–' : d < 0.05 ? '0 d' : `${d < 10 ? (+d).toFixed(1) : Math.round(d)} d`);
const ORD = ['', '1st', '2nd', '3rd'];
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;
const ords = (n) => (n === 1 ? 'order' : 'orders');
const MODE = { road: 'Road', rail: 'Rail', sea: 'Sea', barge: 'Barge', air: 'Air' };
const plain = (name) => String(name || '').replace(/\s*\(synthetic\)\s*$/i, '');

const Q = new URLSearchParams(location.search);
const META_AS_OF = document.querySelector('meta[name="radar-default-as-of"]')?.content || '';
const ROUTE = Q.get('route') || '';
const AS_OF = Q.get('as_of') || (META_AS_OF.startsWith('__') ? '' : META_AS_OF);
const SHIPMENTS = Q.get('shipments') || '';
const PRESELECT = Q.get('opt') || '';

function qs(extra = {}) {
  const q = new URLSearchParams();
  if (AS_OF) q.set('as_of', AS_OF);
  if (SHIPMENTS) q.set('shipments', SHIPMENTS);
  Object.entries(extra).forEach(([k, v]) => v != null && q.set(k, v));
  return q.toString();
}

const S = {
  d: null,          // the decision payload
  T: new Map(),     // node id -> node
  path: [],         // the chosen node at each level
  focus: null,      // the node whose numbers the panel shows
  carrier: {},      // `${wayId}|${mode}` -> index of the chosen carrier
  booked: {},       // option id -> { ids, sentence, until }
  more: false,      // "More detail" open
};

// ================================================================ the tree
/* Every node, built once from the payload. A node knows its children and
 * the question its children answer; the renderer only walks the path. */
function build(d) {
  const T = new Map();
  const add = (n) => { T.set(n.id, n); return n.id; };
  const hit = d.hit || {};
  const notHit = Math.max(0, (hit.of || 0) - (hit.orders || 0));
  const total = d.cost?.total_chf ?? hit.exposure_chf;

  add({ id: 'root', kind: 'root', ask: 'Who is hit', kids: ['nothit', 'absorbed', 'need'] });
  add({ id: 'nothit', kind: 'nothit', n: notHit, disabled: !notHit });
  add({ id: 'absorbed', kind: 'absorbed', n: hit.absorbed || 0, disabled: !hit.absorbed });
  add({ id: 'need', kind: 'need', n: hit.need || 0, total, disabled: !hit.need,
    ask: 'Can we keep the promised dates?', kids: ['keep', 'reduce', 'tell'] });

  // ---- keep the date: the ways, ranked; another site; as planned. When
  // staying is likely on time and cheaper than any way, it ranks first.
  const stayBest = Boolean(d.keep?.stay_best);
  const ways = (d.keep?.options || []).map((o, i) => add({
    id: `way:${o.id}`, kind: 'way', o, rank: i + 1 + (stayBest ? 1 : 0),
  }));
  const sources = (d.sources || []).map((s) => add({ id: `src:${s.id}`, kind: 'src', s }));
  const stay = (d.compare || []).find((r) => r.baseline);
  const stayId = stay ? add({ id: 'stay', kind: 'stay', r: stay, best: stayBest, rank: stayBest ? 1 : null }) : null;
  const keepKids = stayBest ? [stayId, ...ways, ...sources] : [...ways, ...sources, ...(stayId ? [stayId] : [])];
  add({ id: 'keep', kind: 'keep', n: d.keep?.kept?.length || 0, disabled: !(d.keep?.kept?.length),
    ask: 'Which way? Best first', kids: keepKids });

  // ---- cut the damage: best net first.
  const reduce = (d.reduce?.options || []).map((o) => ({
    o, net: o.route ? -1e12 + (o.cost_chf || 0) * -1 : (o.saves_chf || 0) - (o.cost_chf || 0),
  })).sort((a, b) => b.net - a.net);
  const reds = reduce.map(({ o }, i) => add({ id: `red:${o.id || o.label}`, kind: 'red', o, rank: i + 1 }));
  add({ id: 'reduce', kind: 'reduce', n: d.reduce?.orders?.length || 0, disabled: !(d.reduce?.orders?.length),
    ask: 'What cuts the damage? Best first', kids: reds });

  // ---- tell the customer: one box per customer.
  const byCustomer = new Map();
  (d.tell?.orders || []).forEach((o) => {
    const c = byCustomer.get(o.customer) || { customer: o.customer, orders: [], late: 0, p: 0 };
    c.orders.push(o); c.late = Math.max(c.late, o.late_days || 0); c.p = Math.max(c.p, o.p_late || 0);
    byCustomer.set(o.customer, c);
  });
  const custs = [...byCustomer.values()].sort((a, b) => b.late - a.late)
    .map((c) => add({ id: `cust:${c.customer}`, kind: 'cust', c }));
  add({ id: 'tell', kind: 'tell', n: d.tell?.orders?.length || 0, disabled: !(d.tell?.orders?.length),
    ask: 'Who to tell', kids: custs });

  // ---- under each way: its carriers per mode, then sign off and book.
  ways.forEach((id) => {
    const way = T.get(id);
    const o = way.o;
    const modes = [...new Set((o.carriers || []).flatMap((c) => c.modes.filter((m) => (o.modes || []).includes(m))))];
    const fin = finals(T, add, id, { cost: o.cost_chf, orders: o.orders, act: o.id, label: o.label });
    if (modes.length) {
      way.ask = 'Who carries it';
      way.kids = modes.map((m) => add({ id: `car:${id}|${m}`, kind: 'car', way: id, mode: m,
        ask: 'Sign off and book', kids: fin }));
    } else {
      way.ask = 'Sign off and book';
      way.kids = fin;
    }
  });
  sources.forEach((id) => {
    const src = T.get(id);
    src.ask = 'Who arranges it';
    src.kids = [add({ id: `ask:${id}`, kind: 'ask', src: id })];
  });
  reds.forEach((id) => {
    const red = T.get(id);
    const o = red.o;
    const orders = o.route ? (o.orders_n || o.shipment_ids?.length || 0) : (o.orders?.length || 0);
    const act = o.route ? o.id : `template:${o.label}`;
    red.ask = 'Sign off and book';
    red.kids = finals(T, add, id, { cost: o.cost_chf, orders, act, label: o.label, tell: true });
  });
  return T;
}

/* The last level: sign-off, book, and (when the date still slips) tell. */
function finals(T, add, parent, x) {
  const out = [
    add({ id: `sign:${parent}`, kind: 'sign', parent, x }),
    add({ id: `book:${parent}`, kind: 'book', parent, x }),
  ];
  if (x.tell) out.push(add({ id: `told:${parent}`, kind: 'told', parent, x }));
  return out;
}

// ================================================================ the boxes
function face(n) {
  const d = S.d;
  switch (n.kind) {
    case 'root': {
      const ev = d.happening || [];
      const first = d.clocks?.[0]?.starts_at;
      return {
        kicker: ev[0]?.kind || 'Now', title: short(ev[0]?.title || 'No event on this route', 54),
        big: String(ev.length), unit: ev.length === 1 ? 'event' : 'events',
        sub: first ? `since ${DeadlineClock.at(first).replace(' UTC', '')}` : '',
        more: ev.length > 1 ? `+ ${short(ev[1].title, 40)}` : '', wide: true,
      };
    }
    case 'nothit': return { kicker: 'Not hit', big: String(n.n), unit: ords(n.n), sub: 'no change', tone: 'leaf' };
    case 'absorbed': return { kicker: 'Absorbed', big: String(n.n), unit: ords(n.n), sub: 'buffers take it', tone: 'leaf' };
    case 'need': return { kicker: 'Need action', big: String(n.n), unit: ords(n.n),
      sub: `${chf(n.total)} if nobody acts` };
    case 'keep': {
      const best = d.keep?.options?.[0];
      return { kicker: 'Yes', title: 'Keep the date', big: `${n.n}/${d.hit.need}`, unit: 'orders',
        sub: d.keep?.stay_best ? 'best: stay as planned' : best ? `best way ${chf(best.cost_chf)} extra` : '' };
    }
    case 'reduce': return { kicker: 'Partly', title: 'Cut the damage', big: String(n.n), unit: ords(n.n),
      sub: plural((d.reduce?.options || []).length, 'way') };
    case 'tell': {
      const c = new Set((d.tell?.orders || []).map((o) => o.customer)).size;
      return { kicker: 'No', title: 'Tell the customer', big: String(n.n), unit: ords(n.n),
        sub: plural(c, 'customer') };
    }
    case 'way': {
      const o = n.o;
      return { rank: n.rank, kicker: n.rank === 1 ? 'Best' : ORD[n.rank] || `${n.rank}th`,
        title: wayName(o), big: `${o.on_time}/${o.orders}`, unit: 'on time',
        sub: `${chf(o.cost_chf)} · starts ${hrs(o.starts_in_h)}`, clock: o.closes_at };
    }
    case 'src': {
      const s = n.s;
      return { kicker: 'Other site', title: s.label, big: `${s.on_time}/${s.orders}`, unit: 'on time',
        sub: `goods ${chf(s.value_chf)}`, clock: s.closes_at, tone: 'site' };
    }
    case 'stay': {
      const r = n.r;
      if (n.best) {
        return { rank: 1, kicker: 'Best', title: 'Stay as planned', big: `${r.on_time}/${r.orders}`, unit: 'likely on time',
          sub: `${chf(r.exposure_chf)} expected · no extra cost` };
      }
      return { kicker: 'As planned', title: 'Stay as planned', big: `${r.on_time}/${r.orders}`, unit: 'on time',
        sub: `${chf(r.exposure_chf)} at risk · ${days(r.late_after_days)} late`, tone: 'base' };
    }
    case 'red': {
      const o = n.o;
      if (o.route) {
        return { rank: Math.min(n.rank, 3), kicker: 'Faster, still late', title: wayName(o),
          big: days(o.late_after_days), unit: 'late', sub: `${chf(o.cost_chf)} · ${o.orders_n} orders`,
          clock: o.closes_at };
      }
      return { rank: Math.min(n.rank, 3), kicker: n.rank === 1 ? 'Best' : ORD[n.rank] || `${n.rank}th`,
        title: short(o.label, 40), big: chf(o.saves_chf), unit: 'saved',
        sub: `costs ${chf(o.cost_chf)} · ${o.orders.length} orders`, clock: o.closes_at };
    }
    case 'cust': {
      const c = n.c;
      const small = c.late < 0.5;
      return { kicker: tierOf(c.customer) === 'A' ? 'Key account' : 'Customer', title: c.customer,
        big: small ? pct(c.p) : days(c.late), unit: small ? 'chance late' : 'late',
        sub: `${plural(c.orders.length, 'order')} · ${small ? 'under a day if late' : `${pct(c.p)} chance`}` };
    }
    case 'car': {
      const c = carrierFor(n.way, n.mode);
      const cap = capacity(c?.capacity);
      return { kicker: MODE[n.mode] || n.mode, title: c ? plain(c.name) : 'None listed',
        big: cap.big || 'on request', unit: '',
        sub: c ? [cap.rest, `${num(c.km)} km away · ${c.channel || 'call'}`].filter(Boolean).join(' · ') : '',
        tone: c?.synthetic ? '' : 'real', tag: c ? (c.synthetic ? 'example' : 'real operator') : '' };
    }
    case 'ask': {
      const p = d.approval?.procurement;
      return { kicker: 'Procurement', title: p?.name || 'Procurement', big: '1', unit: 'call',
        sub: '', tone: 'act' };
    }
    case 'sign': {
      const sign = signOff(n.x.cost);
      return { kicker: 'Sign-off', title: sign.ok ? 'You approve' : sign.who, big: chf(n.x.cost), unit: '',
        sub: `limit ${chf(sign.limit)}${sign.ok ? ' ✓' : ''}`, tone: sign.ok ? 'ok' : '' };
    }
    case 'book': {
      const done = S.booked[n.x.act];
      return { kicker: done ? 'Booked' : 'Book', title: done ? 'Running' : 'Book it', big: String(n.x.orders),
        unit: ords(n.x.orders), sub: done ? 'undo possible' : 'undo within 15 min', tone: 'act' };
    }
    case 'told': return { kicker: 'Tell', title: 'Tell customers', big: String(n.x.orders), unit: ords(n.x.orders),
      sub: 'new date, one message' };
    default: return { title: n.id };
  }
}

function short(s, n) {
  const t = String(s || '');
  return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t;
}

function wayName(o) {
  return String(o.label || '').replace(/^Reroute via /, 'Via ').replace(/ \+(\d+) more$/, ' +$1');
}

function tierOf(customer) {
  return (S.d.orders || []).find((o) => o.customer === customer)?.tier || 'B';
}

/* "140 TEU on a Cape-routed service" is a number and a note: the number
 * goes big, the note goes under it. */
function capacity(text) {
  const t = String(text || '').trim();
  const m = t.match(/^([\d.,]+\s*[A-Za-z]+)\s*(.*)$/);
  return m ? { big: m[1], rest: m[2] } : { big: t, rest: '' };
}

function carrierFor(wayId, mode) {
  const o = S.T.get(wayId)?.o;
  const list = (o?.carriers || []).filter((c) => c.modes.includes(mode));
  const i = S.carrier[`${wayId}|${mode}`] ?? 0;
  return list[i] || list[0] || null;
}

function signOff(cost) {
  const a = S.d.approval || {};
  const limit = a.limit_chf ?? 10000;
  const ok = (cost || 0) <= limit;
  return { ok, limit, who: a.controlling?.name ? 'Controlling' : 'Controlling', crisis: a.crisis,
    person: a.controlling };
}

function nodeHTML(n, i, level) {
  const f = face(n);
  const onPath = S.path[level] === n.id;
  const cls = ['tn', `tn--${n.kind}`];
  if (f.tone) cls.push(`tn--${f.tone}`);
  if (f.wide) cls.push('tn--wide');
  if (onPath) cls.push('is-on');
  if (S.focus === n.id) cls.push('is-focus');
  if (n.disabled) cls.push('is-off');
  if (S.path[level] && !onPath) cls.push('is-dim');
  const k = f.rank ? Math.min(f.rank, 3) : 0;
  const rank = f.rank ? `<span class="tn-rank" style="--r:var(--rank-${k});--ri:var(--rank-${k}-ink)">${f.rank}</span>` : '';
  const stripe = f.rank ? ` style="--i:${i};--r:var(--rank-${k})"` : ` style="--i:${i}"`;
  return `<button type="button" class="${cls.join(' ')}" data-id="${esc(n.id)}" data-level="${level}"${stripe}
      aria-pressed="${onPath}"${n.disabled ? ' aria-disabled="true"' : ''}>
    <span class="tn-top">${rank}<span class="tn-kicker">${esc(f.kicker || '')}</span>${f.tag ? `<span class="tn-tag">${esc(f.tag)}</span>` : ''}</span>
    ${f.title ? `<span class="tn-title">${esc(f.title)}</span>` : ''}
    <span class="tn-big"><b>${esc(f.big ?? '')}</b>${f.unit ? ` <span>${esc(f.unit)}</span>` : ''}</span>
    ${f.sub ? `<span class="tn-sub">${esc(f.sub)}</span>` : ''}
    ${f.more ? `<span class="tn-sub tn-sub--more">${esc(f.more)}</span>` : ''}
    ${f.clock !== undefined ? `<span class="tn-clock"><span class="tn-clock-k">closes in</span>${DeadlineClock.html(f.clock)}</span>` : ''}
  </button>`;
}

// ================================================================ render
function levels() {
  const out = [{ ids: ['root'], ask: 'What is happening' }];
  for (let i = 0; i < S.path.length; i++) {
    const n = S.T.get(S.path[i]);
    if (!n?.kids?.length || n.disabled) break;
    out.push({ ids: n.kids, ask: n.ask });
  }
  return out;
}

/* Rows up to `keep` stay in the DOM (their classes are refreshed); the rest
 * are rebuilt, and the new ones fade in. */
function render(keep = 0, grow = true) {
  const host = $('tr-levels');
  const rows = levels();
  const existing = [...host.querySelectorAll('.tr-level')];
  existing.forEach((el, i) => { if (i >= keep) el.remove(); });
  rows.forEach((row, level) => {
    let el = host.querySelector(`.tr-level[data-level="${level}"]`);
    const html = `<div class="tr-ask"><span class="tr-step">${level + 1}</span>${esc(row.ask)}</div>
      <div class="tr-row">${row.ids.map((id, i) => nodeHTML(S.T.get(id), i, level)).join('')}</div>`;
    if (el) {
      el.innerHTML = html;
      el.classList.remove('is-new');
      return;
    }
    el = document.createElement('div');
    el.className = `tr-level${grow ? ' is-new' : ''}`;
    el.dataset.level = String(level);
    el.innerHTML = html;
    host.appendChild(el);
  });
  fit();
  wires(keep);
}

/* A level is one row: its boxes narrow (down to 150 px) before they wrap,
 * so the branches never have to cross a box to reach one below it. */
function fit() {
  const host = $('tr-levels');
  const avail = host.clientWidth;
  host.querySelectorAll('.tr-row').forEach((row) => {
    const boxes = [...row.querySelectorAll('.tn:not(.tn--wide)')];
    if (!boxes.length) return;
    const gap = 16;
    const w = Math.floor((avail - gap * (boxes.length - 1)) / boxes.length);
    row.style.setProperty('--tn-w', `${Math.max(150, Math.min(190, w))}px`);
  });
}

function clearLevels() {
  $('tr-levels').querySelectorAll('.tr-level').forEach((el) => el.remove());
}

// ================================================================ wires
function offset(el, host) {
  let x = 0;
  let y = 0;
  while (el && el !== host) {
    x += el.offsetLeft;
    y += el.offsetTop;
    el = el.offsetParent;
  }
  return { x, y };
}

/* The branches: from the chosen box of each level to every box of the next,
 * drawn from layout positions, so a box still sliding in is already joined
 * where it will land. The chosen line is the accent; the others are quiet. */
function wires(fresh = 0) {
  const host = $('tr-levels');
  const svg = $('tr-wires');
  const w = host.scrollWidth;
  const h = host.scrollHeight;
  svg.setAttribute('width', w);
  svg.setAttribute('height', h);
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const paths = [];
  const rows = [...host.querySelectorAll('.tr-level')];
  rows.forEach((row, level) => {
    if (!level) return;
    const parentId = S.path[level - 1];
    const parent = rows[level - 1].querySelector(`.tn[data-id="${CSS.escape(parentId)}"]`);
    if (!parent) return;
    const p = offset(parent, host);
    const x1 = p.x + parent.offsetWidth / 2;
    const y1 = p.y + parent.offsetHeight;
    row.querySelectorAll('.tn').forEach((kid, i) => {
      const k = offset(kid, host);
      const x2 = k.x + kid.offsetWidth / 2;
      const y2 = k.y;
      const my = Math.max(18, (y2 - y1) * 0.55);
      const on = S.path[level] === kid.dataset.id;
      const off = kid.classList.contains('is-off');
      const cls = ['tw', on ? 'is-on' : '', off ? 'is-off' : '', level >= fresh ? 'is-new' : ''].join(' ');
      paths.push(`<path class="${cls}" style="--i:${i}" pathLength="1"
        d="M${x1},${y1} C${x1},${y1 + my} ${x2},${y2 - my} ${x2},${y2}"/>`
        + `<circle class="tw-dot ${on ? 'is-on' : ''} ${level >= fresh ? 'is-new' : ''}" style="--i:${i}" cx="${x2}" cy="${y2}" r="3.2"/>`);
    });
    paths.push(`<circle class="tw-dot is-on is-root" cx="${x1}" cy="${y1}" r="3.6"/>`);
  });
  svg.innerHTML = paths.join('');
}

// ================================================================ clicks
function choose(id, level, { scroll = true } = {}) {
  const n = S.T.get(id);
  if (!n) return;
  S.focus = id;
  S.more = false;
  if (!n.disabled && S.path[level] !== id) {
    // A different choice at this level: the branch below it regrows.
    S.path = S.path.slice(0, level).concat([id]);
    render(level + 1);
  } else {
    // Already on the path (or not a choice): show its numbers, keep the
    // tree below it as it is.
    render(99, false);
  }
  side(n);
  if (scroll && !n.disabled && n.kids?.length) {
    const next = $('tr-levels').querySelector(`.tr-level[data-level="${level + 1}"]`);
    if (next) next.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

$('tr-levels').addEventListener('click', (e) => {
  const box = e.target.closest('.tn');
  if (!box) return;
  choose(box.dataset.id, Number(box.dataset.level));
});

// ================================================================ the panel
function tiles(list) {
  return `<div class="ins-tiles">${list.filter(Boolean).map((t) => `
    <div class="ins-tile${t.wide ? ' ins-tile--wide' : ''}"><b>${t.v}</b><span>${esc(t.k)}</span></div>`).join('')}</div>`;
}

function table(cols, rows, { sel = null, key = null, cap = '' } = {}) {
  if (!rows.length) return '';
  // Fixed widths where a column says so; the rest share what is left, so a
  // long name ellipsises instead of pushing the numbers off the panel.
  const fixed = cols.some((c) => c.w);
  const group = fixed ? `<colgroup>${cols.map((c) => `<col${c.w ? ` style="width:${c.w}px"` : ''}>`).join('')}</colgroup>` : '';
  return `<div class="ins-tablewrap"><table class="ins-table${fixed ? ' ins-table--fixed' : ''}">
    ${cap ? `<caption>${esc(cap)}</caption>` : ''}${group}
    <thead><tr>${cols.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.k)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r) => `<tr class="${sel != null && key && r[key] === sel ? 'is-sel' : ''}${r._base ? ' is-base' : ''}">
      ${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

/* The deadline under the tiles: the clock, and the moment itself. */
function closes(iso, what = 'Closes in') {
  if (!iso) return '';
  return `<div class="ins-closes"><span class="ins-closes-k">${esc(what)}</span>
    <span class="ins-closes-v">${DeadlineClock.html(iso)}</span>
    <span class="ins-closes-at">${esc(DeadlineClock.at(iso))}</span></div>`;
}

function head(kicker, title, extra = '') {
  return `<div class="ins-head"><span class="ins-kicker">${esc(kicker)}</span><h2>${esc(title)}</h2>${extra}</div>`;
}

function more(html) {
  if (!html) return '';
  return `<button type="button" class="ins-more" aria-expanded="${S.more}">
      <span>${S.more ? 'Less detail' : 'More detail'}</span><i aria-hidden="true"></i></button>
    <div class="ins-more-body"${S.more ? '' : ' hidden'}>${html}</div>`;
}

function rankCell(r) {
  if (r._base && !r.best) return '<span class="ins-rank ins-rank--base">–</span>';
  if (r._site) return '<span class="ins-rank ins-rank--site">S</span>';
  const k = Math.min(r._rank, 3);
  return `<span class="ins-rank" style="--r:var(--rank-${k});--ri:var(--rank-${k}-ink)">${r._rank}</span>`;
}

/* Every way on one table: rank, orders on time, extra cost, how soon it
 * starts, how late it still is, and how long it stays open. */
function compareTable(selId) {
  const d = S.d;
  const lift = d.keep?.stay_best ? 1 : 0;
  const rows = (d.keep?.options || []).map((o, i) => ({ ...o, _rank: i + 1 + lift, _id: `way:${o.id}` }));
  (d.sources || []).forEach((s) => rows.push({ ...s, label: s.label, cost_chf: null, starts_in_h: null,
    late_after_days: 0, _site: true, _id: `src:${s.id}` }));
  const stay = (d.compare || []).find((r) => r.baseline);
  if (stay && lift) rows.unshift({ ...stay, _base: true, _rank: 1, _id: 'stay' });
  else if (stay) rows.push({ ...stay, _base: true, _id: 'stay' });
  return table([
    { k: '#', w: 30, f: rankCell },
    { k: 'Way', f: (r) => `<span class="ins-way" title="${esc(r._base ? 'Stay as planned' : r.label)}">${esc(r._base ? 'Stay as planned' : wayName(r))}</span>` },
    { k: 'On time', num: true, w: 62, f: (r) => `${r.on_time}/${r.orders}` },
    { k: '+CHF', num: true, w: 50, f: (r) => (r._base ? '0'
      : r.cost_chf == null ? '<span class="muted">n/a</span>' : num(r.cost_chf)) },
    { k: 'Closes in', num: true, w: 124, f: (r) => (r._base ? `<span class="muted">${days(r.late_after_days)} late</span>`
      : DeadlineClock.html(r.closes_at)) },
  ], rows, { sel: selId, key: '_id', cap: 'All ways, best first' });
}

function costBars() {
  const c = S.d.cost || {};
  const p = c.parts || {};
  const rows = [
    ['Customer impact', p.customer_impact],
    ['Expediting', p.expediting],
    ['Surcharges', p.surcharge],
    ['Delay penalties', c.penalties_counted ? p.penalty : null],
  ].filter(([, v]) => v != null && v > 0.5).sort((a, b) => b[1] - a[1]);
  const top = Math.max(1, ...rows.map(([, v]) => v), c.penalties_counted ? 0 : (p.penalty_if_counted || 0));
  const bar = (k, v, ghost = false) => `<div class="cb-row${ghost ? ' cb-row--ghost' : ''}">
      <span class="cb-k">${esc(k)}</span>
      <span class="cb-track"><i style="width:${Math.max(1.5, (v / top) * 100).toFixed(1)}%"></i></span>
      <span class="cb-v">${num(v)}</span></div>`;
  const penalty = c.penalties_counted ? ''
    : (p.penalty_if_counted > 0.5 ? bar('Penalties, if counted', p.penalty_if_counted, true) : '');
  return `<div class="ins-sec"><h3>What it costs if nobody acts <span class="ins-sum">${chf(c.total_chf)}</span></h3>
    <div class="cb">${rows.map(([k, v]) => bar(k, v)).join('')}${penalty}</div>
    <p class="ins-note">${c.penalties_counted
      ? 'Delay penalties from the framework contracts are counted.'
      : 'Delay penalties are not counted in the total.'}
      <button type="button" class="ins-link" id="pen-toggle">${c.penalties_counted ? 'Leave them out' : 'Count them'}</button></p></div>`;
}

function clocksTable() {
  const seen = new Map();
  (S.d.clocks || []).forEach((e) => (e.clocks || []).forEach((c) => {
    const key = `${c.id}|${c.due_at}`;
    if (!seen.has(key)) seen.set(key, { ...c, events: [e.title] });
    else seen.get(key).events.push(e.title);
  }));
  const rows = [...seen.values()].sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
  return table([
    { k: 'Contract clock', f: (r) => esc(r.label) },
    { k: 'Who', f: (r) => esc(r.party === 'sika' ? 'Sika' : r.party === 'carrier' ? 'Carrier' : r.party || '') },
    { k: 'Due', f: (r) => `<span class="ins-at">${esc(DeadlineClock.at(r.due_at))}</span>` },
    { k: 'Left', num: true, f: (r) => DeadlineClock.html(r.due_at) },
  ], rows, { cap: 'Started by the event timestamps' });
}

function ordersTable(filter = null) {
  const rows = (S.d.orders || []).filter((o) => !filter || filter(o));
  const word = { kept: 'keep date', reduced: 'cut damage', told: 'tell', absorbed: 'absorbed' };
  return table([
    { k: 'Order', f: (r) => `<span class="ins-id">${esc(r.shipment_id)}</span>` },
    { k: 'Customer', f: (r) => `${esc(short(r.customer, 26))}${r.tier === 'A' ? ' <span class="ins-a">A</span>' : ''}` },
    { k: 'At risk', num: true, f: (r) => num(r.loss_chf) },
    { k: 'Late', num: true, f: (r) => days(r.late_days) },
    { k: 'Chance', num: true, f: (r) => pct(r.p_late) },
    { k: 'Branch', f: (r) => `<span class="ins-br ins-br--${r.branch}">${word[r.branch] || r.branch}</span>` },
  ], rows);
}

function carriersTable(o, wayId) {
  const rows = (o.carriers || []).map((c) => {
    const mode = c.modes.find((m) => (o.modes || []).includes(m)) || c.modes[0];
    const list = o.carriers.filter((x) => x.modes.includes(mode));
    return { ...c, _mode: mode, _chosen: carrierFor(wayId, mode) === c, _i: list.indexOf(c) };
  });
  return `<div class="ins-sec"><h3>Who can carry it <span class="ins-sum">${rows.length}</span></h3>
    ${table([
      { k: '', f: (r) => `<input type="radio" class="car-pick" name="car-${esc(r._mode)}" data-mode="${esc(r._mode)}"
          data-i="${r._i}" data-way="${esc(wayId)}" ${r._chosen ? 'checked' : ''} aria-label="Choose ${esc(plain(r.name))}">` },
      { k: 'Carrier', f: (r) => `<span class="ins-mode">${esc(MODE[r._mode] || r._mode)}</span>
          ${esc(plain(r.name))}${r.synthetic ? '' : ' <span class="ins-real">real</span>'}` },
      { k: 'Free', num: true, f: (r) => `<span title="${esc(r.capacity || '')}">${esc(capacity(r.capacity).big || 'ask')}</span>` },
      { k: 'Away', num: true, f: (r) => `${num(r.km)} km` },
      { k: 'Contact', f: (r) => contact(r, true) },
    ], rows)}</div>`;
}

/* Email, phone and website as icons; the address is the tooltip. */
const REACH_ICON = {
  phone: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6.2 2.8l2 3.6-1.5 1.5a10 10 0 0 0 5.4 5.4l1.5-1.5 3.6 2-1 3a2 2 0 0 1-2.1 1.3A15 15 0 0 1 1.9 5.9a2 2 0 0 1 1.3-2.1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  email: '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="4.5" width="15" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3 5.5l7 5.5 7-5.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  web: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M2.8 10h14.4M10 2.8c2 2.1 3 4.5 3 7.2s-1 5.1-3 7.2c-2-2.1-3-4.5-3-7.2s1-5.1 3-7.2z" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
};
function contact(r) {
  if (!r) return '';
  const bits = [];
  if (r.email) bits.push(`<a class="reach" href="mailto:${esc(r.email)}" title="${esc(r.email)}" aria-label="Email">${REACH_ICON.email}</a>`);
  if (r.phone) bits.push(`<a class="reach" href="tel:${esc(r.phone.replace(/\s+/g, ''))}" title="${esc(r.phone)}" aria-label="Call">${REACH_ICON.phone}</a>`);
  if (r.portal) bits.push(`<a class="reach" href="${esc(r.portal)}" target="_blank" rel="noopener" title="${esc(r.portal)}" aria-label="Website">${REACH_ICON.web}</a>`);
  return bits.length ? `<span class="reach-row">${bits.join('')}</span>` : '<span class="muted">—</span>';
}

function pathChain(path) {
  if (!path?.length) return '';
  return `<ol class="ins-chain">${path.map((p) => `<li>${esc(p.name)}</li>`).join('')}</ol>`;
}

function side(n) {
  const d = S.d;
  const host = $('tr-side');
  let html = '';
  switch (n.kind) {
    case 'root': {
      const ev = d.happening || [];
      html = head('What is happening', d.route?.name || d.route_id)
        + tiles([
          { v: String(ev.length), k: ev.length === 1 ? 'event' : 'events' },
          { v: String(d.hit.of), k: 'orders on the route' },
          { v: String(d.hit.orders), k: 'orders hit' },
          { v: chf(d.cost?.total_chf), k: 'if nobody acts' },
        ])
        + `<div class="ins-sec"><h3>Events</h3><ul class="ins-events">${(d.clocks || []).map((e, i) => `
            <li><b>${esc(e.title)}</b><span>${esc(ev[i]?.kind || '')} · since ${esc(DeadlineClock.at(e.starts_at))}
              · ${ev[i]?.orders ?? 0} orders</span></li>`).join('')}</ul></div>`
        + `<div class="ins-sec"><h3>Contract clocks</h3>${clocksTable()}</div>`
        + more(`${(d.clocks?.[0]?.clocks || []).map((c) => `<p><b>${esc(c.label)}:</b> ${esc(c.basis)}</p>`).join('')}
          ${(d.after_delivery || []).map((c) => `<p><b>${esc(c.label)}, ${c.days} days after delivery:</b> ${esc(c.basis)}
            <a href="${esc(c.source)}" target="_blank" rel="noopener">source</a></p>`).join('')}
          ${ev.map((e) => `<p><b>${esc(e.kind)}:</b> ${esc(e.meaning)}</p>`).join('')}`);
      break;
    }
    case 'nothit':
      html = head('Who is hit', 'Not hit') + tiles([{ v: String(n.n), k: 'orders not touched by any event' }])
        + '<p class="ins-note">Nothing to do for these.</p>';
      break;
    case 'absorbed':
      html = head('Who is hit', 'Absorbed by buffers')
        + tiles([{ v: String(n.n), k: 'orders' }, { v: '< CHF 50', k: 'at risk each' }])
        + ordersTable((o) => o.branch === 'absorbed');
      break;
    case 'need':
      html = head('Who is hit', `${n.n} orders need action`)
        + tiles([
          { v: String(n.n), k: 'need action' },
          { v: String(d.hit.absorbed), k: 'absorbed' },
          { v: String(Math.max(0, d.hit.of - d.hit.orders)), k: 'not hit' },
          { v: chf(n.total), k: 'if nobody acts' },
        ])
        + costBars()
        + `<div class="ins-sec"><h3>Orders at risk</h3>${ordersTable((o) => o.branch !== 'absorbed')}</div>`;
      break;
    case 'keep': {
      const best = d.keep.options[0];
      const stay = (d.compare || []).find((r) => r.baseline);
      html = head('Keep the promised dates', `${n.n} of ${d.hit.need} orders can keep their date`)
        + tiles([
          { v: `${n.n}/${d.hit.need}`, k: 'keep their date' },
          { v: String(d.keep.options.length + (d.sources || []).length), k: 'ways that do' },
          d.keep.stay_best ? { v: 'CHF 0', k: 'best: stay as planned' } : { v: chf(best?.cost_chf), k: 'best way, extra' },
        ])
        + (d.keep.stay_best ? `<p class="ins-ok">Best: stay as planned. Every order is likely on time, and lateness
            would cost ${chf(stay?.exposure_chf)} in expectation, less than the cheapest way (${chf(best?.cost_chf)}).</p>`
          : closes(best?.closes_at, 'Best way closes in'))
        + `<div class="ins-sec">${compareTable(null)}</div>`
        + more('<p>A way is on this list only if every order it carries lands on its promised date and it still pays for itself. Ranked by on time, then how soon the freight moves, then cost.</p>'
          + (d.keep.more ? `<p>${d.keep.more} more ways were found and ranked lower.</p>` : ''));
      break;
    }
    case 'reduce':
      html = head('Cut the damage', `${n.n} orders will be late whatever we do`)
        + tiles([
          { v: String(n.n), k: 'orders' },
          { v: chf(d.reduce.options.reduce((s, o) => s + (o.saves_chf || 0), 0)), k: 'can be saved' },
          { v: chf(d.reduce.options.reduce((s, o) => s + (o.cost_chf || 0), 0)), k: 'costs' },
        ])
        + `<div class="ins-sec">${table([
          { k: '#', f: (r) => rankCell({ _rank: r._rank }) },
          { k: 'Action', f: (r) => esc(short(r.label, 34)) },
          { k: 'Orders', num: true, f: (r) => String(r.route ? r.orders_n : r.orders.length) },
          { k: 'Saves', num: true, f: (r) => (r.route ? '–' : num(r.saves_chf)) },
          { k: 'Costs', num: true, f: (r) => num(r.cost_chf) },
          { k: 'Closes in', num: true, f: (r) => DeadlineClock.html(r.closes_at) },
        ], n.kids.map((id, i) => ({ ...S.T.get(id).o, _rank: i + 1 })), { cap: 'Best net saving first' })}</div>`;
      break;
    case 'tell': {
      const rows = d.tell.orders;
      const likely = rows.filter((o) => o.p_late >= 0.5).length;
      const customers = new Set(rows.map((o) => o.customer)).size;
      const latest = Math.max(0, ...rows.map((o) => o.late_days));
      html = head('Tell the customer', likely
        ? `${plural(likely, 'order')} likely to miss the date`
        : `${plural(rows.length, 'order')} at risk, no way left to protect ${rows.length === 1 ? 'it' : 'them'}`)
        + tiles([
          { v: String(rows.length), k: ords(rows.length) },
          { v: String(customers), k: customers === 1 ? 'customer' : 'customers' },
          { v: pct(Math.max(0, ...rows.map((o) => o.p_late))), k: 'highest chance late' },
          latest >= 0.5 ? { v: days(latest), k: 'latest, if late' } : null,
        ])
        + `<div class="ins-sec">${ordersTable((o) => o.branch === 'told')}</div>`
        + more('<p>No way on the network lands these on time and still pays. Telling the customer early turns a missed date into an agreed one.</p>');
      break;
    }
    case 'way': {
      const o = n.o;
      const done = S.booked[o.id];
      html = head(n.rank === 1 ? 'Best way' : `${ORD[n.rank] || n.rank} way`, wayName(o),
        `<span class="ins-rank ins-rank--big" style="--r:var(--rank-${n.rank});--ri:var(--rank-${n.rank}-ink)">${n.rank}</span>`)
        + tiles([
          { v: `${o.on_time}/${o.orders}`, k: 'on time' },
          { v: chf(o.cost_chf), k: 'extra cost' },
          { v: hrs(o.starts_in_h), k: 'to start' },
        ])
        + closes(o.closes_at)
        + (done ? `<p class="ins-ok">Booked. ${esc(done.sentence)}</p>` : '')
        + pathChain(o.path)
        + `<div class="ins-sec">${compareTable(n.id)}</div>`
        + (o.carriers?.length ? carriersTable(o, n.id) : '')
        + more(`<p>${esc(o.detail)}</p>${o.capacity_note ? `<p>${esc(o.capacity_note)}</p>` : ''}
          <p>Closes ${esc(DeadlineClock.at(o.closes_at))}: after that, starting it misses a promised date.</p>
          <p>Orders: ${o.shipment_ids.map(esc).join(', ')}</p>`);
      break;
    }
    case 'src': {
      const s = n.s;
      html = head('Another Sika site', s.label)
        + tiles([
          { v: `${s.on_time}/${s.orders}`, k: 'on time' },
          { v: chf(s.value_chf), k: 'goods value moved' },
          { v: hrs(s.hours), k: 'to deliver' },
        ])
        + closes(s.closes_at)
        + `<div class="ins-sec"><h3>Via</h3><p class="ins-lead">${esc(s.via || '')}</p></div>`
        + `<div class="ins-sec">${compareTable(n.id)}</div>`
        + more('<p>The same destination served from a Sika site whose own route is calm. Assumes a 48 h handover; production capacity at the other site is for Procurement and Manufacturing to confirm.</p>'
          + `<p>Orders: ${s.shipment_ids.map(esc).join(', ')}</p>`);
      break;
    }
    case 'stay': {
      const r = n.r;
      const cheapest = Math.min(...(d.keep?.options || []).map((o) => o.cost_chf));
      html = head(n.best ? 'Best way' : 'Baseline', 'Stay as planned',
        n.best ? '<span class="ins-rank ins-rank--big" style="--r:var(--rank-1);--ri:var(--rank-1-ink)">1</span>' : '')
        + (n.best ? `<p class="ins-ok">Likely on time as it is, and ${chf(r.exposure_chf)} of expected lateness
            costs less than the cheapest way (${chf(cheapest)}). Keep watching.</p>` : '')
        + tiles([
          { v: `${r.on_time}/${r.orders}`, k: 'on time' },
          { v: days(r.late_after_days), k: 'worst lateness' },
          { v: chf(r.exposure_chf), k: 'at risk' },
        ])
        + pathChain(r.path)
        + costBars()
        + `<div class="ins-sec">${compareTable('stay')}</div>`;
      break;
    }
    case 'red': {
      const o = n.o;
      html = head(o.route ? 'Faster, still late' : 'Cuts the damage', o.label)
        + tiles(o.route ? [
          { v: days(o.late_after_days), k: 'still late' },
          { v: chf(o.cost_chf), k: 'extra cost' },
          { v: String(o.orders_n), k: 'orders' },
        ] : [
          { v: chf(o.saves_chf), k: 'saved' },
          { v: chf(o.cost_chf), k: 'costs' },
          { v: String(o.orders.length), k: 'orders' },
        ])
        + closes(o.closes_at)
        + (o.route ? pathChain(o.path) : `<div class="ins-sec">${table([
          { k: 'Order', f: (r) => `<span class="ins-id">${esc(r.shipment_id)}</span>` },
          { k: 'Customer', f: (r) => `${esc(short(r.customer, 26))}${r.priority === 'A' ? ' <span class="ins-a">A</span>' : ''}` },
          { k: 'Closes in', num: true, f: (r) => (r.clock_h == null ? '–'
            : DeadlineClock.html(new Date(Date.parse(d.as_of) + r.clock_h * 3.6e6).toISOString())) },
        ], o.orders || [])}</div>`)
        + more(`<p>${esc(o.detail || '')}</p>${o.owner ? `<p>Owner: ${esc(o.owner)}</p>` : ''}`);
      break;
    }
    case 'cust': {
      const c = n.c;
      html = head(tierOf(c.customer) === 'A' ? 'Key account' : 'Customer', c.customer)
        + tiles([
          { v: String(c.orders.length), k: c.orders.length === 1 ? 'order at risk' : 'orders at risk' },
          { v: pct(c.p), k: 'chance late' },
          { v: days(c.late), k: 'expected late' },
        ])
        + draft(c.customer, c.orders);
      break;
    }
    case 'car': {
      const way = S.T.get(n.way);
      const c = carrierFor(n.way, n.mode);
      html = head(`${MODE[n.mode] || n.mode} carrier`, c ? plain(c.name) : 'None listed')
        + tiles(c ? [
          { v: esc(c.capacity || 'ask'), k: 'free capacity' },
          { v: `${num(c.km)} km`, k: 'from the route' },
          { v: esc(c.channel || 'call'), k: 'how to book' },
        ] : [])
        + (c ? `<div class="ins-sec"><h3>Contact</h3><p class="ins-lead">${contact(c)}</p>
            ${c.note ? `<p class="ins-note">${esc(c.note)}</p>` : ''}
            ${c.synthetic ? '<p class="ins-note">An example partner: the name and number are made up.</p>' : ''}</div>` : '')
        + carriersTable(way.o, n.way);
      break;
    }
    case 'ask': {
      const p = d.approval?.procurement;
      const src = S.T.get(n.src).s;
      html = head('Who arranges it', p?.name || 'Procurement')
        + tiles([{ v: String(src.orders), k: 'orders' }, { v: chf(src.value_chf), k: 'goods value' }])
        + `<div class="ins-sec"><h3>Contact</h3><p class="ins-lead">${contact(p)}</p>
          <p><a class="ctl ctl--primary" href="mailto:${esc(p?.email || '')}?subject=${encodeURIComponent(`Ship ${src.orders} orders from ${src.label}`)}&body=${encodeURIComponent(`${src.label}: ${src.orders} orders (${src.shipment_ids.join(', ')}), ${src.on_time}/${src.orders} on time via ${src.via}. Can we switch the source?`)}">Email Procurement</a></p></div>`;
      break;
    }
    case 'sign': {
      const sign = signOff(n.x.cost);
      const a = d.approval || {};
      html = head('Sign-off', sign.ok ? 'Within your limit' : `Above your limit: ${sign.who} signs`)
        + tiles([
          { v: chf(n.x.cost), k: 'this costs' },
          { v: chf(a.base_limit_chf ?? sign.limit), k: 'your limit' },
          a.crisis_limit_chf ? { v: chf(a.crisis_limit_chf), k: a.crisis ? 'crisis limit, active' : 'crisis limit' } : null,
        ])
        + (sign.ok ? '<p class="ins-ok">You can book it yourself.</p>'
          : `<div class="ins-sec"><h3>Ask</h3><p class="ins-lead">${esc(sign.person?.name || 'Controlling')}<br>${contact(sign.person)}</p></div>`);
      break;
    }
    case 'book':
      html = bookPanel(n);
      break;
    case 'told': {
      const way = S.T.get(n.parent);
      const ids = way.o.route ? way.o.shipment_ids : (way.o.orders || []).map((o) => o.shipment_id);
      const rows = (d.orders || []).filter((o) => ids.includes(o.shipment_id));
      html = head('Tell customers', `${rows.length} orders still late`)
        + draft(null, rows);
      break;
    }
    default:
      html = head('', n.id);
  }
  host.innerHTML = html;
  host.scrollTop = 0;
}

function draft(customer, orders) {
  const likely = orders.some((o) => (o.p_late ?? 1) >= 0.5);
  const list = orders.map((o) => (o.late_days >= 0.5 ? `${o.shipment_id} (about ${days(o.late_days)})` : o.shipment_id)).join(', ');
  const who = customer || 'customer';
  const text = `Dear ${who},\n\nA disruption on the route ${likely ? 'is delaying' : 'may delay'} your order`
    + `${orders.length === 1 ? '' : 's'} ${list}. `
    + (likely ? 'We are working on it and will confirm a new delivery date within 24 hours.'
      : 'We are watching it closely and will confirm the delivery date within 24 hours.')
    + '\n\nKind regards,\nSika Supply Chain';
  return `<div class="ins-sec"><h3>Message</h3><textarea class="ins-draft" rows="7" readonly>${esc(text)}</textarea>
    <p class="ins-actions"><button type="button" class="ctl ctl--ghost" id="copy-draft">Copy</button>
    <a class="ctl ctl--primary" href="mailto:?subject=${encodeURIComponent('Delivery update')}&body=${encodeURIComponent(text)}">Email</a></p></div>`;
}

function bookPanel(n) {
  const x = n.x;
  const parent = S.T.get(n.parent);
  const sign = signOff(x.cost);
  const done = S.booked[x.act];
  const carriers = parent.kind === 'way'
    ? [...new Set((parent.o.carriers || []).flatMap((c) => c.modes))].filter((m) => (parent.o.modes || []).includes(m))
      .map((m) => ({ mode: m, c: carrierFor(parent.id, m) })).filter((r) => r.c)
    : [];
  return head(done ? 'Booked' : 'Book it', x.label)
    + tiles([
      { v: String(x.orders), k: 'orders' },
      { v: chf(x.cost), k: 'cost' },
      { v: sign.ok ? '✓' : '!', k: sign.ok ? 'within limit' : `${sign.who} signs` },
    ])
    + (carriers.length ? `<div class="ins-sec"><h3>Carriers</h3>${table([
      { k: 'Mode', f: (r) => esc(MODE[r.mode] || r.mode) },
      { k: 'Carrier', f: (r) => esc(plain(r.c.name)) },
      { k: 'Contact', f: (r) => contact(r.c) },
    ], carriers)}</div>` : '')
    + (done
      ? `<p class="ins-ok">${esc(done.sentence)}</p>
         <p class="ins-actions"><button type="button" class="ctl ctl--ghost" id="undo-btn" data-act="${esc(x.act)}">Undo</button></p>`
      : `<p class="ins-actions">
          ${sign.ok ? '' : `<a class="ctl ctl--ghost" href="mailto:${esc(sign.person?.email || '')}?subject=${encodeURIComponent(`Sign-off: ${x.label}`)}&body=${encodeURIComponent(`${x.label}: ${x.orders} orders, ${chf(x.cost)}. Above the ${chf(sign.limit)} limit.`)}">Ask ${esc(sign.who)}</a>`}
          <button type="button" class="ctl ctl--primary" id="book-btn" data-act="${esc(x.act)}">${sign.ok ? 'Book it now' : 'Book it (signed off)'}</button>
        </p>
        <p class="ins-note">Runs at once on every order it covers. Undo within 15 minutes.</p>`);
}

// ================================================================ panel clicks
$('tr-side').addEventListener('click', async (e) => {
  const t = e.target;
  if (t.closest('.ins-more')) {
    S.more = !S.more;
    side(S.T.get(S.focus));
    return;
  }
  if (t.id === 'copy-draft') {
    const text = $('tr-side').querySelector('.ins-draft')?.value || '';
    try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Select the text and copy it'); }
    return;
  }
  if (t.id === 'pen-toggle') {
    const on = !S.d.cost?.penalties_counted;
    t.disabled = true;
    try {
      await fetch('/api/penalties', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: on }) });
      await load({ keepPath: true });
      toast(on ? 'Delay penalties counted' : 'Delay penalties left out');
    } catch { toast('Could not switch penalties'); t.disabled = false; }
    return;
  }
  if (t.id === 'book-btn') { book(t.dataset.act, t); return; }
  if (t.id === 'undo-btn') { undo(t.dataset.act, t); }
});

$('tr-side').addEventListener('change', (e) => {
  const r = e.target.closest('.car-pick');
  if (!r) return;
  S.carrier[`${r.dataset.way}|${r.dataset.mode}`] = Number(r.dataset.i);
  render(99, false);
  side(S.T.get(S.focus));
});

async function book(act, btn) {
  btn.disabled = true;
  try {
    const res = await fetch(`/api/v2/act?${qs()}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ route_id: S.d.route_id, option_id: act }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.ok) {
      toast(body.sentence || body.detail || 'Nothing ran');
      btn.disabled = false;
      return;
    }
    S.booked[act] = { ids: body.executed.map((x) => x.execution_id), sentence: body.sentence };
    toast(body.sentence);
    render(99, false);
    side(S.T.get(S.focus));
  } catch {
    toast('Could not reach the server');
    btn.disabled = false;
  }
}

async function undo(act, btn) {
  const done = S.booked[act];
  if (!done) return;
  btn.disabled = true;
  try {
    const res = await fetch(`/api/v2/undo?${qs()}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ execution_ids: done.ids }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.ok) {
      toast(body.refused?.[0]?.detail || 'Undo refused');
      btn.disabled = false;
      return;
    }
    delete S.booked[act];
    toast('Undone');
    render(99, false);
    side(S.T.get(S.focus));
  } catch {
    toast('Could not reach the server');
    btn.disabled = false;
  }
}

let toastTimer = null;
function toast(text) {
  const el = $('tr-toast');
  el.textContent = text;
  el.hidden = false;
  el.classList.add('is-on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.remove('is-on'); setTimeout(() => { el.hidden = true; }, 250); }, 3200);
}

// ================================================================ header
function header(d) {
  document.title = `Decision tree · ${d.route?.name || d.route_id}`;
  const chip = $('tr-chip');
  chip.className = `level-chip level-${d.route?.level || 'green'}`;
  chip.textContent = d.route?.level_label || '';
  $('tr-route').textContent = d.route?.name || d.route_id;
  const stuck = !d.decide_at && ['red', 'yellow'].includes(d.route?.level) && (d.tell?.orders || []).length;
  $('tr-decide').innerHTML = d.decide_at
    ? `<span class="tr-decide-k" title="The route's deadline: when its first option closes">First option closes in</span> ${DeadlineClock.html(d.decide_at)}
       <span class="tr-decide-at">${esc(DeadlineClock.at(d.decide_at))}</span>`
    : stuck ? '<span class="tr-decide-k">No option left: tell the customer now</span>'
      : '<span class="tr-decide-k">No decision deadline</span>';
  // The route line doubles as the route picker when there is more than one.
  const pick = $('tr-pick');
  const others = d.others || [];
  pick.innerHTML = others.map((r) => `<option value="${esc(r.route_id)}"${r.route_id === d.route_id ? ' selected' : ''}>
    ${esc(short(r.name, 70))} · ${esc(r.level_label || '')}, ${r.at_risk} at risk</option>`).join('');
  const many = others.length > 1 && others.some((r) => r.route_id === d.route_id);
  $('tr-pick-wrap').hidden = !many;
  $('tr-route').hidden = many;
  const back = new URLSearchParams(qs());
  back.set('route', d.route_id);
  $('tr-back').href = `/?${back}`;
  caseControl(d);
}

/* CLOSE THE CASE, here where the work is finished: one click on how it
 * ended, and it goes to the risk ledger's history. */
function caseControl(d) {
  const host = $('tr-case');
  if (!host) return;
  const c = d.case || { status: 'open' };
  if (c.status === 'closed') {
    host.innerHTML = `<span class="case-done" title="${esc(`${c.closed_by} · ${new Date(c.closed_at).toLocaleString()}`)}">✓ Closed · ${esc(c.outcome_label)}</span>
      <button type="button" class="ctl ctl--ghost" id="tr-case-reopen">Reopen</button>`;
    $('tr-case-reopen').addEventListener('click', () => caseCall(`/api/cases/${encodeURIComponent(c.case_id)}/reopen`, {}));
    return;
  }
  if ((d.route?.level || 'green') === 'green') { host.innerHTML = ''; return; }
  const outcomes = Object.entries(d.case_outcomes || {});
  host.innerHTML = `<button type="button" class="ctl" id="tr-case-close" aria-expanded="false">✓ Close case</button>
    <div class="case-pick case-pick--pop" id="tr-case-pick" hidden>
      ${outcomes.map(([id, label]) => `<button type="button" class="case-opt" data-outcome="${esc(id)}">${esc(label)}</button>`).join('')}
      <input type="text" id="tr-case-note" maxlength="500" placeholder="Note (optional)" aria-label="Note">
    </div>`;
  $('tr-case-close').addEventListener('click', () => {
    const pick = $('tr-case-pick');
    pick.hidden = !pick.hidden;
    $('tr-case-close').setAttribute('aria-expanded', String(!pick.hidden));
  });
  host.querySelectorAll('.case-opt').forEach((b) => b.addEventListener('click', () =>
    caseCall(`/api/cases/${encodeURIComponent(d.route_id)}/close`,
      { outcome: b.dataset.outcome, note: $('tr-case-note').value, actor: 'planner' })));
}

async function caseCall(url, body) {
  const host = $('tr-case');
  host.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  try {
    const res = await fetch(`${url}?${qs()}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out.detail || String(res.status));
    toast(url.endsWith('/close') ? 'Case closed: it is in the risk ledger history' : 'Case reopened');
    await load({ keepPath: true });
  } catch (err) {
    host.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    toast(`Could not: ${err.message}`);
  }
}

$('tr-pick').addEventListener('change', (e) => {
  location.href = `/tree?${qs({ route: e.target.value })}`;
});

$('tr-reset').addEventListener('click', () => {
  S.path = ['root'];
  S.focus = 'root';
  clearLevels();
  render(0);
  side(S.T.get('root'));
  $('tr-stage').scrollTo({ top: 0, behavior: 'smooth' });
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { fit(); wires(99); }, 80);
});
document.addEventListener('themechange', () => wires(99));

// ================================================================ load
/* The recommended path, grown one level at a time so the tree visibly
 * grows: the disruption, who is hit, the branch the engines recommend, and
 * the ranked ways under it. ?opt= opens one way directly. */
function initialPath(d) {
  const T = S.T;
  const branch = d.keep?.kept?.length ? 'keep' : d.reduce?.orders?.length ? 'reduce'
    : d.tell?.orders?.length ? 'tell' : null;
  if (!d.hit?.need || !branch) return { path: ['root'], focus: 'root' };
  const want = PRESELECT.startsWith('act:') ? PRESELECT.slice(4) : PRESELECT;
  const pre = want && [...T.keys()].find((id) => id === `way:${want}` || id === `red:${want}`);
  if (pre) {
    // The branch the option lives under, not the recommended one.
    return { path: ['root', 'need', pre.startsWith('red:') ? 'reduce' : 'keep', pre], focus: pre };
  }
  return { path: ['root', 'need', branch], focus: branch };
}

async function load({ keepPath = false } = {}) {
  if (!ROUTE) {
    $('tr-side').innerHTML = head('No route', 'Open this from a route on the board');
    return;
  }
  let d;
  try {
    const res = await fetch(`/api/decision/${encodeURIComponent(ROUTE)}?${qs()}`);
    if (!res.ok) throw new Error(String(res.status));
    d = await res.json();
  } catch (err) {
    $('tr-side').innerHTML = head('Could not load', `The tree for ${ROUTE} did not load (${esc(err.message)}).`);
    return;
  }
  const first = !S.d;
  S.d = d;
  S.T = build(d);
  if (first) DeadlineClock.start(d.as_of);
  header(d);
  if (keepPath && S.path.every((id) => S.T.has(id))) {
    render(0, false);
    side(S.T.get(S.focus) || S.T.get('root'));
    return;
  }
  const { path, focus } = initialPath(d);
  S.path = [];
  S.focus = 'root';
  clearLevels();
  render(0);
  side(S.T.get('root'));
  // Grow the recommended path, a level at a time.
  path.forEach((id, i) => {
    setTimeout(() => {
      S.path = path.slice(0, i + 1);
      S.focus = i === path.length - 1 ? focus : id;
      render(i + 1);
      if (i === path.length - 1) side(S.T.get(focus));
    }, 260 * (i + 1));
  });
}

load();
