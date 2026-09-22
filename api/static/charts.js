/* Charts shared by the board and the route page.
 *
 * WHY A SHARED FILE. The matrix and the radar are drawn in two places now —
 * the event modal on the board, and the route page. Two copies of the same
 * arithmetic drift, and the day they disagree is the day a planner stops
 * believing both of them. There is one copy, and it lives here.
 *
 * Plain script, not a module: the rest of this front end is plain scripts
 * with no build step, and introducing one bundler for one file would be a
 * worse trade than a namespace object. Everything hangs off `Charts`, and
 * nothing here touches the DOM outside the element it is handed.
 */
'use strict';

const Charts = (() => {
  const $c = (id) => document.getElementById(id);
  const escC = (v) => String(v ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const tokenC = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  /* Band colours read from the stylesheet AT DRAW TIME, not from a table
   * somebody else populated at boot.
   *
   * app.js caches them into BAND_COLOR after reading the palette, which is
   * fine there and wrong here: this file is loaded by the route page too,
   * where that boot never runs — and the failure was silent, an undefined
   * colour producing an empty chart rather than an error anyone would see.
   * Reading the token directly costs nothing and cannot be out of date after
   * a theme switch. */
  const BAND_COLOR = new Proxy({}, {
    get: (_, band) => tokenC(`--band-${String(band)}`),
  });

  function drawRadar(radar, opts = {}) {
    const svg = typeof opts.svg === 'string' ? $c(opts.svg) : (opts.svg || $c('radar'));
    const legendEl = opts.legend === null
      ? null
      : (typeof opts.legend === 'string' ? $c(opts.legend) : (opts.legend || $c('radar-legend')));
    const scale = opts.scale || 1;
    const W = 340 * scale, H = 300 * scale, cx = W / 2, cy = H / 2 + 6 * scale, R = 96 * scale;
    // Geometry scales; font sizes below do not. That is the point of drawing it
    // bigger — the polygon gets room, the labels stay legible instead of
    // ballooning, and a ten-spoke chart stops being a smudge.
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const axes = radar.axes;
    const n = axes.length;

    if (!n) {
      svg.innerHTML = `<text x="${cx}" y="${cy}" text-anchor="middle"
        fill="var(--muted)" font-size="12">No risk variables active on this route</text>`;
      if (legendEl) legendEl.innerHTML = '';
      return;
    }

    const max = Math.max(radar.max, 0.5);
    const bands = ['minor', 'moderate', 'severe'];
    const parts = [];

    // A single axis cannot make a polygon, and two make a line. Below three
    // spokes the radar is drawn as labelled rays — honest, and still readable.
    const angle = (i) => (Math.PI * 2 * i) / Math.max(n, 3) - Math.PI / 2;
    const at = (i, v) => {
      const a = angle(i), rad = (v / max) * R;
      return [cx + Math.cos(a) * rad, cy + Math.sin(a) * rad];
    };

    // rings
    for (let g = 1; g <= 4; g++) {
      const rr = (R * g) / 4;
      if (n >= 3) {
        const pts = axes.map((_, i) => {
          const a = angle(i);
          return `${cx + Math.cos(a) * rr},${cy + Math.sin(a) * rr}`;
        }).join(' ');
        parts.push(`<polygon points="${pts}" fill="none" stroke="var(--grid)" stroke-width="1"/>`);
      } else {
        parts.push(`<circle cx="${cx}" cy="${cy}" r="${rr}" fill="none" stroke="var(--grid)" stroke-width="1"/>`);
      }
    }

    // spokes + labels
    axes.forEach((label, i) => {
      const a = angle(i);
      const ex = cx + Math.cos(a) * R, ey = cy + Math.sin(a) * R;
      parts.push(`<line x1="${cx}" y1="${cy}" x2="${ex}" y2="${ey}" stroke="var(--axis)" stroke-width="1"/>`);

      const lx = cx + Math.cos(a) * (R + 22), ly = cy + Math.sin(a) * (R + 22);
      const anchor = Math.abs(Math.cos(a)) < 0.3 ? 'middle' : (Math.cos(a) > 0 ? 'start' : 'end');
      parts.push(`<text x="${lx}" y="${ly}" text-anchor="${anchor}" dominant-baseline="middle"
        fill="var(--text-secondary)" font-size="11">${escC(label)}</text>`);
    });

    // bands, cumulative so the polygons nest instead of hiding one another
    const cum = axes.map(() => 0);
    bands.forEach((band) => {
      const vals = radar.series[band] || [];
      let any = false;
      vals.forEach((v, i) => { cum[i] += v; if (v > 0) any = true; });
      if (!any) return;

      const snapshot = cum.slice();
      if (n >= 3) {
        const pts = snapshot.map((v, i) => at(i, v).join(',')).join(' ');
        parts.push(`<polygon points="${pts}" fill="${BAND_COLOR[band]}" fill-opacity="0.26"
          stroke="${BAND_COLOR[band]}" stroke-width="2" stroke-linejoin="round"/>`);
      } else {
        snapshot.forEach((v, i) => {
          const [x, y] = at(i, v);
          parts.push(`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"
            stroke="${BAND_COLOR[band]}" stroke-width="7" stroke-linecap="round" opacity="0.72"/>`);
        });
      }
      snapshot.forEach((v, i) => {
        if (v <= 0) return;
        const [x, y] = at(i, v);
        parts.push(`<circle cx="${x}" cy="${y}" r="3.4" fill="${BAND_COLOR[band]}"/>`);
      });
    });

    // value labels on the spoke tips — selective, never one per point
    cum.forEach((v, i) => {
      if (v <= 0) return;
      const [x, y] = at(i, v);
      // Nudge the value label off the spoke so it never sits under the axis
      // name, which collides whenever a value lands near the outer ring.
      const a = angle(i);
      const ox = Math.cos(a) * 13, oy = Math.sin(a) * 13;
      parts.push(`<text x="${x - ox}" y="${y - oy}" text-anchor="middle"
        dominant-baseline="middle"
        fill="var(--text-primary)" font-size="10.5" font-family="var(--mono)">${v.toFixed(1)}d</text>`);
    });

    svg.innerHTML = parts.join('');

    const used = bands.filter((b) => (radar.series[b] || []).some((v) => v > 0));
    if (legendEl) legendEl.innerHTML = used.map((b) =>
      `<span class="rl"><span class="rl-sw" style="background:${BAND_COLOR[b]}"></span>${b}</span>`
    ).join('') || '<span class="rl muted">no contribution</span>';
  }

  /* ------------------------------------------------------------------
   * Family gauges: the same numbers the polygon draws, read at a glance.
   *
   * A radar polygon has to be TRACED to be read — you follow the outline
   * round and compare distances from a centre that is not marked. That is
   * fine for shape ("this lane is weather-shaped") and poor for magnitude,
   * which is the question actually being asked. So each family also gets a
   * row of symbols filled to its intensity, which is read the way a battery
   * icon is read: instantly, and without a legend.
   *
   * A family at zero still gets a row. An empty row says "checked,
   * contributes nothing", which is a different and more useful statement
   * than absence — absence would mean the family was never assessed.
   * ------------------------------------------------------------------ */
  const GAUGE_PIPS = 10;

  const FAMILY_GLYPH = {
    climate: '☁', waterway: '≋', port_ops: '⚓', geopolitical: '⚑',
    labour: '✋', infrastructure: '⌂', force_majeure: '⚠',
    mechanical: '⚙', capacity: '▤', cyber: '⌁',
  };

  function familyGauges(radar, opts = {}) {
    const { empty = 'Nothing in this group is contributing delay.' } = opts;
    const keys = radar.axis_keys || [];
    if (!keys.length) return `<p class="gauge-none">${escC(empty)}</p>`;

    const rows = keys.map((key, i) => {
      const days = (radar.days || [])[i] || 0;
      const level = (radar.intensity || [])[i] || 0;
      const band = (radar.dominant || [])[i];
      const lit = days > 0 ? Math.max(1, Math.round(level * GAUGE_PIPS)) : 0;

      const pips = Array.from({ length: GAUGE_PIPS }, (_, k) =>
        `<i class="pip${k < lit ? ' pip--on' : ''}"></i>`).join('');

      return `
        <div class="gauge${days > 0 ? '' : ' gauge--quiet'}"
             style="${band ? `--pip: ${BAND_COLOR[band]};` : ''}">
          <span class="gauge__icon" aria-hidden="true">${FAMILY_GLYPH[key] || '◆'}</span>
          <span class="gauge__name">${escC((radar.axes || [])[i] || key)}</span>
          <span class="gauge__pips" role="img"
                aria-label="${days > 0 ? `${days} days of delay` : 'no contribution'}"
                >${pips}</span>
          <span class="gauge__val">${days > 0 ? `${days.toFixed(1)} d` : '—'}</span>
        </div>`;
    });

    const quiet = keys.length - rows.filter((_, i) =>
      ((radar.days || [])[i] || 0) > 0).length;
    return rows.join('') + (quiet
      ? `<p class="gauge-foot">${quiet} more checked, contributing nothing.</p>`
      : '');
  }

  function impactRows(grid) {
    // Declared most severe first; the grid draws worst at the top.
    return grid.impact_bands;
  }

  function dots(n, solidCount) {
    // Capped: past a handful the count is the information, not the arrangement.
    const shown = Math.min(n, 6);
    let out = '';
    for (let i = 0; i < shown; i += 1) {
      out += `<i class="mx-dot mx-dot--${i < solidCount ? 'solid' : 'hollow'}"></i>`;
    }
    if (n > shown) out += `<span class="mx-more">+${n - shown}</span>`;
    return out;
  }

  /* A risk matrix's colour is its POSITION, not its contents.
   *
   * The old rule tinted every cell by the CHF sitting in it, on one pale
   * hue. That fails in the case that matters most: the busiest cell on the
   * board holds 193 consignments whose expected loss rounds to zero —
   * small shipments, unsourced probability — so it rendered as an empty
   * white box. A planner reading that sees nothing there. There is a great
   * deal there.
   *
   * So the two questions are answered by two channels:
   *
   *   HUE     where the cell sits in the grid. Bottom-left green, top-right
   *           red, exactly as a risk matrix has always been read. This is a
   *           property of the cell and never changes, so an empty
   *           high-impact cell still shows as the place you do not want
   *           anything to appear.
   *   FILL    how loaded the cell is, from both the count and the money —
   *           whichever says more. An occupied cell is never invisible.
   */
  const MX_RAMP = ['--mx-low', '--mx-mid', '--mx-high', '--mx-top'];

  function cellRisk(rowIndex, rowCount, colIndex, colCount) {
    // Impact rows arrive worst-first, so invert to get "how bad" upward.
    const impact = rowCount > 1 ? (rowCount - 1 - rowIndex) / (rowCount - 1) : 1;
    const likelihood = colCount > 1 ? colIndex / (colCount - 1) : 0.5;
    return (impact * 0.6) + (likelihood * 0.4);
  }

  function cellTint(chf, worst, opts = {}) {
    const { n = 0, mostN = 0, risk = 0.5 } = opts;
    if (!n) return '';

    // Whichever channel is louder decides the weight. Money usually is; on a
    // long tail of small consignments the count is the only thing there.
    const byMoney = worst > 0 ? Math.sqrt(chf / worst) : 0;
    const byCount = mostN > 0 ? Math.sqrt(n / mostN) : 0;
    // Floored, because "occupied" is itself information. A cell with one
    // consignment in it must not look like a cell with none.
    const weight = Math.max(0.22, Math.min(1, Math.max(byMoney, byCount)));

    const band = MX_RAMP[Math.min(
      MX_RAMP.length - 1, Math.floor(risk * MX_RAMP.length)
    )];
    return `background: color-mix(in srgb, var(${band}) ` +
           `${(weight * 82).toFixed(0)}%, transparent);`;
  }

  function buildMatrixGrid(event, board) {
    const grid = board.matrix_grid;
    const m = event.matrix;
    const rows = impactRows(grid);
    const cols = grid.probability_bands;

    const count = new Map(), solid = new Map(), money = new Map();
    for (const pt of m.points) {
      const key = `${pt.impact_band}|${pt.probability_band}`;
      count.set(key, (count.get(key) || 0) + 1);
      money.set(key, (money.get(key) || 0) + (pt.expected_loss_chf || 0));
      if (pt.ring === 'solid') solid.set(key, (solid.get(key) || 0) + 1);
    }
    const worst = Math.max(0, ...money.values());
    const mostN = Math.max(0, ...count.values());
    const unsourced = m.points.filter((p) => p.p_late === null);

    const cell = (rowId, rowIndex, colId, colIndex, opts = {}) => {
      const { unsourced: isUnsourced = false } = opts;
      const key = `${rowId}|${colId}`;
      const n = count.get(key) || 0;
      const s = solid.get(key) || 0;
      const chf = money.get(key) || 0;
      // The unsourced column is off the likelihood axis entirely — it is the
      // gutter for events nobody can put a number on. It takes its rating
      // from impact alone rather than borrowing a column position it does
      // not have, but it is still tinted: on a lane where every event is
      // unsourceable it holds the whole picture, and leaving it untinted was
      // how the matrix came to show nothing at all.
      const risk = isUnsourced
        ? cellRisk(rowIndex, rows.length, 0, 1)
        : cellRisk(rowIndex, rows.length, colIndex, cols.length);
      // A cell where options have ALREADY closed is outlined, whatever it is
      // worth. Money you can still act on and money you cannot are different
      // problems, and the ramp alone cannot say which this is.
      const lost = n > s;
      const why = n
        ? `${n} shipment(s) · CHF ${Math.round(chf).toLocaleString()} expected loss`
          + (lost ? ' · some options already closed' : '')
          + (isUnsourced ? ' · probability not sourceable' : '')
        : 'empty';
      return `<td class="mx-cell${n ? ' has' : ''}${lost ? ' mx-cell--lost' : ''}${isUnsourced ? ' mx-unsourced' : ''}"
                  style="${cellTint(chf, worst, { n, mostN, risk })}"
                  title="${escC(why)}">
                ${n ? dots(n, s) : ''}
                ${chf ? `<span class="mx-chf">${shortChf(chf)}</span>` : ''}
              </td>`;
    };

    const unsourcedId = grid.unsourced_band.id;

    return `
      <table class="mx-grid mx-grid--big">
        <thead>
          <tr>
            <th class="mx-corner"><span>impact if late</span></th>
            ${cols.map((c) => `<th>${escC(c.label)}</th>`).join('')}
            ${unsourced.length ? `<th class="mx-unsourced-h">${escC(grid.unsourced_band.label)}</th>` : ''}
          </tr>
        </thead>
        <tbody>
          ${rows.map((r, ri) => `
            <tr>
              <th class="mx-row" title="${escC(r.action)}">${escC(r.label)}</th>
              ${cols.map((c, ci) => cell(r.id, ri, c.id, ci)).join('')}
              ${unsourced.length
                ? cell(r.id, ri, unsourcedId, 0, { unsourced: true })
                : ''}
            </tr>`).join('')}
        </tbody>
        <tfoot>
          <tr><td></td>
            <td colspan="${cols.length + (unsourced.length ? 1 : 0)}" class="mx-xaxis">
              P(this shipment is late) →
            </td></tr>
        </tfoot>
      </table>`;
  }

  function shortChf(n) {
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(Math.round(n));
  }

  return { drawRadar, familyGauges, impactRows, dots, cellTint,
           buildMatrixGrid, shortChf };
})();
