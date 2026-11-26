'use strict';
/* Results extras: since-last-time, deeper stats, the rally strip and match player, "show me the
   clips behind this number", and the highlight reel. All of it comes from per-session endpoints
   that only exist on the live backend, so demo mode just hides the cards. */

const MOMENT_LABELS = {
  longest: 'Longest rally', won: 'Points won', lost: 'Points lost', unforced_errors: 'Unforced errors',
  serves: 'Serves', drive: 'Drives', dink: 'Dinks', drop: 'Drops', medium: 'Medium-paced shots',
};
let moments = null, momentKind = null, playUntil = null, queueOn = false, queueIdx = 0, tlNow = -1;

const fmtN = v => v == null ? '—' : (Math.abs(v - Math.round(v)) < 0.005 ? String(Math.round(v)) : Number(v).toFixed(1));
const signed = v => (v > 0 ? '+' : v < 0 ? '−' : '') + fmtN(Math.abs(v));

function resetInsights() {
  moments = null; momentKind = null; playUntil = null; queueOn = false; tlNow = -1;
  ['changesCard', 'analyticsCard', 'timelineWrap', 'matchWrap'].forEach(id => { $(id).style.display = 'none'; });
  ['momentChips', 'pointMoments', 'shotMoments', 'momentPanel'].forEach(id => { $(id).innerHTML = ''; });
  const v = $('matchVideo');
  v.pause(); v.removeAttribute('src');
  try { v.load(); } catch (e) {}
  hlReset();
}

function loadInsights(s) {
  resetInsights();
  renderTimeline();
  if (DEMO) return;
  loadChanges(); loadAnalytics(); loadMoments(); loadHlStatus();
}
// a correction changes who won, which everything below is derived from
function refreshDerived() { renderTimeline(); if (!DEMO) { loadMoments(); loadAnalytics(); } }

/* ---------------- since last time ---------------- */
async function loadChanges() {
  const mine = sid;
  let c;
  try { c = await api('/session/' + mine + '/changes'); } catch (e) { return; }
  if (sid !== mine || !c || !c.available) return;
  const chip = m => m.direction === 'same' ? '<span class="dchip same">≈ same</span>'
    : '<span class="dchip ' + m.direction + '">' + (m.direction === 'better' ? '▲ ' : m.direction === 'worse' ? '▼ ' : '') + signed(m.delta) + '</span>';
  let rating = '';
  if (c.rating) {
    rating = '<div class="chgrating">Rating estimate <b>' + fmtBand(c.rating.before) + '</b> → <b>' + fmtBand(c.rating.now) + '</b> ' +
      (c.rating.within_margin
        ? '<span class="mut">— within the estimate\'s own margin, so not a real change.</span>'
        : '<span class="mut">— more than the margin of error, so likely a real change.</span>') + '</div>';
  }
  $('changesBody').innerHTML =
    '<div class="chghead">' + esc(c.headline) + '</div>' + rating +
    '<div class="chglist">' + c.metrics.map(m =>
      '<div class="chgrow"><span class="chl">' + esc(m.label) + '</span>' +
      '<span class="chv">' + fmtN(m.before) + esc(m.unit) + ' → <b>' + fmtN(m.now) + esc(m.unit) + '</b></span>' + chip(m) + '</div>').join('') + '</div>' +
    '<div class="heatnote" style="margin-top:8px">compared with ' + esc(c.previous.label) + ' (' + esc(c.previous.played_at) + ')</div>';
  $('changesCard').style.display = 'block';
}

/* ---------------- deeper stats ---------------- */
const anBlock = (title, body, note) =>
  '<div class="anblock"><h3>' + title + '</h3>' + body + (note ? '<div class="heatnote">' + note + '</div>' : '') + '</div>';
const rateBar = (label, pct, n, color) =>
  '<div class="bar"><span class="lb">' + esc(label) + '</span><div class="track"><div class="fill" data-w="' + (pct || 0) + '%"' +
  (color ? ' style="background:' + color + '"' : '') + '></div></div><span class="val">' + (pct == null ? '—' : pct + '%') + '<small> (' + n + ')</small></span></div>';

async function loadAnalytics() {
  const mine = sid;
  let a;
  try { a = await api('/session/' + mine + '/analytics'); } catch (e) { return; }
  if (sid !== mine || !a) return;
  const blocks = [];
  const L = a.by_rally_length;
  if (L && L.available) {
    blocks.push(anBlock('Win rate by rally length',
      '<div class="bars">' + L.buckets.map(b => rateBar(b.label, b.win_pct, b.played)).join('') + '</div>',
      'of ' + L.scored + ' scored rallies · the number in brackets is how many rallies each row rests on'));
  }
  const P = a.pace;
  if (P && P.available) {
    blocks.push(anBlock('Pace',
      '<div class="grid">' + statCard(nz(P.rallies_per_min), 'rallies per minute') +
      statCard(nz(P.avg_rest_s, ' s'), 'rest between rallies') +
      statCard('1 : ' + fmtN(P.work_rest_ratio ? 1 / P.work_rest_ratio : null), 'work : rest') + '</div>',
      P.breaks ? P.breaks + ' long break' + (P.breaks === 1 ? '' : 's') + ' (over a minute) not counted as rest' : ''));
  }
  const R = a.return_and_run;
  if (R && R.available) {
    blocks.push(anBlock('Return and run',
      '<div class="grid">' + statCard(nz(R.reached_pct, '%'), 'followed your return in<span class="n">of ' + R.returns + ' returns</span>') +
      statCard(nz(R.avg_secs_to_kitchen, ' s'), 'to reach the kitchen<span class="n">when you did</span>') + '</div>',
      'time from hitting the return until you were at the kitchen line (within 8 s)'));
  }
  const T = a.third_shot;
  if (T && T.available) {
    const total = T.n, order = ['drive', 'dink', 'drop', 'medium'];
    blocks.push(anBlock('Your third shot',
      '<div class="bars">' + order.filter(k => T.mix[k]).map(k =>
        '<div class="bar"><span class="lb">' + esc(SHOT_TYPE_LABELS[k] || k) + '</span><div class="track"><div class="fill" data-w="' +
        Math.round(100 * T.mix[k] / total) + '%"></div></div><span class="val">' + T.mix[k] + '</span></div>').join('') + '</div>',
      'what you hit after your own serve, ' + total + ' time' + (total === 1 ? '' : 's')));
  }
  const E = a.errors, known = E ? E.net + E.long + E.wide : 0;
  if (known > 0) {
    blocks.push(anBlock('Where your errors went',
      '<div class="grid">' + statCard(E.net, 'into the net') + statCard(E.long, 'long') + statCard(E.wide, 'wide') + '</div>',
      'your side\'s unforced errors' + (E.unknown ? ' · ' + E.unknown + ' more from before direction was recorded' : '')));
  }
  const Z = a.end_zone;
  if (Z && Z.available) {
    const names = { kitchen: 'At the kitchen line', transition: 'In no-man\'s land', baseline: 'Back at the baseline' };
    blocks.push(anBlock('Where you stood when the point ended',
      '<div class="bars">' + ['kitchen', 'transition', 'baseline'].filter(k => Z.zones[k]).map(k =>
        rateBar(names[k], Z.zones[k].win_pct, Z.zones[k].played)).join('') + '</div>',
      'win rate by your position at the end of the rally'));
  }
  const tips = (a.tips || []).map(t => '<div class="coach">' + tipSVG + '<span>' + esc(t) + '</span></div>').join('');
  if (!blocks.length && !tips) return;
  $('analyticsBody').innerHTML = (tips ? '<div style="margin-bottom:6px">' + tips + '</div>' : '') + blocks.join('');
  $('analyticsCard').style.display = 'block';
  animateBars($('analyticsBody')); animateStats($('analyticsBody'));
}

/* ---------------- the rally strip ---------------- */
function renderTimeline() {
  const rallies = (lastSession && lastSession.events && lastSession.events.rallies) || [];
  const wrap = $('timelineWrap');
  if (!rallies.length || DEMO) { wrap.style.display = 'none'; return; }
  const outs = (lastSession.points && lastSession.points.outcomes) || [];
  const maxHits = Math.max(1, ...rallies.map(r => r.hits || 0));
  wrap.style.display = 'block';
  $('rallyTimeline').innerHTML = rallies.map((r, i) => {
    const o = outs[i] || {};
    const cls = isMyTeam(o.winner) ? 'won' : isOppTeam(o.winner) ? 'lost' : 'unk';
    const word = cls === 'won' ? 'won' : cls === 'lost' ? 'lost' : 'result unclear';
    const h = Math.round(22 + 78 * Math.min(1, (r.hits || 0) / maxHits));
    return '<button type="button" class="tlseg ' + cls + '" id="tl' + i + '" onclick="seekRally(' + i + ')" ' +
      'aria-label="Rally ' + (i + 1) + ', ' + r.hits + ' hits, ' + word + ', at ' + fmtClock(r.start) + '. Watch it." title="Rally ' + (i + 1) + ' · ' + word + '">' +
      '<i style="height:' + h + '%"></i><span>' + (i + 1) + '</span></button>';
  }).join('');
  tlNow = -1;
}
function seekRally(i) {
  const r = lastSession.events.rallies[i];
  queueOn = false; momentKind = null; $('momentPanel').innerHTML = '';
  seekWindow(Math.max(0, r.start - 1.5), r.end + 1.5);
}
function updatePlayhead() {
  const rallies = (lastSession && lastSession.events && lastSession.events.rallies) || [];
  const t = $('matchVideo').currentTime;
  const i = rallies.findIndex(r => t >= r.start - 1.5 && t <= r.end + 1.5);
  if (i === tlNow) return;
  const old = $('tl' + tlNow); if (old) old.classList.remove('now');
  tlNow = i;
  const el = $('tl' + i);
  if (el) { el.classList.add('now'); const tl = $('rallyTimeline'); tl.scrollLeft = el.offsetLeft - tl.clientWidth / 2; }
}

/* ---------------- the match player ---------------- */
function seekWindow(start, end) {
  const v = $('matchVideo');
  $('matchWrap').style.display = 'block';
  if (!v.getAttribute('src')) { v.preload = 'metadata'; v.src = BASE + '/api/session/' + sid + '/video'; }
  playUntil = end;
  v.play().catch(() => {});          // inside the tap, so phones allow it; the seek below lands before anything is seen
  let tries = 0;
  (function go() {
    if (v.readyState >= 1) { v.currentTime = start; v.play().catch(() => {}); return; }
    if (++tries < 100) setTimeout(go, 100);
  })();
  if (!noMotion()) v.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
$('matchVideo').addEventListener('timeupdate', () => {
  updatePlayhead();
  const v = $('matchVideo');
  if (playUntil != null && v.currentTime >= playUntil) {
    playUntil = null;
    if (queueOn && momentKind && queueIdx + 1 < moments[momentKind].length) playMoment(queueIdx + 1, true);
    else { v.pause(); queueOn = false; }
  }
});

/* ---------------- numbers -> clips ---------------- */
async function loadMoments() {
  const mine = sid;
  let m;
  try { m = await api('/session/' + mine + '/moments'); } catch (e) { return; }
  if (sid !== mine || !m) return;
  moments = m;
  const chips = kinds => kinds.filter(k => (m[k] || []).length).map(k =>
    '<button type="button" class="chip" onclick="showMoments(\'' + k + '\')">▶ ' + esc(MOMENT_LABELS[k]) + ' <b>' + m[k].length + '</b></button>').join('');
  $('momentChips').innerHTML = chips(['longest', 'won', 'lost', 'unforced_errors', 'serves', 'drive', 'dink', 'drop', 'medium']);
  $('pointMoments').innerHTML = chips(['won', 'lost', 'unforced_errors']);
  $('shotMoments').innerHTML = chips(['serves', 'drive', 'dink', 'drop', 'medium']);
  if (momentKind) showMomentsList();   // a correction while a list is open: keep it in step
}
function showMoments(kind) {
  if (!moments || !(moments[kind] || []).length) return;
  momentKind = kind; queueOn = false;
  showMomentsList();
  $('matchWrap').scrollIntoView({ behavior: noMotion() ? 'auto' : 'smooth', block: 'center' });
  playMoment(0, false);
}
function showMomentsList() {
  const list = (moments && moments[momentKind]) || [];
  if (!list.length) { closeMoments(); return; }
  $('matchWrap').style.display = 'block';
  $('momentPanel').innerHTML =
    '<div class="mphead"><b>' + esc(MOMENT_LABELS[momentKind]) + ' · ' + list.length + '</b><span>' +
    '<button type="button" class="ghost sm" onclick="playAllMoments()">▶ Play all</button> ' +
    '<button type="button" class="ghost sm" onclick="closeMoments()">Close</button></span></div>' +
    list.map((m, i) => '<button type="button" class="mrow" id="mom' + i + '" onclick="playMoment(' + i + ', false)"><span>' + esc(m.label) +
      '</span><span class="play">▶ watch</span></button>').join('');
}
function playMoment(i, keepQueue) {
  const list = (moments && moments[momentKind]) || [];
  if (!list[i]) return;
  queueOn = !!keepQueue; queueIdx = i;
  document.querySelectorAll('.mrow').forEach(el => el.classList.remove('playing'));
  const row = $('mom' + i); if (row) row.classList.add('playing');
  seekWindow(list[i].start, list[i].end);
}
function playAllMoments() { playMoment(0, true); }
function closeMoments() {
  momentKind = null; queueOn = false; $('momentPanel').innerHTML = '';
  const v = $('matchVideo'); v.pause(); playUntil = null;
}

/* ---------------- highlight reel: build in the background, then share ---------------- */
let hl = { sid: null, state: 'none', file: null };
function hlReset() { hl = { sid: null, state: 'none', file: null }; hlLabel(); }
function hlLabel() {
  const txt = { none: 'Make highlight reel', building: 'Building highlight reel…',
                ready: 'Share highlight reel', error: 'Highlight reel failed — retry' }[hl.state];
  const btn = $('hlBtn'), menu = $('hlMenu');
  if (btn) { btn.textContent = txt; btn.disabled = hl.state === 'building'; }
  if (menu) { menu.textContent = hl.state === 'none' ? 'Highlight reel' : txt; menu.disabled = hl.state === 'building'; }
}
async function loadHlStatus() {
  const mine = sid;
  try {
    const st = await api('/session/' + mine + '/highlights/status');
    if (sid === mine && st.state === 'ready') { hl = { sid: mine, state: 'ready', file: null }; hlLabel(); }
  } catch (e) {}
}
async function highlightsClick() {
  if (DEMO) { toast('Highlight reel needs the live backend'); return; }
  if (hl.sid === sid && hl.state === 'ready') return openHighlights();
  if (hl.state === 'building') return;
  const mine = sid;
  hl = { sid: mine, state: 'building', file: null }; hlLabel();
  try {
    let st = await api('/session/' + mine + '/highlights', { method: 'POST' });
    const t0 = Date.now();
    while (st.state === 'building' && Date.now() - t0 < 180000) {
      await new Promise(r => setTimeout(r, 1000));
      if (sid !== mine) return;
      st = await api('/session/' + mine + '/highlights/status');
    }
    if (sid !== mine) return;
    if (st.state !== 'ready') throw st.error || 'timed out';
    hl.state = 'ready';
    // fetch it now, so the Share tap that follows is still inside the browser's user-gesture window
    try {
      const blob = await (await fetch(BASE + '/api/session/' + mine + '/highlights.mp4')).blob();
      if (sid === mine) hl.file = new File([blob], 'dinkiq-highlights.mp4', { type: 'video/mp4' });
    } catch (e) {}
    hlLabel(); toast('Highlight reel ready — tap Share');
  } catch (e) {
    if (sid === mine) { hl = { sid: mine, state: 'error', file: null }; hlLabel(); }
    toast('Highlight reel failed: ' + e, true);
  }
}
async function openHighlights() {
  const url = BASE + '/api/session/' + sid + '/highlights.mp4';
  try {
    let f = hl.file;
    if (!f) { const blob = await (await fetch(url)).blob(); f = hl.file = new File([blob], 'dinkiq-highlights.mp4', { type: 'video/mp4' }); }
    if (navigator.canShare && navigator.canShare({ files: [f] })) { await navigator.share({ files: [f], title: 'My DinkIQ highlights' }); return; }
  } catch (e) { if (e && e.name === 'AbortError') return; }
  window.open(url, '_blank');
}
