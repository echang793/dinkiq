'use strict';
/* ---------------- Trends page ---------------- */
async function loadTrendsPage() {
  let pts;
  try { pts = await api('/progress' + playerQS()); } catch (e) { return; }
  loadLeaderboard();
  loadStreak();
  if (!pts.length) {
    $('trendSummary').innerHTML = '<div class="empty">Complete a session to build your trends.</div>';
    $('trendChartBig').innerHTML = ''; $('trendTableBig').innerHTML = ''; return;
  }
  const rated = pts.filter(p => p.dupr_band != null);
  const last = rated[rated.length - 1], first = rated[0];
  const delta = rated.length > 1 ? last.dupr_band - first.dupr_band : 0;
  const avgKit = pts.reduce((a,p) => a + (p.kitchen_pct||0), 0) / pts.length;
  const totDist = pts.reduce((a,p) => a + (p.distance_ft||0), 0);
  $('trendSummary').innerHTML =
    statCard(pts.length, 'sessions analyzed') +
    statCard((rated.length ? '~' + last.dupr_band : '—'), 'latest estimate') +
    statCard((delta >= 0 ? '+' : '') + delta.toFixed(2), 'change since first') +
    statCard(avgKit.toFixed(0) + '<small>%</small>', 'avg kitchen time') +
    statCard(Math.round(totDist).toLocaleString() + '<small> ft</small>', 'total distance');
  animateStats($('trendSummary'));
  const cb = $('trendChartBig');
  cb.innerHTML = rated.length > 1 ? trendChart(rated) : '<div class="empty">Two rated sessions needed to draw a line.</div>';
  const path = cb.querySelector('#trendLine');
  if (path) { const L = path.getTotalLength(); path.style.strokeDasharray = L; path.style.strokeDashoffset = L;
    requestAnimationFrame(() => requestAnimationFrame(() => { path.style.transition = 'stroke-dashoffset 1.5s cubic-bezier(.22,1,.36,1)'; path.style.strokeDashoffset = 0; })); }
  const rows = pts.slice().reverse().map(p =>
    '<tr><td class="num">' + esc(p.date || '') + '</td>' +
    '<td>' + esc(p.label || p.session_id) + '</td>' +
    '<td class="dupr">' + (p.dupr_band != null ? '~' + p.dupr_band : '—') + (p.known_dupr != null ? ' <span style="color:var(--dim);font:400 11px Archivo">(known ' + p.known_dupr + ')</span>' : '') + '</td>' +
    '<td class="num"><span class="zbar"><i style="width:' + Math.min(100, p.kitchen_pct || 0) + '%"></i></span>' + (p.kitchen_pct != null ? p.kitchen_pct + '%' : '—') + '</td>' +
    '<td class="num">' + (p.avg_rally_hits != null ? p.avg_rally_hits : '—') + '</td>' +
    '<td class="num">' + (p.distance_ft != null ? Math.round(p.distance_ft).toLocaleString() + ' ft' : '—') + '</td></tr>').join('');
  $('trendTableBig').innerHTML = '<table class="trend" style="min-width:520px"><tr><th>Date</th><th>Session</th><th>~DUPR</th><th>Kitchen time</th><th>Rally avg</th><th>Distance</th></tr>' + rows + '</table>';

  const weakCard = $('weakTrendCard');
  const flagged = pts.filter(p => p.weakest_dimension);
  if (flagged.length) {
    weakCard.style.display = 'block';
    const tally = {};
    flagged.forEach(p => { tally[p.weakest_label] = (tally[p.weakest_label] || 0) + 1; });
    const ranked = Object.entries(tally).sort((a, b) => b[1] - a[1]);
    const max = ranked[0][1];
    $('weakTrendBars').innerHTML = ranked.map(([label, n]) =>
      '<div class="bar"><span class="lb">' + esc(label) + '</span>' +
      '<div class="track"><div class="fill" data-w="' + Math.round(100 * n / max) + '%" style="background:var(--amber)"></div></div>' +
      '<span class="val">' + n + '/' + flagged.length + '</span></div>').join('');
    animateBars($('weakTrendBars'));
    const top = ranked[0];
    let note = top[1] > 1
      ? top[0] + ' has been your weakest dimension in ' + top[1] + ' of your last ' + flagged.length + ' rated sessions — worth targeting.'
      : 'No single dimension has repeated as your weakest yet — keep playing to build a pattern.';
    // "did it work?" — once a dimension's been flagged weak more than once,
    // check whether it's actually gotten better since the first flag
    const dimKey = (flagged.find(p => p.weakest_label === top[0]) || {}).weakest_dimension;
    const firstFlagged = dimKey && flagged.find(p => p.weakest_dimension === dimKey);
    const withDim = dimKey ? pts.filter(p => p.dimensions && p.dimensions[dimKey]) : [];
    const latestWithDim = withDim[withDim.length - 1];
    if (top[1] > 1 && firstFlagged && latestWithDim && firstFlagged.dimensions &&
        firstFlagged.dimensions[dimKey]) {
      const before = firstFlagged.dimensions[dimKey].band, after = latestWithDim.dimensions[dimKey].band;
      if (after - before >= 0.25) {
        note += ' Good news: it\'s improved from ' + before.toFixed(2) + ' to ' + after.toFixed(2) +
          ' since ' + firstFlagged.date + '.';
      }
    }
    $('weakTrendNote').textContent = note;
  } else {
    weakCard.style.display = 'none';
  }
}

async function loadLeaderboard() {
  let data;
  try { data = await api('/leaderboard'); } catch (e) { return; }
  const rows = (data && data.players) || [];
  const card = $('leaderboardCard');
  if (rows.length < 2) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  $('leaderboardTable').innerHTML = '<table class="trend" style="min-width:480px"><tr>' +
    '<th>#</th><th>Player</th><th>Sessions</th><th>Latest ~DUPR</th><th>Best ~DUPR</th><th>Win %</th></tr>' +
    rows.map((r, i) =>
      '<tr><td class="num">' + (i + 1) + '</td>' +
      '<td>' + esc(r.player) + '</td>' +
      '<td class="num">' + r.sessions + '</td>' +
      '<td class="dupr">' + (r.latest_band != null ? '~' + r.latest_band : '—') + '</td>' +
      '<td class="dupr">' + (r.best_band != null ? '~' + r.best_band : '—') + '</td>' +
      '<td class="num">' + (r.win_pct != null ? r.win_pct + '%' : '—') + '</td></tr>').join('') +
    '</table>';
}
async function loadStreak() {
  let s;
  try { s = await api('/streak' + playerQS()); } catch (e) { return; }
  const card = $('streakCard');
  if (!s || !s.days_played) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  $('streakStats').innerHTML =
    statCard(s.current_streak_days + '<small> day' + (s.current_streak_days === 1 ? '' : 's') + '</small>', 'current streak') +
    statCard(s.longest_streak_days + '<small> day' + (s.longest_streak_days === 1 ? '' : 's') + '</small>', 'longest streak') +
    statCard(s.sessions_this_week, 'sessions this week');
  animateStats($('streakStats'));
}

/* ---------------- trend ---------------- */
async function loadFocusReminder() {
  const box = $('focusReminder');
  let pts;
  try { pts = await api('/progress' + playerQS()); } catch (e) { box.style.display = 'none'; return; }
  const last = pts[pts.length - 1];
  const dims = (last && last.dimensions) || {};
  // 3.5 mirrors feedback.WEAK_BAND -- same cutoff the drills/tips already use,
  // so this checklist always agrees with what the last session's coach panel flagged
  const weak = Object.values(dims).filter(d => d.band < 3.5).sort((a, b) => a.band - b.band).slice(0, 3);
  if (!weak.length) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  box.innerHTML = '<div class="mut" style="font-size:12px;margin-bottom:6px">Before you start — focus areas from your last session:</div>' +
    weak.map(d => {
      const target = d.next_target_value != null
        ? ' — reach ' + d.next_target_value + (d.unit || '') + ' for the next tier (' + d.next_target_band.toFixed(2) + ')'
        : '';
      return '<div class="coach">' + tipSVG + '<span>Work on <b>' + esc(d.label) + '</b>' + target + '</span></div>';
    }).join('');
}
async function loadTrend() {
  let pts;
  try { pts = await api('/progress' + playerQS()); } catch (e) { return; }
  window._pcProg = pts;
  const w = $('trendWrap');
  if (!pts.length) { w.innerHTML = '<div class="empty">Complete a session to start your trend line.</div>'; return; }
  const rated = pts.filter(p => p.dupr_band != null);
  const first = rated[0], last = rated[rated.length - 1];
  const delta = rated.length > 1 ? last.dupr_band - first.dupr_band : 0;
  const head = '<div class="trendhead">' +
    '<span class="n">' + pts.length + '</span><span class="l">sessions<br>analyzed</span>' +
    (rated.length ? '<span class="n" style="color:var(--ink)">~' + last.dupr_band + '</span><span class="l">latest<br>estimate</span>' : '') +
    (rated.length > 1 ? '<span class="delta ' + (delta >= 0 ? 'up' : 'dn') + '">' + (delta >= 0 ? '▲' : '▼') + ' ' + Math.abs(delta).toFixed(2) + ' since first session</span>' : '') +
    '</div>';
  const chart = rated.length > 1 ? trendChart(rated) : '';
  const rows = pts.slice().reverse().map(p =>
    '<tr><td class="num">' + esc(p.date || '') + '</td>' +
    '<td>' + esc(p.label || p.session_id) + '</td>' +
    '<td class="dupr">' + (p.dupr_band != null ? '~' + p.dupr_band : '—') + (p.known_dupr != null ? ' <span style="color:var(--dim);font:400 11px Archivo">(known ' + p.known_dupr + ')</span>' : '') + '</td>' +
    '<td class="num"><span class="zbar"><i style="width:' + Math.min(100, p.kitchen_pct || 0) + '%"></i></span>' + (p.kitchen_pct != null ? p.kitchen_pct + '%' : '—') + '</td>' +
    '<td class="num">' + (p.avg_rally_hits != null ? p.avg_rally_hits : '—') + '</td>' +
    '<td class="num">' + (p.distance_ft != null ? Math.round(p.distance_ft).toLocaleString() + ' ft' : '—') + '</td></tr>').join('');
  w.innerHTML = head + chart +
    '<div class="ttscroll"><table class="trend"><tr><th>Date</th><th>Session</th><th>~DUPR</th><th>Kitchen time</th><th>Rally avg</th><th>Distance</th></tr>' + rows + '</table></div>';
  const path = w.querySelector('#trendLine');
  if (path) { const L = path.getTotalLength(); path.style.strokeDasharray = L; path.style.strokeDashoffset = L;
    requestAnimationFrame(() => requestAnimationFrame(() => { path.style.transition = 'stroke-dashoffset 1.5s cubic-bezier(.22,1,.36,1)'; path.style.strokeDashoffset = 0; })); }
  decorateSparks(pts);
}
function trendChart(pts) {
  const W = 640, H = 140, PX = 16, PY = 18;
  const bands = pts.map(p => p.dupr_band);
  const known = pts.filter(p => p.known_dupr != null);
  // scale to whichever is wider: video estimates cap around 5.0, but a
  // user's real known DUPR can run well past that — without this, a known
  // DUPR above the estimate range plots off-chart with no indication
  const allVals = bands.concat(known.map(p => p.known_dupr));
  const lo = Math.min(...allVals) - .25, hi = Math.max(...allVals) + .25;
  const x = i => PX + i * (W - 2*PX) / Math.max(1, pts.length - 1);
  const y = v => H - PY - (v - lo) / (hi - lo) * (H - 2*PY);
  const path = pts.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p.dupr_band).toFixed(1)).join(' ');
  const area = path + ' L' + x(pts.length-1).toFixed(1) + ' ' + (H-4) + ' L' + PX + ' ' + (H-4) + ' Z';
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;margin-top:10px" preserveAspectRatio="none">' +
    '<defs><linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".18"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>' +
    '<path d="' + area + '" fill="url(#tg)" style="opacity:0;animation:figin .9s ease .6s forwards"/>' +
    '<path id="trendLine" d="' + path + '" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="stroke:var(--accent)"/>' +
    known.map(p => { const i = pts.indexOf(p); return '<circle cx="' + x(i) + '" cy="' + y(p.known_dupr) + '" r="3.5" fill="none" stroke-width="2" style="stroke:var(--red)"/>'; }).join('') +
    pts.map((p, i) => '<circle cx="' + x(i) + '" cy="' + y(p.dupr_band) + '" r="4" stroke-width="2.5" style="fill:var(--panel);stroke:var(--accent)"/>').join('') +
    '</svg>' +
    (known.length ? '<div style="color:var(--dim);font-size:11.5px;margin-top:4px"><span style="color:var(--accent)">●</span> video estimate &nbsp; <span style="color:var(--red)">○</span> your known DUPR</div>' : '');
}
