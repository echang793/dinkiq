'use strict';
/* ---------------- subject/opponent marker ---------------- */
async function loadSubjectMarker(id) {
  const card = $('subjMarkerCard');
  let data;
  try { data = await api('/session/' + id + '/subject-marker'); }
  catch (e) { card.style.display = 'none'; return; }
  const opponents = data && data.opponents && data.opponents.length ? data.opponents : (data && data.opponent ? [data.opponent] : []);
  if (!data || (!data.subject && !data.partner && !opponents.length)) { card.style.display = 'none'; return; }
  card.style.display = 'flex';
  const img = new Image();
  img.onload = () => drawSubjectMarker(img, data, opponents);
  img.src = frameURL();
}
function drawSubjectMarker(img, data, opponents) {
  const c = $('subjCanvas'), ctx = c.getContext('2d');
  c.width = img.width; c.height = img.height;
  ctx.drawImage(img, 0, 0);
  const box = (b, color, label, dashed) => {
    if (!b) return;
    ctx.strokeStyle = color; ctx.lineWidth = 3;
    ctx.setLineDash(dashed ? [10, 6] : []);
    ctx.strokeRect(b.x1, b.y1, b.x2 - b.x1, b.y2 - b.y1);
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    const tw = ctx.measureText(label).width + 10;
    ctx.fillRect(b.x1, Math.max(0, b.y1 - 20), tw, 20);
    ctx.fillStyle = '#0a1428'; ctx.font = '800 12px Archivo, sans-serif';
    ctx.fillText(label, b.x1 + 5, Math.max(14, b.y1 - 5));
  };
  const V = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  box(data.subject, V('--accent'), 'YOU', false);
  box(data.partner, '#8b6bf0', 'PARTNER', false);
  opponents.forEach((b, i) => box(b, V('--red'), opponents.length > 1 ? 'OPP' + (i + 1) : 'OPPONENT', true));
  const frames = [data.subject, data.partner, ...opponents].filter(Boolean).map(b => b.frame || 0);
  const delta = frames.length ? Math.max(...frames) : 0;
  const legend = 'solid = YOU' + (data.partner ? '/PARTNER' : '') + ' · dashed = opponent' + (opponents.length > 1 ? 's' : '');
  $('subjNote').innerHTML = legend +
    (delta > 0 ? ' <span class="caveat" style="display:inline;padding:2px 6px;margin-left:6px">approximate — detected ' + delta + ' frames in</span>' : '');
}

/* ---------------- meta editing ---------------- */
function fillMeta(meta) {
  $('mLabel').value = (meta && meta.label) || '';
  $('mPlayer').value = (meta && meta.player) || '';
  $('mOpponent').value = (meta && meta.opponent) || '';
  $('mDate').value = (meta && meta.played_at) || '';
  $('mDupr').value = (meta && meta.known_dupr) || '';
  $('mNotes').value = (meta && meta.notes) || '';
  $('mContext').value = (meta && meta.context) || '';
  $('mSaved').classList.remove('on');
  loadHeadToHead();
}
async function saveMeta() {
  // Every field is sent every time. Text fields clear with "", the number and the enum
  // clear with an explicit null (the server treats null as "clear", absent as "leave alone").
  const dupr = $('mDupr').value.trim();
  const body = {
    label: $('mLabel').value,
    player: $('mPlayer').value,
    opponent: $('mOpponent').value,
    played_at: $('mDate').value,
    notes: $('mNotes').value,
    known_dupr: dupr === '' ? null : parseFloat(dupr),
    context: $('mContext').value || null,
  };
  try {
    await api('/session/' + sid + '/meta', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    $('mSaved').classList.add('on');
    setTimeout(() => $('mSaved').classList.remove('on'), 2400);
  } catch (e) { toast('Save failed: ' + e, true); }
}
async function loadHeadToHead() {
  const box = $('h2hBox');
  const name = $('mOpponent').value.trim();
  if (!name) { box.style.display = 'none'; return; }
  let h;
  try { h = await api('/opponents/' + encodeURIComponent(name) + '/history'); }
  catch (e) { box.style.display = 'none'; return; }
  if (!h || h.matches == null || h.matches < 2) { box.style.display = 'none'; return; }  // 1 match = this session itself
  box.style.display = 'block';
  let html = '<div class="caveat" style="display:flex">' +
    '<span>Head-to-head vs <b>' + esc(name) + '</b>: <b>' + h.wins + '-' + h.losses + '</b>' +
    (h.win_pct != null ? ' (' + h.win_pct + '% win rate' : '') + (h.win_pct != null ? ') across ' : ' across ') +
    h.matches + ' session' + (h.matches === 1 ? '' : 's') + '</span></div>';
  try {
    const sc = await api('/opponents/' + encodeURIComponent(name) + '/scouting');
    if (sc && sc.dominant_side && sc.shots_tracked >= 3) {
      html += '<div class="caveat" style="display:flex;margin-top:6px">' +
        '<span>Scouting: <b>' + sc.dominant_side_pct + '%</b> of their tracked shots land <b>' + esc(sc.dominant_side) +
        '</b> (' + sc.shots_tracked + ' shots tracked across ' + h.matches + ' sessions)</span></div>';
    }
  } catch (e) {}
  box.innerHTML = html;
}
async function doReupload() {
  const f = $('mReuploadFile').files[0];
  if (!f || !sid) return;
  if (!/\.(mp4|mov|m4v)$/i.test(f.name)) return toast('Only MP4 / MOV / M4V files are supported', true);
  const target = sid;
  const mt = lastSession && lastSession.meta && lastSession.meta.match_type;
  // same chunked, resumable path as a first upload (the old single multipart
  // POST had no progress, no retry and no size limit)
  lastMsgStage = null;
  setProcView(false); show('proc'); renderProcSteps('ingest', 0);
  $('procMsg').textContent = 'Uploading replacement video…';
  $('procEta').textContent = '';
  resumableUpload(f, mt || 'singles', () => {
    toast('Video replaced — reprocessing…');
    $('procMsg').textContent = 'Preparing video…';
    // reupload only re-ingests (extracts frames), it doesn't re-analyze —
    // that needs a fresh calibration, same as a brand-new upload. Waiting
    // on stage==='done' here would hang forever since nothing advances
    // the pipeline past ingest until calibration is submitted.
    waitFor(s => s.stage === 'ingest' && s.state === 'done', () => startCalib(mt), 'Preparing video', true);
  }, msg => {
    toast('Replace failed: ' + msg, true);
    openSession(target);  // the original is untouched — go back to it
  }, {
    sessionId: target,
    onProgress: frac => {
      const pct = Math.round(frac * 100);
      $('procBar').style.width = pct + '%';
      $('procPct').textContent = pct + '%';
    },
  });
}
