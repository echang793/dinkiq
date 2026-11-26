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
  const morph = portalFrom && !noMotion();
  show('results', morph);
  if (morph) runPortal(portalFrom);
  else {
    const st = $('stinger');
    if (st && !noMotion()) {
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
    statCard(nz(zp.kitchen, '%'), 'time at kitchen line<span class="n">' + (m.zone_scope === 'live' && m.live_seconds != null ? 'of ' + Math.round(m.live_seconds) + ' s of live play' : 'of ' + Math.round(m.active_seconds || 0) + ' s tracked') + '</span>') +
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
  updateDq();
  loadInsights(s);
}

/* ---- DUPR reveal ---- */
const CONF_WORDS = {
  low:    ['Low confidence', 'A rough guess: too little clean footage to narrow it down.'],
  medium: ['Medium confidence', 'A reasonable read, but still a few bands wide.'],
  high:   ['High confidence', 'Enough clean footage for a fairly tight estimate.'],
};
function confLevel(d) { return d.confidence_level || (d.confidence < .33 ? 'low' : d.confidence < .66 ? 'medium' : 'high'); }
const fmtBand = v => v.toFixed(2).replace(/0$/, '');

function renderDupr(d) {
  const body = $('duprBody');
  $('dqBody').innerHTML = '';
  const noTips = '<div class="empty">No tips for this session.</div>';
  if (!d) {
    body.innerHTML = '<div class="empty" style="margin-top:8px">Skill estimate not computed for this session — use ⋯ → Re-analyze.</div>';
    $('tipBox').innerHTML = noTips; $('drillBox').style.display = 'none';
    return;
  }
  if (!d.available) {
    body.innerHTML = '<div class="caveat" style="margin-top:12px">' + warnSVG + '<span>' + esc(d.reason || 'Not enough signal for a skill estimate.') + '</span></div>' +
      '<div class="resemble" style="margin-top:14px">Your movement and rally stats below are still real — only the rating needs more footage.</div>';
    $('dqBody').innerHTML = (d.caveats || []).map(c => '<div class="caveat">' + warnSVG + '<span>' + esc(c) + '</span></div>').join('');
    $('tipBox').innerHTML = noTips; $('drillBox').style.display = 'none';
    return;
  }
  const lvl = confLevel(d), [lvlWord, lvlWhy] = CONF_WORDS[lvl];
  const lo = d.band_low != null ? d.band_low : d.band, hi = d.band_high != null ? d.band_high : d.band;
  // season high: strictly above every OTHER session (a tie is not a new best)
  let isHigh = false;
  try { const bs = (window._pcProg || []).filter(p => p.session_id !== sid).map(p => p.dupr_band).filter(v => v != null); isHigh = bs.length > 0 && d.band > Math.max.apply(null, bs); } catch (e) {}
  const dims = Object.values(d.dimensions || {});
  const weakest = dims.length ? dims.reduce((a, b) => b.band < a.band ? b : a) : null;
  const drill = d.drills && d.drills[0];
  const known = lastSession && lastSession.meta && lastSession.meta.known_dupr;
  const pos = v => Math.max(0, Math.min(100, (v - DUPR_BAND_MIN) / DUPR_BAND_SPAN * 100));

  let knownLine = '';
  if (known != null) {
    const diff = d.band - known;
    knownLine = '<div class="knownline">Your known DUPR is <b>' + Number(known).toFixed(2) + '</b> — the estimate is ' +
      (Math.abs(diff) < .05 ? 'right on it' : Math.abs(diff).toFixed(2) + (diff > 0 ? ' above' : ' below') + ' it') +
      (known > 5 ? ' <span class="mut">(this tool can only tell apart play up to about 5.0)</span>' : '') + '</div>';
  }
  const edge = d.at_ceiling ? 'At or above the top of what this tool can tell apart (about 5.0).'
    : d.at_floor ? 'At or below the bottom of what this tool can tell apart (about 2.5).' : '';

  body.innerHTML =
    '<div class="heroflex">' +
      '<div class="duprnum" id="duprNum"><small>~</small>' + fmtBand(d.band) + '</div>' +
      '<div class="rngline"><span>Likely range <b>' + fmtBand(lo) + ' – ' + fmtBand(hi) + '</b></span>' +
        '<span class="conflvl ' + lvl + '">' + lvlWord + '</span>' +
        (lvl !== 'high' ? '<button class="linkbtn" onclick="openDq()">Why?</button>' : '') + '</div>' +
      (drill
            ? '<div class="worknext"><div class="wk">Work on next</div>' +
                '<div class="wt">' + esc(drill.target_label || (weakest && weakest.label) || drill.name) + '</div>' +
                '<div class="wd"><b>' + esc(drill.name) + '</b> · ' + esc(drill.reps) + '<br>' + esc(drill.description) + '</div></div>'
            : '') +
      '<div class="resemble">Your play <b>resembles a ~' + fmtBand(d.band) + ' DUPR player</b>. ' + lvlWhy + ' This is a video pattern match, not a certified rating' +
        (d.calibrated ? '.' : ' — and it is <b>uncalibrated</b> (not yet checked against your known ratings).') + (edge ? ' ' + edge : '') + '</div>' +
      knownLine +
      '<div class="scale" role="img" aria-label="Estimate ' + fmtBand(d.band) + ' on a scale from 2.5 to 5.0, likely range ' + fmtBand(lo) + ' to ' + fmtBand(hi) + '"><div class="rail"></div>' +
        '<div class="rng" style="left:' + pos(lo) + '%;width:' + Math.max(1.5, pos(hi) - pos(lo)) + '%"></div>' +
        [2.5,3.0,3.5,4.0,4.5,5.0].map(t => '<span class="tick" style="left:' + pos(t) + '%">' + t.toFixed(1) + '</span>').join('') +
        '<div class="mk" id="duprMk"></div></div>' +
      (isHigh ? '<span class="pbbug">▲ SEASON HIGH — BEST ON RECORD</span>' : '') +
    '</div>' +
    '<div class="bars" id="duprDims" style="margin-top:22px">' +
      dims.slice().sort((a, b) => a.band - b.band).map(dim => {
        const pct = Math.max(4, Math.min(100, 100 * (dim.band - DUPR_BAND_MIN) / DUPR_BAND_SPAN));
        const weak = dim.band < d.band - .25;
        const target = dim.next_target_value != null
          ? '<div class="mut" style="font-size:11px;margin-top:1px">reach ' + dim.next_target_value + (dim.unit || '') +
            ' for the next tier (' + dim.next_target_band.toFixed(2) + ')</div>' : '';
        return '<div class="bar"><span class="lb">' + esc(dim.label) + (weak ? '<span class="weaktag">▼ below your level</span>' : '') + target + '</span>' +
          '<div class="track"><div class="fill" data-w="' + pct + '%" style="background:' + (weak ? 'var(--amber)' : 'var(--accent)') + '"></div></div>' +
          '<span class="val">' + dim.band.toFixed(1) + '</span></div>';
      }).join('') + '</div>';
  $('dqBody').innerHTML = (d.caveats || []).map(c => '<div class="caveat">' + warnSVG + '<span>' + esc(c) + '</span></div>').join('');

  // the reveal: count up + marker slide
  const numEl = $('duprNum'), to = d.band;
  const reduce = noMotion();
  if (!reduce) {
    const t0 = performance.now(), DUR = 1200, from = DUPR_BAND_MIN;
    const ease = t => 1 - Math.pow(1 - t, 3);
    (function stepFn(now) {
      const t = Math.min(1, (now - t0) / DUR);
      numEl.innerHTML = '<small>~</small>' + (from + (to - from) * ease(t)).toFixed(2);
      if (t < 1) requestAnimationFrame(stepFn); else numEl.innerHTML = '<small>~</small>' + fmtBand(to);
    })(t0);
  }
  requestAnimationFrame(() => requestAnimationFrame(() => { $('duprMk').style.left = pos(to) + '%'; }));
  if (isHigh) setTimeout(celebrate, 900);
  animateBars($('duprDims'));
  $('tipBox').innerHTML = (d.tips && d.tips.length)
    ? d.tips.map(t => '<div class="coach">' + tipSVG + '<span>' + esc(t) + '</span></div>').join('')
    : noTips;
  const more = (d.drills || []).slice(1), drillEl = $('drillBox');
  if (more.length) {
    drillEl.style.display = 'block';
    drillEl.innerHTML = '<div class="heatnote" style="margin-bottom:8px">More drills</div>' + more.map(dr =>
      '<div class="caveat" style="border-color:var(--accentline,var(--line2));margin-bottom:8px">' +
        '<div><b>Drill: ' + esc(dr.name) + '</b> <span class="mut">(' + esc(dr.reps) + ' — targets ' + esc(dr.target_label) + ')</span>' +
        '<div style="margin-top:6px">' + esc(dr.description) + '</div></div></div>').join('');
  } else {
    drillEl.style.display = 'none';
  }
}
function updateDq() {
  const n = $('metricWarn').children.length + $('dqBody').children.length;
  $('dqCount').textContent = n ? '· ' + n + (n === 1 ? ' note' : ' notes') : '· nothing flagged';
}
function openDq() { const el = $('dqCard'); el.open = true; el.scrollIntoView({behavior: 'smooth', block: 'center'}); }

/* ---- header actions ---- */
function toggleRMenu(e) {
  e.stopPropagation();
  const m = $('rMenu'), open = m.hidden;
  m.hidden = !open;
  $('rMoreBtn').setAttribute('aria-expanded', String(open));
  if (open) m.querySelector('button').focus();
}
function closeRMenu() { const m = $('rMenu'); if (m && !m.hidden) { m.hidden = true; $('rMoreBtn').setAttribute('aria-expanded', 'false'); } }
document.addEventListener('click', closeRMenu);
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeRMenu(); $('rMoreBtn') && $('rMoreBtn').focus(); } });

// One primary Share: the native share sheet with the stat-card image when the device
// supports it (phones do), otherwise just open the image.
async function shareResult() {
  if (DEMO) { toast('Sharing needs the live backend'); return; }
  const url = BASE + '/api/session/' + sid + '/card.png';
  try {
    const blob = await (await fetch(url)).blob();
    const file = new File([blob], 'dinkiq.png', {type: 'image/png'});
    if (navigator.canShare && navigator.canShare({files: [file]})) {
      await navigator.share({files: [file], title: 'My DinkIQ session'});
      return;
    }
  } catch (e) { if (e && e.name === 'AbortError') return; }
  downloadStatCard();
}
function identityOk() {
  try { localStorage.setItem('pc_idok_' + sid, '1'); } catch (e) {}
  markIdentityConfirmed();
}
function markIdentityConfirmed() {
  let ok = false; try { ok = !!localStorage.getItem('pc_idok_' + sid); } catch (e) {}
  $('idRow').style.display = ok ? 'none' : 'flex';
  const h = $('subjMarkerCard').querySelector('h2');
  if (h) h.textContent = ok ? 'You confirmed this player' : 'Was this you?';
}

function celebrate() {
  const hero = $('duprHero');
  if (!hero || noMotion()) return;
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
  if (has(pt.win_pct)) cards += statCard(pt.win_pct + '<small>%</small>', 'win rate<span class="n">of ' + pt.points_scored + ' point' + (pt.points_scored === 1 ? '' : 's') + '</span>');
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
    note += '<div class="momwrap"><div class="momlbl"><span>Momentum — point by point</span><span>green won · red lost · grey unclear · height = rally length</span></div><div class="momrow">' +
      outs.map((o, i) => {
        const h = hitsArr[i] ? hitsArr[i].hits : 4;
        const hh = Math.round(8 + 24 * Math.min(1, h / maxh));
        const col = isMyTeam(o.winner) ? 'var(--green)' : isOppTeam(o.winner) ? 'var(--red)' : 'var(--faint)';
        const t = 'Rally ' + (i + 1) + (isMyTeam(o.winner) ? ' — won' : isOppTeam(o.winner) ? ' — lost' : '') + (o.unforced_error ? ' (UE)' : '');
        return '<i class="mseg" role="img" aria-label="' + t + '" title="' + t + '" style="height:' + hh + 'px;background:' + col + ';animation-delay:' + (i * 55) + 'ms"></i>';
      }).join('') + '</div></div>';
  }
  if (outs.length) {
    const badge = o => isMyTeam(o.winner) ? '<span class="wbadge win">WON' + (o.winner === 'partner' ? ' (partner)' : '') + '</span>'
      : isOppTeam(o.winner) ? '<span class="wbadge loss">LOST</span>' : '';
    const fixBtns = (i, o) => DEMO ? '' :
      '<span class="ptfix" onclick="event.stopPropagation()"><select class="fixsel" aria-label="Fix the result of rally ' + (i + 1) + '" onchange="fixPoint(' + i + ', this)">' +
      '<option value="">Fix…</option><option value="won">I won it</option><option value="won_ue">I won — their error</option>' +
      '<option value="lost">I lost it</option><option value="lost_ue">I lost — my error</option><option value="unclear">Unclear</option>' +
      (o.corrected ? '<option value="undo">Undo my fix</option>' : '') + '</select></span>';
    note += '<div class="outcomes">' + outs.map((o, i) => {
      const ev2 = ev && ev.rallies && ev.rallies[i];
      return '<div class="orow"><span class="oidx">' + String(i+1).padStart(2,'0') + '</span>' +
        '<span class="oinfo">' + (ev2 ? ev2.hits + ' hits' : 'rally ' + (i+1)) +
        (o.server && o.server !== 'unknown' ? ' · ' + (isMyTeam(o.server) ? playerName(o.server) + ' served' : 'opp. served') : '') + '</span>' +
        (o.unforced_error ? '<span class="wbadge ue" title="Unforced error">UE</span>' : '') + badge(o) +
        (o.corrected ? '<span class="wbadge" style="background:var(--dim)">fixed</span>' : '') + fixBtns(i, o) + '</div>';
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
             statCard(sh.deep_serve_pct + '<small>%</small>', 'deep serves<span class="n">of ' + sh.serves_measured + ' serve' + (sh.serves_measured === 1 ? '' : 's') + '</span>');
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
  const S = 8, P = 20, W = 20, L = 44;  // court feet: width x length
  const CW = Math.round(W * S) + P * 2, CH = Math.round(L * S) + P * 2;
  const c = $('shotHeatCanvas'), ctx = hiDpi(c, CW, CH);
  const counts = {}; shots.forEach(s => { counts[s.type] = (counts[s.type] || 0) + 1; });
  c.setAttribute('role', 'img');
  c.setAttribute('aria-label', 'Where your shots were hit from on the court: ' + Object.entries(counts).map(([t, n]) => n + ' ' + (SHOT_TYPE_LABELS[t] || t).toLowerCase()).join(', ') + '.');
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--panel2').trim();
  ctx.fillRect(0, 0, CW, CH);
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
    const cx = (e.clientX - rect.left) * (CW / rect.width);
    const cy = (e.clientY - rect.top) * (CH / rect.height);
    let best = -1, bestD = matchMedia('(pointer:coarse)').matches ? 26 : 14;  // hit radius in canvas units (fingers are fat)
    shots.forEach((s, i) => {
      const d = Math.hypot(px(s.x) - cx, py(s.y) - cy);
      if (d < bestD) { bestD = d; best = i; }
    });
    if (best >= 0) watchShotAt(shots[best].t);
  };
}

/* ---- jump to a shot's moment inside its rally clip ---- */
