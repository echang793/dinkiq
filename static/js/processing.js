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
function renderProcSteps(stage) {
  const idx = STAGE_ORDER.indexOf(stage);
  $('procSteps').innerHTML = STAGE_ORDER.map((st, i) =>
    '<div class="pstep ' + (i < idx || stage === 'done' ? 'done' : i === idx ? 'act' : '') + '">' +
    '<span class="ring"></span><span class="lbl">' + STAGE_NAMES[st] + '</span></div>').join('');
  drawProcRing(stage === 'done' ? 1 : Math.max(0, idx) / (STAGE_ORDER.length - 1));
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
function waitFor(pred, then, label, quiet) {
  clearInterval(pollTimer);
  const tick = async () => {
    let s;
    try { s = await api('/session/' + sid); } catch (e) { return; }
    if (s.state === 'error') { clearInterval(pollTimer); toast(label + ' failed: ' + (s.error || 'unknown error'), true); goHome(); return; }
    if (!quiet) {
      if (s.stage !== lastMsgStage) {
        lastMsgStage = s.stage;
        const m = $('procMsg');
        m.classList.add('swap');
        setTimeout(() => { m.textContent = (STAGE_FLAVOR[s.stage] || 'Working…'); m.classList.remove('swap'); }, 280);
        renderProcSteps(s.stage);
      }
      const pct = Math.round(100 * (s.overall || 0));
      $('procBar').style.width = pct + '%';
      $('procPct').textContent = pct + '%';
      $('procEta').textContent = s.eta_seconds != null ? fmtEta(s.eta_seconds) : '';
    }
    if (pred(s)) { clearInterval(pollTimer); then(s); }
  };
  tick();
  pollTimer = setInterval(tick, 1500);
}
