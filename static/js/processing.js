'use strict';
/* ---------------- polling ---------------- */
const STAGE_ORDER = ['queued','ingest','tracking','metrics','events','shots','points','rating'];
const STAGE_NAMES = { queued:'Waiting in queue', ingest:'Preparing video', tracking:'Tracking players & ball',
  metrics:'Computing court metrics', events:'Finding your rallies', shots:'Classifying shots', points:'Scoring points & outcomes', rating:'Estimating your level' };
const STAGE_FLAVOR = { queued:'Grab some water — you\'re up next.', ingest:'Reading frames off your video.',
  tracking:'Following every player across the court, frame by frame.', metrics:'Mapping your movement onto real court coordinates.',
  events:'Cutting the film into rallies — dead time removed.', shots:'Drive, dink or drop? Watching every swing.',
  points:'Working out who won each rally — and why.',
  rating:'Comparing your patterns against rating benchmarks.' };

function fmtEta(s) {
  if (s == null || !isFinite(s) || s <= 0) return '';
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return '~' + (m ? m + ' min ' + String(sec).padStart(2,'0') + ' s' : sec + ' s') + ' remaining';
}
function renderProcSteps(stage, overall) {
  const idx = STAGE_ORDER.indexOf(stage);
  $('procSteps').innerHTML = STAGE_ORDER.map((st, i) =>
    '<div class="pstep ' + (i < idx || stage === 'done' ? 'done' : i === idx ? 'act' : '') + '">' +
    '<span class="ring"></span><span class="lbl">' + STAGE_NAMES[st] + '</span></div>').join('');
  drawProcRing(overall != null ? overall : 0);   // the SAME number as the bar below it
}
function drawProcRing(frac) {
  const R = 78, CIRC = 2 * Math.PI * R;
  $('procRing').innerHTML =
    '<svg width="190" height="190" viewBox="0 0 190 190">' +
      '<circle cx="95" cy="95" r="' + R + '" fill="none" style="stroke:var(--panel2)" stroke-width="10"/>' +
      '<circle cx="95" cy="95" r="' + R + '" fill="none" stroke-width="10" stroke-linecap="round" transform="rotate(-90 95 95)" stroke-dasharray="' + CIRC + '" stroke-dashoffset="' + (CIRC * (1 - frac)) + '" style="stroke:var(--accent);transition:stroke-dashoffset .8s cubic-bezier(.22,1,.36,1)"/>' +
      '<text x="95" y="90" text-anchor="middle" font-size="30" font-family="Archivo Black, sans-serif" font-style="italic" style="fill:var(--ink)">' + Math.round(frac * 100) + '</text>' +
      '<text x="95" y="116" text-anchor="middle" font-size="13" font-weight="600" font-family="Archivo, sans-serif" letter-spacing="2" style="fill:var(--dim)">PERCENT</text>' +
    '</svg>';
}
let lastMsgStage = null;
// Every poll belongs to one "generation". Leaving the screen (or starting another poll)
// bumps it, so a response that lands late can never repaint a screen that moved on.
let pollGen = 0;
function stopPoll() { clearInterval(pollTimer); pollGen++; }
const STALL_MS = 10 * 60 * 1000;   // no progress for this long -> say so instead of spinning forever

function setProcView(failed) {
  $('procWork').style.display = failed ? 'none' : '';
  $('procFail').style.display = failed ? '' : 'none';
  $('procTitle').textContent = failed ? 'This session needs attention' : 'Analyzing your match';
}
// A failed session used to toast and bounce Home, leaving no way to retry. Show what
// went wrong and the actions that can actually fix it.
function showFailed(s) {
  stopPoll();
  lastSession = s;   // the Replace-video flow reads the match type from here
  show('proc');
  setProcView(true);
  $('procErr').textContent = s.error || 'Something went wrong while processing this video.';
  const ingestFailed = s.stage === 'ingest';
  $('procFixBtns').innerHTML = ingestFailed
    ? '<button onclick="document.getElementById(\'mReuploadFile\').click()">Replace video</button>'
    : '<button onclick="startCalib(' + JSON.stringify((s.meta && s.meta.match_type) || null).replace(/"/g, '&quot;') + ')">Recalibrate</button>' +
      '<button class="ghost" onclick="reanalyze()">Re-analyze</button>';
  $('procFixBtns').insertAdjacentHTML('beforeend', '<button class="ghost" onclick="goHome()">Back</button>');
}
// One place decides which screen a session belongs on, for every way of arriving at it.
function routeSession(s) {
  if (s.state === 'error') return showFailed(s);
  if (s.stage === 'done' && s.metrics) return showResults(s);
  if (s.stage === 'ingest' && s.state === 'done') return startCalib(s.meta && s.meta.match_type);
  if (s.stage === 'done') return showFailed({...s, error: 'The analysis finished without producing results. Re-analyze to rebuild them.'});
  lastMsgStage = null; stopPoll(); setProcView(false); show('proc');
  renderProcSteps(s.stage, s.overall);
  // ingest finishing is the end of the road until the user calibrates; analysis ends at 'done'
  waitFor(x => x.stage === 'done' || (x.stage === 'ingest' && x.state === 'done'), routeSession, 'Analyzing');
}

function waitFor(pred, then, label, quiet) {
  clearInterval(pollTimer);
  const gen = ++pollGen, mySid = sid;
  let lastKey = '', lastChange = Date.now();
  const tick = async () => {
    let s;
    try { s = await api('/session/' + mySid); } catch (e) { return; }   // offline banner covers it
    if (gen !== pollGen || sid !== mySid) return;
    if (s.state === 'error') { stopPoll(); showFailed(s); return; }
    if (!quiet) {
      setProcView(false);
      if (s.stage !== lastMsgStage) {
        lastMsgStage = s.stage;
        const m = $('procMsg');
        m.classList.add('swap');
        setTimeout(() => { m.textContent = (STAGE_FLAVOR[s.stage] || 'Working…'); m.classList.remove('swap'); }, 280);
      }
      renderProcSteps(s.stage, s.overall || 0);
      const pct = Math.round(100 * (s.overall || 0));
      $('procBar').style.width = pct + '%';
      $('procPct').textContent = pct + '%';
      $('procBarWrap').setAttribute('aria-valuenow', pct);
      const key = s.stage + ':' + pct;
      if (key !== lastKey) { lastKey = key; lastChange = Date.now(); }
      let eta = s.eta_seconds != null ? fmtEta(s.eta_seconds) : '';
      if (s.stage === 'queued' && s.queue_position) eta = s.queue_position === 1 ? 'You\'re next' : (s.queue_position - 1) + ' ahead of you';
      if (Date.now() - lastChange > STALL_MS) eta = 'Taking longer than expected — it is still running. You can leave and come back.';
      $('procEta').textContent = eta;
    }
    if (pred(s)) { stopPoll(); then(s); }
  };
  tick();
  pollTimer = setInterval(tick, 1500);
}
