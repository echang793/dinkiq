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
    const pts = await api('/progress' + playerQS());
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
