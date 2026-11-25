'use strict';
/* ---------------- sessions list ---------------- */
async function loadSessions(list) {
  if (!list) { try { list = await api('/sessions'); } catch (e) { return; } }
  window._pcSessions = list;
  disarmDelete();
  if (!list.length) { $('sessList').innerHTML = '<div class="empty">No sessions yet — upload your first match above.</div>'; return; }
  $('sessList').innerHTML = list.map((s, i) => {
    const running = s.state === 'running' || s.state === 'queued';
    const pillCls = s.state === 'done' && s.stage === 'done' ? 'done' : s.state === 'error' ? 'error' : running ? 'running' : '';
    const pillTxt = s.stage === 'done' ? 'complete' : s.state === 'error' ? 'error' : (STAGE_NAMES[s.stage] || s.stage);
    return '<div class="sess" role="button" tabindex="0" aria-label="Open ' + esc(s.label || s.filename || s.session_id) + '" data-sid="' + s.session_id + '" style="animation-delay:' + (i * 55) + 'ms" onclick="openSession(\'' + s.session_id + '\')">' +
      '<input type="checkbox" class="cb selBox" value="' + s.session_id + '" onclick="event.stopPropagation(); updateDelBtn()">' +
      '<div class="grow"><div class="nm">' + esc(s.label || s.filename || s.session_id) + '</div>' +
        '<div class="meta"><span>' + esc(s.played_at || fmtDate(s.uploaded_at)) + '</span>' +
        (s.known_dupr ? '<span class="kd">DUPR ' + s.known_dupr + '</span>' : '') + '</div>' +
        (running && s.overall != null ? '<div class="minibar"><i style="width:' + Math.round(100*s.overall) + '%"></i></div>' : '') +
      '</div>' +
      '<span class="pill ' + pillCls + '">' + esc(pillTxt) + '</span></div>';
  }).join('');
  if (window._pcProg) decorateSparks(window._pcProg);
}
function decorateSparks(pts) {
  const bands = (pts || []).filter(p => p.dupr_band != null);
  if (bands.length < 2) return;
  document.querySelectorAll('#sessList .sess').forEach(row => {
    const idx = bands.findIndex(p => p.session_id === row.dataset.sid);
    if (idx < 1 || row.querySelector('.spark')) return;
    const ser = bands.slice(0, idx + 1).map(p => p.dupr_band);
    const lo = Math.min(...ser), hi = Math.max(...ser), rng = (hi - lo) || 1;
    const W = 64, H = 20;
    const d = ser.map((v, i) => (i ? 'L' : 'M') + (4 + i * (W - 8) / (ser.length - 1)).toFixed(1) + ' ' + (H - 4 - (v - lo) / rng * (H - 8)).toFixed(1)).join(' ');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('class', 'spark');
    svg.innerHTML = '<path d="' + d + '"/>';
    const pill = row.querySelector('.pill');
    if (pill) row.insertBefore(svg, pill);
  });
}
// local calendar date: toISOString() is UTC, so an evening session was labelled tomorrow
function fmtDate(ts){ if (!ts) return ''; const d = new Date(ts * 1000); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
// Home and the Sessions page both render checkboxes; only the ones on screen count
function selectedIds(){ return [...document.querySelectorAll('.selBox:checked')].filter(b => b.offsetParent !== null).map(b => b.value); }
let delArmed = false, delTimer = null;
function updateDelBtn() {
  const n = selectedIds().length;
  const b = $('delBtn');
  b.disabled = !n;
  if (!delArmed) b.textContent = n ? 'Delete selected (' + n + ')' : 'Delete selected';
  const c = $('cmpBtn'); if (c) { c.disabled = n < 2; c.textContent = n >= 2 ? 'Compare ' + n : 'Compare'; }
}
function compareClicked() {
  const ids = selectedIds();
  if (ids.length >= 2) nav('#/compare?ids=' + ids.map(encodeURIComponent).join(','));
}
async function viewCompare(ids) {
  if (ids.length < 2) { nav('#/sessions'); return; }
  stopPoll();
  show('compare');
  $('cmpBody').innerHTML = '<div class="empty">Comparing…</div>';
  $('clipCmpCard').style.display = 'none';
  if (ids.length === 2) {
    let r;
    try { r = await api('/compare?a=' + encodeURIComponent(ids[0]) + '&b=' + encodeURIComponent(ids[1])); }
    catch (e) { $('cmpBody').innerHTML = '<div class="caveat red">' + warnSVG + '<span>Could not compare: ' + esc(String(e)) + '</span></div>'; return; }
    renderCompare(r);
    loadClipCompare(ids[0], ids[1]);
  } else {
    let r;
    try { r = await api('/compare/multi?ids=' + ids.map(encodeURIComponent).join(',')); }
    catch (e) { $('cmpBody').innerHTML = '<div class="caveat red">' + warnSVG + '<span>Could not compare: ' + esc(String(e)) + '</span></div>'; return; }
    renderCompareMulti(r.sessions || []);
  }
}
let clipCmpIds = [null, null];
async function loadClipCompare(idA, idB) {
  if (DEMO) return;  // rally clips need the live backend
  let sa, sb;
  try { [sa, sb] = await Promise.all([api('/session/' + idA), api('/session/' + idB)]); }
  catch (e) { return; }
  const ralliesA = (sa.events && sa.events.rallies) || [];
  const ralliesB = (sb.events && sb.events.rallies) || [];
  if (!ralliesA.length || !ralliesB.length) return;
  clipCmpIds = [idA, idB];
  $('clipCmpCard').style.display = 'block';
  $('clipCmpLabelA').textContent = (sa.meta && (sa.meta.label || sa.meta.filename)) || 'Session A';
  $('clipCmpLabelB').textContent = (sb.meta && (sb.meta.label || sb.meta.filename)) || 'Session B';
  const opt = r => '<option value="' + r.i + '">Rally ' + (r.i + 1) + ' — ' + r.hits + ' hits · ' + r.duration + 's</option>';
  $('clipCmpRallyA').innerHTML = ralliesA.map((r, i) => opt({ ...r, i })).join('');
  $('clipCmpRallyB').innerHTML = ralliesB.map((r, i) => opt({ ...r, i })).join('');
  $('clipCmpVideoA').src = BASE + '/api/session/' + idA + '/clip/' + $('clipCmpRallyA').value;
  $('clipCmpVideoB').src = BASE + '/api/session/' + idB + '/clip/' + $('clipCmpRallyB').value;
}
function playBothClips() {
  const [idA, idB] = clipCmpIds;
  if (!idA || !idB) return;
  const va = $('clipCmpVideoA'), vb = $('clipCmpVideoB');
  va.src = BASE + '/api/session/' + idA + '/clip/' + $('clipCmpRallyA').value;
  vb.src = BASE + '/api/session/' + idB + '/clip/' + $('clipCmpRallyB').value;
  Promise.all([va.play().catch(() => {}), vb.play().catch(() => {})]);
}
function renderCompareMulti(sessions) {
  const names = sessions.map(s => esc(s.label || s.played_at || s.session_id));
  const rows = CMP_FIELDS.filter(([k]) => sessions.some(s => s[k] != null)).map(([k,l,pre,suf]) =>
    '<div class="cmprow" style="grid-template-columns:180px repeat(' + sessions.length + ', 1fr)">' +
      '<span class="clabel">' + l + '</span>' +
      sessions.map(s => '<span class="cval">' + fmtCmp(s[k],pre,suf) + '</span>').join('') +
    '</div>').join('');
  $('cmpBody').innerHTML =
    '<div class="cmprow chead" style="grid-template-columns:180px repeat(' + sessions.length + ', 1fr)">' +
      '<span class="clabel">Metric</span>' + names.map(n => '<span class="cval">' + n + '</span>').join('') +
    '</div>' + rows;
}
const CMP_FIELDS = [
  ['dupr_band','Est. level','~',''],['win_pct','Win rate','','%'],['kitchen_pct','Kitchen time','','%'],
  ['coverage_pct','Court coverage','','%'],['avg_rally_hits','Avg hits / rally','',''],['rally_count','Rallies','',''],
  ['distance_ft','Distance','',' ft'],['avg_speed_ft_s','Avg speed','',' ft/s'],['play_time_pct','Time in play','','%'],
  ['points_won','Points won','',''],['points_lost','Points lost','','']
];
function fmtCmp(v, pre, suf){ if (v == null) return '—'; const n = suf === ' ft' ? Math.round(v).toLocaleString() : v; return pre + n + suf; }
function renderCompare(r) {
  const a = r.a || {}, b = r.b || {}, diffs = r.diffs || {};
  const nameA = esc(a.label || a.played_at || 'Session A'), nameB = esc(b.label || b.played_at || 'Session B');
  const rows = CMP_FIELDS.filter(([k]) => a[k] != null || b[k] != null).map(([k,l,pre,suf]) => {
    const d = diffs[k] && diffs[k].delta;
    const lowerBetter = (k === 'points_lost');
    let deltaHTML = '<span class="mut">—</span>';
    if (d != null && d !== 0) {
      const good = lowerBetter ? d < 0 : d > 0;
      const val = (suf === ' ft' ? Math.round(Math.abs(d)).toLocaleString() : +Math.abs(d).toFixed(2)) + suf;
      deltaHTML = '<span class="delta ' + (good ? 'up' : 'dn') + '" style="margin:0">' + (d > 0 ? '▲' : '▼') + ' ' + val + '</span>';
    } else if (d === 0) deltaHTML = '<span class="mut">no change</span>';
    return '<div class="cmprow"><span class="clabel">' + l + '</span>' +
      '<span class="cval">' + fmtCmp(a[k],pre,suf) + '</span>' +
      '<span class="cval">' + fmtCmp(b[k],pre,suf) + '</span>' +
      '<span class="cdelta">' + deltaHTML + '</span></div>';
  }).join('');
  $('cmpBody').innerHTML =
    '<div class="cmprow chead"><span class="clabel">Metric</span><span class="cval">' + nameA + '</span><span class="cval">' + nameB + '</span><span class="cdelta">Change</span></div>' + rows;
}
function disarmDelete(){ delArmed = false; clearTimeout(delTimer); const b = $('delBtn'); b.className = 'ghost sm'; updateDelBtn(); }
async function deleteClicked() {
  const ids = selectedIds();
  if (!ids.length) return;
  const b = $('delBtn');
  if (!delArmed) {
    delArmed = true;
    b.className = 'danger sm';
    b.textContent = 'Really delete ' + ids.length + '? (moves to trash)';
    delTimer = setTimeout(disarmDelete, 4000);
    return;
  }
  disarmDelete();
  try {
    const r = await api('/sessions/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
    if (r.errors && r.errors.length) toast('Some deletions failed: ' + r.errors.map(e => e.id + ': ' + e.error).join(', '), true);
    else toast('Deleted ' + (r.deleted ? r.deleted.length : ids.length) + ' session(s)');
  } catch (e) { toast('Delete failed: ' + e, true); }
  loadHome();
  if ($('sessions').classList.contains('on')) loadSessionsPage();   // the list being looked at must drop the deleted rows too
}
// A tap on a row/card: remember where it was (for the portal morph), then change the route
function openSession(id) {
  try {
    const t = window.event && window.event.target;
    const row = t && t.closest && t.closest('.sess,.strow,.card');
    portalFrom = row ? row.getBoundingClientRect() : null;
  } catch(e) { portalFrom = null; }
  nav('#/s/' + encodeURIComponent(id));
}
async function viewSession(id) {
  sid = id;
  stopPoll();
  const gen = ++openGen;
  let s;
  try { s = await api('/session/' + id); }
  catch (e) {
    if (gen !== openGen) return;
    toast('Could not open session: ' + e, true);
    history.replaceState(null, '', '#/');   // a dead link must not trap the back button
    viewHome(); return;
  }
  if (gen !== openGen) return;   // a later tap won; don't let this slower reply paint over it
  routeSession(s);
}
let openGen = 0;

/* ---------------- Sessions page ---------------- */
// typing in a filter used to fire one request per keystroke, and a slow early reply could
// land after a later one and show the wrong list
let sessPageTimer = null, sessPageGen = 0;
function loadSessionsPageSoon() { clearTimeout(sessPageTimer); sessPageTimer = setTimeout(loadSessionsPage, 250); }
async function loadSessionsPage() {
  const gen = ++sessPageGen;
  const q = new URLSearchParams();
  const fLabel = $('sFilterLabel'), fOpp = $('sFilterOpponent'), fFrom = $('sFilterFrom'), fTo = $('sFilterTo'), fCtx = $('sFilterContext');
  if (fLabel && fLabel.value) q.set('label', fLabel.value);
  if (fOpp && fOpp.value) q.set('opponent', fOpp.value);
  if (fFrom && fFrom.value) q.set('date_from', fFrom.value);
  if (fTo && fTo.value) q.set('date_to', fTo.value);
  if (fCtx && fCtx.value) q.set('context', fCtx.value);
  const qs = q.toString();
  let list;
  try { list = await api('/sessions' + (qs ? '?' + qs : '')); } catch (e) { return; }
  if (gen !== sessPageGen) return;
  window._pcSessions = list;
  disarmDelete();
  $('sessCount').textContent = list.length ? (list.length + (list.length === 1 ? ' session' : ' sessions')) : 'No sessions yet';
  const head = '<div class="strow shead"><span></span><span>Session</span><span>Est. level</span><span>Kitchen</span><span>Rally avg</span><span>Status</span></div>';
  if (!list.length) { $('sessTable').innerHTML = head + '<div class="empty" style="padding:18px 6px">Upload a match from the Film Room to get started.</div>'; return; }
  $('sessTable').innerHTML = head + list.map((s, i) => {
    const running = s.state === 'running' || s.state === 'queued';
    const done = s.state === 'done' && s.stage === 'done';
    const pillCls = done ? 'done' : s.state === 'error' ? 'error' : running ? 'running' : '';
    let pillTxt = s.stage === 'done' ? 'complete' : s.state === 'error' ? 'error' : (STAGE_NAMES[s.stage] || s.stage);
    if (s.stage === 'queued' && s.queue_position) pillTxt = 'queued · #' + s.queue_position;
    const band = s.dupr_band != null ? '~' + s.dupr_band : '';
    return '<div class="strow" role="button" tabindex="0" aria-label="Open ' + esc(s.label || s.filename || s.session_id) + '" style="animation-delay:' + (i * 45) + 'ms" onclick="openSession(\'' + s.session_id + '\')">' +
      '<input type="checkbox" class="cb selBox" value="' + s.session_id + '" onclick="event.stopPropagation(); updateDelBtn()">' +
      '<div class="scell"><div class="nm">' + esc(s.label || s.filename || s.session_id) + '</div>' +
        '<div class="sub">' + esc(s.played_at || fmtDate(s.uploaded_at)) + (s.opponent ? ' · vs ' + esc(s.opponent) : '') + (s.known_dupr ? ' · known DUPR ' + s.known_dupr : '') + (s.disk_bytes ? ' · ' + fmtMB(s.disk_bytes) : '') + '</div></div>' +
      '<div class="scell dupr">' + (band || '<span class="mut">—</span>') + '</div>' +
      '<div class="scell num">' + (s.kitchen_pct != null ? s.kitchen_pct + '%' : '<span class="mut">—</span>') + '</div>' +
      '<div class="scell num">' + (s.avg_rally_hits != null ? s.avg_rally_hits : '<span class="mut">—</span>') + '</div>' +
      '<div class="scell"><span class="pill ' + pillCls + '">' + esc(pillTxt) + '</span>' +
        (running && s.overall != null ? '<div class="minibar" style="margin-top:6px"><i style="width:' + Math.round(100*s.overall) + '%"></i></div>' : '') + '</div>' +
      '</div>';
  }).join('');
}

/* ---------------- Trash ---------------- */
function exportSessionsCsv() {
  if (DEMO) { toast('CSV export needs the live backend'); return; }
  window.open(BASE + '/api/sessions/export.csv', '_blank');
}
async function openTrash() {
  const card = $('trashCard');
  const opening = card.style.display === 'none';
  card.style.display = opening ? 'block' : 'none';
  if (opening) await refreshTrash();
}
async function refreshTrash() {
  let list;
  try { list = await api('/sessions/trash'); } catch (e) { return; }
  if (!list.length) { $('trashTable').innerHTML = '<div class="empty">Trash is empty.</div>'; return; }
  $('trashTable').innerHTML = list.map(s =>
    '<div class="strow" style="grid-template-columns:1fr auto">' +
      '<div class="scell"><div class="nm">' + esc(s.label || s.filename || s.session_id) + '</div></div>' +
      '<button class="ghost sm" onclick="restoreSession(\'' + s.session_id + '\')">Restore</button>' +
    '</div>').join('');
}
async function restoreSession(id) {
  try {
    await api('/sessions/restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: [id] }) });
    toast('Restored');
    refreshTrash();
    loadSessionsPage();
  } catch (e) { toast('Restore failed: ' + e, true); }
}
