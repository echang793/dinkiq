'use strict';
/* ---------------- upload ---------------- */
const drop = $('drop'), fileIn = $('file');
let uploading = false;
let matchType = 'singles';
function setMatchType(t) {
  matchType = t;
  $('matchSingles').classList.toggle('on', t === 'singles');
  $('matchDoubles').classList.toggle('on', t === 'doubles');
}
drop.onclick = () => { if (!uploading) fileIn.click(); };
drop.ondragover = e => { e.preventDefault(); if (!uploading) drop.classList.add('on'); };
drop.ondragleave = () => drop.classList.remove('on');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('on'); if (!uploading && e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0]); };
fileIn.onchange = () => { if (fileIn.files[0]) doUpload(fileIn.files[0]); };

function fmtMB(b){ return (b/1048576).toFixed(1) + ' MB'; }
const UPLOAD_MAX_BYTES = 2 * 1024 ** 3;   // keep in step with the server's MAX_UPLOAD_BYTES
// duration from the file's own metadata, without uploading anything; null when the browser can't tell
function probeDuration(f) {
  return new Promise(resolve => {
    const v = document.createElement('video'), url = URL.createObjectURL(f);
    const end = d => { URL.revokeObjectURL(url); v.removeAttribute('src'); resolve(d); };
    v.preload = 'metadata'; v.muted = true;
    v.onloadedmetadata = () => end(isFinite(v.duration) ? v.duration : null);
    v.onerror = () => end(null);
    setTimeout(() => end(null), 4000);
    v.src = url;
  });
}
async function doUpload(f) {
  const ok = /\.(mp4|mov|m4v)$/i.test(f.name);
  if (!ok) return toast('Only MP4 / MOV / M4V files are supported', true);
  // refuse here, in a second, rather than after the first gigabyte has crossed a cellular link
  if (f.size > UPLOAD_MAX_BYTES) return toast('That file is ' + (f.size / 1024 ** 3).toFixed(1) + ' GB; the limit is 2 GB. Trim it or export at a lower quality first.', true);
  const dur = await probeDuration(f);
  if (dur != null && dur < 45) toast('That clip is only ' + Math.round(dur) + ' s long. A rating needs a few minutes of play, so expect "not enough data".', true);
  uploading = true;
  drop.classList.add('busy');
  $('dropTitle').textContent = 'Uploading';
  $('dropSub').textContent = f.name;
  $('upBarWrap').style.display = 'block'; $('upMeta').style.display = 'flex';
  $('upName').textContent = f.name; $('upWarn').innerHTML = '';
  const done = r => {
    uploading = false;
    sid = r.session_id;
    let notes = '';
    if (r.warning) notes += '<div class="warnbox">⚠ ' + esc(r.warning) + '</div>';
    // shown only when the source was bigger than the 720p30 pipeline can use
    if (r.capture_hint) notes += '<div class="coach" style="margin-top:8px">' + tipSVG +
      '<span>' + esc(r.capture_hint) + '</span></div>';
    $('upWarn').innerHTML = notes;
    $('dropTitle').textContent = 'Preparing video';
    $('dropSub').textContent = 'extracting frames…';
    waitFor(s => s.stage === 'ingest' && s.state === 'done', () => { resetDrop(); startCalib(); }, 'Preparing video', true);
  };
  const fail = msg => { uploading = false; resetDrop(); toast('Upload failed: ' + msg, true); };
  if (DEMO) { demoUpload(f, done); return; }
  resumableUpload(f, matchType, done, fail, {onCancel: () => { uploading = false; resetDrop(); }});
}

// Chunked + resumable. A single POST of a multi-GB clip over cellular loses the whole transfer on
// one dropped connection; here a failed chunk is retried from the offset the server reports it
// actually has, the upload id is remembered so a reload (or the phone sleeping) resumes instead of
// restarting, and the screen is kept awake while it runs.
const UPLOAD_CHUNK = 8 << 20;   // 8 MB
const UPLOAD_BACKOFF_S = [1, 2, 4, 8, 15, 30, 30, 30];   // ~2 min of patience: a lock/unlock or a dead zone
let upCtl = null;
const upKey = (f, target) => [f.name, f.size, f.lastModified, target.session_id || ''].join('|');
function pendingUpload() { try { return JSON.parse(localStorage.getItem('pc_upload')); } catch (e) { return null; } }
function setPendingUpload(o) { try { if (o) localStorage.setItem('pc_upload', JSON.stringify(o)); else localStorage.removeItem('pc_upload'); } catch (e) {} }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function whenOnline() { while (navigator.onLine === false) await sleep(1000); }

let wakeLock = null;
async function holdScreen() { try { if ('wakeLock' in navigator && !wakeLock) { wakeLock = await navigator.wakeLock.request('screen'); wakeLock.addEventListener('release', () => { wakeLock = null; }); } } catch (e) {} }
function releaseScreen() { try { if (wakeLock) wakeLock.release(); } catch (e) {} wakeLock = null; }
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && upCtl) holdScreen(); });

function cancelUpload() { if (upCtl) { upCtl.cancelled = true; upCtl.abort.abort(); } }
function setUpStatus(msg) { const el = $('upMb'); if (el) el.textContent = msg; }

// opts.sessionId: replace that session's video instead of creating a session.
// opts.onProgress(frac, loaded, total): defaults to the Home upload bar.
async function resumableUpload(f, matchType, done, fail, opts = {}) {
  const target = opts.sessionId ? {session_id: opts.sessionId} : {};
  const onProgress = opts.onProgress || setUpProgress;
  const ctl = upCtl = {cancelled: false, abort: new AbortController()};
  const post = async (url, body) => {
    const r = await fetch(BASE + url, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), {status: r.status, body: await r.text()});
    return r.json();
  };
  const finish = () => { upCtl = null; releaseScreen(); };
  const key = upKey(f, target);
  let id = null, offset = 0;
  // the same file picked again after a reload: carry on where the server says it is
  const pend = pendingUpload();
  if (pend && pend.key === key) {
    try { offset = (await api('/upload/' + pend.id)).offset; id = pend.id; } catch (e) { id = null; setPendingUpload(null); }
  }
  if (!id) {
    try {
      const init = await post('/api/upload/init', {filename: f.name, match_type: matchType, size: f.size, ...target});
      id = init.upload_id; offset = 0;
      setPendingUpload({key, id, name: f.name});
    } catch (e) {
      finish();
      fail(e.status === undefined ? "Can't reach DinkIQ — check your connection and try again" : (parseDetail(e.body) || e.message)); return;
    }
  }
  await holdScreen();
  try {
    while (offset < f.size) {
      const end = Math.min(offset + UPLOAD_CHUNK, f.size);
      let sent = false;
      for (let attempt = 0; !sent; attempt++) {
        if (ctl.cancelled) throw Object.assign(new Error('cancelled'), {cancelled: true});
        try {
          const r = await fetch(BASE + '/api/upload/' + id + '?offset=' + offset,
                                {method: 'PUT', body: f.slice(offset, end), signal: ctl.abort.signal});
          if (r.status === 409) {          // server disagrees: trust it and re-slice
            offset = (await api('/upload/' + id)).offset;
            continue;
          }
          if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), {status: r.status, body: await r.text()});
          offset = (await r.json()).offset;
          sent = true;
        } catch (err) {
          if (ctl.cancelled) throw Object.assign(new Error('cancelled'), {cancelled: true});
          // a 4xx (too big, disk full, bad file) will not get better by waiting
          if (err.status && err.status < 500 && err.status !== 408 && err.status !== 429) throw Object.assign(err, {permanent: true});
          if (attempt >= UPLOAD_BACKOFF_S.length) throw err;
          setUpStatus('connection lost, retrying (' + (attempt + 1) + ')…');
          await whenOnline();
          await sleep(UPLOAD_BACKOFF_S[attempt] * 1000);
          try { offset = (await api('/upload/' + id)).offset; } catch (e2) { /* retry blind */ }
        }
      }
      onProgress(offset / f.size, offset, f.size);
    }
    const r = await post('/api/upload/' + id + '/finish', {filename: f.name, match_type: matchType, ...target});
    setPendingUpload(null); finish();
    done(r);
  } catch (e) {
    finish();
    if (e.cancelled) {
      setPendingUpload(null);
      fetch(BASE + '/api/upload/' + id, {method: 'DELETE'}).catch(() => {});
      (opts.onCancel || resetDrop)(); toast('Upload cancelled'); return;
    }
    if (e.permanent) { setPendingUpload(null); fetch(BASE + '/api/upload/' + id, {method: 'DELETE'}).catch(() => {}); }
    // otherwise the id stays: pick the same file again and it resumes
    fail(parseDetail(e.body) || e.message);
  }
}
function parseDetail(body) {
  try { return JSON.parse(body).detail; } catch (e) { return null; }
}
function setUpProgress(frac, loaded, total) {
  const pct = Math.round(frac * 100);
  $('upBar').style.width = pct + '%';
  $('upPct').textContent = pct + '%';
  $('upMb').textContent = fmtMB(loaded) + ' / ' + fmtMB(total);
}
function resetDrop() {
  drop.classList.remove('busy');
  $('dropTitle').textContent = 'Drop your match film here';
  $('dropSub').textContent = 'or click to choose · MP4 / MOV / M4V · up to 2 GB';
  showPendingUpload();
  $('upBarWrap').style.display = 'none'; $('upMeta').style.display = 'none';
  $('upBar').style.width = '0';
  fileIn.value = '';
}
// an upload that did not finish (reload, phone slept, long dead zone): say so, and that re-picking resumes it
function showPendingUpload() {
  const p = pendingUpload();
  if (p && p.name && !uploading) $('dropSub').textContent = 'Unfinished upload of ' + p.name + ': choose the same file to pick up where it stopped';
}
showPendingUpload();
