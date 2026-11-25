'use strict';
/* ---------------- calibration ---------------- */
const CAL_STEPS_BASE = [
  { chip:'C1', cls:'', title:'Far-left court corner', sub:'Baseline meets the left sideline on the FAR side of the net. Cut off by the camera? Click its estimated spot in the hatched border.' },
  { chip:'C2', cls:'', title:'Far-right court corner', sub:'Baseline × right sideline, far side. Off-frame estimates in the border are fine.' },
  { chip:'C3', cls:'', title:'Near-right court corner', sub:'The corner closest to the camera, right side.' },
  { chip:'C4', cls:'', title:'Near-left court corner', sub:'The corner closest to the camera, left side.' },
  { chip:'K1', cls:'k', title:'Far-left kitchen corner', sub:'Where the far kitchen line meets the left sideline. Kitchen points anchor the whole calibration — be precise, must be in frame.' },
  { chip:'K2', cls:'k', title:'Far-right kitchen corner', sub:'Far kitchen line × right sideline.' },
  { chip:'K3', cls:'k', title:'Near-right kitchen corner', sub:'Near kitchen line × right sideline.' },
  { chip:'K4', cls:'k', title:'Near-left kitchen corner', sub:'Near kitchen line × left sideline.' },
  { chip:'YOU', cls:'me', title:'Click yourself', sub:'Click your body so the coach knows which player to follow.' },
];
const CAL_STEP_PARTNER = { chip:'P', cls:'partner', title:'Click your partner', sub:'Click your partner\'s body so we know who\'s on your team.' };
function calSteps() { return matchType === 'doubles' ? [...CAL_STEPS_BASE, CAL_STEP_PARTNER] : CAL_STEPS_BASE; }
const PADF = 0.25;
let calClicks = [], calImg = null, padX = 0, padY = 0;

let lastCalibration = null;
function startCalib(mt) {
  // matchType is a page-global set by the upload form's Singles/Doubles
  // toggle — correct right after a fresh upload, but stale (or never set)
  // when calibration is reached any other way (opening an existing
  // pending session, Recalibrate). Callers with the session's real
  // meta.match_type in hand should pass it so the right calibration steps
  // (partner click, for doubles) actually render.
  if (mt) setMatchType(mt);
  clearInterval(pollTimer);
  show('calib'); calClicks = [];
  calImg = new Image();
  calImg.onload = () => {
    const c = $('calCanvas');
    padX = Math.round(calImg.width * PADF);
    padY = Math.round(calImg.height * PADF);
    c.width = calImg.width + 2 * padX;
    c.height = calImg.height + 2 * padY;
    drawCal();
    checkLastCalibration();
  };
  calImg.src = frameURL();
  updateCalUI();
}
async function checkLastCalibration() {
  const btn = $('calReuseBtn');
  btn.style.display = 'none';
  if (DEMO) return;
  try { lastCalibration = await api('/last-calibration'); }
  catch (e) { return; }
  if (lastCalibration && lastCalibration.available) btn.style.display = 'inline-block';
}
function reuseLastCalibration() {
  if (!lastCalibration || !lastCalibration.available) return;
  const fs = lastCalibration.frame_size;
  if (fs && calImg && (fs[0] !== calImg.width || fs[1] !== calImg.height)) {
    toast('Heads up: last calibration was a different frame size — points may be off. Review before analyzing.', true);
  }
  calClicks = [...lastCalibration.corners_px, ...(lastCalibration.kitchen_px || [])];
  drawCal(); updateCalUI();
}
function updateCalUI() {
  const steps = calSteps(), total = steps.length, n = calClicks.length;
  $('calSteps').innerHTML = steps.map((s, i) =>
    '<span class="step ' + s.cls + (i < n ? ' done' : i === n ? ' act' : '') + '">' + s.chip + '</span>').join('');
  const h = $('calHint');
  if (n < total) {
    const s = steps[n];
    const col = n < 4 ? 'var(--accent)' : n < 8 ? 'var(--amber)' : n === 8 ? 'var(--red)' : '#8b6bf0';
    h.innerHTML = '<span class="dot" style="background:' + col + '"></span><div><b>' + (n+1) + ' / ' + total + ' — ' + s.title + '</b><span>' + s.sub + '</span></div>';
  } else {
    h.innerHTML = '<span class="dot" style="background:var(--accent)"></span><div><b>All ' + total + ' points set</b><span>Review the overlay, then hit Analyze. You can undo or start over.</span></div>';
  }
  $('calGo').disabled = n !== total;
}
function drawCal() {
  const c = $('calCanvas'), ctx = c.getContext('2d');
  ctx.fillStyle = '#0e1116'; ctx.fillRect(0, 0, c.width, c.height);
  ctx.strokeStyle = 'rgba(91,130,232,.10)'; ctx.lineWidth = 1;
  for (let d = -c.height; d < c.width; d += 28) {
    ctx.beginPath(); ctx.moveTo(d, 0); ctx.lineTo(d + c.height, c.height); ctx.stroke();
  }
  if (calImg && calImg.complete && calImg.width) ctx.drawImage(calImg, padX, padY);
  ctx.strokeStyle = '#3a4456'; ctx.lineWidth = 2; ctx.strokeRect(padX, padY, calImg.width, calImg.height);
  const quad = (idx, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.setLineDash([8, 6]); ctx.beginPath();
    idx.forEach((k, j) => { const p = calClicks[k];
      j ? ctx.lineTo(p[0] + padX, p[1] + padY) : ctx.moveTo(p[0] + padX, p[1] + padY); });
    ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
  };
  if (calClicks.length >= 4) quad([0,1,2,3], 'rgba(91,130,232,.9)');
  if (calClicks.length >= 8) quad([4,5,6,7], 'rgba(240,166,61,.9)');
  const steps = calSteps();
  calClicks.forEach((p, i) => {
    const x = p[0] + padX, y = p[1] + padY;
    const col = i < 4 ? '#5b82e8' : i < 8 ? '#f0a63d' : i === 8 ? '#ff5c5c' : '#8b6bf0';
    ctx.beginPath(); ctx.arc(x, y, 13, 0, 7); ctx.fillStyle = 'rgba(0,0,0,.5)'; ctx.fill();
    ctx.beginPath(); ctx.arc(x, y, 10, 0, 7); ctx.fillStyle = col; ctx.fill();
    ctx.fillStyle = '#fff'; ctx.font = '800 11px Archivo, sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(steps[i].chip, x, y + 4);
  });
}
const calCanvas = $('calCanvas'), loupe = $('loupe'), loupeC = $('loupeCanvas');
calCanvas.addEventListener('mousemove', e => {
  const r = calCanvas.getBoundingClientRect();
  const cx = (e.clientX - r.left) * calCanvas.width / r.width;
  const cy = (e.clientY - r.top) * calCanvas.height / r.height;
  const lx = e.clientX - r.left, ly = e.clientY - r.top;
  loupe.style.display = 'block';
  loupe.style.left = (lx + (lx > r.width - 150 ? -140 : 20)) + 'px';
  loupe.style.top = (ly + (ly > r.height - 150 ? -140 : 20)) + 'px';
  const lctx = loupeC.getContext('2d');
  lctx.imageSmoothingEnabled = false;
  lctx.fillStyle = '#000'; lctx.fillRect(0, 0, 120, 120);
  const Z = 3, half = 60 / Z;
  lctx.drawImage(calCanvas, cx - half, cy - half, half * 2, half * 2, 0, 0, 120, 120);
  lctx.strokeStyle = 'rgba(91,130,232,.9)'; lctx.lineWidth = 1;
  lctx.beginPath(); lctx.moveTo(60, 46); lctx.lineTo(60, 74); lctx.moveTo(46, 60); lctx.lineTo(74, 60); lctx.stroke();
});
calCanvas.addEventListener('mouseleave', () => loupe.style.display = 'none');
calCanvas.onclick = e => {
  if (calClicks.length >= calSteps().length) return;
  const r = calCanvas.getBoundingClientRect();
  const cx = (e.clientX - r.left) * calCanvas.width / r.width;
  const cy = (e.clientY - r.top) * calCanvas.height / r.height;
  const fx = cx - padX, fy = cy - padY;
  const inFrame = fx >= 0 && fy >= 0 && fx <= calImg.width && fy <= calImg.height;
  if (calClicks.length >= 4 && !inFrame) { toast('Kitchen corners and player positions must be inside the frame', true); return; }
  calClicks.push([fx, fy]);
  drawCal(); updateCalUI();
};
function undoCal() { calClicks.pop(); drawCal(); updateCalUI(); }
function resetCal() { calClicks = []; drawCal(); updateCalUI(); }

async function submitCal() {
  const body = { corners_px: calClicks.slice(0, 4), kitchen_px: calClicks.slice(4, 8), self_px: calClicks[8] };
  if (matchType === 'doubles') body.partner_px = calClicks[9];
  try {
    await api('/session/' + sid + '/calibrate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) { toast('Calibration failed: ' + e, true); return; }
  lastMsgStage = null;
  show('proc'); renderProcSteps('queued');
  waitFor(s => s.stage === 'done', showResults, 'Analyzing');
}
async function reanalyze() {
  try { await api('/session/' + sid + '/reprocess', { method: 'POST' }); }
  catch (e) { toast('Could not re-analyze: ' + e, true); return; }
  lastMsgStage = null;
  show('proc'); renderProcSteps('queued');
  waitFor(s => s.stage === 'done', showResults, 'Re-analyzing');
}
