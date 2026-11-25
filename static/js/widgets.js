'use strict';
/* ---------------- data ribbon ---------------- */
async function buildRibbon(){
  // Was a hardcoded constant shown to every user regardless of whether
  // they'd ever uploaded a match — a brand-new account saw confident fake
  // stats ("Sessions analyzed 3") about play that never happened. Pull the
  // real numbers from the most recent completed session + cross-session
  // progress instead, and show an honest empty state when there's none yet.
  const t = $('ribtrack');
  if (!t) return;
  let items = null;
  try {
    const sessions = await api('/sessions');
    const done = sessions.filter(s => s.stage === 'done');
    const pts = await api('/progress');
    if (done.length) {
      const full = await api('/session/' + done[0].session_id);  // newest first
      const m = full.metrics || {}, ev = full.events || {}, sh = full.shots || {}, dp = full.dupr || {};
      const rated = pts.filter(p => p.dupr_band != null);
      const delta = rated.length > 1 ? rated[rated.length - 1].dupr_band - rated[0].dupr_band : null;
      items = [
        ['Latest estimate', dp.available ? '~' + dp.band + ' DUPR' : '—'],
      ];
      if (delta != null) {
        items.push(['Trend', '<span class="' + (delta >= 0 ? 'up' : 'dn') + '">' + (delta >= 0 ? '▲' : '▼') + ' ' +
          Math.abs(delta).toFixed(2) + '</span> since first session']);
      }
      items.push(
        ['Sessions analyzed', String(pts.length)],
        ['Kitchen time', (m.zone_pct && m.zone_pct.kitchen != null) ? m.zone_pct.kitchen + '%' : '—'],
        ['Longest rally', ev.max_rally_hits != null ? ev.max_rally_hits + ' hits' : '—'],
        ['Deep serves', sh.deep_serve_pct != null ? sh.deep_serve_pct + '%' : '—'],
        ['Distance last match', m.distance_ft != null ? Math.round(m.distance_ft).toLocaleString() + ' ft' : '—'],
        ['Court coverage', m.coverage_pct != null ? m.coverage_pct + '%' : '—'],
      );
    }
  } catch (e) { /* leave items null -> empty state below */ }
  if (!items || !items.length) {
    t.innerHTML = '<div class="ribitem"><span class="rlabel">Get started</span><span class="rval">Upload your first match to see your stats here</span></div>';
    return;
  }
  const group = items.map(([l, v]) =>
    '<div class="ribitem"><span class="rlabel">' + l + '</span><span class="rval">' + v + '</span></div>' +
    '<div class="ribsep"><span class="dot"></span></div>').join('');
  t.innerHTML = group + group;
}

/* ---------------- radar chart ---------------- */
function radarSVG(dims) {
  const ds = Object.values(dims || {});
  if (ds.length < 3) return '';
  const S = 210, C = S / 2, R = 68;
  const pt = (i, f) => { const a = -Math.PI / 2 + i * 2 * Math.PI / ds.length; return [C + Math.cos(a) * R * f, C + Math.sin(a) * R * f]; };
  const ringPts = f => ds.map((_, i) => pt(i, f).map(v => v.toFixed(1)).join(',')).join(' ');
  const rings = [.33, .66, 1].map((f, ri) => '<polygon class="ring" style="animation-delay:' + (ri * .12) + 's" points="' + ringPts(f) + '"/>').join('');
  const axes = ds.map((_, i) => { const p = pt(i, 1); return '<line class="axis" x1="' + C + '" y1="' + C + '" x2="' + p[0].toFixed(1) + '" y2="' + p[1].toFixed(1) + '"/>'; }).join('');
  const val = d => Math.max(.12, Math.min(1, (d.band - DUPR_BAND_MIN) / DUPR_BAND_SPAN));
  const poly = '<polygon class="poly" points="' + ds.map((d, i) => pt(i, val(d)).map(v => v.toFixed(1)).join(',')).join(' ') + '"/>';
  const dots = ds.map((d, i) => { const p = pt(i, val(d)); return '<circle class="pt" cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="3.5" style="animation-delay:' + (.6 + i * .08) + 's"/>'; }).join('');
  const labels = ds.map((d, i) => {
    const p = pt(i, 1.22);
    const anch = Math.abs(p[0] - C) < 10 ? 'middle' : p[0] > C ? 'start' : 'end';
    return '<text x="' + p[0].toFixed(1) + '" y="' + (p[1] + 3).toFixed(1) + '" text-anchor="' + anch + '">' + esc(d.label.split(' ')[0]) + '</text>';
  }).join('');
  const sweep = '<g class="sweep"><line x1="' + C + '" y1="' + C + '" x2="' + C + '" y2="' + (C - R) + '" style="stroke:var(--accent);stroke-width:2"/></g>';
  return '<svg class="radar" width="' + S + '" height="' + S + '" viewBox="0 0 ' + S + ' ' + S + '">' + rings + axes + sweep + poly + dots + labels + '</svg>';
}
