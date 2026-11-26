'use strict';
function findRallyIndexForTime(t) {
  const rallies = (lastSession && lastSession.events && lastSession.events.rallies) || [];
  for (let i = 0; i < rallies.length; i++) {
    if (t >= rallies[i].start - 0.01 && t <= rallies[i].end + 2.0) return i;
  }
  return -1;
}
function watchShotAt(t) {
  if (DEMO) { toast('Clip playback needs the live backend'); return; }
  const i = findRallyIndexForTime(t);
  if (i < 0) { toast("Couldn't find this shot in a rally clip"); return; }
  const rallies = lastSession.events.rallies;
  const clipStart = Math.max(0, rallies[i].start - 1.5);  // matches the padding clips are cut with
  const offset = Math.max(0, t - clipStart);
  const v = $('rallyVideo');
  playClip(i);
  // Polling instead of a 'loadedmetadata' listener: re-watching the SAME
  // clip re-assigns an identical src, which some browsers silently no-op
  // (no reload, no event) — a one-shot event listener would then just
  // never fire and the seek would be lost. Checking readyState directly
  // works regardless of whether a reload actually happened.
  let tries = 0;
  (function trySeek() {
    if (v.readyState >= 1) { v.currentTime = offset; return; }
    if (++tries < 50) setTimeout(trySeek, 100);  // up to ~5s for a fresh load
  })();
}

async function loadSynergy() {
  const card = $('synergyCard');
  if (DEMO) { card.style.display = 'none'; return; }
  let syn;
  const mySid = sid;
  try { syn = await api('/session/' + sid + '/synergy'); } catch (e) { card.style.display = 'none'; return; }
  if (sid !== mySid) return;   // user opened another session while this was in flight
  if (!syn || !syn.available) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  $('synergyStats').innerHTML =
    statCard(syn.avg_separation_ft + '<small> ft</small>', 'avg separation') +
    statCard(syn.min_separation_ft + '<small> ft</small>', 'closest moment') +
    statCard(syn.coverage_overlap_pct + '<small>%</small>', 'coverage overlap');
  animateStats($('synergyStats'));
  $('synergyTip').innerHTML = syn.tip ? tipSVG + '<span>' + esc(syn.tip) + '</span>' : '';
  $('synergyTip').style.display = syn.tip ? 'flex' : 'none';
}
async function loadBallTrajectory(sh) {
  const card = $('ballTrajCard');
  const bounces = (sh && sh.bounces_px) || [];
  if (!bounces.length || DEMO) { card.style.display = 'none'; return; }
  card.style.display = 'flex';
  const img = new Image();
  img.onload = () => drawBallTrajectory(img, bounces);
  img.src = frameURL();
}
function drawBallTrajectory(img, bounces) {
  const c = $('ballTrajCanvas'), ctx = c.getContext('2d');
  c.width = img.width; c.height = img.height;
  ctx.drawImage(img, 0, 0);
  const tMax = Math.max(1e-6, ...bounces.map(b => b.t));
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  bounces.forEach(b => {
    const alpha = 0.25 + 0.65 * (b.t / tMax);
    ctx.beginPath();
    ctx.arc(b.x, b.y, 5, 0, 7);
    ctx.fillStyle = accent;
    ctx.globalAlpha = alpha;
    ctx.fill();
    ctx.globalAlpha = 1;
  });
}

function renderRallies(ev) {
  const list = $('rallyList'), v = $('rallyVideo');
  list.before(v);   // playClip parks the player inside the list; rebuilding the list would destroy it
  v.pause(); v.style.display = 'none'; v.removeAttribute('src');
  if (!ev || !ev.rally_count) {
    $('rallyStats').innerHTML = '';
    list.innerHTML = '<div class="empty">No rallies detected' + (ev && ev.note ? ' — ' + esc(ev.note) : '') + '.</div>';
    return;
  }
  $('rallyStats').innerHTML =
    statCard(nz(ev.rally_count), 'rallies') +
    statCard(nz(ev.subject_shots), 'your shots') +
    statCard(nz(ev.avg_rally_hits), 'avg hits / rally') +
    statCard(nz(ev.max_rally_hits), 'longest rally') +
    statCard(nz(ev.avg_rally_seconds, ' s'), 'avg rally length') +
    statCard(nz(ev.play_time_pct, '%'), 'time in play');
  animateStats($('rallyStats'));
  const notes = (lastSession && lastSession.meta && lastSession.meta.clip_notes) || {};
  list.innerHTML = ev.rallies.map((r, i) =>
    '<div class="rally" id="rallyRow' + i + '" role="button" tabindex="0" aria-label="Watch rally ' + (i + 1) + '" onclick="playClip(' + i + ')">' +
      '<span class="idx">' + String(i+1).padStart(2,'0') + '</span>' +
      '<span class="info">' + r.hits + ' hits <span class="dur">· ' + r.duration + ' s · at ' + fmtClock(r.start) + '</span></span>' +
      '<span class="play">▶ watch</span></div>' +
    '<div class="clipnote" onclick="event.stopPropagation()">' +
      '<input id="clipNote' + i + '" placeholder="note on this rally…" value="' + esc(notes[i] || '') + '" ' +
        'onchange="saveClipNote(' + i + ')" style="width:100%;font-size:12px">' +
    '</div>').join('');
}
async function saveClipNote(i) {
  if (DEMO) { toast('Clip notes need the live backend'); return; }
  const note = $('clipNote' + i).value;
  try {
    await api('/session/' + sid + '/clip/' + i + '/note', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note }) });
    if (lastSession && lastSession.meta) lastSession.meta.clip_notes = { ...(lastSession.meta.clip_notes || {}), [i]: note };
    toast('Note saved');
  } catch (e) { toast('Save failed: ' + e, true); }
}
// "Fix…" menu choice -> [winner, was it an unforced error]. An explicit false clears a stale UE flag.
const FIXES = { won: ['my_team', false], won_ue: ['my_team', true], lost: ['opp_team', false],
                lost_ue: ['opp_team', true], unclear: ['unknown', false] };
async function fixPoint(idx, el) {
  const choice = el.value; el.value = '';
  if (!choice) return;
  if (DEMO) { toast('Correcting points needs the live backend'); return; }
  try {
    if (choice === 'undo') {
      await api('/session/' + sid + '/points/' + idx, { method: 'DELETE' });
    } else {
      const [winner, unforced_error] = FIXES[choice];
      await api('/session/' + sid + '/points/' + idx, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ winner, unforced_error }) });
    }
    toast(choice === 'undo' ? 'Rally ' + (idx + 1) + ' back to the analysis' : 'Rally ' + (idx + 1) + ' updated');
    const fresh = await api('/session/' + sid);
    if (lastSession && lastSession.session_id === sid) {
      lastSession = {...lastSession, points: fresh.points};
      renderPoints(fresh.points, lastSession.events);   // not openSession(): that re-renders everything and jumps the scroll
      refreshDerived();                                  // the strip, the clip lists and the deeper stats all follow the winner
    }
  } catch (e) { toast('Correction failed: ' + e, true); }
}
function fmtClock(s){ const m = Math.floor(s/60); return m + ':' + String(Math.floor(s%60)).padStart(2,'0'); }
function playClip(i) {
  if (DEMO) { toast('Clip playback needs the live backend'); return; }
  const v = $('rallyVideo');
  document.querySelectorAll('.rally').forEach(el => el.classList.remove('playing'));
  const row = $('rallyRow' + i); if (row) { row.classList.add('playing'); row.after(v); }
  v.style.display = 'block';
  v.src = BASE + '/api/session/' + sid + '/clip/' + i;
  v.play().catch(() => {});
}

/* ---- heatmap (blooms hottest-first) ---- */
let heatRAF = null;
function drawHeatmap(m, animate, cid) {
  cid = cid || 'heatCanvas';
  const grid = m.heatmap;
  if (!grid || !grid.length) { $(cid).style.display = 'none'; return; }
  $(cid).style.display = 'block';
  const rows = grid.length, cols = grid[0].length, S = 22, P = 24;
  const CW = cols * S + P * 2, CH = rows * S + P * 2;
  const c = $(cid), ctx = hiDpi(c, CW, CH);
  const V = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const sk = SKINS[currentSkin()] || SKINS.night;
  const H_SURF = V('--panel2'), H_KIT = 'rgba(' + sk.hi + ',.08)';
  const H_HI = sk.hi, H_MID = sk.mid, H_LO = sk.lo;
  const H_OUT = V('--line2'), H_LN = V('--line'), H_NET = V('--dim'), H_CEN = V('--line'), H_T1 = V('--muted'), H_T2 = V('--dim');
  const cell = m.heatmap_cell_ft || 2;
  const ftY = ft => P + (ft / (rows * cell)) * rows * S;
  const max = Math.max(1, ...grid.flat());
  const cells = [];
  for (let r = 0; r < rows; r++) for (let q = 0; q < cols; q++) { const v = grid[r][q] / max; if (v > 0) cells.push({ r, q, v }); }
  cells.sort((a, b) => b.v - a.v);            // hottest first
  const n = cells.length || 1;
  const reduce = noMotion();
  const DUR = (animate && !reduce) ? 950 : 0;
  const ease = t => 1 - Math.pow(1 - t, 3);
  cancelAnimationFrame(heatRAF);
  const t0 = performance.now();
  function frame(now) {
    const prog = DUR ? Math.min(1, (now - t0) / DUR) : 1;
    ctx.fillStyle = H_SURF; ctx.fillRect(0, 0, CW, CH);
    ctx.fillStyle = H_KIT; ctx.fillRect(P, ftY(15), cols * S, ftY(29) - ftY(15));
    cells.forEach((o, i) => {
      const start = (i / n) * 0.6;
      const local = DUR ? ease(Math.max(0, Math.min(1, (prog - start) / 0.4))) : 1;
      if (local <= 0) return;
      const a = (0.14 + 0.82 * Math.sqrt(o.v)) * local;
      const scale = 0.55 + 0.45 * local;
      ctx.fillStyle = 'rgba(' + (o.v > .66 ? H_HI : o.v > .33 ? H_MID : H_LO) + ',' + a + ')';
      const x = P + o.q * S, y = P + o.r * S, sz = (S - 2) * scale, off = (S - 2 - sz) / 2;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x + 1 + off, y + 1 + off, sz, sz, 4 * scale); else ctx.rect(x + 1 + off, y + 1 + off, sz, sz);
      ctx.fill();
    });
    ctx.strokeStyle = H_OUT; ctx.lineWidth = 2;
    ctx.strokeRect(P, P, cols * S, rows * S);
    [[15,H_LN,1.5],[29,H_LN,1.5],[22,H_NET,2.5]].forEach(([f, col, w]) => {
      ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath();
      ctx.moveTo(P, ftY(f)); ctx.lineTo(P + cols * S, ftY(f)); ctx.stroke();
    });
    ctx.strokeStyle = H_CEN; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(P + cols*S/2, P); ctx.lineTo(P + cols*S/2, ftY(15));
    ctx.moveTo(P + cols*S/2, ftY(29)); ctx.lineTo(P + cols*S/2, P + rows*S); ctx.stroke();
    ctx.fillStyle = H_T1; ctx.font = '700 11px Archivo, sans-serif'; ctx.textAlign = 'left';
    ctx.fillText('NET', P + cols * S + 4, ftY(22) + 4);
    ctx.save(); ctx.translate(13, ftY(18.5)); ctx.rotate(-Math.PI/2); ctx.textAlign='center';
    ctx.fillStyle = H_T2; ctx.fillText('KITCHEN', 0, 0); ctx.restore();
    if (prog < 1) heatRAF = requestAnimationFrame(frame);
  }
  heatRAF = requestAnimationFrame(frame);
}

function drawMovementCurve(curve) {
  const wrap = $('curveWrap');
  if (!curve || curve.length < 2) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  const Wd = 560, Hd = 140, P = 30;
  const c = $('curveCanvas'), ctx = hiDpi(c, Wd, Hd);
  const sp = curve.map(p => p.avg_speed_ft_s);
  c.setAttribute('role', 'img');
  c.setAttribute('aria-label', 'Movement speed over the session, ' + Math.min(...sp).toFixed(1) + ' to ' + Math.max(...sp).toFixed(1) + ' feet per second; it ' + (sp[sp.length - 1] < sp[0] ? 'slows' : 'picks up') + ' toward the end.');
  const V = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  ctx.clearRect(0, 0, Wd, Hd);
  const speeds = curve.map(p => p.avg_speed_ft_s);
  const maxS = Math.max(1, ...speeds), minS = Math.min(...speeds);
  const x = i => P + (i / (curve.length - 1)) * (Wd - P * 1.5);
  const y = v => P * 0.7 + (1 - (v - minS) / Math.max(1e-6, maxS - minS)) * (Hd - P * 1.7);   // max near the top, min on the axis
  ctx.strokeStyle = V('--line2'); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(P, Hd - P); ctx.lineTo(Wd - 10, Hd - P); ctx.stroke();
  ctx.strokeStyle = V('--accent'); ctx.lineWidth = 2.5; ctx.beginPath();
  curve.forEach((p, i) => { const px = x(i), py = y(p.avg_speed_ft_s); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.stroke();
  ctx.fillStyle = V('--accent');
  curve.forEach((p, i) => { ctx.beginPath(); ctx.arc(x(i), y(p.avg_speed_ft_s), 3, 0, 7); ctx.fill(); });
  ctx.fillStyle = V('--muted'); ctx.font = '600 10px Archivo, sans-serif'; ctx.textAlign = 'center';
  curve.forEach((p, i) => { if (i % Math.ceil(curve.length / 8 || 1) === 0) ctx.fillText(Math.round(p.t_start / 60) + 'm', x(i), Hd - 8); });
}
