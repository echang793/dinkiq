'use strict';
/* ---------------- results ---------------- */
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// A missing value renders as an em dash, never as the words "null"/"undefined"/"NaN".
function nz(v, suffix, digits) {
  if (v == null || (typeof v === 'number' && !isFinite(v))) return '<span class="mut">—</span>';
  const x = (digits != null && typeof v === 'number') ? v.toFixed(digits) : v;
  return x + (suffix ? '<small>' + suffix + '</small>' : '');
}
function statCard(v, l) { return '<div class="stat"><div class="v">' + v + '</div><div class="l">' + l + '</div></div>'; }
const warnSVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
const tipSVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>';

function animateBars(container) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    container.querySelectorAll('.fill').forEach((f, i) => { f.style.transitionDelay = (i * 90) + 'ms'; f.style.width = f.dataset.w; });
  }));
}

async function showResults(s) {
  stopPoll();
  lastSession = s;
  const m = s.metrics;
  const morph = portalFrom && !matchMedia('(prefers-reduced-motion:reduce)').matches;
  show('results', morph);
  if (morph) runPortal(portalFrom);
  else {
    const st = $('stinger');
    if (st && !matchMedia('(prefers-reduced-motion:reduce)').matches) {
      $('stingerTitle').textContent = (s.meta && (s.meta.label || s.meta.played_at)) || 'Match breakdown';
      st.classList.remove('go'); void st.offsetWidth; st.classList.add('go');
      clearTimeout(st._h); st._h = setTimeout(() => st.classList.remove('go'), 1300);
    }
  }
  portalFrom = null;
  fillMeta(s.meta);
  if (!m) { $('stats').innerHTML = '<div class="empty">No metrics yet for this session.</div>'; return; }
  $('metricWarn').innerHTML = (m.warnings || []).map(w => '<div class="caveat red" style="margin-bottom:10px">' + warnSVG + '<span>' + esc(w) + '</span></div>').join('');
  const zp = m.zone_pct || {};
  $('stats').innerHTML =
    statCard(nz(zp.kitchen, '%'), 'time at kitchen line') +
    statCard(nz(m.distance_ft == null ? null : Math.round(m.distance_ft).toLocaleString(), ' ft'), 'distance covered') +
    statCard(nz(m.avg_speed_ft_s, ' ft/s'), 'avg movement speed') +
    statCard(nz(m.active_seconds == null ? null : Math.round(m.active_seconds / 60), ' min'), 'time analyzed') +
    statCard(nz(m.coverage_pct, '%'), 'court coverage') +
    statCard(nz(m.median_dist_from_net_ft, ' ft'), 'median dist from net');
  animateStats($('stats'));
  const zoneLabel = { kitchen: 'kitchen line', transition: "no-man's land", baseline: 'baseline' };
  $('zones').innerHTML = ['kitchen','transition','baseline'].map(z =>
    '<div class="bar"><span class="lb">' + zoneLabel[z] + '</span>' +
    '<div class="track"><div class="fill" data-w="' + (zp[z] || 0) + '%" style="background:' + (z==='transition' ? 'var(--amber)' : 'var(--accent)') + '"></div></div>' +
    '<span class="val">' + nz(zp[z], '%') + '</span></div>').join('');
  animateBars($('zones'));
  drawHeatmap(m, true);
  drawMovementCurve(m.movement_curve);
  loadSubjectMarker(sid);
  if (m.match_type === 'doubles') loadSynergy(); else $('synergyCard').style.display = 'none';
  renderRallies(s.events);
  renderShots(s.shots);
  renderPoints(s.points, s.events);
  renderDupr(s.dupr);
}

/* ---- DUPR reveal ---- */
function renderDupr(d) {
  const body = $('duprBody');
  if (!d) { body.innerHTML = '<div class="empty" style="margin-top:8px">Skill estimate not computed for this session — hit Re-analyze.</div>'; $('tipBox').innerHTML = '<div class="empty">No tips for this session.</div>'; $('drillBox').style.display = 'none'; return; }
  if (!d.available) {
    body.innerHTML = '<div class="caveat" style="margin-top:12px">' + warnSVG + '<span>' + esc(d.reason || 'Not enough signal for a skill estimate.') +
      (d.caveats || []).map(c => '<br>· ' + esc(c)).join('') + '</span></div>';
    $('tipBox').innerHTML = '<div class="empty">No tips for this session.</div>';
    $('drillBox').style.display = 'none';
    return;
  }
  const conf = Math.round(d.confidence * 100);
  let isHigh = false;
  try { const bs = (window._pcProg || []).filter(p => p.session_id !== sid).map(p => p.dupr_band).filter(v => v != null); isHigh = bs.length > 0 && d.band > Math.max.apply(null, bs); } catch(e){}
  const R = 26, CIRC = 2 * Math.PI * R;
  body.innerHTML =
    '<div class="heroflex">' +
      '<div class="duprnum" id="duprNum"><small>~</small>2.00</div>' +
      '<div class="resemble">Your play <b>resembles a ~' + d.band.toFixed(2).replace(/0$/,'') + ' DUPR player</b>. This is a video-based pattern match, not a certified rating.' +
        '<br><span class="mut" style="font-size:11.5px">DUPR is pickleball\'s standard skill rating, roughly 2.0 (brand new) to 8.0 (pro) — this tool can currently distinguish play up to about a 5.0.</span></div>' +
      '<div class="scale"><div class="rail"></div>' +
        [2.5,3.0,3.5,4.0,4.5,5.0].map(t => '<span class="tick" style="left:' + ((t-DUPR_BAND_MIN)/DUPR_BAND_SPAN*100) + '%">' + t.toFixed(1) + '</span>').join('') +
        '<div class="mk" id="duprMk"></div></div>' +
      '<div class="conf"><svg width="62" height="62" viewBox="0 0 62 62">' +
        '<circle cx="31" cy="31" r="' + R + '" fill="none" style="stroke:var(--panel2)" stroke-width="6"/>' +
        '<circle id="confRing" cx="31" cy="31" r="' + R + '" fill="none" stroke-width="6" stroke-linecap="round" transform="rotate(-90 31 31)" stroke-dasharray="' + CIRC + '" stroke-dashoffset="' + CIRC + '" style="stroke:var(--accent);transition:stroke-dashoffset 1.4s cubic-bezier(.22,1,.36,1) .5s"/>' +
        '<text x="31" y="36" text-anchor="middle" font-size="13" font-family="Archivo Black, sans-serif" style="fill:var(--ink)">' + conf + '</text></svg>' +
      '<div><div class="cv">' + conf + '% confidence</div><div class="cl">How sure the model is, given<br>footage quality &amp; sample size</div></div></div>' +
      (isHigh ? '<span class="pbbug">▲ SEASON HIGH — BEST ON RECORD</span>' : '') +
    '</div>' +
    '<div style="display:flex;gap:26px;align-items:center;flex-wrap:wrap;margin-top:22px">' + radarSVG(d.dimensions) +
    '<div class="bars" id="duprDims" style="flex:1;min-width:250px">' +
      Object.values(d.dimensions || {}).sort((a,b) => a.band - b.band).map(dim => {
        const pct = Math.max(4, Math.min(100, 100 * (dim.band - DUPR_BAND_MIN) / DUPR_BAND_SPAN));
        const col = dim.band < d.band - .25 ? 'var(--amber)' : 'var(--accent)';
        const target = dim.next_target_value != null
          ? '<div class="mut" style="font-size:10px;margin-top:1px">reach ' + dim.next_target_value + (dim.unit || '') +
            ' for the next tier (' + dim.next_target_band.toFixed(2) + ')</div>' : '';
        return '<div class="bar"><span class="lb">' + esc(dim.label) + target + '</span>' +
          '<div class="track"><div class="fill" data-w="' + pct + '%" style="background:' + col + '"></div></div>' +
          '<span class="val">' + dim.band.toFixed(1) + '</span></div>';
      }).join('') + '</div></div>' +
    (d.caveats && d.caveats.length ? d.caveats.map(c => '<div class="caveat">' + warnSVG + '<span>' + esc(c) + '</span></div>').join('') : '');
  // the reveal: count up + marker slide + ring draw
  const numEl = $('duprNum'), t0 = performance.now(), DUR = 1700, from = DUPR_BAND_MIN, to = d.band;
  const ease = t => 1 - Math.pow(1 - t, 3);
  (function stepFn(now) {
    const t = Math.min(1, (now - t0) / DUR);
    const v = from + (to - from) * ease(t);
    numEl.innerHTML = '<small>~</small>' + v.toFixed(2);
    if (t < 1) requestAnimationFrame(stepFn);
    else { numEl.style.transition = 'transform .3s cubic-bezier(.34,1.56,.64,1)'; numEl.style.transform = 'scale(1.06)'; setTimeout(() => { numEl.style.transform = ''; }, 300); }
  })(t0);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    $('duprMk').style.left = ((to - DUPR_BAND_MIN) / DUPR_BAND_SPAN * 100) + '%';
    $('confRing').style.strokeDashoffset = CIRC * (1 - d.confidence);
  }));
  if (isHigh) setTimeout(celebrate, 900);
  animateBars($('duprDims'));
  $('tipBox').innerHTML = (d.tips && d.tips.length)
    ? d.tips.map(t => '<div class="coach">' + tipSVG + '<span>' + esc(t) + '</span></div>').join('')
    : '<div class="empty">No tips for this session.</div>';
  const drillEl = $('drillBox');
  if (d.drills && d.drills.length) {
    drillEl.style.display = 'block';
    drillEl.innerHTML = d.drills.map(dr =>
      '<div class="caveat" style="border-color:var(--accentline,var(--line2));margin-bottom:8px">' +
        '<div><b>Drill: ' + esc(dr.name) + '</b> <span class="mut">(' + esc(dr.reps) + ' — targets ' + esc(dr.target_label) + ')</span>' +
        '<div style="margin-top:6px">' + esc(dr.description) + '</div></div></div>').join('');
  } else {
    drillEl.style.display = 'none';
  }
}

function celebrate() {
  const hero = $('duprHero');
  if (!hero || matchMedia('(prefers-reduced-motion:reduce)').matches) return;
  const cols = ['var(--accent)', 'var(--accent2)', '#fff', 'var(--red)', 'var(--green)'];
  for (let i = 0; i < 36; i++) {
    const b = document.createElement('i');
    b.className = 'confbit';
    const sz = 5 + Math.random() * 7;
    b.style.cssText = 'left:' + (Math.random() * 100) + '%;width:' + sz + 'px;height:' + (sz * .6) + 'px;background:' + cols[i % cols.length];
    hero.appendChild(b);
    b.animate([
      { transform: 'translateY(0) rotate(0deg)', opacity: 1 },
      { transform: 'translateY(' + (hero.offsetHeight + 40) + 'px) rotate(' + Math.round(Math.random() * 720 - 360) + 'deg)', opacity: .9 }
    ], { duration: 1400 + Math.random() * 1200, delay: Math.random() * 350, easing: 'cubic-bezier(.3,.4,.6,1)', fill: 'forwards' }).onfinish = () => b.remove();
  }
}
function downloadReport() {
  if (DEMO) { toast('PDF report needs the live backend'); return; }
  window.open(BASE + '/api/session/' + sid + '/report.pdf', '_blank');
}
function downloadHighlights() {
  if (DEMO) { toast('Highlight reel needs the live backend'); return; }
  toast('Building highlight reel…');
  window.open(BASE + '/api/session/' + sid + '/highlights.mp4', '_blank');
}
function downloadStatCard() {
  if (DEMO) { toast('Stat card needs the live backend'); return; }
  window.open(BASE + '/api/session/' + sid + '/card.png', '_blank');
}
const PLAYER_LABELS = { subject: 'you', partner: 'partner', opponent1: 'opp1', opponent2: 'opp2' };
function isMyTeam(label) { return label === 'my_team' || label === 'subject' || label === 'partner'; }
function isOppTeam(label) { return label === 'opp_team' || label === 'opponent1' || label === 'opponent2'; }
function playerName(label) { return PLAYER_LABELS[label] || label; }
function renderPoints(pt, ev) {
  $('pointStats').innerHTML = ''; $('pointBars').innerHTML = ''; $('pointNote').innerHTML = '';
  if (!pt) { $('pointNote').innerHTML = '<div class="empty">Point scoring not run for this session.</div>'; return; }
  const has = v => v != null;
  let cards = '';
  if (has(pt.points_scored)) cards += statCard(pt.points_scored, 'points played');
  if (has(pt.points_won)) cards += statCard(pt.points_won, 'points won');
  if (has(pt.points_lost)) cards += statCard(pt.points_lost, 'points lost');
  if (has(pt.win_pct)) cards += statCard(pt.win_pct + '<small>%</small>', 'win rate');
  if (has(pt.unforced_errors)) cards += statCard(pt.unforced_errors, 'unforced errors');
  $('pointStats').innerHTML = cards || '<div class="empty">Not enough scored rallies to tally points.</div>';
  animateStats($('pointStats'));
  const bars = [['serve_win_pct','Serve points won'],['return_win_pct','Return points won']]
    .filter(([k]) => has(pt[k]))
    .map(([k,l]) => '<div class="bar"><span class="lb">' + l + '</span>' +
      '<div class="track"><div class="fill" data-w="' + pt[k] + '%" style="background:var(--accent)"></div></div>' +
      '<span class="val">' + pt[k] + '%</span></div>').join('');
  $('pointBars').innerHTML = bars;
  animateBars($('pointBars'));
  let note = pt.caveat ? '<div class="caveat">' + warnSVG + '<span>' + esc(pt.caveat) + '</span></div>' : '';
  const outs = pt.outcomes || [];
  if (outs.length > 1) {
    const hitsArr = (ev && ev.rallies) || [];
    const maxh = Math.max(1, ...hitsArr.map(r => r.hits || 0));
    note += '<div class="momwrap"><div class="momlbl"><span>Momentum — point by point</span><span>height = rally length</span></div><div class="momrow">' +
      outs.map((o, i) => {
        const h = hitsArr[i] ? hitsArr[i].hits : 4;
        const hh = Math.round(8 + 24 * Math.min(1, h / maxh));
        const col = isMyTeam(o.winner) ? 'var(--green)' : isOppTeam(o.winner) ? 'var(--red)' : 'var(--faint)';
        const t = 'Rally ' + (i + 1) + (isMyTeam(o.winner) ? ' — won' : isOppTeam(o.winner) ? ' — lost' : '') + (o.unforced_error ? ' (UE)' : '');
        return '<i class="mseg" title="' + t + '" style="height:' + hh + 'px;background:' + col + ';animation-delay:' + (i * 55) + 'ms"></i>';
      }).join('') + '</div></div>';
  }
  if (outs.length) {
    const badge = o => isMyTeam(o.winner) ? '<span class="wbadge win">WON' + (o.winner === 'partner' ? ' (partner)' : '') + '</span>'
      : isOppTeam(o.winner) ? '<span class="wbadge loss">LOST</span>' : '';
    const fixBtns = i => DEMO ? '' :
      '<span class="ptfix" onclick="event.stopPropagation()">' +
      '<button class="ghost sm" style="padding:3px 8px;font-size:10px" onclick="correctPoint(' + i + ',\'my_team\')">won</button>' +
      '<button class="ghost sm" style="padding:3px 8px;font-size:10px" onclick="correctPoint(' + i + ',\'opp_team\')">lost</button></span>';
    note += '<div class="outcomes">' + outs.map((o, i) => {
      const ev2 = ev && ev.rallies && ev.rallies[i];
      return '<div class="orow"><span class="oidx">' + String(i+1).padStart(2,'0') + '</span>' +
        '<span class="oinfo">' + (ev2 ? ev2.hits + ' hits' : 'rally ' + (i+1)) +
        (o.server && o.server !== 'unknown' ? ' · ' + (isMyTeam(o.server) ? playerName(o.server) + ' served' : 'opp. served') : '') + '</span>' +
        (o.unforced_error ? '<span class="wbadge ue" title="Unforced error">UE</span>' : '') + badge(o) +
        (o.corrected ? '<span class="wbadge" style="background:var(--dim)">fixed</span>' : '') + fixBtns(i) + '</div>';
    }).join('') + '</div>';
  }
  const hbp = pt.hits_by_player || {};
  if (Object.keys(hbp).length > 2) {  // singles is at most subject+opponent1 — only show for doubles
    note += '<div class="bars" style="margin-top:14px">' + Object.keys(hbp).map(k =>
      '<div class="bar"><span class="lb">' + playerName(k) + '</span>' +
      '<div class="track"><div class="fill" data-w="' + hbp[k] + '" style="background:var(--accent);width:' +
        Math.round(100 * hbp[k] / Math.max(1, ...Object.values(hbp))) + '%"></div></div>' +
      '<span class="val">' + hbp[k] + '</span></div>').join('') + '</div>';
  }
  const serveRates = pt.serve_side_win_rates || {};
  if (Object.keys(serveRates).length) {
    note += '<div class="bars" style="margin-top:14px"><div class="heatnote" style="margin-bottom:6px">win rate by serve placement</div>' +
      rateBars(serveRates, ['left', 'middle', 'right']) + '</div>';
  }
  const shotTypeRates = pt.shot_type_outcomes || {};
  if (Object.keys(shotTypeRates).length) {
    note += '<div class="bars" style="margin-top:14px"><div class="heatnote" style="margin-bottom:6px">win rate by shot type (rallies you ended)</div>' +
      rateBars(shotTypeRates, ['drive', 'dink', 'drop', 'medium', 'unknown'], k => SHOT_TYPE_LABELS[k] || k) + '</div>';
  }
  $('pointNote').innerHTML = note;
}
function rateBars(rates, order, labelFn) {
  const keys = (order || Object.keys(rates)).filter(k => rates[k]);
  return keys.map(k => {
    const r = rates[k];
    const lbl = labelFn ? labelFn(k) : k;
    return '<div class="bar"><span class="lb">' + esc(lbl) + '</span>' +
      '<div class="track"><div class="fill" data-w="' + r.win_pct + '%" style="background:var(--accent)"></div></div>' +
      '<span class="val">' + r.win_pct + '%<small> (' + r.attempts + ')</small></span></div>';
  }).join('');
}
function renderShots(sh) {
  $('shotStats').innerHTML = ''; $('shotMix').innerHTML = ''; $('shotNote').innerHTML = '';
  if (!sh) {
    $('shotNote').innerHTML = '<div class="empty">Shot analysis not run for this session.</div>';
    $('ballTrajCard').style.display = 'none';
    return;
  }
  loadBallTrajectory(sh);  // bounces may exist even when shot classification is unavailable
  if (!sh.available) {
    $('shotNote').innerHTML = '<div class="caveat">' + warnSVG + '<span>' + esc(sh.reason || 'Ball could not be tracked') +
      ' — the ball was hard to track in this footage. Positioning and rally stats are unaffected.</span></div>';
    return;
  }
  const mix = sh.shot_mix || {}, total = Object.values(mix).reduce((a,b) => a+b, 0);
  let cards = statCard(total, 'classified shots');
  if (sh.serves_measured) {
    cards += statCard(sh.avg_serve_depth_from_baseline_ft + '<small> ft</small>', 'avg serve dist from baseline') +
             statCard(sh.deep_serve_pct + '<small>%</small>', 'deep serves');
  }
  if (sh.avg_shot_mph != null) {
    cards += statCard(sh.avg_shot_mph + '<small> mph</small>', 'avg shot speed') +
             statCard(sh.top_shot_mph + '<small> mph</small>', 'top shot speed');
  }
  if (sh.avg_swing_mph != null) {
    cards += statCard(sh.avg_swing_mph + '<small> mph</small>', 'avg swing speed');
  }
  cards += statCard(Math.round((sh.ball && sh.ball.coverage || 0) * 100) + '<small>%</small>', 'ball track coverage');
  $('shotStats').innerHTML = cards;
  animateStats($('shotStats'));
  const order = ['drive','dink','drop','medium','unknown'];
  const cols = { drive:'var(--accent)', dink:'var(--accent2)', drop:'var(--accent3)', medium:'var(--amber)', unknown:'var(--faint)' };
  $('shotMix').innerHTML = order.filter(k => mix[k]).map(k => {
    const pct = total ? Math.round(100 * mix[k] / total) : 0;
    return '<div class="bar"><span class="lb">' + SHOT_TYPE_LABELS[k] + '</span>' +
      '<div class="track"><div class="fill" data-w="' + pct + '%" style="background:' + cols[k] + '"></div></div>' +
      '<span class="val">' + mix[k] + '</span></div>';
  }).join('');
  animateBars($('shotMix'));

  const withPos = (sh.shots || []).filter(s => s.x != null && s.y != null);
  const vizWrap = $('shotVizWrap');
  if (withPos.length) {
    vizWrap.style.display = 'grid';
    drawShotHeatmap(withPos);
  } else {
    vizWrap.style.display = 'none';
  }
  const serveCard = $('serveVizCard');
  if (sh.serve_side_counts && Object.keys(sh.serve_side_counts).length) {
    vizWrap.style.display = 'grid';
    serveCard.style.display = 'flex';
    const counts = sh.serve_side_counts, max = Math.max(1, ...Object.values(counts));
    $('serveMapBars').innerHTML = ['left','middle','right'].map(side => {
      const n = counts[side] || 0;
      return '<div class="bar"><span class="lb">' + side + '</span>' +
        '<div class="track"><div class="fill" data-w="' + Math.round(100 * n / max) + '%" style="background:var(--accent)"></div></div>' +
        '<span class="val">' + n + '</span></div>';
    }).join('');
    animateBars($('serveMapBars'));
  } else {
    serveCard.style.display = 'none';
  }
  if (sh.top_shot_t != null && !DEMO) {
    $('shotNote').innerHTML = '<div class="link" style="margin-top:10px;cursor:pointer" ' +
      'onclick="watchShotAt(' + sh.top_shot_t + ')" tabindex="0" role="button">▶ Watch your fastest shot (' +
      sh.top_shot_mph + ' mph)</div>';
  }
}
const SHOT_TYPE_COLORS = { drive: '#5b82e8', dink: '#39d5ff', drop: '#f0a63d', medium: '#8b6bf0', unknown: '#6b7280' };
const SHOT_TYPE_LABELS = { drive:'Drive', dink:'Dink', drop:'Drop', medium:'Medium-paced', unknown:'Not classified' };
function drawShotHeatmap(shots) {
  const c = $('shotHeatCanvas'), ctx = c.getContext('2d');
  const S = 8, P = 20, W = 20, L = 44;  // court feet: width x length
  c.width = Math.round(W * S) + P * 2; c.height = Math.round(L * S) + P * 2;
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--panel2').trim();
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--line2').trim();
  ctx.lineWidth = 2;
  ctx.strokeRect(P, P, W * S, L * S);
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--dim').trim();
  ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.moveTo(P, P + 22 * S); ctx.lineTo(P + W * S, P + 22 * S); ctx.stroke();
  const px = x => P + Math.max(0, Math.min(W, x)) * S;
  const py = y => P + Math.max(0, Math.min(L, y)) * S;
  shots.forEach(s => {
    ctx.beginPath();
    ctx.arc(px(s.x), py(s.y), 4, 0, 7);
    ctx.fillStyle = SHOT_TYPE_COLORS[s.type] || SHOT_TYPE_COLORS.unknown;
    ctx.globalAlpha = 0.85;
    ctx.fill();
    ctx.globalAlpha = 1;
  });
  const types = [...new Set(shots.map(s => s.type))];
  $('shotHeatLegend').innerHTML = types.map(t =>
    '<span style="display:inline-flex;align-items:center;gap:5px;margin-right:12px">' +
    '<span style="width:9px;height:9px;border-radius:50%;background:' + (SHOT_TYPE_COLORS[t] || SHOT_TYPE_COLORS.unknown) + ';display:inline-block"></span>' +
    esc(SHOT_TYPE_LABELS[t] || t) + '</span>').join('') +
    (DEMO ? '' : '<span class="mut" style="margin-left:8px">· tap a dot to watch that shot</span>');
  c.style.cursor = DEMO ? '' : 'pointer';
  c.onclick = DEMO ? null : (e) => {
    const rect = c.getBoundingClientRect();
    const cx = (e.clientX - rect.left) * (c.width / rect.width);
    const cy = (e.clientY - rect.top) * (c.height / rect.height);
    let best = -1, bestD = 14;  // px hit radius in canvas space
    shots.forEach((s, i) => {
      const d = Math.hypot(px(s.x) - cx, py(s.y) - cy);
      if (d < bestD) { bestD = d; best = i; }
    });
    if (best >= 0) watchShotAt(shots[best].t);
  };
}

/* ---- jump to a shot's moment inside its rally clip ---- */
