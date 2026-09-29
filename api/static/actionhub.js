/* The Action Hub, the partner card, and the boot that wires the map together.
 *
 * Both cards are renderers of MapStore. Every control on them calls MapAgent
 * — the weight sliders are `rankRoutes`, the split toggle is `toggleSplit`,
 * a partner row is `selectVendor` — so an agent driving the same functions
 * opens the same cards with the same contents.
 *
 * DISAPPEAR ON CLOSE
 * ------------------
 * Closing is `MapAgent.clearSelection()`. The store drops the routes, the split
 * and the partners with the selection; the map erases them because the state
 * no longer has them. The hazards matrix and radar are the shipment's own
 * page (/shipment/{id}), not this card. No renderer has to remember to clean up.
 *
 * GLASS
 * -----
 * backdrop-filter: blur(10px) over rgba(255,255,255,0.8), or the dark
 * equivalent — translucent enough that the route under the card still reads
 * as a route, opaque enough that the text on it is text.
 */
'use strict';

(function (root) {
  const $ = (id) => document.getElementById(id);
  const tokenOf = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const chf = (v) => (v == null ? '—' : `CHF ${Math.round(v).toLocaleString('en-US')}`);
  const when = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return `${d.toUTCString().slice(0, 3)} ${d.toISOString().slice(8, 10)} ${d.toUTCString().slice(8, 11)} ${d.toISOString().slice(11, 16)}`;
  };
  const hoursText = (h) => {
    if (h == null) return '—';
    const a = Math.abs(h);
    if (a < 1) return `${Math.round(a * 60)} min`;
    if (a < 48) return `${a.toFixed(a < 10 ? 1 : 0)} h`;
    return `${(a / 24).toFixed(1)} days`;
  };
  const statusColour = (s) => tokenOf(`--status-${s}`);
  // The card keys to the ROUTE's ladder level, like the vehicle on the map
  // and the ladder in the top bar; the vehicle's own delay stays in words.
  const LEVEL_LABEL = { red: 'Critical', yellow: 'Alert', blue: 'Watch', white: 'Bias', green: 'Normal' };
  const routeLevel = (s, id) => {
    const a = s.assets.byId[id];
    return a && LEVEL_LABEL[a.route_level] ? a.route_level : 'green';
  };
  const levelColour = (l) => tokenOf(`--lvl-${l}`);
  const tCo2 = (kg) => (kg == null ? '—' : `${(kg / 1000).toFixed(kg < 10000 ? 2 : 1)} t`);
  const pct = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(Math.round(v))}%`);
  const altColour = (rank) => tokenOf(`--alt-${Math.max(1, Math.min(4, rank || 4))}`);

  function createHub({ store, agent, select }) {
    const hub = $('hub');
    const card = $('vendor-card');
    const hubBody = $('hub-body');
    let placed = false;

    // ---------------------------------------------------------------
    // Dragging — a card you cannot move is a card that hides the route
    // ---------------------------------------------------------------
    function draggable(node, handle) {
      let start = null;
      handle.addEventListener('pointerdown', (e) => {
        if (e.target.closest('button, a, input, select')) return;
        const r = node.getBoundingClientRect();
        start = { x: e.clientX, y: e.clientY, left: r.left, top: r.top };
        handle.setPointerCapture(e.pointerId);
      });
      handle.addEventListener('pointermove', (e) => {
        if (!start) return;
        const w = node.offsetWidth;
        const left = Math.min(Math.max(8 - w + 80, start.left + e.clientX - start.x), innerWidth - 80);
        const top = Math.min(Math.max(8, start.top + e.clientY - start.y), innerHeight - 48);
        node.style.left = `${left}px`;
        node.style.top = `${top}px`;
        node.dataset.moved = '1';
      });
      const end = () => { start = null; };
      handle.addEventListener('pointerup', end);
      handle.addEventListener('pointercancel', end);
    }
    draggable(hub, $('hub-head'));
    draggable(card, $('vcard-head'));

    function place() {
      if (placed && hub.dataset.moved) return;
      const pane = document.querySelector('.globe-wrap').getBoundingClientRect();
      const w = hub.offsetWidth || 420;
      // Right side of the map, clear of the zoom / compass controls.
      hub.style.left = `${Math.max(8, Math.min(pane.right - w - 60, innerWidth - w - 8))}px`;
      hub.style.top = `${Math.max(8, pane.top + 64)}px`;
      hub.style.maxHeight = `${Math.max(320, Math.min(innerHeight, pane.bottom) - Math.max(8, pane.top + 64) - 12)}px`;
      placed = true;
    }
    function placeCard() {
      if (card.dataset.moved) return;
      const r = hub.getBoundingClientRect();
      const w = card.offsetWidth || 330;
      const left = r.left - w - 12 > 8 ? r.left - w - 12 : Math.min(r.right + 12, innerWidth - w - 8);
      card.style.left = `${left}px`;
      card.style.top = `${Math.max(8, r.top + 40)}px`;
    }

    $('hub-close').addEventListener('click', () => agent.clearSelection());
    $('vcard-close').addEventListener('click', () => agent.selectVendor(null));
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const s = store.getState();
      if (s.vendors.selected) agent.selectVendor(null);
      else if (s.selection.id) agent.clearSelection();
    });

    // ---------------------------------------------------------------
    // Sections
    // ---------------------------------------------------------------
    function head(d, s) {
      const st = d.status;
      const lvl = routeLevel(s, d.shipment_id);
      $('hub-title').innerHTML = `
        <h3>${esc(d.asset.name)} <span class="hub-id">${esc(d.asset.asset_id)}</span>
          <span class="st-chip" style="--c:${levelColour(lvl)}" title="The route's level on the ladder: how soon a decision on this lane is due"><i></i>${esc(LEVEL_LABEL[lvl])} route</span></h3>
        <p class="hub-sub"><b>${esc(d.shipment_id)}</b> · ${esc(d.logistics.customer)}
          <span class="hub-st hub-st--${esc(st.level)}" title="${esc(st.reason)}">${esc(st.label)}</span></p>`;
      return '';
    }

    /* The vehicle: who runs it, where it is, which leg, last heard of. Its
     * dates and its legs are the shipment's card in the Shipments tab. */
    const ICON = {
      crew: '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="6.5" r="3.3"/><path d="M3.5 17.5c.6-3.6 3.2-5.6 6.5-5.6s5.9 2 6.5 5.6z"/></svg>',
      pin: '<svg viewBox="0 0 20 20" aria-hidden="true"><path fill-rule="evenodd" d="M10 18.5s-5.8-6-5.8-10a5.8 5.8 0 0 1 11.6 0c0 4-5.8 10-5.8 10zm0-7.9a2.1 2.1 0 1 0 0-4.2 2.1 2.1 0 0 0 0 4.2z"/></svg>',
      leg: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2.5l6 15-6-3.6-6 3.6z"/></svg>',
      sync: '<svg viewBox="0 0 20 20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M16 7.5A6.5 6.5 0 0 0 4.3 6M4 12.5A6.5 6.5 0 0 0 15.7 14M4 2.8V6.4h3.6M16 17.2v-3.6h-3.6"/></svg>',
    };
    function vehicle(d) {
      const crew = d.asset.crew;
      const pos = d.position;
      const planned = pos.source === 'schedule';
      const tile = (icon, v, k, title) => `<div class="vt" title="${esc(title || '')}"><span class="vt-i">${ICON[icon]}</span>
        <span class="vt-v">${v}</span><span class="vt-k">${esc(k)}</span></div>`;
      return `
      <section class="hsec" data-sec="vehicle">
        <div class="vtiles">
          ${tile('pin', `<span class="mono">${pos.lat.toFixed(2)}, ${pos.lon.toFixed(2)}</span>`, planned ? 'Position (planned)' : 'Position (reported)',
            planned ? 'From the schedule: no GPS feed' : `Field report${pos.accuracy_m ? ` ±${Math.round(pos.accuracy_m)} m` : ''}${d.drift_km != null ? ` · ${d.drift_km} km off plan` : ''}`)}
          ${tile('leg', `${Math.round(d.heading_deg)}° · ${d.leg.index + 1}/${d.leg.count}`, `Leg · ${d.leg.mode}`, `${d.leg.from_name} → ${d.leg.to_name}`)}
          ${tile('sync', esc(when(d.last_sync).slice(4)), 'Last sync', d.last_sync_source)}
          ${tile('crew', esc(crew.name), crew.role, crew.verified ? 'verified' : 'synthetic')}
        </div>
      </section>`;
    }

    function load(d) {
      const l = d.load;
      const cap = l.capacity_teu;
      const pct = (x) => `${Math.min(100, (100 * x) / cap).toFixed(1)}%`;
      const boxes = d.containers.map((b) => `
        <span class="bx ${b.on_time_original ? 'is-ok' : 'is-late'}${b.priority === 'critical' ? ' is-crit' : ''}"
              title="${esc(b.container_id)} · ${esc(b.content)} · ${b.gross_t} t${b.dangerous_goods ? ' · ADR' : ''}${b.temperature_controlled ? ' · reefer' : ''} · ${b.priority} · ${b.on_time_original ? 'on time' : 'late'} as planned, due ${esc(when(b.deadline).slice(4, 10))}">
          ${b.priority === 'critical' ? '<i></i>' : ''}${b.size_ft}' <b>${b.on_time_original ? '✓' : '✗'}</b></span>`).join('');
      return `
      <section class="hsec" data-sec="load">
        <h4>Load <span>${esc(d.cargo.type)}${d.cargo.dangerous_goods ? ' · <span class="tag tag--warn">ADR</span>' : ''}${d.cargo.temperature_controlled ? ' · <span class="tag">reefer</span>' : ''}</span></h4>
        <div class="capnums"><span><b>${Math.round(100 * l.utilisation)}%</b> full · ${l.loaded_teu}/${cap} TEU${l.vehicles && l.vehicles.count > 1 ? ` · ${l.vehicles.count} ${esc(l.vehicles.unit)}` : ''}</span>
          <span>ours <b>${l.ours_teu} TEU</b></span></div>
        <div class="capbar" title="Loaded ${l.loaded_teu} of ${cap} TEU; ours ${l.ours_teu}${l.usable_teu != null ? `; only ${l.usable_teu} TEU usable at today's water level` : ''}">
          <span style="width:${pct(l.loaded_teu)}"></span>
          <span class="ours" style="width:${pct(l.ours_teu)}"></span>
          ${l.usable_teu != null ? `<em style="left:${pct(l.usable_teu)}"></em>` : ''}
        </div>
        <div class="bxs" title="Each box: size, on time as planned; red dot: critical">${boxes}</div>
      </section>`;
    }

    function logs(d) {
      const colour = (l) => (l === 'red' || l === 'yellow' || l === 'green' ? statusColour(l) : tokenOf('--accent'));
      return `
      <section class="hsec" data-sec="logs">
        <details class="hfold"><summary><h4>Status log <span>${d.logs.length}</span></h4></summary>
        <div class="logs">${d.logs.map((l) => `
          <div class="log" style="--c:${colour(l.level)}">
            <div class="log-top"><span class="tag">${esc(l.kind)}</span><b>${esc(l.title)}</b></div>
            <div class="log-time">${esc(when(l.at))} UTC</div>
            ${l.detail ? `<div class="log-detail" title="${esc(l.detail)}">${esc(l.detail)}</div>` : ''}
            <div class="log-src">${esc(l.source)}</div>
          </div>`).join('')}</div>
        </details>
      </section>`;
    }

    function routes(s) {
      const r = s.routing;
      const d = r.data;
      if (r.status === 'idle' && !d) return '';
      if (!d) {
        return `<section class="hsec" data-sec="routes"><h4>Recovery routes</h4>
          <p class="hnote">${r.status === 'error' ? `Could not calculate routes: ${esc(r.error)}` : 'Calculating recovery routes…'}</p></section>`;
      }
      const w = r.weights || d.weights;
      const slider = (k, label) => `
        <label for="w-${k}">${label}</label>
        <input type="range" id="w-${k}" data-w="${k}" min="0" max="100" step="5" value="${Math.round(w[k] * 100)}">
        <output>${Math.round(w[k] * 100)}%</output>`;
      const alts = d.candidates.map((c) => `
        <button type="button" class="alt${r.hovered === c.id ? ' is-hot' : ''}${r.chosen === c.id ? ' is-chosen' : ''}"
                data-route="${esc(c.id)}" style="--c:${altColour(c.rank)}">
          <span class="rbadge" style="--c:${altColour(c.rank)}">${esc(c.badge || `#${c.rank}`)}</span>
          <span class="alt-label" title="${esc([`${c.delta.text} · arrives ${when(c.eta)}${c.meets_commitment ? ' ✓' : ' ✗'} · ${Math.round(c.km).toLocaleString('en-US')} km · setup ${c.setup_hours} h`, c.touches.length ? `Still passes: ${c.touches.map((t) => t.title).join('; ')}` : '', ...c.notes].filter(Boolean).join('\n'))}">${esc(c.label)}</span>
        </button>`).join('');
      return `
      <section class="hsec" data-sec="routes">
        <h4>Ways on the map <span>${r.status === 'loading' ? 're-ranking…' : 'hover to light one up'}</span></h4>
        ${d.eligible && d.candidates.length ? `<div class="weights">${slider('time', 'Time')}${slider('cost', 'Cost')}${slider('risk', 'Risk')}</div>` : ''}
        ${d.note ? `<p class="hnote hnote--warn">${esc(d.note)}</p>` : ''}
        ${(d.no_route || []).map((t) => `<p class="hnote">${esc(t)}</p>`).join('')}
        <div class="alts">${alts}</div>
      </section>`;
    }

    /* This shipment's own branch of the Action decision tree, in its window. */
    function shipTreeLink(s, cls) {
      const det = s.selection.detail;
      if (!det) return '';
      const q = new URLSearchParams();
      if (s.params.as_of) q.set('as_of', s.params.as_of);
      if (s.params.shipments) q.set('shipments', s.params.shipments);
      q.set('route', det.logistics.lane_id);
      q.set('ship', s.selection.id);
      return `<a class="${cls}" href="/tree?${q}" target="_blank" rel="noopener" title="Decide this shipment step by step: its ways, partners and booking"><svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><path d="M10 3v4M10 7l-5 4M10 7l5 4M5 11v3M15 11v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="10" cy="3.5" r="1.8" fill="currentColor"/><circle cx="5" cy="15.5" r="1.8" fill="currentColor"/><circle cx="15" cy="15.5" r="1.8" fill="currentColor"/></svg> Decision tree for ${esc(s.selection.id)} ↗</a>`;
    }

    function split(s) {
      const d = s.routing.data;
      if (!d || !d.eligible || !d.candidates.length) return '';
      const detail = s.selection.detail;
      const few = detail && detail.containers.length < 2;
      const sp = s.split;
      const data = sp.enabled ? sp.data : null;
      let body = '';
      if (sp.enabled && !data) {
        body = `<p class="hnote">${sp.status === 'error' ? `Could not evaluate: ${esc(sp.error)}` : 'Working out which containers to move…'}</p>`;
      } else if (data) {
        const colourOf = (routeId) => (routeId === 'ORIGINAL' ? levelColour(routeLevel(s, d.shipment_id))
          : altColour((d.candidates.find((c) => c.id === routeId) || {}).rank));
        const target = data.target || s.routing.chosen || (d.candidates[0] && d.candidates[0].id);
        const moved = data.containers.filter((c) => c.route_id !== 'ORIGINAL').length;
        const total = data.summary.teu_total || 1;
        const sm = data.summary;
        body = `
          ${data.suggestion_reason ? `<p class="hnote">${esc(data.suggestion_reason)}</p>` : ''}
          <div class="alloc">
            <div class="alloc-bar" title="TEU by branch">
              ${data.branches.map((b) => `<span style="--c:${colourOf(b.route_id)};width:${(100 * b.teu) / total}%" title="${esc(b.label)}: ${b.teu} TEU"></span>`).join('')}
            </div>
            <div class="alloc-row">
              <span>Move</span>
              <input type="range" id="split-n" min="0" max="${data.containers.length}" step="1" value="${moved}"
                     aria-label="Number of most urgent containers to move">
              <b id="split-n-out">${moved}</b>
              <span>most urgent to</span>
            </div>
            <div class="alloc-row">
              <select id="split-target" aria-label="Route for the moved containers">
                ${d.candidates.map((c) => `<option value="${esc(c.id)}"${c.id === target ? ' selected' : ''}>${esc(c.badge || `#${c.rank}`)} ${esc(c.label)}</option>`).join('')}
              </select>
            </div>
            <div class="chips">${data.containers.map((c) => {
              const branch = data.branches.find((b) => b.route_id === c.route_id);
              const ok = branch && branch.on_time.includes(c.container_id);
              return `<button type="button" class="chip" data-box="${esc(c.container_id)}" data-route="${esc(c.route_id)}"
                        style="--c:${colourOf(c.route_id)}" title="${esc(c.content)} · ${c.teu} TEU · due ${esc(when(c.deadline))} · click to move">
                  ${c.priority === 'critical' ? '<span class="crit">!</span>' : ''}<span class="mono">${esc(c.container_id.slice(4, 10))}</span> ${ok ? '✓' : '✗'}</button>`;
            }).join('')}</div>
          </div>
          <div class="split-sum">
            <div><div class="k">On time</div><div class="v">${sm.on_time_before} → ${sm.on_time_after}<small style="font-weight:500"> / ${sm.containers}</small></div></div>
            <div><div class="k">Extra cost</div><div class="v">${sm.extra_cost_chf >= 0 ? '+' : '−'}${chf(Math.abs(sm.extra_cost_chf)).replace('CHF ', 'CHF ')}</div></div>
            <div><div class="k">CO₂e</div><div class="v">${tCo2(sm.co2e_before_kg)} → ${tCo2(sm.co2e_after_kg)}</div></div>
          </div>
          <div class="branches">${data.branches.map((b, i) => `
            <div class="branch" style="--c:${colourOf(b.route_id)}"><b>${'ABCDEFG'[i]}</b>
              <span>${esc(b.label)} · ${b.containers.length} box(es), ${b.teu} TEU · ETA ${esc(when(b.eta))} · ${tCo2(b.co2e_kg)} CO₂e${b.vehicles ? ` · ${b.vehicles.count} ${esc(b.vehicles.unit)}` : ''}${b.late.length ? ` · <span style="color:var(--status-red)">${b.late.length} late</span>` : ''}</span></div>`).join('')}</div>`;
      }
      return `
      <section class="hsec" data-sec="split">
        <h4>Smart split <span>urgent boxes fast, the rest stay put</span></h4>
        <label class="switch"><input type="checkbox" id="split-toggle" ${sp.enabled ? 'checked' : ''} ${few ? 'disabled' : ''}>
          <span class="track"></span> Split shipment</label>
        ${few ? '<p class="chart-note">One container: nothing to split.</p>' : ''}
        ${body}
      </section>`;
    }

    function partners(s) {
      const v = s.vendors;
      if (v.status === 'idle' && !v.data) return '';
      if (!v.data) {
        return `<section class="hsec" data-sec="partners"><h4>Partners nearby</h4>
          <p class="hnote">${v.status === 'error' ? `Could not query partners: ${esc(v.error)}` : 'Querying partners in the radius…'}</p></section>`;
      }
      const d = v.data;
      const rows = d.vendors.map((p) => `
        <button type="button" class="partner${p.covers_any ? '' : ' no'}${v.selected === p.id ? ' is-on' : ''}" data-vendor="${esc(p.id)}">
          ${root.MapView.CHANNEL[p.channel] || root.MapView.CHANNEL.phone}
          <span><span class="partner-name">${esc(p.name)}</span>
            <span class="partner-sub">${esc(p.kind.replace(/_/g, ' '))} · ${esc(p.city || '')} · ${Math.round(p.distance_km)} km${p.within_radius ? '' : ' (outside radius)'} · ${p.capacity ? `${p.capacity.available} ${esc(p.capacity.unit)}` : 'capacity unknown'}</span></span>
          <span class="covers">${p.serviceable.map((c) => `<span class="rbadge${c.ok ? '' : ' no'}" style="--c:${altColour(c.rank)}" title="${esc(c.label)}: ${c.ok ? 'can cover' : 'cannot'}${c.reasons.length ? `. ${esc(c.reasons.join('; '))}` : ''}">${esc(c.badge || `#${c.rank}`)}</span>`).join('')}</span>
        </button>`).join('');
      return `
      <section class="hsec" data-sec="partners">
        <h4>Partners nearby <span>within ${Math.round(d.radius_km)} km of the asset</span></h4>
        ${d.note ? `<p class="hnote hnote--warn">${esc(d.note)}</p>` : ''}
        <div class="partners">${rows || '<p class="chart-note">No partners on file.</p>'}</div>
      </section>`;
    }

    function foot(s) {
      const d = s.selection.detail;
      const p = s.params;
      const q = new URLSearchParams();
      if (p.as_of) q.set('as_of', p.as_of);
      if (p.shipments) q.set('shipments', p.shipments);
      const lane = d.logistics.lane_id;
      const recent = s.journal.slice(-8).reverse();
      return `
      <div class="hub-foot">
        ${shipTreeLink(s, 'ctl ctl--primary')}
        <a class="ctl" href="/route/${encodeURIComponent(lane)}?${q}" title="Route page: matrix and charts">Route page</a>
      </div>
      <details class="journal"><summary>Actions on this map (${s.journal.length}) · planner or agent</summary>
        <ol>${recent.map((j) => `<li><span class="who">${esc(j.actor)}</span> ${esc(j.summary || j.action)}</li>`).join('')}</ol>
      </details>
      `;
    }

    // ---------------------------------------------------------------
    // Render
    // ---------------------------------------------------------------
    function render(s, prev) {
      const sel = s.selection;
      if (!sel.id) {
        if (!hub.hidden) {
          hub.hidden = true;
          hubBody.innerHTML = '';
        }
        card.hidden = true;
        return;
      }
      if (hub.hidden) { hub.hidden = false; place(); }

      if (sel.status !== 'ready') {
        $('hub-title').innerHTML = `<h3><span class="mono">${esc(sel.id)}</span></h3>
          <p class="hub-sub">${sel.status === 'error' ? `Could not load: ${esc(sel.error)}` : 'Loading…'}</p>`;
        hubBody.innerHTML = '';
        card.hidden = true;
        return;
      }

      const detailChanged = !prev || prev.selection.detail !== sel.detail;
      const d = sel.detail;
      if (detailChanged) {
        // What to do comes first (the ways round, the split, who can help);
        // what the vehicle is and carries follows.
        hubBody.innerHTML = `${head(d, s)}
          <div id="hub-routes"></div>${vehicle(d)}${load(d)}<div id="hub-split"></div><div id="hub-partners"></div>
          ${logs(d)}
          <div id="hub-foot"></div>`;
      }
      if (detailChanged || prev.routing !== s.routing) $('hub-routes').innerHTML = routes(s);
      if (detailChanged || prev.split !== s.split || prev.routing.data !== s.routing.data
          || prev.routing.chosen !== s.routing.chosen) $('hub-split').innerHTML = split(s);
      if (detailChanged || prev.vendors !== s.vendors) $('hub-partners').innerHTML = partners(s);
      if (detailChanged || prev.journal !== s.journal) $('hub-foot').innerHTML = foot(s);
      renderCard(s);
    }

    function renderCard(s) {
      const v = select.selectedVendor(s);
      if (!v) { card.hidden = true; return; }
      const wasHidden = card.hidden;
      card.hidden = false;
      if (wasHidden) placeCard();
      const c = v.contact || {};
      $('vcard-title').innerHTML = `<h3>${esc(v.name)}</h3>
        <p class="hub-sub">${esc(v.kind.replace(/_/g, ' '))} · ${esc(v.city || '')} · ${Math.round(v.distance_km)} km away${v.synthetic ? ' · synthetic' : ''}</p>`;
      $('vcard-body').innerHTML = `
        <div class="vcontact">
          ${c.email ? `<a class="vreach" href="mailto:${esc(c.email)}?subject=${encodeURIComponent(`Capacity request: ${s.selection.id}`)}" title="${esc(c.email)}" aria-label="Email">${root.MapView.CHANNEL.email}</a>` : ''}
          ${c.phone ? `<a class="vreach" href="tel:${esc(c.phone.replace(/\s+/g, ''))}" title="${esc(c.phone)}" aria-label="Call">${root.MapView.CHANNEL.phone}</a>` : ''}
          ${c.portal && /^https?:\/\//i.test(c.portal) ? `<a class="vreach" href="${esc(c.portal)}" target="_blank" rel="noopener noreferrer" title="${esc(c.portal)}" aria-label="Website">${root.MapView.CHANNEL.portal}</a>` : ''}
          ${!c.phone && !c.email && !c.portal ? '<span class="muted">—</span>' : ''}
        </div>
        ${v.checked_against && /^https?:\/\//i.test(v.checked_against) ? `<p class="chart-note" title="${esc(v.note || '')}">Real operator · checked ${esc(v.checked || '')} · <a href="${esc(v.checked_against)}" target="_blank" rel="noopener noreferrer">source</a> · confirm before booking</p>` : ''}
        <div class="k muted" style="font-size:10px;letter-spacing:.06em">AVAILABLE NOW</div>
        <div class="vcap">${v.capacity ? `${v.capacity.available} ${esc(v.capacity.unit)}` : 'Not on file'}
          <small>${v.capacity ? ` · from ${esc(v.source)}` : ' · capacity unknown, call'}</small></div>
        <p class="chart-note">${esc(v.modes.join(' · '))} · ADR ${v.adr_certified === true ? 'yes' : v.adr_certified === false ? 'no' : 'unknown'} · reefer ${v.reefer === true ? 'yes' : v.reefer === false ? 'no' : 'unknown'} · serves ${Math.round(v.service_radius_km)} km</p>
        <h4 style="font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-top:10px">Serviceable routes</h4>
        <div class="vroutes">${v.serviceable.length ? v.serviceable.map((r) => `
          <div class="vroute ${r.ok ? 'ok' : 'no'}" data-route="${esc(r.route_id)}">
            <span class="rbadge" style="--c:${altColour(r.rank)}">${esc(r.badge || `#${r.rank}`)}</span>
            <span><span class="verdict">${r.ok ? 'Can cover' : 'Cannot'}</span> ${esc(r.label)}</span>
            ${r.reasons.length ? `<span class="why">${esc(r.reasons.join(' · '))}</span>` : ''}
          </div>`).join('') : '<p class="chart-note">No recovery routes to cover for this asset.</p>'}</div>
        <p class="chart-note">Not booked from here. Call to confirm.</p>`;
    }

    // ---------------------------------------------------------------
    // Events — delegated, and every one is a MapAgent call
    // ---------------------------------------------------------------
    hub.addEventListener('mouseover', (e) => {
      const r = e.target.closest('.alt[data-route], .opt-table tr[data-route]:not([data-route=""])');
      if (r) agent.highlightRoute(r.dataset.route);
    });
    hub.addEventListener('mouseout', (e) => {
      const r = e.target.closest('.alt[data-route], .opt-table tr[data-route]:not([data-route=""])');
      if (r && !r.contains(e.relatedTarget)) agent.highlightRoute(null);
    });
    hub.addEventListener('click', (e) => {
      const alt = e.target.closest('.alt[data-route], .opt-table tr[data-route]:not([data-route=""])');
      if (alt) { agent.chooseRoute(alt.dataset.route); return; }
      const vendor = e.target.closest('.partner[data-vendor]');
      if (vendor) { agent.selectVendor(vendor.dataset.vendor); return; }
      const chip = e.target.closest('.chip[data-box]');
      if (chip) {
        const s = store.getState();
        const target = (s.split.data && s.split.data.target) || s.routing.chosen
          || (select.rankedRoutes(s)[0] || {}).id;
        const to = chip.dataset.route === 'ORIGINAL' ? target : 'ORIGINAL';
        if (to) agent.assignContainers([chip.dataset.box], to);
      }
    });
    hub.addEventListener('change', (e) => {
      const t = e.target;
      if (t.dataset && t.dataset.w) {
        const w = {};
        hub.querySelectorAll('input[data-w]').forEach((i) => { w[i.dataset.w] = Number(i.value); });
        agent.rankRoutes(w);
      } else if (t.id === 'split-toggle') {
        agent.toggleSplit(t.checked);
      } else if (t.id === 'split-target') {
        agent.chooseRoute(t.value);
        agent.splitShipment(null, { target: t.value });
      } else if (t.id === 'split-n') {
        const s = store.getState();
        const data = s.split.data;
        if (!data) return;
        const target = hub.querySelector('#split-target').value;
        const byUrgency = data.containers.slice().sort((a, b) =>
          (a.priority === 'critical' ? 0 : 1) - (b.priority === 'critical' ? 0 : 1)
          || a.deadline.localeCompare(b.deadline));
        const n = Number(t.value);
        const allocation = {};
        byUrgency.forEach((c, i) => { allocation[c.container_id] = i < n ? target : 'ORIGINAL'; });
        agent.splitShipment(allocation);
      }
    });
    hub.addEventListener('input', (e) => {
      const t = e.target;
      if (t.dataset && t.dataset.w) t.nextElementSibling.textContent = `${t.value}%`;
      if (t.id === 'split-n') $('split-n-out').textContent = t.value;
    });
    card.addEventListener('mouseover', (e) => {
      const r = e.target.closest('.vroute[data-route]');
      if (r) agent.highlightRoute(r.dataset.route);
    });
    card.addEventListener('mouseout', (e) => {
      const r = e.target.closest('.vroute[data-route]');
      if (r && !r.contains(e.relatedTarget)) agent.highlightRoute(null);
    });

    document.addEventListener('themechange', () => {
      const s = store.getState();
      if (s.selection.detail) render(s, null);
    });

    store.subscribe((s, prev) => render(s, prev));
    return { render };
  }

  // =================================================================
  // Boot: one store, one agent, two renderers
  // =================================================================
  function boot() {
    const { MapStore, MapAgentFactory, MapView } = root;
    if (!MapStore || !MapAgentFactory || !MapView || !$('fleetmap')) return;
    const q = new URLSearchParams(location.search);
    const store = MapStore.createStore();
    const agent = MapAgentFactory.createAgent({ store, fetchJson: MapAgentFactory.fetchJson });
    root.MapAgent = agent;   // the agent hook surface: see mapagent.js

    const select = MapStore.select;
    if (q.get('view') === 'globe') agent.setView('globe', { actor: 'system' });
    MapView.createMapView({ store, agent, select });
    createHub({ store, agent, select });

    agent.loadAssets({
      as_of: q.get('as_of') || null,
      shipments: q.get('shipments') || '150',
    }, { actor: 'system' });

    // ?asset=SYN-0001 opens that asset's Action Hub once the assets are in —
    // the same call a planner's click makes, so the link is shareable.
    const wanted = q.get('asset');
    if (wanted) {
      const off = store.subscribe((s) => {
        if (s.assets.status !== 'ready') return;
        off();
        if (s.assets.byId[wanted]) agent.selectAsset(wanted, { actor: 'link' });
      });
    }
  }

  root.ActionHub = { createHub };
  boot();
})(typeof globalThis !== 'undefined' ? globalThis : this);
