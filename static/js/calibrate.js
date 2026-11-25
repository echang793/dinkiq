'use strict';
/* ---------------- calibration (touch-first) ----------------
   Touch and hold: a magnifier floats above the finger and the target floats just above the
   fingertip, so the thing being placed is never hidden by the thing placing it. Slide to the exact
   spot, lift to set it. Placed points can be dragged. Pinch/two-finger drag zooms and pans (the
   picture can be panned out past its edge: a cut-off court corner is tapped where it would be).
   Once 4 points are down, the fitted court is drawn over the frame so a bad tap shows up here
   instead of as a wrong analysis ten minutes later. All sizes are CSS pixels. */
const CAL_STEPS_BASE = [
  { chip:'C1', cls:'', title:'Far-left corner of the court', sub:'The far side of the net, left side: where the back line meets the left sideline. If the camera cuts it off, tap where it would be. You can zoom and pan past the edge of the picture.' },
  { chip:'C2', cls:'', title:'Far-right corner of the court', sub:'Same back line, right sideline. Off-picture is fine: tap your best estimate.' },
  { chip:'C3', cls:'', title:'Near-right corner of the court', sub:'The corner closest to the camera, on the right.' },
  { chip:'C4', cls:'', title:'Near-left corner of the court', sub:'The corner closest to the camera, on the left.' },
  { chip:'K1', cls:'k', title:'Far-left kitchen corner', sub:'The kitchen is the 7-foot no-volley zone beside the net. Tap where its far line meets the left sideline. This one must be inside the picture, and it matters most: be precise.' },
  { chip:'K2', cls:'k', title:'Far-right kitchen corner', sub:'Far kitchen line, right sideline.' },
  { chip:'K3', cls:'k', title:'Near-right kitchen corner', sub:'Near kitchen line, right sideline.' },
  { chip:'K4', cls:'k', title:'Near-left kitchen corner', sub:'Near kitchen line, left sideline.' },
  { chip:'YOU', cls:'me', title:'Tap yourself', sub:'Tap the middle of your body, so we follow the right player.' },
];
const CAL_STEP_PARTNER = { chip:'P', cls:'partner', title:'Tap your partner', sub:'Tap the middle of your partner\'s body.' };
function calSteps() { return matchType === 'doubles' ? [...CAL_STEPS_BASE, CAL_STEP_PARTNER] : CAL_STEPS_BASE; }
const CAL_COLORS = i => i < 4 ? '#5b82e8' : i < 8 ? '#f0a63d' : i === 8 ? '#ff5c5c' : '#8b6bf0';

let calClicks = [], calImg = null, lastCalibration = null;
const calView = { s: 1, tx: 0, ty: 0 };
let calW = 0, calH = 0, calDpr = 1, calFull = false, calPanMode = false;
let calG = null, calPinch = null, calFitH = null, calFitErrs = null;
const calPtrs = new Map();
const CAL_LIFT = 46;          // px the target floats above a finger
const CAL_OUTSIDE = 0.25;     // corners may be tapped this far (fraction of the frame) outside the picture

function startCalib(mt) {
  // matchType is a page-global set by the upload form's Singles/Doubles toggle: right after an
  // upload it is correct, otherwise (opening a pending session, Recalibrate) pass the session's own
  // meta.match_type so the right steps (partner tap for doubles) render.
  if (mt) setMatchType(mt);
  stopPoll();
  setCalFull(false);
  show('calib'); calClicks = []; calG = null; calPinch = null; calPtrs.clear();
  $('calHelp').textContent = matchMedia('(pointer:coarse)').matches
    ? 'Touch and hold: a magnifier appears above your finger. Slide to the exact spot, then lift. Drag a placed point to fix it. Pinch to zoom; two fingers to pan.'
    : 'Click to place a point. Scroll to zoom; hold Shift and drag (or press Pan) to move around. Drag any placed point to adjust it.';
  calImg = new Image();
  calImg.onload = () => {
    sizeCal(); calFit(); recomputeFit(); updateCalUI();
    checkLastCalibration();
    if (matchMedia('(pointer:coarse)').matches && innerWidth < 800) setCalFull(true);
  };
  calImg.src = frameURL();
  updateCalUI();
}

/* ---------- view ---------- */
function sizeCal() {
  const c = $('calCanvas'), r = c.getBoundingClientRect();
  calW = r.width; calH = r.height; calDpr = Math.min(window.devicePixelRatio || 1, 3);
  c.width = Math.round(calW * calDpr); c.height = Math.round(calH * calDpr);
}
function calFitScale() { return calImg ? Math.min(calW / (calImg.width * (1 + 2 * 0.05)), calH / (calImg.height * (1 + 2 * 0.05))) : 1; }
function calFit() {
  if (!calImg || !calW) return;
  const s = calFitScale();
  calView.s = s; calView.tx = (calW - calImg.width * s) / 2; calView.ty = (calH - calImg.height * s) / 2;
  drawCal();
}
function clampView() {
  const fs = calFitScale();
  calView.s = Math.max(fs * 0.6, Math.min(fs * 10, calView.s));
  const iw = calImg.width * calView.s, ih = calImg.height * calView.s, keep = 80;
  calView.tx = Math.min(calW - keep, Math.max(keep - iw, calView.tx));
  calView.ty = Math.min(calH - keep, Math.max(keep - ih, calView.ty));
}
const toScreen = p => [p[0] * calView.s + calView.tx, p[1] * calView.s + calView.ty];
const toImg = (x, y) => [(x - calView.tx) / calView.s, (y - calView.ty) / calView.s];
function calZoom(factor, cx, cy) {
  if (!calImg) return;
  if (cx == null) { cx = calW / 2; cy = calH / 2; }
  const before = calView.s;
  calView.s *= factor; clampView();
  const k = calView.s / before;
  calView.tx = cx - (cx - calView.tx) * k; calView.ty = cy - (cy - calView.ty) * k;
  clampView(); drawCal();
}
function toggleCalPan() {
  calPanMode = !calPanMode;
  $('calPanBtn').setAttribute('aria-pressed', String(calPanMode));
  $('calCanvas').classList.toggle('panning', calPanMode);
}
let calHome = null;   // where the wrap lives when it is not full screen
function setCalFull(on) {
  calFull = on;
  const wrap = $('calWrap');
  // position:fixed is relative to the nearest transformed ancestor, and the card animates with a
  // transform, so a full-screen wrap has to live directly under <body> while it is full
  if (on && !calHome) { calHome = document.createComment('calwrap'); wrap.replaceWith(calHome); document.body.appendChild(wrap); }
  if (!on && calHome) { calHome.replaceWith(wrap); calHome = null; }
  wrap.classList.toggle('full', on);
  document.body.style.overflow = on ? 'hidden' : '';
  $('calFullBtn').style.display = on ? 'none' : '';
  requestAnimationFrame(() => { sizeCal(); calFit(); });
}
addEventListener('keydown', e => { if (e.key === 'Escape' && calFull) setCalFull(false); });
if (window.ResizeObserver) new ResizeObserver(() => { if (calImg && calImg.complete) { sizeCal(); calFit(); } }).observe($('calWrap'));

/* ---------- fit ---------- */
function recomputeFit() {
  const n = Math.min(calClicks.length, 8);
  calFitH = null; calFitErrs = null;
  if (!calImg || n < 4) return;
  const tg = CAL_CORNER_TARGETS.concat(CAL_KITCHEN_TARGETS);
  const pairs = [];
  for (let i = 0; i < n; i++) pairs.push([tg[i], calClicks[i]]);
  calFitH = fitHomography(pairs, calImg.width);
  if (calFitH && n === 8) calFitErrs = reprojectionErrors(calFitH, pairs);
}
function calBadLimit() { return 0.012 * (calImg ? calImg.width : 1280); }   // ~15 px on a 1280-wide frame

/* ---------- ui ---------- */
function stepDiagram(i) {
  // a small camera-view court with the point we want highlighted
  const H = fitHomography([[[0, 0], [52, 12]], [[20, 0], [108, 12]], [[20, 44], [152, 92]], [[0, 44], [8, 92]]], 160);
  const P = (x, y) => courtToImage(H, x, y).map(v => v.toFixed(1)).join(',');
  const lines = COURT_LINES.map(([a, b]) => { const p = P(a[0], a[1]).split(','), q = P(b[0], b[1]).split(',');
    return '<line x1="' + p[0] + '" y1="' + p[1] + '" x2="' + q[0] + '" y2="' + q[1] + '" stroke="#6b7a90" stroke-width="1.5"/>'; }).join('');
  const target = i < 4 ? CAL_CORNER_TARGETS[i] : i < 8 ? CAL_KITCHEN_TARGETS[i - 4] : i === 8 ? [10, 36] : [16, 34];
  const t = P(target[0], target[1]).split(',');
  const col = CAL_COLORS(i);
  return '<svg viewBox="0 0 160 100" aria-hidden="true">' + lines +
    '<circle cx="' + t[0] + '" cy="' + t[1] + '" r="9" fill="' + col + '" fill-opacity=".25"/><circle cx="' + t[0] + '" cy="' + t[1] + '" r="5" fill="' + col + '"/></svg>';
}
function updateCalUI() {
  const steps = calSteps(), total = steps.length, n = calClicks.length;
  $('calSteps').innerHTML = steps.map((s, i) =>
    '<span class="step ' + s.cls + (i < n ? ' done' : i === n ? ' act' : '') + '">' + s.chip + '</span>').join('');
  const h = $('calHint');
  if (n < total) {
    const s = steps[n];
    h.innerHTML = '<div class="diagram">' + stepDiagram(n) + '</div><div><b>' + (n + 1) + ' of ' + total + ': ' + s.title + '</b><span>' + s.sub + '</span></div>';
    $('calMiniText').textContent = (n + 1) + '/' + total + ' · ' + s.title;
  } else {
    h.innerHTML = '<span class="dot" style="background:var(--accent)"></span><div><b>All ' + total + ' points set</b><span>Check that the green court lines sit on the real lines, drag any point that is off, then analyze.</span></div>';
    $('calMiniText').textContent = 'All ' + total + ' points set';
  }
  const fit = $('calFitMsg');
  fit.className = 'calfit';
  if (n >= 8 && calFitErrs) {
    const w = calFitErrs.reduce((bi, e, i) => e > calFitErrs[bi] ? i : bi, 0);
    if (calFitErrs[w] > calBadLimit()) {
      fit.className = 'calfit bad';
      fit.textContent = steps[w].chip + ' looks off by about ' + Math.round(calFitErrs[w]) + ' px. Drag it onto the exact corner; the green lines should sit on the real court lines.';
    } else {
      fit.className = 'calfit ok';
      fit.textContent = 'The court fits your points. ✓ Check the green lines sit on the real lines.';
    }
  } else if (n >= 4 && calFitH) {
    fit.textContent = 'Green lines: the court as fitted from your corners. The kitchen points will sharpen it.';
  } else {
    fit.textContent = '';
  }
  $('calGo').disabled = n !== total;
}

/* ---------- drawing ---------- */
function drawCal() {
  const c = $('calCanvas');
  if (!c || !calW) return;
  const ctx = c.getContext('2d');
  ctx.setTransform(calDpr, 0, 0, calDpr, 0, 0);
  ctx.fillStyle = '#0e1116'; ctx.fillRect(0, 0, calW, calH);
  if (calImg && calImg.complete && calImg.width) {
    ctx.drawImage(calImg, calView.tx, calView.ty, calImg.width * calView.s, calImg.height * calView.s);
    ctx.strokeStyle = '#3a4456'; ctx.lineWidth = 1.5;
    ctx.strokeRect(calView.tx, calView.ty, calImg.width * calView.s, calImg.height * calView.s);
  }
  if (calFitH) {
    ctx.save(); ctx.lineWidth = 2; ctx.strokeStyle = 'rgba(74,222,128,.95)';
    COURT_LINES.forEach(([a, b], k) => {
      const p = toScreen(courtToImage(calFitH, a[0], a[1])), q = toScreen(courtToImage(calFitH, b[0], b[1]));
      if (![...p, ...q].every(v => isFinite(v) && Math.abs(v) < 1e5)) return;
      ctx.setLineDash(k < 4 ? [] : [7, 5]);
      ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
    });
    ctx.restore();
  }
  const steps = calSteps(), limit = calBadLimit();
  calClicks.forEach((pt, i) => {
    const [x, y] = toScreen(pt);
    const dragging = calG && calG.type === 'move' && calG.i === i;
    const off = calFitErrs && calFitErrs[i] > limit;
    ctx.beginPath(); ctx.arc(x, y, (dragging ? 15 : 12) + 2, 0, 7); ctx.fillStyle = off ? '#f0a63d' : 'rgba(0,0,0,.55)'; ctx.fill();
    ctx.beginPath(); ctx.arc(x, y, dragging ? 15 : 12, 0, 7); ctx.fillStyle = CAL_COLORS(i); ctx.fill();
    ctx.fillStyle = '#fff'; ctx.font = '800 10px Archivo, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(steps[i].chip, x, y + 0.5);
  });
  if (calG && calG.target) {            // the reticle
    const [x, y] = calG.target;
    ctx.save(); ctx.lineWidth = 3; ctx.strokeStyle = '#fff';
    ctx.beginPath(); ctx.arc(x, y, 14, 0, 7); ctx.moveTo(x - 24, y); ctx.lineTo(x - 8, y); ctx.moveTo(x + 8, y); ctx.lineTo(x + 24, y);
    ctx.moveTo(x, y - 24); ctx.lineTo(x, y - 8); ctx.moveTo(x, y + 8); ctx.lineTo(x, y + 24); ctx.stroke();
    ctx.lineWidth = 1.5; ctx.strokeStyle = CAL_COLORS(calG.type === 'move' ? calG.i : calClicks.length); ctx.stroke();
    ctx.restore();
  }
}
function drawLoupe(sx, sy) {
  const L = $('loupe'), size = 120;
  let lx = sx - size / 2, ly = sy - size - 40;
  if (ly < 6) ly = sy + 40;                                  // no room above: put it below
  lx = Math.max(6, Math.min(calW - size - 6, lx));
  L.style.left = lx + 'px'; L.style.top = ly + 'px'; L.style.display = 'block';
  const ctx = L.getContext('2d');
  ctx.setTransform(2, 0, 0, 2, 0, 0);
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, size, size);
  const ip = toImg(sx, sy), half = size / (2 * calView.s * 3);   // 3x what is on screen
  try { ctx.drawImage(calImg, ip[0] - half, ip[1] - half, half * 2, half * 2, 0, 0, size, size); } catch (e) {}
  if (calFitH) {
    ctx.strokeStyle = 'rgba(74,222,128,.9)'; ctx.lineWidth = 1.5;
    COURT_LINES.forEach(([a, b]) => {
      const p = courtToImage(calFitH, a[0], a[1]), q = courtToImage(calFitH, b[0], b[1]);
      const f = v => [(v[0] - ip[0]) * calView.s * 3 + size / 2, (v[1] - ip[1]) * calView.s * 3 + size / 2];
      const P = f(p), Q = f(q);
      if (![...P, ...Q].every(v => isFinite(v) && Math.abs(v) < 1e5)) return;
      ctx.beginPath(); ctx.moveTo(P[0], P[1]); ctx.lineTo(Q[0], Q[1]); ctx.stroke();
    });
  }
  ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.beginPath();
  ctx.moveTo(size / 2, size / 2 - 14); ctx.lineTo(size / 2, size / 2 + 14); ctx.moveTo(size / 2 - 14, size / 2); ctx.lineTo(size / 2 + 14, size / 2); ctx.stroke();
}
const hideLoupe = () => { $('loupe').style.display = 'none'; };

/* ---------- pointer gestures ---------- */
const calCanvas = $('calCanvas');
const cpos = e => { const r = calCanvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
function hitPoint(x, y, radius) {
  let best = -1, bd = radius;
  calClicks.forEach((p, i) => { const s = toScreen(p), d = Math.hypot(s[0] - x, s[1] - y); if (d <= bd) { bd = d; best = i; } });
  return best;
}
function allowedAt(i, ip) {
  const iw = calImg.width, ih = calImg.height;
  if (i < 4) return ip[0] >= -CAL_OUTSIDE * iw && ip[0] <= (1 + CAL_OUTSIDE) * iw && ip[1] >= -CAL_OUTSIDE * ih && ip[1] <= (1 + CAL_OUTSIDE) * ih;
  return ip[0] >= 0 && ip[0] <= iw && ip[1] >= 0 && ip[1] <= ih;
}
function cancelGesture() {
  if (calG && calG.type === 'move') calClicks[calG.i] = calG.orig;
  calG = null; hideLoupe(); recomputeFit(); updateCalUI(); drawCal();
}
function setTarget(x, y) {
  calG.target = calG.type === 'move' ? [x + calG.dx, y + calG.dy] : [x, y - calG.lift];
  if (calG.type === 'move') { calClicks[calG.i] = toImg(calG.target[0], calG.target[1]); recomputeFit(); }
  drawCal(); drawLoupe(calG.target[0], calG.target[1]);
}
calCanvas.addEventListener('pointerdown', e => {
  if (!calImg || !calImg.complete) return;
  e.preventDefault();
  try { calCanvas.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
  calPtrs.set(e.pointerId, cpos(e));
  if (calPtrs.size === 2) {                      // second finger: zoom/pan, abandon any placement
    cancelGesture();
    const [a, b] = [...calPtrs.values()];
    calPinch = { d0: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, m0: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], s0: calView.s, tx0: calView.tx, ty0: calView.ty };
    return;
  }
  if (calPtrs.size > 2) return;
  const [x, y] = cpos(e), touch = e.pointerType !== 'mouse';
  const total = calSteps().length;
  if (calPanMode || e.button === 1 || e.shiftKey) { calG = { type: 'pan', x, y, tx: calView.tx, ty: calView.ty }; return; }
  const hit = hitPoint(x, y, touch ? 30 : 16);
  if (hit >= 0) {
    const sp = toScreen(calClicks[hit]);
    calG = { type: 'move', i: hit, dx: sp[0] - x, dy: sp[1] - y, orig: calClicks[hit].slice(), x, y, t0: performance.now() };
    setTarget(x, y); return;
  }
  if (calClicks.length >= total) { calG = { type: 'pan', x, y, tx: calView.tx, ty: calView.ty }; return; }
  calG = { type: 'place', lift: touch ? CAL_LIFT : 0, touch, x, y, t0: performance.now(), moved: 0 };
  setTarget(x, y);
});
calCanvas.addEventListener('pointermove', e => {
  if (!calPtrs.has(e.pointerId)) {                // hovering mouse: show the loupe like a cursor
    if (e.pointerType === 'mouse' && calImg && calImg.complete && !calPanMode && calClicks.length < calSteps().length) {
      const [x, y] = cpos(e); calG = null; drawCal(); drawLoupe(x, y);
    }
    return;
  }
  calPtrs.set(e.pointerId, cpos(e));
  if (calPinch && calPtrs.size >= 2) {
    const [a, b] = [...calPtrs.values()];
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    calView.s = calPinch.s0 * d / calPinch.d0; clampView();
    const k = calView.s / calPinch.s0;
    calView.tx = m[0] - (calPinch.m0[0] - calPinch.tx0) * k; calView.ty = m[1] - (calPinch.m0[1] - calPinch.ty0) * k;
    clampView(); drawCal(); return;
  }
  if (!calG) return;
  const [x, y] = cpos(e);
  if (calG.type === 'pan') { calView.tx = calG.tx + x - calG.x; calView.ty = calG.ty + y - calG.y; clampView(); drawCal(); return; }
  calG.moved = Math.max(calG.moved || 0, Math.hypot(x - calG.x, y - calG.y));
  setTarget(x, y);
});
function endPointer(e, cancelled) {
  calPtrs.delete(e.pointerId);
  if (calPinch) { if (calPtrs.size < 2) calPinch = null; return; }
  const g = calG;
  if (!g || cancelled) { if (g) cancelGesture(); return; }
  if (g.type === 'pan') { calG = null; return; }
  if (g.type === 'place') {
    // a quick tap with a finger means "here", not "above here": only a press-and-slide gets the lift
    const quick = g.touch && performance.now() - g.t0 < 220 && g.moved < 8;
    const [x, y] = cpos(e);
    const target = quick ? [x, y] : g.target;
    const ip = toImg(target[0], target[1]);
    calG = null; hideLoupe();
    if (!allowedAt(calClicks.length, ip)) {
      toast(calClicks.length < 4 ? 'That is too far outside the picture' : 'This point has to be inside the picture', true);
      drawCal(); return;
    }
    calClicks.push(ip);
  } else if (g.type === 'move') {
    const ip = calClicks[g.i];
    calG = null; hideLoupe();
    if (!allowedAt(g.i, ip)) { calClicks[g.i] = g.orig; toast('That point has to stay inside the picture', true); }
  }
  recomputeFit(); updateCalUI(); drawCal();
  if (calClicks.length === calSteps().length && calFull) setCalFull(false);   // done: back to the page to review and analyze
}
calCanvas.addEventListener('pointerup', e => endPointer(e, false));
calCanvas.addEventListener('pointercancel', e => endPointer(e, true));
calCanvas.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse' && !calG) { hideLoupe(); } });
calCanvas.addEventListener('wheel', e => {
  e.preventDefault();
  const [x, y] = cpos(e);
  calZoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, x, y);
}, { passive: false });

/* ---------- actions ---------- */
function undoCal() { calClicks.pop(); recomputeFit(); updateCalUI(); drawCal(); }
function resetCal() { calClicks = []; recomputeFit(); updateCalUI(); drawCal(); }

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
    toast('Heads up: the last calibration was a different frame size. Check the green lines before analyzing.', true);
  }
  calClicks = [...lastCalibration.corners_px, ...(lastCalibration.kitchen_px || [])];
  recomputeFit(); updateCalUI(); drawCal();
}

async function submitCal() {
  setCalFull(false);
  const body = { corners_px: calClicks.slice(0, 4), kitchen_px: calClicks.slice(4, 8), self_px: calClicks[8] };
  if (matchType === 'doubles') body.partner_px = calClicks[9];
  try {
    await api('/session/' + sid + '/calibrate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) { toast('Calibration failed: ' + e, true); return; }
  routeSession({session_id: sid, stage: 'queued', state: 'queued', overall: 0});
}
async function reanalyze() {
  try { await api('/session/' + sid + '/reprocess', { method: 'POST' }); }
  catch (e) { toast('Could not re-analyze: ' + e, true); return; }
  routeSession({session_id: sid, stage: 'queued', state: 'queued', overall: 0});
}
