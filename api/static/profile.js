/* The planner's risk profile.
 *
 * Read-only for five tabs, editable for one. Everything shown is the config
 * the engine actually ran on — there is no second store to drift out of sync.
 */
'use strict';

const css = getComputedStyle(document.documentElement);
const LEVEL_COLOR = {
  green:  css.getPropertyValue('--lvl-green').trim(),
  white:  css.getPropertyValue('--lvl-white').trim(),
  blue:   css.getPropertyValue('--lvl-blue').trim(),
  yellow: css.getPropertyValue('--lvl-yellow').trim(),
  red:    css.getPropertyValue('--lvl-red').trim(),
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const chf = (v) => v == null ? '—' : 'CHF ' + Math.round(v).toLocaleString('en-CH');
const num = (v) => v == null ? '—' : Number(v).toLocaleString('en-CH');

function hours(h) {
  if (h == null) return '—';
  if (h < 48) return Math.round(h) + ' h';
  return Math.round(h / 24) + ' days';
}

// The profile describes the same run the board does, so it carries the same
// as-of through the URL. A profile read at a different instant would describe
// a different book of shipments.
const params = new URLSearchParams(location.search);
const AS_OF = params.get('as_of') || '2026-09-18T06:00:00+00:00';
const SHIPMENTS = params.get('shipments') || '150';

const state = { profile: null, dirty: new Map() };

// ===============================================================
// Load
// ===============================================================
async function load() {
  const url = `/api/profile?as_of=${encodeURIComponent(AS_OF)}&shipments=${SHIPMENTS}`;
  const res = await fetch(url);
  if (!res.ok) {
    $('brand-sub').textContent = 'could not load the profile';
    return;
  }
  state.profile = await res.json();
  state.dirty.clear();
  render();
}

function render() {
  const p = state.profile;
  $('brand-sub').textContent =
    `as of ${p.as_of_label} · config ${p.config_version}`;
  renderOverlayFlag(p.overlay_active);
  renderDesk(p.desk, p.overlay_active);
  renderNetwork(p.network);
  renderLedger(p.ledger);
  renderAppetite(p.appetite);
  renderResponse(p.response);
  renderSources(p.sources);
  refreshSaveBar();
}

/* Which files are running on the customer overlay and which on the committed
 * stand-in. Stated as a count rather than a single flag, because a
 * half-configured profile is the normal state during onboarding and one flag
 * would hide it. */
function renderOverlayFlag(overlay) {
  const files = Object.entries(overlay);
  const own = files.filter(([, f]) => !f.is_example);
  const el = $('overlay-flag');
  if (!own.length) {
    el.className = 'overlay-flag';
    el.innerHTML = `<span class="ofdot"></span> all ${files.length} files on the public stand-in`;
    el.title = 'config.example/ — synthetic, committed, nothing customer-specific';
  } else {
    el.className = 'overlay-flag is-own';
    el.innerHTML =
      `<span class="ofdot"></span> ${own.length} of ${files.length} file(s) from config/`;
    el.title = own.map(([n]) => n).join(', ') + ' are the customer overlay';
  }
}

// ===============================================================
// Small builders
// ===============================================================
function section(title, note, body) {
  return `<div class="psec">
    <h2>${esc(title)}</h2>
    ${note ? `<p class="psec-note">${note}</p>` : ''}
    ${body}
  </div>`;
}

function stat(label, value, sub) {
  return `<div class="pstat">
    <div class="pstat-v num">${value}</div>
    <div class="pstat-l">${esc(label)}</div>
    ${sub ? `<div class="pstat-s">${esc(sub)}</div>` : ''}
  </div>`;
}

function table(headers, rows) {
  return `<div class="table-wrap"><table class="rtable">
    <thead><tr>${headers.map((h) =>
      `<th${h.num ? ' class="num"' : ''}>${esc(h.label ?? h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.join('')}</tbody>
  </table></div>`;
}

// ===============================================================
// Desk
// ===============================================================
function renderDesk(d, overlay) {
  const files = Object.entries(overlay).map(([name, f]) => `
    <tr>
      <td>${esc(name)}</td>
      <td><code>${esc(f.source + f.path)}</code></td>
      <td>${f.is_example
        ? '<span class="tag tag--warn">stand-in</span>'
        : '<span class="tag tag--ok">customer</span>'}</td>
    </tr>`);

  $('tab-desk').innerHTML =
    section('This desk', null, `
      <div class="pstat-row">
        ${stat('lanes owned', num(d.lanes))}
        ${stat('shipments in the book', num(d.shipments_in_book),
               d.book_is_synthetic ? 'generated — no order book connected' : 'from the order book')}
        ${stat('corridors', num(d.corridors.length))}
        ${stat('countries touched', num(d.countries.length))}
      </div>`) +

    section('Corridors and their owners',
      'A corridor is how the desk is actually split, and it is what decides who '
      + 'gets called first when a route on it goes red.',
      table(['Corridor', { label: 'Lanes', num: true }, 'Route owner'],
        d.corridors.map((c) => `<tr>
          <td class="r-route">${esc(c.focus.replace(/_/g, ' '))}</td>
          <td class="num">${num(c.lanes)}</td>
          <td class="muted">${esc(c.owner)}</td>
        </tr>`))) +

    section('Modes carried',
      'Each variable in the ledger names the modes it can touch, so this is '
      + 'also the list of which risks can reach this desk at all.',
      `<div class="chips">${d.modes.map((m) =>
        `<span class="chip">${esc(m.mode)} <b>${num(m.legs)}</b> legs</span>`).join('')}</div>`) +

    section('Configuration in force',
      'The profile is not a separate store — it is these files. '
      + '<code>config/</code> is gitignored and overrides <code>config.example/</code>, '
      + 'which stays synthetic and committed.',
      table(['File', 'Loaded from', 'Kind'], files));
}

// ===============================================================
// Network
// ===============================================================
function renderNetwork(n) {
  const nodeRows = n.nodes.map((node) => `<tr>
    <td>
      <div class="r-route">${esc(node.name)}</div>
      <div class="r-focus">${esc(node.id)} · ${esc(node.country)}</div>
    </td>
    <td class="muted">${esc(node.kind)}</td>
    <td class="muted">${node.modes.map(esc).join(' · ')}</td>
    <td>${node.chokepoint ? '<span class="tag tag--warn">chokepoint</span>' : ''}
        ${node.on_river ? '<span class="tag">river</span>' : ''}</td>
    <td class="muted">${node.alternatives.length
      ? node.alternatives.map(esc).join(', ')
      : '<span class="r-dash">none configured</span>'}</td>
    <td class="num">${node.shipments ? num(node.shipments) : '<span class="r-dash">—</span>'}</td>
  </tr>`);

  const laneRows = n.lanes.map((lane) => `<tr>
    <td>
      <div class="r-route">${esc(lane.name)}</div>
      <div class="r-focus">${esc(lane.id)}</div>
    </td>
    <td class="muted">${esc(lane.focus.replace(/_/g, ' '))}</td>
    <td class="muted">${lane.modes.map(esc).join(' · ')}</td>
    <td class="muted">${lane.nodes.map(esc).join(' → ')}</td>
  </tr>`);

  $('tab-network').innerHTML =
    section('Nodes',
      'An alternative is what the response tab offers when a node is blocked. '
      + 'Where none is configured the tool says so rather than implying none exists.',
      table(['Node', 'Kind', 'Modes', 'Flags', 'Alternatives',
             { label: 'Shipments', num: true }], nodeRows)) +
    section('Lanes', null,
      table(['Lane', 'Corridor', 'Modes', 'Path'], laneRows));
}

// ===============================================================
// Risk ledger
// ===============================================================
function renderLedger(l) {
  const families = l.families.map((f) => {
    const rows = f.variables.map((v) => `<tr>
      <td>
        <div class="r-route">${esc(v.name)}</div>
        <div class="r-driver">${esc(v.description)}</div>
      </td>
      <td class="muted">${v.modes.map(esc).join(' · ')}</td>
      <td class="num">${hours(v.lead_time_hours)}</td>
      <td class="num">${v.duration_days == null ? '—' : v.duration_days + ' d'}</td>
      <td>${v.probability_sourceable
        ? '<span class="tag tag--ok">sourceable</span>'
        : '<span class="tag tag--warn">P unsourced</span>'}</td>
    </tr>`);

    const delay = ['minor', 'moderate', 'severe'].map((sev) => {
      const t = f.delay[sev];
      if (!t) return `<span class="chip">${sev} <b>—</b></span>`;
      return `<span class="chip chip--${sev}">${sev}
        <b>${t.optimistic}/${t.likely}/${t.pessimistic} d</b></span>`;
    }).join('');

    return `<details class="fam"${f.unsourceable ? '' : ''}>
      <summary>
        <span class="fam-name">${esc(f.label)}</span>
        <span class="fam-count">${f.variables.length} variables</span>
        ${f.unsourceable
          ? `<span class="tag tag--warn">${f.unsourceable} without a sourceable P</span>`
          : ''}
      </summary>
      <div class="fam-body">
        <p class="psec-note">Delay if it bites, as optimistic / likely / pessimistic
          days. Held at family level, which is why the model has 90 numbers and
          not 45 × 3 × every variation.</p>
        <div class="chips">${delay}</div>
        ${table(['Variable', 'Modes',
                 { label: 'Warning', num: true },
                 { label: 'Typical', num: true }, 'Probability'], rows)}
      </div>
    </details>`;
  });

  $('tab-ledger').innerHTML =
    section(`Risk ledger — ${l.total} variables`,
      'Sika confirmed no risk ledger for outgoing shipments exists today, so '
      + 'this file <b>is</b> the proposal. Every variable names the modes it can '
      + 'touch, how much warning it usually gives, and whether a probability can '
      + 'honestly be sourced for it — where it cannot, the board shows it in a '
      + 'separate band rather than assuming a half.',
      families.join('')) +
    (l.overrides.length
      ? section('Per-variable overrides',
        'Variables whose delay estimate differs from their family default.',
        `<div class="chips">${l.overrides.map((o) =>
          `<span class="chip">${esc(o)}</span>`).join('')}</div>`)
      : '');
}

// ===============================================================
// Appetite — the editable tab
// ===============================================================
function field(path, label, value, unit, note, type) {
  const attrs = type === 'number' || type === undefined
    ? 'type="number" step="any" min="0"'
    : `type="${esc(type)}"`;
  return `<label class="pfield">
    <span class="pfield-l">${esc(label)}</span>
    <span class="pfield-in">
      <input ${attrs} data-path="${esc(path)}"
             value="${value == null ? '' : esc(value)}">
      ${unit ? `<em>${esc(unit)}</em>` : ''}
    </span>
    ${note ? `<span class="pfield-n">${note}</span>` : ''}
  </label>`;
}

function renderAppetite(a) {
  const ladder = a.ladder.map((r) => `
    <div class="prung" style="border-left-color:${LEVEL_COLOR[r.level]}">
      <span class="prung-name" style="color:${LEVEL_COLOR[r.level]}">${esc(r.label)}</span>
      <span class="prung-dir">${esc(r.directive)}</span>
    </div>`).join('');

  const conv = a.convene_rule;
  const agreed = conv.agreed_on
    ? `Agreed ${esc(conv.agreed_on)}${conv.agreed_by ? ' by ' + esc(conv.agreed_by) : ''}.`
    : '<b>Not yet agreed with the planning team.</b> Until it is, the board says '
      + '"proposed convene rule" rather than "convene rule crossed" — the whole '
      + 'mechanism depends on the group having owned the threshold in calm '
      + 'conditions, so claiming agreement it does not have would break it.';

  const minAction = Object.entries(a.min_action_hours).map(([k, v]) =>
    `<span class="chip">${esc(k.replace(/_/g, ' '))} <b>${hours(v)}</b></span>`).join('');

  $('tab-appetite').innerHTML =
    section('The ladder',
      'Every rung of the client\'s own scheme is phrased as a <b>deadline</b>, '
      + 'not a damage band. So the level is not "how bad is this" but "how soon '
      + 'must somebody decide" — which is a quantity the engine already computes.',
      `<div class="prungs">${ladder}</div>`) +

    section('Cutoffs',
      'These are the hours that separate the rungs. Change them and the whole '
      + 'board re-levels. Critical must stay sooner than Alert, and Alert sooner '
      + 'than Watch — otherwise a rung becomes unreachable and nothing on screen '
      + 'would say so, so those are refused on save.',
      `<div class="pfields">
        ${field('alert_levels.red_hours', 'Critical within', a.alert_levels.red_hours,
                'hours', 'client\'s wording: “action within 6 hours”')}
        ${field('alert_levels.yellow_hours', 'Alert within', a.alert_levels.yellow_hours,
                'hours', 'client\'s wording: “within 24–48 hours”')}
        ${field('alert_levels.blue_hours', 'Watch within', a.alert_levels.blue_hours,
                'hours', 'client\'s wording: “determine action within 3–7 days”')}
        ${field('alert_levels.material_chf', 'Something is at stake above',
                a.alert_levels.material_chf, 'CHF',
                '<b>assumed by us.</b> Below this a touched route is Bias '
                + '(monitor) rather than Watch — it is the floor that keeps '
                + 'absorbed disruptions off the decision list.')}
      </div>`) +

    section('Convene rule',
      'Sika, answering Q6: <i>"The real problem is that we declare a crisis too '
      + 'late and lose on available options."</i> Authority is not the '
      + 'bottleneck; the decision to convene is. So the tool does not argue for a '
      + 'crisis — it reports that a threshold the group pre-agreed in calm '
      + 'conditions has been crossed. ' + agreed,
      `<div class="pfields">
        ${field('convene_thresholds.exposure_chf', 'Expected loss across the book',
                conv.thresholds.exposure_chf, 'CHF',
                'out of the Monte Carlo, not a rule of thumb')}
        ${field('convene_thresholds.contracts_exposed', 'Customer contracts exposed',
                conv.thresholds.contracts_exposed, 'contracts',
                'distinct customers with expected loss above zero')}
        ${field('convene_thresholds.shipments_needing_decision',
                'Shipments needing a decision', conv.thresholds.shipments_needing_decision,
                'shipments',
                'inside 48 h of their decision deadline — the Alert rung reused, '
                + 'not a number we invented')}
      </div>
      <p class="psec-note">Every trigger is something a planner can check —
        an expected loss and two counts. Nothing here rests on the summed value
        of acting, which is built on our invented action costs and is not a
        number anyone can stand behind.</p>
      <div class="socket">
        Meeting cadence is modelled as every ${esc(conv.meeting_cadence_days)} days,
        rising to every ${esc(conv.escalated_cadence_days)} in crisis.
        <b>Connecting the team calendar</b> would replace the model with the real
        next slot, which is what the headline "the team does not sit again until…"
        actually needs.
      </div>`) +

    section('Agreement',
      'This is the part that changes behaviour, and it is not technical. Teams '
      + 'do not declare late because they lack a number — they declare late '
      + 'because declaring is socially expensive: somebody has to stick their '
      + 'neck out and risk crying wolf. Recording the date and the group here '
      + 'moves the decision from a judgement one person owns to a rule the '
      + 'group already owns, and the headline changes wording to match. Leave '
      + 'it blank until the meeting has actually happened — claiming agreement '
      + 'it does not have is the one way to break the mechanism outright.',
      `<div class="pfields">
        ${field('convene_meta.agreed_on', 'Agreed on', conv.agreed_on, '',
                'the date the group signed off the thresholds above', 'date')}
        ${field('convene_meta.agreed_by', 'Agreed by', conv.agreed_by, '',
                'the standing group that owns it — e.g. the S&amp;OP meeting', 'text')}
        ${field('convene_meta.meeting_cadence_days', 'Meeting cadence',
                conv.meeting_cadence_days, 'days',
                'how often that group currently sits')}
      </div>`) +

    section('Action durations',
      'How long each mitigation takes to execute. They set the decision '
      + 'deadline: <code>deadline = impact − duration</code>. All <b>assumed by '
      + 'us</b> and not editable here, because changing one silently moves every '
      + 'deadline that depends on it — they belong in a reviewed config change.',
      `<div class="chips">${minAction}</div>`) +

    section('Simulation', null,
      `<div class="pstat-row">
        ${stat('draws per run', num(a.simulation.draws),
               'durations drawn once per iteration, shared across every shipment the event gates')}
        ${stat('seed', num(a.simulation.seed), 'same as-of, same numbers')}
      </div>`);

  bindFields();
}

function bindFields() {
  $('tab-appetite').querySelectorAll('input[data-path]').forEach((input) => {
    input.dataset.original = input.value;
    input.addEventListener('input', () => {
      const changed = input.value !== input.dataset.original;
      input.classList.toggle('is-dirty', changed);
      if (changed) state.dirty.set(input.dataset.path, input.value);
      else state.dirty.delete(input.dataset.path);
      refreshSaveBar();
    });
  });
}

// ===============================================================
// Response
// ===============================================================
function renderResponse(r) {
  const owners = table(['Corridor', 'Owner', 'Role', 'Reach them on', 'Hours'],
    r.route_owners.map((o) => `<tr>
      <td class="r-route">${esc(o.corridor.replace(/_/g, ' '))}</td>
      <td>${esc(o.name)}</td>
      <td class="muted">${esc(o.role || '')}</td>
      <td class="muted">${esc(o.email || o.phone || '—')}</td>
      <td class="muted">${esc(o.hours || '—')}</td>
    </tr>`));

  const teams = table(
    ['Team', 'What they decide', { label: 'Tier', num: true },
     { label: 'Responds within', num: true }, 'In the convene group', 'Reach them on'],
    r.standing_teams.map((t) => `<tr>
      <td class="r-route">${esc(t.name)}</td>
      <td class="r-driver">${esc(t.role)}</td>
      <td class="num">${t.tier == null ? '—' : esc(t.tier)}</td>
      <td class="num">${hours(t.responds_within_hours)}</td>
      <td>${t.convene_member
        ? '<span class="tag tag--ok">yes</span>'
        : '<span class="tag tag--off">no</span>'}</td>
      <td class="muted">${esc(t.email || t.phone || '—')}</td>
    </tr>`));

  const byLevel = Object.entries(r.convene_by_level).map(([level, names]) => `
    <div class="prung" style="border-left-color:${LEVEL_COLOR[level] || 'var(--muted)'}">
      <span class="prung-name" style="color:${LEVEL_COLOR[level] || 'var(--muted)'}">
        ${esc(level)}</span>
      <span class="prung-dir">${(names || []).length
        ? (names || []).map(esc).join(' · ')
        : 'nobody is pulled out of their week'}</span>
    </div>`).join('');

  const seniors = r.seniors.map((s) => `
    <div class="contact">
      <div class="contact-top">
        <span class="contact-name">${esc(s.name)}</span>
        <span class="muted">from ${esc(s.from_level)} upward</span>
      </div>
      <div class="contact-role">${esc(s.role || '')}</div>
      ${s.why ? `<div class="contact-why">${esc(s.why)}</div>` : ''}
      <div class="contact-links">
        ${s.email ? `<a href="mailto:${esc(s.email)}">${esc(s.email)}</a>` : ''}
        ${s.phone ? `<span>${esc(s.phone)}</span>` : ''}
      </div>
    </div>`).join('');

  const esclist = r.escalation.map((e) => `
    <div class="esc-card">
      <div class="esc-level">step ${esc(e.level)}</div>
      <div class="esc-body">
        From ${chf(e.trigger_chf)} of exposure.
        Acknowledge within ${hours(e.acknowledge_within_hours)}.
      </div>
      <div class="esc-list">${(e.notify || []).map(esc).join(' · ')}</div>
    </div>`).join('');

  const carriers = table(['Carrier', 'Modes', { label: 'Responds within', num: true },
                          'Reach them on'],
    r.carriers.map((c) => `<tr>
      <td class="r-route">${esc(c.name)}</td>
      <td class="muted">${(c.modes || []).map(esc).join(' · ')}</td>
      <td class="num">${hours(c.responds_within_hours)}</td>
      <td class="muted">${esc(c.email || c.phone || '—')}</td>
    </tr>`));

  const vendors = r.vendors.map((v) => `
    <div class="cgroup">
      <div class="cgroup-head"><span>${esc(v.node)}</span>
        <span>${v.entries.length}</span></div>
      ${v.entries.map((e) => `<div class="contact">
        <div class="contact-top"><span class="contact-name">${esc(e.name)}</span></div>
        <div class="contact-role">${esc((e.service || e.role || '').replace(/_/g, ' '))}</div>
        <div class="contact-links">
          ${e.email ? `<a href="mailto:${esc(e.email)}">${esc(e.email)}</a>` : ''}
          ${e.phone ? `<span>${esc(e.phone)}</span>` : ''}
        </div>
      </div>`).join('')}
    </div>`).join('');

  $('tab-response').innerHTML =
    section('Route owners',
      'Resolved per corridor, so a Rhine barge problem never hands the planner '
      + 'the Singapore agency. A corridor with no owner configured is reported as '
      + 'exactly that, rather than falling back to somebody who does not own it.',
      owners) +

    section('Who is drawn in, by level',
      'Sika: <i>"in crisis, established teams that meet on a weekly schedule '
      + 'increase meeting frequency… they have full authority to decide on '
      + 'mitigation."</i> So the ladder decides how many of those teams are '
      + 'pulled out of their weekly cycle — nobody at Normal, everybody at '
      + 'Critical. It never invents a new committee.',
      `<div class="prungs">${byLevel}</div>`) +

    section('Standing teams', null, teams) +

    section('Seniors',
      'Drawn in by <b>level</b>, not by money. A large but comfortable exposure '
      + 'does not need a senior; a small one that must be decided within six '
      + 'hours does.', seniors) +

    section('Escalation steps',
      'Exposure decides how far up a single route escalates. This is separate '
      + 'from the convene rule, which looks at the whole book.', esclist) +

    section('Spend authority', null,
      `<div class="pstat-row">
        ${stat('delegated limit', chf(r.approval.delegated_limit_chf),
               'below this the planner acts without asking')}
        ${stat('approver above it', r.approval.approver ? esc(r.approval.approver) : '—',
               'named on the action itself, so the ask is already addressed')}
      </div>`) +

    section('Alternate carriers', null, carriers) +

    section('Alternate vendors by node',
      'Filtered to the route in question when this appears on the board — a '
      + 'contact list that returns everyone is the same as no contact list.',
      vendors);
}

// ===============================================================
// Sources
// ===============================================================
const STATUS_TAG = {
  connected: 'tag--ok',
  fixture: 'tag--warn',
  absent: 'tag--off',
};
const STATUS_WORD = {
  connected: 'connected',
  fixture: 'example stand-in',
  absent: 'absent',
};

/* What a source costs, said plainly. "free" is the interesting word here and
 * it is worth the pixels: the whole catalogue is free, and a planner reading
 * this screen should not have to take that on trust from a README. */
const COST_WORD = {
  free: 'free · no key',
  free_with_key: 'free · needs a free account',
  paid: 'PAID',
};

/* Green for the ones that cost nothing and need nothing, amber for the ones
 * that are still free but need a registration first. There is no red variant
 * because there are no paid sources — a test fails the build if one ever
 * ships enabled, so a red tag here would be unreachable code on a screen. */
const COST_TAG = {
  free: 'tag--ok',
  free_with_key: 'tag--warn',
  paid: 'tag--warn',
};

/* report vs instrument. This one field decides whether a source ever costs a
 * model call, so it belongs on the screen next to the source, not buried in a
 * design document. */
const NATURE_WORD = {
  report: 'read by a model',
  instrument: 'measured — no model',
};

function renderSources(s) {
  const feeds = s.feeds.map((f) => `
    <div class="feed">
      <div class="feed-top">
        <span class="tag ${STATUS_TAG[f.status] || ''}">${esc(STATUS_WORD[f.status] || f.status)}</span>
        <span class="feed-name">${esc(f.label)}</span>
        ${f.records ? `<span class="muted num">${num(f.records)} records</span>` : ''}
        <span class="muted">tier ${esc(f.source_tier)}</span>
        ${f.cost ? `<span class="tag ${COST_TAG[f.cost] || 'tag--off'}">${esc(COST_WORD[f.cost] || f.cost)}</span>` : ''}
        ${f.nature ? `<span class="muted">${esc(NATURE_WORD[f.nature] || f.nature)}</span>` : ''}
      </div>
      <div class="feed-detail">${esc(f.detail)}</div>
      ${f.status !== 'connected' && f.unlocks_if_connected
        ? `<div class="feed-unlock">↳ connecting it would unlock: ${esc(f.unlocks_if_connected)}</div>`
        : ''}
      ${f.url ? `<div class="feed-url"><code>${esc(f.url)}</code></div>` : ''}
    </div>`).join('');

  const tiers = s.tiers.map((t) => `<tr>
    <td class="num">${esc(t.tier)}</td>
    <td>${esc(t.label || t.name || '')}</td>
    <td class="muted">${esc(t.description || t.note || '')}</td>
  </tr>`);

  const priced = s.feeds.filter((f) => f.cost);
  const billable = priced.filter((f) => f.cost === 'paid').length;
  const modelled = priced.filter((f) => f.reaches_a_model).length;

  const costLine = priced.length
    ? `<p class="rpanel-note"><b>${priced.length} external source(s) configured, `
      + `${billable} of them billable.</b> Nothing reaches the internet until `
      + `<code>RADAR_ALLOW_NETWORK=1</code>; until then every one falls back to a `
      + `recorded sample and says so above. ${modelled} are read by a model; the `
      + `rest are measurements a threshold table reads for free.</p>`
    : '';

  $('tab-sources').innerHTML =
    section('Inputs',
      'Three states, never two: <b>connected</b>, <b>example stand-in</b> or '
      + '<b>absent</b>. An absent feed says what connecting it would unlock, so '
      + 'the gap is a scoping decision rather than a silent hole.', costLine + feeds) +
    (tiers.length
      ? section('Source tiers',
        'How much corroboration a claim needs before it is allowed to move a '
        + 'number. Social media can raise a flag; it cannot on its own change a '
        + 'delivery date.',
        table([{ label: 'Tier', num: true }, 'Name', 'What it means'], tiers))
      : '');
}

// ===============================================================
// Save / reset
// ===============================================================
/* The bar is shown whenever there is something to DO, which is not the same
 * as "there are unsaved edits".
 *
 * Tying it to the dirty count alone hid Reset the moment a save succeeded —
 * so the only route back to the committed stand-in was to make a dummy edit
 * first. An overlay in force is itself an actionable state: the planner is no
 * longer on the numbers the repo ships, and they must be able to say so and
 * undo it. Each button is enabled by its own condition. */
function hasOverlay() {
  const files = Object.values(state.profile?.overlay_active || {});
  return files.some((f) => !f.is_example);
}

function refreshSaveBar() {
  const n = state.dirty.size;
  const overlay = hasOverlay();

  $('savebar').hidden = n === 0 && !overlay;
  $('btn-save').disabled = n === 0;
  $('btn-reset-profile').disabled = !overlay;

  if (n) {
    $('savebar-text').innerHTML =
      `<b>${n}</b> setting${n === 1 ? '' : 's'} changed. Saving writes `
      + `<code>config/scoring.yaml</code> — gitignored, and it overrides the `
      + `committed stand-in. The board re-runs on the new numbers.`;
  } else if (overlay) {
    $('savebar-text').innerHTML =
      'Running on the <b>customer overlay</b> in <code>config/</code>, not the '
      + 'committed stand-in. Reset deletes that file and puts every number back '
      + 'to what the repository ships.';
  }
}

function collectEdits() {
  const edits = {};
  for (const [path, value] of state.dirty) {
    const [group, key] = path.split('.');
    (edits[group] ||= {})[key] = value;
  }
  return edits;
}

function flash(message, kind) {
  const bar = $('savebar');
  bar.hidden = false;
  $('savebar-text').innerHTML = `<span class="flash flash--${kind}">${message}</span>`;
}

async function save() {
  const res = await fetch('/api/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collectEdits()),
  });
  const out = await res.json();

  if (out.problems && out.problems.length) {
    // Nothing was written. Say which rung would have become unreachable.
    flash('Nothing saved — ' + out.problems.map(esc).join('; '), 'bad');
    return;
  }
  const parts = [];
  if (out.applied.length) parts.push(`${out.applied.length} setting(s) saved`);
  if (out.rejected.length) parts.push(`${out.rejected.length} rejected (not editable)`);
  flash(esc(parts.join('; ') || 'nothing to save') + '. Reloading the profile…', 'ok');
  await load();
}

async function reset() {
  const out = await fetch('/api/profile', { method: 'DELETE' }).then((r) => r.json());
  flash(out.removed
    ? 'Overlay removed — back on the committed stand-in.'
    : 'No overlay was in place; already on the committed stand-in.', 'ok');
  await load();
}

// ===============================================================
// Tabs
// ===============================================================
$('prof-rail').addEventListener('click', (event) => {
  const button = event.target.closest('.prail');
  if (!button) return;
  $('prof-rail').querySelectorAll('.prail').forEach((b) =>
    b.classList.toggle('is-on', b === button));
  document.querySelectorAll('.ppanel').forEach((panel) =>
    panel.classList.toggle('is-on', panel.id === 'tab-' + button.dataset.tab));
  location.hash = button.dataset.tab;
});

$('btn-save').addEventListener('click', save);
$('btn-reset-profile').addEventListener('click', reset);

load().then(() => {
  const wanted = location.hash.slice(1);
  const button = wanted && $('prof-rail').querySelector(`[data-tab="${wanted}"]`);
  if (button) button.click();
});
