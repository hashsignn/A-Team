/* The Action decision tree, per shipment.
 *
 * Every shipment on a disrupted route has its own way out: a truck still at
 * the plant can switch to rail, a barge already on the Rhine cannot; a key
 * account's line stops if its order is late, a DIY chain takes a note. So
 * the tree opens on the route and each order grows its own branch:
 *
 *    What is happening      the events on the route
 *    Who is hit             the customers, key accounts first
 *    Which shipment         that customer's orders, the most at stake first
 *    Which way, best first  the order's recovery routes, ranked on time, cost
 *                           and risk: arrival, extra cost, CO2e, risk; staying
 *                           on the plan in the same pool; another Sika site
 *    Who carries it         the partners along the way who can take it
 *    Sign off and book      the limit, then book it, with undo
 *
 * The ways are the fleet map's recovery routes (engine/fleet/reroute.py),
 * the same numbers as the Action Hub's "Plan recovery", so the two never
 * disagree about a shipment. Route data: /api/decision/{route}?ways=1, with
 * each order's ways in one line for its box; one shipment:
 * /api/decision/shipment/{id}, fetched when its box is opened.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (v) => (v == null || !Number.isFinite(+v) ? '–' : Math.round(+v).toLocaleString('en-US'));
const chf = (v) => (v == null ? '–' : `CHF ${num(v)}`);
const signed = (v) => (v == null ? '–' : `${v >= 0 ? '+' : '−'}CHF ${num(Math.abs(v))}`);
const pct = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`);
const hrs = (h) => (h == null ? '–' : h < 1 ? '<1 h' : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`);
const days = (d) => (d == null ? '–' : d < 0.05 ? '0 d' : `${d < 10 ? (+d).toFixed(1) : Math.round(d)} d`);
const ORD = ['', '1st', '2nd', '3rd', '4th', '5th', '6th'];
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;
const ords = (n) => (n === 1 ? 'order' : 'orders');
const MODE = { road: 'Road', rail: 'Rail', sea: 'Sea', barge: 'Barge', air: 'Air' };
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const plain = (name) => String(name || '').replace(/\s*\(synthetic\)\s*$/i, '');
const short = (s, n) => { const t = String(s || ''); return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t; };
const dateShort = (iso) => { const d = new Date(iso); return Number.isFinite(+d) ? `${d.getUTCDate()} ${MON[d.getUTCMonth()]}` : '–'; };
const rankVar = (r) => `var(--alt-${Math.max(1, Math.min(4, r || 4))})`;
const IMPACT_WORD = { line_down: 'Line stops', stock_out: 'Runs out', inconvenience: 'Minor' };

const Q = new URLSearchParams(location.search);
const META_AS_OF = document.querySelector('meta[name="radar-default-as-of"]')?.content || '';
let ROUTE = Q.get('route') || '';
const SHIP = Q.get('ship') || '';
const AS_OF = Q.get('as_of') || (META_AS_OF.startsWith('__') ? '' : META_AS_OF);
const SHIPMENTS = Q.get('shipments') || '';

/* Time, cost and risk: the Action Hub's weights, as four presets. */
const WEIGHTS = {
  balanced: { label: 'Balanced', time: 50, cost: 30, risk: 20 },
  fastest: { label: 'Fastest', time: 100, cost: 0, risk: 0 },
  cheapest: { label: 'Cheapest', time: 0, cost: 100, risk: 0 },
  safest: { label: 'Safest', time: 0, cost: 0, risk: 100 },
};

function qs(extra = {}) {
  const q = new URLSearchParams();
  if (AS_OF) q.set('as_of', AS_OF);
  if (SHIPMENTS) q.set('shipments', SHIPMENTS);
  Object.entries(extra).forEach(([k, v]) => v != null && q.set(k, v));
  return q.toString();
}

const S = {
  d: null,            // the route payload
  T: new Map(),       // node id -> node
  path: [],           // the chosen node at each level
  focus: null,        // the node whose numbers the panel shows
  ship: new Map(),    // `${sid}|${weights}` -> { data } | { error } | { loading }
  weights: 'balanced',
  booked: {},         // `${sid}|${option}` -> { ids, sentence }
  more: false,
};

// ================================================================ glyphs
/* One small drawn icon per mode: an emoji renders differently everywhere. */
const GLYPH = {
  road: '<rect x="1" y="5" width="12" height="8" rx="1.5"/><path d="M13 8h4l3 3v2h-7z"/><circle cx="5" cy="15" r="1.9"/><circle cx="16" cy="15" r="1.9"/>',
  rail: '<rect x="4" y="2" width="14" height="11" rx="2.5"/><path d="M4 8h14"/><circle cx="8" cy="15.5" r="1.5"/><circle cx="14" cy="15.5" r="1.5"/><path d="M3 18.5h16"/>',
  barge: '<path d="M1.5 12h19l-2.5 5h-14z"/><rect x="6" y="6" width="10" height="5" rx="1"/>',
  sea: '<path d="M1.5 12h19l-2.5 5h-14z"/><rect x="5" y="5" width="4" height="6"/><rect x="10" y="3" width="4" height="8"/><rect x="15" y="7" width="3" height="4"/>',
  air: '<path d="M2 11l18-7-5 7 5 7-18-7z"/>',
};
const modeIcon = (mode, colour) => `<svg class="mi" viewBox="0 0 22 20" aria-hidden="true"${colour ? ` style="color:${colour}"` : ''}>${GLYPH[mode] || GLYPH.road}</svg>`;

/* A way as its modes: the changed legs in the way's colour, the planned
 * ones grey, so "switch the barge to rail" reads as one coloured icon. */
function chainHTML(chain, colour) {
  if (!chain?.length) return '';
  return `<span class="tn-chain" title="${esc(chain.map((c) => `${MODE[c.mode] || c.mode}${c.new ? ' (new)' : ''} ${c.from} → ${c.to}, ${num(c.km)} km`).join('\n'))}">${
    chain.map((c) => `<span class="tn-leg${c.new ? ' is-new' : ''}">${modeIcon(c.mode, c.new ? colour : '')}</span>`).join('<i class="tn-arrow">→</i>')}</span>`;
}

const REACH_ICON = {
  phone: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6.2 2.8l2 3.6-1.5 1.5a10 10 0 0 0 5.4 5.4l1.5-1.5 3.6 2-1 3a2 2 0 0 1-2.1 1.3A15 15 0 0 1 1.9 5.9a2 2 0 0 1 1.3-2.1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  email: '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="4.5" width="15" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3 5.5l7 5.5 7-5.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  web: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M2.8 10h14.4M10 2.8c2 2.1 3 4.5 3 7.2s-1 5.1-3 7.2c-2-2.1-3-4.5-3-7.2s1-5.1 3-7.2z" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>',
};
/* Email, phone and website as icons; the address is the tooltip. */
function contact(r) {
  if (!r) return '';
  const bits = [];
  if (r.email) bits.push(`<a class="reach" href="mailto:${esc(r.email)}" title="${esc(r.email)}" aria-label="Email">${REACH_ICON.email}</a>`);
  if (r.phone) bits.push(`<a class="reach" href="tel:${esc(r.phone.replace(/\s+/g, ''))}" title="${esc(r.phone)}" aria-label="Call">${REACH_ICON.phone}</a>`);
  if (r.portal) bits.push(`<a class="reach" href="${esc(r.portal)}" target="_blank" rel="noopener" title="${esc(r.portal)}" aria-label="Website">${REACH_ICON.web}</a>`);
  return bits.length ? `<span class="reach-row">${bits.join('')}</span>` : '<span class="muted">—</span>';
}

// ================================================================ data
function shipEntry(sid) { return S.ship.get(`${sid}|${S.weights}`); }
function shipData(sid) { return shipEntry(sid)?.data || null; }

function ensureShip(sid) {
  const key = `${sid}|${S.weights}`;
  if (S.ship.has(key)) return;
  const w = WEIGHTS[S.weights];
  S.ship.set(key, { loading: true });
  fetch(`/api/decision/shipment/${encodeURIComponent(sid)}?${qs({ w_time: w.time, w_cost: w.cost, w_risk: w.risk })}`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`the server answered ${r.status}`))))
    .then((data) => { S.ship.set(key, { data }); })
    .catch((err) => { S.ship.set(key, { error: err.message }); })
    .finally(() => { if (key === `${sid}|${S.weights}`) shipArrived(sid); });
}

/* The shipment's ways arrived: grow them under its box if it is still the
 * one chosen, and fill the panel if it is the one shown. */
function shipArrived(sid) {
  const level = S.path.indexOf(`ship:${sid}`);
  if (level >= 0) render(level + 1);
  const n = S.T.get(S.focus);
  if (n && (n.sid === sid || (n.kind === 'cust' && n.c.orders.some((o) => o.shipment_id === sid)))) side(n);
}

// ================================================================ the tree
function add(n) { S.T.set(n.id, n); return n.id; }

/* A shipment's own ways in one line, from the same engine as its branch:
 * the box never promises a way its branch does not show. */
function waysOf(sid) { return S.d?.ways?.[sid] || null; }
/* The three-colour status, from the engine when it has one, else from how late. */
function statusOf(level, label, lateDays) {
  const lv = level || (lateDays >= 1 ? 'red' : lateDays > 0.1 ? 'yellow' : 'green');
  return { level: lv, label: label || (lv === 'red' ? 'Major disruption' : lv === 'yellow' ? 'Minor disruption' : 'On schedule') };
}
function waysTag(v) {
  if (!v) return null;
  if (v.located === false) return { ok: false, text: 'no live position' };
  if (v.on_time) return { ok: true, text: `${plural(v.on_time, 'way')} on time` };
  if (v.plan_on_time) return { ok: true, text: 'on time as planned' };
  if (v.ways) return { ok: false, text: `${plural(v.ways, 'way')}, all late` };
  return { ok: false, text: 'no other route' };
}

/* The route's levels: the events, the customers, their shipments. */
function build(d) {
  S.T = new Map();
  const orders = (d.orders || []).filter((o) => o.branch !== 'absorbed');
  const byCustomer = new Map();
  orders.forEach((o) => {
    const c = byCustomer.get(o.customer) || { name: o.customer, tier: o.tier || 'B', orders: [], loss: 0 };
    c.orders.push(o);
    c.loss += o.loss_chf || 0;
    byCustomer.set(o.customer, c);
  });
  const rank = { A: 0, B: 1, C: 2 };
  const customers = [...byCustomer.values()].sort((a, b) => (rank[a.tier] ?? 1) - (rank[b.tier] ?? 1) || b.loss - a.loss);
  const custIds = customers.map((c) => {
    c.orders.sort((a, b) => (b.loss_chf || 0) - (a.loss_chf || 0));
    const kids = c.orders.map((o) => add({ id: `ship:${o.shipment_id}`, kind: 'ship', sid: o.shipment_id, row: o,
      customer: c.name, ask: 'Which way? Best first' }));
    return add({ id: `cust:${c.name}`, kind: 'cust', c, ask: 'Which shipment', kids });
  });
  const absorbed = (d.orders || []).length - orders.length;
  const notHit = Math.max(0, (d.hit?.of || 0) - (d.hit?.orders || 0));
  const calm = add({ id: 'calm', kind: 'calm', absorbed, notHit, disabled: !(absorbed + notHit) });
  add({ id: 'root', kind: 'root', ask: 'Who is hit', kids: [...custIds, calm] });
}

/* One shipment's ways, then who carries each, then sign-off and book. */
function shipKids(sid, data) {
  const ids = [];
  const pool = [...(data.options || []), ...(data.stay ? [data.stay] : [])].sort((a, b) => a.rank - b.rank);
  pool.forEach((o) => {
    if (o.plan) {
      const tell = !o.on_time ? [add({ id: `told:${sid}:plan`, kind: 'told', sid, o, data })] : [];
      ids.push(add({ id: `stay:${sid}`, kind: 'stay', sid, o, data, ask: 'Tell the customer', kids: tell }));
      return;
    }
    const x = { sid, o, data };
    const fin = [
      add({ id: `sign:${sid}:${o.id}`, kind: 'sign', ...x }),
      add({ id: `book:${sid}:${o.id}`, kind: 'book', ...x }),
      ...(!o.on_time ? [add({ id: `told:${sid}:${o.id}`, kind: 'told', ...x })] : []),
    ];
    const carriers = (data.partners || []).filter((p) => p.serves.includes(o.id)).slice(0, 4)
      .map((p) => add({ id: `par:${sid}:${o.id}:${p.id}`, kind: 'par', ...x, p, ask: 'Sign off and book', kids: fin }));
    ids.push(add({ id: `opt:${sid}:${o.id}`, kind: 'opt', ...x, ask: carriers.length ? 'Who carries it' : 'Sign off and book',
      kids: carriers.length ? carriers : fin }));
  });
  (data.sources || []).forEach((s, i) => {
    const askId = add({ id: `ask:${sid}:${i}`, kind: 'ask', sid, s, data });
    ids.push(add({ id: `src:${sid}:${i}`, kind: 'src', sid, s, data, ask: 'Who arranges it', kids: [askId] }));
  });
  if (!pool.length) {
    // No position on the map, so no way to draw: what is left is the message.
    ids.push(add({ id: `told:${sid}:plan`, kind: 'told', sid, o: { id: 'plan', late_days: data.order.late_days }, data }));
  }
  return ids;
}

function kidsOf(n) {
  if (n.kind === 'ship') {
    const entry = shipEntry(n.sid);
    if (!entry) ensureShip(n.sid);
    if (!entry || entry.loading) return [add({ id: `wait:${n.sid}`, kind: 'wait', sid: n.sid })];
    if (entry.error) return [add({ id: `fail:${n.sid}`, kind: 'fail', sid: n.sid, error: entry.error })];
    return shipKids(n.sid, entry.data);
  }
  return n.kids || [];
}

// ================================================================ the boxes
const RISK_TONE = { Low: 'low', Medium: 'mid', High: 'high' };

function face(n) {
  const d = S.d;
  switch (n.kind) {
    case 'root': {
      const ev = d.happening || [];
      const first = d.clocks?.[0]?.starts_at;
      return { kicker: ev[0]?.kind || 'Now', title: short(ev[0]?.title || 'No event on this route', 54),
        big: String(ev.length), unit: ev.length === 1 ? 'event' : 'events',
        sub: first ? `since ${DeadlineClock.at(first).replace(' UTC', '')}` : '',
        more: ev.length > 1 ? `+ ${short(ev[1].title, 40)}` : '', wide: true, tone: 'event' };
    }
    case 'calm': return { kicker: 'Fine', big: String(n.absorbed + n.notHit), unit: ords(n.absorbed + n.notHit),
      sub: `${n.absorbed} absorbed · ${n.notHit} not hit`, tone: 'leaf' };
    case 'cust': {
      const c = n.c;
      const key = c.tier === 'A';
      const words = { A: 'Key account', B: 'Standard', C: 'Flexible' };
      return { kicker: `${key ? '★ ' : ''}${words[c.tier] || 'Customer'}`, title: c.name,
        big: String(c.orders.length), unit: `${ords(c.orders.length)} at risk`, sub: chf(c.loss), tone: key ? 'key' : 'cust' };
    }
    case 'ship': {
      const r = n.row;
      const v = waysOf(n.sid);
      const data = shipData(n.sid);
      const st = data?.order.status || {};
      const lateDays = v?.late_days ?? r.late_days;
      const { level, label } = statusOf(st.level || v?.level, st.label || v?.label, lateDays);
      const late = lateDays >= 0.5;
      const tag = waysTag(v);
      return { dot: level, kicker: label,
        title: r.shipment_id, icon: data?.order.vehicle?.mode || v?.mode,
        big: late ? `+${days(lateDays)}` : pct(r.p_late), unit: late ? 'late' : 'chance late',
        sub: `${chf(r.loss_chf)} at risk`, sub2: tag ? `${tag.ok ? '✓' : '✗'} ${tag.text}` : '',
        sub2Tone: tag ? (tag.ok ? 'ok' : 'late') : '', tone: 'ship' };
    }
    case 'wait': return { kicker: 'Working out', title: `the ways for ${n.sid}`, big: '…', unit: '', tone: 'wait' };
    case 'fail': return { kicker: 'Could not load', title: n.sid, big: '!', unit: '', sub: short(n.error, 40), tone: 'leaf' };
    case 'opt': {
      const o = n.o;
      return { rank: o.rank, kicker: o.best ? 'Best' : ORD[o.rank] || `#${o.rank}`, title: o.label,
        chain: chainHTML(o.chain, rankVar(o.rank)), risk: o.risk_label,
        big: dateShort(o.eta), mark: o.on_time ? 'ok' : 'late',
        sub: o.extra_chf > 0.5 ? signed(o.extra_chf) : 'no extra cost', sub2: `${o.lowest_co2 ? '🌿 ' : ''}${o.co2e_t} t CO₂e`,
        clock: o.on_time ? o.closes_at : undefined, tone: 'opt' };
    }
    case 'stay': {
      const o = n.o;
      // Best among ways, not "best" when it is the only one.
      const best = o.best && n.data.options.length > 0;
      return { rank: best ? 1 : null, kicker: best ? 'Best' : n.data.options.length ? 'The plan' : 'No other route', title: 'Stay on the plan',
        chain: chainHTML(o.chain, ''), risk: o.risk_label, big: dateShort(o.eta), mark: o.on_time ? 'ok' : 'late',
        sub: `${o.on_time ? 'on time' : `${days(o.late_days)} late`} · ${o.co2e_t} t CO₂e`, tone: best ? 'opt' : 'base' };
    }
    case 'src': {
      const s = n.s;
      return { kicker: 'Other Sika site', title: s.label, big: s.on_time_here ? 'on time' : 'late', mark: s.on_time_here ? 'ok' : 'late',
        sub: `goods ${chf(s.value_chf)}`, clock: s.on_time_here ? s.closes_at : undefined, tone: 'site' };
    }
    case 'par': {
      const p = n.p;
      const cap = capacity(p.capacity);
      return { kicker: p.kind ? p.kind.replace(/_/g, ' ') : 'Partner', icons: p.modes, title: plain(p.name),
        big: cap.big || 'on request', unit: '',
        sub: `${num(p.km)} km${p.needs_adr ? ` · ADR ${p.adr === true ? '✓' : p.adr === false ? '✗' : '?'}` : ''}`,
        tag: p.synthetic ? 'example' : 'real', tone: p.synthetic ? 'par' : 'par real' };
    }
    case 'ask': {
      const who = d.approval?.procurement;
      return { kicker: 'Procurement', title: who?.name || 'Procurement', big: '1', unit: 'call', tone: 'act' };
    }
    case 'sign': {
      const sign = signOff(n.o.extra_chf);
      return { kicker: 'Sign-off', title: sign.ok ? 'You approve' : 'Controlling signs', big: chf(Math.max(0, n.o.extra_chf)), unit: '',
        sub: `limit ${chf(sign.limit)}${sign.ok ? ' ✓' : ''}`, tone: sign.ok ? 'ok' : 'sign' };
    }
    case 'book': {
      const done = S.booked[`${n.sid}|${n.o.id}`];
      return { kicker: done ? 'Booked' : 'Book', title: done ? 'Running' : 'Book it', big: n.sid, unit: '',
        sub: done ? 'undo possible' : 'undo within 15 min', tone: 'act' };
    }
    case 'told': return { kicker: 'Tell', title: 'Tell the customer', big: n.o.late_days >= 0.5 ? `+${days(n.o.late_days)}` : pct(n.data.order.p_late),
      unit: n.o.late_days >= 0.5 ? 'late' : 'chance late', sub: 'new date, one message', tone: 'tell' };
    default: return { title: n.id };
  }
}

/* "140 TEU on a Cape-routed service" is a number and a note: the number
 * goes big, the note goes under it. */
function capacity(text) {
  const t = String(text || '').trim();
  const m = t.match(/^([\d.,]+\s*[A-Za-z]+)\s*(.*)$/);
  return m ? { big: m[1], rest: m[2] } : { big: t, rest: '' };
}

function signOff(cost) {
  const a = S.d.approval || {};
  const limit = a.limit_chf ?? 10000;
  return { ok: (cost || 0) <= limit, limit, crisis: a.crisis, person: a.controlling };
}

function nodeHTML(n, i, level) {
  const f = face(n);
  const onPath = S.path[level] === n.id;
  const cls = ['tn', `tn--${n.kind}`, ...(f.tone ? f.tone.split(' ').map((t) => `tn--${t}`) : [])];
  if (f.wide) cls.push('tn--wide');
  if (onPath) cls.push('is-on');
  if (S.focus === n.id) cls.push('is-focus');
  if (n.disabled) cls.push('is-off');
  if (S.path[level] && !onPath) cls.push('is-dim');
  const colour = f.rank ? rankVar(f.rank) : '';
  const style = `--i:${i}${colour ? `;--r:${colour}` : ''}`;
  const rank = f.rank ? `<span class="tn-rank">${f.rank}</span>` : '';
  const dot = f.dot ? `<span class="tn-dot tn-dot--${f.dot}" aria-hidden="true"></span>` : '';
  const risk = f.risk ? `<span class="tn-risk tn-risk--${RISK_TONE[f.risk] || 'mid'}" title="Risk">${esc(f.risk)}</span>` : '';
  const tag = f.tag ? `<span class="tn-tag">${esc(f.tag)}</span>` : '';
  const icons = f.icons ? `<span class="tn-icons">${f.icons.map((m) => modeIcon(m)).join('')}</span>` : '';
  const mark = f.mark ? `<span class="tn-mark tn-mark--${f.mark}" aria-label="${f.mark === 'ok' ? 'on time' : 'late'}">${f.mark === 'ok' ? '✓' : '✗'}</span>` : '';
  return `<button type="button" class="${cls.join(' ')}" data-id="${esc(n.id)}" data-level="${level}" style="${style}"
      aria-pressed="${onPath}"${n.disabled ? ' aria-disabled="true"' : ''}>
    <span class="tn-top">${rank}${dot}${icons}<span class="tn-kicker">${esc(f.kicker || '')}</span>${risk}${tag}</span>
    ${f.title ? `<span class="tn-title">${f.icon ? modeIcon(f.icon) : ''}${esc(f.title)}</span>` : ''}
    ${f.chain || ''}
    <span class="tn-big"><b>${esc(f.big ?? '')}</b>${mark}${f.unit ? ` <span>${esc(f.unit)}</span>` : ''}</span>
    ${f.sub ? `<span class="tn-sub">${esc(f.sub)}</span>` : ''}
    ${f.sub2 ? `<span class="tn-sub tn-sub--2${f.sub2Tone ? ` tn-sub--${f.sub2Tone}` : ''}">${esc(f.sub2)}</span>` : ''}
    ${f.more ? `<span class="tn-sub tn-sub--more">${esc(f.more)}</span>` : ''}
    ${f.clock !== undefined ? `<span class="tn-clock"><span class="tn-clock-k">closes in</span>${DeadlineClock.html(f.clock)}</span>` : ''}
  </button>`;
}

// ================================================================ render
const ASK0 = 'What is happening';

function levels() {
  const out = [{ ids: ['root'], ask: ASK0 }];
  for (let i = 0; i < S.path.length; i++) {
    const n = S.T.get(S.path[i]);
    if (!n || n.disabled) break;
    const kids = kidsOf(n);
    if (!kids.length) break;
    // A shipment with no way to choose goes straight to what is left.
    const only = kids.length === 1 && kids[0].startsWith('told:');
    out.push({ ids: kids, ask: only ? 'Tell the customer' : n.ask });
  }
  return out;
}

/* Rows up to `keep` stay (their boxes are refreshed); the rest are rebuilt
 * and the new ones grow in. */
function render(keep = 0, grow = true) {
  const host = $('tr-levels');
  const rows = levels();
  [...host.querySelectorAll('.tr-level')].forEach((el, i) => { if (i >= keep || i >= rows.length) el.remove(); });
  rows.forEach((row, level) => {
    let el = host.querySelector(`.tr-level[data-level="${level}"]`);
    const html = `<div class="tr-ask"><span class="tr-step" style="--s:var(--step-${Math.min(level + 1, 6)})">${level + 1}</span>${esc(row.ask)}</div>
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
    const w = Math.floor((avail - 16 * (boxes.length - 1)) / boxes.length);
    row.style.setProperty('--tn-w', `${Math.max(150, Math.min(196, w))}px`);
  });
}

function clearLevels() {
  $('tr-levels').querySelectorAll('.tr-level').forEach((el) => el.remove());
}

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

/* The branches: from the chosen box of each level to every box of the next.
 * The chosen line takes its box's colour (a way's rank colour), the others
 * stay quiet. */
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
    const parent = rows[level - 1].querySelector(`.tn[data-id="${CSS.escape(S.path[level - 1] || '')}"]`);
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
      const colour = on ? (kid.style.getPropertyValue('--r') || '') : '';
      const cls = ['tw', on ? 'is-on' : '', off ? 'is-off' : '', level >= fresh ? 'is-new' : ''].join(' ');
      paths.push(`<path class="${cls}" style="--i:${i}${colour ? `;--wc:${colour}` : ''}" pathLength="1"
        d="M${x1},${y1} C${x1},${y1 + my} ${x2},${y2 - my} ${x2},${y2}"/>`
        + `<circle class="tw-dot ${on ? 'is-on' : ''} ${level >= fresh ? 'is-new' : ''}" style="--i:${i}${colour ? `;--wc:${colour}` : ''}" cx="${x2}" cy="${y2}" r="3.2"/>`);
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
    S.path = S.path.slice(0, level).concat([id]);
    render(level + 1);
  } else {
    render(99, false);
  }
  side(n);
  if (scroll && !n.disabled) {
    const next = $('tr-levels').querySelector(`.tr-level[data-level="${level + 1}"]`);
    if (next) next.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

$('tr-levels').addEventListener('click', (e) => {
  const box = e.target.closest('.tn');
  if (!box) return;
  choose(box.dataset.id, Number(box.dataset.level));
});

/* Open one shipment from anywhere (a table row, a link): its customer and
 * its box on the path. */
function openShip(sid) {
  const n = S.T.get(`ship:${sid}`);
  if (!n) return;
  S.path = ['root', `cust:${n.customer}`, n.id];
  S.focus = n.id;
  S.more = false;
  render(1);
  side(n);
}

// ================================================================ the panel
function tiles(list) {
  return `<div class="ins-tiles">${list.filter(Boolean).map((t) => `
    <div class="ins-tile${t.tone ? ` ins-tile--${t.tone}` : ''}"><b>${t.v}</b><span>${esc(t.k)}</span></div>`).join('')}</div>`;
}

function table(cols, rows, { sel = null, key = null, cap = '', rowAttr = null } = {}) {
  if (!rows.length) return '';
  const fixed = cols.some((c) => c.w);
  const group = fixed ? `<colgroup>${cols.map((c) => `<col${c.w ? ` style="width:${c.w}px"` : ''}>`).join('')}</colgroup>` : '';
  return `<div class="ins-tablewrap"><table class="ins-table${fixed ? ' ins-table--fixed' : ''}">
    ${cap ? `<caption>${esc(cap)}</caption>` : ''}${group}
    <thead><tr>${cols.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.k)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((r) => `<tr class="${sel != null && key && r[key] === sel ? 'is-sel' : ''}${r._base ? ' is-base' : ''}"${rowAttr ? ` ${rowAttr(r)}` : ''}>
      ${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${c.f(r)}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

function closes(iso, what = 'Closes in') {
  if (!iso) return '';
  return `<div class="ins-closes"><span class="ins-closes-k">${esc(what)}</span>
    <span class="ins-closes-v">${DeadlineClock.html(iso)}</span>
    <span class="ins-closes-at">${esc(DeadlineClock.at(iso))}</span></div>`;
}

function head(kicker, title, extra = '') {
  return `<div class="ins-head"><span class="ins-kicker">${kicker}</span><h2>${esc(title)}</h2>${extra}</div>`;
}

function more(html) {
  if (!html) return '';
  return `<button type="button" class="ins-more" aria-expanded="${S.more}">
      <span>${S.more ? 'Less detail' : 'More detail'}</span><i aria-hidden="true"></i></button>
    <div class="ins-more-body"${S.more ? '' : ' hidden'}>${html}</div>`;
}

const rankBadge = (r, big = false) => (r ? `<span class="ins-rank${big ? ' ins-rank--big' : ''}" style="--r:${rankVar(r)}">${r}</span>` : '');
const markHTML = (ok) => `<span class="tn-mark tn-mark--${ok ? 'ok' : 'late'}">${ok ? '✓' : '✗'}</span>`;
const riskChip = (label) => (label ? `<span class="tn-risk tn-risk--${RISK_TONE[label] || 'mid'}">${esc(label)}</span>` : '');

/* Every way for this shipment on one table: rank, arrival, extra cost,
 * CO2e, risk, and how long it stays open. */
function waysTable(data, selId) {
  const pool = [...(data.options || []), ...(data.stay ? [data.stay] : [])].sort((a, b) => a.rank - b.rank);
  const w = WEIGHTS[S.weights];
  const body = pool.map((o) => `
    <tr class="wt-name${o.id === selId ? ' is-sel' : ''}"><td colspan="5">${rankBadge(o.rank)}
      ${chainHTML(o.chain, o.plan ? '' : rankVar(o.rank))}<b>${esc(o.plan ? 'Stay on the plan' : o.label)}</b></td></tr>
    <tr class="wt-nums${o.id === selId ? ' is-sel' : ''}">
      <td>${dateShort(o.eta)} ${markHTML(o.on_time)}</td>
      <td class="num">${o.plan ? '0' : num(o.extra_chf)}</td>
      <td class="num">${o.lowest_co2 ? '🌿 ' : ''}${o.co2e_t}</td>
      <td>${riskChip(o.risk_label)}</td>
      <td class="num">${o.on_time && !o.plan ? DeadlineClock.html(o.closes_at) : '<span class="muted">–</span>'}</td></tr>`).join('');
  return `<div class="ins-tablewrap"><table class="ins-table wt">
    <caption>Ranked ${esc(w.label.toLowerCase())}: time ${w.time} · cost ${w.cost} · risk ${w.risk}</caption>
    <thead><tr><th>Arrives</th><th class="num">+CHF</th><th class="num">CO₂e t</th><th>Risk</th><th class="num">Closes in</th></tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

/* When each way arrives, against the promised date: one row per way, a dot
 * where it lands, the promise as a line. Left of the line is on time. */
function arrivals(data, selId) {
  const pool = [...(data.options || []), ...(data.stay ? [data.stay] : [])].sort((a, b) => a.rank - b.rank);
  const promised = Date.parse(data.order.committed);
  const start = Date.parse(data.as_of);
  const times = pool.map((o) => Date.parse(o.eta)).filter(Number.isFinite);
  if (!Number.isFinite(promised) || !times.length) return '';
  const lo = start;
  const hi = Math.max(promised, ...times) + 2 * 86400e3;
  const x = (t) => `${(((t - lo) / (hi - lo)) * 100).toFixed(2)}%`;
  const promise = x(promised);
  return `<div class="ins-sec"><h3>When each way arrives</h3>
    <div class="arr">
      <div class="arr-row arr-row--head"><span></span><span class="arr-track arr-track--head">
        <b class="arr-pl" style="left:${promise}">promised ${esc(dateShort(data.order.committed))}</b></span><span></span></div>
      ${pool.map((o) => `<div class="arr-row${o.id === selId ? ' is-sel' : ''}">
        <span class="arr-k">${rankBadge(o.rank)} ${esc(short(o.plan ? 'The plan' : o.label, 26))}</span>
        <span class="arr-track"><i class="arr-promise" style="left:${promise}"></i><i class="arr-dot${o.plan ? ' is-plan' : ''}" style="left:${x(Date.parse(o.eta))};--r:${o.plan ? 'var(--muted)' : rankVar(o.rank)}" title="${esc(dateShort(o.eta))}"></i></span>
        <span class="arr-v">${esc(dateShort(o.eta))} ${markHTML(o.on_time)}</span></div>`).join('')}
    </div></div>`;
}

/* The shipment on one line of time: now, the promise, the plan, and what
 * happens if nothing is done. */
function timeline(data) {
  const o = data.order;
  const pts = [
    ['Now', data.as_of, 'now'],
    ['Promised', o.committed, 'promise'],
    ['Planned', o.eta_original, 'plan'],
    ['If nothing is done', data.stay?.eta || o.eta_revised, (data.stay ? !data.stay.on_time : o.late_days >= 0.5) ? 'late' : 'plan'],
  ].filter(([, t]) => Number.isFinite(Date.parse(t)));
  if (pts.length < 3) return '';
  const ts = pts.map(([, t]) => Date.parse(t));
  const lo = Math.min(...ts);
  const hi = Math.max(...ts);
  const at = (t) => ((Date.parse(t) - lo) / Math.max(1, hi - lo)) * 92 + 4;
  // Points closer than a label's width take turns above and below the line.
  let last = -99;
  let up = false;
  const placed = [...pts].sort((a, b) => Date.parse(a[1]) - Date.parse(b[1])).map(([k, t, kind]) => {
    const pos = at(t);
    up = pos - last < 16 ? !up : false;
    last = pos;
    return `<span class="tl-pt tl-pt--${kind}${up ? ' is-up' : ''}" style="left:${pos.toFixed(2)}%">
      <i></i><span class="tl-l"><b>${esc(dateShort(t))}</b><small>${esc(k)}</small></span></span>`;
  });
  return `<div class="tl"><span class="tl-bar"></span>${placed.join('')}</div>`;
}

function costBars(parts, counted) {
  const p = parts || {};
  const rows = [
    ['Customer impact', p.customer_impact], ['Expediting', p.expediting], ['Surcharges', p.surcharge],
    ['Delay penalties', counted ? p.penalty : null],
  ].filter(([, v]) => v != null && v > 0.5).sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((s, [, v]) => s + v, 0);
  const top = Math.max(1, ...rows.map(([, v]) => v), counted ? 0 : (p.penalty_if_counted || 0));
  const bar = (k, v, ghost = false) => `<div class="cb-row${ghost ? ' cb-row--ghost' : ''}">
      <span class="cb-k">${esc(k)}</span>
      <span class="cb-track"><i style="width:${Math.max(1.5, (v / top) * 100).toFixed(1)}%"></i></span>
      <span class="cb-v">${num(v)}</span></div>`;
  const penalty = counted ? '' : (p.penalty_if_counted > 0.5 ? bar('Penalties, if counted', p.penalty_if_counted, true) : '');
  return `<div class="ins-sec"><h3>If nobody acts <span class="ins-sum">${chf(total)}</span></h3>
    <div class="cb">${rows.map(([k, v]) => bar(k, v)).join('')}${penalty}</div>
    <p class="ins-note">${counted ? 'Contract delay penalties are counted.' : 'Contract delay penalties are not counted.'}
      <button type="button" class="ins-link" id="pen-toggle">${counted ? 'Leave them out' : 'Count them'}</button></p></div>`;
}

function clocksTable(clocks) {
  const rows = [...(clocks || [])].sort((a, b) => Date.parse(a.due_at) - Date.parse(b.due_at));
  return table([
    { k: 'Contract clock', f: (r) => esc(r.label) },
    { k: 'Who', f: (r) => esc(r.party === 'sika' ? 'Sika' : r.party === 'carrier' ? 'Carrier' : r.party || '') },
    { k: 'Due', f: (r) => `<span class="ins-at">${esc(DeadlineClock.at(r.due_at))}</span>` },
    { k: 'Left', num: true, f: (r) => DeadlineClock.html(r.due_at) },
  ], rows, { cap: 'Started by the event timestamp' });
}

function routeClocks() {
  const seen = new Map();
  (S.d.clocks || []).forEach((e) => (e.clocks || []).forEach((c) => {
    const key = `${c.id}|${c.due_at}`;
    if (!seen.has(key)) seen.set(key, c);
  }));
  return [...seen.values()];
}

function weightsBar() {
  return `<div class="wbar" role="group" aria-label="Rank the ways by">${Object.entries(WEIGHTS).map(([k, w]) => `
    <button type="button" class="wbar-b${k === S.weights ? ' is-on' : ''}" data-weights="${k}" aria-pressed="${k === S.weights}">${esc(w.label)}</button>`).join('')}</div>`;
}

function legsList(o, colour) {
  return `<ol class="legs">${(o.chain || []).map((c) => `<li class="${c.new ? 'is-new' : ''}" style="${c.new ? `--r:${colour}` : ''}">
    ${modeIcon(c.mode, c.new ? colour : '')}<span>${esc(c.from)} → ${esc(c.to)}</span><b>${num(c.km)} km</b>${c.new ? '<em>new</em>' : ''}</li>`).join('')}</ol>`;
}

function deltas(o) {
  const bits = [];
  if (o.days_saved) bits.push(`<span class="dchip ${o.days_saved > 0 ? 'is-good' : 'is-bad'}">${o.days_saved > 0 ? '−' : '+'}${days(Math.abs(o.days_saved))}</span>`);
  if (o.extra_chf) bits.push(`<span class="dchip ${o.extra_chf > 0 ? 'is-cost' : 'is-good'}">${signed(o.extra_chf)}</span>`);
  if (o.co2e_pct != null) bits.push(`<span class="dchip ${o.co2e_pct <= 0 ? 'is-good' : 'is-cost'}">CO₂e ${o.co2e_pct > 0 ? '+' : ''}${Math.round(o.co2e_pct)}%</span>`);
  return bits.length ? `<div class="ins-sec"><h3>Against the plan</h3><p class="dchips">${bits.join('')}</p></div>` : '';
}

function partnersTable(data, o) {
  const rows = (data.partners || []).filter((p) => p.serves.includes(o.id) || (p.reasons[o.id] || []).length).slice(0, 8);
  if (!rows.length) return '';
  return `<div class="ins-sec"><h3>Who can carry it <span class="ins-sum">${rows.filter((p) => p.serves.includes(o.id)).length}</span></h3>${table([
    { k: '', w: 24, f: (p) => (p.serves.includes(o.id) ? '<span class="tn-mark tn-mark--ok">✓</span>' : '<span class="tn-mark tn-mark--late">✗</span>') },
    { k: 'Partner', f: (p) => `<span class="ins-mode">${p.modes.map((m) => modeIcon(m)).join('')}</span>${esc(plain(p.name))}${p.synthetic ? '' : ' <span class="ins-real">real</span>'}` },
    { k: 'Free', num: true, w: 70, f: (p) => `<span title="${esc(p.capacity || '')}">${esc(capacity(p.capacity).big || 'ask')}</span>` },
    { k: 'Away', num: true, w: 58, f: (p) => `${num(p.km)} km` },
    { k: '', w: 64, f: (p) => contact(p) },
  ], rows)}</div>`;
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
          { v: String(ev.length), k: ev.length === 1 ? 'event' : 'events', tone: 'event' },
          { v: String(d.hit.orders), k: 'orders hit' },
          { v: String((d.orders || []).filter((o) => o.branch !== 'absorbed').length), k: 'need a decision' },
          { v: chf(d.cost?.total_chf), k: 'if nobody acts' },
        ])
        + `<div class="ins-sec"><h3>Events</h3><ul class="ins-events">${(d.clocks || []).map((e, i) => `
            <li><b>${esc(e.title)}</b><span>${esc(ev[i]?.kind || '')} · since ${esc(DeadlineClock.at(e.starts_at))}
              · ${ev[i]?.orders ?? 0} orders</span></li>`).join('')}</ul></div>`
        + `<div class="ins-sec"><h3>Contract clocks</h3>${clocksTable(routeClocks())}</div>`
        + more(`${(d.clocks?.[0]?.clocks || []).map((c) => `<p><b>${esc(c.label)}:</b> ${esc(c.basis)}</p>`).join('')}
          ${(d.after_delivery || []).map((c) => `<p><b>${esc(c.label)}, ${c.days} days after delivery:</b> ${esc(c.basis)}
            <a href="${esc(c.source)}" target="_blank" rel="noopener">source</a></p>`).join('')}
          ${ev.map((e) => `<p><b>${esc(e.kind)}:</b> ${esc(e.meaning)}</p>`).join('')}`);
      break;
    }
    case 'calm':
      html = head('Who is hit', 'Fine as planned')
        + tiles([{ v: String(n.absorbed), k: 'absorbed by buffers' }, { v: String(n.notHit), k: 'not hit' }])
        + table([
          { k: 'Order', f: (r) => `<span class="ins-id">${esc(r.shipment_id)}</span>` },
          { k: 'Customer', f: (r) => esc(short(r.customer, 28)) },
          { k: 'At risk', num: true, f: (r) => num(r.loss_chf) },
        ], (d.orders || []).filter((o) => o.branch === 'absorbed'));
      break;
    case 'cust': {
      const c = n.c;
      const first = shipData(c.orders[0]?.shipment_id);
      const clause = first?.customer?.clause;
      html = head(`${c.tier === 'A' ? '<span class="ins-star">★</span> Key account' : c.tier === 'C' ? 'Flexible' : 'Standard'}`, c.name)
        + tiles([
          { v: String(c.orders.length), k: `${ords(c.orders.length)} at risk`, tone: c.tier === 'A' ? 'key' : '' },
          { v: chf(c.loss), k: 'if nobody acts' },
          first?.customer?.impact ? { v: IMPACT_WORD[first.customer.impact] || '!', k: 'if late', tone: first.customer.impact === 'line_down' ? 'late' : '' } : null,
        ])
        + (clause ? `<div class="ins-clause"><b>Contract</b> ${esc(clause.summary || clause.clause || '')}
            <span class="muted">· ${clause.counted ? 'counted' : 'not counted'}</span></div>` : '')
        + `<div class="ins-sec"><h3>Shipments <span class="muted">· click one</span></h3>${table([
          { k: 'Order', f: (r) => `<span class="ins-id">${esc(r.shipment_id)}</span>` },
          { k: 'Late', num: true, f: (r) => days(waysOf(r.shipment_id)?.late_days ?? r.late_days) },
          { k: 'Chance', num: true, f: (r) => pct(r.p_late) },
          { k: 'At risk', num: true, f: (r) => num(r.loss_chf) },
          { k: 'Ways', f: (r) => { const g = waysTag(waysOf(r.shipment_id)); return g ? `<span class="ins-br ins-br--${g.ok ? 'ok' : 'late'}">${g.ok ? '✓' : '✗'} ${esc(g.text)}</span>` : '–'; } },
        ], c.orders, { rowAttr: (r) => `data-open-ship="${esc(r.shipment_id)}" tabindex="0"` })}</div>`
        + (clause ? more(`<p>${esc(clause.clause || '')}</p>${(clause.sources || []).map((u) => `<p><a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a></p>`).join('')}`) : '');
      if (!first && c.orders[0]) ensureShip(c.orders[0].shipment_id);
      break;
    }
    case 'ship':
    case 'wait':
    case 'fail': {
      const sid = n.sid;
      const data = shipData(sid);
      const r = S.T.get(`ship:${sid}`)?.row || {};
      if (!data) {
        html = head('Shipment', `${sid} · ${r.customer || ''}`)
          + (shipEntry(sid)?.error ? `<p class="ins-note">Could not load its ways: ${esc(shipEntry(sid).error)}</p>`
            : '<p class="ins-note ins-wait">Working out its ways…</p>');
        break;
      }
      const o = data.order;
      const best = [...data.options, ...(data.stay ? [data.stay] : [])].find((x) => x.best);
      // Late on the plan, from the same engine as its ways and its timeline.
      const planLate = data.stay ? data.stay.late_days : o.late_days;
      const noWays = !data.options.length && !data.stay;
      const st = statusOf(o.status.level, o.status.label, planLate);
      html = head(`<span class="tn-dot tn-dot--${esc(st.level)}"></span> ${esc(st.label)}`,
        `${sid} · ${data.customer.name}`)
        + tiles([
          { v: planLate >= 0.5 ? `+${days(planLate)}` : '0 d', k: 'late if nothing is done', tone: planLate >= 0.5 ? 'late' : '' },
          { v: pct(o.p_late), k: 'chance late' },
          { v: chf(o.loss_chf), k: 'at risk' },
          best ? { v: `${dateShort(best.eta)} ${best.on_time ? '✓' : '✗'}`, k: !data.options.length ? 'no other route' : `best: ${short(best.plan ? 'the plan' : best.label, 22)}`, tone: 'best' } : null,
        ])
        + timeline(data)
        + `<p class="ins-vehicle">${modeIcon(o.vehicle.mode || o.leg.mode)} <b>${esc(o.vehicle.name || '')}</b> · ${esc(o.phase || '')}
            · ${esc(o.leg.from || '')} → ${esc(o.leg.to || '')}</p>
          <p class="ins-chips">${o.vehicles ? `<span>${o.vehicles.count} ${esc(o.vehicles.unit)}</span>` : ''}
            <span>${num(o.teu)} TEU</span>${o.tonnes ? `<span>${o.tonnes} t</span>` : ''}<span>${plural(o.containers, 'container')}</span>
            <span>${esc(o.cargo.type || '')}</span>${o.cargo.dangerous_goods ? '<span class="is-dg">ADR</span>' : ''}
            <span>${chf(o.cargo.value_chf)} goods</span></p>`
        + (noWays ? `<p class="ins-note">${esc(data.note || '')}</p>` : weightsBar()
          + `<div class="ins-sec">${waysTable(data, null)}</div>`
          + arrivals(data, null))
        + costBars(data.cost.parts, data.cost.penalties_counted)
        + more(`<p>${esc(o.status.reason || '')}</p>${data.note && !noWays ? `<p>${esc(data.note)}</p>` : ''}
          <p>Promised ${esc(DeadlineClock.at(o.committed))}; planned ${esc(DeadlineClock.at(o.eta_original))}; if nothing is done ${esc(DeadlineClock.at(data.stay?.eta || o.eta_revised))}.</p>
          ${data.clocks.length ? `<h3>Contract clocks</h3>${clocksTable(data.clocks)}` : ''}`);
      break;
    }
    case 'opt':
    case 'stay': {
      const { o, data } = n;
      const colour = o.plan ? 'var(--muted)' : rankVar(o.rank);
      const done = S.booked[`${n.sid}|${o.id}`];
      const only = o.plan && !data.options.length;
      html = head(only ? 'No other route' : o.best ? 'Best way' : o.plan ? 'The plan' : `${ORD[o.rank] || `#${o.rank}`} way`, o.plan ? 'Stay on the plan' : o.label, only ? '' : rankBadge(o.rank, true))
        + tiles([
          { v: `${dateShort(o.eta)} ${o.on_time ? '✓' : '✗'}`, k: o.on_time ? 'arrives on time' : `arrives ${days(o.late_days)} late`, tone: o.on_time ? 'ok' : 'late' },
          { v: o.plan ? 'CHF 0' : signed(o.extra_chf), k: 'extra cost' },
          { v: `${o.lowest_co2 ? '🌿 ' : ''}${o.co2e_t} t`, k: 'CO₂e', tone: o.lowest_co2 ? 'green' : '' },
          { v: riskChip(o.risk_label), k: 'risk' },
        ])
        + (o.on_time && !o.plan ? closes(o.closes_at) : '')
        + (done ? `<p class="ins-ok">Booked. ${esc(done.sentence)}</p>` : '')
        + (o.plan ? '' : deltas(o))
        + `<div class="ins-sec"><h3>The way</h3>${legsList(o, colour)}</div>`
        + arrivals(data, o.id)
        + `<div class="ins-sec">${waysTable(data, o.id)}</div>`
        + (o.plan ? '' : partnersTable(data, o))
        + more(`<p>${num(o.km)} km · ${hrs(o.hours)} moving · setup ${hrs(o.setup_h)} · ${plural(o.transfers || 0, 'transfer')}</p>
          ${(o.notes || []).map((t) => `<p>${esc(t)}</p>`).join('')}
          ${o.closes_at ? `<p>Closes ${esc(DeadlineClock.at(o.closes_at))}: after that, starting it misses the promised date.</p>` : ''}
          <p>CO₂e is indicative (GLEC defaults, well to wheel), for the delivery leg after the factory gate.</p>`);
      break;
    }
    case 'src': {
      const s = n.s;
      html = head('Another Sika site', s.label)
        + tiles([
          { v: s.on_time_here ? '✓' : '✗', k: s.on_time_here ? 'on time' : 'still late', tone: s.on_time_here ? 'ok' : 'late' },
          { v: chf(s.value_chf), k: 'goods moved' },
          { v: hrs(s.hours), k: 'to deliver' },
        ])
        + closes(s.on_time_here ? s.closes_at : null)
        + `<div class="ins-sec"><h3>Via</h3><p class="ins-lead">${esc(s.via || '')}</p></div>`
        + more('<p>The same destination served from a Sika site whose own route is calm. Assumes a 48 h handover; the other site\'s capacity is for Procurement and Manufacturing to confirm.</p>');
      break;
    }
    case 'par': {
      const p = n.p;
      const cap = capacity(p.capacity);
      html = head(`${p.modes.map((m) => modeIcon(m)).join('')} ${esc((p.kind || 'partner').replace(/_/g, ' '))}`, plain(p.name))
        + tiles([
          { v: esc(cap.big || 'ask'), k: 'free capacity' },
          { v: `${num(p.km)} km`, k: 'from the freight' },
          p.needs_adr ? { v: p.adr === true ? '✓' : p.adr === false ? '✗' : '?', k: 'ADR (dangerous goods)', tone: p.adr === true ? 'ok' : p.adr === false ? 'late' : '' } : null,
        ])
        + `<div class="ins-sec"><h3>Contact</h3><p class="ins-lead">${contact(p)}</p>
          ${p.synthetic ? '<p class="ins-note">An example partner: the name and number are made up.</p>'
            : `<p class="ins-note">Real operator${p.checked_against ? ` · <a href="${esc(p.checked_against)}" target="_blank" rel="noopener">source</a>` : ''} · confirm before booking</p>`}</div>`
        + `<div class="ins-sec"><h3>Can it carry each way?</h3><ul class="vlist">${[...(n.data.options || [])].map((o) => `
            <li class="${p.serves.includes(o.id) ? 'is-ok' : 'is-no'}">${rankBadge(o.rank)} <span>${esc(o.label)}</span>
              ${p.serves.includes(o.id) ? '<span class="tn-mark tn-mark--ok">✓</span>' : `<small>${esc((p.reasons[o.id] || []).join(' · '))}</small>`}</li>`).join('')}</ul></div>`
        + (p.note ? more(`<p>${esc(p.note)}</p>`) : '');
      break;
    }
    case 'ask': {
      const who = d.approval?.procurement;
      const s = n.s;
      html = head('Who arranges it', who?.name || 'Procurement')
        + tiles([{ v: n.sid, k: 'order' }, { v: chf(s.value_chf), k: 'goods value' }])
        + `<div class="ins-sec"><h3>Contact</h3><p class="ins-lead">${contact(who)}</p>
          <p><a class="ctl ctl--primary" href="mailto:${esc(who?.email || '')}?subject=${encodeURIComponent(`Ship ${n.sid} from ${s.label}`)}&body=${encodeURIComponent(`${s.label}: order ${n.sid}, ${s.on_time_here ? 'on time' : 'late'} via ${s.via}. Can we switch the source?`)}">Email Procurement</a></p></div>`;
      break;
    }
    case 'sign': {
      const sign = signOff(n.o.extra_chf);
      const a = d.approval || {};
      html = head('Sign-off', sign.ok ? 'Within your limit' : 'Above your limit: Controlling signs')
        + tiles([
          { v: chf(Math.max(0, n.o.extra_chf)), k: 'this costs' },
          { v: chf(a.base_limit_chf ?? sign.limit), k: 'your limit' },
          a.crisis_limit_chf ? { v: chf(a.crisis_limit_chf), k: a.crisis ? 'crisis limit, active' : 'crisis limit' } : null,
        ])
        + (sign.ok ? '<p class="ins-ok">You can book it yourself.</p>'
          : `<div class="ins-sec"><h3>Ask</h3><p class="ins-lead">${esc(sign.person?.name || 'Controlling')} ${contact(sign.person)}</p></div>`);
      break;
    }
    case 'book':
      html = bookPanel(n);
      break;
    case 'told':
      html = head('Tell the customer', `${n.sid} · ${n.data.customer.name}`)
        + tiles([
          { v: n.o.late_days >= 0.5 ? `+${days(n.o.late_days)}` : '–', k: 'late by this way', tone: 'late' },
          { v: pct(n.data.order.p_late), k: 'chance late' },
        ])
        + draft(n.data.customer.name, [{ shipment_id: n.sid, late_days: n.o.late_days, p_late: n.data.order.p_late }]);
      break;
    default:
      html = head('', n.id);
  }
  host.innerHTML = html;
  host.scrollTop = 0;
}

function draft(customer, orders) {
  const likely = orders.some((o) => (o.p_late ?? 1) >= 0.5);
  const list = orders.map((o) => (o.late_days >= 0.5 ? `${o.shipment_id} (about ${days(o.late_days)})` : o.shipment_id)).join(', ');
  const text = `Dear ${customer || 'customer'},\n\nA disruption on the route ${likely ? 'is delaying' : 'may delay'} your order`
    + `${orders.length === 1 ? '' : 's'} ${list}. `
    + (likely ? 'We are working on it and will confirm a new delivery date within 24 hours.'
      : 'We are watching it closely and will confirm the delivery date within 24 hours.')
    + '\n\nKind regards,\nSika Supply Chain';
  return `<div class="ins-sec"><h3>Message</h3><textarea class="ins-draft" rows="7" readonly>${esc(text)}</textarea>
    <p class="ins-actions"><button type="button" class="ctl ctl--ghost" id="copy-draft">Copy</button>
    <a class="ctl ctl--primary" href="mailto:?subject=${encodeURIComponent('Delivery update')}&body=${encodeURIComponent(text)}">Email</a></p></div>`;
}

function bookPanel(n) {
  const { o, sid } = n;
  const sign = signOff(o.extra_chf);
  const key = `${sid}|${o.id}`;
  const done = S.booked[key];
  const partner = S.path.map((id) => S.T.get(id)).find((x) => x && x.kind === 'par' && x.sid === sid);
  return head(done ? 'Booked' : 'Book it', `${o.label} · ${sid}`, rankBadge(o.rank, true))
    + tiles([
      { v: `${dateShort(o.eta)} ${o.on_time ? '✓' : '✗'}`, k: 'arrives', tone: o.on_time ? 'ok' : 'late' },
      { v: signed(Math.max(0, o.extra_chf)), k: 'extra cost' },
      { v: sign.ok ? '✓' : '!', k: sign.ok ? 'within limit' : 'Controlling signs' },
    ])
    + (partner ? `<div class="ins-sec"><h3>With</h3><p class="ins-lead">${esc(plain(partner.p.name))} ${contact(partner.p)}</p></div>` : '')
    + (done
      ? `<p class="ins-ok">${esc(done.sentence)}</p>
         <p class="ins-actions"><button type="button" class="ctl ctl--ghost" id="undo-btn" data-key="${esc(key)}">Undo</button></p>`
      : `<p class="ins-actions">
          ${sign.ok ? '' : `<a class="ctl ctl--ghost" href="mailto:${esc(sign.person?.email || '')}?subject=${encodeURIComponent(`Sign-off: ${o.label} for ${sid}`)}&body=${encodeURIComponent(`${o.label} for ${sid}: ${signed(o.extra_chf)}. Above the ${chf(sign.limit)} limit.`)}">Ask Controlling</a>`}
          <button type="button" class="ctl ctl--primary" id="book-btn" data-sid="${esc(sid)}" data-opt="${esc(o.id)}">${sign.ok ? 'Book it now' : 'Book it (signed off)'}</button>
        </p>
        <p class="ins-note">Books this way for ${esc(sid)} at once. Undo within 15 minutes.</p>`);
}

// ================================================================ panel clicks
$('tr-side').addEventListener('click', async (e) => {
  const t = e.target;
  const row = t.closest('[data-open-ship]');
  if (row) { openShip(row.dataset.openShip); return; }
  const w = t.closest('[data-weights]');
  if (w) { setWeights(w.dataset.weights); return; }
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
    const current = Boolean(shipData(S.T.get(S.focus)?.sid)?.cost.penalties_counted ?? S.d.cost?.penalties_counted);
    t.disabled = true;
    try {
      await fetch('/api/penalties', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !current }) });
      S.ship.clear();
      await load({ keepPath: true });
      toast(!current ? 'Delay penalties counted' : 'Delay penalties left out');
    } catch { toast('Could not switch penalties'); t.disabled = false; }
    return;
  }
  if (t.id === 'book-btn') { book(t.dataset.sid, t.dataset.opt, t); return; }
  if (t.id === 'undo-btn') { undo(t.dataset.key, t); }
});

$('tr-side').addEventListener('keydown', (e) => {
  const row = e.target.closest('[data-open-ship]');
  if (row && e.key === 'Enter') openShip(row.dataset.openShip);
});

/* New weights re-rank this shipment's ways: the tree keeps the shipment and
 * regrows what is under it. */
function setWeights(key) {
  if (!WEIGHTS[key] || key === S.weights) return;
  S.weights = key;
  const level = S.path.findIndex((id) => id.startsWith('ship:'));
  if (level >= 0) {
    S.path = S.path.slice(0, level + 1);
    S.focus = S.path[level];
    render(level + 1);
    side(S.T.get(S.focus));
  }
}

async function book(sid, opt, btn) {
  btn.disabled = true;
  const w = WEIGHTS[S.weights];
  try {
    const res = await fetch(`/api/decision/shipment/${encodeURIComponent(sid)}/book?${qs()}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ option_id: opt, weights: { time: w.time, cost: w.cost, risk: w.risk } }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.ok) {
      toast(body.sentence || body.detail || 'Not booked');
      btn.disabled = false;
      return;
    }
    S.booked[`${sid}|${opt}`] = { ids: body.executed.map((x) => x.execution_id), sentence: body.sentence };
    toast(body.sentence);
    render(99, false);
    side(S.T.get(S.focus));
  } catch {
    toast('Could not reach the server');
    btn.disabled = false;
  }
}

async function undo(key, btn) {
  const done = S.booked[key];
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
    delete S.booked[key];
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
/* The path the page opens on, grown a level at a time: the events, the
 * customer with the most at stake (key accounts first), and that customer's
 * first shipment, whose ways then grow under it. ?ship= opens one directly. */
function initialPath() {
  const root = S.T.get('root');
  const firstCust = (root.kids || []).map((id) => S.T.get(id)).find((n) => n.kind === 'cust');
  if (SHIP && S.T.has(`ship:${SHIP}`)) {
    const n = S.T.get(`ship:${SHIP}`);
    return { path: ['root', `cust:${n.customer}`, n.id], focus: n.id };
  }
  if (!firstCust) return { path: ['root'], focus: 'root' };
  return { path: ['root', firstCust.id, firstCust.kids[0]], focus: firstCust.kids[0] };
}

async function load({ keepPath = false } = {}) {
  if (!ROUTE && SHIP) {
    // A shipment alone: find its route first.
    try {
      const res = await fetch(`/api/decision/shipment/${encodeURIComponent(SHIP)}?${qs()}`);
      if (res.ok) ROUTE = (await res.json()).route_id;
    } catch { /* reported below */ }
  }
  if (!ROUTE) {
    $('tr-side').innerHTML = head('No route', 'Open this from a route or a shipment on the board');
    return;
  }
  let d;
  try {
    const res = await fetch(`/api/decision/${encodeURIComponent(ROUTE)}?${qs({ ways: 1 })}`);
    if (!res.ok) throw new Error(String(res.status));
    d = await res.json();
  } catch (err) {
    $('tr-side').innerHTML = head('Could not load', `The tree for ${ROUTE} did not load (${esc(err.message)}).`);
    return;
  }
  const first = !S.d;
  S.d = d;
  build(d);
  if (first) DeadlineClock.start(d.as_of);
  header(d);
  if (keepPath && S.path.every((id) => S.T.has(id) || id.startsWith('opt:') || id.startsWith('par:')
      || id.startsWith('stay:') || id.startsWith('src:') || id.startsWith('ask:')
      || id.startsWith('sign:') || id.startsWith('book:') || id.startsWith('told:'))) {
    render(0, false);
    side(S.T.get(S.focus) || S.T.get('root'));
    return;
  }
  const { path, focus } = initialPath();
  S.path = [];
  S.focus = 'root';
  clearLevels();
  render(0);
  side(S.T.get('root'));
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
