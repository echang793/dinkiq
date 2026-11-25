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
function doUpload(f) {
  const ok = /\.(mp4|mov|m4v)$/i.test(f.name);
  if (!ok) return toast('Only MP4 / MOV / M4V files are supported', true);
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
  resumableUpload(f, matchType, done, fail);
}

// Chunked + resumable. A single POST of a multi-GB clip over cellular loses
// the whole transfer on one dropped connection; here a failed chunk is
// retried from the offset the server reports it actually has.
const UPLOAD_CHUNK = 8 << 20;   // 8 MB
const UPLOAD_RETRIES = 5;
// opts.sessionId: replace that session's video instead of creating a session.
// opts.onProgress(frac, loaded, total): defaults to the Home upload bar.
async function resumableUpload(f, matchType, done, fail, opts = {}) {
  const target = opts.sessionId ? {session_id: opts.sessionId} : {};
  const onProgress = opts.onProgress || setUpProgress;
  const post = async (url, body) => {
    const r = await fetch(BASE + url, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), {status: r.status, body: await r.text()});
    return r.json();
  };
  let init;
  try {
    init = await post('/api/upload/init', {filename: f.name, match_type: matchType, size: f.size, ...target});
  } catch (e) {
    // no resumable endpoints (older server) or no backend at all
    if (e.status === 404 || e.status === undefined) { enableDemo(); demoUpload(f, done); return; }
    fail(parseDetail(e.body) || e.message); return;
  }
  const id = init.upload_id;
  let offset = 0;
  try {
    while (offset < f.size) {
      const end = Math.min(offset + UPLOAD_CHUNK, f.size);
      let sent = false;
      for (let attempt = 0; attempt < UPLOAD_RETRIES && !sent; attempt++) {
        try {
          const r = await fetch(BASE + '/api/upload/' + id + '?offset=' + offset,
                                {method: 'PUT', body: f.slice(offset, end)});
          if (r.status === 409) {          // server disagrees — trust it and re-slice
            offset = (await api('/upload/' + id)).offset;
            continue;
          }
          if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), {body: await r.text()});
          offset = (await r.json()).offset;
          sent = true;
        } catch (err) {
          if (attempt === UPLOAD_RETRIES - 1) throw err;
          await new Promise(res => setTimeout(res, 500 * (attempt + 1)));  // backoff
          try { offset = (await api('/upload/' + id)).offset; } catch (e2) { /* retry blind */ }
        }
      }
      onProgress(offset / f.size, offset, f.size);
    }
    done(await post('/api/upload/' + id + '/finish', {filename: f.name, match_type: matchType, ...target}));
  } catch (e) {
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
  $('upBarWrap').style.display = 'none'; $('upMeta').style.display = 'none';
  $('upBar').style.width = '0';
  fileIn.value = '';
}
