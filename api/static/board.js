/* The solution board: three or four ways to move a lane's freight.
 *
 * The options list above it answers "what do I do about THIS consignment".
 * This answers the question a lane manager actually has when a corridor
 * closes, which is "there are four hundred of them and one river — what is
 * the mix?".
 *
 * Big tabs on purpose. Each plan is an argument, not a row: a thesis that
 * says why you would pick it, a coverage bar that says what it leaves
 * behind, and the fleet it needs against the fleet that exists. A planner
 * choosing between four of these is making a judgement, and a judgement
 * needs the reasoning on screen, not folded behind a chevron.
 */
'use strict';

const Board = (() => {
  const PLAN_ICONS = {
    consolidate: '▤',
    fastest: '⚡',
    balanced: '⚖',
    triage: '◐',
  };

  const UNIT_ICONS = { barge: '⛴', rail: '▭', road: '▬', sea: '⛴' };

  function pct(value) {
    return `${Math.round((value || 0) * 100)}%`;
  }

  /* The fleet bar: what this plan books against what the corridor has.
   * Drawn as units rather than tonnes because units are what runs out —
   * a planner short of drivers is not short of tonnage. */
  function fleetHTML(row) {
    const share = row.units_available
      ? Math.min(1, row.units / row.units_available)
      : 0;
    const tight = share >= 0.999;
    return `
      <div class="alloc${tight ? ' alloc--tight' : ''}">
        <div class="alloc__head">
          <span class="alloc__mode">
            <span class="alloc__icon">${UNIT_ICONS[row.mode] || '▬'}</span>
            ${FastUI.esc(row.mode)}
          </span>
          <span class="alloc__units">
            <strong>${row.units}</strong> of ${row.units_available}
            ${FastUI.esc(row.unit_name_available || row.unit_name)}
          </span>
        </div>
        <div class="alloc__bar" role="img"
             aria-label="${row.units} of ${row.units_available} ${FastUI.esc(row.unit_name_available || row.unit_name)} used">
          <span style="width:${(share * 100).toFixed(1)}%"></span>
        </div>
        <div class="alloc__foot">
          <span>${row.tonnes.toLocaleString()} t · ${row.shipments} consignments</span>
          <span>ready ${FastUI.hoursShort(row.hours_to_ready)}, last away
            ${FastUI.hoursShort(row.hours_to_last_away)}</span>
          <span class="num">${row.cost_chf > 0 ? `+${FastUI.chf(row.cost_chf)}` : 'no premium'}</span>
        </div>
      </div>`;
  }

  /* Coverage as one bar with the deferred remainder still on it. A plan that
   * shows only what it moves is the plan that forgets what it did not. */
  function coverageHTML(plan) {
    const moved = Math.max(0, Math.min(1, plan.coverage));
    return `
      <div class="cover">
        <div class="cover__bar">
          <span class="cover__moved" style="width:${(moved * 100).toFixed(1)}%"></span>
          <span class="cover__left" style="width:${((1 - moved) * 100).toFixed(1)}%"></span>
        </div>
        <div class="cover__legend">
          <span><i class="sw sw--moved"></i>${plan.covered_tonnes.toLocaleString()} t
            moving (${pct(plan.coverage)})</span>
          ${plan.deferred_tonnes > 0
            ? `<span><i class="sw sw--left"></i>${plan.deferred_tonnes.toLocaleString()} t
                 waiting (${plan.deferred.length} consignments)</span>`
            : '<span class="muted">nothing left behind</span>'}
        </div>
      </div>`;
  }

  /* What the disruption has taken, as a phrase rather than a verdict.
     A mode at 53% is neither open nor shut, and the header has to be able
     to say so — it is the commonest state and the one worth acting on. */
  function pressureHTML(data) {
    const parts = [];
    (data.blocked_modes || []).forEach(
      (m) => parts.push(`${FastUI.esc(m)} gone`));
    Object.entries(data.derated_modes || {}).forEach(
      ([m, share]) => parts.push(
        `${FastUI.esc(m)} at ${Math.round(share * 100)}%`));
    return parts.length ? `· ${parts.join(', ')}` : '· no mode lost, re-mixing for time';
  }

  function planPanel(plan, index) {
    const facts = [
      ['Moves', pct(plan.coverage), `${plan.covered_tonnes.toLocaleString()} of
        ${plan.displaced_tonnes.toLocaleString()} t`],
      ['First freight away', FastUI.hoursShort(plan.hours_to_first_move),
        'from now'],
      ['Worst case', plan.worst_days_late > 0
        ? `${plan.worst_days_late.toFixed(1)} d late` : 'on the date',
        `${plan.late_shipments} consignment(s) affected`],
      ['Extra cost', plan.extra_cost_chf > 0
        ? FastUI.chf(plan.extra_cost_chf) : 'none',
        'above the planned move'],
    ];

    return `
      <section class="plan" id="plan-${FastUI.esc(plan.plan_id)}"
               role="tabpanel" aria-labelledby="tab-${FastUI.esc(plan.plan_id)}" hidden>
        <div class="plan__title">
          <h4>${FastUI.esc(plan.label)}</h4>
          ${index === 0
            ? `<span class="pick" title="Coverage first, then lateness, then
                 cost — the same rule the options list uses">Best on delivery</span>`
            : ''}
        </div>
        <p class="plan__thesis">${FastUI.esc(plan.thesis)}</p>

        ${plan.also.length
          ? `<p class="plan__also">Every other mix lands here too
             (${plan.also.map(FastUI.esc).join(', ')}) — this corridor leaves
             one answer.</p>`
          : ''}

        ${coverageHTML(plan)}

        <dl class="facts facts--plan">
          ${facts.map(([label, value, note]) => `
            <div class="fact">
              <dt>${label}</dt>
              <dd>${FastUI.esc(String(value))}<span class="note">${FastUI.esc(note)}</span></dd>
            </div>`).join('')}
        </dl>

        <h4 class="plan__h">The mix</h4>
        ${plan.allocations.length
          ? plan.allocations.map(fleetHTML).join('')
          : `<p class="note-row">Nothing can be loaded inside this window.
             Every mode needs more notice than the tightest consignment has
             left.</p>`}

        ${plan.limits.length
          ? `<div class="plan__limits">
               <h4 class="plan__h">What runs out</h4>
               <ul>${plan.limits.map((l) => `<li>${FastUI.esc(l)}</li>`).join('')}</ul>
             </div>`
          : ''}

        ${plan.deferred.length
          ? `<details class="fold fold--inline">
               <summary>Waiting for next week (${plan.deferred.length})</summary>
               <div class="fold__body">
                 <table class="mini">
                   <tr><th>Consignment</th><th>Customer</th>
                       <th class="num">Tonnes</th><th class="num">Days late</th></tr>
                   ${plan.deferred.slice(0, 25).map((d) => `
                     <tr><td>${FastUI.esc(d.shipment_id)}</td>
                         <td>${FastUI.esc(d.customer)}</td>
                         <td class="num">${d.tonnes}</td>
                         <td class="num">${d.days_late.toFixed(1)}</td></tr>`).join('')}
                 </table>
                 ${plan.deferred.length > 25
                   ? `<p class="note-row">…and ${plan.deferred.length - 25} more.</p>`
                   : ''}
               </div>
             </details>`
          : ''}

        <div class="plan__money">
          <span>
            Contribution left after this plan:
            <strong>${plan.margin_chf === null ? '—' : FastUI.chf(plan.margin_chf)}</strong>
          </span>
          ${plan.unprofitable_shipments > 0
            ? `<span class="plan__warn">${plan.unprofitable_shipments}
               consignment(s) go under water to hold this — the block still
               pays, they do not.</span>`
            : '<span class="muted">No consignment is sold at a loss.</span>'}
          ${plan.margin_viable === false
            ? `<span class="plan__veto">Vetoed: ${FastUI.esc(plan.vetoed_because || '')}</span>`
            : ''}
        </div>
      </section>`;
  }

  /* ``first`` and ``active`` are the same thing on load and stop being the
     same thing the moment a planner clicks elsewhere — the badge marks what
     the ranking chose, not what is currently open. */
  function tabHTML(plan, active, first) {
    const flags = [];
    // "short" and "at the ceiling" are different facts. A plan that books the
    // last truck on the corridor and still lands everything on the date has
    // hit a ceiling; it has not come up short, and labelling it so sends a
    // planner looking for a problem that is not there.
    if (!plan.feasible) flags.push('leaves freight behind');
    else if ((plan.at_ceiling || []).length) flags.push('at the ceiling');
    if (plan.margin_viable === false) flags.push('loses money');
    return `
      <button type="button" class="ptab${active ? ' ptab--on' : ''}"
              id="tab-${FastUI.esc(plan.plan_id)}" role="tab"
              aria-selected="${active}" aria-controls="plan-${FastUI.esc(plan.plan_id)}"
              data-plan="${FastUI.esc(plan.plan_id)}">
        <span class="ptab__icon">${PLAN_ICONS[plan.plan_id] || '◆'}</span>
        <span class="ptab__body">
          <span class="ptab__label">${FastUI.esc(plan.label)}${first
            ? '<span class="ptab__pick">best</span>' : ''}</span>
          <span class="ptab__line">${FastUI.esc(plan.sentence)}</span>
          <span class="ptab__nums">
            <b>${pct(plan.coverage)}</b> moved
            · ${plan.worst_days_late > 0
                 ? `${plan.worst_days_late.toFixed(1)} d late`
                 : 'on the date'}
            · ${plan.extra_cost_chf > 0 ? FastUI.chf(plan.extra_cost_chf) : 'no premium'}
            ${flags.length ? `· <em>${flags.join(', ')}</em>` : ''}
          </span>
        </span>
      </button>`;
  }

  function vendorsHTML(vendors) {
    if (!vendors.length) return '';
    return `
      <details class="fold">
        <summary>Capacity you can buy in (${vendors.length})</summary>
        <div class="fold__body">
          <p class="note-row">Not allocated against any plan above: a 3PL is
            capacity you do not own, so it cannot be booked against a declared
            corridor ceiling. This is what you reach for when the ceilings do
            not add up.</p>
          <table class="mini">
            <tr><th>Vendor</th><th>Service</th><th>Where</th>
                <th class="num">Ready in</th><th class="num">Cost</th><th>Call</th></tr>
            ${vendors.map((v) => `
              <tr><td>${FastUI.esc(v.vendor)}</td>
                  <td>${FastUI.esc(v.service)}</td>
                  <td>${FastUI.esc(v.at)} (${v.distance_km} km)</td>
                  <td class="num">${FastUI.hoursShort(v.hours_to_handover)}</td>
                  <td class="num">${FastUI.chf(v.cost_chf)}</td>
                  <td class="mono">${FastUI.esc(v.phone)}</td></tr>`).join('')}
          </table>
        </div>
      </details>`;
  }

  function render(host, data) {
    if (!data || data.quiet || !data.plans.length) {
      host.innerHTML = `
        <div class="sboard sboard--quiet">
          <h3>Capacity</h3>
          <p class="note-row">${FastUI.esc(
            (data && data.sentence) || 'Nothing on this lane is displaced.')}</p>
        </div>`;
      return;
    }

    host.innerHTML = `
      <div class="sboard">
        <div class="sboard__head">
          <h3>How the freight actually moves</h3>
          <p class="sboard__sub">
            ${data.displaced_shipments} consignments,
            <strong>${data.displaced_tonnes.toLocaleString()} t</strong> displaced
            ${pressureHTML(data)}
            · window ${FastUI.hoursShort(data.horizon_hours)}
          </p>
          ${data.replacement
            ? `<p class="sboard__swap">${FastUI.esc(data.replacement)}</p>`
            : ''}
        </div>
        <div class="ptabs" role="tablist" aria-label="Capacity plans">
          ${data.plans.map((p, i) => tabHTML(p, i === 0, i === 0)).join('')}
        </div>
        <div class="sboard__panels">
          ${data.plans.map((p, i) => planPanel(p, i)).join('')}
        </div>
        ${vendorsHTML(data.vendors || [])}
      </div>`;

    const first = host.querySelector('.plan');
    if (first) first.hidden = false;

    host.querySelectorAll('.ptab').forEach((tab) => {
      tab.addEventListener('click', () => select(host, tab.dataset.plan));
    });
  }

  function select(host, planId) {
    host.querySelectorAll('.ptab').forEach((tab) => {
      const on = tab.dataset.plan === planId;
      tab.classList.toggle('ptab--on', on);
      tab.setAttribute('aria-selected', String(on));
    });
    host.querySelectorAll('.plan').forEach((panel) => {
      panel.hidden = panel.id !== `plan-${planId}`;
    });
  }

  async function load(host, routeId) {
    host.innerHTML = `
      <div class="sboard sboard--loading">
        <h3>How the freight actually moves</h3>
        <p class="note-row">Working out the mix…</p>
      </div>`;
    try {
      render(host, await FastUI.get(
        `/api/v2/board/${encodeURIComponent(routeId)}`));
    } catch (err) {
      host.innerHTML = `
        <div class="sboard sboard--quiet">
          <h3>Capacity</h3>
          <p class="note-row">Could not work out the mix:
            ${FastUI.esc(err.message)}</p>
        </div>`;
    }
  }

  return { load, render };
})();
